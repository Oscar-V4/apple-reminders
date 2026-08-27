#!/usr/bin/env python3
"""Production adapter for bounded Recently Deleted reads and guarded recovery."""

from __future__ import annotations

import copy
import re
import uuid
from typing import Any, Callable, Mapping

if __package__:
    from .v2_recovery import (
        DeletedGuard,
        DeletedSnapshot,
        RecoveryBackendError,
        RecoveryOutcome,
    )
else:  # pragma: no cover - exercised by the stdio entry point
    from v2_recovery import (
        DeletedGuard,
        DeletedSnapshot,
        RecoveryBackendError,
        RecoveryOutcome,
    )


AdapterCall = Callable[[list[str]], tuple[dict[str, Any], bool]]
BridgeCall = Callable[[str, dict[str, Any]], tuple[dict[str, Any], bool]]
ReceiptValidator = Callable[..., str | None]
HEX_64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
STATUSES = {
    "unchanged",
    "verified",
    "committed_verification_pending",
    "partial_success",
    "failed_no_mutation",
    "failed_manual_repair_required",
}
PUBLIC_CODES = {
    "ambiguous_scope",
    "ambiguous_target",
    "concurrent_modification",
    "invalid_input",
    "not_found",
    "permission_denied",
    "rate_limited",
    "schema_mismatch",
    "sync_pending",
    "unsupported_capability",
    "unexpected_error",
}


def _reason(value: Any, fallback: str) -> str:
    candidate = value if isinstance(value, str) else fallback
    normalized = re.sub(r"[^a-z0-9_]+", "_", candidate.lower()).strip("_")
    return (normalized or fallback)[:128]


def _public_code(value: Any) -> str:
    if value in PUBLIC_CODES:
        return str(value)
    text = str(value or "")
    if "not_found" in text or "not_recoverable" in text:
        return "not_found"
    if "concurrent" in text or "reference" in text or "changed" in text:
        return "concurrent_modification"
    if "permission" in text or "access" in text:
        return "permission_denied"
    if "unsupported" in text or "cross_account" in text:
        return "unsupported_capability"
    if "invalid" in text:
        return "invalid_input"
    if "pending" in text or "timeout" in text or "sync" in text:
        return "sync_pending"
    return "unexpected_error"


def _error_from_payload(payload: Mapping[str, Any]) -> RecoveryBackendError:
    raw = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
    code = _public_code(raw.get("code") or payload.get("code"))
    reason = _reason(raw.get("reason_code") or raw.get("code"), "adapter_read_failed")
    message = raw.get("message") or payload.get("message") or "The local Recently Deleted read failed."
    return RecoveryBackendError(
        code,
        reason,
        str(message),
        retryable=bool(raw.get("retryable")) and code not in {"sync_pending", "concurrent_modification"},
    )


