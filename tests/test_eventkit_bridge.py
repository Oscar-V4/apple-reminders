from __future__ import annotations

import importlib.util
import json
import plistlib
import re
import stat
import struct
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


def committed_helper_bytes() -> bytes:
    return eventkit_bridge.BUNDLED_HELPER_PATH.read_bytes()


def committed_helper_slices(payload: bytes) -> list[tuple[int, int, bytes]]:
    magic, count = struct.unpack_from(">II", payload, 0)
    if magic != 0xCAFEBABE or count != 2:
        raise AssertionError("committed helper fixture is not the reviewed FAT32 binary")
    slices: list[tuple[int, int, bytes]] = []
    for index in range(count):
        cpu_type, cpu_subtype, offset, size, _alignment = struct.unpack_from(
            ">IIIII",
            payload,
            8 + index * 20,
        )
        slices.append((cpu_type, cpu_subtype, payload[offset : offset + size]))
    return slices


def fat_variant(payload: bytes, *, endian: str, uses_64_bit_entries: bool) -> bytes:
    slices = committed_helper_slices(payload)
    magic = 0xCAFEBABF if uses_64_bit_entries else 0xCAFEBABE
    entry_format = f"{endian}{'IIQQII' if uses_64_bit_entries else 'IIIII'}"
    entry_size = struct.calcsize(entry_format)
    next_offset = 8 + len(slices) * entry_size
    entries: list[bytes] = []
    contents: list[bytes] = []
    for cpu_type, cpu_subtype, content in slices:
        if uses_64_bit_entries:
            entry = (cpu_type, cpu_subtype, next_offset, len(content), 0, 0)
        else:
            entry = (cpu_type, cpu_subtype, next_offset, len(content), 0)
        entries.append(struct.pack(entry_format, *entry))
        contents.append(content)
        next_offset += len(content)
    return (
        struct.pack(f"{endian}II", magic, len(slices))
        + b"".join(entries)
        + b"".join(contents)
    )


