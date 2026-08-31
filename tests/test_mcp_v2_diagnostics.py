from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
SCRIPTS = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(PLUGIN_ROOT))
sys.path.insert(0, str(SCRIPTS))

from mcp.v2_contract import validate_public_result
from mcp.v2_diagnostics import DiagnosticsFacade
import reminders_doctor


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


def diagnostic_attestations(mode: str = "metadata_only") -> dict[str, Any]:
    experimental = mode == "experimental_toolchain"
    return {
        "execution": {
            "mode": mode,
            "developer_tool_process_attempted": experimental,
            "compiler_process_attempted": experimental,
            "install_request_attempted": False,
        },
        "capabilities": {
            "runtime_boundaries": reminders_doctor.runtime_boundary_metadata()
        },
    }


class DiagnosticsBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def doctor(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(deepcopy(arguments))
        mode = str(arguments.get("execution_mode", "metadata_only"))
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
            **diagnostic_attestations(mode),
        }


class DiagnosticsFacadeTests(unittest.TestCase):
    def test_experimental_toolchain_mode_is_explicit_and_scope_bound(self) -> None:
        backend = DiagnosticsBackend()
        facade = DiagnosticsFacade(
            doctor_call=backend.doctor,
            environment_fingerprint=lambda: FINGERPRINT,
        )

        result = facade.call(
            "diagnose_reminders",
            {
                "scope": "native_extension",
                "detail_level": "summary",
                "execution_mode": "experimental_toolchain",
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            backend.calls,
            [
                {
                    "scope": "native_extension",
                    "detail_level": "summary",
                    "execution_mode": "experimental_toolchain",
                }
            ],
        )

        for forbidden_scope in ("core", "access", "tags", "packaging"):
            with self.subTest(forbidden_scope=forbidden_scope):
                core_backend = DiagnosticsBackend()
                core_result = DiagnosticsFacade(
                    doctor_call=core_backend.doctor,
                    environment_fingerprint=lambda: FINGERPRINT,
                ).call(
                    "diagnose_reminders",
                    {
                        "scope": forbidden_scope,
                        "execution_mode": "experimental_toolchain",
                    },
                )

                self.assertFalse(core_result["ok"])
                self.assertEqual(
                    core_result["error"]["reason_code"],
                    "experimental_toolchain_scope_required",
                )
                self.assertEqual(core_backend.calls, [])

    def test_core_diagnosis_never_executes_clang_shim_when_clt_missing(self) -> None:
        commands: list[list[str]] = []

        def runner(
            argv: list[str], *, timeout: float = 20.0
        ) -> subprocess.CompletedProcess[str]:
            commands.append(argv)
            return subprocess.CompletedProcess(
                argv,
                1,
                "",
                "xcrun: error: install requested for command line developer tools",
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            scripts = root / "scripts"
            home.mkdir()
            scripts.mkdir()
            paths = reminders_doctor.default_paths(home=home, script_dir=scripts)
            paths["helper_source"].write_text(
                "#import <Foundation/Foundation.h>\nint main(void) { return 0; }\n",
                encoding="utf-8",
            )
            paths["adapter_source"].write_text("# synthetic adapter\n", encoding="utf-8")
            paths["reminders_app_candidates"] = []
            paths["public_frameworks"] = {
                name: root / f"{name}.framework"
                for name in ("Foundation", "AppKit", "ImageIO")
            }
            for framework in paths["public_frameworks"].values():
                framework.mkdir()
            paths["private_frameworks"] = {
                name: root / f"missing/{name}.framework/{name}"
                for name in ("ReminderKit", "ReminderKitInternal")
            }

            def doctor(arguments: dict[str, Any]) -> dict[str, Any]:
                report = reminders_doctor.collect_report(
                    paths,
                    system_info={
                        "system": "Darwin",
                        "macos_version": "26.5",
                        "kernel_release": "25.5.0",
                        "machine": "arm64",
                        "python_version": "3.11.13",
                    },
                    which=lambda command: (
                        "/usr/bin/clang" if command == "clang" else None
                    ),
                    runner=runner,
                )
                return (
                    reminders_doctor.summarize_report(report)
                    if arguments.get("detail_level") == "summary"
                    else report
                )

            facade = DiagnosticsFacade(
                doctor_call=doctor,
                environment_fingerprint=lambda: FINGERPRINT,
            )
            result = facade.call(
                "diagnose_reminders",
                {"scope": "core", "detail_level": "summary"},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(commands, [])
        self.assertEqual(
            result["data"]["execution"],
            {
                "mode": "metadata_only",
                "developer_tool_process_attempted": False,
                "compiler_process_attempted": False,
                "install_request_attempted": False,
            },
        )
        self.assertEqual(
            set(result["data"]["capability_boundaries"]),
            {"core", "compiler_free_private", "compiler_required_private"},
        )
        self.assertFalse(result["data"]["privacy"]["prompt_triggered"])
        validate_public_result("diagnose_reminders", result)

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
        self.assertEqual(
            backend.calls,
            [
                {
                    "scope": "native_extension",
                    "detail_level": "summary",
                    "execution_mode": "metadata_only",
                }
            ],
        )
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
                **diagnostic_attestations(),
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
                **diagnostic_attestations(),
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
        self.assertEqual(
            calls,
            [
                {
                    "scope": "core",
                    "detail_level": "summary",
                    "execution_mode": "metadata_only",
                }
            ],
        )
        validate_public_result("diagnose_reminders", result)

    def test_diagnosis_runs_once_and_projects_only_requested_scope(self) -> None:
        backend = DiagnosticsBackend()
        facade = DiagnosticsFacade(
            doctor_call=backend.doctor,
            environment_fingerprint=lambda: FINGERPRINT,
        )

        result = facade.call("diagnose_reminders", {"scope": "core"})

        self.assertEqual(
            backend.calls,
            [
                {
                    "scope": "core",
                    "detail_level": "summary",
                    "execution_mode": "metadata_only",
                }
            ],
        )
        self.assertEqual(
            [check["name"] for check in result["data"]["checks"]], ["platform"]
        )
        self.assertEqual(result["data"]["overall"], "ready")
        self.assertNotIn("private_frameworks", str(result))

    def test_core_packaging_diagnosis_excludes_private_helper_toolchain(self) -> None:
        backend = DiagnosticsBackend()

        def doctor(arguments: dict[str, Any]) -> dict[str, Any]:
            result = backend.doctor(arguments)
            result["checks"].update(
                {
                    "helper_toolchain": {
                        "status": "unknown",
                        "code": "helper_syntax_check_skipped",
                        "message": "Private-helper compiler gate was not run.",
                    },
                    "local_artifacts": {
                        "status": "ok",
                        "code": "artifact_metadata_collected",
                        "message": "Package metadata is available.",
                    },
                }
            )
            return result

        result = DiagnosticsFacade(
            doctor_call=doctor,
            environment_fingerprint=lambda: FINGERPRINT,
        ).call("diagnose_reminders", {"scope": "packaging"})

        check_names = [check["name"] for check in result["data"]["checks"]]
        self.assertIn("local_artifacts", check_names)
        self.assertNotIn("helper_toolchain", check_names)

    def test_capability_projection_distinguishes_support_and_runtime_boundaries(
        self,
    ) -> None:
        def doctor(arguments: dict[str, Any]) -> dict[str, Any]:
            attestations = diagnostic_attestations()
            return {
                "ok": True,
                "status": "degraded",
                "checks": {
                    "helper_toolchain": {
                        "status": "blocked",
                        "code": "clang_missing",
                        "message": "The helper compiler is unavailable.",
                    }
                },
                "execution": attestations["execution"],
                "capabilities": {
                    **attestations["capabilities"],
                    "stable_core": {
                        "capability": "stable_core",
                        "support_tier": "stable_core",
                        "api_boundary": "documented_eventkit",
                        "compiler_requirement": "not_required",
                        "build_compatibility": "not_applicable",
                        "schema_compatibility": "not_applicable",
                        "runtime_state": "documented_api",
                        "reason_code": "documented_eventkit_core",
                        "available": True,
                        "runtime_verification_required": False,
                    },
                    "experimental_internals": {
                        "image_attachment_mutation": {
                            "capability": "image_attachment_mutation",
                            "support_tier": "experimental_internals",
                            "api_boundary": "private_apple_internals",
                            "compiler_requirement": "required",
                            "build_compatibility": "allowlisted",
                            "schema_compatibility": "unverified",
                            "runtime_state": "runtime_unverified",
                            "reason_code": "compiler_required",
                            "available": False,
                            "runtime_verification_required": True,
                        },
                        "url_attachment_mutation": {
                            "capability": "url_attachment_mutation",
                            "support_tier": "experimental_internals",
                            "api_boundary": "private_apple_internals",
                            "compiler_requirement": "not_required",
                            "build_compatibility": "unsupported",
                            "schema_compatibility": "unverified",
                            "runtime_state": "runtime_unverified",
                            "reason_code": "unsupported_build",
                            "available": False,
                            "runtime_verification_required": True,
                        },
                    },
                },
                "privacy": deepcopy(CONTENT_FREE_PRIVACY),
            }

        facade = DiagnosticsFacade(
            doctor_call=doctor,
            environment_fingerprint=lambda: FINGERPRINT,
        )
        result = facade.call("diagnose_reminders", {"scope": "attachments"})

        projected = {
            item["capability"]: item for item in result["data"]["capabilities"]
        }
        self.assertEqual(
            projected["image_attachment_mutation"]["compiler_requirement"],
            "required",
        )
        self.assertEqual(
            projected["image_attachment_mutation"]["reason_code"],
            "compiler_required",
        )
        self.assertEqual(
            projected["url_attachment_mutation"]["reason_code"],
            "unsupported_build",
        )
        self.assertNotIn("stable_core", projected)
        validate_public_result("diagnose_reminders", result)

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

    def test_metadata_diagnosis_rejects_process_or_install_attestations(self) -> None:
        cases = (
            (
                {"developer_tool_process_attempted": True},
                "metadata_diagnosis_executed_process",
            ),
            (
                {"compiler_process_attempted": True},
                "metadata_diagnosis_executed_process",
            ),
            (
                {"install_request_attempted": True},
                "doctor_install_request_forbidden",
            ),
        )
        for override, reason_code in cases:
            with self.subTest(override=override):
                backend = DiagnosticsBackend()

                def unsafe_doctor(arguments: dict[str, Any]) -> dict[str, Any]:
                    result = backend.doctor(arguments)
                    result["execution"].update(override)
                    return result

                result = DiagnosticsFacade(
                    doctor_call=unsafe_doctor,
                    environment_fingerprint=lambda: FINGERPRINT,
                ).call("diagnose_reminders", {"scope": "core"})

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["reason_code"], reason_code)
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
