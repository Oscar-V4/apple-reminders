from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
MATRIX = REPO_ROOT / "docs" / "workflow-capability-matrix.md"
REGRESSION_CONTRACT = REPO_ROOT / "docs" / "regression-contract.md"
ADAPTER_NOTES = REPO_ROOT / "docs" / "reminders-adapter-notes.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
PLUGIN_CHANGELOG = PLUGIN_ROOT / "CHANGELOG.md"


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
        contract = REGRESSION_CONTRACT.read_text(encoding="utf-8")
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

    def test_relative_alarm_safety_language_is_durable_and_mirrored(self) -> None:
        matrix = MATRIX.read_text(encoding="utf-8")
        contract = REGRESSION_CONTRACT.read_text(encoding="utf-8")
        adapter_notes = ADAPTER_NOTES.read_text(encoding="utf-8")
        combined = "\n".join((matrix, contract, adapter_notes))

        for required in (
            "31,536,000 seconds (365 elapsed days)",
            "bare default-display",
            "`read_only:true`",
            "unsupported trigger",
            "bounded `action` metadata",
            "Supplying `alarms` replaces the complete",
            "omitting it preserves",
            "`null` or `[]` explicitly clears",
            "`calendar_id` at the native boundary",
            "`list_id` at the public boundary",
            "fresh exact read",
            "`calendarItemWithIdentifier:`",
            "direct helper inputs using either `false`",
            "`true` fail before mutation",
            "Setting `due:null` while retaining a relative alarm is rejected",
            "complete non-relative replacement",
        ):
            self.assertIn(required, combined)

        root_changelog = CHANGELOG.read_bytes()
        self.assertEqual(root_changelog, PLUGIN_CHANGELOG.read_bytes())
        unreleased = root_changelog.decode("utf-8").split(
            "## 0.5.0", maxsplit=1
        )[0]
        for required in (
            "complete-array contract",
            "fresh identifier lookup",
            "Boolean-as-`NSNumber` bypass",
            "no target-controlled code executed",
        ):
            self.assertIn(required, unreleased)


if __name__ == "__main__":
    unittest.main()
