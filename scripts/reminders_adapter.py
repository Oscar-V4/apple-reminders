#!/usr/bin/env python3
"""Dependency-light Apple Reminders adapter.

This is the core local adapter. It is intentionally a JSON CLI first; an MCP
server can wrap these commands later without owning the business logic.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any


HOME = Path.home()
GROUP = HOME / "Library/Group Containers/group.com.apple.reminders/Container_v1"
STORES = GROUP / "Stores"
FILES = GROUP / "Files"
APP_SUPPORT = HOME / "Library/Application Support/apple-reminders-codex"
JOURNAL = APP_SUPPORT / "actions.jsonl"
CACHE_DIR = HOME / "Library/Caches/apple-reminders-codex"
CACHE_FILE = CACHE_DIR / "cache.json"
CACHE_VERSION = 1
APPLE_EPOCH_OFFSET = 978307200
IMAGE_ATTACHMENT_ENT = 25
URL_ATTACHMENT_ENT = 26
TAG_OBJECT_ENT = 32

REQUIRED_TABLES = {
    "ZREMCDREMINDER",
    "ZREMCDBASELIST",
    "ZREMCDBASESECTION",
    "ZREMCDHASHTAGLABEL",
    "ZREMCDOBJECT",
    "ZREMCKCLOUDSTATE",
    "Z_PRIMARYKEY",
}


class AdapterError(RuntimeError):
    pass


def json_out(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def fail(message: str, **extra: Any) -> int:
    json_out({"ok": False, "error": message, **extra})
    return 1


def core_now() -> float:
    return time.time() - APPLE_EPOCH_OFFSET


def core_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        timestamp = float(value) + APPLE_EPOCH_OFFSET
    except (TypeError, ValueError):
        return None
    return dt.datetime.fromtimestamp(timestamp).astimezone().isoformat()


def local_timezone_name() -> str:
    try:
        resolved = Path("/etc/localtime").resolve()
        parts = resolved.parts
        if "zoneinfo" in parts:
            idx = parts.index("zoneinfo")
            name = "/".join(parts[idx + 1 :])
            if name:
                return name
    except OSError:
        pass
    return time.tzname[0] if time.tzname else "UTC"


def parse_local_datetime(value: str) -> dt.datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AdapterError(f"Invalid datetime: {value}. Use ISO format like 2026-07-03T14:30:00+09:00.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return parsed.astimezone()


def core_from_datetime(value: dt.datetime) -> float:
    return value.timestamp() - APPLE_EPOCH_OFFSET


def schedule_values(
    due_at: str | None = None,
    remind_at: str | None = None,
    all_day_due_date: str | None = None,
    clear_due: bool = False,
) -> dict[str, Any] | None:
    provided = [item is not None for item in (due_at, remind_at, all_day_due_date)].count(True)
    if clear_due and provided:
        raise AdapterError("--clear-due cannot be combined with due date options")
    if provided > 1:
        raise AdapterError("Use only one of --due-at, --remind-at, or --all-day-due-date")
    if clear_due:
        return {
            "ZALLDAY": 0,
            "ZDISPLAYDATEISALLDAY": 0,
            "ZDUEDATE": None,
            "ZDISPLAYDATEDATE": None,
            "ZTIMEZONE": None,
            "ZDISPLAYDATETIMEZONE": None,
        }
    if all_day_due_date is not None:
        try:
            day = dt.date.fromisoformat(all_day_due_date.strip())
        except ValueError as exc:
            raise AdapterError(f"Invalid all-day date: {all_day_due_date}. Use YYYY-MM-DD.") from exc
        local_midnight = dt.datetime.combine(
            day,
            dt.time.min,
            tzinfo=dt.datetime.now().astimezone().tzinfo,
        )
        utc_midnight = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
        return {
            "ZALLDAY": 1,
            "ZDISPLAYDATEISALLDAY": 1,
            "ZDUEDATE": core_from_datetime(utc_midnight),
            "ZDISPLAYDATEDATE": core_from_datetime(local_midnight),
            "ZTIMEZONE": None,
            "ZDISPLAYDATETIMEZONE": None,
        }
    timestamp_text = due_at if due_at is not None else remind_at
    if timestamp_text is not None:
        parsed = parse_local_datetime(timestamp_text)
        core_value = core_from_datetime(parsed)
        timezone_name = local_timezone_name()
        return {
            "ZALLDAY": 0,
            "ZDISPLAYDATEISALLDAY": 0,
            "ZDUEDATE": core_value,
            "ZDISPLAYDATEDATE": core_value,
            "ZTIMEZONE": timezone_name,
            "ZDISPLAYDATETIMEZONE": timezone_name,
        }
    return None


def normalized_url(value: str) -> str:
    url = value.strip()
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        raise AdapterError(f"Invalid URL: {value}. Include a scheme such as https://.")
    if parsed.scheme in {"http", "https"} and not parsed.netloc:
        raise AdapterError(f"Invalid URL: {value}. Include a host.")
    return url


def normalized_tag_name(value: str) -> str:
    tag = value.strip()
    while tag.startswith("#"):
        tag = tag[1:].strip()
    if not tag:
        raise AdapterError("Tag name is required")
    return tag


def canonical_tag_name(value: str) -> str:
    return normalized_tag_name(value).casefold()


def normalize_uuid(value: str) -> str:
    if value.startswith("x-apple-reminder://"):
        value = value.removeprefix("x-apple-reminder://")
    return str(uuid.UUID(value)).upper()


def reminder_url(value: str) -> str:
    return f"x-apple-reminder://{normalize_uuid(value)}"


def uuid_blob(value: str) -> bytes:
    return uuid.UUID(value).bytes


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def length_field(tag: int, payload: bytes) -> bytes:
    return bytes([tag]) + varint(len(payload)) + payload


def reminder_text_document(text: str) -> bytes:
    text_bytes = text.encode("utf-8")
    n = len(text_bytes)
    inner = (
        length_field(0x12, text_bytes)
        + b"\x1a\x10\x0a\x04\x08\x00\x10\x00\x10\x00\x1a\x04\x08\x00\x10\x00\x28\x01"
        + b"\x1a\x10\x0a\x04\x08\x01\x10\x00\x10"
        + varint(n)
        + b"\x1a\x04\x08\x01\x10\x00\x28\x02"
        + b"\x1a\x16\x0a\x08\x08\x00\x10\xff\xff\xff\xff\x0f\x10\x00\x1a\x08\x08\x00\x10\xff\xff\xff\xff\x0f"
        + b"\x22\x1c\x0a\x1a\x0a\x10"
        + os.urandom(16)
        + b"\x12\x02\x08"
        + varint(n)
        + b"\x12\x02\x08\x01\x2a\x02\x08"
        + varint(n)
    )
    raw = b"\x08\x00" + length_field(0x12, b"\x08\x00\x10\x00" + length_field(0x1A, inner))
    return gzip.compress(raw, mtime=0)


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("pragma busy_timeout=5000")
    return con


def table_names(con: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in con.execute("select name from sqlite_master where type='table'")
    }


def usable_dbs() -> list[Path]:
    paths: list[Path] = []
    for db in sorted(STORES.glob("*.sqlite")):
        try:
            con = connect(db)
            try:
                if REQUIRED_TABLES <= table_names(con):
                    paths.append(db)
            finally:
                con.close()
        except sqlite3.Error:
            continue
    return paths


def db_counts(db: Path) -> dict[str, int]:
    con = connect(db)
    try:
        return {
            "lists": con.execute(
                "select count(*) from ZREMCDBASELIST where coalesce(ZMARKEDFORDELETION,0)=0"
            ).fetchone()[0],
            "sections": con.execute(
                "select count(*) from ZREMCDBASESECTION where coalesce(ZMARKEDFORDELETION,0)=0"
            ).fetchone()[0],
            "reminders": con.execute(
                "select count(*) from ZREMCDREMINDER where coalesce(ZMARKEDFORDELETION,0)=0"
            ).fetchone()[0],
            "image_attachments": con.execute(
                "select count(*) from ZREMCDOBJECT where Z_ENT=25 and coalesce(ZMARKEDFORDELETION,0)=0"
            ).fetchone()[0],
            "url_attachments": con.execute(
                "select count(*) from ZREMCDOBJECT where Z_ENT=26 and coalesce(ZMARKEDFORDELETION,0)=0"
            ).fetchone()[0],
            "tag_labels": con.execute("select count(*) from ZREMCDHASHTAGLABEL").fetchone()[0],
            "tag_assignments": con.execute(
                "select count(*) from ZREMCDOBJECT where Z_ENT=? and coalesce(ZMARKEDFORDELETION,0)=0",
                (TAG_OBJECT_ENT,),
            ).fetchone()[0],
        }
    finally:
        con.close()


def main_db() -> Path:
    dbs = usable_dbs()
    if not dbs:
        raise AdapterError("No usable Reminders databases found")
    return max(dbs, key=lambda path: (db_counts(path)["reminders"], db_counts(path)["lists"]))


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def log_action(action: str, payload: dict[str, Any]) -> None:
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    entry = {
        "time": dt.datetime.now().astimezone().isoformat(),
        "action": action,
        "payload": payload,
    }
    with JOURNAL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def run_osascript(script: str, args: list[str]) -> str:
    proc = subprocess.run(
        ["osascript", "-e", script, *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise AdapterError(proc.stderr.strip() or proc.stdout.strip() or "osascript failed")
    return proc.stdout.strip()


def sync_reminder_text_applescript(reminder_id: str, title: str | None = None, notes: str | None = None) -> str | None:
    if title is None and notes is None:
        return None
    script = """
on run argv
  set reminderID to item 1 of argv
  set newTitle to item 2 of argv
  set newBody to item 3 of argv
  tell application "Reminders"
    set targetReminder to reminder id reminderID
    if newTitle is not "__NO_CHANGE__" then set name of targetReminder to newTitle
    if newBody is not "__NO_CHANGE__" then set body of targetReminder to newBody
    return id of targetReminder
  end tell
