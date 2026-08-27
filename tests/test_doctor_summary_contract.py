from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import reminders_doctor  # noqa: E402


def sample_full_report() -> dict[str, object]:
    return {
        "schema_version": 1,
        "doctor": "apple-reminders-doctor",
        "ok": True,
        "status": "degraded",
        "summary": {"blocked": 0, "ok": 1, "skipped": 0, "unknown": 0, "warning": 1},
        "privacy": {
            "content_free": True,
            "reminder_rows_read": False,
            "write_attempted": False,
        },
        "checks": {
            "platform": {
                "status": "ok",
                "code": "macos_detected",
                "message": "macOS was detected.",
                "details": {"large": "do not include in summary"},
                "errors": [],
            },
            "private_frameworks": {
                "status": "warning",
                "code": "private_framework_unavailable",
                "message": "Private framework needs runtime verification.",
                "details": {"large": "do not include in summary"},
                "errors": [],
            },
        },
        "capabilities": {
            "sqlite_schema_reads": {"status": "ok", "basis": "content_free_schema_probe"},
            "sqlite_writes": {
                "status": "unknown",
                "basis": "write_access_and_semantics_not_probed",
                "requires_runtime_verification": True,
            },
            "command_schema": {
                "delete_attachment_db": {"status": "ok", "supported": True},
                "add_tag_db": {"status": "blocked", "supported": False},
            },
        },
        "errors": [],
    }


def load_server_module() -> object:
    name = f"apple_reminders_server_doctor_contract_{id(object())}"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / "mcp/server.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load MCP server")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class DoctorSummaryCliTests(unittest.TestCase):
    def run_main(self, argv: list[str]) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with mock.patch.object(
            reminders_doctor, "collect_report", return_value=sample_full_report()
        ), mock.patch.object(reminders_doctor.sys, "stdout", output):
            code = reminders_doctor.main(argv)
        return code, json.loads(output.getvalue())

    def test_default_cli_report_is_a_compact_behavior_summary(self) -> None:
        code, report = self.run_main(["--compact"])

        self.assertEqual(code, 0)
        self.assertEqual(report["detail_level"], "summary")
        self.assertTrue(report["privacy"]["content_free"])
        self.assertNotIn("details", report["checks"]["platform"])
        self.assertEqual(
            report["capabilities"]["command_schema"],
            {"supported": 1, "blocked": 1, "total": 2},
        )
        self.assertLess(len(json.dumps(report, separators=(",", ":"))), 4_000)

    def test_full_cli_report_preserves_the_existing_diagnostic_shape(self) -> None:
        code, report = self.run_main(["--compact", "--detail-level", "full"])

        self.assertEqual(code, 0)
        self.assertEqual(report["detail_level"], "full")
        self.assertEqual(
            report["checks"]["platform"]["details"],
            {"large": "do not include in summary"},
        )
        command_schema = report["capabilities"]["command_schema"]
        self.assertIn("delete_attachment_db", command_schema)
        self.assertTrue(
            {"create_reminder_db", "cleanup_tags", "delete_reminder_db"}.isdisjoint(
                command_schema
            )
        )


class DoctorSummaryMcpContractTests(unittest.TestCase):
    def test_doctor_tool_exposes_summary_default_and_explicit_full_mode(self) -> None:
        schema = json.loads(
            (PLUGIN_ROOT / "schemas/mcp-tools.json").read_text(encoding="utf-8")
        )
        doctor = next(
            tool for tool in schema["tools"] if tool["name"] == "diagnose_reminders"
        )

        detail = doctor["inputSchema"]["properties"]["detail_level"]
        self.assertEqual(detail["enum"], ["summary", "full"])
        self.assertEqual(detail["default"], "summary")

    def test_mcp_initialize_instructions_do_not_repeat_a_long_playbook(self) -> None:
        server = load_server_module()
        runtime = server.McpRuntime()

        response = runtime.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        )

        instructions = response["result"]["instructions"]
        self.assertLessEqual(len(instructions), 220)
        self.assertNotIn("diagnose_reminders first", instructions)


if __name__ == "__main__":
    unittest.main()
