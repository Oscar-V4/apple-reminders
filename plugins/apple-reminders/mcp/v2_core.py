#!/usr/bin/env python3
"""Deep Core facade for the public v2 Apple Reminders MCP interface.

The facade deliberately speaks in Reminder List vocabulary.  EventKit's
``calendar_*`` names exist only inside :class:`EventKitCoreAdapter` and the
injected bridge port.  Exact reads and guarded changes are delegated to the
existing ``CoreModule`` so the transport does not grow a second reference
store or concurrency policy.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from reminders_service import (  # noqa: E402
    ActionRejected,
    AdapterConflict,
    AdapterContractError,
    ChangeResult,
    CoreAction,
    CoreModule,
    ExactRead,
    Guard,
    MoveToListAction,
    MutationOutcome,
    MutationOutcomeUnknown,
    PatchAction,
    ReferenceRejected,
    SetCompletionAction,
    Snapshot,
)
if __package__:  # Package import in tests; script-local import in the stdio server.
    from .v2_contract import MUTATION_STATUSES, SUCCESS_STATUSES
else:  # pragma: no cover - exercised by the script entry point
    from v2_contract import MUTATION_STATUSES, SUCCESS_STATUSES


PUBLIC_ERROR_CODES = frozenset(
    {
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
)
PUBLIC_CHANGE_OPERATIONS = {
    "patch": "change_reminder.patch",
    "set_completion": "change_reminder.set_completion",
    "move_to_list": "change_reminder.move_to_list",
}
PUBLIC_REMINDER_FIELDS = (
    "id",
    "external_id",
    "title",
    "notes",
    "url",
    "location",
    "priority",
    "completed",
    "completion_date",
    "due",
    "start",
    "alarms",
    "recurrence_rules",
    "created",
    "last_modified",
    "source_id",
    "source_title",
)
PUBLIC_CHANGE_REMINDER_FIELDS = (
    "id",
    "title",
    "notes",
    "url",
    "priority",
    "completed",
    "due",
    "alarms",
    "recurrence_rules",
    "last_modified",
)
FETCH_CURSOR_FIELDS = (
    "list_ids",
    "status",
    "query",
    "due_start",
    "due_end",
    "completion_start",
    "completion_end",
    "modified_after",
    "limit",
    "sort",
)
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,256}$")


@dataclass(frozen=True)
class EventKitReply:
    """One validated bridge reply plus its transport-level error signal."""

    payload: Mapping[str, Any]
    is_error: bool = False


@dataclass(frozen=True)
class _IdempotentResult:
    fingerprint: str
    payload: dict[str, Any]


class EventKitPort(Protocol):
    """Injectable boundary around the bundled EventKit launcher.

    Production wiring can dispatch reads to ``invoke_eventkit_bridge`` and
    mutations to the existing hybrid mutation wrapper.  Tests can provide a
    deterministic in-memory implementation without environment variables or
    repository-relative test hooks.
    """

    def invoke(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        mutation: bool,
    ) -> EventKitReply:
        ...


class CoreReferencePort(Protocol):
    """Shared opaque-reference port used by Core and Native facades."""

    def read_exact(self, reminder_id: str) -> ExactRead:
        ...

    def change(self, reference: str, raw_action: Mapping[str, Any]) -> ChangeResult:
        ...

    def revalidate_reference(self, reference: str) -> Guard:
        ...

    def invalidate_reference(self, reference: str) -> None:
        ...


class FacadeInputError(ValueError):
    """The caller supplied an invalid value after schema validation."""


class EventKitOperationError(RuntimeError):
    """A read operation returned a structured EventKit failure."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        super().__init__("EventKit operation failed")
        self.payload = copy.deepcopy(dict(payload))


