from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "scripts" / "reminders_adapter.py"
SPEC = importlib.util.spec_from_file_location("reminders_adapter", ADAPTER_PATH)
assert SPEC and SPEC.loader
reminders_adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reminders_adapter)


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


class AttachmentSyncTests(unittest.TestCase):
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
                insert into ZREMCDREMINDER values (1,'REM-GOOD','A Good','',null,1,0);
                insert into ZREMCDREMINDER values (2,'REM-BAD','B Bad','',null,1,0);
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

        args = parser.parse_args(["attach_image", "--id", "AAA", "--image", "/tmp/example.png"])

        self.assertEqual(args.backend, "reminderkit")

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
                return_value={"Z_PK": 1, "ZCKIDENTIFIER": reminder_id},
            ),
            mock.patch.object(reminders_adapter, "resolve_attachment_selection", return_value=(selected, [], None)),
            mock.patch.object(reminders_adapter, "attachment_payload", return_value={"pk": 10, "id": "ATTACH-OLD"}),
            mock.patch.object(reminders_adapter, "attach_image_reminderkit_record", return_value=new_result) as attach_reminderkit,
            mock.patch.object(reminders_adapter, "attach_image_record") as attach_db,
            mock.patch.object(reminders_adapter, "soft_delete_attachment_record", return_value={"pk": 10}),
            mock.patch.object(
                reminders_adapter,
                "attachment_replacement_readback",
                return_value={
                    "old_attachment_soft_deleted": True,
                    "new_attachment_active": True,
                },
            ),
            mock.patch.object(reminders_adapter, "log_action"),
            mock.patch.object(reminders_adapter, "json_out"),
        ):
            result = reminders_adapter.cmd_replace_attachment(args)

        self.assertEqual(result, 0)
        attach_reminderkit.assert_called_once()
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
                    )
            finally:
                con.close()

        self.assertTrue(raised.exception.details["partial_failure"])
        self.assertIn("delete_attachment", raised.exception.details["cleanup_command"])

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
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1"}
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
            mock.patch.object(reminders_adapter, "soft_delete_attachment_record", side_effect=reminders_adapter.AdapterError("delete failed")),
            mock.patch.object(reminders_adapter, "compensate_new_attachment", return_value={"pk": 11}) as compensate,
            mock.patch.object(reminders_adapter, "json_out") as json_out,
        ):
            result = reminders_adapter.cmd_replace_attachment(args)

        compensate.assert_called_once_with(con, reminder, new_result)
        receipt = json_out.call_args.args[0]
        self.assertEqual(result, 1)
        self.assertEqual(receipt["status"], "failed_no_mutation")
        self.assertEqual(receipt["verification"]["state"], "compensated")
        self.assertTrue(receipt["verification"]["compensation_succeeded"])

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
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1"}
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

        compensate.assert_called_once_with(con, reminder, expected_result)
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
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1"}
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
            mock.patch.object(reminders_adapter, "soft_delete_attachment_record", side_effect=reminders_adapter.AdapterError("delete failed")),
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
        )
        con = mock.Mock()
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1"}
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
            mock.patch.object(reminders_adapter, "soft_delete_attachment_record", return_value={"id": "ATTACH-OLD", "marked_for_deletion": True}),
            mock.patch.object(
                reminders_adapter,
                "attachment_replacement_readback",
                return_value={"old_attachment_soft_deleted": True, "new_attachment_active": False},
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
        reminder = {"Z_PK": 1, "ZCKIDENTIFIER": "REM-1"}
        old_row = {"Z_PK": 10, "ZCKIDENTIFIER": "ATTACH-OLD"}
        item = {
            "reminder": {"id": "REM-1", "title": "Task"},
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
        compensate.assert_called_once_with(con, reminder, expected_result)


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

    def test_notes_metadata_never_returns_full_notes(self) -> None:
        notes = "Private notes that should not be cached verbatim"

        metadata = reminders_adapter.cache_notes_metadata(notes)

        self.assertEqual(metadata["notes_length"], len(notes))
        self.assertIn("notes_sha256", metadata)
        self.assertNotIn(notes, metadata.values())


class PluginMetadataTests(unittest.TestCase):
    def test_mit_plugin_manifest_has_license_file(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["license"], "MIT")
        self.assertTrue(any(ROOT.glob("LICENSE*")))

    def test_plugin_manifest_asset_paths_exist(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        interface = manifest["interface"]

        for key in ("composerIcon", "logo", "logoDark"):
            path = ROOT / interface[key]
            self.assertTrue(path.exists(), f"{key} does not exist: {path}")


class DatabaseSafetyTests(unittest.TestCase):
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
