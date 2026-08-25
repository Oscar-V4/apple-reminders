from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
BENCHMARK_PATH = REPO_ROOT / "scripts" / "benchmark_plugin.py"
SPEC = importlib.util.spec_from_file_location("benchmark_plugin", BENCHMARK_PATH)
assert SPEC and SPEC.loader
benchmark_plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark_plugin
SPEC.loader.exec_module(benchmark_plugin)


def benchmark_without_timing(action, *, warmups: int, samples: int):
    for _ in range(warmups + samples):
        action()
    return {
        "samples": samples,
        "min_ms": 1.0,
        "median_ms": 2.0,
        "mean_ms": 2.5,
        "p95_ms": 4.0,
        "max_ms": 5.0,
    }


class BenchmarkCliContractTests(unittest.TestCase):
    def run_main_with_captured_cases(self):
        cases = []

        def capture(case, *, root=benchmark_plugin.ROOT):
            self.assertEqual(root, benchmark_plugin.ROOT)
            cases.append(case)

        with (
            mock.patch.object(benchmark_plugin, "run_command", side_effect=capture),
            mock.patch.object(
                benchmark_plugin, "benchmark_action", side_effect=benchmark_without_timing
            ),
            mock.patch.object(
                benchmark_plugin,
                "package_snapshot",
                return_value={
                    "allowlisted_files": 24,
                    "allowlisted_source_bytes": 12345,
                    "archive_bytes": 67890,
                },
            ),
            mock.patch.object(benchmark_plugin, "git_value", return_value=None),
            mock.patch.object(benchmark_plugin.platform, "system", return_value="Darwin"),
            mock.patch("sys.stdout"),
        ):
            result = benchmark_plugin.main(["--label", "unit", "--samples", "1"])

        self.assertEqual(result, 0)
        return cases

    def test_mcp_benchmark_starts_server_with_initialize_and_tools_list(self) -> None:
        cases = self.run_main_with_captured_cases()
        case = next(case for case in cases if case.name == "mcp_initialize_tools_list")
        requests = [json.loads(line) for line in case.stdin.splitlines()]

        self.assertEqual(
            case.command,
            (benchmark_plugin.PYTHON, str(PLUGIN_ROOT / "mcp" / "server.py")),
        )
        self.assertEqual([request["method"] for request in requests], ["initialize", "tools/list"])

    def test_eventkit_benchmark_runs_validation_only_request(self) -> None:
        cases = self.run_main_with_captured_cases()
        case = next(case for case in cases if case.name == "eventkit_validate_create")
        request = json.loads(case.stdin)

        self.assertEqual(
            case.command,
            (
                benchmark_plugin.PYTHON,
                str(PLUGIN_ROOT / "scripts" / "eventkit_bridge.py"),
                "--validate-only",
            ),
        )
        self.assertEqual(request["operation"], "create_reminder")

    def test_eventkit_build_benchmarks_share_an_isolated_cache(self) -> None:
        cases = self.run_main_with_captured_cases()
        fresh = next(case for case in cases if case.name == "eventkit_helper_build_fresh")
        cached = next(case for case in cases if case.name == "eventkit_helper_build_cached")

        expected_prefix = (
            benchmark_plugin.PYTHON,
            str(PLUGIN_ROOT / "scripts" / "eventkit_bridge.py"),
            "--build-only",
            "--cache-dir",
        )
        self.assertEqual(fresh.command[:4], expected_prefix)
        self.assertEqual(cached.command[:4], expected_prefix)
        self.assertEqual(fresh.command[4], cached.command[4])
        self.assertIn("apple-reminders-benchmark-eventkit-cache-", fresh.command[4])
        self.assertEqual(fresh.command[5:], ("--force-build",))
        self.assertEqual(cached.command[5:], ())

    def test_doctor_benchmark_uses_actual_mcp_route_and_isolated_home(self) -> None:
        cases = self.run_main_with_captured_cases()
        case = next(case for case in cases if case.name == "mcp_doctor_route")
        requests = [json.loads(line) for line in case.stdin.splitlines()]

        self.assertEqual(
            case.command,
            (benchmark_plugin.PYTHON, str(PLUGIN_ROOT / "mcp" / "server.py")),
        )
        self.assertEqual([request["method"] for request in requests], ["initialize", "tools/call"])
        self.assertEqual(requests[-1]["params"]["name"], "diagnose_reminders")
        self.assertEqual(
            requests[-1]["params"]["arguments"],
            {"scope": "core", "detail_level": "summary"},
        )
        self.assertEqual(case.environment.keys(), {"HOME"})
        self.assertNotEqual(case.environment["HOME"], str(Path.home()))
        self.assertIn("apple-reminders-benchmark-home-", case.environment["HOME"])

    def test_source_audit_benchmark_outputs_json_only(self) -> None:
        cases = self.run_main_with_captured_cases()
        case = next(case for case in cases if case.name == "source_package_audit")

        self.assertEqual(
            case.command,
            (
                benchmark_plugin.PYTHON,
                str(REPO_ROOT / "scripts" / "audit_source_package.py"),
                str(PLUGIN_ROOT),
                "--json",
            ),
        )

    def test_plugin_root_can_target_another_checkout(self) -> None:
        alternate = Path("/tmp/apple-reminders-alternate").resolve()
        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(benchmark_plugin, "run_command") as run,
            mock.patch.object(
                benchmark_plugin, "benchmark_action", side_effect=benchmark_without_timing
            ),
            mock.patch.object(benchmark_plugin, "package_snapshot", return_value={}),
            mock.patch.object(benchmark_plugin, "git_value", return_value=None),
            mock.patch("sys.stdout"),
        ):
            result = benchmark_plugin.main(
                ["--label", "alternate", "--samples", "1", "--plugin-root", str(alternate)]
            )

        self.assertEqual(result, 0)
        commands = [
            call.args[0].command
            for call in run.mock_calls
            if call.args
        ]
        self.assertTrue(any(str(alternate / "mcp" / "server.py") in command for command in commands))

    def test_payload_records_percentiles_safety_and_package_bytes(self) -> None:
        with (
            mock.patch.object(benchmark_plugin, "run_command"),
            mock.patch.object(
                benchmark_plugin, "benchmark_action", side_effect=benchmark_without_timing
            ),
            mock.patch.object(
                benchmark_plugin,
                "package_snapshot",
                return_value={
                    "allowlisted_files": 24,
                    "allowlisted_source_bytes": 12345,
                    "archive_bytes": 67890,
                },
            ),
            mock.patch.object(benchmark_plugin, "git_value", return_value=None),
            mock.patch("sys.stdout") as stdout,
        ):
            result = benchmark_plugin.main(["--label", "unit", "--samples", "1"])

        payload = json.loads(stdout.write.call_args.args[0])
        self.assertEqual(result, 0)
        self.assertEqual(payload["safety"]["reminder_rows_read"], False)
        self.assertEqual(payload["package"]["archive_bytes"], 67890)
        self.assertEqual(payload["measurements"]["mcp_initialize_tools_list"]["median_ms"], 2.0)
        self.assertEqual(payload["measurements"]["mcp_initialize_tools_list"]["p95_ms"], 4.0)
        self.assertEqual(
            payload["measurement_semantics"]["timing"],
            "end_to_end_subprocess_wall_clock",
        )
        self.assertTrue(payload["measurement_semantics"]["process_startup_included"])
        self.assertFalse(payload["measurement_semantics"]["cross_machine_comparable"])
        self.assertFalse(payload["safety"]["eventkit_api_called"])
        self.assertFalse(payload["safety"]["native_helper_executed"])

    def test_main_returns_nonzero_when_a_benchmark_case_fails(self) -> None:
        with (
            mock.patch.object(
                benchmark_plugin,
                "run_command",
                side_effect=RuntimeError("eventkit_validate_create exited 2"),
            ),
            mock.patch("sys.stderr") as stderr,
        ):
            result = benchmark_plugin.main(["--label", "unit", "--samples", "1"])

        self.assertEqual(result, 1)
        self.assertIn("benchmark failed: eventkit_validate_create exited 2", stderr.write.call_args.args[0])

    def test_main_returns_nonzero_when_output_cannot_be_written(self) -> None:
        payload = {
            "schema_version": 1,
            "label": "unit",
            "safety": {},
            "package": {},
            "measurements": {},
        }
        with (
            mock.patch.object(benchmark_plugin, "collect_payload", return_value=payload),
            mock.patch.object(Path, "write_text", side_effect=OSError("read-only output")),
            mock.patch("sys.stderr") as stderr,
        ):
            result = benchmark_plugin.main(
                ["--label", "unit", "--samples", "1", "--output", "result.json"]
            )

        self.assertEqual(result, 1)
        self.assertIn("benchmark failed: read-only output", stderr.write.call_args.args[0])

    def test_main_returns_two_when_performance_gate_fails(self) -> None:
        payload = {
            "schema_version": 1,
            "label": "unit",
            "safety": {},
            "package": {},
            "performance_gates": {
                "passed": False,
                "cases": {"mcp_doctor_route": {"status": "failed"}},
            },
            "measurements": {},
        }
        with (
            mock.patch.object(benchmark_plugin, "collect_payload", return_value=payload),
            mock.patch("sys.stdout"),
            mock.patch("sys.stderr") as stderr,
        ):
            result = benchmark_plugin.main(["--label", "unit", "--samples", "1"])

        self.assertEqual(result, 2)
        self.assertIn("performance gate failed", stderr.write.call_args.args[0])