class UnsafeRevisionError(AdapterContractError):
    """EventKit returned an exact item that cannot guard a later write."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _deep_dict(value: Any) -> dict[str, Any]:
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _trimmed_string(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise FacadeInputError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise FacadeInputError(f"{name} must not be empty")
    if len(normalized) > maximum:
        raise FacadeInputError(f"{name} exceeds its {maximum}-character limit")
    return normalized


def _public_source(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return copy.deepcopy(value)
    raw = dict(value)
    source_type = str(raw.get("type") or "unknown")
    if source_type not in {
        "local",
        "exchange",
        "caldav",
        "mobile_me",
        "subscribed",
        "birthdays",
        "unknown",
    }:
        source_type = "unknown"
    raw_count = raw.get("reminder_list_count", raw.get("reminder_calendar_count", 0))
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else 0
    return {
        "id": str(raw.get("id") or "")[:2048],
        "title": str(raw.get("title") or "")[:512],
        "type": source_type,
        "is_delegate": raw.get("is_delegate") is True,
        "reminder_list_count": max(0, min(count, 10_000)),
    }


def _public_list(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return copy.deepcopy(value)
    raw = dict(value)
    list_type = str(raw.get("type") or "unknown")
    if list_type not in {
        "local",
        "caldav",
        "exchange",
        "subscription",
        "birthday",
        "unknown",
    }:
        list_type = "unknown"
    return {
        "id": str(raw.get("id") or raw.get("list_id") or "")[:2048],
        "title": str(raw.get("title") or raw.get("name") or "")[:512],
        "type": list_type,
        "allows_content_modifications": raw.get("allows_content_modifications") is True,
        "subscribed": raw.get("subscribed") is True,
        "immutable": raw.get("immutable") is True,
        "source": _public_source(raw.get("source")),
    }


def _public_reminder(value: Mapping[str, Any]) -> dict[str, Any]:
    reminder = {
        field: copy.deepcopy(value.get(field)) for field in PUBLIC_REMINDER_FIELDS
    }
    reminder["list_id"] = copy.deepcopy(
        value.get("list_id", value.get("calendar_id"))
    )
    reminder["list_title"] = copy.deepcopy(
        value.get("list_title", value.get("calendar_title"))
    )
    return reminder


def _bounded_text(value: Any, maximum: int) -> str | None:
    return value[:maximum] if isinstance(value, str) else None


def _bounded_due(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for field, maximum in (
        ("kind", 16),
        ("date", 10),
        ("date_time", 64),
        ("local_date_time", 64),
        ("time_zone", 128),
    ):
        if value.get(field) is None and field in value:
            result[field] = None
        elif isinstance(value.get(field), str):
            result[field] = value[field][:maximum]
    if isinstance(value.get("floating"), bool):
        result["floating"] = value["floating"]
    return result or None


def _bounded_alarm(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for field, maximum in (("kind", 16), ("date_time", 64), ("proximity", 16)):
        if isinstance(value.get(field), str):
            result[field] = value[field][:maximum]
    if isinstance(value.get("offset_seconds"), (int, float)) and not isinstance(
        value.get("offset_seconds"), bool
    ):
        result["offset_seconds"] = value["offset_seconds"]
    if isinstance(value.get("read_only"), bool):
        result["read_only"] = value["read_only"]
    location = value.get("location")
    if isinstance(location, Mapping):
        public_location: dict[str, Any] = {}
        if isinstance(location.get("title"), str):
            public_location["title"] = location["title"][:1000]
        for field in ("latitude", "longitude", "radius_meters"):
            candidate = location.get(field)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                public_location[field] = candidate
        if public_location:
            result["location"] = public_location
    return result or None


def _public_reminder_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    raw_alarms = value.get("alarms") if isinstance(value.get("alarms"), list) else []
    alarms = [
        item
        for item in (_bounded_alarm(raw) for raw in raw_alarms[:5])
        if item is not None
    ]
    raw_recurrence = value.get("recurrence_rules")
    return {
        "id": _bounded_text(value.get("id"), 2048),
        "title": _bounded_text(value.get("title"), 2000) or "",
        "notes": _bounded_text(value.get("notes"), 2000),
        "url": _bounded_text(value.get("url"), 8192),
        "location": _bounded_text(value.get("location"), 1000),
        "priority": value.get("priority") if isinstance(value.get("priority"), int) else 0,
        "completed": value.get("completed") is True,
        "completion_date": _bounded_text(value.get("completion_date"), 64),
        "due": _bounded_due(value.get("due")),
        "start": _bounded_due(value.get("start")),
        "alarms": alarms,
        "alarm_count": len(raw_alarms),
        "recurring": isinstance(raw_recurrence, list) and bool(raw_recurrence),
        "last_modified": _bounded_text(value.get("last_modified"), 64),
        "list_id": _bounded_text(value.get("list_id", value.get("calendar_id")), 2048),
        "list_title": _bounded_text(
            value.get("list_title", value.get("calendar_title")), 512
        ),
        "source_id": _bounded_text(value.get("source_id"), 2048),
        "source_title": _bounded_text(value.get("source_title"), 512),
    }


def _change_reminder(
    value: Any,
    *,
    reference: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or not value:
        return None
    reminder = {
        field: copy.deepcopy(value.get(field))
        for field in PUBLIC_CHANGE_REMINDER_FIELDS
    }
    reminder["list_id"] = copy.deepcopy(
        value.get("list_id", value.get("calendar_id"))
    )
    reminder["url_attachment"] = copy.deepcopy(value.get("url_attachment"))
    if reference is not None:
        reminder["reference"] = reference
    return reminder


def _reason_code(value: Any, fallback: str) -> str:
    candidate = value if isinstance(value, str) else fallback
    normalized = re.sub(r"[^a-z0-9_]+", "_", candidate.lower()).strip("_")
    return (normalized or fallback)[:128]


def _stable_error_code(error: Mapping[str, Any]) -> str:
    code = error.get("code")
    reason = str(error.get("reason_code") or code or "")
    category = str(error.get("category") or "")
    if category == "not_found" or reason.endswith("not_found"):
        return "not_found"
    if code in PUBLIC_ERROR_CODES:
        return str(code)
    if "concurrent" in reason or "stale" in reason or reason in {
        "expired_reference",
        "invalid_reference",
    }:
        return "concurrent_modification"
    if "permission" in category or "authoriz" in reason or "access_denied" in reason:
        return "permission_denied"
    if "ambiguous" in reason or reason == "unbounded_read":
        return "ambiguous_scope"
    if "unsupported" in category or "unsupported" in reason:
        return "unsupported_capability"
    if "schema" in reason:
        return "schema_mismatch"
    if "pending" in reason or "timeout" in reason or error.get("retryable") is True:
        return "sync_pending"
    if category in {"invalid_request", "validation"} or reason.startswith("invalid"):
        return "invalid_input"
    return "unexpected_error"


def _public_error(
    value: Any,
    *,
    fallback_reason: str = "unexpected_error",
    fallback_message: str = "The local Reminders operation failed.",
) -> dict[str, Any]:
    error = value if isinstance(value, Mapping) else {}
    reason = _reason_code(
        error.get("reason_code") or error.get("code"),
        fallback_reason,
    )
    message = error.get("message")
    if not isinstance(message, str) or not message.strip():
        message = fallback_message
    return {
        "code": _stable_error_code(error),
        "reason_code": reason,
        "message": message[:2000],
        "retryable": bool(error.get("retryable", False)),
    }


def _next_action(
    error: Mapping[str, Any],
    *,
    operation: str | None = None,
) -> dict[str, Any] | None:
    code = error.get("code")
    if code == "permission_denied":
        return {
            "kind": "request_access",
            "tool": "request_reminders_access",
            "retry_original_once": True,
            "message": "Request Reminders access, then retry this operation once.",
        }
    if code in {"concurrent_modification", "sync_pending"}:
        return {
            "kind": "fresh_read",
            "tool": (
                "fetch_reminders"
                if operation == "create_reminder"
                else "read_reminder"
            ),
            "retry_original_once": False,
            "message": (
                "Fetch the target list and resolve whether the create committed before retrying."
                if operation == "create_reminder"
                else "Read the exact Reminder again before attempting another change."
            ),
        }
    return None


def _read_failure(
    operation: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    error = _public_error(payload.get("error"))
    result: dict[str, Any] = {
        "schema_version": 2,
        "ok": False,
        "status": "failed_no_mutation",
        "operation": operation,
        "error": error,
    }
    next_action = _next_action(error)
    if next_action is not None:
        result["next_action"] = next_action
    return result


def _cursor_fingerprint(arguments: Mapping[str, Any]) -> str:
    immutable = {
        field: copy.deepcopy(arguments[field])
        for field in FETCH_CURSOR_FIELDS
        if field in arguments
    }
    if isinstance(immutable.get("list_ids"), list):
        immutable["list_ids"] = sorted(immutable["list_ids"])
    encoded = json.dumps(
        immutable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _encode_cursor(offset: int, arguments: Mapping[str, Any]) -> str:
    value = {
        "v": 2,
        "o": offset,
        "f": _cursor_fingerprint(arguments),
    }
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Any, arguments: Mapping[str, Any]) -> int:
    if not isinstance(cursor, str) or not cursor:
        raise FacadeInputError("cursor must be a non-empty opaque string")
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FacadeInputError("cursor is not a valid v2 pagination cursor") from exc
    if not isinstance(decoded, dict) or set(decoded) != {"v", "o", "f"}:
        raise FacadeInputError("cursor has an unsupported structure")
    offset = decoded.get("o")
    if (
        decoded.get("v") != 2
        or not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or offset > 10_000
    ):
        raise FacadeInputError("cursor has an unsupported version or offset")
    if decoded.get("f") != _cursor_fingerprint(arguments):
        raise FacadeInputError("cursor cannot be reused with different fetch filters")
    if _encode_cursor(offset, arguments) != cursor:
        raise FacadeInputError("cursor is not in canonical form")
    return offset


def _verification(
    value: Any,
    *,
    status: str,
) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    state = raw.get("state")
    if state not in {
        "not_needed",
        "read_back",
        "pending",
        "partial",
        "revalidated_under_lock",
    }:
        state = "pending" if status == "committed_verification_pending" else "not_needed"
    write_performed = raw.get("write_performed")
    if not isinstance(write_performed, bool):
        write_performed = (
            None
            if status
            in {
                "committed_verification_pending",
                "partial_success",
                "failed_manual_repair_required",
            }
            else status not in {"unchanged", "failed_no_mutation"}
        )
    final_read = raw.get("final_read")
    if not isinstance(final_read, bool):
        eventkit_final = raw.get("eventkit_final_read")
        final_read = state == "read_back" or (
            isinstance(eventkit_final, Mapping)
            and eventkit_final.get("state") == "read_back"
        )
    result: dict[str, Any] = {
        "state": state,
        "write_performed": write_performed,
        "final_read": final_read,
    }
    for field in (
        "matched",
        "local_absence",
        "visible_url_attachment",
        "mobile_visible_likely",
        "cloud_version",
    ):
        if field in raw:
            result[field] = copy.deepcopy(raw[field])
    store_no_longer_active = raw.get("store_no_longer_active")
    if (
        "local_absence" not in result
        and isinstance(store_no_longer_active, bool)
    ):
        # The bundled EventKit helper names its exact post-delete lookup this
        # way. Public callers receive the backend-neutral absence contract.
        result["local_absence"] = store_no_longer_active
        if store_no_longer_active and "matched" not in result:
            result["matched"] = True
    url_attachment = raw.get("url_attachment")
    if (
        "visible_url_attachment" not in result
        and isinstance(url_attachment, Mapping)
        and isinstance(url_attachment.get("attachment_active"), bool)
    ):
        result["visible_url_attachment"] = url_attachment["attachment_active"]
    return result


def _recovery(value: Any, *, status: str) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    semantics = raw.get("semantics")
    if not isinstance(semantics, str) or not semantics:
        semantics = "read_before_retry"
    automatic = raw.get("automatic_retry_safe")
    if not isinstance(automatic, bool):
        automatic = status == "unchanged" or semantics == "not_applicable"
    result: dict[str, Any] = {
        "semantics": semantics[:128],
        "automatic_retry_safe": automatic,
    }
    for field in ("recently_deleted_expected", "manual_action", "snapshot"):
        if field in raw:
            result[field] = copy.deepcopy(raw[field])
    return result


def _warnings(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value[:20]:
        if not isinstance(item, Mapping):
            continue
        code = _reason_code(item.get("code"), "warning")
        message = item.get("message")
        if isinstance(message, str) and message:
            result.append({"code": code, "message": message[:2000]})
    return result


class EventKitCoreAdapter:
    """Translate one EventKit receipt at a time into the CoreModule seam."""

    def __init__(
        self,
        eventkit: EventKitPort,
        *,
        operation_id_source: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._eventkit = eventkit
        self._operation_id_source = operation_id_source

    def read_exact(self, reminder_id: str) -> Snapshot:
        reply = self._eventkit.invoke(
            "read_reminder",
            {"reminder_id": reminder_id},
            mutation=False,
        )
        payload = _deep_dict(reply.payload)
        if reply.is_error or payload.get("ok") is not True or payload.get("status") != "verified":
            raise EventKitOperationError(payload)
        data = payload.get("data")
        raw_reminder = data.get("reminder") if isinstance(data, Mapping) else None
        if not isinstance(raw_reminder, Mapping):
            raise UnsafeRevisionError(
                "invalid_exact_read",
                "EventKit exact read did not return a Reminder object",
            )
        reminder = _public_reminder(raw_reminder)
        if reminder.get("id") != reminder_id:
            raise UnsafeRevisionError(
                "exact_read_target_mismatch",
                "EventKit exact read returned a different Reminder identity",
            )
        revision = reminder.get("last_modified")
        if not isinstance(revision, str) or not revision:
            raise UnsafeRevisionError(
                "missing_last_modified",
                "EventKit did not return a revision that can guard a later write",
            )
        source_id = reminder.get("source_id")
        list_id = reminder.get("list_id")
        store_part = source_id if isinstance(source_id, str) and source_id else list_id
        if not isinstance(store_part, str) or not store_part:
            raise UnsafeRevisionError(
                "missing_store_identity",
                "EventKit exact read did not return a stable source or Reminder List identity",
            )
        return Snapshot(
            reminder=reminder,
            guard=Guard(
                reminder_id=reminder_id,
                store_identity=f"eventkit:{store_part}",
                public_concurrency_value=revision,
            ),
        )

    def apply_action(self, guard: Guard, action: CoreAction) -> MutationOutcome:
        arguments: dict[str, Any] = {
            "reminder_id": guard.reminder_id,
            "expected_last_modified": guard.public_concurrency_value,
        }
        if isinstance(action, PatchAction):
            native_operation = "update_reminder"
            public_operation = PUBLIC_CHANGE_OPERATIONS["patch"]
            arguments["patch"] = copy.deepcopy(dict(action.patch))
        elif isinstance(action, SetCompletionAction):
            native_operation = "complete_reminder" if action.completed else "reopen_reminder"
            public_operation = PUBLIC_CHANGE_OPERATIONS["set_completion"]
        elif isinstance(action, MoveToListAction):
            native_operation = "move_reminder"
            public_operation = PUBLIC_CHANGE_OPERATIONS["move_to_list"]
            arguments["calendar_id"] = action.list_id
        else:  # pragma: no cover - CoreAction is a closed union.
            raise AdapterContractError("Core supplied an unsupported action type")

        reply = self._eventkit.invoke(
            native_operation,
            arguments,
            mutation=True,
        )
        payload = _deep_dict(reply.payload)
        status = payload.get("status")
        if not isinstance(status, str):
            raise AdapterContractError("EventKit mutation omitted its receipt status")
        raw_error = payload.get("error")
        public_error = _public_error(raw_error)
        if status == "failed_no_mutation" and public_error["code"] == "concurrent_modification":
            raise AdapterConflict(public_error["reason_code"])

        target_raw = payload.get("target")
        target = target_raw if isinstance(target_raw, Mapping) else {}
        target_list_id = target.get("list_id", target.get("calendar_id"))
        if target_list_id is None and isinstance(action, MoveToListAction):
            target_list_id = action.list_id
        public_target: dict[str, Any] = {"reminder_id": guard.reminder_id}
        if isinstance(target_list_id, str) and target_list_id:
            public_target["list_id"] = target_list_id

        hybrid_url = (
            isinstance(action, PatchAction)
            and isinstance(action.patch.get("url"), str)
        )
        receipt: dict[str, Any] = {
            "ok": status not in {"failed_no_mutation", "failed_manual_repair_required"},
            "status": status,
            "operation": public_operation,
            "operation_id": str(payload.get("operation_id") or self._operation_id_source()),
            "backend": (
                "eventkit_plus_native_url"
                if hybrid_url
                else "eventkit_public_sdk"
            ),
            "target": public_target,
            "before": _public_reminder(payload["before"])
            if isinstance(payload.get("before"), Mapping) and payload["before"]
            else {},
            "after": _public_reminder(payload["after"])
            if isinstance(payload.get("after"), Mapping) and payload["after"]
            else {},
            "verification": _verification(payload.get("verification"), status=status),
            "recovery": _recovery(payload.get("recovery"), status=status),
        }
        warnings = _warnings(payload.get("warnings"))
        if warnings:
            receipt["warnings"] = warnings
        if status in {"failed_no_mutation", "failed_manual_repair_required"} or isinstance(
            raw_error, Mapping
        ):
            receipt["error"] = public_error

        if status in {"unchanged", "failed_no_mutation"}:
            mutation_state = "not_mutated"
        elif status == "verified":
            mutation_state = "committed"
        elif status == "partial_success":
            mutation_state = (
                "committed"
                if receipt["verification"]["write_performed"] is True
                and receipt["verification"]["final_read"]
                else "unknown"
            )
        elif status in {
            "committed_verification_pending",
            "failed_manual_repair_required",
        }:
            mutation_state = "unknown"
        else:
            raise AdapterContractError(f"EventKit returned unsupported status: {status}")
        return MutationOutcome(receipt=receipt, mutation_state=mutation_state)


class V2CoreFacade:
    """One cohesive v2 surface for Core Reminder reads and guarded changes."""

    def __init__(
        self,
        eventkit: EventKitPort,
        *,
        reference_port: CoreReferencePort | None = None,
        clock: Callable[[], float] = time.monotonic,
        token_source: Callable[[], str] | None = None,
        operation_id_source: Callable[[], str] = lambda: str(uuid.uuid4()),
        reference_ttl_seconds: float = 300.0,
        max_active_references: int = 1024,
        max_idempotency_results: int = 256,
    ) -> None:
        if max_idempotency_results <= 0:
            raise ValueError("Facade bounds must be positive")
        self._eventkit = eventkit
        self._operation_id_source = operation_id_source
        self._max_idempotency_results = max_idempotency_results
        self._idempotency: dict[str, _IdempotentResult] = {}
        self._idempotency_lock = threading.RLock()
        self._adapter = EventKitCoreAdapter(
            eventkit,
            operation_id_source=operation_id_source,
        )
        if reference_port is None:
            core_arguments: dict[str, Any] = {
                "clock": clock,
                "reference_ttl_seconds": reference_ttl_seconds,
                "max_active_references": max_active_references,
            }
            if token_source is not None:
                core_arguments["token_source"] = token_source
            reference_port = CoreModule(self._adapter, **core_arguments)
        self._references = reference_port

    @property
    def reference_port(self) -> CoreReferencePort:
        """Return the shared internal port for Native-extension composition."""

        return self._references

    def call(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        routes: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
            "request_reminders_access": self.request_reminders_access,
            "list_reminder_lists": self.list_reminder_lists,
            "fetch_reminders": self.fetch_reminders,
            "read_reminder": self.read_reminder,
            "create_reminder": self.create_reminder,
            "change_reminder": self.change_reminder,
            "delete_reminder": self.delete_reminder,
            "ensure_reminder_list": self.ensure_reminder_list,
        }
        try:
            route = routes[tool_name]
        except KeyError as exc:
            raise FacadeInputError(f"unsupported v2 Core tool: {tool_name}") from exc
        return route(arguments)

    def request_reminders_access(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if arguments:
            return _read_failure(
                "request_reminders_access",
                {
                    "error": {
                        "code": "invalid_input",
                        "reason_code": "unexpected_access_arguments",
                        "message": "request_reminders_access does not accept arguments.",
                        "retryable": False,
                    }
                },
            )
        reply = self._eventkit.invoke("request_access", {}, mutation=False)
        payload = _deep_dict(reply.payload)
        data = payload.get("data")
        if (
            reply.is_error
            or payload.get("ok") is not True
            or payload.get("status") != "verified"
            or not isinstance(data, Mapping)
        ):
            return _read_failure("request_reminders_access", payload)
        authorization = data.get("authorization")
        if authorization not in {
            "not_determined",
            "restricted",
            "denied",
            "full_access",
            "write_only",
            "unknown",
        }:
            return _read_failure(
                "request_reminders_access",
                {
                    "error": {
                        "code": "unexpected_error",
                        "reason_code": "invalid_access_response",
                        "message": "EventKit returned an invalid authorization state.",
                        "retryable": False,
                    }
                },
            )
        return {
            "schema_version": 2,
            "ok": True,
            "status": "verified",
            "operation": "request_reminders_access",
            "data": {
                "authorization": authorization,
                "prompted_explicitly": True,
            },
        }

    def ensure_reminder_list(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if set(arguments) != {"source_id", "name", "idempotency_key"}:
            return self._mutation_failure(
                "ensure_reminder_list",
                target={"source_id": arguments.get("source_id"), "list_id": None},
                code="invalid_input",
                reason_code="invalid_ensure_list_fields",
                message="ensure_reminder_list requires source_id, name, and idempotency_key.",
                retryable=False,
            )
        try:
            source_id = _trimmed_string(arguments["source_id"], "source_id", 2048)
            name = _trimmed_string(arguments["name"], "name", 512)
        except FacadeInputError as exc:
            return self._mutation_failure(
                "ensure_reminder_list",
                target={"source_id": arguments.get("source_id"), "list_id": None},
                code="invalid_input",
                reason_code="invalid_list_identity",
                message=str(exc),
                retryable=False,
            )
        key = arguments["idempotency_key"]
        if not isinstance(key, str) or not IDEMPOTENCY_PATTERN.fullmatch(key):
            return self._mutation_failure(
                "ensure_reminder_list",
                target={"source_id": source_id, "list_id": None},
                code="invalid_input",
                reason_code="invalid_idempotency_key",
                message="idempotency_key must contain 8-256 safe characters.",
                retryable=False,
            )
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        request_fingerprint = hashlib.sha256(
            json.dumps(
                {"source_id": source_id, "name": name},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self._idempotency_lock:
            previous = self._idempotency.get(key_hash)
            if previous is not None:
                if previous.fingerprint != request_fingerprint:
                    return self._mutation_failure(
                        "ensure_reminder_list",
                        target={"source_id": source_id, "list_id": None},
                        code="invalid_input",
                        reason_code="idempotency_key_conflict",
                        message="The idempotency key was already used for different list input.",
                        retryable=False,
                    )
                replay = copy.deepcopy(previous.payload)
                replay["replayed"] = True
                return replay

            try:
                reply = self._eventkit.invoke(
                    "ensure_reminder_list",
                    {"source_id": source_id, "name": name},
                    mutation=True,
                )
            except Exception:
                result = self._pending_list_result(
                    source_id,
                    reason_code="eventkit_list_dispatch_failed",
                    message=(
                        "The Reminder List may have been created; list the exact "
                        "source before retrying."
                    ),
                )
            else:
                result = self._project_list_receipt(
                    reply,
                    source_id=source_id,
                    expected_name=name,
                )
            result["replayed"] = False
            result["idempotency_key_hash"] = key_hash
            if result["status"] in SUCCESS_STATUSES:
                while len(self._idempotency) >= self._max_idempotency_results:
                    self._idempotency.pop(next(iter(self._idempotency)))
                self._idempotency[key_hash] = _IdempotentResult(
                    request_fingerprint,
                    copy.deepcopy(result),
                )
            return result

    def _project_list_receipt(
        self,
        reply: EventKitReply,
        *,
        source_id: str,
        expected_name: str,
    ) -> dict[str, Any]:
        payload = _deep_dict(reply.payload)
        status = payload.get("status")
        if (
            status not in MUTATION_STATUSES
            or payload.get("ok") is not (status in SUCCESS_STATUSES)
            or (
                reply.is_error
                and status
                not in {"failed_no_mutation", "failed_manual_repair_required"}
            )
        ):
            return self._pending_list_result(
                source_id,
                reason_code="invalid_eventkit_list_receipt",
                message=(
                    "The Reminder List result could not be validated; list the "
                    "exact source before retrying."
                ),
            )
        before = _public_list(payload.get("before"))
        after = _public_list(payload.get("after"))
        target_raw = payload.get("target") if isinstance(payload.get("target"), Mapping) else {}
        list_id = target_raw.get("list_id") or (
            after.get("id") if isinstance(after, Mapping) else None
        )
        if status in {"unchanged", "verified"}:
            after_source = after.get("source") if isinstance(after, Mapping) else None
            if (
                not isinstance(after, Mapping)
                or after.get("id") != list_id
                or after.get("title") != expected_name
                or not isinstance(after_source, Mapping)
                or after_source.get("id") != source_id
            ):
                return self._pending_list_result(
                    source_id,
                    reason_code="invalid_eventkit_list_receipt",
                    message=(
                        "The exact Reminder List read-back did not match the requested "
                        "source and name; list the source before retrying."
                    ),
                )
        result = self._project_mutation_receipt(
            payload,
            operation="ensure_reminder_list",
            target={
                "source_id": source_id,
                "list_id": list_id if isinstance(list_id, str) else None,
            },
            before=before,
            after=after,
            backend="eventkit_public_sdk",
        )
        if status in {"unchanged", "verified"}:
            result["verification"] = {
                "state": "read_back",
                "write_performed": status == "verified",
                "final_read": True,
                "matched": True,
            }
        elif status == "failed_no_mutation":
            result["verification"] = {
                "state": "not_needed",
                "write_performed": False,
                "final_read": False,
            }
        return result

    def _pending_list_result(
        self,
        source_id: str,
        *,
        reason_code: str,
        message: str,
    ) -> dict[str, Any]:
        result = self._unknown_mutation_result(
            "ensure_reminder_list",
            target={"source_id": source_id, "list_id": None},
            reason_code=reason_code,
            message=message,
        )
        result.pop("next_action", None)
        return result

    def list_reminder_lists(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        limit = int(arguments.get("limit", 100))
        bridge_arguments = {
            field: copy.deepcopy(arguments[field])
            for field in ("source_id", "writable_only")
            if field in arguments
        }
        reply = self._eventkit.invoke(
            "list_calendars",
            bridge_arguments,
            mutation=False,
        )
        payload = _deep_dict(reply.payload)
        if reply.is_error or payload.get("ok") is not True or payload.get("status") != "verified":
            return _read_failure("list_reminder_lists", payload)
        data = payload.get("data")
        raw_items = data.get("items") if isinstance(data, Mapping) else None
        if not isinstance(raw_items, list):
            return _read_failure(
                "list_reminder_lists",
                {
                    "error": {
                        "code": "unexpected_error",
                        "reason_code": "invalid_eventkit_list_response",
                        "message": "EventKit did not return a Reminder List array.",
                        "retryable": False,
                    }
                },
            )
        items = [_public_list(item) for item in raw_items[:limit]]
        return {
            "schema_version": 2,
            "ok": True,
            "status": "verified",
            "operation": "list_reminder_lists",
            "data": {
                "items": items,
                "limit": limit,
                "returned": len(items),
                "truncated": len(raw_items) > limit,
            },
        }

    def fetch_reminders(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        effective = copy.deepcopy(dict(arguments))
        effective.setdefault("status", "incomplete")
        effective.setdefault("limit", 100)
        effective.setdefault("sort", "due")
        try:
            offset = (
                _decode_cursor(effective["cursor"], effective)
                if "cursor" in effective
                else 0
            )
        except FacadeInputError as exc:
            return _read_failure(
                "fetch_reminders",
                {
                    "error": {
                        "code": "invalid_input",
                        "reason_code": "invalid_cursor",
                        "message": str(exc),
                        "retryable": False,
                    }
                },
            )
        bridge_arguments = {
            field: copy.deepcopy(effective[field])
            for field in (
                "status",
                "query",
                "due_start",
                "due_end",
                "completion_start",
                "completion_end",
                "modified_after",
                "limit",
                "sort",
            )
            if field in effective
        }
        if "list_ids" in effective:
            bridge_arguments["calendar_ids"] = copy.deepcopy(effective["list_ids"])
        bridge_arguments["offset"] = offset
        reply = self._eventkit.invoke(
            "fetch_reminders",
            bridge_arguments,
            mutation=False,
        )
        payload = _deep_dict(reply.payload)
        if reply.is_error or payload.get("ok") is not True or payload.get("status") != "verified":
            return _read_failure("fetch_reminders", payload)
        data = payload.get("data")
        raw_items = data.get("items") if isinstance(data, Mapping) else None
        if not isinstance(data, Mapping) or not isinstance(raw_items, list):
            return _read_failure(
                "fetch_reminders",
                {
                    "error": {
                        "code": "unexpected_error",
                        "reason_code": "invalid_eventkit_fetch_response",
                        "message": "EventKit did not return a bounded Reminder page.",
                        "retryable": False,
                    }
                },
            )
        items = [
            _public_reminder_summary(item)
            for item in raw_items
            if isinstance(item, Mapping)
        ]
        has_more = data.get("has_more") is True
        next_offset = data.get("next_offset")
        if (
            has_more
            and isinstance(next_offset, int)
            and not isinstance(next_offset, bool)
            and 0 <= next_offset <= 10_000
        ):
            next_cursor = _encode_cursor(next_offset, effective)
        else:
            next_cursor = None
        return {
            "schema_version": 2,
            "ok": True,
            "status": "verified",
            "operation": "fetch_reminders",
            "data": {
                "items": items,
                "total_matched": int(data.get("total_matched", len(items))),
                "limit": int(effective["limit"]),
                "returned": len(items),
                "has_more": has_more,
                "next_cursor": next_cursor,
                "pagination_exhausted": has_more and next_cursor is None,
            },
        }

    def read_reminder(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        reminder_id = arguments.get("reminder_id")
        if not isinstance(reminder_id, str) or not reminder_id:
            return _read_failure(
                "read_reminder",
                {
                    "error": {
                        "code": "invalid_input",
                        "reason_code": "invalid_reminder_id",
                        "message": "reminder_id must be a non-empty exact identifier.",
                        "retryable": False,
                    }
                },
            )
        try:
            exact = self._references.read_exact(reminder_id)
        except EventKitOperationError as exc:
            return _read_failure("read_reminder", exc.payload)
        except UnsafeRevisionError as exc:
            return _read_failure(
                "read_reminder",
                {
                    "error": {
                        "code": "sync_pending",
                        "reason_code": exc.reason_code,
                        "message": str(exc),
                        "retryable": True,
                    }
                },
            )
        reminder = _public_reminder(exact.reminder)
        reminder["reference"] = exact.reference
        return {
            "schema_version": 2,
            "ok": True,
            "status": "verified",
            "operation": "read_reminder",
            "data": {"reminder": reminder},
        }

    def create_reminder(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        list_id = arguments.get("list_id")
        title = arguments.get("title")
        idempotency_key = arguments.get("idempotency_key")
        if not all(
            isinstance(value, str) and value
            for value in (list_id, title, idempotency_key)
        ):
            return self._mutation_failure(
                "create_reminder",
                target={"list_id": list_id} if isinstance(list_id, str) else {},
                code="invalid_input",
                reason_code="missing_create_fields",
                message="create_reminder requires list_id, title, and idempotency_key.",
                retryable=False,
            )
        bridge_arguments = {
            key: copy.deepcopy(value)
            for key, value in arguments.items()
            if key != "list_id"
        }
        bridge_arguments["calendar_id"] = list_id
        try:
            reply = self._eventkit.invoke(
                "create_reminder",
                bridge_arguments,
                mutation=True,
            )
        except Exception:
            return self._unknown_mutation_result(
                "create_reminder",
                target={"list_id": list_id},
                reason_code="eventkit_dispatch_failed",
                message="The create outcome is unknown; read the list before retrying.",
            )
        payload = _deep_dict(reply.payload)
        status = payload.get("status")
        if status not in {
            "unchanged",
            "verified",
            "committed_verification_pending",
            "partial_success",
            "failed_no_mutation",
            "failed_manual_repair_required",
        }:
            # Dispatch already occurred. A malformed reply cannot be relabeled
            # failed-no-mutation because the native process may have committed.
            return self._unknown_mutation_result(
                "create_reminder",
                target={"list_id": list_id},
                reason_code="invalid_eventkit_mutation_receipt",
                message="The create outcome could not be validated; read the list before retrying.",
            )

        target_raw = payload.get("target")
        target = target_raw if isinstance(target_raw, Mapping) else {}
        after_raw = payload.get("after")
        reminder_id = target.get("reminder_id", target.get("id"))
        if not isinstance(reminder_id, str) and isinstance(after_raw, Mapping):
            reminder_id = after_raw.get("id")
        public_target: dict[str, Any] = {"list_id": list_id}
        if isinstance(reminder_id, str) and reminder_id:
            public_target["reminder_id"] = reminder_id
        receipt = self._project_mutation_receipt(
            payload,
            operation="create_reminder",
            target=public_target,
            before=None,
            after=None,
            backend=(
                "eventkit_plus_native_url"
                if isinstance(arguments.get("url"), str)
                else "eventkit_public_sdk"
            ),
        )
        receipt["replayed"] = payload.get("replayed") is True
        key_hash = payload.get("idempotency_key_hash")
        if not isinstance(key_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", key_hash):
            key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        receipt["idempotency_key_hash"] = key_hash

        if status in {"verified", "unchanged"}:
            if not isinstance(reminder_id, str) or not reminder_id:
                return self._mark_final_read_pending(
                    receipt,
                    reason_code="created_reminder_id_missing",
                    message="The create committed, but its exact Reminder identifier was unavailable.",
                )
            try:
                exact = self._references.read_exact(reminder_id)
            except (EventKitOperationError, UnsafeRevisionError, AdapterContractError):
                return self._mark_final_read_pending(
                    receipt,
                    reason_code="create_final_read_failed",
                    message="The create committed, but a fresh exact reference could not be issued.",
                )
            receipt["after"] = _change_reminder(
                exact.reminder,
                reference=exact.reference,
            )
            receipt["verification"] = {
                **_deep_dict(receipt.get("verification")),
                "state": "read_back",
                "write_performed": status != "unchanged",
                "final_read": True,
                "matched": True,
            }
        return receipt

    def delete_reminder(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        reference = arguments.get("reference")
        if not isinstance(reference, str) or not reference:
            return self._mutation_failure(
                "delete_reminder",
                target={},
                code="invalid_input",
                reason_code="invalid_reference",
                message="delete_reminder requires one opaque reference.",
                retryable=False,
            )
        try:
            guard = self._references.revalidate_reference(reference)
        except ReferenceRejected as exc:
            return self._mutation_failure(
                "delete_reminder",
                target={},
                code="concurrent_modification",
                reason_code=exc.code,
                message=str(exc),
                retryable=True,
            )
        except (EventKitOperationError, UnsafeRevisionError, AdapterContractError) as exc:
            return self._mutation_failure(
                "delete_reminder",
                target={},
                code="sync_pending",
                reason_code="reference_revalidation_failed",
                message=str(exc),
                retryable=True,
            )

        target = {"reminder_id": guard.reminder_id}
        try:
            reply = self._eventkit.invoke(
                "delete_reminder",
                {
                    "reminder_id": guard.reminder_id,
                    "expected_last_modified": guard.public_concurrency_value,
                },
                mutation=True,
            )
        except Exception:
            self._references.invalidate_reference(reference)
            return self._unknown_mutation_result(
                "delete_reminder",
                target=target,
                reason_code="eventkit_dispatch_failed",
                message="The delete outcome is unknown; read before retrying.",
            )
        payload = _deep_dict(reply.payload)
        status = payload.get("status")
        if status not in {
            "unchanged",
            "verified",
            "committed_verification_pending",
            "partial_success",
            "failed_no_mutation",
            "failed_manual_repair_required",
        }:
            self._references.invalidate_reference(reference)
            return self._unknown_mutation_result(
                "delete_reminder",
                target=target,
                reason_code="invalid_eventkit_mutation_receipt",
                message="The delete outcome could not be validated; read before retrying.",
            )

        public_error = _public_error(payload.get("error"))
        if status != "failed_no_mutation" or public_error["code"] == "concurrent_modification":
            self._references.invalidate_reference(reference)
        before = (
            _change_reminder(payload.get("before"))
            if isinstance(payload.get("before"), Mapping)
            else None
        )
        verification = _verification(payload.get("verification"), status=status)
        after = (
            {"reminder_id": guard.reminder_id, "deleted": True}
            if verification.get("local_absence") is True
            else None
        )
        receipt = self._project_mutation_receipt(
            payload,
            operation="delete_reminder",
            target=target,
            before=before,
            after=after,
        )
        if status in {"verified", "unchanged"} and verification.get("local_absence") is not True:
            return self._mark_final_read_pending(
                receipt,
                reason_code="delete_absence_unverified",
                message="The delete may have committed, but exact local absence was not verified.",
            )
        return receipt

    def change_reminder(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        reference = arguments.get("reference")
        raw_action = arguments.get("action")
        action_kind = raw_action.get("kind") if isinstance(raw_action, Mapping) else None
        public_operation = PUBLIC_CHANGE_OPERATIONS.get(
            str(action_kind),
            "change_reminder.patch",
        )
        try:
            if not isinstance(reference, str) or not isinstance(raw_action, Mapping):
                raise ActionRejected("change_reminder requires reference and action")
            result = self._references.change(reference, raw_action)
        except ReferenceRejected as exc:
            return self._change_failure(
                public_operation,
                None,
                code="concurrent_modification",
                reason_code=exc.code,
                message=str(exc),
                retryable=True,
            )
        except ActionRejected as exc:
            return self._change_failure(
                public_operation,
                None,
                code="invalid_input",
                reason_code=exc.code,
                message=str(exc),
                retryable=False,
            )
        except MutationOutcomeUnknown as exc:
            return self._unknown_mutation_result(
                public_operation,
                target={},
                reason_code="eventkit_dispatch_failed",
                message=str(exc),
            )
        except AdapterContractError as exc:
            return self._unknown_mutation_result(
                public_operation,
                target={},
                reason_code="adapter_contract_error",
                message=str(exc),
            )

        receipt = copy.deepcopy(result.receipt)
        receipt["schema_version"] = 2
        receipt["operation"] = public_operation
        receipt["target"] = _deep_dict(receipt.get("target"))

        if result.reference is not None and result.final_reminder is not None:
            receipt["before"] = _change_reminder(
                receipt.get("before"),
            )
            receipt["after"] = _change_reminder(
                result.final_reminder,
                reference=result.reference,
            )
            verification = _deep_dict(receipt.get("verification"))
            verification.update(
                {
                    "state": "read_back",
                    "final_read": True,
                    "matched": True,
                }
            )
            receipt["verification"] = verification
        else:
            # A pending/partial/failing result must not expose an old or
            # fabricated writable reference in a historical projection.
            receipt["before"] = None
            receipt["after"] = None

        if result.reference_error == "final_read_failed":
            receipt["status"] = "committed_verification_pending"
            receipt["ok"] = True
            receipt["verification"] = {
                "state": "pending",
                "write_performed": True,
                "final_read": False,
                "matched": None,
            }
            warnings = list(receipt.get("warnings", []))
            warnings.append(
                {
                    "code": "final_read_failed",
                    "message": (
                        "The write committed, but a fresh exact reference could not "
                        "be issued."
                    ),
                }
            )
            receipt["warnings"] = warnings[:20]
            receipt["error"] = {
                "code": "sync_pending",
                "reason_code": "final_read_failed",
                "message": "Read the Reminder again before another change.",
                "retryable": True,
            }
        if result.reference_error is not None or receipt["status"] in {
            "committed_verification_pending",
            "partial_success",
        }:
            next_action = _next_action(
                receipt.get("error", {"code": "sync_pending"}),
                operation=str(receipt.get("operation") or ""),
            )
            if next_action is not None:
                receipt["next_action"] = next_action
        return receipt

    def _project_mutation_receipt(
        self,
        payload: Mapping[str, Any],
        *,
        operation: str,
        target: Mapping[str, Any],
        before: Any,
        after: Any,
        backend: str | None = None,
    ) -> dict[str, Any]:
        status = str(payload.get("status"))
        result: dict[str, Any] = {
            "schema_version": 2,
            "ok": status not in {
                "failed_no_mutation",
                "failed_manual_repair_required",
            },
            "status": status,
            "operation": operation,
            "operation_id": str(payload.get("operation_id") or self._operation_id_source()),
            "backend": backend or str(payload.get("backend") or "eventkit_public_sdk"),
            "target": copy.deepcopy(dict(target)),
            "before": copy.deepcopy(before),
            "after": copy.deepcopy(after),
            "verification": _verification(payload.get("verification"), status=status),
            "recovery": _recovery(payload.get("recovery"), status=status),
        }
        warnings = _warnings(payload.get("warnings"))
        if warnings:
            result["warnings"] = warnings
        if status in {"failed_no_mutation", "failed_manual_repair_required"} or isinstance(
            payload.get("error"), Mapping
        ):
            error = _public_error(payload.get("error"))
            result["error"] = error
            next_action = _next_action(error, operation=operation)
            if next_action is not None:
                result["next_action"] = next_action
        return result

    def _mark_final_read_pending(
        self,
        receipt: Mapping[str, Any],
        *,
        reason_code: str,
        message: str,
    ) -> dict[str, Any]:
        result = copy.deepcopy(dict(receipt))
        result["ok"] = True
        result["status"] = "committed_verification_pending"
        result["after"] = None
        existing_verification = _deep_dict(result.get("verification"))
        result["verification"] = {
            "state": "pending",
            "write_performed": existing_verification.get("write_performed", True),
            "final_read": False,
            "matched": None,
        }
        warnings = _warnings(result.get("warnings"))
        warnings.append(
            {
                "code": _reason_code(reason_code, "final_read_failed"),
                "message": message[:2000],
            }
        )
        result["warnings"] = warnings[:20]
        error = {
            "code": "sync_pending",
            "reason_code": _reason_code(reason_code, "final_read_failed"),
            "message": message[:2000],
            "retryable": True,
        }
        result["error"] = error
        result["next_action"] = _next_action(
            error,
            operation=str(result.get("operation") or ""),
        )
        return result

    def _unknown_mutation_result(
        self,
        operation: str,
        *,
        target: Mapping[str, Any],
        reason_code: str,
        message: str,
    ) -> dict[str, Any]:
        error = {
            "code": "sync_pending",
            "reason_code": _reason_code(reason_code, "mutation_outcome_unknown"),
            "message": message[:2000],
            "retryable": True,
        }
        next_action = (
            {
                "kind": "fresh_read",
                "tool": "fetch_reminders",
                "retry_original_once": False,
                "message": "Fetch the exact Reminder List and resolve whether the create committed before retrying.",
            }
            if operation == "create_reminder"
            else _next_action(error)
        )
        return {
            "schema_version": 2,
            "ok": True,
            "status": "committed_verification_pending",
            "operation": operation,
            "operation_id": self._operation_id_source(),
            "backend": "eventkit_public_sdk",
            "target": copy.deepcopy(dict(target)),
            "before": None,
            "after": None,
            "verification": {
                "state": "pending",
                "write_performed": None,
                "final_read": False,
                "matched": None,
            },
            "recovery": {
                "semantics": "read_before_retry",
                "automatic_retry_safe": False,
            },
            "warnings": [
                {
                    "code": "verification_pending",
                    "message": "The native process may have committed; read before retrying.",
                }
            ],
            "error": error,
            "next_action": next_action,
        }

    def _mutation_failure(
        self,
        operation: str,
        *,
        target: Mapping[str, Any],
        code: str,
        reason_code: str,
        message: str,
        retryable: bool,
        backend: str = "eventkit_public_sdk",
    ) -> dict[str, Any]:
        error = {
            "code": code,
            "reason_code": _reason_code(reason_code, code),
            "message": (message or "The Reminder mutation was rejected.")[:2000],
            "retryable": retryable,
        }
        result: dict[str, Any] = {
            "schema_version": 2,
            "ok": False,
            "status": "failed_no_mutation",
            "operation": operation,
            "operation_id": self._operation_id_source(),
            "backend": backend,
            "target": copy.deepcopy(dict(target)),
            "before": None,
            "after": None,
            "verification": {
                "state": "not_needed",
                "write_performed": False,
                "final_read": False,
            },
            "recovery": {
                "semantics": "fresh_read_required",
                "automatic_retry_safe": False,
            },
            "error": error,
        }
        next_action = _next_action(error, operation=operation)
        if next_action is not None:
            result["next_action"] = next_action
        return result

    def _change_failure(
        self,
        operation: str,
        reminder_id: str | None,
        *,
        code: str,
        reason_code: str,
        message: str,
        retryable: bool,
    ) -> dict[str, Any]:
        return self._mutation_failure(
            operation,
            target={"reminder_id": reminder_id} if reminder_id else {},
            code=code,
            reason_code=reason_code,
            message=message,
            retryable=retryable,
        )


__all__ = [
    "CoreReferencePort",
    "EventKitCoreAdapter",
    "EventKitPort",
    "EventKitReply",
    "FacadeInputError",
    "V2CoreFacade",
]
