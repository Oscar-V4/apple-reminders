from __future__ import annotations

import json
import os
import stat
import tempfile
import zipfile
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "plugins/apple-reminders"))
import smoke_installed_package as smoke
from mcp import server
import eventkit_bridge


class InstalledPackageSmokeTests(unittest.TestCase):
    def test_public_probe_cannot_dispatch_to_backend_in_either_mode(self):
        requests = [json.loads(line) for line in smoke.probe_wire().splitlines()]
        calls = [item for item in requests if item["method"] == "tools/call"]
        self.assertEqual(len(calls), 1)
        call = calls[0]["params"]
        self.assertEqual(call["name"], "diagnose_reminders")
        for experimental, native in ((False, True), (True, True), (False, False)):
            dispatch = Mock(side_effect=AssertionError("backend must never run"))
            limiter = Mock(side_effect=AssertionError("validation must run first"))
            result = server._call_tool(call["name"], call["arguments"], dispatch=dispatch,
                                      rate_limit_allows_call=limiter,
                                      enable_experimental=experimental, enable_native_tools=native)
            dispatch.assert_not_called()
            limiter.assert_not_called()
            payload = result["structuredContent"]
            self.assertTrue(result["isError"])
            self.assertEqual(payload["error"]["reason_code"], "invalid_arguments")
            self.assertEqual(payload["status"], "failed_no_mutation")

    def fixture(self, names=smoke.PUBLIC_TOOL_NAMES):
        return [
            {"jsonrpc": "2.0", "id": 1, "result": {
                "protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}},
            {"jsonrpc": "2.0", "id": 2, "result": {"tools": [
                {"name": name, "inputSchema": {"type": "object"}} for name in sorted(names)]}},
            {"jsonrpc": "2.0", "id": 3, "result": {"isError": True, "structuredContent": {
                "ok": False, "status": "failed_no_mutation",
                "error": {"code": "invalid_input", "reason_code": "invalid_arguments"}}}},
        ]

    def test_accepts_valid_contract(self):
        for names in (smoke.PUBLIC_TOOL_NAMES, smoke.CORE_TOOL_NAMES):
            smoke.validate_responses("\n".join(map(json.dumps, self.fixture(names))), names)

    def test_positive_packaging_probe_rejects_private_access_or_scope_drift(self):
        wire = [json.loads(line) for line in smoke.probe_wire(True).splitlines()]
        self.assertEqual(wire[-1]['params']['arguments'], {
            'scope': 'packaging', 'detail_level': 'summary', 'execution_mode': 'metadata_only'})
        rows = self.fixture()
        data = {'scope': 'packaging', 'capabilities': [],
                'checks': [{'name': name} for name in ('platform', 'local_artifacts', 'redaction')],
                'privacy': {'content_free': True, 'reminder_content_read': False, 'prompt_triggered': False},
                'execution': {'mode': 'metadata_only', 'developer_tool_process_attempted': False,
                              'compiler_process_attempted': False, 'install_request_attempted': False}}
        rows.append({'jsonrpc': '2.0', 'id': 4, 'result': {'isError': False,
            'structuredContent': {'ok': True, 'status': 'verified', 'data': data}}})
        smoke.validate_responses('\n'.join(map(json.dumps, rows)), smoke.PUBLIC_TOOL_NAMES, True)
        for field in ('reminder_content_read', 'prompt_triggered'):
            data['privacy'][field] = True
            with self.assertRaisesRegex(smoke.SmokeError, 'privacy or scope'):
                smoke.validate_responses('\n'.join(map(json.dumps, rows)), smoke.PUBLIC_TOOL_NAMES, True)
            data['privacy'][field] = False
        data['checks'].append({'name': 'store_access'})
        with self.assertRaisesRegex(smoke.SmokeError, 'privacy or scope'):
            smoke.validate_responses('\n'.join(map(json.dumps, rows)), smoke.PUBLIC_TOOL_NAMES, True)

    def test_rejects_inventory_drift_duplicate_response_or_unexpected_success(self):
        for mutation in (lambda rows: rows[1]["result"]["tools"].append(
                            rows[1]["result"]["tools"][0]),
                         lambda rows: rows.append(rows[-1]),
                         lambda rows: rows[2]["result"].update(isError=False)):
            rows = self.fixture()
            mutation(rows)
            with self.assertRaises(smoke.SmokeError):
                smoke.validate_responses("\n".join(map(json.dumps, rows)), smoke.PUBLIC_TOOL_NAMES)

    def test_rejects_same_count_substituted_names_in_each_profile(self):
        for names in (smoke.PUBLIC_TOOL_NAMES, smoke.CORE_TOOL_NAMES):
            rows = self.fixture(names)
            replaced = next(tool for tool in rows[1]["result"]["tools"]
                            if tool["name"] != "diagnose_reminders")
            replaced["name"] = "unexpected_tool"
            with self.assertRaisesRegex(smoke.SmokeError, "inventory names"):
                smoke.validate_responses("\n".join(map(json.dumps, rows)), names)

    def test_profile_modes_support_current_and_legacy_default(self):
        self.assertEqual(smoke.profile_modes(15, 15, 9), [
            ("default", [], smoke.PUBLIC_TOOL_NAMES),
            ("experimental", ["--experimental"], smoke.PUBLIC_TOOL_NAMES),
            ("core-only", ["--core-only"], smoke.CORE_TOOL_NAMES),
        ])
        self.assertEqual(smoke.profile_modes(9, 15, None), [
            ("default", [], smoke.CORE_TOOL_NAMES),
            ("experimental", ["--experimental"], smoke.PUBLIC_TOOL_NAMES),
        ])
        with self.assertRaises(smoke.SmokeError):
            smoke.profile_modes(14, 15, 9)

    def test_extracted_real_helper_passes_inventory_modes_under_private_umask(self):
        # Package real signed Core helper bytes, then run only Python inventory
        # checks. No native helper or signing command is launched.
        native = eventkit_bridge.BUNDLED_HELPER_NATIVE_DIR
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "fixture.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                paths = list(eventkit_bridge.BUNDLED_HELPER_APP.rglob("*"))
                paths.append(eventkit_bridge.BUNDLED_HELPER_MANIFEST_PATH)
                for path in paths:
                    if path.is_file():
                        info = zipfile.ZipInfo(path.relative_to(native.parent).as_posix())
                        info.create_system = 3
                        info.external_attr = (stat.S_IFREG | stat.S_IMODE(path.stat().st_mode)) << 16
                        handle.writestr(info, path.read_bytes())
            installed = root / "installed"
            previous = os.umask(0o077)
            try:
                smoke.extract_audited_archive(archive, installed)
            finally:
                os.umask(previous)
            extracted_native = installed / "native"
            app = extracted_native / eventkit_bridge.BUNDLED_HELPER_APP_NAME
            with (
                patch.object(eventkit_bridge, "BUNDLED_HELPER_NATIVE_DIR", extracted_native),
                patch.object(eventkit_bridge, "BUNDLED_HELPER_APP", app),
            ):
                self.assertTrue(eventkit_bridge._bundled_helper_inventory())
            executable = app / "Contents/MacOS/apple-reminders-eventkit-helper"
            self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o755)
            self.assertEqual(executable.read_bytes(), (eventkit_bridge.BUNDLED_HELPER_APP / "Contents/MacOS/apple-reminders-eventkit-helper").read_bytes())



if __name__ == "__main__":
    unittest.main()
