#!/usr/bin/env python3
"""Dependency-light stdio MCP shim for the bundled Reminders backends.

The public EventKit bridge and private-feature adapter own Reminders business
logic. This module owns the transport boundary: typed inputs, exact selectors,
bounded/redacted outputs, JSON-RPC framing, and subprocess isolation.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
import time
import urllib.parse
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
MCP_DIR = Path(__file__).resolve().parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from receipt_contract import (  # noqa: E402
    adapter_receipt_error,
)
from bounded_process import (  # noqa: E402
    ProcessDecodeError,
    ProcessError,
    ProcessLaunchError,
    ProcessOutputLimitError,
    ProcessTimeoutError,
    run as run_bounded_process,
)
from durable_idempotency import execute_idempotent  # noqa: E402
from eventkit_protocol import (  # noqa: E402
    mutation_outcome_unknown_response,
    validate_response as validate_eventkit_response,
)

if __package__:  # Package import in tests; script-local import in the launcher.
    from .v2_contract import (  # noqa: E402
        MUTATION_TOOLS as V2_MUTATION_TOOLS,
        PUBLIC_TOOLS as V2_PUBLIC_TOOLS,
        PublicResultContractError,
        validate_public_result,
    )
    from .v2_transport import DispatchCertainty, TransportResult  # noqa: E402
else:  # pragma: no cover - exercised by the stdio entry point
    from v2_contract import (  # noqa: E402
        MUTATION_TOOLS as V2_MUTATION_TOOLS,
        PUBLIC_TOOLS as V2_PUBLIC_TOOLS,
        PublicResultContractError,
        validate_public_result,
    )
    from v2_transport import DispatchCertainty, TransportResult  # noqa: E402


SERVER_NAME = "apple-reminders-local"
SERVER_TITLE = "Apple Reminders"
SERVER_VERSION = "0.5.2"
LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {
    LATEST_PROTOCOL_VERSION,
    "2025-06-18",
    "2024-11-05",
}

TOOLS_SCHEMA_PATH = PLUGIN_ROOT / "schemas" / "mcp-tools.json"
DEFAULT_ADAPTER_PATH = PLUGIN_ROOT / "scripts" / "reminders_adapter.py"
DEFAULT_EVENTKIT_BRIDGE_PATH = PLUGIN_ROOT / "scripts" / "eventkit_bridge.py"
DEFAULT_DOCTOR_PATH = PLUGIN_ROOT / "scripts" / "reminders_doctor.py"


@dataclass(frozen=True)
class BackendPaths:
    adapter: Path
    eventkit_bridge: Path
    doctor: Path


DEFAULT_BACKEND_PATHS = BackendPaths(
    adapter=DEFAULT_ADAPTER_PATH,
    eventkit_bridge=DEFAULT_EVENTKIT_BRIDGE_PATH,
    doctor=DEFAULT_DOCTOR_PATH,
)

ADAPTER_TIMEOUT_SECONDS = 45
EVENTKIT_BRIDGE_TIMEOUT_SECONDS = 80
MAX_ADAPTER_STDOUT_BYTES = 2_000_000
MAX_HOST_STDERR_BYTES = 256 * 1024
MAX_EVENTKIT_REQUEST_BYTES = 1_000_000
MAX_MCP_MESSAGE_BYTES = 2_000_000
MAX_CALLS_PER_MINUTE = 120
MIN_PYTHON_VERSION = (3, 11)

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
JSONRPC_SERVER_NOT_INITIALIZED = -32002


@dataclass(frozen=True)
class ToolRoute:
    command: str
    static: tuple[str, ...] = ()
    options: tuple[tuple[str, str], ...] = ()


ROUTES: dict[str, ToolRoute] = {
    "list_reminder_sections": ToolRoute(
        command="list_sections",
        options=(("list_id", "--list-id"), ("limit", "--limit")),
    ),
    "list_reminder_tags": ToolRoute(
        command="list_tags",
        options=(
            ("account_id", "--account-id"),
            ("query", "--query"),
            ("limit", "--limit"),
        ),
    ),
    "add_reminder_tag": ToolRoute(
        command="add_tag",
        options=(
            ("reminder_id", "--id"),
            ("tag", "--tag"),
            ("if_version", "--if-version"),
        ),
    ),
    "remove_reminder_tag": ToolRoute(
        command="remove_tag",
        options=(
            ("reminder_id", "--id"),
            ("tag", "--tag"),
            ("if_version", "--if-version"),
        ),
    ),
    "create_reminder_section": ToolRoute(
        command="create_section",
        options=(("list_id", "--list-id"), ("name", "--name")),
    ),
    "move_reminder_to_section": ToolRoute(
        command="move_to_section",
        options=(
            ("reminder_id", "--id"),
            ("section_id", "--section-id"),
            ("if_version", "--if-version"),
        ),
    ),
    "attach_image_to_reminder": ToolRoute(
        command="attach_image",
        static=("--backend", "reminderkit"),
        options=(
            ("reminder_id", "--id"),
            ("image_path", "--image"),
            ("if_version", "--if-version"),
            ("idempotency_key", "--idempotency-key"),
        ),
    ),
    "copy_image_attachment": ToolRoute(
        command="copy_image_attachment",
        options=(
            ("source_reminder_id", "--source-id"),
            ("reminder_id", "--id"),
            ("attachment_id", "--attachment-id"),
            ("if_source_version", "--if-source-version"),
            ("if_version", "--if-version"),
            ("idempotency_key", "--idempotency-key"),
        ),
    ),
    "attach_url_to_reminder": ToolRoute(
        command="attach_url",
        options=(
            ("reminder_id", "--id"),
            ("url", "--url"),
            ("if_version", "--if-version"),
        ),
    ),
    "list_reminder_attachments": ToolRoute(
        command="list_attachments",
        options=(
            ("reminder_id", "--id"),
            ("attachment_type", "--type"),
            ("limit", "--limit"),
        ),
    ),
    "delete_reminder_attachment": ToolRoute(
        command="delete_attachment",
        options=(
            ("reminder_id", "--id"),
            ("attachment_id", "--attachment-id"),
            ("if_version", "--if-version"),
        ),
    ),
    "replace_reminder_attachment": ToolRoute(
        command="replace_attachment",
        options=(
            ("reminder_id", "--id"),
            ("attachment_id", "--attachment-id"),
            ("image_path", "--image"),
            ("url", "--url"),
            ("if_version", "--if-version"),
            ("idempotency_key", "--idempotency-key"),
        ),
    ),
}


EVENTKIT_MUTATION_ROUTES: dict[str, str] = {
    "ensure_reminder_list": "ensure_reminder_list",
    "create_reminder": "create_reminder",
    "update_reminder": "update_reminder",
    "complete_reminder": "complete_reminder",
    "reopen_reminder": "reopen_reminder",
    "move_reminder_to_list": "move_reminder",
    "delete_reminder": "delete_reminder",
}


def load_tools(path: Path = TOOLS_SCHEMA_PATH) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != 2 or not isinstance(payload.get("tools"), list):
        raise RuntimeError("Unsupported MCP tool schema document")
    tools = payload["tools"]
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise RuntimeError("Every MCP tool must have a string name")
        name = tool["name"]
        if name in names:
            raise RuntimeError(f"Duplicate MCP tool name: {name}")
        if name not in V2_PUBLIC_TOOLS:
            raise RuntimeError(f"MCP tool has no local route: {name}")
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise RuntimeError(f"MCP tool input schema must be an object: {name}")
        if schema.get("additionalProperties") is not False:
            raise RuntimeError(f"MCP tool input schema must reject unknown fields: {name}")
        if "outputSchema" in tool:
            raise RuntimeError(f"Public output schemas must not bloat tool discovery: {name}")
        names.add(name)
    if names != set(V2_PUBLIC_TOOLS):
        raise RuntimeError(
            "Public v2 tool definitions do not match the "
            f"{len(V2_PUBLIC_TOOLS)}-tool runtime surface"
        )
    return tools


TOOLS = load_tools()
TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


class ToolInputError(ValueError):
    pass


def _is_expected_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return False


def _schema_branch_matches(
    name: str,
    value: Any,
    definition: dict[str, Any],
    root: dict[str, Any],
) -> bool:
    try:
        _validate_schema_value(name, value, definition, root)
    except ToolInputError:
        return False
    return True


def _validate_schema_value(
    name: str,
    value: Any,
    definition: dict[str, Any],
    root: dict[str, Any] | None = None,
) -> None:
    if root is None:
        root = definition
    reference = definition.get("$ref")
    if reference:
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            raise ToolInputError(f"{name} uses an unsupported schema reference")
        referenced = root.get("$defs", {}).get(reference.removeprefix("#/$defs/"))
        if not isinstance(referenced, dict):
            raise ToolInputError(f"{name} uses an unresolved schema reference")
        _validate_schema_value(name, value, referenced, root)
        return
    if "const" in definition and value != definition["const"]:
        raise ToolInputError(f"{name} must equal {definition['const']!r}")
    if "enum" in definition and value not in definition["enum"]:
        raise ToolInputError(f"{name} must be one of: {', '.join(map(str, definition['enum']))}")
    if "not" in definition and isinstance(definition["not"], dict):
        if _schema_branch_matches(name, value, definition["not"], root):
            raise ToolInputError(f"{name} contains a disallowed value")
    if "oneOf" in definition:
        matches = sum(
            _schema_branch_matches(name, value, branch, root)
            for branch in definition["oneOf"]
        )
        if matches != 1:
            raise ToolInputError(f"{name} must match exactly one allowed shape")
    if "anyOf" in definition:
        if not any(
            _schema_branch_matches(name, value, branch, root)
            for branch in definition["anyOf"]
        ):
            raise ToolInputError(f"{name} must match at least one required shape")

    expected = definition.get("type")
    if isinstance(expected, str):
        expected_types = [expected]
    elif isinstance(expected, list) and all(isinstance(item, str) for item in expected):
        expected_types = expected
    else:
        expected_types = []
    if expected_types and not any(_is_expected_type(value, item) for item in expected_types):
        raise ToolInputError(f"{name} must be of type {' or '.join(expected_types)}")
    if value is None:
        return

    if isinstance(value, str):
        if "\x00" in value:
            raise ToolInputError(f"{name} must not contain NUL characters")
        if len(value) < definition.get("minLength", 0):
            raise ToolInputError(f"{name} is shorter than the allowed minimum")
        if definition.get("minLength", 0) > 0 and not value.strip():
            raise ToolInputError(f"{name} must not be blank")
        if len(value) > definition.get("maxLength", sys.maxsize):
            raise ToolInputError(f"{name} exceeds the allowed maximum length")
        pattern = definition.get("pattern")
        if pattern and re.search(pattern, value) is None:
            raise ToolInputError(f"{name} does not match the required format")
        if definition.get("format"):
            _validate_format(name, value, definition["format"])

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < definition.get("minimum", value):
            raise ToolInputError(f"{name} is below the allowed minimum")
        if value > definition.get("maximum", value):
            raise ToolInputError(f"{name} exceeds the allowed maximum")
        if "exclusiveMinimum" in definition and value <= definition["exclusiveMinimum"]:
            raise ToolInputError(f"{name} must be greater than the allowed minimum")
        if "exclusiveMaximum" in definition and value >= definition["exclusiveMaximum"]:
            raise ToolInputError(f"{name} must be less than the allowed maximum")

    if isinstance(value, list):
        if len(value) < definition.get("minItems", 0):
            raise ToolInputError(f"{name} has fewer items than allowed")
        if len(value) > definition.get("maxItems", sys.maxsize):
            raise ToolInputError(f"{name} has more items than allowed")
        if definition.get("uniqueItems"):
            serialized = [json.dumps(item, sort_keys=True) for item in value]
            if len(serialized) != len(set(serialized)):
                raise ToolInputError(f"{name} must not contain duplicate items")
        item_definition = definition.get("items")
        if isinstance(item_definition, dict):
            for index, item in enumerate(value):
                _validate_schema_value(f"{name}[{index}]", item, item_definition, root)

    if isinstance(value, dict):
        required = definition.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise ToolInputError(f"{name} is missing required field(s): {', '.join(missing)}")
        if len(value) < definition.get("minProperties", 0):
            raise ToolInputError(f"{name} has fewer properties than allowed")
        properties = definition.get("properties", {})
        if definition.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ToolInputError(f"{name} contains unknown field(s): {', '.join(unknown)}")
        for key, item in value.items():
            child_definition = properties.get(key)
            if isinstance(child_definition, dict):
                _validate_schema_value(f"{name}.{key}", item, child_definition, root)


def _validate_format(name: str, value: str, format_name: str) -> None:
    if format_name == "date":
        try:
            parsed = dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ToolInputError(f"{name} must be a valid date in YYYY-MM-DD form") from exc
        if parsed.isoformat() != value:
            raise ToolInputError(f"{name} must use canonical YYYY-MM-DD form")
        return
    if format_name == "date-time":
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = dt.datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ToolInputError(f"{name} must be a valid RFC 3339 date-time") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ToolInputError(f"{name} must include an explicit UTC offset or Z")
        return
    if format_name == "uri":
        try:
            parsed = urllib.parse.urlparse(value)
        except ValueError as exc:
            raise ToolInputError(f"{name} must be a valid absolute URI") from exc
        if not parsed.scheme:
            raise ToolInputError(f"{name} must be an absolute URI with a scheme")
        if parsed.scheme in {"http", "https"} and not parsed.netloc:
            raise ToolInputError(f"{name} must include a host")


def validate_arguments(tool: dict[str, Any], arguments: Any) -> dict[str, Any]:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolInputError("arguments must be a JSON object")

    schema = tool["inputSchema"]
    _validate_schema_value("arguments", arguments, schema)

    tool_name = tool["name"]
    if tool_name == "create_reminder" and arguments.get("recurrence_rules") and "due" not in arguments:
        raise ToolInputError("a recurring reminder requires a due date")
    if tool_name == "change_reminder":
        action = arguments.get("action", {})
        patch = action.get("patch", {}) if action.get("kind") == "patch" else {}
        if patch.get("recurrence_rules") and "due" in patch and patch["due"] is None:
            raise ToolInputError("recurrence_rules cannot be set while clearing due")
    if tool_name == "fetch_reminders":
        status = arguments.get("status", "incomplete")
        has_list_bound = bool(arguments.get("list_ids"))
        has_due_bound = "due_start" in arguments and "due_end" in arguments
        has_completion_bound = (
            "completion_start" in arguments and "completion_end" in arguments
        )
        if status == "completed" and not has_completion_bound:
            raise ToolInputError(
                "completed fetch_reminders requires completion_start and completion_end"
            )
        if status == "incomplete" and not (has_list_bound or has_due_bound):
            raise ToolInputError(
                "incomplete fetch_reminders requires list_ids or due_start and due_end"
            )
        for start_name, end_name in (
            ("due_start", "due_end"),
            ("completion_start", "completion_end"),
        ):
            if start_name in arguments and end_name in arguments:
                start = dt.datetime.fromisoformat(arguments[start_name].replace("Z", "+00:00"))
                end = dt.datetime.fromisoformat(arguments[end_name].replace("Z", "+00:00"))
                if start >= end:
                    raise ToolInputError(f"{start_name} must be earlier than {end_name}")
                maximum_days = 366 if start_name == "due_start" else 90
                if end - start > dt.timedelta(days=maximum_days):
                    raise ToolInputError(
                        f"{start_name}/{end_name} cannot exceed {maximum_days} days"
                    )

    return dict(arguments)


def effective_arguments(tool: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    effective: dict[str, Any] = {}
    for name, definition in tool["inputSchema"].get("properties", {}).items():
        if "default" in definition:
            effective[name] = definition["default"]
    effective.update(arguments)
    return effective


def build_adapter_argv(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    route = ROUTES[tool_name]
    argv = [route.command, *route.static]
    for name, flag in route.options:
        if name in arguments:
            argv.extend([flag, str(arguments[name])])
    return argv


PRIVATE_RESULT_KEYS = {
    "__dispatch_phase",
    "db",
    "database",
    "database_path",
    "stores_dir",
    "files_dir",
    "main_db",
    "pk",
    "object_pk",
    "attachment_pk",
    "list_pk",
}


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    home = str(Path.home())

    def walk(value: Any, path: str) -> Any:
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for key, item in value.items():
                if key in PRIVATE_RESULT_KEYS or re.fullmatch(r"Z[A-Z0-9_]+", key):
                    continue
                child_path = f"{path}.{key}" if path else key
                cleaned[key] = walk(item, child_path)
            return cleaned
        if isinstance(value, list):
            return [
                walk(item, f"{path}[{index}]")
                for index, item in enumerate(value)
            ]
        if isinstance(value, str):
            return value.replace(home, "~") if home else value
        return value

    return walk(payload, "")


def invoke_adapter(
    argv: list[str],
    *,
    backend_paths: BackendPaths = DEFAULT_BACKEND_PATHS,
) -> TransportResult:
    path = backend_paths.adapter
    if not path.is_file():
        return TransportResult(
            payload={
                "ok": False,
                "error": {
                    "code": "adapter_unavailable",
                    "message": "The bundled Reminders adapter is unavailable.",
                },
            },
            is_error=True,
            dispatch_certainty=DispatchCertainty.PROVEN_NOT_STARTED,
        )
    try:
        completed = run_bounded_process(
            [sys.executable, str(path), *argv],
            cwd=PLUGIN_ROOT,
            timeout_s=ADAPTER_TIMEOUT_SECONDS,
            stdout_limit=MAX_ADAPTER_STDOUT_BYTES,
            stderr_limit=MAX_HOST_STDERR_BYTES,
            output="utf8",
        )
    except ProcessTimeoutError:
        return TransportResult(
            payload={
                "ok": False,
                "error": {
                    "code": "adapter_timeout",
                    "message": "The Reminders adapter timed out before returning a result.",
                },
            },
            is_error=True,
            dispatch_certainty=DispatchCertainty.MAY_HAVE_STARTED,
        )
    except ProcessOutputLimitError:
        return TransportResult(
            payload={
                "ok": False,
                "error": {
                    "code": "adapter_output_too_large",
                    "message": "The Reminders adapter result exceeded the MCP output bound.",
                },
            },
            is_error=True,
            dispatch_certainty=DispatchCertainty.MAY_HAVE_STARTED,
        )
    except ProcessDecodeError:
        return TransportResult(
            payload={
                "ok": False,
                "error": {
                    "code": "invalid_adapter_response",
                    "message": "The Reminders adapter returned an invalid JSON response.",
                },
            },
            is_error=True,
            dispatch_certainty=DispatchCertainty.MAY_HAVE_STARTED,
        )
    except ProcessLaunchError as exc:
        return TransportResult(
            payload={
                "ok": False,
                "error": {
                    "code": "adapter_process_failed",
                    "message": f"The Reminders adapter failed before returning ({type(exc).__name__}).",
                },
            },
            is_error=True,
            dispatch_certainty=DispatchCertainty.PROVEN_NOT_STARTED,
        )
    except ProcessError as exc:
        return TransportResult(
            payload={
                "ok": False,
                "error": {
                    "code": "adapter_process_failed",
                    "message": (
                        "The Reminders adapter failed after launch without "
                        f"returning a result ({type(exc).__name__})."
                    ),
                },
            },
            is_error=True,
            dispatch_certainty=DispatchCertainty.MAY_HAVE_STARTED,
        )

    stdout = completed.stdout.strip()
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return TransportResult(
            payload={
                "ok": False,
                "error": {
                    "code": "invalid_adapter_response",
                    "message": "The Reminders adapter returned an invalid JSON response.",
                    "exit_code": completed.returncode,
                },
            },
            is_error=True,
            dispatch_certainty=DispatchCertainty.MAY_HAVE_STARTED,
        )
    if not isinstance(payload, dict):
        payload = {
            "ok": completed.returncode == 0,
            "result": payload,
        }
    else:
        payload = dict(payload)
        # Dispatch provenance belongs to this launcher, never to child JSON.
        payload.pop("__dispatch_phase", None)
    is_error = payload.get("ok") is not True
    return TransportResult(
        payload=payload,
        is_error=is_error,
        dispatch_certainty=DispatchCertainty.MAY_HAVE_STARTED,
    )


def invoke_doctor(
    arguments: dict[str, Any],
    *,
    backend_paths: BackendPaths = DEFAULT_BACKEND_PATHS,
) -> tuple[dict[str, Any], bool]:
    path = backend_paths.doctor
    if not path.is_file():
        return (
            {
                "ok": False,
                "error": {
                    "code": "doctor_unavailable",
                    "message": "The bundled content-free Reminders doctor is unavailable.",
                },
            },
            True,
        )
    argv = [
        sys.executable,
        str(path),
        "--compact",
        "--detail-level",
        str(arguments.get("detail_level", "summary")),
    ]
    if arguments.get("execution_mode") == "experimental_toolchain":
        argv.append("--run-experimental-toolchain-check")
    try:
        completed = run_bounded_process(
            argv,
            cwd=PLUGIN_ROOT,
            timeout_s=ADAPTER_TIMEOUT_SECONDS,
            stdout_limit=MAX_ADAPTER_STDOUT_BYTES,
            stderr_limit=MAX_HOST_STDERR_BYTES,
            output="utf8",
        )
    except ProcessTimeoutError:
        return (
            {
                "ok": False,
                "error": {
                    "code": "doctor_timeout",
                    "message": "The content-free Reminders doctor timed out.",
                },
            },
            True,
        )
    except ProcessOutputLimitError:
        return (
            {
                "ok": False,
                "error": {
                    "code": "doctor_output_too_large",
                    "message": "The Reminders doctor report exceeded the MCP output bound.",
                },
            },
            True,
        )
    except ProcessDecodeError:
        return (
            {
                "ok": False,
                "error": {
                    "code": "invalid_doctor_response",
                    "message": "The Reminders doctor returned an invalid JSON report.",
                },
            },
            True,
        )
    except ProcessLaunchError as exc:
        return (
            {
                "ok": False,
                "error": {
                    "code": "doctor_launch_failed",
                    "message": f"The Reminders doctor could not start ({type(exc).__name__}).",
                },
            },
            True,
        )
    except ProcessError:
        return (
            {
                "ok": False,
                "error": {
                    "code": "invalid_doctor_response",
                    "message": "The Reminders doctor did not return a valid report.",
                },
            },
            True,
        )
    stdout = completed.stdout.strip()
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return (
            {
                "ok": False,
                "error": {
                    "code": "invalid_doctor_response",
                    "message": "The Reminders doctor returned an invalid JSON report.",
                    "exit_code": completed.returncode,
                },
            },
            True,
        )
    if not isinstance(payload, dict) or payload.get("privacy", {}).get("content_free") is not True:
        return (
            {
                "ok": False,
                "error": {
                    "code": "invalid_doctor_response",
                    "message": "The Reminders doctor did not return its content-free privacy contract.",
                },
            },
            True,
        )
    # A valid blocked/degraded diagnosis is a successful diagnostic tool call.
    return payload, False


def invoke_eventkit_bridge(
    operation: str,
    arguments: dict[str, Any],
    *,
    backend_paths: BackendPaths = DEFAULT_BACKEND_PATHS,
) -> TransportResult:
    path = backend_paths.eventkit_bridge
    if not path.is_file():
        return TransportResult(
            payload={
                "ok": False,
                "error": {
                    "code": "eventkit_bridge_unavailable",
                    "message": "The bundled EventKit bridge is unavailable.",
                },
            },
            is_error=True,
            dispatch_certainty=DispatchCertainty.PROVEN_NOT_STARTED,
        )
    bridge_arguments = dict(arguments)
    # Account/calendar enumeration is bounded defensively after the bridge
    # responds; its native contract does not accept a limit field.
    if operation in {"list_accounts", "list_calendars"}:
        bridge_arguments.pop("limit", None)
    if operation == "fetch_reminders":
        # The Core facade owns filter-bound cursors and passes only a validated
        # decoded offset across this internal transport seam.
        bridge_arguments.setdefault("offset", 0)
    request = {"schema_version": 1, "operation": operation, **bridge_arguments}

    def transport_failure(
        *, code: str, message: str, details: dict[str, Any] | None = None
    ) -> TransportResult:
        if operation in EVENTKIT_MUTATION_ROUTES.values():
            payload = mutation_outcome_unknown_response(
                request,
                reason_code=code,
                message=message,
                details=details,
            )
            return TransportResult(
                payload=payload,
                is_error=False,
                dispatch_certainty=DispatchCertainty.MAY_HAVE_STARTED,
            )
        return TransportResult(
            payload={"ok": False, "error": {"code": code, "message": message}},
            is_error=True,
            dispatch_certainty=DispatchCertainty.MAY_HAVE_STARTED,
        )
    encoded_request = json.dumps(request, ensure_ascii=False)
    if len(encoded_request.encode("utf-8")) > MAX_EVENTKIT_REQUEST_BYTES:
        return TransportResult(
            payload={
                "ok": False,
                "error": {
                    "code": "eventkit_request_too_large",
                    "message": "The EventKit request exceeded the local bridge input bound.",
                },
            },
            is_error=True,
            dispatch_certainty=DispatchCertainty.PROVEN_NOT_STARTED,
        )
    try:
        completed = run_bounded_process(
            [sys.executable, str(path)],
            cwd=PLUGIN_ROOT,
            input=encoded_request.encode("utf-8"),
            timeout_s=EVENTKIT_BRIDGE_TIMEOUT_SECONDS,
            stdout_limit=MAX_ADAPTER_STDOUT_BYTES,
            stderr_limit=MAX_HOST_STDERR_BYTES,
            output="utf8",
        )
    except ProcessTimeoutError:
        return transport_failure(
            code="eventkit_bridge_timeout",
            message="The EventKit bridge timed out before returning a result.",
            details={"timeout_seconds": EVENTKIT_BRIDGE_TIMEOUT_SECONDS},
        )
    except ProcessOutputLimitError as exc:
        return transport_failure(
            code="eventkit_bridge_output_too_large",
            message="The EventKit bridge result exceeded the MCP output bound.",
            details={"exit_code": exc.returncode},
        )
    except ProcessDecodeError:
        return transport_failure(
            code="invalid_eventkit_bridge_response",
            message="The EventKit bridge returned an invalid JSON response.",
        )
    except ProcessLaunchError as exc:
        return TransportResult(
            payload={
                "ok": False,
                "error": {
                    "code": "eventkit_bridge_process_failed",
                    "message": (
                        "The EventKit bridge failed before returning "
                        f"({type(exc).__name__})."
                    ),
                },
            },
            is_error=True,
            dispatch_certainty=DispatchCertainty.PROVEN_NOT_STARTED,
        )
    except ProcessError:
        return transport_failure(
            code="invalid_eventkit_bridge_response",
            message="The EventKit bridge did not return a valid response.",
        )

    stdout = completed.stdout.strip()
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return transport_failure(
            code="invalid_eventkit_bridge_response",
            message="The EventKit bridge returned an invalid JSON response.",
            details={"exit_code": completed.returncode},
        )
    if not isinstance(payload, dict):
        return transport_failure(
            code="invalid_eventkit_bridge_response",
            message="The EventKit bridge response must be a JSON object.",
            details={"exit_code": completed.returncode},
        )
    payload = dict(payload)
    # Dispatch provenance belongs to this launcher, never to child JSON.
    payload.pop("__dispatch_phase", None)
    try:
        validate_eventkit_response(payload, operation)
    except RuntimeError as exc:
        return transport_failure(
            code="invalid_eventkit_bridge_response",
            message=str(exc),
            details={"exit_code": completed.returncode},
        )
    is_error = payload.get("ok") is not True
    return TransportResult(
        payload=payload,
        is_error=is_error,
        dispatch_certainty=DispatchCertainty.MAY_HAVE_STARTED,
    )


def _v2_environment_fingerprint(
    backend_paths: BackendPaths = DEFAULT_BACKEND_PATHS,
) -> str:
    facts: list[str] = [
        sys.platform,
        f"{sys.version_info.major}.{sys.version_info.minor}",
    ]
    for path in (
        backend_paths.adapter,
        backend_paths.eventkit_bridge,
        backend_paths.doctor,
    ):
        try:
            stat = path.stat()
            facts.append(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            facts.append(f"{path.name}:missing")
    return hashlib.sha256("\n".join(facts).encode("utf-8")).hexdigest()


def validate_adapter_receipt(
    payload: dict[str, Any],
    *,
    expected_operation: str,
) -> str | None:
    return adapter_receipt_error(payload, expected_operation=expected_operation)


@dataclass(frozen=True)
class ToolOutcome:
    """One Facade result plus the independent mutation fact when available."""

    payload: dict[str, Any]
    mutation_state: str | None


class _LocalToolDispatch:
    """Own one runtime's lazy Facade graph and immutable backend paths."""

    def __init__(
        self,
        backend_paths: BackendPaths,
        *,
        facade_overrides: Mapping[str, Any] | None = None,
    ) -> None:
        self._backend_paths = backend_paths
        overrides = dict(facade_overrides or {})
        self._core = overrides.get("core")
        self._native = overrides.get("native")
        self._recovery = overrides.get("recovery")
        self._diagnostics = overrides.get("diagnostics")

    def _adapter_call(self, argv: list[str]) -> TransportResult:
        return invoke_adapter(argv, backend_paths=self._backend_paths)

    def _bridge_call(
        self,
        operation: str,
        arguments: dict[str, Any],
    ) -> TransportResult:
        return invoke_eventkit_bridge(
            operation,
            arguments,
            backend_paths=self._backend_paths,
        )

    def _doctor_call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        payload, _ = invoke_doctor(arguments, backend_paths=self._backend_paths)
        return payload

    def core_facade(self) -> Any:
        if self._core is None:
            if __package__:
                from .v2_core import V2CoreFacade  # noqa: PLC0415
                from .v2_core_backend import CoreBackend  # noqa: PLC0415
            else:  # pragma: no cover - exercised by the stdio entry point
                from v2_core import V2CoreFacade  # noqa: PLC0415
                from v2_core_backend import CoreBackend  # noqa: PLC0415

            backend = CoreBackend(
                bridge_call=self._bridge_call,
                adapter_call=self._adapter_call,
                build_adapter_argv=build_adapter_argv,
                idempotency_call=execute_idempotent,
                receipt_validator=validate_adapter_receipt,
            )
            self._core = V2CoreFacade(backend)
        return self._core

    def native_facade(self) -> Any:
        if self._native is None:
            if __package__:
                from .v2_native import NativeFacade  # noqa: PLC0415
                from .v2_native_backend import NativeBackend  # noqa: PLC0415
            else:  # pragma: no cover - exercised by the stdio entry point
                from v2_native import NativeFacade  # noqa: PLC0415
                from v2_native_backend import NativeBackend  # noqa: PLC0415

            backend = NativeBackend(
                bridge_call=self._bridge_call,
                adapter_call=self._adapter_call,
                build_adapter_argv=build_adapter_argv,
                receipt_validator=validate_adapter_receipt,
            )
            self._native = NativeFacade(
                adapter_call=backend.adapter_call,
                section_mutation=backend.create_section,
                references=self.core_facade().reference_port,
                native_read=backend.read,
                native_mutation=backend.mutate,
                native_copy_mutation=backend.copy_image,
            )
        return self._native

    def recovery_facade(self) -> Any:
        if self._recovery is None:
            if __package__:
                from .v2_recovery import RecoveryFacade  # noqa: PLC0415
                from .v2_recovery_backend import RecoveryBackend  # noqa: PLC0415
            else:  # pragma: no cover - exercised by the stdio entry point
                from v2_recovery import RecoveryFacade  # noqa: PLC0415
                from v2_recovery_backend import RecoveryBackend  # noqa: PLC0415

            self._recovery = RecoveryFacade(
                RecoveryBackend(
                    adapter_call=self._adapter_call,
                    bridge_call=self._bridge_call,
                    receipt_validator=validate_adapter_receipt,
                )
            )
        return self._recovery

    def diagnostics_facade(self) -> Any:
        if self._diagnostics is None:
            if __package__:
                from .v2_diagnostics import DiagnosticsFacade  # noqa: PLC0415
            else:  # pragma: no cover - exercised by the stdio entry point
                from v2_diagnostics import DiagnosticsFacade  # noqa: PLC0415

            self._diagnostics = DiagnosticsFacade(
                doctor_call=self._doctor_call,
                environment_fingerprint=lambda: _v2_environment_fingerprint(
                    self._backend_paths
                ),
            )
        return self._diagnostics

    def __call__(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> ToolOutcome:
        if name in _V2_CORE_TOOLS:
            facade = self.core_facade()
        elif name in _V2_NATIVE_TOOLS:
            facade = self.native_facade()
        elif name in _V2_RECOVERY_TOOLS:
            facade = self.recovery_facade()
        elif name in _V2_DIAGNOSTIC_TOOLS:
            facade = self.diagnostics_facade()
        else:
            raise RuntimeError(f"Public tool has no Facade owner: {name}")

        call_with_state = getattr(type(facade), "call_with_state", None)
        if callable(call_with_state):
            payload, mutation_state = call_with_state(facade, name, arguments)
        else:
            if name in V2_MUTATION_TOOLS:
                raise RuntimeError(
                    "A mutation Facade must return independent mutation state"
                )
            payload = facade.call(name, arguments)
            mutation_state = None
        if not isinstance(payload, dict):
            raise RuntimeError("A public Facade must return an object")
        if name in V2_MUTATION_TOOLS:
            if mutation_state not in {"not_mutated", "committed", "unknown"}:
                raise RuntimeError(
                    "A mutation Facade returned invalid independent mutation state"
                )
        elif mutation_state is not None:
            raise RuntimeError("A read Facade must not return mutation state")
        return ToolOutcome(payload=payload, mutation_state=mutation_state)


_SUMMARY_TARGET_FIELDS = frozenset(
    {
        "account_id",
        "attachment_id",
        "calendar_id",
        "id",
        "list_id",
        "new_attachment_id",
        "old_attachment_id",
        "reminder_id",
        "section_id",
        "source_attachment_id",
        "source_id",
        "source_reminder_id",
    }
)
_SUMMARY_READ_ONLY_TOOLS = V2_PUBLIC_TOOLS - V2_MUTATION_TOOLS


def _content_free_target(value: Any) -> dict[str, Any]:
    """Project only stable target identifiers into the human text summary."""

    if not isinstance(value, Mapping):
        return {}
    return {
        str(name): item
        for name, item in value.items()
        if name in _SUMMARY_TARGET_FIELDS
        and (item is None or isinstance(item, (str, int, bool)))
    }


def _summary_write_state(
    status: Any,
    verification: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    if verification is None:
        return False, "not_applicable"
    write_performed = verification.get("write_performed")
    if write_performed is False:
        return False, "not_mutated"
    if status == "verified" and write_performed is True:
        return True, "committed_and_verified"
    if status == "committed_verification_pending" and write_performed is True:
        return True, "committed_unverified"
    if status == "partial_success":
        return True, "partial"
    if status == "failed_manual_repair_required" and write_performed is True:
        return True, "committed_manual_repair_required"
    if status == "failed_manual_repair_required":
        return True, "committed_or_unknown"
    return True, "unknown"


def _summary_evidence_scope(
    verification: Mapping[str, Any] | None,
) -> str:
    if verification is None:
        return "bounded_public_read"
    if verification.get("write_performed") is False:
        return "affirmed_no_write"
    if (
        verification.get("state") == "read_back"
        and verification.get("final_read") is True
        and verification.get("matched") is True
    ):
        return "matched_exact_final_read"
    if verification.get("final_read") is True:
        return "partial_or_unmatched_final_read"
    return "no_final_read"


def _summary_next_read_only_action(
    payload: Mapping[str, Any],
    target: Mapping[str, Any],
) -> dict[str, Any]:
    candidate = payload.get("next_action")
    if isinstance(candidate, Mapping) and candidate.get("tool") in _SUMMARY_READ_ONLY_TOOLS:
        return {
            "kind": candidate.get("kind"),
            "tool": candidate.get("tool"),
            "retry_original_once": False,
        }

    operation = str(payload.get("operation") or "")
    if isinstance(target.get("reminder_id"), str):
        tool = "read_reminder"
    elif isinstance(target.get("list_id"), str):
        tool = (
            "inspect_reminder_native"
            if "section" in operation
            else "fetch_reminders"
        )
    elif isinstance(target.get("source_id"), str):
        tool = "list_reminder_lists"
    else:
        tool = "diagnose_reminders"
    return {"kind": "fresh_read", "tool": tool, "retry_original_once": False}


def _tool_result_summary(payload: Mapping[str, Any]) -> str:
    status = payload.get("status")
    data = payload.get("data")
    diagnostic_attention = (
        isinstance(data, Mapping)
        and data.get("overall") in {"blocked", "degraded"}
    )
    needs_attention = (
        status not in {"verified", "unchanged"}
        or payload.get("ok") is not True
        or isinstance(payload.get("error"), Mapping)
        or diagnostic_attention
    )
    verification_raw = payload.get("verification")
    verification = (
        verification_raw if isinstance(verification_raw, Mapping) else None
    )
    may_have_mutated, write_state = _summary_write_state(status, verification)
    target = _content_free_target(payload.get("target"))
    summary: dict[str, Any] = {
        "outcome": "attention_required" if needs_attention else str(status),
        "operation": payload.get("operation"),
        "status": payload.get("status"),
        "ok": payload.get("ok"),
        "needs_attention": needs_attention,
        "may_have_mutated": may_have_mutated,
        "write_state": write_state,
        "evidence_scope": _summary_evidence_scope(verification),
        "next_read_only_action": (
            _summary_next_read_only_action(payload, target)
            if needs_attention
            else None
        ),
    }
    if target:
        summary["target"] = target
    if verification is not None:
        summary["verification"] = {
            name: verification.get(name)
            for name in ("state", "write_performed", "final_read", "matched")
            if name in verification
        }
    if isinstance(data, Mapping):
        for name in ("returned", "truncated", "has_more", "overall", "scope"):
            if name in data:
                summary[name] = data[name]
    error = payload.get("error")
    if isinstance(error, Mapping):
        summary["error"] = {
            "code": error.get("code"),
            "reason_code": error.get("reason_code"),
            "retryable": error.get("retryable"),
        }
    warnings = payload.get("warnings")
    if isinstance(warnings, list):
        summary["warning_codes"] = [
            warning.get("code")
            for warning in warnings[:10]
            if isinstance(warning, Mapping)
        ]
    return json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def tool_result(payload: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
    text = _tool_result_summary(payload)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": is_error,
    }


def _mcp_tool_call_is_error(tool_name: str, payload: Mapping[str, Any]) -> bool:
    """Signal every mutation that still needs resolution as a tool error."""

    if payload.get("ok") is not True:
        return True
    return (
        tool_name in V2_MUTATION_TOOLS
        and payload.get("status") not in {"verified", "unchanged"}
    )


_V2_CORE_TOOLS = frozenset(
    {
        "request_reminders_access",
        "list_reminder_lists",
        "fetch_reminders",
        "read_reminder",
        "create_reminder",
        "change_reminder",
        "delete_reminder",
        "ensure_reminder_list",
    }
)
_V2_NATIVE_TOOLS = frozenset(
    {
        "inspect_reminder_native",
        "create_reminder_section",
        "organize_reminder",
        "change_reminder_attachment",
    }
)
_V2_DIAGNOSTIC_TOOLS = frozenset({"diagnose_reminders"})
_V2_RECOVERY_TOOLS = frozenset(
    {"inspect_recently_deleted", "recover_deleted_reminder"}
)


def _v2_public_operation(name: str, arguments: Mapping[str, Any]) -> str:
    action = arguments.get("action")
    kind = action.get("kind") if isinstance(action, Mapping) else None
    if name == "change_reminder":
        return (
            f"change_reminder.{kind}"
            if kind in {"patch", "set_completion", "move_to_list"}
            else "change_reminder.patch"
        )
    if name == "organize_reminder":
        return (
            f"organize_reminder.{kind}"
            if kind in {"move_to_section", "add_tag", "remove_tag"}
            else "organize_reminder.move_to_section"
        )
    if name == "change_reminder_attachment":
        return (
            f"change_reminder_attachment.{kind}"
            if kind in {
                "attach_image",
                "attach_url",
                "replace_image",
                "replace_url",
                "copy_image",
                "delete",
            }
            else "change_reminder_attachment.attach_image"
        )
    return name


def _v2_public_backend(name: str, arguments: Mapping[str, Any]) -> str:
    if name in {"create_reminder", "change_reminder"}:
        action = arguments.get("action")
        has_url = isinstance(arguments.get("url"), str) or (
            isinstance(action, Mapping)
            and isinstance(action.get("patch"), Mapping)
            and isinstance(action["patch"].get("url"), str)
        )
        return "eventkit_plus_native_url" if has_url else "eventkit_public_sdk"
    if name in {"delete_reminder", "ensure_reminder_list"}:
        return "eventkit_public_sdk"
    return "native_extension"


def _v2_public_target(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    if name == "create_reminder":
        return {"list_id": arguments.get("list_id")}
    if name == "ensure_reminder_list":
        return {"source_id": arguments.get("source_id"), "list_id": None}
    if name == "create_reminder_section":
        return {"list_id": arguments.get("list_id"), "section_id": None}
    if name == "recover_deleted_reminder":
        return {"list_id": arguments.get("list_id")}
    return {}


def _v2_pre_dispatch_failure(
    name: str,
    arguments: Mapping[str, Any],
    *,
    code: str,
    reason_code: str,
    message: str,
    retryable: bool,
) -> tuple[dict[str, Any], str]:
    error = {
        "code": code,
        "reason_code": reason_code,
        "message": message[:2000],
        "retryable": retryable,
    }
    if name not in V2_MUTATION_TOOLS:
        return (
            {
                "schema_version": 2,
                "ok": False,
                "status": "failed_no_mutation",
                "operation": name,
                "error": error,
            },
            "not_mutated",
        )
    return (
        {
            "schema_version": 2,
            "ok": False,
            "status": "failed_no_mutation",
            "operation": _v2_public_operation(name, arguments),
            "operation_id": str(uuid.uuid4()),
            "backend": _v2_public_backend(name, arguments),
            "target": _v2_public_target(name, arguments),
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
        },
        "not_mutated",
    )


def _v2_contract_failure(
    name: str,
    arguments: Mapping[str, Any],
    *,
    mutation_state: str | None,
    message: str,
) -> tuple[dict[str, Any], str | None]:
    if name in V2_MUTATION_TOOLS and mutation_state in {"committed", "unknown"}:
        from reminders_service import unverified_mutation_projection  # noqa: PLC0415

        recovery_tool = {
            "create_reminder": "fetch_reminders",
            "ensure_reminder_list": "list_reminder_lists",
            "create_reminder_section": "inspect_reminder_native",
        }.get(name, "read_reminder")
        recovery_message = {
            "create_reminder": (
                "Fetch the target list and resolve whether the Reminder create "
                "committed before retrying."
            ),
            "ensure_reminder_list": (
                "List the exact Reminder account's lists and resolve whether the "
                "list create committed before retrying."
            ),
            "create_reminder_section": (
                "Inspect the exact Reminder List's sections and resolve whether "
                "the section create committed before retrying."
            ),
        }.get(name, "Read the exact Reminder again before another change.")
        return (
            {
                "schema_version": 2,
                **unverified_mutation_projection(mutation_state),
                "operation": _v2_public_operation(name, arguments),
                "operation_id": str(uuid.uuid4()),
                "backend": _v2_public_backend(name, arguments),
                "target": _v2_public_target(name, arguments),
                "before": None,
                "after": None,
                "error": {
                    "code": "sync_pending",
                    "reason_code": "public_result_contract_failed",
                    "message": message[:2000],
                    "retryable": False,
                },
                "next_action": {
                    "kind": "fresh_read",
                    "tool": recovery_tool,
                    "retry_original_once": False,
                    "message": recovery_message,
                },
            },
            mutation_state,
        )
    return _v2_pre_dispatch_failure(
        name,
        arguments,
        code="unexpected_error",
        reason_code="public_result_contract_failed",
        message=message,
        retryable=False,
    )


class McpRuntime:
    """One initialized MCP connection with isolated mutable runtime state."""

    def __init__(
        self,
        backend_paths: BackendPaths = DEFAULT_BACKEND_PATHS,
        *,
        dispatch: Callable[[str, Mapping[str, Any]], ToolOutcome] | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_calls_per_minute: int = MAX_CALLS_PER_MINUTE,
    ) -> None:
        if max_calls_per_minute <= 0:
            raise ValueError("max_calls_per_minute must be positive")
        self._dispatch = dispatch or _LocalToolDispatch(backend_paths)
        self._clock = clock
        self._max_calls_per_minute = max_calls_per_minute
        self._recent_calls: deque[float] = deque()
        self._initialized = False

    def _rate_limit_allows_call(self) -> bool:
        now = self._clock()
        while self._recent_calls and now - self._recent_calls[0] >= 60:
            self._recent_calls.popleft()
        if len(self._recent_calls) >= self._max_calls_per_minute:
            return False
        self._recent_calls.append(now)
        return True

    def call_tool(self, name: str, raw_arguments: Any) -> dict[str, Any]:
        return _call_tool(
            name,
            raw_arguments,
            dispatch=self._dispatch,
            rate_limit_allows_call=self._rate_limit_allows_call,
        )

    def handle(self, message: Any) -> dict[str, Any] | None:
        try:
            return _handle_message(self, message)
        except Exception as exc:
            request_id = message.get("id") if isinstance(message, dict) else None
            print(f"{SERVER_NAME}: {type(exc).__name__}", file=sys.stderr)
            return jsonrpc_error(
                request_id,
                JSONRPC_INTERNAL_ERROR,
                "Internal server error",
            )


def _call_tool(
    name: str,
    raw_arguments: Any,
    *,
    dispatch: Callable[[str, Mapping[str, Any]], ToolOutcome],
    rate_limit_allows_call: Callable[[], bool],
) -> dict[str, Any]:
    tool = TOOLS_BY_NAME[name]
    supplied_for_error = raw_arguments if isinstance(raw_arguments, dict) else {}
    try:
        supplied = validate_arguments(tool, raw_arguments)
    except ToolInputError as exc:
        payload, mutation_state = _v2_pre_dispatch_failure(
            name,
            supplied_for_error,
            code="invalid_input",
            reason_code="invalid_arguments",
            message=str(exc),
            retryable=False,
        )
    else:
        arguments = effective_arguments(tool, supplied)
        if sys.version_info < MIN_PYTHON_VERSION:
            detected_version = ".".join(
                str(part) for part in sys.version_info[:3]
            )
            payload, mutation_state = _v2_pre_dispatch_failure(
                name,
                arguments,
                code="unsupported_capability",
                reason_code="unsupported_python_runtime",
                message=(
                    "Apple Reminders requires Python 3.11 or newer, but the "
                    f"plugin command resolved to Python {detected_version}. Ensure "
                    "that `python3` in the Codex process PATH resolves to a supported "
                    "interpreter, restart Codex, and retry."
                ),
                retryable=False,
            )
        elif not rate_limit_allows_call():
            payload, mutation_state = _v2_pre_dispatch_failure(
                name,
                arguments,
                code="rate_limited",
                reason_code="local_rate_limit",
                message="Too many local Reminders calls were requested; wait briefly.",
                retryable=True,
            )
        else:
            try:
                outcome = dispatch(name, arguments)
                if not isinstance(outcome, ToolOutcome):
                    raise RuntimeError("Tool dispatch must return ToolOutcome")
                payload = outcome.payload
                mutation_state = outcome.mutation_state
            except Exception as exc:
                payload, mutation_state = _v2_contract_failure(
                    name,
                    arguments,
                    mutation_state=(
                        "unknown" if name in V2_MUTATION_TOOLS else None
                    ),
                    message=f"The public facade failed ({type(exc).__name__}).",
                )

    arguments_for_contract = (
        effective_arguments(tool, supplied)
        if "supplied" in locals()
        else supplied_for_error
    )
    payload = sanitize_payload(payload)
    try:
        payload = validate_public_result(name, payload, mutation_state)
    except PublicResultContractError as exc:
        payload, mutation_state = _v2_contract_failure(
            name,
            arguments_for_contract,
            mutation_state=mutation_state,
            message=str(exc),
        )
        payload = validate_public_result(name, payload, mutation_state)
    return tool_result(payload, is_error=_mcp_tool_call_is_error(name, payload))


def jsonrpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(
    request_id: Any,
    code: int,
    message: str,
    data: Any | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _handle_message(runtime: McpRuntime, message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return jsonrpc_error(None, JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC request")

    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params", {})
    if not isinstance(method, str):
        return jsonrpc_error(request_id, JSONRPC_INVALID_REQUEST, "Request method is required")

    request_methods = {"initialize", "ping", "tools/list", "tools/call"}
    if method in request_methods:
        has_valid_id = (
            "id" in message
            and not isinstance(request_id, bool)
            and isinstance(request_id, (str, int))
        )
        if not has_valid_id:
            return jsonrpc_error(
                None,
                JSONRPC_INVALID_REQUEST,
                f"{method} requires a non-null string or integer request id",
            )

    if method == "initialize":
        if not isinstance(params, dict):
            return jsonrpc_error(request_id, JSONRPC_INVALID_PARAMS, "initialize params must be an object")
        requested = params.get("protocolVersion")
        if not isinstance(requested, str) or not requested:
            return jsonrpc_error(
                request_id,
                JSONRPC_INVALID_PARAMS,
                "initialize protocolVersion must be a non-empty string",
            )
        if not isinstance(params.get("capabilities"), dict):
            return jsonrpc_error(
                request_id,
                JSONRPC_INVALID_PARAMS,
                "initialize capabilities must be an object",
            )
        if not isinstance(params.get("clientInfo"), dict):
            return jsonrpc_error(
                request_id,
                JSONRPC_INVALID_PARAMS,
                "initialize clientInfo must be an object",
            )
        negotiated = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        runtime._initialized = True
        return jsonrpc_result(
            request_id,
            {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "title": SERVER_TITLE,
                    "version": SERVER_VERSION,
                    "description": "Typed local tools for Apple Reminders.",
                },
                "instructions": (
                    "Use bounded reads and exact IDs. Read one Reminder before changing it, then "
                    "pass back its opaque reference. Request Reminders access only after a "
                    "permission result; diagnose only after a relevant failure."
                ),
            },
        )

    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "ping":
        return jsonrpc_result(request_id, {})
    if not runtime._initialized:
        return jsonrpc_error(
            request_id,
            JSONRPC_SERVER_NOT_INITIALIZED,
            "Server has not been initialized",
        )
    if method == "tools/list":
        if not isinstance(params, dict):
            return jsonrpc_error(request_id, JSONRPC_INVALID_PARAMS, "tools/list params must be an object")
        if params.get("cursor") not in {None, ""}:
            return jsonrpc_error(request_id, JSONRPC_INVALID_PARAMS, "Unknown tools/list cursor")
        return jsonrpc_result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        if not isinstance(params, dict):
            return jsonrpc_error(request_id, JSONRPC_INVALID_PARAMS, "tools/call params must be an object")
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOLS_BY_NAME:
            return jsonrpc_error(request_id, JSONRPC_INVALID_PARAMS, f"Unknown tool: {name or ''}")
        return jsonrpc_result(
            request_id,
            runtime.call_tool(name, params.get("arguments", {})),
        )

    if request_id is None:
        return None
    return jsonrpc_error(request_id, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def serve_stdio(
    runtime: McpRuntime,
    *,
    stdin: Any = None,
    send_message: Callable[[dict[str, Any]], None] = send,
) -> int:
    source = sys.stdin if stdin is None else stdin
    for raw_line in source:
        if len(raw_line.encode("utf-8", errors="replace")) > MAX_MCP_MESSAGE_BYTES:
            send_message(
                jsonrpc_error(
                    None,
                    JSONRPC_INVALID_REQUEST,
                    "JSON-RPC message exceeds the size limit",
                )
            )
            continue
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            send_message(jsonrpc_error(None, JSONRPC_PARSE_ERROR, "Parse error"))
            continue
        response = runtime.handle(message)
        if response is not None:
            send_message(response)
    return 0


def main(*, backend_paths: BackendPaths | None = None) -> int:
    runtime = McpRuntime(backend_paths or DEFAULT_BACKEND_PATHS)
    return serve_stdio(runtime)


if __name__ == "__main__":
    raise SystemExit(main())
