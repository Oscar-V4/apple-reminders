from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from reminders_recovery import (  # noqa: E402
    RetentionPolicy,
    create_container_backup,
    create_sqlite_backup,
    prune_managed_backups,
)
from audit_source_package import package_files  # noqa: E402


class BackupPolicyTests(unittest.TestCase):
    def test_runtime_package_includes_the_backup_module(self) -> None:
        files, errors = package_files(ROOT)

        self.assertEqual(errors, [])
        self.assertIn(Path("scripts/reminders_recovery.py"), files)

    def test_sqlite_backup_is_consistent_and_small_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "Stores" / "Data.sqlite"
            database.parent.mkdir()
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("create table tasks (title text)")
                connection.execute("insert into tasks values ('preserved')")
                connection.commit()
            backup_dir = root / "backups"

            result = create_sqlite_backup(
                database=database,
                backup_dir=backup_dir,
                label="tag-cleanup",
                now=dt.datetime(2026, 8, 24, 12, 0, 0),
            )

            backup = Path(result["backup"])
            with closing(sqlite3.connect(backup)) as connection:
                title = connection.execute("select title from tasks").fetchone()[0]
            self.assertEqual(title, "preserved")
            self.assertEqual(result["kind"], "sqlite_online")
            self.assertEqual(result["consistency"], "sqlite_online_backup")
            self.assertEqual(result["source_scope"], "single_database")
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_explicit_output_preserves_existing_parent_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            group = root / "Container_v1"
            group.mkdir()
            (group / "marker.txt").write_text("preserved", encoding="utf-8")
            output_dir = root / "exports"
            output_dir.mkdir(mode=0o755)
            output_dir.chmod(0o755)
            output = output_dir / "manual-copy.tgz"

            result = create_container_backup(
                group=group,
                backup_dir=root / "managed-backups",
                output=output,
                now=dt.datetime(2026, 8, 24, 12, 0, 0),
            )

            self.assertEqual(Path(result["backup"]), output.resolve())
            self.assertEqual(output_dir.stat().st_mode & 0o777, 0o755)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertIsNone(result["retention"])

    def test_container_backup_keeps_existing_archive_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            group = root / "Container_v1"
            group.mkdir()
            (group / "marker.txt").write_text("preserved", encoding="utf-8")

            result = create_container_backup(
                group=group,
                backup_dir=root / "backups",
                now=dt.datetime(2026, 8, 24, 12, 0, 0),
            )

            backup = Path(result["backup"])
            with tarfile.open(backup, "r:gz") as archive:
                self.assertEqual(
                    archive.extractfile("Container_v1/marker.txt").read(), b"preserved"
                )
            self.assertEqual(result["kind"], "container_archive")
            self.assertEqual(result["consistency"], "best_effort_live_container")
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_retention_removes_only_old_managed_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backup_dir = Path(temporary)
            managed = []
            for index, stamp in enumerate(("20260820-120000", "20260821-120000", "20260822-120000")):
                path = backup_dir / f"reminders-container-backup-{stamp}.tgz"
                path.write_bytes(bytes([index]) * 10)
                os.utime(path, (index + 1, index + 1))
                managed.append(path)
            unrelated = backup_dir / "family-photos.tgz"
            unrelated.write_bytes(b"never touch")

            result = prune_managed_backups(
                backup_dir=backup_dir,
                kind="container",
                policy=RetentionPolicy(max_count=2, max_bytes=1_000),
            )

            self.assertFalse(managed[0].exists())
            self.assertTrue(managed[1].exists())
            self.assertTrue(managed[2].exists())
            self.assertTrue(unrelated.exists())
            self.assertEqual(result["removed_count"], 1)
            self.assertEqual(result["removed_bytes"], 10)
            self.assertEqual(result["retained_count"], 2)
            self.assertEqual(result["removed"], [managed[0].name])

    def test_retention_never_deletes_the_protected_new_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backup_dir = Path(temporary)
            old = backup_dir / "reminders-database-backup-tag-cleanup-20260820-120000.sqlite"
            protected = backup_dir / "reminders-database-backup-tag-cleanup-20260824-120000.sqlite"
            old.write_bytes(b"old")
            protected.write_bytes(b"new")
            os.utime(old, (1, 1))
            os.utime(protected, (2, 2))

            result = prune_managed_backups(
                backup_dir=backup_dir,
                kind="database",
                policy=RetentionPolicy(max_count=0, max_bytes=0),
                protected={protected},
            )

            self.assertFalse(old.exists())
            self.assertTrue(protected.exists())
            self.assertEqual(result["retained_count"], 1)


if __name__ == "__main__":
    unittest.main()
