from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "apple-reminders"
PRIMARY_SKILL = PLUGIN / "skills" / "apple-reminders" / "SKILL.md"
ORGANIZE_SKILL = PLUGIN / "skills" / "apple-reminders-organize-cleanup" / "SKILL.md"
ATTACHMENT_SKILL = (
    PLUGIN / "skills" / "apple-reminders-attachment-maintenance" / "SKILL.md"
)
QUICK_CAPTURE_SKILL = PLUGIN / "skills" / "apple-reminders-quick-capture" / "SKILL.md"
DAILY_BRIEF_SKILL = PLUGIN / "skills" / "apple-reminders-daily-brief" / "SKILL.md"
PUBLIC_INTERFACE = (
    PLUGIN / "skills" / "apple-reminders" / "references" / "public-interface.md"
)
PRIMARY_EVALS = PLUGIN / "skills" / "apple-reminders" / "evals" / "evals.json"
SCHEMA = PLUGIN / "schemas" / "mcp-tools.json"
CORE_RUNTIME = PLUGIN / "mcp" / "v2_core.py"
MATRIX = ROOT / "docs" / "workflow-capability-matrix.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def tools_by_name() -> dict[str, dict]:
    payload = json.loads(read(SCHEMA))
    return {tool["name"]: tool for tool in payload["tools"]}