def first_build_version_offset(payload: bytes) -> int:
    _cpu, _subtype, slice_offset, _size, _alignment = struct.unpack_from(
        ">IIIII",
        payload,
        8,
    )
    header = struct.unpack_from("<IIIIIIII", payload, slice_offset)
    command_count = header[4]
    cursor = slice_offset + 32
    for _ in range(command_count):
        command, command_size = struct.unpack_from("<II", payload, cursor)
        if command == eventkit_bridge.LC_BUILD_VERSION:
            return cursor
        cursor += command_size
    raise AssertionError("committed helper fixture has no LC_BUILD_VERSION")


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

    def test_relative_alarm_preserves_due_relative_offset(self) -> None:
        normalized = eventkit_bridge.normalize_request(
            {
                "schema_version": 1,
                "operation": "create_reminder",
                "calendar_id": "CALENDAR-1",
                "title": "Apple Developer membership expires",
                "due": {"kind": "all_day", "date": "2027-08-31"},
                "alarms": [
                    {"kind": "relative", "offset_seconds": -14 * 24 * 60 * 60}
                ],
            }
        )

        self.assertEqual(normalized["due"], {"kind": "all_day", "date": "2027-08-31"})
        self.assertEqual(
            normalized["alarms"],
            [{"kind": "relative", "offset_seconds": -1_209_600}],
        )

    def test_relative_alarm_requires_a_due_anchor_on_create(self) -> None:
        with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
            eventkit_bridge.normalize_request(
                {
                    "schema_version": 1,
                    "operation": "create_reminder",
                    "calendar_id": "CALENDAR-1",
                    "title": "Unanchored early reminder",
                    "alarms": [{"kind": "relative", "offset_seconds": -900}],
                }
            )

        self.assertEqual(raised.exception.code, "relative_alarm_requires_due")

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

    def test_native_fetch_fingerprints_ordered_membership_and_revision(self) -> None:
        source = (PLUGIN_ROOT / "scripts" / "reminders_eventkit.m").read_text(
            encoding="utf-8"
        )

        self.assertIn("OrderedReminderSnapshotFingerprint(matched)", source)
        self.assertIn("reminder.calendarItemIdentifier", source)
        self.assertIn("reminder.lastModifiedDate", source)
        self.assertIn("CC_SHA256", source)
        self.assertIn('@"snapshot_fingerprint" : snapshotFingerprint', source)

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

    def test_request_access_reports_attempt_without_claiming_prompt_observation(
        self,
    ) -> None:
        source = (PLUGIN_ROOT / "scripts" / "reminders_eventkit.m").read_text(
            encoding="utf-8"
        )

        self.assertIn('@"authorization_before"', source)
        self.assertIn('@"request_attempted" : @YES', source)
        self.assertIn('@"prompt_expected"', source)
        self.assertIn('@"prompt_observed" : [NSNull null]', source)
        self.assertIn("does not claim that this process observed a macOS prompt", source)
        self.assertIn('@"prompted_explicitly" : @YES', source)
        self.assertIn("AccessFailureClassification", source)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS EventKit")
    def test_access_callback_and_final_state_classification_matrix(self) -> None:
        source = PLUGIN_ROOT / "scripts" / "reminders_eventkit.m"
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "access-classification"
            compiled = subprocess.run(
                [
                    "clang",
                    "-x",
                    "objective-c",
                    "-fobjc-arc",
                    "-DAPPLE_REMINDERS_ACCESS_CLASSIFICATION_TEST",
                    "-framework",
                    "Foundation",
                    "-framework",
                    "EventKit",
                    str(source),
                    "-o",
                    str(binary),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            completed = subprocess.run(
                [str(binary)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

        self.assertEqual(
            json.loads(completed.stdout),
            {
                "verified": "verified",
                "denied": "permission_denied",
                "restricted": "permission_denied",
                "write_only": "permission_denied",
                "internal_error": "runtime",
                "granted_but_denied": "runtime",
                "not_determined": "runtime",
                "unknown": "runtime",
                "full_access_with_error": "runtime",
            },
        )

    def test_access_request_timeout_is_inside_both_transport_timeouts(self) -> None:
        native_source = (PLUGIN_ROOT / "scripts" / "reminders_eventkit.m").read_text(
            encoding="utf-8"
        )
        server_source = (PLUGIN_ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
        native_match = re.search(
            r"AccessRequestTimeoutSeconds\s*=\s*([0-9.]+)", native_source
        )
        server_match = re.search(
            r"EVENTKIT_BRIDGE_TIMEOUT_SECONDS\s*=\s*([0-9]+)", server_source
        )
        self.assertIsNotNone(native_match)
        self.assertIsNotNone(server_match)

        native_wait = float(native_match.group(1))
        server_timeout = int(server_match.group(1))
        self.assertLess(native_wait, eventkit_bridge.NATIVE_TIMEOUT_SECONDS)
        self.assertLess(eventkit_bridge.NATIVE_TIMEOUT_SECONDS, server_timeout)

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
        self.assertEqual(
            info["CFBundleIdentifier"],
            eventkit_bridge.LEGACY_HELPER_BUNDLE_IDENTIFIER,
        )
        self.assertNotEqual(
            eventkit_bridge.LEGACY_HELPER_BUNDLE_IDENTIFIER,
            eventkit_bridge.BUNDLED_HELPER_BUNDLE_IDENTIFIER,
        )

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
            mock.patch.object(eventkit_bridge, "resolve_helper", return_value=Path("/helper")),
            mock.patch.object(
                eventkit_bridge,
                "run_bounded_process",
                side_effect=eventkit_bridge.ProcessTimeoutError(
                    timeout_s=45,
                    argv=("/helper",),
                    pid=123,
                    returncode=-15,
                ),
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

    def test_native_output_limit_after_mutation_dispatch_is_verification_pending(
        self,
    ) -> None:
        request = {
            "schema_version": 1,
            "operation": "update_reminder",
            "reminder_id": "OPAQUE-REMINDER-ID",
            "expected_last_modified": "2026-08-08T00:00:00.000Z",
            "patch": {"title": "Changed"},
        }
        failure = eventkit_bridge.ProcessOutputLimitError(
            stream="stdout",
            limit=eventkit_bridge.MAX_NATIVE_STDOUT_BYTES,
            argv=("/helper",),
            pid=123,
            returncode=-15,
        )
        with (
            mock.patch.object(eventkit_bridge, "resolve_helper", return_value=Path("/helper")),
            mock.patch.object(
                eventkit_bridge,
                "run_bounded_process",
                side_effect=failure,
            ),
        ):
            payload = eventkit_bridge.invoke_native(request)

        self.assertEqual(payload["status"], "committed_verification_pending")
        self.assertEqual(payload["error"]["reason_code"], "invalid_native_response")
        self.assertIsNone(payload["verification"]["write_performed"])

    def test_native_utf8_decode_failure_for_read_is_no_mutation_failure(self) -> None:
        request = {
            "schema_version": 1,
            "operation": "read_reminder",
            "reminder_id": "OPAQUE-REMINDER-ID",
        }
        failure = eventkit_bridge.ProcessDecodeError(
            stream="stdout",
            cause=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
            argv=("/helper",),
            pid=123,
            returncode=0,
            stdout=b"\xff",
        )
        with (
            mock.patch.object(eventkit_bridge, "resolve_helper", return_value=Path("/helper")),
            mock.patch.object(
                eventkit_bridge,
                "run_bounded_process",
                side_effect=failure,
            ),
        ):
            payload = eventkit_bridge.invoke_native(request)

        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["reason_code"], "invalid_native_response")

    def test_native_typed_launch_failure_proves_no_mutation(self) -> None:
        request = {
            "schema_version": 1,
            "operation": "create_reminder",
            "calendar_id": "CALENDAR-1",
            "title": "Outcome unknown",
        }
        with (
            mock.patch.object(eventkit_bridge, "resolve_helper", return_value=Path("/helper")),
            mock.patch.object(
                eventkit_bridge,
                "run_bounded_process",
                side_effect=eventkit_bridge.ProcessLaunchError(
                    argv=("/helper",),
                    cause=OSError("process state unavailable"),
                ),
            ),
        ):
            payload = eventkit_bridge.invoke_native(request)

        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "unexpected_error")
        self.assertEqual(payload["error"]["reason_code"], "native_launch_failed")
        self.assertEqual(
            payload["error"]["message"],
            "The EventKit helper process could not start.",
        )
        self.assertIs(eventkit_bridge.validate_response(payload, "create_reminder"), payload)

    def test_native_helper_unavailable_is_no_mutation_failure(self) -> None:
        request = {
            "schema_version": 1,
            "operation": "create_reminder",
            "calendar_id": "CALENDAR-1",
            "title": "Never launched",
        }
        with (
            mock.patch.object(
                eventkit_bridge,
                "resolve_helper",
                side_effect=eventkit_bridge.BundledHelperUnavailable("missing"),
            ),
            mock.patch.object(
                eventkit_bridge,
                "run_bounded_process",
            ) as native_process,
        ):
            payload = eventkit_bridge.invoke_native(request)

        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["reason_code"], "native_helper_unavailable")
        native_process.assert_not_called()

    def test_resolver_never_compiles_automatically(self) -> None:
        with (
            mock.patch.object(
                eventkit_bridge,
                "_verify_bundled_helper",
                side_effect=eventkit_bridge.BundledHelperUnavailable("missing"),
            ),
            mock.patch.object(eventkit_bridge, "build_helper") as build_helper,
        ):
            with self.assertRaises(eventkit_bridge.BundledHelperUnavailable):
                eventkit_bridge.resolve_helper()

        build_helper.assert_not_called()

    def test_resolver_compiles_only_for_explicit_development_fallback(self) -> None:
        cache_root = Path("/development-cache")
        with (
            mock.patch.object(
                eventkit_bridge,
                "_verify_bundled_helper",
                side_effect=eventkit_bridge.BundledHelperUnavailable("missing"),
            ),
            mock.patch.object(
                eventkit_bridge,
                "build_helper",
                return_value=Path("/development-helper"),
            ) as build_helper,
        ):
            resolved = eventkit_bridge.resolve_helper(
                cache_root,
                allow_source_build=True,
            )

        self.assertEqual(resolved, Path("/development-helper"))
        build_helper.assert_called_once_with(cache_root)

    def test_valid_bundle_wins_even_when_development_fallback_is_allowed(self) -> None:
        with (
            mock.patch.object(
                eventkit_bridge,
                "_verify_bundled_helper",
                return_value=Path("/signed-helper"),
            ),
            mock.patch.object(eventkit_bridge, "build_helper") as build_helper,
        ):
            resolved = eventkit_bridge.resolve_helper(allow_source_build=True)

        self.assertEqual(resolved, Path("/signed-helper"))
        build_helper.assert_not_called()

    def test_explicit_development_build_failure_is_no_mutation_failure(self) -> None:
        request = {
            "schema_version": 1,
            "operation": "create_reminder",
            "calendar_id": "CALENDAR-1",
            "title": "Never launched",
        }
        with mock.patch.object(
            eventkit_bridge,
            "resolve_helper",
            side_effect=PermissionError("cache denied"),
        ):
            payload = eventkit_bridge.invoke_native(
                request,
                allow_source_build=True,
            )

        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertEqual(payload["error"]["reason_code"], "native_helper_build_failed")

    def test_bundled_inventory_rejects_symlinks_before_trust_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            native_root = Path(directory) / "native"
            app = native_root / eventkit_bridge.BUNDLED_HELPER_APP_NAME
            native_root.mkdir()
            app.mkdir()
            (native_root / eventkit_bridge.BUNDLED_HELPER_MANIFEST_NAME).write_text(
                "{}",
                encoding="utf-8",
            )
            (app / "Contents").symlink_to(native_root, target_is_directory=True)
            with (
                mock.patch.object(
                    eventkit_bridge,
                    "BUNDLED_HELPER_NATIVE_DIR",
                    native_root,
                ),
                mock.patch.object(eventkit_bridge, "BUNDLED_HELPER_APP", app),
                self.assertRaises(eventkit_bridge.BundledHelperUnavailable),
            ):
                eventkit_bridge._bundled_helper_inventory()

    def test_bundled_manifest_rejects_identity_metadata_drift(self) -> None:
        app_files = eventkit_bridge._bundled_helper_inventory()
        manifest = json.loads(
            eventkit_bridge.BUNDLED_HELPER_MANIFEST_PATH.read_text(encoding="utf-8")
        )
        manifest["team_id"] = "AAAAAAAAAA"
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "eventkit-helper-build.json"
            manifest_path.write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    eventkit_bridge,
                    "BUNDLED_HELPER_MANIFEST_PATH",
                    manifest_path,
                ),
                self.assertRaises(eventkit_bridge.BundledHelperUnavailable),
            ):
                eventkit_bridge._load_bundled_helper_manifest(
                    app_files,
                    eventkit_bridge._load_plugin_version(),
                )

    def test_bundled_macho_parser_accepts_reviewed_fat_endian_and_width_variants(
        self,
    ) -> None:
        payload = committed_helper_bytes()
        for endian, uses_64_bit_entries in (
            (">", False),
            ("<", False),
            (">", True),
            ("<", True),
        ):
            with (
                self.subTest(
                    endian=endian,
                    uses_64_bit_entries=uses_64_bit_entries,
                ),
                mock.patch.object(
                    eventkit_bridge,
                    "_run_bundled_helper_check",
                ) as system_tool,
            ):
                eventkit_bridge._verify_bundled_helper_architectures(
                    fat_variant(
                        payload,
                        endian=endian,
                        uses_64_bit_entries=uses_64_bit_entries,
                    )
                )
                system_tool.assert_not_called()

    def test_bundled_macho_parser_requires_exactly_both_reviewed_slices(self) -> None:
        payload = bytearray(committed_helper_bytes())
        struct.pack_into(">I", payload, 4, 1)

        with self.assertRaises(eventkit_bridge.BundledHelperUnavailable):
            eventkit_bridge._verify_bundled_helper_architectures(bytes(payload))

        capability_subtype = bytearray(committed_helper_bytes())
        struct.pack_into(">I", capability_subtype, 12, 0x80000003)
        with self.assertRaises(eventkit_bridge.BundledHelperUnavailable):
            eventkit_bridge._verify_bundled_helper_architectures(
                bytes(capability_subtype)
            )

        inner_subtype_drift = bytearray(committed_helper_bytes())
        first_slice_offset = struct.unpack_from(">I", inner_subtype_drift, 16)[0]
        struct.pack_into("<I", inner_subtype_drift, first_slice_offset + 8, 8)
        with self.assertRaises(eventkit_bridge.BundledHelperUnavailable):
            eventkit_bridge._verify_bundled_helper_architectures(
                bytes(inner_subtype_drift)
            )

    def test_bundled_macho_parser_rejects_malformed_boundaries(self) -> None:
        original = committed_helper_bytes()
        mutations: dict[str, bytearray] = {}

        inside_table = bytearray(original)
        struct.pack_into(">I", inside_table, 16, 0)
        mutations["slice inside fat table"] = inside_table

        past_eof = bytearray(original)
        struct.pack_into(">I", past_eof, 20, 0xFFFFFFFF)
        mutations["slice past eof"] = past_eof

        overlap = bytearray(original)
        first_slice_offset = struct.unpack_from(">I", overlap, 16)[0]
        struct.pack_into(">I", overlap, 36, first_slice_offset)
        mutations["overlapping slices"] = overlap

        truncated_commands = bytearray(original)
        struct.pack_into(
            "<I",
            truncated_commands,
            first_slice_offset + 20,
            len(truncated_commands),
        )
        mutations["load commands outside slice"] = truncated_commands

        for name, payload in mutations.items():
            with self.subTest(name=name), self.assertRaises(
                eventkit_bridge.BundledHelperUnavailable
            ):
                eventkit_bridge._verify_bundled_helper_architectures(bytes(payload))

    def test_bundled_macho_parser_rejects_wrong_or_missing_build_version(self) -> None:
        original = committed_helper_bytes()
        command_offset = first_build_version_offset(original)

        wrong_minimum = bytearray(original)
        struct.pack_into("<I", wrong_minimum, command_offset + 12, 13 << 16)
        with self.assertRaises(eventkit_bridge.BundledHelperUnavailable):
            eventkit_bridge._verify_bundled_helper_architectures(
                bytes(wrong_minimum)
            )

        missing_command = bytearray(original)
        struct.pack_into("<I", missing_command, command_offset, 0x33)
        with self.assertRaises(eventkit_bridge.BundledHelperUnavailable):
            eventkit_bridge._verify_bundled_helper_architectures(
                bytes(missing_command)
            )

        _cpu, _subtype, slice_offset, _size, _alignment = struct.unpack_from(
            ">IIIII",
            original,
            8,
        )
        header = struct.unpack_from("<IIIIIIII", original, slice_offset)
        cursor = slice_offset + 32
        duplicate_target: int | None = None
        for _ in range(header[4]):
            command, command_size = struct.unpack_from("<II", original, cursor)
            if command != eventkit_bridge.LC_BUILD_VERSION and command_size == 32:
                duplicate_target = cursor
                break
            cursor += command_size
        self.assertIsNotNone(duplicate_target)
        assert duplicate_target is not None
        duplicate_command = bytearray(original)
        duplicate_command[duplicate_target : duplicate_target + 32] = original[
            command_offset : command_offset + 32
        ]
        with self.assertRaises(eventkit_bridge.BundledHelperUnavailable):
            eventkit_bridge._verify_bundled_helper_architectures(
                bytes(duplicate_command)
            )

    def test_bundled_signature_check_pins_developer_id_and_team(self) -> None:
        signing_details = "\n".join(
            (
                f"Identifier={eventkit_bridge.BUNDLED_HELPER_BUNDLE_IDENTIFIER}",
                "CodeDirectory v=20500 flags=0x10000(runtime) hashes=1+1",
                "Timestamp=Aug 31, 2026 at 12:16:32 AM",
                f"TeamIdentifier={eventkit_bridge.BUNDLED_HELPER_TEAM_IDENTIFIER}",
            )
        )
        with mock.patch.object(
            eventkit_bridge,
            "_run_bundled_helper_check",
            side_effect=[("", ""), ("", signing_details)],
        ) as check:
            eventkit_bridge._verify_bundled_helper_signature()

        verification_argv = check.call_args_list[0].args[0]
        self.assertIn("--test-requirement", verification_argv)
        requirement = verification_argv[
            verification_argv.index("--test-requirement") + 1
        ]
        self.assertIn(eventkit_bridge.BUNDLED_HELPER_BUNDLE_IDENTIFIER, requirement)
        self.assertIn(eventkit_bridge.BUNDLED_HELPER_TEAM_IDENTIFIER, requirement)
        self.assertIn("1.2.840.113635.100.6.1.13", requirement)

        wrong_team_details = signing_details.replace(
            eventkit_bridge.BUNDLED_HELPER_TEAM_IDENTIFIER,
            "AAAAAAAAAA",
        )
        with (
            mock.patch.object(
                eventkit_bridge,
                "_run_bundled_helper_check",
                side_effect=[("", ""), ("", wrong_team_details)],
            ),
            self.assertRaises(eventkit_bridge.BundledHelperUnavailable),
        ):
            eventkit_bridge._verify_bundled_helper_signature()

    @unittest.skipUnless(sys.platform == "darwin", "signed helper requires macOS")
    def test_committed_bundled_helper_passes_runtime_trust_checks(self) -> None:
        previous = eventkit_bridge._verified_bundled_helper_fingerprint
        eventkit_bridge._verified_bundled_helper_fingerprint = None
        trust_runner = eventkit_bridge._run_bundled_helper_check
        try:
            with mock.patch.object(
                eventkit_bridge,
                "_run_bundled_helper_check",
                side_effect=trust_runner,
            ) as trust_check:
                resolved = eventkit_bridge._verify_bundled_helper()
        finally:
            eventkit_bridge._verified_bundled_helper_fingerprint = previous

        self.assertEqual(resolved, eventkit_bridge.BUNDLED_HELPER_PATH)
        self.assertTrue(resolved.is_file())
        trust_commands = [call.args[0] for call in trust_check.call_args_list]
        self.assertEqual(len(trust_commands), 2)
        self.assertTrue(
            all(command[0] == "/usr/bin/codesign" for command in trust_commands)
        )
        self.assertFalse(
            any(
                developer_tool in argument
                for command in trust_commands
                for argument in command
                for developer_tool in ("xcrun", "clang", "lipo", "vtool")
            )
        )

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
            mock.patch.object(eventkit_bridge, "resolve_helper", return_value=Path("/helper")),
            mock.patch.object(eventkit_bridge, "run_bounded_process", return_value=completed),
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
            mock.patch.object(eventkit_bridge, "resolve_helper", return_value=Path("/helper")),
            mock.patch.object(
                eventkit_bridge,
                "run_bounded_process",
                side_effect=eventkit_bridge.ProcessTimeoutError(
                    timeout_s=45,
                    argv=("/helper",),
                    pid=123,
                    returncode=-15,
                ),
            ),
        ):
            payload = eventkit_bridge.invoke_native(request)

        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertFalse(payload["ok"])

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
                f"Identifier={eventkit_bridge.LEGACY_HELPER_BUNDLE_IDENTIFIER}",
                signature.stdout,
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
                    self.assertIs(
                        payload["data"]["fields"]["relative_alarm_writes"],
                        True,
                    )
                    self.assertNotIn(
                        "early_reminder_relative_alarm_writes",
                        payload["data"]["not_exposed"],
                    )
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
