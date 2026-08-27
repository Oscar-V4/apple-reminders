#!/usr/bin/env python3
"""Deep facade for the public v2 native Reminders surface.

The module deliberately does not know how references are stored or how native
database versions are acquired.  Reference ownership stays behind
``ReferencePort``; native reads and mutations receive the resolved EventKit
``Guard`` and must revalidate it before touching private state.  This prevents
the transport layer from turning an opaque ``rev1`` token into an unguarded
ReminderKit/SQLite write.

Maintenance preview/apply is intentionally absent from this facade. A safe
public version needs a separate bounded plan-token vault and exact-list repair
backend; neither belongs in the 0.3 native core by accident.
"""

from __future__ import annotations

import copy
import hashlib
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from reminders_service import (  # noqa: E402
    ExactRead,
    Guard,
    MutationOutcome,
    ReferenceRejected,
)
if __package__:  # Package import in tests; script-local import in the stdio server.
    from .v2_contract import (
        FAILURE_STATUSES,
        MUTATION_STATUSES,
        SUCCESS_STATUSES,
    )
else:  # pragma: no cover - exercised by the script entry point
    from v2_contract import FAILURE_STATUSES, MUTATION_STATUSES, SUCCESS_STATUSES


REFERENCE_PATTERN = re.compile(r"^rev1\.[A-Za-z0-9_-]{32,4091}$")
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,256}$")


class ReferencePort(Protocol):
    """Internal ownership seam for opaque public revision references."""

    def revalidate_reference(self, reference: str) -> Guard:
        """Exact-read and return the still-current Guard for ``reference``."""

    def invalidate_reference(self, reference: str) -> None:
        """Permanently revoke a reference after a possible mutation."""

    def read_exact(self, reminder_id: str) -> ExactRead:
        """Perform the canonical final read and issue a replacement reference."""


NativeRead = Callable[[Guard, dict[str, Any]], dict[str, Any]]
NativeMutation = Callable[[Guard, str, dict[str, Any]], MutationOutcome]
BackendCall = Callable[[str, dict[str, Any]], dict[str, Any]]

# ``NativeMutation`` is a guarded port, not a raw adapter call. Its production
# implementation must re-check the supplied EventKit Guard, acquire the exact
# private reminder version immediately before dispatch, pass that version to
# the existing adapter, and classify transport loss as mutation_state=unknown.


class FacadeError(ValueError):
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


