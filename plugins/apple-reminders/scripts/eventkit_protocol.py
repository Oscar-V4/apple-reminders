#!/usr/bin/env python3
"""Dependency-light EventKit response validation and recovery receipts."""

from __future__ import annotations

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
