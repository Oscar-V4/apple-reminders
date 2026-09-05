#!/usr/bin/env python3
"""Opt-in live smoke test for the installable Apple Reminders public MCP surface."""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import os
import select
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, TextIO


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
SERVER_PATH = PLUGIN_ROOT / "mcp" / "server.py"
SYNTHETIC_LIST_PREFIX = "Codex-Apple-Reminders-Live-Smoke-"
MCP_TIMEOUT_SECONDS = 60.0
SERVER_ENV_DENYLIST = {
    "APPLE_REMINDERS_MCP_TEST_MODE",
    "APPLE_REMINDERS_ADAPTER_PATH",
    "APPLE_REMINDERS_EVENTKIT_BRIDGE_PATH",
    "APPLE_REMINDERS_DOCTOR_PATH",
    "APPLE_REMINDERS_TEST_HARNESS_ADAPTER_PATH",
    "APPLE_REMINDERS_TEST_HARNESS_EVENTKIT_BRIDGE_PATH",
    "APPLE_REMINDERS_TEST_HARNESS_DOCTOR_PATH",
}

CLEANUP_SCRIPT = r'''
on run argv
  if (count of argv) is not 2 then return "invalid_arguments"
  set expectedName to item 1 of argv
  set expectedID to item 2 of argv
  tell application "Reminders"
    set namedLists to every list whose name is expectedName
    if (count of namedLists) is 0 then return "absent"
    if (count of namedLists) is not 1 then return "not_unique"
    set targetList to item 1 of namedLists
    set resolvedID to (id of targetList) as text
    if resolvedID is not expectedID then return "id_mismatch"
    delete targetList
    delay 1
    set remainingNamedLists to every list whose name is expectedName
    repeat with candidateList in remainingNamedLists
      if ((id of candidateList) as text) is expectedID then return "still_present"
    end repeat
    return "deleted"
  end tell
end run
'''.strip()


class SmokeFailure(RuntimeError):
    """The disposable live workflow could not be proven safe and complete."""


class ToolClient(Protocol):
    def call_tool(
        self, name: str, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


class McpStdioClient:
    """Minimal newline-delimited JSON-RPC client for the installable MCP runtime."""

    def __init__(
        self,
        *,
        server_path: Path = SERVER_PATH,
        plugin_root: Path = PLUGIN_ROOT,
        timeout_seconds: float = MCP_TIMEOUT_SECONDS,
        experimental: bool = False,
        bundled_runtime: bool = False,
    ) -> None:
        self._server_path = server_path.resolve()
        self._plugin_root = plugin_root.resolve()
        self._timeout_seconds = timeout_seconds
        self._experimental = experimental
        self._bundled_runtime = bundled_runtime
        self._process: subprocess.Popen[str] | None = None
        self._next_id = 1

    def __enter__(self) -> McpStdioClient:
        if not self._server_path.is_file():
            raise SmokeFailure("the packaged MCP server is missing")
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        for name in SERVER_ENV_DENYLIST:
            environment.pop(name, None)
        if self._bundled_runtime:
            launcher = self._plugin_root / "scripts" / "launch_bundled_mcp.sh"
            if launcher.is_symlink() or not launcher.is_file():
                raise SmokeFailure("the packaged bundled-runtime launcher is missing or unsafe")
            command = ["/bin/sh", str(launcher)]
        else:
            command = [sys.executable, str(self._server_path)]
        if self._experimental:
            command.append("--experimental")
        self._process = subprocess.Popen(
            command,
            cwd=self._plugin_root,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        try:
            result = self._request(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "apple-reminders-live-smoke",
                        "version": "1",
                    },
                },
            )
            if not isinstance(result.get("serverInfo"), Mapping):
                raise SmokeFailure("the MCP initialize response was incomplete")
            self._notify("notifications/initialized", {})
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _streams(self) -> tuple[subprocess.Popen[str], TextIO, TextIO]:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise SmokeFailure("the MCP server is not running")
        return process, process.stdin, process.stdout

    def _send(self, message: Mapping[str, Any]) -> None:
        process, stdin, _ = self._streams()
        if process.poll() is not None:
            raise SmokeFailure("the MCP server exited unexpectedly")
        stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        stdin.flush()

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    def _request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )
        process, _, stdout = self._streams()
        ready, _, _ = select.select([stdout], [], [], self._timeout_seconds)
        if not ready:
            raise SmokeFailure("the MCP server response timed out")
        line = stdout.readline()
        if not line:
            if process.poll() is None:
                raise SmokeFailure("the MCP server closed stdout")
            raise SmokeFailure("the MCP server exited without a response")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SmokeFailure("the MCP server emitted invalid JSON") from exc
        if not isinstance(response, Mapping) or response.get("id") != request_id:
            raise SmokeFailure("the MCP response identity did not match")
        if "error" in response:
            raise SmokeFailure("the MCP server returned a JSON-RPC error")
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise SmokeFailure("the MCP server omitted its result")
        return result

    def call_tool(
        self, name: str, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        result = self._request(
            "tools/call", {"name": name, "arguments": dict(arguments)}
        )
        structured = result.get("structuredContent")
        if not isinstance(structured, Mapping):
            raise SmokeFailure("the MCP tool omitted structuredContent")
        return structured

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _report(output: TextIO, step: str, status: str, latency_ms: float) -> None:
    output.write(
        f"step={step} status={status} latency_ms={latency_ms:.3f}\n"
    )
    output.flush()


def _synthetic_list_name() -> str:
    return f"{SYNTHETIC_LIST_PREFIX}{uuid.uuid4().hex}"


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(
        ">I", binascii.crc32(body) & 0xFFFFFFFF
    )


def _write_tiny_png(
    path: Path,
    *,
    rgb: bytes = b"\x1f\x6f\xff",
) -> None:
    if len(rgb) != 3:
        raise ValueError("rgb must contain exactly three bytes")
    pixels = zlib.compress(b"\x00" + rgb)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", pixels)
        + _png_chunk(b"IEND", b"")
    )


