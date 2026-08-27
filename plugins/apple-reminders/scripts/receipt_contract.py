#!/usr/bin/env python3
"""Dependency-light mutation receipt contract for local Python boundaries."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any


SUCCESS_RECEIPT_STATUSES = frozenset(
    {
        "unchanged",
        "verified",
        "committed_verification_pending",
        "partial_success",
    }
)
FAILURE_RECEIPT_STATUSES = frozenset(
    {"failed_no_mutation", "failed_manual_repair_required"}
)
RESULT_RECEIPT_STATUSES = SUCCESS_RECEIPT_STATUSES | FAILURE_RECEIPT_STATUSES
RECEIPT_OBJECT_FIELDS = frozenset({"target", "after", "verification", "recovery"})

STABLE_ERROR_CODES = frozenset(
    {
        "ambiguous_scope",
        "ambiguous_target",
        "concurrent_modification",
        "invalid_input",
        "not_found",
        "permission_denied",
        "schema_mismatch",
        "sync_pending",
        "unsupported_capability",
        "unexpected_error",
    }
)


def receipt_status_is_success(status: Any) -> bool:
    return status in SUCCESS_RECEIPT_STATUSES


def failed_no_mutation_evidence_error(payload: Any) -> str | None:
    """Reject a no-write label when the same payload carries commit evidence.

    A shallow launcher failure may omit Receipt objects because it occurred
    before a native helper returned.  Once a verification object is present,
    however, it must affirmatively prove that no write occurred.  Callers must
    never erase contradictory evidence while projecting a public Receipt.
    """

    if not isinstance(payload, dict) or payload.get("status") != "failed_no_mutation":
        return None
    if payload.get("ok") is not False:
        return "failed_no_mutation must set ok=false"

    after = payload.get("after")
    if after is not None and (not isinstance(after, dict) or bool(after)):
        return "failed_no_mutation must not include post-mutation state"
    data = payload.get("data")
    if data is not None and (not isinstance(data, dict) or bool(data)):
        return "failed_no_mutation must not include mutation result data"

    for field in (
        "saved",
        "mutation_attempted",
        "may_have_mutated",
        "partial_failure",
        "committed",
        "commit_succeeded",
        "write_performed",
    ):
        if payload.get(field) is True:
            return f"failed_no_mutation contradicts {field}=true"

    verification = payload.get("verification")
    if verification is not None:
        if not isinstance(verification, dict):
            return "failed_no_mutation verification must be an object"
        if verification.get("write_performed") is not False:
            return "failed_no_mutation verification must prove write_performed=false"
        if verification.get("final_read") not in {None, False}:
            return "failed_no_mutation cannot claim a post-write final read"
        if verification.get("state") not in {
            "not_performed",
            "not_needed",
            "read_back",
        }:
            return "failed_no_mutation verification state is not a no-write state"
        for field in (
            "saved",
            "mutation_attempted",
            "may_have_mutated",
            "partial_failure",
            "committed",
            "commit_succeeded",
        ):
            if verification.get(field) is True:
                return f"failed_no_mutation verification contradicts {field}=true"
    return None


def build_operation_receipt(
    *,
    status: str,
    operation: str,
    operation_id: str,
    backend: str,
    target: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    recovery: dict[str, Any] | None = None,
    warnings: list[dict[str, Any] | str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    if status not in RESULT_RECEIPT_STATUSES:
        raise ValueError(f"Unsupported operation status: {status}")
    payload: dict[str, Any] = {
        "ok": receipt_status_is_success(status),
        "status": status,
        "operation": operation,
        "operation_id": operation_id,
        "backend": backend,
        "target": target or {},
        "verification": verification or {"state": "not_requested"},
        "recovery": recovery or {"semantics": "not_applicable"},
    }
    if before is not None:
        payload["before"] = before
    if after is not None:
        payload["after"] = after
    if warnings:
        payload["warnings"] = warnings
    payload.update(extra)
    return payload


def adapter_receipt_error(
    payload: dict[str, Any], *, expected_operation: str
) -> str | None:
    """Return the MCP adapter-boundary error without weakening that boundary."""

    status = payload.get("status")
    if payload.get("ok") is True:
        if status not in SUCCESS_RECEIPT_STATUSES:
            return f"unsupported successful mutation status: {status}"
    elif payload.get("ok") is False:
        if status not in FAILURE_RECEIPT_STATUSES:
            return f"unsupported failed mutation receipt status: {status}"
    else:
        return "a mutation receipt must set ok to a boolean"
    for name in ("status", "operation", "operation_id", "backend"):
        value = payload.get(name)
        if not isinstance(value, str) or not value:
            return f"an adapter mutation receipt requires non-empty {name}"
    if payload["operation"] != expected_operation:
        return (
            "mutation receipt operation mismatch: "
            f"expected {expected_operation}, received {payload['operation']}"
        )
    for name in RECEIPT_OBJECT_FIELDS:
        if not isinstance(payload.get(name), dict):
            return f"an adapter mutation receipt requires object field {name}"
    no_write_error = failed_no_mutation_evidence_error(payload)
    if no_write_error:
        return no_write_error
    return None


def eventkit_mutation_receipt_error(
    payload: Any,
    *,
    operation: str | None,
    mutation_operations: Collection[str],
    stable_error_codes: Collection[str] = STABLE_ERROR_CODES,
) -> str | None:
    """Return the EventKit launcher-boundary error for a mutation receipt."""

    if not isinstance(payload, dict):
        return "Mutation receipt must be an object"
    expected_operation = operation or payload.get("operation")
    if expected_operation not in mutation_operations:
        return "Mutation receipt operation is not supported"
    required = {
        "ok",
        "status",
        "operation",
        "operation_id",
        "backend",
        "target",
        "after",
        "verification",
        "recovery",
    }
    if expected_operation != "create_reminder":
        required.add("before")
    missing = sorted(required - set(payload))
    if missing:
        return f"Mutation receipt is missing: {', '.join(missing)}"
    if payload["operation"] != expected_operation:
        return "Mutation receipt operation does not match the request"
    if payload["backend"] != "eventkit_public_sdk":
        return "Mutation receipt backend must be eventkit_public_sdk"
    try:
        uuid.UUID(payload["operation_id"])
    except (AttributeError, TypeError, ValueError):
        return "Mutation receipt operation_id must be a UUID"
    for key in ("target", "after", "verification", "recovery"):
        if not isinstance(payload[key], dict):
            return f"Mutation receipt {key} must be an object"
    if "before" in payload and not isinstance(payload["before"], dict):
        return "Mutation receipt before must be an object"
    if "warnings" in payload and not isinstance(payload["warnings"], list):
        return "Mutation receipt warnings must be an array"
    if payload["status"] == "committed_verification_pending":
        if payload["verification"].get("state") != "pending":
            return "Pending mutation receipt must report verification.state=pending"
        if not payload.get("warnings"):
            return "Pending mutation receipt must include a warning"
        error = payload.get("error")
        if not isinstance(error, dict):
            return "Pending mutation receipt must include an error object"
        if error.get("code") not in stable_error_codes or not isinstance(
            error.get("message"), str
        ):
            return "Pending mutation receipt error must include a stable code and message"
    return None
