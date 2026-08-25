#!/usr/bin/env python3
"""Dependency-light stdio MCP shim for the bundled Reminders backends.

The public EventKit bridge and private-feature adapter own Reminders business
logic. This module owns the transport boundary: typed inputs, exact selectors,
bounded/redacted outputs, JSON-RPC framing, and subprocess isolation.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import time
import urllib.parse
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
MCP_DIR = Path(__file__).resolve().parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from receipt_contract import (  # noqa: E402
    FAILURE_RECEIPT_STATUSES,
    SUCCESS_RECEIPT_STATUSES,
    adapter_receipt_error,
)
from v2_contract import (  # noqa: E402
    MUTATION_TOOLS as V2_MUTATION_TOOLS,
    PUBLIC_TOOLS as V2_PUBLIC_TOOLS,
    PublicResultContractError,
    validate_public_result,
)


SERVER_NAME = "apple-reminders-local"
SERVER_TITLE = "Apple Reminders"
SERVER_VERSION = "0.3.0"
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
_ACTIVE_BACKEND_PATHS = DEFAULT_BACKEND_PATHS

ADAPTER_TIMEOUT_SECONDS = 45
EVENTKIT_BRIDGE_TIMEOUT_SECONDS = 60
MAX_ADAPTER_STDOUT_BYTES = 2_000_000
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
            "Public v2 tool definitions do not match the 13-tool runtime surface"
        )
    return tools


TOOLS = load_tools()
TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}
RECEIPT_STATUSES = set(SUCCESS_RECEIPT_STATUSES)
FAILED_RECEIPT_STATUSES = set(FAILURE_RECEIPT_STATUSES)
RECENT_CALLS: deque[float] = deque()
SESSION_INITIALIZED = False
_ADAPTER_MODULE: Any | None = None
_EVENTKIT_BRIDGE_MODULE: Any | None = None
_V2_CORE_FACADE: Any | None = None
_V2_NATIVE_FACADE: Any | None = None
_V2_TYPES_LOADED = False


def _ensure_v2_types() -> None:
    """Load write-path facades only when the first tool is called."""

    global _V2_TYPES_LOADED
    if _V2_TYPES_LOADED:
        return
    from reminders_service import (  # noqa: PLC0415
        MutationOutcome as _MutationOutcome,
        ReferenceRejected as _ReferenceRejected,
    )
    from v2_core import (  # noqa: PLC0415
        EventKitReply as _EventKitReply,
        V2CoreFacade as _V2CoreFacade,
    )
    from v2_native import NativeFacade as _NativeFacade  # noqa: PLC0415

    globals().update(
        {
            "MutationOutcome": _MutationOutcome,
            "ReferenceRejected": _ReferenceRejected,
            "EventKitReply": _EventKitReply,
            "V2CoreFacade": _V2CoreFacade,
            "NativeFacade": _NativeFacade,
        }
    )
    _V2_TYPES_LOADED = True


class ToolInputError(ValueError):
    pass


class EventKitBridgeFailure(RuntimeError):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("EventKit bridge operation failed")
        self.payload = payload


class EventKitReceiptFailure(RuntimeError):
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


def adapter_path() -> Path:
    return _ACTIVE_BACKEND_PATHS.adapter


def eventkit_bridge_path() -> Path:
    return _ACTIVE_BACKEND_PATHS.eventkit_bridge


def doctor_path() -> Path:
    return _ACTIVE_BACKEND_PATHS.doctor


def _load_local_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load local module: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def bundled_adapter_module() -> Any:
    global _ADAPTER_MODULE
    if _ADAPTER_MODULE is None:
        _ADAPTER_MODULE = _load_local_module(
            "_apple_reminders_adapter_for_mcp",
            DEFAULT_ADAPTER_PATH,
        )
    return _ADAPTER_MODULE


def bundled_eventkit_bridge_module() -> Any:
    global _EVENTKIT_BRIDGE_MODULE
    if _EVENTKIT_BRIDGE_MODULE is None:
        _EVENTKIT_BRIDGE_MODULE = _load_local_module(
            "_apple_reminders_eventkit_bridge_for_mcp",
            DEFAULT_EVENTKIT_BRIDGE_PATH,
        )
    return _EVENTKIT_BRIDGE_MODULE


def invoke_adapter(argv: list[str]) -> tuple[dict[str, Any], bool]:
    path = adapter_path()
    if not path.is_file():
        return (
            {
                "ok": False,
                "error": {
                    "code": "adapter_unavailable",
                    "message": "The bundled Reminders adapter is unavailable.",
                },
            },
            True,
        )
    try:
        completed = subprocess.run(
            [sys.executable, str(path), *argv],
            cwd=PLUGIN_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=ADAPTER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            {
                "ok": False,
                "error": {
                    "code": "adapter_timeout",
                    "message": "The Reminders adapter timed out before returning a result.",
                },
            },
            True,
        )
    except OSError as exc:
        return (
            {
                "ok": False,
                "error": {
                    "code": "adapter_launch_failed",
                    "message": f"The Reminders adapter could not start ({type(exc).__name__}).",
                },
            },
            True,
        )

    stdout = completed.stdout.strip()
    if len(stdout.encode("utf-8", errors="replace")) > MAX_ADAPTER_STDOUT_BYTES:
        return (
            {
                "ok": False,
                "error": {
                    "code": "adapter_output_too_large",
                    "message": "The Reminders adapter result exceeded the MCP output bound.",
                },
            },
            True,
        )
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return (
            {
                "ok": False,
                "error": {
                    "code": "invalid_adapter_response",
                    "message": "The Reminders adapter returned an invalid JSON response.",
                    "exit_code": completed.returncode,
                },
            },
            True,
        )
    if not isinstance(payload, dict):
        payload = {"ok": completed.returncode == 0, "result": payload}
    is_error = payload.get("ok") is not True
    return payload, is_error


def invoke_doctor(arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    path = doctor_path()
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
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(path),
                "--compact",
                "--detail-level",
                str(arguments.get("detail_level", "summary")),
            ],
            cwd=PLUGIN_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=ADAPTER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
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
    except OSError as exc:
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
    stdout = completed.stdout.strip()
    if len(stdout.encode("utf-8", errors="replace")) > MAX_ADAPTER_STDOUT_BYTES:
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


def invoke_eventkit_bridge(operation: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    path = eventkit_bridge_path()
    if not path.is_file():
        return (
            {
                "ok": False,
                "error": {
                    "code": "eventkit_bridge_unavailable",
                    "message": "The bundled EventKit bridge is unavailable.",
                },
            },
            True,
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
    ) -> tuple[dict[str, Any], bool]:
        if operation in EVENTKIT_MUTATION_ROUTES.values():
            payload = bundled_eventkit_bridge_module().mutation_outcome_unknown_response(
                request,
                reason_code=code,
                message=message,
                details=details,
            )
            return payload, False
        return {"ok": False, "error": {"code": code, "message": message}}, True
    encoded_request = json.dumps(request, ensure_ascii=False)
    if len(encoded_request.encode("utf-8")) > MAX_EVENTKIT_REQUEST_BYTES:
        return (
            {
                "ok": False,
                "error": {
                    "code": "eventkit_request_too_large",
                    "message": "The EventKit request exceeded the local bridge input bound.",
                },
            },
            True,
        )
    try:
        completed = subprocess.run(
            [sys.executable, str(path)],
            cwd=PLUGIN_ROOT,
            input=encoded_request,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=EVENTKIT_BRIDGE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return transport_failure(
            code="eventkit_bridge_timeout",
            message="The EventKit bridge timed out before returning a result.",
            details={"timeout_seconds": EVENTKIT_BRIDGE_TIMEOUT_SECONDS},
        )
    except OSError as exc:
        return (
            {
                "ok": False,
                "error": {
                    "code": "eventkit_bridge_launch_failed",
                    "message": f"The EventKit bridge could not start ({type(exc).__name__}).",
                },
            },
            True,
        )

    stdout = completed.stdout.strip()
    if len(stdout.encode("utf-8", errors="replace")) > MAX_ADAPTER_STDOUT_BYTES:
        return transport_failure(
            code="eventkit_bridge_output_too_large",
            message="The EventKit bridge result exceeded the MCP output bound.",
            details={"exit_code": completed.returncode},
        )
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
    try:
        bundled_eventkit_bridge_module().validate_response(payload, operation)
    except RuntimeError as exc:
        return transport_failure(
            code="invalid_eventkit_bridge_response",
            message=str(exc),
            details={"exit_code": completed.returncode},
        )
    is_error = payload.get("ok") is not True
    return payload, is_error


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
    payload.setdefault("warnings", []).append(
        {
            "code": code,
            "message": message,
        }
    )
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
    payload["error"] = {
        "code": "sync_pending",
        "reason_code": reason_code,
        "message": message,
    }
    return payload


def ensure_visible_url_attachment(
    payload: dict[str, Any],
    url: str,
) -> dict[str, Any]:
    if payload.get("status") not in {"verified", "unchanged"}:
        return payload
    reminder_id = _mutation_reminder_id(payload)
    if reminder_id is None:
        return _url_attachment_partial_receipt(
            payload,
            code="native_url_attachment_target_missing",
            message=(
                "The EventKit write succeeded, but its reminder identifier was unavailable for "
                "the visible URL attachment step."
            ),
        )

    initial_eventkit_status = str(payload["status"])
    attachment: dict[str, Any] | None = None
    attachment_status: str | None = None
    list_arguments = {"reminder_id": reminder_id, "limit": 1}
    list_payload, list_is_error = invoke_adapter(
        build_adapter_argv("list_reminder_attachments", list_arguments)
    )
    reminder_version = list_payload.get("reminder_version")
    if (
        list_is_error
        or not isinstance(reminder_version, int)
        or isinstance(reminder_version, bool)
        or reminder_version < 0
    ):
        error = list_payload.get("error") if isinstance(list_payload.get("error"), dict) else {}
        _url_attachment_partial_receipt(
            payload,
            code=str(error.get("code") or "native_url_attachment_precondition_failed"),
            message=(
                "The EventKit write succeeded, but the plugin could not obtain a fresh reminder "
                "version for the visible URL attachment."
            ),
        )
    else:
        attach_arguments = {
            "reminder_id": reminder_id,
            "url": url,
            "if_version": reminder_version,
        }
        attach_payload, attach_is_error = invoke_adapter(
            build_adapter_argv("attach_url_to_reminder", attach_arguments)
        )
        receipt_error = (
            validate_adapter_receipt(attach_payload, expected_operation="attach_url")
            if not attach_is_error
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
        if (
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
            _url_attachment_partial_receipt(
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

    final_payload, final_is_error = invoke_eventkit_bridge(
        "read_reminder",
        {"reminder_id": reminder_id},
    )
    final_data = final_payload.get("data")
    final_reminder = (
        final_data.get("reminder")
        if isinstance(final_data, dict) and isinstance(final_data.get("reminder"), dict)
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
    elif _mutation_reminder_id({"after": final_reminder}) != reminder_id:
        reason_code = "eventkit_final_read_target_mismatch"
    elif final_reminder.get("url") != url:
        reason_code = "eventkit_final_read_url_mismatch"
    elif not _is_rfc3339_timestamp(final_reminder.get("last_modified")):
        reason_code = "eventkit_final_last_modified_invalid"
    if reason_code is not None:
        return _url_final_read_pending(
            payload,
            reminder_id=reminder_id,
            attachment=attachment,
            reason_code=reason_code,
        )

    final_after = dict(final_reminder)
    if attachment is not None:
        final_after["url_attachment"] = attachment
    payload["after"] = final_after
    payload.setdefault("verification", {})["eventkit_final_read"] = {
        "state": "read_back",
        "reminder_id": reminder_id,
        "last_modified_safe_for_precondition": True,
    }
    payload.setdefault("recovery", {})["eventkit_final_read"] = {
        "semantics": "not_applicable",
        "automatic_retry_safe": True,
    }
    if payload.get("status") == "partial_success":
        verification = payload.setdefault("verification", {})
        verification["state"] = "partial"
        verification["final_read"] = True
        verification["matched"] = True
        return payload
    payload["status"] = (
        "unchanged"
        if initial_eventkit_status == "unchanged" and attachment_status == "unchanged"
        else "verified"
    )
    return payload


def invoke_eventkit_mutation(
    tool_name: str,
    operation: str,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    bridge_arguments = dict(arguments)
    idempotency_key = bridge_arguments.pop("idempotency_key", None)
    bridge_contract = bundled_eventkit_bridge_module()

    def execute_once() -> dict[str, Any]:
        payload, is_error = invoke_eventkit_bridge(operation, bridge_arguments)
        if is_error:
            raise EventKitBridgeFailure(payload)
        try:
            bridge_contract.validate_mutation_receipt(payload, operation)
        except RuntimeError as exc:
            raise EventKitReceiptFailure(str(exc)) from exc
        visible_url = bridge_arguments.get("url") if tool_name == "create_reminder" else None
        if tool_name == "update_reminder" and isinstance(bridge_arguments.get("patch"), dict):
            visible_url = bridge_arguments["patch"].get("url")
        if isinstance(visible_url, str):
            payload = ensure_visible_url_attachment(payload, visible_url)
        return payload

    try:
        if tool_name == "create_reminder":
            adapter = bundled_adapter_module()
            request = {"schema_version": 1, "operation": operation, **bridge_arguments}
            payload = adapter.execute_idempotent(
                operation="eventkit_create_reminder",
                key=idempotency_key,
                input_payload=request,
                callback=execute_once,
            )
            if payload.get("replayed") is True and payload.get("status") == "committed_verification_pending":
                payload["warnings"] = [
                    {
                        "code": "sync_pending",
                        "message": "This replayed creation receipt is still awaiting verification.",
                    }
                ]
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
    except EventKitBridgeFailure as exc:
        return exc.payload, True
    except EventKitReceiptFailure as exc:
        return (
            {
                "ok": False,
                "error": {
                    "code": "invalid_eventkit_receipt",
                    "message": str(exc),
                },
            },
            True,
        )
    except Exception as exc:
        adapter_error = getattr(bundled_adapter_module(), "AdapterError", ())
        if adapter_error and isinstance(exc, adapter_error):
            return (
                {
                    "ok": False,
                    "status": "failed_no_mutation",
                    "operation": operation,
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "details": exc.details,
                    },
                },
                True,
            )
        raise
    return payload, payload.get("ok") is not True


class _ServerEventKitPort:
    """Production v2 Core port over the existing guarded EventKit launcher."""

    _MUTATION_TOOL_NAMES = {
        "create_reminder": "create_reminder",
        "update_reminder": "update_reminder",
        "complete_reminder": "complete_reminder",
        "reopen_reminder": "reopen_reminder",
        "move_reminder": "move_reminder_to_list",
        "delete_reminder": "delete_reminder",
    }

    def invoke(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        mutation: bool,
    ) -> EventKitReply:
        _ensure_v2_types()
        supplied = copy.deepcopy(dict(arguments))
        if mutation:
            tool_name = self._MUTATION_TOOL_NAMES.get(operation)
            if tool_name is None:
                raise RuntimeError(f"Unsupported v2 EventKit mutation: {operation}")
            payload, is_error = invoke_eventkit_mutation(
                tool_name,
                operation,
                supplied,
            )
        else:
            payload, is_error = invoke_eventkit_bridge(operation, supplied)
        return EventKitReply(payload=payload, is_error=is_error)


def _v2_eventkit_call(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    payload, _ = invoke_eventkit_mutation(
        operation,
        operation,
        copy.deepcopy(arguments),
    )
    return payload


def _v2_adapter_call(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
    public_route = {
        "list_sections": "list_reminder_sections",
        "list_tags": "list_reminder_tags",
        "create_section": "create_reminder_section",
    }.get(command)
    if public_route is None:
        raise RuntimeError(f"Unsupported v2 adapter read: {command}")
    route_arguments = {
        key: value
        for key, value in arguments.items()
        if value is not None
    }
    payload, _ = invoke_adapter(build_adapter_argv(public_route, route_arguments))
    return payload


def _v2_doctor_call(arguments: dict[str, Any]) -> dict[str, Any]:
    payload, _ = invoke_doctor(arguments)
    return payload


def _v2_environment_fingerprint() -> str:
    facts: list[str] = [
        sys.platform,
        f"{sys.version_info.major}.{sys.version_info.minor}",
    ]
    for path in (adapter_path(), eventkit_bridge_path(), doctor_path()):
        try:
            stat = path.stat()
            facts.append(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            facts.append(f"{path.name}:missing")
    return hashlib.sha256("\n".join(facts).encode("utf-8")).hexdigest()


def _v2_revalidate_guard(guard: Guard) -> dict[str, Any]:
    _ensure_v2_types()
    payload, is_error = invoke_eventkit_bridge(
        "read_reminder",
        {"reminder_id": guard.reminder_id},
    )
    data = payload.get("data")
    reminder = data.get("reminder") if isinstance(data, dict) else None
    if is_error or payload.get("status") != "verified" or not isinstance(reminder, dict):
        raise ReferenceRejected(
            "concurrent_modification",
            "The exact Reminder could not be revalidated before native access",
        )
    if reminder.get("id") != guard.reminder_id:
        raise ReferenceRejected(
            "invalid_reference",
            "The native guard resolved to a different Reminder",
        )
    store_part = reminder.get("source_id") or reminder.get("list_id") or reminder.get(
        "calendar_id"
    )
    store_identity = f"eventkit:{store_part}" if isinstance(store_part, str) else None
    if (
        store_identity != guard.store_identity
        or reminder.get("last_modified") != guard.public_concurrency_value
    ):
        raise ReferenceRejected(
            "concurrent_modification",
            "The Reminder changed before native access",
        )
    return reminder


def _v2_private_read_reminder(reminder_id: str) -> dict[str, Any]:
    payload, is_error = invoke_adapter(["read_reminder", "--id", reminder_id])
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


def _v2_private_attachments(
    reminder_id: str,
    *,
    limit: int,
    attachment_type: str | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"reminder_id": reminder_id, "limit": limit}
    if attachment_type is not None:
        arguments["attachment_type"] = attachment_type
    payload, is_error = invoke_adapter(
        build_adapter_argv("list_reminder_attachments", arguments)
    )
    if is_error or payload.get("reminder_id") != reminder_id:
        raise RuntimeError("The private attachment read failed")
    return payload


def _v2_native_read(guard: Guard, arguments: dict[str, Any]) -> dict[str, Any]:
    _v2_revalidate_guard(guard)
    include = set(arguments.get("include") or [])
    result: dict[str, Any] = {"reminder_id": guard.reminder_id}
    if include & {"section", "tags", "sync"}:
        reminder = _v2_private_read_reminder(guard.reminder_id)
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
        attachments = _v2_private_attachments(
            guard.reminder_id,
            limit=int(arguments.get("limit", 100)),
            attachment_type=arguments.get("attachment_type"),
        )
        result["attachment_items"] = copy.deepcopy(
            attachments.get("attachments", [])
        )
        result["truncated"] = attachments.get("truncated") is True
    return result


def _v2_native_failure_outcome(
    guard: Guard,
    command: str,
    *,
    code: str,
    message: str,
) -> MutationOutcome:
    _ensure_v2_types()
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


def _v2_native_pending_outcome(
    guard: Guard,
    command: str,
    *,
    reason_code: str,
    message: str,
) -> MutationOutcome:
    _ensure_v2_types()
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


def _v2_native_route(
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
        return "move_reminder_to_section", {**base, "section_id": arguments["section_id"]}
    if command in {"add_tag", "remove_tag"}:
        tool_name = "add_reminder_tag" if command == "add_tag" else "remove_reminder_tag"
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


def _v2_native_mutation(
    guard: Guard,
    command: str,
    arguments: dict[str, Any],
) -> MutationOutcome:
    _ensure_v2_types()
    try:
        _v2_revalidate_guard(guard)
        private_state = _v2_private_attachments(guard.reminder_id, limit=1)
    except ReferenceRejected as exc:
        return _v2_native_failure_outcome(
            guard,
            command,
            code="concurrent_modification",
            message=str(exc),
        )
    except Exception as exc:
        return _v2_native_failure_outcome(
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
        return _v2_native_failure_outcome(
            guard,
            command,
            code="schema_mismatch",
            message="The private Reminder revision was unavailable.",
        )

    tool_name, routed_arguments = _v2_native_route(
        command,
        guard,
        arguments,
        reminder_version,
    )
    payload, is_error = invoke_adapter(build_adapter_argv(tool_name, routed_arguments))
    expected_operation = ROUTES[tool_name].command
    status = payload.get("status")
    if status not in RECEIPT_STATUSES | FAILED_RECEIPT_STATUSES:
        return _v2_native_pending_outcome(
            guard,
            command,
            reason_code="invalid_native_mutation_receipt",
            message="The native mutation result could not be validated.",
        )
    receipt_error = validate_adapter_receipt(
        payload,
        expected_operation=expected_operation,
    )
    if receipt_error:
        return _v2_native_pending_outcome(
            guard,
            command,
            reason_code="invalid_native_mutation_receipt",
            message=receipt_error,
        )
    if is_error and status not in {
        "failed_no_mutation",
        "failed_manual_repair_required",
    }:
        return _v2_native_pending_outcome(
            guard,
            command,
            reason_code="native_mutation_failed_after_dispatch",
            message="The native mutation may have partially committed.",
        )

    if status in {"verified", "unchanged"}:
        try:
            if command in {"move_to_section", "add_tag", "remove_tag"}:
                final_state = _v2_private_read_reminder(guard.reminder_id)
                payload["after"] = final_state
            else:
                final_state = _v2_private_attachments(guard.reminder_id, limit=200)
                payload["after"] = {
                    "reminder": {"id": guard.reminder_id},
                    "attachments": final_state.get("attachments", []),
                }
        except Exception:
            if status == "verified":
                return _v2_native_pending_outcome(
                    guard,
                    command,
                    reason_code="native_final_read_failed",
                    message="The native write committed, but its final state could not be read.",
                )

    if status in {"unchanged", "failed_no_mutation"}:
        mutation_state = "not_mutated"
    elif status in {"verified", "partial_success"}:
        mutation_state = "committed"
    else:
        mutation_state = "unknown"
    return MutationOutcome(receipt=payload, mutation_state=mutation_state)


def _v2_core_facade() -> V2CoreFacade:
    global _V2_CORE_FACADE
    _ensure_v2_types()
    if _V2_CORE_FACADE is None:
        _V2_CORE_FACADE = V2CoreFacade(_ServerEventKitPort())
    return _V2_CORE_FACADE


def _v2_native_facade() -> NativeFacade:
    global _V2_NATIVE_FACADE
    _ensure_v2_types()
    if _V2_NATIVE_FACADE is None:
        _V2_NATIVE_FACADE = NativeFacade(
            eventkit_call=_v2_eventkit_call,
            adapter_call=_v2_adapter_call,
            doctor_call=_v2_doctor_call,
            environment_fingerprint=_v2_environment_fingerprint,
            references=_v2_core_facade().reference_port,
            native_read=_v2_native_read,
            native_mutation=_v2_native_mutation,
        )
    return _V2_NATIVE_FACADE


def validate_adapter_receipt(
    payload: dict[str, Any],
    *,
    expected_operation: str,
) -> str | None:
    return adapter_receipt_error(payload, expected_operation=expected_operation)


def rate_limit_allows_call() -> bool:
    now = time.monotonic()
    while RECENT_CALLS and now - RECENT_CALLS[0] >= 60:
        RECENT_CALLS.popleft()
    if len(RECENT_CALLS) >= MAX_CALLS_PER_MINUTE:
        return False
    RECENT_CALLS.append(now)
    return True


def _tool_result_summary(payload: Mapping[str, Any]) -> str:
    summary: dict[str, Any] = {
        "operation": payload.get("operation"),
        "status": payload.get("status"),
        "ok": payload.get("ok"),
    }
    data = payload.get("data")
    if isinstance(data, Mapping):
        for name in ("returned", "truncated", "has_more"):
            if name in data:
                summary[name] = data[name]
    error = payload.get("error")
    if isinstance(error, Mapping):
        summary["error"] = {
            "code": error.get("code"),
            "message": str(error.get("message") or "")[:500],
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


_V2_CORE_TOOLS = frozenset(
    {
        "request_reminders_access",
        "list_reminder_lists",
        "fetch_reminders",
        "read_reminder",
        "create_reminder",
        "change_reminder",
        "delete_reminder",
    }
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


def _v2_mutation_state(payload: Mapping[str, Any]) -> str | None:
    status = payload.get("status")
    if status in {"unchanged", "failed_no_mutation"}:
        return "not_mutated"
    if status == "verified":
        return "committed"
    if status in {
        "committed_verification_pending",
        "partial_success",
        "failed_manual_repair_required",
    }:
        return "unknown"
    return None


def _v2_contract_failure(
    name: str,
    arguments: Mapping[str, Any],
    *,
    may_have_mutated: bool,
    message: str,
) -> tuple[dict[str, Any], str | None]:
    if name in V2_MUTATION_TOOLS and may_have_mutated:
        return (
            {
                "schema_version": 2,
                "ok": True,
                "status": "committed_verification_pending",
                "operation": _v2_public_operation(name, arguments),
                "operation_id": str(uuid.uuid4()),
                "backend": _v2_public_backend(name, arguments),
                "target": _v2_public_target(name, arguments),
                "before": None,
                "after": None,
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
                        "message": "The local result contract failed after a possible write.",
                    }
                ],
                "error": {
                    "code": "sync_pending",
                    "reason_code": "public_result_contract_failed",
                    "message": message[:2000],
                    "retryable": True,
                },
            },
            "unknown",
        )
    return _v2_pre_dispatch_failure(
        name,
        arguments,
        code="unexpected_error",
        reason_code="public_result_contract_failed",
        message=message,
        retryable=False,
    )


def call_tool(name: str, raw_arguments: Any) -> dict[str, Any]:
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
                facade = (
                    _v2_core_facade()
                    if name in _V2_CORE_TOOLS
                    else _v2_native_facade()
                )
                payload = facade.call(name, arguments)
                mutation_state = (
                    _v2_mutation_state(payload)
                    if name in V2_MUTATION_TOOLS
                    else None
                )
            except Exception as exc:
                payload, mutation_state = _v2_contract_failure(
                    name,
                    arguments,
                    may_have_mutated=name in V2_MUTATION_TOOLS,
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
            may_have_mutated=exc.may_have_mutated
            or mutation_state in {"committed", "unknown"},
            message=str(exc),
        )
        payload = validate_public_result(name, payload, mutation_state)
    return tool_result(payload, is_error=payload.get("ok") is not True)


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


def handle_message(message: Any) -> dict[str, Any] | None:
    global SESSION_INITIALIZED

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
        SESSION_INITIALIZED = True
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
    if not SESSION_INITIALIZED:
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
        return jsonrpc_result(request_id, call_tool(name, params.get("arguments", {})))

    if request_id is None:
        return None
    return jsonrpc_error(request_id, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main(*, backend_paths: BackendPaths | None = None) -> int:
    global _ACTIVE_BACKEND_PATHS, _V2_CORE_FACADE, _V2_NATIVE_FACADE

    previous_backend_paths = _ACTIVE_BACKEND_PATHS
    previous_core_facade = _V2_CORE_FACADE
    previous_native_facade = _V2_NATIVE_FACADE
    _ACTIVE_BACKEND_PATHS = backend_paths or DEFAULT_BACKEND_PATHS
    _V2_CORE_FACADE = None
    _V2_NATIVE_FACADE = None
    try:
        for raw_line in sys.stdin:
            if len(raw_line.encode("utf-8", errors="replace")) > MAX_MCP_MESSAGE_BYTES:
                send(
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
                send(jsonrpc_error(None, JSONRPC_PARSE_ERROR, "Parse error"))
                continue
            try:
                response = handle_message(message)
            except Exception as exc:  # keep protocol stdout valid even on an internal defect
                request_id = message.get("id") if isinstance(message, dict) else None
                print(f"{SERVER_NAME}: {type(exc).__name__}", file=sys.stderr)
                response = jsonrpc_error(
                    request_id,
                    JSONRPC_INTERNAL_ERROR,
                    "Internal server error",
                )
            if response is not None:
                send(response)
        return 0
    finally:
        _ACTIVE_BACKEND_PATHS = previous_backend_paths
        _V2_CORE_FACADE = previous_core_facade
        _V2_NATIVE_FACADE = previous_native_facade


if __name__ == "__main__":
    raise SystemExit(main())
