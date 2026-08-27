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


OPERATION_ID = "12345678-1234-4234-9234-1234567890ab"


class Backend:
    def __init__(self) -> None:
        self.adapter_calls: list[tuple[str, dict[str, Any]]] = []
        self.adapter_errors: dict[str, Exception] = {}
        self.preview_payloads: dict[str, dict[str, Any]] = {}
        self.apply_payloads: dict[str, dict[str, Any]] = {}
        self.native_reads: list[tuple[Guard, dict[str, Any]]] = []
        self.native_mutations: list[tuple[Guard, str, dict[str, Any]]] = []
        self.native_copy_mutations: list[
            tuple[Guard, Guard, str, dict[str, Any]]
        ] = []
        self.native_read_payload: dict[str, Any] = {}
        self.native_mutation_payloads: dict[str, Any] = {}
        self.native_mutation_errors: dict[str, Exception] = {}

    def adapter(self, command: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.adapter_calls.append((command, deepcopy(arguments)))
        if command in self.adapter_errors:
            raise self.adapter_errors[command]
        if arguments.get("apply"):
            return deepcopy(self.apply_payloads[command])
        return deepcopy(self.preview_payloads[command])

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

    def native_copy_mutation(
        self,
        destination_guard: Guard,
        source_guard: Guard,
        command: str,
        arguments: dict[str, Any],
    ) -> MutationOutcome:
        self.native_copy_mutations.append(
            (
                destination_guard,
                source_guard,
                command,
                deepcopy(arguments),
            )
        )
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
        self.guards: dict[str, Guard] = {}
        self.rejections: set[str] = set()
        self.revalidation_errors: set[str] = set()

    def revalidate_reference(self, reference: str) -> Guard:
        self.resolved.append(reference)
        if self.reject or reference in self.rejections:
            raise ReferenceRejected("concurrent_modification", "Read the reminder again")
        if reference in self.revalidation_errors:
            raise RuntimeError("exact reference backend unavailable")
        return self.guards.get(reference, self.guard)

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
            adapter_call=backend.adapter,
            references=references or References(),
            native_read=backend.native_read,
            native_mutation=backend.native_mutation,
            native_copy_mutation=backend.native_copy_mutation,
        )

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
        self.assertEqual(result["next_action"]["tool"], "inspect_reminder_native")
        self.assertFalse(result["next_action"]["retry_original_once"])
        validate_public_result("create_reminder_section", result, "unknown")

    def test_pending_section_receipt_preserves_identity_and_causal_error(self) -> None:
        backend = Backend()
        receipt = mutation_payload(
            "create_section",
            status="committed_verification_pending",
            after={
                "section": {
                    "id": "SECTION-1",
                    "name": "Next",
                    "list_id": "LIST-1",
                    "list_title": "Work",
                    "order": 0,
                }
            },
            recovery={
                "semantics": "inspect_exact_list_before_retry",
                "automatic_retry_safe": False,
            },
        )
        receipt["verification"] = {
            "state": "pending",
            "write_performed": True,
            "final_read": False,
        }
        receipt["warnings"] = [
            {
                "code": "journal_write_failed",
                "message": "The audit journal could not be written.",
            },
            {
                "code": "section_sync_pending",
                "message": "The section exists but its sync read-back is pending.",
            },
        ]
        backend.preview_payloads["create_section"] = receipt
        facade = self.make_facade(backend)

        result = facade.call(
            "create_reminder_section", {"list_id": "LIST-1", "name": "Next"}
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertEqual(
            result["target"], {"list_id": "LIST-1", "section_id": "SECTION-1"}
        )
        self.assertEqual(result["error"]["code"], "sync_pending")
        self.assertEqual(result["error"]["reason_code"], "section_sync_pending")
        self.assertEqual(result["next_action"]["tool"], "inspect_reminder_native")
        self.assertFalse(result["next_action"]["retry_original_once"])
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
        self.assertEqual(result["next_action"]["tool"], "inspect_reminder_native")

    def test_create_section_concurrent_receipt_points_to_section_inspection(self) -> None:
        backend = Backend()
        receipt = mutation_payload("create_section", status="failed_no_mutation")
        receipt["error"] = {
            "code": "concurrent_modification",
            "reason_code": "section_changed",
            "message": "The section list changed before the save.",
            "retryable": True,
        }
        backend.preview_payloads["create_section"] = receipt
        facade = self.make_facade(backend)

        result = facade.call(
            "create_reminder_section", {"list_id": "LIST-1", "name": "Next"}
        )

        self.assertEqual(result["status"], "failed_no_mutation")
        self.assertEqual(result["error"]["code"], "concurrent_modification")
        self.assertEqual(result["next_action"]["tool"], "inspect_reminder_native")
        self.assertFalse(result["next_action"]["retry_original_once"])
        validate_public_result("create_reminder_section", result, "not_mutated")

    def test_create_section_permission_receipt_requests_access_then_one_retry(self) -> None:
        backend = Backend()
        receipt = mutation_payload("create_section", status="failed_no_mutation")
        receipt["verification"] = {
            "state": "not_performed",
            "write_performed": False,
            "final_read": False,
        }
        receipt["error"] = {
            "code": "permission_denied",
            "reason_code": "reminders_access_denied",
            "message": "Full Reminders access is required.",
            "retryable": False,
        }
        backend.preview_payloads["create_section"] = receipt
        facade = self.make_facade(backend)

        result = facade.call(
            "create_reminder_section", {"list_id": "LIST-1", "name": "Next"}
        )

        self.assertEqual(result["status"], "failed_no_mutation")
        self.assertEqual(result["next_action"]["kind"], "request_access")
        self.assertEqual(
            result["next_action"]["tool"],
            "request_reminders_access",
        )
        self.assertTrue(result["next_action"]["retry_original_once"])
        validate_public_result("create_reminder_section", result, "not_mutated")

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

    def test_pending_native_receipt_without_error_uses_warning_as_structured_error(
        self,
    ) -> None:
        backend = Backend()
        references = References()
        receipt = mutation_payload(
            "attach_image",
            status="committed_verification_pending",
        )
        receipt["warnings"] = [
            {
                "code": "journal_write_failed",
                "message": "The audit journal could not be written.",
            },
            {
                "code": "mobile_visibility_pending",
                "message": "The native image exists but mobile visibility is pending.",
            }
        ]
        backend.native_mutation_payloads["attach_image"] = MutationOutcome(
            receipt=receipt,
            mutation_state="unknown",
        )
        facade = self.make_facade(backend, references=references)
        reference = f"rev1.{'x' * 32}"

        result = facade.call(
            "change_reminder_attachment",
            {
                "reference": reference,
                "action": {
                    "kind": "attach_image",
                    "image_path": "/tmp/synthetic.png",
                    "idempotency_key": "attachment-contract-repro",
                },
            },
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertEqual(result["error"]["code"], "sync_pending")
        self.assertEqual(
            result["error"]["reason_code"], "mobile_visibility_pending"
        )
        self.assertFalse(result["error"]["retryable"])
        self.assertEqual(references.invalidated, [reference])
        validate_public_result("change_reminder_attachment", result, "unknown")

    def test_pending_native_receipt_without_warning_uses_stable_fallback(self) -> None:
        backend = Backend()
        references = References()
        backend.native_mutation_payloads["add_tag"] = MutationOutcome(
            receipt=mutation_payload(
                "add_tag",
                status="committed_verification_pending",
                recovery={
                    "semantics": "read_before_retry",
                    "automatic_retry_safe": False,
                },
            ),
            mutation_state="unknown",
        )
        facade = self.make_facade(backend, references=references)

        result = facade.call(
            "organize_reminder",
            {
                "reference": f"rev1.{'x' * 32}",
                "action": {"kind": "add_tag", "tag": "next"},
            },
        )

        self.assertEqual(result["error"]["code"], "sync_pending")
        self.assertEqual(result["error"]["reason_code"], "verification_pending")
        self.assertEqual(result["next_action"]["tool"], "read_reminder")
        self.assertFalse(result["next_action"]["retry_original_once"])
        validate_public_result("organize_reminder", result, "unknown")

    def test_pending_native_receipt_normalizes_non_sync_error_code(self) -> None:
        backend = Backend()
        references = References()
        receipt = mutation_payload(
            "add_tag",
            status="committed_verification_pending",
            recovery={
                "semantics": "read_before_retry",
                "automatic_retry_safe": False,
            },
        )
        receipt["error"] = {}
        backend.native_mutation_payloads["add_tag"] = MutationOutcome(
            receipt=receipt,
            mutation_state="unknown",
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
        self.assertEqual(result["error"]["code"], "sync_pending")
        self.assertEqual(result["error"]["reason_code"], "backend_error")
        self.assertFalse(result["error"]["retryable"])
        validate_public_result("organize_reminder", result, "unknown")

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
        self.assertFalse(result["error"]["retryable"])
        self.assertEqual(result["next_action"]["tool"], "read_reminder")
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

    def test_reference_backend_failure_before_dispatch_is_proven_no_write(self) -> None:
        cases = (
            (
                "organize_reminder",
                {"kind": "add_tag", "tag": "next"},
                f"rev1.{'d' * 32}",
            ),
            (
                "change_reminder_attachment",
                {
                    "kind": "copy_image",
                    "source_reference": f"rev1.{'s' * 32}",
                    "attachment_id": "ATTACHMENT-SOURCE",
                    "idempotency_key": "copy-image-pre-dispatch-failure",
                },
                f"rev1.{'s' * 32}",
            ),
        )
        for tool_name, action, failing_reference in cases:
            with self.subTest(tool_name=tool_name):
                backend = Backend()
                references = References()
                destination_reference = f"rev1.{'d' * 32}"
                references.guards[f"rev1.{'s' * 32}"] = Guard(
                    reminder_id="REMINDER-SOURCE",
                    store_identity="STORE-SOURCE",
                    public_concurrency_value="2026-08-25T00:00:00Z",
                )
                references.revalidation_errors.add(failing_reference)
                facade = self.make_facade(backend, references=references)

                result = facade.call(
                    tool_name,
                    {"reference": destination_reference, "action": action},
                )

                self.assertEqual(result["status"], "failed_no_mutation")
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "sync_pending")
                self.assertEqual(
                    result["error"]["reason_code"],
                    "reference_revalidation_failed",
                )
                self.assertEqual(result["verification"]["write_performed"], False)
                self.assertTrue(result["recovery"]["automatic_retry_safe"])
                self.assertEqual(backend.native_mutations, [])
                self.assertEqual(backend.native_copy_mutations, [])
                self.assertEqual(references.invalidated, [])
                validate_public_result(tool_name, result, "not_mutated")

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

    def test_malformed_optional_post_dispatch_receipt_fields_remain_unknown(
        self,
    ) -> None:
        cases = (
            ("warnings", None, "invalid_native_receipt_warnings"),
            ("warnings", 1, "invalid_native_receipt_warnings"),
            ("error", None, "invalid_native_receipt_error"),
        )
        for field, value, reason_code in cases:
            with self.subTest(field=field, value=value):
                backend = Backend()
                references = References()
                receipt = mutation_payload(
                    "add_tag",
                    status="committed_verification_pending",
                    recovery={
                        "semantics": "read_before_retry",
                        "automatic_retry_safe": False,
                    },
                )
                receipt[field] = value
                backend.native_mutation_payloads["add_tag"] = MutationOutcome(
                    receipt=receipt,
                    mutation_state="unknown",
                )
                facade = self.make_facade(backend, references=references)
                reference = f"rev1.{'x' * 32}"

                result = facade.call(
                    "organize_reminder",
                    {
                        "reference": reference,
                        "action": {"kind": "add_tag", "tag": "next"},
                    },
                )

                self.assertEqual(references.invalidated, [reference])
                self.assertEqual(
                    result["status"], "committed_verification_pending"
                )
                self.assertTrue(result["ok"])
                self.assertEqual(result["error"]["code"], "sync_pending")
                self.assertEqual(result["error"]["reason_code"], reason_code)
                self.assertIsNone(result["verification"]["write_performed"])
                self.assertFalse(result["recovery"]["automatic_retry_safe"])
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
                "matched": None,
            },
        )
        self.assertEqual(result["recovery"]["semantics"], "read_reminder_before_retry")
        self.assertFalse(result["recovery"]["automatic_retry_safe"])
        validate_public_result("organize_reminder", result, "committed")

    def test_unchanged_native_result_with_failed_final_read_is_valid_pending(self) -> None:
        backend = Backend()
        references = References()
        references.fail_fresh_read = True
        receipt = mutation_payload(
            "remove_tag",
            status="unchanged",
            after={"reminder_id": "REMINDER-1", "section": None, "tags": []},
        )
        receipt["verification"]["write_performed"] = False
        receipt["recovery"] = {
            "semantics": "not_applicable",
            "automatic_retry_safe": True,
        }
        backend.native_mutation_payloads["remove_tag"] = MutationOutcome(
            receipt=receipt,
            mutation_state="not_mutated",
        )
        facade = self.make_facade(backend, references=references)
        reference = f"rev1.{'x' * 32}"

        result = facade.call(
            "organize_reminder",
            {
                "reference": reference,
                "action": {"kind": "remove_tag", "tag": "next"},
            },
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertIsNone(result["verification"]["write_performed"])
        self.assertFalse(result["verification"]["final_read"])
        self.assertFalse(result["recovery"]["automatic_retry_safe"])
        self.assertEqual(result["error"]["reason_code"], "native_final_read_failed")
        self.assertEqual(references.invalidated, [reference])
        self.assertNotIn("rev1.", repr(result))
        validate_public_result("organize_reminder", result, "unknown")

    def test_native_terminal_receipt_must_match_the_requested_action(self) -> None:
        backend = Backend()
        backend.native_mutation_payloads["attach_url"] = MutationOutcome(
            receipt=mutation_payload(
                "attach_url",
                after={"reminder_id": "REMINDER-1", "attachments": []},
            ),
            mutation_state="committed",
        )
        facade = self.make_facade(backend)

        result = facade.call(
            "change_reminder_attachment",
            {
                "reference": f"rev1.{'x' * 32}",
                "action": {"kind": "attach_url", "url": "https://example.com/item"},
            },
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertIsNone(result["verification"]["matched"])
        self.assertFalse(result["verification"]["final_read"])
        self.assertEqual(result["error"]["reason_code"], "native_final_state_mismatch")
        self.assertNotIn("rev1.", repr(result))
        self.assertFalse(result["recovery"]["automatic_retry_safe"])

    def test_facade_final_state_matching_canonicalizes_native_identifiers_and_tags(self) -> None:
        old_upper = "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"
        old_lower = old_upper.lower()
        new_upper = "11111111-2222-4333-8444-555555555555"
        existing = {
            "attachments": [{"id": old_upper, "type": "image"}],
            "truncated": False,
        }
        replaced_but_not_deleted = {
            "attachments": [
                {"id": old_upper, "type": "image"},
                {"id": new_upper, "type": "image"},
            ],
            "truncated": False,
        }

        self.assertFalse(
            NativeFacade._native_final_state_matches(
                "delete_attachment", {"attachment_id": old_lower}, {}, existing
            )
        )
        self.assertFalse(
            NativeFacade._native_final_state_matches(
                "replace_image",
                {"attachment_id": old_lower},
                {"target": {"new_attachment_id": new_upper.lower()}},
                replaced_but_not_deleted,
            )
        )
        self.assertTrue(
            NativeFacade._native_final_state_matches(
                "move_to_section",
                {"section_id": old_lower},
                {},
                {"section": {"id": old_upper}},
            )
        )
        self.assertTrue(
            NativeFacade._native_final_state_matches(
                "add_tag", {"tag": "#Next"}, {}, {"tags": [{"name": "next"}]}
            )
        )
        self.assertFalse(
            NativeFacade._native_final_state_matches(
                "remove_tag", {"tag": "next"}, {}, {"reminder_id": "REMINDER-1"}
            )
        )
        self.assertFalse(
            NativeFacade._native_final_state_matches(
                "delete_attachment",
                {"attachment_id": old_lower},
                {},
                {"attachments": [], "truncated": True},
            )
        )
        self.assertTrue(
            NativeFacade._native_final_state_matches(
                "delete_attachment",
                {"attachment_id": old_lower},
                {},
                {"attachments": [], "truncated": False},
            )
        )
        oversized_attachments = NativeFacade._native_state(
            "change_reminder_attachment",
            {
                "attachments": [
                    {
                        "id": f"ATTACHMENT-{index}",
                        "type": "url",
                        "url": f"https://example.com/{index}",
                    }
                    for index in range(201)
                ],
                "truncated": False,
            },
            "REMINDER-1",
            None,
        )
        self.assertIsNotNone(oversized_attachments)
        self.assertTrue(oversized_attachments["truncated"])
        self.assertFalse(
            NativeFacade._native_final_state_matches(
                "delete_attachment",
                {"attachment_id": "ATTACHMENT-200"},
                {},
                oversized_attachments,
            )
        )
        oversized_tags = NativeFacade._native_state(
            "organize_reminder",
            {"tags": [{"name": f"tag-{index}"} for index in range(201)]},
            "REMINDER-1",
            None,
        )
        self.assertIsNotNone(oversized_tags)
        self.assertIsNone(oversized_tags["tags"])
        self.assertFalse(
            NativeFacade._native_final_state_matches(
                "remove_tag", {"tag": "tag-200"}, {}, oversized_tags
            )
        )

    def test_post_dispatch_projection_failure_is_never_false_no_mutation(self) -> None:
        backend = Backend()
        references = References()
        backend.native_mutation_payloads["add_tag"] = MutationOutcome(
            receipt=mutation_payload(
                "add_tag",
                after={
                    "reminder_id": "REMINDER-1",
                    "section": None,
                    "tags": [
                        {
                            "id": "TAG-1",
                            "name": "next",
                            "active_count": "not-an-int",
                        }
                    ],
                },
            ),
            mutation_state="committed",
        )
        facade = self.make_facade(backend, references=references)
        reference = f"rev1.{'x' * 32}"

        result = facade.call(
            "organize_reminder",
            {
                "reference": reference,
                "action": {"kind": "add_tag", "tag": "next"},
            },
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertIsNone(result["verification"]["write_performed"])
        self.assertFalse(result["recovery"]["automatic_retry_safe"])
        self.assertEqual(result["error"]["reason_code"], "native_facade_failure")
        self.assertIn(reference, references.invalidated)

    def test_attachment_change_uses_closed_action_and_guarded_port(self) -> None:
        backend = Backend()
        references = References()
        receipt = mutation_payload(
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
        )
        receipt["target"] = {
            "reminder_id": "REMINDER-1",
            "attachment_id": "ATTACHMENT-1",
        }
        backend.native_mutation_payloads["attach_url"] = MutationOutcome(
            receipt=receipt,
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

    def test_copy_image_revalidates_two_exact_references_and_returns_destination_only(
        self,
    ) -> None:
        backend = Backend()
        references = References()
        destination_reference = f"rev1.{'d' * 32}"
        source_reference = f"rev1.{'s' * 32}"
        source_guard = Guard(
            reminder_id="REMINDER-SOURCE",
            store_identity="STORE-SOURCE",
            public_concurrency_value="2026-08-25T00:00:00Z",
        )
        references.guards[source_reference] = source_guard
        receipt = mutation_payload(
            "copy_image",
            after={
                "reminder": {"id": "REMINDER-1"},
                "attachments": [
                    {
                        "id": "ATTACHMENT-NEW",
                        "type": "image",
                        "filename": "copied.png",
                        "sync": {"mobile_visible_likely": True},
                    }
                ],
            },
        )
        receipt["target"] = {
            "source_reminder_id": "REMINDER-SOURCE",
            "reminder_id": "REMINDER-1",
            "source_attachment_id": "ATTACHMENT-SOURCE",
            "attachment_id": "ATTACHMENT-NEW",
        }
        receipt["verification"].update(
            {
                "source_unchanged": True,
                "source_bytes_matched": True,
                "destination_attachment_active": True,
                "destination_content_matched": True,
                "destination_mobile_visible_likely": True,
            }
        )
        backend.native_mutation_payloads["copy_image"] = MutationOutcome(
            receipt=receipt,
            mutation_state="committed",
        )
        facade = self.make_facade(backend, references=references)

        result = facade.call(
            "change_reminder_attachment",
            {
                "reference": destination_reference,
                "action": {
                    "kind": "copy_image",
                    "source_reference": source_reference,
                    "attachment_id": "ATTACHMENT-SOURCE",
                    "idempotency_key": "copy-image-contract",
                },
            },
        )

        self.assertEqual(references.resolved, [destination_reference, source_reference])
        self.assertEqual(
            backend.native_copy_mutations,
            [
                (
                    references.guard,
                    source_guard,
                    "copy_image",
                    {
                        "attachment_id": "ATTACHMENT-SOURCE",
                        "idempotency_key": "copy-image-contract",
                        "source_reminder_id": "REMINDER-SOURCE",
                    },
                )
            ],
        )
        self.assertEqual(
            references.invalidated,
            [destination_reference, source_reference],
        )
        self.assertEqual(references.fresh_reads, ["REMINDER-1"])
        self.assertEqual(
            result["target"],
            {
                "source_reminder_id": "REMINDER-SOURCE",
                "reminder_id": "REMINDER-1",
                "source_attachment_id": "ATTACHMENT-SOURCE",
                "attachment_id": "ATTACHMENT-NEW",
            },
        )
        self.assertEqual(result["after"]["reference"], f"rev1.{'n' * 32}")
        self.assertNotIn("image_path", str(result))
        self.assertNotIn("source_reference", str(result))
        validate_public_result("change_reminder_attachment", result, "committed")

    def test_image_success_requires_mobile_visibility_and_backend_content_proof(self) -> None:
        attachment = {
            "id": "ATTACHMENT-NEW",
            "type": "image",
            "sync": {"mobile_visible_likely": False},
        }
        receipt = {
            "target": {"attachment_id": "ATTACHMENT-NEW"},
            "verification": {
                "final_attachment_content_matched": True,
                "mobile_visible_likely": True,
            },
        }
        self.assertFalse(
            NativeFacade._native_final_state_matches(
                "attach_image",
                {},
                receipt,
                {"attachments": [attachment], "truncated": False},
            )
        )
        attachment["sync"]["mobile_visible_likely"] = True
        receipt["verification"]["final_attachment_content_matched"] = False
        self.assertFalse(
            NativeFacade._native_final_state_matches(
                "attach_image",
                {},
                receipt,
                {"attachments": [attachment], "truncated": False},
            )
        )

    def test_copy_image_stale_source_fails_before_native_dispatch(self) -> None:
        backend = Backend()
        references = References()
        destination_reference = f"rev1.{'d' * 32}"
        source_reference = f"rev1.{'s' * 32}"
        references.rejections.add(source_reference)
        facade = self.make_facade(backend, references=references)

        result = facade.call(
            "change_reminder_attachment",
            {
                "reference": destination_reference,
                "action": {
                    "kind": "copy_image",
                    "source_reference": source_reference,
                    "attachment_id": "ATTACHMENT-SOURCE",
                    "idempotency_key": "copy-image-stale-source",
                },
            },
        )

        self.assertEqual(result["status"], "failed_no_mutation")
        self.assertEqual(result["error"]["code"], "concurrent_modification")
        self.assertEqual(backend.native_copy_mutations, [])
        self.assertEqual(references.fresh_reads, [])
        validate_public_result("change_reminder_attachment", result, "not_mutated")

    def test_copy_image_pending_never_issues_a_destination_reference(self) -> None:
        backend = Backend()
        references = References()
        destination_reference = f"rev1.{'d' * 32}"
        source_reference = f"rev1.{'s' * 32}"
        references.guards[source_reference] = Guard(
            reminder_id="REMINDER-SOURCE",
            store_identity="STORE-SOURCE",
            public_concurrency_value="2026-08-25T00:00:00Z",
        )
        receipt = mutation_payload(
            "copy_image",
            status="committed_verification_pending",
        )
        receipt["warnings"] = [
            {"code": "verification_pending", "message": "Read both reminders."}
        ]
        receipt["error"] = {
            "code": "sync_pending",
            "reason_code": "copy_image_final_read_failed",
            "message": "Read both reminders.",
            "retryable": False,
        }
        receipt["verification"] = {
            "state": "pending",
            "write_performed": None,
            "final_read": False,
        }
        receipt["recovery"] = {
            "semantics": "read_both_reminders_before_retry",
            "automatic_retry_safe": False,
        }
        backend.native_mutation_payloads["copy_image"] = MutationOutcome(
            receipt=receipt,
            mutation_state="unknown",
        )
        facade = self.make_facade(backend, references=references)

        result = facade.call(
            "change_reminder_attachment",
            {
                "reference": destination_reference,
                "action": {
                    "kind": "copy_image",
                    "source_reference": source_reference,
                    "attachment_id": "ATTACHMENT-SOURCE",
                    "idempotency_key": "copy-image-pending",
                },
            },
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertIsNone(result["after"])
        self.assertNotIn("rev1.", str(result))
        self.assertFalse(result["recovery"]["automatic_retry_safe"])
        self.assertEqual(references.fresh_reads, [])
        self.assertEqual(
            references.invalidated,
            [destination_reference, source_reference],
        )
        validate_public_result("change_reminder_attachment", result, "unknown")

    def test_native_concurrent_receipt_points_to_exact_reminder_read(self) -> None:
        backend = Backend()
        references = References()
        receipt = mutation_payload("add_tag", status="failed_no_mutation")
        receipt["error"] = {
            "code": "concurrent_modification",
            "reason_code": "reminder_changed",
            "message": "The Reminder changed before the native save.",
            "retryable": True,
        }
        backend.native_mutation_payloads["add_tag"] = MutationOutcome(
            receipt=receipt,
            mutation_state="not_mutated",
        )
        facade = self.make_facade(backend, references=references)

        result = facade.call(
            "organize_reminder",
            {
                "reference": f"rev1.{'x' * 32}",
                "action": {"kind": "add_tag", "tag": "next"},
            },
        )

        self.assertEqual(result["status"], "failed_no_mutation")
        self.assertEqual(result["error"]["code"], "concurrent_modification")
        self.assertEqual(result["next_action"]["tool"], "read_reminder")
        self.assertFalse(result["next_action"]["retry_original_once"])
        validate_public_result("organize_reminder", result, "not_mutated")

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
