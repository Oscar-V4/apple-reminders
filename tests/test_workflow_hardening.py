from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIMARY_SKILL = ROOT / "plugins/apple-reminders/skills/apple-reminders/SKILL.md"
ORGANIZE_SKILL = (
    ROOT
    / "plugins/apple-reminders/skills/apple-reminders-organize-cleanup/SKILL.md"
)
ATTACHMENT_SKILL = (
    ROOT
    / "plugins/apple-reminders/skills/apple-reminders-attachment-maintenance/SKILL.md"
)
PUBLIC_INTERFACE = (
    ROOT
    / "plugins/apple-reminders/skills/apple-reminders/references/public-interface.md"
)
PRIMARY_EVALS = (
    ROOT / "plugins/apple-reminders/skills/apple-reminders/evals/evals.json"
)
CAPABILITY_MATRIX = ROOT / "docs/workflow-capability-matrix.md"


class WorkflowHardeningTests(unittest.TestCase):
    def test_public_delete_is_terminal_without_recovery_claim(self) -> None:
        primary = PRIMARY_SKILL.read_text(encoding="utf-8")
        public_interface = PUBLIC_INTERFACE.read_text(encoding="utf-8")

        self.assertNotIn("Recently Deleted is expected", primary)
        self.assertIn("Treat `delete_reminder` as terminal", primary)
        self.assertIn("there is no public restore", primary)
        self.assertIn("terminal on the public surface", public_interface)
        self.assertIn("Recently Deleted inspection/recovery", public_interface)

    def test_ui_relative_selection_requires_explicit_api_snapshot(self) -> None:
        primary = PRIMARY_SKILL.read_text(encoding="utf-8")
        organize = ORGANIZE_SKILL.read_text(encoding="utf-8")
        public_interface = PUBLIC_INTERFACE.read_text(encoding="utf-8")

        self.assertIn("supported sort (`due`, `modified`, or `title`)", primary)
        self.assertIn("API order is not current Reminders UI order", primary)
        self.assertIn("current UI order is unsupported", organize)
        self.assertIn("exact scope, sort, limit, and returned IDs", public_interface)

    def test_attachment_consolidation_precedes_source_deletion(self) -> None:
        organize = ORGANIZE_SKILL.read_text(encoding="utf-8")
        attachment = ATTACHMENT_SKILL.read_text(encoding="utf-8")
        public_interface = PUBLIC_INTERFACE.read_text(encoding="utf-8")

        non_destructive = organize.index(
            "Complete and verify every non-destructive dependency first"
        )
        destructive = organize.index("Delete exact sources one at a time")
        self.assertLess(non_destructive, destructive)
        self.assertIn("metadata, not exported image bytes", attachment)
        self.assertIn("attach and verify the destination first", attachment)
        self.assertIn("must finish before source deletion", public_interface)
        self.assertNotIn("recoverable object lifecycle", attachment)

    def test_representative_multiturn_journey_has_an_eval(self) -> None:
        evals = json.loads(PRIMARY_EVALS.read_text(encoding="utf-8"))
        representative = [item for item in evals if "화면 위 4개" in item["prompt"]]

        self.assertEqual(len(representative), 1)
        expected = representative[0]["expected_behavior"]
        self.assertIn("explicit API sort", expected)
        self.assertIn("verifies the destination before deletion", expected)
        self.assertIn("macOS-only follow-up", expected)

    def test_capability_matrix_distinguishes_all_required_classes(self) -> None:
        matrix = CAPABILITY_MATRIX.read_text(encoding="utf-8")

        for classification in (
            "**Bug**",
            "**Unsafe gap**",
            "**Intentional boundary**",
            "**macOS-only follow-up**",
        ):
            self.assertIn(classification, matrix)

        self.assertIn("Delete the top four reminders", matrix)
        self.assertIn("dependency-first", matrix)
        self.assertIn("No Reminders database", matrix)

    def test_capability_matrix_uses_primary_official_comparisons(self) -> None:
        matrix = CAPABILITY_MATRIX.read_text(encoding="utf-8")

        for source in (
            "support.apple.com/guide/reminders/sort-reminders",
            "support.apple.com/guide/reminders/delete-reminders",
            "developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/delete",
            "developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list",
            "developers.google.com/workspace/tasks/reference/rest",
        ):
            self.assertIn(source, matrix)


if __name__ == "__main__":
    unittest.main()
