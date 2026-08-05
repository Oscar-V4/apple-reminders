from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "scripts" / "eventkit_bridge.py"
SCHEMA_PATH = ROOT / "scripts" / "eventkit_bridge_schema.json"
SPEC = importlib.util.spec_from_file_location("eventkit_bridge", BRIDGE_PATH)
assert SPEC and SPEC.loader
eventkit_bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eventkit_bridge)


class EventKitRequestValidationTests(unittest.TestCase):
    def test_timed_due_keeps_named_zone_and_canonical_offset(self) -> None:
        normalized = eventkit_bridge.normalize_request(
            {
                "schema_version": 1,
                "operation": "create_reminder",
                "calendar_id": "CALENDAR-1",
                "title": "Call dentist",
                "due": {
                    "kind": "timed",
                    "date_time": "2026-08-05T14:30:00+09:00",
                    "time_zone": "Asia/Seoul",
                },
            }
        )

        self.assertEqual(normalized["due"]["kind"], "timed")
        self.assertEqual(normalized["due"]["time_zone"], "Asia/Seoul")
        self.assertEqual(normalized["due"]["date_time"], "2026-08-05T14:30:00.000+09:00")

    def test_timed_due_rejects_offset_that_disagrees_with_named_zone(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "create_reminder",
                    "calendar_id": "CALENDAR-1",
                    "title": "Call dentist",
                    "due": {
                        "kind": "timed",
                        "date_time": "2026-08-05T14:30:00-04:00",
                        "time_zone": "Asia/Seoul",
                    },
                }
            )

        self.assertEqual(raised.exception.code, "time_zone_offset_mismatch")

    def test_all_day_due_is_distinct_from_timed_due(self) -> None:
        normalized = eventkit_bridge.normalize_request(
            {
                "schema_version": 1,
                "operation": "create_reminder",
                "calendar_id": "CALENDAR-1",
                "title": "Renew membership",
                "due": {"kind": "all_day", "date": "2026-08-31"},
            }
        )

        self.assertEqual(normalized["due"], {"kind": "all_day", "date": "2026-08-31"})

    def test_absolute_alarm_normalizes_to_utc(self) -> None:
        normalized = eventkit_bridge.normalize_request(
            {
                "schema_version": 1,
                "operation": "create_reminder",
                "calendar_id": "CALENDAR-1",
                "title": "Leave home",
                "alarms": [
                    {"kind": "absolute", "date_time": "2026-08-05T08:00:00+09:00"}
                ],
            }
        )

        self.assertEqual(normalized["alarms"][0]["date_time"], "2026-08-04T23:00:00.000Z")

    def test_relative_alarm_fails_clearly_instead_of_guessing_anchor(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "create_reminder",
                    "calendar_id": "CALENDAR-1",
                    "title": "Leave home",
                    "alarms": [{"kind": "relative", "offset_seconds": -900}],
                }
            )

        self.assertEqual(raised.exception.code, "unsupported_relative_alarm")
        self.assertEqual(raised.exception.status, "unsupported")

    def test_location_alarm_requires_coordinates_and_typed_proximity(self) -> None:
        normalized = eventkit_bridge.normalize_request(
            {
                "schema_version": 1,
                "operation": "create_reminder",
                "calendar_id": "CALENDAR-1",
                "title": "Buy milk",
                "alarms": [
                    {
                        "kind": "location",
                        "proximity": "enter",
                        "location": {
                            "title": "Market",
                            "latitude": 37.5665,
                            "longitude": 126.978,
                            "radius_meters": 150,
                        },
                    }
                ],
            }
        )

        alarm = normalized["alarms"][0]
        self.assertEqual(alarm["kind"], "location")
        self.assertEqual(alarm["proximity"], "enter")
        self.assertEqual(alarm["location"]["radius_meters"], 150.0)

    def test_recurrence_requires_due_on_create(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "create_reminder",
                    "calendar_id": "CALENDAR-1",
                    "title": "Weekly review",
                    "recurrence_rules": [{"frequency": "weekly", "interval": 1}],
                }
            )

        self.assertEqual(raised.exception.code, "recurrence_requires_due")

    def test_daily_recurrence_rejects_filters_eventkit_would_ignore(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_recurrence(
                {
                    "frequency": "daily",
                    "interval": 1,
                    "days_of_week": [{"day": "monday"}],
                },
                "$.recurrence_rules[0]",
            )

        self.assertEqual(raised.exception.code, "invalid_recurrence_combination")

    def test_weekly_recurrence_rejects_ordinal_weekday(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_recurrence(
                {
                    "frequency": "weekly",
                    "interval": 1,
                    "days_of_week": [{"day": "monday", "ordinal": 1}],
                },
                "$.recurrence_rules[0]",
            )

        self.assertEqual(raised.exception.code, "invalid_recurrence_combination")

    def test_recurrence_filter_must_be_omitted_instead_of_empty(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_recurrence(
                {"frequency": "monthly", "interval": 1, "days_of_month": []},
                "$.recurrence_rules[0]",
            )

        self.assertEqual(raised.exception.code, "empty_array")

    def test_fetch_requires_semantic_bound_in_addition_to_limit(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {"schema_version": 1, "operation": "fetch_reminders", "limit": 50}
            )

        self.assertEqual(raised.exception.code, "unbounded_read")

    def test_bounded_fetch_normalizes_pagination_defaults(self) -> None:
        normalized = eventkit_bridge.normalize_request(
            {
                "schema_version": 1,
                "operation": "fetch_reminders",
                "calendar_ids": ["CALENDAR-1"],
                "limit": 50,
            }
        )

        self.assertEqual(normalized["status"], "incomplete")
        self.assertEqual(normalized["offset"], 0)
        self.assertEqual(normalized["sort"], "due")

    def test_modified_after_only_cannot_bound_native_eventkit_fetch(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "fetch_reminders",
                    "modified_after": "2026-08-05T00:00:00Z",
                    "limit": 50,
                }
            )

        self.assertEqual(raised.exception.code, "unbounded_read")

    def test_completed_due_range_requires_calendar_or_completion_bound(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "fetch_reminders",
                    "status": "completed",
                    "due_start": "2026-08-01T00:00:00Z",
                    "due_end": "2026-08-08T00:00:00Z",
                    "limit": 50,
                }
            )

        self.assertEqual(raised.exception.code, "unbounded_read")

    def test_incomplete_due_range_is_a_native_eventkit_bound(self) -> None:
        normalized = eventkit_bridge.normalize_request(
            {
                "schema_version": 1,
                "operation": "fetch_reminders",
                "status": "incomplete",
                "due_start": "2026-08-01T00:00:00Z",
                "due_end": "2026-08-08T00:00:00Z",
                "limit": 50,
            }
        )

        self.assertEqual(normalized["status"], "incomplete")

    def test_existing_item_write_requires_last_modified_precondition(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "update_reminder",
                    "reminder_id": "REMINDER-1",
                    "patch": {"title": "Changed"},
                }
            )

        self.assertEqual(raised.exception.code, "missing_fields")

    def test_update_preserves_explicit_null_clear_in_patch(self) -> None:
        normalized = eventkit_bridge.normalize_request(
            {
                "schema_version": 1,
                "operation": "update_reminder",
                "reminder_id": "REMINDER-1",
                "expected_last_modified": "2026-08-05T00:00:00Z",
                "patch": {"due": None, "alarms": None},
            }
        )

        self.assertIn("due", normalized["patch"])
        self.assertIsNone(normalized["patch"]["due"])
        self.assertIn("alarms", normalized["patch"])
        self.assertIsNone(normalized["patch"]["alarms"])

    def test_unknown_patch_field_is_rejected(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "update_reminder",
                    "reminder_id": "REMINDER-1",
                    "expected_last_modified": None,
                    "patch": {"flagged": True},
                }
            )

        self.assertEqual(raised.exception.code, "unknown_fields")


