from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest
from collections import defaultdict, deque
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
sys.path.insert(0, str(PLUGIN_ROOT))

from mcp.v2_core import EventKitReply, V2CoreFacade, _change_reminder
from mcp.v2_contract import validate_public_result


class DeterministicTokens:
    def __init__(self) -> None:
        self._next = 0

    def __call__(self) -> str:
        value = chr(ord("A") + self._next) * 32
        self._next += 1
        return value


class DeterministicOperationIDs:
    def __init__(self) -> None:
        self._next = 1

    def __call__(self) -> str:
        value = f"00000000-0000-4000-8000-{self._next:012d}"
        self._next += 1
        return value


class FakeEventKit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], bool]] = []
        self._replies: dict[tuple[str, bool], deque[EventKitReply]] = defaultdict(deque)
        self._failures: dict[tuple[str, bool], deque[Exception]] = defaultdict(deque)

    def queue(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        mutation: bool = False,
        is_error: bool = False,
        mutation_state: str | None = None,
    ) -> None:
        if mutation and mutation_state is None:
            status = payload.get("status")
            mutation_state = (
                "not_mutated"
                if status in {"unchanged", "failed_no_mutation"}
                else "committed"
                if status == "verified"
                else "unknown"
            )
        self._replies[(operation, mutation)].append(
            EventKitReply(
                payload=copy.deepcopy(dict(payload)),
                is_error=is_error,
                mutation_state=mutation_state,  # type: ignore[arg-type]
            )
        )

    def fail(self, operation: str, exc: Exception, *, mutation: bool = False) -> None:
        self._failures[(operation, mutation)].append(exc)

    def invoke(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        mutation: bool,
    ) -> EventKitReply:
        self.calls.append((operation, copy.deepcopy(dict(arguments)), mutation))
        failures = self._failures[(operation, mutation)]
        if failures:
            raise failures.popleft()
        queue = self._replies[(operation, mutation)]
        if not queue:
            raise AssertionError(f"unexpected EventKit call: {operation}, mutation={mutation}")
        return queue.popleft()


def native_reminder(
    *,
    reminder_id: str = "REMINDER-1",
    title: str = "Ship beta",
    last_modified: str = "2026-08-25T01:00:00.000Z",
    calendar_id: str = "LIST-1",
    calendar_title: str = "Inbox",
    completed: bool = False,
) -> dict[str, Any]:
    return {
        "id": reminder_id,
        "external_id": None,
        "title": title,
        "notes": None,
        "url": None,
        "location": None,
        "priority": 0,
        "completed": completed,
        "completion_date": None,
        "due": None,
        "start": None,
        "alarms": [],
        "recurrence_rules": [],
        "created": "2026-08-24T01:00:00.000Z",
        "last_modified": last_modified,
        "calendar_id": calendar_id,
        "calendar_title": calendar_title,
        "source_id": "SOURCE-1",
        "source_title": "iCloud",
    }


def read_receipt(reminder: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ok": True,
        "status": "verified",
        "operation": "read_reminder",
        "data": {"reminder": copy.deepcopy(dict(reminder))},
    }


def fetch_receipt(
    items: list[Mapping[str, Any]],
    *,
    total_matched: int,
    offset: int,
    has_more: bool,
    next_offset: int | None,
    snapshot_fingerprint: str | None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "items": [copy.deepcopy(dict(item)) for item in items],
        "total_matched": total_matched,
        "limit": len(items),
        "offset": offset,
        "has_more": has_more,
        "next_offset": next_offset,
    }
    if snapshot_fingerprint is not None:
        data["snapshot_fingerprint"] = snapshot_fingerprint
    return {
        "schema_version": 1,
        "ok": True,
        "status": "verified",
        "operation": "fetch_reminders",
        "data": data,
    }


def mutation_receipt(
    operation: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    backend: str = "eventkit_public_sdk",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ok": True,
        "status": "verified",
        "operation": operation,
        "operation_id": "B6A66F8E-B44F-426F-95CD-3A678865F793",
        "backend": backend,
        "target": {
            "id": after["id"],
            "calendar_id": after["calendar_id"],
        },
        "before": copy.deepcopy(dict(before)),
        "after": copy.deepcopy(dict(after)),
        "verification": {
            "state": "read_back",
            "read_back": True,
            "matched": True,
            "write_performed": True,
            "target_fields": ["title"],
        },
        "recovery": {
            "semantics": "eventkit_native_api",
            "plugin_backup": "not_created",
            "retry_policy": "read_before_retry",
        },
        "warnings": [
            {
                "code": "native_receipt_preserved",
                "message": "The structured warning survives the public projection.",
            }
        ],
    }


def facade(
    eventkit: FakeEventKit, *, enable_experimental: bool = False
) -> V2CoreFacade:
    return V2CoreFacade(
        eventkit,
        token_source=DeterministicTokens(),
        operation_id_source=DeterministicOperationIDs(),
        reference_ttl_seconds=30.0,
        enable_experimental=enable_experimental,
    )


