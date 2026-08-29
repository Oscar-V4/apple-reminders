from __future__ import annotations

import argparse
import contextlib
import io
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import reminders_adapter as adapter  # noqa: E402


RETAINED_COMMANDS = {
    "read_reminder",
    "list_sections",
    "list_tags",
    "add_tag",
    "remove_tag",
    "create_section",
    "move_to_section",
    "attach_image",
    "copy_image_attachment",
    "attach_url",
    "list_attachments",
    "delete_attachment",
    "replace_attachment",
    "list_deleted_reminders",
    "read_deleted_reminder",
    "recover_deleted_reminder",
}

REMOVED_COMMANDS = {
    "doctor",
    "backup_store",
    "purge_logs",
    "cache_rebuild",
    "cache_info",
    "cache_search",
    "cache_query",
    "list_lists",
    "snapshot",
    "search_reminders",
    "show_reminder",
    "cleanup_tags",
    "create_list",
    "create_reminder",
    "update_reminder",
    "complete_reminder",
    "reopen_reminder",
    "delete_reminder",
    "audit_attachments",
    "repair_attachments",
}


class LegacyCliDeprecationTests(unittest.TestCase):
    def test_parser_exposes_only_the_runtime_command_allowlist(self) -> None:
        parser = adapter.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )

        self.assertEqual(set(subparsers.choices), RETAINED_COMMANDS)
        self.assertTrue(REMOVED_COMMANDS.isdisjoint(subparsers.choices))

    def test_every_removed_command_is_rejected(self) -> None:
        parser = adapter.build_parser()

        for command in sorted(REMOVED_COMMANDS):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    parser.parse_args([command])
                self.assertEqual(raised.exception.code, 2)

    def test_removed_command_handlers_are_physically_absent(self) -> None:
        for command in sorted(REMOVED_COMMANDS):
            with self.subTest(command=command):
                self.assertFalse(hasattr(adapter, f"cmd_{command}"))


if __name__ == "__main__":
    unittest.main()
