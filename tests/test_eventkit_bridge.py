from __future__ import annotations

import importlib.util
import json
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
BRIDGE_PATH = PLUGIN_ROOT / "scripts" / "eventkit_bridge.py"
SCHEMA_PATH = PLUGIN_ROOT / "scripts" / "eventkit_bridge_schema.json"
SPEC = importlib.util.spec_from_file_location("eventkit_bridge", BRIDGE_PATH)
assert SPEC and SPEC.loader
eventkit_bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eventkit_bridge)


class EventKitRequestValidationTests(unittest.TestCase):
    def test_ensure_list_requires_an_exact_source_and_normalizes_name(self) -> None:
        normalized = eventkit_bridge.normalize_request(
            {
                "schema_version": 1,
                "operation": "ensure_reminder_list",
                "source_id": "SOURCE-ICLOUD",
                "name": "  Work  ",
            }
        )

        self.assertEqual(
            normalized,
            {
                "schema_version": 1,
                "operation": "ensure_reminder_list",
                "source_id": "SOURCE-ICLOUD",
                "name": "Work",
            },
        )

    def test_ensure_list_rejects_unscoped_or_styling_fields(self) -> None:
        cases = [
            {"name": "Work"},
            {"source_id": "SOURCE-ICLOUD", "name": "Work", "emblem": "briefcase"},
        ]
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(
                eventkit_bridge.BridgeValidationError
            ):
                eventkit_bridge.normalize_request(
                    {
                        "schema_version": 1,
                        "operation": "ensure_reminder_list",
                        **fields,
                    }
                )

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

    def test_timed_due_rejects_path_like_time_zone_as_typed_input_error(self) -> None:
        for zone_name in ("/etc/passwd", "../zoneinfo/UTC"):
            with self.subTest(zone_name=zone_name):
                with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
                    eventkit_bridge.normalize_request(
                        {
                            "schema_version": 1,
                            "operation": "create_reminder",
                            "calendar_id": "CALENDAR-1",
                            "title": "Invalid zone",
                            "due": {
                                "kind": "timed",
                                "date_time": "2026-08-05T14:30:00+09:00",
                                "time_zone": zone_name,
                            },
                        }
                    )

                self.assertEqual(raised.exception.code, "invalid_time_zone")

    def test_timed_due_rejects_utc_conversion_overflow_as_typed_input_error(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "create_reminder",
                    "calendar_id": "CALENDAR-1",
                    "title": "Overflow boundary",
                    "due": {
                        "kind": "timed",
                        "date_time": "0001-01-01T00:00:00+14:00",
                        "time_zone": "Etc/GMT-14",
                    },
                }
            )

        self.assertEqual(raised.exception.code, "invalid_rfc3339")

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

    def test_fetch_rejects_status_any(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "fetch_reminders",
                    "calendar_ids": ["CALENDAR-1"],
                    "status": "any",
                    "limit": 50,
                }
            )

        self.assertEqual(raised.exception.code, "invalid_enum")
        self.assertEqual(
            str(raised.exception),
            "$.status must be incomplete or completed",
        )

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

    def test_completed_due_range_does_not_replace_completion_range(self) -> None:
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

        self.assertEqual(raised.exception.code, "missing_completion_range")

    def test_completed_fetch_requires_completion_range_even_for_calendar_scope(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "fetch_reminders",
                    "calendar_ids": ["CALENDAR-1"],
                    "status": "completed",
                    "limit": 50,
                }
            )

        self.assertEqual(raised.exception.code, "missing_completion_range")
        self.assertEqual(
            str(raised.exception),
            "status=completed requires both $.completion_start and $.completion_end",
        )

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

    def test_incomplete_fetch_rejects_due_range_over_366_days(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "fetch_reminders",
                    "status": "incomplete",
                    "due_start": "2026-01-01T00:00:00Z",
                    "due_end": "2027-01-03T00:00:00Z",
                    "limit": 50,
                }
            )

        self.assertEqual(raised.exception.code, "range_too_wide")
        self.assertEqual(
            str(raised.exception),
            "$.due_start/$.due_end span must not exceed 366 days",
        )

    def test_completed_fetch_rejects_completion_range_over_90_days(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "fetch_reminders",
                    "status": "completed",
                    "completion_start": "2026-01-01T00:00:00Z",
                    "completion_end": "2026-04-02T00:00:00Z",
                    "limit": 50,
                }
            )

        self.assertEqual(raised.exception.code, "range_too_wide")
        self.assertEqual(
            str(raised.exception),
            "$.completion_start/$.completion_end span must not exceed 90 days",
        )

    def test_fetch_accepts_exact_maximum_date_ranges(self) -> None:
        incomplete = eventkit_bridge.normalize_request(
            {
                "schema_version": 1,
                "operation": "fetch_reminders",
                "status": "incomplete",
                "due_start": "2026-01-01T00:00:00Z",
                "due_end": "2027-01-02T00:00:00Z",
                "limit": 50,
            }
        )
        completed = eventkit_bridge.normalize_request(
            {
                "schema_version": 1,
                "operation": "fetch_reminders",
                "calendar_ids": ["CALENDAR-1"],
                "status": "completed",
                "completion_start": "2026-01-01T00:00:00Z",
                "completion_end": "2026-04-01T00:00:00Z",
                "limit": 50,
            }
        )

        self.assertEqual(incomplete["status"], "incomplete")
        self.assertEqual(completed["status"], "completed")

    def test_incomplete_fetch_rejects_completion_range_as_its_only_scope(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "fetch_reminders",
                    "status": "incomplete",
                    "completion_start": "2026-01-01T00:00:00Z",
                    "completion_end": "2026-01-31T00:00:00Z",
                    "limit": 50,
                }
            )

        self.assertEqual(raised.exception.code, "unbounded_read")

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

    def test_delete_reminder_requires_exact_id_and_last_modified(self) -> None:
        normalized = eventkit_bridge.normalize_request(
            {
                "schema_version": 1,
                "operation": "delete_reminder",
                "reminder_id": "REMINDER-1",
                "expected_last_modified": "2026-08-05T00:00:00Z",
            }
        )

        self.assertEqual(normalized["operation"], "delete_reminder")
        self.assertEqual(normalized["reminder_id"], "REMINDER-1")
        self.assertEqual(
            normalized["expected_last_modified"],
            "2026-08-05T00:00:00.000Z",
        )

    def test_delete_reminder_rejects_null_last_modified(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "delete_reminder",
                    "reminder_id": "REMINDER-1",
                    "expected_last_modified": None,
                }
            )

        self.assertEqual(raised.exception.code, "invalid_type")

    def test_existing_item_operations_reject_reminder_deep_links(self) -> None:
        requests = (
            {
                "schema_version": 1,
                "operation": "read_reminder",
                "reminder_id": "x-apple-reminder://REMINDER-1",
            },
            {
                "schema_version": 1,
                "operation": "update_reminder",
                "reminder_id": "x-apple-reminder://REMINDER-1",
                "expected_last_modified": "2026-08-05T00:00:00Z",
                "patch": {"title": "Changed"},
            },
            {
                "schema_version": 1,
                "operation": "complete_reminder",
                "reminder_id": "x-apple-reminder://REMINDER-1",
                "expected_last_modified": "2026-08-05T00:00:00Z",
            },
            {
                "schema_version": 1,
                "operation": "reopen_reminder",
                "reminder_id": "x-apple-reminder://REMINDER-1",
                "expected_last_modified": "2026-08-05T00:00:00Z",
            },
            {
                "schema_version": 1,
                "operation": "move_reminder",
                "reminder_id": "x-apple-reminder://REMINDER-1",
                "expected_last_modified": "2026-08-05T00:00:00Z",
                "calendar_id": "CALENDAR-1",
            },
            {
                "schema_version": 1,
                "operation": "delete_reminder",
                "reminder_id": "x-apple-reminder://REMINDER-1",
                "expected_last_modified": "2026-08-05T00:00:00Z",
            },
        )

        for request in requests:
            with self.subTest(operation=request["operation"]):
                with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
                    eventkit_bridge.normalize_request(request)

                self.assertEqual(raised.exception.code, "invalid_identifier")

    def test_native_delete_does_not_claim_missing_identifier_was_already_deleted(self) -> None:
        source = (PLUGIN_ROOT / "scripts" / "reminders_eventkit.m").read_text(
            encoding="utf-8"
        )

        self.assertNotIn('@"already_absent"', source)
        self.assertIn(
            '@"Reminder was not found; read current state before retrying"',
            source,
        )

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
                    "expected_last_modified": "2026-08-05T00:00:00Z",
                    "patch": {"flagged": True},
                }
            )

        self.assertEqual(raised.exception.code, "unknown_fields")

    def test_plain_location_is_rejected_instead_of_committing_a_noop(self) -> None:
        for request in (
            {
                "schema_version": 1,
                "operation": "create_reminder",
                "calendar_id": "CALENDAR-1",
                "title": "Unsupported location",
                "location": "Market",
            },
            {
                "schema_version": 1,
                "operation": "update_reminder",
                "reminder_id": "REMINDER-1",
                "expected_last_modified": "2026-08-05T00:00:00Z",
                "patch": {"location": "Market"},
            },
        ):
            with self.subTest(operation=request["operation"]):
                with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
                    eventkit_bridge.normalize_request(request)

                self.assertEqual(raised.exception.code, "unknown_fields")
                self.assertEqual(raised.exception.details["fields"], ["location"])

    def test_malformed_url_is_a_typed_validation_error(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "create_reminder",
                    "calendar_id": "CALENDAR-1",
                    "title": "Malformed URL",
                    "url": "http://[::1",
                }
            )

        self.assertEqual(raised.exception.code, "invalid_url")

    def test_notes_character_bound_fits_worst_case_json_transport(self) -> None:
        normalized = eventkit_bridge.normalize_request(
            {
                "schema_version": 1,
                "operation": "create_reminder",
                "calendar_id": "CALENDAR-1",
                "title": "Bounded notes",
                "notes": "\0" * 100_000,
            }
        )
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        self.assertLess(len(encoded), eventkit_bridge.MAX_REQUEST_BYTES)
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "create_reminder",
                    "calendar_id": "CALENDAR-1",
                    "title": "Oversized notes",
                    "notes": "n" * 100_001,
                }
            )

        self.assertEqual(raised.exception.code, "string_too_long")


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
            schema["properties"]["status"]["enum"],
            ["incomplete", "completed"],
        )

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

    def test_helper_and_bundle_match_the_documented_macos_14_minimum(self) -> None:
        helper = (PLUGIN_ROOT / "scripts" / "reminders_eventkit.m").read_text(
            encoding="utf-8"
        )
        bridge = BRIDGE_PATH.read_text(encoding="utf-8")
        info = plistlib.loads(
            (PLUGIN_ROOT / "scripts" / "eventkit_bridge_info.plist").read_bytes()
        )

        self.assertIn("requestFullAccessToRemindersWithCompletion", helper)
        self.assertNotIn("requestAccessToEntityType", helper)
        self.assertIn('"-mmacosx-version-min=14.0"', bridge)
        self.assertEqual(info["LSMinimumSystemVersion"], "14.0")

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

    def test_native_timeout_after_possible_mutation_is_verification_pending(self) -> None:
        request = {
            "schema_version": 1,
            "operation": "delete_reminder",
            "reminder_id": "OPAQUE-REMINDER-ID",
            "expected_last_modified": "2026-08-08T00:00:00.000Z",
        }
        with (
            mock.patch.object(eventkit_bridge, "build_helper", return_value=Path("/helper")),
            mock.patch.object(
                eventkit_bridge.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["/helper"], 45),
            ),
        ):
            payload = eventkit_bridge.invoke_native(request)

        self.assertEqual(payload["status"], "committed_verification_pending")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["verification"]["state"], "pending")
        self.assertIsNone(payload["verification"]["write_performed"])
        self.assertEqual(payload["error"]["code"], "sync_pending")
        self.assertEqual(payload["error"]["reason_code"], "native_timeout")
        self.assertIs(eventkit_bridge.validate_response(payload, "delete_reminder"), payload)

    def test_native_helper_build_failure_is_no_mutation_failure(self) -> None:
        request = {
            "schema_version": 1,
            "operation": "create_reminder",
            "calendar_id": "CALENDAR-1",
            "title": "Never launched",
        }
        with mock.patch.object(
            eventkit_bridge,
            "build_helper",
            side_effect=PermissionError("cache denied"),
        ):
            payload = eventkit_bridge.invoke_native(request)

        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["reason_code"], "native_helper_build_failed")

    def test_invalid_native_output_after_possible_mutation_is_verification_pending(self) -> None:
        request = {
            "schema_version": 1,
            "operation": "update_reminder",
            "reminder_id": "OPAQUE-REMINDER-ID",
            "expected_last_modified": "2026-08-08T00:00:00.000Z",
            "patch": {"title": "Changed"},
        }
        completed = subprocess.CompletedProcess(["/helper"], 0, b"not-json", b"")
        with (
            mock.patch.object(eventkit_bridge, "build_helper", return_value=Path("/helper")),
            mock.patch.object(eventkit_bridge.subprocess, "run", return_value=completed),
        ):
            payload = eventkit_bridge.invoke_native(request)

        self.assertEqual(payload["status"], "committed_verification_pending")
        self.assertEqual(payload["error"]["reason_code"], "invalid_native_response")
        self.assertIs(eventkit_bridge.validate_response(payload, "update_reminder"), payload)

    def test_native_timeout_for_read_remains_a_no_mutation_failure(self) -> None:
        request = {
            "schema_version": 1,
            "operation": "read_reminder",
            "reminder_id": "OPAQUE-REMINDER-ID",
        }
        with (
            mock.patch.object(eventkit_bridge, "build_helper", return_value=Path("/helper")),
            mock.patch.object(
                eventkit_bridge.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["/helper"], 45),
            ),
        ):
            payload = eventkit_bridge.invoke_native(request)

        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertFalse(payload["ok"])

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

    def test_unexpected_prelaunch_normalization_exception_is_no_mutation_failure(self) -> None:
        raw = {
            "schema_version": 1,
            "operation": "create_reminder",
            "calendar_id": "CALENDAR-1",
            "title": "Never launched",
        }
        with (
            mock.patch.object(eventkit_bridge, "read_request", return_value=raw),
            mock.patch.object(
                eventkit_bridge,
                "normalize_request",
                side_effect=RuntimeError("normalizer bug"),
            ),
            mock.patch.object(eventkit_bridge, "invoke_native") as invoke_native,
            mock.patch.object(eventkit_bridge, "emit") as emit,
        ):
            exit_code = eventkit_bridge.main(["--validate-only"])

        payload = emit.call_args.args[0]
        self.assertEqual(exit_code, eventkit_bridge.EXIT_CODES["failed_no_mutation"])
        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["reason_code"], "request_normalization_failed")
        invoke_native.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "Objective-C EventKit helper requires macOS")
    def test_native_helper_static_operations_emit_valid_receipts_without_reading_reminders(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = eventkit_bridge.build_helper(Path(directory))
            self.assertTrue(binary.is_file())
            self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(Path(directory).stat().st_mode), 0o700)
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
                self.assertIs(payload["ok"], True)
                if operation == "capabilities":
                    self.assertIs(payload["data"]["fields"]["plain_location"], False)
                self.assertIs(eventkit_bridge.validate_response(payload, operation), payload)

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
