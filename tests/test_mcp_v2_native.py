from __future__ import annotations

import unittest
from copy import deepcopy
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
sys.path.insert(0, str(PLUGIN_ROOT))

from mcp.v2_native import NativeFacade
from mcp.v2_contract import validate_public_result


SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from reminders_service import (  # noqa: E402
    ExactRead,
    Guard,
    MutationOutcome,
    ReferenceRejected,
)


FINGERPRINT = "a" * 64
OPERATION_ID = "12345678-1234-4234-9234-1234567890ab"


class Backend:
    def __init__(self) -> None:
        self.eventkit_calls: list[tuple[str, dict[str, Any]]] = []
        self.adapter_calls: list[tuple[str, dict[str, Any]]] = []
        self.doctor_calls: list[dict[str, Any]] = []
        self.environment = FINGERPRINT
        self.eventkit_error: Exception | None = None
        self.eventkit_payload: dict[str, Any] | None = None
        self.adapter_errors: dict[str, Exception] = {}
        self.preview_payloads: dict[str, dict[str, Any]] = {}
        self.apply_payloads: dict[str, dict[str, Any]] = {}
        self.native_reads: list[tuple[Guard, dict[str, Any]]] = []
        self.native_mutations: list[tuple[Guard, str, dict[str, Any]]] = []
        self.native_read_payload: dict[str, Any] = {}
        self.native_mutation_payloads: dict[str, Any] = {}
        self.native_mutation_errors: dict[str, Exception] = {}

    def eventkit(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.eventkit_calls.append((operation, deepcopy(arguments)))
        if self.eventkit_error is not None:
            raise self.eventkit_error
        if self.eventkit_payload is not None:
            return deepcopy(self.eventkit_payload)
        assert operation == "ensure_reminder_list"
        return {
            "ok": True,
            "status": "verified",
            "operation": operation,
            "operation_id": OPERATION_ID,
            "backend": "eventkit_public_sdk",
            "target": {"source_id": arguments["source_id"], "list_id": "LIST-1"},
            "before": {},
            "after": {
                "id": "LIST-1",
                "title": arguments["name"],
                "type": "caldav",
                "allows_content_modifications": True,
                "subscribed": False,
                "immutable": False,
                "source": {
                    "id": arguments["source_id"],
                    "title": "iCloud",
                    "type": "caldav",
                    "is_delegate": False,
                    "reminder_calendar_count": 3,
                },
            },
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "delete_list_in_reminders",
                "automatic_retry_safe": True,
            },
        }

    def adapter(self, command: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.adapter_calls.append((command, deepcopy(arguments)))
        if command in self.adapter_errors:
            raise self.adapter_errors[command]
        if arguments.get("apply"):
            return deepcopy(self.apply_payloads[command])
        return deepcopy(self.preview_payloads[command])

    def doctor(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.doctor_calls.append(deepcopy(arguments))
        return {
            "ok": True,
            "status": "degraded",
            "summary": {"ok": 2, "warning": 1, "blocked": 0, "unknown": 0},
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
            "privacy": {
                "content_free": True,
                "permission_prompt_attempted": False,
                "reminder_rows_read": False,
            },
        }

    def fingerprint(self) -> str:
        return self.environment

    def native_read(self, guard: Guard, arguments: dict[str, Any]) -> dict[str, Any]:
        self.native_reads.append((guard, deepcopy(arguments)))
        return deepcopy(self.native_read_payload)

    def native_mutation(
        self,
        guard: Guard,
        command: str,
        arguments: dict[str, Any],
    ) -> MutationOutcome:
        self.native_mutations.append((guard, command, deepcopy(arguments)))
        if command in self.native_mutation_errors:
            raise self.native_mutation_errors[command]
        return deepcopy(self.native_mutation_payloads[command])


class References:
    def __init__(self) -> None:
        self.guard = Guard(
            reminder_id="REMINDER-1",
            store_identity="STORE-1",
            public_concurrency_value="2026-08-25T00:00:00Z",
        )
        self.invalidated: list[str] = []
        self.resolved: list[str] = []
        self.fresh_reads: list[str] = []
        self.reject = False
        self.fail_fresh_read = False

    def revalidate_reference(self, reference: str) -> Guard:
        self.resolved.append(reference)
        if self.reject:
            raise ReferenceRejected("concurrent_modification", "Read the reminder again")
        return self.guard

    def invalidate_reference(self, reference: str) -> None:
        self.invalidated.append(reference)

    def read_exact(self, reminder_id: str) -> ExactRead:
        self.fresh_reads.append(reminder_id)
        if self.fail_fresh_read:
            raise RuntimeError("canonical final read failed")
        return ExactRead(
            reminder={"id": reminder_id, "title": "Fresh"},
            reference=f"rev1.{'n' * 32}",
        )


def mutation_payload(
    operation: str,
    *,
    status: str = "verified",
    after: dict[str, Any] | None = None,
    recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": status not in {"failed_no_mutation", "failed_manual_repair_required"},
        "status": status,
        "operation": operation,
        "operation_id": OPERATION_ID,
        "backend": "sqlite_private_maintenance",
        "target": {},
        "before": {},
        "after": after or {},
        "verification": {"state": "read_back", "write_performed": True},
        "recovery": recovery or {"semantics": "not_applicable"},
    }


class NativeFacadeTests(unittest.TestCase):
    def make_facade(
        self,
        backend: Backend,
        *,
        references: References | None = None,
    ) -> NativeFacade:
        return NativeFacade(
            eventkit_call=backend.eventkit,
            adapter_call=backend.adapter,
            doctor_call=backend.doctor,
            environment_fingerprint=backend.fingerprint,
            references=references or References(),
            native_read=backend.native_read,
            native_mutation=backend.native_mutation,
        )

    def test_ensure_list_uses_exact_source_and_replays_idempotently(self) -> None:
        backend = Backend()
        facade = self.make_facade(backend)
        arguments = {
            "source_id": "SOURCE-1",
            "name": "  Work  ",
            "idempotency_key": "ensure:work:1",
        }

        first = facade.call("ensure_reminder_list", arguments)
        second = facade.call("ensure_reminder_list", arguments)

        self.assertEqual(
            backend.eventkit_calls,
            [("ensure_reminder_list", {"source_id": "SOURCE-1", "name": "Work"})],
        )
        self.assertEqual(first["target"], {"source_id": "SOURCE-1", "list_id": "LIST-1"})
        self.assertEqual(first["after"]["id"], "LIST-1")
        self.assertNotIn("color", first["after"])
        self.assertNotIn("emblem", first["after"])
        self.assertEqual(first["after"]["source"]["reminder_list_count"], 3)
        self.assertNotIn("calendar", str(first).casefold())
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(len(first["idempotency_key_hash"]), 64)
        self.assertNotIn("ensure:work:1", str(first))
        validate_public_result("ensure_reminder_list", first, "committed")
        validate_public_result("ensure_reminder_list", second, "committed")

    def test_idempotency_key_cannot_be_reused_for_a_different_list(self) -> None:
        backend = Backend()
        facade = self.make_facade(backend)
        facade.call(
            "ensure_reminder_list",
            {"source_id": "SOURCE-1", "name": "Work", "idempotency_key": "same:key"},
        )

        result = facade.call(
            "ensure_reminder_list",
            {"source_id": "SOURCE-1", "name": "Home", "idempotency_key": "same:key"},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_no_mutation")
        self.assertEqual(result["error"]["reason_code"], "idempotency_key_conflict")
        self.assertEqual(len(backend.eventkit_calls), 1)

    def test_ensure_list_dispatch_exception_is_pending_and_replays_without_redispatch(self) -> None:
        backend = Backend()
        backend.eventkit_error = RuntimeError("native helper disappeared")
        facade = self.make_facade(backend)
        arguments = {
            "source_id": "SOURCE-1",
            "name": "Work",
            "idempotency_key": "ensure:work:unknown",
        }

        first = facade.call("ensure_reminder_list", arguments)
        second = facade.call("ensure_reminder_list", arguments)

        self.assertEqual(len(backend.eventkit_calls), 1)
        self.assertEqual(first["status"], "committed_verification_pending")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(first["error"]["code"], "sync_pending")
        self.assertEqual(first["warnings"][0]["code"], "verification_pending")
        self.assertNotIn("next_action", first)
        validate_public_result("ensure_reminder_list", first, "unknown")
        validate_public_result("ensure_reminder_list", second, "unknown")

    def test_ensure_list_malformed_post_dispatch_reply_is_not_false_no_mutation(self) -> None:
        backend = Backend()
        backend.eventkit_payload = {"ok": True, "unexpected": "shape"}
        facade = self.make_facade(backend)

        result = facade.call(
            "ensure_reminder_list",
            {
                "source_id": "SOURCE-1",
                "name": "Work",
                "idempotency_key": "ensure:work:malformed",
            },
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertEqual(result["error"]["reason_code"], "invalid_eventkit_list_receipt")

    def test_diagnosis_normalizes_content_free_doctor_and_never_prompts(self) -> None:
        backend = Backend()
        facade = self.make_facade(backend)

        result = facade.call(
            "diagnose_reminders", {"scope": "native_extension", "detail_level": "summary"}
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
        self.assertEqual(backend.doctor_calls, [{"detail_level": "summary"}])

    def test_blocked_doctor_report_is_still_a_successful_diagnostic_read(self) -> None:
        backend = Backend()

        def blocked_doctor(arguments: dict[str, Any]) -> dict[str, Any]:
            backend.doctor_calls.append(deepcopy(arguments))
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
                "privacy": {
                    "content_free": True,
                    "permission_prompt_attempted": False,
                    "reminder_rows_read": False,
                },
            }

        facade = NativeFacade(
            eventkit_call=backend.eventkit,
            adapter_call=backend.adapter,
            doctor_call=blocked_doctor,
            environment_fingerprint=backend.fingerprint,
            references=References(),
            native_read=backend.native_read,
            native_mutation=backend.native_mutation,
        )

        result = facade.call(
            "diagnose_reminders", {"scope": "core", "detail_level": "summary"}
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["data"]["overall"], "blocked")

    def test_diagnosis_runs_once_then_reports_only_the_requested_area(self) -> None:
        backend = Backend()
        facade = self.make_facade(backend)

        result = facade.call("diagnose_reminders", {"scope": "core"})

        self.assertEqual(backend.doctor_calls, [{"detail_level": "summary"}])
        self.assertEqual([check["name"] for check in result["data"]["checks"]], ["platform"])
        self.assertEqual(result["data"]["overall"], "ready")
        self.assertNotIn("private_frameworks", str(result))

    def test_withheld_maintenance_scopes_are_not_public_diagnosis_scopes(self) -> None:
        for scope in ("maintenance", "snapshots"):
            with self.subTest(scope=scope):
                backend = Backend()
                result = self.make_facade(backend).call(
                    "diagnose_reminders", {"scope": scope}
                )

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["reason_code"], "invalid_diagnosis_scope")
                self.assertEqual(backend.doctor_calls, [])

    def test_create_section_preserves_exact_list_id_and_receipt(self) -> None:
        backend = Backend()
        backend.preview_payloads["create_section"] = mutation_payload(
            "create_section",
            after={
                "section": {
                    "id": "SECTION-1",
                    "name": "Next",
                    "list_id": "LIST-1",
                    "list_title": "Work",
                    "order": 0,
                }
            },
        )
        facade = self.make_facade(backend)

        result = facade.call(
            "create_reminder_section", {"list_id": "LIST-1", "name": "Next"}
        )

        self.assertEqual(
            backend.adapter_calls,
            [("create_section", {"list_id": "LIST-1", "name": "Next"})],
        )
        self.assertEqual(result["operation"], "create_reminder_section")
        self.assertEqual(result["backend"], "native_extension")
        self.assertEqual(result["target"], {"list_id": "LIST-1", "section_id": "SECTION-1"})
        self.assertEqual(result["after"]["id"], "SECTION-1")
        self.assertEqual(result["verification"]["final_read"], True)
        self.assertEqual(result["verification"]["matched"], True)
        validate_public_result("create_reminder_section", result, "committed")

    def test_create_section_dispatch_exception_is_not_false_no_mutation(self) -> None:
        backend = Backend()
        backend.adapter_errors["create_section"] = RuntimeError("helper response lost")
        facade = self.make_facade(backend)

        result = facade.call(
            "create_reminder_section", {"list_id": "LIST-1", "name": "Next"}
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertTrue(result["ok"])
        self.assertEqual(result["target"], {"list_id": "LIST-1", "section_id": None})
        self.assertEqual(result["error"]["reason_code"], "native_section_dispatch_failed")
        self.assertEqual(result["warnings"][0]["code"], "verification_pending")
        validate_public_result("create_reminder_section", result, "unknown")

    def test_create_section_malformed_post_dispatch_reply_is_pending(self) -> None:
        backend = Backend()
        backend.preview_payloads["create_section"] = {"ok": True, "status": "verified"}
        facade = self.make_facade(backend)

        result = facade.call(
            "create_reminder_section", {"list_id": "LIST-1", "name": "Next"}
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertEqual(result["error"]["reason_code"], "invalid_native_section_receipt")

    def test_reference_inspection_resolves_guard_and_bounds_native_data(self) -> None:
        backend = Backend()
        references = References()
        backend.native_read_payload = {
            "reminder_id": "REMINDER-1",
            "section": None,
            "tags": [],
            "attachments": [
                {
                    "id": "ATTACHMENT-1",
                    "type": "url",
                    "url": "https://example.com",
                    "sync": {
                        "fields_available": True,
                        "mobile_visible_likely": True,
                        "has_server_record": True,
                        "in_cloud": 1,
                        "current_local_version": 7,
                        "latest_synced_version": 7,
                    },
                },
                {
                    "id": "ATTACHMENT-2",
                    "type": "url",
                    "url": "https://example.org",
                    "sync": {
                        "fields_available": True,
                        "mobile_visible_likely": True,
                        "has_server_record": True,
                        "in_cloud": 1,
                        "current_local_version": 7,
                        "latest_synced_version": 7,
                    },
                },
            ],
            "sync": None,
        }
        facade = self.make_facade(backend, references=references)
        reference = f"rev1.{'x' * 32}"

        result = facade.call(
            "inspect_reminder_native",
            {
                "kind": "reminder",
                "reference": reference,
                "include": ["attachments", "sync"],
                "attachment_type": "url",
                "limit": 1,
            },
        )

        self.assertEqual(references.resolved, [reference])
        self.assertEqual(
            backend.native_reads,
            [
                (
                    references.guard,
                    {
                        "include": ["attachments", "sync"],
                        "attachment_type": "url",
                        "limit": 1,
                    },
                )
            ],
        )
        self.assertEqual(references.fresh_reads, ["REMINDER-1"])
        self.assertEqual(references.invalidated, [reference])
        self.assertEqual(result["data"]["reference"], f"rev1.{'n' * 32}")
        self.assertEqual(result["data"]["returned"], 1)
        self.assertTrue(result["data"]["truncated"])
        self.assertEqual(len(result["data"]["attachments"]), 1)

    def test_reference_inspection_fails_closed_when_reference_refresh_fails(self) -> None:
        backend = Backend()
        references = References()
        references.fail_fresh_read = True
        backend.native_read_payload = {
            "reminder_id": "REMINDER-1",
            "attachments": [],
        }
        facade = self.make_facade(backend, references=references)
        reference = f"rev1.{'x' * 32}"

        result = facade.call(
            "inspect_reminder_native",
            {
                "kind": "reminder",
                "reference": reference,
                "include": ["attachments"],
            },
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_no_mutation")
        self.assertEqual(result["error"]["code"], "sync_pending")
        self.assertEqual(references.fresh_reads, ["REMINDER-1"])
        self.assertEqual(references.invalidated, [])

    def test_reference_inspection_projects_existing_adapter_payload_shape(self) -> None:
        backend = Backend()
        backend.native_read_payload = {
            "id": "REMINDER-1",
            "version": 7,
            "list_id": "LIST-1",
            "list": "Work",
            "section_id": "SECTION-1",
            "section": "Next",
            "tags": [
                {
                    "object_pk": 99,
                    "label": {
                        "uuid": "TAG-1",
                        "name": "next",
                        "canonical_name": "next",
                        "account_identifier": "ACCOUNT-1",
                    },
                }
            ],
            "attachment_items": [
                {
                    "pk": 101,
                    "id": "ATTACHMENT-1",
                    "type": "url",
                    "url": "https://example.com",
                    "sync": {"fields_available": False},
                }
            ],
        }
        facade = self.make_facade(backend)

        result = facade.call(
            "inspect_reminder_native",
            {
                "kind": "reminder",
                "reference": f"rev1.{'x' * 32}",
                "include": ["section", "tags", "attachments"],
            },
        )["data"]

        self.assertEqual(
            result["section"],
            {
                "id": "SECTION-1",
                "name": "Next",
                "list_id": "LIST-1",
                "list_title": "Work",
                "order": None,
            },
        )
        self.assertEqual(result["tags"][0]["id"], "TAG-1")
        self.assertEqual(result["attachments"][0]["id"], "ATTACHMENT-1")
        self.assertNotIn("object_pk", str(result))
        self.assertNotIn("'pk'", str(result))

    def test_section_and_tag_inspection_keep_exact_public_scope(self) -> None:
        backend = Backend()
        backend.preview_payloads["list_sections"] = {
            "ok": True,
            "sections": [
                {
                    "id": "SECTION-1",
                    "name": "Next",
                    "list_id": "LIST-1",
                    "list_title": "Work",
                    "order": 0,
                }
            ],
            "truncated": False,
        }
        facade = self.make_facade(backend)

        result = facade.call(
            "inspect_reminder_native",
            {"kind": "sections", "list_id": "LIST-1", "limit": 10},
        )

        self.assertEqual(
            backend.adapter_calls,
            [("list_sections", {"list_id": "LIST-1", "limit": 10})],
        )
        self.assertEqual(result["data"]["list_id"], "LIST-1")
        self.assertEqual(result["data"]["sections"][0]["id"], "SECTION-1")

    def test_native_organization_uses_guarded_port_and_rotates_reference(self) -> None:
        backend = Backend()
        references = References()
        receipt = mutation_payload(
            "move_to_section",
            after={
                "reminder_id": "REMINDER-1",
                "section": {"id": "SECTION-1", "name": "Next", "list_id": "LIST-1"},
                "tags": [],
            },
        )
        receipt["before"] = {
            "reminder_id": "REMINDER-1",
            "section": None,
            "tags": [],
        }
        backend.native_mutation_payloads["move_to_section"] = MutationOutcome(
            receipt=receipt,
            mutation_state="committed",
        )
        facade = self.make_facade(backend, references=references)
        reference = f"rev1.{'x' * 32}"

        result = facade.call(
            "organize_reminder",
            {
                "reference": reference,
                "action": {"kind": "move_to_section", "section_id": "SECTION-1"},
            },
        )

        self.assertEqual(
            backend.native_mutations,
            [(references.guard, "move_to_section", {"section_id": "SECTION-1"})],
        )
        self.assertEqual(references.invalidated, [reference])
        self.assertEqual(references.fresh_reads, ["REMINDER-1"])
        self.assertEqual(result["operation"], "organize_reminder.move_to_section")
        self.assertNotIn("reference", result["before"])
        self.assertEqual(result["after"]["reference"], f"rev1.{'n' * 32}")
        self.assertEqual(result["target"]["reminder_id"], "REMINDER-1")
        self.assertEqual(
            result["verification"],
            {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
                "matched": True,
            },
        )
        validate_public_result("organize_reminder", result, "committed")

    def test_unknown_native_mutation_invalidates_without_issuing_a_reference(self) -> None:
        backend = Backend()
        references = References()
        backend.native_mutation_payloads["add_tag"] = MutationOutcome(
            receipt=mutation_payload(
                "add_tag",
                status="committed_verification_pending",
            ),
            mutation_state="unknown",
        )
        facade = self.make_facade(backend, references=references)
        reference = f"rev1.{'x' * 32}"

        result = facade.call(
            "organize_reminder",
            {"reference": reference, "action": {"kind": "add_tag", "tag": "next"}},
        )

        self.assertEqual(references.invalidated, [reference])
        self.assertEqual(references.fresh_reads, [])
        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertIsNone(result["after"])
        self.assertEqual(
            result["verification"],
            {
                "state": "pending",
                "write_performed": None,
                "final_read": False,
                "matched": None,
            },
        )

    def test_manual_repair_receipt_survives_the_public_native_facade(self) -> None:
        backend = Backend()
        references = References()
        receipt = mutation_payload(
            "attach_url",
            status="failed_manual_repair_required",
            recovery={
                "semantics": "inspect_and_repair_manually",
                "automatic_retry_safe": False,
                "manual_action": "Inspect both attachments before changing either one.",
            },
        )
        receipt["verification"] = {
            "state": "partial",
            "write_performed": None,
            "final_read": False,
        }
        receipt["warnings"] = [
            {
                "code": "manual_repair_required",
                "message": "Automatic compensation did not complete.",
            }
        ]
        receipt["error"] = {
            "code": "sync_pending",
            "reason_code": "compensation_failed",
            "message": "Inspect the exact attachments before another write.",
            "retryable": False,
        }
        backend.native_mutation_payloads["attach_url"] = MutationOutcome(
            receipt=receipt,
            mutation_state="unknown",
        )
        facade = self.make_facade(backend, references=references)
        reference = f"rev1.{'x' * 32}"

        result = facade.call(
            "change_reminder_attachment",
            {
                "reference": reference,
                "action": {"kind": "attach_url", "url": "https://example.com"},
            },
        )

        self.assertEqual(result["status"], "failed_manual_repair_required")
        self.assertFalse(result["ok"])
        self.assertEqual(result["recovery"]["semantics"], "inspect_and_repair_manually")
        self.assertEqual(result["warnings"][0]["code"], "manual_repair_required")
        self.assertEqual(references.invalidated, [reference])
        validate_public_result("change_reminder_attachment", result, "unknown")

    def test_guarded_port_exception_is_unknown_and_consumes_reference(self) -> None:
        backend = Backend()
        references = References()
        backend.native_mutation_errors["add_tag"] = RuntimeError("lost helper response")
        facade = self.make_facade(backend, references=references)
        reference = f"rev1.{'x' * 32}"

        result = facade.call(
            "organize_reminder",
            {"reference": reference, "action": {"kind": "add_tag", "tag": "next"}},
        )

        self.assertEqual(references.invalidated, [reference])
        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertTrue(result["ok"])
        self.assertEqual(result["error"]["code"], "sync_pending")
        self.assertEqual(result["verification"]["state"], "pending")
        self.assertIsNone(result["verification"]["write_performed"])
        self.assertFalse(result["verification"]["final_read"])
        validate_public_result("organize_reminder", result, "unknown")

    def test_malformed_post_dispatch_outcome_is_unknown_and_consumes_reference(self) -> None:
        backend = Backend()
        references = References()
        backend.native_mutation_payloads["remove_tag"] = {"not": "an outcome"}
        facade = self.make_facade(backend, references=references)
        reference = f"rev1.{'x' * 32}"

        result = facade.call(
            "organize_reminder",
            {"reference": reference, "action": {"kind": "remove_tag", "tag": "old"}},
        )

        self.assertEqual(references.invalidated, [reference])
        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertEqual(result["error"]["reason_code"], "invalid_native_mutation_outcome")
        validate_public_result("organize_reminder", result, "unknown")

    def test_committed_native_write_with_failed_final_read_is_pending(self) -> None:
        backend = Backend()
        references = References()
        references.fail_fresh_read = True
        backend.native_mutation_payloads["add_tag"] = MutationOutcome(
            receipt=mutation_payload(
                "add_tag",
                after={"reminder_id": "REMINDER-1", "section": None, "tags": []},
            ),
            mutation_state="committed",
        )
        facade = self.make_facade(backend, references=references)

        result = facade.call(
            "organize_reminder",
            {
                "reference": f"rev1.{'x' * 32}",
                "action": {"kind": "add_tag", "tag": "next"},
            },
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertIsNone(result["after"])
        self.assertEqual(
            result["verification"],
            {
                "state": "pending",
                "write_performed": True,
                "final_read": False,
            },
        )
        self.assertEqual(result["recovery"]["semantics"], "read_reminder_before_retry")
        self.assertFalse(result["recovery"]["automatic_retry_safe"])
        validate_public_result("organize_reminder", result, "committed")

    def test_attachment_change_uses_closed_action_and_guarded_port(self) -> None:
        backend = Backend()
        references = References()
        backend.native_mutation_payloads["attach_url"] = MutationOutcome(
            receipt=mutation_payload(
                "attach_url",
                after={
                    "reminder_id": "REMINDER-1",
                    "attachments": [
                        {
                            "id": "ATTACHMENT-1",
                            "type": "url",
                            "url": "https://example.com",
                            "sync": {
                                "fields_available": True,
                                "mobile_visible_likely": True,
                                "has_server_record": True,
                                "in_cloud": 1,
                                "current_local_version": 8,
                                "latest_synced_version": 8,
                            },
                        }
                    ],
                },
            ),
            mutation_state="committed",
        )
        facade = self.make_facade(backend, references=references)

        result = facade.call(
            "change_reminder_attachment",
            {
                "reference": f"rev1.{'x' * 32}",
                "action": {"kind": "attach_url", "url": "https://example.com"},
            },
        )

        self.assertEqual(
            backend.native_mutations,
            [(references.guard, "attach_url", {"url": "https://example.com"})],
        )
        self.assertEqual(result["operation"], "change_reminder_attachment.attach_url")
        self.assertEqual(result["after"]["attachments"][0]["id"], "ATTACHMENT-1")
        validate_public_result("change_reminder_attachment", result, "committed")

    def test_reference_rejection_never_reaches_native_mutation(self) -> None:
        backend = Backend()
        references = References()
        references.reject = True
        facade = self.make_facade(backend, references=references)

        result = facade.call(
            "organize_reminder",
            {
                "reference": f"rev1.{'x' * 32}",
                "action": {"kind": "remove_tag", "tag": "old"},
            },
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "concurrent_modification")
        self.assertEqual(result["next_action"]["kind"], "fresh_read")
        self.assertEqual(backend.native_mutations, [])


if __name__ == "__main__":
    unittest.main()
