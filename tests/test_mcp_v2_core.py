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

from mcp.v2_core import EventKitReply, V2CoreFacade
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
    ) -> None:
        self._replies[(operation, mutation)].append(
            EventKitReply(payload=copy.deepcopy(dict(payload)), is_error=is_error)
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


def facade(eventkit: FakeEventKit) -> V2CoreFacade:
    return V2CoreFacade(
        eventkit,
        token_source=DeterministicTokens(),
        operation_id_source=DeterministicOperationIDs(),
        reference_ttl_seconds=30.0,
    )


class V2CoreFacadeTests(unittest.TestCase):
    def test_missing_native_build_prerequisite_has_an_actionable_recovery_path(self) -> None:
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
                    "message": "EventKit helper could not be prepared (RuntimeError)",
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
        self.assertIn("xcode-select --install", result["next_action"]["message"])
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
        self.assertEqual(len(eventkit.calls), 2)

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

    def test_ensure_list_uses_exact_source_and_replays_idempotently(self) -> None:
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
        arguments = {
            "source_id": "SOURCE-1",
            "name": "  Work  ",
            "idempotency_key": "ensure:work:1",
        }

        first = subject.ensure_reminder_list(arguments)
        second = subject.ensure_reminder_list(arguments)

        self.assertEqual(
            eventkit.calls,
            [
                (
                    "ensure_reminder_list",
                    {"source_id": "SOURCE-1", "name": "Work"},
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
        self.assertTrue(second["replayed"])
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(len(first["idempotency_key_hash"]), 64)
        validate_public_result("ensure_reminder_list", first, "committed")
        validate_public_result("ensure_reminder_list", second, "committed")

    def test_ensure_list_idempotency_key_rejects_different_input(self) -> None:
        eventkit = FakeEventKit()
        eventkit.queue(
            "ensure_reminder_list",
            {
                "ok": True,
                "status": "verified",
                "operation": "ensure_reminder_list",
                "operation_id": "12345678-1234-4234-9234-1234567890ab",
                "backend": "eventkit_public_sdk",
                "target": {"source_id": "SOURCE-1", "list_id": "LIST-1"},
                "before": None,
                "after": {
                    "id": "LIST-1",
                    "title": "Work",
                    "type": "caldav",
                    "allows_content_modifications": True,
                    "subscribed": False,
                    "immutable": False,
                    "source": {"id": "SOURCE-1", "type": "caldav"},
                },
                "verification": {"state": "read_back", "write_performed": True},
                "recovery": {"semantics": "delete_list_in_reminders"},
            },
            mutation=True,
        )
        subject = facade(eventkit)
        subject.ensure_reminder_list(
            {"source_id": "SOURCE-1", "name": "Work", "idempotency_key": "same:key"}
        )

        result = subject.ensure_reminder_list(
            {"source_id": "SOURCE-1", "name": "Home", "idempotency_key": "same:key"}
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_no_mutation")
        self.assertEqual(result["error"]["reason_code"], "idempotency_key_conflict")
        self.assertEqual(len(eventkit.calls), 1)

    def test_ensure_list_dispatch_exception_is_pending_and_replays_without_redispatch(self) -> None:
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
        second = subject.ensure_reminder_list(arguments)

        self.assertEqual(len(eventkit.calls), 1)
        self.assertEqual(first["status"], "committed_verification_pending")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(first["error"]["code"], "sync_pending")
        self.assertFalse(first["error"]["retryable"])
        self.assertEqual(first["warnings"][0]["code"], "verification_pending")
        self.assertEqual(first["next_action"]["tool"], "list_reminder_lists")
        self.assertFalse(first["next_action"]["retry_original_once"])
        validate_public_result("ensure_reminder_list", first, "unknown")
        validate_public_result("ensure_reminder_list", second, "unknown")

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
        created = native_reminder(title="Buy oat milk")
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

        result = facade(eventkit).create_reminder(
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

    def test_change_uses_core_reference_guard_and_returns_the_final_exact_read(self) -> None:
        eventkit = FakeEventKit()
        before = native_reminder()
        committed = native_reminder(
            title="Ship public beta",
            last_modified="2026-08-25T02:00:00.000Z",
        )
        final = {**committed, "notes": "Verified by a final exact read"}
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
            eventkit.calls[1],
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
            eventkit.calls[2],
            ("read_reminder", {"reminder_id": "REMINDER-1"}, False),
        )
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["operation"], "change_reminder.patch")
        self.assertEqual(result["target"], {"reminder_id": "REMINDER-1", "list_id": "LIST-1"})
        self.assertEqual(result["after"]["title"], "Ship public beta")
        self.assertEqual(result["after"]["notes"], "Verified by a final exact read")
        self.assertEqual(result["after"]["reference"], "rev1." + "B" * 32)
        self.assertNotIn("reference", result["before"])
        self.assertEqual(result["verification"]["state"], "read_back")
        self.assertTrue(result["verification"]["final_read"])
        self.assertEqual(result["recovery"]["semantics"], "eventkit_native_api")
        self.assertFalse(result["recovery"]["automatic_retry_safe"])
        self.assertEqual(result["warnings"][0]["code"], "native_receipt_preserved")
        self.assertNotIn("calendar_id", repr(result))

    def test_change_move_translates_public_list_id_only_at_the_eventkit_seam(self) -> None:
        eventkit = FakeEventKit()
        before = native_reminder()
        after = native_reminder(
            calendar_id="LIST-2",
            calendar_title="Work",
            last_modified="2026-08-25T02:00:00.000Z",
        )
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

        self.assertEqual(eventkit.calls[1][0], "move_reminder")
        self.assertEqual(eventkit.calls[1][1]["calendar_id"], "LIST-2")
        self.assertNotIn("list_id", eventkit.calls[1][1])
        self.assertEqual(result["operation"], "change_reminder.move_to_list")
        self.assertEqual(result["after"]["list_id"], "LIST-2")

    def test_known_concurrent_change_returns_a_structured_fresh_read_failure(self) -> None:
        eventkit = FakeEventKit()
        before = native_reminder()
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


if __name__ == "__main__":
    unittest.main()
