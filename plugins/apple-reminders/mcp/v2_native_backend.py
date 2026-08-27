#!/usr/bin/env python3
"""Production guarded adapter for the public v2 Native Extension Module.

The MCP server injects the local EventKit and private-adapter transports.  This
module owns guard revalidation, exact private revision acquisition, dispatch,
receipt classification, and final read-back behind the two callables consumed
by :mod:`v2_native`.
"""

from __future__ import annotations

import copy
import sys
import uuid
from pathlib import Path
from typing import Any, Callable


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from reminders_service import Guard, MutationOutcome, ReferenceRejected
from receipt_contract import FAILURE_RECEIPT_STATUSES, SUCCESS_RECEIPT_STATUSES


BridgeCall = Callable[[str, dict[str, Any]], tuple[dict[str, Any], bool]]
AdapterCall = Callable[[list[str]], tuple[dict[str, Any], bool]]
ArgvBuilder = Callable[[str, dict[str, Any]], list[str]]
ReceiptValidator = Callable[..., str | None]


class NativeBackend:
    """Production adapter for guarded reads and mutations of native features."""

    _PUBLIC_READ_ROUTES = {
        "list_sections": "list_reminder_sections",
        "list_tags": "list_reminder_tags",
        "create_section": "create_reminder_section",
    }
    _EXPECTED_OPERATIONS = {
        "move_reminder_to_section": "move_to_section",
        "add_reminder_tag": "add_tag",
        "remove_reminder_tag": "remove_tag",
        "attach_image_to_reminder": "attach_image",
        "copy_image_attachment": "copy_image",
        "attach_url_to_reminder": "attach_url",
        "replace_reminder_attachment": "replace_attachment",
        "delete_reminder_attachment": "delete_attachment",
    }

    def __init__(
        self,
        *,
        bridge_call: BridgeCall,
        adapter_call: AdapterCall,
        build_adapter_argv: ArgvBuilder,
        receipt_validator: ReceiptValidator,
    ) -> None:
        self._bridge_call = bridge_call
        self._adapter_call = adapter_call
        self._build_adapter_argv = build_adapter_argv
        self._receipt_validator = receipt_validator

    def adapter_call(self, command: str, arguments: dict[str, Any]) -> dict[str, Any]:
        public_route = self._PUBLIC_READ_ROUTES.get(command)
        if public_route is None:
            raise RuntimeError(f"Unsupported v2 adapter read: {command}")
        route_arguments = {
            key: value for key, value in arguments.items() if value is not None
        }
        payload, _ = self._adapter_call(
            self._build_adapter_argv(public_route, route_arguments)
        )
        return payload

    def _revalidate_guard(self, guard: Guard) -> dict[str, Any]:
        payload, is_error = self._bridge_call(
            "read_reminder",
            {"reminder_id": guard.reminder_id},
        )
        data = payload.get("data")
        reminder = data.get("reminder") if isinstance(data, dict) else None
        if (
            is_error
            or payload.get("status") != "verified"
            or not isinstance(reminder, dict)
        ):
            raise ReferenceRejected(
                "concurrent_modification",
                "The exact Reminder could not be revalidated before native access",
            )
        if reminder.get("id") != guard.reminder_id:
            raise ReferenceRejected(
                "invalid_reference",
                "The native guard resolved to a different Reminder",
            )
        store_part = (
            reminder.get("source_id")
            or reminder.get("list_id")
            or reminder.get("calendar_id")
        )
        store_identity = (
            f"eventkit:{store_part}" if isinstance(store_part, str) else None
        )
        if (
            store_identity != guard.store_identity
            or reminder.get("last_modified") != guard.public_concurrency_value
        ):
            raise ReferenceRejected(
                "concurrent_modification",
                "The Reminder changed before native access",
            )
        return reminder

    def _private_read_reminder(self, reminder_id: str) -> dict[str, Any]:
        payload, is_error = self._adapter_call(
            ["read_reminder", "--id", reminder_id]
        )
        reminder = payload.get("reminder")
        if is_error or not isinstance(reminder, dict):
            raise RuntimeError("The private Reminder read failed")
        identities = [
            reminder[name]
            for name in ("id", "identifier")
            if reminder.get(name) is not None
        ]
        if not identities or any(identity != reminder_id for identity in identities):
            raise RuntimeError("The private Reminder read returned a different identity")
        return reminder

    def _private_attachments(
        self,
        reminder_id: str,
        *,
        limit: int,
        attachment_type: str | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"reminder_id": reminder_id, "limit": limit}
        if attachment_type is not None:
            arguments["attachment_type"] = attachment_type
        payload, is_error = self._adapter_call(
            self._build_adapter_argv("list_reminder_attachments", arguments)
        )
        if is_error or payload.get("reminder_id") != reminder_id:
            raise RuntimeError("The private attachment read failed")
        return payload

    def read(self, guard: Guard, arguments: dict[str, Any]) -> dict[str, Any]:
        self._revalidate_guard(guard)
        include = set(arguments.get("include") or [])
        result: dict[str, Any] = {"reminder_id": guard.reminder_id}
        if include & {"section", "tags", "sync"}:
            reminder = self._private_read_reminder(guard.reminder_id)
            result.update(copy.deepcopy(reminder))
            result["reminder_id"] = guard.reminder_id
            if "sync" in include and "sync" not in result:
                result["sync"] = {
                    key: reminder.get(key)
                    for key in (
                        "fields_available",
                        "mobile_visible_likely",
                        "has_server_record",
                        "in_cloud",
                        "current_local_version",
                        "latest_synced_version",
                    )
                    if key in reminder
                }
        if "attachments" in include:
            attachments = self._private_attachments(
                guard.reminder_id,
                limit=int(arguments.get("limit", 100)),
                attachment_type=arguments.get("attachment_type"),
            )
            result["attachment_items"] = copy.deepcopy(
                attachments.get("attachments", [])
            )
            result["truncated"] = attachments.get("truncated") is True
        return result

    @staticmethod
    def _failure_outcome(
        guard: Guard,
        command: str,
        *,
        code: str,
        message: str,
    ) -> MutationOutcome:
        return MutationOutcome(
            receipt={
                "ok": False,
                "status": "failed_no_mutation",
                "operation": command,
                "operation_id": str(uuid.uuid4()),
                "backend": "native_extension",
                "target": {"reminder_id": guard.reminder_id},
                "before": {},
                "after": {},
                "verification": {
                    "state": "not_needed",
                    "write_performed": False,
                    "final_read": False,
                },
                "recovery": {
                    "semantics": "not_applicable",
                    "automatic_retry_safe": True,
                },
                "error": {
                    "code": code,
                    "reason_code": code,
                    "message": message,
                    "retryable": code in {"concurrent_modification", "sync_pending"},
                },
            },
            mutation_state="not_mutated",
        )

    @staticmethod
    def _pending_outcome(
        guard: Guard,
        command: str,
        *,
        reason_code: str,
        message: str,
    ) -> MutationOutcome:
        return MutationOutcome(
            receipt={
                "ok": True,
                "status": "committed_verification_pending",
                "operation": command,
                "operation_id": str(uuid.uuid4()),
                "backend": "native_extension",
                "target": {"reminder_id": guard.reminder_id},
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
                        "message": "The native process may have committed; read before retrying.",
                    }
                ],
                "error": {
                    "code": "sync_pending",
                    "reason_code": reason_code,
                    "message": message,
                    "retryable": True,
                },
            },
            mutation_state="unknown",
        )

    @staticmethod
    def _route(
        command: str,
        guard: Guard,
        arguments: dict[str, Any],
        reminder_version: int,
    ) -> tuple[str, dict[str, Any]]:
        base = {
            "reminder_id": guard.reminder_id,
            "if_version": reminder_version,
        }
        if command == "move_to_section":
            return "move_reminder_to_section", {
                **base,
                "section_id": arguments["section_id"],
            }
        if command in {"add_tag", "remove_tag"}:
            tool_name = (
                "add_reminder_tag" if command == "add_tag" else "remove_reminder_tag"
            )
            return tool_name, {**base, "tag": arguments["tag"]}
        if command == "attach_image":
            return "attach_image_to_reminder", {
                **base,
                "image_path": arguments["image_path"],
                "idempotency_key": arguments["idempotency_key"],
            }
        if command == "attach_url":
            return "attach_url_to_reminder", {**base, "url": arguments["url"]}
        if command in {"replace_image", "replace_url"}:
            routed = {
                **base,
                "attachment_id": arguments["attachment_id"],
                "idempotency_key": arguments["idempotency_key"],
            }
            if command == "replace_image":
                routed["image_path"] = arguments["image_path"]
            else:
                routed["url"] = arguments["url"]
            return "replace_reminder_attachment", routed
        if command == "delete_attachment":
            return "delete_reminder_attachment", {
                **base,
                "attachment_id": arguments["attachment_id"],
            }
        raise RuntimeError(f"Unsupported v2 native mutation: {command}")

    def mutate(
        self,
        guard: Guard,
        command: str,
        arguments: dict[str, Any],
    ) -> MutationOutcome:
        try:
            self._revalidate_guard(guard)
            private_state = self._private_attachments(guard.reminder_id, limit=1)
        except ReferenceRejected as exc:
            return self._failure_outcome(
                guard,
                command,
                code="concurrent_modification",
                message=str(exc),
            )
        except Exception as exc:
            return self._failure_outcome(
                guard,
                command,
                code="sync_pending",
                message=f"The private revision could not be read ({type(exc).__name__}).",
            )
        reminder_version = private_state.get("reminder_version")
        if (
            not isinstance(reminder_version, int)
            or isinstance(reminder_version, bool)
            or reminder_version < 0
        ):
            return self._failure_outcome(
                guard,
                command,
                code="schema_mismatch",
                message="The private Reminder revision was unavailable.",
            )

        tool_name, routed_arguments = self._route(
            command,
            guard,
            arguments,
            reminder_version,
        )
        payload, is_error = self._adapter_call(
            self._build_adapter_argv(tool_name, routed_arguments)
        )
        expected_operation = self._EXPECTED_OPERATIONS[tool_name]
        status = payload.get("status")
        if status not in SUCCESS_RECEIPT_STATUSES | FAILURE_RECEIPT_STATUSES:
            return self._pending_outcome(
                guard,
                command,
                reason_code="invalid_native_mutation_receipt",
                message="The native mutation result could not be validated.",
            )
        receipt_error = self._receipt_validator(
            payload,
            expected_operation=expected_operation,
        )
        if receipt_error:
            return self._pending_outcome(
                guard,
                command,
                reason_code="invalid_native_mutation_receipt",
                message=receipt_error,
            )
        if is_error and status not in {
            "failed_no_mutation",
            "failed_manual_repair_required",
        }:
            return self._pending_outcome(
                guard,
                command,
                reason_code="native_mutation_failed_after_dispatch",
                message="The native mutation may have partially committed.",
            )

        if status in {"verified", "unchanged"}:
            try:
                if command in {"move_to_section", "add_tag", "remove_tag"}:
                    final_state = self._private_read_reminder(guard.reminder_id)
                    payload["after"] = final_state
                else:
                    final_state = self._private_attachments(
                        guard.reminder_id,
                        limit=200,
                    )
                    payload["after"] = {
                        "reminder": {"id": guard.reminder_id},
                        "attachments": final_state.get("attachments", []),
                    }
            except Exception:
                if status == "verified":
                    return self._pending_outcome(
                        guard,
                        command,
                        reason_code="native_final_read_failed",
                        message=(
                            "The native write committed, but its final state could not be read."
                        ),
                    )

        if status in {"unchanged", "failed_no_mutation"}:
            mutation_state = "not_mutated"
        elif status in {"verified", "partial_success"}:
            mutation_state = "committed"
        else:
            mutation_state = "unknown"
        return MutationOutcome(receipt=payload, mutation_state=mutation_state)

    @staticmethod
    def _exact_image_attachment(
        payload: dict[str, Any],
        attachment_id: str,
    ) -> dict[str, Any]:
        attachments = payload.get("attachments")
        if not isinstance(attachments, list) or payload.get("truncated") is True:
            raise RuntimeError("The exact source attachment set was not bounded")
        matches = [
            item
            for item in attachments
            if isinstance(item, dict) and item.get("id") == attachment_id
        ]
        if len(matches) != 1 or matches[0].get("type") != "image":
            raise RuntimeError("The exact active source image attachment was not found")
        return copy.deepcopy(matches[0])

    @staticmethod
    def _copy_attachment_identity(value: dict[str, Any]) -> dict[str, Any]:
        return {
            name: value.get(name)
            for name in (
                "id",
                "type",
                "uti",
                "filename",
                "sha512",
                "file_size",
                "width",
                "height",
                "marked_for_deletion",
            )
        }

    @staticmethod
    def _copy_content_identity(value: dict[str, Any]) -> dict[str, Any]:
        return {
            name: value.get(name)
            for name in ("type", "sha512", "file_size", "width", "height")
        }

    def copy_image(
        self,
        destination_guard: Guard,
        source_guard: Guard,
        command: str,
        arguments: dict[str, Any],
    ) -> MutationOutcome:
        """Copy one exact active image under independent source/destination Guards."""

        if command != "copy_image":
            raise RuntimeError(f"Unsupported guarded copy mutation: {command}")
        attachment_id = arguments.get("attachment_id")
        idempotency_key = arguments.get("idempotency_key")
        if (
            arguments.get("source_reminder_id") != source_guard.reminder_id
            or source_guard.reminder_id == destination_guard.reminder_id
            or not isinstance(attachment_id, str)
            or not attachment_id
            or not isinstance(idempotency_key, str)
            or not idempotency_key
        ):
            return self._failure_outcome(
                destination_guard,
                command,
                code="invalid_input",
                message="The guarded copy identities were incomplete or inconsistent.",
            )

        try:
            self._revalidate_guard(destination_guard)
            self._revalidate_guard(source_guard)
            source_before = self._private_attachments(
                source_guard.reminder_id,
                limit=200,
                attachment_type="image",
            )
            destination_before = self._private_attachments(
                destination_guard.reminder_id,
                limit=1,
            )
            source_attachment = self._exact_image_attachment(
                source_before,
                attachment_id,
            )
        except ReferenceRejected as exc:
            return self._failure_outcome(
                destination_guard,
                command,
                code="concurrent_modification",
                message=str(exc),
            )
        except Exception as exc:
            return self._failure_outcome(
                destination_guard,
                command,
                code="sync_pending",
                message=f"The guarded copy preflight failed ({type(exc).__name__}).",
            )

        source_version = source_before.get("reminder_version")
        destination_version = destination_before.get("reminder_version")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (source_version, destination_version)
        ):
            return self._failure_outcome(
                destination_guard,
                command,
                code="schema_mismatch",
                message="Both private Reminder revisions are required for image copy.",
            )

        routed_arguments = {
            "source_reminder_id": source_guard.reminder_id,
            "reminder_id": destination_guard.reminder_id,
            "attachment_id": attachment_id,
            "if_source_version": source_version,
            "if_version": destination_version,
            "idempotency_key": idempotency_key,
        }
        payload, is_error = self._adapter_call(
            self._build_adapter_argv("copy_image_attachment", routed_arguments)
        )
        status = payload.get("status")
        if (
            is_error
            and status == "failed_no_mutation"
            and payload.get("operation") == "copy_image_attachment"
        ):
            # The adapter's generic CLI error boundary reports the command name
            # when copy preflight fails before dispatch. Normalize that one
            # proven-no-write envelope to the adapter operation expected by the
            # guarded backend; post-dispatch or successful receipts must still
            # name copy_image exactly.
            payload = copy.deepcopy(payload)
            payload["operation"] = self._EXPECTED_OPERATIONS[
                "copy_image_attachment"
            ]
        if status not in SUCCESS_RECEIPT_STATUSES | FAILURE_RECEIPT_STATUSES:
            return self._pending_outcome(
                destination_guard,
                command,
                reason_code="invalid_copy_image_receipt",
                message="The image-copy result could not be validated.",
            )
        receipt_error = self._receipt_validator(
            payload,
            expected_operation=self._EXPECTED_OPERATIONS["copy_image_attachment"],
        )
        if receipt_error:
            return self._pending_outcome(
                destination_guard,
                command,
                reason_code="invalid_copy_image_receipt",
                message=receipt_error,
            )
        if is_error and status not in {
            "failed_no_mutation",
            "failed_manual_repair_required",
        }:
            return self._pending_outcome(
                destination_guard,
                command,
                reason_code="copy_image_failed_after_dispatch",
                message="The image copy may have committed to the destination.",
            )
        if status not in {"verified", "unchanged"}:
            mutation_state = (
                "not_mutated" if status == "failed_no_mutation" else "unknown"
            )
            return MutationOutcome(receipt=payload, mutation_state=mutation_state)

        target = payload.get("target")
        new_attachment_id = (
            target.get("attachment_id") if isinstance(target, dict) else None
        )
        if (
            not isinstance(target, dict)
            or target.get("source_reminder_id") != source_guard.reminder_id
            or target.get("reminder_id") != destination_guard.reminder_id
            or target.get("source_attachment_id") != attachment_id
            or not isinstance(new_attachment_id, str)
            or not new_attachment_id
        ):
            return self._pending_outcome(
                destination_guard,
                command,
                reason_code="copy_image_target_mismatch",
                message="The committed copy receipt did not preserve exact identities.",
            )

        try:
            source_after = self._private_attachments(
                source_guard.reminder_id,
                limit=200,
                attachment_type="image",
            )
            destination_after = self._private_attachments(
                destination_guard.reminder_id,
                limit=200,
                attachment_type="image",
            )
            source_attachment_after = self._exact_image_attachment(
                source_after,
                attachment_id,
            )
            destination_attachment = self._exact_image_attachment(
                destination_after,
                new_attachment_id,
            )
            source_unchanged = (
                source_after.get("reminder_version") == source_version
                and self._copy_attachment_identity(source_attachment_after)
                == self._copy_attachment_identity(source_attachment)
            )
            if not source_unchanged:
                raise RuntimeError("The source changed during image copy")
            if self._copy_content_identity(destination_attachment) != self._copy_content_identity(
                source_attachment
            ):
                raise RuntimeError("The destination image content differs from the source")
        except Exception:
            return self._pending_outcome(
                destination_guard,
                command,
                reason_code="copy_image_final_read_failed",
                message=(
                    "The destination may contain the copied image, but exact source and "
                    "destination read-back did not complete."
                ),
            )

        payload["after"] = {
            "reminder": {"id": destination_guard.reminder_id},
            "attachments": destination_after.get("attachments", []),
        }
        verification = payload.setdefault("verification", {})
        verification.update(
            {
                "state": "read_back",
                "write_performed": status == "verified",
                "final_read": True,
                "matched": True,
                "source_unchanged": True,
                "destination_attachment_active": True,
            }
        )
        payload.setdefault("recovery", {}).setdefault(
            "automatic_retry_safe", status == "unchanged"
        )
        payload["target"] = {
            "source_reminder_id": source_guard.reminder_id,
            "reminder_id": destination_guard.reminder_id,
            "source_attachment_id": attachment_id,
            "attachment_id": destination_attachment["id"],
        }
        return MutationOutcome(
            receipt=payload,
            mutation_state="committed" if status == "verified" else "not_mutated",
        )
