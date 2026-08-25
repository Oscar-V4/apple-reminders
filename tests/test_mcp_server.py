from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp" / "server.py"
SERVER_HARNESS = ROOT / "tests" / "mcp_server_harness.py"
CONFIG = ROOT / ".mcp.json"
TOOLS_SCHEMA = ROOT / "schemas" / "mcp-tools.json"
REMINDER_ID = "7718459E-2672-4E99-9E6A-B9AA430E570F"

TEST_BACKEND_ENVIRONMENTS = {
    "adapter": "APPLE_REMINDERS_TEST_HARNESS_ADAPTER_PATH",
    "eventkit_bridge": "APPLE_REMINDERS_TEST_HARNESS_EVENTKIT_BRIDGE_PATH",
    "doctor": "APPLE_REMINDERS_TEST_HARNESS_DOCTOR_PATH",
}


def run_server(
    messages: list[dict[str, Any]],
    *,
    adapter_path: Path | None = None,
    eventkit_bridge_path: Path | None = None,
    doctor_path: Path | None = None,
    home_path: Path | None = None,
    enable_test_backends: bool = True,
) -> list[dict[str, Any]]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for environment_name in TEST_BACKEND_ENVIRONMENTS.values():
        env.pop(environment_name, None)
    if enable_test_backends:
        if adapter_path is not None:
            env[TEST_BACKEND_ENVIRONMENTS["adapter"]] = str(adapter_path)
        if eventkit_bridge_path is not None:
            env[TEST_BACKEND_ENVIRONMENTS["eventkit_bridge"]] = str(
                eventkit_bridge_path
            )
        if doctor_path is not None:
            env[TEST_BACKEND_ENVIRONMENTS["doctor"]] = str(doctor_path)
    if home_path is not None:
        env["HOME"] = str(home_path)
    completed = subprocess.run(
        [sys.executable, str(SERVER_HARNESS)],
        cwd=ROOT,
        env=env,
        input="".join(json.dumps(message) + "\n" for message in messages),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"MCP server exited {completed.returncode}: {completed.stderr}"
        )
    return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]


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


def mock_adapter(path: Path) -> None:
    path.write_text(
        """from __future__ import annotations
import json
import sys
from pathlib import Path

argv = sys.argv[1:]
Path(__file__).with_suffix(".called").write_text("called", encoding="utf-8")
command = argv[0] if argv else None
if command == "list_lists":
    payload = {
        "ok": True,
        "db": "/private/reminders.sqlite",
        "lists": [
            {"ZCKIDENTIFIER": "A", "ZNAME": "Alpha", "ZISGROUP": 0, "reminder_count": 1},
            {"ZCKIDENTIFIER": "B", "ZNAME": "Beta", "ZISGROUP": 1, "reminder_count": 2},
            {"ZCKIDENTIFIER": "C", "ZNAME": "Gamma", "ZISGROUP": 0, "reminder_count": 3},
        ],
    }
elif command == "list_attachments":
    payload = {
        "ok": True,
        "db": "/private/reminders.sqlite",
        "reminder_id": "7718459E-2672-4E99-9E6A-B9AA430E570F",
        "reminder_version": 12,
        "attachments": [{"id": "A"}, {"id": "B"}, {"id": "C"}],
        "truncated": False,
    }
else:
    payload = {
        "ok": True,
        "db": "/private/reminders.sqlite",
        "argv": argv,
        "matches": [{"id": "MOCK", "title": "Mock reminder"}],
    }
    mutating = command in {
        "create_list",
        "create_reminder",
        "update_reminder",
        "complete_reminder",
        "reopen_reminder",
        "delete_reminder",
        "show_reminder",
        "add_tag",
        "remove_tag",
        "create_section",
        "move_to_section",
        "attach_image",
        "attach_url",
        "delete_attachment",
        "replace_attachment",
        "purge_logs",
    } or (command in {"cleanup_tags", "repair_attachments"} and "--apply" in argv)
    if mutating:
        payload.update({
            "status": "verified",
            "operation": command,
            "operation_id": "MOCK-OPERATION",
            "backend": "mock",
            "target": {},
            "after": {},
            "verification": {"state": "mock"},
            "recovery": {"semantics": "mock"},
        })
print(json.dumps(payload))
""",
        encoding="utf-8",
    )


def mock_visible_url_adapter(path: Path) -> None:
    path.write_text(
        """from __future__ import annotations
import json
import sys
from pathlib import Path

argv = sys.argv[1:]
request_log = Path(__file__).with_suffix(".requests")
previous = request_log.read_text(encoding="utf-8") if request_log.exists() else ""
request_log.write_text(previous + json.dumps(argv) + "\\n", encoding="utf-8")
command = argv[0] if argv else None
reminder_id = argv[argv.index("--id") + 1]
if command == "list_attachments":
    payload = {
        "ok": True,
        "reminder_id": reminder_id,
        "reminder_version": 12,
        "attachments": [],
        "truncated": False,
    }
elif command == "attach_url":
    url = argv[argv.index("--url") + 1]
    payload = {
        "ok": True,
        "status": "verified",
        "operation": "attach_url",
        "operation_id": "8D0346C4-90B4-48D7-A18D-A8F3E9B68054",
        "backend": "sqlite_private",
        "target": {
            "id": reminder_id,
            "attachment_id": "URL-ATTACHMENT",
        },
        "before": {"id": reminder_id},
        "after": {
            "reminder": {"id": reminder_id},
            "attachment": {
                "id": "URL-ATTACHMENT",
                "type": "url",
                "url": url,
            },
        },
        "verification": {"state": "read_back", "attachment_active": True},
        "recovery": {"semantics": "delete_attachment"},
    }
else:
    raise SystemExit(99)
print(json.dumps(payload))
""",
        encoding="utf-8",
    )


def mock_failing_visible_url_adapter(path: Path) -> None:
    path.write_text(
        """from __future__ import annotations
import json
import sys

argv = sys.argv[1:]
command = argv[0] if argv else None
reminder_id = argv[argv.index("--id") + 1]
if command == "list_attachments":
    payload = {
        "ok": True,
        "reminder_id": reminder_id,
        "reminder_version": 12,
        "attachments": [],
        "truncated": False,
    }
elif command == "attach_url":
    payload = {
        "ok": False,
        "status": "failed_no_mutation",
        "operation": "attach_url",
        "operation_id": "4EA7E143-1456-4F28-B11A-97597EE0EF51",
        "backend": "sqlite_private",
        "target": {"id": reminder_id},
        "after": {},
        "verification": {"state": "failed"},
        "recovery": {"semantics": "not_applicable"},
        "error": {"code": "schema_mismatch", "message": "fixture failure"},
    }
else:
    raise SystemExit(99)
print(json.dumps(payload))
""",
        encoding="utf-8",
    )


