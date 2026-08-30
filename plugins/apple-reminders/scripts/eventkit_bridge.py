#!/usr/bin/env python3
"""Strict JSON-in/JSON-out launcher for the native Reminders EventKit bridge.

The Python layer owns request validation and helper trust checks. The signed,
notarized Objective-C helper owns EventKit access. ``--validate-only`` never
launches EventKit, and source compilation is an explicit contributor-only path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import stat
import struct
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from eventkit_protocol import (  # noqa: E402
    EXIT_CODES,
    MUTATION_OPERATIONS,
    SCHEMA_VERSION,
    STABLE_ERROR_CODES,
    mutation_outcome_unknown_response,
    validate_mutation_receipt,
    validate_response,
)
from bounded_process import (  # noqa: E402
    ProcessDecodeError,
    ProcessError,
    ProcessLaunchError,
    ProcessOutputLimitError,
    ProcessTimeoutError,
    run as run_bounded_process,
)


SOURCE_PATH = SCRIPT_DIR / "reminders_eventkit.m"
INFO_PLIST_PATH = SCRIPT_DIR / "eventkit_bridge_info.plist"
SCHEMA_PATH = SCRIPT_DIR / "eventkit_bridge_schema.json"
PLUGIN_ROOT = SCRIPT_DIR.parent
PLUGIN_MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
BUNDLED_HELPER_NATIVE_DIR = PLUGIN_ROOT / "native"
BUNDLED_HELPER_APP_NAME = "AppleRemindersEventKitHelper.app"
BUNDLED_HELPER_EXECUTABLE_NAME = "apple-reminders-eventkit-helper"
BUNDLED_HELPER_MANIFEST_NAME = "eventkit-helper-build.json"
BUNDLED_HELPER_APP = BUNDLED_HELPER_NATIVE_DIR / BUNDLED_HELPER_APP_NAME
BUNDLED_HELPER_PATH = (
    BUNDLED_HELPER_APP / "Contents" / "MacOS" / BUNDLED_HELPER_EXECUTABLE_NAME
)
BUNDLED_HELPER_MANIFEST_PATH = (
    BUNDLED_HELPER_NATIVE_DIR / BUNDLED_HELPER_MANIFEST_NAME
)
DEFAULT_CACHE_ROOT = Path.home() / "Library/Caches/apple-reminders-codex/eventkit-bridge"
MAX_REQUEST_BYTES = 1_000_000
MAX_NOTES_CHARS = 100_000
NATIVE_TIMEOUT_SECONDS = 70
LEGACY_HELPER_BUNDLE_IDENTIFIER = "com.codex.apple-reminders.eventkit-bridge"
BUNDLED_HELPER_BUNDLE_IDENTIFIER = (
    "io.github.oscar-v4.apple-reminders.eventkit-bridge"
)
BUNDLED_HELPER_TEAM_IDENTIFIER = "V8347N9346"
BUNDLED_HELPER_MINIMUM_MACOS = "14.0"
BUNDLED_HELPER_ARCHITECTURES = frozenset(("arm64", "x86_64"))
BUNDLED_HELPER_DESIGNATED_REQUIREMENT = (
    'anchor apple generic and identifier "'
    + BUNDLED_HELPER_BUNDLE_IDENTIFIER
    + '" and certificate 1[field.1.2.840.113635.100.6.2.6] exists'
    + ' and certificate leaf[subject.OU] = "'
    + BUNDLED_HELPER_TEAM_IDENTIFIER
    + '" and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
)
MAX_NATIVE_STDOUT_BYTES = 2_000_000
MAX_NATIVE_STDERR_BYTES = 256 * 1024
MAX_COMPILER_IDENTITY_STDOUT_BYTES = 64 * 1024
MAX_COMPILER_IDENTITY_STDERR_BYTES = 64 * 1024
MAX_BUILD_STDOUT_BYTES = 256 * 1024
MAX_BUILD_STDERR_BYTES = 1024 * 1024
MAX_HELPER_VERIFY_STDOUT_BYTES = 256 * 1024
MAX_HELPER_VERIFY_STDERR_BYTES = 256 * 1024
MAX_BUNDLED_HELPER_FILE_BYTES = 16 * 1024 * 1024

FAT_MAGIC_FORMATS = {
    b"\xca\xfe\xba\xbe": (">", False),
    b"\xbe\xba\xfe\xca": ("<", False),
    b"\xca\xfe\xba\xbf": (">", True),
    b"\xbf\xba\xfe\xca": ("<", True),
}
MACH_HEADER_64_SIZE = 32
MACH_MAGIC_64_LITTLE_ENDIAN = 0xFEEDFACF
MACH_FILETYPE_EXECUTE = 0x2
CPU_TYPE_X86_64 = 0x01000007
CPU_SUBTYPE_X86_64_ALL = 0x3
CPU_TYPE_ARM64 = 0x0100000C
CPU_SUBTYPE_ARM64_ALL = 0x0
CPU_SUBTYPE_CAPABILITY_MASK = 0xFF000000
CPU_SUBTYPE_BASE_MASK = 0x00FFFFFF
EXPECTED_MACH_CPU_SUBTYPES = {
    CPU_TYPE_X86_64: CPU_SUBTYPE_X86_64_ALL,
    CPU_TYPE_ARM64: CPU_SUBTYPE_ARM64_ALL,
}
LC_VERSION_MIN_MACOSX = 0x24
LC_BUILD_VERSION = 0x32
PLATFORM_MACOS = 0x1
MACOS_14_0_0_PACKED_VERSION = 14 << 16
MAX_MACH_LOAD_COMMANDS = 4096

BUNDLED_HELPER_EXPECTED_DIRECTORIES = frozenset(
    (
        "Contents",
        "Contents/MacOS",
        "Contents/_CodeSignature",
    )
)
BUNDLED_HELPER_EXPECTED_FILE_MODES = {
    "Contents/CodeResources": 0o644,
    "Contents/Info.plist": 0o644,
    f"Contents/MacOS/{BUNDLED_HELPER_EXECUTABLE_NAME}": 0o755,
    "Contents/_CodeSignature/CodeResources": 0o644,
}
BUNDLED_HELPER_SOURCE_RELATIVE_PATHS = (
    ".codex-plugin/plugin.json",
    "scripts/eventkit_bridge_schema.json",
    "scripts/reminders_eventkit.m",
)
BUNDLED_HELPER_BUILD_INPUT_RELATIVE_PATHS = frozenset(
    (
        ".github/workflows/prepare-signed-helper-source.yml",
        "scripts/build_eventkit_helper_app.py",
        "scripts/eventkit_helper_app_info.plist",
        "scripts/prepare_signed_eventkit_helper.sh",
        "scripts/verify_eventkit_helper.py",
    )
)
BUNDLED_HELPER_BUILD_ENVIRONMENT_KEYS = frozenset(
    ("clang", "linker", "macos_sdk", "xcode_path")
)
BUNDLED_HELPER_MANIFEST_KEYS = frozenset(
    (
        "app_files",
        "app_name",
        "architectures",
        "binary_sha256",
        "build_environment",
        "build_inputs",
        "bundle_identifier",
        "executable",
        "minimum_macos",
        "minimum_macos_by_architecture",
        "notarization_checked",
        "notarized",
        "plugin_version",
        "schema_version",
        "signature",
        "source_commit",
        "source_files",
        "team_id",
        "workflow_commit",
    )
)

# Content-addressed caching skips expensive system trust checks only while every
# reviewed app byte and the provenance manifest remain identical. Failures are
# never cached.
_verified_bundled_helper_fingerprint: tuple[str, ...] | None = None

OPERATIONS = {
    "schema",
    "capabilities",
    "doctor",
    "request_access",
    "list_accounts",
    "list_calendars",
    "ensure_reminder_list",
    "fetch_reminders",
    "read_reminder",
    "create_reminder",
    "update_reminder",
    "complete_reminder",
    "reopen_reminder",
    "move_reminder",
    "delete_reminder",
}

RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BridgeValidationError(ValueError):
    """A request error that maps directly to the public response envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: str = "invalid_request",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


class BundledHelperUnavailable(RuntimeError):
    """The reviewed release helper is absent, unsafe, or no longer trusted."""


def response(
    operation: str | None,
    status: str,
    *,
    data: Any | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "operation": operation,
        "status": status,
        "ok": status != "failed_no_mutation",
    }
    if data is not None:
        payload["data"] = data
    if error is not None:
        payload["error"] = error
    return payload


