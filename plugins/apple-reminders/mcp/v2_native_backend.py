#!/usr/bin/env python3
"""Production guarded adapter for the public v2 Native Extension Module.

The MCP server injects the local EventKit and private-adapter transports.  This
module owns guard revalidation, exact private revision acquisition, dispatch,
receipt classification, and final read-back behind the two callables consumed
by :mod:`v2_native`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Callable


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from reminders_service import (
    Guard,
    MutationOutcome,
    MutationState,
    ReferenceRejected,
    mutation_state_after_unverified_projection,
    unverified_mutation_projection,
)
from receipt_contract import (
    FAILURE_RECEIPT_STATUSES,
    SUCCESS_RECEIPT_STATUSES,
    validated_receipt_mutation_state,
)
if __package__:
    from .v2_transport import TransportResult
else:  # pragma: no cover - exercised by the stdio entry point
    from v2_transport import TransportResult


BridgeCall = Callable[[str, dict[str, Any]], TransportResult]
AdapterCall = Callable[[list[str]], TransportResult]
ArgvBuilder = Callable[[str, dict[str, Any]], list[str]]
ReceiptValidator = Callable[..., str | None]


class NativeBackend:
    """Production adapter for guarded reads and mutations of native features."""

    _PUBLIC_READ_ROUTES = {
        "list_sections": "list_reminder_sections",
        "list_tags": "list_reminder_tags",
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
        transport = self._adapter_call(
            self._build_adapter_argv(public_route, route_arguments)
        )
        return transport.payload

    def create_section(self, list_id: str, name: str) -> MutationOutcome:
        route_arguments = {"list_id": list_id, "name": name}
        target = {"list_id": list_id, "section_id": None}
        try:
            transport = self._adapter_call(
                self._build_adapter_argv(
                    "create_reminder_section",
                    route_arguments,
                )
            )
        except Exception as exc:
            return self._pending_outcome(
                target,
                "create_section",
                reason_code="native_section_dispatch_failed",
                message=(
                    "The section helper did not return after dispatch may have "
                    f"started ({type(exc).__name__})."
                ),
            )

        if transport.proves_not_started:
            raw_error = (
                transport.payload.get("error")
                if isinstance(transport.payload.get("error"), dict)
                else {}
            )
            code = str(raw_error.get("code") or "adapter_unavailable")
            message = str(
                raw_error.get("message")
                or "The section helper could not be started."
            )
            return self._failure_outcome(
                target,
                "create_section",
                code=code,
                message=message,
            )

        payload = copy.deepcopy(transport.payload)
        status = payload.get("status")
        if status not in SUCCESS_RECEIPT_STATUSES | FAILURE_RECEIPT_STATUSES:
            return self._pending_outcome(
                target,
                "create_section",
                reason_code="invalid_native_section_receipt",
                message="The section result could not be validated.",
            )
        receipt_error = self._receipt_validator(
            payload,
            expected_operation="create_section",
        )
        if receipt_error:
            return self._pending_outcome(
                target,
                "create_section",
                reason_code="invalid_native_section_receipt",
                message=receipt_error,
            )
        if transport.is_error and status not in {
            "failed_no_mutation",
            "failed_manual_repair_required",
        }:
            return self._pending_outcome(
                target,
                "create_section",
                reason_code="native_section_failed_after_dispatch",
                message="The section mutation may have partially committed.",
            )

        mutation_state = validated_receipt_mutation_state(payload)

        if status in {"verified", "unchanged"}:
            raw_after = payload.get("after")
            after = raw_after.get("section") if isinstance(raw_after, dict) else None
            if (
                not isinstance(after, dict)
                or after.get("list_id") != list_id
                or not isinstance(after.get("id"), str)
                or not after.get("id")
            ):
                return self._pending_outcome(
                    target,
                    "create_section",
                    reason_code="native_section_identity_mismatch",
                    message=(
                        "The section result could not be bound to the requested "
                        "exact list."
                    ),
                    mutation_state=mutation_state_after_unverified_projection(
                        mutation_state
                    ),
                )
        return MutationOutcome(receipt=payload, mutation_state=mutation_state)

    def _revalidate_guard(self, guard: Guard) -> dict[str, Any]:
        transport = self._bridge_call(
            "read_reminder",
            {"reminder_id": guard.reminder_id},
        )
        payload = transport.payload
        is_error = transport.is_error
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
        transport = self._adapter_call(
            ["read_reminder", "--id", reminder_id]
        )
        payload = transport.payload
        is_error = transport.is_error
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
        transport = self._adapter_call(
            self._build_adapter_argv("list_reminder_attachments", arguments)
        )
        payload = transport.payload
        is_error = transport.is_error
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
        owner: Guard | dict[str, Any],
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
                "target": (
                    {"reminder_id": owner.reminder_id}
                    if isinstance(owner, Guard)
                    else copy.deepcopy(owner)
                ),
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
        owner: Guard | dict[str, Any],
        command: str,
        *,
        reason_code: str,
        message: str,
        mutation_state: MutationState = "unknown",
    ) -> MutationOutcome:
        receipt: dict[str, Any] = {
            **unverified_mutation_projection(mutation_state),
            "operation": command,
            "operation_id": str(uuid.uuid4()),
            "backend": "native_extension",
            "target": (
                {"reminder_id": owner.reminder_id}
                if isinstance(owner, Guard)
                else copy.deepcopy(owner)
            ),
            "before": {},
            "after": {},
            "error": {
                "code": "sync_pending",
                "reason_code": reason_code,
                "message": message,
                "retryable": False,
            },
        }
        return MutationOutcome(
            receipt=receipt,
            mutation_state=mutation_state,
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

    @staticmethod
    def _tag_names(final_state: dict[str, Any]) -> set[str] | None:
        names: set[str] = set()
        raw_tags = final_state.get("tags")
        if not isinstance(raw_tags, list):
            return None
        for value in raw_tags:
            if not isinstance(value, dict):
                return None
            if "label" in value and not isinstance(value.get("label"), dict):
                return None
            raw = value.get("label") if isinstance(value.get("label"), dict) else value
            found_name = False
            for field in ("canonical_name", "name", "ZCANONICALNAME", "ZNAME"):
                candidate = raw.get(field)
                if isinstance(candidate, str) and candidate.strip():
                    canonical = NativeBackend._canonical_tag(candidate)
                    if canonical is not None:
                        names.add(canonical)
                        found_name = True
            if not found_name:
                return None
        return names

    @staticmethod
    def _canonical_tag(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        canonical = value.strip()
        while canonical.startswith("#"):
            canonical = canonical[1:].strip()
        return canonical.casefold() if canonical else None

    @staticmethod
    def _canonical_identifier(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        candidate = value.strip()
        if not candidate:
            return None
        if candidate.startswith("x-apple-reminder://"):
            candidate = candidate.removeprefix("x-apple-reminder://")
        try:
            return str(uuid.UUID(candidate)).upper()
        except ValueError:
            # Test doubles and future native identifiers may not be UUIDs. Keep
            # their exact spelling instead of inventing case-insensitive
            # semantics that the adapter itself does not promise.
            return candidate

    @staticmethod
    def _image_content_identity(
        value: Any,
        *,
        require_uti: bool,
    ) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        digest = value.get("sha512")
        file_size = value.get("file_size")
        width = value.get("width")
        height = value.get("height")
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[A-Fa-f0-9]{128}", digest) is None
            or not isinstance(file_size, int)
            or isinstance(file_size, bool)
            or file_size < 0
            or not isinstance(width, int)
            or isinstance(width, bool)
            or width <= 0
            or not isinstance(height, int)
            or isinstance(height, bool)
            or height <= 0
        ):
            return None
        identity: dict[str, Any] = {
            "type": "image",
            "sha512": digest.casefold(),
            "file_size": file_size,
            "width": width,
            "height": height,
        }
        if require_uti:
            uti = value.get("uti")
            if uti not in {"public.jpeg", "public.png"}:
                return None
            identity["uti"] = uti
        return identity

    @staticmethod
    def _adapter_image_attachment(command: str, adapter_after: Any) -> dict[str, Any] | None:
        if not isinstance(adapter_after, dict):
            return None
        if command == "attach_image":
            candidate = adapter_after.get("attachment")
            return candidate if isinstance(candidate, dict) else None
        if command == "replace_image":
            replacement = adapter_after.get("new_attachment")
            if not isinstance(replacement, dict):
                return None
            candidate = replacement.get("attachment")
            return candidate if isinstance(candidate, dict) else None
        return None

    @staticmethod
    def _image_content_hash(value: Any) -> str | None:
        identity = NativeBackend._image_content_identity(value, require_uti=True)
        if identity is None:
            return None
        encoded = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _section_id(final_state: dict[str, Any]) -> str | None:
        section = final_state.get("section")
        if isinstance(section, dict):
            candidate = section.get("id") or section.get("ZCKIDENTIFIER")
            if isinstance(candidate, str) and candidate:
                return candidate
        candidate = final_state.get("section_id")
        return candidate if isinstance(candidate, str) and candidate else None

    @staticmethod
    def _attachment_rows(
        final_state: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], bool] | None:
        raw = final_state.get("attachments")
        if not isinstance(raw, list):
            return None
        rows: list[dict[str, Any]] = []
        complete = final_state.get("truncated") is False
        seen_ids: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                complete = False
                continue
            rows.append(copy.deepcopy(item))
            canonical_id = NativeBackend._canonical_identifier(item.get("id"))
            if canonical_id is None:
                complete = False
            elif canonical_id in seen_ids:
                complete = False
            else:
                seen_ids.add(canonical_id)
        return rows, complete

    @classmethod
    def _final_state_matches(
        cls,
        command: str,
        arguments: dict[str, Any],
        payload: dict[str, Any],
        final_state: dict[str, Any],
        *,
        adapter_after: Any = None,
    ) -> bool:
        if command == "move_to_section":
            expected_section = cls._canonical_identifier(arguments.get("section_id"))
            return (
                expected_section is not None
                and cls._canonical_identifier(cls._section_id(final_state))
                == expected_section
            )
        if command in {"add_tag", "remove_tag"}:
            requested = cls._canonical_tag(arguments.get("tag"))
            if requested is None:
                return False
            names = cls._tag_names(final_state)
            if names is None:
                return False
            present = requested in names
            return present if command == "add_tag" else not present

        inventory = cls._attachment_rows(final_state)
        if inventory is None:
            return False
        attachments, inventory_complete = inventory
        by_id: dict[str, dict[str, Any]] = {}
        duplicate_ids: set[str] = set()
        for item in attachments:
            canonical_id = cls._canonical_identifier(item.get("id"))
            if canonical_id is not None:
                if canonical_id in by_id:
                    duplicate_ids.add(canonical_id)
                by_id[canonical_id] = item
        target = payload.get("target")
        target = target if isinstance(target, dict) else {}
        if command == "delete_attachment":
            attachment_id = cls._canonical_identifier(arguments.get("attachment_id"))
            return (
                inventory_complete
                and attachment_id is not None
                and attachment_id not in by_id
            )

        expected_id = (
            target.get("new_attachment_id")
            if command in {"replace_image", "replace_url"}
            else target.get("attachment_id")
        )
        expected_id = cls._canonical_identifier(expected_id)
        if expected_id is None:
            return False
        if expected_id in duplicate_ids:
            return False
        attachment = by_id.get(expected_id)
        if attachment is None:
            return False
        if command in {"attach_image", "replace_image"}:
            expected_attachment = cls._adapter_image_attachment(command, adapter_after)
            expected_identity = cls._image_content_identity(
                expected_attachment,
                require_uti=True,
            )
            final_identity = cls._image_content_identity(
                attachment,
                require_uti=True,
            )
            expected_hash = cls._image_content_hash(expected_attachment)
            if expected_hash is None and payload.get("replayed") is True:
                verification = payload.get("verification")
                replay_hash = (
                    verification.get("final_attachment_content_hash")
                    if isinstance(verification, dict)
                    else None
                )
                if isinstance(replay_hash, str) and re.fullmatch(
                    r"[a-f0-9]{64}", replay_hash
                ):
                    expected_hash = replay_hash
            final_hash = cls._image_content_hash(attachment)
            sync = attachment.get("sync")
            kind_matches = (
                attachment.get("type") == "image"
                and isinstance(sync, dict)
                and sync.get("mobile_visible_likely") is True
                and expected_hash is not None
                and final_hash == expected_hash
                and (
                    expected_identity is None
                    or final_identity == expected_identity
                )
            )
        elif command in {"attach_url", "replace_url"}:
            kind_matches = (
                attachment.get("type") == "url"
                and attachment.get("url") == arguments.get("url")
            )
        else:
            return False
        if not kind_matches:
            return False
        if command in {"replace_image", "replace_url"}:
            old_id = cls._canonical_identifier(arguments.get("attachment_id"))
            return inventory_complete and old_id is not None and old_id not in by_id
        return True

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
        transport = self._adapter_call(
            self._build_adapter_argv(tool_name, routed_arguments)
        )
        payload = transport.payload
        is_error = transport.is_error
        adapter_after = copy.deepcopy(payload.get("after"))
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

        mutation_state = validated_receipt_mutation_state(payload)
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
                        "truncated": final_state.get("truncated"),
                    }
            except Exception:
                return self._pending_outcome(
                    guard,
                    command,
                    reason_code="native_final_read_failed",
                    message=(
                        "The native result could not be bound to a final state; "
                        "read the exact Reminder before retrying."
                    ),
                    mutation_state=mutation_state_after_unverified_projection(
                        mutation_state
                    ),
                )
            if not self._final_state_matches(
                command,
                arguments,
                payload,
                final_state,
                adapter_after=adapter_after,
            ):
                return self._pending_outcome(
                    guard,
                    command,
                    reason_code="native_final_state_mismatch",
                    message=(
                        "The native final read did not match the requested action; "
                        "read the exact Reminder before another mutation."
                    ),
                    mutation_state=mutation_state_after_unverified_projection(
                        mutation_state
                    ),
                )
            if command in {"attach_image", "replace_image"}:
                verification = payload.setdefault("verification", {})
                verification["final_attachment_content_matched"] = True
                verification["mobile_visible_likely"] = True

        return MutationOutcome(receipt=payload, mutation_state=mutation_state)

    @staticmethod
    def _exact_image_attachment(
        payload: dict[str, Any],
        attachment_id: str,
    ) -> dict[str, Any]:
        attachments = payload.get("attachments")
        if not isinstance(attachments, list) or payload.get("truncated") is not False:
            raise RuntimeError("The exact source attachment set was not bounded")
        canonical_id = NativeBackend._canonical_identifier(attachment_id)
        matches = [
            item
            for item in attachments
            if isinstance(item, dict)
            and canonical_id is not None
            and NativeBackend._canonical_identifier(item.get("id")) == canonical_id
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
        identity = NativeBackend._image_content_identity(value, require_uti=False)
        return identity or {}

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
        transport = self._adapter_call(
            self._build_adapter_argv("copy_image_attachment", routed_arguments)
        )
        payload = transport.payload
        is_error = transport.is_error
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
        mutation_state = validated_receipt_mutation_state(payload)
        if status not in {"verified", "unchanged"}:
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
                mutation_state=mutation_state_after_unverified_projection(mutation_state),
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
            source_content = self._copy_content_identity(source_attachment)
            destination_content = self._copy_content_identity(destination_attachment)
            if (
                not source_content
                or not destination_content
                or destination_content != source_content
            ):
                raise RuntimeError("The destination image content differs from the source")
            destination_sync = destination_attachment.get("sync")
            if (
                not isinstance(destination_sync, dict)
                or destination_sync.get("mobile_visible_likely") is not True
            ):
                raise RuntimeError("The destination image is not proven mobile-visible")
        except Exception:
            return self._pending_outcome(
                destination_guard,
                command,
                reason_code="copy_image_final_read_failed",
                message=(
                    "The destination may contain the copied image, but exact source and "
                    "destination read-back did not complete."
                ),
                mutation_state=mutation_state_after_unverified_projection(mutation_state),
            )

        payload["after"] = {
            "reminder": {"id": destination_guard.reminder_id},
            "attachments": destination_after.get("attachments", []),
            "truncated": destination_after.get("truncated"),
        }
        verification = payload.setdefault("verification", {})
        verification.update(
            {
                "state": "read_back",
                "write_performed": status == "verified",
                "final_read": True,
                "matched": True,
                "source_unchanged": True,
                "source_bytes_matched": True,
                "destination_attachment_active": True,
                "destination_content_matched": True,
                "destination_mobile_visible_likely": True,
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
            mutation_state=mutation_state,
        )
