#!/usr/bin/env python3
"""Strict JSON-in/JSON-out launcher for the native Reminders EventKit bridge.

The Python layer owns request validation and deterministic helper compilation.
The Objective-C helper owns EventKit access.  `--validate-only` never compiles or
instantiates EventKit, which keeps contract tests independent from live data and
macOS privacy prompts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from receipt_contract import (  # noqa: E402
    STABLE_ERROR_CODES as CONTRACT_STABLE_ERROR_CODES,
    eventkit_mutation_receipt_error,
)


SCHEMA_VERSION = 1
SOURCE_PATH = SCRIPT_DIR / "reminders_eventkit.m"
INFO_PLIST_PATH = SCRIPT_DIR / "eventkit_bridge_info.plist"
SCHEMA_PATH = SCRIPT_DIR / "eventkit_bridge_schema.json"
DEFAULT_CACHE_ROOT = Path.home() / "Library/Caches/apple-reminders-codex/eventkit-bridge"
MAX_REQUEST_BYTES = 1_000_000
MAX_NOTES_CHARS = 100_000
NATIVE_TIMEOUT_SECONDS = 45
HELPER_BUNDLE_IDENTIFIER = "com.codex.apple-reminders.eventkit-bridge"

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

MUTATION_OPERATIONS = {
    "ensure_reminder_list",
    "create_reminder",
    "update_reminder",
    "complete_reminder",
    "reopen_reminder",
    "move_reminder",
    "delete_reminder",
}

EXIT_CODES = {
    "unchanged": 0,
    "verified": 0,
    "committed_verification_pending": 7,
    "partial_success": 7,
    "failed_no_mutation": 2,
}

STABLE_ERROR_CODES = set(CONTRACT_STABLE_ERROR_CODES)

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


def mutation_outcome_unknown_response(
    request: dict[str, Any],
    *,
    reason_code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    operation = request.get("operation")
    if operation not in MUTATION_OPERATIONS:
        raise ValueError("Only EventKit mutations can have an unknown commit outcome")
    target = {
        key: request[key]
        for key in ("reminder_id", "calendar_id")
        if isinstance(request.get(key), str)
    }
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "operation": operation,
        "status": "committed_verification_pending",
        "ok": True,
        "operation_id": str(uuid.uuid4()).upper(),
        "backend": "eventkit_public_sdk",
        "target": target,
        "before": {},
        "after": {},
        "verification": {
            "state": "pending",
            "write_performed": None,
            "reason_code": reason_code,
        },
        "recovery": {
            "semantics": "read_before_retry",
            "automatic_retry_safe": False,
        },
        "warnings": [
            {
                "code": "verification_pending",
                "message": "The native process may have committed; read the target before retrying.",
            }
        ],
        "error": {
            "code": "sync_pending",
            "reason_code": reason_code,
            "message": message,
            "details": details or {},
        },
    }
    return payload


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
        fail(
            "unsupported_relative_alarm",
            "Relative reminder alarm anchoring is not stable enough in the public macOS SDK; use an absolute alarm",
            status="unsupported",
            details={"path": path},
        )
    if kind is None:
        fail("missing_fields", f"{path} is missing required field: kind", details={"path": path})
    fail(
        "unsupported_alarm_kind",
        f"{path}.kind must be absolute or location",
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
        if status not in {"incomplete", "completed", "any"}:
            fail("invalid_enum", "$.status must be incomplete, completed, or any", details={"path": "$.status"})
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
        native_predicate_is_bounded = (
            has_calendar_scope
            or (status == "incomplete" and has_due_range)
            or (status == "completed" and has_completion_range)
        )
        if not native_predicate_is_bounded:
            fail(
                "unbounded_read",
                "fetch_reminders requires calendar_ids, an incomplete due_start/due_end range, or a completed completion_start/completion_end range; query and modified_after are post-filters and cannot bound EventKit's underlying fetch",
                details={
                    "accepted_native_bounds": [
                        "calendar_ids",
                        "status=incomplete + due_start + due_end",
                        "status=completed + completion_start + completion_end",
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
        result = subprocess.run(
            ["xcrun", "clang", "--version"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
        return result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Unable to locate the Xcode command-line compiler: {exc}") from exc


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
        "-mmacosx-version-min=12.0",
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
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        temporary_binary.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to run the EventKit bridge compiler: {exc}") from exc
    if result.returncode != 0:
        temporary_binary.unlink(missing_ok=True)
        diagnostics = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"EventKit bridge compilation failed: {diagnostics}")
    try:
        signing = subprocess.run(
            [
                "codesign",
                "--force",
                "--sign",
                "-",
                "--identifier",
                HELPER_BUNDLE_IDENTIFIER,
                str(temporary_binary),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        temporary_binary.unlink(missing_ok=True)
        raise RuntimeError(f"Unable to ad-hoc sign the EventKit bridge: {exc}") from exc
    if signing.returncode != 0:
        temporary_binary.unlink(missing_ok=True)
        diagnostics = (signing.stderr or signing.stdout).strip()
        raise RuntimeError(f"EventKit bridge ad-hoc signing failed: {diagnostics}")
    temporary_binary.chmod(0o700)
    os.replace(temporary_binary, binary)
    return binary


def validate_response(payload: Any, operation: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("Native bridge returned a non-object JSON response")
    required = {"schema_version", "operation", "status", "ok"}
    missing = sorted(required - set(payload))
    if missing:
        raise RuntimeError(f"Native bridge response is missing: {', '.join(missing)}")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise RuntimeError("Native bridge response schema version does not match the launcher")
    if payload["operation"] != operation:
        raise RuntimeError("Native bridge response operation does not match the request")
    if payload["status"] not in EXIT_CODES:
        raise RuntimeError(f"Native bridge returned unknown status: {payload['status']!r}")
    if not isinstance(payload["ok"], bool):
        raise RuntimeError("Native bridge response ok field must be boolean")
    expected_ok = payload["status"] != "failed_no_mutation"
    if payload["ok"] is not expected_ok:
        raise RuntimeError("Native bridge response ok field disagrees with its receipt status")
    if not expected_ok:
        error = payload.get("error")
        if not isinstance(error, dict):
            raise RuntimeError("Failed native bridge response must include an error object")
        if error.get("code") not in STABLE_ERROR_CODES or not isinstance(error.get("message"), str):
            raise RuntimeError("Native bridge error must include a stable code and message")
    if operation in MUTATION_OPERATIONS and payload["status"] != "failed_no_mutation":
        validate_mutation_receipt(payload, operation)
    return payload


def validate_mutation_receipt(payload: Any, operation: str | None = None) -> dict[str, Any]:
    error = eventkit_mutation_receipt_error(
        payload,
        operation=operation,
        mutation_operations=MUTATION_OPERATIONS,
        stable_error_codes=STABLE_ERROR_CODES,
    )
    if error:
        raise RuntimeError(error)
    return payload


def invoke_native(
    request: dict[str, Any],
    *,
    cache_root: Path | None = None,
    timeout: int = NATIVE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    try:
        binary = build_helper(cache_root)
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
        result = subprocess.run(
            [str(binary)],
            input=encoded,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
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
    except OSError as exc:
        return response(
            request["operation"],
            "failed_no_mutation",
            error={
                "code": "unexpected_error",
                "reason_code": "native_launch_failed",
                "message": str(exc),
                "retryable": False,
                "details": {},
            },
        )
    try:
        decoded = json.loads(result.stdout.decode("utf-8"))
        return validate_response(decoded, request["operation"])
    except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        if request["operation"] in MUTATION_OPERATIONS:
            return mutation_outcome_unknown_response(
                request,
                reason_code="invalid_native_response",
                message=str(exc),
                details={"exit_code": result.returncode, "stderr": stderr[-4000:]},
            )
        return response(
            request["operation"],
            "failed_no_mutation",
            error={
                "code": "unexpected_error",
                "reason_code": "invalid_native_response",
                "message": str(exc),
                "retryable": False,
                "details": {"exit_code": result.returncode, "stderr": stderr[-4000:]},
            },
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
        help="Compile the helper and print its path without accessing EventKit",
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
            payload = invoke_native(normalized, cache_root=args.cache_dir)
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
