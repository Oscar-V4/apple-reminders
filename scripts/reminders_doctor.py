#!/usr/bin/env python3
"""Read-only Reminders store doctor for the Apple Reminders Codex plugin."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


GROUP = Path.home() / "Library/Group Containers/group.com.apple.reminders/Container_v1"
STORES = GROUP / "Stores"
FILES = GROUP / "Files"


REQUIRED_TABLES = {
    "ZREMCDREMINDER",
    "ZREMCDBASELIST",
    "ZREMCDBASESECTION",
    "ZREMCDOBJECT",
    "ZREMCKCLOUDSTATE",
    "Z_PRIMARYKEY",
}


def inspect_db(path: Path) -> dict:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    tables = {
        row["name"]
        for row in cur.execute("select name from sqlite_master where type='table'")
    }
    missing = sorted(REQUIRED_TABLES - tables)
    result = {
        "path": str(path),
        "missing_tables": missing,
        "usable": not missing,
    }
    if not missing:
        result["counts"] = {
            "lists": cur.execute(
                "select count(*) from ZREMCDBASELIST where coalesce(ZMARKEDFORDELETION,0)=0"
            ).fetchone()[0],
            "sections": cur.execute(
                "select count(*) from ZREMCDBASESECTION where coalesce(ZMARKEDFORDELETION,0)=0"
            ).fetchone()[0],
            "reminders": cur.execute(
                "select count(*) from ZREMCDREMINDER where coalesce(ZMARKEDFORDELETION,0)=0"
            ).fetchone()[0],
            "image_attachments": cur.execute(
                "select count(*) from ZREMCDOBJECT where Z_ENT=25 and coalesce(ZMARKEDFORDELETION,0)=0"
            ).fetchone()[0],
            "url_attachments": cur.execute(
                "select count(*) from ZREMCDOBJECT where Z_ENT=26 and coalesce(ZMARKEDFORDELETION,0)=0"
            ).fetchone()[0],
        }
    con.close()
    return result


def main() -> None:
    dbs = sorted(STORES.glob("*.sqlite"))
    payload = {
        "group_container_exists": GROUP.exists(),
        "stores_dir": str(STORES),
        "files_dir": str(FILES),
        "databases": [inspect_db(db) for db in dbs],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