def _trimmed(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise FacadeError("invalid_input", "invalid_type", f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise FacadeError("invalid_input", "empty_string", f"{name} must not be empty")
    if len(normalized) > maximum:
        raise FacadeError(
            "invalid_input",
            "string_too_long",
            f"{name} exceeds its {maximum}-character limit",
        )
    return normalized


def _limit(value: Any, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise FacadeError(
            "invalid_input",
            "invalid_limit",
            f"limit must be an integer between 1 and {maximum}",
        )
    return value


def _arguments(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FacadeError(
            "invalid_input", "arguments_not_object", "Tool arguments must be an object"
        )
    return dict(value)


def _closed(arguments: Mapping[str, Any], allowed: set[str], required: set[str]) -> None:
    unknown = sorted(set(arguments) - allowed)
    missing = sorted(required - set(arguments))
    if unknown:
        raise FacadeError(
            "invalid_input",
            "unknown_fields",
            f"Unsupported fields: {', '.join(unknown)}",
        )
    if missing:
        raise FacadeError(
            "invalid_input",
            "missing_fields",
            f"Missing required fields: {', '.join(missing)}",
        )


def _error(
    code: str,
    reason_code: str,
    message: str,
    *,
    retryable: bool = False,
) -> dict[str, Any]:
    stable = code if code in {
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
    } else "unexpected_error"
    return {
        "code": stable,
        "reason_code": re.sub(r"[^a-z0-9_]+", "_", reason_code.casefold()).strip("_")
        or "unexpected_error",
        "message": str(message)[:2000] or "The operation failed.",
        "retryable": bool(retryable),
    }


def _normalized_error(value: Any) -> dict[str, Any]:
    raw = dict(value) if isinstance(value, Mapping) else {}
    return _error(
        str(raw.get("code") or "unexpected_error"),
        str(raw.get("reason_code") or raw.get("code") or "backend_error"),
        str(raw.get("message") or "The native backend reported an error."),
        retryable=raw.get("retryable") is True,
    )


def _warning(value: Any) -> dict[str, str] | None:
    if isinstance(value, str) and value:
        return {"code": "native_warning", "message": value[:2000]}
    if not isinstance(value, Mapping):
        return None
    code = re.sub(
        r"[^a-z0-9_]+", "_", str(value.get("code") or "native_warning").casefold()
    ).strip("_")
    message = str(value.get("message") or "Native operation warning.")
    return {"code": code[:128] or "native_warning", "message": message[:2000]}


def _operation_id(value: Any) -> str:
    if isinstance(value, str):
        try:
            return str(uuid.UUID(value))
        except ValueError:
            pass
    return str(uuid.uuid4())


def _verification(value: Any) -> dict[str, Any]:
    raw = dict(value) if isinstance(value, Mapping) else {}
    state = str(raw.get("state") or "pending")
    if state not in {
        "not_needed",
        "read_back",
        "pending",
        "partial",
        "revalidated_under_lock",
    }:
        if state in {"cloud_read_back", "candidate_snapshot"}:
            state = "read_back"
        elif state in {"not_performed", "native_return"}:
            state = "not_needed"
        else:
            state = "pending"
    result: dict[str, Any] = {
        "state": state,
        # ``None`` intentionally represents an unknown possible-commit outcome.
        # The public schema must allow null here rather than claim no write.
        "write_performed": raw.get("write_performed"),
        "final_read": raw.get("final_read") is True,
    }
    if not isinstance(result["write_performed"], bool):
        result["write_performed"] = None
    for name in (
        "matched",
        "mobile_visible_likely",
        "visible_url_attachment",
        "cloud_version",
    ):
        if name in raw:
            result[name] = raw[name]
    return result


def _recovery(value: Any) -> dict[str, Any]:
    raw = dict(value) if isinstance(value, Mapping) else {}
    semantics = str(raw.get("semantics") or "read_before_retry")[:128]
    result: dict[str, Any] = {
        "semantics": semantics or "read_before_retry",
        "automatic_retry_safe": raw.get("automatic_retry_safe") is True,
    }
    if "manual_action" in raw:
        result["manual_action"] = (
            None if raw["manual_action"] is None else str(raw["manual_action"])[:2000]
        )
    return result


def _section(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    raw = dict(value)
    identifier = raw.get("id") or raw.get("ZCKIDENTIFIER")
    list_id = raw.get("list_id") or raw.get("LIST_ID")
    if not isinstance(identifier, str) or not identifier or not isinstance(list_id, str) or not list_id:
        return None
    order = raw.get("order", raw.get("Z_FOK_LIST"))
    if not isinstance(order, int) or isinstance(order, bool) or order < 0:
        order = None
    return {
        "id": identifier[:2048],
        "name": str(raw.get("name") or raw.get("ZDISPLAYNAME") or "")[:512],
        "list_id": list_id[:2048],
        "list_title": str(
            raw.get("list_title")
            or raw.get("list")
            or raw.get("list_name")
            or raw.get("LIST_NAME")
            or ""
        )[:512],
        "order": order,
    }


def _tag(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    raw = dict(value)
    if isinstance(raw.get("label"), Mapping):
        raw = dict(raw["label"])
    name = raw.get("name") or raw.get("ZNAME")
    if not isinstance(name, str):
        return None
    identifier = raw.get("id", raw.get("uuid"))
    account = raw.get("account_id", raw.get("account_identifier"))
    return {
        "id": identifier[:2048] if isinstance(identifier, str) else None,
        "name": name[:512],
        "canonical_name": str(
            raw.get("canonical_name") or raw.get("ZCANONICALNAME") or name.casefold()
        )[:512],
        "account_id": account[:512] if isinstance(account, str) else None,
        "first_seen_at": raw.get("first_seen_at") if isinstance(raw.get("first_seen_at"), str) else None,
        "recency_at": raw.get("recency_at") if isinstance(raw.get("recency_at"), str) else None,
        "active_count": max(0, min(int(raw.get("active_count") or 0), 1_000_000)),
    }


def _section_from_container(value: Mapping[str, Any]) -> dict[str, Any] | None:
    direct = _section(value.get("section"))
    if direct is not None:
        return direct
    section_id = value.get("section_id")
    section_name = value.get("section")
    list_id = value.get("list_id")
    if not all(isinstance(item, str) and item for item in (section_id, section_name, list_id)):
        return None
    return _section(
        {
            "id": section_id,
            "name": section_name,
            "list_id": list_id,
            "list_title": value.get("list_title") or value.get("list") or "",
            "order": value.get("section_order"),
        }
    )


def _sync(value: Any) -> dict[str, Any]:
    raw = dict(value) if isinstance(value, Mapping) else {}
    return {
        "fields_available": raw.get("fields_available", raw.get("sync_fields_available"))
        is True,
        "mobile_visible_likely": raw.get("mobile_visible_likely")
        if isinstance(raw.get("mobile_visible_likely"), bool)
        else None,
        "has_server_record": raw.get("has_server_record")
        if isinstance(raw.get("has_server_record"), bool)
        else None,
        "in_cloud": raw.get("in_cloud") if raw.get("in_cloud") in {0, 1} else None,
        "current_local_version": raw.get("current_local_version")
        if isinstance(raw.get("current_local_version"), int)
        else None,
        "latest_synced_version": raw.get("latest_synced_version")
        if isinstance(raw.get("latest_synced_version"), int)
        else None,
    }


def _attachment(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    raw = dict(value)
    identifier = raw.get("id")
    kind = raw.get("type")
    if not isinstance(identifier, str) or not identifier or kind not in {"image", "url"}:
        return None
    result: dict[str, Any] = {"id": identifier[:2048], "type": kind, "sync": _sync(raw.get("sync"))}
    if kind == "url":
        url = raw.get("url")
        if not isinstance(url, str) or not url:
            return None
        result["url"] = url[:100_000]
    else:
        result.update(
            {
                "filename": raw.get("filename")[:1024]
                if isinstance(raw.get("filename"), str)
                else None,
                "file_size": raw.get("file_size")
                if isinstance(raw.get("file_size"), int)
                else None,
                "width": raw.get("width") if isinstance(raw.get("width"), int) else None,
                "height": raw.get("height") if isinstance(raw.get("height"), int) else None,
            }
        )
    return result


class NativeFacade:
    """Closed public facade for native-extension tools and their safety seams."""

    def __init__(
        self,
        *,
        adapter_call: BackendCall,
        references: ReferencePort,
        native_read: NativeRead,
        native_mutation: NativeMutation,
    ) -> None:
        self._adapter_call = adapter_call
        self._references = references
        self._native_read = native_read
        self._native_mutation = native_mutation

    def call(self, name: str, raw_arguments: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {}
        try:
            arguments = _arguments(raw_arguments)
            if name == "inspect_reminder_native":
                return self._inspect(arguments)
            if name == "create_reminder_section":
                return self._create_section(arguments)
            if name == "organize_reminder":
                return self._native_change(name, arguments)
            if name == "change_reminder_attachment":
                return self._native_change(name, arguments)
            raise FacadeError(
                "invalid_input", "unknown_tool", f"Unknown v2 native tool: {name}"
            )
        except ReferenceRejected as exc:
            code = "concurrent_modification" if exc.code in {
                "invalid_reference",
                "expired_reference",
                "concurrent_modification",
            } else exc.code
            return self._failure(
                name,
                arguments,
                FacadeError(code, exc.code, str(exc)),
            )
        except FacadeError as exc:
            return self._failure(name, arguments, exc)
        except Exception as exc:  # Keep backend details out of the public envelope.
            return self._failure(
                name,
                arguments,
                FacadeError(
                    "unexpected_error",
                    "native_facade_failure",
                    f"The native facade could not complete ({type(exc).__name__}).",
                ),
            )

    def _failure(
        self,
        name: str,
        arguments: Mapping[str, Any],
        exc: FacadeError,
    ) -> dict[str, Any]:
        error = _error(exc.code, exc.reason_code, str(exc), retryable=exc.retryable)
        next_action = None
        if error["code"] == "concurrent_modification":
            next_action = {
                "kind": "fresh_read",
                "tool": "read_reminder",
                "retry_original_once": False,
                "message": "Read the exact reminder again before retrying this operation.",
            }
        elif error["code"] == "permission_denied":
            next_action = {
                "kind": "request_access",
                "tool": "request_reminders_access",
                "retry_original_once": True,
                "message": "Request Reminders access, then retry this operation once.",
            }
        if name in {
            "inspect_reminder_native",
        }:
            result: dict[str, Any] = {
                "schema_version": 2,
                "ok": False,
                "status": "failed_no_mutation",
                "operation": name,
                "error": error,
            }
            if next_action:
                result["next_action"] = next_action
            return result

        operation = self._public_operation(name, arguments)
        target = self._target_for_failure(name, arguments)
        result = {
            "schema_version": 2,
            "ok": False,
            "status": "failed_no_mutation",
            "operation": operation,
            "operation_id": str(uuid.uuid4()),
            "backend": "native_extension",
            "target": target,
            "before": None,
            "after": None,
            "verification": {
                "state": "not_needed",
                "write_performed": False,
                "final_read": False,
            },
            "recovery": {
                "semantics": "not_applicable",
                "automatic_retry_safe": True,
            },
            "error": error,
        }
        if next_action:
            result["next_action"] = next_action
        return result

    @staticmethod
    def _public_operation(name: str, arguments: Mapping[str, Any]) -> str:
        action = arguments.get("action")
        if isinstance(action, Mapping) and isinstance(action.get("kind"), str):
            return f"{name}.{action['kind']}"
        return name

    @staticmethod
    def _target_for_failure(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name == "create_reminder_section":
            return {"list_id": arguments.get("list_id"), "section_id": None}
        if name == "organize_reminder":
            action = arguments.get("action") if isinstance(arguments.get("action"), Mapping) else {}
            return {
                "reminder_id": "unknown",
                "section_id": action.get("section_id"),
                "tag": action.get("tag"),
            }
        if name == "change_reminder_attachment":
            action = arguments.get("action") if isinstance(arguments.get("action"), Mapping) else {}
            return {"reminder_id": "unknown", "attachment_id": action.get("attachment_id")}
        return {}

    def _create_section(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _closed(arguments, {"list_id", "name"}, {"list_id", "name"})
        list_id = _trimmed(arguments["list_id"], "list_id", 2048)
        name = _trimmed(arguments["name"], "name", 512)
        try:
            payload = self._adapter_call(
                "create_section", {"list_id": list_id, "name": name}
            )
        except Exception:
            return self._pending_mutation_result(
                "create_reminder_section",
                backend="native_extension",
                target={"list_id": list_id, "section_id": None},
                reason_code="native_section_dispatch_failed",
                message=(
                    "The section may have been created; inspect this exact list before retrying."
                ),
                next_tool=None,
            )
        try:
            return self._section_receipt(payload, list_id=list_id)
        except Exception:
            return self._pending_mutation_result(
                "create_reminder_section",
                backend="native_extension",
                target={"list_id": list_id, "section_id": None},
                reason_code="invalid_native_section_receipt",
                message=(
                    "The section result could not be validated; inspect this exact list "
                    "before retrying."
                ),
                next_tool=None,
            )

    def _section_receipt(self, payload: Any, *, list_id: str) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise FacadeError(
                "schema_mismatch", "invalid_native_receipt", "The section backend returned invalid data."
            )
        raw = dict(payload)
        status = raw.get("status")
        if status not in MUTATION_STATUSES:
            raise FacadeError(
                "schema_mismatch", "invalid_native_status", "The section backend returned an invalid status."
            )
        if raw.get("ok") is not (status in SUCCESS_STATUSES):
            raise FacadeError(
                "schema_mismatch",
                "native_section_status_ok_mismatch",
                "Native section status and ok flag disagree.",
            )
        raw_after = raw.get("after") if isinstance(raw.get("after"), Mapping) else {}
        raw_before = raw.get("before") if isinstance(raw.get("before"), Mapping) else {}
        after = _section(raw_after.get("section", raw_after))
        before = _section(raw_before.get("section", raw_before))
        if status in {"unchanged", "verified"} and after is None:
            raise FacadeError(
                "schema_mismatch",
                "native_section_after_missing",
                "A terminal section receipt requires an exact read-back.",
            )
        section_id = after.get("id") if after else None
        verification = _verification(raw.get("verification"))
        recovery = _recovery(raw.get("recovery"))
        if status in {"unchanged", "verified"}:
            verification = {
                "state": "read_back",
                "write_performed": status == "verified",
                "final_read": True,
                "matched": True,
            }
        elif status == "failed_no_mutation":
            verification = {
                "state": "not_needed",
                "write_performed": False,
                "final_read": False,
            }
        result: dict[str, Any] = {
            "schema_version": 2,
            "ok": status in SUCCESS_STATUSES,
            "status": status,
            "operation": "create_reminder_section",
            "operation_id": _operation_id(raw.get("operation_id")),
            "backend": "native_extension",
            "target": {"list_id": list_id, "section_id": section_id},
            "before": before,
            "after": after,
            "verification": verification,
            "recovery": recovery,
        }
        self._copy_receipt_extras(raw, result)
        self._finalize_pending_receipt(
            result,
            next_tool=None,
            next_message=(
                "Inspect this exact list before attempting to create the section again."
            ),
        )
        return result

    def _inspect(self, arguments: dict[str, Any]) -> dict[str, Any]:
        kind = arguments.get("kind")
        if kind == "reminder":
            _closed(
                arguments,
                {"kind", "reference", "include", "attachment_type", "limit"},
                {"kind", "reference", "include"},
            )
            reference = self._reference(arguments["reference"])
            include = arguments["include"]
            if (
                not isinstance(include, list)
                or not 1 <= len(include) <= 4
                or len(set(include)) != len(include)
                or any(item not in {"section", "tags", "attachments", "sync"} for item in include)
            ):
                raise FacadeError(
                    "invalid_input", "invalid_include", "include must contain 1-4 unique native fields."
                )
            attachment_type = arguments.get("attachment_type")
            if attachment_type not in {None, "image", "url"}:
                raise FacadeError(
                    "invalid_input",
                    "invalid_attachment_type",
                    "attachment_type must be image or url.",
                )
            limit = _limit(arguments.get("limit"), default=100, maximum=200)
            guard = self._references.revalidate_reference(reference)
            raw = self._native_read(
                guard,
                {
                    "include": list(include),
                    "attachment_type": attachment_type,
                    "limit": limit,
                },
            )
            if (
                not isinstance(raw, Mapping)
                or (raw.get("reminder_id") or raw.get("id")) != guard.reminder_id
            ):
                raise FacadeError(
                    "schema_mismatch",
                    "native_identity_mismatch",
                    "Native inspection returned data for a different reminder.",
                )
            try:
                exact = self._references.read_exact(guard.reminder_id)
            except Exception as exc:
                raise FacadeError(
                    "sync_pending",
                    "inspection_reference_refresh_failed",
                    (
                        "Native data was read, but a fresh exact Reminder reference "
                        f"could not be issued ({type(exc).__name__})."
                    ),
                    retryable=True,
                ) from exc
            if (
                not isinstance(exact, ExactRead)
                or exact.reminder.get("id") != guard.reminder_id
                or not REFERENCE_PATTERN.fullmatch(exact.reference)
            ):
                raise FacadeError(
                    "schema_mismatch",
                    "invalid_inspection_reference_refresh",
                    "The exact read returned an invalid replacement reference.",
                )
            self._references.invalidate_reference(reference)
            sections = _section_from_container(raw)
            raw_tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
            tags = [item for value in raw_tags if (item := _tag(value)) is not None]
            raw_attachments = raw.get("attachment_items", raw.get("attachments", []))
            if not isinstance(raw_attachments, list):
                raw_attachments = []
            attachments = [
                item
                for value in raw_attachments
                if (item := _attachment(value)) is not None
                and (attachment_type is None or item["type"] == attachment_type)
            ]
            original_count = max(len(tags), len(attachments))
            tags = tags[:limit]
            attachments = attachments[:limit]
            data = {
                "kind": "reminder",
                "reminder_id": guard.reminder_id,
                "reference": exact.reference,
                "included": list(include),
                "section": sections if "section" in include else None,
                "tags": tags if "tags" in include else [],
                "attachments": attachments if "attachments" in include else [],
                "sync": _sync(raw.get("sync")) if "sync" in include and raw.get("sync") is not None else None,
                "returned": min(original_count, limit),
                "truncated": original_count > limit or raw.get("truncated") is True,
            }
            return self._read_success("inspect_reminder_native", data)

        if kind == "sections":
            _closed(arguments, {"kind", "list_id", "limit"}, {"kind", "list_id"})
            list_id = _trimmed(arguments["list_id"], "list_id", 2048)
            limit = _limit(arguments.get("limit"), default=100, maximum=200)
            raw = self._adapter_call(
                "list_sections", {"list_id": list_id, "limit": limit}
            )
            if not isinstance(raw, Mapping) or raw.get("ok") is not True:
                self._raise_backend_read(raw, "list_sections_failed")
            sections = [
                item for value in raw.get("sections", []) if (item := _section(value)) is not None
            ][:limit]
            return self._read_success(
                "inspect_reminder_native",
                {
                    "kind": "sections",
                    "list_id": list_id,
                    "sections": sections,
                    "returned": len(sections),
                    "truncated": raw.get("truncated") is True,
                },
            )

        if kind == "tags":
            _closed(arguments, {"kind", "account_id", "query", "limit"}, {"kind"})
            account_id = (
                _trimmed(arguments["account_id"], "account_id", 512)
                if "account_id" in arguments
                else None
            )
            query = (
                _trimmed(arguments["query"], "query", 512) if "query" in arguments else None
            )
            limit = _limit(arguments.get("limit"), default=100, maximum=200)
            raw = self._adapter_call(
                "list_tags",
                {
                    "account_id": account_id,
                    "query": query,
                    "limit": limit,
                },
            )
            if not isinstance(raw, Mapping) or raw.get("ok") is not True:
                self._raise_backend_read(raw, "list_tags_failed")
            tags = [item for value in raw.get("tags", []) if (item := _tag(value)) is not None]
            if account_id is not None:
                tags = [item for item in tags if item["account_id"] == account_id]
            tags = tags[:limit]
            return self._read_success(
                "inspect_reminder_native",
                {
                    "kind": "tags",
                    "tags": tags,
                    "returned": len(tags),
                    "truncated": raw.get("truncated") is True,
                },
            )
        raise FacadeError(
            "invalid_input",
            "invalid_inspection_kind",
            "kind must be reminder, sections, or tags.",
        )

    def _native_change(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        _closed(arguments, {"reference", "action"}, {"reference", "action"})
        reference = self._reference(arguments["reference"])
        action = arguments["action"]
        if not isinstance(action, Mapping):
            raise FacadeError("invalid_input", "invalid_action", "action must be an object")
        command, native_arguments = self._native_action(tool_name, dict(action))
        guard = self._references.revalidate_reference(reference)
        public_operation = f"{tool_name}.{action['kind']}"
        target = self._native_target(tool_name, guard.reminder_id, native_arguments)
        try:
            outcome = self._native_mutation(guard, command, native_arguments)
        except Exception:
            # The guarded backend call is the dispatch boundary. An exception
            # after crossing it cannot prove that no native write occurred.
            self._references.invalidate_reference(reference)
            return self._unknown_native_result(
                public_operation,
                target,
                "native_mutation_dispatch_failed",
            )
        if not isinstance(outcome, MutationOutcome):
            self._references.invalidate_reference(reference)
            return self._unknown_native_result(
                public_operation,
                target,
                "invalid_native_mutation_outcome",
            )
        if outcome.mutation_state in {"committed", "unknown"}:
            self._references.invalidate_reference(reference)
        try:
            receipt = self._validated_outcome(outcome)
        except FacadeError as exc:
            if outcome.mutation_state not in {"committed", "unknown"}:
                self._references.invalidate_reference(reference)
            return self._unknown_native_result(
                public_operation,
                target,
                exc.reason_code,
            )
        except Exception:
            if outcome.mutation_state not in {"committed", "unknown"}:
                self._references.invalidate_reference(reference)
            return self._unknown_native_result(
                public_operation,
                target,
                "invalid_native_mutation_receipt",
            )

        before = self._native_state(
            tool_name,
            receipt.get("before"),
            guard.reminder_id,
            None,
        )
        after = None
        verification = _verification(receipt.get("verification"))
        recovery = _recovery(receipt.get("recovery"))
        if (
            outcome.mutation_state == "unknown"
            and receipt["status"] != "failed_manual_repair_required"
        ):
            verification = {
                "state": "pending",
                "write_performed": None,
                "final_read": False,
                "matched": None,
            }
            recovery = {
                "semantics": "read_reminder_before_retry",
                "automatic_retry_safe": False,
            }
        elif receipt["status"] == "failed_no_mutation":
            verification = {
                "state": "not_needed",
                "write_performed": False,
                "final_read": False,
            }
        if receipt["status"] in {"unchanged", "verified"}:
            try:
                exact = self._references.read_exact(guard.reminder_id)
                if (
                    not isinstance(exact, ExactRead)
                    or exact.reminder.get("id") != guard.reminder_id
                    or not REFERENCE_PATTERN.fullmatch(exact.reference)
                ):
                    raise RuntimeError("canonical final read violated its contract")
            except Exception:
                if outcome.mutation_state == "committed":
                    receipt["status"] = "committed_verification_pending"
                    receipt["ok"] = True
                    receipt["error"] = _error(
                        "sync_pending",
                        "native_final_read_failed",
                        "The native write committed, but the canonical final read failed.",
                        retryable=True,
                    )
                    verification = {
                        "state": "pending",
                        "write_performed": True,
                        "final_read": False,
                    }
                    recovery = {
                        "semantics": "read_reminder_before_retry",
                        "automatic_retry_safe": False,
                    }
                exact = None
            if exact is not None:
                if outcome.mutation_state == "not_mutated":
                    self._references.invalidate_reference(reference)
                after = self._native_state(
                    tool_name,
                    receipt.get("after"),
                    guard.reminder_id,
                    exact.reference,
                )
                verification = {
                    "state": "read_back",
                    "write_performed": outcome.mutation_state == "committed",
                    "final_read": True,
                    "matched": True,
                }

        result: dict[str, Any] = {
            "schema_version": 2,
            "ok": receipt["status"] in SUCCESS_STATUSES,
            "status": receipt["status"],
            "operation": public_operation,
            "operation_id": _operation_id(receipt.get("operation_id")),
            "backend": "native_extension",
            "target": target,
            "before": before,
            "after": after,
            "verification": verification,
            "recovery": recovery,
        }
        self._copy_receipt_extras(receipt, result)
        self._finalize_pending_receipt(
            result,
            next_tool="read_reminder",
            next_message=(
                "Read the exact Reminder again before attempting another change."
            ),
        )
        action_key = action.get("idempotency_key")
        if isinstance(action_key, str):
            result["idempotency_key_hash"] = hashlib.sha256(
                action_key.encode("utf-8")
            ).hexdigest()
            result["replayed"] = receipt.get("replayed") is True
        return result

    @staticmethod
    def _native_target(
        tool_name: str,
        reminder_id: str,
        native_arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        if tool_name == "organize_reminder":
            return {
                "reminder_id": reminder_id,
                "section_id": native_arguments.get("section_id"),
                "tag": native_arguments.get("tag"),
            }
        return {
            "reminder_id": reminder_id,
            "attachment_id": native_arguments.get("attachment_id"),
        }

    @staticmethod
    def _pending_mutation_result(
        operation: str,
        *,
        backend: str,
        target: dict[str, Any],
        reason_code: str,
        message: str,
        next_tool: str | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": 2,
            "ok": True,
            "status": "committed_verification_pending",
            "operation": operation,
            "operation_id": str(uuid.uuid4()),
            "backend": backend,
            "target": copy.deepcopy(target),
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
            "error": _error(
                "sync_pending",
                reason_code,
                message,
                retryable=True,
            ),
        }
        if next_tool is not None:
            result["next_action"] = {
                "kind": "fresh_read",
                "tool": next_tool,
                "retry_original_once": False,
                "message": message,
            }
        return result

    @staticmethod
    def _unknown_native_result(
        operation: str,
        target: dict[str, Any],
        reason_code: str,
    ) -> dict[str, Any]:
        return NativeFacade._pending_mutation_result(
            operation,
            backend="native_extension",
            target=target,
            reason_code=reason_code,
            message=(
                "The guarded native call may have committed; read the reminder before retrying."
            ),
            next_tool="read_reminder",
        )

    @staticmethod
    def _native_action(tool_name: str, action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        kind = action.get("kind")
        if tool_name == "organize_reminder":
            cases = {
                "move_to_section": ({"kind", "section_id"}, "section_id", 2048),
                "add_tag": ({"kind", "tag"}, "tag", 512),
                "remove_tag": ({"kind", "tag"}, "tag", 512),
            }
            if kind not in cases:
                raise FacadeError(
                    "invalid_input", "invalid_action", "Unsupported organization action."
                )
            allowed, value_name, maximum = cases[kind]
            _closed(action, allowed, allowed)
            return str(kind), {value_name: _trimmed(action[value_name], value_name, maximum)}

        if kind == "attach_image":
            _closed(action, {"kind", "image_path", "idempotency_key"}, {"kind", "image_path", "idempotency_key"})
            image_path = _trimmed(action["image_path"], "image_path", 4096)
            if not image_path.startswith("/"):
                raise FacadeError(
                    "invalid_input", "image_path_not_absolute", "image_path must be absolute."
                )
            key = NativeFacade._idempotency_key(action["idempotency_key"])
            return "attach_image", {"image_path": image_path, "idempotency_key": key}
        if kind == "attach_url":
            _closed(action, {"kind", "url"}, {"kind", "url"})
            return "attach_url", {"url": NativeFacade._http_url(action["url"])}
        if kind == "replace_image":
            _closed(
                action,
                {"kind", "attachment_id", "image_path", "idempotency_key"},
                {"kind", "attachment_id", "image_path", "idempotency_key"},
            )
            path = _trimmed(action["image_path"], "image_path", 4096)
            if not path.startswith("/"):
                raise FacadeError(
                    "invalid_input", "image_path_not_absolute", "image_path must be absolute."
                )
            return "replace_image", {
                "attachment_id": _trimmed(action["attachment_id"], "attachment_id", 2048),
                "image_path": path,
                "idempotency_key": NativeFacade._idempotency_key(action["idempotency_key"]),
            }
        if kind == "replace_url":
            _closed(
                action,
                {"kind", "attachment_id", "url", "idempotency_key"},
                {"kind", "attachment_id", "url", "idempotency_key"},
            )
            return "replace_url", {
                "attachment_id": _trimmed(action["attachment_id"], "attachment_id", 2048),
                "url": NativeFacade._http_url(action["url"]),
                "idempotency_key": NativeFacade._idempotency_key(action["idempotency_key"]),
            }
        if kind == "delete":
            _closed(action, {"kind", "attachment_id"}, {"kind", "attachment_id"})
            return "delete_attachment", {
                "attachment_id": _trimmed(action["attachment_id"], "attachment_id", 2048)
            }
        raise FacadeError(
            "invalid_input", "invalid_action", "Unsupported attachment action."
        )

    @staticmethod
    def _idempotency_key(value: Any) -> str:
        if not isinstance(value, str) or not IDEMPOTENCY_PATTERN.fullmatch(value):
            raise FacadeError(
                "invalid_input",
                "invalid_idempotency_key",
                "idempotency_key must contain 8-256 safe characters.",
            )
        return value

    @staticmethod
    def _http_url(value: Any) -> str:
        url = _trimmed(value, "url", 8192)
        parsed = urlparse(url)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            raise FacadeError(
                "invalid_input", "invalid_url", "url must be an absolute HTTP or HTTPS URL."
            )
        return url

    @staticmethod
    def _reference(value: Any) -> str:
        if not isinstance(value, str) or not REFERENCE_PATTERN.fullmatch(value):
            raise FacadeError(
                "concurrent_modification",
                "invalid_reference",
                "The revision reference is invalid; read the reminder again.",
            )
        return value

    @staticmethod
    def _validated_outcome(outcome: MutationOutcome) -> dict[str, Any]:
        if outcome.mutation_state not in {"not_mutated", "committed", "unknown"}:
            raise FacadeError(
                "schema_mismatch", "invalid_mutation_state", "Native mutation state is invalid."
            )
        if not isinstance(outcome.receipt, Mapping):
            raise FacadeError(
                "schema_mismatch", "invalid_native_receipt", "Native mutation receipt is invalid."
            )
        receipt = copy.deepcopy(dict(outcome.receipt))
        status = receipt.get("status")
        allowed = {
            "not_mutated": {"unchanged", "failed_no_mutation"},
            "committed": {
                "verified",
                "committed_verification_pending",
                "partial_success",
                "failed_manual_repair_required",
            },
            "unknown": {
                "committed_verification_pending",
                "partial_success",
                "failed_manual_repair_required",
            },
        }
        if status not in allowed[outcome.mutation_state]:
            raise FacadeError(
                "schema_mismatch",
                "mutation_state_status_mismatch",
                "Native mutation state and receipt status disagree.",
            )
        expected_ok = status in SUCCESS_STATUSES
        if receipt.get("ok") is not expected_ok:
            raise FacadeError(
                "schema_mismatch",
                "mutation_status_ok_mismatch",
                "Native receipt status and ok flag disagree.",
            )
        required_objects = {"target", "before", "after", "verification", "recovery"}
        if any(not isinstance(receipt.get(name), Mapping) for name in required_objects):
            raise FacadeError(
                "schema_mismatch",
                "invalid_native_receipt_objects",
                "Native mutation receipt objects are missing or invalid.",
            )
        if "warnings" in receipt:
            warnings = receipt["warnings"]
            if not isinstance(warnings, list) or any(
                not isinstance(item, Mapping)
                and not (isinstance(item, str) and bool(item))
                for item in warnings
            ):
                raise FacadeError(
                    "schema_mismatch",
                    "invalid_native_receipt_warnings",
                    "Native mutation receipt warnings are invalid.",
                )
        if "error" in receipt and not isinstance(receipt["error"], Mapping):
            raise FacadeError(
                "schema_mismatch",
                "invalid_native_receipt_error",
                "Native mutation receipt error is invalid.",
            )
        if status in {"unchanged", "verified"} and not receipt["after"]:
            raise FacadeError(
                "schema_mismatch",
                "native_receipt_after_missing",
                "A terminal native receipt requires a non-empty read-back state.",
            )
        return receipt

    @staticmethod
    def _native_state(
        tool_name: str,
        value: Any,
        reminder_id: str,
        reference: str | None,
    ) -> dict[str, Any] | None:
        if not isinstance(value, Mapping) or not value:
            return None
        raw = dict(value)
        if isinstance(raw.get("reminder"), Mapping):
            raw = {**raw, **dict(raw["reminder"])}
        result: dict[str, Any] = {"reminder_id": reminder_id}
        if reference is not None:
            result["reference"] = reference
        if tool_name == "organize_reminder":
            result["section"] = _section_from_container(raw)
            raw_tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
            result["tags"] = [
                item for candidate in raw_tags if (item := _tag(candidate)) is not None
            ][:200]
        else:
            raw_attachments = (
                raw.get("attachments") if isinstance(raw.get("attachments"), list) else []
            )
            result["attachments"] = [
                item
                for candidate in raw_attachments
                if (item := _attachment(candidate)) is not None
            ][:200]
            if not result["attachments"]:
                single = _attachment(raw.get("attachment"))
                if single is not None:
                    result["attachments"] = [single]
        return result

    @staticmethod
    def _read_success(operation: str, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "ok": True,
            "status": "verified",
            "operation": operation,
            "data": data,
        }

    @staticmethod
    def _raise_backend_read(raw: Any, reason_code: str) -> None:
        error = raw.get("error") if isinstance(raw, Mapping) else None
        normalized = _normalized_error(error)
        raise FacadeError(
            normalized["code"],
            str(normalized.get("reason_code") or reason_code),
            normalized["message"],
            retryable=normalized["retryable"],
        )

    @staticmethod
    def _copy_receipt_extras(raw: Mapping[str, Any], result: dict[str, Any]) -> None:
        raw_warnings = raw.get("warnings", [])
        warnings = (
            [item for value in raw_warnings if (item := _warning(value))]
            if isinstance(raw_warnings, list)
            else []
        )
        if warnings:
            result["warnings"] = warnings[:20]
        if result["status"] in FAILURE_STATUSES or (
            "error" in raw and isinstance(raw.get("error"), Mapping)
        ):
            result["error"] = _normalized_error(raw.get("error"))

    @staticmethod
    def _finalize_pending_receipt(
        result: dict[str, Any],
        *,
        next_tool: str | None,
        next_message: str,
    ) -> None:
        if result.get("status") != "committed_verification_pending":
            return
        warnings = result.setdefault("warnings", [])
        if not any(
            isinstance(item, Mapping) and item.get("code") == "verification_pending"
            for item in warnings
        ):
            warnings.append(
                {
                    "code": "verification_pending",
                    "message": "The native call may have committed; read before retrying.",
                }
            )
        result["warnings"] = warnings[:20]
        current_error = result.get("error")
        if (
            not isinstance(current_error, Mapping)
            or current_error.get("code") != "sync_pending"
        ):
            causal_warning = next(
                (
                    item
                    for item in result["warnings"]
                    if isinstance(item, Mapping)
                    and str(item.get("code") or "").endswith("_pending")
                ),
                {
                    "code": "native_verification_pending",
                    "message": (
                        "The native call may have committed; read before retrying."
                    ),
                },
            )
            reason_code = causal_warning.get("code")
            message = causal_warning.get("message")
            retryable = True
            if isinstance(current_error, Mapping):
                reason_code = current_error.get("reason_code") or reason_code
                message = current_error.get("message") or message
                retryable = current_error.get("retryable") is True
            result["error"] = _error(
                "sync_pending",
                str(reason_code or "native_verification_pending"),
                str(
                    message
                    or "The native call may have committed; read before retrying."
                ),
                retryable=retryable,
            )
        if next_tool is not None:
            result["next_action"] = {
                "kind": "fresh_read",
                "tool": next_tool,
                "retry_original_once": False,
                "message": next_message,
            }


__all__ = ["NativeFacade", "ReferencePort"]
