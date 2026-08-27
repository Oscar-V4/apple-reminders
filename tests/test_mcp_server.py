from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
sys.path.insert(0, str(PLUGIN_ROOT))
SERVER_HARNESS = REPO_ROOT / "tests" / "mcp_server_harness.py"
CONFIG = PLUGIN_ROOT / ".mcp.json"
TOOLS_SCHEMA = PLUGIN_ROOT / "schemas" / "mcp-tools.json"

PUBLIC_TOOLS = {
    "request_reminders_access",
    "list_reminder_lists",
    "fetch_reminders",
    "read_reminder",
    "create_reminder",
    "change_reminder",
    "delete_reminder",
    "inspect_recently_deleted",
    "recover_deleted_reminder",
    "inspect_reminder_native",
    "ensure_reminder_list",
    "create_reminder_section",
    "organize_reminder",
    "change_reminder_attachment",
    "diagnose_reminders",
}
CORE_TOOLS = {
    "request_reminders_access",
    "list_reminder_lists",
    "fetch_reminders",
    "read_reminder",
    "create_reminder",
    "change_reminder",
    "delete_reminder",
    "ensure_reminder_list",
}
NATIVE_TOOLS = {
    "inspect_reminder_native",
    "create_reminder_section",
    "organize_reminder",
    "change_reminder_attachment",
}
RECOVERY_TOOLS = {"inspect_recently_deleted", "recover_deleted_reminder"}
DIAGNOSTIC_TOOLS = {"diagnose_reminders"}
REFERENCE_MUTATIONS = {
    "create_reminder",
    "change_reminder",
    "organize_reminder",
    "change_reminder_attachment",
}
REFERENCE = "rev1." + "A" * 32
DELETED_REFERENCE = "del1." + "D" * 32
REMINDER_ID = "REMINDER-EXACT-1"

TEST_BACKEND_ENVIRONMENTS = {
    "adapter": "APPLE_REMINDERS_TEST_HARNESS_ADAPTER_PATH",
    "eventkit_bridge": "APPLE_REMINDERS_TEST_HARNESS_EVENTKIT_BRIDGE_PATH",
    "doctor": "APPLE_REMINDERS_TEST_HARNESS_DOCTOR_PATH",
}


def strict_json_load(path: Path) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )


def initialize(request_id: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "unit-test", "version": "1.0.0"},
        },
    }


def run_server(
    messages: list[dict[str, Any]],
    *,
    adapter_path: Path | None = None,
    eventkit_bridge_path: Path | None = None,
    doctor_path: Path | None = None,
) -> list[dict[str, Any]]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for environment_name in TEST_BACKEND_ENVIRONMENTS.values():
        env.pop(environment_name, None)
    for name, path in (
        ("adapter", adapter_path),
        ("eventkit_bridge", eventkit_bridge_path),
        ("doctor", doctor_path),
    ):
        if path is not None:
            env[TEST_BACKEND_ENVIRONMENTS[name]] = str(path)
    completed = subprocess.run(
        [sys.executable, str(SERVER_HARNESS)],
        cwd=PLUGIN_ROOT,
        env=env,
        input="".join(json.dumps(message) + "\n" for message in messages),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"MCP server exited {completed.returncode}: {completed.stderr}"
        )
    return [
        json.loads(line)
        for line in completed.stdout.splitlines()
        if line.strip()
    ]


def mock_eventkit_bridge(path: Path) -> None:
    path.write_text(
        """from __future__ import annotations
import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
log = Path(__file__).with_suffix(".requests")
previous = log.read_text(encoding="utf-8") if log.exists() else ""
log.write_text(previous + json.dumps(request, sort_keys=True) + "\\n", encoding="utf-8")
operation = request["operation"]
if operation == "list_calendars":
    data = {
        "items": [{
            "id": "LIST-1",
            "title": "Inbox",
            "type": "caldav",
            "allows_content_modifications": True,
            "subscribed": False,
            "immutable": False,
            "source": {
                "id": "SOURCE-1",
                "title": "iCloud",
                "type": "caldav",
                "is_delegate": False,
                "reminder_calendar_count": 1
            }
        }]
    }
elif operation == "fetch_reminders":
    data = {
        "items": [],
        "total_matched": 0,
        "limit": request["limit"],
        "offset": request["offset"],
        "has_more": False,
        "next_offset": None
    }
else:
    raise SystemExit(91)
print(json.dumps({
    "schema_version": 1,
    "operation": operation,
    "status": "verified",
    "ok": True,
    "data": data
}))
""",
        encoding="utf-8",
    )


def mock_doctor(path: Path) -> None:
    path.write_text(
        """from __future__ import annotations
import json
import sys
from pathlib import Path

Path(__file__).with_suffix(".argv").write_text(
    json.dumps(sys.argv[1:]), encoding="utf-8"
)
print(json.dumps({
    "schema_version": 1,
    "doctor": "apple-reminders-doctor",
    "ok": False,
    "status": "blocked",
    "privacy": {
        "content_free": True,
        "reminder_rows_read": False,
        "reminder_titles_read": False,
        "list_section_tag_names_read": False,
        "journal_cache_backup_contents_read": False,
        "permission_prompt_attempted": False,
        "write_attempted": False,
        "application_launched": False,
        "private_framework_loaded": False
    },
    "checks": {
        "permissions": {
            "status": "blocked",
            "code": "permission_denied",
            "message": "Reminders access has not been granted.",
            "details": {
                "authorization": "denied",
                "prompt_attempted": False,
                "nested_private_state": {"must": "not escape"}
            }
        }
    }
}))
""",
        encoding="utf-8",
    )


