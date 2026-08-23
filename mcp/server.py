#!/usr/bin/env python3
"""Dependency-light stdio MCP shim for the bundled Reminders backends.

The public EventKit bridge and private-feature adapter own Reminders business
logic. This module owns the transport boundary: typed inputs, exact selectors,
bounded/redacted outputs, JSON-RPC framing, and subprocess isolation.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from receipt_contract import (  # noqa: E402
    FAILURE_RECEIPT_STATUSES,
    RECEIPT_OBJECT_FIELDS as CONTRACT_RECEIPT_OBJECT_FIELDS,
    SUCCESS_RECEIPT_STATUSES,
    adapter_receipt_error,
)


SERVER_NAME = "apple-reminders-local"
SERVER_TITLE = "Apple Reminders"
SERVER_VERSION = "0.2.0"
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
SOURCE_TEST_GATE = PLUGIN_ROOT / "tests" / "test_mcp_server.py"
TEST_MODE_ENV = "APPLE_REMINDERS_MCP_TEST_MODE"

ADAPTER_TIMEOUT_SECONDS = 45
EVENTKIT_BRIDGE_TIMEOUT_SECONDS = 60
MAX_ADAPTER_STDOUT_BYTES = 2_000_000
MAX_EVENTKIT_REQUEST_BYTES = 1_000_000
MAX_MCP_MESSAGE_BYTES = 2_000_000
MAX_RESULT_STRING_CHARS = 65_536
MAX_RESULT_ARRAY_ITEMS = 200
MAX_CALLS_PER_MINUTE = 120

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
    positional: tuple[str, ...] = ()
    options: tuple[tuple[str, str], ...] = ()
    booleans: tuple[tuple[str, str, str | None], ...] = ()
    bounded_array: str | None = None


ROUTES: dict[str, ToolRoute] = {
    "list_reminder_sections": ToolRoute(
        command="list_sections",
        options=(("list_name", "--list"), ("limit", "--limit")),
        bounded_array="sections",
    ),
    "list_reminder_tags": ToolRoute(
        command="list_tags",
        options=(("query", "--query"), ("limit", "--limit")),
    ),
    "preview_unused_reminder_tags": ToolRoute(
        command="cleanup_tags",
        options=(
            ("tag", "--tag"),
            ("prefix", "--prefix"),
            ("account_id", "--account-id"),
            ("limit", "--limit"),
        ),
    ),
    "cleanup_unused_reminder_tags": ToolRoute(
        command="cleanup_tags",
        static=("--apply",),
        options=(
            ("tag", "--tag"),
            ("prefix", "--prefix"),
            ("account_id", "--account-id"),
            ("candidate_digest", "--preview-digest"),
            ("limit", "--limit"),
        ),
    ),
    "create_reminder_list": ToolRoute(
        command="create_list",
        options=(("name", "--name"), ("color", "--color"), ("emblem", "--emblem")),
    ),
    "show_reminder": ToolRoute(
        command="show_reminder",
        options=(("reminder_id", "--id"),),
    ),
    "purge_reminder_plugin_logs": ToolRoute(command="purge_logs"),
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
        bounded_array="attachments",
    ),
    "audit_reminder_attachments": ToolRoute(
        command="audit_attachments",
        options=(("search", "--search"), ("list_name", "--list"), ("limit", "--limit")),
        booleans=(("problems_only", "--problems-only", None),),
    ),
    "preview_reminder_attachment_repairs": ToolRoute(
        command="repair_attachments",
        options=(("search", "--search"), ("list_name", "--list"), ("limit", "--limit")),
    ),
    "apply_reminder_attachment_repairs": ToolRoute(
        command="repair_attachments",
        static=("--apply",),
        options=(
            ("search", "--search"),
            ("list_name", "--list"),
            ("limit", "--limit"),
            ("candidate_digest", "--preview-digest"),
        ),
        booleans=(("no_backup", "--no-backup", None),),
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


# Public EventKit is the standard surface for list/reminder reads. Private-store
# adapter routes remain only for features EventKit does not expose.
EVENTKIT_READ_ROUTES: dict[str, str] = {
    "list_reminder_accounts": "list_accounts",
    "list_reminder_lists": "list_calendars",
    "fetch_reminders": "fetch_reminders",
    "read_reminder": "read_reminder",
}


EVENTKIT_CONTROL_ROUTES: dict[str, str] = {
    "get_reminders_capabilities": "capabilities",
    "request_reminders_access": "request_access",
}


EVENTKIT_MUTATION_ROUTES: dict[str, str] = {
    "create_reminder": "create_reminder",
    "update_reminder": "update_reminder",
    "complete_reminder": "complete_reminder",
    "reopen_reminder": "reopen_reminder",
    "move_reminder_to_list": "move_reminder",
    "delete_reminder": "delete_reminder",
}


DOCTOR_TOOLS = {"reminders_plugin_doctor"}


def load_tools(path: Path = TOOLS_SCHEMA_PATH) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != 1 or not isinstance(payload.get("tools"), list):
        raise RuntimeError("Unsupported MCP tool schema document")
    tools = payload["tools"]
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise RuntimeError("Every MCP tool must have a string name")
        name = tool["name"]
        if name in names:
            raise RuntimeError(f"Duplicate MCP tool name: {name}")
        if (
            name not in ROUTES
            and name not in EVENTKIT_READ_ROUTES
            and name not in EVENTKIT_CONTROL_ROUTES
            and name not in EVENTKIT_MUTATION_ROUTES
            and name not in DOCTOR_TOOLS
        ):
            raise RuntimeError(f"MCP tool has no local route: {name}")
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise RuntimeError(f"MCP tool input schema must be an object: {name}")
        if schema.get("additionalProperties") is not False:
            raise RuntimeError(f"MCP tool input schema must reject unknown fields: {name}")
        names.add(name)
    missing = (
        set(ROUTES)
        | set(EVENTKIT_READ_ROUTES)
        | set(EVENTKIT_CONTROL_ROUTES)
        | set(EVENTKIT_MUTATION_ROUTES)
        | DOCTOR_TOOLS
    ) - names
    if missing:
        raise RuntimeError(f"Local routes have no MCP tool definitions: {sorted(missing)}")
    return tools


TOOLS = load_tools()
TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}
MUTATION_TOOLS = {
    tool["name"]
    for tool in TOOLS
    if tool.get("annotations", {}).get("readOnlyHint") is not True
}
RECEIPT_STATUSES = set(SUCCESS_RECEIPT_STATUSES)
FAILED_RECEIPT_STATUSES = set(FAILURE_RECEIPT_STATUSES)
RECEIPT_OBJECT_FIELDS = set(CONTRACT_RECEIPT_OBJECT_FIELDS)
RECENT_CALLS: deque[float] = deque()
SESSION_INITIALIZED = False
_ADAPTER_MODULE: Any | None = None
_EVENTKIT_BRIDGE_MODULE: Any | None = None


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
    if tool_name == "update_reminder":
        patch = arguments.get("patch", {})
        if patch.get("recurrence_rules") and "due" in patch and patch["due"] is None:
            raise ToolInputError("recurrence_rules cannot be set while clearing due")
    if tool_name == "cleanup_unused_reminder_tags" and not (
        arguments.get("tag") or arguments.get("prefix")
    ):
        raise ToolInputError("cleanup requires tag or prefix from the preview scope")
    if tool_name == "fetch_reminders":
        has_calendar_bound = bool(arguments.get("calendar_ids"))
        has_incomplete_due_bound = (
            arguments.get("status", "incomplete") == "incomplete"
            and "due_start" in arguments
            and "due_end" in arguments
        )
        has_completed_range_bound = (
            arguments.get("status") == "completed"
            and "completion_start" in arguments
            and "completion_end" in arguments
        )
        if not (has_calendar_bound or has_incomplete_due_bound or has_completed_range_bound):
            raise ToolInputError(
                "fetch_reminders requires calendar_ids, an incomplete due_start/due_end range, "
                "or a completed completion_start/completion_end range"
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
    for name in route.positional:
        argv.append(str(arguments[name]))
    for name, flag in route.options:
        if name in arguments:
            argv.extend([flag, str(arguments[name])])
    for name, true_flag, false_flag in route.booleans:
        if name not in arguments:
            continue
        if arguments[name] is True:
            argv.append(true_flag)
        elif false_flag is not None:
            argv.append(false_flag)
    return argv


def _normalize_list(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    if "ZCKIDENTIFIER" not in item and "ZNAME" not in item:
        return item
    return {
        "id": item.get("ZCKIDENTIFIER"),
        "name": item.get("ZNAME"),
        "is_group": bool(item.get("ZISGROUP")),
        "reminder_count": item.get("reminder_count"),
    }


def _normalize_section(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    if "ZCKIDENTIFIER" not in item and "ZDISPLAYNAME" not in item:
        return item
    return {
        "id": item.get("ZCKIDENTIFIER"),
        "name": item.get("ZDISPLAYNAME"),
        "list_name": item.get("list_name"),
    }


def normalize_route_payload(
    tool_name: str,
    payload: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    route = ROUTES[tool_name]
    limit = int(arguments.get("limit", MAX_RESULT_ARRAY_ITEMS))
    if route.bounded_array and isinstance(payload.get(route.bounded_array), list):
        original = payload[route.bounded_array]
        if route.bounded_array == "lists":
            original = [_normalize_list(item) for item in original]
        elif route.bounded_array == "sections":
            original = [_normalize_section(item) for item in original]
        payload[route.bounded_array] = original[:limit]
        payload["returned"] = len(payload[route.bounded_array])
        payload["truncated"] = bool(payload.get("truncated")) or len(original) > limit

    if tool_name == "create_reminder_list" and isinstance(payload.get("list"), dict):
        payload["list"] = _normalize_list(payload["list"])
    if tool_name == "create_reminder_section" and isinstance(payload.get("section"), dict):
        payload["section"] = _normalize_section(payload["section"])
    if tool_name == "read_reminder" and isinstance(payload.get("reminder"), dict):
        reminder = payload["reminder"]
        # The adapter exposes one stable normalized attachment list plus two
        # legacy raw-SQL lists. Keep only the normalized surface for MCP.
        reminder.pop("attachments", None)
        reminder.pop("url_attachments", None)
    return payload


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
    truncations: list[str] = []
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
            if len(value) > MAX_RESULT_ARRAY_ITEMS:
                truncations.append(path)
            return [walk(item, f"{path}[{index}]") for index, item in enumerate(value[:MAX_RESULT_ARRAY_ITEMS])]
        if isinstance(value, str):
            cleaned = value.replace(home, "~") if home else value
            if len(cleaned) > MAX_RESULT_STRING_CHARS:
                truncations.append(path)
                return cleaned[:MAX_RESULT_STRING_CHARS]
            return cleaned
        return value

    cleaned = walk(payload, "")
    if truncations:
        cleaned["_mcp"] = {
            "truncated": True,
            "truncated_fields": sorted(set(truncations)),
        }
    return cleaned


def _backend_path(default: Path, override_env: str) -> Path:
    # Release archives intentionally omit tests/, so packaged runtime cannot
    # redirect a reviewed backend through process environment. Source-level
    # integration tests opt in explicitly and keep their substitutes local to
    # the disposable test checkout.
    if os.environ.get(TEST_MODE_ENV) != "1" or not SOURCE_TEST_GATE.is_file():
        return default
    configured = os.environ.get(override_env)
    return Path(configured).expanduser().resolve() if configured else default


def adapter_path() -> Path:
    return _backend_path(DEFAULT_ADAPTER_PATH, "APPLE_REMINDERS_ADAPTER_PATH")


def eventkit_bridge_path() -> Path:
    return _backend_path(
        DEFAULT_EVENTKIT_BRIDGE_PATH,
        "APPLE_REMINDERS_EVENTKIT_BRIDGE_PATH",
    )


def doctor_path() -> Path:
    return _backend_path(DEFAULT_DOCTOR_PATH, "APPLE_REMINDERS_DOCTOR_PATH")


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


EVENTKIT_CURSOR_FIELDS = (
    "calendar_ids",
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


def eventkit_cursor_fingerprint(arguments: dict[str, Any]) -> str:
    immutable: dict[str, Any] = {
        name: arguments[name]
        for name in EVENTKIT_CURSOR_FIELDS
        if name in arguments
    }
    if isinstance(immutable.get("calendar_ids"), list):
        immutable["calendar_ids"] = sorted(immutable["calendar_ids"])
    encoded = json.dumps(
        immutable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def encode_eventkit_cursor(offset: int, arguments: dict[str, Any]) -> str:
    cursor_payload = {
        "v": 1,
        "o": offset,
        "f": eventkit_cursor_fingerprint(arguments),
    }
    raw = json.dumps(cursor_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_eventkit_cursor(cursor: str, arguments: dict[str, Any]) -> int:
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolInputError("cursor is not a valid EventKit pagination cursor") from exc
    if not isinstance(decoded, dict) or set(decoded) != {"v", "o", "f"}:
        raise ToolInputError("cursor has an unsupported structure")
    offset = decoded.get("o")
    if decoded.get("v") != 1 or not isinstance(offset, int) or isinstance(offset, bool):
        raise ToolInputError("cursor has an unsupported version or offset")
    if offset < 0 or offset > 10_000:
        raise ToolInputError("cursor offset is outside the supported range")
    expected_fingerprint = eventkit_cursor_fingerprint(arguments)
    if decoded.get("f") != expected_fingerprint:
        raise ToolInputError("cursor cannot be reused with changed filters, sort, or page size")
    if encode_eventkit_cursor(offset, arguments) != cursor:
        raise ToolInputError("cursor is not in canonical form")
    return offset


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


def invoke_doctor() -> tuple[dict[str, Any], bool]:
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
            [sys.executable, str(path), "--compact"],
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
        cursor = bridge_arguments.pop("cursor", None)
        bridge_arguments["offset"] = (
            decode_eventkit_cursor(cursor, arguments) if cursor is not None else 0
        )
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


def normalize_eventkit_payload(
    operation: str,
    payload: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return payload
    limit = int(arguments.get("limit", MAX_RESULT_ARRAY_ITEMS))
    original = data["items"]
    data["items"] = original[:limit]
    data["limit"] = limit
    data["returned"] = len(data["items"])
    if len(original) > limit:
        data["truncated"] = True
    if operation == "fetch_reminders":
        next_offset = data.pop("next_offset", None)
        data.pop("offset", None)
        if (
            data.get("has_more") is True
            and isinstance(next_offset, int)
            and not isinstance(next_offset, bool)
            and 0 <= next_offset <= 10_000
        ):
            data["next_cursor"] = encode_eventkit_cursor(next_offset, arguments)
        else:
            data["next_cursor"] = None
        if data.get("has_more") is True and data["next_cursor"] is None:
            data["pagination_exhausted"] = True
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


def tool_result(payload: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": is_error,
    }


def tool_execution_error(code: str, message: str) -> dict[str, Any]:
    return tool_result(
        {"ok": False, "error": {"code": code, "message": message}},
        is_error=True,
    )


def call_tool(name: str, raw_arguments: Any) -> dict[str, Any]:
    tool = TOOLS_BY_NAME[name]
    try:
        supplied = validate_arguments(tool, raw_arguments)
    except ToolInputError as exc:
        return tool_execution_error("invalid_arguments", str(exc))
    if not rate_limit_allows_call():
        return tool_execution_error(
            "rate_limited",
            "Too many local Reminders tool calls were requested; wait briefly and retry.",
        )
    arguments = effective_arguments(tool, supplied)
    if name in DOCTOR_TOOLS:
        payload, is_error = invoke_doctor()
        payload = sanitize_payload(payload)
        return tool_result(payload, is_error=is_error)
    if name in EVENTKIT_CONTROL_ROUTES:
        operation = EVENTKIT_CONTROL_ROUTES[name]
        payload, is_error = invoke_eventkit_bridge(operation, arguments)
        payload = sanitize_payload(payload)
        return tool_result(payload, is_error=is_error)
    if name in EVENTKIT_READ_ROUTES:
        operation = EVENTKIT_READ_ROUTES[name]
        try:
            payload, is_error = invoke_eventkit_bridge(operation, arguments)
        except ToolInputError as exc:
            return tool_execution_error("invalid_arguments", str(exc))
        payload = normalize_eventkit_payload(operation, payload, arguments)
        payload = sanitize_payload(payload)
        return tool_result(payload, is_error=is_error)
    if name in EVENTKIT_MUTATION_ROUTES:
        operation = EVENTKIT_MUTATION_ROUTES[name]
        payload, is_error = invoke_eventkit_mutation(name, operation, arguments)
        if payload.get("ok") is True:
            try:
                bundled_eventkit_bridge_module().validate_mutation_receipt(payload, operation)
            except RuntimeError as exc:
                return tool_execution_error("invalid_eventkit_receipt", str(exc))
        payload = sanitize_payload(payload)
        return tool_result(payload, is_error=is_error)
    argv = build_adapter_argv(name, arguments)
    payload, is_error = invoke_adapter(argv)
    has_required_receipt_status = (
        payload.get("ok") is True
        or payload.get("status") in FAILED_RECEIPT_STATUSES
    )
    if has_required_receipt_status and name in MUTATION_TOOLS:
        receipt_error = validate_adapter_receipt(
            payload,
            expected_operation=ROUTES[name].command,
        )
        if receipt_error:
            return tool_execution_error("invalid_adapter_receipt", receipt_error)
    payload = normalize_route_payload(name, payload, arguments)
    payload = sanitize_payload(payload)
    return tool_result(payload, is_error=is_error)


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
                    "Start with reminders_plugin_doctor and get_reminders_capabilities. Call "
                    "request_reminders_access only when the user expects a macOS permission prompt. "
                    "Use bounded public EventKit reads to discover stable IDs and last_modified values, "
                    "then use exact IDs and concurrency preconditions for mutations. Private-only tag, "
                    "section, and attachment tools may sync changes through iCloud. "
                    "Unused-tag cleanup and attachment repair require an untruncated preview, its exact "
                    "digest, and the same scope."
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


def main() -> int:
    for raw_line in sys.stdin:
        if len(raw_line.encode("utf-8", errors="replace")) > MAX_MCP_MESSAGE_BYTES:
            send(jsonrpc_error(None, JSONRPC_INVALID_REQUEST, "JSON-RPC message exceeds the size limit"))
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
            response = jsonrpc_error(request_id, JSONRPC_INTERNAL_ERROR, "Internal server error")
        if response is not None:
            send(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