class EventKitContractTests(unittest.TestCase):
    @staticmethod
    def mutation_fixture(operation: str, status: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "ok": status != "failed_no_mutation",
            "status": status,
            "operation": operation,
            "operation_id": "B6A66F8E-B44F-426F-95CD-3A678865F793",
            "backend": "eventkit_public_sdk",
            "target": {"id": "REMINDER-1", "calendar_id": "CALENDAR-1"},
            "after": {"id": "REMINDER-1", "title": "After"},
            "verification": {
                "state": "pending" if status == "committed_verification_pending" else "read_back",
                "matched": status != "committed_verification_pending",
            },
            "recovery": {"semantics": "eventkit_native_api"},
        }
        if operation != "create_reminder":
            payload["before"] = {"id": "REMINDER-1", "title": "Before"}
        if status == "committed_verification_pending":
            payload["warnings"] = [
                {"code": "verification_pending", "message": "Read back before retrying."}
            ]
            payload["error"] = {
                "code": "sync_pending",
                "reason_code": "committed_verification_mismatch",
                "message": "Committed; verification pending",
            }
        return payload

    def test_schema_declares_only_core_receipt_statuses(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        self.assertEqual(
            set(schema["x-response-statuses"]),
            {
                "unchanged",
                "verified",
                "committed_verification_pending",
                "partial_success",
                "failed_no_mutation",
            },
        )

        self.assertEqual(set(schema["x-error-codes"]), eventkit_bridge.STABLE_ERROR_CODES)

    def test_validation_error_uses_failed_no_mutation_and_stable_code(self) -> None:
        error = eventkit_bridge.BridgeValidationError(
            "unbounded_read", "A semantic bound is required"
        )

        payload = eventkit_bridge.validation_error_response("fetch_reminders", error)

        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "ambiguous_scope")
        self.assertEqual(payload["error"]["reason_code"], "unbounded_read")
        self.assertEqual(payload["error"]["message"], "A semantic bound is required")

    def test_committed_verification_pending_is_nonterminal_success_like_core_receipt(self) -> None:
        payload = eventkit_bridge.response(
            "update_reminder",
            "committed_verification_pending",
            data={"verification": {"state": "pending"}},
        )

        self.assertTrue(payload["ok"])

    def test_verified_create_fixture_satisfies_full_mutation_receipt(self) -> None:
        payload = self.mutation_fixture("create_reminder", "verified")

        self.assertIs(eventkit_bridge.validate_mutation_receipt(payload), payload)
        self.assertNotIn("before", payload)

    def test_unchanged_update_fixture_satisfies_full_mutation_receipt(self) -> None:
        payload = self.mutation_fixture("update_reminder", "unchanged")
        payload["verification"] = {
            "state": "not_needed",
            "matched": True,
            "write_performed": False,
        }
        payload["after"] = payload["before"]

        self.assertIs(eventkit_bridge.validate_mutation_receipt(payload), payload)

    def test_pending_move_fixture_satisfies_full_mutation_receipt(self) -> None:
        payload = self.mutation_fixture("move_reminder", "committed_verification_pending")

        self.assertIs(eventkit_bridge.validate_mutation_receipt(payload), payload)
        self.assertEqual(payload["error"]["code"], "sync_pending")

    def test_mutation_fixture_without_backend_is_rejected(self) -> None:
        payload = self.mutation_fixture("complete_reminder", "verified")
        del payload["backend"]

        with self.assertRaisesRegex(RuntimeError, "backend"):
            eventkit_bridge.validate_mutation_receipt(payload)

    def test_validate_only_cli_does_not_compile_or_access_eventkit(self) -> None:
        request = {
            "schema_version": 1,
            "operation": "read_reminder",
            "reminder_id": "REMINDER-1",
        }

        result = subprocess.run(
            [sys.executable, str(BRIDGE_PATH), "--validate-only"],
            input=json.dumps(request),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        payload = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "verified")
        self.assertTrue(payload["data"]["validation_only"])
        self.assertEqual(payload["data"]["normalized_request"], request)

    @unittest.skipUnless(sys.platform == "darwin", "Objective-C EventKit helper requires macOS")
    def test_native_helper_compiles_and_static_operations_do_not_read_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = eventkit_bridge.build_helper(Path(directory))
            self.assertTrue(binary.is_file())
            signature = subprocess.run(
                ["codesign", "-d", "--verbose=2", str(binary)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            self.assertEqual(signature.returncode, 0)
            self.assertIn(
                f"Identifier={eventkit_bridge.HELPER_BUNDLE_IDENTIFIER}", signature.stdout
            )
            for operation in ("schema", "capabilities"):
                result = subprocess.run(
                    [str(binary)],
                    input=json.dumps({"schema_version": 1, "operation": operation}),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=10,
                )
                payload = json.loads(result.stdout)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(payload["operation"], operation)
                self.assertEqual(payload["status"], "verified")

    @unittest.skipUnless(sys.platform == "darwin", "Objective-C EventKit helper requires macOS")
    def test_native_helper_rejects_bad_schema_before_eventkit_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = eventkit_bridge.build_helper(Path(directory))
            result = subprocess.run(
                [str(binary)],
                input=json.dumps({"schema_version": 999, "operation": "list_accounts"}),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            payload = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertEqual(payload["error"]["code"], "schema_mismatch")
        self.assertEqual(payload["error"]["reason_code"], "unsupported_schema_version")


if __name__ == "__main__":
    unittest.main()
