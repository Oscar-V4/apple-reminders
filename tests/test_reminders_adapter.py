from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
ADAPTER_PATH = PLUGIN_ROOT / "scripts" / "reminders_adapter.py"
SPEC = importlib.util.spec_from_file_location("reminders_adapter", ADAPTER_PATH)
assert SPEC and SPEC.loader
reminders_adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reminders_adapter)


def validated_image_fixture(
    path: Path,
    *,
    width: int = 100,
    height: int = 50,
    image_format: str = "png",
) -> object:
    data = path.read_bytes()
    return reminders_adapter.ValidatedImage(
        path=path.resolve(),
        format=image_format,
        bytes=len(data),
        width=width,
        height=height,
        sha256=hashlib.sha256(data).hexdigest(),
    )


class ReminderTextDocumentTests(unittest.TestCase):
    def test_generated_document_is_gzip_and_contains_source_text(self) -> None:
        text = "Call dentist about crown repair"

        blob = reminders_adapter.reminder_text_document(text)
        raw = gzip.decompress(blob)

        self.assertTrue(blob.startswith(b"\x1f\x8b"))
        self.assertIn(text.encode("utf-8"), raw)

    def test_generated_document_roundtrips_unicode_text_shape(self) -> None:
        text = "수강신청 확인하고 메모 남기기"

        raw = gzip.decompress(reminders_adapter.reminder_text_document(text))

        self.assertIn(text.encode("utf-8"), raw)


class ScheduleHelperTests(unittest.TestCase):
    def test_timed_schedule_values_use_same_due_and_display_date(self) -> None:
        values = reminders_adapter.schedule_values(due_at="2026-07-03T14:30:00+09:00")

        self.assertEqual(values["ZALLDAY"], 0)
        self.assertEqual(values["ZDISPLAYDATEISALLDAY"], 0)
        self.assertEqual(values["ZDUEDATE"], values["ZDISPLAYDATEDATE"])
        self.assertIsInstance(values["ZTIMEZONE"], str)

    def test_all_day_schedule_values_mark_all_day(self) -> None:
        values = reminders_adapter.schedule_values(all_day_due_date="2026-07-03")

        self.assertEqual(values["ZALLDAY"], 1)
        self.assertEqual(values["ZDISPLAYDATEISALLDAY"], 1)
        self.assertIsNone(values["ZTIMEZONE"])
        self.assertIsNotNone(values["ZDUEDATE"])
        self.assertIsNotNone(values["ZDISPLAYDATEDATE"])

    def test_schedule_values_reject_conflicting_date_options(self) -> None:
        with self.assertRaises(reminders_adapter.AdapterError):
            reminders_adapter.schedule_values(
                due_at="2026-07-03T14:30:00+09:00",
                all_day_due_date="2026-07-03",
            )

    def test_schedule_values_rejects_alert_due_date_conflation(self) -> None:
        with self.assertRaises(reminders_adapter.AdapterError) as raised:
            reminders_adapter.schedule_values(remind_at="2026-07-03T14:30:00+09:00")

        self.assertEqual(raised.exception.code, "unsupported_capability")

    def test_timed_schedule_requires_explicit_timezone(self) -> None:
        with self.assertRaises(reminders_adapter.AdapterError):
            reminders_adapter.schedule_values(due_at="2026-07-03T14:30:00")

    def test_normalized_url_requires_scheme(self) -> None:
        with self.assertRaises(reminders_adapter.AdapterError):
            reminders_adapter.normalized_url("example.com/no-scheme")

    def test_reminder_url_accepts_bare_or_url_ids(self) -> None:
        rid = "7718459E-2672-4E99-9E6A-B9AA430E570F"

        self.assertEqual(
            reminders_adapter.reminder_url(rid),
            "x-apple-reminder://7718459E-2672-4E99-9E6A-B9AA430E570F",
        )
        self.assertEqual(
            reminders_adapter.reminder_url(f"x-apple-reminder://{rid}"),
            "x-apple-reminder://7718459E-2672-4E99-9E6A-B9AA430E570F",
        )


class ListSectionScopeTests(unittest.TestCase):
    def test_duplicate_list_names_are_scoped_by_exact_list_identifier(self) -> None:
        first_list = "11111111-1111-4111-8111-111111111111"
        second_list = "22222222-2222-4222-8222-222222222222"
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "duplicate-list-names.sqlite"
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    f"""
                    create table ZREMCDBASELIST (
                        Z_PK integer primary key,
                        ZCKIDENTIFIER text,
                        ZNAME text
                    );
                    create table ZREMCDBASESECTION (
                        Z_PK integer primary key,
                        ZCKIDENTIFIER text,
                        ZDISPLAYNAME text,
                        ZLIST integer,
                        Z_FOK_LIST integer,
                        ZMARKEDFORDELETION integer
                    );
                    insert into ZREMCDBASELIST values
                        (1, '{first_list}', 'Inbox'),
                        (2, '{second_list}', 'Inbox');
                    insert into ZREMCDBASESECTION values
                        (11, 'SECTION-PERSONAL', 'Personal', 1, 1024, 0),
                        (22, 'SECTION-WORK', 'Work', 2, 1024, 0);
                    """
                )
                connection.commit()
            finally:
                connection.close()

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ADAPTER_PATH),
                    "list_sections",
                    "--db",
                    str(database),
                    "--list-id",
                    second_list,
                    "--limit",
                    "10",
                ],
                cwd=PLUGIN_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(len(payload["sections"]), 1)
        section = payload["sections"][0]
        self.assertEqual(section["ZCKIDENTIFIER"], "SECTION-WORK")
        self.assertEqual(section["ZDISPLAYNAME"], "Work")
        self.assertEqual(section["list_id"], second_list)
        self.assertEqual(section["list_name"], "Inbox")