def public_operation(name: str, arguments: Mapping[str, Any]) -> str:
    action = arguments.get("action")
    kind = action.get("kind") if isinstance(action, Mapping) else None
    if name in {
        "change_reminder",
        "organize_reminder",
        "change_reminder_attachment",
    }:
        return f"{name}.{kind}"
    return name


def valid_public_result(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    operation = public_operation(name, arguments)
    if name not in {
        "create_reminder",
        "change_reminder",
        "delete_reminder",
        "recover_deleted_reminder",
        "ensure_reminder_list",
        "create_reminder_section",
        "organize_reminder",
        "change_reminder_attachment",
    }:
        data: dict[str, Any] = {"fixture": name}
        if name == "request_reminders_access":
            data = {
                "authorization_before": "full_access",
                "authorization": "full_access",
                "request_attempted": True,
                "prompt_expected": False,
                "prompt_observed": None,
                "prompted_explicitly": True,
            }
        elif name == "read_reminder":
            data = {
                "reminder": {
                    "id": arguments["reminder_id"],
                    "title": "Exact projection",
                    "reference": REFERENCE,
                }
            }
        elif name == "inspect_reminder_native":
            data = {
                "kind": "sections",
                "list_id": arguments["list_id"],
                "sections": [],
                "returned": 0,
                "truncated": False,
            }
        elif name == "inspect_recently_deleted":
            data = {
                "kind": "list",
                "items": [],
                "returned": 0,
                "limit": 20,
                "total_matched": 0,
                "truncated": False,
                "has_more": False,
                "next_cursor": None,
                "pagination_exhausted": False,
                "retention_days": 30,
            }
        return {
            "schema_version": 2,
            "ok": True,
            "status": "verified",
            "operation": operation,
            "data": data,
        }

    backend = (
        "eventkit_public_sdk"
        if name
        in {
            "create_reminder",
            "change_reminder",
            "delete_reminder",
            "ensure_reminder_list",
        }
        else "native_extension"
    )
    after: dict[str, Any] = {}
    if name in REFERENCE_MUTATIONS:
        after = {"reminder_id": REMINDER_ID, "reference": REFERENCE}
    verification = {
        "state": "read_back",
        "write_performed": True,
        "final_read": True,
        "matched": True,
    }
    if name == "recover_deleted_reminder":
        verification.update(
            {
                "pre_save_guard_matched": True,
                "destination_list_matched": True,
                "attachments_active": True,
                "attachments_preserved": True,
                "attachment_bytes_verified": True,
                "attachment_counts_match": True,
                "before_attachment_count": 1,
                "native_attachment_count": 1,
                "after_attachment_count": 1,
            }
        )
    return {
        "schema_version": 2,
        "ok": True,
        "status": "verified",
        "operation": operation,
        "operation_id": "11111111-1111-4111-8111-111111111111",
        "backend": backend,
        "target": {},
        "before": {},
        "after": after,
        "verification": verification,
        "recovery": {
            "semantics": "verified_final_read",
            "automatic_retry_safe": False,
        },
    }


class RecordingFacade:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        copied = copy.deepcopy(dict(arguments))
        self.calls.append((name, copied))
        return valid_public_result(name, copied)


VALID_ARGUMENTS: dict[str, dict[str, Any]] = {
    "request_reminders_access": {},
    "list_reminder_lists": {},
    "fetch_reminders": {"list_ids": ["LIST-1"]},
    "read_reminder": {"reminder_id": REMINDER_ID},
    "create_reminder": {
        "list_id": "LIST-1",
        "title": "Create fixture",
        "idempotency_key": "create-key-0001",
    },
    "change_reminder": {
        "reference": REFERENCE,
        "action": {"kind": "patch", "patch": {"title": "Changed"}},
    },
    "delete_reminder": {"reference": REFERENCE},
    "inspect_recently_deleted": {"kind": "list"},
    "recover_deleted_reminder": {
        "reference": DELETED_REFERENCE,
        "list_id": "LIST-1",
        "idempotency_key": "recover-key-0001",
    },
    "inspect_reminder_native": {
        "kind": "sections",
        "list_id": "LIST-1",
    },
    "ensure_reminder_list": {
        "source_id": "SOURCE-1",
        "name": "Inbox",
        "idempotency_key": "ensure-key-0001",
    },
    "create_reminder_section": {"list_id": "LIST-1", "name": "Next"},
    "organize_reminder": {
        "reference": REFERENCE,
        "action": {"kind": "add_tag", "tag": "work"},
    },
    "change_reminder_attachment": {
        "reference": REFERENCE,
        "action": {"kind": "attach_url", "url": "https://example.com/item"},
    },
    "diagnose_reminders": {},
}


class McpPackagingTests(unittest.TestCase):
    def test_config_registers_one_repo_relative_stdio_server(self) -> None:
        config = strict_json_load(CONFIG)

        self.assertEqual(set(config), {"mcpServers"})
        self.assertEqual(set(config["mcpServers"]), {"apple-reminders-local"})
        registered = config["mcpServers"]["apple-reminders-local"]
        self.assertEqual(registered["cwd"], ".")
        self.assertEqual(registered["command"], "/bin/sh")
        self.assertEqual(registered["args"], ["./scripts/launch_mcp.sh"])
        self.assertNotIn("url", registered)

    def test_discovery_is_exact_closed_bounded_and_under_32_kib(self) -> None:
        payload = strict_json_load(TOOLS_SCHEMA)
        tools = payload["tools"]
        names = [tool["name"] for tool in tools]

        self.assertEqual(payload["schemaVersion"], 2)
        self.assertEqual(len(names), 15)
        self.assertEqual(set(names), PUBLIC_TOOLS)
        self.assertEqual(len(names), len(set(names)))
        compact_discovery = json.dumps(
            tools,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertLess(len(compact_discovery), 32 * 1024)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("calendar_id", serialized)
        self.assertNotIn("calendar_ids", serialized)

        def assert_bounded(node: Any, path: str) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(
                        node.get("additionalProperties"),
                        False,
                        f"open object schema at {path}",
                    )
                if node.get("type") == "array":
                    self.assertIn("maxItems", node, f"unbounded array at {path}")
                if node.get("type") == "string":
                    self.assertIn("maxLength", node, f"unbounded string at {path}")
                for key, value in node.items():
                    assert_bounded(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    assert_bounded(value, f"{path}[{index}]")

        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertNotIn("outputSchema", tool)
                self.assertIs(tool["annotations"]["openWorldHint"], False)
                assert_bounded(tool["inputSchema"], f"$.{tool['name']}")

    def test_tool_discovery_does_not_eagerly_import_facade_backends(self) -> None:
        probe = """
import json
import sys
sys.path.insert(0, 'mcp')
import server
server.handle_message({
    'jsonrpc': '2.0',
    'id': 1,
    'method': 'initialize',
    'params': {
        'protocolVersion': '2025-11-25',
        'capabilities': {},
        'clientInfo': {'name': 'lazy-import-probe', 'version': '1'},
    },
})
server.handle_message({
    'jsonrpc': '2.0',
    'id': 2,
    'method': 'tools/list',
    'params': {},
})
print(json.dumps(sorted(
    name for name in (
        'v2_core',
        'v2_core_backend',
        'v2_native',
        'v2_native_backend',
        'v2_recovery',
        'v2_recovery_backend',
        'v2_diagnostics',
        'reminders_service',
    )
    if name in sys.modules
)))
"""
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=PLUGIN_ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), [])


class McpProtocolTests(unittest.TestCase):
    def test_adapter_dispatch_phase_is_private_and_only_marks_launcher_failures(self) -> None:
        with mock.patch.object(
            self.server,
            "adapter_path",
            return_value=Path("/definitely/missing/reminders_adapter.py"),
        ):
            missing, missing_error = self.server.invoke_adapter(["read_reminder"])

        self.assertTrue(missing_error)
        self.assertEqual(missing["__dispatch_phase"], "not_started")
        self.assertNotIn("__dispatch_phase", self.server.sanitize_payload(missing))

        with (
            mock.patch.object(
                self.server,
                "adapter_path",
                return_value=Path(__file__),
            ),
            mock.patch.object(
                self.server.subprocess,
                "run",
                side_effect=OSError("process transport failed"),
            ),
        ):
            process_failed, process_error = self.server.invoke_adapter(
                ["create_reminder"]
            )

        self.assertTrue(process_error)
        self.assertEqual(process_failed["__dispatch_phase"], "started_unknown")

        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"ok":true,"status":"verified"}',
            stderr="",
        )
        with (
            mock.patch.object(
                self.server,
                "adapter_path",
                return_value=Path(__file__),
            ),
            mock.patch.object(self.server.subprocess, "run", return_value=completed),
        ):
            success, success_error = self.server.invoke_adapter(["read_reminder"])

        self.assertFalse(success_error)
        self.assertNotIn("__dispatch_phase", success)

        spoofed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=(
                '{"ok":false,"__dispatch_phase":"not_started",'
                '"error":{"code":"invalid_adapter_response"}}'
            ),
            stderr="",
        )
        with (
            mock.patch.object(
                self.server,
                "adapter_path",
                return_value=Path(__file__),
            ),
            mock.patch.object(self.server.subprocess, "run", return_value=spoofed),
        ):
            child_payload, child_error = self.server.invoke_adapter(["read_reminder"])

        self.assertTrue(child_error)
        self.assertNotIn("__dispatch_phase", child_payload)

    def test_eventkit_dispatch_phase_marks_only_parent_proven_prelaunch_failures(
        self,
    ) -> None:
        with mock.patch.object(
            self.server,
            "eventkit_bridge_path",
            return_value=Path("/definitely/missing/eventkit_bridge.py"),
        ):
            missing, missing_error = self.server.invoke_eventkit_bridge(
                "create_reminder",
                {"calendar_id": "LIST-1", "title": "Missing bridge"},
            )

        self.assertTrue(missing_error)
        self.assertEqual(missing["__dispatch_phase"], "not_started")
        self.assertNotIn("__dispatch_phase", self.server.sanitize_payload(missing))

        with mock.patch.object(
            self.server,
            "eventkit_bridge_path",
            return_value=Path(__file__),
        ):
            too_large, too_large_error = self.server.invoke_eventkit_bridge(
                "create_reminder",
                {
                    "calendar_id": "LIST-1",
                    "title": "x" * self.server.MAX_EVENTKIT_REQUEST_BYTES,
                },
            )

        self.assertTrue(too_large_error)
        self.assertEqual(too_large["__dispatch_phase"], "not_started")

        with (
            mock.patch.object(
                self.server,
                "eventkit_bridge_path",
                return_value=Path(__file__),
            ),
            mock.patch.object(
                self.server.subprocess,
                "run",
                side_effect=OSError("launch denied"),
            ),
        ):
            launch_failed, launch_error = self.server.invoke_eventkit_bridge(
                "create_reminder",
                {"calendar_id": "LIST-1", "title": "Launch failure"},
            )

        self.assertFalse(launch_error)
        self.assertEqual(
            launch_failed["status"],
            "committed_verification_pending",
        )
        self.assertIsNone(launch_failed["verification"]["write_performed"])
        self.assertNotIn("__dispatch_phase", launch_failed)

        spoofed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "schema_version": 1,
                    "operation": "doctor",
                    "status": "verified",
                    "ok": True,
                    "data": {},
                    "__dispatch_phase": "not_started",
                }
            ),
            stderr="",
        )
        with (
            mock.patch.object(
                self.server,
                "eventkit_bridge_path",
                return_value=Path(__file__),
            ),
            mock.patch.object(self.server.subprocess, "run", return_value=spoofed),
        ):
            child_payload, child_error = self.server.invoke_eventkit_bridge(
                "doctor",
                {},
            )

        self.assertFalse(child_error)
        self.assertNotIn("__dispatch_phase", child_payload)

        spoofed_create = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps(
                {
                    "ok": False,
                    "__dispatch_phase": "not_started",
                    "error": {
                        "code": "eventkit_bridge_unavailable",
                        "message": "Child output cannot prove parent launch state.",
                    },
                }
            ),
            stderr="",
        )
        with (
            mock.patch.object(
                self.server,
                "eventkit_bridge_path",
                return_value=Path(__file__),
            ),
            mock.patch.object(
                self.server.subprocess,
                "run",
                return_value=spoofed_create,
            ),
        ):
            create_payload, create_error = self.server.invoke_eventkit_bridge(
                "create_reminder",
                {"calendar_id": "LIST-1", "title": "Spoofed provenance"},
            )

        self.assertFalse(create_error)
        self.assertEqual(
            create_payload["status"],
            "committed_verification_pending",
        )
        self.assertIsNone(create_payload["verification"]["write_performed"])
        self.assertNotIn("__dispatch_phase", create_payload)

    def setUp(self) -> None:
        from mcp import server

        self.server = server
        self.previous_session = server.SESSION_INITIALIZED
        self.previous_core = server._V2_CORE_FACADE
        self.previous_native = server._V2_NATIVE_FACADE
        self.previous_recovery = server._V2_RECOVERY_FACADE
        self.previous_diagnostics = getattr(server, "_V2_DIAGNOSTICS_FACADE", None)
        self.previous_paths = server._ACTIVE_BACKEND_PATHS
        server.SESSION_INITIALIZED = False
        server._V2_CORE_FACADE = None
        server._V2_NATIVE_FACADE = None
        server._V2_RECOVERY_FACADE = None
        server._V2_DIAGNOSTICS_FACADE = None
        server._ACTIVE_BACKEND_PATHS = server.DEFAULT_BACKEND_PATHS
        server.RECENT_CALLS.clear()

    def tearDown(self) -> None:
        self.server.SESSION_INITIALIZED = self.previous_session
        self.server._V2_CORE_FACADE = self.previous_core
        self.server._V2_NATIVE_FACADE = self.previous_native
        self.server._V2_RECOVERY_FACADE = self.previous_recovery
        self.server._V2_DIAGNOSTICS_FACADE = self.previous_diagnostics
        self.server._ACTIVE_BACKEND_PATHS = self.previous_paths
        self.server.RECENT_CALLS.clear()

    def test_initialize_tool_list_and_ping_over_stdio(self) -> None:
        responses = run_server(
            [
                initialize(),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
            ]
        )

        self.assertEqual([response["id"] for response in responses], [1, 2, 3])
        initialized = responses[0]["result"]
        self.assertEqual(initialized["protocolVersion"], "2025-11-25")
        self.assertEqual(initialized["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(initialized["serverInfo"]["name"], "apple-reminders-local")
        self.assertEqual(
            {tool["name"] for tool in responses[1]["result"]["tools"]},
            PUBLIC_TOOLS,
        )
        self.assertTrue(
            all(
                isinstance(tool.get("title"), str) and tool["title"].strip()
                for tool in responses[1]["result"]["tools"]
            )
        )
        self.assertEqual(responses[2]["result"], {})

    def test_tools_are_unavailable_before_initialize(self) -> None:
        response = self.server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )

        self.assertEqual(response["error"]["code"], -32002)

    def test_notification_shaped_mutation_cannot_execute(self) -> None:
        facade = RecordingFacade()
        self.server._V2_CORE_FACADE = facade
        self.server.handle_message(initialize())

        response = self.server.handle_message(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "delete_reminder",
                    "arguments": {"reference": REFERENCE},
                },
            }
        )

        self.assertEqual(response["error"]["code"], -32600)
        self.assertEqual(facade.calls, [])

    def test_unknown_tool_is_a_protocol_error(self) -> None:
        self.server.handle_message(initialize())

        response = self.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "old_write_route", "arguments": {}},
            }
        )

        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("Unknown tool", response["error"]["message"])

    def test_invalid_read_and_mutation_arguments_fail_before_facade_dispatch(self) -> None:
        core = RecordingFacade()
        native = RecordingFacade()
        self.server._V2_CORE_FACADE = core
        self.server._V2_NATIVE_FACADE = native

        read_result = self.server.call_tool("read_reminder", {})
        mutation_result = self.server.call_tool(
            "create_reminder", {"list_id": "LIST-1", "title": "Missing key"}
        )

        self.assertEqual(core.calls, [])
        self.assertEqual(native.calls, [])
        read_payload = read_result["structuredContent"]
        self.assertTrue(read_result["isError"])
        self.assertEqual(
            set(read_payload),
            {"schema_version", "ok", "status", "operation", "error"},
        )
        self.assertEqual(read_payload["status"], "failed_no_mutation")
        self.assertEqual(read_payload["error"]["code"], "invalid_input")

        mutation_payload = mutation_result["structuredContent"]
        self.assertTrue(mutation_result["isError"])
        self.assertEqual(mutation_payload["schema_version"], 2)
        self.assertEqual(mutation_payload["status"], "failed_no_mutation")
        self.assertIs(mutation_payload["verification"]["write_performed"], False)
        self.assertIs(mutation_payload["verification"]["final_read"], False)
        self.assertEqual(mutation_payload["error"]["code"], "invalid_input")
        for field in (
            "operation_id",
            "backend",
            "target",
            "before",
            "after",
            "verification",
            "recovery",
        ):
            self.assertIn(field, mutation_payload)

    def test_unsupported_python_fails_explicitly_before_facade_dispatch(self) -> None:
        core = RecordingFacade()
        self.server._V2_CORE_FACADE = core

        with mock.patch.object(self.server.sys, "version_info", (3, 10, 14)):
            result = self.server.call_tool(
                "read_reminder",
                {"reminder_id": REMINDER_ID},
            )

        payload = result["structuredContent"]
        self.assertTrue(result["isError"])
        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertEqual(payload["error"]["code"], "unsupported_capability")
        self.assertEqual(
            payload["error"]["reason_code"],
            "unsupported_python_runtime",
        )
        self.assertIn("Python 3.11", payload["error"]["message"])
        self.assertEqual(core.calls, [])

    def test_change_patch_cannot_add_recurrence_while_clearing_due(self) -> None:
        core = RecordingFacade()
        self.server._V2_CORE_FACADE = core

        result = self.server.call_tool(
            "change_reminder",
            {
                "reference": REFERENCE,
                "action": {
                    "kind": "patch",
                    "patch": {
                        "due": None,
                        "recurrence_rules": [
                            {"frequency": "daily", "interval": 1}
                        ],
                    },
                },
            },
        )

        payload = result["structuredContent"]
        self.assertTrue(result["isError"])
        self.assertEqual(payload["error"]["code"], "invalid_input")
        self.assertEqual(core.calls, [])

    def test_all_15_tools_dispatch_to_their_v2_facades(self) -> None:
        core = RecordingFacade()
        native = RecordingFacade()
        recovery = RecordingFacade()
        diagnostics = RecordingFacade()
        self.server._V2_CORE_FACADE = core
        self.server._V2_NATIVE_FACADE = native
        self.server._V2_RECOVERY_FACADE = recovery
        self.server._V2_DIAGNOSTICS_FACADE = diagnostics

        for name in sorted(PUBLIC_TOOLS):
            with self.subTest(tool=name):
                result = self.server.call_tool(name, copy.deepcopy(VALID_ARGUMENTS[name]))
                self.assertFalse(result["isError"], result)
                payload = result["structuredContent"]
                self.assertEqual(payload["schema_version"], 2)
                self.assertEqual(
                    payload["operation"],
                    public_operation(name, VALID_ARGUMENTS[name]),
                )

        self.assertEqual({name for name, _ in core.calls}, CORE_TOOLS)
        self.assertEqual(
            {name for name, _ in native.calls},
            NATIVE_TOOLS,
        )
        self.assertEqual(
            {name for name, _ in diagnostics.calls},
            DIAGNOSTIC_TOOLS,
        )
        self.assertEqual(
            {name for name, _ in recovery.calls},
            RECOVERY_TOOLS,
        )
        self.assertEqual(
            len(core.calls)
            + len(native.calls)
            + len(recovery.calls)
            + len(diagnostics.calls),
            15,
        )

    def test_post_dispatch_contract_fallback_uses_exact_safe_recovery(self) -> None:
        cases = (
            ("create_reminder", "fetch_reminders"),
            ("ensure_reminder_list", "list_reminder_lists"),
            ("create_reminder_section", "inspect_reminder_native"),
            ("delete_reminder", "read_reminder"),
            ("recover_deleted_reminder", "read_reminder"),
        )
        for name, recovery_tool in cases:
            facade = mock.Mock()
            facade.call.side_effect = RuntimeError("lost public facade result")
            self.server._V2_CORE_FACADE = facade
            self.server._V2_NATIVE_FACADE = facade
            self.server._V2_RECOVERY_FACADE = facade
            self.server.RECENT_CALLS.clear()

            with self.subTest(tool=name):
                result = self.server.call_tool(
                    name, copy.deepcopy(VALID_ARGUMENTS[name])
                )
                payload = result["structuredContent"]
                self.assertFalse(result["isError"])
                self.assertEqual(
                    payload["status"], "committed_verification_pending"
                )
                self.assertFalse(payload["error"]["retryable"])
                self.assertEqual(payload["next_action"]["tool"], recovery_tool)
                self.assertFalse(
                    payload["next_action"]["retry_original_once"]
                )
                summary = json.loads(result["content"][0]["text"])
                self.assertEqual(summary["outcome"], "attention_required")
                self.assertTrue(summary["needs_attention"])
                self.assertTrue(summary["may_have_mutated"])
                self.assertEqual(summary["write_state"], "unknown")
                self.assertEqual(summary["evidence_scope"], "no_final_read")
                self.assertEqual(
                    summary["next_read_only_action"]["tool"], recovery_tool
                )

    def test_recovery_without_attachment_proof_uses_safe_contract_fallback(self) -> None:
        facade = mock.Mock()
        unsafe = valid_public_result(
            "recover_deleted_reminder",
            VALID_ARGUMENTS["recover_deleted_reminder"],
        )
        unsafe["verification"]["attachments_preserved"] = False
        facade.call.return_value = unsafe
        self.server._V2_RECOVERY_FACADE = facade
        self.server.RECENT_CALLS.clear()

        result = self.server.call_tool(
            "recover_deleted_reminder",
            copy.deepcopy(VALID_ARGUMENTS["recover_deleted_reminder"]),
        )

        payload = result["structuredContent"]
        self.assertFalse(result["isError"])
        self.assertEqual(payload["status"], "committed_verification_pending")
        self.assertEqual(
            payload["error"]["reason_code"], "public_result_contract_failed"
        )
        self.assertEqual(payload["next_action"]["tool"], "read_reminder")
        summary = json.loads(result["content"][0]["text"])
        self.assertEqual(summary["outcome"], "attention_required")
        self.assertTrue(summary["needs_attention"])
        self.assertEqual(
            summary["next_read_only_action"]["tool"], "read_reminder"
        )

    def test_recovery_preserves_independent_mutation_state_across_facade_boundary(
        self,
    ) -> None:
        arguments = copy.deepcopy(VALID_ARGUMENTS["recover_deleted_reminder"])
        false_no_write, _ = self.server._v2_pre_dispatch_failure(
            "recover_deleted_reminder",
            arguments,
            code="unexpected_error",
            reason_code="fault_injected_no_mutation_claim",
            message="Fault-injected receipt conflicts with the mutation fact.",
            retryable=False,
        )

        class ConflictingStateFacade:
            def __init__(self) -> None:
                self.calls = 0

            def call_with_state(
                self,
                name: str,
                supplied: Mapping[str, Any],
            ) -> tuple[dict[str, Any], str]:
                self.calls += 1
                self.name = name
                self.supplied = copy.deepcopy(dict(supplied))
                return copy.deepcopy(false_no_write), "committed"

            def call(self, name: str, supplied: Mapping[str, Any]) -> dict[str, Any]:
                raise AssertionError("server discarded the independent mutation state")

        facade = ConflictingStateFacade()
        self.server._V2_RECOVERY_FACADE = facade
        self.server.RECENT_CALLS.clear()

        result = self.server.call_tool("recover_deleted_reminder", arguments)

        payload = result["structuredContent"]
        self.assertEqual(facade.calls, 1)
        self.assertEqual(facade.name, "recover_deleted_reminder")
        self.assertEqual(payload["status"], "committed_verification_pending")
        self.assertEqual(
            payload["error"]["reason_code"], "public_result_contract_failed"
        )
        self.assertIsNone(payload["verification"]["write_performed"])
        self.assertEqual(payload["next_action"]["tool"], "read_reminder")
        self.assertNotIn("mutation_state", payload)
        summary = json.loads(result["content"][0]["text"])
        self.assertEqual(summary["outcome"], "attention_required")
        self.assertEqual(summary["write_state"], "unknown")

    def test_valid_long_notes_cross_the_public_result_boundary_unchanged(self) -> None:
        notes = "n" * 70_000
        facade = mock.Mock()
        facade.call.return_value = {
            "schema_version": 2,
            "ok": True,
            "status": "verified",
            "operation": "read_reminder",
            "data": {
                "reminder": {
                    "id": REMINDER_ID,
                    "title": "Long notes fixture",
                    "notes": notes,
                    "reference": REFERENCE,
                }
            },
        }
        self.server._V2_CORE_FACADE = facade

        result = self.server.call_tool(
            "read_reminder",
            {"reminder_id": REMINDER_ID},
        )

        self.assertFalse(result["isError"], result)
        payload = result["structuredContent"]
        self.assertNotIn("_mcp", payload)
        self.assertEqual(payload["data"]["reminder"]["notes"], notes)

    def test_text_content_is_a_concise_content_free_summary(self) -> None:
        payload = {
            "schema_version": 2,
            "ok": True,
            "status": "verified",
            "operation": "fetch_reminders",
            "data": {
                "items": [{"title": "private reminder title"}],
                "returned": 1,
                "truncated": False,
                "has_more": False,
            },
        }

        result = self.server.tool_result(payload, is_error=False)
        text = result["content"][0]["text"]

        self.assertEqual(result["structuredContent"], payload)
        self.assertLess(len(text), 512)
        self.assertNotIn("private reminder title", text)
        self.assertNotEqual(text, json.dumps(payload, ensure_ascii=False))
        summary = json.loads(text)
        self.assertEqual(summary["returned"], 1)
        self.assertEqual(summary["outcome"], "verified")
        self.assertFalse(summary["needs_attention"])
        self.assertFalse(summary["may_have_mutated"])
        self.assertEqual(summary["write_state"], "not_applicable")
        self.assertEqual(summary["evidence_scope"], "bounded_public_read")
        self.assertIsNone(summary["next_read_only_action"])

    def test_text_summary_reports_exact_verified_write_evidence(self) -> None:
        payload = {
            "schema_version": 2,
            "ok": True,
            "status": "verified",
            "operation": "organize_reminder.add_tag",
            "target": {
                "reminder_id": REMINDER_ID,
                "section_id": "SECTION-1",
                "tag": "private tag",
            },
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
                "matched": True,
            },
        }

        summary = json.loads(
            self.server.tool_result(payload, is_error=False)["content"][0]["text"]
        )

        self.assertEqual(summary["outcome"], "verified")
        self.assertFalse(summary["needs_attention"])
        self.assertTrue(summary["may_have_mutated"])
        self.assertEqual(summary["write_state"], "committed_and_verified")
        self.assertEqual(summary["evidence_scope"], "matched_exact_final_read")
        self.assertEqual(
            summary["target"],
            {"reminder_id": REMINDER_ID, "section_id": "SECTION-1"},
        )
        self.assertNotIn("private tag", json.dumps(summary))
        self.assertIsNone(summary["next_read_only_action"])

    def test_text_summary_marks_pending_partial_stale_and_failure_for_attention(self) -> None:
        cases = (
            (
                "pending_unknown_write",
                "committed_verification_pending",
                True,
                "unknown",
                None,
                False,
                "sync_pending",
            ),
            (
                "pending_known_write",
                "committed_verification_pending",
                True,
                "committed_unverified",
                True,
                False,
                "sync_pending",
            ),
            (
                "partial",
                "partial_success",
                True,
                "partial",
                True,
                True,
                "sync_pending",
            ),
            (
                "stale",
                "failed_no_mutation",
                False,
                "not_mutated",
                False,
                False,
                "concurrent_modification",
            ),
            (
                "manual_repair",
                "failed_manual_repair_required",
                True,
                "committed_manual_repair_required",
                True,
                False,
                "unexpected_error",
            ),
        )
        for (
            case_name,
            status,
            may_mutate,
            write_state,
            write_performed,
            final_read,
            error_code,
        ) in cases:
            payload = {
                "schema_version": 2,
                "ok": status in {"committed_verification_pending", "partial_success"},
                "status": status,
                "operation": "change_reminder.patch",
                "target": {"reminder_id": REMINDER_ID},
                "verification": {
                    "state": "partial" if final_read else "pending",
                    "write_performed": write_performed,
                    "final_read": final_read,
                    "matched": False if final_read else None,
                },
                "error": {
                    "code": error_code,
                    "reason_code": "fixture_reason",
                    "message": "private failure detail",
                    "retryable": False,
                },
                "next_action": {
                    "kind": "fresh_read",
                    "tool": "read_reminder",
                    "retry_original_once": False,
                    "message": "read before retry",
                },
            }

            with self.subTest(case=case_name):
                summary = json.loads(
                    self.server.tool_result(payload, is_error=False)["content"][0]["text"]
                )
                self.assertEqual(summary["outcome"], "attention_required")
                self.assertTrue(summary["needs_attention"])
                self.assertIs(summary["may_have_mutated"], may_mutate)
                self.assertEqual(summary["write_state"], write_state)
                self.assertEqual(summary["target"], {"reminder_id": REMINDER_ID})
                self.assertEqual(
                    summary["next_read_only_action"],
                    {
                        "kind": "fresh_read",
                        "tool": "read_reminder",
                        "retry_original_once": False,
                    },
                )
                self.assertNotIn("private failure detail", json.dumps(summary))

    def test_stale_page_summary_recommends_restarting_the_same_read_without_cursor(self) -> None:
        for operation in ("fetch_reminders", "inspect_recently_deleted"):
            payload = {
                "schema_version": 2,
                "ok": False,
                "status": "failed_no_mutation",
                "operation": operation,
                "error": {
                    "code": "concurrent_modification",
                    "reason_code": "pagination_snapshot_stale",
                    "message": "The ordered page snapshot changed.",
                    "retryable": False,
                },
                "next_action": {
                    "kind": "fresh_read",
                    "tool": operation,
                    "retry_original_once": False,
                    "message": f"Restart {operation} without a cursor.",
                },
            }

            summary = json.loads(
                self.server.tool_result(payload, is_error=True)["content"][0]["text"]
            )

            self.assertEqual(
                summary["next_read_only_action"],
                {
                    "kind": "fresh_read",
                    "tool": operation,
                    "retry_original_once": False,
                },
            )

    def test_text_summary_marks_blocked_diagnosis_for_attention(self) -> None:
        payload = {
            "schema_version": 2,
            "ok": True,
            "status": "verified",
            "operation": "diagnose_reminders",
            "data": {"overall": "blocked", "scope": "access"},
        }

        summary = json.loads(
            self.server.tool_result(payload, is_error=False)["content"][0]["text"]
        )

        self.assertEqual(summary["outcome"], "attention_required")
        self.assertTrue(summary["needs_attention"])
        self.assertEqual(summary["overall"], "blocked")
        self.assertEqual(summary["next_read_only_action"]["tool"], "diagnose_reminders")

    def test_explicit_test_backend_injection_serves_list_and_doctor_over_stdio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            eventkit = temporary_root / "mock_eventkit.py"
            doctor = temporary_root / "mock_doctor.py"
            mock_eventkit_bridge(eventkit)
            mock_doctor(doctor)

            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "list_reminder_lists",
                            "arguments": {"limit": 1},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "diagnose_reminders",
                            "arguments": {"scope": "access"},
                        },
                    },
                ],
                eventkit_bridge_path=eventkit,
                doctor_path=doctor,
            )

            eventkit_requests = [
                json.loads(line)
                for line in eventkit.with_suffix(".requests").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            doctor_argv = json.loads(
                doctor.with_suffix(".argv").read_text(encoding="utf-8")
            )

        listed = responses[1]["result"]
        diagnosed = responses[2]["result"]
        self.assertFalse(listed["isError"], listed)
        self.assertEqual(
            listed["structuredContent"]["data"]["items"][0]["id"],
            "LIST-1",
        )
        self.assertFalse(diagnosed["isError"], diagnosed)
        self.assertEqual(diagnosed["structuredContent"]["data"]["overall"], "blocked")
        self.assertEqual(eventkit_requests[0]["operation"], "list_calendars")
        self.assertNotIn("limit", eventkit_requests[0])
        self.assertEqual(
            doctor_argv,
            ["--compact", "--detail-level", "summary"],
        )

    def test_full_doctor_details_cross_stdio_as_bounded_scalar_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            doctor = Path(temporary) / "mock_doctor.py"
            mock_doctor(doctor)

            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "diagnose_reminders",
                            "arguments": {
                                "scope": "access",
                                "detail_level": "full",
                            },
                        },
                    },
                ],
                doctor_path=doctor,
            )
            doctor_argv = json.loads(
                doctor.with_suffix(".argv").read_text(encoding="utf-8")
            )

        diagnosed = responses[1]["result"]
        self.assertFalse(diagnosed["isError"], diagnosed)
        facts = diagnosed["structuredContent"]["data"]["checks"][0]["facts"]
        self.assertEqual(
            facts,
            [
                {"name": "authorization", "value": "denied"},
                {"name": "prompt_attempted", "value": False},
            ],
        )
        self.assertEqual(
            doctor_argv,
            ["--compact", "--detail-level", "full"],
        )

    def test_production_paths_ignore_legacy_environment_overrides(self) -> None:
        legacy = {
            "APPLE_REMINDERS_ADAPTER_PATH": "/tmp/not-the-adapter",
            "APPLE_REMINDERS_EVENTKIT_BRIDGE_PATH": "/tmp/not-the-bridge",
            "APPLE_REMINDERS_DOCTOR_PATH": "/tmp/not-the-doctor",
            "APPLE_REMINDERS_MCP_TEST_MODE": "1",
        }

        with mock.patch.dict(os.environ, legacy, clear=False):
            self.assertEqual(
                self.server.adapter_path(),
                self.server.DEFAULT_ADAPTER_PATH,
            )
            self.assertEqual(
                self.server.eventkit_bridge_path(),
                self.server.DEFAULT_EVENTKIT_BRIDGE_PATH,
            )
            self.assertEqual(
                self.server.doctor_path(),
                self.server.DEFAULT_DOCTOR_PATH,
            )


if __name__ == "__main__":
    unittest.main()
