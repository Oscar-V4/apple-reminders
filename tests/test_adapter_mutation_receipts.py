from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "scripts" / "reminders_adapter.py"
SPEC = importlib.util.spec_from_file_location("reminders_adapter_mutation_receipts", ADAPTER_PATH)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


ACCOUNT_ID = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
LIST_ALPHA_ID = "BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB"
LIST_BETA_ID = "CCCCCCCC-CCCC-4CCC-8CCC-CCCCCCCCCCCC"
LIST_GAMMA_ID = "DDDDDDDD-DDDD-4DDD-8DDD-DDDDDDDDDDDD"
REMINDER_ID = "EEEEEEEE-EEEE-4EEE-8EEE-EEEEEEEEEEEE"
SECOND_REMINDER_ID = "FFFFFFFF-FFFF-4FFF-8FFF-FFFFFFFFFFFF"
SECTION_A_ID = "11111111-1111-4111-8111-111111111111"
SECTION_B_ID = "22222222-2222-4222-8222-222222222222"
SECTION_C_ID = "33333333-3333-4333-8333-333333333333"
ATTACHMENT_1_ID = "44444444-4444-4444-8444-444444444444"
ATTACHMENT_2_ID = "55555555-5555-4555-8555-555555555555"


def build_fixture_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        create table ZREMCDREMINDER (
            Z_PK integer primary key,
            Z_ENT integer,
            Z_OPT integer,
            ZALLDAY integer,
            ZCKDIRTYFLAGS integer,
            ZCOMPLETED integer,
            ZCOMPLETIONDATE real,
            ZDISPLAYDATEISALLDAY integer,
            ZDISPLAYDATEUPDATEDFORSECONDSFROMGMT integer,
            ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION integer,
            ZFLAGGED integer,
            ZICSDISPLAYORDER integer,
            ZISURGENTSTATEENABLEDFORCURRENTUSER integer,
            ZMARKEDFORDELETION integer,
            ZMINIMUMSUPPORTEDAPPVERSION integer,
            ZPRIORITY integer,
            ZSPOTLIGHTINDEXCOUNT integer,
            ZACCOUNT integer,
            ZCKCLOUDSTATE integer,
            ZLIST integer,
            Z_FOK_LIST integer,
            ZCREATIONDATE real,
            ZDISPLAYDATEDATE real,
            ZDUEDATE real,
            ZLASTMODIFIEDDATE real,
            ZCKIDENTIFIER text,
            ZDACALENDARITEMUNIQUEIDENTIFIER text,
            ZDISPLAYDATETIMEZONE text,
            ZNOTES text,
            ZTIMEZONE text,
            ZTITLE text,
            ZIDENTIFIER blob,
            ZNOTESDOCUMENT blob,
            ZTITLEDOCUMENT blob,
            ZRESOLUTIONTOKENMAP_V3_JSONDATA text
        );
        create table ZREMCDBASELIST (
            Z_PK integer primary key,
            Z_OPT integer,
            ZNAME text,
            ZACCOUNT integer,
            ZCKCLOUDSTATE integer,
            ZCKIDENTIFIER text,
            ZMARKEDFORDELETION integer,
            ZREMINDERIDSMERGEABLEORDERING_V2_JSON text,
            ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA text,
            ZISGROUP integer,
            ZPARENTLIST integer,
            ZPARENTACCOUNT integer,
            ZCOLOR text,
            ZBADGEEMBLEM text
        );
        create table ZREMCDBASESECTION (
            Z_PK integer primary key,
            Z_ENT integer,
            Z_OPT integer,
            ZCKDIRTYFLAGS integer,
            ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION integer,
            ZMINIMUMSUPPORTEDAPPVERSION integer,
            ZSPOTLIGHTINDEXCOUNT integer,
            ZACCOUNT integer,
            ZCKCLOUDSTATE integer,
            ZLIST integer,
            Z_FOK_LIST integer,
            ZCREATIONDATE real,
            ZCKIDENTIFIER text,
            ZDISPLAYNAME text,
            ZIDENTIFIER blob,
            ZRESOLUTIONTOKENMAP_V3_JSONDATA text,
            ZMARKEDFORDELETION integer
        );
        create table ZREMCDOBJECT (
            Z_PK integer primary key,
            Z_ENT integer,
            Z_OPT integer,
            ZCKDIRTYFLAGS integer,
            ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION integer,
            ZMARKEDFORDELETION integer,
            ZMINIMUMSUPPORTEDAPPVERSION integer,
            ZACCOUNT integer,
            ZCKCLOUDSTATE integer,
            ZREMINDER2 integer,
            ZREMINDER3 integer,
            Z_FOK_REMINDER1 integer,
            ZFILESIZE integer,
            ZHEIGHT integer,
            ZWIDTH integer,
            ZUTI text,
            ZFILENAME text,
            ZSHA512SUM text,
            ZURL text,
            ZHOSTURL text,
            ZIDENTIFIER blob,
            ZCKIDENTIFIER text,
            ZHASHTAGLABEL integer,
            ZCKSERVERRECORDDATA blob
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
        create table ZREMCKCLOUDSTATE (
            Z_PK integer primary key,
            Z_ENT integer,
            Z_OPT integer,
            ZCURRENTLOCALVERSION integer,
            ZLATESTVERSIONSYNCEDTOCLOUD integer,
            ZREMINDER integer,
            ZLOCALVERSIONDATE real,
            ZSECTION integer,
            Z5_SECTION integer,
            ZOBJECT integer,
            Z13_OBJECT integer,
            ZINCLOUD integer,
            ZCKSERVERRECORDDATA blob
        );
        create table Z_PRIMARYKEY (
            Z_ENT integer primary key,
            Z_NAME text,
            Z_MAX integer
        );
        """
    )


def insert_row(con: sqlite3.Connection, table: str, **values: object) -> None:
    cols = list(values)
    placeholders = ",".join("?" for _ in cols)
    con.execute(
        f"insert into {table} ({','.join(cols)}) values ({placeholders})",
        [values[col] for col in cols],
    )


def seed_catalog_fixture(path: Path) -> None:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        build_fixture_schema(con)
        now = 1.0
        insert_row(con, "Z_PRIMARYKEY", Z_ENT=5, Z_NAME="REMCDBaseSection", Z_MAX=40)
        insert_row(con, "Z_PRIMARYKEY", Z_ENT=11, Z_NAME="REMCDHashtagLabel", Z_MAX=40)
        insert_row(con, "Z_PRIMARYKEY", Z_ENT=13, Z_NAME="REMCDObject", Z_MAX=200)
        insert_row(con, "Z_PRIMARYKEY", Z_ENT=39, Z_NAME="REMCDReminder", Z_MAX=10)
        insert_row(con, "Z_PRIMARYKEY", Z_ENT=45, Z_NAME="REMCKCloudState", Z_MAX=500)

        insert_row(
            con,
            "ZREMCDOBJECT",
            Z_PK=100,
            Z_ENT=14,
            Z_OPT=1,
            ZCKDIRTYFLAGS=0,
            ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION=0,
            ZMARKEDFORDELETION=0,
            ZMINIMUMSUPPORTEDAPPVERSION=0,
            ZACCOUNT=None,
            ZCKCLOUDSTATE=None,
            ZREMINDER2=None,
            ZREMINDER3=None,
            Z_FOK_REMINDER1=None,
            ZFILESIZE=None,
            ZHEIGHT=None,
            ZWIDTH=None,
            ZUTI=None,
            ZFILENAME=None,
            ZSHA512SUM=None,
            ZURL=None,
            ZHOSTURL=None,
            ZIDENTIFIER=sqlite3.Binary(ACCOUNT_ID.encode("utf-8")),
            ZCKIDENTIFIER=ACCOUNT_ID,
            ZHASHTAGLABEL=None,
            ZCKSERVERRECORDDATA=None,
        )

        for pk, name, cloud_pk, identifier in (
            (10, "Alpha", 210, LIST_ALPHA_ID),
            (11, "Beta", 211, LIST_BETA_ID),
            (12, "Gamma", 212, LIST_GAMMA_ID),
        ):
            insert_row(
                con,
                "ZREMCKCLOUDSTATE",
                Z_PK=cloud_pk,
                Z_ENT=45,
                Z_OPT=1,
                ZCURRENTLOCALVERSION=1,
                ZLATESTVERSIONSYNCEDTOCLOUD=1,
                ZREMINDER=None,
                ZLOCALVERSIONDATE=now,
                ZSECTION=None,
                Z5_SECTION=None,
                ZOBJECT=None,
                Z13_OBJECT=None,
                ZINCLOUD=1,
                ZCKSERVERRECORDDATA=b"cloud",
            )
            insert_row(
                con,
                "ZREMCDBASELIST",
                Z_PK=pk,
                Z_OPT=1,
                ZNAME=name,
                ZACCOUNT=100,
                ZCKCLOUDSTATE=cloud_pk,
                ZCKIDENTIFIER=identifier,
                ZMARKEDFORDELETION=0,
                ZREMINDERIDSMERGEABLEORDERING_V2_JSON=json.dumps([REMINDER_ID]) if name == "Beta" else "[]",
                ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA=(
                    adapter.membership_payload({REMINDER_ID: SECTION_A_ID})
                    if name == "Beta"
                    else adapter.membership_payload({})
                ),
                ZISGROUP=0,
                ZPARENTLIST=None,
                ZPARENTACCOUNT=None,
                ZCOLOR=name.lower(),
                ZBADGEEMBLEM=name.lower(),
            )

        for pk, cloud_pk, name, identifier, order in (
            (20, 220, "Section A", SECTION_A_ID, 1024),
            (21, 221, "Section B", SECTION_B_ID, 2048),
        ):
            insert_row(
                con,
                "ZREMCKCLOUDSTATE",
                Z_PK=cloud_pk,
                Z_ENT=45,
                Z_OPT=1,
                ZCURRENTLOCALVERSION=1,
                ZLATESTVERSIONSYNCEDTOCLOUD=1,
                ZREMINDER=None,
                ZLOCALVERSIONDATE=now,
                ZSECTION=None,
                Z5_SECTION=None,
                ZOBJECT=None,
                Z13_OBJECT=None,
                ZINCLOUD=1,
                ZCKSERVERRECORDDATA=b"cloud",
            )
            insert_row(
                con,
                "ZREMCDBASESECTION",
                Z_PK=pk,
                Z_ENT=6,
                Z_OPT=1,
                ZCKDIRTYFLAGS=0,
                ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION=0,
                ZMINIMUMSUPPORTEDAPPVERSION=0,
                ZSPOTLIGHTINDEXCOUNT=0,
                ZACCOUNT=100,
                ZCKCLOUDSTATE=cloud_pk,
                ZLIST=11,
                Z_FOK_LIST=order,
                ZCREATIONDATE=now,
                ZCKIDENTIFIER=identifier,
                ZDISPLAYNAME=name,
                ZIDENTIFIER=sqlite3.Binary(identifier.encode("utf-8")),
                ZRESOLUTIONTOKENMAP_V3_JSONDATA=json.dumps({"map": {}}),
                ZMARKEDFORDELETION=0,
            )

        insert_row(
            con,
            "ZREMCKCLOUDSTATE",
            Z_PK=301,
            Z_ENT=45,
            Z_OPT=1,
            ZCURRENTLOCALVERSION=1,
            ZLATESTVERSIONSYNCEDTOCLOUD=1,
            ZREMINDER=1,
            ZLOCALVERSIONDATE=now,
            ZSECTION=None,
            Z5_SECTION=None,
            ZOBJECT=None,
            Z13_OBJECT=None,
            ZINCLOUD=1,
            ZCKSERVERRECORDDATA=b"cloud",
        )
        insert_row(
            con,
            "ZREMCDREMINDER",
            Z_PK=1,
            Z_ENT=39,
            Z_OPT=1,
            ZALLDAY=0,
            ZCKDIRTYFLAGS=0,
            ZCOMPLETED=0,
            ZCOMPLETIONDATE=None,
            ZDISPLAYDATEISALLDAY=0,
            ZDISPLAYDATEUPDATEDFORSECONDSFROMGMT=0,
            ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION=0,
            ZFLAGGED=0,
            ZICSDISPLAYORDER=0,
            ZISURGENTSTATEENABLEDFORCURRENTUSER=0,
            ZMARKEDFORDELETION=0,
            ZMINIMUMSUPPORTEDAPPVERSION=0,
            ZPRIORITY=1,
            ZSPOTLIGHTINDEXCOUNT=1,
            ZACCOUNT=100,
            ZCKCLOUDSTATE=301,
            ZLIST=11,
            Z_FOK_LIST=1024,
            ZCREATIONDATE=now,
            ZDISPLAYDATEDATE=None,
            ZDUEDATE=None,
            ZLASTMODIFIEDDATE=now,
            ZCKIDENTIFIER=REMINDER_ID,
            ZDACALENDARITEMUNIQUEIDENTIFIER=REMINDER_ID,
            ZDISPLAYDATETIMEZONE=None,
            ZNOTES="Original notes",
            ZTIMEZONE=None,
            ZTITLE="Original title",
            ZIDENTIFIER=sqlite3.Binary(REMINDER_ID.encode("utf-8")),
            ZNOTESDOCUMENT=None,
            ZTITLEDOCUMENT=None,
            ZRESOLUTIONTOKENMAP_V3_JSONDATA=json.dumps({"map": {}}),
        )

        insert_row(
            con,
            "ZREMCKCLOUDSTATE",
            Z_PK=401,
            Z_ENT=45,
            Z_OPT=1,
            ZCURRENTLOCALVERSION=1,
            ZLATESTVERSIONSYNCEDTOCLOUD=1,
            ZREMINDER=None,
            ZLOCALVERSIONDATE=now,
            ZSECTION=20,
            Z5_SECTION=20,
            ZOBJECT=None,
            Z13_OBJECT=None,
            ZINCLOUD=1,
            ZCKSERVERRECORDDATA=b"cloud",
        )
        insert_row(
            con,
            "ZREMCKCLOUDSTATE",
            Z_PK=402,
            Z_ENT=45,
            Z_OPT=1,
            ZCURRENTLOCALVERSION=1,
            ZLATESTVERSIONSYNCEDTOCLOUD=1,
            ZREMINDER=None,
            ZLOCALVERSIONDATE=now,
            ZSECTION=21,
            Z5_SECTION=21,
            ZOBJECT=None,
            Z13_OBJECT=None,
            ZINCLOUD=1,
            ZCKSERVERRECORDDATA=b"cloud",
        )

        for pk, identifier, url, order in (
            (30, ATTACHMENT_1_ID, "https://one.example/path", 1024),
            (31, ATTACHMENT_2_ID, "https://two.example/path", 2048),
        ):
            insert_row(
                con,
                "ZREMCDOBJECT",
                Z_PK=pk,
                Z_ENT=26,
                Z_OPT=1,
                ZCKDIRTYFLAGS=0,
                ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION=0,
                ZMARKEDFORDELETION=0,
                ZMINIMUMSUPPORTEDAPPVERSION=0,
                ZACCOUNT=100,
                ZCKCLOUDSTATE=pk + 300,
                ZREMINDER2=1,
                ZREMINDER3=None,
                Z_FOK_REMINDER1=order,
                ZFILESIZE=None,
                ZHEIGHT=None,
                ZWIDTH=None,
                ZUTI="public.url",
                ZFILENAME=None,
                ZSHA512SUM=None,
                ZURL=url,
                ZHOSTURL="one.example" if "one" in url else "two.example",
                ZIDENTIFIER=sqlite3.Binary(identifier.encode("utf-8")),
                ZCKIDENTIFIER=identifier,
                ZHASHTAGLABEL=None,
                ZCKSERVERRECORDDATA=b"server-record",
            )
            insert_row(
                con,
                "ZREMCKCLOUDSTATE",
                Z_PK=pk + 300,
                Z_ENT=45,
                Z_OPT=1,
                ZCURRENTLOCALVERSION=1,
                ZLATESTVERSIONSYNCEDTOCLOUD=1,
                ZREMINDER=None,
                ZLOCALVERSIONDATE=now,
                ZSECTION=None,
                Z5_SECTION=None,
                ZOBJECT=pk,
                Z13_OBJECT=26,
                ZINCLOUD=1,
                ZCKSERVERRECORDDATA=b"cloud",
            )

        con.commit()
    finally:
        con.close()


def seed_repair_fixture(path: Path) -> tuple[Path, Path, Path]:
    support = path.parent / "support"
    files_root = path.parent / "files"
    attachment_dir = files_root / f"Account-{ACCOUNT_ID}" / "Attachments"
    attachment_dir.mkdir(parents=True, exist_ok=True)
    support.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        build_fixture_schema(con)
        now = 1.0
        insert_row(con, "Z_PRIMARYKEY", Z_ENT=39, Z_NAME="REMCDReminder", Z_MAX=10)
        insert_row(con, "Z_PRIMARYKEY", Z_ENT=13, Z_NAME="REMCDObject", Z_MAX=200)
        insert_row(con, "Z_PRIMARYKEY", Z_ENT=45, Z_NAME="REMCKCloudState", Z_MAX=500)

        insert_row(
            con,
            "ZREMCDOBJECT",
            Z_PK=100,
            Z_ENT=14,
            Z_OPT=1,
            ZCKDIRTYFLAGS=0,
            ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION=0,
            ZMARKEDFORDELETION=0,
            ZMINIMUMSUPPORTEDAPPVERSION=0,
            ZACCOUNT=None,
            ZCKCLOUDSTATE=None,
            ZREMINDER2=None,
            ZREMINDER3=None,
            Z_FOK_REMINDER1=None,
            ZFILESIZE=None,
            ZHEIGHT=None,
            ZWIDTH=None,
            ZUTI=None,
            ZFILENAME=None,
            ZSHA512SUM=None,
            ZURL=None,
            ZHOSTURL=None,
            ZIDENTIFIER=sqlite3.Binary(ACCOUNT_ID.encode("utf-8")),
            ZCKIDENTIFIER=ACCOUNT_ID,
            ZHASHTAGLABEL=None,
            ZCKSERVERRECORDDATA=None,
        )

        insert_row(
            con,
            "ZREMCKCLOUDSTATE",
            Z_PK=301,
            Z_ENT=45,
            Z_OPT=1,
            ZCURRENTLOCALVERSION=1,
            ZLATESTVERSIONSYNCEDTOCLOUD=0,
            ZREMINDER=1,
            ZLOCALVERSIONDATE=now,
            ZSECTION=None,
            Z5_SECTION=None,
            ZOBJECT=None,
            Z13_OBJECT=None,
            ZINCLOUD=0,
            ZCKSERVERRECORDDATA=b"cloud",
        )
        insert_row(
            con,
            "ZREMCDBASELIST",
            Z_PK=11,
            Z_OPT=1,
            ZNAME="Beta",
            ZACCOUNT=100,
            ZCKCLOUDSTATE=211,
            ZCKIDENTIFIER=LIST_BETA_ID,
            ZMARKEDFORDELETION=0,
            ZREMINDERIDSMERGEABLEORDERING_V2_JSON=json.dumps([REMINDER_ID]),
            ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA=adapter.membership_payload({REMINDER_ID: SECTION_A_ID}),
            ZISGROUP=0,
            ZPARENTLIST=None,
            ZPARENTACCOUNT=None,
            ZCOLOR="blue",
            ZBADGEEMBLEM="blue",
        )
        insert_row(
            con,
            "ZREMCDREMINDER",
            Z_PK=1,
            Z_ENT=39,
            Z_OPT=1,
            ZALLDAY=0,
            ZCKDIRTYFLAGS=0,
            ZCOMPLETED=0,
            ZCOMPLETIONDATE=None,
            ZDISPLAYDATEISALLDAY=0,
            ZDISPLAYDATEUPDATEDFORSECONDSFROMGMT=0,
            ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION=0,
            ZFLAGGED=0,
            ZICSDISPLAYORDER=0,
            ZISURGENTSTATEENABLEDFORCURRENTUSER=0,
            ZMARKEDFORDELETION=0,
            ZMINIMUMSUPPORTEDAPPVERSION=0,
            ZPRIORITY=1,
            ZSPOTLIGHTINDEXCOUNT=1,
            ZACCOUNT=100,
            ZCKCLOUDSTATE=301,
            ZLIST=11,
            Z_FOK_LIST=1024,
            ZCREATIONDATE=now,
            ZDISPLAYDATEDATE=None,
            ZDUEDATE=None,
            ZLASTMODIFIEDDATE=now,
            ZCKIDENTIFIER=REMINDER_ID,
            ZDACALENDARITEMUNIQUEIDENTIFIER=REMINDER_ID,
            ZDISPLAYDATETIMEZONE=None,
            ZNOTES="Needs repair",
            ZTIMEZONE=None,
            ZTITLE="Repair me",
            ZIDENTIFIER=sqlite3.Binary(REMINDER_ID.encode("utf-8")),
            ZNOTESDOCUMENT=None,
            ZTITLEDOCUMENT=None,
            ZRESOLUTIONTOKENMAP_V3_JSONDATA=json.dumps({"map": {}}),
        )

        from hashlib import sha512

        source_bytes = b"repair-source"
        digest = sha512(source_bytes).hexdigest()
        source = attachment_dir / f"{digest}.png"
        source.write_bytes(source_bytes)
        insert_row(
            con,
            "ZREMCDOBJECT",
            Z_PK=30,
            Z_ENT=25,
            Z_OPT=1,
            ZCKDIRTYFLAGS=0,
            ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION=0,
            ZMARKEDFORDELETION=0,
            ZMINIMUMSUPPORTEDAPPVERSION=0,
            ZACCOUNT=100,
            ZCKCLOUDSTATE=330,
            ZREMINDER2=1,
            ZREMINDER3=None,
            Z_FOK_REMINDER1=1024,
            ZFILESIZE=len(source.read_bytes()),
            ZHEIGHT=1,
            ZWIDTH=1,
            ZUTI="public.png",
            ZFILENAME="repair-source.png",
            ZSHA512SUM=digest,
            ZURL=None,
            ZHOSTURL=None,
            ZIDENTIFIER=sqlite3.Binary("REPAIR-ATTACH".encode("utf-8")),
            ZCKIDENTIFIER="REPAIR-ATTACH",
            ZHASHTAGLABEL=None,
            ZCKSERVERRECORDDATA=None,
        )
        insert_row(
            con,
            "ZREMCKCLOUDSTATE",
            Z_PK=330,
            Z_ENT=45,
            Z_OPT=1,
            ZCURRENTLOCALVERSION=1,
            ZLATESTVERSIONSYNCEDTOCLOUD=0,
            ZREMINDER=None,
            ZLOCALVERSIONDATE=now,
            ZSECTION=None,
            Z5_SECTION=None,
            ZOBJECT=30,
            Z13_OBJECT=25,
            ZINCLOUD=0,
            ZCKSERVERRECORDDATA=None,
        )
        con.commit()
    finally:
        con.close()
    return support, files_root, source


def capture_json_output(func, args: argparse.Namespace, *, patches: list[tuple[str, object]] | None = None):
    patches = patches or []
    with ExitStack() as stack:
        for name, value in patches:
            if name == "resolve_database" and not callable(value):
                value = mock.Mock(return_value=value)
            elif name == "log_action" and value is None:
                value = mock.Mock(return_value=None)
            stack.enter_context(mock.patch.object(adapter, name, value))
        output = stack.enter_context(mock.patch.object(adapter, "json_out"))
        result = func(args)
        payload = output.call_args.args[0]
    return result, payload


class MutationReceiptTests(unittest.TestCase):
    def test_unexpected_mutation_error_does_not_claim_no_write(self) -> None:
        parser = mock.Mock()
        parser.parse_args.return_value = argparse.Namespace(
            command="replace_attachment",
            func=mock.Mock(side_effect=RuntimeError("unexpected")),
        )
        with (
            mock.patch.object(adapter, "build_parser", return_value=parser),
            mock.patch.object(adapter, "json_out") as output,
        ):
            exit_code = adapter.main(["replace_attachment"])
        payload = output.call_args.args[0]

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed_manual_repair_required")
        self.assertIsNone(payload["verification"]["write_performed"])
        self.assertEqual(
            payload["recovery"]["semantics"],
            "manual_inspection_required",
        )

    def test_cli_stale_private_mutation_emits_a_full_failure_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "reminders.sqlite"
            seed_catalog_fixture(database)
            with (
                mock.patch.object(adapter, "resolve_database", return_value=database),
                mock.patch.object(adapter, "json_out") as output,
            ):
                exit_code = adapter.main(
                    [
                        "remove_tag",
                        "--db",
                        str(database),
                        "--id",
                        REMINDER_ID,
                        "--tag",
                        "health",
                        "--if-version",
                        "2",
                    ]
                )
            payload = output.call_args.args[0]

        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertEqual(payload["operation"], "remove_tag")
        self.assertEqual(payload["error"]["code"], "concurrent_modification")
        self.assertIsInstance(payload["operation_id"], str)
        self.assertIsInstance(payload["backend"], str)
        for field in ("target", "after", "verification", "recovery"):
            self.assertIsInstance(payload[field], dict)

    def test_private_existing_item_mutations_reject_missing_version(self) -> None:
        cases = [
            (
                "update_reminder_db",
                adapter.cmd_update_reminder,
                argparse.Namespace(
                    db=None,
                    backend="db",
                    id=REMINDER_ID,
                    title=None,
                    list=None,
                    new_title="Guarded title",
                    notes=None,
                    flagged=None,
                    priority=None,
                    due_at=None,
                    remind_at=None,
                    all_day_due_date=None,
                    clear_due=False,
                    if_version=None,
                ),
            ),
            (
                "complete_reminder_db",
                adapter.cmd_complete_reminder,
                argparse.Namespace(
                    db=None,
                    backend="db",
                    id=REMINDER_ID,
                    title=None,
                    list=None,
                    if_version=None,
                ),
            ),
            (
                "reopen_reminder_db",
                adapter.cmd_reopen_reminder,
                argparse.Namespace(
                    db=None,
                    backend="db",
                    id=REMINDER_ID,
                    title=None,
                    list=None,
                    if_version=None,
                ),
            ),
            (
                "add_tag",
                adapter.cmd_add_tag,
                argparse.Namespace(
                    db=None,
                    id=REMINDER_ID,
                    title=None,
                    list=None,
                    tag="health",
                    if_version=None,
                ),
            ),
            (
                "remove_tag",
                adapter.cmd_remove_tag,
                argparse.Namespace(
                    db=None,
                    id=REMINDER_ID,
                    title=None,
                    list=None,
                    tag="health",
                    if_version=None,
                ),
            ),
            (
                "move_to_section",
                adapter.cmd_move_to_section,
                argparse.Namespace(
                    db=None,
                    id=REMINDER_ID,
                    title=None,
                    list=None,
                    section=None,
                    section_id=SECTION_B_ID,
                    if_version=None,
                ),
            ),
            (
                "attach_url",
                adapter.cmd_attach_url,
                argparse.Namespace(
                    db=None,
                    id=REMINDER_ID,
                    title=None,
                    list=None,
                    url="https://docs.example/item",
                    if_version=None,
                ),
            ),
            (
                "attach_image",
                adapter.cmd_attach_image,
                argparse.Namespace(
                    db=None,
                    backend="reminderkit",
                    id=REMINDER_ID,
                    title=None,
                    list=None,
                    image="/nonexistent/synthetic.png",
                    if_version=None,
                    idempotency_key=None,
                ),
            ),
            (
                "delete_attachment",
                adapter.cmd_delete_attachment,
                argparse.Namespace(
                    db=None,
                    id=REMINDER_ID,
                    title=None,
                    list=None,
                    attachment_id=ATTACHMENT_1_ID,
                    attachment_pk=None,
                    type=None,
                    filename=None,
                    url=None,
                    if_version=None,
                ),
            ),
            (
                "replace_attachment",
                adapter.cmd_replace_attachment,
                argparse.Namespace(
                    db=None,
                    id=REMINDER_ID,
                    title=None,
                    list=None,
                    attachment_id=ATTACHMENT_1_ID,
                    attachment_pk=None,
                    type=None,
                    filename=None,
                    old_url=None,
                    image=None,
                    url="https://docs.example/replacement",
                    if_version=None,
                    idempotency_key=None,
                ),
            ),
        ]

        for name, command, args in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                db = Path(temp_dir) / "reminders.sqlite"
                seed_catalog_fixture(db)
                with (
                    mock.patch.object(adapter, "resolve_database", return_value=db),
                    self.assertRaises(adapter.AdapterError) as raised,
                ):
                    command(args)

                con = sqlite3.connect(db)
                try:
                    version = con.execute(
                        "select Z_OPT from ZREMCDREMINDER where Z_PK=1"
                    ).fetchone()[0]
                finally:
                    con.close()

                self.assertEqual(raised.exception.code, "invalid_input")
                self.assertEqual(version, 1)

    def test_update_reminder_roundtrip_and_stale_version_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "reminders.sqlite"
            seed_catalog_fixture(db)

            args = argparse.Namespace(
                db=str(db),
                backend="db",
                id=REMINDER_ID,
                title=None,
                list=None,
                new_title="Updated title",
                notes="Updated notes",
                flagged=True,
                priority=5,
                due_at=None,
                remind_at=None,
                all_day_due_date=None,
                clear_due=False,
                if_version=1,
            )
            _, receipt = capture_json_output(
                adapter.cmd_update_reminder,
                args,
                patches=[
                    ("resolve_database", db),
                    ("log_action", None),
                    ("sync_reminder_text_applescript", lambda *_args, **_kwargs: None),
                ],
            )

            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                row = con.execute(
                    "select ZTITLE,ZNOTES,ZFLAGGED,ZPRIORITY,Z_OPT from ZREMCDREMINDER where Z_PK=1"
                ).fetchone()
            finally:
                con.close()

            stale_args = argparse.Namespace(**{**vars(args), "new_title": "Should not land", "if_version": 1})
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                self.assertRaises(adapter.AdapterError) as raised,
            ):
                adapter.cmd_update_reminder(stale_args)

        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["before"]["title"], "Original title")
        self.assertEqual(receipt["after"]["title"], "Updated title")
        self.assertEqual(receipt["after"]["flagged"], True)
        self.assertEqual(receipt["after"]["priority"], 5)
        self.assertEqual(row["ZTITLE"], "Updated title")
        self.assertEqual(row["ZNOTES"], "Updated notes")
        self.assertEqual(row["ZFLAGGED"], 1)
        self.assertEqual(row["ZPRIORITY"], 5)
        self.assertEqual(row["Z_OPT"], 2)
        self.assertEqual(raised.exception.code, "concurrent_modification")

    def test_complete_and_reopen_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "reminders.sqlite"
            seed_catalog_fixture(db)

            complete_args = argparse.Namespace(
                db=str(db),
                backend="db",
                id=REMINDER_ID,
                title=None,
                list=None,
                if_version=1,
            )
            _, complete_receipt = capture_json_output(
                adapter.cmd_complete_reminder,
                complete_args,
                patches=[("resolve_database", db), ("log_action", None)],
            )

            reopen_args = argparse.Namespace(
                db=str(db),
                backend="db",
                id=REMINDER_ID,
                title=None,
                list=None,
                if_version=2,
            )
            _, reopen_receipt = capture_json_output(
                adapter.cmd_reopen_reminder,
                reopen_args,
                patches=[("resolve_database", db), ("log_action", None)],
            )

            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                row = con.execute(
                    "select ZCOMPLETED,ZCOMPLETIONDATE,Z_OPT from ZREMCDREMINDER where Z_PK=1"
                ).fetchone()
            finally:
                con.close()

        self.assertEqual(complete_receipt["status"], "verified")
        self.assertTrue(complete_receipt["after"]["completed"])
        self.assertEqual(reopen_receipt["status"], "verified")
        self.assertFalse(reopen_receipt["after"]["completed"])
        self.assertEqual(row["ZCOMPLETED"], 0)
        self.assertIsNone(row["ZCOMPLETIONDATE"])
        self.assertEqual(row["Z_OPT"], 3)

    def test_add_and_remove_tag_roundtrip_and_rolls_back_on_failed_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "reminders.sqlite"
            seed_catalog_fixture(db)

            add_args = argparse.Namespace(
                db=str(db),
                id=REMINDER_ID,
                title=None,
                list=None,
                tag="health",
                if_version=1,
            )
            _, add_receipt = capture_json_output(
                adapter.cmd_add_tag,
                add_args,
                patches=[("resolve_database", db), ("log_action", None)],
            )

            remove_args = argparse.Namespace(
                db=str(db),
                id=REMINDER_ID,
                title=None,
                list=None,
                tag="health",
                if_version=2,
            )
            _, remove_receipt = capture_json_output(
                adapter.cmd_remove_tag,
                remove_args,
                patches=[("resolve_database", db), ("log_action", None)],
            )

            rollback_args = argparse.Namespace(
                db=str(db),
                id=REMINDER_ID,
                title=None,
                list=None,
                tag="triage",
                if_version=3,
            )
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "log_action", return_value=None),
                mock.patch.object(adapter, "reread_reminder", side_effect=adapter.AdapterError("boom")),
                self.assertRaises(adapter.AdapterError),
            ):
                adapter.cmd_add_tag(rollback_args)

            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                active_assignments = con.execute(
                    """
                    select count(*)
                    from ZREMCDOBJECT o
                    join ZREMCDHASHTAGLABEL l on l.Z_PK=o.ZHASHTAGLABEL
                    where lower(l.ZNAME)=? and coalesce(o.ZMARKEDFORDELETION,0)=0
                    """,
                    ("triage",),
                ).fetchone()[0]
                rolled_back_labels = con.execute(
                    "select count(*) from ZREMCDHASHTAGLABEL where lower(ZNAME)=?",
                    ("triage",),
                ).fetchone()[0]
            finally:
                con.close()

        self.assertEqual(add_receipt["status"], "verified")
        self.assertTrue(add_receipt["after"]["assignment_id"])
        self.assertEqual(remove_receipt["status"], "verified")
        self.assertEqual(remove_receipt["after"]["removed_assignment_ids"], [add_receipt["after"]["assignment_id"]])
        self.assertEqual(active_assignments, 0)
        self.assertEqual(rolled_back_labels, 0)

    def test_create_section_and_move_to_section_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "reminders.sqlite"
            seed_catalog_fixture(db)

            create_args = argparse.Namespace(
                db=str(db),
                list=None,
                list_id=LIST_BETA_ID,
                name="Section C",
            )
            _, create_receipt = capture_json_output(
                adapter.cmd_create_section,
                create_args,
                patches=[("resolve_database", db), ("log_action", None)],
            )
            section_id = create_receipt["after"]["section"]["id"]

            move_args = argparse.Namespace(
                db=str(db),
                id=REMINDER_ID,
                title=None,
                list=None,
                section=None,
                section_id=section_id,
                if_version=1,
            )
            _, move_receipt = capture_json_output(
                adapter.cmd_move_to_section,
                move_args,
                patches=[("resolve_database", db), ("log_action", None)],
            )

            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                row = con.execute(
                    "select ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA from ZREMCDBASELIST where Z_PK=11"
                ).fetchone()
            finally:
                con.close()

        self.assertEqual(create_receipt["status"], "committed_verification_pending")
        self.assertTrue(create_receipt["after"]["created"])
        self.assertEqual(move_receipt["status"], "committed_verification_pending")
        self.assertEqual(move_receipt["after"]["section_id"], section_id)
        self.assertEqual(adapter.membership_map(row["ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA"])[REMINDER_ID], section_id)

    def test_section_sqlite_write_does_not_claim_icloud_verified(self) -> None:
        """A local Core Data read-back is not evidence that a section reached iCloud."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "reminders.sqlite"
            seed_catalog_fixture(db)

            create_args = argparse.Namespace(
                db=str(db),
                list=None,
                list_id=LIST_BETA_ID,
                name="Section C",
            )
            _, create_receipt = capture_json_output(
                adapter.cmd_create_section,
                create_args,
                patches=[("resolve_database", db), ("log_action", None)],
            )
            section_id = create_receipt["after"]["section"]["id"]

            move_args = argparse.Namespace(
                db=str(db),
                id=REMINDER_ID,
                title=None,
                list=None,
                section=None,
                section_id=section_id,
                if_version=1,
            )
            _, move_receipt = capture_json_output(
                adapter.cmd_move_to_section,
                move_args,
                patches=[("resolve_database", db), ("log_action", None)],
            )

        self.assertEqual(create_receipt["status"], "committed_verification_pending")
        self.assertEqual(create_receipt["verification"]["icloud_sync"], "not_verified")
        self.assertEqual(move_receipt["status"], "committed_verification_pending")
        self.assertEqual(move_receipt["verification"]["icloud_sync"], "not_verified")

    def test_native_section_create_requires_cloud_readback_for_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "reminders.sqlite"
            seed_catalog_fixture(db)
            section_id = "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"

            def native_create(operation: str, list_id: str, name: str) -> dict[str, object]:
                self.assertEqual((operation, list_id, name), ("create", LIST_BETA_ID, "Section C"))
                con = sqlite3.connect(db)
                try:
                    insert_row(
                        con,
                        "ZREMCDBASESECTION",
                        Z_PK=41,
                        Z_ENT=6,
                        Z_OPT=2,
                        ZCKDIRTYFLAGS=0,
                        ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION=0,
                        ZMINIMUMSUPPORTEDAPPVERSION=0,
                        ZSPOTLIGHTINDEXCOUNT=0,
                        ZACCOUNT=100,
                        ZCKCLOUDSTATE=501,
                        ZLIST=11,
                        Z_FOK_LIST=3072,
                        ZCREATIONDATE=2.0,
                        ZCKIDENTIFIER=section_id,
                        ZDISPLAYNAME=name,
                        ZIDENTIFIER=sqlite3.Binary(adapter.uuid_blob(section_id)),
                        ZRESOLUTIONTOKENMAP_V3_JSONDATA=sqlite3.Binary(b"native-token"),
                        ZMARKEDFORDELETION=0,
                    )
                    insert_row(
                        con,
                        "ZREMCKCLOUDSTATE",
                        Z_PK=501,
                        Z_ENT=45,
                        Z_OPT=2,
                        ZCURRENTLOCALVERSION=1,
                        ZLATESTVERSIONSYNCEDTOCLOUD=1,
                        ZREMINDER=None,
                        ZLOCALVERSIONDATE=2.0,
                        ZSECTION=41,
                        Z5_SECTION=6,
                        ZOBJECT=None,
                        Z13_OBJECT=None,
                        ZINCLOUD=1,
                        ZCKSERVERRECORDDATA=None,
                    )
                    con.commit()
                finally:
                    con.close()
                return {"ok": True, "saved": True, "section_id": section_id}

            args = argparse.Namespace(
                db=None,
                list=None,
                list_id=LIST_BETA_ID,
                name="Section C",
            )
            _, receipt = capture_json_output(
                adapter.cmd_create_section,
                args,
                patches=[
                    ("resolve_database", db),
                    ("invoke_reminderkit_section", native_create),
                    ("log_action", None),
                ],
            )

        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["backend"], "reminderkit_private")
        self.assertEqual(receipt["after"]["section"]["id"], section_id)
        self.assertEqual(receipt["verification"]["icloud_sync"], "verified")
        self.assertTrue(receipt["verification"]["cloud"]["icloud_sync_verified"])

    def test_native_section_move_writes_membership_through_reminderkit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "reminders.sqlite"
            seed_catalog_fixture(db)

            def native_move(operation: str, reminder_id: str, section_id: str) -> dict[str, object]:
                self.assertEqual((operation, reminder_id, section_id), ("move", REMINDER_ID, SECTION_B_ID))
                con = sqlite3.connect(db)
                con.row_factory = sqlite3.Row
                try:
                    row = con.execute(
                        "select ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA from ZREMCDBASELIST where Z_PK=11"
                    ).fetchone()
                    mapping = adapter.membership_map(row["ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA"])
                    mapping[REMINDER_ID] = SECTION_B_ID
                    con.execute(
                        "update ZREMCDBASELIST set ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA=?,Z_OPT=Z_OPT+1 where Z_PK=11",
                        (adapter.membership_payload(mapping),),
                    )
                    con.execute(
                        "update ZREMCKCLOUDSTATE set ZCURRENTLOCALVERSION=2,ZLATESTVERSIONSYNCEDTOCLOUD=2,ZINCLOUD=1 where Z_PK=211"
                    )
                    con.commit()
                finally:
                    con.close()
                return {"ok": True, "saved": True}

            args = argparse.Namespace(
                db=None,
                id=REMINDER_ID,
                title=None,
                list=None,
                section=None,
                section_id=SECTION_B_ID,
                if_version=1,
            )
            _, receipt = capture_json_output(
                adapter.cmd_move_to_section,
                args,
                patches=[
                    ("resolve_database", db),
                    ("invoke_reminderkit_section", native_move),
                    ("log_action", None),
                ],
            )

        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["backend"], "reminderkit_private")
        self.assertEqual(receipt["after"]["section_id"], SECTION_B_ID)
        self.assertEqual(receipt["verification"]["icloud_sync"], "verified")

    def test_attach_url_and_delete_attachment_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "reminders.sqlite"
            seed_catalog_fixture(db)

            attach_args = argparse.Namespace(
                db=str(db),
                id=REMINDER_ID,
                title=None,
                list=None,
                url="https://docs.example/item",
                if_version=1,
            )
            _, attach_receipt = capture_json_output(
                adapter.cmd_attach_url,
                attach_args,
                patches=[("resolve_database", db), ("log_action", None)],
            )
            attachment_id = attach_receipt["after"]["attachment"]["id"]

            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                attached_row = con.execute(
                    "select Z_PK,ZCKCLOUDSTATE from ZREMCDOBJECT where ZCKIDENTIFIER=?",
                    (attachment_id,),
                ).fetchone()
            finally:
                con.close()
            self.assertIsNotNone(attached_row)
            attachment_pk = attached_row["Z_PK"]
            cloud_pk = attached_row["ZCKCLOUDSTATE"]

            delete_args = argparse.Namespace(
                db=str(db),
                id=REMINDER_ID,
                title=None,
                list=None,
                attachment_id=attachment_id,
                attachment_pk=None,
                type=None,
                filename=None,
                url=None,
                if_version=2,
            )
            _, delete_receipt = capture_json_output(
                adapter.cmd_delete_attachment,
                delete_args,
                patches=[("resolve_database", db), ("log_action", None)],
            )

            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                row = con.execute(
                    "select ZMARKEDFORDELETION,ZURL from ZREMCDOBJECT where ZCKIDENTIFIER=?",
                    (attachment_id,),
                ).fetchone()
                cloud_row = con.execute(
                    """
                    select ZCURRENTLOCALVERSION,ZOBJECT,Z13_OBJECT
                    from ZREMCKCLOUDSTATE where Z_PK=?
                    """,
                    (cloud_pk,),
                ).fetchone()
                reminder_version = con.execute(
                    "select Z_OPT from ZREMCDREMINDER where Z_PK=1"
                ).fetchone()[0]
            finally:
                con.close()

        self.assertEqual(attach_receipt["status"], "verified")
        self.assertEqual(attach_receipt["after"]["attachment"]["type"], "url")
        self.assertEqual(delete_receipt["status"], "verified")
        self.assertEqual(delete_receipt["recovery"]["semantics"], "reattach_url")
        self.assertIsNone(row)
        self.assertEqual(delete_receipt["verification"]["attachment_row_deleted"], True)
        self.assertEqual(
            delete_receipt["verification"]["cloud_state_tombstone_retained"],
            True,
        )
        self.assertEqual(cloud_row["ZCURRENTLOCALVERSION"], 2)
        self.assertEqual(cloud_row["ZOBJECT"], attachment_pk)
        self.assertEqual(cloud_row["Z13_OBJECT"], adapter.URL_ATTACHMENT_ENT)
        self.assertEqual(reminder_version, 3)

    def test_replace_url_deletes_old_row_and_preserves_display_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "reminders.sqlite"
            seed_catalog_fixture(db)
            args = argparse.Namespace(
                db=str(db),
                id=REMINDER_ID,
                title=None,
                list=None,
                attachment_id=ATTACHMENT_1_ID,
                attachment_pk=None,
                type=None,
                filename=None,
                old_url=None,
                image=None,
                url="https://replacement.example/path",
                if_version=1,
            )

            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "log_action", return_value=None),
            ):
                receipt = adapter.replace_attachment_once(args)

            new_attachment = receipt["after"]["new_attachment"]["attachment"]
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                old_row = con.execute(
                    "select Z_PK from ZREMCDOBJECT where ZCKIDENTIFIER=?",
                    (ATTACHMENT_1_ID,),
                ).fetchone()
                new_row = con.execute(
                    """
                    select Z_FOK_REMINDER1,ZURL,ZMARKEDFORDELETION
                    from ZREMCDOBJECT where ZCKIDENTIFIER=?
                    """,
                    (new_attachment["id"],),
                ).fetchone()
                old_cloud = con.execute(
                    """
                    select ZCURRENTLOCALVERSION,ZOBJECT,Z13_OBJECT
                    from ZREMCKCLOUDSTATE where Z_PK=330
                    """
                ).fetchone()
                reminder_version = con.execute(
                    "select Z_OPT from ZREMCDREMINDER where Z_PK=1"
                ).fetchone()[0]
            finally:
                con.close()

        self.assertEqual(receipt["status"], "verified")
        self.assertIsNone(old_row)
        self.assertEqual(new_row["Z_FOK_REMINDER1"], 1024)
        self.assertEqual(new_row["ZURL"], "https://replacement.example/path")
        self.assertEqual(new_row["ZMARKEDFORDELETION"], 0)
        self.assertEqual(old_cloud["ZCURRENTLOCALVERSION"], 2)
        self.assertEqual(old_cloud["ZOBJECT"], 30)
        self.assertEqual(old_cloud["Z13_OBJECT"], adapter.URL_ATTACHMENT_ENT)
        self.assertEqual(reminder_version, 3)
        self.assertTrue(receipt["verification"]["old_attachment_row_deleted"])
        self.assertTrue(receipt["verification"]["replacement_order_preserved"])
        self.assertTrue(receipt["verification"]["old_attachment_cloud_state_retained"])

    def test_replace_url_preserves_nullable_display_order_without_skipping_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "reminders.sqlite"
            seed_catalog_fixture(db)
            con = sqlite3.connect(db)
            try:
                con.execute(
                    "update ZREMCDOBJECT set Z_FOK_REMINDER1=NULL where ZCKIDENTIFIER=?",
                    (ATTACHMENT_1_ID,),
                )
                con.commit()
            finally:
                con.close()
            args = argparse.Namespace(
                db=str(db),
                id=REMINDER_ID,
                title=None,
                list=None,
                attachment_id=ATTACHMENT_1_ID,
                attachment_pk=None,
                type=None,
                filename=None,
                old_url=None,
                image=None,
                url="https://nullable-order.example/path",
                if_version=1,
            )

            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "log_action", return_value=None),
            ):
                receipt = adapter.replace_attachment_once(args)

            new_attachment = receipt["after"]["new_attachment"]["attachment"]
            con = sqlite3.connect(db)
            try:
                new_order = con.execute(
                    "select Z_FOK_REMINDER1 from ZREMCDOBJECT where ZCKIDENTIFIER=?",
                    (new_attachment["id"],),
                ).fetchone()[0]
            finally:
                con.close()

        self.assertEqual(receipt["status"], "verified")
        self.assertIsNone(new_order)
        self.assertTrue(receipt["verification"]["replacement_order_preserved"])

    def test_replace_url_rejects_an_already_attached_target_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "reminders.sqlite"
            seed_catalog_fixture(db)
            args = argparse.Namespace(
                db=str(db),
                id=REMINDER_ID,
                title=None,
                list=None,
                attachment_id=ATTACHMENT_1_ID,
                attachment_pk=None,
                type=None,
                filename=None,
                old_url=None,
                image=None,
                url="https://two.example/path",
                if_version=1,
            )
            replacement_connection = adapter.connect(db)
            real_normalized_url = adapter.normalized_url
            transaction_states: list[bool] = []

            def observe_transaction(value: str) -> str:
                transaction_states.append(replacement_connection.in_transaction)
                return real_normalized_url(value)

            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "connect", return_value=replacement_connection),
                mock.patch.object(adapter, "normalized_url", side_effect=observe_transaction),
                mock.patch.object(adapter, "log_action", return_value=None),
                self.assertRaises(adapter.AdapterError) as raised,
            ):
                adapter.replace_attachment_once(args)

            con = sqlite3.connect(db)
            try:
                active_rows = con.execute(
                    """
                    select ZCKIDENTIFIER,Z_FOK_REMINDER1
                    from ZREMCDOBJECT
                    where ZREMINDER2=1 and Z_ENT=? and coalesce(ZMARKEDFORDELETION,0)=0
                    order by Z_FOK_REMINDER1
                    """,
                    (adapter.URL_ATTACHMENT_ENT,),
                ).fetchall()
                reminder_version = con.execute(
                    "select Z_OPT from ZREMCDREMINDER where Z_PK=1"
                ).fetchone()[0]
            finally:
                con.close()

        self.assertEqual(raised.exception.code, "invalid_input")
        self.assertEqual(active_rows, [(ATTACHMENT_1_ID, 1024), (ATTACHMENT_2_ID, 2048)])
        self.assertEqual(reminder_version, 1)
        self.assertEqual(transaction_states, [True])

    def test_url_delete_rolls_back_when_cloud_state_tombstone_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "reminders.sqlite"
            seed_catalog_fixture(db)
            con = sqlite3.connect(db)
            try:
                con.execute("delete from ZREMCKCLOUDSTATE where Z_PK=330")
                con.commit()
            finally:
                con.close()

            args = argparse.Namespace(
                db=str(db),
                id=REMINDER_ID,
                title=None,
                list=None,
                attachment_id=ATTACHMENT_1_ID,
                attachment_pk=None,
                type=None,
                filename=None,
                url=None,
                if_version=1,
            )
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "log_action", return_value=None),
                self.assertRaises(adapter.AdapterError) as raised,
            ):
                adapter.cmd_delete_attachment(args)

            con = sqlite3.connect(db)
            try:
                object_count = con.execute(
                    "select count(*) from ZREMCDOBJECT where ZCKIDENTIFIER=?",
                    (ATTACHMENT_1_ID,),
                ).fetchone()[0]
                reminder_version = con.execute(
                    "select Z_OPT from ZREMCDREMINDER where Z_PK=1"
                ).fetchone()[0]
            finally:
                con.close()

        self.assertEqual(raised.exception.code, "schema_mismatch")
        self.assertEqual(object_count, 1)
        self.assertEqual(reminder_version, 1)

    def test_url_delete_rolls_back_when_cloud_state_owns_another_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "reminders.sqlite"
            seed_catalog_fixture(db)
            con = sqlite3.connect(db)
            try:
                con.execute(
                    "update ZREMCKCLOUDSTATE set ZOBJECT=31 where Z_PK=330"
                )
                con.commit()
            finally:
                con.close()

            args = argparse.Namespace(
                db=str(db),
                id=REMINDER_ID,
                title=None,
                list=None,
                attachment_id=ATTACHMENT_1_ID,
                attachment_pk=None,
                type=None,
                filename=None,
                url=None,
                if_version=1,
            )
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "log_action", return_value=None),
                self.assertRaises(adapter.AdapterError) as raised,
            ):
                adapter.cmd_delete_attachment(args)

            con = sqlite3.connect(db)
            try:
                object_count = con.execute(
                    "select count(*) from ZREMCDOBJECT where ZCKIDENTIFIER=?",
                    (ATTACHMENT_1_ID,),
                ).fetchone()[0]
                cloud = con.execute(
                    "select ZOBJECT,Z13_OBJECT,ZCURRENTLOCALVERSION from ZREMCKCLOUDSTATE where Z_PK=330"
                ).fetchone()
            finally:
                con.close()

        self.assertEqual(raised.exception.code, "schema_mismatch")
        self.assertEqual(object_count, 1)
        self.assertEqual(cloud, (31, adapter.URL_ATTACHMENT_ENT, 1))

    def test_bounded_reads_report_truncated_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "reminders.sqlite"
            seed_catalog_fixture(db)

            list_args = argparse.Namespace(db=str(db), limit=2)
            _, list_payload = capture_json_output(adapter.cmd_list_lists, list_args)
            _, list_exact_payload = capture_json_output(
                adapter.cmd_list_lists,
                argparse.Namespace(db=str(db), limit=3),
            )

            section_args = argparse.Namespace(db=str(db), list="Beta", limit=1)
            _, section_payload = capture_json_output(adapter.cmd_list_sections, section_args)
            _, section_exact_payload = capture_json_output(
                adapter.cmd_list_sections,
                argparse.Namespace(db=str(db), list="Beta", limit=2),
            )

            attachment_args = argparse.Namespace(db=str(db), id=REMINDER_ID, title=None, list=None, type="url", limit=1)
            _, attachment_payload = capture_json_output(adapter.cmd_list_attachments, attachment_args)
            _, attachment_exact_payload = capture_json_output(
                adapter.cmd_list_attachments,
                argparse.Namespace(db=str(db), id=REMINDER_ID, title=None, list=None, type="url", limit=2),
            )

        self.assertTrue(list_payload["truncated"])
        self.assertFalse(list_exact_payload["truncated"])
        self.assertTrue(section_payload["truncated"])
        self.assertFalse(section_exact_payload["truncated"])
        self.assertTrue(attachment_payload["truncated"])
        self.assertFalse(attachment_exact_payload["truncated"])
        self.assertIsInstance(list_payload["truncated"], bool)
        self.assertIsInstance(section_payload["truncated"], bool)
        self.assertIsInstance(attachment_payload["truncated"], bool)
        self.assertEqual(len(list_payload["lists"]), 2)
        self.assertEqual(len(section_payload["sections"]), 1)
        self.assertEqual(len(attachment_payload["attachments"]), 1)
        self.assertEqual(attachment_payload["reminder_version"], 1)

    def test_purge_logs_removes_current_and_rotated_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            support.mkdir(parents=True, exist_ok=True)
            journal = support / "actions.jsonl"
            rotated = support / "actions.jsonl.1"
            journal.write_text("current\n", encoding="utf-8")
            rotated.write_text("rotated\n", encoding="utf-8")

            with (
                mock.patch.object(adapter, "JOURNAL", journal),
                mock.patch.object(adapter, "json_out") as output,
            ):
                result = adapter.cmd_purge_logs(argparse.Namespace())

            payload = output.call_args.args[0]

        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "verified")
        self.assertEqual(sorted(payload["after"]["removed"]), ["actions.jsonl", "actions.jsonl.1"])
        self.assertEqual(payload["verification"]["remaining_files"], [])
        self.assertFalse(journal.exists())
        self.assertFalse(rotated.exists())

    def test_repair_preview_requires_digest_and_detects_digest_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "repair.sqlite"
            support, files_root, source = seed_repair_fixture(db)
            args = argparse.Namespace(
                db=str(db),
                search=None,
                list="Beta",
                limit=10,
                preview_digest=None,
                apply=False,
                no_backup=True,
            )

            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "FILES", files_root),
                mock.patch.object(adapter, "json_out") as output,
            ):
                adapter.cmd_repair_attachments(args)
            preview = output.call_args.args[0]

            source.unlink()
            apply_args = argparse.Namespace(
                **{**vars(args), "apply": True, "preview_digest": preview["target"]["candidate_digest"]}
            )
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "FILES", files_root),
                self.assertRaises(adapter.AdapterError) as missing_digest,
            ):
                adapter.cmd_repair_attachments(argparse.Namespace(**{**vars(args), "apply": True, "preview_digest": None}))

            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "FILES", files_root),
                self.assertRaises(adapter.AdapterError) as changed_digest,
            ):
                adapter.cmd_repair_attachments(apply_args)

        self.assertEqual(preview["status"], "unchanged")
        self.assertEqual(preview["verification"]["state"], "candidate_snapshot")
        self.assertEqual(missing_digest.exception.code, "ambiguous_scope")
        self.assertEqual(changed_digest.exception.code, "concurrent_modification")

    def test_repair_digest_rejects_reminder_version_drift_before_private_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "repair.sqlite"
            support, files_root, _ = seed_repair_fixture(db)
            args = argparse.Namespace(
                db=str(db),
                search=None,
                list="Beta",
                limit=10,
                preview_digest=None,
                apply=False,
                no_backup=True,
            )
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "FILES", files_root),
                mock.patch.object(adapter, "json_out") as output,
            ):
                adapter.cmd_repair_attachments(args)
            preview = output.call_args.args[0]

            con = sqlite3.connect(db)
            try:
                con.execute("update ZREMCDREMINDER set Z_OPT=2 where Z_PK=1")
                con.commit()
            finally:
                con.close()

            apply_args = argparse.Namespace(
                **{
                    **vars(args),
                    "apply": True,
                    "preview_digest": preview["target"]["candidate_digest"],
                }
            )
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "FILES", files_root),
                mock.patch.object(adapter, "attach_image_reminderkit_record") as attach,
                self.assertRaises(adapter.AdapterError) as raised,
            ):
                adapter.cmd_repair_attachments(apply_args)

        self.assertEqual(raised.exception.code, "concurrent_modification")
        attach.assert_not_called()

    def test_repair_batch_tracks_its_own_version_changes_for_same_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Path(temp_dir) / "repair.sqlite"
            support, files_root, _ = seed_repair_fixture(db)
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            try:
                first = con.execute(
                    "select * from ZREMCDOBJECT where ZCKIDENTIFIER='REPAIR-ATTACH'"
                ).fetchone()
                second = dict(first)
                second.update(
                    {
                        "Z_PK": 31,
                        "ZCKIDENTIFIER": "REPAIR-ATTACH-2",
                        "ZCKCLOUDSTATE": 331,
                        "Z_FOK_REMINDER1": 2048,
                    }
                )
                insert_row(con, "ZREMCDOBJECT", **second)
                insert_row(
                    con,
                    "ZREMCKCLOUDSTATE",
                    Z_PK=331,
                    Z_ENT=45,
                    Z_OPT=1,
                    ZCURRENTLOCALVERSION=1,
                    ZLATESTVERSIONSYNCEDTOCLOUD=0,
                    ZREMINDER=None,
                    ZLOCALVERSIONDATE=1.0,
                    ZSECTION=None,
                    Z5_SECTION=None,
                    ZOBJECT=31,
                    Z13_OBJECT=25,
                    ZINCLOUD=0,
                    ZCKSERVERRECORDDATA=None,
                )
                con.commit()
            finally:
                con.close()

            preview_args = argparse.Namespace(
                db=str(db),
                search=None,
                list="Beta",
                limit=10,
                preview_digest=None,
                apply=False,
                no_backup=True,
            )
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "FILES", files_root),
                mock.patch.object(adapter, "json_out") as output,
            ):
                adapter.cmd_repair_attachments(preview_args)
            preview = output.call_args.args[0]
            apply_args = argparse.Namespace(
                **{
                    **vars(preview_args),
                    "apply": True,
                    "preview_digest": preview["target"]["candidate_digest"],
                }
            )
            replacements = (
                {
                    "attachment": {"pk": 40, "id": "REPAIRED-1"},
                    "sync": {"mobile_visible_likely": True},
                },
                {
                    "attachment": {"pk": 41, "id": "REPAIRED-2"},
                    "sync": {"mobile_visible_likely": True},
                },
            )
            with (
                mock.patch.object(adapter, "resolve_database", return_value=db),
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "FILES", files_root),
                mock.patch.object(
                    adapter,
                    "attach_image_reminderkit_record",
                    side_effect=replacements,
                ) as attach,
                mock.patch.object(adapter, "log_action", return_value=None),
                mock.patch.object(adapter, "json_out") as output,
            ):
                exit_code = adapter.cmd_repair_attachments(apply_args)
            receipt = output.call_args.args[0]
            con = sqlite3.connect(db)
            try:
                states = con.execute(
                    "select ZCKIDENTIFIER,ZMARKEDFORDELETION from ZREMCDOBJECT "
                    "where ZCKIDENTIFIER like 'REPAIR-ATTACH%' order by ZCKIDENTIFIER"
                ).fetchall()
            finally:
                con.close()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["after"]["counts"]["repaired"], 2)
        self.assertEqual(receipt["after"]["counts"]["failed"], 0)
        self.assertEqual([row[1] for row in states], [1, 1])
        self.assertEqual(attach.call_count, 2)

    def test_main_maps_partial_failure_to_manual_repair_or_compensated_status(self) -> None:
        parser = mock.Mock()
        manual_args = argparse.Namespace(
            func=mock.Mock(
                side_effect=adapter.AdapterError(
                    "Replacement failed",
                    partial_failure=True,
                    compensated=False,
                    new_attachment_id="A-1",
                )
            )
        )
        parser.parse_args.return_value = manual_args
        with (
            mock.patch.object(adapter, "build_parser", return_value=parser),
            mock.patch.object(adapter, "json_out") as output,
        ):
            manual_code = adapter.main(["replace_attachment"])
        manual_payload = output.call_args.args[0]

        compensated_args = argparse.Namespace(
            func=mock.Mock(
                side_effect=adapter.AdapterError(
                    "Replacement failed",
                    partial_failure=True,
                    compensated=True,
                    new_attachment_id="A-2",
                )
            )
        )
        parser.parse_args.return_value = compensated_args
        with (
            mock.patch.object(adapter, "build_parser", return_value=parser),
            mock.patch.object(adapter, "json_out") as output2,
        ):
            compensated_code = adapter.main(["replace_attachment"])
        compensated_payload = output2.call_args.args[0]

        self.assertEqual(manual_code, 1)
        self.assertEqual(manual_payload["status"], "failed_manual_repair_required")
        self.assertEqual(manual_payload["error"]["new_attachment_id"], "A-1")
        self.assertEqual(compensated_code, 1)
        self.assertEqual(compensated_payload["status"], "failed_no_mutation")
        self.assertEqual(compensated_payload["error"]["new_attachment_id"], "A-2")
