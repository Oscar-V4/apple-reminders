from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/apple-reminders"))
sys.path.insert(0, str(ROOT / "plugins/apple-reminders/scripts"))
from mcp import server
from mcp import v2_diagnostics as diagnostics
from mcp.v2_contract import validate_public_result


class FrameworkDiagnosticSummaryTests(unittest.TestCase):
    def fixture(self):
        checks = {name: {"status": "ok", "code": "static_check_passed", "message": "Static metadata."}
                  for name in diagnostics.DIAGNOSIS_CHECKS["attachments"]}
        checks["helper_toolchain"]["code"] = "native_helper_verified"
        checks["private_frameworks"].update(status="warning", code=diagnostics.FRAMEWORK_INCONCLUSIVE_CODE)
        capabilities = {name: {"capability": name, "available": True,
            "runtime_verification_required": True, "build_compatibility": "allowlisted",
            "schema_compatibility": "allowlisted", "runtime_state": "runtime_unverified"}
            for name in diagnostics.DIAGNOSIS_CAPABILITIES["attachments"]}
        return {"ok": True, "status": "degraded", "checks": checks,
            "execution": {"mode": "metadata_only", "developer_tool_process_attempted": False,
                "compiler_process_attempted": False, "install_request_attempted": False},
            "capabilities": {"runtime_boundaries": diagnostics.runtime_boundary_metadata(),
                             "experimental_internals": capabilities},
            "privacy": {"content_free": True, **{name: False for name in diagnostics.CONTENT_FREE_FALSE_FIELDS}}}

    def result(self, raw):
        facade = diagnostics.DiagnosticsFacade(doctor_call=lambda arguments: raw,
            environment_fingerprint=lambda: "a" * 64)
        with patch.object(server, "run_bounded_process", side_effect=AssertionError("process launched")):
            result = facade.call("diagnose_reminders", {"scope": "attachments"})
        validate_public_result("diagnose_reminders", result)
        return result

    def summary(self, result):
        return json.loads(server.tool_result(result, is_error=False)["content"][0]["text"])

    def test_deferred_runtime_verification_keeps_attention_without_repeat_diagnosis(self):
        raw = self.fixture()
        before = deepcopy(raw)
        result = self.result(raw)
        self.assertEqual(raw, before)
        self.assertEqual(result["data"]["overall"], "degraded")
        self.assertEqual(result["status"], "verified")
        self.assertIn(diagnostics.FRAMEWORK_INCONCLUSIVE_EXPLANATION, result["data"]["summary"])
        self.assertTrue(all(capability["runtime_verification_required"] for capability in result["data"]["capabilities"]))
        summary = self.summary(result)
        self.assertEqual(summary["outcome"], "attention_required")
        self.assertTrue(summary["needs_attention"])
        self.assertFalse(summary["may_have_mutated"])
        self.assertIsNone(summary["next_read_only_action"])
        self.assertEqual(summary["diagnostic_reason_code"], diagnostics.FRAMEWORK_INCONCLUSIVE_CODE)
        self.assertEqual(summary["diagnostic_explanation"], diagnostics.FRAMEWORK_INCONCLUSIVE_EXPLANATION)

    def test_unavailable_unknown_partial_and_additional_failures_keep_existing_action(self):
        def capability(raw):
            return next(iter(raw["capabilities"]["experimental_internals"].values()))
        mutations = {
            "helper_unverified": lambda raw: raw["checks"]["helper_toolchain"].update(code="helper_metadata_only"),
            "unreadable_framework": lambda raw: raw["checks"]["private_frameworks"].update(code="private_framework_path_unreadable"),
            "real_blocker": lambda raw: raw["checks"]["store_access"].update(status="blocked"),
            "second_warning": lambda raw: raw["checks"]["command_schema"].update(status="warning"),
            "unknown_check": lambda raw: raw["checks"]["command_schema"].update(status="unknown"),
            "unknown_os": lambda raw: capability(raw).update(build_compatibility="unsupported", available=False),
            "unknown_schema": lambda raw: capability(raw).update(schema_compatibility="unverified", available=False),
            "contradictory_os": lambda raw: capability(raw).update(build_compatibility="unsupported"),
            "runtime_not_deferred": lambda raw: capability(raw).update(runtime_verification_required=False),
            "empty_capabilities": lambda raw: raw["capabilities"].update(experimental_internals={}),
            "partial_capabilities": lambda raw: raw["capabilities"]["experimental_internals"].pop("image_attachment_mutation"),
            "missing_check": lambda raw: raw["checks"].pop("command_schema"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                raw = self.fixture()
                mutate(raw)
                result = self.result(raw)
                self.assertNotIn(diagnostics.FRAMEWORK_INCONCLUSIVE_EXPLANATION, result["data"]["summary"])
                summary = self.summary(result)
                self.assertTrue(summary["needs_attention"])
                self.assertEqual(summary["next_read_only_action"]["tool"], "diagnose_reminders")
                self.assertNotIn("diagnostic_explanation", summary)

    def test_short_text_uses_only_fixed_explanation_not_backend_summary_or_messages(self):
        result = self.result(self.fixture())
        result["data"]["summary"] = "PRIVATE BACKEND SUMMARY"
        for check in result["data"]["checks"]:
            check["message"] = "PRIVATE BACKEND MESSAGE"
        text = server.tool_result(result, is_error=False)["content"][0]["text"]
        self.assertNotIn("PRIVATE", text)
        self.assertIn(diagnostics.FRAMEWORK_INCONCLUSIVE_EXPLANATION, text)

    def test_explicit_next_action_and_errors_are_not_suppressed(self):
        result = self.result(self.fixture())
        result["next_action"] = {"kind": "fresh_read", "tool": "diagnose_reminders"}
        self.assertEqual(self.summary(result)["next_read_only_action"]["tool"], "diagnose_reminders")
        result.pop("next_action")
        result["error"] = {"code": "unexpected_error"}
        self.assertEqual(self.summary(result)["next_read_only_action"]["tool"], "diagnose_reminders")


if __name__ == "__main__":
    unittest.main()
