from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"


class GoldenRegressionContractTests(unittest.TestCase):
    def test_v2_maps_user_validated_behaviors_without_preserving_v1_names(self) -> None:
        schema = json.loads(
            (PLUGIN_ROOT / "schemas/mcp-tools.json").read_text(encoding="utf-8")
        )
        names = {tool["name"] for tool in schema["tools"]}

        self.assertTrue(
            {
                "fetch_reminders",
                "read_reminder",
                "create_reminder",
                "change_reminder",
                "delete_reminder",
                "inspect_reminder_native",
                "create_reminder_section",
                "organize_reminder",
                "change_reminder_attachment",
            }.issubset(names)
        )
        self.assertTrue(
            {
                "update_reminder",
                "complete_reminder",
                "reopen_reminder",
                "move_reminder_to_list",
                "list_reminder_attachments",
                "apply_reminder_attachment_repairs",
            }.isdisjoint(names)
        )

    def test_regression_contract_names_the_live_validated_feature_families(self) -> None:
        contract = (REPO_ROOT / "docs/regression-contract.md").read_text(encoding="utf-8")
        normalized = contract.casefold()

        for required in (
            "eventkit",
            "partial_success",
            "url attachment",
            "reminderkit",
            "section creation",
            "tag add/remove",
            "attachment",
            "concurrency",
            "idempotency",
            "release package",
        ):
            self.assertIn(required, normalized)


if __name__ == "__main__":
    unittest.main()
