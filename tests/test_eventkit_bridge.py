from __future__ import annotations

import copy
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
SCRIPTS_PATH = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_PATH) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_PATH))
BRIDGE_PATH = PLUGIN_ROOT / "scripts" / "eventkit_bridge.py"
SCHEMA_PATH = PLUGIN_ROOT / "scripts" / "eventkit_bridge_schema.json"
SPEC = importlib.util.spec_from_file_location("eventkit_bridge", BRIDGE_PATH)
assert SPEC and SPEC.loader
eventkit_bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eventkit_bridge)

from reminders_service import (  # noqa: E402
    MoveToListAction,
    PatchAction,
    SetCompletionAction,
    canonical_action_projection,
    reminder_matches_fields,
)


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

    def test_repeated_due_wall_times_fail_before_native_dispatch(self) -> None:
        cases = (
            ("America/New_York", "2026-11-01T01:30:00-04:00"),
            ("America/New_York", "2026-11-01T01:30:00-05:00"),
            ("Australia/Lord_Howe", "2026-04-05T01:45:00+11:00"),
            ("Australia/Lord_Howe", "2026-04-05T01:45:00+10:30"),
        )
        for zone_name, timestamp in cases:
            for operation in ("create_reminder", "update_reminder"):
                with self.subTest(
                    zone=zone_name, timestamp=timestamp, operation=operation
                ):
                    due = {
                        "kind": "timed",
                        "date_time": timestamp,
                        "time_zone": zone_name,
                    }
                    raw = {"schema_version": 1, "operation": operation}
                    if operation == "create_reminder":
                        raw.update(
                            {
                                "calendar_id": "CALENDAR-1",
                                "title": "Repeated clock time",
                                "due": due,
                            }
                        )
                    else:
                        raw.update(
                            {
                                "reminder_id": "REMINDER-1",
                                "expected_last_modified": "2026-01-01T00:00:00Z",
                                "patch": {"due": due},
                            }
                        )
                    with (
                        mock.patch.object(eventkit_bridge, "read_request", return_value=raw),
                        mock.patch.object(eventkit_bridge, "invoke_native") as invoke_native,
                        mock.patch.object(eventkit_bridge, "emit") as emit,
                    ):
                        exit_code = eventkit_bridge.main([])

                    invoke_native.assert_not_called()
                    self.assertEqual(exit_code, 2)
                    payload = emit.call_args.args[0]
                    self.assertEqual(payload["status"], "failed_no_mutation")
                    self.assertEqual(payload["error"]["code"], "unsupported_capability")
                    self.assertEqual(
                        payload["error"]["reason_code"], "ambiguous_local_time"
                    )
                    self.assertIn("Choose a time outside", payload["error"]["message"])

    def test_unambiguous_due_times_neighboring_dst_changes_remain_supported(self) -> None:
        for timestamp in (
            "2026-11-01T00:59:59-04:00",
            "2026-11-01T02:00:00-05:00",
            "2026-03-08T01:59:59-05:00",
            "2026-03-08T03:00:00-04:00",
        ):
            with self.subTest(timestamp=timestamp):
                normalized = eventkit_bridge.normalize_due(
                    {
                        "kind": "timed",
                        "date_time": timestamp,
                        "time_zone": "America/New_York",
                    },
                    "$.due",
                )
                self.assertEqual(
                    normalized["date_time"], timestamp[:-6] + ".000" + timestamp[-6:]
                )

    def test_nonexistent_due_wall_times_and_wrong_dst_offsets_are_rejected(self) -> None:
        for timestamp in (
            "2026-03-08T02:30:00-05:00",
            "2026-03-08T02:30:00-04:00",
            "2026-11-01T02:30:00-04:00",
        ):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
                    eventkit_bridge.normalize_due(
                        {
                            "kind": "timed",
                            "date_time": timestamp,
                            "time_zone": "America/New_York",
                        },
                        "$.due",
                    )
                self.assertEqual(raised.exception.code, "time_zone_offset_mismatch")

    def test_absolute_alarms_preserve_both_repeated_hour_instants(self) -> None:
        for timestamp, expected in (
            ("2026-11-01T01:30:00-04:00", "2026-11-01T05:30:00.000Z"),
            ("2026-11-01T01:30:00-05:00", "2026-11-01T06:30:00.000Z"),
        ):
            with self.subTest(timestamp=timestamp):
                alarm = eventkit_bridge.normalize_alarm(
                    {"kind": "absolute", "date_time": timestamp}, "$.alarms[0]"
                )
                self.assertEqual(alarm["date_time"], expected)

    def test_named_zone_conversion_overflow_is_a_typed_input_error(self) -> None:
        for timestamp, zone_name in (
            ("9999-12-31T23:59:59Z", "Pacific/Kiritimati"),
            ("0001-01-01T00:00:00Z", "America/New_York"),
        ):
            with self.subTest(timestamp=timestamp, zone_name=zone_name):
                with self.assertRaises(eventkit_bridge.BridgeValidationError) as raised:
                    eventkit_bridge.normalize_due(
                        {"kind": "timed", "date_time": timestamp, "time_zone": zone_name},
                        "$.due",
                    )
                self.assertEqual(raised.exception.code, "invalid_rfc3339")

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

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS EventKit")
    def test_native_verification_projection_tracks_relative_alarm_dependencies(
        self,
    ) -> None:
        source = PLUGIN_ROOT / "scripts" / "reminders_eventkit.m"
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "relative-alarm-contract"
            compiled = subprocess.run(
                [
                    "clang",
                    "-x",
                    "objective-c",
                    "-fobjc-arc",
                    "-DAPPLE_REMINDERS_VERIFICATION_PROJECTION_TEST",
                    "-framework",
                    "Foundation",
                    "-framework",
                    "EventKit",
                    "-framework",
                    "CoreLocation",
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

        payload = json.loads(completed.stdout)
        self.assertEqual(
            payload["absolute_title_projection"],
            {
                "calendar_id": "CALENDAR-1",
                "title": "Changed title",
                "notes": "Stable notes",
                "url": None,
                "location": "Stable location",
                "priority": 5,
                "completed": False,
                "due": {"kind": "all_day", "date": "2027-08-31"},
                "start": None,
                "alarms": [
                    {
                        "kind": "absolute",
                        "date_time": "2027-08-17T00:00:00.000Z",
                    }
                ],
                "recurrence_rules": [],
            },
        )
        alarm_kinds = {
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
        actions = {
            "title_patch": (False, PatchAction({"title": "Changed title"})),
            "completion": (False, SetCompletionAction(True)),
            "reopen": (True, SetCompletionAction(False)),
            "move": (False, MoveToListAction("CALENDAR-2")),
        }
        for alarm_name, alarm in alarm_kinds.items():
            for action_name, (completed_state, action) in actions.items():
                with self.subTest(alarm=alarm_name, action=action_name):
                    before = {
                        "title": "Original title",
                        "notes": "Stable notes",
                        "url": None,
                        "location": "Stable location",
                        "priority": 5,
                        "completed": completed_state,
                        "due": {"kind": "all_day", "date": "2027-08-31"},
                        "start": None,
                        "alarms": [alarm],
                        "recurrence_rules": [],
                        "list_id": "CALENDAR-1",
                    }
                    native_projection = dict(
                        payload["semantic_matrix"][alarm_name][action_name]
                    )
                    native_projection["list_id"] = native_projection.pop(
                        "calendar_id"
                    )
                    self.assertEqual(
                        native_projection,
                        canonical_action_projection(before, action),
                    )
        expected_due = {"kind": "all_day", "date": "2027-09-30"}
        expected_alarms = [
            {"kind": "relative", "offset_seconds": -1_209_600}
        ]
        self.assertEqual(
            payload["due_projection"],
            {
                "calendar_id": "CALENDAR-1",
                "due": expected_due,
                "alarms": expected_alarms,
            },
        )
        self.assertEqual(
            payload["alarm_projection"],
            {
                "calendar_id": "CALENDAR-1",
                "due": {"kind": "all_day", "date": "2027-08-31"},
                "alarms": expected_alarms,
            },
        )
        self.assertEqual(
            payload["move_projection"],
            {
                "calendar_id": "CALENDAR-2",
                "due": {"kind": "all_day", "date": "2027-08-31"},
                "alarms": expected_alarms,
            },
        )
        self.assertIs(payload["due_drift_matched"], False)
        self.assertIs(payload["alarm_drift_matched"], False)
        self.assertIs(payload["move_drift_matched"], False)
        self.assertIs(payload["setter_drift_matched"], False)
        self.assertIs(payload["permuted_alarms_matched"], True)
        self.assertIs(payload["permuted_duplicate_alarms_matched"], True)
        self.assertIs(payload["lost_duplicate_alarm_matched"], False)
        read_only_alarm = {
            "kind": "absolute",
            "date_time": "2027-08-17T00:00:00.000Z",
            "read_only": True,
            "action": {"type": "procedure", "url": "example:before"},
        }
        self.assertEqual(
            payload["read_only_move_projection"],
            {
                "calendar_id": "CALENDAR-2",
                "completed": False,
                "due": None,
                "alarms": [read_only_alarm],
            },
        )
        self.assertEqual(
            payload["read_only_completion_projection"],
            {
                "calendar_id": "CALENDAR-1",
                "completed": True,
                "due": None,
                "alarms": [read_only_alarm],
            },
        )
        self.assertIs(payload["read_only_move_drift_matched"], False)
        self.assertIs(payload["read_only_completion_drift_matched"], False)
        self.assertEqual(
            payload["timed_due"],
            {
                "kind": "timed",
                "date_time": "2027-01-15T08:00:00.123+09:00",
                "time_zone": "Asia/Seoul",
            },
        )
        self.assertEqual(
            payload["fractional_alarm_projection"],
            {
                "alarms": [
                    {
                        "kind": "absolute",
                        "date_time": "2027-01-14T23:00:00.123Z",
                    }
                ]
            },
        )
        self.assertIs(payload["fractional_alarm_setter_drift_matched"], False)
        self.assertIs(payload["timed_due_setter_drift_matched"], False)
        self.assertEqual(
            payload["parsed_fractional_date"],
            "2027-01-14T23:00:00.123Z",
        )

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS EventKit")
    def test_public_matcher_mirrors_native_non_alarm_order_and_lossy_actual_rules(
        self,
    ) -> None:
        source = PLUGIN_ROOT / "scripts" / "reminders_eventkit.m"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wrapper = root / "projection-parity.m"
            binary = root / "projection-parity"
            wrapper.write_text(
                f'''\
#define main AppleRemindersProductionMain
#include "{source.as_posix()}"
#undef main

int main(void) {{
    @autoreleasepool {{
        NSDictionary *expectedRecurrence = @{{
            @"recurrence_rules" : @[@{{
                @"frequency" : @"weekly",
                @"interval" : @1,
                @"days_of_week" : @[
                    @{{ @"day" : @"monday" }},
                    @{{ @"day" : @"tuesday" }},
                ],
            }}],
        }};
        NSDictionary *reorderedRecurrence = @{{
            @"recurrence_rules" : @[@{{
                @"frequency" : @"weekly",
                @"interval" : @1,
                @"days_of_week" : @[
                    @{{ @"day" : @"tuesday" }},
                    @{{ @"day" : @"monday" }},
                ],
            }}],
        }};
        NSDictionary *expectedAlarm = @{{
            @"alarms" : @[@{{
                @"kind" : @"absolute",
                @"date_time" : @"2027-08-17T00:00:00.000Z",
                @"read_only" : @YES,
                @"action" : @{{
                    @"type" : @"procedure",
                    @"url" : @"example:run",
                }},
            }}],
        }};
        NSDictionary *newlyLossyAlarm = @{{
            @"alarms" : @[@{{
                @"kind" : @"absolute",
                @"date_time" : @"2027-08-17T00:00:00.000Z",
                @"read_only" : @YES,
                @"action" : @{{
                    @"type" : @"procedure",
                    @"url" : @"example:run",
                }},
                @"_verification_unavailable" : @YES,
            }}],
        }};
        NSDictionary *payload = @{{
            @"recurrence_order_matched" : @(
                ProjectionMatches(expectedRecurrence, reorderedRecurrence)),
            @"newly_lossy_alarm_matched" : @(
                ProjectionMatches(expectedAlarm, newlyLossyAlarm)),
        }};
        NSData *output = [NSJSONSerialization dataWithJSONObject:payload
                                                         options:NSJSONWritingSortedKeys
                                                           error:nil];
        [[NSFileHandle fileHandleWithStandardOutput] writeData:output];
        return 0;
    }}
}}
''',
                encoding="utf-8",
            )
            compiled = subprocess.run(
                [
                    "clang",
                    "-x",
                    "objective-c",
                    "-fobjc-arc",
                    "-framework",
                    "Foundation",
                    "-framework",
                    "EventKit",
                    "-framework",
                    "CoreLocation",
                    str(wrapper),
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

        native = json.loads(completed.stdout)
        expected_recurrence = {
            "recurrence_rules": [
                {
                    "frequency": "weekly",
                    "interval": 1,
                    "days_of_week": [{"day": "monday"}, {"day": "tuesday"}],
                }
            ]
        }
        reordered_recurrence = {
            "recurrence_rules": [
                {
                    "frequency": "weekly",
                    "interval": 1,
                    "days_of_week": [{"day": "tuesday"}, {"day": "monday"}],
                }
            ]
        }
        expected_alarm = {
            "alarms": [
                {
                    "kind": "absolute",
                    "date_time": "2027-08-17T00:00:00.000Z",
                    "read_only": True,
                    "action": {"type": "procedure", "url": "example:run"},
                }
            ]
        }
        newly_lossy_alarm = copy.deepcopy(expected_alarm)
        newly_lossy_alarm["alarms"][0]["_verification_unavailable"] = True

        self.assertIs(native["recurrence_order_matched"], False)
        self.assertEqual(
            reminder_matches_fields(reordered_recurrence, expected_recurrence),
            native["recurrence_order_matched"],
        )
        self.assertIs(native["newly_lossy_alarm_matched"], False)
        self.assertEqual(
            reminder_matches_fields(newly_lossy_alarm, expected_alarm),
            native["newly_lossy_alarm_matched"],
        )

    def test_native_save_verifies_through_a_fresh_identifier_lookup(self) -> None:
        source = (PLUGIN_ROOT / "scripts" / "reminders_eventkit.m").read_text(
            encoding="utf-8"
        )
        save_and_verify = source.split(
            "static NSDictionary *SaveAndVerify", 1
        )[1].split("static NSDictionary *DeleteAndVerify", 1)[0]

        self.assertIn("calendarItemWithIdentifier", save_and_verify)
        self.assertIn("[store reset]", save_and_verify)
        self.assertLess(
            save_and_verify.index("[store reset]"),
            save_and_verify.index("calendarItemWithIdentifier"),
        )
        self.assertNotIn("[reminder refresh]", save_and_verify)
        self.assertNotIn("ReminderTarget(reminder)", save_and_verify)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS EventKit")
    def test_native_alarm_validation_rejects_boolean_and_invalid_location_numbers(
        self,
    ) -> None:
        source = PLUGIN_ROOT / "scripts" / "reminders_eventkit.m"
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "alarm-validation"
            compiled = subprocess.run(
                [
                    "clang",
                    "-x",
                    "objective-c",
                    "-fobjc-arc",
                    "-DAPPLE_REMINDERS_ALARM_VALIDATION_TEST",
                    "-framework",
                    "Foundation",
                    "-framework",
                    "EventKit",
                    "-framework",
                    "CoreLocation",
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

            for offset in (False, True):
                with self.subTest(offset=offset):
                    completed = subprocess.run(
                        [str(binary)],
                        input=json.dumps(
                            {"kind": "relative", "offset_seconds": offset}
                        ),
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=10,
                        check=False,
                    )
                    payload = json.loads(completed.stdout)
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertEqual(payload["status"], "failed_no_mutation")
                    self.assertEqual(payload["error"]["code"], "invalid_input")
                    self.assertEqual(
                        payload["error"]["reason_code"], "invalid_alarm"
                    )

            location_base = {
                "kind": "location",
                "proximity": "enter",
                "location": {
                    "title": "Office",
                    "latitude": 37.5665,
                    "longitude": 126.978,
                    "radius_meters": 150,
                },
            }
            invalid_locations = (
                {"title": ""},
                {"title": " \t\n "},
                {"title": "L" * 1_001},
                {"latitude": True},
                {"longitude": False},
                {"latitude": None},
                {"latitude": []},
                {"latitude": {}},
                {"latitude": "37.5665"},
                {"longitude": None},
                {"latitude": 91},
                {"longitude": 181},
                {"radius_meters": True},
                {"radius_meters": 100_001},
                {"bogus": "must not be ignored"},
            )
            for changed in invalid_locations:
                with self.subTest(location_change=changed):
                    request = json.loads(json.dumps(location_base))
                    request["location"].update(changed)
                    completed = subprocess.run(
                        [str(binary)],
                        input=json.dumps(request),
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=10,
                        check=False,
                    )
                    payload = json.loads(completed.stdout)
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertEqual(payload["status"], "failed_no_mutation")
                    self.assertEqual(
                        payload["error"]["reason_code"],
                        "invalid_type" if changed == {"title": ""} else "invalid_alarm",
                    )

            fractional_absolute = subprocess.run(
                [str(binary)],
                input=json.dumps(
                    {
                        "kind": "absolute",
                        "date_time": "2027-01-15T08:00:00.123+09:00",
                    }
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.assertEqual(
                fractional_absolute.returncode,
                0,
                fractional_absolute.stderr,
            )
            self.assertEqual(
                json.loads(fractional_absolute.stdout)["data"]["alarm"],
                {
                    "kind": "absolute",
                    "date_time": "2027-01-14T23:00:00.123Z",
                },
            )
            overprecise_absolute = subprocess.run(
                [str(binary)],
                input=json.dumps(
                    {
                        "kind": "absolute",
                        "date_time": "2027-01-15T08:00:00.1234+09:00",
                    }
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(overprecise_absolute.returncode, 0)
            self.assertEqual(
                json.loads(overprecise_absolute.stdout)["error"]["reason_code"],
                "invalid_alarm",
            )

            accepted = subprocess.run(
                [str(binary)],
                input=json.dumps({"kind": "relative", "offset_seconds": 0}),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )

        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(json.loads(accepted.stdout)["status"], "verified")

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS EventKit")
    def test_native_alarm_json_exposes_only_faithful_writable_subset(self) -> None:
        source = PLUGIN_ROOT / "scripts" / "reminders_eventkit.m"
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "alarm-round-trip"
            compiled = subprocess.run(
                [
                    "clang",
                    "-x",
                    "objective-c",
                    "-fobjc-arc",
                    "-DAPPLE_REMINDERS_ALARM_ROUNDTRIP_TEST",
                    "-framework",
                    "Foundation",
                    "-framework",
                    "EventKit",
                    "-framework",
                    "CoreLocation",
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

        payload = json.loads(completed.stdout)
        fixtures = payload["fixtures"]
        for name, offset in (
            ("safe", -900),
            ("zero", 0),
            ("lower_bound", -31_536_000),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    fixtures[name],
                    {"kind": "relative", "offset_seconds": offset},
                )

        for name in ("positive", "fractional", "out_of_range"):
            with self.subTest(name=name):
                self.assertIs(fixtures[name]["read_only"], True)
                self.assertEqual(fixtures[name]["action"], {"type": "display"})

        for name in ("not_a_number", "infinite"):
            with self.subTest(name=name):
                self.assertIsNone(fixtures[name]["offset_seconds"])
                self.assertIs(fixtures[name]["read_only"], True)
                self.assertEqual(fixtures[name]["action"], {"type": "display"})
                self.assertIs(
                    fixtures[name]["_verification_unavailable"], True
                )

        self.assertEqual(
            fixtures["audio"]["action"],
            {"type": "audio", "sound_name": "Glass"},
        )
        self.assertEqual(
            fixtures["email"]["action"],
            {"type": "email", "email_address": "alerts@example.com"},
        )
        self.assertEqual(
            fixtures["procedure"]["action"],
            {"type": "procedure", "url": "example:run"},
        )
        self.assertEqual(
            fixtures["procedure_hidden_url"]["action"],
            {"type": "procedure"},
        )
        self.assertIs(fixtures["procedure_hidden_url"]["read_only"], True)
        self.assertIs(
            fixtures["procedure_hidden_url"]["_verification_unavailable"],
            True,
        )
        self.assertEqual(
            fixtures["display_with_sound"]["action"],
            {"type": "display", "sound_name": "Glass"},
        )
        for name in ("audio", "email", "procedure", "display_with_sound"):
            with self.subTest(name=name):
                self.assertIs(fixtures[name]["read_only"], True)

        self.assertEqual(
            fixtures["absolute_millisecond"],
            {
                "kind": "absolute",
                "date_time": "2027-01-15T08:00:00.123Z",
            },
        )
        self.assertEqual(
            fixtures["absolute_submillisecond"]["date_time"],
            "2027-01-15T08:00:00.123Z",
        )
        self.assertIs(fixtures["absolute_submillisecond"]["read_only"], True)
        self.assertIs(
            fixtures["absolute_submillisecond"]["_verification_unavailable"],
            True,
        )

        self.assertEqual(
            fixtures["location_missing_geo"]["location"],
            {
                "title": "",
                "latitude": None,
                "longitude": None,
            },
        )
        self.assertIs(fixtures["location_missing_geo"]["read_only"], True)
        self.assertIs(
            fixtures["location_missing_geo"]["_verification_unavailable"],
            True,
        )
        self.assertNotIn(
            "radius_meters", fixtures["location_infinite_radius"]["location"]
        )
        self.assertIs(
            fixtures["location_infinite_radius"]["read_only"], True
        )
        self.assertEqual(
            len(fixtures["location_overlong_title"]["location"]["title"]),
            1_001,
        )
        self.assertIs(fixtures["location_overlong_title"]["read_only"], True)
        self.assertIs(
            fixtures["location_whitespace_title"]["read_only"], True
        )
        self.assertIs(fixtures["dormant_location"]["read_only"], True)
        self.assertIs(
            fixtures["dormant_location"]["_verification_unavailable"], True
        )

        self.assertEqual(
            payload["safe_round_trip"],
            {"kind": "relative", "offset_seconds": -900},
        )
        self.assertIs(payload["unsafe_reinput_rejected"], True)
        self.assertIs(payload["nonempty_replacement_rejected"], True)
        self.assertIs(payload["empty_array_allowed"], True)
        self.assertIs(payload["null_clear_allowed"], True)
        self.assertIs(payload["nonfinite_drift_matched"], False)
        self.assertIs(payload["hidden_procedure_stable_matched"], False)
        self.assertIs(payload["clear_due_retaining_relative_rejected"], True)
        self.assertIs(
            payload["clear_due_retaining_dormant_location_rejected"], True
        )
        self.assertIs(payload["clear_due_replacing_absolute_allowed"], True)
        self.assertIs(payload["clear_due_and_null_alarms_allowed"], True)
        self.assertIs(payload["clear_due_and_empty_alarms_allowed"], True)
        self.assertIs(payload["add_relative_to_existing_due_allowed"], True)

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
    def test_runtime_trust_accepts_main_owned_workflow_provenance_shape(self) -> None:
        app_files = {
            (
                f"{eventkit_bridge.BUNDLED_HELPER_APP_NAME}/Contents/MacOS/"
                f"{eventkit_bridge.BUNDLED_HELPER_EXECUTABLE_NAME}"
            ): "e" * 64,
        }
        source_files = {
            relative: "a" * 64
            for relative in eventkit_bridge.BUNDLED_HELPER_SOURCE_RELATIVE_PATHS
        }
        build_inputs = {
            relative: "b" * 64
            for relative in eventkit_bridge.BUNDLED_HELPER_BUILD_INPUT_RELATIVE_PATHS
        }
        manifest = {
            "app_files": app_files,
            "app_name": eventkit_bridge.BUNDLED_HELPER_APP_NAME,
            "architectures": sorted(eventkit_bridge.BUNDLED_HELPER_ARCHITECTURES),
            "binary_sha256": "e" * 64,
            "build_environment": {
                "clang": "fixture clang",
                "linker": "fixture linker",
                "macos_sdk": "15.0",
                "xcode_path": "/Applications/Xcode.app/Contents/Developer",
            },
            "build_inputs": build_inputs,
            "bundle_identifier": eventkit_bridge.BUNDLED_HELPER_BUNDLE_IDENTIFIER,
            "executable": eventkit_bridge.BUNDLED_HELPER_EXECUTABLE_NAME,
            "minimum_macos": eventkit_bridge.BUNDLED_HELPER_MINIMUM_MACOS,
            "minimum_macos_by_architecture": {
                architecture: eventkit_bridge.BUNDLED_HELPER_MINIMUM_MACOS
                for architecture in sorted(eventkit_bridge.BUNDLED_HELPER_ARCHITECTURES)
            },
            "notarization_checked": True,
            "notarized": True,
            "plugin_version": "0.5.1",
            "schema_version": 1,
            "signature": "developer-id",
            "source_commit": "a" * 40,
            "source_files": source_files,
            "team_id": eventkit_bridge.BUNDLED_HELPER_TEAM_IDENTIFIER,
            "workflow_commit": "c" * 40,
        }
        raw = json.dumps(manifest, sort_keys=True).encode()

        with (
            mock.patch.object(eventkit_bridge, "_read_regular_file", return_value=raw),
            mock.patch.object(
                eventkit_bridge,
                "_sha256_regular_file",
                return_value="a" * 64,
            ),
        ):
            loaded, manifest_hash, loaded_source_files = (
                eventkit_bridge._load_bundled_helper_manifest(app_files, "0.5.1")
            )

        self.assertEqual(loaded, manifest)
        self.assertRegex(manifest_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(loaded_source_files, source_files)
        self.assertIn(
            ".github/workflows/prepare-signed-helper-source.yml",
            build_inputs,
        )
        self.assertNotIn(".github/workflows/prepare-signed-helper.yml", build_inputs)

    def test_relative_alarm_schema_describes_safe_writes_and_replace_all(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        relative = next(
            branch
            for branch in schema["$defs"]["alarm"]["oneOf"]
            if branch["properties"]["kind"].get("const") == "relative"
        )
        self.assertEqual(
            set(relative["properties"]),
            {"kind", "offset_seconds"},
        )
        self.assertIn("due anchor", relative["description"])
        self.assertIn("bare default-display form only", relative["description"])
        self.assertIn("action metadata", relative["description"])

        offset = relative["properties"]["offset_seconds"]
        self.assertEqual(offset["type"], "integer")
        self.assertEqual(offset["minimum"], -31_536_000)
        self.assertEqual(offset["maximum"], 0)
        self.assertIn("inclusive", offset["description"].lower())
        self.assertIn("-31,536,000 through 0", offset["description"])
        self.assertIn(
            "31,536,000 seconds (365 elapsed days)",
            offset["description"],
        )
        location_alarm = next(
            branch
            for branch in schema["$defs"]["alarm"]["oneOf"]
            if branch["properties"]["kind"].get("const") == "location"
        )
        self.assertEqual(
            location_alarm["properties"]["location"]["properties"]["title"][
                "maxLength"
            ],
            1_000,
        )

        create_alarms = schema["properties"]["alarms"]["description"]
        self.assertIn("complete alarm array", create_alarms.lower())
        self.assertIn("relative alarm requires due in the same create", create_alarms)

        patch_alarms = schema["properties"]["patch"]["properties"]["alarms"][
            "description"
        ]
        for phrase in (
            "complete-array replace-all",
            "Omission preserves",
            "null or [] explicitly clears",
            "alarm-only patch against existing state",
            "exact read",
            "due anchor",
            "complete current alarms",
            "read_only:true",
            "non-empty replacement is rejected before mutation",
            "explicit clear-all request",
            "resulting due remains non-null",
            "Setting due:null while retaining a relative alarm is rejected",
            "complete non-relative replacement",
        ):
            self.assertIn(phrase, patch_alarms)

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
                    self.assertEqual(
                        payload["data"]["alarm_write_contract"],
                        {
                            "alarms_replace_complete_array": True,
                            "omitted_alarms_are_preserved": True,
                            "null_or_empty_alarms_clear_all": True,
                            "relative_due_anchor_required": True,
                            "relative_default_display_action_only": True,
                            "relative_integer_offset_minimum_seconds": -31_536_000,
                            "relative_integer_offset_maximum_seconds": 0,
                            "unsupported_existing_alarms_are_read_only": True,
                        },
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


class EventKitFloatingDueTests(unittest.TestCase):
    due = {
        "kind": "timed",
        "floating": True,
        "local_date_time": "2026-09-08T09:30:00",
    }

    def invalid_cases(self):
        for field, value in (
            ("time_zone", "Asia/Seoul"),
            ("time_zone", None),
            ("date_time", "2026-09-08T09:30:00+09:00"),
            ("date_time", None),
            ("floating", False),
            ("floating", 1),
            ("floating", "true"),
        ):
            yield f"mixed or invalid {field}={value}", {**self.due, field: value}
        for value in (
            "2026-09-08T09:30:00Z",
            "2026-09-08T09:30:00+09:00",
            "2026-09-08T09:30:00.123",
            "2026-09-08T09:30",
            "2026-02-29T09:30:00",
            "2026-09-08T24:00:00",
            "2026-09-08T09:60:00",
            "2026-09-08T09:30:60",
            "2026-09-08T09:30:00\n",
        ):
            yield value, {**self.due, "local_date_time": value}
        for field in ("floating", "local_date_time"):
            yield f"missing {field}", {
                key: value for key, value in self.due.items() if key != field
            }

    def test_create_and_patch_preserve_explicit_floating_intent(self) -> None:
        for operation in ("create_reminder", "update_reminder"):
            with self.subTest(operation=operation):
                raw = {"schema_version": 1, "operation": operation}
                fields = {
                    "due": self.due,
                    "alarms": [{"kind": "relative", "offset_seconds": -600}],
                }
                if operation == "create_reminder":
                    raw.update({"calendar_id": "CALENDAR-1", "title": "Local task", **fields})
                else:
                    raw.update({
                        "reminder_id": "REMINDER-1",
                        "expected_last_modified": "2026-09-05T00:00:00Z",
                        "patch": fields,
                    })
                normalized = eventkit_bridge.normalize_request(raw)
                actual = normalized if operation == "create_reminder" else normalized["patch"]
                self.assertEqual(actual["due"], self.due)
                self.assertEqual(actual["alarms"], fields["alarms"])

    def test_invalid_floating_shapes_fail_before_dispatch(self) -> None:
        for name, due in self.invalid_cases():
            with self.subTest(name=name):
                raw = {
                    "schema_version": 1,
                    "operation": "create_reminder",
                    "calendar_id": "CALENDAR-1",
                    "title": "Invalid local time",
                    "due": due,
                }
                with (
                    mock.patch.object(eventkit_bridge, "read_request", return_value=raw),
                    mock.patch.object(eventkit_bridge, "invoke_native") as invoke_native,
                    mock.patch.object(eventkit_bridge, "emit") as emit,
                ):
                    exit_code = eventkit_bridge.main([])
                invoke_native.assert_not_called()
                self.assertEqual(exit_code, 2)
                self.assertEqual(emit.call_args.args[0]["error"]["code"], "invalid_input")

    def test_public_projection_checks_floating_intent_and_exact_wall_time(self) -> None:
        actual = {**self.due, "date_time": None, "time_zone": None}
        self.assertTrue(reminder_matches_fields({"due": actual}, {"due": self.due}))
        for changed in (
            {**actual, "local_date_time": "2026-09-08T09:20:00"},
            {**actual, "floating": False},
            {**actual, "time_zone": "Asia/Seoul"},
            {**actual, "date_time": "2026-09-08T09:30:00+09:00"},
            {
                "kind": "timed",
                "date_time": "2026-09-08T09:30:00+09:00",
                "time_zone": "Asia/Seoul",
            },
        ):
            self.assertFalse(reminder_matches_fields({"due": changed}, {"due": self.due}))
        zoned = {
            "kind": "timed",
            "date_time": "2026-09-08T09:30:00+09:00",
            "time_zone": "Asia/Seoul",
        }
        self.assertFalse(reminder_matches_fields({"due": actual}, {"due": zoned}))

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS Foundation")
    def test_native_floating_components_round_trip_without_event_store(self) -> None:
        source = PLUGIN_ROOT / "scripts" / "reminders_eventkit.m"
        valid = [
            self.due,
            {**self.due, "local_date_time": "2028-02-29T23:59:59"},
            {**self.due, "local_date_time": "2026-03-08T02:30:00"},
            {**self.due, "local_date_time": "2026-11-01T01:30:00"},
        ]
        invalid = list(self.invalid_cases())
        with tempfile.TemporaryDirectory() as temporary:
            wrapper = Path(temporary) / "floating-components.m"
            binary = Path(temporary) / "floating-components"
            wrapper.write_text(
                f'''\
#define main AppleRemindersProductionMain
#include "{source.as_posix()}"
#undef main
int main(void) {{
    @autoreleasepool {{
        NSData *input = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
        NSArray *cases = [NSJSONSerialization JSONObjectWithData:input options:0 error:nil];
        NSMutableArray *results = [NSMutableArray array];
        for (NSDictionary *due in cases) {{
            @try {{
                NSDateComponents *components = ComponentsFromDueJSON(due);
                [results addObject:@{{
                    @"due" : DateComponentsJSON(components),
                    @"has_time_zone" : components.timeZone != nil ? @YES : @NO,
                    @"gregorian" : @([components.calendar.calendarIdentifier
                        isEqual:NSCalendarIdentifierGregorian]),
                    @"canonical" : CanonicalDueVerificationValue(due),
                }}];
            }} @catch (NSException *exception) {{
                [results addObject:@{{ @"error" : exception.userInfo[@"code"] ?: exception.name }}];
            }}
        }}
        NSData *output = [NSJSONSerialization dataWithJSONObject:results options:0 error:nil];
        [[NSFileHandle fileHandleWithStandardOutput] writeData:output];
        return 0;
    }}
}}
''',
                encoding="utf-8",
            )
            compiled = subprocess.run(
                [
                    "clang", "-x", "objective-c", "-fobjc-arc",
                    "-framework", "Foundation", "-framework", "EventKit",
                    "-framework", "CoreLocation", str(wrapper), "-o", str(binary),
                ],
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            completed = subprocess.run(
                [str(binary)], input=json.dumps(valid + [due for _, due in invalid]),
                capture_output=True, text=True, timeout=10, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
        results = json.loads(completed.stdout)
        for expected, result in zip(valid, results):
            with self.subTest(expected=expected):
                self.assertEqual(result["due"], {**expected, "date_time": None, "time_zone": None})
                self.assertEqual(result["canonical"], result["due"])
                self.assertIs(result["has_time_zone"], False)
                self.assertIs(result["gregorian"], True)
        for (name, _), result in zip(invalid, results[len(valid):]):
            with self.subTest(name=name):
                self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
