from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "scripts/reminders_adapter.py"


class LegacyCliDeprecationTests(unittest.TestCase):
    def test_help_keeps_but_marks_direct_public_write_commands_deprecated(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, str(ADAPTER), "--help"],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

        for command in (
            "create_reminder",
            "update_reminder",
            "complete_reminder",
            "reopen_reminder",
            "delete_reminder",
        ):
            self.assertIn(command, completed.stdout)
        self.assertGreaterEqual(completed.stdout.casefold().count("deprecated"), 5)
        self.assertIn("0.3.0", completed.stdout)


if __name__ == "__main__":
    unittest.main()