def mock_eventkit_bridge(path: Path, *, final_read_mode: str = "success") -> None:
    if final_read_mode not in {
        "success",
        "failure",
        "wrong_id",
        "missing_last_modified",
        "invalid_last_modified",
        "url_mismatch",
    }:
        raise ValueError(final_read_mode)
    path.with_suffix(".mode").write_text(final_read_mode, encoding="utf-8")
    path.write_text(
        """from __future__ import annotations
import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
marker = Path(__file__).with_suffix(".called")
previous = marker.read_text(encoding="utf-8") if marker.exists() else ""
marker.write_text(previous + "called\\n", encoding="utf-8")
request_log = Path(__file__).with_suffix(".requests")
previous_requests = request_log.read_text(encoding="utf-8") if request_log.exists() else ""
request_log.write_text(previous_requests + json.dumps(request) + "\\n", encoding="utf-8")
operation = request["operation"]
mode = Path(__file__).with_suffix(".mode").read_text(encoding="utf-8")
state_path = Path(__file__).with_suffix(".state")
if operation == "list_accounts":
    data = {"items": [
        {"id": "ACCOUNT-A", "title": "iCloud"},
        {"id": "ACCOUNT-B", "title": "Local"},
        {"id": "ACCOUNT-C", "title": "Work"},
    ]}
elif operation == "list_calendars":
    data = {"items": [
        {"id": "LIST-A", "title": "Alpha"},
        {"id": "LIST-B", "title": "Beta"},
        {"id": "LIST-C", "title": "Gamma"},
    ]}
elif operation == "fetch_reminders":
    offset = request["offset"]
    limit = request["limit"]
    total = 5
    count = min(limit, max(0, total - offset))
    items = [{"id": f"REMINDER-{index}", "title": f"Reminder {index}"}
             for index in range(offset, offset + count)]
    next_offset = offset + count if offset + count < total else None
    data = {
        "items": items,
        "total_matched": total,
        "limit": limit,
        "offset": offset,
        "has_more": next_offset is not None,
        "next_offset": next_offset,
    }
elif operation == "read_reminder":
    if mode == "failure":
        print(json.dumps({
            "schema_version": 1,
            "operation": operation,
            "status": "failed_no_mutation",
            "ok": False,
            "error": {
                "code": "permission_denied",
                "message": "Synthetic final read failure.",
            },
        }))
        raise SystemExit(0)
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {"url": None}
    )
    reminder = {
        "id": "WRONG-REMINDER" if mode == "wrong_id" else request["reminder_id"],
        "title": "Final exact projection",
        "url": "https://example.com/wrong" if mode == "url_mismatch" else state["url"],
    }
    if mode != "missing_last_modified":
        reminder["last_modified"] = (
            "not-a-timestamp"
            if mode == "invalid_last_modified"
            else "2026-08-25T00:00:01.000Z"
        )
    data = {"reminder": reminder}
elif operation in {"create_reminder", "update_reminder", "complete_reminder", "reopen_reminder", "move_reminder", "delete_reminder"}:
    reminder_id = request.get("reminder_id", "REMINDER-CREATED")
    visible_url = request.get("url")
    if operation == "update_reminder":
        visible_url = request.get("patch", {}).get("url")
    state_path.write_text(
        json.dumps({"id": reminder_id, "url": visible_url}),
        encoding="utf-8",
    )
    payload = {
        "schema_version": 1,
        "operation": operation,
        "operation_id": "3E7CFECB-A25C-4AB7-9F14-DA89B5C11831",
        "backend": "eventkit_public_sdk",
        "status": "verified",
        "ok": True,
        "target": {"reminder_id": reminder_id},
        "after": {
            "id": reminder_id,
            "title": "Pre-final projection",
            "url": visible_url,
            "last_modified": "2026-08-25T00:00:00.000Z",
        },
        "verification": {"state": "read_back"},
        "recovery": {"semantics": "eventkit"},
    }
    if operation != "create_reminder":
        payload["before"] = {"id": request["reminder_id"]}
    print(json.dumps(payload))
    raise SystemExit(0)
else:
    data = {"request": request}
print(json.dumps({
    "schema_version": 1,
    "operation": operation,
    "status": "verified",
    "ok": True,
    "data": data,
}))
""",
        encoding="utf-8",
    )


def mock_doctor(path: Path) -> None:
    path.write_text(
        """import json
print(json.dumps({
    "schema_version": 1,
    "doctor": "apple-reminders-doctor",
    "ok": False,
    "status": "blocked",
    "privacy": {
        "content_free": True,
        "reminder_rows_read": False,
        "permission_prompt_attempted": False,
        "write_attempted": False,
    },
    "checks": {"permissions": {"status": "blocked"}},
}))
""",
        encoding="utf-8",
    )