def _public_deleted_before(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_before = payload.get("before")
    deleted = raw_before.get("deleted_reminder") if isinstance(raw_before, Mapping) else None
    if not isinstance(deleted, Mapping):
        return {}
    return {
        "deleted_reminder": {
            "id": deleted.get("id"),
            "deleted_at": deleted.get("deleted_at"),
            "attachment_count": deleted.get("attachment_count"),
        }
    }


def _public_recovered_after(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_after = payload.get("after")
    reminder = raw_after.get("reminder") if isinstance(raw_after, Mapping) else None
    if not isinstance(reminder, Mapping):
        return {}
    return {
        "reminder": {
            "id": reminder.get("id"),
            "list_id": reminder.get("list_id"),
            "attachment_count": reminder.get("attachment_count"),
        }
    }


class RecoveryBackend:
    def __init__(
        self,
        *,
        adapter_call: AdapterCall,
        bridge_call: BridgeCall,
        receipt_validator: ReceiptValidator,
    ) -> None:
        self._adapter_call = adapter_call
        self._bridge_call = bridge_call
        self._receipt_validator = receipt_validator

    def list_deleted(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        argv = ["list_deleted_reminders", "--limit", str(int(arguments.get("limit", 20)))]
        account_id = arguments.get("account_id")
        if isinstance(account_id, str) and account_id:
            argv.extend(["--account-id", account_id])
        payload, is_error = self._adapter_call(argv)
        items = payload.get("deleted_reminders")
        if is_error or payload.get("ok") is not True or not isinstance(items, list):
            raise _error_from_payload(payload)
        return {
            "items": copy.deepcopy(items),
            "returned": len(items),
            "limit": int(arguments.get("limit", 20)),
            "truncated": payload.get("truncated") is True,
        }

    def read_deleted(self, reminder_id: str, attachment_limit: int) -> DeletedSnapshot:
        payload, is_error = self._adapter_call(
            [
                "read_deleted_reminder",
                "--id",
                reminder_id,
                "--attachment-limit",
                str(attachment_limit),
            ]
        )
        deleted = payload.get("deleted_reminder")
        guard = payload.get("guard")
        if (
            is_error
            or payload.get("ok") is not True
            or not isinstance(deleted, Mapping)
            or not isinstance(guard, Mapping)
        ):
            raise _error_from_payload(payload)
        private_version = guard.get("private_version")
        if not isinstance(private_version, int) or isinstance(private_version, bool):
            raise RecoveryBackendError(
                "unexpected_error",
                "invalid_private_version",
                "The deleted Reminder did not provide a valid recovery revision.",
            )
        values = {
            "reminder_id": guard.get("reminder_id"),
            "store_identity": guard.get("store_identity"),
            "deleted_at": guard.get("deleted_at"),
            "attachment_digest": guard.get("attachment_digest"),
            "account_id": guard.get("account_id"),
        }
        if (
            values["reminder_id"] != reminder_id
            or not all(isinstance(value, str) and value for value in values.values())
            or not HEX_64_PATTERN.fullmatch(str(values["attachment_digest"]))
        ):
            raise RecoveryBackendError(
                "unexpected_error",
                "invalid_deleted_guard",
                "The deleted Reminder returned a mismatched recovery guard.",
            )
        return DeletedSnapshot(
            deleted_reminder=copy.deepcopy(dict(deleted)),
            guard=DeletedGuard(private_version=private_version, **values),
        )

    @staticmethod
    def _pending(
        guard: DeletedGuard,
        list_id: str,
        *,
        reason_code: str,
        message: str,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        write_performed: bool | None = None,
    ) -> RecoveryOutcome:
        return RecoveryOutcome(
            receipt={
                "ok": True,
                "status": "committed_verification_pending",
                "operation": "recover_deleted_reminder",
                "operation_id": str(uuid.uuid4()),
                "backend": "native_extension",
                "target": {"reminder_id": guard.reminder_id, "list_id": list_id},
                "before": copy.deepcopy(dict(before or {})),
                "after": copy.deepcopy(dict(after or {})),
                "verification": {
                    "state": "pending",
                    "write_performed": write_performed,
                    "final_read": False,
                    "evidence_scope": ["local_native"],
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
                    "reason_code": reason_code,
                    "message": message,
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

    def recover(
        self,
        guard: DeletedGuard,
        list_id: str,
        idempotency_key: str,
    ) -> RecoveryOutcome:
        argv = [
            "recover_deleted_reminder",
            "--id",
            guard.reminder_id,
            "--list-id",
            list_id,
            "--if-store-identity",
            guard.store_identity,
            "--if-version",
            str(guard.private_version),
            "--if-deleted-at",
            guard.deleted_at,
            "--if-attachment-digest",
            guard.attachment_digest,
            "--idempotency-key",
            idempotency_key,
        ]
        payload, _ = self._adapter_call(argv)
        if self._receipt_validator(
            payload,
            expected_operation="recover_deleted_reminder",
        ) is not None or payload.get("status") not in STATUSES:
            return self._pending(
                guard,
                list_id,
                reason_code="invalid_recovery_receipt",
                message="Recovery was dispatched, but its receipt could not be validated.",
            )

        status = str(payload["status"])
        before = _public_deleted_before(payload)
        after = _public_recovered_after(payload)
        raw_error = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
        code = _public_code(raw_error.get("code"))
        reason_code = _reason(raw_error.get("reason_code") or raw_error.get("code"), "recovery_failed")
        message = str(raw_error.get("message") or "The deleted Reminder recovery failed.")[:2000]

        if status == "failed_no_mutation":
            receipt: dict[str, Any] = {
                "ok": False,
                "status": status,
                "operation": "recover_deleted_reminder",
                "operation_id": str(payload.get("operation_id") or uuid.uuid4()),
                "backend": "native_extension",
                "target": {"reminder_id": guard.reminder_id, "list_id": list_id},
                "before": before,
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
                    "message": message,
                    "retryable": False,
                },
            }
            if code == "concurrent_modification":
                receipt["next_action"] = {
                    "kind": "fresh_read",
                    "tool": "inspect_recently_deleted",
                    "retry_original_once": False,
                    "message": "Inspect the exact deleted Reminder again before recovery.",
                }
            return RecoveryOutcome(receipt=receipt, mutation_state="not_mutated")

        if status == "verified":
            bridge, bridge_error = self._bridge_call(
                "read_reminder",
                {"reminder_id": guard.reminder_id},
            )
            data = bridge.get("data") if isinstance(bridge, Mapping) else None
            active = data.get("reminder") if isinstance(data, Mapping) else None
            exact_match = (
                not bridge_error
                and bridge.get("ok") is True
                and bridge.get("status") == "verified"
                and isinstance(active, Mapping)
                and active.get("id") == guard.reminder_id
                and active.get("list_id", active.get("calendar_id")) == list_id
            )
            if not exact_match:
                return self._pending(
                    guard,
                    list_id,
                    reason_code="eventkit_recovery_readback_pending",
                    message="Native recovery committed, but EventKit has not exposed the exact active Reminder yet.",
                    before=before,
                    after=after,
                    write_performed=True,
                )
            recovered = after.get("reminder") if isinstance(after.get("reminder"), Mapping) else {}
            recovered = {
                **copy.deepcopy(dict(recovered)),
                "id": active.get("id"),
                "title": active.get("title"),
                "list_id": active.get("list_id", active.get("calendar_id")),
                "last_modified": active.get("last_modified"),
            }
            return RecoveryOutcome(
                receipt={
                    "ok": True,
                    "status": "verified",
                    "operation": "recover_deleted_reminder",
                    "operation_id": str(payload.get("operation_id") or uuid.uuid4()),
                    "backend": "native_extension",
                    "target": {"reminder_id": guard.reminder_id, "list_id": list_id},
                    "before": before,
                    "after": {"reminder": recovered},
                    "verification": {
                        "state": "read_back",
                        "write_performed": True,
                        "final_read": True,
                        "matched": True,
                        "attachments_preserved": payload.get("verification", {}).get(
                            "attachments_preserved"
                        )
                        is True,
                        "evidence_scope": ["local_native", "local_eventkit"],
                    },
                    "recovery": {
                        "semantics": "recently_deleted_recovery",
                        "automatic_retry_safe": False,
                    },
                },
                mutation_state="committed",
            )

        raw_verification = payload.get("verification")
        write_performed = (
            raw_verification.get("write_performed")
            if isinstance(raw_verification, Mapping)
            and raw_verification.get("write_performed") in {True, None}
            else None
        )
        public_status = status
        verification = {
            "state": "pending" if status == "committed_verification_pending" else "read_back",
            "write_performed": write_performed,
            "final_read": False
            if status == "committed_verification_pending"
            else bool(isinstance(raw_verification, Mapping) and raw_verification.get("final_read")),
            "evidence_scope": ["local_native"],
        }
        if status == "partial_success" and verification["final_read"]:
            verification["matched"] = False
        if status == "failed_manual_repair_required":
            verification["state"] = "manual_repair_required"
        receipt = {
            "ok": status in {"partial_success", "committed_verification_pending"},
            "status": public_status,
            "operation": "recover_deleted_reminder",
            "operation_id": str(payload.get("operation_id") or uuid.uuid4()),
            "backend": "native_extension",
            "target": {"reminder_id": guard.reminder_id, "list_id": list_id},
            "before": before,
            "after": after,
            "verification": verification,
            "recovery": {
                "semantics": "read_before_retry",
                "automatic_retry_safe": False,
            },
            "warnings": [
                {
                    "code": "verification_pending",
                    "message": "Recovery needs an exact active Reminder read before any retry.",
                }
            ],
            "error": {
                "code": "sync_pending",
                "reason_code": reason_code,
                "message": message,
                "retryable": False,
            },
            "next_action": {
                "kind": "fresh_read",
                "tool": "read_reminder",
                "retry_original_once": False,
                "message": "Read the exact Reminder before any retry.",
            },
        }
        return RecoveryOutcome(receipt=receipt, mutation_state="unknown")


__all__ = ["RecoveryBackend"]
