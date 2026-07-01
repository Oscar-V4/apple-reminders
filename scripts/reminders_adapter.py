#!/usr/bin/env python3
"""Dependency-light Apple Reminders adapter.

This is the core local adapter. It is intentionally a JSON CLI first; an MCP
server can wrap these commands later without owning the business logic.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any


HOME = Path.home()
GROUP = HOME / "Library/Group Containers/group.com.apple.reminders/Container_v1"
STORES = GROUP / "Stores"
FILES = GROUP / "Files"
APP_SUPPORT = HOME / "Library/Application Support/apple-reminders-codex"
JOURNAL = APP_SUPPORT / "actions.jsonl"
APPLE_EPOCH_OFFSET = 978307200

REQUIRED_TABLES = {
    "ZREMCDREMINDER",
    "ZREMCDBASELIST",
    "ZREMCDBASESECTION",
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


def normalize_uuid(value: str) -> str:
    if value.startswith("x-apple-reminder://"):
        value = value.removeprefix("x-apple-reminder://")
    return str(uuid.UUID(value)).upper()


def uuid_blob(value: str) -> bytes:
    return uuid.UUID(value).bytes


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


def update_primary_key(con: sqlite3.Connection, ent: int, new_max: int) -> None:
    con.execute("update Z_PRIMARYKEY set Z_MAX=max(Z_MAX, ?) where Z_ENT=?", (new_max, ent))


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
        "marked_for_deletion": bool(row["ZMARKEDFORDELETION"]),
    }
    if include_attachments:
        attachments = con.execute(
            """
            select Z_PK,ZCKIDENTIFIER,ZFILENAME,ZSHA512SUM,ZUTI,ZFILESIZE,ZWIDTH,ZHEIGHT,ZMARKEDFORDELETION
            from ZREMCDOBJECT
            where ZREMINDER2=? and Z_ENT=25 and coalesce(ZMARKEDFORDELETION,0)=0
            order by Z_FOK_REMINDER1, Z_PK
            """,
            (row["Z_PK"],),
        ).fetchall()
        payload["attachments"] = [dict(item) for item in attachments]
    return payload


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


def cmd_create_reminder(args: argparse.Namespace) -> int:
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
    log_action("create_reminder", {"id": rem_id, "list": args.list, "title": args.title})
    json_out({"ok": True, "id": rem_id, "title": args.title, "list": args.list})
    return 0


def cmd_update_reminder(args: argparse.Namespace) -> int:
    rem_id = args.id
    if not rem_id:
        db = main_db()
        con = connect(db)
        try:
            rem_id = f"x-apple-reminder://{find_reminder(con, title=args.title, list_name=args.list)['ZCKIDENTIFIER']}"
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
    log_action("update_reminder", {"id": out, "new_title": args.new_title, "notes_changed": args.notes is not None})
    json_out({"ok": True, "id": out})
    return 0


def cmd_complete_reminder(args: argparse.Namespace) -> int:
    rem_id = args.id
    if not rem_id:
        db = main_db()
        con = connect(db)
        try:
            rem_id = f"x-apple-reminder://{find_reminder(con, title=args.title, list_name=args.list)['ZCKIDENTIFIER']}"
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
    log_action("complete_reminder", {"id": out})
    json_out({"ok": True, "id": out, "completed": True})
    return 0


def cmd_delete_reminder(args: argparse.Namespace) -> int:
    rem_id = args.id
    if not rem_id:
        db = main_db()
        con = connect(db)
        try:
            rem_id = f"x-apple-reminder://{find_reminder(con, title=args.title, list_name=args.list)['ZCKIDENTIFIER']}"
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
    log_action("delete_reminder_native", {"id": out})
    json_out({"ok": True, "id": out, "deleted_via": "native_reminders"})
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


def cmd_attach_image(args: argparse.Namespace) -> int:
    db = Path(args.db).expanduser() if args.db else main_db()
    image = Path(args.image).expanduser().resolve()
    if not image.exists():
        raise AdapterError(f"Image not found: {image}")
    data = image.read_bytes()
    sha512 = hashlib.sha512(data).hexdigest()
    ext = image.suffix.lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    uti = "public.jpeg" if ext in {"jpeg", "jpg"} else "public.png"
    width, height = image_size(image)
    con = connect(db)
    try:
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        existing = con.execute(
            """
            select Z_PK,ZCKIDENTIFIER from ZREMCDOBJECT
            where ZREMINDER2=? and ZSHA512SUM=? and coalesce(ZMARKEDFORDELETION,0)=0
            """,
            (reminder["Z_PK"], sha512),
        ).fetchone()
        if existing:
            json_out({"ok": True, "db": str(db), "attached": False, "reason": "already_attached", "object": dict(existing)})
            return 0
        attach_dir = attachment_dir_for_account()
        stored = attach_dir / f"{sha512}.{ext}"
        if not stored.exists():
            shutil.copy2(image, stored)
        now = core_now()
        object_id = str(uuid.uuid4()).upper()
        display_filename = f"{object_id}-codex.{ext}"
        con.execute("begin immediate")
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
            ) values (?,25,1,0,0,0,0,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                object_pk,
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
            ) values (?,45,1,1,0,?,25,?)
            """,
            (cloud_pk, object_pk, now),
        )
        con.execute(
            "update ZREMCDREMINDER set Z_OPT=coalesce(Z_OPT,0)+1,ZLASTMODIFIEDDATE=? where Z_PK=?",
            (now, reminder["Z_PK"]),
        )
        con.execute(
            """
            update ZREMCKCLOUDSTATE
            set Z_OPT=coalesce(Z_OPT,0)+1,
                ZCURRENTLOCALVERSION=coalesce(ZCURRENTLOCALVERSION,0)+1,
                ZLOCALVERSIONDATE=?
            where Z_PK=?
            """,
            (now, reminder["ZCKCLOUDSTATE"]),
        )
        update_primary_key(con, 13, object_pk)
        update_primary_key(con, 45, cloud_pk)
        con.commit()
        log_action("attach_image", {"reminder": reminder["ZCKIDENTIFIER"], "image": str(image), "object": object_id, "stored": str(stored)})
        json_out(
            {
                "ok": True,
                "db": str(db),
                "attached": True,
                "reminder_id": reminder["ZCKIDENTIFIER"],
                "object_id": object_id,
                "stored_path": str(stored),
                "width": width,
                "height": height,
            }
        )
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def add_common_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", help="Specific Reminders sqlite database path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apple Reminders local JSON adapter")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("backup_store")
    p.add_argument("--output")
    p.set_defaults(func=cmd_backup_store)

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

    p = sub.add_parser("create_reminder")
    p.add_argument("--list", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--notes")
    p.set_defaults(func=cmd_create_reminder)

    p = sub.add_parser("update_reminder")
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--new-title")
    p.add_argument("--notes")
    p.add_argument("--flagged", action=argparse.BooleanOptionalAction)
    p.add_argument("--priority", type=int)
    p.set_defaults(func=cmd_update_reminder)

    p = sub.add_parser("complete_reminder")
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.set_defaults(func=cmd_complete_reminder)

    p = sub.add_parser("delete_reminder")
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