class V2CoreFacadeTests(unittest.TestCase):
    def test_url_create_receipt_identifies_the_selected_runtime(self) -> None:
        for experimental in (False, True):
            with self.subTest(experimental=experimental):
                eventkit = FakeEventKit()
                created = {
                    **native_reminder(title="Open the project"),
                    "url": "https://example.com/project",
                }
                eventkit.queue(
                    "create_reminder",
                    mutation_receipt("create_reminder", {}, created),
                    mutation=True,
                )
                eventkit.queue("read_reminder", read_receipt(created))
                subject = facade(eventkit, enable_experimental=experimental)

                result = subject.create_reminder(
                    {
                        "list_id": "LIST-1",
                        "title": created["title"],
                        "url": created["url"],
                        "idempotency_key": "create-url-runtime-mode",
                    }
                )

                self.assertEqual(result["status"], "verified")
                self.assertEqual(
                    result["backend"],
                    "eventkit_plus_native_url" if experimental else "eventkit_public_sdk",
                )
                self.assertEqual(result["after"]["url"], created["url"])
                self.assertTrue(result["verification"]["matched"])
                validate_public_result("create_reminder", result, "committed")

    def test_url_patch_receipt_identifies_the_selected_runtime(self) -> None:
        for experimental in (False, True):
            for url in ("https://example.com/project", None):
                with self.subTest(experimental=experimental, url=url):
                    eventkit = FakeEventKit()
                    before = {
                        **native_reminder(),
                        "url": "https://example.com/original",
                    }
                    after = {
                        **before,
                        "url": url,
                        "last_modified": "2026-08-25T01:00:01.000Z",
                    }
                    eventkit.queue("read_reminder", read_receipt(before))
                    eventkit.queue("read_reminder", read_receipt(before))
                    eventkit.queue("read_reminder", read_receipt(after))
                    eventkit.queue(
                        "update_reminder",
                        mutation_receipt("update_reminder", before, after),
                        mutation=True,
                    )
                    subject = facade(eventkit, enable_experimental=experimental)
                    reference = subject.read_reminder({"reminder_id": before["id"]})[
                        "data"
                    ]["reminder"]["reference"]

                    result = subject.change_reminder(
                        {
                            "reference": reference,
                            "action": {"kind": "patch", "patch": {"url": url}},
                        }
                    )

                    self.assertEqual(result["status"], "verified")
                    self.assertEqual(
                        result["backend"],
                        "eventkit_plus_native_url"
                        if experimental and url is not None
                        else "eventkit_public_sdk",
                    )
                    self.assertEqual(result["after"]["url"], url)
                    self.assertTrue(result["verification"]["matched"])
                    validate_public_result("change_reminder", result, "committed")

    def test_list_and_fetch_reject_non_object_items_beyond_public_limit(self) -> None:
        cases = (
            (
                "list_calendars",
                "list_reminder_lists",
                {"id": "LIST-1", "title": "Inbox"},
                None,
                "invalid_eventkit_list_item",
            ),
            (
                "fetch_reminders",
                "fetch_reminders",
                native_reminder(),
                42,
                "invalid_eventkit_fetch_item",
            ),
        )
        for (
            native_operation,
            public_operation,
            valid_item,
            malformed_item,
            reason_code,
        ) in cases:
            eventkit = FakeEventKit()
            data: dict[str, Any] = {"items": [valid_item, malformed_item]}
            if native_operation == "fetch_reminders":
                data.update(
                    {
                        "total_matched": 2,
                        "has_more": False,
                        "next_offset": None,
                    }
                )
            eventkit.queue(
                native_operation,
                {
                    "schema_version": 1,
                    "ok": True,
                    "status": "verified",
                    "operation": native_operation,
                    "data": data,
                },
            )

            with self.subTest(operation=public_operation):
                result = getattr(facade(eventkit), public_operation)({"limit": 1})
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "failed_no_mutation")
                self.assertEqual(result["error"]["reason_code"], reason_code)
                validate_public_result(public_operation, result, "not_mutated")

    def test_missing_signed_native_helper_has_an_actionable_recovery_path(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue(
            "list_calendars",
            {
                "schema_version": 1,
                "ok": False,
                "status": "failed_no_mutation",
                "operation": "list_calendars",
                "error": {
                    "code": "unexpected_error",
                    "reason_code": "native_helper_unavailable",
                    "message": "The signed EventKit helper is unavailable",
                    "retryable": False,
                },
            },
            is_error=True,
        )

        result = facade(eventkit).list_reminder_lists({})

        self.assertEqual(result["status"], "failed_no_mutation")
        self.assertEqual(result["next_action"]["kind"], "diagnose")
        self.assertEqual(result["next_action"]["tool"], "diagnose_reminders")
        self.assertFalse(result["next_action"]["retry_original_once"])
        self.assertIn("scope=packaging", result["next_action"]["message"])
        self.assertIn("Reinstall or update", result["next_action"]["message"])
        self.assertNotIn("xcode-select --install", result["next_action"]["message"])
        validate_public_result("list_reminder_lists", result)

    def test_core_build_failure_never_recommends_a_compiler_or_install_prompt(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue(
            "list_calendars",
            {
                "schema_version": 1,
                "ok": False,
                "status": "failed_no_mutation",
                "operation": "list_calendars",
                "error": {
                    "code": "unexpected_error",
                    "reason_code": "native_helper_build_failed",
                    "message": "The Core helper could not be prepared",
                    "retryable": False,
                },
            },
            is_error=True,
        )

        result = facade(eventkit).list_reminder_lists({})

        message = result["next_action"]["message"]
        self.assertIn("scope=packaging", message)
        self.assertIn("execution_mode=metadata_only", message)
        self.assertNotIn("compiler", message.casefold())
        self.assertNotIn("xcode-select", message)
        validate_public_result("list_reminder_lists", result)

    def test_exact_read_maps_backend_not_found_category_to_public_not_found(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue(
            "read_reminder",
            {
                "schema_version": 1,
                "ok": False,
                "status": "failed_no_mutation",
                "operation": "read_reminder",
                "error": {
                    "category": "not_found",
                    "code": "ambiguous_target",
                    "reason_code": "reminder_not_found",
                    "message": "Reminder was not found",
                    "retryable": False,
                },
            },
            is_error=True,
        )

        result = facade(eventkit).read_reminder({"reminder_id": "MISSING"})

        self.assertEqual(result["status"], "failed_no_mutation")
        self.assertEqual(result["error"]["code"], "not_found")
        self.assertEqual(result["error"]["reason_code"], "reminder_not_found")

    def test_create_transport_exception_is_an_unknown_outcome_not_a_false_failure(self) -> None:
        eventkit = FakeEventKit()
        eventkit.fail(
            "create_reminder",
            RuntimeError("native process disappeared after dispatch"),
            mutation=True,
        )

        result = facade(eventkit).create_reminder(
            {
                "list_id": "LIST-1",
                "title": "Possibly created",
                "idempotency_key": "capture-20260825-unknown",
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertEqual(result["verification"]["state"], "pending")
        self.assertFalse(result["recovery"]["automatic_retry_safe"])
        self.assertEqual(result["next_action"]["tool"], "fetch_reminders")
        self.assertIn("list", result["next_action"]["message"].lower())

    def test_change_transport_exception_consumes_reference_and_reports_pending(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue("read_reminder", read_receipt(native_reminder()))
        eventkit.queue("read_reminder", read_receipt(native_reminder()))
        eventkit.fail(
            "update_reminder",
            RuntimeError("native process disappeared after dispatch"),
            mutation=True,
        )
        subject = facade(eventkit)
        reference = subject.read_reminder({"reminder_id": "REMINDER-1"})["data"][
            "reminder"
        ]["reference"]

        result = subject.change_reminder(
            {
                "reference": reference,
                "action": {"kind": "patch", "patch": {"title": "Maybe written"}},
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "committed_verification_pending")
        rejected = subject.change_reminder(
            {
                "reference": reference,
                "action": {"kind": "patch", "patch": {"title": "Unsafe retry"}},
            }
        )
        self.assertEqual(rejected["error"]["reason_code"], "invalid_reference")
        self.assertEqual(len(eventkit.calls), 3)

    def test_delete_transport_exception_consumes_reference_and_reports_pending(self) -> None:
        eventkit = FakeEventKit()
        before = native_reminder()
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.fail(
            "delete_reminder",
            RuntimeError("native process disappeared after dispatch"),
            mutation=True,
        )
        subject = facade(eventkit)
        reference = subject.read_reminder({"reminder_id": "REMINDER-1"})["data"][
            "reminder"
        ]["reference"]

        result = subject.delete_reminder({"reference": reference})

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "committed_verification_pending")
        rejected = subject.delete_reminder({"reference": reference})
        self.assertEqual(rejected["error"]["reason_code"], "invalid_reference")
        self.assertEqual(len(eventkit.calls), 3)

    def test_delete_public_no_write_claim_cannot_preserve_a_stale_reference(self) -> None:
        for raw_state in ("committed", "unknown"):
            with self.subTest(raw_state=raw_state):
                eventkit = FakeEventKit()
                before = native_reminder()
                eventkit.queue("read_reminder", read_receipt(before))
                eventkit.queue("read_reminder", read_receipt(before))
                eventkit.queue(
                    "delete_reminder",
                    {
                        "schema_version": 1,
                        "ok": False,
                        "status": "failed_no_mutation",
                        "operation": "delete_reminder",
                        "operation_id": "12345678-1234-4234-9234-1234567890ab",
                        "backend": "eventkit_public_sdk",
                        "target": {"id": "REMINDER-1"},
                        "before": before,
                        "after": None,
                        "verification": {
                            "state": "not_needed",
                            "write_performed": False,
                            "final_read": False,
                        },
                        "recovery": {
                            "semantics": "fresh_read_required",
                            "automatic_retry_safe": False,
                        },
                        "error": {
                            "code": "permission_denied",
                            "reason_code": "synthetic_public_projection",
                            "message": "The public projection claims no write.",
                            "retryable": False,
                        },
                    },
                    mutation=True,
                    is_error=True,
                    mutation_state=raw_state,
                )
                subject = facade(eventkit)
                reference = subject.read_reminder(
                    {"reminder_id": "REMINDER-1"}
                )["data"]["reminder"]["reference"]

                payload, state = subject.call_with_state(
                    "delete_reminder",
                    {"reference": reference},
                )
                rejected, rejected_state = subject.call_with_state(
                    "delete_reminder",
                    {"reference": reference},
                )

                self.assertEqual(
                    payload["status"],
                    "committed_verification_pending",
                )
                self.assertIs(
                    payload["verification"]["write_performed"],
                    True if raw_state == "committed" else None,
                )
                self.assertEqual(state, raw_state)
                validate_public_result("delete_reminder", payload, state)
                self.assertEqual(
                    rejected["error"]["reason_code"],
                    "invalid_reference",
                )
                self.assertEqual(rejected_state, "not_mutated")
                self.assertEqual(len(eventkit.calls), 3)

    def test_delete_verified_without_exact_absence_is_downgraded_to_pending(self) -> None:
        eventkit = FakeEventKit()
        before = native_reminder()
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue("read_reminder", read_receipt(before))
        receipt = mutation_receipt("delete_reminder", before, before)
        receipt["verification"] = {
            "state": "read_back",
            "write_performed": True,
            "final_read": True,
            "matched": True,
        }
        eventkit.queue("delete_reminder", receipt, mutation=True)
        subject = facade(eventkit)
        reference = subject.read_reminder({"reminder_id": "REMINDER-1"})["data"][
            "reminder"
        ]["reference"]

        result = subject.delete_reminder({"reference": reference})

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertFalse(result["verification"]["final_read"])
        self.assertEqual(result["error"]["reason_code"], "delete_absence_unverified")

    def test_request_access_is_explicit_and_content_free(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue(
            "request_access",
            {
                "schema_version": 1,
                "ok": True,
                "status": "verified",
                "operation": "request_access",
                "data": {
                    "authorization_before": "full_access",
                    "authorization": "full_access",
                    "request_attempted": True,
                    "prompt_expected": False,
                    "prompt_observed": None,
                    "prompted_explicitly": True,
                },
            },
        )

        result = facade(eventkit).request_reminders_access({})

        self.assertEqual(eventkit.calls, [("request_access", {}, False)])
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["operation"], "request_reminders_access")
        self.assertEqual(
            result["data"],
            {
                "authorization_before": "full_access",
                "authorization": "full_access",
                "request_attempted": True,
                "prompt_expected": False,
                "prompt_observed": None,
                "prompted_explicitly": True,
            },
        )
        self.assertTrue(result["data"]["prompted_explicitly"])

    def test_request_access_rejects_inconsistent_prompt_expectation(self) -> None:
        for authorization_before, prompt_expected in (
            ("full_access", True),
            ("not_determined", False),
        ):
            with self.subTest(
                authorization_before=authorization_before,
                prompt_expected=prompt_expected,
            ):
                eventkit = FakeEventKit()
                eventkit.queue(
                    "request_access",
                    {
                        "schema_version": 1,
                        "ok": True,
                        "status": "verified",
                        "operation": "request_access",
                        "data": {
                            "authorization_before": authorization_before,
                            "authorization": "full_access",
                            "request_attempted": True,
                            "prompt_expected": prompt_expected,
                            "prompt_observed": None,
                            "prompted_explicitly": True,
                        },
                    },
                )

                result = facade(eventkit).request_reminders_access({})

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["reason_code"], "invalid_access_response")

    def test_request_access_denial_preserves_receipt_without_self_retry(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue(
            "request_access",
            {
                "schema_version": 1,
                "ok": False,
                "status": "failed_no_mutation",
                "operation": "request_access",
                "error": {
                    "code": "permission_denied",
                    "reason_code": "reminders_access_denied",
                    "message": "Reminders access was not granted",
                    "retryable": False,
                    "details": {
                        "authorization_before": "not_determined",
                        "authorization": "denied",
                        "request_attempted": True,
                        "prompt_expected": True,
                        "prompt_observed": None,
                        "prompted_explicitly": True,
                    },
                },
            },
            is_error=True,
        )

        result = facade(eventkit).request_reminders_access({})

        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(result["data"]["authorization"], "denied")
        self.assertTrue(result["data"]["prompt_expected"])
        self.assertNotIn("next_action", result)
        validate_public_result("request_reminders_access", result)

    def test_already_denied_or_revoked_access_does_not_expect_another_prompt(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue(
            "request_access",
            {
                "schema_version": 1,
                "ok": False,
                "status": "failed_no_mutation",
                "operation": "request_access",
                "error": {
                    "code": "permission_denied",
                    "reason_code": "reminders_access_denied",
                    "message": "Reminders access remains denied",
                    "retryable": False,
                    "details": {
                        "authorization_before": "denied",
                        "authorization": "denied",
                        "request_attempted": True,
                        "prompt_expected": False,
                        "prompt_observed": None,
                        "prompted_explicitly": True,
                    },
                },
            },
            is_error=True,
        )

        result = facade(eventkit).request_reminders_access({})

        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(result["data"]["authorization_before"], "denied")
        self.assertFalse(result["data"]["prompt_expected"])
        self.assertNotIn("next_action", result)
        validate_public_result("request_reminders_access", result)

    def test_request_access_rejects_verified_non_full_access(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue(
            "request_access",
            {
                "schema_version": 1,
                "ok": True,
                "status": "verified",
                "operation": "request_access",
                "data": {
                    "authorization_before": "not_determined",
                    "authorization": "denied",
                    "request_attempted": True,
                    "prompt_expected": True,
                    "prompt_observed": None,
                    "prompted_explicitly": True,
                },
            },
        )

        result = facade(eventkit).request_reminders_access({})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["reason_code"], "invalid_access_response")

    def test_ensure_list_passes_normalized_input_to_durable_backend(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue(
            "ensure_reminder_list",
            {
                "schema_version": 1,
                "ok": True,
                "status": "verified",
                "operation": "ensure_reminder_list",
                "operation_id": "12345678-1234-4234-9234-1234567890ab",
                "backend": "eventkit_public_sdk",
                "idempotency_key_hash": "a" * 64,
                "target": {"source_id": "SOURCE-1", "list_id": "LIST-1"},
                "before": None,
                "after": {
                    "id": "LIST-1",
                    "title": "Work",
                    "color": "#ff0000",
                    "emblem": "private-symbol",
                    "private_backend_field": "must-not-escape",
                    "type": "caldav",
                    "allows_content_modifications": True,
                    "subscribed": False,
                    "immutable": False,
                    "source": {
                        "id": "SOURCE-1",
                        "title": "iCloud",
                        "type": "caldav",
                        "is_delegate": False,
                        "reminder_calendar_count": 3,
                        "private_source_field": "must-not-escape",
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
            },
            mutation=True,
        )
        subject = facade(eventkit)
        self.assertFalse(hasattr(subject, "_idempotency"))
        self.assertFalse(hasattr(subject, "_idempotency_lock"))
        arguments = {
            "source_id": "SOURCE-1",
            "name": "  Work  ",
            "idempotency_key": "ensure:work:1",
        }

        first = subject.ensure_reminder_list(arguments)

        self.assertEqual(
            eventkit.calls,
            [
                (
                    "ensure_reminder_list",
                    {
                        "source_id": "SOURCE-1",
                        "name": "Work",
                        "idempotency_key": "ensure:work:1",
                    },
                    True,
                )
            ],
        )
        self.assertEqual(first["target"], {"source_id": "SOURCE-1", "list_id": "LIST-1"})
        self.assertEqual(first["after"]["source"]["reminder_list_count"], 3)
        self.assertNotIn("color", first["after"])
        self.assertNotIn("emblem", first["after"])
        self.assertNotIn("private_backend_field", first["after"])
        self.assertNotIn("private_source_field", first["after"]["source"])
        self.assertFalse(first["replayed"])
        self.assertEqual(len(first["idempotency_key_hash"]), 64)
        validate_public_result("ensure_reminder_list", first, "committed")

    def test_duplicate_list_warning_cannot_hide_a_committed_or_unknown_write(self) -> None:
        for status, mutation_state in (("verified", "committed"), ("unchanged", "unknown")):
            with self.subTest(status=status, mutation_state=mutation_state):
                eventkit = FakeEventKit()
                eventkit.queue(
                    "ensure_reminder_list",
                    {
                        "schema_version": 1,
                        "ok": True,
                        "status": status,
                        "operation": "ensure_reminder_list",
                        "target": {"source_id": "SOURCE-1", "list_id": "ARBITRARY-LIST"},
                        "after": {"id": "ARBITRARY-LIST", "title": "Work"},
                        "warnings": [
                            {"code": "duplicate_list_name_in_source", "message": "duplicate"}
                        ],
                    },
                    mutation=True,
                    mutation_state=mutation_state,
                )
                result, state = facade(eventkit).call_with_state(
                    "ensure_reminder_list",
                    {"source_id": "SOURCE-1", "name": "Work", "idempotency_key": "ensure:uncertain"},
                )

                self.assertEqual(result["status"], "committed_verification_pending")
                self.assertEqual(state, mutation_state)
                self.assertEqual(result["error"]["reason_code"], "ambiguous_list_mutation_outcome")
                self.assertNotIn("ARBITRARY-LIST", repr(result))
                self.assertIsNone(result["after"])
                self.assertEqual(result["next_action"]["tool"], "list_reminder_lists")
                validate_public_result("ensure_reminder_list", result, state)

    def test_ensure_list_dispatch_exception_is_pending(self) -> None:
        eventkit = FakeEventKit()
        eventkit.fail(
            "ensure_reminder_list",
            RuntimeError("bridge disappeared after dispatch"),
            mutation=True,
        )
        subject = facade(eventkit)
        arguments = {
            "source_id": "SOURCE-1",
            "name": "Work",
            "idempotency_key": "ensure:work:unknown",
        }

        first = subject.ensure_reminder_list(arguments)

        self.assertEqual(len(eventkit.calls), 1)
        self.assertEqual(first["status"], "committed_verification_pending")
        self.assertFalse(first["replayed"])
        self.assertEqual(first["error"]["code"], "sync_pending")
        self.assertFalse(first["error"]["retryable"])
        self.assertEqual(first["warnings"][0]["code"], "verification_pending")
        self.assertEqual(first["next_action"]["tool"], "list_reminder_lists")
        self.assertFalse(first["next_action"]["retry_original_once"])
        validate_public_result("ensure_reminder_list", first, "unknown")

    def test_ensure_list_malformed_post_dispatch_reply_is_pending(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue(
            "ensure_reminder_list",
            {"ok": True, "unexpected": "shape"},
            mutation=True,
        )

        result = facade(eventkit).ensure_reminder_list(
            {
                "source_id": "SOURCE-1",
                "name": "Work",
                "idempotency_key": "ensure:work:malformed",
            }
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertEqual(result["error"]["reason_code"], "invalid_eventkit_list_receipt")
        self.assertFalse(result["error"]["retryable"])
        self.assertEqual(result["next_action"]["tool"], "list_reminder_lists")
        validate_public_result("ensure_reminder_list", result, "unknown")

    def test_create_translates_list_id_and_returns_one_final_reference(self) -> None:
        eventkit = FakeEventKit()
        created = {**native_reminder(title="Buy oat milk"), "notes": "Unsweetened"}
        eventkit.queue(
            "create_reminder",
            mutation_receipt("create_reminder", {}, created),
            mutation=True,
        )
        eventkit.queue("read_reminder", read_receipt(created))

        result = facade(eventkit).create_reminder(
            {
                "list_id": "LIST-1",
                "title": "Buy oat milk",
                "notes": "Unsweetened",
                "idempotency_key": "capture-20260825-1",
            }
        )

        self.assertEqual(eventkit.calls[0][0], "create_reminder")
        self.assertTrue(eventkit.calls[0][2])
        self.assertEqual(eventkit.calls[0][1]["calendar_id"], "LIST-1")
        self.assertNotIn("list_id", eventkit.calls[0][1])
        self.assertEqual(
            eventkit.calls[0][1]["idempotency_key"],
            "capture-20260825-1",
        )
        self.assertEqual(
            eventkit.calls[1],
            ("read_reminder", {"reminder_id": "REMINDER-1"}, False),
        )
        self.assertEqual(result["operation"], "create_reminder")
        self.assertEqual(result["after"]["list_id"], "LIST-1")
        self.assertEqual(result["after"]["reference"], "rev1." + "A" * 32)
        self.assertTrue(result["verification"]["final_read"])

    def test_create_final_read_must_match_the_requested_fields(self) -> None:
        eventkit = FakeEventKit()
        created = native_reminder(title="Requested title")
        eventkit.queue(
            "create_reminder",
            mutation_receipt("create_reminder", {}, created),
            mutation=True,
        )
        eventkit.queue(
            "read_reminder",
            read_receipt(native_reminder(title="Conflicting title")),
        )

        result = facade(eventkit).create_reminder(
            {
                "list_id": "LIST-1",
                "title": "Requested title",
                "idempotency_key": "capture-final-mismatch",
            }
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertIsNone(result["verification"]["matched"])
        self.assertFalse(result["verification"]["final_read"])
        self.assertEqual(result["error"]["reason_code"], "create_final_state_mismatch")
        self.assertNotIn("rev1.", repr(result))
        self.assertFalse(result["recovery"]["automatic_retry_safe"])

    def test_unchanged_create_with_failed_final_read_is_contract_valid_pending(self) -> None:
        eventkit = FakeEventKit()
        created = native_reminder(title="Already present")
        unchanged = mutation_receipt("create_reminder", {}, created)
        unchanged["status"] = "unchanged"
        unchanged["verification"]["write_performed"] = False
        unchanged["recovery"]["automatic_retry_safe"] = True
        eventkit.queue("create_reminder", unchanged, mutation=True)
        eventkit.queue(
            "read_reminder",
            {
                "ok": False,
                "status": "failed_no_mutation",
                "operation": "read_reminder",
                "error": {
                    "code": "sync_pending",
                    "reason_code": "exact_read_unavailable",
                    "message": "The exact read is temporarily unavailable.",
                    "retryable": True,
                },
            },
            is_error=True,
        )

        result = facade(eventkit).create_reminder(
            {
                "list_id": "LIST-1",
                "title": "Already present",
                "idempotency_key": "capture-unchanged-final-read-failure",
            }
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertIsNone(result["verification"]["write_performed"])
        self.assertFalse(result["verification"]["final_read"])
        self.assertFalse(result["recovery"]["automatic_retry_safe"])
        self.assertEqual(result["next_action"]["tool"], "fetch_reminders")
        validate_public_result("create_reminder", result, "unknown")

    def test_unchanged_delete_without_absence_proof_is_contract_valid_pending(self) -> None:
        eventkit = FakeEventKit()
        before = native_reminder()
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue("read_reminder", read_receipt(before))
        unchanged = mutation_receipt("delete_reminder", before, before)
        unchanged["status"] = "unchanged"
        unchanged["verification"]["write_performed"] = False
        unchanged["verification"].pop("local_absence", None)
        unchanged["recovery"]["automatic_retry_safe"] = True
        eventkit.queue("delete_reminder", unchanged, mutation=True)
        subject = facade(eventkit)
        reference = subject.read_reminder({"reminder_id": "REMINDER-1"})["data"][
            "reminder"
        ]["reference"]

        result = subject.delete_reminder({"reference": reference})

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertIsNone(result["verification"]["write_performed"])
        self.assertFalse(result["recovery"]["automatic_retry_safe"])
        self.assertEqual(result["next_action"]["tool"], "read_reminder")
        validate_public_result("delete_reminder", result, "unknown")

    def test_create_partial_success_never_issues_a_writable_reference(self) -> None:
        eventkit = FakeEventKit()
        created = native_reminder()
        receipt = mutation_receipt(
            "create_reminder",
            {},
            {**created, "url": "https://example.com"},
            backend="eventkit_public_sdk+reminderkit_private",
        )
        receipt["status"] = "partial_success"
        receipt["verification"] = {
            "state": "partial",
            "write_performed": True,
            "final_read": True,
            "matched": True,
            "url_attachment": {"state": "failed"},
        }
        receipt["warnings"] = [
            {
                "code": "native_url_attachment_failed",
                "message": "The metadata committed but the visible attachment did not.",
            }
        ]
        receipt["error"] = {
            "code": "sync_pending",
            "reason_code": "native_url_attachment_failed",
            "message": "Visible URL attachment requires repair.",
            "retryable": True,
        }
        eventkit.queue("create_reminder", receipt, mutation=True)

        result = facade(eventkit, enable_experimental=True).create_reminder(
            {
                "list_id": "LIST-1",
                "title": "Open spec",
                "url": "https://example.com",
                "idempotency_key": "capture-20260825-2",
            }
        )

        self.assertEqual(result["status"], "partial_success")
        self.assertEqual(result["backend"], "eventkit_plus_native_url")
        self.assertIsNone(result["after"])
        self.assertEqual(len(eventkit.calls), 1)
        self.assertNotIn("rev1.", repr(result))

    def test_delete_revalidates_the_reference_then_consumes_it(self) -> None:
        eventkit = FakeEventKit()
        before = native_reminder()
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue("read_reminder", read_receipt(before))
        receipt = mutation_receipt("delete_reminder", before, before)
        receipt["after"] = {"id": "REMINDER-1", "deleted": True}
        receipt["verification"] = {
            "state": "read_back",
            "write_performed": True,
            # This is the exact field emitted by the bundled EventKit helper.
            "store_no_longer_active": True,
        }
        eventkit.queue("delete_reminder", receipt, mutation=True)
        subject = facade(eventkit)
        reference = subject.read_reminder({"reminder_id": "REMINDER-1"})["data"][
            "reminder"
        ]["reference"]

        result = subject.delete_reminder({"reference": reference})

        self.assertEqual(eventkit.calls[1][0], "read_reminder")
        self.assertEqual(
            eventkit.calls[2],
            (
                "delete_reminder",
                {
                    "reminder_id": "REMINDER-1",
                    "expected_last_modified": "2026-08-25T01:00:00.000Z",
                },
                True,
            ),
        )
        self.assertEqual(result["operation"], "delete_reminder")
        self.assertTrue(result["verification"]["local_absence"])
        rejected = subject.delete_reminder({"reference": reference})
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"]["reason_code"], "invalid_reference")
        self.assertEqual(len(eventkit.calls), 3)

    def test_list_reminder_lists_translates_the_public_name_and_bounds_output(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue(
            "list_calendars",
            {
                "schema_version": 1,
                "ok": True,
                "status": "verified",
                "operation": "list_calendars",
                "data": {
                    "items": [
                        {
                            "id": "LIST-1",
                            "title": "Inbox",
                            "type": "caldav",
                            "allows_content_modifications": True,
                            "subscribed": False,
                            "immutable": False,
                            "source": {
                                "id": "SOURCE-1",
                                "title": "iCloud",
                                "type": "caldav",
                                "is_delegate": False,
                                "reminder_calendar_count": 2,
                            },
                        },
                        {
                            "id": "LIST-2",
                            "title": "Work",
                            "type": "caldav",
                            "allows_content_modifications": True,
                            "subscribed": False,
                            "immutable": False,
                            "source": {
                                "id": "SOURCE-1",
                                "title": "iCloud",
                                "type": "caldav",
                                "is_delegate": False,
                                "reminder_calendar_count": 2,
                            },
                        },
                    ]
                },
            },
        )

        result = facade(eventkit).list_reminder_lists(
            {"source_id": "SOURCE-1", "writable_only": True, "limit": 1}
        )

        self.assertEqual(
            eventkit.calls,
            [("list_calendars", {"source_id": "SOURCE-1", "writable_only": True}, False)],
        )
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["operation"], "list_reminder_lists")
        self.assertEqual(result["data"]["returned"], 1)
        self.assertTrue(result["data"]["truncated"])
        self.assertEqual(
            result["data"]["items"][0]["source"]["reminder_list_count"],
            2,
        )
        self.assertNotIn("calendar", repr(result))

    def test_read_reminder_returns_public_list_vocabulary_and_one_opaque_reference(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue("read_reminder", read_receipt(native_reminder()))
        subject = facade(eventkit)

        result = subject.read_reminder({"reminder_id": "REMINDER-1"})

        reminder = result["data"]["reminder"]
        self.assertEqual(result["operation"], "read_reminder")
        self.assertEqual(reminder["list_id"], "LIST-1")
        self.assertEqual(reminder["list_title"], "Inbox")
        self.assertEqual(reminder["reference"], "rev1." + "A" * 32)
        self.assertNotIn("calendar_id", reminder)
        self.assertEqual(
            eventkit.calls,
            [("read_reminder", {"reminder_id": "REMINDER-1"}, False)],
        )
        self.assertTrue(callable(subject.reference_port.revalidate_reference))
        self.assertTrue(callable(subject.reference_port.invalidate_reference))

    def test_exact_read_bounds_read_only_alarm_action_metadata(self) -> None:
        eventkit = FakeEventKit()
        reminder = native_reminder()
        reminder["alarms"] = [
            {
                "kind": "relative",
                "offset_seconds": -900,
                "read_only": True,
                "_verification_unavailable": True,
                "action": {
                    "type": "audio" * 20,
                    "email_address": "e" * 2_000,
                    "sound_name": "s" * 1_000,
                    "url": "u" * 10_000,
                },
            }
        ]
        eventkit.queue("read_reminder", read_receipt(reminder))

        result = facade(eventkit).read_reminder({"reminder_id": "REMINDER-1"})

        action = result["data"]["reminder"]["alarms"][0]["action"]
        self.assertNotIn(
            "_verification_unavailable",
            result["data"]["reminder"]["alarms"][0],
        )
        self.assertEqual(len(action["type"]), 16)
        self.assertEqual(len(action["email_address"]), 1_000)
        self.assertEqual(len(action["sound_name"]), 512)
        self.assertEqual(len(action["url"]), 8_192)

    def test_final_verification_keeps_full_alarm_action_metadata(self) -> None:
        eventkit = FakeEventKit()
        before = native_reminder()
        before["due"] = {"kind": "all_day", "date": "2027-08-31"}
        before["alarms"] = [
            {
                "kind": "relative",
                "offset_seconds": -900,
                "read_only": True,
                "action": {
                    "type": "audio",
                    "sound_name": "s" * 512 + "A",
                },
            }
        ]
        committed = copy.deepcopy(before)
        committed["due"] = {"kind": "all_day", "date": "2027-09-30"}
        final = copy.deepcopy(committed)
        final["alarms"][0]["action"]["sound_name"] = "s" * 512 + "B"
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue(
            "update_reminder",
            mutation_receipt("update_reminder", before, committed),
            mutation=True,
        )
        eventkit.queue("read_reminder", read_receipt(final))
        subject = facade(eventkit)
        reference = subject.read_reminder({"reminder_id": "REMINDER-1"})[
            "data"
        ]["reminder"]["reference"]

        result = subject.change_reminder(
            {
                "reference": reference,
                "action": {
                    "kind": "patch",
                    "patch": {
                        "due": {"kind": "all_day", "date": "2027-09-30"}
                    },
                },
            }
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertEqual(result["error"]["reason_code"], "final_state_mismatch")

    def test_exact_read_does_not_invent_a_reference_without_last_modified(self) -> None:
        eventkit = FakeEventKit()
        reminder = native_reminder()
        reminder["last_modified"] = None
        eventkit.queue("read_reminder", read_receipt(reminder))

        result = facade(eventkit).read_reminder({"reminder_id": "REMINDER-1"})

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_no_mutation")
        self.assertEqual(result["error"]["code"], "sync_pending")
        self.assertEqual(result["error"]["reason_code"], "missing_last_modified")
        self.assertEqual(result["next_action"]["kind"], "fresh_read")

    def test_fetch_translates_list_filters_without_issuing_writable_references(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue(
            "fetch_reminders",
            {
                "schema_version": 1,
                "ok": True,
                "status": "verified",
                "operation": "fetch_reminders",
                "data": {
                    "items": [
                        native_reminder(),
                        native_reminder(
                            reminder_id="REMINDER-2",
                            title="Write changelog",
                            last_modified="2026-08-25T02:00:00.000Z",
                        ),
                    ],
                    "total_matched": 2,
                    "limit": 2,
                    "offset": 0,
                    "has_more": False,
                    "next_offset": None,
                },
            },
        )

        result = facade(eventkit).fetch_reminders(
            {"list_ids": ["LIST-1"], "limit": 2, "sort": "modified"}
        )

        self.assertEqual(
            eventkit.calls,
            [
                (
                    "fetch_reminders",
                    {
                        "calendar_ids": ["LIST-1"],
                        "status": "incomplete",
                        "limit": 2,
                        "sort": "modified",
                        "offset": 0,
                    },
                    False,
                )
            ],
        )
        self.assertEqual(result["data"]["returned"], 2)
        self.assertFalse(result["data"]["has_more"])
        self.assertFalse(result["data"]["pagination_exhausted"])
        self.assertIsNone(result["data"]["next_cursor"])
        self.assertNotIn("reference", result["data"]["items"][0])
        self.assertNotIn("reference", result["data"]["items"][1])
        self.assertNotIn("calendar_id", repr(result))

    def test_fetch_uses_a_bounded_summary_projection(self) -> None:
        eventkit = FakeEventKit()
        reminder = native_reminder()
        reminder["notes"] = "n" * 10_000
        reminder["alarms"] = [
            {"kind": "absolute", "date_time": f"2026-08-25T0{index}:00:00.000Z"}
            for index in range(10)
        ]
        eventkit.queue(
            "fetch_reminders",
            {
                "schema_version": 1,
                "ok": True,
                "status": "verified",
                "operation": "fetch_reminders",
                "data": {
                    "items": [reminder],
                    "total_matched": 1,
                    "has_more": False,
                    "next_offset": None,
                },
            },
        )

        result = facade(eventkit).fetch_reminders(
            {"list_ids": ["LIST-1"], "limit": 1}
        )

        summary = result["data"]["items"][0]
        self.assertEqual(len(summary["notes"]), 2_000)
        self.assertEqual(len(summary["alarms"]), 5)
        self.assertEqual(summary["alarm_count"], 10)
        self.assertNotIn("external_id", summary)
        self.assertNotIn("created", summary)

    def test_fetch_preserves_read_only_relative_alarm_action_metadata(self) -> None:
        eventkit = FakeEventKit()
        reminder = native_reminder()
        reminder["alarms"] = [
            {
                "kind": "relative",
                "offset_seconds": -900,
                "read_only": True,
                "action": {
                    "type": "audio",
                    "sound_name": "Glass",
                    "email_address": "alerts@example.com",
                    "url": "example:run",
                },
            }
        ]
        eventkit.queue(
            "fetch_reminders",
            fetch_receipt(
                [reminder],
                total_matched=1,
                offset=0,
                has_more=False,
                next_offset=None,
                snapshot_fingerprint=None,
            ),
        )

        result = facade(eventkit).fetch_reminders(
            {"list_ids": ["LIST-1"], "limit": 1}
        )

        self.assertEqual(
            result["data"]["items"][0]["alarms"],
            [
                {
                    "kind": "relative",
                    "offset_seconds": -900,
                    "read_only": True,
                    "action": {
                        "type": "audio",
                        "sound_name": "Glass",
                        "email_address": "alerts@example.com",
                        "url": "example:run",
                    },
                }
            ],
        )

    def test_fetch_cursor_keeps_the_ordered_snapshot_private_across_pages(self) -> None:
        eventkit = FakeEventKit()
        snapshot = "a" * 64
        eventkit.queue(
            "fetch_reminders",
            fetch_receipt(
                [native_reminder()],
                total_matched=2,
                offset=0,
                has_more=True,
                next_offset=1,
                snapshot_fingerprint=snapshot,
            ),
        )
        eventkit.queue(
            "fetch_reminders",
            fetch_receipt(
                [native_reminder(reminder_id="REMINDER-2", title="Second")],
                total_matched=2,
                offset=1,
                has_more=False,
                next_offset=None,
                snapshot_fingerprint=snapshot,
            ),
        )
        subject = facade(eventkit)
        filters = {"list_ids": ["LIST-1"], "limit": 1, "sort": "title"}

        first = subject.fetch_reminders(filters)
        cursor = first["data"]["next_cursor"]
        second = subject.fetch_reminders({**filters, "cursor": cursor})

        self.assertTrue(first["ok"])
        self.assertIsInstance(cursor, str)
        self.assertNotIn("snapshot_fingerprint", repr(first))
        self.assertTrue(second["ok"])
        self.assertEqual(second["data"]["items"][0]["id"], "REMINDER-2")
        self.assertIsNone(second["data"]["next_cursor"])
        self.assertEqual(eventkit.calls[1][1]["offset"], 1)
        self.assertNotIn("snapshot_fingerprint", repr(second))

    def test_fetch_page_two_fails_when_ordered_snapshot_changed(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue(
            "fetch_reminders",
            fetch_receipt(
                [native_reminder()],
                total_matched=2,
                offset=0,
                has_more=True,
                next_offset=1,
                snapshot_fingerprint="a" * 64,
            ),
        )
        eventkit.queue(
            "fetch_reminders",
            fetch_receipt(
                [native_reminder(reminder_id="REMINDER-3", title="Changed")],
                total_matched=2,
                offset=1,
                has_more=False,
                next_offset=None,
                snapshot_fingerprint="b" * 64,
            ),
        )
        subject = facade(eventkit)
        filters = {"list_ids": ["LIST-1"], "limit": 1, "sort": "title"}
        first = subject.fetch_reminders(filters)

        result = subject.fetch_reminders(
            {**filters, "cursor": first["data"]["next_cursor"]}
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_no_mutation")
        self.assertEqual(result["error"]["code"], "concurrent_modification")
        self.assertEqual(result["error"]["reason_code"], "pagination_snapshot_stale")
        self.assertIn(
            "Restart fetch_reminders without a cursor",
            result["error"]["message"],
        )
        self.assertNotIn("data", result)
        self.assertEqual(result["next_action"]["tool"], "fetch_reminders")
        self.assertFalse(result["next_action"]["retry_original_once"])
        self.assertIn("without a cursor", result["next_action"]["message"])
        validate_public_result("fetch_reminders", result)

    def test_fetch_does_not_issue_a_cursor_without_a_snapshot_fingerprint(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue(
            "fetch_reminders",
            fetch_receipt(
                [native_reminder()],
                total_matched=2,
                offset=0,
                has_more=True,
                next_offset=1,
                snapshot_fingerprint=None,
            ),
        )

        result = facade(eventkit).fetch_reminders(
            {"list_ids": ["LIST-1"], "limit": 1}
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["reason_code"], "missing_pagination_snapshot")
        self.assertNotIn("data", result)

    def test_change_uses_core_reference_guard_and_returns_the_final_exact_read(self) -> None:
        eventkit = FakeEventKit()
        before = native_reminder()
        committed = native_reminder(
            title="Ship public beta",
            last_modified="2026-08-25T02:00:00.000Z",
        )
        final = {
            **committed,
            "last_modified": "2026-08-25T03:00:00.000Z",
        }
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue(
            "update_reminder",
            mutation_receipt("update_reminder", before, committed),
            mutation=True,
        )
        eventkit.queue("read_reminder", read_receipt(final))
        subject = facade(eventkit)
        reference = subject.read_reminder({"reminder_id": "REMINDER-1"})["data"][
            "reminder"
        ]["reference"]

        result = subject.change_reminder(
            {
                "reference": reference,
                "action": {
                    "kind": "patch",
                    "patch": {"title": "Ship public beta"},
                },
            }
        )

        self.assertEqual(
            eventkit.calls[2],
            (
                "update_reminder",
                {
                    "reminder_id": "REMINDER-1",
                    "expected_last_modified": "2026-08-25T01:00:00.000Z",
                    "patch": {"title": "Ship public beta"},
                },
                True,
            ),
        )
        self.assertEqual(
            eventkit.calls[3],
            ("read_reminder", {"reminder_id": "REMINDER-1"}, False),
        )
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["operation"], "change_reminder.patch")
        self.assertEqual(result["target"], {"reminder_id": "REMINDER-1", "list_id": "LIST-1"})
        self.assertEqual(result["after"]["title"], "Ship public beta")
        self.assertEqual(result["after"]["last_modified"], "2026-08-25T03:00:00.000Z")
        self.assertEqual(result["after"]["reference"], "rev1." + "B" * 32)
        self.assertNotIn("reference", result["before"])
        self.assertEqual(result["verification"]["state"], "read_back")
        self.assertTrue(result["verification"]["final_read"])
        self.assertEqual(result["recovery"]["semantics"], "eventkit_native_api")
        self.assertFalse(result["recovery"]["automatic_retry_safe"])
        self.assertEqual(result["warnings"][0]["code"], "native_receipt_preserved")
        self.assertNotIn("calendar_id", repr(result))

    def test_change_final_read_must_match_the_requested_action(self) -> None:
        eventkit = FakeEventKit()
        before = native_reminder()
        committed = native_reminder(
            title="Requested title",
            last_modified="2026-08-25T02:00:00.000Z",
        )
        conflicting = native_reminder(
            title="Conflicting title",
            last_modified="2026-08-25T03:00:00.000Z",
        )
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue(
            "update_reminder",
            mutation_receipt("update_reminder", before, committed),
            mutation=True,
        )
        eventkit.queue("read_reminder", read_receipt(conflicting))
        subject = facade(eventkit)
        reference = subject.read_reminder({"reminder_id": "REMINDER-1"})["data"][
            "reminder"
        ]["reference"]

        result = subject.change_reminder(
            {
                "reference": reference,
                "action": {
                    "kind": "patch",
                    "patch": {"title": "Requested title"},
                },
            }
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertIsNone(result["verification"]["matched"])
        self.assertFalse(result["verification"]["final_read"])
        self.assertEqual(result["error"]["reason_code"], "final_state_mismatch")
        self.assertNotIn("rev1.", repr(result))
        self.assertFalse(result["recovery"]["automatic_retry_safe"])

    def test_alarm_integrity_mismatch_never_becomes_verified_or_writable(
        self,
    ) -> None:
        alarms = {
            "absolute": {
                "kind": "absolute",
                "date_time": "2027-08-17T00:00:00.000Z",
            },
            "location": {
                "kind": "location",
                "proximity": "enter",
                "location": {
                    "title": "Office",
                    "latitude": 37.5,
                    "longitude": 127.0,
                    "radius_meters": 100.0,
                },
            },
            "writable_relative": {
                "kind": "relative",
                "offset_seconds": -900,
            },
            "read_only": {
                "kind": "absolute",
                "date_time": "2027-08-17T00:00:00.000Z",
                "read_only": True,
                "action": {"type": "procedure", "url": "example:before"},
            },
        }
        actions = (
            (
                "title_patch",
                "update_reminder",
                False,
                {"kind": "patch", "patch": {"title": "Changed title"}},
                {"title": "Changed title"},
            ),
            (
                "completion",
                "complete_reminder",
                False,
                {"kind": "set_completion", "completed": True},
                {"completed": True},
            ),
            (
                "reopen",
                "reopen_reminder",
                True,
                {"kind": "set_completion", "completed": False},
                {"completed": False},
            ),
            (
                "move",
                "move_reminder",
                False,
                {"kind": "move_to_list", "list_id": "LIST-2"},
                {"calendar_id": "LIST-2", "calendar_title": "Work"},
            ),
        )

        for alarm_name, alarm in alarms.items():
            for action_name, operation, completed, action, delta in actions:
                with self.subTest(alarm=alarm_name, action=action_name):
                    before = native_reminder(completed=completed)
                    before["due"] = {
                        "kind": "all_day",
                        "date": "2027-08-31",
                    }
                    before["alarms"] = [copy.deepcopy(alarm)]
                    before["recurrence_rules"] = (
                        [] if action_name == "completion"
                        else [{"frequency": "weekly", "interval": 1}]
                    )
                    committed = {
                        **copy.deepcopy(before),
                        **copy.deepcopy(delta),
                        "last_modified": "2026-08-25T02:00:00.000Z",
                    }
                    final = {
                        **copy.deepcopy(committed),
                        "alarms": [],
                        "last_modified": "2026-08-25T03:00:00.000Z",
                    }
                    eventkit = FakeEventKit()
                    eventkit.queue("read_reminder", read_receipt(before))
                    eventkit.queue("read_reminder", read_receipt(before))
                    eventkit.queue(
                        operation,
                        mutation_receipt(operation, before, committed),
                        mutation=True,
                    )
                    eventkit.queue("read_reminder", read_receipt(final))
                    subject = facade(eventkit)
                    reference = subject.read_reminder(
                        {"reminder_id": "REMINDER-1"}
                    )["data"]["reminder"]["reference"]

                    result = subject.change_reminder(
                        {"reference": reference, "action": action}
                    )

                    self.assertEqual(
                        result["status"],
                        "committed_verification_pending",
                    )
                    self.assertEqual(
                        result["error"]["reason_code"],
                        "final_state_mismatch",
                    )
                    self.assertNotIn("rev1.", repr(result))

    def test_relative_alarm_dependencies_must_match_the_public_final_read(self) -> None:
        before = native_reminder()
        before["due"] = {"kind": "all_day", "date": "2027-08-31"}
        before["alarms"] = [
            {"kind": "relative", "offset_seconds": -1_209_600}
        ]

        due_after = copy.deepcopy(before)
        due_after["due"] = {"kind": "all_day", "date": "2027-09-30"}
        due_drift = copy.deepcopy(due_after)
        due_drift["alarms"] = []

        alarm_after = copy.deepcopy(before)
        alarm_after["alarms"] = [
            {"kind": "relative", "offset_seconds": -604_800}
        ]
        alarm_drift = copy.deepcopy(alarm_after)
        alarm_drift["due"] = {"kind": "all_day", "date": "2027-09-01"}

        move_after = copy.deepcopy(before)
        move_after["calendar_id"] = "LIST-2"
        move_after["calendar_title"] = "Work"
        move_drift = copy.deepcopy(move_after)
        move_drift["alarms"] = [
            {
                "kind": "absolute",
                "date_time": "2027-08-17T00:00:00.000Z",
            }
        ]

        cases = (
            (
                "due-only patch",
                "update_reminder",
                {"kind": "patch", "patch": {"due": due_after["due"]}},
                due_after,
                due_drift,
            ),
            (
                "alarm-only patch",
                "update_reminder",
                {"kind": "patch", "patch": {"alarms": alarm_after["alarms"]}},
                alarm_after,
                alarm_drift,
            ),
            (
                "list move",
                "move_reminder",
                {"kind": "move_to_list", "list_id": "LIST-2"},
                move_after,
                move_drift,
            ),
        )

        for label, operation, action, committed, final in cases:
            with self.subTest(label=label):
                eventkit = FakeEventKit()
                eventkit.queue("read_reminder", read_receipt(before))
                eventkit.queue("read_reminder", read_receipt(before))
                eventkit.queue(
                    operation,
                    mutation_receipt(operation, before, committed),
                    mutation=True,
                )
                eventkit.queue("read_reminder", read_receipt(final))
                subject = facade(eventkit)
                reference = subject.read_reminder(
                    {"reminder_id": "REMINDER-1"}
                )["data"]["reminder"]["reference"]

                result = subject.change_reminder(
                    {"reference": reference, "action": action}
                )

                self.assertEqual(result["status"], "committed_verification_pending")
                self.assertIsNone(result["verification"]["matched"])
                self.assertFalse(result["verification"]["final_read"])
                self.assertEqual(
                    result["error"]["reason_code"], "final_state_mismatch"
                )
                self.assertNotIn("rev1.", repr(result))

    def test_unchanged_receipt_with_mismatched_final_state_preserves_uncertainty(self) -> None:
        eventkit = FakeEventKit()
        before = native_reminder()
        unchanged = mutation_receipt("update_reminder", before, before)
        unchanged["status"] = "unchanged"
        unchanged["verification"]["write_performed"] = False
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue("update_reminder", unchanged, mutation=True)
        eventkit.queue(
            "read_reminder",
            read_receipt(native_reminder(title="Conflicting title")),
        )
        subject = facade(eventkit)
        reference = subject.read_reminder({"reminder_id": "REMINDER-1"})["data"][
            "reminder"
        ]["reference"]

        result = subject.change_reminder(
            {
                "reference": reference,
                "action": {"kind": "patch", "patch": {"title": "Requested title"}},
            }
        )

        self.assertEqual(result["status"], "committed_verification_pending")
        self.assertIsNone(result["verification"]["write_performed"])
        self.assertFalse(result["verification"]["final_read"])
        self.assertIsNone(result["verification"]["matched"])
        self.assertNotIn("rev1.", repr(result))
        validate_public_result("change_reminder", result, "unknown")

    def test_change_projection_strips_private_url_attachment_fields(self) -> None:
        result = _change_reminder(
            {
                "id": "REMINDER-1",
                "url_attachment": {
                    "id": "ATTACHMENT-URL-1",
                    "type": "url",
                    "url": "https://example.com/item",
                    "pk": 99,
                    "database": "/private/store.sqlite",
                },
            }
        )

        self.assertEqual(
            result["url_attachment"],
            {
                "id": "ATTACHMENT-URL-1",
                "type": "url",
                "url": "https://example.com/item",
            },
        )
        self.assertNotIn("/private/store.sqlite", repr(result))

    def test_change_move_translates_public_list_id_only_at_the_eventkit_seam(self) -> None:
        eventkit = FakeEventKit()
        before = native_reminder()
        after = native_reminder(
            calendar_id="LIST-2",
            calendar_title="Work",
            last_modified="2026-08-25T02:00:00.000Z",
        )
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue(
            "move_reminder",
            mutation_receipt("move_reminder", before, after),
            mutation=True,
        )
        eventkit.queue("read_reminder", read_receipt(after))
        subject = facade(eventkit)
        reference = subject.read_reminder({"reminder_id": "REMINDER-1"})["data"][
            "reminder"
        ]["reference"]

        result = subject.change_reminder(
            {
                "reference": reference,
                "action": {"kind": "move_to_list", "list_id": "LIST-2"},
            }
        )

        self.assertEqual(eventkit.calls[2][0], "move_reminder")
        self.assertEqual(eventkit.calls[2][1]["calendar_id"], "LIST-2")
        self.assertNotIn("list_id", eventkit.calls[2][1])
        self.assertEqual(result["operation"], "change_reminder.move_to_list")
        self.assertEqual(result["after"]["list_id"], "LIST-2")

    def test_recurring_completion_returns_actionable_no_write_in_both_modes(self) -> None:
        for experimental in (False, True):
            with self.subTest(experimental=experimental):
                eventkit = FakeEventKit()
                before = {
                    **native_reminder(),
                    "due": {"kind": "all_day", "date": "2026-09-08"},
                    "recurrence_rules": [{"frequency": "daily", "interval": 1}],
                }
                eventkit.queue("read_reminder", read_receipt(before))
                eventkit.queue("read_reminder", read_receipt(before))
                subject = facade(eventkit, enable_experimental=experimental)
                reference = subject.read_reminder({"reminder_id": before["id"]})["data"]["reminder"]["reference"]

                result, state = subject.call_with_state(
                    "change_reminder",
                    {"reference": reference, "action": {"kind": "set_completion", "completed": True}},
                )

                self.assertEqual(result["status"], "failed_no_mutation")
                self.assertEqual(state, "not_mutated")
                self.assertEqual(result["error"]["code"], "unsupported_capability")
                self.assertEqual(result["error"]["reason_code"], "unsupported_recurring_completion")
                self.assertIn("Reminders app", result["error"]["message"])
                self.assertFalse(result["error"]["retryable"])
                self.assertFalse(result["recovery"]["automatic_retry_safe"])
                self.assertNotIn("rev1.", repr(result))
                self.assertFalse(any(mutation for _, _, mutation in eventkit.calls))
                validate_public_result("change_reminder", result, state)

    def test_completed_historical_occurrence_reopens_as_that_exact_item(self) -> None:
        eventkit = FakeEventKit()
        before = {
            **native_reminder(reminder_id="COMPLETED-OCCURRENCE", completed=True),
            "due": {"kind": "all_day", "date": "2026-09-07"},
            "recurrence_rules": [],
        }
        after = {**before, "completed": False, "last_modified": "2026-09-05T09:00:00.000Z"}
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue("reopen_reminder", mutation_receipt("reopen_reminder", before, after), mutation=True)
        eventkit.queue("read_reminder", read_receipt(after))
        subject = facade(eventkit)
        reference = subject.read_reminder({"reminder_id": before["id"]})["data"]["reminder"]["reference"]

        result, state = subject.call_with_state(
            "change_reminder", {"reference": reference, "action": {"kind": "set_completion", "completed": False}}
        )

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["after"]["id"], "COMPLETED-OCCURRENCE")
        self.assertEqual(result["after"]["due"], before["due"])
        self.assertEqual(result["after"]["recurrence_rules"], [])
        self.assertEqual([name for name, _, mutation in eventkit.calls if mutation], ["reopen_reminder"])
        validate_public_result("change_reminder", result, state)

    def test_known_concurrent_change_returns_a_structured_fresh_read_failure(self) -> None:
        eventkit = FakeEventKit()
        before = native_reminder()
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue(
            "update_reminder",
            {
                "schema_version": 1,
                "ok": False,
                "status": "failed_no_mutation",
                "operation": "update_reminder",
                "error": {
                    "code": "concurrent_modification",
                    "reason_code": "concurrent_modification",
                    "message": "Reminder changed after it was read",
                    "retryable": True,
                },
            },
            mutation=True,
            is_error=True,
        )
        subject = facade(eventkit)
        reference = subject.read_reminder({"reminder_id": "REMINDER-1"})["data"][
            "reminder"
        ]["reference"]

        result = subject.change_reminder(
            {
                "reference": reference,
                "action": {"kind": "patch", "patch": {"title": "Stale"}},
            }
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_no_mutation")
        self.assertEqual(result["error"]["code"], "concurrent_modification")
        # The opaque token is authoritative. A rejected/expired reference must
        # not be decoded or mirrored in a second facade-side token store.
        self.assertEqual(result["target"], {})
        self.assertEqual(result["next_action"]["kind"], "fresh_read")
        self.assertIsNone(result["before"])
        self.assertIsNone(result["after"])

    def test_stale_alarm_reference_is_consumed_before_semantic_preflight(self) -> None:
        cached_alarms = (
            (
                "read_only",
                {
                    "kind": "absolute",
                    "date_time": "2027-08-17T00:00:00.000Z",
                    "read_only": True,
                    "action": {"type": "procedure", "url": "example:before"},
                },
                {
                    "kind": "patch",
                    "patch": {
                        "alarms": [
                            {
                                "kind": "absolute",
                                "date_time": "2027-08-18T00:00:00.000Z",
                            }
                        ]
                    },
                },
            ),
            (
                "relative",
                {"kind": "relative", "offset_seconds": -900},
                {"kind": "patch", "patch": {"due": None}},
            ),
        )

        for label, alarm, action in cached_alarms:
            with self.subTest(alarm=label):
                before = native_reminder()
                before["due"] = {"kind": "all_day", "date": "2027-08-31"}
                before["alarms"] = [alarm]
                current = native_reminder(
                    last_modified="2026-08-25T02:00:00.000Z"
                )
                eventkit = FakeEventKit()
                eventkit.queue("read_reminder", read_receipt(before))
                eventkit.queue("read_reminder", read_receipt(current))
                subject = facade(eventkit)
                reference = subject.read_reminder(
                    {"reminder_id": "REMINDER-1"}
                )["data"]["reminder"]["reference"]

                result = subject.change_reminder(
                    {"reference": reference, "action": action}
                )
                replay = subject.change_reminder(
                    {
                        "reference": reference,
                        "action": {
                            "kind": "patch",
                            "patch": {"title": "No replay"},
                        },
                    }
                )

                self.assertEqual(result["status"], "failed_no_mutation")
                self.assertEqual(
                    result["error"]["reason_code"],
                    "concurrent_modification",
                )
                self.assertEqual(
                    replay["error"]["reason_code"],
                    "invalid_reference",
                )
                self.assertEqual(
                    [call[0] for call in eventkit.calls],
                    ["read_reminder", "read_reminder"],
                )

    def test_change_revalidation_failure_is_no_write_and_consumes_reference(
        self,
    ) -> None:
        eventkit = FakeEventKit()
        eventkit.queue("read_reminder", read_receipt(native_reminder()))
        subject = facade(eventkit)
        reference = subject.read_reminder({"reminder_id": "REMINDER-1"})[
            "data"
        ]["reminder"]["reference"]
        eventkit.fail("read_reminder", RuntimeError("fresh read unavailable"))

        result = subject.change_reminder(
            {
                "reference": reference,
                "action": {"kind": "patch", "patch": {"title": "No dispatch"}},
            }
        )
        replay = subject.change_reminder(
            {
                "reference": reference,
                "action": {"kind": "patch", "patch": {"title": "No replay"}},
            }
        )

        self.assertEqual(result["status"], "failed_no_mutation")
        self.assertEqual(result["error"]["code"], "sync_pending")
        self.assertEqual(
            result["error"]["reason_code"],
            "reference_revalidation_failed",
        )
        self.assertEqual(replay["error"]["reason_code"], "invalid_reference")
        self.assertEqual(
            [call[0] for call in eventkit.calls],
            ["read_reminder", "read_reminder"],
        )

    def test_call_with_state_does_not_infer_no_write_from_public_failure(self) -> None:
        for raw_state in ("committed", "unknown"):
            with self.subTest(raw_state=raw_state):
                eventkit = FakeEventKit()
                eventkit.queue(
                    "create_reminder",
                    {
                        "schema_version": 1,
                        "ok": False,
                        "status": "failed_no_mutation",
                        "operation": "create_reminder",
                        "operation_id": "12345678-1234-4234-9234-1234567890ab",
                        "backend": "eventkit_public_sdk",
                        "target": {"calendar_id": "LIST-1"},
                        "before": None,
                        "after": None,
                        "verification": {
                            "state": "not_needed",
                            "write_performed": False,
                            "final_read": False,
                        },
                        "recovery": {
                            "semantics": "fresh_read_required",
                            "automatic_retry_safe": False,
                        },
                        "error": {
                            "code": "permission_denied",
                            "reason_code": "synthetic_public_projection",
                            "message": "The public projection claims no write.",
                            "retryable": False,
                        },
                    },
                    mutation=True,
                    is_error=True,
                    mutation_state=raw_state,
                )

                payload, state = facade(eventkit).call_with_state(
                    "create_reminder",
                    {
                        "list_id": "LIST-1",
                        "title": "State channel",
                        "idempotency_key": f"state-conflict-{raw_state}",
                    },
                )

                self.assertEqual(
                    payload["status"],
                    "committed_verification_pending",
                )
                self.assertIs(
                    payload["verification"]["write_performed"],
                    True if raw_state == "committed" else None,
                )
                self.assertEqual(state, raw_state)
                validate_public_result("create_reminder", payload, state)

    def test_change_contract_failure_preserves_raw_committed_state(self) -> None:
        eventkit = FakeEventKit()
        before = native_reminder()
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue(
            "update_reminder",
            {
                "schema_version": 1,
                "ok": False,
                "status": "failed_no_mutation",
                "operation": "update_reminder",
                "operation_id": "12345678-1234-4234-9234-1234567890ab",
                "backend": "eventkit_public_sdk",
                "target": {"id": "REMINDER-1"},
                "before": before,
                "after": None,
                "verification": {
                    "state": "not_needed",
                    "write_performed": False,
                    "final_read": False,
                },
                "recovery": {
                    "semantics": "fresh_read_required",
                    "automatic_retry_safe": False,
                },
                "error": {
                    "code": "permission_denied",
                    "reason_code": "synthetic_public_projection",
                    "message": "Synthetic projection conflict.",
                    "retryable": False,
                },
            },
            mutation=True,
            is_error=True,
            mutation_state="committed",
        )
        subject = facade(eventkit)
        reference = subject.read_reminder({"reminder_id": "REMINDER-1"})["data"][
            "reminder"
        ]["reference"]

        payload, state = subject.call_with_state(
            "change_reminder",
            {
                "reference": reference,
                "action": {"kind": "patch", "patch": {"title": "Changed"}},
            },
        )

        self.assertEqual(payload["status"], "committed_verification_pending")
        self.assertEqual(state, "committed")

    def test_concurrent_projection_cannot_erase_raw_change_state(self) -> None:
        for raw_state in ("committed", "unknown"):
            with self.subTest(raw_state=raw_state):
                eventkit = FakeEventKit()
                before = native_reminder()
                eventkit.queue("read_reminder", read_receipt(before))
                eventkit.queue("read_reminder", read_receipt(before))
                eventkit.queue(
                    "update_reminder",
                    {
                        "schema_version": 1,
                        "ok": False,
                        "status": "failed_no_mutation",
                        "operation": "update_reminder",
                        "operation_id": "12345678-1234-4234-9234-1234567890ab",
                        "backend": "eventkit_public_sdk",
                        "target": {"id": "REMINDER-1"},
                        "before": before,
                        "after": None,
                        "verification": {
                            "state": "not_needed",
                            "write_performed": False,
                            "final_read": False,
                        },
                        "recovery": {
                            "semantics": "fresh_read_required",
                            "automatic_retry_safe": False,
                        },
                        "error": {
                            "code": "concurrent_modification",
                            "reason_code": "synthetic_concurrent_projection",
                            "message": "The public projection claims no write.",
                            "retryable": True,
                        },
                    },
                    mutation=True,
                    is_error=True,
                    mutation_state=raw_state,
                )
                subject = facade(eventkit)
                reference = subject.read_reminder(
                    {"reminder_id": "REMINDER-1"}
                )["data"]["reminder"]["reference"]

                payload, state = subject.call_with_state(
                    "change_reminder",
                    {
                        "reference": reference,
                        "action": {
                            "kind": "patch",
                            "patch": {"title": "Changed"},
                        },
                    },
                )

                self.assertEqual(
                    payload["status"],
                    "committed_verification_pending",
                )
                self.assertEqual(state, raw_state)
                self.assertNotEqual(
                    payload.get("error", {}).get("code"),
                    "concurrent_modification",
                )

    def test_change_and_delete_carry_success_state_from_eventkit(self) -> None:
        eventkit = FakeEventKit()
        before = native_reminder()
        changed = native_reminder(
            title="Changed",
            last_modified="2026-08-25T01:00:01.000Z",
        )
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue("read_reminder", read_receipt(before))
        eventkit.queue(
            "update_reminder",
            mutation_receipt("update_reminder", before, changed),
            mutation=True,
            mutation_state="committed",
        )
        eventkit.queue("read_reminder", read_receipt(changed))
        eventkit.queue("read_reminder", read_receipt(changed))
        subject = facade(eventkit)
        reference = subject.read_reminder({"reminder_id": "REMINDER-1"})["data"][
            "reminder"
        ]["reference"]

        _, change_state = subject.call_with_state(
            "change_reminder",
            {
                "reference": reference,
                "action": {"kind": "patch", "patch": {"title": "Changed"}},
            },
        )

        eventkit.queue("read_reminder", read_receipt(changed))
        delete_receipt = mutation_receipt("delete_reminder", changed, changed)
        delete_receipt["verification"]["local_absence"] = True
        eventkit.queue(
            "delete_reminder",
            delete_receipt,
            mutation=True,
            mutation_state="committed",
        )
        delete_reference = subject.read_reminder({"reminder_id": "REMINDER-1"})[
            "data"
        ]["reminder"]["reference"]
        _, delete_state = subject.call_with_state(
            "delete_reminder",
            {"reference": delete_reference},
        )

        self.assertEqual(change_state, "committed")
        self.assertEqual(delete_state, "committed")

    def test_dispatched_create_exception_reports_unknown_state(self) -> None:
        eventkit = FakeEventKit()
        eventkit.fail(
            "create_reminder",
            OSError("helper connection dropped"),
            mutation=True,
        )

        payload, state = facade(eventkit).call_with_state(
            "create_reminder",
            {
                "list_id": "LIST-1",
                "title": "Unknown outcome",
                "idempotency_key": "unknown-create-state",
            },
        )

        self.assertEqual(payload["status"], "committed_verification_pending")
        self.assertEqual(state, "unknown")

    def test_call_with_state_reports_reads_and_pre_dispatch_failures(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue(
            "request_access",
            {
                "schema_version": 1,
                "ok": True,
                "status": "verified",
                "operation": "request_access",
                "data": {
                    "authorization_before": "full_access",
                    "authorization": "full_access",
                    "request_attempted": False,
                    "prompt_expected": False,
                    "prompt_observed": False,
                    "prompted_explicitly": True,
                },
            },
        )
        subject = facade(eventkit)

        invalid, invalid_state = subject.call_with_state("create_reminder", {})
        _, read_state = subject.call_with_state("request_reminders_access", {})

        self.assertEqual(invalid["status"], "failed_no_mutation")
        self.assertEqual(invalid_state, "not_mutated")
        self.assertIsNone(read_state)


if __name__ == "__main__":
    unittest.main()
