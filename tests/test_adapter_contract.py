from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "scripts" / "reminders_adapter.py"
SPEC = importlib.util.spec_from_file_location("reminders_adapter_contract_tests", ADAPTER_PATH)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


def create_tag_store(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(
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
              Z_ENT integer,
              ZHASHTAGLABEL integer,
              ZMARKEDFORDELETION integer
            );
            insert into ZREMCDHASHTAGLABEL values (1, 'Unused', 'unused', 'ACCOUNT-A', null, 1, 1);
            insert into ZREMCDHASHTAGLABEL values (2, 'Used', 'used', 'ACCOUNT-A', null, 1, 1);
            insert into ZREMCDHASHTAGLABEL values (3, 'Historical', 'historical', 'ACCOUNT-A', null, 1, 1);
            insert into ZREMCDHASHTAGLABEL values (4, '%literal', '%literal', 'ACCOUNT-B', null, 1, 1);
            insert into ZREMCDHASHTAGLABEL values (5, 'Anything', 'anything', 'ACCOUNT-B', null, 1, 1);
            insert into ZREMCDOBJECT values (10, 32, 2, 0);
            insert into ZREMCDOBJECT values (11, 32, 3, 1);
            """
        )
        con.commit()
    finally:
        con.close()


def create_delete_store(path: Path) -> str:
    reminder_id = "7718459E-2672-4E99-9E6A-B9AA430E570F"
    con = sqlite3.connect(path)
    try:
        con.executescript(
            f"""
            create table ZREMCDREMINDER (
              Z_PK integer primary key,
              Z_OPT integer,
              ZCKIDENTIFIER text,
              ZCKCLOUDSTATE integer,
              ZLASTMODIFIEDDATE real,
              ZLIST integer,
              Z_FOK_LIST integer,
              ZMARKEDFORDELETION integer,
              ZTITLE text,
              ZCOMPLETED integer
            );
            create table ZREMCDBASELIST (
              Z_PK integer primary key,
              Z_OPT integer,
              ZCKCLOUDSTATE integer,
              ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA text,
              ZREMINDERIDSMERGEABLEORDERING_V2_JSON text,
              ZNAME text
            );
            create table ZREMCKCLOUDSTATE (
              Z_PK integer primary key,
              Z_OPT integer,
              ZCURRENTLOCALVERSION integer,
              ZLOCALVERSIONDATE real
            );
            insert into ZREMCDREMINDER values (
              1, 3, '{reminder_id}', 7, 1, 2, 1024, 0, 'Fixture reminder', 0
            );
            insert into ZREMCDBASELIST values (
              2, 1, 8,
              '{{"memberships":[{{"memberID":"{reminder_id}","groupID":"SECTION-1"}}]}}',
              '["{reminder_id}"]',
              'Fixture list'
            );
            insert into ZREMCKCLOUDSTATE values (7, 1, 1, 1);
            insert into ZREMCKCLOUDSTATE values (8, 1, 1, 1);
            """
        )
        con.commit()
    finally:
        con.close()
    return reminder_id


def create_create_store(path: Path) -> None:
    requirements = adapter.COMMAND_SCHEMA_REQUIREMENTS["create_reminder_db"]
    con = sqlite3.connect(path)
    try:
        for table, columns in requirements.items():
            definitions = []
            for column in sorted(columns):
                if column == "Z_PK":
                    definitions.append("Z_PK integer primary key")
                elif column in {"Z_ENT", "Z_OPT", "Z_MAX"} or column.startswith("Z_"):
                    definitions.append(f"{column}")
                else:
                    definitions.append(f"{column}")
            con.execute(f"create table {table} ({', '.join(definitions)})")

        list_values = {
            "Z_PK": 2,
            "Z_OPT": 1,
            "ZNAME": "Fixture list",
            "ZACCOUNT": 14,
            "ZCKCLOUDSTATE": 8,
            "ZCKIDENTIFIER": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            "ZMARKEDFORDELETION": 0,
            "ZREMINDERIDSMERGEABLEORDERING_V2_JSON": "[]",
        }
        columns = list(list_values)
        con.execute(
            f"insert into ZREMCDBASELIST ({','.join(columns)}) values ({','.join('?' for _ in columns)})",
            [list_values[column] for column in columns],
        )
        cloud_values = {
            "Z_PK": 8,
            "Z_ENT": 45,
            "Z_OPT": 1,
            "ZCURRENTLOCALVERSION": 1,
            "ZLATESTVERSIONSYNCEDTOCLOUD": 1,
            "ZLOCALVERSIONDATE": 1,
        }
        columns = list(cloud_values)
        con.execute(
            f"insert into ZREMCKCLOUDSTATE ({','.join(columns)}) values ({','.join('?' for _ in columns)})",
            [cloud_values[column] for column in columns],
        )
        con.execute("insert into Z_PRIMARYKEY (Z_ENT,Z_NAME,Z_MAX) values (39,'REMCDReminder',0)")
        con.execute("insert into Z_PRIMARYKEY (Z_ENT,Z_NAME,Z_MAX) values (45,'REMCKCloudState',8)")
        con.commit()
    finally:
        con.close()


class ReceiptAndErrorContractTests(unittest.TestCase):
    def test_operation_receipt_has_stable_shape(self) -> None:
        payload = adapter.operation_receipt(
            status="verified",
            operation="update_reminder",
            backend="db",
            target={"id": "R-1"},
            verification={"state": "read_back"},
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "verified")
        self.assertEqual(payload["operation"], "update_reminder")
        self.assertTrue(payload["operation_id"])
        self.assertEqual(payload["target"], {"id": "R-1"})

    def test_exact_reminder_selector_rejects_unbounded_or_title_only_delete(self) -> None:
        with self.assertRaises(adapter.AdapterError) as empty:
            adapter.require_exact_reminder_selector(reminder_id=None, title=None, list_name=None)
        with self.assertRaises(adapter.AdapterError) as title_only:
            adapter.require_exact_reminder_selector(reminder_id=None, title="Only title", list_name=None)

        self.assertEqual(empty.exception.code, "ambiguous_target")
        self.assertEqual(title_only.exception.code, "ambiguous_target")

    def test_adapter_error_is_rendered_as_machine_readable_error(self) -> None:
        with mock.patch.object(adapter, "json_out") as output:
            code = adapter.fail("Changed", code="concurrent_modification", current_version=3)

        self.assertEqual(code, 1)
        payload = output.call_args.args[0]
        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertEqual(payload["error"]["code"], "concurrent_modification")
        self.assertEqual(payload["error"]["current_version"], 3)

    def test_uncompensated_partial_failure_requires_manual_repair(self) -> None:
        args = argparse.Namespace(
            func=mock.Mock(
                side_effect=adapter.AdapterError(
                    "Replacement failed",
                    partial_failure=True,
                    compensated=False,
                    new_attachment_id="A-1",
                )
            )
        )
        parser = mock.Mock()
        parser.parse_args.return_value = args

        with (
            mock.patch.object(adapter, "build_parser", return_value=parser),
            mock.patch.object(adapter, "json_out") as output,
        ):
            code = adapter.main([])

        self.assertEqual(code, 1)
        payload = output.call_args.args[0]
        self.assertEqual(payload["status"], "failed_manual_repair_required")
        self.assertEqual(payload["error"]["new_attachment_id"], "A-1")


class JournalPrivacyTests(unittest.TestCase):
    def test_sensitive_action_fields_are_redacted(self) -> None:
        raw = {
            "id": "R-1",
            "title": "Private health reminder",
            "list": "Medical",
            "url": "https://secret.example/path",
            "source_path": "/Users/example/private.png",
            "nested": {"filename": "private.png", "count": 2},
        }

        redacted = adapter.redact_log_payload(raw)
        encoded = json.dumps(redacted, ensure_ascii=False)

        self.assertEqual(redacted["id"], "R-1")
        self.assertTrue(redacted["title"]["redacted"])
        self.assertTrue(redacted["nested"]["filename"]["redacted"])
        self.assertNotIn("Private health reminder", encoded)
        self.assertNotIn("secret.example", encoded)
        self.assertNotIn("private.png", encoded)

    def test_journal_failure_is_returned_as_warning_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            journal = support / "actions.jsonl"
            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "JOURNAL", journal),
                mock.patch.object(Path, "open", side_effect=OSError("denied")),
            ):
                warning = adapter.log_action("create_reminder", {"title": "Private"})

        self.assertIsNotNone(warning)
        self.assertEqual(warning["code"], "journal_write_failed")


class IdempotencyContractTests(unittest.TestCase):
    def test_repeated_key_replays_once_and_store_contains_no_user_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            calls = 0

            def callback() -> dict[str, object]:
                nonlocal calls
                calls += 1
                return adapter.operation_receipt(
                    status="verified",
                    operation="create_reminder",
                    backend="db",
                    target={"id": "R-1", "title": "Sensitive title", "list": "Private list"},
                )

            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "IDEMPOTENCY_STORE", support / "idempotency.json"),
                mock.patch.object(adapter, "IDEMPOTENCY_LOCK", support / "idempotency.lock"),
            ):
                first = adapter.execute_idempotent(
                    operation="create_reminder",
                    key="request-1",
                    input_payload={"title": "Sensitive title"},
                    callback=callback,
                )
                second = adapter.execute_idempotent(
                    operation="create_reminder",
                    key="request-1",
                    input_payload={"title": "Sensitive title"},
                    callback=callback,
                )
                stored = (support / "idempotency.json").read_text(encoding="utf-8")

        self.assertEqual(calls, 1)
        self.assertFalse(first.get("replayed", False))
        self.assertTrue(second["replayed"])
        self.assertEqual(second["target"]["id"], "R-1")
        self.assertNotIn("Sensitive title", stored)
        self.assertNotIn("Private list", stored)
        self.assertNotIn("request-1", stored)

    def test_reusing_key_with_different_input_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "IDEMPOTENCY_STORE", support / "idempotency.json"),
                mock.patch.object(adapter, "IDEMPOTENCY_LOCK", support / "idempotency.lock"),
            ):
                adapter.execute_idempotent(
                    operation="create_reminder",
                    key="request-1",
                    input_payload={"title": "A"},
                    callback=lambda: {"ok": True, "status": "verified", "id": "R-1"},
                )
                with self.assertRaises(adapter.AdapterError) as raised:
                    adapter.execute_idempotent(
                        operation="create_reminder",
                        key="request-1",
                        input_payload={"title": "B"},
                        callback=lambda: {"ok": True, "status": "verified", "id": "R-2"},
                    )

        self.assertEqual(raised.exception.code, "concurrent_modification")


class SchemaCapabilityTests(unittest.TestCase):
    def test_cleanup_capability_reports_missing_column_before_write(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        try:
            con.execute("create table ZREMCDHASHTAGLABEL (Z_PK integer)")
            con.execute("create table ZREMCDOBJECT (Z_PK integer)")

            capability = adapter.command_capability(con, "cleanup_tags")

            self.assertFalse(capability["supported"])
            self.assertIn("ZNAME", capability["missing_columns"]["ZREMCDHASHTAGLABEL"])
            self.assertTrue(capability["schema_fingerprint"])
        finally:
            con.close()


class CleanupTagContractTests(unittest.TestCase):
    def make_args(self, db: Path, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "db": str(db),
            "tag": None,
            "prefix": None,
            "account_id": None,
            "preview_digest": None,
            "apply": False,
            "no_backup": False,
            "limit": 100,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_percent_prefix_is_literal_not_global_wildcard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "tags.sqlite"
            create_tag_store(db)
            con = adapter.connect(db)
            try:
                rows, truncated = adapter.cleanup_tag_candidates(
                    con,
                    tag=None,
                    prefix="%",
                    account_id=None,
                    limit=100,
                )
            finally:
                con.close()

        self.assertFalse(truncated)
        self.assertEqual([row["ZNAME"] for row in rows], ["%literal"])

    def test_soft_deleted_assignment_still_prevents_label_hard_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "tags.sqlite"
            create_tag_store(db)
            con = adapter.connect(db)
            try:
                rows, _ = adapter.cleanup_tag_candidates(
                    con,
                    tag=None,
                    prefix=None,
                    account_id="ACCOUNT-A",
                    limit=100,
                )
            finally:
                con.close()

        self.assertEqual([row["ZNAME"] for row in rows], ["Unused"])

    def test_apply_requires_preview_digest_and_deletes_only_previewed_label(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "tags.sqlite"
            create_tag_store(db)
            preview_args = self.make_args(db, tag="Unused")
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "json_out") as output,
            ):
                adapter.cmd_cleanup_tags(preview_args)
            preview = output.call_args.args[0]

            missing_digest = self.make_args(db, tag="Unused", apply=True)
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                self.assertRaises(adapter.AdapterError) as raised,
            ):
                adapter.cmd_cleanup_tags(missing_digest)
            self.assertEqual(raised.exception.code, "ambiguous_scope")

            apply_args = self.make_args(
                db,
                tag="Unused",
                apply=True,
                preview_digest=preview["candidate_digest"],
            )
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "log_action", return_value=None),
                mock.patch.object(adapter, "json_out") as applied_output,
            ):
                adapter.cmd_cleanup_tags(apply_args)
            receipt = applied_output.call_args.args[0]

            con = sqlite3.connect(db)
            try:
                names = [row[0] for row in con.execute("select ZNAME from ZREMCDHASHTAGLABEL order by Z_PK")]
            finally:
                con.close()

        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["verification"]["scope"], "local_private_store")
        self.assertEqual(receipt["verification"]["icloud_propagation"], "not_verified")
        self.assertEqual(receipt["warnings"][0]["code"], "private_label_sync_unverified")
        self.assertNotIn("Unused", names)
        self.assertIn("Used", names)
        self.assertIn("Historical", names)

    def test_apply_rejects_changed_candidate_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "tags.sqlite"
            create_tag_store(db)
            preview_args = self.make_args(db, prefix="A", account_id="ACCOUNT-B")
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "json_out") as output,
            ):
                adapter.cmd_cleanup_tags(preview_args)
            digest = output.call_args.args[0]["candidate_digest"]

            con = sqlite3.connect(db)
            try:
                con.execute(
                    "insert into ZREMCDHASHTAGLABEL values (6, 'Another', 'another', 'ACCOUNT-B', null, 1, 1)"
                )
                con.commit()
            finally:
                con.close()

            apply_args = self.make_args(
                db,
                prefix="A",
                account_id="ACCOUNT-B",
                apply=True,
                preview_digest=digest,
            )
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                self.assertRaises(adapter.AdapterError) as raised,
            ):
                adapter.cmd_cleanup_tags(apply_args)

        self.assertEqual(raised.exception.code, "concurrent_modification")


class DeleteReminderContractTests(unittest.TestCase):
    def make_args(self, db: Path, reminder_id: str, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "db": str(db),
            "backend": "db",
            "id": reminder_id,
            "title": None,
            "list": None,
            "if_version": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_db_soft_delete_receipt_and_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "delete.sqlite"
            reminder_id = create_delete_store(db)
            args = self.make_args(db, reminder_id, if_version=3)
            capability_record = Path(temp_dir) / "missing-capabilities.json"
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "CAPABILITY_RECORD", capability_record),
                mock.patch.object(adapter, "log_action", return_value=None),
                mock.patch.object(adapter, "json_out") as output,
            ):
                adapter.cmd_delete_reminder(args)
            receipt = output.call_args.args[0]

            con = sqlite3.connect(db)
            try:
                row = con.execute(
                    "select ZMARKEDFORDELETION,ZLIST from ZREMCDREMINDER where Z_PK=1"
                ).fetchone()
                list_row = con.execute(
                    "select ZREMINDERIDSMERGEABLEORDERING_V2_JSON,ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA "
                    "from ZREMCDBASELIST where Z_PK=2"
                ).fetchone()
            finally:
                con.close()

        self.assertEqual(receipt["backend"], "db")
        self.assertEqual(receipt["status"], "committed_verification_pending")
        self.assertEqual(receipt["recovery"]["semantics"], "soft_deleted_unverified")
        self.assertEqual(row, (1, None))
        self.assertEqual(json.loads(list_row[0]), [])
        self.assertEqual(adapter.membership_map(list_row[1]), {})

    def test_if_version_conflict_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "delete.sqlite"
            reminder_id = create_delete_store(db)
            args = self.make_args(db, reminder_id, if_version=99)
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                self.assertRaises(adapter.AdapterError) as raised,
            ):
                adapter.cmd_delete_reminder(args)

            con = sqlite3.connect(db)
            try:
                row = con.execute(
                    "select ZMARKEDFORDELETION,ZLIST from ZREMCDREMINDER where Z_PK=1"
                ).fetchone()
            finally:
                con.close()

        self.assertEqual(raised.exception.code, "concurrent_modification")
        self.assertEqual(row, (0, 2))

    def test_auto_uses_native_until_db_recovery_parity_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "delete.sqlite"
            reminder_id = create_delete_store(db)
            args = self.make_args(db, reminder_id, backend="auto")
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "CAPABILITY_RECORD", Path(temp_dir) / "missing.json"),
                mock.patch.object(adapter, "run_osascript", return_value=f"x-apple-reminder://{reminder_id}"),
                mock.patch.object(adapter, "log_action", return_value=None),
                mock.patch.object(adapter, "json_out") as output,
            ):
                adapter.cmd_delete_reminder(args)

        receipt = output.call_args.args[0]
        self.assertEqual(receipt["backend"], "applescript")
        self.assertEqual(receipt["backend_requested"], "auto")
        self.assertFalse(receipt["auto_evidence"]["verified"])


class CreateReminderContractTests(unittest.TestCase):
    def make_args(self, db: Path) -> argparse.Namespace:
        return argparse.Namespace(
            db=str(db),
            backend="db",
            list="Fixture list",
            title="Fixture title",
            notes="Fixture notes",
            due_at=None,
            remind_at=None,
            all_day_due_date=None,
            flagged=False,
            priority=0,
            idempotency_key=None,
        )

    def test_post_commit_native_text_failure_is_partial_success_not_retryable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "create.sqlite"
            create_create_store(db)
            args = self.make_args(db)
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(
                    adapter,
                    "sync_reminder_text_applescript",
                    side_effect=adapter.AdapterError("Automation denied", code="permission_denied"),
                ),
                mock.patch.object(adapter, "log_action", return_value=None),
            ):
                receipt = adapter.create_reminder_once(args)

            con = sqlite3.connect(db)
            try:
                row = con.execute("select ZCKIDENTIFIER,ZTITLE,ZNOTES from ZREMCDREMINDER").fetchone()
            finally:
                con.close()

        self.assertEqual(receipt["status"], "partial_success")
        self.assertTrue(receipt["verification"]["database_row"])
        self.assertFalse(receipt["verification"]["native_text_sync"])
        self.assertEqual(row[1:], ("Fixture title", "Fixture notes"))
        self.assertTrue(row[0])

    def test_create_parser_accepts_idempotency_key(self) -> None:
        parser = adapter.build_parser()

        args = parser.parse_args(
            [
                "create_reminder",
                "--list",
                "Fixture list",
                "--title",
                "Fixture title",
                "--idempotency-key",
                "request-123",
            ]
        )

        self.assertEqual(args.idempotency_key, "request-123")


if __name__ == "__main__":
    unittest.main()
