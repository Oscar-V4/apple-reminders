from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "plugins/apple-reminders"))
import smoke_installed_package as smoke
from mcp import server


class InstalledPackageSmokeTests(unittest.TestCase):
    def test_public_probe_cannot_dispatch_to_backend_in_either_mode(self):
        requests = [json.loads(line) for line in smoke.probe_wire().splitlines()]
        calls = [item for item in requests if item["method"] == "tools/call"]
        self.assertEqual(len(calls), 1)
        call = calls[0]["params"]
        self.assertEqual(call["name"], "diagnose_reminders")
        for experimental in (False, True):
            dispatch = Mock(side_effect=AssertionError("backend must never run"))
            limiter = Mock(side_effect=AssertionError("validation must run first"))
            result = server._call_tool(call["name"], call["arguments"], dispatch=dispatch,
                                      rate_limit_allows_call=limiter,
                                      enable_experimental=experimental)
            dispatch.assert_not_called()
            limiter.assert_not_called()
            payload = result["structuredContent"]
            self.assertTrue(result["isError"])
            self.assertEqual(payload["error"]["reason_code"], "invalid_arguments")
            self.assertEqual(payload["status"], "failed_no_mutation")

    def fixture(self):
        return [
            {"jsonrpc": "2.0", "id": 1, "result": {
                "protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}},
            {"jsonrpc": "2.0", "id": 2, "result": {"tools": [
                {"name": "diagnose_reminders", "inputSchema": {"type": "object"}}]}},
            {"jsonrpc": "2.0", "id": 3, "result": {"isError": True, "structuredContent": {
                "ok": False, "status": "failed_no_mutation",
                "error": {"code": "invalid_input", "reason_code": "invalid_arguments"}}}},
        ]

    def test_accepts_valid_contract(self):
        smoke.validate_responses("\n".join(map(json.dumps, self.fixture())), 1)

    def test_rejects_inventory_drift_duplicate_response_or_unexpected_success(self):
        for mutation in (lambda rows: rows[1]["result"]["tools"].append(
                            rows[1]["result"]["tools"][0]),
                         lambda rows: rows.append(rows[-1]),
                         lambda rows: rows[2]["result"].update(isError=False)):
            rows = self.fixture()
            mutation(rows)
            with self.assertRaises(smoke.SmokeError):
                smoke.validate_responses("\n".join(map(json.dumps, rows)), 1)


if __name__ == "__main__":
    unittest.main()