end run
"""
    return run_osascript(
        script,
        [
            reminder_id,
            title if title is not None else "__NO_CHANGE__",
            notes if notes is not None else "__NO_CHANGE__",
        ],
    )


def find_list(con: sqlite3.Connection, name: str | None = None, list_id: str | None = None) -> dict[str, Any]:
    if list_id:
        uid = normalize_uuid(list_id)
        row = con.execute(
            """
            select * from ZREMCDBASELIST
            where ZCKIDENTIFIER=? and coalesce(ZMARKEDFORDELETION,0)=0
            """,
            (uid,),
        ).fetchone()
        if not row:
            raise AdapterError(f"List not found: {list_id}")
        return dict(row)
    if not name:
        raise AdapterError("list name or id is required")
    rows = con.execute(
        """
        select * from ZREMCDBASELIST
        where ZNAME=? and coalesce(ZMARKEDFORDELETION,0)=0
        order by Z_PK
        """,
        (name,),
    ).fetchall()
    if not rows:
        raise AdapterError(f"List not found: {name}")
    if len(rows) > 1:
        raise AdapterError(f"Multiple lists named {name}; use an id")
    return dict(rows[0])


def find_section(
    con: sqlite3.Connection,
    list_pk: int,
    name: str | None = None,
    section_id: str | None = None,
) -> dict[str, Any]:
    if section_id:
        uid = normalize_uuid(section_id)
        row = con.execute(
            """
            select * from ZREMCDBASESECTION
            where ZLIST=? and ZCKIDENTIFIER=? and coalesce(ZMARKEDFORDELETION,0)=0
            """,
            (list_pk, uid),
        ).fetchone()
        if not row:
            raise AdapterError(f"Section not found: {section_id}")
        return dict(row)
    if not name:
        raise AdapterError("section name or id is required")
    rows = con.execute(
        """
        select * from ZREMCDBASESECTION
        where ZLIST=? and ZDISPLAYNAME=? and coalesce(ZMARKEDFORDELETION,0)=0
        order by Z_PK
        """,
        (list_pk, name),
    ).fetchall()
    if not rows:
        raise AdapterError(f"Section not found: {name}")
    if len(rows) > 1:
        raise AdapterError(f"Multiple sections named {name}; use an id")
    return dict(rows[0])


def find_reminder(
    con: sqlite3.Connection,
    reminder_id: str | None = None,
    title: str | None = None,
    list_name: str | None = None,
) -> dict[str, Any]:
    params: list[Any] = []
    where = ["coalesce(r.ZMARKEDFORDELETION,0)=0"]
    if reminder_id:
        where.append("r.ZCKIDENTIFIER=?")
        params.append(normalize_uuid(reminder_id))
    if title:
        where.append("r.ZTITLE=?")
        params.append(title)
    if list_name:
        where.append("l.ZNAME=?")
        params.append(list_name)
    rows = con.execute(
        f"""
        select r.*, l.ZNAME as LIST_NAME
        from ZREMCDREMINDER r
        left join ZREMCDBASELIST l on l.Z_PK=r.ZLIST
        where {" and ".join(where)}
        order by r.ZLASTMODIFIEDDATE desc, r.Z_PK desc
        """,
        params,
    ).fetchall()
    if not rows:
        raise AdapterError("Reminder not found")
    if len(rows) > 1 and not reminder_id:
        candidates = [
            {
                "id": row["ZCKIDENTIFIER"],
                "title": row["ZTITLE"],
                "list": row["LIST_NAME"],
                "completed": bool(row["ZCOMPLETED"]),
            }
            for row in rows[:10]
        ]
        raise AdapterError(f"Multiple reminders matched; use an id. Candidates: {candidates}")
    return dict(rows[0])


def account_identifier(con: sqlite3.Connection, account_pk: int | None) -> str | None:
    if account_pk is None:
        return None
    row = con.execute(
        """
        select ZCKIDENTIFIER
        from ZREMCDOBJECT
        where Z_PK=? and Z_ENT=14 and coalesce(ZMARKEDFORDELETION,0)=0
        """,
        (account_pk,),
    ).fetchone()
    return row["ZCKIDENTIFIER"] if row else None


def tag_label_payload(row: sqlite3.Row | dict[str, Any], active_count: int | None = None) -> dict[str, Any]:
    data = dict(row)
    raw_uuid = data.get("ZUUIDFORCHANGETRACKING")
    label_uuid = None
    if raw_uuid:
        try:
            label_uuid = str(uuid.UUID(bytes=bytes(raw_uuid))).upper()
        except (TypeError, ValueError):
            label_uuid = None
    payload = {
        "pk": data["Z_PK"],
        "uuid": label_uuid,
        "name": data["ZNAME"],
        "canonical_name": data["ZCANONICALNAME"],
        "account_identifier": data["ZACCOUNTIDENTIFIER"],
        "first_seen_at": core_to_iso(data["ZFIRSTOCCURRENCECREATIONDATE"]),
        "recency_at": core_to_iso(data["ZRECENCYDATE"]),
    }
    if active_count is not None:
        payload["active_count"] = active_count
    return payload


def find_tag_label(
    con: sqlite3.Connection,
    tag: str,
    account_id: str | None = None,
) -> dict[str, Any] | None:
    canonical = canonical_tag_name(tag)
    params: list[Any] = [canonical]
    where = ["lower(ZCANONICALNAME)=?"]
    if account_id:
        where.append("(ZACCOUNTIDENTIFIER=? or ZACCOUNTIDENTIFIER is null)")
        params.append(account_id)
    rows = con.execute(
        f"""
        select *
        from ZREMCDHASHTAGLABEL
        where {" and ".join(where)}
        order by case when ZACCOUNTIDENTIFIER=? then 0 else 1 end, Z_PK
        """,
        [*params, account_id or ""],
    ).fetchall()
    return dict(rows[0]) if rows else None


def create_tag_label(
    con: sqlite3.Connection,
    tag: str,
    account_id: str | None,
    now: float,
) -> dict[str, Any]:
    name = normalized_tag_name(tag)
    label_id = uuid.uuid4()
    label_pk = con.execute("select Z_MAX + 1 from Z_PRIMARYKEY where Z_ENT=11").fetchone()[0]
    con.execute(
        """
        insert into ZREMCDHASHTAGLABEL (
          Z_PK,Z_ENT,Z_OPT,ZFIRSTOCCURRENCECREATIONDATE,ZRECENCYDATE,
          ZACCOUNTIDENTIFIER,ZCANONICALNAME,ZNAME,ZUUIDFORCHANGETRACKING
        ) values (?,11,1,?,?,?,?,?,?)
        """,
        (
            label_pk,
            now,
            now,
            account_id,
            canonical_tag_name(name),
            name,
            sqlite3.Binary(label_id.bytes),
        ),
    )
    update_primary_key(con, 11, label_pk)
    row = con.execute("select * from ZREMCDHASHTAGLABEL where Z_PK=?", (label_pk,)).fetchone()
    if not row:
        raise AdapterError("Created tag label could not be read back")
    return dict(row)


def find_or_create_tag_label(
    con: sqlite3.Connection,
    tag: str,
    account_id: str | None,
    now: float,
) -> tuple[dict[str, Any], bool]:
    existing = find_tag_label(con, tag, account_id=account_id)
    if existing:
        return existing, False
    return create_tag_label(con, tag, account_id, now), True


def reminder_tag_rows(con: sqlite3.Connection, reminder_pk: int) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        select o.Z_PK as object_pk,o.ZCKIDENTIFIER as object_id,o.ZMARKEDFORDELETION,
               l.*
        from ZREMCDOBJECT o
        join ZREMCDHASHTAGLABEL l on l.Z_PK=o.ZHASHTAGLABEL
        where o.ZREMINDER3=? and o.Z_ENT=? and coalesce(o.ZMARKEDFORDELETION,0)=0
        order by lower(l.ZNAME), o.Z_PK
        """,
        (reminder_pk, TAG_OBJECT_ENT),
    ).fetchall()
    return [dict(row) for row in rows]


def tag_assignment_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "object_pk": row["object_pk"],
        "object_id": row["object_id"],
        "label": tag_label_payload(row),
    }


def touch_reminder(con: sqlite3.Connection, reminder: dict[str, Any], now: float) -> None:
    con.execute(
        "update ZREMCDREMINDER set Z_OPT=coalesce(Z_OPT,0)+1,ZLASTMODIFIEDDATE=? where Z_PK=?",
        (now, reminder["Z_PK"]),
    )
    bump_cloud_state(con, reminder.get("ZCKCLOUDSTATE"), now)


def attachment_type_for_ent(ent: int) -> str:
    if ent == IMAGE_ATTACHMENT_ENT:
        return "image"
    if ent == URL_ATTACHMENT_ENT:
        return "url"
    return f"ent:{ent}"


