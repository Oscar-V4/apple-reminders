from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GoldenRegressionContractTests(unittest.TestCase):
    def test_0_2_x_keeps_the_user_validated_mcp_tool_surface(self) -> None:
        expected = json.loads(
            (ROOT / "tests/fixtures/golden_mcp_tools.json").read_text(encoding="utf-8")
        )
        schema = json.loads(
            (ROOT / "schemas/mcp-tools.json").read_text(encoding="utf-8")
        )

        self.assertEqual([tool["name"] for tool in schema["tools"]], expected)
        self.assertEqual(len(expected), 32)

    def test_regression_contract_names_the_live_validated_feature_families(self) -> None:
        contract = (ROOT / "docs/regression-contract.md").read_text(encoding="utf-8")

        for required in (
            "EventKit",
            "partial_success",
            "URL attachment",
            "ReminderKit",
            "Section creation",
            "Tag assignment",
            "Attachment audit",
            "show_reminder",
            "release package",
        ):
            self.assertIn(required, contract)


if __name__ == "__main__":
    unittest.main()
