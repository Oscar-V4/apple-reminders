#!/usr/bin/env python3
"""Run local helpers with bounded output and POSIX process-group containment."""

from __future__ import annotations

import codecs
import errno
import math
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal, Sequence


OutputMode = Literal["utf8", "bytes"]
StreamName = Literal["stdout", "stderr"]
Arg = str | os.PathLike[str]

_READ_SIZE = 64 * 1024
_POLL_SECONDS = 0.01
_TERM_GRACE_SECONDS = 0.15


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str | bytes
    stderr: str | bytes


class ProcessError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        argv: tuple[str, ...],
        pid: int | None = None,
        returncode: int | None = None,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        super().__init__(message)
        self.argv = argv
        self.pid = pid
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ProcessLaunchError(ProcessError):
    def __init__(self, *, argv: tuple[str, ...], cause: OSError) -> None:
        # argv/cause can contain local paths; keep the message fixed.
        super().__init__("process could not be launched", argv=argv)
        self.cause = cause


class ProcessTimeoutError(ProcessError):
    def __init__(self, *, timeout_s: float, **context: object) -> None:
        super().__init__(
            f"process exceeded the {timeout_s:g}-second timeout",
            **context,  # type: ignore[arg-type]
        )
        self.timeout_s = timeout_s


class ProcessOutputLimitError(ProcessError):
    def __init__(
        self,
        *,
        stream: StreamName,
        limit: int,
        **context: object,
    ) -> None:
        super().__init__(
            f"process {stream} exceeded the {limit}-byte limit",
            **context,  # type: ignore[arg-type]
        )
        self.stream = stream
        self.limit = limit


class ProcessDecodeError(ProcessError):
    def __init__(
        self,
        *,
        stream: StreamName,
        cause: UnicodeDecodeError,
        **context: object,
    ) -> None:
        super().__init__(
            f"process {stream} was not valid UTF-8",
            **context,  # type: ignore[arg-type]
        )
        self.stream = stream
        self.cause = cause


class ProcessIOError(ProcessError):
    def __init__(
        self,
        *,
        stream: StreamName,
        cause: OSError,
        **context: object,
    ) -> None:
        super().__init__(
            f"process {stream} could not be read",
            **context,  # type: ignore[arg-type]
        )
        self.stream = stream
        self.cause = cause


@dataclass
class _StreamState:
    name: StreamName
    pipe: IO[bytes]
    limit: int
    validate_utf8: bool
    data: bytearray
    output_exceeded: bool = False
    decode_cause: UnicodeDecodeError | None = None
    io_cause: OSError | None = None


def _normalize_argv(argv: Sequence[Arg]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)):
        raise TypeError("argv must be a sequence of text arguments")
    normalized = tuple(os.fspath(value) for value in argv)
    if not normalized:
        raise ValueError("argv must contain at least one executable")
    if any(not isinstance(value, str) for value in normalized):
        raise TypeError("argv entries must be str or text PathLike values")
    if "\0" in "".join(normalized):
        raise ValueError("argv entries must not contain NUL bytes")
    return normalized


