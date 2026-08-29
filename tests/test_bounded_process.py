from __future__ import annotations

import ast
import errno
import importlib.util
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "plugins" / "apple-reminders" / "scripts" / "bounded_process.py"
SPEC = importlib.util.spec_from_file_location("bounded_process", MODULE_PATH)
assert SPEC and SPEC.loader
bounded_process = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bounded_process
SPEC.loader.exec_module(bounded_process)


class BoundedProcessTests(unittest.TestCase):
    def run_python(
        self,
        source: str,
        *,
        timeout_s: float = 3.0,
        stdout_limit: int = 64_000,
        stderr_limit: int = 64_000,
        output: str = "utf8",
        input: bytes | None = None,
        cwd: Path | None = None,
    ):
        return bounded_process.run(
            [sys.executable, "-c", source],
            input=input,
            cwd=cwd,
            timeout_s=timeout_s,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
            output=output,
        )

    def assert_process_group_gone(self, pgid: int) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        self.fail(f"process group {pgid} still exists")

    def test_success_and_nonzero_exit_are_normal_results(self) -> None:
        success = self.run_python(
            "import sys; sys.stdout.write('out'); sys.stderr.write('err')"
        )
        nonzero = self.run_python(
            "import sys; sys.stdout.write('nope'); raise SystemExit(7)"
        )

        self.assertEqual(success.returncode, 0)
        self.assertEqual(success.stdout, "out")
        self.assertEqual(success.stderr, "err")
        self.assertEqual(nonzero.returncode, 7)
        self.assertEqual(nonzero.stdout, "nope")

    def test_bytes_output_is_not_decoded(self) -> None:
        result = self.run_python(
            "import os; os.write(1, b'\\xff'); os.write(2, b'\\xfe')",
            output="bytes",
        )

        self.assertEqual(result.stdout, b"\xff")
        self.assertEqual(result.stderr, b"\xfe")

    def test_launch_failure_is_typed(self) -> None:
        missing = f"/definitely/missing/bounded-process-{os.getpid()}"

        with self.assertRaises(bounded_process.ProcessLaunchError) as raised:
            bounded_process.run(
                [missing],
                timeout_s=1.0,
                stdout_limit=100,
                stderr_limit=100,
            )

        self.assertEqual(raised.exception.argv, (missing,))
        self.assertIsInstance(raised.exception.cause, OSError)
        self.assertEqual(str(raised.exception), "process could not be launched")

    def test_invalid_options_are_rejected_before_launch(self) -> None:
        base = {
            "timeout_s": 1.0,
            "stdout_limit": 100,
            "stderr_limit": 100,
        }
        invalid_overrides = (
            {"timeout_s": 0},
            {"timeout_s": -1},
            {"timeout_s": float("nan")},
            {"timeout_s": float("inf")},
            {"timeout_s": True},
            {"stdout_limit": -1},
            {"stdout_limit": 1.5},
            {"stdout_limit": True},
            {"stderr_limit": -1},
            {"stderr_limit": 1.5},
            {"stderr_limit": False},
            {"output": "locale"},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                bounded_process.run(
                    ["/definitely/not/launched"],
                    **(base | overrides),
                )

        for argv in ([], "not-a-sequence"):
            with self.subTest(argv=argv), self.assertRaises((TypeError, ValueError)):
                bounded_process.run(argv, **base)

    def test_pipe_read_failure_is_typed_and_contained(self) -> None:
        real_read = os.read
        raised_once = False

        def fail_one_read(file_descriptor: int, size: int) -> bytes:
            nonlocal raised_once
            if (
                not raised_once
                and threading.current_thread().name.startswith("bounded-process-")
            ):
                raised_once = True
                raise OSError(errno.EIO, "synthetic pipe failure")
            return real_read(file_descriptor, size)

        with mock.patch.object(bounded_process.os, "read", side_effect=fail_one_read):
            with self.assertRaises(bounded_process.ProcessIOError) as raised:
                self.run_python("import time; time.sleep(30)", timeout_s=2.0)

        self.assertIn(raised.exception.stream, ("stdout", "stderr"))
        self.assertIsInstance(raised.exception.cause, OSError)
        self.assert_process_group_gone(raised.exception.pid)

    def test_timeout_kills_child_and_grandchild_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_pid = root / "child.pid"
            grandchild_pid = root / "grandchild.pid"
            child_source = f"""
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open({str(child_pid)!r}, 'w').write(str(os.getpid()))
grandchild = subprocess.Popen([sys.executable, '-c', "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open({str(grandchild_pid)!r}, 'w').write(str(os.getpid())); time.sleep(30)"])
while not os.path.exists({str(grandchild_pid)!r}): time.sleep(0.01)
print('ready', flush=True)
time.sleep(30)
"""

            with self.assertRaises(bounded_process.ProcessTimeoutError) as raised:
                self.run_python(child_source, timeout_s=0.6)

            self.assertEqual(raised.exception.timeout_s, 0.6)
            self.assertTrue(child_pid.exists())
            self.assertTrue(grandchild_pid.exists())
            self.assertIsNotNone(raised.exception.returncode)
            self.assert_process_group_gone(raised.exception.pid)

    def test_output_cap_kills_the_whole_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "descendant-survived"
            source = f"""
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
subprocess.Popen([sys.executable, '-c', "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(0.8); open({str(marker)!r}, 'w').write('alive'); time.sleep(30)"])
os.write(1, b'x' * 8192)
time.sleep(30)
"""

            with self.assertRaises(bounded_process.ProcessOutputLimitError) as raised:
                self.run_python(source, stdout_limit=64)

            self.assertEqual(raised.exception.stream, "stdout")
            self.assertEqual(raised.exception.limit, 64)
            self.assertLessEqual(len(raised.exception.stdout), 64)
            self.assert_process_group_gone(raised.exception.pid)
            time.sleep(0.9)
            self.assertFalse(marker.exists())

    def test_invalid_stdout_and_stderr_are_classified(self) -> None:
        for file_descriptor, expected_stream in ((1, "stdout"), (2, "stderr")):
            with self.subTest(stream=expected_stream):
                with self.assertRaises(bounded_process.ProcessDecodeError) as raised:
                    self.run_python(
                        f"import os, time; os.write({file_descriptor}, b'\\xff'); time.sleep(30)",
                        timeout_s=2.0,
                    )

                self.assertEqual(raised.exception.stream, expected_stream)
                self.assertIsInstance(raised.exception.cause, UnicodeDecodeError)
                self.assert_process_group_gone(raised.exception.pid)

    def test_split_multibyte_utf8_is_decoded_incrementally(self) -> None:
        result = self.run_python(
            "import os, time; os.write(1, b'\\xe2'); time.sleep(0.05); "
            "os.write(1, b'\\x82'); time.sleep(0.05); os.write(1, b'\\xac')"
        )

        self.assertEqual(result.stdout, "€")

    def test_input_and_cwd_are_supported_without_text_coercion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.run_python(
                "import os, sys; data=sys.stdin.buffer.read(); "
                "sys.stdout.buffer.write(os.getcwd().encode()+b'\\0'+data)",
                input=b"\x00binary\xff",
                cwd=root,
                output="bytes",
            )

        self.assertEqual(
            result.stdout,
            os.fsencode(root.resolve()) + b"\0\0binary\xff",
        )

    def test_normal_leader_exit_cannot_leave_a_background_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "descendant-survived"
            source = f"""
import subprocess, sys
subprocess.Popen([sys.executable, '-c', "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(0.7); open({str(marker)!r}, 'w').write('alive'); time.sleep(30)"])
print('leader done')
"""

            result = self.run_python(source)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "leader done\n")
            time.sleep(0.8)
            self.assertFalse(marker.exists())

    def test_production_python_cannot_import_subprocess_outside_module(self) -> None:
        plugin_root = REPO_ROOT / "plugins" / "apple-reminders"
        violations: list[str] = []
        for path in sorted(plugin_root.rglob("*.py")):
            if path == MODULE_PATH:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                imports_subprocess = (
                    isinstance(node, ast.Import)
                    and any(alias.name == "subprocess" for alias in node.names)
                ) or (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "subprocess"
                )
                if imports_subprocess:
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                    )

        self.assertEqual(
            violations,
            [],
            "production process launches must route through bounded_process",
        )


if __name__ == "__main__":
    unittest.main()
