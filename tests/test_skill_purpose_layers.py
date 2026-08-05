from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_NAMES = [
    "apple-reminders-daily-brief",
    "apple-reminders-quick-capture",
    "apple-reminders-organize-cleanup",
    "apple-reminders-attachment-maintenance",
]


class PurposeSkillLayerTests(unittest.TestCase):
    def test_daily_brief_renderer_buckets_bounded_adapter_json(self) -> None:
        payload = {
            "ok": True,
            "truncated": True,
            "reminders": [
                {
                    "id": "OVERDUE-1",
                    "title": "Pay invoice",
                    "list": "Work",
                    "section": "Admin",
                    "display_at": "2026-08-05T09:00:00+09:00",
                    "completed": False,
                    "flagged": True,
                    "priority": 5,
                    "notes": "should not render",
                },
                {
                    "id": "TODAY-1",
                    "title": "Submit report",
                    "list": "Work",
                    "display_at": "2026-08-06",
                    "completed": False,
                    "display_date_is_all_day": True,
                },
                {
                    "id": "WEEK-1",
                    "title": "Book tickets",
                    "list": "Personal",
                    "display_at": "2026-08-08T13:00:00+09:00",
                    "completed": False,
                },
                {
                    "id": "NEXT-1",
                    "title": "Renew license",
                    "list": "Admin",
                    "display_at": "2026-08-10T10:00:00+09:00",
                    "completed": False,
                },
                {
                    "id": "NO-DATE-1",
                    "title": "Someday task",
                    "list": "Inbox",
                    "completed": False,
                },
                {
                    "id": "DONE-1",
                    "title": "Completed task",
                    "list": "Inbox",
                    "display_at": "2026-08-06T10:00:00+09:00",
                    "completed": True,
                },
            ],
        }
        script = ROOT / "skills/apple-reminders-daily-brief/scripts/render_daily_brief.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--date",
                "2026-08-06",
                "--timezone",
                "Asia/Seoul",
                "--limit-unscheduled",
                "1",
            ],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        output = proc.stdout
        self.assertIn("# Apple Reminders Brief - 2026-08-06 (Asia/Seoul)", output)
        self.assertIn("source was truncated", output)
        self.assertIn("Overdue 1 | Due today 1 | Later this week 1 | Upcoming 1 | No due date 1", output)
        self.assertIn("Pay invoice [Work / Admin] id: OVERDUE-1", output)
        self.assertIn("Submit report [Work] id: TODAY-1 - 2026-08-06 all-day", output)
        self.assertIn("Upcoming after this week: 1 not shown by default.", output)
        self.assertIn("Someday task [Inbox] id: NO-DATE-1", output)
        self.assertNotIn("Completed task", output)
        self.assertNotIn("should not render", output)

    def test_daily_brief_renderer_accepts_eventkit_mcp_payload(self) -> None:
        payload = {
            "ok": True,
            "operation": "fetch_reminders",
            "data": {
                "items": [
                    {
                        "id": "all-day",
                        "title": "All day",
                        "calendar_title": "Work",
                        "completed": False,
                        "due": {"kind": "all_day", "date": "2026-08-05"},
                    },
                    {
                        "id": "timed",
                        "title": "Timed",
                        "calendar_title": "Home",
                        "completed": False,
                        "due": {
                            "kind": "timed",
                            "date_time": "2026-08-06T09:00:00+09:00",
                            "time_zone": "Asia/Seoul",
                        },
                    },
                    {
                        "id": "none",
                        "title": "No date",
                        "calendar_title": "Inbox",
                        "completed": False,
                        "due": None,
                    },
                ],
                "total_matched": 4,
                "has_more": True,
                "next_cursor": "opaque",
            },
        }
        script = ROOT / "skills/apple-reminders-daily-brief/scripts/render_daily_brief.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--date",
                "2026-08-05",
                "--timezone",
                "Asia/Seoul",
            ],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Active reminders reviewed: 3 of 4; source was truncated", proc.stdout)
        self.assertIn("All day [Work] id: all-day - 2026-08-05 all-day", proc.stdout)
        self.assertIn("Timed [Home] id: timed - 2026-08-06", proc.stdout)
        self.assertIn("No date [Inbox] id: none - no due date", proc.stdout)

    def test_daily_brief_renderer_accepts_jsonrpc_structured_content_wrapper(self) -> None:
        payload = {
            "result": {
                "structuredContent": {
                    "data": {
                        "items": [
                            {
                                "id": "floating",
                                "title": "Floating time",
                                "calendar_title": "Inbox",
                                "completed": False,
                                "due": {
                                    "kind": "timed",
                                    "date_time": None,
                                    "local_date_time": "2026-08-05T15:30:00",
                                    "time_zone": None,
                                    "floating": True,
                                },
                            }
                        ],
                        "total_matched": 1,
                        "has_more": False,
                    }
                }
            }
        }
        script = ROOT / "skills/apple-reminders-daily-brief/scripts/render_daily_brief.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--date",
                "2026-08-05",
                "--timezone",
                "Asia/Seoul",
            ],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Floating time [Inbox] id: floating - 2026-08-05", proc.stdout)

    def test_new_skills_have_complete_metadata_and_evals(self) -> None:
        for name in SKILL_NAMES:
            with self.subTest(skill=name):
                skill_dir = ROOT / "skills" / name
                skill_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
                self.assertNotIn("TODO", skill_text)
                self.assertIn(f"name: {name}", skill_text)
                self.assertIn("description:", skill_text)
                agent_text = (skill_dir / "agents/openai.yaml").read_text(encoding="utf-8")
                self.assertIn(f"Use ${name}", agent_text)
                evals = json.loads((skill_dir / "evals/evals.json").read_text(encoding="utf-8"))
                self.assertEqual(evals["skill_name"], name)
                self.assertGreaterEqual(len(evals["evals"]), 2)

    def test_main_skill_routes_to_purpose_specific_skills(self) -> None:
        skill_text = (ROOT / "skills/apple-reminders/SKILL.md").read_text(encoding="utf-8")
        for name in SKILL_NAMES:
            self.assertIn(f"${name}", skill_text)


if __name__ == "__main__":
    unittest.main()