class McpPackagingTests(unittest.TestCase):
    def test_config_registers_one_local_stdio_server(self) -> None:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))

        self.assertEqual(set(config), {"mcpServers"})
        self.assertEqual(set(config["mcpServers"]), {"apple-reminders-local"})
        server = config["mcpServers"]["apple-reminders-local"]
        self.assertEqual(server["cwd"], ".")
        self.assertEqual(server["command"], "python3")
        self.assertEqual(server["args"], ["./mcp/server.py"])
        self.assertNotIn("url", server)

    def test_tool_schemas_are_closed_unique_and_route_safe(self) -> None:
        payload = json.loads(TOOLS_SCHEMA.read_text(encoding="utf-8"))
        tools = payload["tools"]
        by_name = {tool["name"]: tool for tool in tools}

        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(len(by_name), len(tools))
        self.assertGreaterEqual(len(tools), 15)
        for tool in tools:
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertIs(schema["additionalProperties"], False)
            self.assertEqual(
                tool["annotations"]["openWorldHint"],
                tool["name"] in {"attach_url_to_reminder", "replace_reminder_attachment"},
            )

        delete = by_name["delete_reminder"]
        self.assertEqual(
            delete["inputSchema"]["required"],
            ["reminder_id", "expected_last_modified"],
        )
        self.assertEqual(
            delete["inputSchema"]["properties"]["expected_last_modified"]["type"],
            "string",
        )
        delete_id = delete["inputSchema"]["properties"]["reminder_id"]
        self.assertEqual(delete_id["type"], "string")
        self.assertEqual(delete_id["minLength"], 1)
        self.assertEqual(delete_id["maxLength"], 2048)
        self.assertNotIn("pattern", delete_id)
        self.assertIn("opaque", delete_id["description"])
        self.assertNotIn("if_version", delete["inputSchema"]["properties"])
        self.assertIn("EventKit", delete["description"])
        self.assertTrue(delete["annotations"]["destructiveHint"])
        self.assertNotIn("title", delete["inputSchema"]["properties"])
        self.assertNotIn("list_name", delete["inputSchema"]["properties"])
        self.assertNotIn("backend", delete["inputSchema"]["properties"])

        preview = by_name["preview_unused_reminder_tags"]
        self.assertTrue(preview["annotations"]["readOnlyHint"])
        self.assertNotIn("apply", preview["inputSchema"]["properties"])
        self.assertNotIn("preview_digest", preview["inputSchema"]["properties"])

        cleanup = by_name["cleanup_unused_reminder_tags"]
        self.assertTrue(cleanup["annotations"]["destructiveHint"])
        self.assertIn("candidate_digest", cleanup["inputSchema"]["required"])
        self.assertNotIn("no_backup", cleanup["inputSchema"]["properties"])

        fetch = by_name["fetch_reminders"]
        self.assertTrue(fetch["annotations"]["readOnlyHint"])
        self.assertIn("cursor", fetch["inputSchema"]["properties"])
        self.assertNotIn("offset", fetch["inputSchema"]["properties"])
        self.assertIn("calendar_ids", fetch["inputSchema"]["properties"])

        guarded_private_mutations = {
            "add_reminder_tag",
            "remove_reminder_tag",
            "move_reminder_to_section",
            "attach_image_to_reminder",
            "attach_url_to_reminder",
            "delete_reminder_attachment",
            "replace_reminder_attachment",
        }
        for name in guarded_private_mutations:
            with self.subTest(name=name):
                schema = by_name[name]["inputSchema"]
                self.assertIn("if_version", schema["properties"])
                self.assertIn("if_version", schema["required"])
                self.assertIn("list_reminder_attachments", by_name[name]["description"])

        self.assertTrue(by_name["reminders_plugin_doctor"]["annotations"]["readOnlyHint"])
        self.assertTrue(by_name["get_reminders_capabilities"]["annotations"]["readOnlyHint"])
        self.assertFalse(by_name["request_reminders_access"]["annotations"]["readOnlyHint"])
        show = by_name["show_reminder"]
        self.assertEqual(show["inputSchema"]["required"], ["reminder_id"])
        self.assertFalse(show["annotations"]["destructiveHint"])

        create = by_name["create_reminder"]
        self.assertEqual(
            create["inputSchema"]["required"],
            ["calendar_id", "title", "idempotency_key"],
        )
        self.assertIn("recurrence_rules", create["inputSchema"]["properties"])
        self.assertIn("visible URL attachment", create["description"])
        self.assertIn(
            "visible URL attachment",
            create["inputSchema"]["properties"]["url"]["description"],
        )
        self.assertEqual(
            create["inputSchema"]["properties"]["notes"]["maxLength"],
            100_000,
        )
        create_list = by_name["create_reminder_list"]
        self.assertIn("color", create_list["inputSchema"]["properties"])
        self.assertIn("emblem", create_list["inputSchema"]["properties"])
        update = by_name["update_reminder"]
        self.assertEqual(
            update["inputSchema"]["required"],
            ["reminder_id", "expected_last_modified", "patch"],
        )
        self.assertEqual(
            update["inputSchema"]["properties"]["patch"]["properties"]["notes"][
                "maxLength"
            ],
            100_000,
        )
        self.assertIn(
            "visible URL attachment",
            update["inputSchema"]["properties"]["patch"]["properties"]["url"][
                "description"
            ],
        )
        attach_image = by_name["attach_image_to_reminder"]
        self.assertEqual(
            attach_image["inputSchema"]["required"],
            ["reminder_id", "image_path", "if_version", "idempotency_key"],
        )
        self.assertNotIn("backend", attach_image["inputSchema"]["properties"])
        self.assertIn("list_reminder_attachments", attach_image["description"])
        self.assertIn(
            "fresh",
            attach_image["inputSchema"]["properties"]["if_version"]["description"],
        )
        self.assertIn(
            "private existing-item mutations",
            by_name["list_reminder_attachments"]["description"],
        )
        repair_apply = by_name["apply_reminder_attachment_repairs"]
        self.assertEqual(repair_apply["inputSchema"]["required"], ["candidate_digest"])
        self.assertFalse(repair_apply["inputSchema"]["properties"]["no_backup"]["default"])
        self.assertIn(
            "list_reminder_attachments",
            by_name["replace_reminder_attachment"]["description"],
        )
        self.assertIn(
            "fresh",
            by_name["replace_reminder_attachment"]["inputSchema"]["properties"][
                "if_version"
            ]["description"],
        )

    def test_section_scope_uses_an_exact_list_identifier(self) -> None:
        schema = json.loads(TOOLS_SCHEMA.read_text(encoding="utf-8"))
        sections = next(
            tool for tool in schema["tools"] if tool["name"] == "list_reminder_sections"
        )

        properties = sections["inputSchema"]["properties"]
        self.assertIn("list_id", properties)
        self.assertNotIn("list_name", properties)
        self.assertIn("duplicate", sections["description"].casefold())


