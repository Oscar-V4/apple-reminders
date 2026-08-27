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
ORGANIZE_EVALS = (
    ROOT
    / "plugins/apple-reminders/skills/apple-reminders-organize-cleanup/evals/evals.json"
)
ATTACHMENT_EVALS = (
    ROOT
    / "plugins/apple-reminders/skills/apple-reminders-attachment-maintenance/evals/evals.json"
)
CAPABILITY_MATRIX = ROOT / "docs/workflow-capability-matrix.md"


class WorkflowHardeningTests(unittest.TestCase):
    def test_public_delete_is_terminal_without_recovery_claim(self) -> None:
        primary = PRIMARY_SKILL.read_text(encoding="utf-8")
        public_interface = PUBLIC_INTERFACE.read_text(encoding="utf-8")

        self.assertNotIn("Recently Deleted is expected", primary)
        self.assertIn("Treat `delete_reminder` as terminal", primary)
        self.assertIn("there is no public restore or undo tool", primary)
        self.assertIn("public recovery is unavailable", primary)
        self.assertIn("terminal operation on the public surface", public_interface)
        self.assertIn("Recently Deleted inspection/recovery", public_interface)

    def test_ui_relative_selection_requires_explicit_api_snapshot(self) -> None:
        primary = PRIMARY_SKILL.read_text(encoding="utf-8")
        organize = ORGANIZE_SKILL.read_text(encoding="utf-8")
        public_interface = PUBLIC_INTERFACE.read_text(encoding="utf-8")

        self.assertIn("supported sort (`due`, `modified`, or `title`)", primary)
        self.assertIn(
            "Do not equate API order with the current Reminders UI order",
            primary,
        )
        self.assertIn("API order is not evidence", organize)
        self.assertIn("original filters, sort, and limit", public_interface)

    def test_attachment_consolidation_precedes_source_deletion(self) -> None:
        organize = ORGANIZE_SKILL.read_text(encoding="utf-8")
        attachment = ATTACHMENT_SKILL.read_text(encoding="utf-8")
        public_interface = PUBLIC_INTERFACE.read_text(encoding="utf-8")

        non_destructive = organize.index(
            "Complete and verify every non-destructive dependency first"
        )
        destructive = organize.index("Delete each exact source one at a time")
        self.assertLess(non_destructive, destructive)
        self.assertIn("stop before deleting or changing the source reminders", attachment)
        self.assertIn(
            "public tools do not download, export, or copy image bytes",
            attachment.lower(),
        )
        self.assertIn("before deleting a source reminder", public_interface)
        self.assertNotIn("recoverable object lifecycle", attachment)

    def test_representative_multiturn_journey_has_skill_evals(self) -> None:
        primary = json.loads(PRIMARY_EVALS.read_text(encoding="utf-8"))
        organize = json.loads(ORGANIZE_EVALS.read_text(encoding="utf-8"))["evals"]
        attachments = json.loads(ATTACHMENT_EVALS.read_text(encoding="utf-8"))[
            "evals"
        ]

        self.assertTrue(any("위 4개" in item["prompt"] for item in primary))
        self.assertTrue(any("화면 맨 위 4개" in item["prompt"] for item in organize))
        self.assertTrue(any("사진 첨부를 하나" in item["prompt"] for item in attachments))

        combined = "\n".join(
            item.get("expected_behavior", item.get("expected_output", ""))
            for item in [*primary, *organize, *attachments]
        )
        self.assertIn("public restore", combined)
        self.assertIn("before deletion", combined)
        self.assertIn("macOS-only follow-up", combined)

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
