#!/usr/bin/env python3
"""Central result contract for the public v2 Apple Reminders tools.

MCP discovery intentionally omits duplicated output schemas.  This module is
the single narrow seam that every structured public result crosses before it
is returned to a caller.  It validates public vocabulary and mutation safety,
then returns a plain deep copy so backend-owned objects cannot be mutated
after validation.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any, NoReturn


MAX_RESULT_BYTES = 1_048_576
MAX_STRING_LENGTH = 100_000
MAX_ARRAY_ITEMS = 200
MAX_OBJECT_ITEMS = 256
MAX_DEPTH = 16
MAX_NODES = 10_000
MAX_WARNING_ITEMS = 20
MAX_MESSAGE_LENGTH = 2_000


READ_TOOLS = frozenset(
    {
        "request_reminders_access",
        "list_reminder_lists",
        "fetch_reminders",
        "read_reminder",
        "inspect_reminder_native",
        "diagnose_reminders",
    }
)
MUTATION_TOOLS = frozenset(
    {
        "create_reminder",
        "change_reminder",
        "delete_reminder",
        "ensure_reminder_list",
        "create_reminder_section",
        "organize_reminder",
        "change_reminder_attachment",
    }
)
PUBLIC_TOOLS = READ_TOOLS | MUTATION_TOOLS

OPERATION_FAMILIES = {
    "request_reminders_access": frozenset({"request_reminders_access"}),
    "list_reminder_lists": frozenset({"list_reminder_lists"}),
    "fetch_reminders": frozenset({"fetch_reminders"}),
    "read_reminder": frozenset({"read_reminder"}),
    "create_reminder": frozenset({"create_reminder"}),
    "change_reminder": frozenset(
        {
            "change_reminder.patch",
            "change_reminder.set_completion",
            "change_reminder.move_to_list",
        }
    ),
    "delete_reminder": frozenset({"delete_reminder"}),
    "inspect_reminder_native": frozenset({"inspect_reminder_native"}),
    "ensure_reminder_list": frozenset({"ensure_reminder_list"}),
    "create_reminder_section": frozenset({"create_reminder_section"}),
    "organize_reminder": frozenset(
        {
            "organize_reminder.move_to_section",
            "organize_reminder.add_tag",
            "organize_reminder.remove_tag",
        }
    ),
    "change_reminder_attachment": frozenset(
        {
            "change_reminder_attachment.attach_image",
            "change_reminder_attachment.attach_url",
            "change_reminder_attachment.replace_image",
            "change_reminder_attachment.replace_url",
            "change_reminder_attachment.delete",
        }
    ),
    "diagnose_reminders": frozenset({"diagnose_reminders"}),
}

SUCCESS_STATUSES = frozenset(
    {"unchanged", "verified", "committed_verification_pending", "partial_success"}
)
FAILURE_STATUSES = frozenset(
    {"failed_no_mutation", "failed_manual_repair_required"}
)
MUTATION_STATUSES = SUCCESS_STATUSES | FAILURE_STATUSES
STATUS_MUTATION_STATES = {
    "unchanged": frozenset({"not_mutated"}),
    "verified": frozenset({"committed"}),
    "committed_verification_pending": frozenset({"committed", "unknown"}),
    "partial_success": frozenset({"committed", "unknown"}),
    "failed_no_mutation": frozenset({"not_mutated"}),
    "failed_manual_repair_required": frozenset({"committed", "unknown"}),
}

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
REFERENCE_PATTERN = re.compile(r"^rev1\.[A-Za-z0-9_-]{32,4091}$")
CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
HEX_64_PATTERN = re.compile(r"^[0-9a-f]{64}$")

READ_BASE_FIELDS = frozenset({"schema_version", "ok", "status", "operation"})
READ_SUCCESS_FIELDS = READ_BASE_FIELDS | {"data", "warnings"}
READ_FAILURE_FIELDS = READ_BASE_FIELDS | {"error", "next_action", "warnings"}
MUTATION_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "ok",
        "status",
        "operation",
        "operation_id",
        "backend",
        "target",
        "before",
        "after",
        "verification",
        "recovery",
    }
)
MUTATION_OPTIONAL_FIELDS = frozenset(
    {
        "warnings",
        "error",
        "next_action",
        "idempotency_key_hash",
        "replayed",
    }
)
REFERENCE_MUTATION_TOOLS = frozenset(
    {
        "create_reminder",
        "change_reminder",
        "organize_reminder",
        "change_reminder_attachment",
    }
)
REFERENCE_CONTENT_FIELDS = frozenset(
    {"title", "notes", "url", "message", "name", "summary", "filename"}
)
EXACT_FORBIDDEN_FIELDS = frozenset(
    {
        "calendar_id",
        "calendar_ids",
        "calendar_title",
        "reminder_calendar_count",
        "db",
        "database",
        "db_path",
        "database_path",
        "sqlite_path",
        "sqlite_file",
        "store_path",
        "container_path",
        "file_path",
        "image_path",
        "source_path",
        "absolute_path",
        "path",
        "pk",
        "primary_key",
        "row_pk",
        "rowid",
        "row_id",
        "z_pk",
        "z_ent",
        "z_opt",
    }
)


class PublicResultContractError(ValueError):
    """A public result is unsafe or does not satisfy its v2 contract."""

    def __init__(
        self,
        code: str,
        path: str,
        message: str,
        *,
        tool_name: str,
        mutation_state: str | None,
    ) -> None:
        super().__init__(f"{path}: {message}")
        self.code = code
        self.path = path
        self.tool_name = tool_name
        self.mutation_state = mutation_state

    @property
    def may_have_mutated(self) -> bool:
        """Whether a caller must preserve an unknown/committed write outcome."""

        return self.mutation_state in {"committed", "unknown"}


def _fail(
    code: str,
    path: str,
    message: str,
    *,
    tool_name: str,
    mutation_state: str | None,
) -> NoReturn:
    raise PublicResultContractError(
        code,
        path,
        message,
        tool_name=tool_name,
        mutation_state=mutation_state,
    )


def _is_forbidden_field(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    if normalized in EXACT_FORBIDDEN_FIELDS or normalized.startswith("calendar_"):
        return True
    if normalized.endswith("_path") or normalized.startswith("path_"):
        return True
    if normalized.endswith("_pk") or normalized.startswith("pk_"):
        return True
    if normalized.endswith("_db") or normalized.startswith("db_"):
        return True
    if normalized.endswith("_database") or normalized.startswith("database_"):
        return True
    return False


def _plain_copy(
    value: Any,
    *,
    path: str,
    depth: int,
    active: set[int],
    nodes: list[int],
    tool_name: str,
    mutation_state: str | None,
) -> Any:
    nodes[0] += 1
    if nodes[0] > MAX_NODES:
        _fail(
            "result_too_large",
            path,
            f"result exceeds {MAX_NODES} structured values",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if depth > MAX_DEPTH:
        _fail(
            "result_too_deep",
            path,
            f"result exceeds depth {MAX_DEPTH}",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if value is None or isinstance(value, (bool, int)):
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**63 - 1:
            _fail(
                "integer_out_of_range",
                path,
                "integer is outside the signed 64-bit public range",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(
                "invalid_number",
                path,
                "non-finite numbers are not valid public JSON",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        return value
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            _fail(
                "string_too_long",
                path,
                f"string exceeds {MAX_STRING_LENGTH} characters",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            _fail(
                "cyclic_result",
                path,
                "cyclic objects are not valid public JSON",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        if len(value) > MAX_OBJECT_ITEMS:
            _fail(
                "object_too_large",
                path,
                f"object exceeds {MAX_OBJECT_ITEMS} fields",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        active.add(identity)
        result: dict[str, Any] = {}
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    _fail(
                        "invalid_key",
                        path,
                        "public object keys must be strings",
                        tool_name=tool_name,
                        mutation_state=mutation_state,
                    )
                if len(key) > 128:
                    _fail(
                        "key_too_long",
                        path,
                        "public object key exceeds 128 characters",
                        tool_name=tool_name,
                        mutation_state=mutation_state,
                    )
                if _is_forbidden_field(key):
                    _fail(
                        "forbidden_internal_field",
                        f"{path}.{key}",
                        "raw database, private-key, path, or EventKit calendar fields "
                        "must not cross the public result seam",
                        tool_name=tool_name,
                        mutation_state=mutation_state,
                    )
                result[key] = _plain_copy(
                    item,
                    path=f"{path}.{key}",
                    depth=depth + 1,
                    active=active,
                    nodes=nodes,
                    tool_name=tool_name,
                    mutation_state=mutation_state,
                )
        finally:
            active.remove(identity)
        return result
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            _fail(
                "cyclic_result",
                path,
                "cyclic arrays are not valid public JSON",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        if len(value) > MAX_ARRAY_ITEMS:
            _fail(
                "array_too_large",
                path,
                f"array exceeds {MAX_ARRAY_ITEMS} items",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        active.add(identity)
        try:
            return [
                _plain_copy(
                    item,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    active=active,
                    nodes=nodes,
                    tool_name=tool_name,
                    mutation_state=mutation_state,
                )
                for index, item in enumerate(value)
            ]
        finally:
            active.remove(identity)
    _fail(
        "invalid_json_type",
        path,
        f"{type(value).__name__} is not a public JSON value",
        tool_name=tool_name,
        mutation_state=mutation_state,
    )


def _closed_fields(
    value: Mapping[str, Any],
    *,
    required: frozenset[str] | set[str],
    allowed: frozenset[str] | set[str],
    path: str,
    tool_name: str,
    mutation_state: str | None,
    missing_code: str = "missing_envelope_field",
) -> None:
    missing = sorted(set(required) - set(value))
    if missing:
        _fail(
            missing_code,
            path,
            f"missing required fields: {', '.join(missing)}",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        _fail(
            "unknown_envelope_field",
            path,
            f"unsupported public fields: {', '.join(unknown)}",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )


def _bounded_text(
    value: Any,
    *,
    path: str,
    maximum: int,
    tool_name: str,
    mutation_state: str | None,
    code: str,
) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail(
            code,
            path,
            f"must be a non-empty string no longer than {maximum} characters",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    return value


def _validate_error(
    value: Any,
    *,
    tool_name: str,
    mutation_state: str | None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(
            "invalid_error",
            "$.error",
            "error must be a closed object",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    _closed_fields(
        value,
        required={"code", "reason_code", "message", "retryable"},
        allowed={"code", "reason_code", "message", "retryable"},
        path="$.error",
        tool_name=tool_name,
        mutation_state=mutation_state,
        missing_code="invalid_error",
    )
    code = value.get("code")
    if code not in PUBLIC_ERROR_CODES:
        _fail(
            "invalid_error",
            "$.error.code",
            "error code is not a stable public v2 code",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    reason = value.get("reason_code")
    if not isinstance(reason, str) or not CODE_PATTERN.fullmatch(reason):
        _fail(
            "invalid_error",
            "$.error.reason_code",
            "reason_code must be bounded lower_snake_case",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    _bounded_text(
        value.get("message"),
        path="$.error.message",
        maximum=MAX_MESSAGE_LENGTH,
        tool_name=tool_name,
        mutation_state=mutation_state,
        code="invalid_error",
    )
    if not isinstance(value.get("retryable"), bool):
        _fail(
            "invalid_error",
            "$.error.retryable",
            "retryable must be boolean",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if (
        code == "sync_pending"
        and tool_name in MUTATION_TOOLS
        and value.get("retryable") is not False
    ):
        _fail(
            "unsafe_retry",
            "$.error.retryable",
            "a pending mutation must not authorize retrying the original write",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    return dict(value)


def _validate_warnings(
    value: Any,
    *,
    required: bool,
    tool_name: str,
    mutation_state: str | None,
) -> None:
    if value is None and not required:
        return
    if not isinstance(value, list) or (required and not value):
        _fail(
            "incomplete_receipt" if required else "invalid_warnings",
            "$.warnings",
            "warnings must be a non-empty list for this status",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if len(value) > MAX_WARNING_ITEMS:
        _fail(
            "warnings_too_large",
            "$.warnings",
            f"warnings exceed {MAX_WARNING_ITEMS} items",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    for index, warning in enumerate(value):
        path = f"$.warnings[{index}]"
        if not isinstance(warning, Mapping):
            _fail(
                "invalid_warnings",
                path,
                "warning must be an object",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        _closed_fields(
            warning,
            required={"code", "message"},
            allowed={"code", "message"},
            path=path,
            tool_name=tool_name,
            mutation_state=mutation_state,
            missing_code="invalid_warnings",
        )
        code = warning.get("code")
        if not isinstance(code, str) or not CODE_PATTERN.fullmatch(code):
            _fail(
                "invalid_warnings",
                f"{path}.code",
                "warning code must be bounded lower_snake_case",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        _bounded_text(
            warning.get("message"),
            path=f"{path}.message",
            maximum=MAX_MESSAGE_LENGTH,
            tool_name=tool_name,
            mutation_state=mutation_state,
            code="invalid_warnings",
        )


def _validate_next_action(
    value: Any,
    *,
    error_code: str,
    tool_name: str,
    mutation_state: str | None,
) -> None:
    if value is None:
        if (
            error_code == "permission_denied"
            and tool_name != "request_reminders_access"
        ):
            _fail(
                "incomplete_failure",
                "$.next_action",
                "permission failure must provide the access next action",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        if error_code in {"concurrent_modification", "sync_pending"} and (
            tool_name in MUTATION_TOOLS
        ):
            _fail(
                "incomplete_failure",
                "$.next_action",
                "a recoverable mutation result must provide its exact fresh-read action",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        return
    if not isinstance(value, Mapping):
        _fail(
            "invalid_next_action",
            "$.next_action",
            "next_action must be an object",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if (
        tool_name == "request_reminders_access"
        and value.get("tool") == "request_reminders_access"
    ):
        _fail(
            "invalid_next_action",
            "$.next_action",
            "the access tool must not direct callers back to itself",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    _closed_fields(
        value,
        required={"kind", "tool", "retry_original_once", "message"},
        allowed={"kind", "tool", "retry_original_once", "message"},
        path="$.next_action",
        tool_name=tool_name,
        mutation_state=mutation_state,
        missing_code="invalid_next_action",
    )
    sync_pending_tool = {
        "create_reminder": "fetch_reminders",
        "ensure_reminder_list": "list_reminder_lists",
        "create_reminder_section": "inspect_reminder_native",
    }.get(tool_name, "read_reminder")
    expected = {
        "permission_denied": ("request_access", "request_reminders_access"),
        "concurrent_modification": ("fresh_read", sync_pending_tool),
        "sync_pending": ("fresh_read", sync_pending_tool),
    }.get(error_code)
    kind = value.get("kind")
    next_tool = value.get("tool")
    if expected is not None and (kind, next_tool) != expected:
        _fail(
            "invalid_next_action",
            "$.next_action",
            "next action does not match the public error code",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if not isinstance(kind, str) or kind not in {"request_access", "fresh_read", "diagnose"}:
        _fail(
            "invalid_next_action",
            "$.next_action.kind",
            "next action kind is unsupported",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if not isinstance(next_tool, str) or next_tool not in PUBLIC_TOOLS:
        _fail(
            "invalid_next_action",
            "$.next_action.tool",
            "next action tool is not public",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if not isinstance(value.get("retry_original_once"), bool):
        _fail(
            "invalid_next_action",
            "$.next_action.retry_original_once",
            "retry_original_once must be boolean",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if kind == "fresh_read" and value.get("retry_original_once") is not False:
        _fail(
            "unsafe_retry",
            "$.next_action.retry_original_once",
            "a fresh-read recovery must not authorize retrying the original mutation",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    _bounded_text(
        value.get("message"),
        path="$.next_action.message",
        maximum=MAX_MESSAGE_LENGTH,
        tool_name=tool_name,
        mutation_state=mutation_state,
        code="invalid_next_action",
    )


def _reference_locations(value: Any, path: str = "$", field: str | None = None) -> list[str]:
    locations: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            locations.extend(_reference_locations(item, f"{path}.{key}", key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            locations.extend(_reference_locations(item, f"{path}[{index}]", field))
    elif (
        isinstance(value, str)
        and field not in REFERENCE_CONTENT_FIELDS
        and REFERENCE_PATTERN.fullmatch(value)
    ):
        locations.append(path)
    return locations


def _validate_direct_reference(
    value: Any,
    *,
    path: str,
    tool_name: str,
    mutation_state: str | None,
) -> None:
    if not isinstance(value, str) or not REFERENCE_PATTERN.fullmatch(value):
        _fail(
            "missing_fresh_reference",
            path,
            "a canonical final read must issue one fresh opaque rev1 reference",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )


def _validate_access_request_data(
    value: Any,
    *,
    tool_name: str,
    mutation_state: str | None,
) -> None:
    path = "$.data"
    if not isinstance(value, Mapping):
        _fail(
            "invalid_read_envelope",
            path,
            "access-request data must be an object",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    _closed_fields(
        value,
        required={
            "authorization_before",
            "authorization",
            "request_attempted",
            "prompt_expected",
            "prompt_observed",
            "prompted_explicitly",
        },
        allowed={
            "authorization_before",
            "authorization",
            "request_attempted",
            "prompt_expected",
            "prompt_observed",
            "prompted_explicitly",
        },
        path=path,
        tool_name=tool_name,
        mutation_state=mutation_state,
        missing_code="invalid_read_envelope",
    )
    authorization_states = {
        "not_determined",
        "restricted",
        "denied",
        "full_access",
        "write_only",
        "unknown",
    }
    authorization_before = value.get("authorization_before")
    authorization = value.get("authorization")
    prompt_expected = value.get("prompt_expected")
    if authorization_before not in authorization_states:
        _fail(
            "invalid_read_envelope",
            f"{path}.authorization_before",
            "authorization_before is unsupported",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if authorization not in authorization_states:
        _fail(
            "invalid_read_envelope",
            f"{path}.authorization",
            "authorization is unsupported",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if value.get("request_attempted") is not True:
        _fail(
            "invalid_read_envelope",
            f"{path}.request_attempted",
            "request_attempted must be true",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if value.get("prompt_observed") is not None:
        _fail(
            "invalid_read_envelope",
            f"{path}.prompt_observed",
            "prompt_observed must be null because the process cannot observe system UI",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if (
        not isinstance(prompt_expected, bool)
        or prompt_expected != (authorization_before == "not_determined")
    ):
        _fail(
            "invalid_read_envelope",
            f"{path}.prompt_expected",
            "prompt_expected must match the pre-request authorization state",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if value.get("prompted_explicitly") is not True:
        _fail(
            "invalid_read_envelope",
            f"{path}.prompted_explicitly",
            "the deprecated compatibility flag must be true",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )


def _validate_read_result(
    tool_name: str,
    result: dict[str, Any],
    mutation_state: str | None,
) -> None:
    if mutation_state not in {None, "not_mutated"}:
        _fail(
            "mutation_state_mismatch",
            "$.status",
            "a read result cannot claim a committed or unknown mutation",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    status = result.get("status")
    ok = result.get("ok")
    if status == "verified":
        if ok is not True:
            _fail(
                "status_ok_mismatch",
                "$.ok",
                "verified read status requires ok=true",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        _closed_fields(
            result,
            required=READ_BASE_FIELDS | {"data"},
            allowed=READ_SUCCESS_FIELDS,
            path="$",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
        if not isinstance(result.get("data"), Mapping):
            _fail(
                "invalid_read_envelope",
                "$.data",
                "successful read data must be an object",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        if tool_name == "request_reminders_access":
            _validate_access_request_data(
                result.get("data"),
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
            if result["data"].get("authorization") != "full_access":
                _fail(
                    "invalid_read_envelope",
                    "$.data.authorization",
                    "verified access requires full_access after the request",
                    tool_name=tool_name,
                    mutation_state=mutation_state,
                )
        _validate_warnings(
            result.get("warnings"),
            required=False,
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
        expected_reference_path: str | None = None
        if tool_name == "read_reminder":
            reminder = result["data"].get("reminder")
            if not isinstance(reminder, Mapping):
                _fail(
                    "invalid_read_envelope",
                    "$.data.reminder",
                    "exact read must return one Reminder object",
                    tool_name=tool_name,
                    mutation_state=mutation_state,
                )
            _validate_direct_reference(
                reminder.get("reference"),
                path="$.data.reminder.reference",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
            expected_reference_path = "$.data.reminder.reference"
        elif tool_name == "inspect_reminder_native" and result["data"].get("kind") == "reminder":
            _validate_direct_reference(
                result["data"].get("reference"),
                path="$.data.reference",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
            expected_reference_path = "$.data.reference"
        locations = _reference_locations(result)
        if expected_reference_path is None and locations:
            _fail(
                "unsafe_reference",
                locations[0],
                "this read family must not issue writable references",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        if expected_reference_path is not None and locations != [expected_reference_path]:
            _fail(
                "unsafe_reference",
                "$",
                "exact read must expose exactly one public reference",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        return

    if status != "failed_no_mutation":
        _fail(
            "invalid_read_status",
            "$.status",
            "read result status must be verified or failed_no_mutation",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if ok is not False:
        _fail(
            "status_ok_mismatch",
            "$.ok",
            "failed read status requires ok=false",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    _closed_fields(
        result,
        required=READ_BASE_FIELDS | {"error"},
        allowed=(
            READ_FAILURE_FIELDS | {"data"}
            if tool_name == "request_reminders_access"
            else READ_FAILURE_FIELDS
        ),
        path="$",
        tool_name=tool_name,
        mutation_state=mutation_state,
    )
    error = _validate_error(
        result.get("error"), tool_name=tool_name, mutation_state=mutation_state
    )
    if tool_name == "request_reminders_access":
        if error["code"] == "permission_denied" and "data" not in result:
            _fail(
                "incomplete_failure",
                "$.data",
                "permission denial must preserve the access-request receipt",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        if "data" in result:
            _validate_access_request_data(
                result.get("data"),
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        if error["code"] == "permission_denied" and result["data"].get(
            "authorization"
        ) not in {"restricted", "denied", "write_only"}:
            _fail(
                "invalid_read_envelope",
                "$.data.authorization",
                "permission denial requires a final non-full permission state",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
    _validate_warnings(
        result.get("warnings"),
        required=False,
        tool_name=tool_name,
        mutation_state=mutation_state,
    )
    _validate_next_action(
        result.get("next_action"),
        error_code=error["code"],
        tool_name=tool_name,
        mutation_state=mutation_state,
    )
    locations = _reference_locations(result)
    if locations:
        _fail(
            "unsafe_reference",
            locations[0],
            "failed reads must not issue writable references",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )


def _allowed_backends(operation: str) -> frozenset[str]:
    if operation in {"create_reminder", "change_reminder.patch"}:
        return frozenset({"eventkit_public_sdk", "eventkit_plus_native_url"})
    if operation in {
        "change_reminder.set_completion",
        "change_reminder.move_to_list",
        "delete_reminder",
        "ensure_reminder_list",
    }:
        return frozenset({"eventkit_public_sdk"})
    if operation == "create_reminder_section" or operation.startswith(
        ("organize_reminder.", "change_reminder_attachment.")
    ):
        return frozenset({"native_extension"})
    return frozenset()


def _validate_mutation_result(
    tool_name: str,
    result: dict[str, Any],
    mutation_state: str | None,
) -> None:
    _closed_fields(
        result,
        required=MUTATION_REQUIRED_FIELDS,
        allowed=MUTATION_REQUIRED_FIELDS | MUTATION_OPTIONAL_FIELDS,
        path="$",
        tool_name=tool_name,
        mutation_state=mutation_state,
        missing_code="missing_receipt_field",
    )
    status = result.get("status")
    if status not in MUTATION_STATUSES:
        _fail(
            "invalid_mutation_status",
            "$.status",
            "mutation receipt status is unsupported",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    expected_ok = status in SUCCESS_STATUSES
    if result.get("ok") is not expected_ok:
        _fail(
            "status_ok_mismatch",
            "$.ok",
            "mutation receipt status and ok flag disagree",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if mutation_state is not None and mutation_state not in STATUS_MUTATION_STATES[status]:
        _fail(
            "mutation_state_mismatch",
            "$.status",
            "receipt status disagrees with the independent mutation outcome",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    operation = result["operation"]
    backend = result.get("backend")
    if not isinstance(backend, str) or backend not in _allowed_backends(operation):
        _fail(
            "backend_mismatch",
            "$.backend",
            "backend is not public or does not match the exact operation",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    _bounded_text(
        result.get("operation_id"),
        path="$.operation_id",
        maximum=128,
        tool_name=tool_name,
        mutation_state=mutation_state,
        code="invalid_receipt_field",
    )
    if not isinstance(result.get("target"), Mapping):
        _fail(
            "invalid_receipt_field",
            "$.target",
            "target must be an object",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    for name in ("before", "after"):
        if result.get(name) is not None and not isinstance(result.get(name), Mapping):
            _fail(
                "invalid_receipt_field",
                f"$.{name}",
                f"{name} must be an object or null",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
    verification = result.get("verification")
    if not isinstance(verification, Mapping):
        _fail(
            "invalid_verification",
            "$.verification",
            "verification must be an object",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    for field in ("state", "write_performed", "final_read"):
        if field not in verification:
            _fail(
                "invalid_verification",
                "$.verification",
                f"verification is missing {field}",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
    if not isinstance(verification.get("state"), str) or len(verification["state"]) > 128:
        _fail(
            "invalid_verification",
            "$.verification.state",
            "verification state must be a bounded string",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if verification.get("write_performed") not in {True, False, None}:
        _fail(
            "invalid_verification",
            "$.verification.write_performed",
            "write_performed must be true, false, or null",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if not isinstance(verification.get("final_read"), bool):
        _fail(
            "invalid_verification",
            "$.verification.final_read",
            "final_read must be boolean",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    write_performed = verification.get("write_performed")
    if mutation_state == "committed" and write_performed is not True:
        _fail(
            "mutation_state_mismatch",
            "$.verification.write_performed",
            "a committed mutation outcome requires write_performed=true",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if mutation_state == "unknown" and write_performed is False:
        _fail(
            "mutation_state_mismatch",
            "$.verification.write_performed",
            "an unknown outcome cannot claim write_performed=false",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    recovery = result.get("recovery")
    if not isinstance(recovery, Mapping):
        _fail(
            "invalid_recovery",
            "$.recovery",
            "recovery must be an object",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    _bounded_text(
        recovery.get("semantics"),
        path="$.recovery.semantics",
        maximum=128,
        tool_name=tool_name,
        mutation_state=mutation_state,
        code="invalid_recovery",
    )
    if not isinstance(recovery.get("automatic_retry_safe"), bool):
        _fail(
            "invalid_recovery",
            "$.recovery.automatic_retry_safe",
            "automatic_retry_safe must be boolean",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )

    requires_problem = status in {
        "committed_verification_pending",
        "partial_success",
        "failed_no_mutation",
        "failed_manual_repair_required",
    }
    requires_warning = status in {
        "committed_verification_pending",
        "partial_success",
        "failed_manual_repair_required",
    }
    if requires_problem and "error" not in result:
        _fail(
            "incomplete_receipt",
            "$.error",
            "this receipt status requires a structured error",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if not requires_problem and "error" in result:
        _fail(
            "invalid_receipt_field",
            "$.error",
            "verified and unchanged receipts must not carry an error",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    error = (
        _validate_error(
            result.get("error"), tool_name=tool_name, mutation_state=mutation_state
        )
        if "error" in result
        else None
    )
    _validate_warnings(
        result.get("warnings"),
        required=requires_warning,
        tool_name=tool_name,
        mutation_state=mutation_state,
    )
    if error is not None:
        _validate_next_action(
            result.get("next_action"),
            error_code=error["code"],
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    elif "next_action" in result:
        _fail(
            "invalid_next_action",
            "$.next_action",
            "next_action requires a structured error",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )

    if status in {"verified", "unchanged"}:
        if (
            verification.get("state") != "read_back"
            or verification.get("final_read") is not True
            or verification.get("matched") is not True
        ):
            _fail(
                "unsafe_final_read",
                "$.verification",
                "verified or unchanged requires a matched canonical final exact read",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        expected_write = status == "verified"
        if verification.get("write_performed") is not expected_write:
            _fail(
                "invalid_verification",
                "$.verification.write_performed",
                "write_performed disagrees with verified/unchanged status",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
    elif status == "failed_no_mutation":
        if verification.get("write_performed") is not False:
            _fail(
                "false_no_mutation_claim",
                "$.verification.write_performed",
                "failed_no_mutation requires affirmative evidence that no write occurred",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
    elif status == "committed_verification_pending":
        if (
            verification.get("state") != "pending"
            or verification.get("final_read") is not False
            or verification.get("write_performed") is False
            or recovery.get("automatic_retry_safe") is not False
        ):
            _fail(
                "invalid_pending_receipt",
                "$.verification",
                "pending receipt must preserve a possible write and require a fresh read",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
    else:
        if verification.get("write_performed") is False:
            _fail(
                "invalid_possible_write_receipt",
                "$.verification.write_performed",
                "partial/manual receipt cannot claim that no write occurred",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        if recovery.get("automatic_retry_safe") is not False:
            _fail(
                "invalid_recovery",
                "$.recovery.automatic_retry_safe",
                "partial/manual receipts cannot authorize automatic retry",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )

    locations = _reference_locations(result)
    if status in {"verified", "unchanged"} and tool_name in REFERENCE_MUTATION_TOOLS:
        after = result.get("after")
        if not isinstance(after, Mapping):
            _fail(
                "missing_fresh_reference",
                "$.after",
                "final exact Reminder state is required",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
        _validate_direct_reference(
            after.get("reference"),
            path="$.after.reference",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
        if locations != ["$.after.reference"]:
            _fail(
                "unsafe_reference",
                "$",
                "receipt must expose exactly one fresh reference from the final read",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
    elif locations:
        _fail(
            "unsafe_reference",
            locations[0],
            "only a verified or unchanged exact Reminder result may expose rev1",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )

    if "idempotency_key_hash" in result:
        if not isinstance(result["idempotency_key_hash"], str) or not HEX_64_PATTERN.fullmatch(
            result["idempotency_key_hash"]
        ):
            _fail(
                "invalid_receipt_field",
                "$.idempotency_key_hash",
                "idempotency hash must be a lowercase SHA-256 digest",
                tool_name=tool_name,
                mutation_state=mutation_state,
            )
    if "replayed" in result and not isinstance(result["replayed"], bool):
        _fail(
            "invalid_receipt_field",
            "$.replayed",
            "replayed must be boolean",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )


def validate_public_result(
    tool_name: str,
    payload: Mapping[str, Any],
    mutation_state: str | None = None,
) -> dict[str, Any]:
    """Validate and deep-copy one public v2 structured result.

    ``mutation_state`` is the adapter's independent outcome classification.
    Callers should pass it for mutations whenever it is known.  Contract
    errors retain that state so a committed or unknown outcome is never
    rewritten into a false ``failed_no_mutation`` claim.
    """

    if not isinstance(tool_name, str) or tool_name not in PUBLIC_TOOLS:
        raise PublicResultContractError(
            "unknown_tool",
            "$.tool_name",
            "tool name is not a supported public v2 tool",
            tool_name=str(tool_name),
            mutation_state=mutation_state,
        )
    if mutation_state not in {None, "not_mutated", "committed", "unknown"}:
        _fail(
            "invalid_mutation_state",
            "$.mutation_state",
            "mutation_state must be not_mutated, committed, unknown, or omitted",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if not isinstance(payload, Mapping):
        _fail(
            "result_not_object",
            "$",
            "public result must be an object",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    result = _plain_copy(
        payload,
        path="$",
        depth=0,
        active=set(),
        nodes=[0],
        tool_name=tool_name,
        mutation_state=mutation_state,
    )
    try:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except UnicodeEncodeError:
        _fail(
            "invalid_string_encoding",
            "$",
            "result contains a string that is not valid UTF-8",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if len(encoded) > MAX_RESULT_BYTES:
        _fail(
            "result_too_large",
            "$",
            f"structured result exceeds {MAX_RESULT_BYTES} bytes",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if result.get("schema_version") != 2 or isinstance(
        result.get("schema_version"), bool
    ):
        _fail(
            "schema_version_mismatch",
            "$.schema_version",
            "public structured results require schema_version=2",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    operation = result.get("operation")
    if operation not in OPERATION_FAMILIES[tool_name]:
        _fail(
            "operation_mismatch",
            "$.operation",
            "operation does not match the exact public tool family",
            tool_name=tool_name,
            mutation_state=mutation_state,
        )
    if tool_name in READ_TOOLS:
        _validate_read_result(tool_name, result, mutation_state)
    else:
        _validate_mutation_result(tool_name, result, mutation_state)
    return result


__all__ = [
    "MAX_RESULT_BYTES",
    "MAX_ARRAY_ITEMS",
    "MAX_STRING_LENGTH",
    "MAX_WARNING_ITEMS",
    "PublicResultContractError",
    "validate_public_result",
]