def _verified(payload: Mapping[str, Any], step: str) -> Mapping[str, Any]:
    if payload.get("ok") is not True or payload.get("status") != "verified":
        raise SmokeFailure(f"{step} was not verified")
    return payload


def _mapping(value: Any, step: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SmokeFailure(f"{step} returned an invalid object")
    return value


def _nonempty_string(value: Any, step: str) -> str:
    if not isinstance(value, str) or not value:
        raise SmokeFailure(f"{step} omitted an exact identity")
    return value


def run_public_mcp_smoke(
    client: ToolClient,
    *,
    source_id: str,
    cleanup: Callable[[str, str | None], bool],
    stdout: TextIO | None = None,
    list_name_factory: Callable[[], str] | None = None,
    experimental: bool = False,
) -> None:
    """Exercise only synthetic data through the public MCP interface.

    ``client`` and ``cleanup`` are the two system seams. Tests provide fake
    adapters; the CLI provides the packaged stdio server and exact osascript
    list cleanup. Core includes EventKit URL metadata; private sections and
    native attachment operations require explicit Experimental opt-in.
    """

    output = stdout if stdout is not None else sys.stdout
    list_name = (list_name_factory or _synthetic_list_name)()
    if not isinstance(list_name, str) or not list_name.startswith(SYNTHETIC_LIST_PREFIX):
        raise SmokeFailure("the synthetic list name is outside the reserved prefix")
    list_id: str | None = None
    workflow_error: BaseException | None = None
    token = hashlib.sha256(list_name.encode("utf-8")).hexdigest()[:20]
    title = "Codex synthetic live smoke reminder"
    url = "https://example.com/apple-reminders-live-smoke"
    notes = "Synthetic public MCP live smoke data."
    if not experimental:
        notes += f"\n{url}"

    def call(
        step: str,
        tool: str,
        arguments: Mapping[str, Any],
        validate: Callable[[Mapping[str, Any]], Any],
    ) -> Any:
        started = time.perf_counter_ns()
        try:
            result = client.call_tool(tool, arguments)
            if not isinstance(result, Mapping):
                raise SmokeFailure(f"{step} returned a non-object")
            value = validate(result)
        except BaseException:
            _report(
                output,
                step,
                "failed",
                (time.perf_counter_ns() - started) / 1_000_000,
            )
            raise
        _report(
            output,
            step,
            "passed",
            (time.perf_counter_ns() - started) / 1_000_000,
        )
        return value

    try:
        ensure_arguments = {
            "source_id": source_id,
            "name": list_name,
            "idempotency_key": f"live-smoke-list-{token}",
        }
        ensured = call(
            "ensure_list",
            "ensure_reminder_list",
            ensure_arguments,
            lambda value: _verified(value, "ensure_list"),
        )
        ensured_target = _mapping(ensured.get("target"), "ensure_list")
        list_id = _nonempty_string(ensured_target.get("list_id"), "ensure_list")

        def replayed_list(value: Mapping[str, Any]) -> None:
            replay = _verified(value, "ensure_list_replay")
            target = _mapping(replay.get("target"), "ensure_list_replay")
            after = _mapping(replay.get("after"), "ensure_list_replay")
            if (
                replay.get("replayed") is not True
                or target.get("list_id") != list_id
                or after.get("id") != list_id
            ):
                raise SmokeFailure("ensure_list replay did not preserve exact identity")

        call(
            "ensure_list_replay",
            "ensure_reminder_list",
            ensure_arguments,
            replayed_list,
        )

        if experimental:
            section = call(
                "create_section",
                "create_reminder_section",
                {"list_id": list_id, "name": f"Live-Smoke-Section-{token}"},
                lambda value: _verified(value, "create_section"),
            )
            section_target = _mapping(section.get("target"), "create_section")
            section_id = _nonempty_string(
                section_target.get("section_id"), "create_section"
            )

        create_arguments = {
            "list_id": list_id,
            "title": title,
            "notes": notes,
            "url": url,
            "idempotency_key": f"live-smoke-create-{token}",
        }
        created = call(
            "create_reminder",
            "create_reminder",
            create_arguments,
            lambda value: _verified(value, "create_reminder"),
        )
        created_target = _mapping(created.get("target"), "create_reminder")
        reminder_id = _nonempty_string(
            created_target.get("reminder_id"), "create_reminder"
        )

        def replayed_create(value: Mapping[str, Any]) -> None:
            replay = _verified(value, "create_reminder_replay")
            target = _mapping(replay.get("target"), "create_reminder_replay")
            after = _mapping(replay.get("after"), "create_reminder_replay")
            if (
                replay.get("replayed") is not True
                or target.get("reminder_id") != reminder_id
                or after.get("id") != reminder_id
            ):
                raise SmokeFailure("create replay did not preserve exact identity")

        call(
            "create_reminder_replay",
            "create_reminder",
            create_arguments,
            replayed_create,
        )

        def bounded_fetch(value: Mapping[str, Any]) -> None:
            fetched = _verified(value, "bounded_fetch")
            data = _mapping(fetched.get("data"), "bounded_fetch")
            items = data.get("items")
            returned = data.get("returned")
            if (
                data.get("limit") != 5
                or not isinstance(returned, int)
                or isinstance(returned, bool)
                or returned < 1
                or returned > 5
                or not isinstance(items, list)
                or not any(
                    isinstance(item, Mapping)
                    and item.get("id") == reminder_id
                    and item.get("list_id") == list_id
                    and item.get("title") == title
                    for item in items
                )
            ):
                raise SmokeFailure("bounded_fetch did not return the exact synthetic item")

        call(
            "bounded_fetch",
            "fetch_reminders",
            {
                "list_ids": [list_id],
                "status": "incomplete",
                "limit": 5,
                "sort": "modified",
            },
            bounded_fetch,
        )

        def exact_projection(step: str) -> Callable[[Mapping[str, Any]], str]:
            def validate(value: Mapping[str, Any]) -> str:
                exact = _verified(value, step)
                reminder = _mapping(
                    _mapping(exact.get("data"), step).get("reminder"),
                    step,
                )
                if (
                    reminder.get("id") != reminder_id
                    or reminder.get("list_id") != list_id
                    or reminder.get("title") != title
                    or reminder.get("url") != url
                    or (not experimental and reminder.get("notes") != notes)
                ):
                    raise SmokeFailure(f"{step} did not match the synthetic create")
                return _nonempty_string(reminder.get("reference"), step)

            return validate

        first_reference = call(
            "exact_read_primary",
            "read_reminder",
            {"reminder_id": reminder_id},
            exact_projection("exact_read_primary"),
        )
        stale_reference = call(
            "exact_read_parallel",
            "read_reminder",
            {"reminder_id": reminder_id},
            exact_projection("exact_read_parallel"),
        )
        if stale_reference == first_reference:
            raise SmokeFailure("parallel exact reads reused one Reference")

        def fresh_reference(step: str) -> Callable[[Mapping[str, Any]], str]:
            def validate(value: Mapping[str, Any]) -> str:
                receipt = _verified(value, step)
                after = _mapping(receipt.get("after"), step)
                return _nonempty_string(after.get("reference"), step)

            return validate

        reference = call(
            "patch_reminder",
            "change_reminder",
            {
                "reference": first_reference,
                "action": {
                    "kind": "patch",
                    "patch": {"notes": "Synthetic public MCP smoke patch."},
                },
            },
            fresh_reference("patch_reminder"),
        )

        def stale_revision(value: Mapping[str, Any]) -> None:
            error = _mapping(value.get("error"), "stale_reference")
            if (
                value.get("ok") is not False
                or value.get("status") != "failed_no_mutation"
                or error.get("code") != "concurrent_modification"
                or error.get("reason_code") != "concurrent_modification"
            ):
                raise SmokeFailure("a stale parallel Reference remained writable")

        call(
            "stale_reference",
            "change_reminder",
            {
                "reference": stale_reference,
                "action": {
                    "kind": "patch",
                    "patch": {"notes": "Synthetic public MCP smoke patch."},
                },
            },
            stale_revision,
        )
        reference = call(
            "complete_reminder",
            "change_reminder",
            {
                "reference": reference,
                "action": {"kind": "set_completion", "completed": True},
            },
            fresh_reference("complete_reminder"),
        )
        reference = call(
            "reopen_reminder",
            "change_reminder",
            {
                "reference": reference,
                "action": {"kind": "set_completion", "completed": False},
            },
            fresh_reference("reopen_reminder"),
        )
        if experimental:
            reference = call(
                "move_to_section",
                "organize_reminder",
                {
                    "reference": reference,
                    "action": {"kind": "move_to_section", "section_id": section_id},
                },
                fresh_reference("move_to_section"),
            )

            def attachment_mutation_state(
                value: Mapping[str, Any],
                step: str,
                *,
                expected_image_count: int,
                forbidden_image_id: str | None = None,
            ) -> tuple[str, str | None]:
                receipt = _verified(value, step)
                after = _mapping(receipt.get("after"), step)
                attachments = after.get("attachments")
                if not isinstance(attachments, list):
                    raise SmokeFailure(f"{step} omitted attachment read-back")
                images = [
                    item
                    for item in attachments
                    if isinstance(item, Mapping) and item.get("type") == "image"
                ]
                has_exact_url = any(
                    isinstance(item, Mapping)
                    and item.get("type") == "url"
                    and item.get("url") == url
                    for item in attachments
                )
                if len(images) != expected_image_count or not has_exact_url:
                    raise SmokeFailure(
                        f"{step} did not preserve the exact URL and image count"
                    )
                image_id = None
                if images:
                    image_id = _nonempty_string(images[0].get("id"), step)
                if forbidden_image_id is not None and any(
                    item.get("id") == forbidden_image_id for item in images
                ):
                    raise SmokeFailure(f"{step} retained the replaced image")
                return _nonempty_string(after.get("reference"), step), image_id

            with tempfile.TemporaryDirectory(
                prefix="apple-reminders-live-smoke-"
            ) as temporary:
                image_path = Path(temporary) / "synthetic.png"
                _write_tiny_png(image_path)

                reference, old_image_id = call(
                    "attach_image",
                    "change_reminder_attachment",
                    {
                        "reference": reference,
                        "action": {
                            "kind": "attach_image",
                            "image_path": str(image_path.resolve()),
                            "idempotency_key": f"live-smoke-image-{token}",
                        },
                    },
                    lambda value: attachment_mutation_state(
                        value,
                        "attach_image",
                        expected_image_count=1,
                    ),
                )
            old_image_id = _nonempty_string(old_image_id, "attach_image")

            def native_evidence(
                value: Mapping[str, Any],
                step: str,
                *,
                expected_image_id: str | None,
            ) -> str:
                inspected = _verified(value, step)
                data = _mapping(inspected.get("data"), step)
                section_state = _mapping(data.get("section"), step)
                attachments = data.get("attachments")
                images = (
                    [
                        item
                        for item in attachments
                        if isinstance(item, Mapping) and item.get("type") == "image"
                    ]
                    if isinstance(attachments, list)
                    else []
                )
                has_exact_url = isinstance(attachments, list) and any(
                    isinstance(item, Mapping)
                    and item.get("type") == "url"
                    and item.get("url") == url
                    for item in attachments
                )

                expected_image_count = 0 if expected_image_id is None else 1
                image_matches = len(images) == expected_image_count
                if expected_image_id is not None and image_matches:
                    image_matches = (
                        images[0].get("id") == expected_image_id
                        and isinstance(images[0].get("sync"), Mapping)
                        and images[0]["sync"].get("mobile_visible_likely") is True
                    )
                if (
                    section_state.get("id") != section_id
                    or not image_matches
                    or not has_exact_url
                    or data.get("truncated") is not False
                ):
                    raise SmokeFailure(
                        f"{step} did not prove exact section, URL, and image state"
                    )
                return _nonempty_string(data.get("reference"), step)

            reference = call(
                "inspect_native",
                "inspect_reminder_native",
                {
                    "kind": "reminder",
                    "reference": reference,
                    "include": ["section", "attachments", "sync"],
                    "limit": 20,
                },
                lambda value: native_evidence(
                    value,
                    "inspect_native",
                    expected_image_id=old_image_id,
                ),
            )

            with tempfile.TemporaryDirectory(
                prefix="apple-reminders-live-smoke-replacement-"
            ) as temporary:
                replacement_path = Path(temporary) / "replacement.png"
                _write_tiny_png(replacement_path, rgb=b"\xff\x6f\x1f")
                reference, new_image_id = call(
                    "replace_image",
                    "change_reminder_attachment",
                    {
                        "reference": reference,
                        "action": {
                            "kind": "replace_image",
                            "attachment_id": old_image_id,
                            "image_path": str(replacement_path.resolve()),
                            "idempotency_key": f"live-smoke-replacement-{token}",
                        },
                    },
                    lambda value: attachment_mutation_state(
                        value,
                        "replace_image",
                        expected_image_count=1,
                        forbidden_image_id=old_image_id,
                    ),
                )
            new_image_id = _nonempty_string(new_image_id, "replace_image")
            reference = call(
                "inspect_replaced_image",
                "inspect_reminder_native",
                {
                    "kind": "reminder",
                    "reference": reference,
                    "include": ["section", "attachments", "sync"],
                    "limit": 20,
                },
                lambda value: native_evidence(
                    value,
                    "inspect_replaced_image",
                    expected_image_id=new_image_id,
                ),
            )
            reference, _ = call(
                "delete_image",
                "change_reminder_attachment",
                {
                    "reference": reference,
                    "action": {"kind": "delete", "attachment_id": new_image_id},
                },
                lambda value: attachment_mutation_state(
                    value,
                    "delete_image",
                    expected_image_count=0,
                    forbidden_image_id=new_image_id,
                ),
            )
            reference = call(
                "inspect_deleted_image",
                "inspect_reminder_native",
                {
                    "kind": "reminder",
                    "reference": reference,
                    "include": ["section", "attachments", "sync"],
                    "limit": 20,
                },
                lambda value: native_evidence(
                    value,
                    "inspect_deleted_image",
                    expected_image_id=None,
                ),
            )

        def deleted(value: Mapping[str, Any]) -> None:
            receipt = _verified(value, "delete_reminder")
            verification = _mapping(receipt.get("verification"), "delete_reminder")
            after = _mapping(receipt.get("after"), "delete_reminder")
            if verification.get("local_absence") is not True or after.get(
                "deleted"
            ) is not True:
                raise SmokeFailure("delete_reminder did not prove exact local absence")

        call(
            "delete_reminder",
            "delete_reminder",
            {"reference": reference},
            deleted,
        )

        def post_delete(value: Mapping[str, Any]) -> None:
            error = _mapping(value.get("error"), "post_delete_read")
            if (
                value.get("ok") is not False
                or value.get("status") != "failed_no_mutation"
                or error.get("code") != "not_found"
            ):
                raise SmokeFailure("post-delete exact read was not not_found")

        call(
            "post_delete_read",
            "read_reminder",
            {"reminder_id": reminder_id},
            post_delete,
        )
    except BaseException as exc:
        workflow_error = exc

    cleanup_started = time.perf_counter_ns()
    cleaned = False
    try:
        cleaned = cleanup(list_name, list_id) is True
    except BaseException:
        cleaned = False
    cleanup_step = "cleanup" if cleaned else f"cleanup:{list_name}"
    _report(
        output,
        cleanup_step,
        "passed" if cleaned else "failed",
        (time.perf_counter_ns() - cleanup_started) / 1_000_000,
    )

    if workflow_error is not None:
        raise workflow_error
    if not cleaned:
        raise SmokeFailure("exact synthetic list cleanup could not be proven")


def cleanup_synthetic_list(
    list_name: str,
    list_id: str | None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Delete only one reserved list whose scripting identity exactly matches."""

    if not isinstance(list_name, str) or not list_name.startswith(
        SYNTHETIC_LIST_PREFIX
    ):
        return False
    expected_id = list_id if isinstance(list_id, str) else ""
    try:
        completed = runner(
            ["/usr/bin/osascript", "-e", CLEANUP_SCRIPT, list_name, expected_id],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and completed.stdout.strip() in {
        "absent",
        "deleted",
    }


def _default_client_factory(
    *, server_path: Path, plugin_root: Path, experimental: bool = False,
    bundled_runtime: bool = False,
) -> McpStdioClient:
    return McpStdioClient(
        server_path=server_path, plugin_root=plugin_root, experimental=experimental,
        bundled_runtime=bundled_runtime,
    )


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[..., Any] | None = None,
    cleanup: Callable[..., Any] | None = None,
    stdout: TextIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live-reminders", action="store_true")
    parser.add_argument("--source-id")
    parser.add_argument(
        "--bundled-runtime",
        action="store_true",
        help="Start the packaged signed-runtime launcher instead of the current Python interpreter.",
    )
    parser.add_argument(
        "--experimental",
        action="store_true",
        help="Also test private sections, native URLs, and image attachments.",
    )
    args = parser.parse_args(argv)
    output = stdout if stdout is not None else sys.stdout
    if not args.confirm_live_reminders or not (
        isinstance(args.source_id, str) and args.source_id.strip()
    ):
        _report(output, "preflight", "blocked", 0.0)
        return 2
    factory = client_factory or _default_client_factory
    cleaner = cleanup or cleanup_synthetic_list
    server_started = time.perf_counter_ns()
    entered = False
    try:
        with factory(
            server_path=SERVER_PATH,
            plugin_root=PLUGIN_ROOT,
            experimental=args.experimental,
            bundled_runtime=args.bundled_runtime,
        ) as client:
            entered = True
            _report(
                output,
                "server_start",
                "passed",
                (time.perf_counter_ns() - server_started) / 1_000_000,
            )
            run_public_mcp_smoke(
                client,
                source_id=args.source_id.strip(),
                cleanup=cleaner,
                stdout=output,
                experimental=args.experimental,
            )
    except Exception:
        if not entered:
            _report(
                output,
                "server_start",
                "failed",
                (time.perf_counter_ns() - server_started) / 1_000_000,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
