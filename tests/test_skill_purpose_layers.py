from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
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
        script = (
            PLUGIN_ROOT
            / "skills/apple-reminders-daily-brief/scripts/render_daily_brief.py"
        )
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
        self.assertIn("medium priority (Apple/EventKit 5)", output)
        self.assertNotRegex(output, r"\bp5\b")
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
                        "list_title": "Work",
                        "completed": False,
                        "due": {"kind": "all_day", "date": "2026-08-05"},
                    },
                    {
                        "id": "timed",
                        "title": "Timed",
                        "list_title": "Home",
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
                        "list_title": "Inbox",
                        "completed": False,
                        "due": None,
                    },
                ],
                "total_matched": 4,
                "has_more": True,
                "next_cursor": "opaque",
            },
        }
        script = (
            PLUGIN_ROOT
            / "skills/apple-reminders-daily-brief/scripts/render_daily_brief.py"
        )
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
        self.assertIn(
            "Timed [Home] id: timed - 2026-08-06 09:00 Asia/Seoul",
            proc.stdout,
        )
        self.assertIn("No date [Inbox] id: none - no due date", proc.stdout)

    def test_daily_brief_renderer_preserves_timed_due_clock_and_timezone(self) -> None:
        payload = {
            "ok": True,
            "items": [
                {
                    "id": "utc-timed",
                    "title": "UTC deadline",
                    "list_title": "Work",
                    "completed": False,
                    "due": {
                        "kind": "timed",
                        "date_time": "2026-08-05T23:30:00Z",
                        "time_zone": "UTC",
                    },
                }
            ],
            "total_matched": 1,
        }
        script = (
            PLUGIN_ROOT
            / "skills/apple-reminders-daily-brief/scripts/render_daily_brief.py"
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--date",
                "2026-08-06",
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
        self.assertIn("Due today 1", proc.stdout)
        self.assertIn(
            "UTC deadline [Work] id: utc-timed - 2026-08-06 08:30 Asia/Seoul",
            proc.stdout,
        )

    def test_daily_brief_renderer_renders_bounded_completed_payload(self) -> None:
        payload = {
            "ok": True,
            "data": {
                "items": [
                    {
                        "id": "done-high",
                        "title": "High done",
                        "list_title": "Work",
                        "completed": True,
                        "completion_date": "2026-08-05T01:00:00Z",
                        "priority": 1,
                    },
                    {
                        "id": "done-medium",
                        "title": "Medium done",
                        "list_title": "Home",
                        "completed": True,
                        "completion_date": "2026-08-06T02:00:00Z",
                        "priority": 5,
                    },
                    {
                        "id": "done-low",
                        "title": "Low done",
                        "list_title": "Inbox",
                        "completed": True,
                        "completed_at": "2026-08-07T03:00:00Z",
                        "priority": 9,
                    },
                    {
                        "id": "still-open",
                        "title": "Still open",
                        "list_title": "Inbox",
                        "completed": False,
                        "priority": 1,
                    },
                ],
                "total_matched": 3,
                "has_more": False,
            },
        }
        script = (
            PLUGIN_ROOT
            / "skills/apple-reminders-daily-brief/scripts/render_daily_brief.py"
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--date",
                "2026-08-07",
                "--timezone",
                "Asia/Seoul",
                "--status",
                "completed",
                "--completion-start",
                "2026-08-01T00:00:00+09:00",
                "--completion-end",
                "2026-08-08T00:00:00+09:00",
            ],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Completed reminders reviewed: 3 of 3", proc.stdout)
        self.assertIn(
            "Completion range: 2026-08-01T00:00:00+09:00 to "
            "2026-08-08T00:00:00+09:00",
            proc.stdout,
        )
        self.assertIn("## Completed", proc.stdout)
        self.assertIn("High done [Work] id: done-high - completed 2026-08-05", proc.stdout)
        self.assertIn("Medium done [Home] id: done-medium - completed 2026-08-06", proc.stdout)
        self.assertIn("Low done [Inbox] id: done-low - completed 2026-08-07", proc.stdout)
        self.assertIn("high priority (Apple/EventKit 1)", proc.stdout)
        self.assertIn("medium priority (Apple/EventKit 5)", proc.stdout)
        self.assertIn("low priority (Apple/EventKit 9)", proc.stdout)
        self.assertNotRegex(proc.stdout, r"\bp[1-9]\b")
        self.assertNotIn("Still open", proc.stdout)
        self.assertNotIn("## Overdue", proc.stdout)

    def test_daily_brief_renderer_requires_both_completed_bounds(self) -> None:
        script = (
            PLUGIN_ROOT
            / "skills/apple-reminders-daily-brief/scripts/render_daily_brief.py"
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--date",
                "2026-08-07",
                "--timezone",
                "Asia/Seoul",
                "--status",
                "completed",
                "--completion-start",
                "2026-08-01T00:00:00+09:00",
            ],
            input=json.dumps({"ok": True, "items": []}),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            proc.stderr,
            "--status completed requires both --completion-start and "
            "--completion-end\n",
        )

    def test_daily_brief_renderer_accepts_jsonrpc_structured_content_wrapper(self) -> None:
        payload = {
            "result": {
                "structuredContent": {
                    "data": {
                        "items": [
                            {
                                "id": "floating",
                                "title": "Floating time",
                                "list_title": "Inbox",
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
        script = (
            PLUGIN_ROOT
            / "skills/apple-reminders-daily-brief/scripts/render_daily_brief.py"
        )
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
        self.assertIn(
            "Floating time [Inbox] id: floating - 2026-08-05 15:30 Asia/Seoul",
            proc.stdout,
        )

    def test_daily_brief_renderer_rejects_failed_mcp_result(self) -> None:
        payload = {
            "ok": False,
            "operation": "fetch_reminders",
            "status": "failed_no_mutation",
            "errors": [
                {
                    "code": "permission_denied",
                    "message": "Reminders access is required.",
                }
            ],
        }
        script = (
            PLUGIN_ROOT
            / "skills/apple-reminders-daily-brief/scripts/render_daily_brief.py"
        )
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

        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            proc.stderr,
            "Cannot render failed reminder result: permission_denied\n",
        )
        self.assertNotIn("Nothing due today", proc.stdout)

    def test_daily_brief_renderer_rejects_failed_transport_wrapper(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "isError": True,
                "structuredContent": {
                    "ok": False,
                    "status": "failed_no_mutation",
                    "errors": [{"code": "permission_denied"}],
                    "items": [],
                },
            },
        }
        script = (
            PLUGIN_ROOT
            / "skills/apple-reminders-daily-brief/scripts/render_daily_brief.py"
        )
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

        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            proc.stderr,
            "Cannot render failed reminder result: permission_denied\n",
        )

    def test_daily_brief_renderer_rejects_jsonrpc_error_envelope(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32603,
                "message": "Do not expose this transport message.",
            },
        }
        script = (
            PLUGIN_ROOT
            / "skills/apple-reminders-daily-brief/scripts/render_daily_brief.py"
        )
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

        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(
            proc.stderr,
            "Cannot render failed reminder result: jsonrpc_error_-32603\n",
        )
        self.assertNotIn("transport message", proc.stderr)

    def test_daily_brief_renderer_treats_reminder_fields_as_inert_markdown(self) -> None:
        payload = {
            "ok": True,
            "items": [
                {
                    "id": "id](https://attacker.example/id)",
                    "title": (
                        "- nested ![track](https://attacker.example/pixel.png) "
                        "<img src=x> www.attacker.example"
                        " ~~hidden~~\n## injected"
                    ),
                    "list_title": "[Work](https://attacker.example/list)",
                    "section": "# section",
                    "completed": False,
                    "due": {"kind": "all_day", "date": "2026-08-05"},
                },
                {
                    "id": "exact-id@example.com",
                    "title": "`literal` attacker@example.com",
                    "list_title": "Inbox",
                    "completed": False,
                    "due": {"kind": "all_day", "date": "2026-08-05"},
                }
            ],
        }
        script = (
            PLUGIN_ROOT
            / "skills/apple-reminders-daily-brief/scripts/render_daily_brief.py"
        )
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
        self.assertNotIn("![track](https://attacker.example/pixel.png)", proc.stdout)
        self.assertNotIn("<img src=x>", proc.stdout)
        self.assertNotIn("\n## injected", proc.stdout)
        self.assertNotIn("[Work](https://attacker.example/list)", proc.stdout)
        self.assertNotIn("id](https://attacker.example/id)", proc.stdout)
        self.assertNotIn("https://attacker.example", proc.stdout)
        self.assertNotIn("www.attacker.example", proc.stdout)
        self.assertNotIn("~~hidden~~", proc.stdout)
        self.assertNotIn("\n- - nested", proc.stdout)
        self.assertIn(
            r"\!\[track\]\(https&#58;//attacker&#46;example/pixel&#46;png\)",
            proc.stdout,
        )
        self.assertIn("&lt;img src=x&gt;", proc.stdout)
        self.assertIn("www&#46;attacker&#46;example", proc.stdout)
        self.assertIn(r"\~\~hidden\~\~", proc.stdout)
        self.assertIn(r"\- nested", proc.stdout)
        self.assertIn(r"\#\# injected", proc.stdout)
        self.assertIn(
            r"\[Work\]\(https&#58;//attacker&#46;example/list\)",
            proc.stdout,
        )
        self.assertIn(
            r"id\]\(https&#58;//attacker&#46;example/id\)",
            proc.stdout,
        )
        self.assertIn("`` `literal` attacker@example.com ``", proc.stdout)
        self.assertIn("id: ` exact-id@example.com `", proc.stdout)
        self.assertNotIn("&#8203;", proc.stdout)

    def test_organize_cleanup_uses_jit_revalidation_in_bounded_chunks(self) -> None:
        skill_text = (
            PLUGIN_ROOT
            / "skills/apple-reminders-organize-cleanup/SKILL.md"
        ).read_text(encoding="utf-8")

        discovery = "Discover candidates with `fetch_reminders` summaries"
        approval = "After approval, choose a chunk size from 25 to 40 candidates"
        exact_read = "Call `read_reminder` immediately before the intended write"
        write = "Apply only the approved action using that fresh reference"
        read_back = "then immediately read back"
        halt = "Halt the entire run on the first"

        for instruction in (discovery, approval, exact_read, write, read_back, halt):
            self.assertIn(instruction, skill_text)
        self.assertLess(skill_text.index(discovery), skill_text.index(approval))
        self.assertLess(skill_text.index(approval), skill_text.index(exact_read))
        self.assertLess(skill_text.index(exact_read), skill_text.index(write))
        self.assertLess(skill_text.index(write), skill_text.index(read_back))
        self.assertIn("final remainder may be smaller", skill_text)
        self.assertIn("Do not cache a reference for a later item or chunk", skill_text)
        for stop_status in (
            "`committed_verification_pending`",
            "`partial_success`",
            "concurrent-modification/stale result",
        ):
            self.assertIn(stop_status, skill_text)

    def test_new_skills_have_complete_metadata_and_evals(self) -> None:
        observed_categories: set[str] = set()
        for name in SKILL_NAMES:
            with self.subTest(skill=name):
                skill_dir = PLUGIN_ROOT / "skills" / name
                skill_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
                self.assertNotIn("TODO", skill_text)
                self.assertIn(f"name: {name}", skill_text)
                self.assertIn("description:", skill_text)
                agent_text = (skill_dir / "agents/openai.yaml").read_text(encoding="utf-8")
                self.assertIn(f"Use ${name}", agent_text)
                evals = json.loads((skill_dir / "evals/evals.json").read_text(encoding="utf-8"))
                self.assertEqual(evals["skill_name"], name)
                self.assertGreaterEqual(len(evals["evals"]), 2)
                categories = {case.get("category") for case in evals["evals"]}
                self.assertIn("direct", categories)
                self.assertIn("should_not_activate", categories)
                observed_categories.update(category for category in categories if category)

        self.assertTrue(
            {"direct", "indirect", "incomplete", "should_not_activate", "unsupported_edge"}
            .issubset(observed_categories)
        )

    def test_main_skill_routes_to_purpose_specific_skills(self) -> None:
        skill_text = (PLUGIN_ROOT / "skills/apple-reminders/SKILL.md").read_text(
            encoding="utf-8"
        )
        for name in SKILL_NAMES:
            self.assertIn(f"${name}", skill_text)

    def test_primary_skill_starts_core_without_doctor_and_gates_experimental(self) -> None:
        skill_text = (PLUGIN_ROOT / "skills/apple-reminders/SKILL.md").read_text(
            encoding="utf-8"
        )

        core_instruction = "Start with the requested bounded Core operation"
        diagnosis_instruction = "Use `diagnose_reminders` for an explicitly requested Experimental capability"
        self.assertIn(core_instruction, skill_text)
        self.assertIn("Do not run Doctor for\n   ordinary Core work", skill_text)
        self.assertIn("request access once and retry the original operation once", skill_text)
        self.assertIn(diagnosis_instruction, skill_text)
        self.assertLess(skill_text.index(core_instruction), skill_text.index(diagnosis_instruction))
        self.assertNotIn("On first use or after an environment change, run", skill_text)


if __name__ == "__main__":
    unittest.main()
