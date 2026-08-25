from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
sys.path.insert(0, str(PLUGIN_ROOT))

from mcp.v2_contract import validate_public_result
from mcp.v2_diagnostics import DiagnosticsFacade


FINGERPRINT = "a" * 64
CONTENT_FREE_PRIVACY = {
    "content_free": True,
    "reminder_rows_read": False,
    "reminder_titles_read": False,
    "list_section_tag_names_read": False,
    "journal_cache_backup_contents_read": False,
    "write_attempted": False,
    "permission_prompt_attempted": False,
    "application_launched": False,
    "private_framework_loaded": False,
}


class DiagnosticsBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def doctor(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(deepcopy(arguments))
        return {
            "ok": True,
            "status": "degraded",
            "checks": {
                "platform": {
                    "status": "ok",
                    "code": "macos_detected",
                    "message": "macOS was detected.",
                },
                "private_frameworks": {
                    "status": "warning",
                    "code": "private_framework_unavailable",
                    "message": "A runtime probe is required.",
                },
            },
            "privacy": deepcopy(CONTENT_FREE_PRIVACY),
        }


class DiagnosticsFacadeTests(unittest.TestCase):
    def test_diagnosis_normalizes_content_free_doctor_and_never_prompts(self) -> None:
        backend = DiagnosticsBackend()
        facade = DiagnosticsFacade(
            doctor_call=backend.doctor,
            environment_fingerprint=lambda: FINGERPRINT,
        )

        result = facade.call(
            "diagnose_reminders",
            {"scope": "native_extension", "detail_level": "summary"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["data"]["overall"], "degraded")
        self.assertEqual(result["data"]["environment_fingerprint"], FINGERPRINT)
        self.assertEqual(
            result["data"]["privacy"],
            {
                "content_free": True,
                "reminder_content_read": False,
                "prompt_triggered": False,
            },
        )
        self.assertEqual(backend.calls, [{"detail_level": "summary"}])
        validate_public_result("diagnose_reminders", result)

    def test_full_diagnosis_projects_only_bounded_scalar_details_as_facts(self) -> None:
        details = {
            "system": "Darwin",
            "syntax_check": True,
            "store_count": 2,
            "optional_value": None,
            "nested": {"must": "not escape"},
            "items": ["must not escape"],
            **{f"extra_{index:02d}": index for index in range(20)},
        }

        def doctor(arguments: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "status": "ready",
                "checks": {
                    "platform": {
                        "status": "ok",
                        "code": "macos_detected",
                        "message": "macOS was detected.",
                        "details": details,
                    }
                },
                "privacy": deepcopy(CONTENT_FREE_PRIVACY),
            }

        facade = DiagnosticsFacade(
            doctor_call=doctor,
            environment_fingerprint=lambda: FINGERPRINT,
        )

        result = facade.call(
            "diagnose_reminders", {"scope": "core", "detail_level": "full"}
        )

        facts = result["data"]["checks"][0]["facts"]
        self.assertEqual(
            facts[:4],
            [
                {"name": "system", "value": "Darwin"},
                {"name": "syntax_check", "value": True},
                {"name": "store_count", "value": 2},
                {"name": "optional_value", "value": None},
            ],
        )
        self.assertEqual(len(facts), 20)
        self.assertNotIn("nested", {fact["name"] for fact in facts})
        self.assertNotIn("items", {fact["name"] for fact in facts})
        validate_public_result("diagnose_reminders", result)

    def test_blocked_doctor_report_is_a_successful_diagnostic_read(self) -> None:
        calls: list[dict[str, Any]] = []

        def blocked_doctor(arguments: dict[str, Any]) -> dict[str, Any]:
            calls.append(deepcopy(arguments))
            return {
                "ok": False,
                "status": "blocked",
                "checks": {
                    "store_access": {
                        "status": "blocked",
                        "code": "store_unavailable",
                        "message": "The Reminders store is unavailable.",
                    }
                },
                "privacy": deepcopy(CONTENT_FREE_PRIVACY),
            }

        facade = DiagnosticsFacade(
            doctor_call=blocked_doctor,
            environment_fingerprint=lambda: FINGERPRINT,
        )

        result = facade.call(
            "diagnose_reminders", {"scope": "core", "detail_level": "summary"}
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["data"]["overall"], "blocked")
        self.assertEqual(calls, [{"detail_level": "summary"}])
        validate_public_result("diagnose_reminders", result)

    def test_diagnosis_runs_once_and_projects_only_requested_scope(self) -> None:
        backend = DiagnosticsBackend()
        facade = DiagnosticsFacade(
            doctor_call=backend.doctor,
            environment_fingerprint=lambda: FINGERPRINT,
        )

        result = facade.call("diagnose_reminders", {"scope": "core"})

        self.assertEqual(backend.calls, [{"detail_level": "summary"}])
        self.assertEqual(
            [check["name"] for check in result["data"]["checks"]], ["platform"]
        )
        self.assertEqual(result["data"]["overall"], "ready")
        self.assertNotIn("private_frameworks", str(result))

    def test_withheld_maintenance_scopes_never_call_doctor(self) -> None:
        for scope in ("maintenance", "snapshots"):
            with self.subTest(scope=scope):
                backend = DiagnosticsBackend()
                facade = DiagnosticsFacade(
                    doctor_call=backend.doctor,
                    environment_fingerprint=lambda: FINGERPRINT,
                )

                result = facade.call("diagnose_reminders", {"scope": scope})

                self.assertFalse(result["ok"])
                self.assertEqual(
                    result["error"]["reason_code"], "invalid_diagnosis_scope"
                )
                self.assertEqual(backend.calls, [])
                validate_public_result("diagnose_reminders", result)

    def test_diagnosis_fails_closed_when_doctor_attempts_prompt_or_content_read(self) -> None:
        for privacy_override in (
            {"permission_prompt_attempted": True},
            {"reminder_rows_read": True},
            {"write_attempted": True},
        ):
            with self.subTest(privacy_override=privacy_override):
                backend = DiagnosticsBackend()

                def unsafe_doctor(arguments: dict[str, Any]) -> dict[str, Any]:
                    result = backend.doctor(arguments)
                    result["privacy"].update(privacy_override)
                    return result

                facade = DiagnosticsFacade(
                    doctor_call=unsafe_doctor,
                    environment_fingerprint=lambda: FINGERPRINT,
                )

                result = facade.call("diagnose_reminders", {"scope": "core"})

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "schema_mismatch")
                self.assertEqual(
                    result["error"]["reason_code"], "doctor_not_content_free"
                )
                validate_public_result("diagnose_reminders", result)

    def test_diagnosis_requires_explicit_content_free_attestations(self) -> None:
        required_false_fields = tuple(
            name for name in CONTENT_FREE_PRIVACY if name != "content_free"
        )
        for missing_field in required_false_fields:
            with self.subTest(missing_field=missing_field):
                backend = DiagnosticsBackend()

                def incomplete_doctor(arguments: dict[str, Any]) -> dict[str, Any]:
                    result = backend.doctor(arguments)
                    result["privacy"].pop(missing_field)
                    return result

                facade = DiagnosticsFacade(
                    doctor_call=incomplete_doctor,
                    environment_fingerprint=lambda: FINGERPRINT,
                )

                result = facade.call("diagnose_reminders", {"scope": "core"})

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "schema_mismatch")
                self.assertEqual(
                    result["error"]["reason_code"], "doctor_not_content_free"
                )

    def test_doctor_transport_error_is_not_misreported_as_a_privacy_violation(self) -> None:
        facade = DiagnosticsFacade(
            doctor_call=lambda _: {
                "ok": False,
                "error": {
                    "code": "doctor_timeout",
                    "message": "The content-free Doctor timed out.",
                },
            },
            environment_fingerprint=lambda: FINGERPRINT,
        )

        result = facade.call("diagnose_reminders", {"scope": "core"})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "unexpected_error")
        self.assertEqual(result["error"]["reason_code"], "doctor_timeout")


if __name__ == "__main__":
    unittest.main()