class McpProtocolTests(unittest.TestCase):
    def test_production_server_ignores_backend_environment_even_in_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            adapter = temp_root / "mock_adapter.py"
            marker = adapter.with_suffix(".called")
            mock_adapter(adapter)
            isolated_home = temp_root / "isolated-home"
            isolated_home.mkdir()
            env = {
                **os.environ,
                "APPLE_REMINDERS_ADAPTER_PATH": str(adapter),
                "APPLE_REMINDERS_EVENTKIT_BRIDGE_PATH": str(
                    temp_root / "mock_eventkit.py"
                ),
                "APPLE_REMINDERS_DOCTOR_PATH": str(temp_root / "mock_doctor.py"),
                "APPLE_REMINDERS_MCP_TEST_MODE": "1",
                "HOME": str(isolated_home),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            messages = [
                initialize(),
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "list_reminder_sections",
                        "arguments": {
                            "list_id": "22222222-2222-4222-8222-222222222222",
                            "limit": 1,
                        },
                    },
                },
            ]
            completed = subprocess.run(
                [sys.executable, str(SERVER)],
                cwd=ROOT,
                env=env,
                input="".join(json.dumps(message) + "\n" for message in messages),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(marker.exists())

    def test_section_route_passes_the_exact_list_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp) / "mock_adapter.py"
            mock_adapter(adapter)
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "list_reminder_sections",
                            "arguments": {
                                "list_id": "22222222-2222-4222-8222-222222222222",
                                "limit": 10,
                            },
                        },
                    },
                ],
                adapter_path=adapter,
            )

        result = responses[1]["result"]
        self.assertFalse(result["isError"], result)
        self.assertEqual(
            result["structuredContent"]["argv"],
            [
                "list_sections",
                "--list-id",
                "22222222-2222-4222-8222-222222222222",
                "--limit",
                "10",
            ],
        )

    def test_harness_uses_bundled_backends_when_test_injection_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            adapter = temp_root / "mock_adapter.py"
            marker = adapter.with_suffix(".called")
            mock_adapter(adapter)
            isolated_home = temp_root / "isolated-home"
            isolated_home.mkdir()
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "list_reminder_sections",
                            "arguments": {
                                "list_id": "22222222-2222-4222-8222-222222222222",
                                "limit": 1,
                            },
                        },
                    },
                ],
                adapter_path=adapter,
                home_path=isolated_home,
                enable_test_backends=False,
            )
            self.assertFalse(marker.exists())
            self.assertTrue(responses[1]["result"]["isError"])

    def test_notification_shaped_tool_call_cannot_execute_a_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp) / "mock_adapter.py"
            marker = adapter.with_suffix(".called")
            mock_adapter(adapter)
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "params": {
                            "name": "delete_reminder",
                            "arguments": {"reminder_id": REMINDER_ID},
                        },
                    },
                ],
                adapter_path=adapter,
            )
            self.assertEqual(responses[1]["error"]["code"], -32600)
            self.assertFalse(marker.exists())

    def test_tools_are_unavailable_before_initialize(self) -> None:
        responses = run_server(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {},
                }
            ]
        )

        self.assertEqual(responses[0]["error"]["code"], -32002)

    def test_initialize_tool_list_and_ping_handshake(self) -> None:
        responses = run_server(
            [
                initialize(),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
            ]
        )

        self.assertEqual([item["id"] for item in responses], [1, 2, 3])
        initialized = responses[0]["result"]
        self.assertEqual(initialized["protocolVersion"], "2025-11-25")
        self.assertEqual(initialized["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(initialized["serverInfo"]["name"], "apple-reminders-local")
        tools = responses[1]["result"]["tools"]
        self.assertEqual(
            {tool["name"] for tool in tools},
            {
                tool["name"]
                for tool in json.loads(TOOLS_SCHEMA.read_text(encoding="utf-8"))["tools"]
            },
        )
        self.assertEqual(responses[2]["result"], {})

    def test_onboarding_tools_dispatch_without_live_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            bridge = temp_root / "mock_eventkit.py"
            doctor = temp_root / "mock_doctor.py"
            mock_eventkit_bridge(bridge)
            mock_doctor(doctor)
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "reminders_plugin_doctor", "arguments": {}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "get_reminders_capabilities", "arguments": {}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {"name": "request_reminders_access", "arguments": {}},
                    },
                ],
                eventkit_bridge_path=bridge,
                doctor_path=doctor,
            )

        doctor_result = responses[1]["result"]
        self.assertFalse(doctor_result["isError"])
        self.assertTrue(doctor_result["structuredContent"]["privacy"]["content_free"])
        self.assertEqual(
            responses[2]["result"]["structuredContent"]["operation"],
            "capabilities",
        )
        self.assertEqual(
            responses[3]["result"]["structuredContent"]["operation"],
            "request_access",
        )

    def test_eventkit_fetch_uses_opaque_filter_bound_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Path(tmp) / "mock_eventkit.py"
            mock_eventkit_bridge(bridge)
            arguments = {"calendar_ids": ["LIST-A"], "query": "dentist", "limit": 2}
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "fetch_reminders",
                            "arguments": arguments,
                        },
                    },
                ],
                eventkit_bridge_path=bridge,
            )
            first_bridge_request = json.loads(
                bridge.with_suffix(".requests").read_text(encoding="utf-8").splitlines()[0]
            )

        result = responses[1]["result"]
        self.assertFalse(result["isError"])
        structured = result["structuredContent"]
        data = structured["data"]
        cursor = data["next_cursor"]
        self.assertIsInstance(cursor, str)
        self.assertNotIn("offset", data)
        self.assertNotIn("next_offset", data)
        serialized_data = json.dumps(data, sort_keys=True)
        self.assertNotIn('"offset":', serialized_data)
        self.assertNotIn('"next_offset":', serialized_data)
        self.assertEqual(first_bridge_request["offset"], 0)
        self.assertEqual(first_bridge_request["status"], "incomplete")
        self.assertEqual(first_bridge_request["sort"], "due")
        self.assertEqual(json.loads(result["content"][0]["text"]), structured)

        with tempfile.TemporaryDirectory() as tmp:
            bridge = Path(tmp) / "mock_eventkit.py"
            mock_eventkit_bridge(bridge)
            next_responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "fetch_reminders",
                            "arguments": {**arguments, "cursor": cursor},
                        },
                    },
                ],
                eventkit_bridge_path=bridge,
            )
            next_bridge_request = json.loads(
                bridge.with_suffix(".requests").read_text(encoding="utf-8").splitlines()[0]
            )
        self.assertFalse(next_responses[1]["result"]["isError"])
        self.assertEqual(next_bridge_request["offset"], 2)

        with tempfile.TemporaryDirectory() as tmp:
            bridge = Path(tmp) / "mock_eventkit.py"
            marker = bridge.with_suffix(".called")
            mock_eventkit_bridge(bridge)
            changed_responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "fetch_reminders",
                            "arguments": {
                                **arguments,
                                "query": "changed",
                                "cursor": cursor,
                            },
                        },
                    },
                ],
                eventkit_bridge_path=bridge,
            )
            bridge_was_called = marker.exists()
        changed = changed_responses[1]["result"]
        self.assertTrue(changed["isError"])
        self.assertEqual(changed["structuredContent"]["error"]["code"], "invalid_arguments")
        self.assertFalse(bridge_was_called)

    def test_eventkit_account_and_list_reads_are_defensively_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Path(tmp) / "mock_eventkit.py"
            mock_eventkit_bridge(bridge)
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "list_reminder_lists",
                            "arguments": {
                                "source_id": "ACCOUNT-A",
                                "writable_only": True,
                                "limit": 2,
                            },
                        },
                    },
                ],
                eventkit_bridge_path=bridge,
            )
            bridge_request = json.loads(
                bridge.with_suffix(".requests").read_text(encoding="utf-8").splitlines()[0]
            )

        structured = responses[1]["result"]["structuredContent"]
        self.assertEqual(
            structured["data"]["items"],
            [
                {"id": "LIST-A", "title": "Alpha"},
                {"id": "LIST-B", "title": "Beta"},
            ],
        )
        self.assertEqual(structured["data"]["returned"], 2)
        self.assertTrue(structured["data"]["truncated"])
        self.assertEqual(bridge_request["source_id"], "ACCOUNT-A")
        self.assertTrue(bridge_request["writable_only"])
        self.assertNotIn("limit", bridge_request)

    def test_delete_call_uses_public_eventkit_with_exact_precondition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            adapter = temp_root / "mock_adapter.py"
            mock_adapter(adapter)
            bridge = temp_root / "mock_eventkit.py"
            mock_eventkit_bridge(bridge)
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "delete_reminder",
                            "arguments": {
                                "reminder_id": REMINDER_ID,
                                "expected_last_modified": "2026-08-06T00:00:00Z",
                            },
                        },
                    }
                ],
                adapter_path=adapter,
                eventkit_bridge_path=bridge,
            )

        result = responses[1]["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["backend"], "eventkit_public_sdk")
        self.assertEqual(result["structuredContent"]["operation"], "delete_reminder")

    def test_invalid_eventkit_transport_after_possible_delete_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Path(tmp) / "invalid_eventkit.py"
            bridge.write_text("print('not-json')\n", encoding="utf-8")
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "delete_reminder",
                            "arguments": {
                                "reminder_id": REMINDER_ID,
                                "expected_last_modified": "2026-08-06T00:00:00Z",
                            },
                        },
                    },
                ],
                eventkit_bridge_path=bridge,
            )

        result = responses[1]["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(
            result["structuredContent"]["status"],
            "committed_verification_pending",
        )
        self.assertEqual(
            result["structuredContent"]["error"]["code"],
            "sync_pending",
        )

    def test_private_feature_concurrency_fields_reach_the_adapter(self) -> None:
        calls = [
            (
                "create_reminder_list",
                {"name": "Health", "color": "green", "emblem": "🩺"},
                [
                    "create_list",
                    "--name",
                    "Health",
                    "--color",
                    "green",
                    "--emblem",
                    "🩺",
                ],
            ),
            (
                "create_reminder_section",
                {"list_id": REMINDER_ID, "name": "Next"},
                ["create_section", "--list-id", REMINDER_ID, "--name", "Next"],
            ),
            (
                "add_reminder_tag",
                {"reminder_id": REMINDER_ID, "tag": "health", "if_version": 5},
                ["add_tag", "--id", REMINDER_ID, "--tag", "health", "--if-version", "5"],
            ),
            (
                "remove_reminder_tag",
                {"reminder_id": REMINDER_ID, "tag": "health", "if_version": 6},
                ["remove_tag", "--id", REMINDER_ID, "--tag", "health", "--if-version", "6"],
            ),
            (
                "move_reminder_to_section",
                {
                    "reminder_id": REMINDER_ID,
                    "section_id": REMINDER_ID,
                    "if_version": 7,
                },
                [
                    "move_to_section",
                    "--id",
                    REMINDER_ID,
                    "--section-id",
                    REMINDER_ID,
                    "--if-version",
                    "7",
                ],
            ),
            (
                "attach_url_to_reminder",
                {
                    "reminder_id": REMINDER_ID,
                    "url": "https://example.com/reference",
                    "if_version": 8,
                },
                [
                    "attach_url",
                    "--id",
                    REMINDER_ID,
                    "--url",
                    "https://example.com/reference",
                    "--if-version",
                    "8",
                ],
            ),
            (
                "delete_reminder_attachment",
                {
                    "reminder_id": REMINDER_ID,
                    "attachment_id": REMINDER_ID,
                    "if_version": 9,
                },
                [
                    "delete_attachment",
                    "--id",
                    REMINDER_ID,
                    "--attachment-id",
                    REMINDER_ID,
                    "--if-version",
                    "9",
                ],
            ),
            (
                "show_reminder",
                {"reminder_id": REMINDER_ID},
                ["show_reminder", "--id", REMINDER_ID],
            ),
            (
                "purge_reminder_plugin_logs",
                {},
                ["purge_logs"],
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp) / "mock_adapter.py"
            mock_adapter(adapter)
            messages = [initialize()]
            for request_id, (name, arguments, _) in enumerate(calls, start=2):
                messages.append(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    }
                )
            responses = run_server(messages, adapter_path=adapter)

        for response, (_, _, expected) in zip(responses[1:], calls, strict=True):
            self.assertEqual(response["result"]["structuredContent"]["argv"], expected)

    def test_public_eventkit_mutations_are_typed_and_create_retries_replay(self) -> None:
        timestamp = "2026-08-06T00:00:00Z"
        create_arguments = {
            "calendar_id": "LIST-A",
            "title": "Book dentist",
            "due": {
                "kind": "timed",
                "date_time": "2026-08-07T09:00:00+09:00",
                "time_zone": "Asia/Seoul",
            },
            "alarms": [
                {
                    "kind": "location",
                    "proximity": "enter",
                    "location": {
                        "title": "Clinic",
                        "latitude": 37.5,
                        "longitude": 127.0,
                        "radius_meters": 100,
                    },
                }
            ],
            "recurrence_rules": [
                {
                    "frequency": "weekly",
                    "interval": 1,
                    "days_of_week": [{"day": "friday"}],
                }
            ],
            "idempotency_key": "eventkit:create:dentist:2026",
        }
        calls = [
            ("create_reminder", create_arguments, "create_reminder"),
            (
                "update_reminder",
                {
                    "reminder_id": "REMINDER-1",
                    "expected_last_modified": timestamp,
                    "patch": {"notes": None, "due": {"kind": "all_day", "date": "2026-08-08"}},
                },
                "update_reminder",
            ),
            (
                "complete_reminder",
                {"reminder_id": "REMINDER-1", "expected_last_modified": timestamp},
                "complete_reminder",
            ),
            (
                "reopen_reminder",
                {"reminder_id": "REMINDER-1", "expected_last_modified": timestamp},
                "reopen_reminder",
            ),
            (
                "move_reminder_to_list",
                {
                    "reminder_id": "REMINDER-1",
                    "calendar_id": "LIST-B",
                    "expected_last_modified": timestamp,
                },
                "move_reminder",
            ),
            (
                "delete_reminder",
                {
                    "reminder_id": REMINDER_ID,
                    "expected_last_modified": timestamp,
                },
                "delete_reminder",
            ),
            ("create_reminder", create_arguments, "create_reminder"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            bridge = temp_root / "mock_eventkit.py"
            mock_eventkit_bridge(bridge)
            adapter = temp_root / "forbidden_adapter.py"
            adapter.write_text("raise SystemExit(99)\n", encoding="utf-8")
            messages = [initialize()]
            for request_id, (name, arguments, _) in enumerate(calls, start=2):
                messages.append(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    }
                )
            changed_create = dict(create_arguments)
            changed_create["title"] = "Different logical creation"
            messages.append(
                {
                    "jsonrpc": "2.0",
                    "id": len(calls) + 2,
                    "method": "tools/call",
                    "params": {"name": "create_reminder", "arguments": changed_create},
                }
            )
            responses = run_server(
                messages,
                adapter_path=adapter,
                eventkit_bridge_path=bridge,
                home_path=temp_root / "home",
            )
            bridge_call_count = len(bridge.with_suffix(".called").read_text(encoding="utf-8").splitlines())
            bridge_requests = [
                json.loads(line)
                for line in bridge.with_suffix(".requests").read_text(encoding="utf-8").splitlines()
            ]

        for response, (_, _, operation) in zip(responses[1:-1], calls, strict=True):
            result = response["result"]
            self.assertFalse(result["isError"], result)
            self.assertEqual(result["structuredContent"]["operation"], operation)
            self.assertEqual(result["structuredContent"]["backend"], "eventkit_public_sdk")
        first_create = responses[1]["result"]["structuredContent"]
        replay = responses[-2]["result"]["structuredContent"]
        conflict = responses[-1]["result"]
        self.assertFalse(first_create.get("replayed", False))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["operation_id"], first_create["operation_id"])
        self.assertTrue(conflict["isError"])
        self.assertEqual(
            conflict["structuredContent"]["error"]["code"],
            "concurrent_modification",
        )
        self.assertEqual(bridge_call_count, 6)
        self.assertEqual(
            [request["operation"] for request in bridge_requests],
            [
                "create_reminder",
                "update_reminder",
                "complete_reminder",
                "reopen_reminder",
                "move_reminder",
                "delete_reminder",
            ],
        )
        self.assertNotIn("idempotency_key", bridge_requests[0])
        self.assertEqual(bridge_requests[1]["patch"]["notes"], None)
        self.assertEqual(bridge_requests[4]["calendar_id"], "LIST-B")

    def test_create_with_url_verifies_a_native_reminders_attachment(self) -> None:
        url = "https://example.com/results"
        create_arguments = {
            "calendar_id": "LIST-A",
            "title": "Check results",
            "url": url,
            "idempotency_key": "eventkit:create:visible-url:2026",
        }
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            bridge = temp_root / "mock_eventkit.py"
            adapter = temp_root / "mock_adapter.py"
            mock_eventkit_bridge(bridge)
            mock_visible_url_adapter(adapter)
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "create_reminder",
                            "arguments": create_arguments,
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "create_reminder",
                            "arguments": create_arguments,
                        },
                    },
                ],
                adapter_path=adapter,
                eventkit_bridge_path=bridge,
                home_path=temp_root / "home",
            )
            request_log = adapter.with_suffix(".requests")
            adapter_requests = (
                [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()]
                if request_log.exists()
                else []
            )
            bridge_requests = [
                json.loads(line)
                for line in bridge.with_suffix(".requests")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        result = responses[1]["result"]
        payload = result["structuredContent"]
        self.assertFalse(result["isError"], result)
        self.assertEqual(payload["status"], "verified")
        self.assertIn("url_attachment", payload["after"])
        self.assertEqual(payload["after"]["url_attachment"]["id"], "URL-ATTACHMENT")
        self.assertEqual(payload["after"]["url_attachment"]["url"], url)
        self.assertEqual(payload["after"]["title"], "Final exact projection")
        self.assertEqual(
            payload["after"]["last_modified"],
            "2026-08-25T00:00:01.000Z",
        )
        self.assertTrue(payload["verification"]["url_attachment"]["attachment_active"])
        self.assertTrue(
            payload["verification"]["eventkit_final_read"][
                "last_modified_safe_for_precondition"
            ]
        )
        replay = responses[2]["result"]["structuredContent"]
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["after"]["url_attachment"]["id"], "URL-ATTACHMENT")
        self.assertTrue(replay["verification"]["url_attachment"]["attachment_active"])
        self.assertEqual(
            adapter_requests,
            [
                ["list_attachments", "--id", "REMINDER-CREATED", "--limit", "1"],
                [
                    "attach_url",
                    "--id",
                    "REMINDER-CREATED",
                    "--url",
                    url,
                    "--if-version",
                    "12",
                ],
            ],
        )
        self.assertEqual(
            [request["operation"] for request in bridge_requests],
            ["create_reminder", "read_reminder"],
        )

    def test_update_with_url_verifies_a_native_reminders_attachment(self) -> None:
        url = "https://example.com/results"
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            bridge = temp_root / "mock_eventkit.py"
            adapter = temp_root / "mock_adapter.py"
            mock_eventkit_bridge(bridge)
            mock_visible_url_adapter(adapter)
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "update_reminder",
                            "arguments": {
                                "reminder_id": "REMINDER-1",
                                "expected_last_modified": "2026-08-06T00:00:00Z",
                                "patch": {"url": url},
                            },
                        },
                    },
                ],
                adapter_path=adapter,
                eventkit_bridge_path=bridge,
            )
            request_log = adapter.with_suffix(".requests")
            adapter_requests = (
                [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()]
                if request_log.exists()
                else []
            )
            bridge_requests = [
                json.loads(line)
                for line in bridge.with_suffix(".requests")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        result = responses[1]["result"]
        payload = result["structuredContent"]
        self.assertFalse(result["isError"], result)
        self.assertEqual(payload["status"], "verified")
        self.assertIn("url_attachment", payload["after"])
        self.assertEqual(payload["after"]["url_attachment"]["id"], "URL-ATTACHMENT")
        self.assertEqual(payload["after"]["url_attachment"]["url"], url)
        self.assertEqual(payload["after"]["title"], "Final exact projection")
        self.assertEqual(
            payload["after"]["last_modified"],
            "2026-08-25T00:00:01.000Z",
        )
        self.assertTrue(payload["verification"]["url_attachment"]["attachment_active"])
        self.assertEqual(
            adapter_requests,
            [
                ["list_attachments", "--id", "REMINDER-1", "--limit", "1"],
                [
                    "attach_url",
                    "--id",
                    "REMINDER-1",
                    "--url",
                    url,
                    "--if-version",
                    "12",
                ],
            ],
        )
        self.assertEqual(
            [request["operation"] for request in bridge_requests],
            ["update_reminder", "read_reminder"],
        )

    def test_url_final_read_failures_return_a_valid_pending_receipt(self) -> None:
        url = "https://example.com/results"
        for mode in (
            "failure",
            "wrong_id",
            "missing_last_modified",
            "invalid_last_modified",
            "url_mismatch",
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                temp_root = Path(tmp)
                bridge = temp_root / "mock_eventkit.py"
                adapter = temp_root / "mock_adapter.py"
                mock_eventkit_bridge(bridge, final_read_mode=mode)
                mock_visible_url_adapter(adapter)
                responses = run_server(
                    [
                        initialize(),
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {
                                "name": "update_reminder",
                                "arguments": {
                                    "reminder_id": "REMINDER-1",
                                    "expected_last_modified": "2026-08-06T00:00:00Z",
                                    "patch": {"url": url},
                                },
                            },
                        },
                    ],
                    adapter_path=adapter,
                    eventkit_bridge_path=bridge,
                )
                bridge_requests = [
                    json.loads(line)
                    for line in bridge.with_suffix(".requests")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]

            result = responses[1]["result"]
            payload = result["structuredContent"]
            self.assertFalse(result["isError"], result)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "committed_verification_pending")
            self.assertEqual(payload["verification"]["state"], "pending")
            self.assertFalse(
                payload["verification"]["eventkit_final_read"][
                    "last_modified_safe_for_precondition"
                ]
            )
            self.assertEqual(payload["error"]["code"], "sync_pending")
            self.assertEqual(
                set(payload["after"]),
                {"id", "url_attachment"},
            )
            self.assertEqual(
                [request["operation"] for request in bridge_requests],
                ["update_reminder", "read_reminder"],
            )

    def test_attachment_and_final_read_failures_keep_partial_success_priority(self) -> None:
        url = "https://example.com/results"
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            bridge = temp_root / "mock_eventkit.py"
            adapter = temp_root / "mock_adapter.py"
            mock_eventkit_bridge(bridge, final_read_mode="failure")
            mock_failing_visible_url_adapter(adapter)
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "update_reminder",
                            "arguments": {
                                "reminder_id": "REMINDER-1",
                                "expected_last_modified": "2026-08-06T00:00:00Z",
                                "patch": {"url": url},
                            },
                        },
                    },
                ],
                adapter_path=adapter,
                eventkit_bridge_path=bridge,
            )

        result = responses[1]["result"]
        payload = result["structuredContent"]
        self.assertFalse(result["isError"], result)
        self.assertEqual(payload["status"], "partial_success")
        self.assertEqual(payload["verification"]["state"], "partial")
        self.assertEqual(payload["verification"]["url_attachment"]["state"], "failed")
        self.assertFalse(
            payload["verification"]["eventkit_final_read"][
                "last_modified_safe_for_precondition"
            ]
        )
        self.assertEqual(payload["after"], {"id": "REMINDER-1"})
        self.assertEqual(
            [warning["code"] for warning in payload["warnings"][-2:]],
            ["schema_mismatch", "eventkit_final_read_pending"],
        )

    def test_eventkit_pending_skips_attachment_and_final_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            bridge = temp_root / "pending_eventkit.py"
            adapter = temp_root / "mock_adapter.py"
            bridge.write_text(
                """import json
import sys
from pathlib import Path

json.load(sys.stdin)
marker = Path(__file__).with_suffix('.called')
marker.write_text((marker.read_text() if marker.exists() else '') + 'called\\n')
print('not-json')
""",
                encoding="utf-8",
            )
            mock_visible_url_adapter(adapter)
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "create_reminder",
                            "arguments": {
                                "calendar_id": "LIST-A",
                                "title": "Pending URL",
                                "url": "https://example.com/results",
                                "idempotency_key": "eventkit:create:pending-url:2026",
                            },
                        },
                    },
                ],
                adapter_path=adapter,
                eventkit_bridge_path=bridge,
                home_path=temp_root / "home",
            )
            bridge_calls = bridge.with_suffix(".called").read_text(encoding="utf-8")

        result = responses[1]["result"]
        self.assertFalse(result["isError"], result)
        self.assertEqual(
            result["structuredContent"]["status"],
            "committed_verification_pending",
        )
        self.assertEqual(bridge_calls.splitlines(), ["called"])
        self.assertFalse(adapter.with_suffix(".called").exists())

    def test_url_attachment_failure_preserves_the_eventkit_write_as_partial_success(self) -> None:
        url = "https://example.com/results"
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            bridge = temp_root / "mock_eventkit.py"
            adapter = temp_root / "mock_adapter.py"
            mock_eventkit_bridge(bridge)
            mock_failing_visible_url_adapter(adapter)
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "create_reminder",
                            "arguments": {
                                "calendar_id": "LIST-A",
                                "title": "Check results",
                                "url": url,
                                "idempotency_key": "eventkit:create:partial-url:2026",
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "create_reminder",
                            "arguments": {
                                "calendar_id": "LIST-A",
                                "title": "Check results",
                                "url": url,
                                "idempotency_key": "eventkit:create:partial-url:2026",
                            },
                        },
                    },
                ],
                adapter_path=adapter,
                eventkit_bridge_path=bridge,
                home_path=temp_root / "home",
            )
            bridge_requests = [
                json.loads(line)
                for line in bridge.with_suffix(".requests")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        result = responses[1]["result"]
        payload = result["structuredContent"]
        self.assertFalse(result["isError"], result)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "partial_success")
        self.assertEqual(payload["after"]["id"], "REMINDER-CREATED")
        self.assertEqual(
            payload["after"]["last_modified"],
            "2026-08-25T00:00:01.000Z",
        )
        self.assertEqual(payload["verification"]["url_attachment"]["state"], "failed")
        self.assertFalse(payload["verification"]["url_attachment"]["attachment_active"])
        self.assertEqual(payload["warnings"][-1]["code"], "schema_mismatch")
        self.assertEqual(
            payload["recovery"]["url_attachment"]["semantics"],
            "call_attach_url_to_reminder_after_fresh_attachment_read",
        )
        replay = responses[2]["result"]["structuredContent"]
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "partial_success")
        self.assertEqual(replay["warnings"][-1]["code"], "schema_mismatch")
        self.assertEqual(
            [request["operation"] for request in bridge_requests],
            ["create_reminder", "read_reminder"],
        )

    def test_pending_eventkit_create_retry_replays_a_valid_receipt(self) -> None:
        create_arguments = {
            "calendar_id": "LIST-A",
            "title": "Pending creation",
            "idempotency_key": "eventkit:create:pending:2026",
        }
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            bridge = temp_root / "pending_eventkit.py"
            bridge.write_text(
                """import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
marker = Path(__file__).with_suffix('.called')
marker.write_text((marker.read_text() if marker.exists() else '') + 'called\\n')
print('not-json')
""",
                encoding="utf-8",
            )
            call = {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "create_reminder", "arguments": create_arguments},
            }
            responses = run_server(
                [initialize(), {**call, "id": 2}, {**call, "id": 3}],
                eventkit_bridge_path=bridge,
                home_path=temp_root / "home",
            )
            bridge_call_count = len(
                bridge.with_suffix(".called").read_text(encoding="utf-8").splitlines()
            )

        first = responses[1]["result"]
        replay = responses[2]["result"]
        self.assertFalse(first["isError"])
        self.assertFalse(replay["isError"], replay)
        self.assertEqual(first["structuredContent"]["after"], {})
        self.assertEqual(replay["structuredContent"]["after"], {})
        self.assertTrue(replay["structuredContent"]["replayed"])
        self.assertEqual(replay["structuredContent"]["error"]["code"], "sync_pending")
        self.assertEqual(bridge_call_count, 1)

    def test_mock_tag_cleanup_requires_digest_and_preserves_backup_default(self) -> None:
        digest = "a" * 64
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp) / "mock_adapter.py"
            mock_adapter(adapter)
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "cleanup_unused_reminder_tags",
                            "arguments": {
                                "tag": "unused",
                                "candidate_digest": digest,
                            },
                        },
                    },
                ],
                adapter_path=adapter,
            )

        argv = responses[1]["result"]["structuredContent"]["argv"]
        self.assertEqual(
            argv,
            [
                "cleanup_tags",
                "--apply",
                "--tag",
                "unused",
                "--preview-digest",
                digest,
                "--limit",
                "50",
            ],
        )
        self.assertNotIn("--no-backup", argv)

    def test_attachment_tools_use_exact_ids_digest_and_hidden_reminderkit_backend(self) -> None:
        digest = "b" * 64
        calls = [
            (
                "attach_image_to_reminder",
                {
                    "reminder_id": REMINDER_ID,
                    "image_path": "/tmp/reference.png",
                    "if_version": 10,
                    "idempotency_key": "attach:image:reference",
                },
                [
                    "attach_image",
                    "--backend",
                    "reminderkit",
                    "--id",
                    REMINDER_ID,
                    "--image",
                    "/tmp/reference.png",
                    "--if-version",
                    "10",
                    "--idempotency-key",
                    "attach:image:reference",
                ],
            ),
            (
                "preview_reminder_attachment_repairs",
                {"search": "receipt", "list_name": "Inbox", "limit": 20},
                ["repair_attachments", "--search", "receipt", "--list", "Inbox", "--limit", "20"],
            ),
            (
                "apply_reminder_attachment_repairs",
                {
                    "search": "receipt",
                    "list_name": "Inbox",
                    "limit": 20,
                    "candidate_digest": digest,
                },
                [
                    "repair_attachments",
                    "--apply",
                    "--search",
                    "receipt",
                    "--list",
                    "Inbox",
                    "--limit",
                    "20",
                    "--preview-digest",
                    digest,
                ],
            ),
            (
                "replace_reminder_attachment",
                {
                    "reminder_id": REMINDER_ID,
                    "attachment_id": REMINDER_ID,
                    "url": "https://example.com/replacement",
                    "if_version": 11,
                    "idempotency_key": "replace:url:reference",
                },
                [
                    "replace_attachment",
                    "--id",
                    REMINDER_ID,
                    "--attachment-id",
                    REMINDER_ID,
                    "--url",
                    "https://example.com/replacement",
                    "--if-version",
                    "11",
                    "--idempotency-key",
                    "replace:url:reference",
                ],
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp) / "mock_adapter.py"
            mock_adapter(adapter)
            messages = [initialize()]
            for request_id, (name, arguments, _) in enumerate(calls, start=2):
                messages.append(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    }
                )
            responses = run_server(messages, adapter_path=adapter)

        for response, (_, _, expected) in zip(responses[1:], calls, strict=True):
            result = response["result"]
            self.assertFalse(result["isError"])
            self.assertEqual(result["structuredContent"]["argv"], expected)
            self.assertNotIn("--no-backup", result["structuredContent"]["argv"])

    def test_attachment_read_preserves_reminder_version_for_guarded_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp) / "mock_adapter.py"
            mock_adapter(adapter)
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "list_reminder_attachments",
                            "arguments": {"reminder_id": REMINDER_ID, "limit": 2},
                        },
                    },
                ],
                adapter_path=adapter,
            )

        payload = responses[1]["result"]["structuredContent"]
        self.assertEqual(payload["reminder_version"], 12)
        self.assertEqual(payload["attachments"], [{"id": "A"}, {"id": "B"}])
        self.assertTrue(payload["truncated"])

    def test_mutation_success_without_receipt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp) / "legacy_adapter.py"
            adapter.write_text(
                "import json\nprint(json.dumps({'ok': True, 'created': True}))\n",
                encoding="utf-8",
            )
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "create_reminder_list",
                            "arguments": {"name": "Inbox"},
                        },
                    },
                ],
                adapter_path=adapter,
            )

        result = responses[1]["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error"]["code"],
            "invalid_adapter_receipt",
        )

    def test_manual_repair_failure_without_receipt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp) / "legacy_partial_adapter.py"
            adapter.write_text(
                "import json\nprint(json.dumps({'ok': False, 'status': "
                "'failed_manual_repair_required', 'error': {'code': 'sync_pending'}}))\n",
                encoding="utf-8",
            )
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "replace_reminder_attachment",
                            "arguments": {
                                "reminder_id": REMINDER_ID,
                                "attachment_id": REMINDER_ID,
                                "url": "https://example.com/replacement",
                                "if_version": 2,
                                "idempotency_key": "replace:manual:repair",
                            },
                        },
                    },
                ],
                adapter_path=adapter,
            )

        result = responses[1]["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error"]["code"],
            "invalid_adapter_receipt",
        )

    def test_compensated_failure_without_full_receipt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = Path(tmp) / "incomplete_compensation_adapter.py"
            adapter.write_text(
                "import json\nprint(json.dumps({'ok': False, 'status': "
                "'failed_no_mutation', 'operation': 'replace_attachment', "
                "'error': {'code': 'sync_pending'}}))\n",
                encoding="utf-8",
            )
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "replace_reminder_attachment",
                            "arguments": {
                                "reminder_id": REMINDER_ID,
                                "attachment_id": REMINDER_ID,
                                "url": "https://example.com/replacement",
                                "if_version": 2,
                                "idempotency_key": "replace:compensated",
                            },
                        },
                    },
                ],
                adapter_path=adapter,
            )

        result = responses[1]["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error"]["code"],
            "invalid_adapter_receipt",
        )

    def test_invalid_arguments_are_tool_errors_and_never_launch_adapter(self) -> None:
        responses = run_server(
            [
                initialize(),
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "delete_reminder",
                        "arguments": {"reminder_id": "not-an-id"},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "create_reminder",
                        "arguments": {
                            "calendar_id": "LIST-A",
                            "title": "Conflict",
                            "recurrence_rules": [{"frequency": "daily", "interval": 1}],
                            "idempotency_key": "invalid:no-due",
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "fetch_reminders",
                        "arguments": {
                            "modified_after": "2026-08-06T09:00:00+09:00",
                            "limit": 10,
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "attach_image_to_reminder",
                        "arguments": {
                            "reminder_id": REMINDER_ID,
                            "image_path": "relative.png",
                            "if_version": 1,
                            "idempotency_key": "invalid:relative",
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "replace_reminder_attachment",
                        "arguments": {
                            "reminder_id": REMINDER_ID,
                            "attachment_id": REMINDER_ID,
                            "image_path": "/tmp/image.png",
                            "url": "https://example.com/image.png",
                            "if_version": 1,
                            "idempotency_key": "invalid:both",
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "create_reminder",
                        "arguments": {
                            "calendar_id": "LIST-A",
                            "title": "Malformed URL",
                            "url": "http://[::1",
                            "idempotency_key": "invalid:malformed-url",
                        },
                    },
                },
            ],
            adapter_path=Path("/definitely/missing/mock-adapter.py"),
        )

        for response in responses[1:]:
            result = response["result"]
            self.assertTrue(result["isError"])
            self.assertEqual(
                result["structuredContent"]["error"]["code"],
                "invalid_arguments",
            )

    def test_unknown_tool_is_a_protocol_error(self) -> None:
        responses = run_server(
            [
                initialize(),
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "not_a_tool", "arguments": {}},
                }
            ]
        )

        self.assertEqual(responses[1]["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