def validation_error_response(
    operation: str | None, exc: BridgeValidationError
) -> dict[str, Any]:
    if exc.code in STABLE_ERROR_CODES:
        stable_code = exc.code
    elif exc.code == "unsupported_schema_version":
        stable_code = "schema_mismatch"
    elif exc.code == "unbounded_read":
        stable_code = "ambiguous_scope"
    elif exc.status == "unsupported":
        stable_code = "unsupported_capability"
    else:
        stable_code = "invalid_input"
    return response(
        operation,
        "failed_no_mutation",
        error={
            "code": stable_code,
            "reason_code": exc.code,
            "message": exc.message,
            "category": exc.status,
            "retryable": False,
            "details": exc.details,
        },
    )


def fail(
    code: str,
    message: str,
    *,
    status: str = "invalid_request",
    details: dict[str, Any] | None = None,
) -> NoReturn:
    raise BridgeValidationError(code, message, status=status, details=details)


def require_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail("invalid_type", f"{path} must be an object", details={"path": path})
    return value


def reject_unknown(obj: dict[str, Any], allowed: set[str], path: str = "$") -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        fail(
            "unknown_fields",
            f"{path} contains unsupported fields: {', '.join(unknown)}",
            details={"path": path, "fields": unknown},
        )


def require_fields(obj: dict[str, Any], fields: set[str], path: str = "$") -> None:
    missing = sorted(field for field in fields if field not in obj)
    if missing:
        fail(
            "missing_fields",
            f"{path} is missing required fields: {', '.join(missing)}",
            details={"path": path, "fields": missing},
        )


def normalized_string(
    value: Any,
    path: str,
    *,
    allow_empty: bool = False,
    maximum: int | None = None,
) -> str:
    if not isinstance(value, str):
        fail("invalid_type", f"{path} must be a string", details={"path": path})
    if not allow_empty and not value.strip():
        fail("empty_string", f"{path} must not be empty", details={"path": path})
    if maximum is not None and len(value) > maximum:
        fail(
            "string_too_long",
            f"{path} exceeds the {maximum}-character limit",
            details={"path": path, "maximum": maximum},
        )
    return value


def normalized_reminder_identifier(value: Any, path: str) -> str:
    identifier = normalized_string(value, path, maximum=2_048)
    if identifier.strip().lower().startswith("x-apple-reminder://"):
        fail(
            "invalid_identifier",
            f"{path} must be the exact opaque ID returned by a reminder read, not a Reminders deep link",
            details={"path": path, "expected": "opaque_reminder_id"},
        )
    return identifier


def normalized_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        fail("invalid_type", f"{path} must be a boolean", details={"path": path})
    return value


