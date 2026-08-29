from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
sys.path.insert(0, str(PLUGIN_ROOT))

from mcp.v2_contract import (
    MAX_ARRAY_ITEMS,
    MAX_RESULT_BYTES,
    MAX_STRING_LENGTH,
    MAX_WARNING_ITEMS,
    PublicResultContractError,
    validate_public_result,
)


REFERENCE = "rev1." + "A" * 32
DELETED_REFERENCE = "del1." + "D" * 32
OPERATION_ID = "12345678-1234-4234-9234-1234567890ab"

READ_TOOLS = (
    "request_reminders_access",
    "list_reminder_lists",
    "fetch_reminders",
    "read_reminder",
    "inspect_recently_deleted",
    "inspect_reminder_native",
    "diagnose_reminders",
)

MUTATION_CASES = {
    "create_reminder": ("create_reminder", "eventkit_public_sdk", True),
    "change_reminder": ("change_reminder.patch", "eventkit_public_sdk", True),
    "delete_reminder": ("delete_reminder", "eventkit_public_sdk", False),
    "recover_deleted_reminder": (
        "recover_deleted_reminder",
        "native_extension",
        False,
    ),
    "ensure_reminder_list": ("ensure_reminder_list", "eventkit_public_sdk", False),
    "create_reminder_section": (
        "create_reminder_section",
        "native_extension",
        False,
    ),
    "organize_reminder": (
        "organize_reminder.move_to_section",
        "native_extension",
        True,
    ),
    "change_reminder_attachment": (
        "change_reminder_attachment.attach_image",
        "native_extension",
        True,
    ),
}


def read_success(tool_name: str) -> dict[str, object]:
    data: dict[str, object] = {"available": True}
    if tool_name == "request_reminders_access":
        data = {
            "authorization_before": "full_access",
            "authorization": "full_access",
            "request_attempted": True,
            "prompt_expected": False,
            "prompt_observed": None,
            "prompted_explicitly": True,
        }
    elif tool_name == "list_reminder_lists":
        data = {"items": [], "returned": 0, "truncated": False}
    elif tool_name == "fetch_reminders":
        data = {"items": [], "returned": 0, "has_more": False}
    elif tool_name == "read_reminder":
        data = {
            "reminder": {
                "id": "REMINDER-1",
                "title": "Ship beta",
                "reference": REFERENCE,
            }
        }
    elif tool_name == "inspect_recently_deleted":
        data = {
            "kind": "item",
            "deleted_reminder": {
                "id": "REMINDER-1",
                "title": "Recover me",
                "reference": DELETED_REFERENCE,
            },
        }
    elif tool_name == "inspect_reminder_native":
        data = {
            "kind": "reminder",
            "reminder_id": "REMINDER-1",
            "reference": REFERENCE,
            "tags": [],
            "attachments": [],
        }
    elif tool_name == "diagnose_reminders":
        data = {
            "diagnostic_status": "ready",
            "checks": {},
            "privacy": {"content_free": True},
        }
    return {
        "schema_version": 2,
        "ok": True,
        "status": "verified",
        "operation": tool_name,
        "data": data,
    }


def error_payload(code: str = "invalid_input") -> dict[str, object]:
    return {
        "code": code,
        "reason_code": "fixture_failure",
        "message": "The fixture operation failed safely.",
        "retryable": False,
    }


def read_failure(tool_name: str, code: str = "invalid_input") -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 2,
        "ok": False,
        "status": "failed_no_mutation",
        "operation": tool_name,
        "error": error_payload(code),
    }
    if code == "permission_denied":
        if tool_name == "request_reminders_access":
            payload["data"] = {
                "authorization_before": "not_determined",
                "authorization": "denied",
                "request_attempted": True,
                "prompt_expected": True,
                "prompt_observed": None,
                "prompted_explicitly": True,
            }
        else:
            payload["next_action"] = {
                "kind": "request_access",
                "tool": "request_reminders_access",
                "retry_original_once": True,
                "message": "Request access and retry once.",
            }
    return payload


