from __future__ import annotations

from contextlib import ExitStack
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins/apple-reminders"
sys.path.insert(0, str(PLUGIN_ROOT))
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import reminders_doctor as doctor
from mcp import server
from mcp.v2_contract import validate_public_result


class PackagingDiagnosisBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.paths = doctor.default_paths(home=self.home)
        # Any access to these paths is a bug, even if the read is content-free.
        for key in ("group", "stores", "files", "reminders_app_candidates", "private_frameworks"):
            self.paths.pop(key)
        self.stack.enter_context(mock.patch.object(doctor, "default_paths", return_value=self.paths))
        platform = doctor.check_result(
            doctor.STATUS_OK, "macos_detected", "Synthetic macOS platform.",
            details={"system": "Darwin", "macos_version": "14.0"},
        )
        self.platform = self.stack.enter_context(mock.patch.object(doctor, "inspect_platform", return_value=platform))
        self.forbidden = {}
        for name in (
            "inspect_reminders_app", "inspect_store_access", "aggregate_command_schema",
            "inspect_helper_toolchain", "inspect_private_frameworks", "inspect_permission_symptoms",
            "inspect_account_visibility", "derive_capabilities", "run_static_command",
            "resolve_native_helper", "resolve_selected_clang", "detect_runtime_identity",
        ):
            self.forbidden[name] = self.stack.enter_context(mock.patch.object(
                doctor, name, side_effect=AssertionError(f"packaging reached {name}"),
            ))

    def assert_no_private_access(self) -> None:
        for probe in self.forbidden.values():
            probe.assert_not_called()

    def test_collection_reads_only_platform_and_plugin_artifact_metadata(self) -> None:
        self.paths["app_support"].mkdir(parents=True, mode=0o700)
        self.paths["journal"].write_text("sensitive journal body must stay unread")
        self.paths["journal"].chmod(0o600)
        real_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.is_relative_to(self.home):
                self.fail("Packaging opened plugin support-file contents")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_open):
            report = doctor.collect_report(scope="packaging")
        self.assertTrue(report["ok"])
        self.assertEqual(set(report["checks"]), {"platform", "local_artifacts", "redaction"})
        self.assertEqual(report["capabilities"], {"runtime_boundaries": doctor.runtime_boundary_metadata()})
        self.assertNotIn("sensitive journal body", json.dumps(report))
        self.assertFalse(report["execution"]["compiler_process_attempted"])
        self.assert_no_private_access()

    def test_packaging_cli_rejects_toolchain_before_collecting(self) -> None:
        with mock.patch.object(doctor, "collect_report") as collect, mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                doctor.main(["--scope", "packaging", "--run-experimental-toolchain-check"])
        self.assertEqual(raised.exception.code, 2)
        collect.assert_not_called()
        self.platform.assert_not_called()

    def test_public_packaging_call_carries_scope_to_real_collection(self) -> None:
        seen = []

        def run_doctor(argv, **kwargs):
            seen.append(argv)
            self.assertEqual(Path(argv[1]), server.DEFAULT_BACKEND_PATHS.doctor)
            output = io.StringIO()
            with mock.patch.object(sys, "stdout", output):
                code = doctor.main(argv[2:])
            return SimpleNamespace(stdout=output.getvalue(), stderr="", returncode=code)

        for detail in ("summary", "full"):
            with self.subTest(detail=detail), mock.patch.object(server, "run_bounded_process", side_effect=run_doctor):
                runtime = server.McpRuntime()
                runtime.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
                response = runtime.handle({
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "diagnose_reminders", "arguments": {"scope": "packaging", "detail_level": detail}},
                })
            result = response["result"]["structuredContent"]
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["data"]["scope"], "packaging")
            self.assertEqual(result["data"]["capabilities"], [])
            self.assertEqual({check["name"] for check in result["data"]["checks"]}, {"platform", "local_artifacts", "redaction"})
            validate_public_result("diagnose_reminders", result)
            self.assertIn("--scope", seen[-1])
            self.assert_no_private_access()

    def test_platform_failure_is_retained_without_triggering_private_probes(self) -> None:
        self.platform.return_value = doctor.check_result(doctor.STATUS_BLOCKED, "unsupported_platform", "macOS is required.")
        report = doctor.collect_report(scope="packaging")
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "blocked")
        self.assert_no_private_access()


if __name__ == "__main__":
    unittest.main()