class WorkflowHardeningTests(unittest.TestCase):
    def test_recovery_docs_match_public_del1_surface(self) -> None:
        tools = tools_by_name()
        self.assertIn("inspect_recently_deleted", tools)
        self.assertIn("recover_deleted_reminder", tools)

        recovery_schema = tools["recover_deleted_reminder"]["inputSchema"]
        self.assertEqual(
            recovery_schema["$defs"]["deleted_reference"]["pattern"],
            r"^del1\.[A-Za-z0-9_-]{32,4091}$",
        )

        combined = "\n".join(
            read(path)
            for path in (PRIMARY_SKILL, ORGANIZE_SKILL, PUBLIC_INTERFACE, MATRIX)
        )
        for phrase in (
            "inspect_recently_deleted",
            "recover_deleted_reminder",
            "opaque `del1`",
            "30-day",
            "private ReminderKit",
            "macOS-only",
        ):
            self.assertIn(phrase, combined)
        for obsolete in (
            "there is no public restore",
            "public tools cannot restore",
            "restore are withheld from public 0.3",
        ):
            self.assertNotIn(obsolete, combined)

    def test_ui_relative_selection_has_two_non_conflated_evidence_paths(self) -> None:
        primary = read(PRIMARY_SKILL)
        organize = read(ORGANIZE_SKILL)
        matrix = read(MATRIX)

        self.assertIn("directly observe the current UI", primary)
        self.assertIn("explicit bounded API snapshot", primary)
        self.assertIn("never conflate", primary)
        self.assertIn("API order is not current UI order", organize)
        self.assertIn("UI order and API order are different evidence", matrix)
        self.assertIn("A local UI observation proves only", matrix)
        self.assertIn("Stop on duplicate UI-to-ID mapping", primary)
        self.assertIn("explicitly agrees to reinterpret", primary)
        self.assertIn("does not authorize deletion", organize)

    def test_pagination_docs_match_v3_snapshot_drift_contract(self) -> None:
        runtime = read(CORE_RUNTIME)
        combined = "\n".join(
            read(path)
            for path in (PRIMARY_SKILL, ORGANIZE_SKILL, DAILY_BRIEF_SKILL, PUBLIC_INTERFACE, MATRIX)
        )

        self.assertIn('"v": 3', runtime)
        self.assertIn("snapshot_fingerprint", runtime)
        self.assertIn("pagination_snapshot_stale", runtime)
        for phrase in (
            "opaque v3 cursor",
            "ordered Reminder IDs and revisions",
            "pagination_snapshot_stale",
            "restart without a cursor",
            "not a write precondition",
        ):
            self.assertIn(phrase, combined)
        self.assertNotIn("does not freeze later pages", combined)
        self.assertNotIn("not snapshot-isolated", combined)

    def test_recently_deleted_inventory_has_snapshot_bound_reachable_pages(self) -> None:
        recovery = read(PLUGIN / "mcp" / "v2_recovery.py")
        adapter = read(PLUGIN / "scripts" / "reminders_adapter.py")
        combined = "\n".join(
            read(path)
            for path in (PRIMARY_SKILL, ORGANIZE_SKILL, PUBLIC_INTERFACE, MATRIX)
        )

        self.assertIn("_encode_deleted_cursor", recovery)
        self.assertIn("pagination_snapshot_stale", recovery)
        self.assertIn("snapshot_fingerprint", adapter)
        for phrase in (
            "Recently Deleted cursor",
            "identical account and limit",
            "restart without a cursor",
        ):
            self.assertIn(phrase, combined)

    def test_dependency_first_copy_precedes_source_deletion(self) -> None:
        organize = read(ORGANIZE_SKILL)
        attachment = read(ATTACHMENT_SKILL)
        matrix = read(MATRIX)

        dependency = organize.index(
            "Complete and verify every non-destructive dependency"
        )
        deletion = organize.index("Apply deletion through `delete_reminder`")
        self.assertLess(dependency, deletion)
        self.assertIn("copy and verify every destination image first", attachment)
        self.assertIn("source deletion is last", organize)
        self.assertIn("dependency-first", matrix)

    def test_copy_claim_is_gated_by_current_schema_and_runtime(self) -> None:
        tools = tools_by_name()
        attachment_tool = tools["change_reminder_attachment"]
        actions = attachment_tool["inputSchema"]["properties"]["action"]["oneOf"]
        action_by_kind = {
            action["properties"]["kind"]["const"]: action for action in actions
        }
        runtime_text = "\n".join(
            read(path)
            for directory in (PLUGIN / "mcp", PLUGIN / "scripts")
            for pattern in ("*.py", "*.m")
            for path in directory.glob(pattern)
        )
        matrix = read(MATRIX)
        public_interface = read(PUBLIC_INTERFACE)
        attachment_skill = read(ATTACHMENT_SKILL)

        copy_is_public = "copy_image" in action_by_kind and "copy_image" in runtime_text
        if copy_is_public:
            copy_action = action_by_kind["copy_image"]
            self.assertEqual(
                set(copy_action["required"]),
                {"kind", "source_reference", "attachment_id", "idempotency_key"},
            )
            self.assertIn(
                "Cross-reminder image copy | **Supported with platform boundary**",
                matrix,
            )
            self.assertIn('{"kind":"copy_image"', public_interface)
            self.assertIn("source stays unchanged", attachment_skill)
            self.assertIn(
                "Both references are consumed as one-use preconditions",
                attachment_skill,
            )
            self.assertIn("returned fresh destination reference", attachment_skill)
        else:
            self.assertIn("Cross-reminder image copy | **Intentional boundary**", matrix)
            self.assertNotIn('{"kind":"copy_image"', public_interface)

    def test_cleanup_chunks_and_completed_brief_semantics_are_durable(self) -> None:
        organize = read(ORGANIZE_SKILL)
        daily = read(DAILY_BRIEF_SKILL)
        matrix = read(MATRIX)

        for phrase in (
            "chunk size from 25 to 40 candidates",
            "Call `read_reminder` immediately before the intended write",
            "Do not cache a reference for a later item or chunk",
        ):
            self.assertIn(phrase, organize)
        for phrase in (
            "status=completed",
            "completion_start",
            "completion_end",
            "no wider than 90 days",
            "1–4 high",
            "5 medium",
            "6–9 low",
        ):
            self.assertIn(phrase, daily + "\n" + matrix)
        self.assertIn("bare `pN` labels are not user-facing semantics", matrix)

    def test_crud_boundaries_and_representative_evals_are_explicit(self) -> None:
        matrix = read(MATRIX)
        public_interface = read(PUBLIC_INTERFACE)
        quick_capture = read(QUICK_CAPTURE_SKILL)
        evals = json.loads(read(PRIMARY_EVALS))

        for resource in (
            "Reminder List CRUD",
            "Section CRUD",
            "Tag CRUD",
            "Attachment CRUD",
        ):
            self.assertIn(resource, matrix)
        for boundary in (
            "no rename, delete, color, or emblem write",
            "no raw export/download",
            "no global unused-label row deletion",
        ):
            self.assertIn(boundary, public_interface)
        self.assertIn("does not recreate a deleted Reminder", quick_capture)

        top_four = [item for item in evals if "화면 위 4개" in item["prompt"]]
        recovery = [item for item in evals if "최근 삭제된 '치과 예약'" in item["prompt"]]
        completed = [item for item in evals if "지난주 완료한 일" in item["prompt"]]
        self.assertEqual(len(top_four), 1)
        self.assertEqual(len(recovery), 1)
        self.assertEqual(len(completed), 1)
        self.assertIn("fresh del1", top_four[0]["expected_behavior"])
        self.assertIn("same-account", recovery[0]["expected_behavior"])
        self.assertIn("high, medium, or low", completed[0]["expected_behavior"])


if __name__ == "__main__":
    unittest.main()
