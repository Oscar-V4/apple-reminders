#!/usr/bin/env python3
"""Production EventKit adapter for the public v2 Core Module.

The MCP server injects the local subprocess transports.  Core uses EventKit
only by default.  An explicitly enabled Experimental runtime can also compose
the legacy native URL attachment workflow through the same ``EventKitPort``
interface consumed by :mod:`v2_core`.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import re
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from receipt_contract import (
    AdapterError,
    MutationNotStartedError,
    validated_receipt_mutation_state,
)
from eventkit_protocol import (
    validate_ensure_list_receipt,
    validate_mutation_receipt as validate_eventkit_mutation_receipt,
    validate_response as validate_eventkit_response,
)
from durable_idempotency import idempotency_key_hash

if __package__:  # Package import in tests; script-local import in the stdio server.
    from .v2_core import EventKitReply, MutationState
    from .v2_transport import TransportResult
else:  # pragma: no cover - exercised by the script entry point
    from v2_core import EventKitReply, MutationState
    from v2_transport import TransportResult


BridgeCall = Callable[[str, dict[str, Any]], TransportResult]
AdapterCall = Callable[[list[str]], TransportResult]
ArgvBuilder = Callable[[str, dict[str, Any]], list[str]]
ReceiptValidator = Callable[..., str | None]


class IdempotencyCall(Protocol):
    def __call__(
        self,
        *,
        operation: str,
        key: str | None,
        input_payload: dict[str, Any],
        callback: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]: ...


class _EventKitBridgeFailure(RuntimeError):
    def __init__(
        self,
        payload: dict[str, Any],
        mutation_state: MutationState,
    ) -> None:
        super().__init__("EventKit bridge operation failed")
        self.payload = payload
        self.mutation_state = mutation_state


class _EventKitReceiptFailure(RuntimeError):
    pass


class CoreBackend:
    """Production adapter satisfying the Core Module's ``EventKitPort`` seam."""

    _MUTATION_TOOL_NAMES = {
        "ensure_reminder_list": "ensure_reminder_list",
        "create_reminder": "create_reminder",
        "update_reminder": "update_reminder",
        "complete_reminder": "complete_reminder",
        "reopen_reminder": "reopen_reminder",
        "move_reminder": "move_reminder_to_list",
        "delete_reminder": "delete_reminder",
    }
    _DURABLE_OPERATION_NAMES = {
        "create_reminder": "eventkit_create_reminder",
        "ensure_reminder_list": "eventkit_ensure_reminder_list",
    }

    def __init__(
        self,
        *,
        bridge_call: BridgeCall,
        adapter_call: AdapterCall,
        build_adapter_argv: ArgvBuilder,
        idempotency_call: IdempotencyCall,
        receipt_validator: ReceiptValidator,
        enable_experimental: bool = False,
    ) -> None:
        self._bridge_call = bridge_call
        self._adapter_call = adapter_call
        self._build_adapter_argv = build_adapter_argv
        self._idempotency_call = idempotency_call
        self._receipt_validator = receipt_validator
        self._enable_experimental = enable_experimental

    def invoke(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        mutation: bool,
    ) -> EventKitReply:
        supplied = copy.deepcopy(dict(arguments))
        if mutation:
            tool_name = self._MUTATION_TOOL_NAMES.get(operation)
            if tool_name is None:
                raise RuntimeError(f"Unsupported v2 EventKit mutation: {operation}")
            payload, is_error, mutation_state = self._invoke_mutation(
                tool_name,
                operation,
                supplied,
            )
            durable_operation = self._DURABLE_OPERATION_NAMES.get(tool_name)
            key = supplied.get("idempotency_key")
            if durable_operation is not None and isinstance(key, str) and key:
                payload["idempotency_key_hash"] = idempotency_key_hash(
                    durable_operation,
                    key,
                )
                payload.setdefault("replayed", False)
        else:
            transport = self._bridge_call(operation, supplied)
            payload = transport.payload
            is_error = transport.is_error
            mutation_state = None
        return EventKitReply(
            payload=payload,
            is_error=is_error,
            mutation_state=mutation_state,
        )

    @staticmethod
    def _mutation_reminder_id(payload: dict[str, Any]) -> str | None:
        for container_name in ("target", "after"):
            container = payload.get(container_name)
            if not isinstance(container, dict):
                continue
            for key in ("reminder_id", "id", "external_id"):
                value = container.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    @staticmethod
    def _url_attachment_partial_receipt(
        payload: dict[str, Any],
        *,
        code: str,
        message: str,
    ) -> dict[str, Any]:
        initial_status = payload.get("status")
        existing_verification = payload.get("verification")
        eventkit_write_committed = initial_status == "verified" or (
            isinstance(existing_verification, dict)
            and existing_verification.get("write_performed") is True
        )
        payload["ok"] = True
        payload["status"] = "partial_success"
        payload.setdefault("warnings", []).append({"code": code, "message": message})
        verification = payload.setdefault("verification", {})
        verification.update(
            {
                "state": "partial",
                "write_performed": True if eventkit_write_committed else None,
                "final_read": False,
            }
        )
        verification["url_attachment"] = {
            "state": "failed",
            "status": "failed_manual_repair_required",
            "attachment_active": False,
            "error_code": code,
        }
        recovery = payload.setdefault("recovery", {})
        recovery.update(
            {
                "semantics": "repair_visible_url_after_fresh_read",
                "automatic_retry_safe": False,
            }
        )
        recovery["url_attachment"] = {
            "semantics": "call_attach_url_to_reminder_after_fresh_attachment_read",
            "automatic_retry_safe": False,
        }
        payload["error"] = {
            "code": "sync_pending",
            "reason_code": code,
            "message": message,
            "retryable": True,
        }
        return payload

    @staticmethod
    def _url_attachment_no_write_ambiguity_receipt(
        payload: dict[str, Any],
        *,
        code: str,
        message: str,
    ) -> dict[str, Any]:
        payload["ok"] = False
        payload["status"] = "failed_no_mutation"
        payload["after"] = {}
        verification = payload.setdefault("verification", {})
        verification.update(
            {
                "state": "not_needed",
                "write_performed": False,
                "final_read": False,
                "matched": False,
            }
        )
        verification["url_attachment"] = {
            "state": "ambiguous",
            "status": "failed_no_mutation",
            "error_code": code,
        }
        recovery = payload.setdefault("recovery", {})
        recovery.update(
            {
                "semantics": "inspect_url_attachments_before_exact_cleanup",
                "automatic_retry_safe": False,
                "manual_action": (
                    "Read the exact Reminder to obtain a fresh Reference, use "
                    "inspect_reminder_native with that Reference, then use "
                    "change_reminder_attachment only for an exact attachment ID "
                    "that the user intends to clean up."
                ),
            }
        )
        payload["error"] = {
            "code": "ambiguous_scope",
            "reason_code": code,
            "message": message,
            "retryable": False,
        }
        payload["next_action"] = {
            "kind": "fresh_read",
            "tool": "read_reminder",
            "retry_original_once": False,
            "message": (
                "Read the exact Reminder to obtain a fresh Reference before native "
                "attachment inspection; do not retry the URL patch."
            ),
        }
        return payload

    @staticmethod
    def _is_rfc3339_timestamp(value: Any) -> bool:
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})",
            value,
        ):
            return False
        try:
            dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True

    @staticmethod
    def _url_final_read_pending(
        payload: dict[str, Any],
        *,
        reminder_id: str,
        attachment: dict[str, Any] | None,
        reason_code: str,
    ) -> dict[str, Any]:
        message = (
            "The EventKit write and native URL attachment step completed, but the final "
            "exact EventKit read was not safe to use. Read the reminder again before the "
            "next guarded write."
        )
        safe_after: dict[str, Any] = {"id": reminder_id}
        if attachment is not None:
            safe_after["url_attachment"] = attachment
        payload["after"] = safe_after
        payload.setdefault("warnings", []).append(
            {"code": "eventkit_final_read_pending", "message": message}
        )
        verification = payload.setdefault("verification", {})
        verification["eventkit_final_read"] = {
            "state": "pending",
            "reason_code": reason_code,
            "last_modified_safe_for_precondition": False,
        }
        payload.setdefault("recovery", {})["eventkit_final_read"] = {
            "semantics": "read_reminder_before_next_write",
            "automatic_retry_safe": False,
        }
        if payload.get("status") == "partial_success":
            verification["state"] = "partial"
            verification["final_read"] = False
            verification["matched"] = None
            return payload
        payload["ok"] = True
        payload["status"] = "committed_verification_pending"
        verification["state"] = "pending"
        url_verification = verification.get("url_attachment")
        verification["write_performed"] = (
            True
            if verification.get("write_performed") is True
            or (
                isinstance(url_verification, dict)
                and url_verification.get("status") == "verified"
            )
            else None
        )
        verification["final_read"] = False
        verification["matched"] = None
        payload["error"] = {
            "code": "sync_pending",
            "reason_code": reason_code,
            "message": message,
        }
        return payload

    def _ensure_visible_url_attachment(
        self,
        payload: dict[str, Any],
        url: str,
    ) -> dict[str, Any]:
        if payload.get("status") not in {"verified", "unchanged"}:
            return payload
        reminder_id = self._mutation_reminder_id(payload)
        if reminder_id is None:
            return self._url_attachment_partial_receipt(
                payload,
                code="native_url_attachment_target_missing",
                message=(
                    "The EventKit write succeeded, but its reminder identifier was unavailable for "
                    "the visible URL attachment step."
                ),
            )

        initial_eventkit_status = str(payload["status"])

        def inventory_failure(*, code: str, message: str) -> dict[str, Any]:
            if initial_eventkit_status == "unchanged":
                return self._url_attachment_no_write_ambiguity_receipt(
                    payload,
                    code=code,
                    message=message,
                )
            return self._url_attachment_partial_receipt(
                payload,
                code=code,
                message=message,
            )

        attachment: dict[str, Any] | None = None
        attachment_status: str | None = None
        before = payload.get("before")
        previous_url = before.get("url") if isinstance(before, Mapping) else None
        list_arguments = {
            "reminder_id": reminder_id,
            "attachment_type": "url",
            "limit": 200,
        }
        list_transport = self._adapter_call(
            self._build_adapter_argv("list_reminder_attachments", list_arguments)
        )
        list_payload = list_transport.payload
        list_is_error = list_transport.is_error
        reminder_version = list_payload.get("reminder_version")
        if (
            list_is_error
            or list_payload.get("ok") is not True
            or not isinstance(reminder_version, int)
            or isinstance(reminder_version, bool)
            or reminder_version < 0
        ):
            error = (
                list_payload.get("error")
                if isinstance(list_payload.get("error"), dict)
                else {}
            )
            return inventory_failure(
                code=str(error.get("code") or "native_url_attachment_precondition_failed"),
                message=(
                    "The EventKit write succeeded, but the plugin could not obtain a "
                    "fresh reminder "
                    "version for the visible URL attachment."
                ),
            )
        else:
            raw_attachments = list_payload.get("attachments")
            inventory_valid = (
                list_payload.get("reminder_id") == reminder_id
                and isinstance(raw_attachments, list)
                and isinstance(list_payload.get("truncated"), bool)
            )
            seen_attachment_ids: set[str] = set()
            if inventory_valid:
                for item in raw_attachments:
                    item_id = item.get("id") if isinstance(item, Mapping) else None
                    if (
                        not isinstance(item, Mapping)
                        or not isinstance(item_id, str)
                        or not item_id
                        or item_id in seen_attachment_ids
                        or item.get("type") != "url"
                        or not isinstance(item.get("url"), str)
                        or not item.get("url")
                    ):
                        inventory_valid = False
                        break
                    seen_attachment_ids.add(item_id)
            if not inventory_valid:
                return inventory_failure(
                    code="native_url_attachment_inventory_invalid",
                    message=(
                        "The plugin could not prove a complete exact URL attachment "
                        "inventory, so it performed no native attachment write."
                    ),
                )
            attachments = raw_attachments
            matching_previous = [
                item
                for item in attachments
                if isinstance(item, Mapping)
                and item.get("type") == "url"
                and item.get("url") == previous_url
            ]
            matching_target = [
                item
                for item in attachments
                if isinstance(item, Mapping)
                and item.get("type") == "url"
                and item.get("url") == url
            ]
            other_url_attachments = [
                item
                for item in attachments
                if isinstance(item, Mapping)
                and item.get("type") == "url"
                and item.get("url") != url
            ]
            mutation_tool = "attach_url_to_reminder"
            expected_operation = "attach_url"
            attachment_arguments: dict[str, Any] = {
                "reminder_id": reminder_id,
                "url": url,
                "if_version": reminder_version,
            }
            replacing_previous = (
                isinstance(previous_url, str)
                and bool(previous_url)
                and previous_url != url
            )
            ambiguity_code: str | None = None
            no_write_ambiguity = False
            reuse_existing = False
            if list_payload.get("truncated") is True:
                ambiguity_code = "native_url_attachment_inventory_truncated"
            elif len(matching_target) > 1:
                ambiguity_code = "ambiguous_target_url_attachment"
            elif (
                initial_eventkit_status == "unchanged"
                and other_url_attachments
            ):
                # A fresh same-URL retry no longer carries the A -> B lineage
                # from an earlier partial composed write. Any non-target URL
                # could be an intentional link or the stale A, whether or not B
                # is already present. Appending B in the A-only case would
                # create the same ambiguous A+B state and falsely claim success.
                no_write_ambiguity = True
            elif len(matching_target) == 1 and replacing_previous and matching_previous:
                # This is the characteristic retry state after the visible B
                # attachment committed but the composed A -> B Receipt was
                # uncertain. Replacing A now would create a second B. Preserve
                # both exact objects and ask for an explicit attachment cleanup.
                ambiguity_code = "target_url_attachment_already_exists"
            elif len(matching_target) == 1:
                reuse_existing = True
            elif replacing_previous and len(matching_previous) > 1:
                ambiguity_code = "ambiguous_visible_url_attachment"
            elif replacing_previous and len(matching_previous) == 1:
                attachment_id = matching_previous[0].get("id")
                if not isinstance(attachment_id, str) or not attachment_id:
                    ambiguity_code = "native_url_attachment_identity_missing"
                else:
                    mutation_tool = "replace_reminder_attachment"
                    expected_operation = "replace_attachment"
                    attachment_arguments["attachment_id"] = attachment_id
                    attachment_arguments["idempotency_key"] = (
                        "core-url-replace-"
                        + hashlib.sha256(
                            (
                                f"{payload.get('operation_id')}\n{reminder_id}\n"
                                f"{attachment_id}\n{url}"
                            ).encode("utf-8")
                        ).hexdigest()
                    )

            if reuse_existing:
                attachment = copy.deepcopy(dict(matching_target[0]))
                attachment_status = "unchanged"
                payload.setdefault("verification", {})["url_attachment"] = {
                    "state": "read_back",
                    "write_performed": False,
                    "final_read": True,
                    "matched": True,
                    "attachment_active": True,
                    "status": "unchanged",
                }
                payload.setdefault("recovery", {})["url_attachment"] = {
                    "semantics": "not_applicable",
                    "automatic_retry_safe": True,
                }
                attach_payload: dict[str, Any] = {}
                attach_is_error = False
            elif no_write_ambiguity:
                attachment = (
                    copy.deepcopy(dict(matching_target[0]))
                    if matching_target
                    else None
                )
                self._url_attachment_no_write_ambiguity_receipt(
                    payload,
                    code="ambiguous_visible_url_attachment",
                    message=(
                        "The EventKit URL was already unchanged, but a non-target URL "
                        "attachment is still present. No attachment was changed because "
                        "the plugin cannot infer whether that object is an intentional "
                        "link or a stale replacement source, and it cannot safely append "
                        "another target attachment. Inspect the exact native "
                        "attachments before cleaning up one exact attachment ID."
                    ),
                )
                attach_payload = {}
                attach_is_error = True
            elif ambiguity_code is not None:
                self._url_attachment_partial_receipt(
                    payload,
                    code=ambiguity_code,
                    message=(
                        "The EventKit URL changed, but the visible URL attachment state "
                        "could not be selected uniquely without risking a duplicate or "
                        "removing the wrong object. Existing attachments were preserved; "
                        "inspect the exact Reminder before any attachment change."
                    ),
                )
                attach_payload = {}
                attach_is_error = True
            else:
                attach_transport = self._adapter_call(
                    self._build_adapter_argv(mutation_tool, attachment_arguments)
                )
                attach_payload = attach_transport.payload
                attach_is_error = attach_transport.is_error
            receipt_error = (
                self._receipt_validator(
                    attach_payload,
                    expected_operation=expected_operation,
                )
                if not attach_is_error
                and ambiguity_code is None
                and not no_write_ambiguity
                else None
            )
            candidate_attachment = (
                attach_payload.get("after", {}).get("attachment")
                if isinstance(attach_payload.get("after"), dict)
                else None
            )
            attachment_active = (
                attach_payload.get("verification", {}).get("attachment_active") is True
                if isinstance(attach_payload.get("verification"), dict)
                else False
            )
            attachment_receipt_complete = attach_payload.get("status") in {
                "verified",
                "unchanged",
            }
            attachment_matches = (
                isinstance(candidate_attachment, dict)
                and candidate_attachment.get("url") == url
            )
            if reuse_existing or ambiguity_code is not None or no_write_ambiguity:
                pass
            elif (
                attach_is_error
                or receipt_error
                or not attachment_receipt_complete
                or not attachment_active
                or not attachment_matches
            ):
                error = (
                    attach_payload.get("error")
                    if isinstance(attach_payload.get("error"), dict)
                    else {}
                )
                self._url_attachment_partial_receipt(
                    payload,
                    code=str(
                        error.get("code")
                        or (
                            "invalid_adapter_receipt"
                            if receipt_error
                            else "native_url_attachment_failed"
                        )
                    ),
                    message=(
                        "The EventKit write succeeded, but the native URL attachment was not "
                        "verified in Reminders."
                    ),
                )
            else:
                attachment = candidate_attachment
                attachment_status = str(attach_payload["status"])
                payload.setdefault("verification", {})["url_attachment"] = {
                    **attach_payload["verification"],
                    "status": attachment_status,
                }
                payload.setdefault("recovery", {})["url_attachment"] = attach_payload[
                    "recovery"
                ]

        final_transport = self._bridge_call(
            "read_reminder",
            {"reminder_id": reminder_id},
        )
        final_payload = final_transport.payload
        final_is_error = final_transport.is_error
        final_data = final_payload.get("data")
        final_reminder = (
            final_data.get("reminder")
            if isinstance(final_data, dict)
            and isinstance(final_data.get("reminder"), dict)
            else None
        )
        final_error = (
            final_payload.get("error")
            if isinstance(final_payload.get("error"), dict)
            else {}
        )
        reason_code: str | None = None
        if final_is_error:
            reason_code = str(
                final_error.get("reason_code")
                or final_error.get("code")
                or "eventkit_final_read_failed"
            )
        elif final_payload.get("status") != "verified" or final_reminder is None:
            reason_code = "eventkit_final_read_unverified"
        elif self._mutation_reminder_id({"after": final_reminder}) != reminder_id:
            reason_code = "eventkit_final_read_target_mismatch"
        elif final_reminder.get("url") != url:
            reason_code = "eventkit_final_read_url_mismatch"
        elif not self._is_rfc3339_timestamp(final_reminder.get("last_modified")):
            reason_code = "eventkit_final_last_modified_invalid"
        if reason_code is not None:
            return self._url_final_read_pending(
                payload,
                reminder_id=reminder_id,
                attachment=attachment,
                reason_code=reason_code,
            )

        payload.setdefault("verification", {})["eventkit_final_read"] = {
            "state": "read_back",
            "reminder_id": reminder_id,
            "last_modified_safe_for_precondition": True,
        }
        payload.setdefault("recovery", {})["eventkit_final_read"] = {
            "semantics": "not_applicable",
            "automatic_retry_safe": True,
        }
        final_after = dict(final_reminder)
        if attachment is not None:
            final_after["url_attachment"] = attachment
        if payload.get("status") == "partial_success":
            payload["after"] = final_after
            verification = payload.setdefault("verification", {})
            verification["state"] = "partial"
            verification["final_read"] = True
            verification["matched"] = True
            return payload
        if payload.get("status") == "failed_no_mutation":
            verification = payload.setdefault("verification", {})
            verification["state"] = "not_needed"
            verification["write_performed"] = False
            verification["final_read"] = False
            verification["matched"] = False
            payload["after"] = {}
            return payload
        payload["after"] = final_after
        payload["status"] = (
            "unchanged"
            if initial_eventkit_status == "unchanged"
            and attachment_status == "unchanged"
            else "verified"
        )
        verification = payload.setdefault("verification", {})
        verification.update(
            {
                "state": "read_back",
                "write_performed": payload["status"] == "verified",
                "final_read": True,
                "matched": True,
            }
        )
        return payload

    @staticmethod
    def _validate_ensure_list_identity(
        payload: dict[str, Any],
        arguments: dict[str, Any],
        *,
        rehydrate: bool = False,
    ) -> dict[str, Any]:
        try:
            return validate_ensure_list_receipt(
                payload,
                source_id=arguments.get("source_id"),
                name=arguments.get("name"),
                rehydrate=rehydrate,
            )
        except RuntimeError as exc:
            raise _EventKitReceiptFailure(str(exc)) from exc

    @staticmethod
    def _merge_warning(payload: dict[str, Any], *, code: str, message: str) -> None:
        raw = payload.get("warnings")
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw if isinstance(raw, list) else []:
            item_code = item.get("code") if isinstance(item, Mapping) else None
            if isinstance(item_code, str) and item_code not in seen:
                items.append(dict(item))
                seen.add(item_code)
        if code not in seen:
            items.append({"code": code, "message": message[:2000]})
        priority = {code, "idempotency_privacy_scrub_failed"}
        payload["warnings"] = sorted(
            items,
            key=lambda item: item.get("code") not in priority,
        )[:20]

    def _invoke_mutation(
        self,
        tool_name: str,
        operation: str,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], bool, MutationState]:
        bridge_arguments = dict(arguments)
        idempotency_key = bridge_arguments.pop("idempotency_key", None)
        executed_state: MutationState | None = None

        def execute_once() -> dict[str, Any]:
            nonlocal executed_state
            transport = self._bridge_call(operation, bridge_arguments)
            payload = transport.payload
            is_error = transport.is_error
            if is_error:
                transport_not_started = transport.proves_not_started
                contract_proved_no_write = (
                    payload.get("status") == "failed_no_mutation"
                    and payload.get("ok") is False
                )
                error_state: MutationState = "unknown"
                if transport_not_started:
                    error_state = "not_mutated"
                elif contract_proved_no_write:
                    try:
                        validate_eventkit_response(payload, operation)
                    except RuntimeError:
                        contract_proved_no_write = False
                    else:
                        error_state = "not_mutated"
                proven_no_write = (
                    tool_name in self._DURABLE_OPERATION_NAMES
                    and (transport_not_started or contract_proved_no_write)
                )
                if proven_no_write and error_state != "not_mutated":
                    proven_no_write = False
                if proven_no_write:
                    error = payload.get("error")
                    if not transport_not_started and not isinstance(error, dict):
                        proven_no_write = False
                if proven_no_write:
                    public_payload = copy.deepcopy(payload)
                    if transport_not_started:
                        error = payload.get("error")
                        if (
                            not isinstance(error, dict)
                            or not isinstance(error.get("code"), str)
                            or not isinstance(error.get("message"), str)
                        ):
                            error = {
                                "code": "unexpected_error",
                                "reason_code": "invalid_prelaunch_failure_payload",
                                "message": "The EventKit bridge did not start.",
                                "retryable": True,
                            }
                        public_payload = {
                            "schema_version": 1,
                            "operation": operation,
                            "status": "failed_no_mutation",
                            "ok": False,
                            "error": copy.deepcopy(error),
                        }
                    raise MutationNotStartedError(
                        str(error["message"]),
                        code=str(error["code"]),
                        reason_code=str(error.get("reason_code") or error["code"]),
                        result_status="failed_no_mutation",
                        public_payload=public_payload,
                    )
                raise _EventKitBridgeFailure(payload, error_state)
            try:
                validate_eventkit_response(payload, operation)
            except RuntimeError as exc:
                raise _EventKitReceiptFailure(str(exc)) from exc
            if tool_name == "ensure_reminder_list":
                payload = self._validate_ensure_list_identity(payload, bridge_arguments)
            raw_state = validated_receipt_mutation_state(payload)
            visible_url = (
                bridge_arguments.get("url")
                if tool_name == "create_reminder"
                else None
            )
            if tool_name == "update_reminder" and isinstance(
                bridge_arguments.get("patch"), dict
            ):
                visible_url = bridge_arguments["patch"].get("url")
            if self._enable_experimental and isinstance(visible_url, str):
                payload = self._ensure_visible_url_attachment(payload, visible_url)
            projected_state = validated_receipt_mutation_state(payload)
            executed_state = (
                "committed"
                if raw_state == "committed"
                else "unknown"
                if raw_state == "unknown"
                else projected_state
            )
            return payload

        payload: dict[str, Any] = {}
        try:
            durable_operation = self._DURABLE_OPERATION_NAMES.get(tool_name)
            if durable_operation is not None:
                request = {
                    "schema_version": 1,
                    "operation": operation,
                    **bridge_arguments,
                }
                if tool_name == "create_reminder" and isinstance(
                    bridge_arguments.get("url"), str
                ):
                    # The same URL has different completion semantics in the
                    # two runtimes. Never replay a metadata-only receipt as a
                    # verified visible attachment (or the reverse).
                    request["url_write_mode"] = (
                        "native_attachment"
                        if self._enable_experimental
                        else "eventkit_metadata_only"
                    )
                payload = self._idempotency_call(
                    operation=durable_operation,
                    key=idempotency_key,
                    input_payload=request,
                    callback=execute_once,
                )
                if tool_name == "ensure_reminder_list":
                    payload = self._validate_ensure_list_identity(
                        payload,
                        bridge_arguments,
                        rehydrate=payload.get("replayed") is True,
                    )
                if (
                    tool_name == "create_reminder"
                    and payload.get("replayed") is True
                    and payload.get("status") == "committed_verification_pending"
                ):
                    self._merge_warning(
                        payload,
                        code="verification_pending",
                        message=(
                            "This replayed creation receipt is still awaiting verification."
                        ),
                    )
                    pending_error = payload.get("error")
                    if not isinstance(pending_error, dict):
                        pending_error = {}
                    pending_error["code"] = "sync_pending"
                    pending_error["message"] = (
                        "The original creation committed but verification remains pending."
                    )
                    payload["error"] = pending_error
            else:
                payload = execute_once()
        except _EventKitBridgeFailure as exc:
            return exc.payload, True, exc.mutation_state
        except _EventKitReceiptFailure as exc:
            failure = {
                "ok": False,
                "replayed": payload.get("replayed") is True,
                "error": {
                    "code": "invalid_eventkit_receipt",
                    "message": str(exc),
                },
            }
            return (
                failure,
                True,
                "unknown",
            )
        except AdapterError as exc:
            error_state: MutationState = (
                "not_mutated"
                if isinstance(exc, MutationNotStartedError)
                else "unknown"
            )
            eventkit_payload = getattr(exc, "public_payload", None)
            if isinstance(eventkit_payload, dict):
                return copy.deepcopy(eventkit_payload), True, error_state
            error = {
                "code": exc.code,
                "message": str(exc),
                "details": exc.details,
            }
            reason_code = exc.details.get("reason_code")
            if isinstance(reason_code, str) and reason_code:
                error["reason_code"] = reason_code
            failure = {
                "ok": False,
                "status": "failed_no_mutation",
                "operation": operation,
                "error": error,
            }
            return (
                failure,
                True,
                error_state,
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": {
                    "code": "unexpected_error",
                    "message": "The durable EventKit operation failed unexpectedly.",
                },
            }, True, "unknown"
        if executed_state is None:
            try:
                validate_eventkit_mutation_receipt(payload, operation)
            except RuntimeError:
                mutation_state: MutationState = "unknown"
            else:
                mutation_state = validated_receipt_mutation_state(payload)
        else:
            mutation_state = executed_state
        return payload, payload.get("ok") is not True, mutation_state
