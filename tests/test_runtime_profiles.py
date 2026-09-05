"""Exercise the public Core/Experimental boundary without personal data access."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_mcp_server import (
    CORE_TOOLS,
    DIAGNOSTIC_TOOLS,
    MUTATION_TOOLS,
    NATIVE_TOOLS,
    PLUGIN_ROOT,
    PUBLIC_TOOLS,
    RECOVERY_TOOLS,
    VALID_ARGUMENTS,
    RecordingFacade,
    initialize,
    mock_eventkit_bridge,
    run_server,
)
from test_mcp_v2_core import native_reminder, read_receipt
from test_mcp_v2_core_backend import transport, valid_eventkit_receipt

from mcp import server


PRIVATE_TOOLS = NATIVE_TOOLS | RECOVERY_TOOLS
LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def discovered_tools(runtime: server.McpRuntime) -> list[dict]:
    runtime.handle(initialize())
    return runtime.handle(LIST_TOOLS)["result"]["tools"]


class RuntimeProfileTests(unittest.TestCase):
    def test_stdio_discovery_selects_nine_or_fifteen_tools(self) -> None:
        for experimental in (False, True):
            with self.subTest(experimental=experimental):
                responses = run_server(
                    [initialize(), LIST_TOOLS], enable_experimental=experimental
                )
                tools = responses[1]["result"]["tools"]
                expected = PUBLIC_TOOLS if experimental else CORE_TOOLS | DIAGNOSTIC_TOOLS
                self.assertEqual({tool["name"] for tool in tools}, expected)
                self.assertEqual(len(tools), 15 if experimental else 9)
                instructions = responses[0]["result"]["instructions"]
                self.assertIn(
                    "Experimental tools are enabled" if experimental else "Core mode",
                    instructions,
                )

    def test_source_cli_discovery_works_in_both_modes(self) -> None:
        # This exercises the actual entry point, including argument parsing.
        # Discovery does not construct a facade or access any backend.
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        wire = "".join(json.dumps(item) + "\n" for item in (initialize(), LIST_TOOLS))
        for experimental in (False, True):
            with self.subTest(experimental=experimental):
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(PLUGIN_ROOT / "mcp" / "server.py"),
                        *(["--experimental"] if experimental else []),
                    ],
                    cwd=PLUGIN_ROOT,
                    env=env,
                    input=wire,
                    text=True,
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                messages = [json.loads(line) for line in completed.stdout.splitlines()]
                expected = PUBLIC_TOOLS if experimental else CORE_TOOLS | DIAGNOSTIC_TOOLS
                self.assertEqual(
                    {tool["name"] for tool in messages[1]["result"]["tools"]}, expected
                )

    def test_source_cli_rejects_unknown_option_before_serving(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "mcp" / "server.py"), "--enable-private"],
            cwd=PLUGIN_ROOT,
            input=json.dumps(initialize()) + "\n",
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn("unrecognized arguments", completed.stderr)

    def test_all_private_names_are_rejected_before_dispatch(self) -> None:
        dispatch = mock.Mock(side_effect=AssertionError("Private dispatch must not run"))
        runtime = server.McpRuntime(dispatch=dispatch)
        runtime.handle(initialize())
        for name in sorted(PRIVATE_TOOLS):
            for via_protocol in (False, True):
                with self.subTest(name=name, via_protocol=via_protocol):
                    arguments = copy.deepcopy(VALID_ARGUMENTS[name])
                    if via_protocol:
                        result = runtime.handle(
                            {
                                "jsonrpc": "2.0",
                                "id": 3,
                                "method": "tools/call",
                                "params": {"name": name, "arguments": arguments},
                            }
                        )["result"]
                    else:
                        result = runtime.call_tool(name, arguments)
                    payload = result["structuredContent"]
                    self.assertTrue(result["isError"])
                    self.assertEqual(payload["status"], "failed_no_mutation")
                    self.assertEqual(payload["error"]["reason_code"], "experimental_disabled")
                    if name in MUTATION_TOOLS:
                        self.assertIs(payload["verification"]["write_performed"], False)
        dispatch.assert_not_called()

    def test_explicit_experimental_mode_admits_all_six_private_routes(self) -> None:
        native, recovery = RecordingFacade(), RecordingFacade()
        dispatch = server._LocalToolDispatch(
            server.DEFAULT_BACKEND_PATHS,
            facade_overrides={"native": native, "recovery": recovery},
            enable_experimental=True,
        )
        runtime = server.McpRuntime(dispatch=dispatch, enable_experimental=True)
        for name in sorted(PRIVATE_TOOLS):
            with self.subTest(name=name):
                result = runtime.call_tool(name, copy.deepcopy(VALID_ARGUMENTS[name]))
                self.assertFalse(result["isError"], result["structuredContent"])
        self.assertEqual({name for name, _ in native.calls}, NATIVE_TOOLS)
        self.assertEqual({name for name, _ in recovery.calls}, RECOVERY_TOOLS)

    def test_private_diagnosis_and_toolchain_are_gated_before_dispatch(self) -> None:
        dispatch = mock.Mock(side_effect=AssertionError("Private diagnosis must not run"))
        runtime = server.McpRuntime(dispatch=dispatch)
        diagnostic = server.TOOLS_BY_NAME["diagnose_reminders"]["inputSchema"]["properties"]
        private_scopes = set(diagnostic["scope"]["enum"]) - {"core", "access", "packaging"}
        cases = [{"scope": scope} for scope in sorted(private_scopes)]
        cases.extend(
            {"scope": scope, "execution_mode": "experimental_toolchain"}
            for scope in diagnostic["scope"]["enum"]
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = runtime.call_tool("diagnose_reminders", arguments)
                payload = result["structuredContent"]
                self.assertTrue(result["isError"])
                self.assertEqual(payload["error"]["reason_code"], "experimental_disabled")
        dispatch.assert_not_called()

    def test_profiles_and_discovery_responses_do_not_share_mutable_schemas(self) -> None:
        catalog_before = copy.deepcopy(server.TOOLS)
        core = server.McpRuntime(dispatch=mock.Mock())
        experimental = server.McpRuntime(dispatch=mock.Mock(), enable_experimental=True)
        core_tools = discovered_tools(core)
        experimental_tools = discovered_tools(experimental)
        core_diagnosis = next(t for t in core_tools if t["name"] == "diagnose_reminders")
        experimental_diagnosis = next(
            t for t in experimental_tools if t["name"] == "diagnose_reminders"
        )
        self.assertEqual(
            core_diagnosis["inputSchema"]["properties"]["scope"]["enum"],
            ["core", "access", "packaging"],
        )
        self.assertEqual(
            core_diagnosis["inputSchema"]["properties"]["execution_mode"]["enum"],
            ["metadata_only"],
        )
        self.assertIn(
            "native_extension",
            experimental_diagnosis["inputSchema"]["properties"]["scope"]["enum"],
        )
        core_diagnosis["inputSchema"]["properties"]["scope"]["enum"].append("modified")
        experimental_diagnosis["description"] = "Modified response"
        self.assertEqual(server.TOOLS, catalog_before)
        self.assertEqual(
            discovered_tools(core),
            discovered_tools(server.McpRuntime(dispatch=mock.Mock())),
        )
        self.assertEqual(
            discovered_tools(experimental),
            discovered_tools(server.McpRuntime(dispatch=mock.Mock(), enable_experimental=True)),
        )

    def test_core_read_works_when_private_and_diagnostic_backends_are_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            bridge = directory / "fake_eventkit.py"
            mock_eventkit_bridge(bridge)
            responses = run_server(
                [
                    initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "fetch_reminders",
                            "arguments": {"list_ids": ["LIST-1"]},
                        },
                    },
                ],
                eventkit_bridge_path=bridge,
                adapter_path=directory / "missing_private_adapter.py",
                doctor_path=directory / "missing_doctor.py",
            )
            result = responses[1]["result"]
            self.assertFalse(result["isError"])
            self.assertEqual(result["structuredContent"]["status"], "verified")
            self.assertEqual(result["structuredContent"]["data"]["items"], [])
            requests = [json.loads(line) for line in bridge.with_suffix(".requests").read_text().splitlines()]
            self.assertEqual([request["operation"] for request in requests], ["fetch_reminders"])

    def test_runtime_mode_reaches_url_backend_and_receipt(self) -> None:
        url = "https://example.com/project"
        reminder = {**native_reminder(title="Open project"), "url": url}
        for experimental in (False, True):
            with self.subTest(experimental=experimental):
                created = valid_eventkit_receipt(
                    "create_reminder",
                    target={"id": reminder["id"]},
                    after=reminder,
                )

                def bridge_call(operation, _arguments, **_kwargs):
                    if operation == "create_reminder":
                        return transport(copy.deepcopy(created))
                    if operation == "read_reminder":
                        return transport(read_receipt(reminder))
                    raise AssertionError(f"Unexpected fake bridge operation: {operation}")

                inventory = {
                    "ok": True,
                    "reminder_id": reminder["id"],
                    "reminder_version": 1,
                    "attachments": [{"id": "ATTACHMENT-1", "type": "url", "url": url}],
                    "truncated": False,
                }
                with (
                    mock.patch.object(server, "invoke_eventkit_bridge", side_effect=bridge_call) as bridge,
                    mock.patch.object(server, "invoke_adapter", return_value=transport(inventory)) as adapter,
                    mock.patch.object(server, "execute_idempotent", side_effect=lambda **kw: kw["callback"]()),
                ):
                    runtime = server.McpRuntime(enable_experimental=experimental)
                    result = runtime.call_tool(
                        "create_reminder",
                        {
                            "list_id": "LIST-1",
                            "title": reminder["title"],
                            "url": url,
                            "idempotency_key": "runtime-url-integration",
                        },
                    )
                payload = result["structuredContent"]
                self.assertFalse(result["isError"], payload)
                self.assertEqual(payload["status"], "verified")
                self.assertEqual(
                    payload["backend"],
                    "eventkit_plus_native_url" if experimental else "eventkit_public_sdk",
                )
                self.assertEqual(payload["after"]["url"], url)
                self.assertEqual(adapter.call_count, 1 if experimental else 0)
                self.assertEqual(bridge.call_count, 3 if experimental else 2)


if __name__ == "__main__":
    unittest.main()
