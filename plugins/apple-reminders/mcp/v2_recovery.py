#!/usr/bin/env python3
"""Recently Deleted recovery facade for the public v2 Reminders surface.

The private store guard never crosses this module.  Callers receive a short-
lived, one-use ``del1`` capability only after an exact deleted-item read.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Protocol


DELETED_REFERENCE_PATTERN = re.compile(r"^del1\.[A-Za-z0-9_-]{32,4091}$")
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,256}$")
HEX_64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MutationState = Literal["not_mutated", "committed", "unknown"]
RECOVERY_PUBLIC_REASON_CODES = frozenset(
    {
        "active_readback_pending",
        "adapter_launch_failed",
        "adapter_output_too_large",
        "adapter_read_failed",
        "adapter_timeout",
        "adapter_unavailable",
        "concurrent_modification",
        "cross_account_restore_not_supported",
        "database_resolution_failed",
        "deleted_image_bytes_mismatch",
        "deleted_image_bytes_unavailable",
        "deleted_image_digest_unavailable",
        "deleted_reminder_not_recoverable",
        "eventkit_recovery_readback_pending",
        "expired_or_consumed_deleted_reference",
        "idempotency_key_conflict",
        "invalid_adapter_response",
        "invalid_deleted_guard",
        "invalid_deleted_reference",
        "invalid_deleted_snapshot",
        "invalid_deleted_read_kind",
        "invalid_native_recovery_guard",
        "invalid_private_version",
        "invalid_recovery_input",
        "invalid_recovery_receipt",
        "native_recovery_guard_timeout",
        "native_recovery_outcome_unknown",
        "recovery_dispatch_failed",
        "recovery_not_dispatched",
        "recovery_post_write_verification_failed",
        "recovery_readback_mismatch",
    }
)
RECOVERY_DEFAULT_REASON_BY_CODE = {
    "not_found": "deleted_reminder_not_recoverable",
    "concurrent_modification": "concurrent_modification",
    "permission_denied": "reminders_access_unavailable",
    "unsupported_capability": "recently_deleted_unsupported",
    "invalid_input": "invalid_recovery_input",
    "rate_limited": "recently_deleted_rate_limited",
    "sync_pending": "native_recovery_outcome_unknown",
    "schema_mismatch": "recently_deleted_schema_mismatch",
    "unexpected_error": "recently_deleted_operation_failed",
}


@dataclass(frozen=True)
class DeletedGuard:
    reminder_id: str
    store_identity: str
    private_version: int
    deleted_at: str
    attachment_digest: str
    native_guard_digest: str
    account_id: str


@dataclass(frozen=True)
class DeletedSnapshot:
    deleted_reminder: Mapping[str, Any]
    guard: DeletedGuard


@dataclass(frozen=True)
class RecoveryOutcome:
    receipt: Mapping[str, Any]
    mutation_state: MutationState


@dataclass(frozen=True)
class _DeletedGrant:
    guard: DeletedGuard
    expires_at: float


@dataclass(frozen=True)
class _IdempotentResult:
    fingerprint: str
    payload: Mapping[str, Any]
    mutation_state: MutationState


class RecoveryBackendPort(Protocol):
    def list_deleted(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        ...

    def read_deleted(self, reminder_id: str, attachment_limit: int) -> DeletedSnapshot:
        ...

    def recover(
        self,
        guard: DeletedGuard,
        list_id: str,
        idempotency_key: str,
    ) -> RecoveryOutcome:
        ...


class RecoveryBackendError(RuntimeError):
    def __init__(
        self,
        code: str,
        reason_code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reason_code = reason_code
        self.retryable = retryable


class DeletedReferenceRejected(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def recovery_reason_code(code: str, value: Any) -> str:
    candidate = re.sub(r"[^a-z0-9_]+", "_", str(value or "").lower()).strip("_")
    if candidate in RECOVERY_PUBLIC_REASON_CODES:
        return candidate
    return RECOVERY_DEFAULT_REASON_BY_CODE.get(
        code,
        "recently_deleted_operation_failed",
    )


def recovery_error_message(code: str, reason_code: str) -> str:
    """Return a fixed public message without adapter paths or helper details."""

    if code == "not_found":
        return "The deleted Reminder is not available in Recently Deleted."
    if code == "concurrent_modification":
        return "The deleted Reminder changed; inspect the exact item again before recovery."
    if code == "permission_denied":
        return "Reminders access is unavailable for this local operation."
    if code == "unsupported_capability":
        return "Recently Deleted recovery is unsupported in this local environment."
    if code == "invalid_input":
        return "The Recently Deleted request is invalid."
    if code == "rate_limited":
        return "Recently Deleted is temporarily rate-limited."
    if code == "sync_pending":
        return "Recovery needs a fresh exact read before another mutation."
    if code == "schema_mismatch":
        return "The local Recently Deleted schema is incompatible with this plugin build."
    return "The local Recently Deleted operation failed without a safe public detail."


def _public_error(exc: RecoveryBackendError) -> dict[str, Any]:
    reason_code = recovery_reason_code(exc.code, exc.reason_code)
    return {
        "code": exc.code,
        "reason_code": reason_code,
        "message": recovery_error_message(exc.code, reason_code),
        "retryable": exc.retryable,
    }


def _read_failure(exc: RecoveryBackendError) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": 2,
        "ok": False,
        "status": "failed_no_mutation",
        "operation": "inspect_recently_deleted",
        "error": _public_error(exc),
    }
    if exc.code == "permission_denied":
        result["next_action"] = {
            "kind": "request_access",
            "tool": "request_reminders_access",
            "retry_original_once": False,
            "message": "Grant Reminders access, then inspect Recently Deleted again.",
        }
    return result


def _public_attachment(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    attachment_type = value.get("type")
    if attachment_type == "image":
        result = {
            "id": value.get("id"),
            "type": "image",
            "filename": value.get("filename"),
            "file_size": value.get("file_size"),
            "width": value.get("width"),
            "height": value.get("height"),
        }
    elif attachment_type == "url":
        result = {"id": value.get("id"), "type": "url", "url": value.get("url")}
    else:
        return None
    return copy.deepcopy(result)


def _public_deleted(value: Any, *, include_attachments: bool) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    reminder_id = value.get("id")
    if not isinstance(reminder_id, str) or not reminder_id:
        return None
    result: dict[str, Any] = {
        field: copy.deepcopy(value.get(field))
        for field in (
            "id",
            "title",
            "completed",
            "priority",
            "created_at",
            "deleted_at",
            "expires_at",
            "account_id",
            "attachment_count",
            "image_attachment_count",
            "url_attachment_count",
        )
    }
    if include_attachments:
        raw_attachments = value.get("attachments")
        attachments = (
            [item for raw in raw_attachments if (item := _public_attachment(raw)) is not None]
            if isinstance(raw_attachments, list)
            else []
        )
        result["attachments"] = attachments
        result["attachments_truncated"] = value.get("attachments_truncated") is True
    return result


class RecoveryFacade:
    """Expose bounded deleted reads and guarded one-item recovery."""

    def __init__(
        self,
        backend: RecoveryBackendPort,
        *,
        clock: Callable[[], float] = time.monotonic,
        token_source: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        reference_ttl_seconds: float = 300.0,
        max_active_references: int = 1024,
        max_idempotency_results: int = 256,
    ) -> None:
        if reference_ttl_seconds <= 0:
            raise ValueError("reference_ttl_seconds must be positive")
        if max_active_references <= 0 or max_idempotency_results <= 0:
            raise ValueError("facade bounds must be positive")
        self._backend = backend
        self._clock = clock
        self._token_source = token_source
        self._reference_ttl_seconds = reference_ttl_seconds
        self._max_active_references = max_active_references
        self._max_idempotency_results = max_idempotency_results
        self._references: dict[str, _DeletedGrant] = {}
        self._idempotency: dict[str, _IdempotentResult] = {}
        self._lock = threading.RLock()

    def call(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        payload, _ = self.call_with_state(tool_name, arguments)
        return payload

    def call_with_state(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], MutationState | None]:
        """Return public JSON plus an internal mutation fact for the server boundary."""

        if tool_name == "inspect_recently_deleted":
            return self.inspect(arguments), None
        if tool_name == "recover_deleted_reminder":
            return self._recover_with_state(arguments)
        raise ValueError(f"unsupported recovery tool: {tool_name}")

    def _purge_references(self) -> None:
        now = self._clock()
        for reference, grant in list(self._references.items()):
            if now >= grant.expires_at:
                self._references.pop(reference, None)

    def _issue_reference(self, guard: DeletedGuard) -> str:
        with self._lock:
            self._purge_references()
            while len(self._references) >= self._max_active_references:
                self._references.pop(next(iter(self._references)))
            entropy = self._token_source()
            reference = f"del1.{entropy}"
            if not DELETED_REFERENCE_PATTERN.fullmatch(reference):
                raise RuntimeError("token_source returned invalid deleted-reference entropy")
            if reference in self._references:
                raise RuntimeError("token_source must return unique deleted-reference entropy")
            self._references[reference] = _DeletedGrant(
                guard=guard,
                expires_at=self._clock() + self._reference_ttl_seconds,
            )
            return reference

    def _consume_reference(self, reference: str) -> DeletedGuard:
        if not DELETED_REFERENCE_PATTERN.fullmatch(reference):
            raise DeletedReferenceRejected(
                "invalid_deleted_reference",
                "The deleted-item reference is not valid; inspect the exact item again.",
            )
        with self._lock:
            self._purge_references()
            grant = self._references.pop(reference, None)
        if grant is None:
            raise DeletedReferenceRejected(
                "expired_or_consumed_deleted_reference",
                "The deleted-item reference expired or was already used; inspect the exact item again.",
            )
        return grant.guard

    @staticmethod
    def _validate_snapshot(snapshot: DeletedSnapshot, reminder_id: str) -> None:
        if not isinstance(snapshot, DeletedSnapshot):
            raise RecoveryBackendError(
                "unexpected_error",
                "invalid_deleted_snapshot",
                "The local deleted-item read returned an invalid snapshot.",
            )
        deleted = snapshot.deleted_reminder
        guard = snapshot.guard
        if (
            not isinstance(deleted, Mapping)
            or deleted.get("id") != reminder_id
            or guard.reminder_id != reminder_id
            or not isinstance(guard.store_identity, str)
            or not guard.store_identity
            or not isinstance(guard.private_version, int)
            or isinstance(guard.private_version, bool)
            or not isinstance(guard.deleted_at, str)
            or not guard.deleted_at
            or not HEX_64_PATTERN.fullmatch(guard.attachment_digest)
            or not HEX_64_PATTERN.fullmatch(guard.native_guard_digest)
            or not isinstance(guard.account_id, str)
            or not guard.account_id
        ):
            raise RecoveryBackendError(
                "unexpected_error",
                "invalid_deleted_snapshot",
                "The local deleted-item read returned a mismatched recovery guard.",
            )

    def inspect(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        kind = arguments.get("kind")
        try:
            if kind == "list":
                raw = self._backend.list_deleted(arguments)
                raw_items = raw.get("items") if isinstance(raw, Mapping) else None
                if not isinstance(raw_items, list):
                    raise RecoveryBackendError(
                        "unexpected_error",
                        "invalid_deleted_list",
                        "The local Recently Deleted read returned an invalid bounded page.",
                    )
                limit = int(arguments.get("limit", 20))
                items = [
                    public
                    for raw_item in raw_items[:limit]
                    if (public := _public_deleted(raw_item, include_attachments=False))
                    is not None
                ]
                return {
                    "schema_version": 2,
                    "ok": True,
                    "status": "verified",
                    "operation": "inspect_recently_deleted",
                    "data": {
                        "kind": "list",
                        "items": items,
                        "returned": len(items),
                        "limit": limit,
                        "truncated": raw.get("truncated") is True,
                        "retention_days": 30,
                    },
                }
            if kind != "item":
                raise RecoveryBackendError(
                    "invalid_input",
                    "invalid_deleted_read_kind",
                    "kind must be list or item.",
                )
            reminder_id = arguments.get("reminder_id")
            if not isinstance(reminder_id, str) or not reminder_id:
                raise RecoveryBackendError(
                    "invalid_input",
                    "invalid_reminder_id",
                    "reminder_id must be one exact non-empty identifier.",
                )
            snapshot = self._backend.read_deleted(
                reminder_id,
                int(arguments.get("attachment_limit", 100)),
            )
            self._validate_snapshot(snapshot, reminder_id)
            public = _public_deleted(snapshot.deleted_reminder, include_attachments=True)
            if public is None:
                raise RecoveryBackendError(
                    "unexpected_error",
                    "invalid_deleted_snapshot",
                    "The exact deleted-item read returned no public Reminder.",
                )
            public["reference"] = self._issue_reference(snapshot.guard)
            return {
                "schema_version": 2,
                "ok": True,
                "status": "verified",
                "operation": "inspect_recently_deleted",
                "data": {"kind": "item", "deleted_reminder": public},
            }
        except RecoveryBackendError as exc:
            return _read_failure(exc)

    @staticmethod
    def _mutation_failure(
        *,
        list_id: str | None,
        reminder_id: str | None,
        code: str,
        reason_code: str,
        message: str,
        next_tool: str | None = None,
    ) -> dict[str, Any]:
        target: dict[str, Any] = {}
        if reminder_id:
            target["reminder_id"] = reminder_id
        if list_id:
            target["list_id"] = list_id
        result: dict[str, Any] = {
            "schema_version": 2,
            "ok": False,
            "status": "failed_no_mutation",
            "operation": "recover_deleted_reminder",
            "operation_id": str(uuid.uuid4()),
            "backend": "native_extension",
            "target": target,
            "before": {},
            "after": {},
            "verification": {
                "state": "not_needed",
                "write_performed": False,
                "final_read": False,
            },
            "recovery": {
                "semantics": "inspect_exact_deleted_item_before_retry",
                "automatic_retry_safe": False,
            },
            "error": {
                "code": code,
                "reason_code": reason_code,
                "message": message[:2000],
                "retryable": False,
            },
        }
        if next_tool:
            result["next_action"] = {
                "kind": "fresh_read",
                "tool": next_tool,
                "retry_original_once": False,
                "message": "Inspect the exact deleted Reminder again before recovery.",
            }
        return result

    def recover(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        payload, _ = self._recover_with_state(arguments)
        return payload

    def _recover_with_state(
        self,
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], MutationState]:
        reference = arguments.get("reference")
        list_id = arguments.get("list_id")
        key = arguments.get("idempotency_key")
        if (
            not isinstance(reference, str)
            or not isinstance(list_id, str)
            or not list_id
            or not isinstance(key, str)
            or not IDEMPOTENCY_PATTERN.fullmatch(key)
        ):
            return (
                self._mutation_failure(
                    list_id=list_id if isinstance(list_id, str) else None,
                    reminder_id=None,
                    code="invalid_input",
                    reason_code="invalid_recovery_input",
                    message="reference, list_id, and a valid idempotency_key are required.",
                ),
                "not_mutated",
            )

        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(
                {"reference": reference, "list_id": list_id},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self._lock:
            previous = self._idempotency.get(key_hash)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    return (
                        self._mutation_failure(
                            list_id=list_id,
                            reminder_id=None,
                            code="invalid_input",
                            reason_code="idempotency_key_conflict",
                            message="The idempotency key was already used for different recovery input.",
                        ),
                        "not_mutated",
                    )
                replay = copy.deepcopy(dict(previous.payload))
                replay["replayed"] = True
                replay["idempotency_key_hash"] = key_hash
                return replay, previous.mutation_state

        try:
            guard = self._consume_reference(reference)
        except DeletedReferenceRejected as exc:
            return (
                self._mutation_failure(
                    list_id=list_id,
                    reminder_id=None,
                    code="concurrent_modification",
                    reason_code=exc.reason_code,
                    message=str(exc),
                    next_tool="inspect_recently_deleted",
                ),
                "not_mutated",
            )

        try:
            outcome = self._backend.recover(guard, list_id, key)
        except Exception:
            outcome = RecoveryOutcome(
                receipt={
                    "schema_version": 2,
                    "ok": True,
                    "status": "committed_verification_pending",
                    "operation": "recover_deleted_reminder",
                    "operation_id": str(uuid.uuid4()),
                    "backend": "native_extension",
                    "target": {
                        "reminder_id": guard.reminder_id,
                        "list_id": list_id,
                    },
                    "before": {},
                    "after": {},
                    "verification": {
                        "state": "pending",
                        "write_performed": None,
                        "final_read": False,
                    },
                    "recovery": {
                        "semantics": "read_before_retry",
                        "automatic_retry_safe": False,
                    },
                    "warnings": [
                        {
                            "code": "verification_pending",
                            "message": "Recovery may have committed; read the exact Reminder before retrying.",
                        }
                    ],
                    "error": {
                        "code": "sync_pending",
                        "reason_code": "recovery_dispatch_failed",
                        "message": "The recovery outcome is unknown; read the exact Reminder before retrying.",
                        "retryable": False,
                    },
                    "next_action": {
                        "kind": "fresh_read",
                        "tool": "read_reminder",
                        "retry_original_once": False,
                        "message": "Read the exact Reminder before any retry.",
                    },
                },
                mutation_state="unknown",
            )
        result = copy.deepcopy(dict(outcome.receipt))
        result["schema_version"] = 2
        result["idempotency_key_hash"] = key_hash
        result["replayed"] = False
        with self._lock:
            while len(self._idempotency) >= self._max_idempotency_results:
                self._idempotency.pop(next(iter(self._idempotency)))
            self._idempotency[key_hash] = _IdempotentResult(
                fingerprint=fingerprint,
                payload=copy.deepcopy(result),
                mutation_state=outcome.mutation_state,
            )
        return result, outcome.mutation_state


__all__ = [
    "DeletedGuard",
    "DeletedSnapshot",
    "RecoveryBackendError",
    "RecoveryFacade",
    "RecoveryOutcome",
    "recovery_error_message",
    "recovery_reason_code",
]
