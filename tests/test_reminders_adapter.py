from __future__ import annotations

import gzip
import importlib.util
import sqlite3
import tempfile
import unittest
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
                    ZMARKEDFORDELETION integer,
                    Z_FOK_LIST integer
                );
                create table ZREMCDOBJECT (
                    ZREMINDER2 integer,
                    Z_ENT integer,
                    ZMARKEDFORDELETION integer
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
                    ZDISPLAYDATEDATE,ZCOMPLETIONDATE,ZMARKEDFORDELETION,Z_FOK_LIST
                ) values (1,'REM-1','Pay bill','Sensitive body text',1,0,1,9,1,2,3,4,null,0,1024)
                """
            )
            con.execute(
                "insert into ZREMCDOBJECT (ZREMINDER2,Z_ENT,ZMARKEDFORDELETION) values (1,25,0)"
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
        self.assertEqual(reminder["section"], "This Week")
        self.assertEqual(reminder["attachment_count"], 1)
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

    def test_notes_metadata_never_returns_full_notes(self) -> None:
        notes = "Private notes that should not be cached verbatim"

        metadata = reminders_adapter.cache_notes_metadata(notes)

        self.assertEqual(metadata["notes_length"], len(notes))
        self.assertIn("notes_sha256", metadata)
        self.assertNotIn(notes, metadata.values())


if __name__ == "__main__":
    unittest.main()