def verified_mutation(tool_name: str) -> dict[str, object]:
    operation, backend, returns_reference = MUTATION_CASES[tool_name]
    after: dict[str, object] = {"id": "PUBLIC-1"}
    if returns_reference:
        after["reference"] = REFERENCE
    verification: dict[str, object] = {
        "state": "read_back",
        "write_performed": True,
        "final_read": True,
        "matched": True,
    }
    if tool_name == "recover_deleted_reminder":
        verification.update(
            {
                "pre_save_guard_matched": True,
                "destination_list_matched": True,
                "attachments_active": True,
                "attachments_preserved": True,
                "attachment_bytes_verified": True,
                "attachment_counts_match": True,
                "before_attachment_count": 1,
                "native_attachment_count": 1,
                "after_attachment_count": 1,
            }
        )
    return {
        "schema_version": 2,
        "ok": True,
        "status": "verified",
        "operation": operation,
        "operation_id": OPERATION_ID,
        "backend": backend,
        "target": {},
        "before": None,
        "after": after,
        "verification": verification,
        "recovery": {
            "semantics": "read_before_retry",
            "automatic_retry_safe": False,
        },
    }


def mutation_failure(
    tool_name: str,
    *,
    code: str = "invalid_input",
) -> dict[str, object]:
    operation, backend, _ = MUTATION_CASES[tool_name]
    payload: dict[str, object] = {
        "schema_version": 2,
        "ok": False,
        "status": "failed_no_mutation",
        "operation": operation,
        "operation_id": OPERATION_ID,
        "backend": backend,
        "target": {},
        "before": None,
        "after": None,
        "verification": {
            "state": "not_needed",
            "write_performed": False,
            "final_read": False,
        },
        "recovery": {
            "semantics": "not_applicable",
            "automatic_retry_safe": True,
        },
        "error": error_payload(code),
    }
    if code == "concurrent_modification":
        payload["next_action"] = {
            "kind": "fresh_read",
            "tool": {
                "create_reminder": "fetch_reminders",
                "ensure_reminder_list": "list_reminder_lists",
                "create_reminder_section": "inspect_reminder_native",
                "recover_deleted_reminder": "inspect_recently_deleted",
            }.get(tool_name, "read_reminder"),
            "retry_original_once": False,
            "message": "Read the exact Reminder again.",
        }
    return payload


def pending_mutation(tool_name: str = "change_reminder") -> dict[str, object]:
    payload = verified_mutation(tool_name)
    payload.update(
        {
            "ok": True,
            "status": "committed_verification_pending",
            "after": None,
            "verification": {
                "state": "pending",
                "write_performed": None,
                "final_read": False,
                "matched": None,
            },
            "warnings": [
                {
                    "code": "verification_pending",
                    "message": "Read the exact Reminder before another write.",
                }
            ],
            "error": {
                "code": "sync_pending",
                "reason_code": "final_read_failed",
                "message": "The write may have committed, but final read failed.",
                "retryable": False,
            },
        }
    )
    payload["next_action"] = {
        "kind": "fresh_read",
        "tool": {
            "create_reminder": "fetch_reminders",
            "ensure_reminder_list": "list_reminder_lists",
            "create_reminder_section": "inspect_reminder_native",
        }.get(tool_name, "read_reminder"),
        "retry_original_once": False,
        "message": "Inspect the exact target before another mutation.",
    }
    return payload