class BenchmarkRunnerTests(unittest.TestCase):
    def test_summary_records_spread_not_only_percentiles(self) -> None:
        result = benchmark_plugin.summarize([1.0, 2.0, 4.0])

        self.assertEqual(result["stdev_ms"], 1.528)
        self.assertEqual(result["mad_ms"], 1.0)

    def test_performance_gates_fail_over_budget_and_skip_unsupported_cases(self) -> None:
        measurements = {
            name: {"p95_ms": 1.0}
            for name in benchmark_plugin.P95_BUDGETS_MS
        }
        measurements["mcp_doctor_route"] = {
            "p95_ms": benchmark_plugin.P95_BUDGETS_MS["mcp_doctor_route"] + 0.001
        }
        measurements["eventkit_helper_build_cached"] = {
            "status": "skipped",
            "reason": "requires_macos",
        }

        gates = benchmark_plugin.evaluate_performance_gates(measurements)

        self.assertFalse(gates["passed"])
        self.assertEqual(gates["cases"]["mcp_doctor_route"]["status"], "failed")
        self.assertEqual(
            gates["cases"]["eventkit_helper_build_cached"]["status"],
            "skipped",
        )

    def test_performance_gates_are_incomplete_when_macos_build_case_is_skipped(self) -> None:
        measurements = {
            name: {"p95_ms": 1.0}
            for name in benchmark_plugin.P95_BUDGETS_MS
        }
        measurements["eventkit_helper_build_fresh"] = {
            "status": "skipped",
            "reason": "requires_macos",
        }

        gates = benchmark_plugin.evaluate_performance_gates(measurements)

        self.assertFalse(gates["passed"])
        self.assertFalse(gates["complete"])

    def test_package_snapshot_uses_a_fresh_temporary_build(self) -> None:
        def fake_run(command, **kwargs):
            if "audit_source_package.py" in command[1]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"package_files": ["README.md"]}),
                    "",
                )
            output = Path(command[command.index("--output-directory") + 1])
            archive = output / "apple-reminders-9.9.9.zip"
            archive.write_bytes(b"x" * 321)
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"archive": str(archive), "bytes": 321}),
                "",
            )

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            snapshot = benchmark_plugin.package_snapshot(PLUGIN_ROOT)

        self.assertEqual(snapshot["archive_bytes"], 321)
        self.assertEqual(snapshot["allowlisted_files"], 1)
        self.assertEqual(
            snapshot["allowlisted_source_bytes"],
            (PLUGIN_ROOT / "README.md").stat().st_size,
        )

    def test_run_command_raises_when_return_code_is_not_allowed(self) -> None:
        completed = subprocess.CompletedProcess(["tool"], 2, "", "permission denied\n")
        with mock.patch.object(subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "case exited 2: permission denied"):
                benchmark_plugin.run_command(
                    benchmark_plugin.CommandCase("case", ("tool",))
                )

    def test_run_command_rejects_zero_exit_mcp_tool_error(self) -> None:
        completed = subprocess.CompletedProcess(
            ["tool"],
            0,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"isError": True, "content": []},
                }
            )
            + "\n",
            "",
        )
        with mock.patch.object(subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "reported an MCP error"):
                benchmark_plugin.run_command(
                    benchmark_plugin.CommandCase(
                        "case",
                        ("tool",),
                        output_contract="mcp_success",
                    )
                )

    def test_run_command_rejects_zero_exit_json_failure(self) -> None:
        completed = subprocess.CompletedProcess(
            ["tool"],
            0,
            json.dumps({"ok": False, "error": {"code": "failed"}}),
            "",
        )
        with mock.patch.object(subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "did not report ok=true"):
                benchmark_plugin.run_command(
                    benchmark_plugin.CommandCase(
                        "case",
                        ("tool",),
                        output_contract="json_ok",
                    )
                )

    def test_run_command_rejects_missing_mcp_response(self) -> None:
        completed = subprocess.CompletedProcess(
            ["tool"],
            0,
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n",
            "",
        )
        stdin = "\n".join(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "ping"})
            for request_id in (1, 2)
        )
        with mock.patch.object(subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "response IDs did not match"):
                benchmark_plugin.run_command(
                    benchmark_plugin.CommandCase(
                        "case",
                        ("tool",),
                        stdin=stdin,
                        output_contract="mcp_success",
                    )
                )

    def test_run_command_sets_python_no_bytecode_without_real_home_override(self) -> None:
        captured = {}

        def fake_run(*args, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(args[0], 0, "", "")

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            benchmark_plugin.run_command(
                benchmark_plugin.CommandCase("case", ("tool",), environment={"HOME": "/tmp/home"})
            )

        self.assertEqual(captured["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(captured["env"]["HOME"], "/tmp/home")

    def test_run_command_removes_test_backend_overrides_from_benchmark_environment(self) -> None:
        captured = {}

        def fake_run(*args, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(args[0], 0, "", "")

        inherited = {
            "APPLE_REMINDERS_MCP_TEST_MODE": "1",
            "APPLE_REMINDERS_ADAPTER_PATH": "/tmp/mock-adapter.py",
            "APPLE_REMINDERS_EVENTKIT_BRIDGE_PATH": "/tmp/mock-eventkit.py",
            "APPLE_REMINDERS_DOCTOR_PATH": "/tmp/mock-doctor.py",
        }
        with (
            mock.patch.dict(benchmark_plugin.os.environ, inherited, clear=False),
            mock.patch.object(subprocess, "run", side_effect=fake_run),
        ):
            benchmark_plugin.run_command(
                benchmark_plugin.CommandCase(
                    "case",
                    ("tool",),
                    environment={"APPLE_REMINDERS_MCP_TEST_MODE": "1"},
                )
            )

        for name in inherited:
            self.assertNotIn(name, captured["env"])


if __name__ == "__main__":
    unittest.main()