def normalized_int(value: Any, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail("invalid_type", f"{path} must be an integer", details={"path": path})
    if not minimum <= value <= maximum:
        fail(
            "out_of_range",
            f"{path} must be between {minimum} and {maximum}",
            details={"path": path, "minimum": minimum, "maximum": maximum},
        )
    return value


def normalized_number(
    value: Any, path: str, minimum: float, maximum: float, *, exclusive_minimum: bool = False
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail("invalid_type", f"{path} must be a number", details={"path": path})
    number = float(value)
    lower_ok = number > minimum if exclusive_minimum else number >= minimum
    if not lower_ok or number > maximum:
        fail(
            "out_of_range",
            f"{path} must be in the supported range",
            details={"path": path, "minimum": minimum, "maximum": maximum},
        )
    return number


def parse_rfc3339(value: Any, path: str) -> tuple[str, datetime]:
    text = normalized_string(value, path)
    if not RFC3339_RE.fullmatch(text):
        fail(
            "invalid_rfc3339",
            f"{path} must be RFC 3339 with Z or an explicit UTC offset",
            details={"path": path},
        )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            fail("missing_utc_offset", f"{path} must include a UTC offset", details={"path": path})
        canonical = parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
    except (OverflowError, ValueError):
        fail("invalid_rfc3339", f"{path} is not a valid timestamp", details={"path": path})
    return canonical, parsed


def parse_date(value: Any, path: str) -> str:
    text = normalized_string(value, path)
    if not DATE_RE.fullmatch(text):
        fail("invalid_date", f"{path} must use YYYY-MM-DD", details={"path": path})
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        fail("invalid_date", f"{path} is not a valid calendar date", details={"path": path})
    return parsed.isoformat()


def normalized_url(value: Any, path: str) -> str | None:
    if value is None:
        return None
    text = normalized_string(value, path, maximum=100_000)
    try:
        parsed = urlparse(text)
    except ValueError:
        fail("invalid_url", f"{path} must be a valid URL", details={"path": path})
    if not parsed.scheme:
        fail("invalid_url", f"{path} must include a URL scheme", details={"path": path})
    return text


def normalized_string_list(
    value: Any, path: str, *, maximum: int, unique: bool = True
) -> list[str]:
    if not isinstance(value, list):
        fail("invalid_type", f"{path} must be an array", details={"path": path})
    if not value:
        fail("empty_array", f"{path} must not be empty", details={"path": path})
    if len(value) > maximum:
        fail("array_too_long", f"{path} supports at most {maximum} items", details={"path": path})
    result = [normalized_string(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if unique and len(set(result)) != len(result):
        fail("duplicate_values", f"{path} must not contain duplicates", details={"path": path})
    return result


def normalize_due(value: Any, path: str) -> dict[str, Any] | None:
    if value is None:
        return None
    due = require_dict(value, path)
    kind = due.get("kind")
    if kind == "all_day":
        reject_unknown(due, {"kind", "date"}, path)
        require_fields(due, {"kind", "date"}, path)
        return {"kind": "all_day", "date": parse_date(due["date"], f"{path}.date")}
    if kind == "timed":
        reject_unknown(due, {"kind", "date_time", "time_zone"}, path)
        require_fields(due, {"kind", "date_time", "time_zone"}, path)
        canonical, parsed = parse_rfc3339(due["date_time"], f"{path}.date_time")
        zone_name = normalized_string(due["time_zone"], f"{path}.time_zone")
        try:
            zone = ZoneInfo(zone_name)
        except (ValueError, ZoneInfoNotFoundError):
            fail(
                "invalid_time_zone",
                f"{path}.time_zone must be a known IANA time-zone name",
                details={"path": f"{path}.time_zone", "value": zone_name},
            )
        expected_offset = parsed.astimezone(zone).utcoffset()
        if expected_offset != parsed.utcoffset():
            fail(
                "time_zone_offset_mismatch",
                f"{path}.date_time offset does not match {zone_name} at that instant",
                details={"path": path, "time_zone": zone_name},
            )
        local = parsed.astimezone(zone)
        return {
            "kind": "timed",
            "date_time": local.isoformat(timespec="milliseconds"),
            "time_zone": zone_name,
        }
    if kind is None:
        fail("missing_fields", f"{path} is missing required field: kind", details={"path": path})
    fail(
        "unsupported_due_kind",
        f"{path}.kind must be all_day or timed",
        status="unsupported",
        details={"path": f"{path}.kind", "value": kind},
    )


def normalize_alarm(value: Any, path: str) -> dict[str, Any]:
    alarm = require_dict(value, path)
    kind = alarm.get("kind")
    if kind == "absolute":
        reject_unknown(alarm, {"kind", "date_time"}, path)
        require_fields(alarm, {"kind", "date_time"}, path)
        date_time, _ = parse_rfc3339(alarm["date_time"], f"{path}.date_time")
        return {"kind": "absolute", "date_time": date_time}
    if kind == "location":
        reject_unknown(alarm, {"kind", "proximity", "location"}, path)
        require_fields(alarm, {"kind", "proximity", "location"}, path)
        proximity = alarm["proximity"]
        if proximity not in {"enter", "leave"}:
            fail(
                "invalid_enum",
                f"{path}.proximity must be enter or leave",
                details={"path": f"{path}.proximity"},
            )
        location = require_dict(alarm["location"], f"{path}.location")
        reject_unknown(
            location,
            {"title", "latitude", "longitude", "radius_meters"},
            f"{path}.location",
        )
        require_fields(location, {"title", "latitude", "longitude"}, f"{path}.location")
        result_location: dict[str, Any] = {
            "title": normalized_string(location["title"], f"{path}.location.title", maximum=1000),
            "latitude": normalized_number(
                location["latitude"], f"{path}.location.latitude", -90, 90
            ),
            "longitude": normalized_number(
                location["longitude"], f"{path}.location.longitude", -180, 180
            ),
        }
        if "radius_meters" in location:
            result_location["radius_meters"] = normalized_number(
                location["radius_meters"],
                f"{path}.location.radius_meters",
                0,
                100_000,
                exclusive_minimum=True,
            )
        return {"kind": "location", "proximity": proximity, "location": result_location}
    if kind == "relative":
        reject_unknown(alarm, {"kind", "offset_seconds"}, path)
        require_fields(alarm, {"kind", "offset_seconds"}, path)
        offset_seconds = normalized_int(
            alarm["offset_seconds"],
            f"{path}.offset_seconds",
            -31_536_000,
            0,
        )
        return {"kind": "relative", "offset_seconds": offset_seconds}
    if kind is None:
        fail("missing_fields", f"{path} is missing required field: kind", details={"path": path})
    fail(
        "unsupported_alarm_kind",
        f"{path}.kind must be absolute, relative, or location",
        status="unsupported",
        details={"path": f"{path}.kind", "value": kind},
    )


def normalize_alarms(value: Any, path: str) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        fail("invalid_type", f"{path} must be an array or null", details={"path": path})
    if len(value) > 20:
        fail("array_too_long", f"{path} supports at most 20 alarms", details={"path": path})
    return [normalize_alarm(item, f"{path}[{index}]") for index, item in enumerate(value)]


def normalize_int_array(
    value: Any,
    path: str,
    *,
    minimum: int,
    maximum: int,
    max_items: int,
    exclude_zero: bool = False,
) -> list[int]:
    if not isinstance(value, list):
        fail("invalid_type", f"{path} must be an array", details={"path": path})
    if not value:
        fail("empty_array", f"{path} must be omitted instead of empty", details={"path": path})
    if len(value) > max_items:
        fail("array_too_long", f"{path} supports at most {max_items} items", details={"path": path})
    result: list[int] = []
    for index, item in enumerate(value):
        number = normalized_int(item, f"{path}[{index}]", minimum, maximum)
        if exclude_zero and number == 0:
            fail("out_of_range", f"{path}[{index}] must not be zero", details={"path": path})
        result.append(number)
    if len(set(result)) != len(result):
        fail("duplicate_values", f"{path} must not contain duplicates", details={"path": path})
    return result


WEEKDAYS = {"sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"}


def normalize_recurrence(value: Any, path: str) -> dict[str, Any]:
    rule = require_dict(value, path)
    allowed = {
        "frequency",
        "interval",
        "days_of_week",
        "days_of_month",
        "months_of_year",
        "weeks_of_year",
        "days_of_year",
        "set_positions",
        "end",
    }
    reject_unknown(rule, allowed, path)
    require_fields(rule, {"frequency", "interval"}, path)
    frequency = rule["frequency"]
    if frequency not in {"daily", "weekly", "monthly", "yearly"}:
        fail("invalid_enum", f"{path}.frequency is unsupported", details={"path": f"{path}.frequency"})
    result: dict[str, Any] = {
        "frequency": frequency,
        "interval": normalized_int(rule["interval"], f"{path}.interval", 1, 999),
    }
    if "days_of_week" in rule:
        raw_days = rule["days_of_week"]
        if not isinstance(raw_days, list):
            fail("invalid_type", f"{path}.days_of_week must be an array", details={"path": path})
        if not raw_days:
            fail(
                "empty_array",
                f"{path}.days_of_week must be omitted instead of empty",
                details={"path": path},
            )
        if len(raw_days) > 7:
            fail("array_too_long", f"{path}.days_of_week supports at most 7 items", details={"path": path})
        days: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for index, raw_day in enumerate(raw_days):
            day_path = f"{path}.days_of_week[{index}]"
            day_obj = require_dict(raw_day, day_path)
            reject_unknown(day_obj, {"day", "ordinal"}, day_path)
            require_fields(day_obj, {"day"}, day_path)
            day = day_obj["day"]
            if day not in WEEKDAYS:
                fail("invalid_enum", f"{day_path}.day is unsupported", details={"path": f"{day_path}.day"})
            normalized_day: dict[str, Any] = {"day": day}
            ordinal = 0
            if "ordinal" in day_obj:
                ordinal = normalized_int(day_obj["ordinal"], f"{day_path}.ordinal", -53, 53)
                if ordinal == 0:
                    fail("out_of_range", f"{day_path}.ordinal must not be zero", details={"path": day_path})
                normalized_day["ordinal"] = ordinal
            if (day, ordinal) in seen:
                fail("duplicate_values", f"{path}.days_of_week contains a duplicate", details={"path": path})
            seen.add((day, ordinal))
            days.append(normalized_day)
        result["days_of_week"] = days
    numeric_arrays = {
        "days_of_month": (-31, 31, 62),
        "months_of_year": (1, 12, 12),
        "weeks_of_year": (-53, 53, 106),
        "days_of_year": (-366, 366, 732),
        "set_positions": (-366, 366, 732),
    }
    for key, (minimum, maximum, max_items) in numeric_arrays.items():
        if key in rule:
            result[key] = normalize_int_array(
                rule[key],
                f"{path}.{key}",
                minimum=minimum,
                maximum=maximum,
                max_items=max_items,
                exclude_zero=key != "months_of_year",
            )
    if "end" in rule:
        end = require_dict(rule["end"], f"{path}.end")
        kind = end.get("kind")
        if kind == "count":
            reject_unknown(end, {"kind", "count"}, f"{path}.end")
            require_fields(end, {"kind", "count"}, f"{path}.end")
            result["end"] = {
                "kind": "count",
                "count": normalized_int(end["count"], f"{path}.end.count", 1, 10_000),
            }
        elif kind == "date":
            reject_unknown(end, {"kind", "date_time"}, f"{path}.end")
            require_fields(end, {"kind", "date_time"}, f"{path}.end")
            end_date, _ = parse_rfc3339(end["date_time"], f"{path}.end.date_time")
            result["end"] = {"kind": "date", "date_time": end_date}
        else:
            fail("invalid_enum", f"{path}.end.kind must be count or date", details={"path": path})

    filters = set(result) - {"frequency", "interval", "end"}
    if frequency == "daily" and filters:
        fail(
            "invalid_recurrence_combination",
            "Daily recurrence does not accept BY* filters because EventKit would silently ignore them",
            details={"path": path, "fields": sorted(filters)},
        )
    if frequency == "weekly":
        unsupported = filters - {"days_of_week"}
        if unsupported:
            fail(
                "invalid_recurrence_combination",
                "Weekly recurrence only supports days_of_week",
                details={"path": path, "fields": sorted(unsupported)},
            )
        if any(day.get("ordinal") for day in result.get("days_of_week", [])):
            fail(
                "invalid_recurrence_combination",
                "Weekly days_of_week cannot have ordinals",
                details={"path": path},
            )
    if frequency == "monthly":
        unsupported = filters - {"days_of_week", "days_of_month", "set_positions"}
        if unsupported:
            fail(
                "invalid_recurrence_combination",
                "Monthly recurrence only supports days_of_week, days_of_month, and set_positions",
                details={"path": path, "fields": sorted(unsupported)},
            )
    if frequency == "yearly":
        unsupported = filters - {
            "days_of_week",
            "months_of_year",
            "weeks_of_year",
            "days_of_year",
            "set_positions",
        }
        if unsupported:
            fail(
                "invalid_recurrence_combination",
                "Yearly recurrence contains filters EventKit would ignore",
                details={"path": path, "fields": sorted(unsupported)},
            )
    if "set_positions" in result and not (
        filters - {"set_positions"}
    ):
        fail(
            "invalid_recurrence_combination",
            "set_positions requires at least one BY* filter",
            details={"path": path},
        )
    return result


def normalize_recurrence_rules(value: Any, path: str) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        fail("invalid_type", f"{path} must be an array or null", details={"path": path})
    if len(value) > 1:
        fail(
            "unsupported_multiple_recurrence_rules",
            "The bridge supports at most one recurrence rule for a reminder",
            status="unsupported",
            details={"path": path},
        )
    return [normalize_recurrence(item, f"{path}[{index}]") for index, item in enumerate(value)]


PATCH_FIELDS = {"title", "notes", "url", "priority", "due", "alarms", "recurrence_rules"}


def normalize_mutable_fields(obj: dict[str, Any], *, path: str, create: bool) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if "title" in obj:
        result["title"] = normalized_string(obj["title"], f"{path}.title", maximum=10_000)
    elif create:
        fail("missing_fields", f"{path} is missing required field: title", details={"path": path})
    for key in ("notes",):
        if key in obj:
            value = obj[key]
            result[key] = None if value is None else normalized_string(
                value, f"{path}.{key}", allow_empty=True, maximum=MAX_NOTES_CHARS
            )
    if "url" in obj:
        result["url"] = normalized_url(obj["url"], f"{path}.url")
    if "priority" in obj:
        result["priority"] = normalized_int(obj["priority"], f"{path}.priority", 0, 9)
    if "due" in obj:
        result["due"] = normalize_due(obj["due"], f"{path}.due")
    if "alarms" in obj:
        result["alarms"] = normalize_alarms(obj["alarms"], f"{path}.alarms")
    if "recurrence_rules" in obj:
        result["recurrence_rules"] = normalize_recurrence_rules(
            obj["recurrence_rules"], f"{path}.recurrence_rules"
        )
    recurrence = result.get("recurrence_rules")
    alarms = result.get("alarms")
    has_relative_alarm = bool(
        alarms
        and any(alarm.get("kind") == "relative" for alarm in alarms)
    )
    if has_relative_alarm:
        if create and result.get("due") is None:
            fail(
                "relative_alarm_requires_due",
                "A relative alarm requires a typed due date to anchor its offset",
                details={"path": path},
            )
        if "due" in result and result["due"] is None:
            fail(
                "relative_alarm_requires_due",
                "Cannot set a relative alarm while clearing the due date",
                details={"path": path},
            )
    if recurrence:
        if create and result.get("due") is None:
            fail(
                "recurrence_requires_due",
                "A recurring reminder requires a typed due date to anchor the recurrence",
                details={"path": path},
            )
        if "due" in result and result["due"] is None:
            fail(
                "recurrence_requires_due",
                "Cannot set recurrence while clearing the due date",
                details={"path": path},
            )
    return result


def normalize_expected_last_modified(value: Any, path: str) -> str:
    if value is None:
        fail(
            "invalid_type",
            f"{path} must be an RFC 3339 string",
            details={"path": path},
        )
    canonical, _ = parse_rfc3339(value, path)
    return canonical


COMMON = {"schema_version", "operation"}


def normalize_request(raw: Any) -> dict[str, Any]:
    request = require_dict(raw, "$")
    operation_value = request.get("operation")
    operation = operation_value if isinstance(operation_value, str) else None
    require_fields(request, {"schema_version", "operation"})
    if isinstance(request["schema_version"], bool) or request["schema_version"] != SCHEMA_VERSION:
        fail(
            "unsupported_schema_version",
            f"schema_version must be {SCHEMA_VERSION}",
            status="unsupported",
            details={"supported": [SCHEMA_VERSION]},
        )
    if not isinstance(operation_value, str) or operation_value not in OPERATIONS:
        fail(
            "unsupported_operation",
            "operation is not supported",
            status="unsupported",
            details={"operation": operation_value, "supported": sorted(OPERATIONS)},
        )
    normalized: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "operation": operation}

    if operation in {"schema", "capabilities", "doctor", "request_access", "list_accounts"}:
        reject_unknown(request, COMMON)
        return normalized
    if operation == "list_calendars":
        reject_unknown(request, COMMON | {"source_id", "writable_only"})
        if "source_id" in request:
            normalized["source_id"] = normalized_string(request["source_id"], "$.source_id")
        if "writable_only" in request:
            normalized["writable_only"] = normalized_bool(request["writable_only"], "$.writable_only")
        return normalized
    if operation == "ensure_reminder_list":
        reject_unknown(request, COMMON | {"source_id", "name"})
        require_fields(request, {"source_id", "name"})
        normalized["source_id"] = normalized_string(
            request["source_id"], "$.source_id", maximum=2_048
        )
        normalized["name"] = normalized_string(
            request["name"], "$.name", maximum=512
        ).strip()
        return normalized
    if operation == "read_reminder":
        reject_unknown(request, COMMON | {"reminder_id"})
        require_fields(request, {"reminder_id"})
        normalized["reminder_id"] = normalized_reminder_identifier(
            request["reminder_id"], "$.reminder_id"
        )
        return normalized
    if operation == "fetch_reminders":
        allowed = COMMON | {
            "calendar_ids",
            "status",
            "query",
            "due_start",
            "due_end",
            "completion_start",
            "completion_end",
            "modified_after",
            "limit",
            "offset",
            "sort",
        }
        reject_unknown(request, allowed)
        require_fields(request, {"limit"})
        if "calendar_ids" in request:
            normalized["calendar_ids"] = normalized_string_list(
                request["calendar_ids"], "$.calendar_ids", maximum=100
            )
        status = request.get("status", "incomplete")
        if status not in {"incomplete", "completed"}:
            fail(
                "invalid_enum",
                "$.status must be incomplete or completed",
                details={"path": "$.status"},
            )
        normalized["status"] = status
        if "query" in request:
            normalized["query"] = normalized_string(request["query"], "$.query", maximum=500)
        parsed_ranges: dict[str, datetime] = {}
        for key in (
            "due_start",
            "due_end",
            "completion_start",
            "completion_end",
            "modified_after",
        ):
            if key in request:
                canonical, parsed = parse_rfc3339(request[key], f"$.{key}")
                normalized[key] = canonical
                parsed_ranges[key] = parsed
        for start_key, end_key in (("due_start", "due_end"), ("completion_start", "completion_end")):
            if start_key in parsed_ranges and end_key in parsed_ranges:
                if parsed_ranges[start_key] >= parsed_ranges[end_key]:
                    fail(
                        "invalid_range",
                        f"$.{start_key} must be earlier than $.{end_key}",
                        details={"start": start_key, "end": end_key},
                    )
                if (
                    start_key == "due_start"
                    and parsed_ranges[end_key] - parsed_ranges[start_key]
                    > timedelta(days=366)
                ):
                    fail(
                        "range_too_wide",
                        "$.due_start/$.due_end span must not exceed 366 days",
                        details={
                            "start": start_key,
                            "end": end_key,
                            "maximum_days": 366,
                        },
                    )
                if (
                    start_key == "completion_start"
                    and parsed_ranges[end_key] - parsed_ranges[start_key]
                    > timedelta(days=90)
                ):
                    fail(
                        "range_too_wide",
                        "$.completion_start/$.completion_end span must not exceed 90 days",
                        details={
                            "start": start_key,
                            "end": end_key,
                            "maximum_days": 90,
                        },
                    )
        normalized["limit"] = normalized_int(request["limit"], "$.limit", 1, 500)
        normalized["offset"] = normalized_int(request.get("offset", 0), "$.offset", 0, 10_000)
        sort = request.get("sort", "due")
        if sort not in {"due", "modified", "title"}:
            fail("invalid_enum", "$.sort must be due, modified, or title", details={"path": "$.sort"})
        normalized["sort"] = sort
        has_calendar_scope = "calendar_ids" in request
        has_due_range = "due_start" in request and "due_end" in request
        has_completion_range = (
            "completion_start" in request and "completion_end" in request
        )
        if status == "completed" and not has_completion_range:
            fail(
                "missing_completion_range",
                "status=completed requires both $.completion_start and $.completion_end",
                details={
                    "required": ["completion_start", "completion_end"],
                    "status": status,
                },
            )
        if status == "incomplete" and not (has_calendar_scope or has_due_range):
            fail(
                "unbounded_read",
                "status=incomplete requires calendar_ids or both $.due_start and $.due_end; query and modified_after are post-filters and cannot bound EventKit's underlying fetch",
                details={
                    "accepted_native_bounds": [
                        "calendar_ids",
                        "status=incomplete + due_start + due_end",
                    ],
                    "status": status,
                },
            )
        return normalized
    if operation == "create_reminder":
        allowed = COMMON | {"calendar_id"} | PATCH_FIELDS
        reject_unknown(request, allowed)
        require_fields(request, {"calendar_id", "title"})
        normalized["calendar_id"] = normalized_string(request["calendar_id"], "$.calendar_id")
        normalized.update(normalize_mutable_fields(request, path="$", create=True))
        return normalized
    if operation == "update_reminder":
        reject_unknown(request, COMMON | {"reminder_id", "expected_last_modified", "patch"})
        require_fields(request, {"reminder_id", "expected_last_modified", "patch"})
        normalized["reminder_id"] = normalized_reminder_identifier(
            request["reminder_id"], "$.reminder_id"
        )
        normalized["expected_last_modified"] = normalize_expected_last_modified(
            request["expected_last_modified"], "$.expected_last_modified"
        )
        patch = require_dict(request["patch"], "$.patch")
        reject_unknown(patch, PATCH_FIELDS, "$.patch")
        if not patch:
            fail("empty_patch", "$.patch must contain at least one field", details={"path": "$.patch"})
        normalized["patch"] = normalize_mutable_fields(patch, path="$.patch", create=False)
        return normalized
    if operation in {"complete_reminder", "reopen_reminder"}:
        reject_unknown(request, COMMON | {"reminder_id", "expected_last_modified"})
        require_fields(request, {"reminder_id", "expected_last_modified"})
        normalized["reminder_id"] = normalized_reminder_identifier(
            request["reminder_id"], "$.reminder_id"
        )
        normalized["expected_last_modified"] = normalize_expected_last_modified(
            request["expected_last_modified"], "$.expected_last_modified"
        )
        return normalized
    if operation == "delete_reminder":
        reject_unknown(request, COMMON | {"reminder_id", "expected_last_modified"})
        require_fields(request, {"reminder_id", "expected_last_modified"})
        normalized["reminder_id"] = normalized_reminder_identifier(
            request["reminder_id"], "$.reminder_id"
        )
        normalized["expected_last_modified"] = normalize_expected_last_modified(
            request["expected_last_modified"], "$.expected_last_modified"
        )
        return normalized
    if operation == "move_reminder":
        reject_unknown(request, COMMON | {"reminder_id", "expected_last_modified", "calendar_id"})
        require_fields(request, {"reminder_id", "expected_last_modified", "calendar_id"})
        normalized["reminder_id"] = normalized_reminder_identifier(
            request["reminder_id"], "$.reminder_id"
        )
        normalized["calendar_id"] = normalized_string(request["calendar_id"], "$.calendar_id")
        normalized["expected_last_modified"] = normalize_expected_last_modified(
            request["expected_last_modified"], "$.expected_last_modified"
        )
        return normalized
    raise AssertionError(f"Unhandled operation: {operation}")


def _compiler_identity() -> bytes:
    try:
        result = run_bounded_process(
            ["xcrun", "clang", "--version"],
            timeout_s=10,
            stdout_limit=MAX_COMPILER_IDENTITY_STDOUT_BYTES,
            stderr_limit=MAX_COMPILER_IDENTITY_STDERR_BYTES,
            output="bytes",
        )
    except ProcessError as exc:
        raise RuntimeError(f"Unable to locate the Xcode command-line compiler: {exc}") from exc
    if result.returncode != 0:
        diagnostics = bytes(result.stderr or result.stdout).decode(
            "utf-8", errors="replace"
        ).strip()
        raise RuntimeError(
            "Unable to locate the Xcode command-line compiler: "
            f"compiler exited {result.returncode}: {diagnostics}"
        )
    return bytes(result.stdout) + bytes(result.stderr)


def helper_digest() -> str:
    digest = hashlib.sha256()
    digest.update(b"eventkit-bridge-build-v2-signed\0")
    for path in (SOURCE_PATH, INFO_PLIST_PATH):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    digest.update(_compiler_identity())
    return digest.hexdigest()[:20]


def build_helper(cache_root: Path | None = None, *, force: bool = False) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("The EventKit bridge can only be compiled on macOS")
    for path in (SOURCE_PATH, INFO_PLIST_PATH):
        if not path.is_file():
            raise RuntimeError(f"Required bridge source is missing: {path}")
    root = (cache_root or DEFAULT_CACHE_ROOT).expanduser().resolve()
    digest = helper_digest()
    build_dir = root / digest
    binary = build_dir / "reminders-eventkit"
    if cache_root is None:
        root.parent.mkdir(parents=True, exist_ok=True)
        root.parent.chmod(0o700)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    build_dir.mkdir(parents=True, exist_ok=True)
    build_dir.chmod(0o700)
    if binary.is_file() and os.access(binary, os.X_OK) and not force:
        binary.chmod(0o700)
        return binary
    with tempfile.NamedTemporaryFile(prefix="reminders-eventkit-", dir=build_dir, delete=False) as temp:
        temporary_binary = Path(temp.name)
    temporary_binary.unlink(missing_ok=True)
    command = [
        "xcrun",
        "clang",
        "-fobjc-arc",
        "-fblocks",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-Wno-deprecated-declarations",
        "-mmacosx-version-min=14.0",
        "-framework",
        "Foundation",
        "-framework",
        "EventKit",
        "-framework",
        "CoreLocation",
        str(SOURCE_PATH),
        "-sectcreate",
        "__TEXT",
        "__info_plist",
        str(INFO_PLIST_PATH),
        "-o",
        str(temporary_binary),
    ]
    try:
        result = run_bounded_process(
            command,
            timeout_s=120,
            stdout_limit=MAX_BUILD_STDOUT_BYTES,
            stderr_limit=MAX_BUILD_STDERR_BYTES,
            output="utf8",
        )
    except ProcessError as exc:
        temporary_binary.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to run the EventKit bridge compiler: {exc}") from exc
    if result.returncode != 0:
        temporary_binary.unlink(missing_ok=True)
        diagnostics = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"EventKit bridge compilation failed: {diagnostics}")
    try:
        signing = run_bounded_process(
            [
                "codesign",
                "--force",
                "--sign",
                "-",
                "--identifier",
                LEGACY_HELPER_BUNDLE_IDENTIFIER,
                str(temporary_binary),
            ],
            timeout_s=30,
            stdout_limit=MAX_BUILD_STDOUT_BYTES,
            stderr_limit=MAX_BUILD_STDERR_BYTES,
            output="utf8",
        )
    except ProcessError as exc:
        temporary_binary.unlink(missing_ok=True)
        raise RuntimeError(f"Unable to ad-hoc sign the EventKit bridge: {exc}") from exc
    if signing.returncode != 0:
        temporary_binary.unlink(missing_ok=True)
        diagnostics = (signing.stderr or signing.stdout).strip()
        raise RuntimeError(f"EventKit bridge ad-hoc signing failed: {diagnostics}")
    temporary_binary.chmod(0o700)
    os.replace(temporary_binary, binary)
    return binary


def _read_regular_file(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BundledHelperUnavailable(f"{label} is missing or unsafe") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BundledHelperUnavailable(f"{label} is not a regular file")
        if metadata.st_size < 0 or metadata.st_size > MAX_BUNDLED_HELPER_FILE_BYTES:
            raise BundledHelperUnavailable(f"{label} exceeds the validation bound")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(MAX_BUNDLED_HELPER_FILE_BYTES + 1)
        if len(payload) != metadata.st_size:
            raise BundledHelperUnavailable(f"{label} changed while it was read")
        return payload
    except OSError as exc:
        raise BundledHelperUnavailable(f"{label} could not be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _sha256_regular_file(path: Path, label: str) -> str:
    return hashlib.sha256(_read_regular_file(path, label)).hexdigest()


def _load_plugin_version() -> str:
    raw = _read_regular_file(PLUGIN_MANIFEST_PATH, "plugin manifest")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundledHelperUnavailable("plugin manifest is invalid") from exc
    if not isinstance(payload, dict):
        raise BundledHelperUnavailable("plugin manifest root is invalid")
    version = payload.get("version")
    if not isinstance(version, str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+", version
    ):
        raise BundledHelperUnavailable("plugin version is invalid")
    return version


def _bundled_helper_inventory() -> dict[str, str]:
    for path, label in (
        (BUNDLED_HELPER_NATIVE_DIR, "native helper directory"),
        (BUNDLED_HELPER_APP, "bundled helper app"),
    ):
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise BundledHelperUnavailable(f"{label} is missing") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise BundledHelperUnavailable(f"{label} is unsafe")
    if stat.S_IMODE(BUNDLED_HELPER_NATIVE_DIR.lstat().st_mode) != 0o755:
        raise BundledHelperUnavailable("native helper directory mode is invalid")
    if stat.S_IMODE(BUNDLED_HELPER_APP.lstat().st_mode) != 0o755:
        raise BundledHelperUnavailable("bundled helper app mode is invalid")

    try:
        with os.scandir(BUNDLED_HELPER_NATIVE_DIR) as iterator:
            native_entries = {entry.name: entry.stat(follow_symlinks=False) for entry in iterator}
    except OSError as exc:
        raise BundledHelperUnavailable(
            "native helper directory could not be inspected"
        ) from exc
    if set(native_entries) != {
        BUNDLED_HELPER_APP_NAME,
        BUNDLED_HELPER_MANIFEST_NAME,
    }:
        raise BundledHelperUnavailable("native helper inventory is invalid")
    manifest_metadata = native_entries[BUNDLED_HELPER_MANIFEST_NAME]
    if (
        stat.S_ISLNK(manifest_metadata.st_mode)
        or not stat.S_ISREG(manifest_metadata.st_mode)
        or stat.S_IMODE(manifest_metadata.st_mode) != 0o644
    ):
        raise BundledHelperUnavailable("native helper manifest is unsafe")

    directories: set[str] = set()
    file_modes: dict[str, int] = {}
    pending = [BUNDLED_HELPER_APP]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
                for entry in entries:
                    metadata = entry.stat(follow_symlinks=False)
                    path = Path(entry.path)
                    relative = path.relative_to(BUNDLED_HELPER_APP).as_posix()
                    if stat.S_ISLNK(metadata.st_mode):
                        raise BundledHelperUnavailable(
                            "bundled helper app contains a symlink"
                        )
                    if stat.S_ISDIR(metadata.st_mode):
                        directories.add(relative)
                        if stat.S_IMODE(metadata.st_mode) != 0o755:
                            raise BundledHelperUnavailable(
                                "bundled helper directory mode is invalid"
                            )
                        pending.append(path)
                    elif stat.S_ISREG(metadata.st_mode):
                        file_modes[relative] = stat.S_IMODE(metadata.st_mode)
                    else:
                        raise BundledHelperUnavailable(
                            "bundled helper app contains a special file"
                        )
        except BundledHelperUnavailable:
            raise
        except OSError as exc:
            raise BundledHelperUnavailable(
                "bundled helper inventory could not be inspected"
            ) from exc

    if directories != set(BUNDLED_HELPER_EXPECTED_DIRECTORIES):
        raise BundledHelperUnavailable("bundled helper directory inventory is invalid")
    if set(file_modes) != set(BUNDLED_HELPER_EXPECTED_FILE_MODES):
        raise BundledHelperUnavailable("bundled helper file inventory is invalid")
    if file_modes != BUNDLED_HELPER_EXPECTED_FILE_MODES:
        raise BundledHelperUnavailable("bundled helper file modes are invalid")

    return {
        f"{BUNDLED_HELPER_APP_NAME}/{relative}": _sha256_regular_file(
            BUNDLED_HELPER_APP / relative,
            f"bundled helper member {relative}",
        )
        for relative in sorted(file_modes)
    }


def _load_bundled_helper_manifest(
    app_files: dict[str, str],
    plugin_version: str,
) -> tuple[dict[str, Any], str, dict[str, str]]:
    raw = _read_regular_file(
        BUNDLED_HELPER_MANIFEST_PATH,
        "bundled helper provenance manifest",
    )
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundledHelperUnavailable(
            "bundled helper provenance manifest is invalid"
        ) from exc
    if not isinstance(manifest, dict) or set(manifest) != set(
        BUNDLED_HELPER_MANIFEST_KEYS
    ):
        raise BundledHelperUnavailable(
            "bundled helper provenance key inventory is invalid"
        )

    source_files = {
        relative: _sha256_regular_file(
            PLUGIN_ROOT / relative,
            f"bundled helper source {relative}",
        )
        for relative in BUNDLED_HELPER_SOURCE_RELATIVE_PATHS
    }
    expected_metadata: dict[str, Any] = {
        "app_name": BUNDLED_HELPER_APP_NAME,
        "architectures": sorted(BUNDLED_HELPER_ARCHITECTURES),
        "bundle_identifier": BUNDLED_HELPER_BUNDLE_IDENTIFIER,
        "executable": BUNDLED_HELPER_EXECUTABLE_NAME,
        "minimum_macos": BUNDLED_HELPER_MINIMUM_MACOS,
        "minimum_macos_by_architecture": {
            architecture: BUNDLED_HELPER_MINIMUM_MACOS
            for architecture in sorted(BUNDLED_HELPER_ARCHITECTURES)
        },
        "plugin_version": plugin_version,
        "signature": "developer-id",
        "team_id": BUNDLED_HELPER_TEAM_IDENTIFIER,
    }
    for key, expected in expected_metadata.items():
        if manifest.get(key) != expected:
            raise BundledHelperUnavailable(
                f"bundled helper provenance {key} is invalid"
            )
    if type(manifest.get("schema_version")) is not int or manifest.get(
        "schema_version"
    ) != 1:
        raise BundledHelperUnavailable(
            "bundled helper provenance schema version is invalid"
        )
    if manifest.get("notarization_checked") is not True or manifest.get(
        "notarized"
    ) is not True:
        raise BundledHelperUnavailable(
            "bundled helper provenance notarization state is invalid"
        )
    if manifest.get("app_files") != app_files:
        raise BundledHelperUnavailable(
            "bundled helper bytes do not match their provenance"
        )
    binary_hash = app_files[
        f"{BUNDLED_HELPER_APP_NAME}/Contents/MacOS/"
        f"{BUNDLED_HELPER_EXECUTABLE_NAME}"
    ]
    if manifest.get("binary_sha256") != binary_hash:
        raise BundledHelperUnavailable(
            "bundled helper executable does not match its provenance"
        )
    if manifest.get("source_files") != source_files:
        raise BundledHelperUnavailable(
            "bundled helper sources do not match their provenance"
        )

    build_inputs = manifest.get("build_inputs")
    if (
        not isinstance(build_inputs, dict)
        or set(build_inputs) != set(BUNDLED_HELPER_BUILD_INPUT_RELATIVE_PATHS)
        or any(
            not isinstance(value, str)
            or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in build_inputs.values()
        )
    ):
        raise BundledHelperUnavailable(
            "bundled helper build-input provenance is invalid"
        )
    build_environment = manifest.get("build_environment")
    if (
        not isinstance(build_environment, dict)
        or set(build_environment) != set(BUNDLED_HELPER_BUILD_ENVIRONMENT_KEYS)
        or any(
            not isinstance(value, str) or not value.strip()
            for value in build_environment.values()
        )
    ):
        raise BundledHelperUnavailable(
            "bundled helper build-environment provenance is invalid"
        )
    source_commit = manifest.get("source_commit")
    if not isinstance(source_commit, str) or not re.fullmatch(
        r"[0-9a-f]{40}(?:[0-9a-f]{24})?", source_commit
    ):
        raise BundledHelperUnavailable(
            "bundled helper source commit provenance is invalid"
        )
    workflow_commit = manifest.get("workflow_commit")
    if not isinstance(workflow_commit, str) or not re.fullmatch(
        r"[0-9a-f]{40}(?:[0-9a-f]{24})?", workflow_commit
    ):
        raise BundledHelperUnavailable(
            "bundled helper workflow commit provenance is invalid"
        )
    return manifest, hashlib.sha256(raw).hexdigest(), source_files


def _verify_bundled_helper_info(plugin_version: str) -> None:
    raw = _read_regular_file(
        BUNDLED_HELPER_APP / "Contents" / "Info.plist",
        "bundled helper Info.plist",
    )
    try:
        info = plistlib.loads(raw)
    except plistlib.InvalidFileException as exc:
        raise BundledHelperUnavailable("bundled helper Info.plist is invalid") from exc
    expected = {
        "CFBundleIdentifier": BUNDLED_HELPER_BUNDLE_IDENTIFIER,
        "CFBundleName": "Apple Reminders EventKit Helper",
        "CFBundleDisplayName": "Apple Reminders EventKit Helper",
        "CFBundleExecutable": BUNDLED_HELPER_EXECUTABLE_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": plugin_version,
        "CFBundleVersion": plugin_version,
        "LSMinimumSystemVersion": BUNDLED_HELPER_MINIMUM_MACOS,
        "NSRemindersUsageDescription": (
            "Access is used only to perform the Apple Reminders actions "
            "explicitly requested by the user."
        ),
        "NSRemindersFullAccessUsageDescription": (
            "Full access is used to read, create, and update Apple Reminders "
            "explicitly requested by the user."
        ),
    }
    if info != expected:
        raise BundledHelperUnavailable("bundled helper Info.plist metadata is invalid")


def _run_bundled_helper_check(argv: list[str]) -> tuple[str, str]:
    try:
        result = run_bounded_process(
            argv,
            timeout_s=30,
            stdout_limit=MAX_HELPER_VERIFY_STDOUT_BYTES,
            stderr_limit=MAX_HELPER_VERIFY_STDERR_BYTES,
            output="utf8",
        )
    except ProcessError as exc:
        raise BundledHelperUnavailable(
            "bundled helper trust check could not run"
        ) from exc
    if result.returncode != 0:
        raise BundledHelperUnavailable("bundled helper trust check failed")
    return str(result.stdout), str(result.stderr)


def _unpack_macho(
    format_string: str,
    payload: bytes | memoryview,
    offset: int,
    label: str,
) -> tuple[int, ...]:
    size = struct.calcsize(format_string)
    if offset < 0 or size < 0 or offset + size > len(payload):
        raise BundledHelperUnavailable(f"bundled helper {label} is out of bounds")
    try:
        return struct.unpack_from(format_string, payload, offset)
    except struct.error as exc:
        raise BundledHelperUnavailable(
            f"bundled helper {label} is malformed"
        ) from exc


def _verify_bundled_helper_mach_slice(
    payload: bytes,
    *,
    slice_offset: int,
    slice_size: int,
    expected_cpu_type: int,
    expected_cpu_subtype: int,
) -> None:
    slice_end = slice_offset + slice_size
    if slice_size < MACH_HEADER_64_SIZE or slice_end > len(payload):
        raise BundledHelperUnavailable("bundled helper Mach-O slice is truncated")
    slice_payload = memoryview(payload)[slice_offset:slice_end]
    (
        magic,
        cpu_type,
        cpu_subtype,
        file_type,
        command_count,
        command_bytes,
        _flags,
        reserved,
    ) = _unpack_macho(
        "<IIIIIIII",
        slice_payload,
        0,
        "64-bit Mach-O header",
    )
    if magic != MACH_MAGIC_64_LITTLE_ENDIAN:
        raise BundledHelperUnavailable(
            "bundled helper slice is not a little-endian 64-bit Mach-O"
        )
    if (
        cpu_type != expected_cpu_type
        or (cpu_subtype & CPU_SUBTYPE_CAPABILITY_MASK) != 0
        or (cpu_subtype & CPU_SUBTYPE_BASE_MASK) != expected_cpu_subtype
    ):
        raise BundledHelperUnavailable("bundled helper Mach-O CPU metadata is invalid")
    if file_type != MACH_FILETYPE_EXECUTE or reserved != 0:
        raise BundledHelperUnavailable("bundled helper Mach-O header is invalid")
    if (
        command_count == 0
        or command_count > MAX_MACH_LOAD_COMMANDS
        or command_bytes < command_count * 8
        or command_bytes % 8 != 0
    ):
        raise BundledHelperUnavailable(
            "bundled helper Mach-O load-command bounds are invalid"
        )
    commands_offset = MACH_HEADER_64_SIZE
    commands_end = commands_offset + command_bytes
    if commands_end > len(slice_payload):
        raise BundledHelperUnavailable(
            "bundled helper Mach-O load commands exceed their slice"
        )

    cursor = commands_offset
    build_version_seen = False
    for _ in range(command_count):
        if cursor + 8 > commands_end:
            raise BundledHelperUnavailable(
                "bundled helper Mach-O load-command header exceeds its table"
            )
        command, command_size = _unpack_macho(
            "<II",
            slice_payload,
            cursor,
            "load-command header",
        )
        if command_size < 8 or command_size % 8 != 0:
            raise BundledHelperUnavailable(
                "bundled helper Mach-O load-command size is invalid"
            )
        command_end = cursor + command_size
        if command_end > commands_end:
            raise BundledHelperUnavailable(
                "bundled helper Mach-O load command exceeds its table"
            )
        if command == LC_VERSION_MIN_MACOSX:
            raise BundledHelperUnavailable(
                "bundled helper uses an unexpected legacy deployment command"
            )
        if command == LC_BUILD_VERSION:
            if build_version_seen or command_size < 24:
                raise BundledHelperUnavailable(
                    "bundled helper LC_BUILD_VERSION inventory is invalid"
                )
            (
                _command,
                _command_size,
                platform,
                minimum_os,
                _sdk,
                tool_count,
            ) = _unpack_macho(
                "<IIIIII",
                slice_payload,
                cursor,
                "LC_BUILD_VERSION",
            )
            if command_size != 24 + tool_count * 8:
                raise BundledHelperUnavailable(
                    "bundled helper LC_BUILD_VERSION tools are malformed"
                )
            if platform != PLATFORM_MACOS:
                raise BundledHelperUnavailable(
                    "bundled helper LC_BUILD_VERSION platform is invalid"
                )
            if minimum_os != MACOS_14_0_0_PACKED_VERSION:
                raise BundledHelperUnavailable(
                    "bundled helper minimum macOS version is invalid"
                )
            build_version_seen = True
        cursor = command_end

    if cursor != commands_end:
        raise BundledHelperUnavailable(
            "bundled helper Mach-O load-command table has trailing bytes"
        )
    if not build_version_seen:
        raise BundledHelperUnavailable(
            "bundled helper has no LC_BUILD_VERSION command"
        )


def _verify_bundled_helper_architectures(payload: bytes) -> None:
    """Parse the universal binary without invoking Xcode or developer-tool shims."""

    if len(payload) < 8:
        raise BundledHelperUnavailable("bundled helper fat header is truncated")
    fat_format = FAT_MAGIC_FORMATS.get(payload[:4])
    if fat_format is None:
        raise BundledHelperUnavailable("bundled helper has an invalid fat magic")
    endian, uses_64_bit_entries = fat_format
    (architecture_count,) = _unpack_macho(
        f"{endian}I",
        payload,
        4,
        "fat architecture count",
    )
    if architecture_count != len(EXPECTED_MACH_CPU_SUBTYPES):
        raise BundledHelperUnavailable(
            "bundled helper must contain exactly two architectures"
        )

    entry_format = f"{endian}{'IIQQII' if uses_64_bit_entries else 'IIIII'}"
    entry_size = struct.calcsize(entry_format)
    table_end = 8 + architecture_count * entry_size
    if table_end > len(payload):
        raise BundledHelperUnavailable("bundled helper fat table is truncated")

    slices: list[tuple[int, int, int, int]] = []
    cpu_types: set[int] = set()
    for index in range(architecture_count):
        values = _unpack_macho(
            entry_format,
            payload,
            8 + index * entry_size,
            "fat architecture entry",
        )
        if uses_64_bit_entries:
            cpu_type, cpu_subtype, slice_offset, slice_size, alignment, reserved = values
            if reserved != 0:
                raise BundledHelperUnavailable(
                    "bundled helper fat64 reserved field is invalid"
                )
        else:
            cpu_type, cpu_subtype, slice_offset, slice_size, alignment = values
        expected_subtype = EXPECTED_MACH_CPU_SUBTYPES.get(cpu_type)
        if (
            expected_subtype is None
            or (cpu_subtype & CPU_SUBTYPE_CAPABILITY_MASK) != 0
            or (cpu_subtype & CPU_SUBTYPE_BASE_MASK) != expected_subtype
        ):
            raise BundledHelperUnavailable(
                "bundled helper fat CPU metadata is invalid"
            )
        if cpu_type in cpu_types:
            raise BundledHelperUnavailable(
                "bundled helper contains a duplicate architecture"
            )
        cpu_types.add(cpu_type)
        maximum_alignment = 63 if uses_64_bit_entries else 31
        if alignment > maximum_alignment or slice_offset % (1 << alignment) != 0:
            raise BundledHelperUnavailable(
                "bundled helper fat slice alignment is invalid"
            )
        if slice_size < MACH_HEADER_64_SIZE or slice_offset < table_end:
            raise BundledHelperUnavailable(
                "bundled helper fat slice boundary is invalid"
            )
        slice_end = slice_offset + slice_size
        if slice_end > len(payload):
            raise BundledHelperUnavailable(
                "bundled helper fat slice exceeds the executable"
            )
        slices.append((slice_offset, slice_end, cpu_type, cpu_subtype))

    if cpu_types != set(EXPECTED_MACH_CPU_SUBTYPES):
        raise BundledHelperUnavailable("bundled helper architectures are invalid")
    previous_end = table_end
    for slice_offset, slice_end, cpu_type, cpu_subtype in sorted(slices):
        if slice_offset < previous_end:
            raise BundledHelperUnavailable("bundled helper fat slices overlap")
        _verify_bundled_helper_mach_slice(
            payload,
            slice_offset=slice_offset,
            slice_size=slice_end - slice_offset,
            expected_cpu_type=cpu_type,
            expected_cpu_subtype=cpu_subtype,
        )
        previous_end = slice_end


def _codesign_value(details: str, key: str) -> str | None:
    prefix = f"{key}="
    for line in details.splitlines():
        if line.startswith(prefix):
            value = line.partition("=")[2].strip()
            return value or None
    return None


def _verify_bundled_helper_signature() -> None:
    _run_bundled_helper_check(
        [
            "/usr/bin/codesign",
            "--verify",
            "--deep",
            "--strict",
            "--test-requirement",
            f"={BUNDLED_HELPER_DESIGNATED_REQUIREMENT}",
            str(BUNDLED_HELPER_APP),
        ]
    )
    stdout, stderr = _run_bundled_helper_check(
        ["/usr/bin/codesign", "-dvvv", str(BUNDLED_HELPER_APP)]
    )
    details = f"{stdout}\n{stderr}"
    if _codesign_value(details, "Identifier") != BUNDLED_HELPER_BUNDLE_IDENTIFIER:
        raise BundledHelperUnavailable("bundled helper signing identifier is invalid")
    if _codesign_value(details, "TeamIdentifier") != BUNDLED_HELPER_TEAM_IDENTIFIER:
        raise BundledHelperUnavailable("bundled helper signing Team ID is invalid")
    code_directory = next(
        (
            line
            for line in details.splitlines()
            if line.startswith("CodeDirectory ")
        ),
        None,
    )
    if code_directory is None or "runtime" not in code_directory:
        raise BundledHelperUnavailable("bundled helper lacks Hardened Runtime")
    if _codesign_value(details, "Timestamp") is None:
        raise BundledHelperUnavailable("bundled helper lacks a secure timestamp")


def _verify_bundled_helper() -> Path:
    """Fail closed unless the exact reviewed release helper is still trusted."""

    global _verified_bundled_helper_fingerprint

    if sys.platform != "darwin":
        raise BundledHelperUnavailable("the EventKit helper requires macOS")
    try:
        plugin_version = _load_plugin_version()
        app_files = _bundled_helper_inventory()
        manifest, manifest_hash, source_files = _load_bundled_helper_manifest(
            app_files,
            plugin_version,
        )
        _verify_bundled_helper_info(plugin_version)
        fingerprint = (
            manifest_hash,
            *(f"app:{key}:{value}" for key, value in sorted(app_files.items())),
            *(
                f"source:{key}:{value}"
                for key, value in sorted(source_files.items())
            ),
        )
        if fingerprint == _verified_bundled_helper_fingerprint:
            return BUNDLED_HELPER_PATH
        executable = _read_regular_file(
            BUNDLED_HELPER_PATH,
            "bundled helper executable",
        )
        if hashlib.sha256(executable).hexdigest() != manifest["binary_sha256"]:
            raise BundledHelperUnavailable(
                "bundled helper executable changed during verification"
            )
        _verify_bundled_helper_architectures(executable)
        _verify_bundled_helper_signature()
        _verified_bundled_helper_fingerprint = fingerprint
        return BUNDLED_HELPER_PATH
    except BundledHelperUnavailable:
        raise
    except Exception as exc:
        raise BundledHelperUnavailable(
            "the bundled helper could not be verified"
        ) from exc


def resolve_helper(
    cache_root: Path | None = None,
    *,
    allow_source_build: bool = False,
) -> Path:
    """Use the release helper, or an explicitly requested contributor build."""

    try:
        return _verify_bundled_helper()
    except BundledHelperUnavailable:
        if not allow_source_build:
            raise
    return build_helper(cache_root)


def _invalid_native_response(
    request: dict[str, Any],
    *,
    message: str,
    returncode: int | None,
    stderr: str,
) -> dict[str, Any]:
    details = {"exit_code": returncode, "stderr": stderr[-4000:]}
    if request["operation"] in MUTATION_OPERATIONS:
        return mutation_outcome_unknown_response(
            request,
            reason_code="invalid_native_response",
            message=message,
            details=details,
        )
    return response(
        request["operation"],
        "failed_no_mutation",
        error={
            "code": "unexpected_error",
            "reason_code": "invalid_native_response",
            "message": message,
            "retryable": False,
            "details": details,
        },
    )


def invoke_native(
    request: dict[str, Any],
    *,
    cache_root: Path | None = None,
    timeout: int = NATIVE_TIMEOUT_SECONDS,
    allow_source_build: bool = False,
) -> dict[str, Any]:
    try:
        binary = resolve_helper(
            cache_root,
            allow_source_build=allow_source_build,
        )
    except BundledHelperUnavailable:
        return response(
            request["operation"],
            "failed_no_mutation",
            error={
                "code": "unexpected_error",
                "reason_code": "native_helper_unavailable",
                "message": (
                    "The signed EventKit helper is missing or could not be verified."
                ),
                "retryable": False,
                "details": {},
            },
        )
    except Exception as exc:
        return response(
            request["operation"],
            "failed_no_mutation",
            error={
                "code": "unexpected_error",
                "reason_code": "native_helper_build_failed",
                "message": f"EventKit helper could not be prepared ({type(exc).__name__})",
                "retryable": False,
                "details": {},
            },
        )
    try:
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        return response(
            request["operation"],
            "failed_no_mutation",
            error={
                "code": "unexpected_error",
                "reason_code": "native_request_encoding_failed",
                "message": f"EventKit request could not be encoded ({type(exc).__name__})",
                "retryable": False,
                "details": {},
            },
        )
    try:
        result = run_bounded_process(
            [str(binary)],
            input=encoded,
            timeout_s=timeout,
            stdout_limit=MAX_NATIVE_STDOUT_BYTES,
            stderr_limit=MAX_NATIVE_STDERR_BYTES,
            output="utf8",
        )
    except ProcessTimeoutError:
        if request["operation"] in MUTATION_OPERATIONS:
            return mutation_outcome_unknown_response(
                request,
                reason_code="native_timeout",
                message=f"EventKit operation exceeded {timeout} seconds",
                details={"timeout_seconds": timeout},
            )
        return response(
            request["operation"],
            "failed_no_mutation",
            error={
                "code": "unexpected_error",
                "reason_code": "native_timeout",
                "message": f"EventKit operation exceeded {timeout} seconds",
                "retryable": True,
                "details": {},
            },
        )
    except ProcessLaunchError:
        return response(
            request["operation"],
            "failed_no_mutation",
            error={
                "code": "unexpected_error",
                "reason_code": "native_launch_failed",
                "message": "The EventKit helper process could not start.",
                "retryable": False,
                "details": {},
            },
        )
    except (ProcessOutputLimitError, ProcessDecodeError) as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        return _invalid_native_response(
            request,
            message=str(exc),
            returncode=exc.returncode,
            stderr=stderr,
        )
    except ProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        return _invalid_native_response(
            request,
            message="The EventKit helper failed after launch without a valid response.",
            returncode=exc.returncode,
            stderr=stderr,
        )
    try:
        decoded = json.loads(result.stdout)
        return validate_response(decoded, request["operation"])
    except (json.JSONDecodeError, RuntimeError) as exc:
        stderr = str(result.stderr).strip()
        return _invalid_native_response(
            request,
            message=str(exc),
            returncode=result.returncode,
            stderr=stderr,
        )


def read_request(path: Path | None) -> Any:
    if path is None:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    else:
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_REQUEST_BYTES + 1)
        except OSError as exc:
            fail("request_read_failed", f"Unable to read request: {exc}")
    if len(raw) > MAX_REQUEST_BYTES:
        fail(
            "request_too_large",
            f"Request exceeds the {MAX_REQUEST_BYTES}-byte limit",
            details={"maximum_bytes": MAX_REQUEST_BYTES},
        )
    if not raw.strip():
        fail("empty_request", "Expected one JSON request object")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail("invalid_json", f"Request is not valid UTF-8 JSON: {exc}")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, help="Read the request from a file instead of stdin")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate and normalize the request without compiling or accessing EventKit",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        help="Print the request JSON Schema without compiling or accessing EventKit",
    )
    parser.add_argument(
        "--build-only",
        action="store_true",
        help=(
            "Compile a contributor-only legacy helper and print its path without "
            "accessing EventKit"
        ),
    )
    parser.add_argument(
        "--development-source-build-fallback",
        action="store_true",
        help=(
            "Allow legacy source compilation only when the bundled release helper "
            "is unavailable"
        ),
    )
    parser.add_argument("--cache-dir", type=Path, help="Override the compiled-helper cache directory")
    parser.add_argument("--force-build", action="store_true", help="Recompile even if the cached helper exists")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if sum(bool(value) for value in (args.schema, args.build_only)) > 1:
        payload = validation_error_response(
            None,
            BridgeValidationError(
                "conflicting_options", "--schema and --build-only cannot be used together"
            ),
        )
        emit(payload)
        return EXIT_CODES[payload["status"]]
    if args.schema:
        try:
            schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            payload = response(
                "schema",
                "failed_no_mutation",
                error={
                    "code": "unexpected_error",
                    "reason_code": "schema_read_failed",
                    "message": str(exc),
                    "retryable": False,
                    "details": {},
                },
            )
            emit(payload)
            return EXIT_CODES[payload["status"]]
        emit(response("schema", "verified", data={"request_schema": schema}))
        return 0
    if args.build_only:
        try:
            binary = build_helper(args.cache_dir, force=args.force_build)
            payload = response("build", "verified", data={"binary": str(binary)})
        except RuntimeError as exc:
            payload = response(
                "build",
                "failed_no_mutation",
                error={
                    "code": "unexpected_error",
                    "reason_code": "build_failed",
                    "message": str(exc),
                    "retryable": False,
                    "details": {},
                },
            )
        emit(payload)
        return EXIT_CODES[payload["status"]]

    operation: str | None = None
    try:
        raw = read_request(args.request)
        if isinstance(raw, dict) and isinstance(raw.get("operation"), str):
            operation = raw["operation"]
        normalized = normalize_request(raw)
    except BridgeValidationError as exc:
        payload = validation_error_response(operation, exc)
        emit(payload)
        return EXIT_CODES[payload["status"]]
    except Exception as exc:
        payload = response(
            operation,
            "failed_no_mutation",
            error={
                "code": "unexpected_error",
                "reason_code": "request_normalization_failed",
                "message": f"Request normalization failed ({type(exc).__name__})",
                "retryable": False,
                "details": {},
            },
        )
        emit(payload)
        return EXIT_CODES[payload["status"]]

    if args.validate_only:
        payload = response(
            normalized["operation"], "verified", data={"normalized_request": normalized, "validation_only": True}
        )
    else:
        try:
            payload = invoke_native(
                normalized,
                cache_root=args.cache_dir,
                allow_source_build=args.development_source_build_fallback,
            )
        except RuntimeError as exc:
            payload = response(
                normalized["operation"],
                "failed_no_mutation",
                error={
                    "code": "unexpected_error",
                    "reason_code": "build_failed",
                    "message": str(exc),
                    "retryable": False,
                    "details": {},
                },
            )
    emit(payload)
    return EXIT_CODES[payload["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