class PublicV2ResultContractTests(unittest.TestCase):
    def test_public_collection_items_must_be_objects(self) -> None:
        recently_deleted = read_success("inspect_recently_deleted")
        recently_deleted["data"] = {
            "kind": "list",
            "items": [None],
            "returned": 1,
            "limit": 20,
            "total_matched": 1,
            "truncated": False,
            "has_more": False,
            "next_cursor": None,
            "pagination_exhausted": False,
            "retention_days": 30,
        }
        fixtures = (
            ("list_reminder_lists", read_success("list_reminder_lists")),
            ("fetch_reminders", read_success("fetch_reminders")),
            ("inspect_recently_deleted", recently_deleted),
        )
        for tool_name, fixture in fixtures:
            fixture["data"]["items"] = [None]  # type: ignore[index]
            fixture["data"]["returned"] = 1  # type: ignore[index]
            with self.subTest(tool=tool_name):
                with self.assertRaises(PublicResultContractError) as raised:
                    validate_public_result(tool_name, fixture)
                self.assertEqual(raised.exception.code, "invalid_read_envelope")
                self.assertEqual(raised.exception.path, "$.data.items[0]")

    def test_read_success_is_returned_as_an_independent_copy(self) -> None:
        payload = {
            "schema_version": 2,
            "ok": True,
            "status": "verified",
            "operation": "list_reminder_lists",
            "data": {"items": [{"id": "LIST-1", "title": "Inbox"}]},
        }

        result = validate_public_result("list_reminder_lists", payload)
        result["data"]["items"][0]["title"] = "Changed"

        self.assertEqual(payload["data"]["items"][0]["title"], "Inbox")

    def test_all_fifteen_tool_families_accept_success_and_safe_failure(self) -> None:
        for tool_name in READ_TOOLS:
            with self.subTest(tool=tool_name, outcome="success"):
                self.assertTrue(validate_public_result(tool_name, read_success(tool_name))["ok"])
            failure_code = {
                "request_reminders_access": "permission_denied",
                "read_reminder": "not_found",
                "inspect_recently_deleted": "not_found",
                "diagnose_reminders": "schema_mismatch",
            }.get(tool_name, "invalid_input")
            with self.subTest(tool=tool_name, outcome="failure"):
                result = validate_public_result(
                    tool_name,
                    read_failure(tool_name, failure_code),
                    mutation_state="not_mutated",
                )
                self.assertFalse(result["ok"])

        for tool_name in MUTATION_CASES:
            with self.subTest(tool=tool_name, outcome="success"):
                self.assertTrue(
                    validate_public_result(
                        tool_name,
                        verified_mutation(tool_name),
                        mutation_state="committed",
                    )["ok"]
                )
            code = "concurrent_modification" if tool_name == "change_reminder" else "invalid_input"
            with self.subTest(tool=tool_name, outcome="failure"):
                self.assertFalse(
                    validate_public_result(
                        tool_name,
                        mutation_failure(tool_name, code=code),
                        mutation_state="not_mutated",
                    )["ok"]
                )

    def test_verified_recovery_requires_complete_native_integrity_proof(self) -> None:
        for field in (
            "pre_save_guard_matched",
            "destination_list_matched",
            "attachments_active",
            "attachments_preserved",
            "attachment_bytes_verified",
            "attachment_counts_match",
        ):
            fixture = verified_mutation("recover_deleted_reminder")
            fixture["verification"][field] = False  # type: ignore[index]
            with self.subTest(field=field):
                with self.assertRaises(PublicResultContractError) as raised:
                    validate_public_result(
                        "recover_deleted_reminder", fixture, "committed"
                    )
                self.assertEqual(raised.exception.code, "unsafe_final_read")
                self.assertEqual(
                    raised.exception.path,
                    f"$.verification.{field}",
                )

        fixture = verified_mutation("recover_deleted_reminder")
        fixture["verification"]["native_attachment_count"] = 2  # type: ignore[index]
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("recover_deleted_reminder", fixture, "committed")
        self.assertEqual(raised.exception.code, "unsafe_final_read")
        self.assertEqual(raised.exception.path, "$.verification.attachment_counts")

    def test_schema_version_and_operation_are_exact_on_every_result(self) -> None:
        cases = [
            ("list_reminder_lists", read_success("list_reminder_lists")),
            ("list_reminder_lists", read_failure("list_reminder_lists")),
            ("change_reminder", verified_mutation("change_reminder")),
            ("change_reminder", mutation_failure("change_reminder")),
        ]
        for tool_name, fixture in cases:
            with self.subTest(tool=tool_name, field="schema_version"):
                fixture["schema_version"] = 1
                with self.assertRaisesRegex(PublicResultContractError, "schema_version"):
                    validate_public_result(tool_name, fixture)
            fixture = (
                read_success(tool_name)
                if tool_name == "list_reminder_lists" and fixture.get("ok") is True
                else read_failure(tool_name)
                if tool_name == "list_reminder_lists"
                else verified_mutation(tool_name)
                if fixture.get("ok") is True
                else mutation_failure(tool_name)
            )
            fixture["operation"] = "tool_execution_error"
            with self.subTest(tool=tool_name, field="operation"):
                with self.assertRaises(PublicResultContractError) as raised:
                    validate_public_result(tool_name, fixture)
                self.assertEqual(raised.exception.code, "operation_mismatch")

        for invalid_operation in (
            "change_reminder.complete",
            "organize_reminder.delete",
            "change_reminder_attachment.replace",
        ):
            tool_name = invalid_operation.split(".", 1)[0]
            fixture = verified_mutation(tool_name)
            fixture["operation"] = invalid_operation
            with self.subTest(operation=invalid_operation):
                with self.assertRaises(PublicResultContractError):
                    validate_public_result(tool_name, fixture, "committed")

    def test_every_closed_action_operation_is_accepted_with_its_public_backend(self) -> None:
        operations = {
            "change_reminder": (
                "change_reminder.patch",
                "change_reminder.set_completion",
                "change_reminder.move_to_list",
            ),
            "organize_reminder": (
                "organize_reminder.move_to_section",
                "organize_reminder.add_tag",
                "organize_reminder.remove_tag",
            ),
            "change_reminder_attachment": (
                "change_reminder_attachment.attach_image",
                "change_reminder_attachment.attach_url",
                "change_reminder_attachment.copy_image",
                "change_reminder_attachment.replace_image",
                "change_reminder_attachment.replace_url",
                "change_reminder_attachment.delete",
            ),
        }
        for tool_name, family in operations.items():
            for operation in family:
                fixture = verified_mutation(tool_name)
                fixture["operation"] = operation
                if operation != "change_reminder.patch":
                    fixture["backend"] = (
                        "eventkit_public_sdk"
                        if tool_name == "change_reminder"
                        else "native_extension"
                    )
                with self.subTest(tool=tool_name, operation=operation):
                    self.assertEqual(
                        validate_public_result(tool_name, fixture, "committed")[
                            "operation"
                        ],
                        operation,
                    )

    def test_status_ok_and_independent_mutation_state_must_correlate(self) -> None:
        bad_ok = verified_mutation("change_reminder")
        bad_ok["ok"] = False
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("change_reminder", bad_ok, "committed")
        self.assertEqual(raised.exception.code, "status_ok_mismatch")

        cases = (
            (verified_mutation("change_reminder"), "not_mutated"),
            (mutation_failure("change_reminder"), "committed"),
            (pending_mutation(), "not_mutated"),
        )
        for fixture, state in cases:
            with self.subTest(status=fixture["status"], state=state):
                with self.assertRaises(PublicResultContractError) as raised:
                    validate_public_result("change_reminder", fixture, state)
                self.assertEqual(raised.exception.code, "mutation_state_mismatch")

        committed_without_write = pending_mutation()
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result(
                "change_reminder",
                committed_without_write,
                "committed",
            )
        self.assertEqual(raised.exception.code, "mutation_state_mismatch")

        malformed = verified_mutation("change_reminder")
        malformed["operation"] = "wrong"
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("change_reminder", malformed, "unknown")
        self.assertTrue(raised.exception.may_have_mutated)
        self.assertEqual(raised.exception.mutation_state, "unknown")

    def test_verified_and_unchanged_require_canonical_final_read_and_fresh_reference(self) -> None:
        for field, value in (("final_read", False), ("matched", False)):
            fixture = verified_mutation("change_reminder")
            fixture["verification"][field] = value  # type: ignore[index]
            with self.subTest(field=field):
                with self.assertRaises(PublicResultContractError) as raised:
                    validate_public_result("change_reminder", fixture, "committed")
                self.assertEqual(raised.exception.code, "unsafe_final_read")

        missing_reference = verified_mutation("organize_reminder")
        missing_reference["after"].pop("reference")  # type: ignore[union-attr]
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("organize_reminder", missing_reference, "committed")
        self.assertEqual(raised.exception.code, "missing_fresh_reference")

        duplicate_reference = verified_mutation("change_reminder")
        duplicate_reference["before"] = {"reference": "rev1." + "B" * 32}
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("change_reminder", duplicate_reference, "committed")
        self.assertEqual(raised.exception.code, "unsafe_reference")

        unchanged = verified_mutation("change_reminder")
        unchanged.update({"status": "unchanged", "before": None})
        unchanged["verification"]["write_performed"] = False  # type: ignore[index]
        self.assertEqual(
            validate_public_result("change_reminder", unchanged, "not_mutated")["status"],
            "unchanged",
        )

    def test_pending_requires_explicit_warning_and_error_and_never_exposes_reference(self) -> None:
        self.assertEqual(
            validate_public_result("change_reminder", pending_mutation(), "unknown")[
                "status"
            ],
            "committed_verification_pending",
        )
        for missing in ("warnings", "error"):
            fixture = pending_mutation()
            fixture.pop(missing)
            with self.subTest(missing=missing):
                with self.assertRaises(PublicResultContractError) as raised:
                    validate_public_result("change_reminder", fixture, "unknown")
                self.assertEqual(raised.exception.code, "incomplete_receipt")

        missing_recovery = pending_mutation()
        missing_recovery.pop("next_action")
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("change_reminder", missing_recovery, "unknown")
        self.assertEqual(raised.exception.code, "incomplete_failure")

        unsafe_retry = pending_mutation()
        unsafe_retry["error"]["retryable"] = True  # type: ignore[index]
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("change_reminder", unsafe_retry, "unknown")
        self.assertEqual(raised.exception.code, "unsafe_retry")

        unsafe_original_retry = pending_mutation()
        unsafe_original_retry["next_action"]["retry_original_once"] = True  # type: ignore[index]
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result(
                "change_reminder", unsafe_original_retry, "unknown"
            )
        self.assertEqual(raised.exception.code, "unsafe_retry")

        exposed = pending_mutation()
        exposed["after"] = {"reference": REFERENCE}
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("change_reminder", exposed, "unknown")
        self.assertEqual(raised.exception.code, "unsafe_reference")

        create_pending = pending_mutation("create_reminder")
        self.assertEqual(
            validate_public_result("create_reminder", create_pending, "unknown")[
                "next_action"
            ]["tool"],
            "fetch_reminders",
        )

        for tool_name, expected_tool in (
            ("ensure_reminder_list", "list_reminder_lists"),
            ("create_reminder_section", "inspect_reminder_native"),
        ):
            fixture = pending_mutation(tool_name)
            with self.subTest(tool=tool_name):
                self.assertEqual(
                    validate_public_result(tool_name, fixture, "unknown")[
                        "next_action"
                    ]["tool"],
                    expected_tool,
                )

        wrong_section_recovery = pending_mutation("create_reminder_section")
        wrong_section_recovery["next_action"]["tool"] = "read_reminder"  # type: ignore[index]
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result(
                "create_reminder_section", wrong_section_recovery, "unknown"
            )
        self.assertEqual(raised.exception.code, "invalid_next_action")

    def test_partial_manual_and_failed_receipts_preserve_write_semantics(self) -> None:
        for status, ok in (
            ("partial_success", True),
            ("failed_manual_repair_required", False),
        ):
            fixture = pending_mutation()
            fixture["status"] = status
            fixture["ok"] = ok
            fixture["verification"]["state"] = (  # type: ignore[index]
                "partial" if status == "partial_success" else "manual_repair_required"
            )
            fixture["after"] = {"reference": REFERENCE}
            with self.subTest(status=status):
                with self.assertRaises(PublicResultContractError) as raised:
                    validate_public_result("change_reminder", fixture, "unknown")
                self.assertEqual(raised.exception.code, "unsafe_reference")

        wrote = mutation_failure("delete_reminder")
        wrote["verification"]["write_performed"] = True  # type: ignore[index]
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("delete_reminder", wrote, "not_mutated")
        self.assertEqual(raised.exception.code, "false_no_mutation_claim")

    def test_backend_is_public_and_correlated_with_the_exact_operation(self) -> None:
        for tool_name, (_, backend, _) in MUTATION_CASES.items():
            fixture = verified_mutation(tool_name)
            self.assertEqual(
                validate_public_result(tool_name, fixture, "committed")["backend"],
                backend,
            )

        for tool_name in ("create_reminder", "change_reminder"):
            fixture = verified_mutation(tool_name)
            fixture["backend"] = "eventkit_plus_native_url"
            validate_public_result(tool_name, fixture, "committed")

        for tool_name in (
            "delete_reminder",
            "ensure_reminder_list",
            "create_reminder_section",
            "organize_reminder",
            "change_reminder_attachment",
        ):
            fixture = verified_mutation(tool_name)
            fixture["backend"] = "eventkit_plus_native_url"
            with self.subTest(tool=tool_name):
                with self.assertRaises(PublicResultContractError) as raised:
                    validate_public_result(tool_name, fixture, "committed")
                self.assertEqual(raised.exception.code, "backend_mismatch")

        for operation in ("change_reminder.set_completion", "change_reminder.move_to_list"):
            fixture = verified_mutation("change_reminder")
            fixture["operation"] = operation
            fixture["backend"] = "eventkit_plus_native_url"
            with self.subTest(operation=operation):
                with self.assertRaises(PublicResultContractError):
                    validate_public_result("change_reminder", fixture, "committed")

        malformed = verified_mutation("create_reminder")
        malformed["backend"] = {"name": "eventkit_public_sdk"}
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("create_reminder", malformed, "committed")
        self.assertEqual(raised.exception.code, "backend_mismatch")

    def test_permission_stale_not_found_and_malformed_backend_failures_stay_typed(self) -> None:
        permission = validate_public_result(
            "request_reminders_access",
            read_failure("request_reminders_access", "permission_denied"),
        )
        self.assertNotIn("next_action", permission)
        self.assertEqual(permission["data"]["authorization"], "denied")

        stale = validate_public_result(
            "change_reminder",
            mutation_failure("change_reminder", code="concurrent_modification"),
            "not_mutated",
        )
        self.assertEqual(stale["next_action"]["kind"], "fresh_read")

        for tool_name, expected_tool in (
            ("create_reminder", "fetch_reminders"),
            ("ensure_reminder_list", "list_reminder_lists"),
            ("create_reminder_section", "inspect_reminder_native"),
        ):
            with self.subTest(tool=tool_name, error="concurrent_modification"):
                result = validate_public_result(
                    tool_name,
                    mutation_failure(tool_name, code="concurrent_modification"),
                    "not_mutated",
                )
                self.assertEqual(result["next_action"]["tool"], expected_tool)

        missing = validate_public_result(
            "read_reminder",
            read_failure("read_reminder", "not_found"),
        )
        self.assertEqual(missing["error"]["code"], "not_found")

        backend_failure = mutation_failure("ensure_reminder_list", code="schema_mismatch")
        backend_failure["error"]["reason_code"] = "malformed_backend_receipt"  # type: ignore[index]
        validate_public_result("ensure_reminder_list", backend_failure, "not_mutated")

        rate_limited = read_failure("fetch_reminders", "rate_limited")
        rate_limited["error"]["retryable"] = True  # type: ignore[index]
        self.assertEqual(
            validate_public_result("fetch_reminders", rate_limited)["operation"],
            "fetch_reminders",
        )

    def test_access_result_shape_is_closed_and_never_self_retries(self) -> None:
        observed = read_success("request_reminders_access")
        observed["data"]["prompt_observed"] = True  # type: ignore[index]
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("request_reminders_access", observed)
        self.assertEqual(raised.exception.code, "invalid_read_envelope")

        missing_receipt = read_failure(
            "request_reminders_access", "permission_denied"
        )
        del missing_receipt["data"]
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("request_reminders_access", missing_receipt)
        self.assertEqual(raised.exception.code, "incomplete_failure")

        self_retry = read_failure("request_reminders_access", "permission_denied")
        self_retry["next_action"] = {
            "kind": "request_access",
            "tool": "request_reminders_access",
            "retry_original_once": True,
            "message": "Try the same request again.",
        }
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("request_reminders_access", self_retry)
        self.assertEqual(raised.exception.code, "invalid_next_action")

        contradictory_denial = read_failure(
            "request_reminders_access", "permission_denied"
        )
        contradictory_denial["data"]["authorization"] = "full_access"  # type: ignore[index]
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result(
                "request_reminders_access", contradictory_denial
            )
        self.assertEqual(raised.exception.code, "invalid_read_envelope")

        diagnose = read_failure("request_reminders_access", "unexpected_error")
        diagnose["next_action"] = {
            "kind": "diagnose",
            "tool": "diagnose_reminders",
            "retry_original_once": False,
            "message": "Inspect the local helper build before retrying.",
        }
        validate_public_result("request_reminders_access", diagnose)

    def test_read_and_mutation_envelopes_are_closed_and_distinct(self) -> None:
        read = read_success("list_reminder_lists")
        read["backend"] = "eventkit_public_sdk"
        with self.assertRaises(PublicResultContractError):
            validate_public_result("list_reminder_lists", read)

        failed_read = read_failure("fetch_reminders")
        failed_read["data"] = {}
        with self.assertRaises(PublicResultContractError):
            validate_public_result("fetch_reminders", failed_read)

        mutation = verified_mutation("delete_reminder")
        mutation.pop("operation_id")
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("delete_reminder", mutation, "committed")
        self.assertEqual(raised.exception.code, "missing_receipt_field")

    def test_errors_warnings_strings_arrays_and_total_result_size_are_bounded(self) -> None:
        too_many_warnings = verified_mutation("delete_reminder")
        too_many_warnings["warnings"] = [
            {"code": "notice", "message": "bounded"}
            for _ in range(MAX_WARNING_ITEMS + 1)
        ]
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("delete_reminder", too_many_warnings, "committed")
        self.assertEqual(raised.exception.code, "warnings_too_large")

        long_error = read_failure("fetch_reminders")
        long_error["error"]["message"] = "x" * 2001  # type: ignore[index]
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("fetch_reminders", long_error)
        self.assertEqual(raised.exception.code, "invalid_error")

        long_string = read_success("diagnose_reminders")
        long_string["data"] = {"summary": "x" * (MAX_STRING_LENGTH + 1)}
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("diagnose_reminders", long_string)
        self.assertEqual(raised.exception.code, "string_too_long")

        long_array = read_success("fetch_reminders")
        long_array["data"] = {"items": [None] * (MAX_ARRAY_ITEMS + 1)}
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("fetch_reminders", long_array)
        self.assertEqual(raised.exception.code, "array_too_large")

        oversized = read_success("diagnose_reminders")
        oversized["data"] = {"chunks": ["x" * MAX_STRING_LENGTH for _ in range(11)]}
        self.assertGreater(
            len(str(oversized["data"])),
            MAX_RESULT_BYTES,
        )
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("diagnose_reminders", oversized)
        self.assertEqual(raised.exception.code, "result_too_large")

    def test_recursive_private_database_calendar_and_path_keys_are_forbidden(self) -> None:
        for key in (
            "calendar_id",
            "reminder_calendar_count",
            "db_path",
            "database_path",
            "Z_PK",
            "rowid",
            "image_path",
            "container_path",
            "store_identity",
            "private_version",
            "attachment_digest",
            "native_guard_digest",
        ):
            fixture = read_success("diagnose_reminders")
            fixture["data"] = {"nested": {key: "private"}}
            with self.subTest(key=key):
                with self.assertRaises(PublicResultContractError) as raised:
                    validate_public_result("diagnose_reminders", fixture)
                self.assertEqual(raised.exception.code, "forbidden_internal_field")

    def test_private_paths_and_native_details_are_forbidden_in_error_messages(self) -> None:
        for detail in (
            "Open ~/Library/Reminders/Container_v1/Stores/Stores.sqlite",
            "Failed at /Users/example/Library/Reminders/Stores.sqlite",
            "remkit_recover: REMStore saveSynchronouslyWithError: failed",
            "/System/Library/PrivateFrameworks/ReminderKit.framework failed",
            "/Library/Application Support/apple-reminders-codex/state.json failed",
        ):
            fixture = read_failure("inspect_recently_deleted", "unexpected_error")
            fixture["error"]["message"] = detail  # type: ignore[index]
            with self.subTest(detail=detail):
                with self.assertRaises(PublicResultContractError) as raised:
                    validate_public_result("inspect_recently_deleted", fixture)
                self.assertEqual(raised.exception.code, "private_error_detail")
                self.assertEqual(raised.exception.path, "$.error.message")

        fixture = read_failure("inspect_recently_deleted", "unexpected_error")
        fixture["error"]["reason_code"] = (  # type: ignore[index]
            "users_alice_library_reminders_stores_sqlite"
        )
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("inspect_recently_deleted", fixture)
        self.assertEqual(raised.exception.code, "private_error_detail")
        self.assertEqual(raised.exception.path, "$.error.reason_code")

    def test_only_json_values_are_returned_and_cycles_are_rejected(self) -> None:
        unsupported = read_success("diagnose_reminders")
        unsupported["data"] = {"value": (1, 2)}
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("diagnose_reminders", unsupported)
        self.assertEqual(raised.exception.code, "invalid_json_type")

        cyclic = read_success("diagnose_reminders")
        loop: dict[str, object] = {}
        loop["self"] = loop
        cyclic["data"] = loop
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("diagnose_reminders", cyclic)
        self.assertEqual(raised.exception.code, "cyclic_result")

        invalid_unicode = read_success("diagnose_reminders")
        invalid_unicode["data"] = {"summary": "\ud800"}
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("diagnose_reminders", invalid_unicode)
        self.assertEqual(raised.exception.code, "invalid_string_encoding")

    def test_invalid_tool_and_invalid_mutation_state_raise_the_typed_error(self) -> None:
        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result("unknown_tool", read_success("diagnose_reminders"))
        self.assertEqual(raised.exception.code, "unknown_tool")

        with self.assertRaises(PublicResultContractError) as raised:
            validate_public_result(
                "change_reminder",
                verified_mutation("change_reminder"),
                "maybe",
            )
        self.assertEqual(raised.exception.code, "invalid_mutation_state")


if __name__ == "__main__":
    unittest.main()