def _validate_options(
    *,
    timeout_s: float,
    stdout_limit: int,
    stderr_limit: int,
    output: OutputMode,
) -> None:
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or timeout_s <= 0
    ):
        raise ValueError("timeout_s must be a finite positive number")
    for name, value in (
        ("stdout_limit", stdout_limit),
        ("stderr_limit", stderr_limit),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    if output not in ("utf8", "bytes"):
        raise ValueError("output must be 'utf8' or 'bytes'")


def _drain_stream(
    state: _StreamState,
    failure_event: threading.Event,
) -> None:
    decoder = (
        codecs.getincrementaldecoder("utf-8")(errors="strict")
        if state.validate_utf8
        else None
    )
    try:
        while True:
            try:
                chunk = os.read(state.pipe.fileno(), _READ_SIZE)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                state.io_cause = exc
                failure_event.set()
                return
            if not chunk:
                if decoder is not None:
                    try:
                        decoder.decode(b"", final=True)
                    except UnicodeDecodeError as exc:
                        state.decode_cause = exc
                        failure_event.set()
                return

            remaining = state.limit - len(state.data)
            if remaining > 0:
                state.data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                state.output_exceeded = True
                failure_event.set()
                return

            if decoder is not None:
                try:
                    decoder.decode(chunk, final=False)
                except UnicodeDecodeError as exc:
                    state.decode_cause = exc
                    failure_event.set()
                    return
    finally:
        try:
            state.pipe.close()
        except OSError:
            pass


def _write_input(pipe: IO[bytes], payload: bytes) -> None:
    try:
        pipe.write(payload)
        pipe.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        # Fall back to the leader if platform policy rejects killpg.
        try:
            os.kill(pgid, sig)
        except ProcessLookupError:
            pass


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _contain_and_reap(process: subprocess.Popen[bytes]) -> int:
    """Terminate every member of the new session and reap its leader."""

    pgid = process.pid
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + _TERM_GRACE_SECONDS
    while time.monotonic() < deadline and _group_exists(pgid):
        time.sleep(_POLL_SECONDS)
    if _group_exists(pgid):
        _signal_group(pgid, signal.SIGKILL)

    try:
        return process.wait(timeout=_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(pgid, signal.SIGKILL)
        return process.wait()


def _context(
    *,
    argv: tuple[str, ...],
    process: subprocess.Popen[bytes],
    stdout: _StreamState,
    stderr: _StreamState,
) -> dict[str, object]:
    return {
        "argv": argv,
        "pid": process.pid,
        "returncode": process.returncode,
        "stdout": bytes(stdout.data),
        "stderr": bytes(stderr.data),
    }


def run(
    argv: Sequence[Arg],
    *,
    input: bytes | None = None,
    cwd: Path | None = None,
    timeout_s: float,
    stdout_limit: int,
    stderr_limit: int,
    output: OutputMode = "utf8",
) -> ProcessResult:
    """Execute ``argv`` under explicit time and output budgets."""

    normalized_argv = _normalize_argv(argv)
    _validate_options(
        timeout_s=timeout_s,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
        output=output,
    )
    if input is not None and not isinstance(input, bytes):
        raise TypeError("input must be bytes or None")

    try:
        process = subprocess.Popen(
            normalized_argv,
            shell=False,
            start_new_session=True,
            cwd=cwd,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ProcessLaunchError(argv=normalized_argv, cause=exc) from exc

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_state = _StreamState(
        "stdout", process.stdout, stdout_limit, output == "utf8", bytearray()
    )
    stderr_state = _StreamState(
        "stderr", process.stderr, stderr_limit, output == "utf8", bytearray()
    )
    failure_event = threading.Event()
    readers = [
        threading.Thread(
            target=_drain_stream,
            args=(state, failure_event),
            name=f"bounded-process-{state.name}",
            daemon=True,
        )
        for state in (stdout_state, stderr_state)
    ]
    for reader in readers:
        reader.start()

    writer: threading.Thread | None = None
    if input is not None:
        assert process.stdin is not None
        writer = threading.Thread(
            target=_write_input,
            args=(process.stdin, input),
            name="bounded-process-stdin",
            daemon=True,
        )
        writer.start()

    deadline = time.monotonic() + timeout_s
    failure_kind: Literal["stream", "timeout"] | None = None
    while True:
        if failure_event.is_set():
            failure_kind = "stream"
            break
        if process.poll() is not None:
            break
        if time.monotonic() >= deadline:
            failure_kind = "timeout"
            break
        failure_event.wait(min(_POLL_SECONDS, max(0.0, deadline - time.monotonic())))

    returncode = _contain_and_reap(process)
    if writer is not None:
        writer.join()
    for reader in readers:
        reader.join()

    context = _context(
        argv=normalized_argv,
        process=process,
        stdout=stdout_state,
        stderr=stderr_state,
    )
    if failure_kind == "timeout":
        raise ProcessTimeoutError(timeout_s=timeout_s, **context)

    for state in (stdout_state, stderr_state):
        if state.io_cause is not None:
            raise ProcessIOError(
                stream=state.name,
                cause=state.io_cause,
                **context,
            )
        if state.decode_cause is not None:
            raise ProcessDecodeError(
                stream=state.name,
                cause=state.decode_cause,
                **context,
            )
        if state.output_exceeded:
            raise ProcessOutputLimitError(
                stream=state.name,
                limit=state.limit,
                **context,
            )

    stdout_bytes = bytes(stdout_state.data)
    stderr_bytes = bytes(stderr_state.data)
    if output == "utf8":
        # The incremental readers already validated these exact bounded bytes.
        stdout_value: str | bytes = stdout_bytes.decode("utf-8")
        stderr_value: str | bytes = stderr_bytes.decode("utf-8")
    else:
        stdout_value = stdout_bytes
        stderr_value = stderr_bytes

    return ProcessResult(
        returncode=returncode,
        stdout=stdout_value,
        stderr=stderr_value,
    )


__all__ = [
    "ProcessDecodeError",
    "ProcessError",
    "ProcessIOError",
    "ProcessLaunchError",
    "ProcessOutputLimitError",
    "ProcessResult",
    "ProcessTimeoutError",
    "run",
]
