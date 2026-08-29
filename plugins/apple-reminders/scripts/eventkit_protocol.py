#!/usr/bin/env python3
"""Dependency-light EventKit response validation and recovery receipts."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from typing import Any

from receipt_contract import (
    STABLE_ERROR_CODES as CONTRACT_STABLE_ERROR_CODES,
    eventkit_mutation_receipt_error,
    failed_no_mutation_evidence_error,
)


__all__ = [
    "validate_response",
    "validate_mutation_receipt",
    "mutation_outcome_unknown_response",
    "validate_ensure_list_receipt",
    "project_reminder_list",
    "reminder_list_metadata_value_is_safe",
]


SCHEMA_VERSION = 1
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
COLLECTION_READ_OPERATIONS = {"list_calendars", "fetch_reminders"}
_LIST_TYPES = frozenset(
    {"local", "caldav", "exchange", "subscription", "birthday", "unknown"}
)
_SOURCE_TYPES = frozenset(
    {
        "local",
        "exchange",
        "caldav",
        "mobile_me",
        "subscribed",
        "birthdays",
        "unknown",
    }
)
_LIST_METADATA_BOOLEANS = frozenset(
    {"allows_content_modifications", "subscribed", "immutable", "is_delegate"}
)
_LIST_METADATA_TYPES = _LIST_TYPES | _SOURCE_TYPES


def reminder_list_metadata_value_is_safe(key: str, value: Any) -> bool:
    return (
        key == "type"
        and isinstance(value, str)
        and value in _LIST_METADATA_TYPES
    ) or (key in _LIST_METADATA_BOOLEANS and type(value) is bool)


def _project_source(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return copy.deepcopy(value)
    raw = dict(value)
    source_type = str(raw.get("type") or "unknown")
    if source_type not in _SOURCE_TYPES:
        source_type = "unknown"
    raw_count = raw.get(
        "reminder_list_count",
        raw.get("reminder_calendar_count", 0),
    )
    count = raw_count if type(raw_count) is int else 0
    result = {
        "id": str(raw.get("id") or "")[:2048],
        "type": source_type,
        "is_delegate": raw.get("is_delegate") is True,
        "reminder_list_count": max(0, min(count, 10_000)),
    }
    if "title" in raw:
        result["title"] = str(raw.get("title") or "")[:512]
    return result


def project_reminder_list(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return copy.deepcopy(value)
    raw = dict(value)
    list_type = str(raw.get("type") or "unknown")
    if list_type not in _LIST_TYPES:
        list_type = "unknown"
    return {
        "id": str(raw.get("id") or raw.get("list_id") or "")[:2048],
        "title": str(raw.get("title") or raw.get("name") or "")[:512],
        "type": list_type,
        "allows_content_modifications": (
            raw.get("allows_content_modifications") is True
        ),
        "subscribed": raw.get("subscribed") is True,
        "immutable": raw.get("immutable") is True,
        "source": _project_source(raw.get("source")),
    }


def validate_ensure_list_receipt(
    payload: Mapping[str, Any],
    *,
    source_id: str,
    name: str | None,
    rehydrate: bool = False,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RuntimeError("EventKit ensure-list receipt identity was invalid")
    result = copy.deepcopy(dict(payload))
    if result.get("status") not in {"unchanged", "verified"}:
        return result
    target = result.get("target")
    list_id = target.get("list_id") if isinstance(target, dict) else None

    def reject() -> None:
        raise RuntimeError("EventKit ensure-list receipt identity was invalid")

    if not (
        result.get("operation") == "ensure_reminder_list"
        and isinstance(source_id, str)
        and source_id
        and (rehydrate or isinstance(name, str) and bool(name))
        and isinstance(list_id, str)
        and list_id
        and target.get("source_id") == source_id
    ):
        reject()

    def validated_list(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            reject()
        source = value.get("source")
        count = (
            source.get("reminder_calendar_count")
            if isinstance(source, dict)
            else None
        )
        if not (
            value.get("id") == list_id
            and (rehydrate or value.get("title") == name)
            and value.get("type") in _LIST_TYPES
            and all(
                type(value.get(field)) is bool
                for field in ("allows_content_modifications", "subscribed", "immutable")
            )
            and isinstance(source, dict)
            and source.get("id") == source_id
            and source.get("type") in _SOURCE_TYPES
            and type(source.get("is_delegate")) is bool
            and type(count) is int
            and 0 <= count <= 10_000
        ):
            reject()
        if rehydrate and isinstance(name, str):
            value["title"] = name
        return value

    result["after"] = validated_list(result.get("after"))
    if result.get("status") == "unchanged":
        result["before"] = validated_list(result.get("before"))
        if result["before"] != result["after"]:
            reject()
    return result


def _validated_status_and_ok(payload: dict[str, Any]) -> tuple[str, bool]:
    status = payload.get("status")
    if not isinstance(status, str) or status not in EXIT_CODES:
        raise RuntimeError(f"Native bridge returned unknown status: {status!r}")
    ok = payload.get("ok")
    if not isinstance(ok, bool):
        raise RuntimeError("Native bridge response ok field must be boolean")
    expected_ok = status != "failed_no_mutation"
    if ok is not expected_ok:
        raise RuntimeError("Native bridge response ok field disagrees with its receipt status")
    return status, expected_ok


def mutation_outcome_unknown_response(
    request: Mapping[str, Any],
    *,
    reason_code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    operation = request.get("operation")
    if not isinstance(operation, str) or operation not in MUTATION_OPERATIONS:
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
                "message": (
                    "The native process may have committed; read the target "
                    "before retrying."
                ),
            }
        ],
        "error": {
            "code": "sync_pending",
            "reason_code": reason_code,
            "message": message,
            "details": dict(details) if details is not None else {},
        },
    }
    return payload


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
    status, expected_ok = _validated_status_and_ok(payload)
    if not expected_ok:
        error = payload.get("error")
        if not isinstance(error, dict):
            raise RuntimeError("Failed native bridge response must include an error object")
        error_code = error.get("code")
        if (
            not isinstance(error_code, str)
            or error_code not in STABLE_ERROR_CODES
            or not isinstance(error.get("message"), str)
        ):
            raise RuntimeError("Native bridge error must include a stable code and message")
    if expected_ok and operation in COLLECTION_READ_OPERATIONS:
        if status != "verified":
            raise RuntimeError(
                "Native bridge collection reads must use verified status"
            )
        data = payload.get("data")
        items = data.get("items") if isinstance(data, Mapping) else None
        if not isinstance(items, list):
            raise RuntimeError("Native bridge response data.items must be an array")
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                raise RuntimeError(
                    f"Native bridge response data.items[{index}] must be an object"
                )
    if operation in MUTATION_OPERATIONS:
        if payload["status"] == "failed_no_mutation":
            no_write_error = failed_no_mutation_evidence_error(payload)
            if no_write_error:
                raise RuntimeError(f"Invalid no-mutation response: {no_write_error}")
        else:
            validate_mutation_receipt(payload, operation)
    return payload


def validate_mutation_receipt(
    payload: Any,
    operation: str | None = None,
) -> dict[str, Any]:
    error = eventkit_mutation_receipt_error(
        payload,
        operation=operation,
        mutation_operations=MUTATION_OPERATIONS,
        stable_error_codes=STABLE_ERROR_CODES,
    )
    if error:
        raise RuntimeError(error)
    status, _ = _validated_status_and_ok(payload)
    if status == "failed_no_mutation":
        no_write_error = failed_no_mutation_evidence_error(payload)
        if no_write_error:
            raise RuntimeError(f"Invalid no-mutation response: {no_write_error}")
    return payload