def attachment_ent_for_type(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.casefold()
    if normalized == "image":
        return IMAGE_ATTACHMENT_ENT
    if normalized == "url":
        return URL_ATTACHMENT_ENT
    raise AdapterError("Attachment type must be image or url")


def attachment_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    ent = int(data["Z_ENT"])
    payload = {
        "pk": data["Z_PK"],
        "id": data["ZCKIDENTIFIER"],
        "type": attachment_type_for_ent(ent),
        "uti": data["ZUTI"],
        "order": data["Z_FOK_REMINDER1"],
        "marked_for_deletion": bool(data["ZMARKEDFORDELETION"]),
    }
    if ent == IMAGE_ATTACHMENT_ENT:
        payload.update(
            {
                "filename": data["ZFILENAME"],
                "sha512": data["ZSHA512SUM"],
                "file_size": data["ZFILESIZE"],
                "width": data["ZWIDTH"],
                "height": data["ZHEIGHT"],
            }
        )
    if ent == URL_ATTACHMENT_ENT:
        payload.update({"url": data["ZURL"], "host_url": data["ZHOSTURL"]})
    return payload


def active_attachment_rows(
    con: sqlite3.Connection,
    reminder_pk: int,
    attachment_ent: int | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = [reminder_pk, IMAGE_ATTACHMENT_ENT, URL_ATTACHMENT_ENT]
    where = [
        "ZREMINDER2=?",
        "Z_ENT in (?,?)",
        "coalesce(ZMARKEDFORDELETION,0)=0",
    ]
    if attachment_ent is not None:
        where.append("Z_ENT=?")
        params.append(attachment_ent)
    rows = con.execute(
        f"""
        select Z_PK,Z_ENT,ZCKIDENTIFIER,ZCKCLOUDSTATE,ZREMINDER2,Z_FOK_REMINDER1,
               ZFILENAME,ZSHA512SUM,ZUTI,ZFILESIZE,ZWIDTH,ZHEIGHT,ZURL,ZHOSTURL,
               ZMARKEDFORDELETION
        from ZREMCDOBJECT
        where {" and ".join(where)}
        order by Z_FOK_REMINDER1, Z_PK
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def resolve_attachment_selection(
    con: sqlite3.Connection,
    reminder: dict[str, Any],
    attachment_id: str | None = None,
    attachment_pk: int | None = None,
    attachment_type: str | None = None,
    filename: str | None = None,
    url: str | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    ent = attachment_ent_for_type(attachment_type)
    rows = active_attachment_rows(con, reminder["Z_PK"], attachment_ent=ent)
    if attachment_id:
        wanted = normalize_uuid(attachment_id)
        matches = [row for row in rows if row["ZCKIDENTIFIER"] == wanted]
    elif attachment_pk is not None:
        matches = [row for row in rows if int(row["Z_PK"]) == int(attachment_pk)]
    elif filename:
        matches = [row for row in rows if row["Z_ENT"] == IMAGE_ATTACHMENT_ENT and row["ZFILENAME"] == filename]
    elif url:
        matches = [row for row in rows if row["Z_ENT"] == URL_ATTACHMENT_ENT and row["ZURL"] == normalized_url(url)]
    elif ent is not None and len(rows) == 1:
        matches = rows
    else:
        reason = "attachment selector is required"
        if ent is not None and len(rows) > 1:
            reason = f"multiple {attachment_type} attachments matched; use an attachment id"
        if ent is not None and not rows:
            reason = f"no active {attachment_type} attachments found"
        return None, [attachment_payload(row) for row in rows], reason
    if not matches:
        return None, [attachment_payload(row) for row in rows], "no attachment matched selector"
    if len(matches) > 1:
        return None, [attachment_payload(row) for row in matches], "multiple attachments matched selector"
    return matches[0], [attachment_payload(row) for row in rows], None


def attachment_dir_for_account(account_uuid: str | None = None) -> Path:
    if account_uuid:
        candidate = FILES / f"Account-{account_uuid}" / "Attachments"
        if candidate.exists():
            return candidate
    matches = sorted(FILES.glob("Account-*/Attachments"))
    if not matches:
        raise AdapterError("No Reminders attachment directory found")
    if len(matches) == 1:
        return matches[0]
    # Prefer the directory with existing files; it is usually the active iCloud account.
    return max(matches, key=lambda p: len(list(p.iterdir())))


def image_size(path: Path) -> tuple[int, int]:
    proc = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise AdapterError(proc.stderr.strip() or "Unable to read image dimensions")
    width = height = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("pixelWidth:"):
            width = int(line.split(":", 1)[1].strip())
        if line.startswith("pixelHeight:"):
            height = int(line.split(":", 1)[1].strip())
    if width is None or height is None:
        raise AdapterError("Unable to parse image dimensions")
    return width, height


def membership_map(raw: str | bytes | None) -> dict[str, str]:
    if not raw:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    result: dict[str, str] = {}
    for item in payload.get("memberships", []):
        member = item.get("memberID")
        group = item.get("groupID")
        if member and group:
            result[member.upper()] = group.upper()
    return result


def membership_payload(mapping: dict[str, str]) -> str:
    now = core_now()
    return json.dumps(
        {
            "minimumSupportedVersion": 20230430,
            "memberships": [
                {"memberID": member, "groupID": group, "modifiedOn": now}
                for member, group in sorted(mapping.items())
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def reminder_order(raw: str | bytes | None) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [str(item).upper() for item in payload if item]


def reminder_order_payload(order: list[str]) -> str:
    seen: set[str] = set()
    unique: list[str] = []
    for item in order:
        uid = item.upper()
        if uid not in seen:
            unique.append(uid)
            seen.add(uid)
    return json.dumps(unique, separators=(",", ":"))


def resolution_map(keys: list[str], now: float) -> str:
    return json.dumps(
        {
            "map": {
                key: {
                    "counter": 1,
                    "modificationTime": now,
                    "replicaID": str(uuid.uuid4()).upper(),
                }
                for key in keys
            }
        },
        separators=(",", ":"),
    )


def update_primary_key(con: sqlite3.Connection, ent: int, new_max: int) -> None:
    con.execute("update Z_PRIMARYKEY set Z_MAX=max(Z_MAX, ?) where Z_ENT=?", (new_max, ent))


def bump_cloud_state(con: sqlite3.Connection, cloud_pk: int | None, now: float) -> None:
    if cloud_pk is None:
        return
    con.execute(
        """
        update ZREMCKCLOUDSTATE
        set Z_OPT=coalesce(Z_OPT,0)+1,
            ZCURRENTLOCALVERSION=coalesce(ZCURRENTLOCALVERSION,0)+1,
            ZLOCALVERSIONDATE=?
        where Z_PK=?
        """,
        (now, cloud_pk),
    )


def update_list_order(con: sqlite3.Connection, list_row: dict[str, Any], reminder_id: str, add: bool, now: float) -> None:
    order = reminder_order(list_row.get("ZREMINDERIDSMERGEABLEORDERING_V2_JSON"))
    uid = reminder_id.upper()
    if add and uid not in order:
        order.append(uid)
    if not add:
        order = [item for item in order if item != uid]
    con.execute(
        """
        update ZREMCDBASELIST
        set ZREMINDERIDSMERGEABLEORDERING_V2_JSON=?,
            Z_OPT=coalesce(Z_OPT,0)+1
        where Z_PK=?
        """,
        (reminder_order_payload(order), list_row["Z_PK"]),
    )
    bump_cloud_state(con, list_row.get("ZCKCLOUDSTATE"), now)


def reminder_payload(
    con: sqlite3.Connection,
    row: dict[str, Any],
    include_attachments: bool = True,
) -> dict[str, Any]:
    list_row = con.execute("select * from ZREMCDBASELIST where Z_PK=?", (row["ZLIST"],)).fetchone()
    memberships = membership_map(list_row["ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA"] if list_row else None)
    section_id = memberships.get((row["ZCKIDENTIFIER"] or "").upper())
    section_name = None
    if section_id:
        sec = con.execute(
            "select ZDISPLAYNAME from ZREMCDBASESECTION where ZLIST=? and ZCKIDENTIFIER=?",
            (row["ZLIST"], section_id),
        ).fetchone()
        section_name = sec["ZDISPLAYNAME"] if sec else None
    payload: dict[str, Any] = {
        "pk": row["Z_PK"],
        "id": row["ZCKIDENTIFIER"],
        "url": f"x-apple-reminder://{row['ZCKIDENTIFIER']}",
        "title": row["ZTITLE"],
        "notes": row["ZNOTES"],
        "list": list_row["ZNAME"] if list_row else row.get("LIST_NAME"),
        "list_id": list_row["ZCKIDENTIFIER"] if list_row else None,
        "section": section_name,
        "section_id": section_id,
        "completed": bool(row["ZCOMPLETED"]),
        "flagged": bool(row["ZFLAGGED"]),
        "priority": row["ZPRIORITY"],
        "created_at": core_to_iso(row["ZCREATIONDATE"]),
        "modified_at": core_to_iso(row["ZLASTMODIFIEDDATE"]),
        "due_at": core_to_iso(row["ZDUEDATE"]),
        "display_at": core_to_iso(row["ZDISPLAYDATEDATE"]),
        "all_day": bool(row["ZALLDAY"]),
        "display_date_is_all_day": bool(row["ZDISPLAYDATEISALLDAY"]),
        "timezone": row["ZTIMEZONE"],
        "ics_url": row["ZICSURL"],
        "marked_for_deletion": bool(row["ZMARKEDFORDELETION"]),
    }
    tags = [tag_assignment_payload(item) for item in reminder_tag_rows(con, row["Z_PK"])]
    payload["tags"] = tags
    payload["tag_names"] = [item["label"]["name"] for item in tags]
    if include_attachments:
        image_attachments = con.execute(
            """
            select Z_PK,ZCKIDENTIFIER,ZFILENAME,ZSHA512SUM,ZUTI,ZFILESIZE,ZWIDTH,ZHEIGHT,ZMARKEDFORDELETION
            from ZREMCDOBJECT
            where ZREMINDER2=? and Z_ENT=25 and coalesce(ZMARKEDFORDELETION,0)=0
            order by Z_FOK_REMINDER1, Z_PK
            """,
            (row["Z_PK"],),
        ).fetchall()
        url_attachments = con.execute(
            """
            select Z_PK,ZCKIDENTIFIER,ZURL,ZHOSTURL,ZUTI,ZMARKEDFORDELETION
            from ZREMCDOBJECT
            where ZREMINDER2=? and Z_ENT=26 and coalesce(ZMARKEDFORDELETION,0)=0
            order by Z_FOK_REMINDER1, Z_PK
            """,
            (row["Z_PK"],),
        ).fetchall()
        payload["attachments"] = [dict(item) for item in image_attachments]
        payload["url_attachments"] = [dict(item) for item in url_attachments]
        payload["attachment_items"] = [
            attachment_payload(item) for item in active_attachment_rows(con, row["Z_PK"])
        ]
    return payload


def cache_notes_metadata(notes: Any) -> dict[str, Any]:
    if not notes:
        return {"notes_length": 0, "notes_sha256": None}
    text = str(notes)
    return {
        "notes_length": len(text),
        "notes_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def source_file_info(db: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"db": str(db)}
    if db.exists():
        stat = db.stat()
        info.update(
            {
                "db_size": stat.st_size,
                "db_mtime_unix": stat.st_mtime,
                "db_mtime": dt.datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
            }
        )
    return info


def build_cache_payload(con: sqlite3.Connection, db: Path) -> dict[str, Any]:
    list_rows = [
        dict(row)
        for row in con.execute(
            """
            select l.Z_PK,l.ZCKIDENTIFIER,l.ZNAME,l.ZISGROUP,l.ZPARENTLIST,
                   l.ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA,
                   count(r.Z_PK) as reminder_count
            from ZREMCDBASELIST l
            left join ZREMCDREMINDER r on r.ZLIST=l.Z_PK and coalesce(r.ZMARKEDFORDELETION,0)=0
            where coalesce(l.ZMARKEDFORDELETION,0)=0 and l.ZNAME is not null
            group by l.Z_PK
            order by lower(l.ZNAME)
            """
        )
    ]
    list_id_by_pk = {row["Z_PK"]: row["ZCKIDENTIFIER"] for row in list_rows}
    memberships_by_list_pk = {
        row["Z_PK"]: membership_map(row.get("ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA"))
        for row in list_rows
    }
    lists = [
        {
            "id": row["ZCKIDENTIFIER"],
            "name": row["ZNAME"],
            "is_group": bool(row["ZISGROUP"]),
            "parent_list_id": list_id_by_pk.get(row["ZPARENTLIST"]),
            "reminder_count": row["reminder_count"],
        }
        for row in list_rows
    ]

    section_rows = [
        dict(row)
        for row in con.execute(
            """
            select s.Z_PK,s.ZCKIDENTIFIER,s.ZDISPLAYNAME,s.ZLIST,l.ZNAME as list_name,
                   l.ZCKIDENTIFIER as list_id,s.Z_FOK_LIST
            from ZREMCDBASESECTION s
            left join ZREMCDBASELIST l on l.Z_PK=s.ZLIST
            where coalesce(s.ZMARKEDFORDELETION,0)=0
            order by lower(l.ZNAME), s.Z_FOK_LIST, lower(s.ZDISPLAYNAME)
            """
        )
    ]
    sections_by_key = {
        (row["ZLIST"], (row["ZCKIDENTIFIER"] or "").upper()): row
        for row in section_rows
    }
    sections = [
        {
            "id": row["ZCKIDENTIFIER"],
            "name": row["ZDISPLAYNAME"],
            "list_id": row["list_id"],
            "list": row["list_name"],
            "order": row["Z_FOK_LIST"],
        }
        for row in section_rows
    ]

    tag_rows = [
        dict(row)
        for row in con.execute(
            """
            select o.ZREMINDER3,l.ZNAME
            from ZREMCDOBJECT o
            join ZREMCDHASHTAGLABEL l on l.Z_PK=o.ZHASHTAGLABEL
            where o.Z_ENT=? and coalesce(o.ZMARKEDFORDELETION,0)=0
            order by lower(l.ZNAME)
            """,
            (TAG_OBJECT_ENT,),
        )
    ]
    tag_names_by_reminder_pk: dict[int, list[str]] = {}
    for row in tag_rows:
        tag_names_by_reminder_pk.setdefault(row["ZREMINDER3"], []).append(row["ZNAME"])

    reminder_rows = [
        dict(row)
        for row in con.execute(
            """
            select r.Z_PK,r.ZCKIDENTIFIER,r.ZTITLE,r.ZNOTES,r.ZLIST,r.ZCOMPLETED,
                   r.ZFLAGGED,r.ZPRIORITY,r.ZCREATIONDATE,r.ZLASTMODIFIEDDATE,
                   r.ZDUEDATE,r.ZDISPLAYDATEDATE,r.ZCOMPLETIONDATE,
                   r.ZALLDAY,r.ZDISPLAYDATEISALLDAY,r.ZTIMEZONE,
                   l.ZNAME as list_name,l.ZCKIDENTIFIER as list_id,
                   coalesce(i.image_attachment_count,0) as image_attachment_count,
                   coalesce(u.url_attachment_count,0) as url_attachment_count
            from ZREMCDREMINDER r
            left join ZREMCDBASELIST l on l.Z_PK=r.ZLIST
            left join (
                select ZREMINDER2, count(*) as image_attachment_count
                from ZREMCDOBJECT
                where Z_ENT=25 and coalesce(ZMARKEDFORDELETION,0)=0
                group by ZREMINDER2
            ) i on i.ZREMINDER2=r.Z_PK
            left join (
                select ZREMINDER2, count(*) as url_attachment_count
                from ZREMCDOBJECT
                where Z_ENT=26 and coalesce(ZMARKEDFORDELETION,0)=0
                group by ZREMINDER2
            ) u on u.ZREMINDER2=r.Z_PK
            where coalesce(r.ZMARKEDFORDELETION,0)=0
            order by coalesce(r.ZDUEDATE, 999999999), lower(l.ZNAME), r.Z_FOK_LIST, lower(r.ZTITLE)
            """
        )
    ]
    reminders: list[dict[str, Any]] = []
    for row in reminder_rows:
        reminder_id = (row["ZCKIDENTIFIER"] or "").upper()
        section_id = memberships_by_list_pk.get(row["ZLIST"], {}).get(reminder_id)
        section = sections_by_key.get((row["ZLIST"], section_id)) if section_id else None
        tag_names = tag_names_by_reminder_pk.get(row["Z_PK"], [])
        reminders.append(
            {
                "id": row["ZCKIDENTIFIER"],
                "url": f"x-apple-reminder://{row['ZCKIDENTIFIER']}",
                "title": row["ZTITLE"],
                **cache_notes_metadata(row["ZNOTES"]),
                "list": row["list_name"],
                "list_id": row["list_id"],
                "section": section["ZDISPLAYNAME"] if section else None,
                "section_id": section_id,
                "completed": bool(row["ZCOMPLETED"]),
                "flagged": bool(row["ZFLAGGED"]),
                "priority": row["ZPRIORITY"],
                "created_at": core_to_iso(row["ZCREATIONDATE"]),
                "modified_at": core_to_iso(row["ZLASTMODIFIEDDATE"]),
                "due_at": core_to_iso(row["ZDUEDATE"]),
                "display_at": core_to_iso(row["ZDISPLAYDATEDATE"]),
                "all_day": bool(row["ZALLDAY"]),
                "display_date_is_all_day": bool(row["ZDISPLAYDATEISALLDAY"]),
                "timezone": row["ZTIMEZONE"],
                "completed_at": core_to_iso(row["ZCOMPLETIONDATE"]),
                "image_attachment_count": int(row["image_attachment_count"] or 0),
                "url_attachment_count": int(row["url_attachment_count"] or 0),
                "attachment_count": int(row["image_attachment_count"] or 0)
                + int(row["url_attachment_count"] or 0),
                "tag_names": tag_names,
                "tag_count": len(tag_names),
            }
        )

    image_attachment_count = con.execute(
        """
        select count(*)
        from ZREMCDOBJECT
        where Z_ENT=25 and coalesce(ZMARKEDFORDELETION,0)=0
        """
    ).fetchone()[0]
    url_attachment_count = con.execute(
        """
        select count(*)
        from ZREMCDOBJECT
        where Z_ENT=26 and coalesce(ZMARKEDFORDELETION,0)=0
        """
    ).fetchone()[0]
    tag_label_count = con.execute("select count(*) from ZREMCDHASHTAGLABEL").fetchone()[0]
    tag_assignment_count = con.execute(
        """
        select count(*)
        from ZREMCDOBJECT
        where Z_ENT=? and coalesce(ZMARKEDFORDELETION,0)=0
        """,
        (TAG_OBJECT_ENT,),
    ).fetchone()[0]
    return {
        "version": CACHE_VERSION,
        "generated_at": dt.datetime.now().astimezone().isoformat(),
        "source": source_file_info(db),
        "counts": {
            "lists": len(lists),
            "sections": len(sections),
            "reminders": len(reminders),
            "image_attachments": image_attachment_count,
            "url_attachments": url_attachment_count,
            "attachments": image_attachment_count + url_attachment_count,
            "tag_labels": tag_label_count,
            "tag_assignments": tag_assignment_count,
        },
        "lists": lists,
        "sections": sections,
        "reminders": reminders,
    }


def write_cache_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def load_cache_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AdapterError(f"Cache not found: {path}. Run cache_rebuild first.")
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise AdapterError(f"Cache is not a JSON object: {path}")
    if payload.get("version") != CACHE_VERSION:
        raise AdapterError(
            f"Unsupported cache version: {payload.get('version')}. Run cache_rebuild."
        )
    return payload


def cache_info_payload(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cache_dir": str(path.parent),
        "cache_path": str(path),
        "exists": path.exists(),
    }
    if not path.exists():
        return payload

    cache = load_cache_file(path)
    stat = path.stat()
    source = cache.get("source") if isinstance(cache.get("source"), dict) else {}
    source_db = Path(source["db"]) if source and source.get("db") else None
    payload.update(
        {
            "bytes": stat.st_size,
            "cache_mtime": dt.datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
            "version": cache.get("version"),
            "generated_at": cache.get("generated_at"),
            "source": source,
            "counts": cache.get("counts", {}),
        }
    )
    if source_db and source_db.exists():
        current_mtime = source_db.stat().st_mtime
        payload["source_db_current_mtime_unix"] = current_mtime
        payload["stale"] = abs(current_mtime - float(source.get("db_mtime_unix", current_mtime))) > 0.001
    else:
        payload["stale"] = None
    return payload


def cache_field_matches(value: Any, expected: str | None) -> bool:
    if expected is None:
        return True
    return str(value or "").casefold() == expected.casefold()


def cached_reminder_matches_query(reminder: dict[str, Any], query: str | None) -> bool:
    if not query:
        return True
    needle = query.casefold()
    for key in ("id", "title", "list", "section", "due_at", "display_at", "modified_at"):
        value = reminder.get(key)
        if value is not None and needle in str(value).casefold():
            return True
    for tag in reminder.get("tag_names", []) or []:
        if needle in str(tag).casefold():
            return True
    return False


def filter_cached_reminders(
    payload: dict[str, Any],
    query: str | None = None,
    list_name: str | None = None,
    section_name: str | None = None,
    include_completed: bool = False,
    flagged: bool | None = None,
    priority: int | None = None,
    limit: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    matches: list[dict[str, Any]] = []
    for reminder in payload.get("reminders", []):
        if not isinstance(reminder, dict):
            continue
        if not include_completed and reminder.get("completed"):
            continue
        if flagged is not None and bool(reminder.get("flagged")) != flagged:
            continue
        if priority is not None and reminder.get("priority") != priority:
            continue
        if not cache_field_matches(reminder.get("list"), list_name):
            continue
        if not cache_field_matches(reminder.get("section"), section_name):
            continue
        if not cached_reminder_matches_query(reminder, query):
            continue
        matches.append(reminder)
    total = len(matches)
    return matches[:limit], total


def cmd_doctor(_: argparse.Namespace) -> int:
    dbs = []
    for db in sorted(STORES.glob("*.sqlite")):
        try:
            con = connect(db)
            try:
                missing = sorted(REQUIRED_TABLES - table_names(con))
                item = {"path": str(db), "usable": not missing, "missing_tables": missing}
                if not missing:
                    item["counts"] = db_counts(db)
                dbs.append(item)
            finally:
                con.close()
        except sqlite3.Error as exc:
            dbs.append({"path": str(db), "usable": False, "error": str(exc)})
    json_out(
        {
            "ok": True,
            "group_container_exists": GROUP.exists(),
            "stores_dir": str(STORES),
            "files_dir": str(FILES),
            "main_db": str(main_db()) if usable_dbs() else None,
            "databases": dbs,
        }
    )
    return 0


def cmd_backup_store(args: argparse.Namespace) -> int:
    default_dir = APP_SUPPORT / "backups"
    default_dir.mkdir(parents=True, exist_ok=True)
    out = Path(args.output).expanduser() if args.output else default_dir / f"reminders-container-backup-{dt.datetime.now():%Y%m%d-%H%M%S}.tgz"
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        tar.add(GROUP, arcname="Container_v1")
    log_action("backup_store", {"path": str(out)})
    json_out({"ok": True, "backup": str(out), "bytes": out.stat().st_size})
    return 0


def cmd_list_lists(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    con = connect(db)
    try:
        rows = con.execute(
            """
            select l.Z_PK,l.ZCKIDENTIFIER,l.ZNAME,l.ZISGROUP,l.ZPARENTLIST,l.ZPARENTACCOUNT,
                   l.ZMARKEDFORDELETION,
                   count(r.Z_PK) as reminder_count
            from ZREMCDBASELIST l
            left join ZREMCDREMINDER r on r.ZLIST=l.Z_PK and coalesce(r.ZMARKEDFORDELETION,0)=0
            where coalesce(l.ZMARKEDFORDELETION,0)=0 and l.ZNAME is not null
            group by l.Z_PK
            order by lower(l.ZNAME)
            """
        ).fetchall()
        json_out({"ok": True, "db": str(db), "lists": [dict(row) for row in rows]})
        return 0
    finally:
        con.close()


def cmd_list_sections(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    con = connect(db)
    try:
        params: list[Any] = []
        where = ["coalesce(s.ZMARKEDFORDELETION,0)=0"]
        if args.list:
            where.append("l.ZNAME=?")
            params.append(args.list)
        rows = con.execute(
            f"""
            select s.Z_PK,s.ZCKIDENTIFIER,s.ZDISPLAYNAME,s.ZLIST,l.ZNAME as list_name,s.Z_FOK_LIST
            from ZREMCDBASESECTION s
            left join ZREMCDBASELIST l on l.Z_PK=s.ZLIST
            where {" and ".join(where)}
            order by lower(l.ZNAME), s.Z_FOK_LIST, lower(s.ZDISPLAYNAME)
            """,
            params,
        ).fetchall()
        json_out({"ok": True, "db": str(db), "sections": [dict(row) for row in rows]})
        return 0
    finally:
        con.close()


def cmd_snapshot(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    con = connect(db)
    try:
        lists = [dict(row) for row in con.execute(
            """
            select Z_PK,ZCKIDENTIFIER,ZNAME,ZISGROUP,ZPARENTLIST
            from ZREMCDBASELIST
            where coalesce(ZMARKEDFORDELETION,0)=0 and ZNAME is not null
            order by lower(ZNAME)
            """
        )]
        sections = [dict(row) for row in con.execute(
            """
            select s.Z_PK,s.ZCKIDENTIFIER,s.ZDISPLAYNAME,s.ZLIST,l.ZNAME as list_name,s.Z_FOK_LIST
            from ZREMCDBASESECTION s
            left join ZREMCDBASELIST l on l.Z_PK=s.ZLIST
            where coalesce(s.ZMARKEDFORDELETION,0)=0
            order by lower(l.ZNAME), s.Z_FOK_LIST
            """
        )]
        params: list[Any] = []
        where = ["coalesce(r.ZMARKEDFORDELETION,0)=0"]
        if not args.include_completed:
            where.append("coalesce(r.ZCOMPLETED,0)=0")
        if args.list:
            where.append("l.ZNAME=?")
            params.append(args.list)
        rows = con.execute(
            f"""
            select r.*, l.ZNAME as LIST_NAME
            from ZREMCDREMINDER r
            left join ZREMCDBASELIST l on l.Z_PK=r.ZLIST
            where {" and ".join(where)}
            order by coalesce(r.ZDUEDATE, 999999999), lower(l.ZNAME), r.Z_FOK_LIST, lower(r.ZTITLE)
            limit ?
            """,
            [*params, args.limit],
        ).fetchall()
        reminders = [
            {k: v for k, v in reminder_payload(con, dict(row), include_attachments=False).items() if k != "notes"}
            for row in rows
        ]
        json_out(
            {
                "ok": True,
                "db": str(db),
                "counts": db_counts(db),
                "lists": lists,
                "sections": sections,
                "reminders": reminders,
                "truncated": len(reminders) >= args.limit,
            }
        )
        return 0
    finally:
        con.close()


def cmd_search_reminders(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    con = connect(db)
    try:
        pattern = f"%{args.query.lower()}%"
        params: list[Any] = [pattern, pattern]
        where = ["coalesce(r.ZMARKEDFORDELETION,0)=0", "(lower(r.ZTITLE) like ? or lower(coalesce(r.ZNOTES,'')) like ?)"]
        if not args.include_completed:
            where.append("coalesce(r.ZCOMPLETED,0)=0")
        if args.list:
            where.append("l.ZNAME=?")
            params.append(args.list)
        rows = con.execute(
            f"""
            select r.*, l.ZNAME as LIST_NAME
            from ZREMCDREMINDER r
            left join ZREMCDBASELIST l on l.Z_PK=r.ZLIST
            where {" and ".join(where)}
            order by r.ZLASTMODIFIEDDATE desc, r.Z_PK desc
            limit ?
            """,
            [*params, args.limit],
        ).fetchall()
        json_out(
            {
                "ok": True,
                "db": str(db),
                "matches": [
                    {k: v for k, v in reminder_payload(con, dict(row), include_attachments=False).items() if k != "notes"}
                    for row in rows
                ],
                "truncated": len(rows) >= args.limit,
            }
        )
        return 0
    finally:
        con.close()


def cmd_read_reminder(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    con = connect(db)
    try:
        row = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        json_out({"ok": True, "db": str(db), "reminder": reminder_payload(con, row)})
        return 0
    finally:
        con.close()


def cmd_list_tags(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    con = connect(db)
    try:
        params: list[Any] = []
        where: list[str] = []
        if args.query:
            where.append("(lower(l.ZNAME) like ? or lower(l.ZCANONICALNAME) like ?)")
            pattern = f"%{args.query.casefold()}%"
            params.extend([pattern, pattern])
        where_sql = f"where {' and '.join(where)}" if where else ""
        rows = con.execute(
            f"""
            select l.*,
                   coalesce(count(o.Z_PK),0) as active_count
            from ZREMCDHASHTAGLABEL l
            left join ZREMCDOBJECT o
              on o.ZHASHTAGLABEL=l.Z_PK
             and o.Z_ENT=?
             and coalesce(o.ZMARKEDFORDELETION,0)=0
            {where_sql}
            group by l.Z_PK
            order by lower(l.ZNAME)
            limit ?
            """,
            [TAG_OBJECT_ENT, *params, args.limit],
        ).fetchall()
        json_out(
            {
                "ok": True,
                "db": str(db),
                "tags": [tag_label_payload(row, active_count=int(row["active_count"] or 0)) for row in rows],
                "truncated": len(rows) >= args.limit,
            }
        )
        return 0
    finally:
        con.close()


def cmd_add_tag(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    tag = normalized_tag_name(args.tag)
    con = connect(db)
    try:
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        now = core_now()
        account_id = account_identifier(con, reminder.get("ZACCOUNT"))
        con.execute("begin immediate")
        label, label_created = find_or_create_tag_label(con, tag, account_id, now)
        existing = con.execute(
            """
            select *
            from ZREMCDOBJECT
            where ZREMINDER3=? and ZHASHTAGLABEL=? and Z_ENT=? and coalesce(ZMARKEDFORDELETION,0)=0
            order by Z_PK
            """,
            (reminder["Z_PK"], label["Z_PK"], TAG_OBJECT_ENT),
        ).fetchone()
        if existing:
            con.commit()
            json_out(
                {
                    "ok": True,
                    "db": str(db),
                    "attached": False,
                    "reason": "already_attached",
                    "tag": tag_label_payload(label),
                    "object": {
                        "object_pk": existing["Z_PK"],
                        "object_id": existing["ZCKIDENTIFIER"],
                    },
                }
            )
            return 0
        object_pk = con.execute("select Z_MAX + 1 from Z_PRIMARYKEY where Z_NAME='REMCDObject'").fetchone()[0]
        cloud_pk = con.execute("select Z_MAX + 1 from Z_PRIMARYKEY where Z_NAME='REMCKCloudState'").fetchone()[0]
        object_id = str(uuid.uuid4()).upper()
        con.execute(
            """
            insert into ZREMCDOBJECT (
              Z_PK,Z_ENT,Z_OPT,ZCKDIRTYFLAGS,ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION,
              ZMARKEDFORDELETION,ZMINIMUMSUPPORTEDAPPVERSION,ZACCOUNT,ZCKCLOUDSTATE,
              ZHASHTAGLABEL,ZREMINDER3,ZIDENTIFIER,ZCKIDENTIFIER
            ) values (?, ?, 1, 0, 0, 0, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                object_pk,
                TAG_OBJECT_ENT,
                reminder["ZACCOUNT"],
                cloud_pk,
                label["Z_PK"],
                reminder["Z_PK"],
                sqlite3.Binary(uuid_blob(object_id)),
                object_id,
            ),
        )
        con.execute(
            """
            insert into ZREMCKCLOUDSTATE (
              Z_PK,Z_ENT,Z_OPT,ZCURRENTLOCALVERSION,ZLATESTVERSIONSYNCEDTOCLOUD,
              ZOBJECT,Z13_OBJECT,ZLOCALVERSIONDATE
            ) values (?,45,1,1,0,?,?,?)
            """,
            (cloud_pk, object_pk, TAG_OBJECT_ENT, now),
        )
        touch_reminder(con, reminder, now)
        update_primary_key(con, 13, object_pk)
        update_primary_key(con, 45, cloud_pk)
        con.commit()
        log_action("add_tag", {"reminder": reminder["ZCKIDENTIFIER"], "tag": tag, "object": object_id})
        json_out(
            {
                "ok": True,
                "db": str(db),
                "attached": True,
                "label_created": label_created,
                "reminder_id": reminder["ZCKIDENTIFIER"],
                "tag": tag_label_payload(label),
                "object_id": object_id,
            }
        )
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def cmd_remove_tag(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    tag = normalized_tag_name(args.tag)
    con = connect(db)
    try:
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        label = find_tag_label(con, tag, account_id=account_identifier(con, reminder.get("ZACCOUNT")))
        if not label:
            json_out({"ok": True, "db": str(db), "removed": False, "reason": "tag_not_found", "tag": tag})
            return 0
        rows = con.execute(
            """
            select *
            from ZREMCDOBJECT
            where ZREMINDER3=? and ZHASHTAGLABEL=? and Z_ENT=? and coalesce(ZMARKEDFORDELETION,0)=0
            order by Z_PK
            """,
            (reminder["Z_PK"], label["Z_PK"], TAG_OBJECT_ENT),
        ).fetchall()
        if not rows:
            json_out({"ok": True, "db": str(db), "removed": False, "reason": "tag_not_attached", "tag": tag_label_payload(label)})
            return 0
        now = core_now()
        con.execute("begin immediate")
        removed = []
        for row in rows:
            con.execute(
                "update ZREMCDOBJECT set ZMARKEDFORDELETION=1,Z_OPT=coalesce(Z_OPT,0)+1 where Z_PK=?",
                (row["Z_PK"],),
            )
            bump_cloud_state(con, row["ZCKCLOUDSTATE"], now)
            removed.append({"object_pk": row["Z_PK"], "object_id": row["ZCKIDENTIFIER"]})
        touch_reminder(con, reminder, now)
        con.commit()
        log_action("remove_tag", {"reminder": reminder["ZCKIDENTIFIER"], "tag": tag, "removed": removed})
        json_out({"ok": True, "db": str(db), "removed": True, "tag": tag_label_payload(label), "objects": removed})
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def cmd_cleanup_tags(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    con = connect(db)
    try:
        params: list[Any] = [TAG_OBJECT_ENT]
        filters = ["coalesce(active_count,0)=0"]
        if args.tag:
            filters.append("lower(ZCANONICALNAME)=?")
            params.append(canonical_tag_name(args.tag))
        if args.prefix:
            filters.append("lower(ZNAME) like ?")
            params.append(f"{normalized_tag_name(args.prefix).casefold()}%")
        rows = con.execute(
            f"""
            select *
            from (
              select l.*,
                     coalesce(count(o.Z_PK),0) as active_count
              from ZREMCDHASHTAGLABEL l
              left join ZREMCDOBJECT o
                on o.ZHASHTAGLABEL=l.Z_PK
               and o.Z_ENT=?
               and coalesce(o.ZMARKEDFORDELETION,0)=0
              group by l.Z_PK
            )
            where {" and ".join(filters)}
            order by lower(ZNAME)
            limit ?
            """,
            [*params, args.limit],
        ).fetchall()
        candidates = [tag_label_payload(row, active_count=int(row["active_count"] or 0)) for row in rows]
        if not args.apply:
            json_out({"ok": True, "db": str(db), "applied": False, "candidates": candidates, "truncated": len(rows) >= args.limit})
            return 0
        if not args.tag and not args.prefix:
            raise AdapterError("cleanup_tags --apply requires --tag or --prefix")
        con.execute("begin immediate")
        for row in rows:
            con.execute("delete from ZREMCDHASHTAGLABEL where Z_PK=?", (row["Z_PK"],))
        con.commit()
        log_action("cleanup_tags", {"deleted": [item["name"] for item in candidates]})
        json_out({"ok": True, "db": str(db), "applied": True, "deleted": candidates})
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def cmd_create_list(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    con = connect(db)
    try:
        existing = con.execute(
            """
            select Z_PK,ZCKIDENTIFIER,ZNAME,ZISGROUP,ZBADGEEMBLEM,ZCOLOR
            from ZREMCDBASELIST
            where ZNAME=? and coalesce(ZMARKEDFORDELETION,0)=0
            order by Z_PK
            """,
            (args.name,),
        ).fetchone()
        if existing:
            json_out({"ok": True, "created": False, "db": str(db), "list": dict(existing)})
            return 0
    finally:
        con.close()

    script = """
on run argv
  set listName to item 1 of argv
  set listColor to item 2 of argv
  set listEmblem to item 3 of argv
  tell application "Reminders"
    set newList to make new list with properties {name:listName}
    if listColor is not "" then set color of newList to listColor
    if listEmblem is not "" then set emblem of newList to listEmblem
    return (id of newList) & linefeed & (name of newList) & linefeed & ((color of newList) as text) & linefeed & ((emblem of newList) as text)
  end tell
end run
"""
    out = run_osascript(script, [args.name, args.color or "", args.emblem or ""])
    lines = out.splitlines()
    payload = {
        "ok": True,
        "created": True,
        "backend": "applescript",
        "id": lines[0] if len(lines) > 0 else None,
        "name": lines[1] if len(lines) > 1 else args.name,
        "color": lines[2] if len(lines) > 2 else args.color,
        "emblem": lines[3] if len(lines) > 3 else args.emblem,
    }
    log_action("create_list_applescript", payload)
    json_out(payload)
    return 0


def cmd_create_reminder(args: argparse.Namespace) -> int:
    if args.backend == "db":
        db = Path(args.db).expanduser() if args.db else main_db()
        con = connect(db)
        try:
            list_row = find_list(con, name=args.list)
            now = core_now()
            reminder_id = str(uuid.uuid4()).upper()
            sched = schedule_values(
                due_at=args.due_at,
                remind_at=args.remind_at,
                all_day_due_date=args.all_day_due_date,
            ) or {}
            resolution_keys = [
                "allDay",
                "completed",
                "creationDate",
                "lastModifiedDate",
                "list",
                "minimumSupportedVersion",
                "notesDocument",
                "titleDocument",
                "flagged",
                "priority",
            ]
            if sched:
                resolution_keys.append("dueDate")
                if sched.get("ZTIMEZONE"):
                    resolution_keys.append("timeZone")
            con.execute("begin immediate")
            reminder_pk = con.execute("select Z_MAX + 1 from Z_PRIMARYKEY where Z_ENT=39").fetchone()[0]
            cloud_pk = con.execute("select Z_MAX + 1 from Z_PRIMARYKEY where Z_ENT=45").fetchone()[0]
            fok = con.execute(
                "select coalesce(max(coalesce(Z_FOK_LIST,0)),0)+1024 from ZREMCDREMINDER where ZLIST=?",
                (list_row["Z_PK"],),
            ).fetchone()[0]
            columns = [
                "Z_PK",
                "Z_ENT",
                "Z_OPT",
                "ZALLDAY",
                "ZCKDIRTYFLAGS",
                "ZCOMPLETED",
                "ZDISPLAYDATEISALLDAY",
                "ZDISPLAYDATEUPDATEDFORSECONDSFROMGMT",
                "ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION",
                "ZFLAGGED",
                "ZICSDISPLAYORDER",
                "ZISURGENTSTATEENABLEDFORCURRENTUSER",
                "ZMARKEDFORDELETION",
                "ZMINIMUMSUPPORTEDAPPVERSION",
                "ZPRIORITY",
                "ZSPOTLIGHTINDEXCOUNT",
                "ZACCOUNT",
                "ZCKCLOUDSTATE",
                "ZLIST",
                "Z_FOK_LIST",
                "ZCREATIONDATE",
                "ZDISPLAYDATEDATE",
                "ZDUEDATE",
                "ZLASTMODIFIEDDATE",
                "ZCKIDENTIFIER",
                "ZDACALENDARITEMUNIQUEIDENTIFIER",
                "ZDISPLAYDATETIMEZONE",
                "ZNOTES",
                "ZTIMEZONE",
                "ZTITLE",
                "ZIDENTIFIER",
                "ZNOTESDOCUMENT",
                "ZTITLEDOCUMENT",
                "ZRESOLUTIONTOKENMAP_V3_JSONDATA",
            ]
            values = [
                reminder_pk,
                39,
                1,
                sched.get("ZALLDAY", 0),
                0,
                0,
                sched.get("ZDISPLAYDATEISALLDAY", 0),
                0,
                0,
                1 if args.flagged else 0,
                0,
                0,
                0,
                0,
                args.priority if args.priority is not None else 0,
                1,
                list_row["ZACCOUNT"],
                cloud_pk,
                list_row["Z_PK"],
                fok,
                now,
                sched.get("ZDISPLAYDATEDATE"),
                sched.get("ZDUEDATE"),
                now,
                reminder_id,
                reminder_id,
                sched.get("ZDISPLAYDATETIMEZONE"),
                args.notes,
                sched.get("ZTIMEZONE"),
                args.title,
                sqlite3.Binary(uuid_blob(reminder_id)),
                sqlite3.Binary(reminder_text_document(args.notes)) if args.notes else None,
                sqlite3.Binary(reminder_text_document(args.title)),
                resolution_map(resolution_keys, now),
            ]
            con.execute(
                f"insert into ZREMCDREMINDER ({','.join(columns)}) values ({','.join('?' for _ in columns)})",
                values,
            )
            con.execute(
                """
                insert into ZREMCKCLOUDSTATE (
                  Z_PK,Z_ENT,Z_OPT,ZCURRENTLOCALVERSION,ZLATESTVERSIONSYNCEDTOCLOUD,
                  ZREMINDER,ZLOCALVERSIONDATE
                ) values (?,45,1,1,0,?,?)
                """,
                (cloud_pk, reminder_pk, now),
            )
            update_list_order(con, list_row, reminder_id, add=True, now=now)
            update_primary_key(con, 39, reminder_pk)
            update_primary_key(con, 45, cloud_pk)
            con.commit()
            rem_url = f"x-apple-reminder://{reminder_id}"
            sync_reminder_text_applescript(rem_url, title=args.title, notes=args.notes)
            log_action(
                "create_reminder_db",
                {
                    "id": rem_url,
                    "list": args.list,
                    "title": args.title,
                    "db": str(db),
                    "text_synced_via_applescript": True,
                },
            )
            json_out(
                {
                    "ok": True,
                    "backend": "db",
                    "id": rem_url,
                    "title": args.title,
                    "list": args.list,
                    "pk": reminder_pk,
                    "scheduled": bool(sched),
                    "text_synced_via_applescript": True,
                }
            )
            return 0
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    if args.due_at or args.remind_at or args.all_day_due_date or args.flagged is not None or args.priority is not None:
        raise AdapterError("date, flag, and priority options currently require --backend db")
    script = """
on run argv
  set listName to item 1 of argv
  set reminderTitle to item 2 of argv
  set reminderBody to item 3 of argv
  tell application "Reminders"
    set targetList to list listName
    set newReminder to make new reminder at end of reminders of targetList with properties {name:reminderTitle}
    if reminderBody is not "" then set body of newReminder to reminderBody
    return id of newReminder
  end tell
end run
"""
    rem_id = run_osascript(script, [args.list, args.title, args.notes or ""])
    log_action("create_reminder_applescript", {"id": rem_id, "list": args.list, "title": args.title})
    json_out({"ok": True, "backend": "applescript", "id": rem_id, "title": args.title, "list": args.list})
    return 0


def cmd_update_reminder(args: argparse.Namespace) -> int:
    if args.backend == "db":
        db = Path(args.db).expanduser() if args.db else main_db()
        con = connect(db)
        try:
            reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
            updates: list[str] = []
            params: list[Any] = []
            if args.new_title is not None:
                updates.append("ZTITLE=?")
                params.append(args.new_title)
                updates.append("ZTITLEDOCUMENT=?")
                params.append(sqlite3.Binary(reminder_text_document(args.new_title)))
            if args.notes is not None:
                updates.append("ZNOTES=?")
                params.append(args.notes)
                updates.append("ZNOTESDOCUMENT=?")
                params.append(sqlite3.Binary(reminder_text_document(args.notes)) if args.notes else None)
            if args.flagged is not None:
                updates.append("ZFLAGGED=?")
                params.append(1 if args.flagged else 0)
            if args.priority is not None:
                updates.append("ZPRIORITY=?")
                params.append(args.priority)
            sched = schedule_values(
                due_at=args.due_at,
                remind_at=args.remind_at,
                all_day_due_date=args.all_day_due_date,
                clear_due=args.clear_due,
            )
            if sched is not None:
                for key, value in sched.items():
                    updates.append(f"{key}=?")
                    params.append(value)
            if not updates:
                raise AdapterError("No update fields provided")
            now = core_now()
            updates.extend(["ZLASTMODIFIEDDATE=?", "Z_OPT=coalesce(Z_OPT,0)+1"])
            params.extend([now, reminder["Z_PK"]])
            con.execute("begin immediate")
            con.execute(f"update ZREMCDREMINDER set {', '.join(updates)} where Z_PK=?", params)
            bump_cloud_state(con, reminder.get("ZCKCLOUDSTATE"), now)
            con.commit()
            rem_url = f"x-apple-reminder://{reminder['ZCKIDENTIFIER']}"
            text_sync_needed = args.new_title is not None or args.notes is not None
            if text_sync_needed:
                sync_reminder_text_applescript(rem_url, title=args.new_title, notes=args.notes)
            log_action(
                "update_reminder_db",
                {
                    "id": rem_url,
                    "db": str(db),
                    "fields": [item.split('=')[0] for item in updates],
                    "text_synced_via_applescript": text_sync_needed,
                },
            )
            json_out({"ok": True, "backend": "db", "id": rem_url, "text_synced_via_applescript": text_sync_needed})
            return 0
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    if args.due_at or args.remind_at or args.all_day_due_date or args.clear_due:
        raise AdapterError("date options currently require --backend db")
    rem_id = reminder_url(args.id) if args.id else None
    if not rem_id:
        db = main_db()
        con = connect(db)
        try:
            rem_id = reminder_url(find_reminder(con, title=args.title, list_name=args.list)["ZCKIDENTIFIER"])
        finally:
            con.close()
    script = """
on run argv
  set reminderID to item 1 of argv
  set newTitle to item 2 of argv
  set newBody to item 3 of argv
  set newFlagged to item 4 of argv
  set newPriority to item 5 of argv
  tell application "Reminders"
    set targetReminder to reminder id reminderID
    if newTitle is not "" then set name of targetReminder to newTitle
    if newBody is not "__NO_CHANGE__" then set body of targetReminder to newBody
    if newFlagged is not "__NO_CHANGE__" then set flagged of targetReminder to (newFlagged is "true")
    if newPriority is not "__NO_CHANGE__" then set priority of targetReminder to (newPriority as integer)
    return id of targetReminder
  end tell
end run
"""
    out = run_osascript(
        script,
        [
            rem_id,
            args.new_title or "",
            args.notes if args.notes is not None else "__NO_CHANGE__",
            "true" if args.flagged is True else "false" if args.flagged is False else "__NO_CHANGE__",
            str(args.priority) if args.priority is not None else "__NO_CHANGE__",
        ],
    )
    log_action("update_reminder_applescript", {"id": out, "new_title": args.new_title, "notes_changed": args.notes is not None})
    json_out({"ok": True, "backend": "applescript", "id": out})
    return 0


def cmd_complete_reminder(args: argparse.Namespace) -> int:
    if args.backend == "db":
        db = Path(args.db).expanduser() if args.db else main_db()
        con = connect(db)
        try:
            reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
            now = core_now()
            con.execute("begin immediate")
            con.execute(
                """
                update ZREMCDREMINDER
                set ZCOMPLETED=1,
                    ZCOMPLETIONDATE=?,
                    ZLASTMODIFIEDDATE=?,
                    Z_OPT=coalesce(Z_OPT,0)+1
                where Z_PK=?
                """,
                (now, now, reminder["Z_PK"]),
            )
            bump_cloud_state(con, reminder.get("ZCKCLOUDSTATE"), now)
            con.commit()
            rem_url = f"x-apple-reminder://{reminder['ZCKIDENTIFIER']}"
            log_action("complete_reminder_db", {"id": rem_url, "db": str(db)})
            json_out({"ok": True, "backend": "db", "id": rem_url, "completed": True})
            return 0
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    rem_id = reminder_url(args.id) if args.id else None
    if not rem_id:
        db = main_db()
        con = connect(db)
        try:
            rem_id = reminder_url(find_reminder(con, title=args.title, list_name=args.list)["ZCKIDENTIFIER"])
        finally:
            con.close()
    script = """
on run argv
  set reminderID to item 1 of argv
  tell application "Reminders"
    set targetReminder to reminder id reminderID
    set completed of targetReminder to true
    return id of targetReminder
  end tell
end run
"""
    out = run_osascript(script, [rem_id])
    log_action("complete_reminder_applescript", {"id": out})
    json_out({"ok": True, "backend": "applescript", "id": out, "completed": True})
    return 0


def cmd_delete_reminder(args: argparse.Namespace) -> int:
    if args.backend == "db":
        db = Path(args.db).expanduser() if args.db else main_db()
        con = connect(db)
        try:
            reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
            list_row = None
            if reminder.get("ZLIST"):
                list_row = row_dict(con.execute("select * from ZREMCDBASELIST where Z_PK=?", (reminder["ZLIST"],)).fetchone())
            now = core_now()
            con.execute("begin immediate")
            if list_row:
                update_list_order(con, list_row, reminder["ZCKIDENTIFIER"], add=False, now=now)
                mapping = membership_map(list_row.get("ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA"))
                if reminder["ZCKIDENTIFIER"].upper() in mapping:
                    mapping.pop(reminder["ZCKIDENTIFIER"].upper(), None)
                    con.execute(
                        """
                        update ZREMCDBASELIST
                        set ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA=?,
                            Z_OPT=coalesce(Z_OPT,0)+1
                        where Z_PK=?
                        """,
                        (membership_payload(mapping), list_row["Z_PK"]),
                    )
                    bump_cloud_state(con, list_row.get("ZCKCLOUDSTATE"), now)
            con.execute(
                """
                update ZREMCDREMINDER
                set ZMARKEDFORDELETION=1,
                    ZLIST=null,
                    Z_FOK_LIST=null,
                    ZLASTMODIFIEDDATE=?,
                    Z_OPT=coalesce(Z_OPT,0)+1
                where Z_PK=?
                """,
                (now, reminder["Z_PK"]),
            )
            bump_cloud_state(con, reminder.get("ZCKCLOUDSTATE"), now)
            con.commit()
            rem_url = f"x-apple-reminder://{reminder['ZCKIDENTIFIER']}"
            log_action("delete_reminder_db_soft", {"id": rem_url, "db": str(db)})
            json_out({"ok": True, "backend": "db", "id": rem_url, "deleted_via": "db_soft_delete"})
            return 0
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    rem_id = reminder_url(args.id) if args.id else None
    if not rem_id:
        db = main_db()
        con = connect(db)
        try:
            rem_id = reminder_url(find_reminder(con, title=args.title, list_name=args.list)["ZCKIDENTIFIER"])
        finally:
            con.close()
    script = """
on run argv
  set reminderID to item 1 of argv
  tell application "Reminders"
    delete reminder id reminderID
  end tell
  return reminderID
end run
"""
    out = run_osascript(script, [rem_id])
    log_action("delete_reminder_applescript_native", {"id": out})
    json_out({"ok": True, "backend": "applescript", "id": out, "deleted_via": "native_reminders"})
    return 0


def cmd_create_section(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    con = connect(db)
    try:
        list_row = find_list(con, name=args.list, list_id=args.list_id)
        existing = con.execute(
            """
            select * from ZREMCDBASESECTION
            where ZLIST=? and ZDISPLAYNAME=? and coalesce(ZMARKEDFORDELETION,0)=0
            """,
            (list_row["Z_PK"], args.name),
        ).fetchone()
        if existing:
            json_out({"ok": True, "db": str(db), "section": dict(existing), "created": False})
            return 0
        now = core_now()
        section_id = str(uuid.uuid4()).upper()
        con.execute("begin immediate")
        section_pk = con.execute("select Z_MAX + 1 from Z_PRIMARYKEY where Z_ENT=5").fetchone()[0]
        cloud_pk = con.execute("select Z_MAX + 1 from Z_PRIMARYKEY where Z_ENT=45").fetchone()[0]
        fok = con.execute(
            "select coalesce(max(coalesce(Z_FOK_LIST,0)),0)+1024 from ZREMCDBASESECTION where ZLIST=?",
            (list_row["Z_PK"],),
        ).fetchone()[0]
        resolution = json.dumps(
            {
                "map": {
                    key: {"counter": 1, "modificationTime": now, "replicaID": str(uuid.uuid4()).upper()}
                    for key in ("minimumSupportedVersion", "creationDate", "list", "displayName")
                }
            },
            separators=(",", ":"),
        )
        con.execute(
            """
            insert into ZREMCDBASESECTION (
              Z_PK,Z_ENT,Z_OPT,ZCKDIRTYFLAGS,ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION,
              ZMARKEDFORDELETION,ZMINIMUMSUPPORTEDAPPVERSION,ZSPOTLIGHTINDEXCOUNT,
              ZACCOUNT,ZCKCLOUDSTATE,ZLIST,Z_FOK_LIST,ZCREATIONDATE,
              ZCKIDENTIFIER,ZDISPLAYNAME,ZIDENTIFIER,ZRESOLUTIONTOKENMAP_V3_JSONDATA
            ) values (?,6,1,0,0,0,0,0,?,?,?,?,?,?,?,?,?)
            """,
            (
                section_pk,
                list_row["ZACCOUNT"],
                cloud_pk,
                list_row["Z_PK"],
                fok,
                now,
                section_id,
                args.name,
                sqlite3.Binary(uuid_blob(section_id)),
                resolution,
            ),
        )
        con.execute(
            """
            insert into ZREMCKCLOUDSTATE (
              Z_PK,Z_ENT,Z_OPT,ZCURRENTLOCALVERSION,ZLATESTVERSIONSYNCEDTOCLOUD,
              ZSECTION,Z5_SECTION,ZLOCALVERSIONDATE
            ) values (?,45,1,1,0,?,6,?)
            """,
            (cloud_pk, section_pk, now),
        )
        update_primary_key(con, 5, section_pk)
        update_primary_key(con, 45, cloud_pk)
        con.commit()
        log_action("create_section", {"db": str(db), "list": list_row["ZNAME"], "section": args.name, "id": section_id})
        json_out({"ok": True, "db": str(db), "created": True, "section": {"pk": section_pk, "id": section_id, "name": args.name}})
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def cmd_move_to_section(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    con = connect(db)
    try:
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        list_row = find_list(con, list_id=reminder["ZLIST"] and con.execute("select ZCKIDENTIFIER from ZREMCDBASELIST where Z_PK=?", (reminder["ZLIST"],)).fetchone()[0])
        section = find_section(con, list_pk=list_row["Z_PK"], name=args.section, section_id=args.section_id)
        mapping = membership_map(list_row.get("ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA"))
        mapping[reminder["ZCKIDENTIFIER"].upper()] = section["ZCKIDENTIFIER"].upper()
        now = core_now()
        con.execute("begin immediate")
        con.execute(
            """
            update ZREMCDBASELIST
            set ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA=?, Z_OPT=coalesce(Z_OPT,0)+1
            where Z_PK=?
            """,
            (membership_payload(mapping), list_row["Z_PK"]),
        )
        con.execute(
            """
            update ZREMCKCLOUDSTATE
            set ZCURRENTLOCALVERSION=coalesce(ZCURRENTLOCALVERSION,0)+1,
                ZLOCALVERSIONDATE=?
            where Z_PK=?
            """,
            (now, list_row["ZCKCLOUDSTATE"]),
        )
        con.commit()
        log_action("move_to_section", {"reminder": reminder["ZCKIDENTIFIER"], "section": section["ZCKIDENTIFIER"]})
        json_out({"ok": True, "db": str(db), "reminder_id": reminder["ZCKIDENTIFIER"], "section_id": section["ZCKIDENTIFIER"], "section": section["ZDISPLAYNAME"]})
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def attach_image_record(con: sqlite3.Connection, reminder: dict[str, Any], image: Path) -> dict[str, Any]:
    if not image.exists():
        raise AdapterError(f"Image not found: {image}")
    data = image.read_bytes()
    sha512 = hashlib.sha512(data).hexdigest()
    ext = image.suffix.lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    uti = "public.jpeg" if ext in {"jpeg", "jpg"} else "public.png"
    width, height = image_size(image)
    existing = con.execute(
        """
        select Z_PK,Z_ENT,ZCKIDENTIFIER,ZCKCLOUDSTATE,ZREMINDER2,Z_FOK_REMINDER1,
               ZFILENAME,ZSHA512SUM,ZUTI,ZFILESIZE,ZWIDTH,ZHEIGHT,ZURL,ZHOSTURL,
               ZMARKEDFORDELETION
        from ZREMCDOBJECT
        where ZREMINDER2=? and ZSHA512SUM=? and Z_ENT=? and coalesce(ZMARKEDFORDELETION,0)=0
        """,
        (reminder["Z_PK"], sha512, IMAGE_ATTACHMENT_ENT),
    ).fetchone()
    if existing:
        return {"attached": False, "reason": "already_attached", "attachment": attachment_payload(existing)}
    attach_dir = attachment_dir_for_account()
    stored = attach_dir / f"{sha512}.{ext}"
    if not stored.exists():
        shutil.copy2(image, stored)
    now = core_now()
    object_id = str(uuid.uuid4()).upper()
    display_filename = f"{object_id}-codex.{ext}"
    object_pk = con.execute("select Z_MAX + 1 from Z_PRIMARYKEY where Z_NAME='REMCDObject'").fetchone()[0]
    cloud_pk = con.execute("select Z_MAX + 1 from Z_PRIMARYKEY where Z_NAME='REMCKCloudState'").fetchone()[0]
    sort_order = con.execute(
        """
        select coalesce(max(coalesce(Z_FOK_REMINDER1,0)),1024)+1024
        from ZREMCDOBJECT
        where ZREMINDER2=? and coalesce(ZMARKEDFORDELETION,0)=0
        """,
        (reminder["Z_PK"],),
    ).fetchone()[0]
    con.execute(
        """
        insert into ZREMCDOBJECT (
          Z_PK,Z_ENT,Z_OPT,ZCKDIRTYFLAGS,ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION,
          ZMARKEDFORDELETION,ZMINIMUMSUPPORTEDAPPVERSION,ZACCOUNT,ZCKCLOUDSTATE,
          ZREMINDER2,Z_FOK_REMINDER1,ZFILESIZE,ZHEIGHT,ZWIDTH,ZUTI,ZFILENAME,
          ZSHA512SUM,ZIDENTIFIER,ZCKIDENTIFIER
        ) values (?, ?, 1, 0, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            object_pk,
            IMAGE_ATTACHMENT_ENT,
            reminder["ZACCOUNT"],
            cloud_pk,
            reminder["Z_PK"],
            sort_order,
            len(data),
            height,
            width,
            uti,
            display_filename,
            sha512,
            sqlite3.Binary(uuid_blob(object_id)),
            object_id,
        ),
    )
    con.execute(
        """
        insert into ZREMCKCLOUDSTATE (
          Z_PK,Z_ENT,Z_OPT,ZCURRENTLOCALVERSION,ZLATESTVERSIONSYNCEDTOCLOUD,
          ZOBJECT,Z13_OBJECT,ZLOCALVERSIONDATE
        ) values (?,45,1,1,0,?,?,?)
        """,
        (cloud_pk, object_pk, IMAGE_ATTACHMENT_ENT, now),
    )
    touch_reminder(con, reminder, now)
    update_primary_key(con, 13, object_pk)
    update_primary_key(con, 45, cloud_pk)
    row = con.execute(
        """
        select Z_PK,Z_ENT,ZCKIDENTIFIER,ZCKCLOUDSTATE,ZREMINDER2,Z_FOK_REMINDER1,
               ZFILENAME,ZSHA512SUM,ZUTI,ZFILESIZE,ZWIDTH,ZHEIGHT,ZURL,ZHOSTURL,
               ZMARKEDFORDELETION
        from ZREMCDOBJECT
        where Z_PK=?
        """,
        (object_pk,),
    ).fetchone()
    return {
        "attached": True,
        "attachment": attachment_payload(row),
        "stored_path": str(stored),
        "width": width,
        "height": height,
    }


def attach_url_record(con: sqlite3.Connection, reminder: dict[str, Any], url: str) -> dict[str, Any]:
    normalized = normalized_url(url)
    existing = con.execute(
        """
        select Z_PK,Z_ENT,ZCKIDENTIFIER,ZCKCLOUDSTATE,ZREMINDER2,Z_FOK_REMINDER1,
               ZFILENAME,ZSHA512SUM,ZUTI,ZFILESIZE,ZWIDTH,ZHEIGHT,ZURL,ZHOSTURL,
               ZMARKEDFORDELETION
        from ZREMCDOBJECT
        where ZREMINDER2=? and Z_ENT=? and ZURL=? and coalesce(ZMARKEDFORDELETION,0)=0
        """,
        (reminder["Z_PK"], URL_ATTACHMENT_ENT, normalized),
    ).fetchone()
    if existing:
        return {"attached": False, "reason": "already_attached", "attachment": attachment_payload(existing)}
    now = core_now()
    object_id = str(uuid.uuid4()).upper()
    object_pk = con.execute("select Z_MAX + 1 from Z_PRIMARYKEY where Z_NAME='REMCDObject'").fetchone()[0]
    cloud_pk = con.execute("select Z_MAX + 1 from Z_PRIMARYKEY where Z_NAME='REMCKCloudState'").fetchone()[0]
    sort_order = con.execute(
        """
        select coalesce(max(coalesce(Z_FOK_REMINDER1,0)),1024)+1024
        from ZREMCDOBJECT
        where ZREMINDER2=? and coalesce(ZMARKEDFORDELETION,0)=0
        """,
        (reminder["Z_PK"],),
    ).fetchone()[0]
    columns = [
        "Z_PK",
        "Z_ENT",
        "Z_OPT",
        "ZCKDIRTYFLAGS",
        "ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION",
        "ZMARKEDFORDELETION",
        "ZMINIMUMSUPPORTEDAPPVERSION",
        "ZACCOUNT",
        "ZCKCLOUDSTATE",
        "ZREMINDER2",
        "Z_FOK_REMINDER1",
        "ZUTI",
        "ZURL",
        "ZIDENTIFIER",
        "ZCKIDENTIFIER",
    ]
    values = [
        object_pk,
        URL_ATTACHMENT_ENT,
        1,
        0,
        0,
        0,
        0,
        reminder["ZACCOUNT"],
        cloud_pk,
        reminder["Z_PK"],
        sort_order,
        "public.url",
        normalized,
        sqlite3.Binary(uuid_blob(object_id)),
        object_id,
    ]
    con.execute(
        f"insert into ZREMCDOBJECT ({','.join(columns)}) values ({','.join('?' for _ in columns)})",
        values,
    )
    con.execute(
        """
        insert into ZREMCKCLOUDSTATE (
          Z_PK,Z_ENT,Z_OPT,ZCURRENTLOCALVERSION,ZLATESTVERSIONSYNCEDTOCLOUD,
          ZOBJECT,Z13_OBJECT,ZLOCALVERSIONDATE
        ) values (?,45,1,1,0,?,?,?)
        """,
        (cloud_pk, object_pk, URL_ATTACHMENT_ENT, now),
    )
    touch_reminder(con, reminder, now)
    update_primary_key(con, 13, object_pk)
    update_primary_key(con, 45, cloud_pk)
    row = con.execute(
        """
        select Z_PK,Z_ENT,ZCKIDENTIFIER,ZCKCLOUDSTATE,ZREMINDER2,Z_FOK_REMINDER1,
               ZFILENAME,ZSHA512SUM,ZUTI,ZFILESIZE,ZWIDTH,ZHEIGHT,ZURL,ZHOSTURL,
               ZMARKEDFORDELETION
        from ZREMCDOBJECT
        where Z_PK=?
        """,
        (object_pk,),
    ).fetchone()
    return {"attached": True, "attachment": attachment_payload(row), "url": normalized}


def soft_delete_attachment_record(
    con: sqlite3.Connection,
    reminder: dict[str, Any],
    attachment: dict[str, Any],
) -> dict[str, Any]:
    now = core_now()
    con.execute(
        "update ZREMCDOBJECT set ZMARKEDFORDELETION=1,Z_OPT=coalesce(Z_OPT,0)+1 where Z_PK=?",
        (attachment["Z_PK"],),
    )
    bump_cloud_state(con, attachment.get("ZCKCLOUDSTATE"), now)
    touch_reminder(con, reminder, now)
    return attachment_payload({**attachment, "ZMARKEDFORDELETION": 1})


def cmd_attach_image(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    image = Path(args.image).expanduser().resolve()
    con = connect(db)
    try:
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        con.execute("begin immediate")
        result = attach_image_record(con, reminder, image)
        con.commit()
        attachment = result["attachment"]
        log_action("attach_image", {"reminder": reminder["ZCKIDENTIFIER"], "image": str(image), "object": attachment["id"], "stored": result.get("stored_path")})
        json_out({"ok": True, "db": str(db), "reminder_id": reminder["ZCKIDENTIFIER"], **result})
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def cmd_attach_url(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    url = normalized_url(args.url)
    con = connect(db)
    try:
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        con.execute("begin immediate")
        result = attach_url_record(con, reminder, url)
        con.commit()
        attachment = result["attachment"]
        log_action("attach_url", {"reminder": reminder["ZCKIDENTIFIER"], "url": url, "object": attachment["id"]})
        json_out({"ok": True, "db": str(db), "reminder_id": reminder["ZCKIDENTIFIER"], **result})
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def cmd_list_attachments(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    con = connect(db)
    try:
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        ent = attachment_ent_for_type(args.type)
        items = [attachment_payload(row) for row in active_attachment_rows(con, reminder["Z_PK"], attachment_ent=ent)]
        json_out({"ok": True, "db": str(db), "reminder_id": reminder["ZCKIDENTIFIER"], "attachments": items})
        return 0
    finally:
        con.close()


def cmd_delete_attachment(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    con = connect(db)
    try:
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        selected, candidates, reason = resolve_attachment_selection(
            con,
            reminder,
            attachment_id=args.attachment_id,
            attachment_pk=args.attachment_pk,
            attachment_type=args.type,
            filename=args.filename,
            url=args.url,
        )
        if not selected:
            json_out({"ok": False, "db": str(db), "error": reason, "candidates": candidates})
            return 1
        before = attachment_payload(selected)
        con.execute("begin immediate")
        deleted = soft_delete_attachment_record(con, reminder, selected)
        con.commit()
        log_action("delete_attachment_soft", {"reminder": reminder["ZCKIDENTIFIER"], "attachment": before})
        json_out(
            {
                "ok": True,
                "db": str(db),
                "deleted": True,
                "reminder_id": reminder["ZCKIDENTIFIER"],
                "attachment": before,
                "deleted_state": deleted,
            }
        )
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def cmd_replace_attachment(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    if bool(args.image) == bool(args.url):
        raise AdapterError("replace_attachment requires exactly one of --image or --url")
    replacement_type = "image" if args.image else "url"
    selector_type = args.type or replacement_type
    con = connect(db)
    try:
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        selected, candidates, reason = resolve_attachment_selection(
            con,
            reminder,
            attachment_id=args.attachment_id,
            attachment_pk=args.attachment_pk,
            attachment_type=selector_type,
            filename=args.filename,
            url=args.old_url,
        )
        if not selected:
            json_out({"ok": False, "db": str(db), "error": reason, "candidates": candidates})
            return 1
        old_attachment = attachment_payload(selected)
        con.execute("begin immediate")
        if args.image:
            new_result = attach_image_record(con, reminder, Path(args.image).expanduser().resolve())
        else:
            new_result = attach_url_record(con, reminder, args.url)
        new_attachment = new_result.get("attachment") or {}
        if int(new_attachment.get("pk", -1)) == int(selected["Z_PK"]):
            raise AdapterError("Replacement source is the same as the selected existing attachment")
        deleted = soft_delete_attachment_record(con, reminder, selected)
        con.commit()
        log_action(
            "replace_attachment",
            {
                "reminder": reminder["ZCKIDENTIFIER"],
                "old": old_attachment,
                "new": new_result.get("attachment"),
            },
        )
        json_out(
            {
                "ok": True,
                "db": str(db),
                "replaced": True,
                "reminder_id": reminder["ZCKIDENTIFIER"],
                "old_attachment": old_attachment,
                "new_attachment": new_result,
                "deleted_state": deleted,
            }
        )
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def cache_path_from_args(args: argparse.Namespace) -> Path:
    return Path(args.cache).expanduser() if getattr(args, "cache", None) else CACHE_FILE


def cmd_cache_rebuild(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    cache_path = cache_path_from_args(args)
    con = connect(db)
    try:
        payload = build_cache_payload(con, db)
        write_cache_file(cache_path, payload)
        json_out(
            {
                "ok": True,
                "cache": str(cache_path),
                "db": str(db),
                "generated_at": payload["generated_at"],
                "counts": payload["counts"],
            }
        )
        return 0
    finally:
        con.close()


def cmd_cache_info(args: argparse.Namespace) -> int:
    json_out({"ok": True, **cache_info_payload(cache_path_from_args(args))})
    return 0


def cached_query_response(args: argparse.Namespace, query: str | None) -> dict[str, Any]:
    cache_path = cache_path_from_args(args)
    payload = load_cache_file(cache_path)
    matches, total = filter_cached_reminders(
        payload,
        query=query,
        list_name=args.list,
        section_name=args.section,
        include_completed=args.include_completed,
        flagged=args.flagged,
        priority=args.priority,
        limit=args.limit,
    )
    return {
        "ok": True,
        "cache": str(cache_path),
        "cache_generated_at": payload.get("generated_at"),
        "query": query,
        "matches": matches,
        "total_matches": total,
        "truncated": total > len(matches),
    }


def cmd_cache_search(args: argparse.Namespace) -> int:
    json_out(cached_query_response(args, args.query))
    return 0


def cmd_cache_query(args: argparse.Namespace) -> int:
    json_out(cached_query_response(args, args.query))
    return 0


def add_common_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", help="Specific Reminders sqlite database path")


def add_common_cache(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cache",
        help=f"Cache JSON path (default: {CACHE_FILE})",
    )


def add_cache_query_args(parser: argparse.ArgumentParser, include_positional_query: bool) -> None:
    add_common_cache(parser)
    if include_positional_query:
        parser.add_argument("query")
    else:
        parser.add_argument("--query")
    parser.add_argument("--list")
    parser.add_argument("--section")
    parser.add_argument("--include-completed", action="store_true")
    parser.add_argument("--flagged", action=argparse.BooleanOptionalAction)
    parser.add_argument("--priority", type=int)
    parser.add_argument("--limit", type=int, default=20)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apple Reminders local JSON adapter")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("backup_store")
    p.add_argument("--output")
    p.set_defaults(func=cmd_backup_store)

    p = sub.add_parser("cache_rebuild")
    add_common_db(p)
    add_common_cache(p)
    p.set_defaults(func=cmd_cache_rebuild)

    p = sub.add_parser("cache_info")
    add_common_cache(p)
    p.set_defaults(func=cmd_cache_info)

    p = sub.add_parser("cache_search")
    add_cache_query_args(p, include_positional_query=True)
    p.set_defaults(func=cmd_cache_search)

    p = sub.add_parser("cache_query")
    add_cache_query_args(p, include_positional_query=False)
    p.set_defaults(func=cmd_cache_query)

    p = sub.add_parser("list_lists")
    add_common_db(p)
    p.set_defaults(func=cmd_list_lists)

    p = sub.add_parser("list_sections")
    add_common_db(p)
    p.add_argument("--list")
    p.set_defaults(func=cmd_list_sections)

    p = sub.add_parser("snapshot")
    add_common_db(p)
    p.add_argument("--list")
    p.add_argument("--include-completed", action="store_true")
    p.add_argument("--limit", type=int, default=1000)
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("search_reminders")
    add_common_db(p)
    p.add_argument("query")
    p.add_argument("--list")
    p.add_argument("--include-completed", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_search_reminders)

    p = sub.add_parser("read_reminder")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.set_defaults(func=cmd_read_reminder)

    p = sub.add_parser("list_tags")
    add_common_db(p)
    p.add_argument("--query")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_list_tags)

    p = sub.add_parser("add_tag")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--tag", required=True)
    p.set_defaults(func=cmd_add_tag)

    p = sub.add_parser("remove_tag")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--tag", required=True)
    p.set_defaults(func=cmd_remove_tag)

    p = sub.add_parser("cleanup_tags")
    add_common_db(p)
    p.add_argument("--tag")
    p.add_argument("--prefix")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_cleanup_tags)

    p = sub.add_parser("create_list")
    add_common_db(p)
    p.add_argument("--name", required=True)
    p.add_argument("--color")
    p.add_argument("--emblem")
    p.set_defaults(func=cmd_create_list)

    p = sub.add_parser("create_reminder")
    add_common_db(p)
    p.add_argument("--backend", choices=["db", "applescript"], default="db")
    p.add_argument("--list", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--notes")
    p.add_argument("--due-at")
    p.add_argument("--remind-at")
    p.add_argument("--all-day-due-date")
    p.add_argument("--flagged", action=argparse.BooleanOptionalAction)
    p.add_argument("--priority", type=int)
    p.set_defaults(func=cmd_create_reminder)

    p = sub.add_parser("update_reminder")
    add_common_db(p)
    p.add_argument("--backend", choices=["db", "applescript"], default="db")
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--new-title")
    p.add_argument("--notes")
    p.add_argument("--flagged", action=argparse.BooleanOptionalAction)
    p.add_argument("--priority", type=int)
    p.add_argument("--due-at")
    p.add_argument("--remind-at")
    p.add_argument("--all-day-due-date")
    p.add_argument("--clear-due", action="store_true")
    p.set_defaults(func=cmd_update_reminder)

    p = sub.add_parser("complete_reminder")
    add_common_db(p)
    p.add_argument("--backend", choices=["db", "applescript"], default="db")
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.set_defaults(func=cmd_complete_reminder)

    p = sub.add_parser("delete_reminder")
    add_common_db(p)
    p.add_argument("--backend", choices=["db", "applescript"], default="db")
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.set_defaults(func=cmd_delete_reminder)

    p = sub.add_parser("create_section")
    add_common_db(p)
    p.add_argument("--list")
    p.add_argument("--list-id")
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_create_section)

    p = sub.add_parser("move_to_section")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--section")
    p.add_argument("--section-id")
    p.set_defaults(func=cmd_move_to_section)

    p = sub.add_parser("attach_image")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--image", required=True)
    p.set_defaults(func=cmd_attach_image)

    p = sub.add_parser("attach_url")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--url", required=True)
    p.set_defaults(func=cmd_attach_url)

    p = sub.add_parser("list_attachments")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--type", choices=["image", "url"])
    p.set_defaults(func=cmd_list_attachments)

    p = sub.add_parser("delete_attachment")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--attachment-id")
    p.add_argument("--attachment-pk", type=int)
    p.add_argument("--type", choices=["image", "url"])
    p.add_argument("--filename")
    p.add_argument("--url")
    p.set_defaults(func=cmd_delete_attachment)

    p = sub.add_parser("replace_attachment")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--attachment-id")
    p.add_argument("--attachment-pk", type=int)
    p.add_argument("--type", choices=["image", "url"])
    p.add_argument("--filename")
    p.add_argument("--old-url")
    p.add_argument("--image")
    p.add_argument("--url")
    p.set_defaults(func=cmd_replace_attachment)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AdapterError as exc:
        return fail(str(exc))
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
