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
MUTATION_TOOLS = {
    "create_reminder",
    "change_reminder",
    "delete_reminder",
    "recover_deleted_reminder",
    "ensure_reminder_list",
    "create_reminder_section",
    "organize_reminder",
    "change_reminder_attachment",
}
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
        elif name == "list_reminder_lists":
            data = {"items": [], "returned": 0, "truncated": False}
        elif name == "fetch_reminders":
            data = {"items": [], "returned": 0, "has_more": False}
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

    def call_with_state(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        payload = self.call(name, arguments)
        return payload, ("committed" if name in MUTATION_TOOLS else None)


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
                self.assertIs(
                    tool["annotations"]["openWorldHint"],
                    tool["name"] in MUTATION_TOOLS,
                )
                assert_bounded(tool["inputSchema"], f"$.{tool['name']}")

    def test_tool_discovery_does_not_eagerly_import_facade_backends(self) -> None:
        probe = """
import json
import sys
sys.path.insert(0, 'mcp')
import server
runtime = server.McpRuntime()
runtime.handle({
    'jsonrpc': '2.0',
    'id': 1,
    'method': 'initialize',
    'params': {
        'protocolVersion': '2025-11-25',
        'capabilities': {},
        'clientInfo': {'name': 'lazy-import-probe', 'version': '1'},
    },
})
runtime.handle({
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
    def test_adapter_dispatch_certainty_is_parent_owned(self) -> None:
        missing = self.server.invoke_adapter(
            ["read_reminder"],
            backend_paths=self.backend_paths(
                adapter=Path("/definitely/missing/reminders_adapter.py")
            ),
        )

        self.assertTrue(missing.is_error)
        self.assertTrue(missing.proves_not_started)
        self.assertNotIn("__dispatch_phase", missing.payload)

        with mock.patch.object(
            self.server,
            "run_bounded_process",
            side_effect=self.server.ProcessLaunchError(
                argv=(sys.executable,),
                cause=OSError("process transport failed"),
            ),
        ):
            process_failed = self.server.invoke_adapter(
                ["create_reminder"],
                backend_paths=self.backend_paths(adapter=Path(__file__)),
            )

        self.assertTrue(process_failed.is_error)
        self.assertTrue(process_failed.proves_not_started)

        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"ok":true,"status":"verified"}',
            stderr="",
        )
        with mock.patch.object(self.server, "run_bounded_process", return_value=completed):
            success = self.server.invoke_adapter(
                ["read_reminder"],
                backend_paths=self.backend_paths(adapter=Path(__file__)),
            )

        self.assertFalse(success.is_error)
        self.assertFalse(success.proves_not_started)
        self.assertNotIn("__dispatch_phase", success.payload)

        spoofed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=(
                '{"ok":false,"__dispatch_phase":"not_started",'
                '"mutation_not_started":true,'
                '"error":{"code":"invalid_adapter_response"}}'
            ),
            stderr="",
        )
        with mock.patch.object(self.server, "run_bounded_process", return_value=spoofed):
            child_result = self.server.invoke_adapter(
                ["read_reminder"],
                backend_paths=self.backend_paths(adapter=Path(__file__)),
            )

        self.assertTrue(child_result.is_error)
        self.assertFalse(child_result.proves_not_started)
        self.assertIs(child_result.payload["mutation_not_started"], True)
        self.assertNotIn("__dispatch_phase", child_result.payload)

    def test_eventkit_dispatch_certainty_marks_only_parent_proven_prelaunch_failures(
        self,
    ) -> None:
        missing = self.server.invoke_eventkit_bridge(
            "create_reminder",
            {"calendar_id": "LIST-1", "title": "Missing bridge"},
            backend_paths=self.backend_paths(
                eventkit_bridge=Path("/definitely/missing/eventkit_bridge.py")
            ),
        )

        self.assertTrue(missing.is_error)
        self.assertTrue(missing.proves_not_started)
        self.assertNotIn("__dispatch_phase", missing.payload)

        too_large = self.server.invoke_eventkit_bridge(
            "create_reminder",
            {
                "calendar_id": "LIST-1",
                "title": "x" * self.server.MAX_EVENTKIT_REQUEST_BYTES,
            },
            backend_paths=self.backend_paths(eventkit_bridge=Path(__file__)),
        )

        self.assertTrue(too_large.is_error)
        self.assertTrue(too_large.proves_not_started)

        with mock.patch.object(
            self.server,
            "run_bounded_process",
            side_effect=self.server.ProcessLaunchError(
                argv=(sys.executable,),
                cause=OSError("launch denied"),
            ),
        ):
            launch_failed = self.server.invoke_eventkit_bridge(
                "create_reminder",
                {"calendar_id": "LIST-1", "title": "Launch failure"},
                backend_paths=self.backend_paths(eventkit_bridge=Path(__file__)),
            )

        self.assertTrue(launch_failed.is_error)
        self.assertTrue(launch_failed.proves_not_started)
        self.assertEqual(
            launch_failed.payload["error"]["code"],
            "eventkit_bridge_process_failed",
        )
        self.assertNotIn("__dispatch_phase", launch_failed.payload)

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
                    "mutation_not_started": True,
                }
            ),
            stderr="",
        )
        with mock.patch.object(self.server, "run_bounded_process", return_value=spoofed):
            child_result = self.server.invoke_eventkit_bridge(
                "doctor",
                {},
                backend_paths=self.backend_paths(eventkit_bridge=Path(__file__)),
            )

        self.assertFalse(child_result.is_error)
        self.assertFalse(child_result.proves_not_started)
        self.assertIs(child_result.payload["mutation_not_started"], True)
        self.assertNotIn("__dispatch_phase", child_result.payload)

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
        with mock.patch.object(
            self.server,
            "run_bounded_process",
            return_value=spoofed_create,
        ):
            create_result = self.server.invoke_eventkit_bridge(
                "create_reminder",
                {"calendar_id": "LIST-1", "title": "Spoofed provenance"},
                backend_paths=self.backend_paths(eventkit_bridge=Path(__file__)),
            )

        self.assertFalse(create_result.is_error)
        self.assertFalse(create_result.proves_not_started)
        self.assertEqual(
            create_result.payload["status"],
            "committed_verification_pending",
        )
        self.assertIsNone(
            create_result.payload["verification"]["write_performed"]
        )
        self.assertNotIn("__dispatch_phase", create_result.payload)

    def test_postlaunch_adapter_failures_keep_unknown_dispatch_certainty(self) -> None:
        decode_cause = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        cases = (
            (
                self.server.ProcessTimeoutError(
                    timeout_s=45,
                    argv=(sys.executable,),
                    pid=123,
                    returncode=-15,
                ),
                "adapter_timeout",
            ),
            (
                self.server.ProcessOutputLimitError(
                    stream="stdout",
                    limit=self.server.MAX_ADAPTER_STDOUT_BYTES,
                    argv=(sys.executable,),
                    pid=123,
                    returncode=-15,
                ),
                "adapter_output_too_large",
            ),
            (
                self.server.ProcessDecodeError(
                    stream="stdout",
                    cause=decode_cause,
                    argv=(sys.executable,),
                    pid=123,
                    returncode=0,
                    stdout=b"\xff",
                ),
                "invalid_adapter_response",
            ),
        )

        for failure, expected_code in cases:
            with self.subTest(expected_code=expected_code), mock.patch.object(
                self.server,
                "run_bounded_process",
                side_effect=failure,
            ):
                result = self.server.invoke_adapter(
                    ["create_reminder"],
                    backend_paths=self.backend_paths(adapter=Path(__file__)),
                )

            self.assertTrue(result.is_error)
            self.assertFalse(result.proves_not_started)
            self.assertEqual(result.payload["error"]["code"], expected_code)

    def test_postlaunch_eventkit_failures_keep_mutation_outcome_unknown(self) -> None:
        decode_cause = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        cases = (
            (
                self.server.ProcessTimeoutError(
                    timeout_s=80,
                    argv=(sys.executable,),
                    pid=123,
                    returncode=-15,
                ),
                "eventkit_bridge_timeout",
            ),
            (
                self.server.ProcessOutputLimitError(
                    stream="stderr",
                    limit=self.server.MAX_HOST_STDERR_BYTES,
                    argv=(sys.executable,),
                    pid=123,
                    returncode=-15,
                ),
                "eventkit_bridge_output_too_large",
            ),
            (
                self.server.ProcessDecodeError(
                    stream="stdout",
                    cause=decode_cause,
                    argv=(sys.executable,),
                    pid=123,
                    returncode=0,
                    stdout=b"\xff",
                ),
                "invalid_eventkit_bridge_response",
            ),
        )

        for failure, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason), mock.patch.object(
                self.server,
                "run_bounded_process",
                side_effect=failure,
            ):
                result = self.server.invoke_eventkit_bridge(
                    "create_reminder",
                    {"calendar_id": "LIST-1", "title": "Outcome unknown"},
                    backend_paths=self.backend_paths(eventkit_bridge=Path(__file__)),
                )

            self.assertFalse(result.is_error)
            self.assertFalse(result.proves_not_started)
            self.assertEqual(result.payload["status"], "committed_verification_pending")
            self.assertEqual(result.payload["error"]["reason_code"], expected_reason)

    def test_doctor_maps_bounded_process_failures_to_stable_codes(self) -> None:
        decode_cause = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        cases = (
            (
                self.server.ProcessTimeoutError(
                    timeout_s=45,
                    argv=(sys.executable,),
                    pid=123,
                    returncode=-15,
                ),
                "doctor_timeout",
            ),
            (
                self.server.ProcessOutputLimitError(
                    stream="stdout",
                    limit=self.server.MAX_ADAPTER_STDOUT_BYTES,
                    argv=(sys.executable,),
                    pid=123,
                    returncode=-15,
                ),
                "doctor_output_too_large",
            ),
            (
                self.server.ProcessDecodeError(
                    stream="stdout",
                    cause=decode_cause,
                    argv=(sys.executable,),
                    pid=123,
                    returncode=0,
                    stdout=b"\xff",
                ),
                "invalid_doctor_response",
            ),
            (
                self.server.ProcessLaunchError(
                    argv=(sys.executable,),
                    cause=OSError("launch denied"),
                ),
                "doctor_launch_failed",
            ),
        )

        for failure, expected_code in cases:
            with self.subTest(expected_code=expected_code), mock.patch.object(
                self.server,
                "run_bounded_process",
                side_effect=failure,
            ):
                payload, is_error = self.server.invoke_doctor(
                    {},
                    backend_paths=self.backend_paths(doctor=Path(__file__)),
                )

            self.assertTrue(is_error)
            self.assertEqual(payload["error"]["code"], expected_code)

    def setUp(self) -> None:
        from mcp import server

        self.server = server
        self.use_runtime()

    def backend_paths(
        self,
        *,
        adapter: Path | None = None,
        eventkit_bridge: Path | None = None,
        doctor: Path | None = None,
    ) -> Any:
        defaults = self.server.DEFAULT_BACKEND_PATHS
        return self.server.BackendPaths(
            adapter=adapter or defaults.adapter,
            eventkit_bridge=eventkit_bridge or defaults.eventkit_bridge,
            doctor=doctor or defaults.doctor,
        )

    def use_runtime(
        self,
        *,
        core: Any | None = None,
        native: Any | None = None,
        recovery: Any | None = None,
        diagnostics: Any | None = None,
        clock: Any | None = None,
        max_calls_per_minute: int | None = None,
    ) -> Any:
        overrides = {
            name: facade
            for name, facade in {
                "core": core,
                "native": native,
                "recovery": recovery,
                "diagnostics": diagnostics,
            }.items()
            if facade is not None
        }
        self.dispatch = self.server._LocalToolDispatch(
            self.server.DEFAULT_BACKEND_PATHS,
            facade_overrides=overrides,
        )
        options: dict[str, Any] = {"dispatch": self.dispatch}
        if clock is not None:
            options["clock"] = clock
        if max_calls_per_minute is not None:
            options["max_calls_per_minute"] = max_calls_per_minute
        self.runtime = self.server.McpRuntime(**options)
        return self.runtime

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
        response = self.runtime.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )

        self.assertEqual(response["error"]["code"], -32002)

    def test_initialization_state_is_isolated_per_runtime(self) -> None:
        first = self.server.McpRuntime(
            dispatch=self.server._LocalToolDispatch(
                self.server.DEFAULT_BACKEND_PATHS,
                facade_overrides={"core": RecordingFacade()},
            )
        )
        second = self.server.McpRuntime(
            dispatch=self.server._LocalToolDispatch(
                self.server.DEFAULT_BACKEND_PATHS,
                facade_overrides={"core": RecordingFacade()},
            )
        )

        initialized = first.handle(initialize())
        second_list = second.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )

        self.assertIn("result", initialized)
        self.assertEqual(second_list["error"]["code"], -32002)

    def test_rate_limit_state_is_isolated_per_runtime(self) -> None:
        def clock() -> float:
            return 100.0

        first_facade = RecordingFacade()
        second_facade = RecordingFacade()
        first = self.server.McpRuntime(
            dispatch=self.server._LocalToolDispatch(
                self.server.DEFAULT_BACKEND_PATHS,
                facade_overrides={"core": first_facade},
            ),
            clock=clock,
            max_calls_per_minute=1,
        )
        second = self.server.McpRuntime(
            dispatch=self.server._LocalToolDispatch(
                self.server.DEFAULT_BACKEND_PATHS,
                facade_overrides={"core": second_facade},
            ),
            clock=clock,
            max_calls_per_minute=1,
        )

        first_result = first.call_tool("read_reminder", {"reminder_id": REMINDER_ID})
        limited = first.call_tool("read_reminder", {"reminder_id": REMINDER_ID})
        second_result = second.call_tool("read_reminder", {"reminder_id": REMINDER_ID})

        self.assertFalse(first_result["isError"])
        self.assertEqual(
            limited["structuredContent"]["error"]["code"], "rate_limited"
        )
        self.assertFalse(second_result["isError"])
        self.assertEqual(len(first_facade.calls), 1)
        self.assertEqual(len(second_facade.calls), 1)

    def test_lazy_facade_graph_is_owned_by_each_dispatch(self) -> None:
        first = self.server._LocalToolDispatch(self.server.DEFAULT_BACKEND_PATHS)
        second = self.server._LocalToolDispatch(self.server.DEFAULT_BACKEND_PATHS)

        runtime = self.server.McpRuntime(dispatch=first)
        runtime.handle(initialize())
        runtime.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )

        self.assertIsNone(first._core)
        self.assertIsNone(first._native)
        self.assertIsNone(first._recovery)
        self.assertIsNone(first._diagnostics)
        first_core = first.core_facade()
        self.assertIs(first._core, first_core)
        self.assertIsNone(second._core)

    def test_notification_shaped_mutation_cannot_execute(self) -> None:
        facade = RecordingFacade()
        self.use_runtime(core=facade)
        self.runtime.handle(initialize())

        response = self.runtime.handle(
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
        self.runtime.handle(initialize())

        response = self.runtime.handle(
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
        self.use_runtime(core=core, native=native)

        read_result = self.runtime.call_tool("read_reminder", {})
        mutation_result = self.runtime.call_tool(
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
        self.use_runtime(core=core)

        with mock.patch.object(self.server.sys, "version_info", (3, 10, 14)):
            result = self.runtime.call_tool(
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
        self.use_runtime(core=core)

        result = self.runtime.call_tool(
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
        self.use_runtime(
            core=core,
            native=native,
            recovery=recovery,
            diagnostics=diagnostics,
        )

        for name in sorted(PUBLIC_TOOLS):
            with self.subTest(tool=name):
                result = self.runtime.call_tool(
                    name, copy.deepcopy(VALID_ARGUMENTS[name])
                )
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
            self.use_runtime(core=facade, native=facade, recovery=facade)

            with self.subTest(tool=name):
                result = self.runtime.call_tool(
                    name, copy.deepcopy(VALID_ARGUMENTS[name])
                )
                payload = result["structuredContent"]
                self.assertTrue(result["isError"])
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
        self.use_runtime(recovery=facade)

        result = self.runtime.call_tool(
            "recover_deleted_reminder",
            copy.deepcopy(VALID_ARGUMENTS["recover_deleted_reminder"]),
        )

        payload = result["structuredContent"]
        self.assertTrue(result["isError"])
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
        self.use_runtime(recovery=facade)

        result = self.runtime.call_tool("recover_deleted_reminder", arguments)

        payload = result["structuredContent"]
        self.assertEqual(facade.calls, 1)
        self.assertEqual(facade.name, "recover_deleted_reminder")
        self.assertEqual(payload["status"], "committed_verification_pending")
        self.assertEqual(
            payload["error"]["reason_code"], "public_result_contract_failed"
        )
        self.assertTrue(payload["verification"]["write_performed"])
        self.assertEqual(payload["next_action"]["tool"], "read_reminder")
        self.assertNotIn("mutation_state", payload)
        summary = json.loads(result["content"][0]["text"])
        self.assertEqual(summary["outcome"], "attention_required")
        self.assertEqual(summary["write_state"], "committed_unverified")

    def test_invalid_independent_mutation_state_fails_closed(self) -> None:
        arguments = copy.deepcopy(VALID_ARGUMENTS["create_reminder"])

        class InvalidStateFacade:
            def call_with_state(
                self,
                name: str,
                supplied: Mapping[str, Any],
            ) -> tuple[dict[str, Any], str]:
                return valid_public_result(name, supplied), "commited"

            def call(self, name: str, supplied: Mapping[str, Any]) -> dict[str, Any]:
                raise AssertionError("mutation dispatch must use call_with_state")

        self.use_runtime(core=InvalidStateFacade())

        result = self.runtime.call_tool("create_reminder", arguments)

        payload = result["structuredContent"]
        self.assertTrue(result["isError"])
        self.assertEqual(payload["status"], "committed_verification_pending")
        self.assertIsNone(payload["verification"]["write_performed"])
        self.assertEqual(
            payload["error"]["reason_code"],
            "public_result_contract_failed",
        )
        summary = json.loads(result["content"][0]["text"])
        self.assertEqual(summary["outcome"], "attention_required")
        self.assertEqual(summary["write_state"], "unknown")

    def test_read_facade_cannot_smuggle_mutation_state(self) -> None:
        class InvalidReadStateFacade:
            def call_with_state(
                self,
                name: str,
                supplied: Mapping[str, Any],
            ) -> tuple[dict[str, Any], str]:
                return valid_public_result(name, supplied), "committed"

            def call(self, name: str, supplied: Mapping[str, Any]) -> dict[str, Any]:
                raise AssertionError("read dispatch must use call_with_state")

        self.use_runtime(core=InvalidReadStateFacade())

        result = self.runtime.call_tool(
            "read_reminder",
            {"reminder_id": REMINDER_ID},
        )

        payload = result["structuredContent"]
        self.assertTrue(result["isError"])
        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertEqual(
            payload["error"]["reason_code"],
            "public_result_contract_failed",
        )

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
        self.use_runtime(core=facade)

        result = self.runtime.call_tool(
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

    def test_mutation_attention_receipts_are_mcp_tool_errors(self) -> None:
        cases = {
            "verified": False,
            "unchanged": False,
            "committed_verification_pending": True,
            "partial_success": True,
            "failed_no_mutation": True,
            "failed_manual_repair_required": True,
        }
        successful_receipts = {
            "verified",
            "unchanged",
            "committed_verification_pending",
            "partial_success",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.assertIs(
                    self.server._mcp_tool_call_is_error(
                        "change_reminder",
                        {"ok": status in successful_receipts, "status": status},
                    ),
                    expected,
                )

        self.assertFalse(
            self.server._mcp_tool_call_is_error(
                "diagnose_reminders",
                {"ok": True, "status": "verified"},
            )
        )

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

    def test_malformed_eventkit_collection_is_returned_as_an_mcp_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            eventkit = Path(temporary) / "mock_eventkit.py"
            mock_eventkit_bridge(eventkit)
            source = eventkit.read_text(encoding="utf-8")
            eventkit.write_text(
                source.replace('"items": [{', '"items": [None, {', 1),
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
                            "name": "list_reminder_lists",
                            "arguments": {"limit": 1},
                        },
                    },
                ],
                eventkit_bridge_path=eventkit,
            )

        result = responses[1]["result"]
        self.assertTrue(result["isError"])
        structured = result["structuredContent"]
        self.assertEqual(structured["status"], "failed_no_mutation")
        self.assertEqual(
            structured["error"]["reason_code"],
            "invalid_eventkit_bridge_response",
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
            runtime = self.server.McpRuntime()
            paths = runtime._dispatch._backend_paths
            self.assertEqual(
                paths.adapter,
                self.server.DEFAULT_ADAPTER_PATH,
            )
            self.assertEqual(
                paths.eventkit_bridge,
                self.server.DEFAULT_EVENTKIT_BRIDGE_PATH,
            )
            self.assertEqual(
                paths.doctor,
                self.server.DEFAULT_DOCTOR_PATH,
            )


if __name__ == "__main__":
    unittest.main()