class TagScopeTests(unittest.TestCase):
    def test_account_filter_is_applied_before_the_tag_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "tag-accounts.sqlite"
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    create table ZREMCDHASHTAGLABEL (
                        Z_PK integer primary key,
                        ZNAME text,
                        ZCANONICALNAME text,
                        ZACCOUNTIDENTIFIER text,
                        ZUUIDFORCHANGETRACKING blob,
                        ZFIRSTOCCURRENCECREATIONDATE real,
                        ZRECENCYDATE real
                    );
                    create table ZREMCDOBJECT (
                        Z_PK integer primary key,
                        ZHASHTAGLABEL integer,
                        Z_ENT integer,
                        ZMARKEDFORDELETION integer
                    );
                    insert into ZREMCDHASHTAGLABEL values
                        (1, 'aaa-other', 'aaa-other', 'ACCOUNT-OTHER', null, null, null),
                        (2, 'zzz-target', 'zzz-target', 'ACCOUNT-TARGET', null, null, null);
                    """
                )
                connection.commit()
            finally:
                connection.close()

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ADAPTER_PATH),
                    "list_tags",
                    "--db",
                    str(database),
                    "--account-id",
                    "ACCOUNT-TARGET",
                    "--limit",
                    "1",
                ],
                cwd=PLUGIN_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual([tag["name"] for tag in payload["tags"]], ["zzz-target"])
        self.assertFalse(payload["truncated"])


class ImageCommandBoundaryTests(unittest.TestCase):
    def test_attach_and_replace_reject_symlinks_before_mutation(self) -> None:
        reminder_id = "7718459E-2672-4E99-9E6A-B9AA430E570F"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.png"
            link = root / "link.png"
            target.write_bytes(b"not-decoded-because-the-symlink-must-fail-first")
            link.symlink_to(target)
            commands = [
                [
                    "attach_image",
                    "--id",
                    reminder_id,
                    "--image",
                    str(link),
                    "--if-version",
                    "1",
                    "--idempotency-key",
                    "image-boundary-attach",
                ],
                [
                    "replace_attachment",
                    "--id",
                    reminder_id,
                    "--attachment-id",
                    "11111111-1111-4111-8111-111111111111",
                    "--image",
                    str(link),
                    "--if-version",
                    "1",
                    "--idempotency-key",
                    "image-boundary-replace",
                ],
            ]
            results = []
            for command in commands:
                completed = subprocess.run(
                    [sys.executable, str(ADAPTER_PATH), *command],
                    cwd=PLUGIN_ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=10,
                )
                results.append((completed, json.loads(completed.stdout)))

        for completed, payload in results:
            self.assertEqual(completed.returncode, 1, completed.stderr)
            self.assertEqual(payload["status"], "failed_no_mutation")
            self.assertFalse(payload["verification"]["write_performed"])
            self.assertEqual(payload["error"]["code"], "invalid_input")
            self.assertEqual(payload["error"]["reason_code"], "symlink_not_allowed")


class AppleScriptSyncTests(unittest.TestCase):
    def test_sync_reminder_text_uses_no_change_sentinels(self) -> None:
        with mock.patch.object(reminders_adapter, "run_osascript", return_value="x-apple-reminder://AAA") as run:
            out = reminders_adapter.sync_reminder_text_applescript(
                "x-apple-reminder://AAA",
                title="New title",
            )

        self.assertEqual(out, "x-apple-reminder://AAA")
        args = run.call_args.args[1]
        self.assertEqual(args, ["x-apple-reminder://AAA", "New title", "__NO_CHANGE__"])

    def test_sync_reminder_text_skips_when_no_fields_are_provided(self) -> None:
        with mock.patch.object(reminders_adapter, "run_osascript") as run:
            out = reminders_adapter.sync_reminder_text_applescript("x-apple-reminder://AAA")

        self.assertIsNone(out)
        run.assert_not_called()


class RecentlyDeletedRecoveryTests(unittest.TestCase):
    REMINDER_ID = "11111111-1111-4111-8111-111111111111"
    LIST_ID = "22222222-2222-4222-8222-222222222222"

    @classmethod
    def _args(cls, guard: dict[str, object]) -> argparse.Namespace:
        return argparse.Namespace(
            command="recover_deleted_reminder",
            db=None,
            id=cls.REMINDER_ID,
            list_id=cls.LIST_ID,
            if_store_identity=guard["store_identity"],
            if_version=guard["private_version"],
            if_deleted_at=guard["deleted_at"],
            if_attachment_digest=guard["attachment_digest"],
            if_native_guard_digest=guard.get("native_guard_digest", "c" * 64),
        )

    def test_deleted_inventory_pages_over_one_snapshot_bound_order(self) -> None:
        rows = [
            {
                "Z_PK": 2,
                "ZCKIDENTIFIER": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "Z_OPT": 8,
                "ZLASTMODIFIEDDATE": 200.0,
            },
            {
                "Z_PK": 1,
                "ZCKIDENTIFIER": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "Z_OPT": 7,
                "ZLASTMODIFIEDDATE": 100.0,
            },
        ]
        connection = mock.Mock()
        snapshot_result = mock.Mock()
        snapshot_result.fetchall.return_value = rows
        page_result = mock.Mock()
        page_result.fetchall.return_value = [rows[1]]
        connection.execute.side_effect = [mock.Mock(), snapshot_result, page_result]

        def public_snapshot(_connection, row):
            return ({"id": row["ZCKIDENTIFIER"]}, {"private": "not public"})

        args = argparse.Namespace(db=None, account_id=None, limit=1, offset=1)
        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/store.sqlite"),
            ),
            mock.patch.object(
                reminders_adapter,
                "connect_read_only",
                return_value=connection,
            ),
            mock.patch.object(reminders_adapter, "require_command_capability"),
            mock.patch.object(
                reminders_adapter,
                "deleted_reminder_snapshot",
                side_effect=public_snapshot,
            ),
            mock.patch.object(reminders_adapter, "json_out") as emit,
        ):
            result = reminders_adapter.cmd_list_deleted_reminders(args)

        self.assertEqual(result, 0)
        payload = emit.call_args.args[0]
        self.assertEqual(
            payload["deleted_reminders"],
            [{"id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}],
        )
        self.assertEqual(payload["total_matched"], 2)
        self.assertFalse(payload["has_more"])
        self.assertIsNone(payload["next_offset"])
        self.assertRegex(payload["snapshot_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertNotIn("private", repr(payload))
        self.assertEqual(connection.execute.call_args_list[0].args[0], "begin")
        snapshot_query = connection.execute.call_args_list[1].args[0]
        page_query = connection.execute.call_args_list[2].args[0]
        self.assertNotIn("select r.*", snapshot_query.casefold())
        self.assertIn(
            "select r.z_pk,r.zckidentifier,r.z_opt,r.zlastmodifieddate",
            " ".join(snapshot_query.casefold().split()),
        )
        self.assertIn("select r.*", page_query.casefold())
        self.assertEqual(connection.execute.call_args_list[2].args[1], [1])

    def test_deleted_inventory_rejects_page_revision_drift(self) -> None:
        snapshot_row = {
            "Z_PK": 1,
            "ZCKIDENTIFIER": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "Z_OPT": 7,
            "ZLASTMODIFIEDDATE": 100.0,
        }
        changed_row = {**snapshot_row, "Z_OPT": 8}
        connection = mock.Mock()
        snapshot_result = mock.Mock()
        snapshot_result.fetchall.return_value = [snapshot_row]
        page_result = mock.Mock()
        page_result.fetchall.return_value = [changed_row]
        connection.execute.side_effect = [mock.Mock(), snapshot_result, page_result]
        args = argparse.Namespace(db=None, account_id=None, limit=1, offset=0)

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/store.sqlite"),
            ),
            mock.patch.object(
                reminders_adapter,
                "connect_read_only",
                return_value=connection,
            ),
            mock.patch.object(reminders_adapter, "require_command_capability"),
            self.assertRaises(reminders_adapter.AdapterError) as raised,
        ):
            reminders_adapter.cmd_list_deleted_reminders(args)

        self.assertEqual(raised.exception.code, "concurrent_modification")
        self.assertEqual(
            raised.exception.details["reason_code"],
            "pagination_snapshot_stale",
        )
        connection.close.assert_called_once_with()

    def test_deleted_item_absence_preserves_typed_not_found(self) -> None:
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = None

        with self.assertRaises(reminders_adapter.AdapterError) as raised:
            reminders_adapter.find_deleted_reminder(connection, self.REMINDER_ID)

        self.assertEqual(raised.exception.code, "not_found")
        self.assertEqual(
            raised.exception.details["reason_code"],
            "deleted_reminder_not_recoverable",
        )

    def test_helper_crash_without_receipt_preserves_possible_write(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["remkit_recover"],
            returncode=-11,
            stdout="",
            stderr="",
        )
        with (
            mock.patch.object(
                reminders_adapter,
                "reminderkit_recover_helper",
                return_value=Path("/tmp/remkit_recover"),
            ),
            mock.patch.object(
                reminders_adapter.subprocess,
                "run",
                return_value=completed,
            ),
            self.assertRaises(reminders_adapter.AdapterError) as raised,
        ):
            reminders_adapter.invoke_reminderkit_recovery(
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
                "c" * 64,
            )

        self.assertEqual(raised.exception.code, "sync_pending")
        self.assertTrue(raised.exception.details["mutation_outcome_unknown"])
        self.assertTrue(raised.exception.details["partial_failure"])

    def test_native_guard_read_is_bounded_and_never_dispatches_a_write(self) -> None:
        payload = json.dumps(
            {
                "ok": True,
                "operation": "read_recovery_guard",
                "mutation_attempted": False,
                "reminder_id": self.REMINDER_ID,
                "native_guard_digest": "c" * 64,
            }
        )
        with (
            mock.patch.object(
                reminders_adapter,
                "reminderkit_recover_helper",
                return_value=Path("/tmp/remkit_recover"),
            ),
            mock.patch.object(
                reminders_adapter.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout=payload, stderr=""
                ),
            ) as run,
        ):
            digest = reminders_adapter.invoke_reminderkit_recovery_guard(
                self.REMINDER_ID
            )

        self.assertEqual(digest, "c" * 64)
        self.assertEqual(
            run.call_args.args[0],
            ["/tmp/remkit_recover", "guard", self.REMINDER_ID],
        )

    def test_native_guard_mismatch_is_known_no_write_concurrency(self) -> None:
        payload = json.dumps(
            {
                "ok": False,
                "error": "concurrent_modification",
                "mutation_attempted": False,
            }
        )
        with (
            mock.patch.object(
                reminders_adapter,
                "reminderkit_recover_helper",
                return_value=Path("/tmp/remkit_recover"),
            ),
            mock.patch.object(
                reminders_adapter.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 2, stdout=payload, stderr=""
                ),
            ),
            self.assertRaises(reminders_adapter.AdapterError) as raised,
        ):
            reminders_adapter.invoke_reminderkit_recovery(
                self.REMINDER_ID,
                self.LIST_ID,
                "c" * 64,
            )

        self.assertEqual(raised.exception.code, "concurrent_modification")
        self.assertFalse(raised.exception.details["partial_failure"])
        self.assertFalse(raised.exception.details["mutation_outcome_unknown"])

    def test_unknown_native_outcome_becomes_a_cacheable_pending_receipt(self) -> None:
        connection = mock.Mock()
        row = {"ZACCOUNT": 7}
        destination = {"ZACCOUNT": 7}
        before = {
            "id": "11111111-1111-4111-8111-111111111111",
            "deleted_at": "2026-08-28T00:00:00+09:00",
            "attachment_count": 1,
        }
        guard = {
            "store_identity": "a" * 64,
            "private_version": 3,
            "deleted_at": before["deleted_at"],
            "attachment_digest": "b" * 64,
        }
        args = argparse.Namespace(
            db=None,
            id=before["id"],
            list_id="22222222-2222-4222-8222-222222222222",
            if_store_identity=guard["store_identity"],
            if_version=guard["private_version"],
            if_deleted_at=guard["deleted_at"],
            if_attachment_digest=guard["attachment_digest"],
            if_native_guard_digest="c" * 64,
        )
        failure = reminders_adapter.AdapterError(
            "helper crashed",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
        )
        with (
            mock.patch.object(
                reminders_adapter, "resolve_database", return_value=Path("/tmp/store.sqlite")
            ),
            mock.patch.object(
                reminders_adapter, "connect_read_only", return_value=connection
            ),
            mock.patch.object(
                reminders_adapter, "find_deleted_reminder", return_value=row
            ),
            mock.patch.object(reminders_adapter, "find_list", return_value=destination),
            mock.patch.object(
                reminders_adapter,
                "deleted_reminder_snapshot",
                return_value=(before, guard),
            ),
            mock.patch.object(
                reminders_adapter,
                "deleted_store_identity",
                return_value=guard["store_identity"],
            ),
            mock.patch.object(
                reminders_adapter,
                "invoke_reminderkit_recovery",
                side_effect=failure,
            ),
        ):
            receipt = reminders_adapter.recover_deleted_reminder_once(args)

        self.assertEqual(receipt["status"], "committed_verification_pending")
        self.assertIsNone(receipt["verification"]["write_performed"])
        self.assertFalse(receipt["recovery"]["automatic_retry_safe"])
        self.assertFalse(receipt["error"]["retryable"])

    def test_helper_relies_on_save_request_sync_and_never_calls_nil_completion(self) -> None:
        source = (
            reminders_adapter.SCRIPT_DIR / "remkit_recover.m"
        ).read_text(encoding="utf-8")

        self.assertIn("setSyncToCloudKit:", source)
        self.assertIn("NativeGuardDigest", source)
        self.assertIn("pre_save_guard_matched", source)
        self.assertIn("concurrent_modification", source)
        self.assertNotIn("triggerCloudKitOnlySyncWithReason:", source)
        staged = source.index("undeleteReminderWithID:usingUndo:")
        immediate_guard = source.index("preSaveDeletedReminder")
        saved = source.index("saveSynchronouslyWithError:", immediate_guard)
        self.assertLess(staged, immediate_guard)
        self.assertLess(immediate_guard, saved)

    def test_post_save_destination_read_failure_is_pending_not_no_mutation(self) -> None:
        digest = reminders_adapter.deleted_attachment_digest([])
        guard = {
            "store_identity": "a" * 64,
            "private_version": 3,
            "deleted_at": "2026-08-28T00:00:00+09:00",
            "attachment_digest": digest,
        }
        before = {
            "id": self.REMINDER_ID,
            "deleted_at": guard["deleted_at"],
            "attachment_count": 0,
        }
        preflight = mock.Mock()
        active_read = mock.Mock()
        destination_read = mock.Mock()
        active_read.execute.return_value.fetchone.return_value = {
            "Z_PK": 9,
            "ZCKIDENTIFIER": self.REMINDER_ID,
            "ZLIST": 7,
        }

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/store.sqlite"),
            ),
            mock.patch.object(
                reminders_adapter,
                "connect_read_only",
                side_effect=[preflight, active_read, destination_read],
            ),
            mock.patch.object(
                reminders_adapter,
                "find_deleted_reminder",
                return_value={"ZACCOUNT": 1},
            ),
            mock.patch.object(
                reminders_adapter,
                "find_list",
                side_effect=[
                    {"ZACCOUNT": 1, "Z_PK": 7},
                    reminders_adapter.AdapterError("List disappeared after save"),
                ],
            ),
            mock.patch.object(
                reminders_adapter,
                "deleted_reminder_snapshot",
                return_value=(before, guard),
            ),
            mock.patch.object(
                reminders_adapter,
                "deleted_store_identity",
                return_value=guard["store_identity"],
            ),
            mock.patch.object(
                reminders_adapter,
                "invoke_reminderkit_recovery",
                return_value={
                    "ok": True,
                    "saved": True,
                    "pre_save_guard_matched": True,
                    "attachment_count": 0,
                },
            ),
            mock.patch.object(
                reminders_adapter,
                "deleted_attachment_rows",
                return_value=[],
            ),
        ):
            receipt = reminders_adapter.recover_deleted_reminder_once(
                self._args(guard)
            )

        self.assertEqual(receipt["status"], "committed_verification_pending")
        self.assertTrue(receipt["verification"]["write_performed"])
        self.assertFalse(receipt["verification"]["final_read"])
        self.assertEqual(
            receipt["error"]["reason_code"],
            "recovery_post_write_verification_failed",
        )

    def test_native_attachment_count_mismatch_cannot_verify(self) -> None:
        attachment = {
            "Z_PK": 10,
            "Z_ENT": reminders_adapter.URL_ATTACHMENT_ENT,
            "ZCKIDENTIFIER": "33333333-3333-4333-8333-333333333333",
            "ZREMINDER2": 9,
            "Z_FOK_REMINDER1": 0,
            "ZFILENAME": None,
            "ZSHA512SUM": None,
            "ZUTI": "public.url",
            "ZFILESIZE": None,
            "ZWIDTH": None,
            "ZHEIGHT": None,
            "ZURL": "https://example.invalid/recovery-count",
            "ZHOSTURL": "https://example.invalid",
            "ZMARKEDFORDELETION": 0,
        }
        digest = reminders_adapter.deleted_attachment_digest([attachment])
        guard = {
            "store_identity": "a" * 64,
            "private_version": 3,
            "deleted_at": "2026-08-28T00:00:00+09:00",
            "attachment_digest": digest,
        }
        before = {
            "id": self.REMINDER_ID,
            "deleted_at": guard["deleted_at"],
            "attachment_count": 1,
        }
        preflight = mock.Mock()
        active_read = mock.Mock()
        destination_read = mock.Mock()
        active_read.execute.return_value.fetchone.return_value = {
            "Z_PK": 9,
            "ZCKIDENTIFIER": self.REMINDER_ID,
            "ZLIST": 7,
        }

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/store.sqlite"),
            ),
            mock.patch.object(
                reminders_adapter,
                "connect_read_only",
                side_effect=[preflight, active_read, destination_read],
            ),
            mock.patch.object(
                reminders_adapter,
                "find_deleted_reminder",
                return_value={"ZACCOUNT": 1},
            ),
            mock.patch.object(
                reminders_adapter,
                "find_list",
                side_effect=[
                    {"ZACCOUNT": 1, "Z_PK": 7},
                    {"ZACCOUNT": 1, "Z_PK": 7},
                ],
            ),
            mock.patch.object(
                reminders_adapter,
                "deleted_reminder_snapshot",
                return_value=(before, guard),
            ),
            mock.patch.object(
                reminders_adapter,
                "deleted_store_identity",
                return_value=guard["store_identity"],
            ),
            mock.patch.object(
                reminders_adapter,
                "invoke_reminderkit_recovery",
                return_value={
                    "ok": True,
                    "saved": True,
                    "pre_save_guard_matched": True,
                    "attachment_count": 0,
                },
            ),
            mock.patch.object(
                reminders_adapter,
                "deleted_attachment_rows",
                return_value=[attachment],
            ),
        ):
            receipt = reminders_adapter.recover_deleted_reminder_once(
                self._args(guard)
            )

        self.assertEqual(receipt["status"], "partial_success")
        self.assertFalse(receipt["verification"]["matched"])
        self.assertFalse(receipt["verification"]["attachment_counts_match"])
        self.assertEqual(receipt["verification"]["before_attachment_count"], 1)
        self.assertEqual(receipt["verification"]["native_attachment_count"], 0)
        self.assertEqual(receipt["verification"]["after_attachment_count"], 1)

    def test_deleted_attachment_digest_rejects_corrupt_backing_bytes(self) -> None:
        expected_bytes = b"expected deleted image bytes"
        expected_sha512 = reminders_adapter.hashlib.sha512(
            expected_bytes
        ).hexdigest()
        attachment = {
            "Z_PK": 10,
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
            "ZCKIDENTIFIER": "33333333-3333-4333-8333-333333333333",
            "ZREMINDER2": 9,
            "Z_FOK_REMINDER1": 0,
            "ZFILENAME": "deleted.png",
            "ZSHA512SUM": expected_sha512,
            "ZUTI": "public.png",
            "ZFILESIZE": len(expected_bytes),
            "ZWIDTH": 2,
            "ZHEIGHT": 3,
            "ZURL": None,
            "ZHOSTURL": None,
            "ZMARKEDFORDELETION": 0,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            files = Path(temp_dir) / "Files"
            attachments = files / "Account-SYNTHETIC" / "Attachments"
            attachments.mkdir(parents=True)
            backing = attachments / f"{expected_sha512}.png"
            backing.write_bytes(expected_bytes)
            with mock.patch.object(reminders_adapter, "FILES", files):
                verified_digest = reminders_adapter.deleted_attachment_digest(
                    [attachment],
                    verify_image_bytes=True,
                )
                backing.write_bytes(b"corrupt bytes")
                with self.assertRaises(reminders_adapter.AdapterError) as raised:
                    reminders_adapter.deleted_attachment_digest(
                        [attachment],
                        verify_image_bytes=True,
                    )

        self.assertRegex(verified_digest, r"^[0-9a-f]{64}$")
        self.assertEqual(raised.exception.code, "sync_pending")
        self.assertEqual(
            raised.exception.details["reason_code"],
            "deleted_image_bytes_mismatch",
        )


class CrossReminderImageCopyTests(unittest.TestCase):
    SOURCE_ID = "11111111-1111-4111-8111-111111111111"
    DESTINATION_ID = "22222222-2222-4222-8222-222222222222"
    SOURCE_ATTACHMENT_ID = "33333333-3333-4333-8333-333333333333"
    NEW_ATTACHMENT_ID = "44444444-4444-4444-8444-444444444444"

    @staticmethod
    def _args() -> argparse.Namespace:
        return argparse.Namespace(
            db=None,
            source_id=CrossReminderImageCopyTests.SOURCE_ID,
            id=CrossReminderImageCopyTests.DESTINATION_ID,
            attachment_id=CrossReminderImageCopyTests.SOURCE_ATTACHMENT_ID,
            if_source_version=7,
            if_version=12,
            idempotency_key="copy-image-adapter",
        )

    def test_copy_uses_private_stable_snapshot_and_never_returns_its_path(self) -> None:
        source = {
            "Z_PK": 1,
            "Z_OPT": 7,
            "ZCKIDENTIFIER": self.SOURCE_ID,
            "ZMARKEDFORDELETION": 0,
        }
        destination = {
            "Z_PK": 2,
            "Z_OPT": 12,
            "ZCKIDENTIFIER": self.DESTINATION_ID,
            "ZMARKEDFORDELETION": 0,
        }
        selected = {"Z_PK": 10, "ZCKIDENTIFIER": self.SOURCE_ATTACHMENT_ID}
        source_bytes = b"private-reminders-png-bytes"
        source_payload = {
            "id": self.SOURCE_ATTACHMENT_ID,
            "type": "image",
            "uti": "public.png",
            "filename": "source.png",
            "sha512": reminders_adapter.hashlib.sha512(source_bytes).hexdigest(),
            "file_size": len(source_bytes),
            "width": 10,
            "height": 10,
            "marked_for_deletion": False,
        }
        new_attachment = {
            "id": self.NEW_ATTACHMENT_ID,
            "type": "image",
            # ReminderKit may normalize stale source metadata to the format
            # decoded from the exact same bytes.
            "uti": "public.jpeg",
            "filename": "source.png",
            "sha512": source_payload["sha512"],
            "file_size": len(source_bytes),
            "width": 10,
            "height": 10,
            "sync": {"mobile_visible_likely": True},
        }
        connection = mock.Mock()
        attached_paths: list[Path] = []

        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            source_path = temp_root / "source.png"
            source_path.write_bytes(source_bytes)

            def validate(path: Path) -> mock.Mock:
                value = Path(path)
                return mock.Mock(
                    path=value,
                    format="png",
                    sha256=reminders_adapter.hashlib.sha256(value.read_bytes()).hexdigest(),
                )

            def attach(
                con: object,
                reminder: dict[str, object],
                image_path: Path,
            ) -> dict[str, object]:
                attached_paths.append(Path(image_path))
                self.assertNotEqual(Path(image_path), source_path)
                self.assertTrue(Path(image_path).is_file())
                self.assertEqual(Path(image_path).read_bytes(), source_bytes)
                return {
                    "attachment": new_attachment,
                    "sync": {"mobile_visible_likely": True},
                    "_row": {"private": "must be removed"},
                }

            with (
                mock.patch.object(
                    reminders_adapter,
                    "resolve_database",
                    return_value=temp_root / "store.sqlite",
                ),
                mock.patch.object(reminders_adapter, "connect", return_value=connection),
                mock.patch.object(
                    reminders_adapter,
                    "require_command_capability",
                    return_value={"supported": True},
                ),
                mock.patch.object(
                    reminders_adapter,
                    "find_reminder",
                    side_effect=[source, destination],
                ),
                mock.patch.object(
                    reminders_adapter,
                    "resolve_attachment_selection",
                    side_effect=[
                        (selected, [source_payload], None),
                        (selected, [source_payload], None),
                        (selected, [source_payload], None),
                    ],
                ),
                mock.patch.object(
                    reminders_adapter,
                    "attachment_payload",
                    return_value=source_payload,
                ),
                mock.patch.object(
                    reminders_adapter,
                    "exact_source_image_path",
                    return_value=source_path,
                ),
                mock.patch.object(
                    reminders_adapter,
                    "validate_image_input",
                    side_effect=validate,
                ),
                mock.patch.object(
                    reminders_adapter,
                    "reread_reminder",
                    side_effect=[source, destination, source, destination],
                ),
                mock.patch.object(
                    reminders_adapter,
                    "attach_image_reminderkit_record",
                    side_effect=attach,
                ),
                mock.patch.object(reminders_adapter, "log_action", return_value=None),
                mock.patch.object(reminders_adapter, "CACHE_DIR", temp_root / "cache"),
            ):
                receipt = reminders_adapter.copy_image_attachment_once(self._args())

        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(
            receipt["target"],
            {
                "source_reminder_id": self.SOURCE_ID,
                "reminder_id": self.DESTINATION_ID,
                "source_attachment_id": self.SOURCE_ATTACHMENT_ID,
                "attachment_id": self.NEW_ATTACHMENT_ID,
            },
        )
        self.assertTrue(receipt["verification"]["source_unchanged"])
        self.assertTrue(receipt["verification"]["source_bytes_matched"])
        self.assertEqual(len(attached_paths), 1)
        self.assertFalse(attached_paths[0].exists())
        self.assertNotIn(str(source_path), json.dumps(receipt))
        self.assertNotIn("copy-image.", json.dumps(receipt))

    def test_destination_digest_mismatch_cannot_return_verified(self) -> None:
        source = {
            "Z_PK": 1,
            "Z_OPT": 7,
            "ZCKIDENTIFIER": self.SOURCE_ID,
            "ZMARKEDFORDELETION": 0,
        }
        destination = {
            "Z_PK": 2,
            "Z_OPT": 12,
            "ZCKIDENTIFIER": self.DESTINATION_ID,
            "ZMARKEDFORDELETION": 0,
        }
        selected = {"Z_PK": 10, "ZCKIDENTIFIER": self.SOURCE_ATTACHMENT_ID}
        source_bytes = b"private-reminders-png-bytes"
        source_payload = {
            "id": self.SOURCE_ATTACHMENT_ID,
            "type": "image",
            "uti": "public.png",
            "filename": "source.png",
            "sha512": reminders_adapter.hashlib.sha512(source_bytes).hexdigest(),
            "file_size": len(source_bytes),
            "width": 10,
            "height": 10,
            "marked_for_deletion": False,
        }
        mismatched_attachment = {
            **source_payload,
            "id": self.NEW_ATTACHMENT_ID,
            "sha512": "f" * 128,
            "sync": {"mobile_visible_likely": True},
        }
        connection = mock.Mock()

        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            source_path = temp_root / "source.png"
            source_path.write_bytes(source_bytes)

            def validate(path: Path) -> mock.Mock:
                value = Path(path)
                return mock.Mock(
                    path=value,
                    format="png",
                    sha256=reminders_adapter.hashlib.sha256(value.read_bytes()).hexdigest(),
                )

            with (
                mock.patch.object(
                    reminders_adapter,
                    "resolve_database",
                    return_value=temp_root / "store.sqlite",
                ),
                mock.patch.object(reminders_adapter, "connect", return_value=connection),
                mock.patch.object(
                    reminders_adapter,
                    "require_command_capability",
                    return_value={"supported": True},
                ),
                mock.patch.object(
                    reminders_adapter,
                    "find_reminder",
                    side_effect=[source, destination],
                ),
                mock.patch.object(
                    reminders_adapter,
                    "resolve_attachment_selection",
                    side_effect=[
                        (selected, [source_payload], None),
                        (selected, [source_payload], None),
                        (selected, [source_payload], None),
                    ],
                ),
                mock.patch.object(
                    reminders_adapter,
                    "attachment_payload",
                    return_value=source_payload,
                ),
                mock.patch.object(
                    reminders_adapter,
                    "exact_source_image_path",
                    return_value=source_path,
                ),
                mock.patch.object(
                    reminders_adapter,
                    "validate_image_input",
                    side_effect=validate,
                ),
                mock.patch.object(
                    reminders_adapter,
                    "reread_reminder",
                    side_effect=[source, destination, source, destination],
                ),
                mock.patch.object(
                    reminders_adapter,
                    "attach_image_reminderkit_record",
                    return_value={
                        "attachment": mismatched_attachment,
                        "sync": {"mobile_visible_likely": True},
                    },
                ),
                mock.patch.object(reminders_adapter, "log_action", return_value=None),
                mock.patch.object(reminders_adapter, "CACHE_DIR", temp_root / "cache"),
            ):
                receipt = reminders_adapter.copy_image_attachment_once(self._args())

        self.assertEqual(receipt["status"], "committed_verification_pending")
        self.assertFalse(receipt["verification"]["destination_content_matched"])
        self.assertFalse(receipt["verification"]["final_read"])
        self.assertEqual(
            receipt["error"]["reason_code"],
            "destination_image_content_mismatch",
        )

    def test_stale_source_private_version_fails_before_native_attach(self) -> None:
        source = {
            "Z_PK": 1,
            "Z_OPT": 7,
            "ZCKIDENTIFIER": self.SOURCE_ID,
            "ZMARKEDFORDELETION": 0,
        }
        stale_source = {**source, "Z_OPT": 8}
        destination = {
            "Z_PK": 2,
            "Z_OPT": 12,
            "ZCKIDENTIFIER": self.DESTINATION_ID,
            "ZMARKEDFORDELETION": 0,
        }
        selected = {"Z_PK": 10, "ZCKIDENTIFIER": self.SOURCE_ATTACHMENT_ID}
        source_bytes = b"private-reminders-png-bytes"
        source_payload = {
            "id": self.SOURCE_ATTACHMENT_ID,
            "type": "image",
            "uti": "public.png",
            "filename": "source.png",
            "sha512": reminders_adapter.hashlib.sha512(source_bytes).hexdigest(),
            "file_size": len(source_bytes),
            "width": 10,
            "height": 10,
            "marked_for_deletion": False,
        }
        connection = mock.Mock()

        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            source_path = temp_root / "source.png"
            source_path.write_bytes(source_bytes)

            def validate(path: Path) -> mock.Mock:
                value = Path(path)
                return mock.Mock(
                    path=value,
                    format="png",
                    sha256=reminders_adapter.hashlib.sha256(value.read_bytes()).hexdigest(),
                )

            attach = mock.Mock()
            with (
                mock.patch.object(
                    reminders_adapter,
                    "resolve_database",
                    return_value=temp_root / "store.sqlite",
                ),
                mock.patch.object(reminders_adapter, "connect", return_value=connection),
                mock.patch.object(
                    reminders_adapter,
                    "require_command_capability",
                    return_value={"supported": True},
                ),
                mock.patch.object(
                    reminders_adapter,
                    "find_reminder",
                    side_effect=[source, destination],
                ),
                mock.patch.object(
                    reminders_adapter,
                    "resolve_attachment_selection",
                    return_value=(selected, [source_payload], None),
                ),
                mock.patch.object(
                    reminders_adapter,
                    "attachment_payload",
                    return_value=source_payload,
                ),
                mock.patch.object(
                    reminders_adapter,
                    "exact_source_image_path",
                    return_value=source_path,
                ),
                mock.patch.object(
                    reminders_adapter,
                    "validate_image_input",
                    side_effect=validate,
                ),
                mock.patch.object(
                    reminders_adapter,
                    "reread_reminder",
                    side_effect=[stale_source, destination],
                ),
                mock.patch.object(
                    reminders_adapter,
                    "attach_image_reminderkit_record",
                    attach,
                ),
                mock.patch.object(reminders_adapter, "CACHE_DIR", temp_root / "cache"),
            ):
                with self.assertRaises(reminders_adapter.AdapterError) as raised:
                    reminders_adapter.copy_image_attachment_once(self._args())

        self.assertEqual(raised.exception.code, "concurrent_modification")
        attach.assert_not_called()

    def test_source_file_resolution_collapses_only_digest_equivalent_candidates(
        self,
    ) -> None:
        attachment = {
            "id": self.SOURCE_ATTACHMENT_ID,
            "type": "image",
            "marked_for_deletion": False,
        }
        with mock.patch.object(
            reminders_adapter,
            "source_paths_for_attachment",
            return_value=[],
        ):
            with self.assertRaises(reminders_adapter.AdapterError) as raised:
                reminders_adapter.exact_source_image_path(attachment)
        self.assertEqual(raised.exception.code, "invalid_input")

        with tempfile.TemporaryDirectory() as temp:
            files_root = Path(temp) / "Files"
            attachment_root = files_root / "Account-1" / "Attachments"
            attachment_root.mkdir(parents=True)
            first = attachment_root / "same.png"
            second = attachment_root / "same.jpeg"
            content = b"exact-private-image-bytes"
            first.write_bytes(content)
            second.write_bytes(content)
            exact = {
                **attachment,
                "sha512": reminders_adapter.hashlib.sha512(content).hexdigest(),
            }
            with (
                mock.patch.object(reminders_adapter, "FILES", files_root),
                mock.patch.object(
                    reminders_adapter,
                    "source_paths_for_attachment",
                    return_value=[second, first],
                ),
            ):
                resolved = reminders_adapter.exact_source_image_path(exact)
            self.assertEqual(
                resolved,
                min((first.resolve(), second.resolve()), key=lambda path: str(path)),
            )

            second.write_bytes(b"different-private-image-bytes")
            with (
                mock.patch.object(reminders_adapter, "FILES", files_root),
                mock.patch.object(
                    reminders_adapter,
                    "source_paths_for_attachment",
                    return_value=[first, second],
                ),
                self.assertRaises(reminders_adapter.AdapterError) as raised,
            ):
                reminders_adapter.exact_source_image_path(exact)
            self.assertEqual(raised.exception.code, "ambiguous_target")
            self.assertEqual(
                raised.exception.details["reason_code"],
                "source_image_files_diverge",
            )

            target = attachment_root / "target.png"
            target.write_bytes(content)
            symlink = attachment_root / "source.png"
            symlink.symlink_to(target)
            with (
                mock.patch.object(reminders_adapter, "FILES", files_root),
                mock.patch.object(
                    reminders_adapter,
                    "source_paths_for_attachment",
                    return_value=[symlink],
                ),
            ):
                with self.assertRaises(reminders_adapter.AdapterError) as raised:
                    reminders_adapter.exact_source_image_path(exact)
            self.assertEqual(
                raised.exception.details["reason_code"],
                "source_image_file_not_regular",
            )

    def test_idempotency_snapshot_keeps_receipt_proof_but_drops_private_paths(self) -> None:
        snapshot = reminders_adapter.idempotency_result_snapshot(
            {
                "status": "verified",
                "target": {"reminder_id": self.DESTINATION_ID},
                "verification": {
                    "state": "read_back",
                    "write_performed": True,
                    "final_read": True,
                    "matched": True,
                    "source_unchanged": True,
                    "final_attachment_content_hash": "a" * 64,
                },
                "recovery": {
                    "semantics": "not_applicable",
                    "automatic_retry_safe": False,
                },
                "private_path": "/Users/example/Library/private.png",
            }
        )

        self.assertTrue(snapshot["verification"]["write_performed"])
        self.assertTrue(snapshot["verification"]["final_read"])
        self.assertTrue(snapshot["verification"]["matched"])
        self.assertEqual(
            snapshot["verification"]["final_attachment_content_hash"],
            "a" * 64,
        )
        self.assertFalse(snapshot["recovery"]["automatic_retry_safe"])
        self.assertNotIn("private_path", snapshot)


class AttachmentSyncTests(unittest.TestCase):
    def test_reminderkit_attach_does_not_hold_a_sqlite_write_transaction(self) -> None:
        con = sqlite3.connect(":memory:")
        args = mock.Mock(
            db="/tmp/reminders.sqlite",
            id="REM-1",
            title=None,
            list=None,
            image="/tmp/example.png",
            backend="reminderkit",
            if_version=1,
        )
        reminder = {
            "Z_PK": 1,
            "ZCKIDENTIFIER": "REM-1",
            "Z_OPT": 1,
        }

        def attach_without_lock(
            active: sqlite3.Connection,
            *_: object,
            **__: object,
        ) -> dict[str, object]:
            self.assertFalse(active.in_transaction)
            return {
                "attached": True,
                "backend": "reminderkit",
                "attachment": {"id": "ATTACH-1", "type": "image"},
                "sync": {"mobile_visible_likely": True},
            }

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/reminders.sqlite"),
            ),
            mock.patch.object(reminders_adapter, "connect", return_value=con),
            mock.patch.object(
                reminders_adapter,
                "require_command_capability",
                return_value={"supported": True},
            ),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(
                reminders_adapter,
                "attach_image_reminderkit_record",
                side_effect=attach_without_lock,
            ),
            mock.patch.object(
                reminders_adapter,
                "reread_reminder",
                return_value=reminder,
            ),
            mock.patch.object(reminders_adapter, "log_action", return_value=None),
        ):
            receipt = reminders_adapter.attach_image_once(args)

        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(
            receipt["recovery"]["semantics"],
            "delete_attachment_with_fresh_reference",
        )

    def test_native_image_helper_uses_decoded_type_and_data_transport(self) -> None:
        source = (
            reminders_adapter.SCRIPT_DIR / "remkit_attach_image.m"
        ).read_text(encoding="utf-8")

        self.assertIn("CGImageSourceCreateWithData", source)
        self.assertIn("CGImageSourceGetType", source)
        self.assertIn("addImageAttachmentWithData:uti:width:height:", source)
        self.assertNotIn("NSURLContentTypeKey", source)
        self.assertNotIn("triggerCloudKitOnlySyncWithReason:", source)

    def test_native_image_helper_removes_one_exact_attachment_through_save_request(self) -> None:
        source = (
            reminders_adapter.SCRIPT_DIR / "remkit_attach_image.m"
        ).read_text(encoding="utf-8")

        self.assertIn('@"remove"', source)
        self.assertIn("removeAttachment:", source)
        self.assertIn("attachmentUUID", source)
        self.assertIn("saveSynchronouslyWithError:", source)
        self.assertIn('@"mutation_attempted": @YES', source)

    def test_reminderkit_image_removal_requires_exact_reminder_detachment(self) -> None:
        reminder_id = "7718459E-2672-4E99-9E6A-B9AA430E570F"
        attachment_id = "8718459E-2672-4E99-9E6A-B9AA430E570F"
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "reminders.sqlite"
            helper = Path(tmp) / "remkit_attach_image"
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            con.executescript(
                """
                create table ZREMCDOBJECT (
                    Z_PK integer primary key,
                    Z_ENT integer,
                    ZCKIDENTIFIER text,
                    ZCKCLOUDSTATE integer,
                    ZREMINDER2 integer,
                    Z_FOK_REMINDER1 integer,
                    ZFILENAME text,
                    ZSHA512SUM text,
                    ZUTI text,
                    ZFILESIZE integer,
                    ZWIDTH integer,
                    ZHEIGHT integer,
                    ZURL text,
                    ZHOSTURL text,
                    ZMARKEDFORDELETION integer
                );
                create table ZREMCKCLOUDSTATE (
                    Z_PK integer primary key,
                    ZOBJECT integer,
                    Z13_OBJECT integer
                );
                insert into ZREMCKCLOUDSTATE values (7,10,25);
                insert into ZREMCDOBJECT values (
                    10,25,'8718459E-2672-4E99-9E6A-B9AA430E570F',7,1,1024,'old.png','sha','public.png',
                    12,100,50,null,null,0
                );
                """
            )
            con.commit()
            attachment = dict(
                con.execute("select * from ZREMCDOBJECT where Z_PK=10").fetchone()
            )
            con.close()

            def native_remove(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
                external = sqlite3.connect(db)
                try:
                    external.execute(
                        "update ZREMCDOBJECT set ZREMINDER2=null where Z_PK=10"
                    )
                    external.commit()
                finally:
                    external.close()
                return subprocess.CompletedProcess(
                    [str(helper), "remove", reminder_id, attachment_id],
                    0,
                    stdout=json.dumps(
                        {
                            "ok": True,
                            "backend": "reminderkit",
                            "operation": "remove_attachment",
                            "reminder_id": reminder_id,
                            "attachment_id": attachment_id,
                        }
                    )
                    + "\n",
                    stderr="",
                )

            try:
                with (
                    mock.patch.object(
                        reminders_adapter,
                        "reminderkit_attach_helper",
                        return_value=helper,
                    ),
                    mock.patch.object(
                        reminders_adapter.subprocess,
                        "run",
                        side_effect=native_remove,
                    ) as run,
                    mock.patch.object(
                        reminders_adapter,
                        "REMINDERKIT_REMOVAL_SETTLE_SECONDS",
                        0,
                    ),
                ):
                    removed = reminders_adapter.remove_image_reminderkit_record(
                        db,
                        {"Z_PK": 1, "ZCKIDENTIFIER": reminder_id},
                        attachment,
                    )
            finally:
                con.close()

        self.assertFalse(removed["row_deleted"])
        self.assertTrue(removed["detached_from_reminder"])
        self.assertTrue(removed["cloud_state_tombstone_retained"])
        self.assertTrue(removed["native_reminderkit"])
        self.assertEqual(
            run.call_args.args[0],
            [str(helper), "remove", reminder_id, attachment_id],
        )

    def test_reminderkit_image_removal_timeout_is_an_unknown_mutation(self) -> None:
        reminder_id = "7718459E-2672-4E99-9E6A-B9AA430E570F"
        attachment_id = "8718459E-2672-4E99-9E6A-B9AA430E570F"
        attachment = {
            "Z_PK": 10,
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
            "ZCKIDENTIFIER": attachment_id,
            "ZCKCLOUDSTATE": 7,
            "ZREMINDER2": 1,
            "Z_FOK_REMINDER1": 1024,
            "ZFILENAME": "old.png",
            "ZSHA512SUM": "sha",
            "ZUTI": "public.png",
            "ZFILESIZE": 12,
            "ZWIDTH": 100,
            "ZHEIGHT": 50,
            "ZURL": None,
            "ZHOSTURL": None,
            "ZMARKEDFORDELETION": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "reminders.sqlite"
            helper = Path(tmp) / "remkit_attach_image"
            db.touch()
            helper.write_text("#!/bin/sh\n", encoding="utf-8")

            with (
                mock.patch.object(
                    reminders_adapter,
                    "reminderkit_attach_helper",
                    return_value=helper,
                ),
                mock.patch.object(
                    reminders_adapter.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired(str(helper), 1),
                ),
                self.assertRaises(reminders_adapter.AdapterError) as raised,
            ):
                reminders_adapter.remove_image_reminderkit_record(
                    db,
                    {"Z_PK": 1, "ZCKIDENTIFIER": reminder_id},
                    attachment,
                )

        self.assertEqual(raised.exception.code, "sync_pending")
        self.assertTrue(raised.exception.details["partial_failure"])
        self.assertTrue(raised.exception.details["mutation_outcome_unknown"])

    def test_reminderkit_image_removal_empty_output_is_an_unknown_mutation(self) -> None:
        reminder_id = "7718459E-2672-4E99-9E6A-B9AA430E570F"
        attachment_id = "8718459E-2672-4E99-9E6A-B9AA430E570F"
        attachment = {
            "Z_PK": 10,
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
            "ZCKIDENTIFIER": attachment_id,
            "ZCKCLOUDSTATE": None,
            "ZREMINDER2": 1,
            "Z_FOK_REMINDER1": 1024,
            "ZFILENAME": "old.png",
            "ZSHA512SUM": "sha",
            "ZUTI": "public.png",
            "ZFILESIZE": 12,
            "ZWIDTH": 1,
            "ZHEIGHT": 1,
            "ZURL": None,
            "ZHOSTURL": None,
            "ZMARKEDFORDELETION": 0,
        }

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "reminders.sqlite"
            helper = Path(tmp) / "remkit_attach_image"
            db.touch()
            helper.write_text("#!/bin/sh\n", encoding="utf-8")

            with (
                mock.patch.object(
                    reminders_adapter,
                    "reminderkit_attach_helper",
                    return_value=helper,
                ),
                mock.patch.object(
                    reminders_adapter.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [str(helper), "remove", reminder_id, attachment_id],
                        -9,
                        stdout="",
                        stderr="",
                    ),
                ),
                self.assertRaises(reminders_adapter.AdapterError) as raised,
            ):
                reminders_adapter.remove_image_reminderkit_record(
                    db,
                    {"Z_PK": 1, "ZCKIDENTIFIER": reminder_id},
                    attachment,
                )

        self.assertEqual(raised.exception.code, "sync_pending")
        self.assertTrue(raised.exception.details["partial_failure"])
        self.assertTrue(raised.exception.details["mutation_outcome_unknown"])

    def test_reminderkit_image_removal_rejects_wrong_cloud_tombstone_owner(self) -> None:
        reminder_id = "7718459E-2672-4E99-9E6A-B9AA430E570F"
        attachment_id = "8718459E-2672-4E99-9E6A-B9AA430E570F"
        attachment = {
            "Z_PK": 10,
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
            "ZCKIDENTIFIER": attachment_id,
            "ZCKCLOUDSTATE": 7,
            "ZREMINDER2": 1,
            "Z_FOK_REMINDER1": 1024,
            "ZFILENAME": "old.png",
            "ZSHA512SUM": "sha",
            "ZUTI": "public.png",
            "ZFILESIZE": 12,
            "ZWIDTH": 1,
            "ZHEIGHT": 1,
            "ZURL": None,
            "ZHOSTURL": None,
            "ZMARKEDFORDELETION": 0,
        }
        remaining = {
            "Z_PK": 10,
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
            "ZCKIDENTIFIER": attachment_id,
            "ZCKCLOUDSTATE": 7,
            "ZREMINDER2": None,
        }
        fresh = mock.Mock()
        fresh.execute.side_effect = [
            mock.Mock(fetchone=mock.Mock(return_value=remaining)),
            mock.Mock(fetchone=mock.Mock(return_value=None)),
            mock.Mock(
                fetchone=mock.Mock(
                    return_value={
                        "Z_PK": 7,
                        "ZOBJECT": 999,
                        "Z13_OBJECT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
                    }
                )
            ),
        ]
        helper_payload = json.dumps(
            {
                "ok": True,
                "operation": "remove_attachment",
                "reminder_id": reminder_id,
                "attachment_id": attachment_id,
            }
        )

        with (
            mock.patch.object(
                reminders_adapter,
                "reminderkit_attach_helper",
                return_value=Path("/tmp/remkit_attach_image"),
            ),
            mock.patch.object(
                reminders_adapter.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout=helper_payload, stderr=""
                ),
            ),
            mock.patch.object(
                reminders_adapter,
                "connect_read_only",
                return_value=fresh,
            ),
            mock.patch.object(
                reminders_adapter,
                "REMINDERKIT_REMOVAL_SETTLE_SECONDS",
                0,
            ),
            mock.patch.object(
                reminders_adapter,
                "REMINDERKIT_REMOVAL_VERIFY_TIMEOUT_SECONDS",
                0,
            ),
            self.assertRaises(reminders_adapter.AdapterError) as raised,
        ):
            reminders_adapter.remove_image_reminderkit_record(
                Path("/tmp/reminders.sqlite"),
                {"Z_PK": 1, "ZCKIDENTIFIER": reminder_id},
                attachment,
            )

        self.assertEqual(raised.exception.code, "sync_pending")
        self.assertFalse(
            raised.exception.details["cloud_state_tombstone_retained"]
        )
        self.assertTrue(raised.exception.details["mutation_outcome_unknown"])

    def test_image_delete_uses_native_reminderkit_removal(self) -> None:
        db = Path("/tmp/reminders.sqlite")
        args = mock.Mock(
            db=str(db),
            id="7718459E-2672-4E99-9E6A-B9AA430E570F",
            title=None,
            list=None,
            attachment_id="8718459E-2672-4E99-9E6A-B9AA430E570F",
            attachment_pk=None,
            type=None,
            filename=None,
            url=None,
            if_version=1,
        )
        initial = mock.Mock()
        reopened = mock.Mock()
        reopened.execute.return_value.fetchone.return_value = None
        reminder = {
            "Z_PK": 1,
            "ZCKIDENTIFIER": args.id,
            "Z_OPT": 1,
        }
        attachment = {
            "Z_PK": 10,
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
            "ZCKIDENTIFIER": args.attachment_id,
        }
        removed = {
            "id": args.attachment_id,
            "row_deleted": False,
            "detached_from_reminder": True,
            "cloud_state_tombstone_retained": True,
            "native_reminderkit": True,
        }

        with (
            mock.patch.object(reminders_adapter, "resolve_database", return_value=db),
            mock.patch.object(
                reminders_adapter,
                "connect",
                side_effect=[initial, reopened],
            ),
            mock.patch.object(
                reminders_adapter,
                "require_command_capability",
                return_value={"supported": True},
            ),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(
                reminders_adapter,
                "resolve_attachment_selection",
                return_value=(attachment, [], None),
            ),
            mock.patch.object(
                reminders_adapter,
                "attachment_payload",
                return_value={"id": args.attachment_id, "type": "image"},
            ),
            mock.patch.object(
                reminders_adapter,
                "remove_image_reminderkit_record",
                return_value=removed,
            ) as remove_native,
            mock.patch.object(
                reminders_adapter,
                "reminder_mutation_snapshot",
                side_effect=[{"version": 1}, {"version": 2}],
            ),
            mock.patch.object(
                reminders_adapter,
                "reread_reminder",
                return_value={**reminder, "Z_OPT": 2},
            ),
            mock.patch.object(reminders_adapter, "log_action", return_value=None),
            mock.patch.object(reminders_adapter, "json_out") as json_out,
        ):
            result = reminders_adapter.cmd_delete_attachment(args)

        self.assertEqual(result, 0)
        initial.close.assert_called_once()
        remove_native.assert_called_once_with(db, reminder, attachment)
        receipt = json_out.call_args.args[0]
        self.assertEqual(receipt["backend"], "reminderkit")
        self.assertTrue(receipt["verification"]["native_reminderkit"])

    def test_image_delete_unknown_outcome_does_not_reopen_write_connection(self) -> None:
        db = Path("/tmp/reminders.sqlite")
        args = mock.Mock(
            db=str(db),
            id="7718459E-2672-4E99-9E6A-B9AA430E570F",
            title=None,
            list=None,
            attachment_id="8718459E-2672-4E99-9E6A-B9AA430E570F",
            attachment_pk=None,
            type=None,
            filename=None,
            url=None,
            if_version=1,
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": args.id, "Z_OPT": 1}
        attachment = {
            "Z_PK": 10,
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
            "ZCKIDENTIFIER": args.attachment_id,
        }
        uncertain = reminders_adapter.AdapterError(
            "native result lost",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
        )

        with (
            mock.patch.object(reminders_adapter, "resolve_database", return_value=db),
            mock.patch.object(reminders_adapter, "connect", return_value=con) as connect,
            mock.patch.object(
                reminders_adapter,
                "require_command_capability",
                return_value={"supported": True},
            ),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(
                reminders_adapter,
                "resolve_attachment_selection",
                return_value=(attachment, [], None),
            ),
            mock.patch.object(
                reminders_adapter,
                "attachment_payload",
                return_value={"id": args.attachment_id, "type": "image"},
            ),
            mock.patch.object(
                reminders_adapter,
                "remove_image_reminderkit_record",
                side_effect=uncertain,
            ),
            self.assertRaises(reminders_adapter.AdapterError),
        ):
            reminders_adapter.cmd_delete_attachment(args)

        connect.assert_called_once_with(db)
        con.close.assert_called_once()

    def test_image_delete_reconnect_failure_preserves_native_commit_evidence(self) -> None:
        db = Path("/tmp/reminders.sqlite")
        args = mock.Mock(
            db=str(db),
            id="7718459E-2672-4E99-9E6A-B9AA430E570F",
            title=None,
            list=None,
            attachment_id="8718459E-2672-4E99-9E6A-B9AA430E570F",
            attachment_pk=None,
            type=None,
            filename=None,
            url=None,
            if_version=1,
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": args.id, "Z_OPT": 1}
        attachment = {
            "Z_PK": 10,
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
            "ZCKIDENTIFIER": args.attachment_id,
        }

        with (
            mock.patch.object(reminders_adapter, "resolve_database", return_value=db),
            mock.patch.object(
                reminders_adapter,
                "connect",
                side_effect=[con, sqlite3.OperationalError("reopen failed")],
            ),
            mock.patch.object(
                reminders_adapter,
                "require_command_capability",
                return_value={"supported": True},
            ),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(
                reminders_adapter,
                "resolve_attachment_selection",
                return_value=(attachment, [], None),
            ),
            mock.patch.object(
                reminders_adapter,
                "attachment_payload",
                return_value={"id": args.attachment_id, "type": "image"},
            ),
            mock.patch.object(
                reminders_adapter,
                "remove_image_reminderkit_record",
                return_value={
                    "id": args.attachment_id,
                    "detached_from_reminder": True,
                    "native_reminderkit": True,
                },
            ),
            self.assertRaises(reminders_adapter.AdapterError) as raised,
        ):
            reminders_adapter.cmd_delete_attachment(args)

        self.assertEqual(raised.exception.code, "sync_pending")
        self.assertTrue(raised.exception.details["native_removal_verified"])
        self.assertTrue(raised.exception.details["mutation_outcome_unknown"])

    def test_image_delete_post_native_read_failure_preserves_commit_evidence(self) -> None:
        db = Path("/tmp/reminders.sqlite")
        args = mock.Mock(
            db=str(db),
            id="7718459E-2672-4E99-9E6A-B9AA430E570F",
            title=None,
            list=None,
            attachment_id="8718459E-2672-4E99-9E6A-B9AA430E570F",
            attachment_pk=None,
            type=None,
            filename=None,
            url=None,
            if_version=1,
        )
        initial = mock.Mock()
        reopened = mock.Mock()
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": args.id, "Z_OPT": 1}
        attachment = {
            "Z_PK": 10,
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
            "ZCKIDENTIFIER": args.attachment_id,
        }

        with (
            mock.patch.object(reminders_adapter, "resolve_database", return_value=db),
            mock.patch.object(
                reminders_adapter,
                "connect",
                side_effect=[initial, reopened],
            ),
            mock.patch.object(
                reminders_adapter,
                "require_command_capability",
                return_value={"supported": True},
            ),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(
                reminders_adapter,
                "resolve_attachment_selection",
                return_value=(attachment, [], None),
            ),
            mock.patch.object(
                reminders_adapter,
                "attachment_payload",
                return_value={"id": args.attachment_id, "type": "image"},
            ),
            mock.patch.object(
                reminders_adapter,
                "remove_image_reminderkit_record",
                return_value={
                    "id": args.attachment_id,
                    "detached_from_reminder": True,
                    "native_reminderkit": True,
                },
            ),
            mock.patch.object(
                reminders_adapter,
                "reread_reminder",
                side_effect=reminders_adapter.AdapterError(
                    "post-native read failed",
                    code="schema_mismatch",
                ),
            ),
            self.assertRaises(reminders_adapter.AdapterError) as raised,
        ):
            reminders_adapter.cmd_delete_attachment(args)

        self.assertEqual(raised.exception.code, "sync_pending")
        self.assertTrue(raised.exception.details["partial_failure"])
        self.assertTrue(raised.exception.details["native_removal_verified"])

    def test_image_attachment_payload_marks_cloudkit_attachment_mobile_visible(self) -> None:
        payload = reminders_adapter.attachment_payload(
            {
                "Z_PK": 1,
                "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
                "ZCKIDENTIFIER": "ATTACH-1",
                "ZUTI": "public.png",
                "Z_FOK_REMINDER1": 1024,
                "ZMARKEDFORDELETION": 0,
                "ZFILENAME": "image.png",
                "ZSHA512SUM": "abc",
                "ZFILESIZE": 12,
                "ZWIDTH": 100,
                "ZHEIGHT": 50,
                "ZURL": None,
                "ZHOSTURL": None,
                "HAS_SERVER_RECORD": 1,
                "SERVER_RECORD_BYTES": 2048,
                "ZINCLOUD": 1,
                "ZCURRENTLOCALVERSION": 1,
                "ZLATESTVERSIONSYNCEDTOCLOUD": 1,
            }
        )

        self.assertTrue(payload["sync"]["mobile_visible_likely"])
        self.assertTrue(payload["sync"]["has_server_record"])

    def test_image_attachment_payload_marks_db_only_attachment_local_only(self) -> None:
        payload = reminders_adapter.attachment_payload(
            {
                "Z_PK": 1,
                "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
                "ZCKIDENTIFIER": "ATTACH-1",
                "ZUTI": "public.png",
                "Z_FOK_REMINDER1": 1024,
                "ZMARKEDFORDELETION": 0,
                "ZFILENAME": "image.png",
                "ZSHA512SUM": "abc",
                "ZFILESIZE": 12,
                "ZWIDTH": 100,
                "ZHEIGHT": 50,
                "ZURL": None,
                "ZHOSTURL": None,
                "HAS_SERVER_RECORD": 0,
                "SERVER_RECORD_BYTES": None,
                "ZINCLOUD": None,
                "ZCURRENTLOCALVERSION": 1,
                "ZLATESTVERSIONSYNCEDTOCLOUD": 0,
            }
        )

        self.assertFalse(payload["sync"]["mobile_visible_likely"])
        self.assertFalse(payload["sync"]["has_server_record"])

    def test_active_attachment_rows_marks_mobile_visibility_unknown_when_sync_columns_are_missing(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        try:
            con.executescript(
                """
                create table ZREMCDOBJECT (
                    Z_PK integer primary key,
                    Z_ENT integer,
                    ZCKIDENTIFIER text,
                    ZCKCLOUDSTATE integer,
                    ZREMINDER2 integer,
                    Z_FOK_REMINDER1 integer,
                    ZFILENAME text,
                    ZSHA512SUM text,
                    ZUTI text,
                    ZFILESIZE integer,
                    ZWIDTH integer,
                    ZHEIGHT integer,
                    ZURL text,
                    ZHOSTURL text,
                    ZMARKEDFORDELETION integer
                );
                create table ZREMCKCLOUDSTATE (Z_PK integer primary key);
                insert into ZREMCDOBJECT values (
                    10,25,'ATTACH-1',null,1,1024,'image.png','sha','public.png',
                    12,100,50,null,null,0
                );
                """
            )

            rows = reminders_adapter.active_attachment_rows(con, 1, attachment_ent=reminders_adapter.IMAGE_ATTACHMENT_ENT)
            payload = reminders_adapter.attachment_payload(rows[0])
        finally:
            con.close()

        self.assertIsNone(payload["sync"]["mobile_visible_likely"])
        self.assertFalse(payload["sync"]["fields_available"])

    def test_problem_attachment_limit_is_applied_after_problem_filter(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        try:
            con.executescript(
                """
                create table ZREMCDBASELIST (Z_PK integer primary key, ZNAME text);
                create table ZREMCDREMINDER (
                    Z_PK integer primary key,
                    Z_OPT integer,
                    ZCKIDENTIFIER text,
                    ZTITLE text,
                    ZNOTES text,
                    ZACCOUNT text,
                    ZLIST integer,
                    ZMARKEDFORDELETION integer
                );
                create table ZREMCKCLOUDSTATE (
                    Z_PK integer primary key,
                    ZINCLOUD integer,
                    ZCURRENTLOCALVERSION integer,
                    ZLATESTVERSIONSYNCEDTOCLOUD integer
                );
                create table ZREMCDOBJECT (
                    Z_PK integer primary key,
                    Z_ENT integer,
                    ZCKIDENTIFIER text,
                    ZCKCLOUDSTATE integer,
                    ZREMINDER2 integer,
                    Z_FOK_REMINDER1 integer,
                    ZFILENAME text,
                    ZSHA512SUM text,
                    ZUTI text,
                    ZFILESIZE integer,
                    ZWIDTH integer,
                    ZHEIGHT integer,
                    ZURL text,
                    ZHOSTURL text,
                    ZMARKEDFORDELETION integer,
                    ZCKSERVERRECORDDATA blob
                );
                insert into ZREMCDBASELIST values (1,'Inbox');
                insert into ZREMCDREMINDER values (1,1,'REM-GOOD','A Good','',null,1,0);
                insert into ZREMCDREMINDER values (2,1,'REM-BAD','B Bad','',null,1,0);
                insert into ZREMCKCLOUDSTATE values (1,1,1,1);
                insert into ZREMCKCLOUDSTATE values (2,0,1,0);
                insert into ZREMCDOBJECT values (
                    10,25,'ATTACH-GOOD',1,1,1024,'good.png','good','public.png',
                    12,100,50,null,null,0,X'01'
                );
                insert into ZREMCDOBJECT values (
                    11,25,'ATTACH-BAD',2,2,1024,'bad.png','bad','public.png',
                    12,100,50,null,null,0,X'02'
                );
                """
            )

            counts = reminders_adapter.image_attachment_audit_counts(con)
            items, problem_count = reminders_adapter.image_attachment_audit_items(
                con,
                problems_only=True,
                limit=1,
            )
        finally:
            con.close()

        self.assertEqual(counts, {"total": 2, "problems": 1})
        self.assertEqual(problem_count, 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["attachment"]["id"], "ATTACH-BAD")

    def test_attach_image_defaults_to_reminderkit_backend(self) -> None:
        parser = reminders_adapter.build_parser()

        args = parser.parse_args(
            [
                "attach_image",
                "--id",
                "AAA",
                "--image",
                "/tmp/example.png",
                "--if-version",
                "7",
            ]
        )

        self.assertEqual(args.backend, "reminderkit")
        self.assertEqual(args.if_version, 7)

    def test_repair_attachments_defaults_to_dry_run(self) -> None:
        parser = reminders_adapter.build_parser()

        args = parser.parse_args(["repair_attachments", "--search", "취업캠프"])

        self.assertFalse(args.apply)
        self.assertEqual(args.limit, 50)

    def test_repair_apply_rejects_missing_sync_verification_columns(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("create table ZREMCDOBJECT (Z_PK integer primary key)")
        con.execute("create table ZREMCKCLOUDSTATE (Z_PK integer primary key)")
        args = argparse.Namespace(
            db="/tmp/reminders.sqlite",
            search=None,
            list=None,
            limit=1,
            apply=True,
            no_backup=True,
        )

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/reminders.sqlite"),
            ),
            mock.patch.object(reminders_adapter, "connect", return_value=con),
            self.assertRaises(reminders_adapter.AdapterError) as raised,
        ):
            reminders_adapter.cmd_repair_attachments(args)

        self.assertIn("sync verification columns", str(raised.exception))
        self.assertTrue(raised.exception.details["missing_columns"])

    def test_replace_image_uses_reminderkit_backend(self) -> None:
        reminder_id = "7718459E-2672-4E99-9E6A-B9AA430E570F"
        args = mock.Mock(
            db="/tmp/reminders.sqlite",
            image="/tmp/example.png",
            url=None,
            id=reminder_id,
            title=None,
            list=None,
            type=None,
            attachment_id="ATTACH-OLD",
            attachment_pk=None,
            filename=None,
            old_url=None,
            if_version=1,
        )
        con = mock.Mock()
        selected = {"Z_PK": 10, "ZCKIDENTIFIER": "ATTACH-OLD", "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT}
        new_result = {"attachment": {"pk": 11, "id": "ATTACH-NEW"}}

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/reminders.sqlite"),
            ),
            mock.patch.object(reminders_adapter, "connect", return_value=con),
            mock.patch.object(
                reminders_adapter,
                "require_command_capability",
                return_value={"supported": True},
            ),
            mock.patch.object(
                reminders_adapter,
                "find_reminder",
                return_value={"Z_PK": 1, "ZCKIDENTIFIER": reminder_id, "Z_OPT": 1},
            ),
            mock.patch.object(reminders_adapter, "resolve_attachment_selection", return_value=(selected, [], None)),
            mock.patch.object(reminders_adapter, "attachment_payload", return_value={"pk": 10, "id": "ATTACH-OLD"}),
            mock.patch.object(reminders_adapter, "attach_image_reminderkit_record", return_value=new_result) as attach_reminderkit,
            mock.patch.object(reminders_adapter, "attach_image_record") as attach_db,
            mock.patch.object(
                reminders_adapter,
                "remove_image_reminderkit_record",
                return_value={
                    "pk": 10,
                    "row_deleted": True,
                    "cloud_state_tombstone_retained": True,
                },
            ) as remove_native,
            mock.patch.object(
                reminders_adapter,
                "attachment_replacement_readback",
                return_value={
                    "old_attachment_removed": True,
                    "old_attachment_detached_from_reminder": True,
                    "new_attachment_active": True,
                },
            ),
            mock.patch.object(reminders_adapter, "log_action"),
            mock.patch.object(reminders_adapter, "json_out"),
        ):
            result = reminders_adapter.cmd_replace_attachment(args)

        self.assertEqual(result, 0)
        attach_reminderkit.assert_called_once()
        remove_native.assert_called_once_with(
            Path("/tmp/reminders.sqlite"),
            mock.ANY,
            selected,
        )
        attach_db.assert_not_called()

    def test_helper_attach_does_not_treat_existing_row_as_new_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "reminders.sqlite"
            image = Path(tmp) / "image.png"
            helper = Path(tmp) / "remkit_attach_image"
            image.write_bytes(b"fake-png")
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                con.executescript(
                    """
                    create table ZREMCDOBJECT (
                        Z_PK integer primary key,
                        Z_ENT integer,
                        ZCKIDENTIFIER text,
                        ZCKCLOUDSTATE integer,
                        ZREMINDER2 integer,
                        Z_FOK_REMINDER1 integer,
                        ZFILENAME text,
                        ZSHA512SUM text,
                        ZUTI text,
                        ZFILESIZE integer,
                        ZWIDTH integer,
                        ZHEIGHT integer,
                        ZURL text,
                        ZHOSTURL text,
                        ZMARKEDFORDELETION integer,
                        ZCKSERVERRECORDDATA blob
                    );
                    create table ZREMCKCLOUDSTATE (
                        Z_PK integer primary key,
                        ZINCLOUD integer,
                        ZCURRENTLOCALVERSION integer,
                        ZLATESTVERSIONSYNCEDTOCLOUD integer
                    );
                    insert into ZREMCKCLOUDSTATE values (7,1,1,1);
                    insert into ZREMCDOBJECT values (
                        10,25,'EXISTING-ATTACHMENT',7,1,1024,'old.png','sha','public.png',
                        12,100,50,null,null,0,X'0102'
                    );
                    """
                )
                proc = subprocess.CompletedProcess(
                    [str(helper), "REM-1", str(image)],
                    0,
                    stdout=json.dumps({"ok": True, "backend": "reminderkit"}) + "\n",
                    stderr="",
                )

                with (
                    mock.patch.object(reminders_adapter, "reminderkit_attach_helper", return_value=helper),
                    mock.patch.object(reminders_adapter.subprocess, "run", return_value=proc),
                    self.assertRaises(reminders_adapter.AdapterError) as raised,
                ):
                    reminders_adapter.attach_image_reminderkit_record(
                        con,
                        {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1"},
                        image,
                        validated_image=validated_image_fixture(image),
                    )
            finally:
                con.close()

        self.assertEqual(raised.exception.code, "sync_pending")
        self.assertTrue(raised.exception.details["partial_failure"])
        self.assertTrue(raised.exception.details["mutation_outcome_unknown"])

    def test_helper_attach_raises_partial_failure_when_new_row_is_not_mobile_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "reminders.sqlite"
            image = Path(tmp) / "image.png"
            helper = Path(tmp) / "remkit_attach_image"
            image.write_bytes(b"fake-png")
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                con.executescript(
                    """
                    create table ZREMCDOBJECT (
                        Z_PK integer primary key,
                        Z_ENT integer,
                        ZCKIDENTIFIER text,
                        ZCKCLOUDSTATE integer,
                        ZREMINDER2 integer,
                        Z_FOK_REMINDER1 integer,
                        ZFILENAME text,
                        ZSHA512SUM text,
                        ZUTI text,
                        ZFILESIZE integer,
                        ZWIDTH integer,
                        ZHEIGHT integer,
                        ZURL text,
                        ZHOSTURL text,
                        ZMARKEDFORDELETION integer,
                        ZCKSERVERRECORDDATA blob
                    );
                    create table ZREMCKCLOUDSTATE (
                        Z_PK integer primary key,
                        ZINCLOUD integer,
                        ZCURRENTLOCALVERSION integer,
                        ZLATESTVERSIONSYNCEDTOCLOUD integer
                    );
                    insert into ZREMCKCLOUDSTATE values (7,0,1,0);
                    """
                )

                def add_unverified_attachment(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
                    con.execute(
                        """
                        insert into ZREMCDOBJECT values (
                            11,25,'7718459E-2672-4E99-9E6A-B9AA430E570F',7,1,1024,
                            'new.png','sha','public.png',12,100,50,null,null,0,null
                        )
                        """
                    )
                    con.commit()
                    return subprocess.CompletedProcess(
                        [str(helper), "REM-1", str(image)],
                        0,
                        stdout=json.dumps(
                            {
                                "ok": True,
                                "backend": "reminderkit",
                                "attachment_id": "7718459E-2672-4E99-9E6A-B9AA430E570F",
                            }
                        )
                        + "\n",
                        stderr="",
                    )

                with (
                    mock.patch.object(reminders_adapter, "reminderkit_attach_helper", return_value=helper),
                    mock.patch.object(reminders_adapter.subprocess, "run", side_effect=add_unverified_attachment),
                    mock.patch.object(reminders_adapter, "ATTACHMENT_VERIFY_TIMEOUT_SECONDS", 0),
                    self.assertRaises(reminders_adapter.AdapterError) as raised,
                ):
                    reminders_adapter.attach_image_reminderkit_record(
                        con,
                        {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1"},
                        image,
                        validated_image=validated_image_fixture(image),
                    )
            finally:
                con.close()

        self.assertTrue(raised.exception.details["partial_failure"])
        self.assertIn("delete_attachment", raised.exception.details["cleanup_command"])

    def test_helper_attach_waits_through_delayed_mobile_visibility(self) -> None:
        attachment_id = "7718459E-2672-4E99-9E6A-B9AA430E570F"
        row = {
            "Z_PK": 11,
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
            "ZCKIDENTIFIER": attachment_id,
            "ZCKCLOUDSTATE": 7,
            "ZREMINDER2": 1,
            "Z_FOK_REMINDER1": 1024,
            "ZFILENAME": "new.png",
            "ZSHA512SUM": hashlib.sha512(b"synthetic").hexdigest(),
            "ZUTI": "public.png",
            "ZFILESIZE": len(b"synthetic"),
            "ZWIDTH": 100,
            "ZHEIGHT": 50,
            "ZURL": None,
            "ZHOSTURL": None,
            "ZMARKEDFORDELETION": 0,
            "HAS_SERVER_RECORD": 1,
            "SERVER_RECORD_BYTES": 2048,
            "ZINCLOUD": 0,
            "ZCURRENTLOCALVERSION": 1,
            "ZLATESTVERSIONSYNCEDTOCLOUD": 0,
        }
        clock = {"now": 0.0}
        calls = {"rows": 0}

        def rows(*_: object, **__: object) -> list[dict[str, object]]:
            calls["rows"] += 1
            if calls["rows"] == 1:
                return []
            current = dict(row)
            if clock["now"] >= 7.0:
                current.update(
                    {
                        "ZINCLOUD": 1,
                        "ZCURRENTLOCALVERSION": 2,
                        "ZLATESTVERSIONSYNCEDTOCLOUD": 2,
                    }
                )
            return [current]

        def advance(seconds: float) -> None:
            clock["now"] += seconds

        con = mock.Mock()
        con.execute.return_value.fetchone.return_value = (
            0,
            "main",
            "/tmp/reminders.sqlite",
        )
        fresh = mock.Mock()
        proc = subprocess.CompletedProcess(
            ["remkit_attach_image", "REM-1", "/tmp/image.png"],
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "attachment_id": attachment_id,
                    "attachment_transport": "data",
                    "image_uti": "public.png",
                }
            ),
            stderr="",
        )

        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "image.png"
            image.write_bytes(b"synthetic")
            with (
                mock.patch.object(
                    reminders_adapter,
                    "active_attachment_rows",
                    side_effect=rows,
                ),
                mock.patch.object(
                    reminders_adapter,
                    "reminderkit_attach_helper",
                    return_value=Path("/tmp/remkit_attach_image"),
                ),
                mock.patch.object(
                    reminders_adapter.subprocess,
                    "run",
                    return_value=proc,
                ),
                mock.patch.object(
                    reminders_adapter,
                    "connect_read_only",
                    return_value=fresh,
                ),
                mock.patch.object(
                    reminders_adapter.time,
                    "time",
                    side_effect=lambda: clock["now"],
                ),
                mock.patch.object(
                    reminders_adapter.time,
                    "sleep",
                    side_effect=advance,
                ),
            ):
                result = reminders_adapter.attach_image_reminderkit_record(
                    con,
                    {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1"},
                    image,
                    validated_image=validated_image_fixture(image),
                )

        self.assertTrue(result["attached"])
        self.assertTrue(result["sync"]["mobile_visible_likely"])
        self.assertGreaterEqual(clock["now"], 7.0)
        fresh.close.assert_called()

    def test_helper_attach_rejects_unrenderable_evidence(self) -> None:
        """Cloud evidence cannot bless the wrong transport or a mismatched UTI."""
        cases = (
            (
                "url_transport",
                "url",
                "public.png",
                "attachment_transport",
                "url",
                "native_image_transport_mismatch",
            ),
            (
                "uti_mismatch",
                "data",
                "public.jpeg",
                "stored_image_uti",
                "public.png",
                "native_image_content_type_mismatch",
            ),
            (
                "content_mismatch",
                "data",
                "public.png",
                None,
                None,
                "native_image_content_mismatch",
            ),
        )
        for (
            name,
            transport,
            helper_uti,
            detail_key,
            detail_value,
            reason_code,
        ) in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "reminders.sqlite"
                image = Path(tmp) / "browser-capture.png"
                helper = Path(tmp) / "remkit_attach_image"
                image.write_bytes(b"fake-image")
                helper.write_text("#!/bin/sh\n", encoding="utf-8")
                con = sqlite3.connect(db)
                con.row_factory = sqlite3.Row
                try:
                    con.executescript(
                        """
                        create table ZREMCDOBJECT (
                            Z_PK integer primary key,
                            Z_ENT integer,
                            ZCKIDENTIFIER text,
                            ZCKCLOUDSTATE integer,
                            ZREMINDER2 integer,
                            Z_FOK_REMINDER1 integer,
                            ZFILENAME text,
                            ZSHA512SUM text,
                            ZUTI text,
                            ZFILESIZE integer,
                            ZWIDTH integer,
                            ZHEIGHT integer,
                            ZURL text,
                            ZHOSTURL text,
                            ZMARKEDFORDELETION integer,
                            ZCKSERVERRECORDDATA blob
                        );
                        create table ZREMCKCLOUDSTATE (
                            Z_PK integer primary key,
                            ZINCLOUD integer,
                            ZCURRENTLOCALVERSION integer,
                            ZLATESTVERSIONSYNCEDTOCLOUD integer
                        );
                        insert into ZREMCKCLOUDSTATE values (7,1,1,1);
                        """
                    )

                    def add_attachment(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
                        con.execute(
                            """
                            insert into ZREMCDOBJECT values (
                                11,25,'7718459E-2672-4E99-9E6A-B9AA430E570F',7,1,1024,
                                'new.png','sha','public.png',12,100,50,null,null,0,X'0102'
                            )
                            """
                        )
                        con.commit()
                        return subprocess.CompletedProcess(
                            [str(helper), "REM-1", str(image)],
                            0,
                            stdout=json.dumps(
                                {
                                    "ok": True,
                                    "backend": "reminderkit",
                                    "attachment_id": "7718459E-2672-4E99-9E6A-B9AA430E570F",
                                    "attachment_transport": transport,
                                    "image_uti": helper_uti,
                                }
                            )
                            + "\n",
                            stderr="",
                        )

                    with (
                        mock.patch.object(
                            reminders_adapter,
                            "reminderkit_attach_helper",
                            return_value=helper,
                        ),
                        mock.patch.object(
                            reminders_adapter.subprocess,
                            "run",
                            side_effect=add_attachment,
                        ),
                        self.assertRaises(
                            reminders_adapter.AttachmentVerificationError
                        ) as raised,
                    ):
                        reminders_adapter.attach_image_reminderkit_record(
                            con,
                            {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1"},
                            image,
                            validated_image=validated_image_fixture(image),
                        )
                finally:
                    con.close()

                self.assertEqual(raised.exception.code, "sync_pending")
                self.assertEqual(raised.exception.reason_code, reason_code)
                self.assertFalse(raised.exception.retryable)
                self.assertTrue(raised.exception.details["partial_failure"])
                if detail_key is not None:
                    self.assertEqual(
                        raised.exception.details[detail_key], detail_value
                    )
                self.assertIn(
                    "delete_attachment",
                    raised.exception.details["cleanup_command"],
                )

    def test_helper_attach_wraps_malformed_helper_json_as_adapter_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "reminders.sqlite"
            image = Path(tmp) / "image.png"
            helper = Path(tmp) / "remkit_attach_image"
            image.write_bytes(b"fake-png")
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                con.executescript(
                    """
                    create table ZREMCDOBJECT (
                        Z_PK integer primary key,
                        Z_ENT integer,
                        ZCKIDENTIFIER text,
                        ZCKCLOUDSTATE integer,
                        ZREMINDER2 integer,
                        Z_FOK_REMINDER1 integer,
                        ZFILENAME text,
                        ZSHA512SUM text,
                        ZUTI text,
                        ZFILESIZE integer,
                        ZWIDTH integer,
                        ZHEIGHT integer,
                        ZURL text,
                        ZHOSTURL text,
                        ZMARKEDFORDELETION integer,
                        ZCKSERVERRECORDDATA blob
                    );
                    create table ZREMCKCLOUDSTATE (
                        Z_PK integer primary key,
                        ZINCLOUD integer,
                        ZCURRENTLOCALVERSION integer,
                        ZLATESTVERSIONSYNCEDTOCLOUD integer
                    );
                    """
                )
                proc = subprocess.CompletedProcess(
                    [str(helper), "REM-1", str(image)],
                    0,
                    stdout="{not json",
                    stderr="",
                )

                with (
                    mock.patch.object(reminders_adapter, "reminderkit_attach_helper", return_value=helper),
                    mock.patch.object(reminders_adapter.subprocess, "run", return_value=proc),
                    self.assertRaises(reminders_adapter.AdapterError),
                ):
                    reminders_adapter.attach_image_reminderkit_record(
                        con,
                        {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1"},
                        image,
                        validated_image=validated_image_fixture(image),
                    )
            finally:
                con.close()

    def test_helper_attach_wraps_subprocess_timeout_as_adapter_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "reminders.sqlite"
            image = Path(tmp) / "image.png"
            helper = Path(tmp) / "remkit_attach_image"
            image.write_bytes(b"fake-png")
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                con.executescript(
                    """
                    create table ZREMCDOBJECT (
                        Z_PK integer primary key,
                        Z_ENT integer,
                        ZCKIDENTIFIER text,
                        ZCKCLOUDSTATE integer,
                        ZREMINDER2 integer,
                        Z_FOK_REMINDER1 integer,
                        ZFILENAME text,
                        ZSHA512SUM text,
                        ZUTI text,
                        ZFILESIZE integer,
                        ZWIDTH integer,
                        ZHEIGHT integer,
                        ZURL text,
                        ZHOSTURL text,
                        ZMARKEDFORDELETION integer,
                        ZCKSERVERRECORDDATA blob
                    );
                    create table ZREMCKCLOUDSTATE (
                        Z_PK integer primary key,
                        ZINCLOUD integer,
                        ZCURRENTLOCALVERSION integer,
                        ZLATESTVERSIONSYNCEDTOCLOUD integer
                    );
                    """
                )

                with (
                    mock.patch.object(reminders_adapter, "reminderkit_attach_helper", return_value=helper),
                    mock.patch.object(
                        reminders_adapter.subprocess,
                        "run",
                        side_effect=subprocess.TimeoutExpired(str(helper), 1),
                    ),
                    self.assertRaises(reminders_adapter.AdapterError) as raised,
                ):
                    reminders_adapter.attach_image_reminderkit_record(
                        con,
                        {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1"},
                        image,
                        validated_image=validated_image_fixture(image),
                    )
            finally:
                con.close()

        self.assertIn("timed out", str(raised.exception))
        self.assertEqual(raised.exception.code, "sync_pending")
        self.assertTrue(raised.exception.details["partial_failure"])
        self.assertTrue(raised.exception.details["mutation_outcome_unknown"])

    def test_attach_image_unknown_helper_outcome_returns_pending_full_receipt(self) -> None:
        reminder_id = "7718459E-2672-4E99-9E6A-B9AA430E570F"
        args = argparse.Namespace(
            db="/tmp/reminders.sqlite",
            id=reminder_id,
            title=None,
            list=None,
            image="/tmp/example.png",
            backend="reminderkit",
            if_version=3,
            idempotency_key=None,
        )
        con = mock.Mock()
        reminder = {
            "Z_PK": 1,
            "ZCKIDENTIFIER": reminder_id,
            "Z_OPT": 3,
            "ZCOMPLETED": 0,
            "ZFLAGGED": 0,
            "ZMARKEDFORDELETION": 0,
        }
        uncertain = reminders_adapter.AdapterError(
            "helper timed out",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
        )

        with (
            mock.patch.object(reminders_adapter, "resolve_database", return_value=Path("/tmp/reminders.sqlite")),
            mock.patch.object(reminders_adapter, "connect", return_value=con),
            mock.patch.object(reminders_adapter, "require_command_capability", return_value={"supported": True}),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(reminders_adapter, "attach_image_reminderkit_record", side_effect=uncertain),
            mock.patch.object(reminders_adapter, "reread_reminder", return_value=reminder),
            mock.patch.object(reminders_adapter, "log_action", return_value=None),
        ):
            receipt = reminders_adapter.attach_image_once(args)

        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["status"], "committed_verification_pending")
        self.assertEqual(receipt["operation"], "attach_image")
        self.assertEqual(receipt["target"]["reminder_id"], reminder_id)
        self.assertIsNone(receipt["target"]["attachment_id"])
        self.assertTrue(receipt["verification"]["mutation_outcome_unknown"])
        self.assertEqual(
            receipt["recovery"]["semantics"],
            "inspect_reminder_attachments_before_retry",
        )
        self.assertEqual(receipt["error"]["code"], "sync_pending")
        self.assertEqual(receipt["error"]["reason_code"], "sync_pending")
        self.assertTrue(receipt["error"]["retryable"])

    def test_attach_image_verification_errors_preserve_reason_and_retryability(
        self,
    ) -> None:
        reminder_id = "7718459E-2672-4E99-9E6A-B9AA430E570F"
        args = argparse.Namespace(
            db="/tmp/reminders.sqlite",
            id=reminder_id,
            title=None,
            list=None,
            image="/tmp/example.png",
            backend="reminderkit",
            if_version=3,
            idempotency_key=None,
        )
        con = mock.Mock()
        reminder = {
            "Z_PK": 1,
            "ZCKIDENTIFIER": reminder_id,
            "Z_OPT": 3,
            "ZCOMPLETED": 0,
            "ZFLAGGED": 0,
            "ZMARKEDFORDELETION": 0,
        }
        cases = (
            ("native_image_transport_mismatch", False),
            ("native_image_content_type_mismatch", False),
            ("mobile_visibility_pending", True),
        )
        for reason_code, retryable in cases:
            with self.subTest(reason_code=reason_code):
                pending = reminders_adapter.AttachmentVerificationError(
                    "Image attachment verification did not complete",
                    row={"Z_PK": 2},
                    attachment={"id": "ATTACHMENT-1"},
                    reason_code=reason_code,
                    retryable=retryable,
                    partial_failure=True,
                )

                with (
                    mock.patch.object(
                        reminders_adapter,
                        "resolve_database",
                        return_value=Path("/tmp/reminders.sqlite"),
                    ),
                    mock.patch.object(reminders_adapter, "connect", return_value=con),
                    mock.patch.object(
                        reminders_adapter,
                        "require_command_capability",
                        return_value={"supported": True},
                    ),
                    mock.patch.object(
                        reminders_adapter, "find_reminder", return_value=reminder
                    ),
                    mock.patch.object(
                        reminders_adapter,
                        "attach_image_reminderkit_record",
                        side_effect=pending,
                    ),
                    mock.patch.object(
                        reminders_adapter, "reread_reminder", return_value=reminder
                    ),
                    mock.patch.object(
                        reminders_adapter, "log_action", return_value=None
                    ),
                ):
                    receipt = reminders_adapter.attach_image_once(args)

                self.assertEqual(
                    receipt["status"], "committed_verification_pending"
                )
                self.assertEqual(receipt["error"]["code"], "sync_pending")
                self.assertEqual(receipt["error"]["reason_code"], reason_code)
                self.assertIs(receipt["error"]["retryable"], retryable)
                self.assertEqual(receipt["warnings"][0]["code"], reason_code)

    def test_image_size_wraps_sips_timeout_as_adapter_error(self) -> None:
        image = Path("/tmp/example.png")

        with (
            mock.patch.object(
                reminders_adapter.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("sips", 1),
            ),
            self.assertRaises(reminders_adapter.AdapterError) as raised,
        ):
            reminders_adapter.image_size(image)

        self.assertIn("timed out", str(raised.exception))
        self.assertEqual(raised.exception.details["image"], image.name)

    def test_replace_image_compensates_new_attachment_when_old_delete_fails(self) -> None:
        args = mock.Mock(
            db="/tmp/reminders.sqlite",
            image="/tmp/example.png",
            url=None,
            id="REM-1",
            title=None,
            list=None,
            type=None,
            attachment_id="ATTACH-OLD",
            attachment_pk=None,
            filename=None,
            old_url=None,
            if_version=1,
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1", "Z_OPT": 1}
        selected = {"Z_PK": 10, "ZCKIDENTIFIER": "ATTACH-OLD", "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT}
        new_result = {
            "attachment": {"pk": 11, "id": "ATTACH-NEW"},
            "_row": {"Z_PK": 11, "ZCKIDENTIFIER": "ATTACH-NEW"},
        }

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/reminders.sqlite"),
            ),
            mock.patch.object(reminders_adapter, "connect", return_value=con),
            mock.patch.object(
                reminders_adapter,
                "require_command_capability",
                return_value={"supported": True},
            ),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(reminders_adapter, "resolve_attachment_selection", return_value=(selected, [], None)),
            mock.patch.object(reminders_adapter, "attachment_payload", return_value={"pk": 10, "id": "ATTACH-OLD"}),
            mock.patch.object(reminders_adapter, "attach_image_reminderkit_record", return_value=new_result),
            mock.patch.object(
                reminders_adapter,
                "remove_image_reminderkit_record",
                side_effect=reminders_adapter.AdapterError("delete failed"),
            ),
            mock.patch.object(reminders_adapter, "compensate_new_attachment", return_value={"pk": 11}) as compensate,
            mock.patch.object(reminders_adapter, "json_out") as json_out,
        ):
            result = reminders_adapter.cmd_replace_attachment(args)

        compensate.assert_called_once_with(
            Path("/tmp/reminders.sqlite"),
            reminder,
            new_result,
        )
        receipt = json_out.call_args.args[0]
        self.assertEqual(result, 1)
        self.assertEqual(receipt["status"], "failed_no_mutation")
        self.assertEqual(receipt["verification"]["state"], "compensated")
        self.assertTrue(receipt["verification"]["compensation_succeeded"])

    def test_replace_image_same_pk_result_is_unknown_and_never_compensated(self) -> None:
        args = mock.Mock(
            db="/tmp/reminders.sqlite",
            image="/tmp/example.png",
            url=None,
            id="REM-1",
            title=None,
            list=None,
            type=None,
            attachment_id="ATTACH-OLD",
            attachment_pk=None,
            filename=None,
            old_url=None,
            if_version=1,
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1", "Z_OPT": 1}
        selected = {
            "Z_PK": 10,
            "ZCKIDENTIFIER": "ATTACH-OLD",
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
        }
        same_row_result = {
            "attachment": {"pk": 10, "id": "ATTACH-OLD"},
            "_row": selected,
            "sync": {"mobile_visible_likely": True},
        }

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/reminders.sqlite"),
            ),
            mock.patch.object(reminders_adapter, "connect", return_value=con),
            mock.patch.object(
                reminders_adapter,
                "require_command_capability",
                return_value={"supported": True},
            ),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(
                reminders_adapter,
                "resolve_attachment_selection",
                return_value=(selected, [], None),
            ),
            mock.patch.object(
                reminders_adapter,
                "attachment_payload",
                return_value={"pk": 10, "id": "ATTACH-OLD"},
            ),
            mock.patch.object(
                reminders_adapter,
                "attach_image_reminderkit_record",
                return_value=same_row_result,
            ),
            mock.patch.object(reminders_adapter, "remove_image_reminderkit_record") as remove,
            mock.patch.object(reminders_adapter, "compensate_new_attachment") as compensate,
            mock.patch.object(reminders_adapter, "json_out") as json_out,
        ):
            result = reminders_adapter.cmd_replace_attachment(args)

        remove.assert_not_called()
        compensate.assert_not_called()
        receipt = json_out.call_args.args[0]
        self.assertEqual(result, 0)
        self.assertEqual(receipt["status"], "committed_verification_pending")
        self.assertTrue(receipt["verification"]["mutation_outcome_unknown"])
        self.assertFalse(receipt["recovery"]["automatic_retry_safe"])

    def test_replace_image_unknown_old_removal_does_not_compensate(self) -> None:
        args = mock.Mock(
            db="/tmp/reminders.sqlite",
            image="/tmp/example.png",
            url=None,
            id="REM-1",
            title=None,
            list=None,
            type=None,
            attachment_id="ATTACH-OLD",
            attachment_pk=None,
            filename=None,
            old_url=None,
            if_version=1,
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1", "Z_OPT": 1}
        selected = {
            "Z_PK": 10,
            "ZCKIDENTIFIER": "ATTACH-OLD",
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
        }
        new_result = {
            "attachment": {"pk": 11, "id": "ATTACH-NEW"},
            "_row": {"Z_PK": 11, "ZCKIDENTIFIER": "ATTACH-NEW"},
        }
        uncertain = reminders_adapter.AdapterError(
            "native removal result lost",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
        )

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/reminders.sqlite"),
            ),
            mock.patch.object(reminders_adapter, "connect", return_value=con),
            mock.patch.object(
                reminders_adapter,
                "require_command_capability",
                return_value={"supported": True},
            ),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(
                reminders_adapter,
                "resolve_attachment_selection",
                return_value=(selected, [], None),
            ),
            mock.patch.object(
                reminders_adapter,
                "attachment_payload",
                return_value={"pk": 10, "id": "ATTACH-OLD"},
            ),
            mock.patch.object(
                reminders_adapter,
                "attach_image_reminderkit_record",
                return_value=new_result,
            ),
            mock.patch.object(
                reminders_adapter,
                "remove_image_reminderkit_record",
                side_effect=uncertain,
            ),
            mock.patch.object(reminders_adapter, "compensate_new_attachment") as compensate,
            mock.patch.object(reminders_adapter, "json_out") as json_out,
        ):
            result = reminders_adapter.cmd_replace_attachment(args)

        compensate.assert_not_called()
        receipt = json_out.call_args.args[0]
        self.assertEqual(result, 0)
        self.assertEqual(receipt["status"], "committed_verification_pending")
        self.assertTrue(receipt["verification"]["mutation_outcome_unknown"])
        self.assertFalse(receipt["recovery"]["automatic_retry_safe"])

    def test_replace_image_reconnect_failure_does_not_compensate_verified_removal(self) -> None:
        args = mock.Mock(
            db="/tmp/reminders.sqlite",
            image="/tmp/example.png",
            url=None,
            id="REM-1",
            title=None,
            list=None,
            type=None,
            attachment_id="ATTACH-OLD",
            attachment_pk=None,
            filename=None,
            old_url=None,
            if_version=1,
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1", "Z_OPT": 1}
        selected = {
            "Z_PK": 10,
            "ZCKIDENTIFIER": "ATTACH-OLD",
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
        }
        new_result = {
            "attachment": {"pk": 11, "id": "ATTACH-NEW"},
            "_row": {"Z_PK": 11, "ZCKIDENTIFIER": "ATTACH-NEW"},
        }

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/reminders.sqlite"),
            ),
            mock.patch.object(
                reminders_adapter,
                "connect",
                side_effect=[con, sqlite3.OperationalError("reopen failed")],
            ),
            mock.patch.object(
                reminders_adapter,
                "require_command_capability",
                return_value={"supported": True},
            ),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(
                reminders_adapter,
                "resolve_attachment_selection",
                return_value=(selected, [], None),
            ),
            mock.patch.object(
                reminders_adapter,
                "attachment_payload",
                return_value={"pk": 10, "id": "ATTACH-OLD"},
            ),
            mock.patch.object(
                reminders_adapter,
                "attach_image_reminderkit_record",
                return_value=new_result,
            ),
            mock.patch.object(
                reminders_adapter,
                "remove_image_reminderkit_record",
                return_value={
                    "id": "ATTACH-OLD",
                    "detached_from_reminder": True,
                    "native_reminderkit": True,
                },
            ),
            mock.patch.object(
                reminders_adapter,
                "attachment_replacement_readback",
                return_value={
                    "old_attachment_removed": True,
                    "old_attachment_detached_from_reminder": True,
                    "new_attachment_active": True,
                    "replacement_order_preserved": True,
                },
            ),
            mock.patch.object(reminders_adapter, "compensate_new_attachment") as compensate,
            mock.patch.object(reminders_adapter, "json_out") as json_out,
        ):
            result = reminders_adapter.cmd_replace_attachment(args)

        compensate.assert_not_called()
        receipt = json_out.call_args.args[0]
        self.assertEqual(result, 0)
        self.assertEqual(receipt["status"], "committed_verification_pending")
        self.assertTrue(receipt["verification"]["native_removal_verified"])

    def test_replace_image_compensates_unverified_new_attachment(self) -> None:
        args = mock.Mock(
            db="/tmp/reminders.sqlite",
            image="/tmp/example.png",
            url=None,
            id="REM-1",
            title=None,
            list=None,
            type=None,
            attachment_id="ATTACH-OLD",
            attachment_pk=None,
            filename=None,
            old_url=None,
            if_version=1,
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1", "Z_OPT": 1}
        selected = {
            "Z_PK": 10,
            "ZCKIDENTIFIER": "ATTACH-OLD",
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
        }
        new_row = {"Z_PK": 11, "ZCKIDENTIFIER": "ATTACH-NEW"}
        new_attachment = {"pk": 11, "id": "ATTACH-NEW"}
        verification_error = reminders_adapter.AttachmentVerificationError(
            "mobile visibility not verified",
            row=new_row,
            partial_failure=True,
            attachment=new_attachment,
        )
        self.assertEqual(verification_error.code, "sync_pending")
        expected_result = {"attachment": new_attachment, "_row": new_row}

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/reminders.sqlite"),
            ),
            mock.patch.object(reminders_adapter, "connect", return_value=con),
            mock.patch.object(
                reminders_adapter,
                "require_command_capability",
                return_value={"supported": True},
            ),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(
                reminders_adapter,
                "resolve_attachment_selection",
                return_value=(selected, [], None),
            ),
            mock.patch.object(
                reminders_adapter,
                "attachment_payload",
                return_value={"pk": 10, "id": "ATTACH-OLD"},
            ),
            mock.patch.object(
                reminders_adapter,
                "attach_image_reminderkit_record",
                side_effect=verification_error,
            ),
            mock.patch.object(
                reminders_adapter,
                "compensate_new_attachment",
                return_value={"pk": 11},
            ) as compensate,
            mock.patch.object(reminders_adapter, "json_out") as json_out,
        ):
            result = reminders_adapter.cmd_replace_attachment(args)

        compensate.assert_called_once_with(
            Path("/tmp/reminders.sqlite"),
            reminder,
            expected_result,
        )
        receipt = json_out.call_args.args[0]
        self.assertEqual(result, 1)
        self.assertEqual(receipt["status"], "failed_no_mutation")
        self.assertEqual(receipt["error"]["code"], "sync_pending")

    def test_replace_image_compensation_failure_returns_full_manual_repair_receipt(self) -> None:
        args = mock.Mock(
            db="/tmp/reminders.sqlite",
            image="/tmp/example.png",
            url=None,
            id="REM-1",
            title=None,
            list=None,
            type=None,
            attachment_id="ATTACH-OLD",
            attachment_pk=None,
            filename=None,
            old_url=None,
            if_version=1,
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1", "Z_OPT": 1}
        selected = {
            "Z_PK": 10,
            "ZCKIDENTIFIER": "ATTACH-OLD",
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
        }
        new_result = {
            "attachment": {"pk": 11, "id": "ATTACH-NEW"},
            "_row": {"Z_PK": 11, "ZCKIDENTIFIER": "ATTACH-NEW"},
        }

        with (
            mock.patch.object(reminders_adapter, "resolve_database", return_value=Path("/tmp/reminders.sqlite")),
            mock.patch.object(reminders_adapter, "connect", return_value=con),
            mock.patch.object(reminders_adapter, "require_command_capability", return_value={"supported": True}),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(reminders_adapter, "resolve_attachment_selection", return_value=(selected, [], None)),
            mock.patch.object(reminders_adapter, "attachment_payload", return_value={"pk": 10, "id": "ATTACH-OLD"}),
            mock.patch.object(reminders_adapter, "attach_image_reminderkit_record", return_value=new_result),
            mock.patch.object(
                reminders_adapter,
                "remove_image_reminderkit_record",
                side_effect=reminders_adapter.AdapterError("delete failed"),
            ),
            mock.patch.object(reminders_adapter, "compensate_new_attachment", side_effect=reminders_adapter.AdapterError("cleanup failed")),
            mock.patch.object(reminders_adapter, "json_out") as json_out,
        ):
            result = reminders_adapter.cmd_replace_attachment(args)

        receipt = json_out.call_args.args[0]
        self.assertEqual(result, 1)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["status"], "failed_manual_repair_required")
        self.assertEqual(receipt["operation"], "replace_attachment")
        self.assertEqual(receipt["target"]["new_attachment_id"], "ATTACH-NEW")
        self.assertEqual(receipt["verification"]["state"], "manual_repair_required")
        self.assertEqual(receipt["recovery"]["semantics"], "delete_new_attachment_manually")
        self.assertEqual(receipt["error"]["code"], "sync_pending")

    def test_replace_postcommit_inconclusive_returns_full_manual_repair_receipt(self) -> None:
        args = mock.Mock(
            db="/tmp/reminders.sqlite",
            image="/tmp/example.png",
            url=None,
            id="REM-1",
            title=None,
            list=None,
            type=None,
            attachment_id="ATTACH-OLD",
            attachment_pk=None,
            filename=None,
            old_url=None,
            if_version=1,
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1", "Z_OPT": 1}
        selected = {
            "Z_PK": 10,
            "ZCKIDENTIFIER": "ATTACH-OLD",
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
        }
        new_result = {
            "attachment": {"pk": 11, "id": "ATTACH-NEW"},
            "_row": {"Z_PK": 11, "ZCKIDENTIFIER": "ATTACH-NEW"},
            "sync": {"mobile_visible_likely": True},
        }

        with (
            mock.patch.object(reminders_adapter, "resolve_database", return_value=Path("/tmp/reminders.sqlite")),
            mock.patch.object(reminders_adapter, "connect", return_value=con),
            mock.patch.object(reminders_adapter, "require_command_capability", return_value={"supported": True}),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(reminders_adapter, "resolve_attachment_selection", return_value=(selected, [], None)),
            mock.patch.object(reminders_adapter, "attachment_payload", return_value={"pk": 10, "id": "ATTACH-OLD"}),
            mock.patch.object(reminders_adapter, "attach_image_reminderkit_record", return_value=new_result),
            mock.patch.object(
                reminders_adapter,
                "remove_image_reminderkit_record",
                return_value={
                    "id": "ATTACH-OLD",
                    "row_deleted": True,
                    "cloud_state_tombstone_retained": True,
                    "native_reminderkit": True,
                },
            ),
            mock.patch.object(
                reminders_adapter,
                "attachment_replacement_readback",
                return_value={"old_attachment_removed": True, "new_attachment_active": False},
            ),
            mock.patch.object(reminders_adapter, "json_out") as json_out,
        ):
            result = reminders_adapter.cmd_replace_attachment(args)

        receipt = json_out.call_args.args[0]
        self.assertEqual(result, 1)
        self.assertEqual(receipt["status"], "failed_manual_repair_required")
        self.assertEqual(receipt["target"]["old_attachment_id"], "ATTACH-OLD")
        self.assertEqual(receipt["target"]["new_attachment_id"], "ATTACH-NEW")
        self.assertTrue(receipt["verification"]["replacement_committed"])
        self.assertEqual(receipt["recovery"]["semantics"], "inspect_both_attachments_and_restore_manually")

    def test_replace_postcommit_readback_exception_returns_pending(self) -> None:
        args = mock.Mock(
            db="/tmp/reminders.sqlite",
            image="/tmp/example.png",
            url=None,
            id="REM-1",
            title=None,
            list=None,
            type=None,
            attachment_id="ATTACH-OLD",
            attachment_pk=None,
            filename=None,
            old_url=None,
            if_version=1,
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1", "Z_OPT": 1}
        selected = {
            "Z_PK": 10,
            "ZCKIDENTIFIER": "ATTACH-OLD",
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
        }
        new_result = {
            "attachment": {"pk": 11, "id": "ATTACH-NEW"},
            "_row": {"Z_PK": 11, "ZCKIDENTIFIER": "ATTACH-NEW"},
            "sync": {"mobile_visible_likely": True},
        }

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/reminders.sqlite"),
            ),
            mock.patch.object(reminders_adapter, "connect", return_value=con),
            mock.patch.object(
                reminders_adapter,
                "require_command_capability",
                return_value={"supported": True},
            ),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(
                reminders_adapter,
                "resolve_attachment_selection",
                return_value=(selected, [], None),
            ),
            mock.patch.object(
                reminders_adapter,
                "attachment_payload",
                return_value={"pk": 10, "id": "ATTACH-OLD"},
            ),
            mock.patch.object(
                reminders_adapter,
                "attach_image_reminderkit_record",
                return_value=new_result,
            ),
            mock.patch.object(
                reminders_adapter,
                "remove_image_reminderkit_record",
                return_value={
                    "id": "ATTACH-OLD",
                    "row_deleted": False,
                    "detached_from_reminder": True,
                    "cloud_state_tombstone_retained": True,
                    "native_reminderkit": True,
                },
            ),
            mock.patch.object(
                reminders_adapter,
                "attachment_replacement_readback",
                side_effect=reminders_adapter.AdapterError(
                    "post-commit read failed",
                    code="schema_mismatch",
                ),
            ),
            mock.patch.object(reminders_adapter, "compensate_new_attachment") as compensate,
            mock.patch.object(reminders_adapter, "json_out") as json_out,
        ):
            result = reminders_adapter.cmd_replace_attachment(args)

        compensate.assert_not_called()
        receipt = json_out.call_args.args[0]
        self.assertEqual(result, 0)
        self.assertEqual(receipt["status"], "committed_verification_pending")
        self.assertTrue(receipt["verification"]["replacement_committed"])
        self.assertTrue(receipt["verification"]["native_removal_verified"])
        self.assertFalse(receipt["recovery"]["automatic_retry_safe"])

    def test_replace_native_commit_followup_failure_never_compensates(self) -> None:
        args = mock.Mock(
            db="/tmp/reminders.sqlite",
            image="/tmp/example.png",
            url=None,
            id="REM-1",
            title=None,
            list=None,
            type=None,
            attachment_id="ATTACH-OLD",
            attachment_pk=None,
            filename=None,
            old_url=None,
            if_version=1,
        )
        initial = mock.Mock()
        reopened = mock.Mock()
        reopened.commit.side_effect = sqlite3.OperationalError("commit failed")
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1", "Z_OPT": 1}
        selected = {
            "Z_PK": 10,
            "ZCKIDENTIFIER": "ATTACH-OLD",
            "Z_ENT": reminders_adapter.IMAGE_ATTACHMENT_ENT,
        }
        new_result = {
            "attachment": {"pk": 11, "id": "ATTACH-NEW"},
            "_row": {"Z_PK": 11, "ZCKIDENTIFIER": "ATTACH-NEW"},
            "sync": {"mobile_visible_likely": True},
        }

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/reminders.sqlite"),
            ),
            mock.patch.object(
                reminders_adapter,
                "connect",
                side_effect=[initial, reopened],
            ),
            mock.patch.object(
                reminders_adapter,
                "require_command_capability",
                return_value={"supported": True},
            ),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(
                reminders_adapter,
                "resolve_attachment_selection",
                return_value=(selected, [], None),
            ),
            mock.patch.object(
                reminders_adapter,
                "attachment_payload",
                return_value={"pk": 10, "id": "ATTACH-OLD"},
            ),
            mock.patch.object(
                reminders_adapter,
                "attach_image_reminderkit_record",
                return_value=new_result,
            ),
            mock.patch.object(
                reminders_adapter,
                "remove_image_reminderkit_record",
                return_value={
                    "id": "ATTACH-OLD",
                    "row_deleted": False,
                    "detached_from_reminder": True,
                    "cloud_state_tombstone_retained": True,
                    "native_reminderkit": True,
                },
            ),
            mock.patch.object(
                reminders_adapter,
                "attachment_replacement_readback",
                return_value={
                    "old_attachment_removed": True,
                    "old_attachment_detached_from_reminder": True,
                    "new_attachment_active": True,
                    "replacement_order_preserved": True,
                },
            ),
            mock.patch.object(reminders_adapter, "compensate_new_attachment") as compensate,
            mock.patch.object(reminders_adapter, "json_out") as json_out,
        ):
            result = reminders_adapter.cmd_replace_attachment(args)

        compensate.assert_not_called()
        receipt = json_out.call_args.args[0]
        self.assertEqual(result, 0)
        self.assertEqual(receipt["status"], "verified")
        reopened.commit.assert_not_called()

    def test_repair_compensates_unverified_new_attachment(self) -> None:
        args = argparse.Namespace(
            db="/tmp/reminders.sqlite",
            search=None,
            list=None,
            limit=1,
            apply=True,
            no_backup=True,
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "Z_OPT": 1, "ZCKIDENTIFIER": "REM-1"}
        old_row = {"Z_PK": 10, "ZCKIDENTIFIER": "ATTACH-OLD"}
        item = {
            "reminder": {"id": "REM-1", "title": "Task", "version": 1},
            "attachment": {"pk": 10, "id": "ATTACH-OLD"},
            "problem": "image_attachment_local_only",
            "_row": old_row,
        }
        source = Path("/tmp/source.png")
        args.preview_digest = reminders_adapter.repair_candidate_digest(
            [
                {
                    "reminder": item["reminder"],
                    "attachment": item["attachment"],
                    "problem": item["problem"],
                    "source_files": [source.name],
                    "repairable": True,
                }
            ]
        )
        new_row = {"Z_PK": 11, "ZCKIDENTIFIER": "ATTACH-NEW"}
        new_attachment = {"pk": 11, "id": "ATTACH-NEW"}
        verification_error = reminders_adapter.AttachmentVerificationError(
            "mobile visibility not verified",
            row=new_row,
            partial_failure=True,
            attachment=new_attachment,
        )
        expected_result = {"attachment": new_attachment, "_row": new_row}

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/reminders.sqlite"),
            ),
            mock.patch.object(reminders_adapter, "connect", return_value=con),
            mock.patch.object(
                reminders_adapter,
                "attachment_sync_capabilities",
                return_value={"available": True, "missing_columns": []},
            ),
            mock.patch.object(
                reminders_adapter,
                "image_attachment_audit_counts",
                return_value={"total": 1, "problems": 1},
            ),
            mock.patch.object(
                reminders_adapter,
                "image_attachment_audit_items",
                return_value=([item], 1),
            ),
            mock.patch.object(
                reminders_adapter,
                "source_paths_for_attachment",
                return_value=[source],
            ),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(
                reminders_adapter,
                "attach_image_reminderkit_record",
                side_effect=verification_error,
            ),
            mock.patch.object(
                reminders_adapter,
                "compensate_new_attachment",
                return_value={"pk": 11},
            ) as compensate,
            mock.patch.object(reminders_adapter, "json_out"),
        ):
            result = reminders_adapter.cmd_repair_attachments(args)

        self.assertEqual(result, 1)
        compensate.assert_called_once_with(
            Path("/tmp/reminders.sqlite"),
            reminder,
            expected_result,
        )

    def test_repair_unknown_helper_outcome_requires_manual_read_and_blocks_same_reminder(self) -> None:
        args = argparse.Namespace(
            db="/tmp/reminders.sqlite",
            search=None,
            list=None,
            limit=3,
            apply=True,
            no_backup=True,
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "Z_OPT": 1, "ZCKIDENTIFIER": "REM-1"}
        source = Path("/tmp/source.png")
        items = [
            {
                "reminder": {"id": "REM-1", "title": "Task", "version": 1},
                "attachment": {"pk": pk, "id": attachment_id},
                "problem": "image_attachment_local_only",
                "_row": {"Z_PK": pk, "ZCKIDENTIFIER": attachment_id},
            }
            for pk, attachment_id in (
                (10, "ATTACH-OLD-1"),
                (11, "ATTACH-OLD-2"),
                (12, "ATTACH-OLD-3"),
            )
        ]
        candidates = [
            {
                "reminder": item["reminder"],
                "attachment": item["attachment"],
                "problem": item["problem"],
                "source_files": [source.name],
                "repairable": True,
            }
            for item in items
        ]
        args.preview_digest = reminders_adapter.repair_candidate_digest(candidates)
        uncertain = reminders_adapter.AdapterError(
            "helper timed out",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
        )
        successful = {
            "attachment": {"pk": 20, "id": "ATTACH-NEW-1"},
            "sync": {"mobile_visible_likely": True},
        }

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/reminders.sqlite"),
            ),
            mock.patch.object(reminders_adapter, "connect", return_value=con),
            mock.patch.object(
                reminders_adapter,
                "attachment_sync_capabilities",
                return_value={"available": True, "missing_columns": []},
            ),
            mock.patch.object(
                reminders_adapter,
                "image_attachment_audit_counts",
                return_value={"total": 3, "problems": 3},
            ),
            mock.patch.object(
                reminders_adapter,
                "image_attachment_audit_items",
                return_value=(items, 3),
            ),
            mock.patch.object(
                reminders_adapter,
                "source_paths_for_attachment",
                return_value=[source],
            ),
            mock.patch.object(
                reminders_adapter,
                "find_reminder",
                side_effect=(reminder, {**reminder, "Z_OPT": 2}),
            ),
            mock.patch.object(
                reminders_adapter,
                "attach_image_reminderkit_record",
                side_effect=(successful, uncertain),
            ) as attach,
            mock.patch.object(
                reminders_adapter,
                "remove_image_reminderkit_record",
                return_value={
                    "id": "ATTACH-OLD-1",
                    "row_deleted": True,
                    "cloud_state_tombstone_retained": True,
                    "native_reminderkit": True,
                },
            ),
            mock.patch.object(
                reminders_adapter,
                "reread_reminder",
                return_value={**reminder, "Z_OPT": 2},
            ),
            mock.patch.object(reminders_adapter, "log_action", return_value=None),
            mock.patch.object(reminders_adapter, "json_out") as output,
        ):
            exit_code = reminders_adapter.cmd_repair_attachments(args)

        receipt = output.call_args.args[0]
        self.assertEqual(exit_code, 1)
        self.assertEqual(receipt["status"], "failed_manual_repair_required")
        self.assertTrue(receipt["verification"]["manual_repair_required"])
        self.assertEqual(
            receipt["recovery"]["semantics"],
            "read_before_retry_and_inspect_attachments",
        )
        self.assertEqual(receipt["after"]["counts"]["repaired"], 1)
        self.assertTrue(receipt["after"]["failed"][0]["mutation_outcome_unknown"])
        self.assertTrue(receipt["after"]["failed"][1]["skipped_after_unknown_mutation"])
        self.assertEqual(attach.call_count, 2)

    def test_repair_unknown_old_removal_does_not_compensate_new_image(self) -> None:
        args = argparse.Namespace(
            db="/tmp/reminders.sqlite",
            search=None,
            list=None,
            limit=2,
            apply=True,
            no_backup=True,
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "Z_OPT": 1, "ZCKIDENTIFIER": "REM-1"}
        source = Path("/tmp/source.png")
        items = [
            {
                "reminder": {"id": reminder_id, "title": "Task", "version": 1},
                "attachment": {"pk": pk, "id": attachment_id},
                "problem": "image_attachment_local_only",
                "_row": {"Z_PK": pk, "ZCKIDENTIFIER": attachment_id},
            }
            for reminder_id, pk, attachment_id in (
                ("REM-1", 10, "ATTACH-OLD"),
                ("REM-2", 11, "ATTACH-OTHER"),
            )
        ]
        candidates = [
            {
                "reminder": candidate["reminder"],
                "attachment": candidate["attachment"],
                "problem": candidate["problem"],
                "source_files": [source.name],
                "repairable": True,
            }
            for candidate in items
        ]
        args.preview_digest = reminders_adapter.repair_candidate_digest(candidates)
        new_result = {
            "attachment": {"pk": 20, "id": "ATTACH-NEW"},
            "_row": {"Z_PK": 20, "ZCKIDENTIFIER": "ATTACH-NEW"},
            "sync": {"mobile_visible_likely": True},
        }
        uncertain = reminders_adapter.AdapterError(
            "native removal result lost",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
        )

        with (
            mock.patch.object(
                reminders_adapter,
                "resolve_database",
                return_value=Path("/tmp/reminders.sqlite"),
            ),
            mock.patch.object(reminders_adapter, "connect", return_value=con),
            mock.patch.object(
                reminders_adapter,
                "attachment_sync_capabilities",
                return_value={"available": True, "missing_columns": []},
            ),
            mock.patch.object(
                reminders_adapter,
                "image_attachment_audit_counts",
                return_value={"total": 2, "problems": 2},
            ),
            mock.patch.object(
                reminders_adapter,
                "image_attachment_audit_items",
                return_value=(items, 2),
            ),
            mock.patch.object(
                reminders_adapter,
                "source_paths_for_attachment",
                return_value=[source],
            ),
            mock.patch.object(reminders_adapter, "find_reminder", return_value=reminder),
            mock.patch.object(
                reminders_adapter,
                "attach_image_reminderkit_record",
                return_value=new_result,
            ) as attach,
            mock.patch.object(
                reminders_adapter,
                "remove_image_reminderkit_record",
                side_effect=uncertain,
            ),
            mock.patch.object(reminders_adapter, "compensate_new_attachment") as compensate,
            mock.patch.object(reminders_adapter, "json_out") as output,
        ):
            result = reminders_adapter.cmd_repair_attachments(args)

        compensate.assert_not_called()
        receipt = output.call_args.args[0]
        self.assertEqual(result, 1)
        self.assertEqual(receipt["status"], "failed_manual_repair_required")
        self.assertTrue(receipt["after"]["failed"][0]["mutation_outcome_unknown"])
        self.assertTrue(receipt["after"]["failed"][1]["skipped_after_unknown_mutation"])
        self.assertEqual(attach.call_count, 1)


class CacheHelperTests(unittest.TestCase):
    def test_build_cache_payload_uses_only_lightweight_reminder_fields(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        try:
            con.executescript(
                """
                create table ZREMCDBASELIST (
                    Z_PK integer primary key,
                    ZCKIDENTIFIER text,
                    ZNAME text,
                    ZISGROUP integer,
                    ZPARENTLIST integer,
                    ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA text,
                    ZMARKEDFORDELETION integer
                );
                create table ZREMCDBASESECTION (
                    Z_PK integer primary key,
                    ZCKIDENTIFIER text,
                    ZDISPLAYNAME text,
                    ZLIST integer,
                    Z_FOK_LIST integer,
                    ZMARKEDFORDELETION integer
                );
                create table ZREMCDREMINDER (
                    Z_PK integer primary key,
                    ZCKIDENTIFIER text,
                    ZTITLE text,
                    ZNOTES text,
                    ZLIST integer,
                    ZCOMPLETED integer,
                    ZFLAGGED integer,
                    ZPRIORITY integer,
                    ZCREATIONDATE real,
                    ZLASTMODIFIEDDATE real,
                    ZDUEDATE real,
                    ZDISPLAYDATEDATE real,
                    ZCOMPLETIONDATE real,
                    ZALLDAY integer,
                    ZDISPLAYDATEISALLDAY integer,
                    ZTIMEZONE text,
                    ZMARKEDFORDELETION integer,
                    Z_FOK_LIST integer
                );
                create table ZREMCDOBJECT (
                    ZREMINDER2 integer,
                    ZREMINDER3 integer,
                    ZHASHTAGLABEL integer,
                    Z_ENT integer,
                    ZMARKEDFORDELETION integer
                );
                create table ZREMCDHASHTAGLABEL (
                    Z_PK integer primary key,
                    Z_ENT integer,
                    Z_OPT integer,
                    ZFIRSTOCCURRENCECREATIONDATE real,
                    ZRECENCYDATE real,
                    ZACCOUNTIDENTIFIER text,
                    ZCANONICALNAME text,
                    ZNAME text,
                    ZUUIDFORCHANGETRACKING blob
                );
                """
            )
            con.execute(
                """
                insert into ZREMCDBASELIST (
                    Z_PK,ZCKIDENTIFIER,ZNAME,ZISGROUP,ZPARENTLIST,
                    ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA,ZMARKEDFORDELETION
                ) values (1,'LIST-1','Inbox',0,null,?,0)
                """,
                (reminders_adapter.membership_payload({"REM-1": "SEC-1"}),),
            )
            con.execute(
                """
                insert into ZREMCDBASESECTION (
                    Z_PK,ZCKIDENTIFIER,ZDISPLAYNAME,ZLIST,Z_FOK_LIST,ZMARKEDFORDELETION
                ) values (1,'SEC-1','This Week',1,1024,0)
                """
            )
            con.execute(
                """
                insert into ZREMCDREMINDER (
                    Z_PK,ZCKIDENTIFIER,ZTITLE,ZNOTES,ZLIST,ZCOMPLETED,ZFLAGGED,
                    ZPRIORITY,ZCREATIONDATE,ZLASTMODIFIEDDATE,ZDUEDATE,
                    ZDISPLAYDATEDATE,ZCOMPLETIONDATE,ZALLDAY,ZDISPLAYDATEISALLDAY,
                    ZTIMEZONE,ZMARKEDFORDELETION,Z_FOK_LIST
                ) values (1,'REM-1','Pay bill','Sensitive body text',1,0,1,9,1,2,3,4,null,0,0,'Asia/Seoul',0,1024)
                """
            )
            con.execute(
                "insert into ZREMCDOBJECT (ZREMINDER2,Z_ENT,ZMARKEDFORDELETION) values (1,25,0)"
            )
            con.execute(
                "insert into ZREMCDOBJECT (ZREMINDER2,Z_ENT,ZMARKEDFORDELETION) values (1,26,0)"
            )
            con.execute(
                """
                insert into ZREMCDHASHTAGLABEL (
                    Z_PK,Z_ENT,Z_OPT,ZFIRSTOCCURRENCECREATIONDATE,ZRECENCYDATE,
                    ZACCOUNTIDENTIFIER,ZCANONICALNAME,ZNAME,ZUUIDFORCHANGETRACKING
                ) values (1,11,1,1,2,'ACCOUNT-1','work','Work',?)
                """,
                (b"\x00" * 16,),
            )
            con.execute(
                "insert into ZREMCDOBJECT (ZREMINDER3,ZHASHTAGLABEL,Z_ENT,ZMARKEDFORDELETION) values (1,1,32,0)"
            )

            payload = reminders_adapter.build_cache_payload(
                con,
                Path("/tmp/synthetic-reminders.sqlite"),
            )
        finally:
            con.close()

        reminder = payload["reminders"][0]
        self.assertEqual(payload["counts"]["reminders"], 1)
        self.assertEqual(payload["counts"]["image_attachments"], 1)
        self.assertEqual(payload["counts"]["url_attachments"], 1)
        self.assertEqual(payload["counts"]["attachments"], 2)
        self.assertEqual(payload["counts"]["tag_labels"], 1)
        self.assertEqual(payload["counts"]["tag_assignments"], 1)
        self.assertEqual(reminder["section"], "This Week")
        self.assertEqual(reminder["image_attachment_count"], 1)
        self.assertEqual(reminder["url_attachment_count"], 1)
        self.assertEqual(reminder["attachment_count"], 2)
        self.assertEqual(reminder["tag_names"], ["Work"])
        self.assertEqual(reminder["tag_count"], 1)
        self.assertFalse(reminder["all_day"])
        self.assertEqual(reminder["timezone"], "Asia/Seoul")
        self.assertEqual(reminder["notes_length"], len("Sensitive body text"))
        self.assertIn("notes_sha256", reminder)
        self.assertNotIn("notes", reminder)

    def test_filter_cached_reminders_searches_lightweight_fields(self) -> None:
        payload = {
            "version": reminders_adapter.CACHE_VERSION,
            "reminders": [
                {
                    "id": "AAA",
                    "title": "Buy oat milk",
                    "list": "Groceries",
                    "section": "This Week",
                    "completed": False,
                    "flagged": True,
                    "priority": 5,
                },
                {
                    "id": "BBB",
                    "title": "Book flights",
                    "list": "Travel",
                    "section": None,
                    "completed": True,
                    "flagged": False,
                    "priority": 0,
                },
            ],
        }

        matches, total = reminders_adapter.filter_cached_reminders(
            payload,
            query="milk",
            list_name="groceries",
            flagged=True,
            limit=10,
        )

        self.assertEqual(total, 1)
        self.assertEqual(matches[0]["id"], "AAA")

    def test_filter_cached_reminders_excludes_completed_by_default(self) -> None:
        payload = {
            "version": reminders_adapter.CACHE_VERSION,
            "reminders": [
                {"id": "AAA", "title": "Open", "completed": False},
                {"id": "BBB", "title": "Done", "completed": True},
            ],
        }

        active, active_total = reminders_adapter.filter_cached_reminders(payload, limit=10)
        all_matches, all_total = reminders_adapter.filter_cached_reminders(
            payload,
            include_completed=True,
            limit=10,
        )

        self.assertEqual(active_total, 1)
        self.assertEqual(active[0]["id"], "AAA")
        self.assertEqual(all_total, 2)
        self.assertEqual([item["id"] for item in all_matches], ["AAA", "BBB"])

    def test_cache_file_helpers_write_load_and_report_info(self) -> None:
        payload = {
            "version": reminders_adapter.CACHE_VERSION,
            "generated_at": "2026-07-02T00:00:00+09:00",
            "source": {"db": "/tmp/nonexistent-reminders.sqlite"},
            "counts": {"lists": 1, "sections": 0, "reminders": 1, "image_attachments": 0},
            "reminders": [{"id": "AAA", "title": "Task", "completed": False}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"

            reminders_adapter.write_cache_file(path, payload)
            loaded = reminders_adapter.load_cache_file(path)
            info = reminders_adapter.cache_info_payload(path)

        self.assertEqual(loaded, payload)
        self.assertTrue(info["exists"])
        self.assertEqual(info["counts"]["reminders"], 1)
        self.assertIsNone(info["stale"])

    def test_load_cache_file_wraps_malformed_json_as_adapter_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text("{not valid json", encoding="utf-8")

            with self.assertRaises(reminders_adapter.AdapterError):
                reminders_adapter.load_cache_file(path)

    def test_cli_parses_positive_limit_and_priority_values(self) -> None:
        parser = reminders_adapter.build_parser()

        args = parser.parse_args(
            [
                "cache_query",
                "--query",
                "invoice",
                "--limit",
                "7",
                "--priority",
                "3",
            ]
        )

        self.assertEqual(args.limit, 7)
        self.assertEqual(args.priority, 3)

    def test_delete_reminder_defaults_to_verified_auto_backend(self) -> None:
        parser = reminders_adapter.build_parser()

        args = parser.parse_args(["delete_reminder", "--id", "7718459E-2672-4E99-9E6A-B9AA430E570F"])

        self.assertEqual(args.backend, "auto")

    def test_private_existing_item_mutation_cli_requires_if_version(self) -> None:
        parser = reminders_adapter.build_parser()
        commands = (
            ["add_tag", "--id", "AAA", "--tag", "health"],
            ["remove_tag", "--id", "AAA", "--tag", "health"],
            ["move_to_section", "--id", "AAA", "--section-id", "SECTION"],
            ["attach_image", "--id", "AAA", "--image", "/tmp/example.png"],
            ["attach_url", "--id", "AAA", "--url", "https://example.com"],
            ["delete_attachment", "--id", "AAA", "--attachment-id", "ATTACHMENT"],
            [
                "replace_attachment",
                "--id",
                "AAA",
                "--attachment-id",
                "ATTACHMENT",
                "--url",
                "https://example.com/new",
            ],
        )

        for command in commands:
            with self.subTest(command=command[0]), mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit):
                    parser.parse_args(command)

    def test_notes_metadata_never_returns_full_notes(self) -> None:
        notes = "Private notes that should not be cached verbatim"

        metadata = reminders_adapter.cache_notes_metadata(notes)

        self.assertEqual(metadata["notes_length"], len(notes))
        self.assertIn("notes_sha256", metadata)
        self.assertNotIn(notes, metadata.values())


class PluginMetadataTests(unittest.TestCase):
    def test_mit_plugin_manifest_has_license_file(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["license"], "MIT")
        self.assertTrue(any(PLUGIN_ROOT.glob("LICENSE*")))

    def test_plugin_manifest_asset_paths_exist(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        interface = manifest["interface"]

        for key in ("composerIcon", "logo", "logoDark"):
            path = PLUGIN_ROOT / interface[key]
            self.assertTrue(path.exists(), f"{key} does not exist: {path}")


class DatabaseSafetyTests(unittest.TestCase):
    def test_native_verification_connection_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "reminders.sqlite"
            sqlite3.connect(database).close()
            connection = reminders_adapter.connect_read_only(database)
            try:
                self.assertEqual(
                    connection.execute("pragma query_only").fetchone()[0],
                    1,
                )
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("create table forbidden (value integer)")
            finally:
                connection.close()

    def test_write_database_must_be_inside_reminders_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stores = root / "Container_v1" / "Stores"
            stores.mkdir(parents=True)
            inside = stores / "reminders.sqlite"
            outside = root / "outside.sqlite"
            inside.touch()
            outside.touch()

            with mock.patch.object(reminders_adapter, "STORES", stores):
                self.assertEqual(
                    reminders_adapter.resolve_database(str(inside), write=True),
                    inside.resolve(),
                )
                self.assertEqual(
                    reminders_adapter.resolve_database(str(outside), write=False),
                    outside.resolve(),
                )
                with self.assertRaises(reminders_adapter.AdapterError):
                    reminders_adapter.resolve_database(str(outside), write=True)


class BackupSafetyTests(unittest.TestCase):
    def test_create_store_backup_rejects_missing_group_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_group = Path(tmp) / "missing" / "Container_v1"
            app_support = Path(tmp) / "support"

            with (
                mock.patch.object(reminders_adapter, "GROUP", missing_group),
                mock.patch.object(reminders_adapter, "APP_SUPPORT", app_support),
                self.assertRaises(reminders_adapter.AdapterError),
            ):
                reminders_adapter.create_store_backup()

    def test_create_store_backup_rejects_output_inside_group_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            group = Path(tmp) / "Container_v1"
            group.mkdir()
            output = group / "nested" / "backup.tgz"

            with (
                mock.patch.object(reminders_adapter, "GROUP", group),
                mock.patch.object(reminders_adapter, "APP_SUPPORT", Path(tmp) / "support"),
                self.assertRaises(reminders_adapter.AdapterError),
            ):
                reminders_adapter.create_store_backup(str(output))


if __name__ == "__main__":
    unittest.main()
