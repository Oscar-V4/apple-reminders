#!/usr/bin/env python3
"""Dependency-light Apple Reminders adapter.

This is the core local adapter. It remains a JSON CLI/library behind the
bundled MCP server; the transport layer does not own its business logic.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
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
CAPABILITY_RECORD = APP_SUPPORT / "verified-capabilities.json"
IDEMPOTENCY_STORE = APP_SUPPORT / "idempotency.json"
IDEMPOTENCY_LOCK = APP_SUPPORT / "idempotency.lock"
CACHE_DIR = HOME / "Library/Caches/apple-reminders-codex"
CACHE_FILE = CACHE_DIR / "cache.json"
CACHE_VERSION = 1
APPLE_EPOCH_OFFSET = 978307200
IMAGE_ATTACHMENT_ENT = 25
URL_ATTACHMENT_ENT = 26
TAG_OBJECT_ENT = 32
SUBPROCESS_TIMEOUT_SECONDS = 30
ATTACHMENT_VERIFY_TIMEOUT_SECONDS = 6
JOURNAL_MAX_BYTES = 1_000_000
JOURNAL_RETENTION_DAYS = 30
IDEMPOTENCY_RETENTION_DAYS = 30
IDEMPOTENCY_MAX_ENTRIES = 500

RESULT_STATUSES = {
    "unchanged",
    "verified",
    "committed_verification_pending",
    "partial_success",
    "failed_no_mutation",
    "failed_manual_repair_required",
}

ERROR_CODES = {
    "ambiguous_scope",
    "ambiguous_target",
    "concurrent_modification",
    "invalid_input",
    "permission_denied",
    "schema_mismatch",
    "sync_pending",
    "unsupported_capability",
    "unexpected_error",
}

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
    def __init__(self, message: str, *, code: str = "invalid_input", **details: Any) -> None:
        super().__init__(message)
        self.code = code if code in ERROR_CODES else "unexpected_error"
        self.details = details


class AttachmentVerificationError(AdapterError):
    def __init__(self, message: str, row: dict[str, Any], **details: Any) -> None:
        code = details.pop("code", "sync_pending")
        super().__init__(message, code=code, **details)
        self.row = row

    def compensation_result(self) -> dict[str, Any]:
        return {"attachment": self.details.get("attachment", {}), "_row": self.row}


def json_out(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def fail(
    message: str,
    *,
    code: str = "unexpected_error",
    status: str = "failed_no_mutation",
    **extra: Any,
) -> int:
    if status not in {"failed_no_mutation", "failed_manual_repair_required"}:
        status = "failed_no_mutation"
    json_out(
        {
            "ok": False,
            "status": status,
            "error": {"code": code, "message": message, **extra},
        }
    )
    return 1


def new_operation_id() -> str:
    return str(uuid.uuid4()).upper()


def operation_receipt(
    *,
    status: str,
    operation: str,
    backend: str,
    target: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    recovery: dict[str, Any] | None = None,
    warnings: list[dict[str, Any] | str] | None = None,
    operation_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    if status not in RESULT_STATUSES:
        raise ValueError(f"Unsupported operation status: {status}")
    payload: dict[str, Any] = {
        "ok": status not in {"failed_no_mutation", "failed_manual_repair_required"},
        "status": status,
        "operation": operation,
        "operation_id": operation_id or new_operation_id(),
        "backend": backend,
        "target": target or {},
        "verification": verification or {"state": "not_requested"},
        "recovery": recovery or {"semantics": "not_applicable"},
    }
    if before is not None:
        payload["before"] = before
    if after is not None:
        payload["after"] = after
    if warnings:
        payload["warnings"] = warnings
    payload.update(extra)
    return payload


def ensure_private_dir(path: Path) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    is_plugin_data = (
        path == APP_SUPPORT
        or APP_SUPPORT in path.parents
        or path == CACHE_DIR
        or CACHE_DIR in path.parents
    )
    if not existed or is_plugin_data:
        path.chmod(0o700)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def reminder_priority(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 9:
        raise argparse.ArgumentTypeError("must be between 0 and 9")
    return parsed


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
        raise AdapterError(
            f"Datetime must include an explicit UTC offset or Z: {value}",
            code="invalid_input",
        )
    return parsed.astimezone()


def core_from_datetime(value: dt.datetime) -> float:
    return value.timestamp() - APPLE_EPOCH_OFFSET


def schedule_values(
    due_at: str | None = None,
    remind_at: str | None = None,
    all_day_due_date: str | None = None,
    clear_due: bool = False,
) -> dict[str, Any] | None:
    if remind_at is not None:
        raise AdapterError(
            "The private SQLite schedule path cannot represent an alert independently from a due date; use the EventKit alarms field",
            code="unsupported_capability",
            remediation="Use the MCP/EventKit create or update tool with an absolute or location alarm.",
        )
    provided = [item is not None for item in (due_at, remind_at, all_day_due_date)].count(True)
    if clear_due and provided:
        raise AdapterError("--clear-due cannot be combined with due date options")
    if provided > 1:
        raise AdapterError("Use only one of --due-at or --all-day-due-date")
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
    timestamp_text = due_at
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
    path = Path(db).expanduser()
    if not path.exists():
        raise AdapterError(f"Reminders database not found: {path}")
    uri = f"file:{urllib.parse.quote(str(path.resolve()))}?mode=rw"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("pragma busy_timeout=5000")
    return con


def resolve_database(value: str | None, *, write: bool = False) -> Path:
    path = Path(value).expanduser().resolve() if value else main_db().resolve()
    if not path.exists():
        raise AdapterError(f"Reminders database not found: {path}")
    if write:
        stores = STORES.expanduser().resolve()
        try:
            path.relative_to(stores)
        except ValueError as exc:
            raise AdapterError(
                "Refusing to write to a database outside the Reminders store",
                database=path.name,
            ) from exc
    return path


def table_names(con: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in con.execute("select name from sqlite_master where type='table'")
    }


def column_names(con: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in con.execute(f"pragma table_info({table})")}


COMMAND_SCHEMA_REQUIREMENTS: dict[str, dict[str, set[str]]] = {
    "create_reminder_db": {
        "ZREMCDREMINDER": {
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
        },
        "ZREMCDBASELIST": {
            "Z_PK",
            "Z_OPT",
            "ZNAME",
            "ZACCOUNT",
            "ZCKCLOUDSTATE",
            "ZCKIDENTIFIER",
            "ZMARKEDFORDELETION",
            "ZREMINDERIDSMERGEABLEORDERING_V2_JSON",
        },
        "ZREMCKCLOUDSTATE": {
            "Z_PK",
            "Z_ENT",
            "Z_OPT",
            "ZCURRENTLOCALVERSION",
            "ZLATESTVERSIONSYNCEDTOCLOUD",
            "ZREMINDER",
            "ZLOCALVERSIONDATE",
        },
        "Z_PRIMARYKEY": {"Z_ENT", "Z_NAME", "Z_MAX"},
    },
    "delete_reminder_db": {
        "ZREMCDREMINDER": {
            "Z_PK",
            "Z_OPT",
            "ZCKIDENTIFIER",
            "ZCKCLOUDSTATE",
            "ZLASTMODIFIEDDATE",
            "ZLIST",
            "Z_FOK_LIST",
            "ZMARKEDFORDELETION",
        },
        "ZREMCDBASELIST": {
            "Z_PK",
            "Z_OPT",
            "ZCKCLOUDSTATE",
            "ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA",
            "ZREMINDERIDSMERGEABLEORDERING_V2_JSON",
        },
        "ZREMCKCLOUDSTATE": {
            "Z_PK",
            "Z_OPT",
            "ZCURRENTLOCALVERSION",
            "ZLOCALVERSIONDATE",
        },
    },
    "cleanup_tags": {
        "ZREMCDHASHTAGLABEL": {
            "Z_PK",
            "ZNAME",
            "ZCANONICALNAME",
            "ZACCOUNTIDENTIFIER",
            "ZUUIDFORCHANGETRACKING",
        },
        "ZREMCDOBJECT": {
            "Z_PK",
            "Z_ENT",
            "ZHASHTAGLABEL",
            "ZMARKEDFORDELETION",
        },
    },
}

COMMAND_SCHEMA_REQUIREMENTS.update(
    {
        "update_reminder_db": {
            "ZREMCDREMINDER": {
                "Z_PK",
                "Z_OPT",
                "ZCKIDENTIFIER",
                "ZCKCLOUDSTATE",
                "ZLASTMODIFIEDDATE",
                "ZTITLE",
                "ZTITLEDOCUMENT",
                "ZNOTES",
                "ZNOTESDOCUMENT",
                "ZFLAGGED",
                "ZPRIORITY",
                "ZDUEDATE",
                "ZDISPLAYDATEDATE",
                "ZALLDAY",
                "ZDISPLAYDATEISALLDAY",
                "ZDISPLAYDATETIMEZONE",
                "ZTIMEZONE",
            },
            "ZREMCKCLOUDSTATE": {
                "Z_PK",
                "Z_OPT",
                "ZCURRENTLOCALVERSION",
                "ZLOCALVERSIONDATE",
            },
        },
        "set_completion_db": {
            "ZREMCDREMINDER": {
                "Z_PK",
                "Z_OPT",
                "ZCKIDENTIFIER",
                "ZCKCLOUDSTATE",
                "ZCOMPLETED",
                "ZCOMPLETIONDATE",
                "ZLASTMODIFIEDDATE",
            },
            "ZREMCKCLOUDSTATE": {
                "Z_PK",
                "Z_OPT",
                "ZCURRENTLOCALVERSION",
                "ZLOCALVERSIONDATE",
            },
        },
        "tag_assignment_db": {
            "ZREMCDREMINDER": {
                "Z_PK",
                "Z_OPT",
                "ZACCOUNT",
                "ZCKCLOUDSTATE",
                "ZCKIDENTIFIER",
                "ZLASTMODIFIEDDATE",
            },
            "ZREMCDHASHTAGLABEL": {
                "Z_PK",
                "Z_ENT",
                "Z_OPT",
                "ZACCOUNTIDENTIFIER",
                "ZCANONICALNAME",
                "ZNAME",
                "ZUUIDFORCHANGETRACKING",
            },
            "ZREMCDOBJECT": {
                "Z_PK",
                "Z_ENT",
                "Z_OPT",
                "ZMARKEDFORDELETION",
                "ZACCOUNT",
                "ZCKCLOUDSTATE",
                "ZHASHTAGLABEL",
                "ZREMINDER3",
                "ZIDENTIFIER",
                "ZCKIDENTIFIER",
            },
            "ZREMCKCLOUDSTATE": {
                "Z_PK",
                "Z_ENT",
                "Z_OPT",
                "ZCURRENTLOCALVERSION",
                "ZLATESTVERSIONSYNCEDTOCLOUD",
                "ZOBJECT",
                "Z13_OBJECT",
                "ZLOCALVERSIONDATE",
            },
            "Z_PRIMARYKEY": {"Z_ENT", "Z_NAME", "Z_MAX"},
        },
        "create_section_db": {
            "ZREMCDBASELIST": {
                "Z_PK",
                "ZACCOUNT",
                "ZCKIDENTIFIER",
                "ZNAME",
                "ZMARKEDFORDELETION",
            },
            "ZREMCDBASESECTION": {
                "Z_PK",
                "Z_ENT",
                "Z_OPT",
                "ZACCOUNT",
                "ZCKCLOUDSTATE",
                "ZLIST",
                "Z_FOK_LIST",
                "ZCREATIONDATE",
                "ZCKIDENTIFIER",
                "ZDISPLAYNAME",
                "ZIDENTIFIER",
                "ZRESOLUTIONTOKENMAP_V3_JSONDATA",
                "ZMARKEDFORDELETION",
            },
            "ZREMCKCLOUDSTATE": {
                "Z_PK",
                "Z_ENT",
                "Z_OPT",
                "ZCURRENTLOCALVERSION",
                "ZLATESTVERSIONSYNCEDTOCLOUD",
                "ZSECTION",
                "Z5_SECTION",
                "ZLOCALVERSIONDATE",
            },
            "Z_PRIMARYKEY": {"Z_ENT", "Z_NAME", "Z_MAX"},
        },
        "move_to_section_db": {
            "ZREMCDREMINDER": {
                "Z_PK",
                "Z_OPT",
                "ZCKIDENTIFIER",
                "ZLIST",
                "ZMARKEDFORDELETION",
            },
            "ZREMCDBASELIST": {
                "Z_PK",
                "Z_OPT",
                "ZCKCLOUDSTATE",
                "ZCKIDENTIFIER",
                "ZNAME",
                "ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA",
            },
            "ZREMCDBASESECTION": {
                "Z_PK",
                "ZCKIDENTIFIER",
                "ZDISPLAYNAME",
                "ZLIST",
                "ZMARKEDFORDELETION",
            },
            "ZREMCKCLOUDSTATE": {
                "Z_PK",
                "ZCURRENTLOCALVERSION",
                "ZLOCALVERSIONDATE",
            },
        },
        "attachment_mutation_db": {
            "ZREMCDREMINDER": {
                "Z_PK",
                "Z_OPT",
                "ZACCOUNT",
                "ZCKCLOUDSTATE",
                "ZCKIDENTIFIER",
                "ZLASTMODIFIEDDATE",
                "ZMARKEDFORDELETION",
            },
            "ZREMCDOBJECT": {
                "Z_PK",
                "Z_ENT",
                "Z_OPT",
                "ZACCOUNT",
                "ZCKCLOUDSTATE",
                "ZCKIDENTIFIER",
                "ZIDENTIFIER",
                "ZREMINDER2",
                "Z_FOK_REMINDER1",
                "ZMARKEDFORDELETION",
                "ZFILENAME",
                "ZSHA512SUM",
                "ZUTI",
                "ZFILESIZE",
                "ZWIDTH",
                "ZHEIGHT",
                "ZURL",
                "ZHOSTURL",
            },
            "ZREMCKCLOUDSTATE": {
                "Z_PK",
                "Z_ENT",
                "Z_OPT",
                "ZCURRENTLOCALVERSION",
                "ZLATESTVERSIONSYNCEDTOCLOUD",
                "ZOBJECT",
                "Z13_OBJECT",
                "ZLOCALVERSIONDATE",
            },
            "Z_PRIMARYKEY": {"Z_ENT", "Z_NAME", "Z_MAX"},
        },
    }
)


def command_capability(con: sqlite3.Connection, command: str) -> dict[str, Any]:
    requirements = COMMAND_SCHEMA_REQUIREMENTS.get(command, {})
    existing_tables = table_names(con)
    missing_tables = sorted(set(requirements) - existing_tables)
    missing_columns: dict[str, list[str]] = {}
    if not missing_tables:
        for table, required in requirements.items():
            missing = sorted(required - column_names(con, table))
            if missing:
                missing_columns[table] = missing
    supported = not missing_tables and not missing_columns
    fingerprint_payload = {
        table: sorted(column_names(con, table))
        for table in sorted(requirements)
        if table in existing_tables
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "command": command,
        "supported": supported,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "schema_fingerprint": fingerprint,
    }


def require_command_capability(con: sqlite3.Connection, command: str) -> dict[str, Any]:
    capability = command_capability(con, command)
    if not capability["supported"]:
        raise AdapterError(
            f"Reminders schema does not support {command}",
            code="schema_mismatch",
            capability=capability,
        )
    return capability


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


def db_counts(db: Path) -> dict[str, int | None]:
    con = connect(db)
    try:
        counts = {
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
        counts.update(image_attachment_sync_counts(con))
        return counts
    finally:
        con.close()


def main_db() -> Path:
    dbs = usable_dbs()
    if not dbs:
        raise AdapterError("No usable Reminders databases found")
    return max(dbs, key=lambda path: (db_counts(path)["reminders"], db_counts(path)["lists"]))


def reminders_build_info() -> dict[str, str | None]:
    info_path = Path("/System/Applications/Reminders.app/Contents/Info.plist")
    try:
        with info_path.open("rb") as fh:
            payload = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException):
        return {"version": None, "build": None}
    return {
        "version": payload.get("CFBundleShortVersionString"),
        "build": payload.get("CFBundleVersion"),
    }


def capability_identity(schema_fingerprint: str) -> dict[str, Any]:
    app = reminders_build_info()
    return {
        "macos_version": platform.mac_ver()[0] or None,
        "reminders_version": app["version"],
        "reminders_build": app["build"],
        "schema_fingerprint": schema_fingerprint,
    }


def load_verified_capabilities() -> dict[str, Any]:
    try:
        with CAPABILITY_RECORD.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"version": 1, "records": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        return {"version": 1, "records": []}
    return payload


def db_soft_delete_verified(con: sqlite3.Connection) -> tuple[bool, dict[str, Any]]:
    capability = command_capability(con, "delete_reminder_db")
    if not capability["supported"]:
        return False, {"capability": capability, "verified": False}
    identity = capability_identity(capability["schema_fingerprint"])
    matched = False
    for record in load_verified_capabilities().get("records", []):
        if not isinstance(record, dict):
            continue
        if record.get("capability") != "db_soft_delete_recently_deleted":
            continue
        if record.get("verified") is not True:
            continue
        if all(record.get(key) == value for key, value in identity.items()):
            matched = True
            break
    return matched, {"identity": identity, "verified": matched}


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


SENSITIVE_LOG_KEY = re.compile(
    r"(?:title|name|notes?|url|path|image|filename|list|section|tag|deleted|payload|database|db)$",
    re.IGNORECASE,
)


def redacted_log_value(value: Any) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(value).encode("utf-8", errors="replace")
    metadata: dict[str, Any] = {
        "redacted": True,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
    }
    if isinstance(value, (list, tuple, set, dict)):
        metadata["items"] = len(value)
    return metadata


def redact_log_payload(value: Any, *, key: str | None = None) -> Any:
    if key and SENSITIVE_LOG_KEY.search(key):
        return redacted_log_value(value)
    if isinstance(value, dict):
        return {str(item_key): redact_log_payload(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_log_payload(item) for item in value]
    return value


def journal_paths() -> list[Path]:
    return [JOURNAL, JOURNAL.with_name(f"{JOURNAL.name}.1")]


def rotate_journal_if_needed() -> None:
    if not JOURNAL.exists() or JOURNAL.stat().st_size < JOURNAL_MAX_BYTES:
        return
    rotated = JOURNAL.with_name(f"{JOURNAL.name}.1")
    rotated.unlink(missing_ok=True)
    os.replace(JOURNAL, rotated)
    rotated.chmod(0o600)


def purge_expired_journals(*, now: float | None = None) -> list[str]:
    cutoff = (now if now is not None else time.time()) - JOURNAL_RETENTION_DAYS * 86400
    removed: list[str] = []
    for path in journal_paths():
        try:
            if path.exists() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path.name)
        except OSError:
            continue
    return removed


def log_action(action: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Write a redacted audit entry without changing a committed mutation into failure."""

    try:
        ensure_private_dir(APP_SUPPORT)
        purge_expired_journals()
        rotate_journal_if_needed()
        entry = {
            "time": dt.datetime.now().astimezone().isoformat(),
            "action": action,
            "payload": redact_log_payload(payload),
        }
        with JOURNAL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        JOURNAL.chmod(0o600)
        return None
    except OSError as exc:
        return {
            "code": "journal_write_failed",
            "message": "The operation completed, but its redacted local audit entry could not be written.",
            "detail": type(exc).__name__,
        }


def cmd_purge_logs(_: argparse.Namespace) -> int:
    operation_id = new_operation_id()
    removed: list[str] = []
    errors: list[dict[str, str]] = []
    for path in journal_paths():
        try:
            if path.exists():
                path.unlink()
                removed.append(path.name)
        except OSError as exc:
            errors.append({"file": path.name, "error": type(exc).__name__})
    json_out(
        operation_receipt(
            status="verified" if not errors else "partial_success",
            operation="purge_logs",
            operation_id=operation_id,
            backend="local_filesystem",
            target={"files": [path.name for path in journal_paths()]},
            before={"existing_files": sorted([*removed, *[item["file"] for item in errors]])},
            after={"removed": removed, "errors": errors},
            verification={
                "state": "read_back",
                "remaining_files": [path.name for path in journal_paths() if path.exists()],
            },
            recovery={"semantics": "irreversible_redacted_log_purge"},
        )
    )
    return 0 if not errors else 1


def stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def idempotency_result_snapshot(value: Any, *, key: str | None = None) -> Any:
    """Keep retry-critical identifiers/status while excluding user-authored content."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for item_key, item in value.items():
            normalized = str(item_key).casefold()
            keep = (
                normalized
                in {
                    "ok",
                    "status",
                    "operation",
                    "operation_id",
                    "backend",
                    "backend_requested",
                    "id",
                    "pk",
                    "count",
                    "state",
                    "semantics",
                    "reason",
                    "verified",
                    "replayed",
                }
                or normalized.endswith("_id")
                or normalized.endswith("_ids")
                or normalized.endswith("_pk")
                or normalized.endswith("_count")
            )
            if keep:
                result[str(item_key)] = idempotency_result_snapshot(item, key=str(item_key))
            elif isinstance(item, (dict, list, tuple)):
                nested = idempotency_result_snapshot(item, key=str(item_key))
                if nested not in ({}, []):
                    result[str(item_key)] = nested
        return result
    if isinstance(value, (list, tuple)):
        return [idempotency_result_snapshot(item, key=key) for item in value]
    return value


def load_idempotency_store() -> dict[str, Any]:
    try:
        with IDEMPOTENCY_STORE.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"version": 1, "entries": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), dict):
        return {"version": 1, "entries": {}}
    return payload


def prune_idempotency_entries(entries: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    current = now if now is not None else time.time()
    cutoff = current - IDEMPOTENCY_RETENTION_DAYS * 86400
    retained = {
        key: value
        for key, value in entries.items()
        if isinstance(value, dict) and float(value.get("created_at_epoch", 0)) >= cutoff
    }
    ordered = sorted(
        retained.items(),
        key=lambda item: float(item[1].get("created_at_epoch", 0)),
        reverse=True,
    )[:IDEMPOTENCY_MAX_ENTRIES]
    return dict(ordered)


def write_idempotency_store(payload: dict[str, Any]) -> None:
    ensure_private_dir(APP_SUPPORT)
    temp_handle = tempfile.NamedTemporaryFile(
        prefix=".idempotency.",
        suffix=".tmp",
        dir=APP_SUPPORT,
        mode="w",
        encoding="utf-8",
        delete=False,
    )
    temp_path = Path(temp_handle.name)
    try:
        with temp_handle as fh:
            json.dump(payload, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        temp_path.chmod(0o600)
        os.replace(temp_path, IDEMPOTENCY_STORE)
        IDEMPOTENCY_STORE.chmod(0o600)
    finally:
        temp_path.unlink(missing_ok=True)


def with_idempotency_lock(callback: Any) -> Any:
    ensure_private_dir(APP_SUPPORT)
    with IDEMPOTENCY_LOCK.open("a+", encoding="utf-8") as lock:
        IDEMPOTENCY_LOCK.chmod(0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return callback()


def idempotency_lookup(
    *,
    operation: str,
    key: str | None,
    input_payload: dict[str, Any],
) -> dict[str, Any] | None:
    if not key:
        return None
    key_hash = stable_hash({"operation": operation, "key": key})
    input_hash = stable_hash(input_payload)

    def lookup() -> dict[str, Any] | None:
        payload = load_idempotency_store()
        entries = prune_idempotency_entries(payload.get("entries", {}))
        record = entries.get(key_hash)
        if not record:
            return None
        if record.get("input_hash") != input_hash:
            raise AdapterError(
                "Idempotency key was already used with different input",
                code="concurrent_modification",
                operation=operation,
            )
        result = dict(record.get("result") or {})
        result["replayed"] = True
        result["idempotency_key_hash"] = key_hash
        return result

    return with_idempotency_lock(lookup)


def idempotency_store_result(
    *,
    operation: str,
    key: str | None,
    input_payload: dict[str, Any],
    result: dict[str, Any],
) -> None:
    if not key:
        return
    key_hash = stable_hash({"operation": operation, "key": key})
    input_hash = stable_hash(input_payload)

    def store_result() -> None:
        payload = load_idempotency_store()
        entries = prune_idempotency_entries(payload.get("entries", {}))
        existing = entries.get(key_hash)
        if existing and existing.get("input_hash") != input_hash:
            raise AdapterError(
                "Idempotency key was already used with different input",
                code="concurrent_modification",
                operation=operation,
            )
        entries[key_hash] = {
            "operation": operation,
            "input_hash": input_hash,
            "created_at_epoch": time.time(),
            "result": idempotency_result_snapshot(result),
        }
        write_idempotency_store({"version": 1, "entries": prune_idempotency_entries(entries)})

    with_idempotency_lock(store_result)


def execute_idempotent(
    *,
    operation: str,
    key: str | None,
    input_payload: dict[str, Any],
    callback: Any,
) -> dict[str, Any]:
    if not key:
        return callback()
    ensure_private_dir(APP_SUPPORT)
    key_hash = stable_hash({"operation": operation, "key": key})
    input_hash = stable_hash(input_payload)
    with IDEMPOTENCY_LOCK.open("a+", encoding="utf-8") as lock:
        IDEMPOTENCY_LOCK.chmod(0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        payload = load_idempotency_store()
        entries = prune_idempotency_entries(payload.get("entries", {}))
        record = entries.get(key_hash)
        if record:
            if record.get("input_hash") != input_hash:
                raise AdapterError(
                    "Idempotency key was already used with different input",
                    code="concurrent_modification",
                    operation=operation,
                )
            replay = dict(record.get("result") or {})
            replay["replayed"] = True
            replay["idempotency_key_hash"] = key_hash
            return replay

        result = callback()
        entries[key_hash] = {
            "operation": operation,
            "input_hash": input_hash,
            "created_at_epoch": time.time(),
            "result": idempotency_result_snapshot(result),
        }
        try:
            write_idempotency_store({"version": 1, "entries": prune_idempotency_entries(entries)})
        except OSError as exc:
            result.setdefault("warnings", []).append(
                {
                    "code": "idempotency_receipt_write_failed",
                    "message": "The mutation completed, but its local retry receipt could not be persisted.",
                    "detail": type(exc).__name__,
                }
            )
        result["idempotency_key_hash"] = key_hash
        return result


def run_osascript(script: str, args: list[str], *, mutation: bool = False) -> str:
    try:
        proc = subprocess.run(
            ["osascript", "-e", script, *args],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdapterError(
            "Reminders AppleScript timed out",
            code="sync_pending" if mutation else "unexpected_error",
            partial_failure=mutation,
            mutation_outcome_unknown=mutation,
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "osascript failed"
        lowered = detail.casefold()
        permission_denied = any(
            marker in lowered
            for marker in ("not authorized", "not permitted", "permission", "-1743")
        )
        raise AdapterError(
            detail,
            code="permission_denied" if permission_denied else "unexpected_error",
            partial_failure=mutation and not permission_denied,
            mutation_outcome_unknown=mutation and not permission_denied,
        )
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
        mutation=True,
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
        raise AdapterError("Reminder not found", code="invalid_input")
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
        raise AdapterError(
            "Multiple reminders matched; use an id",
            code="ambiguous_target",
            candidates=candidates,
        )
    return dict(rows[0])


def require_exact_reminder_selector(
    *,
    reminder_id: str | None,
    title: str | None,
    list_name: str | None,
) -> None:
    if reminder_id:
        return
    if title and list_name:
        return
    raise AdapterError(
        "Use an exact reminder id, or provide both title and list",
        code="ambiguous_target",
        required_selector="id | (title + list)",
    )


def reminder_mutation_snapshot(reminder: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": reminder.get("ZCKIDENTIFIER"),
        "title": reminder.get("ZTITLE"),
        "list": reminder.get("LIST_NAME"),
        "list_pk": reminder.get("ZLIST"),
        "completed": bool(reminder.get("ZCOMPLETED")),
        "completed_at": core_to_iso(reminder.get("ZCOMPLETIONDATE")),
        "flagged": bool(reminder.get("ZFLAGGED")),
        "priority": reminder.get("ZPRIORITY"),
        "due_at": core_to_iso(reminder.get("ZDUEDATE")),
        "display_at": core_to_iso(reminder.get("ZDISPLAYDATEDATE")),
        "all_day": bool(reminder.get("ZALLDAY")),
        "timezone": reminder.get("ZTIMEZONE"),
        "has_notes": bool(reminder.get("ZNOTES")),
        "marked_for_deletion": bool(reminder.get("ZMARKEDFORDELETION")),
        "version": reminder.get("Z_OPT"),
        "last_modified_at": core_to_iso(reminder.get("ZLASTMODIFIEDDATE")),
    }


def require_reminder_version(reminder: dict[str, Any], expected: int | None) -> None:
    if expected is not None and reminder.get("Z_OPT") != expected:
        raise AdapterError(
            "Reminder changed since it was read",
            code="concurrent_modification",
            expected_version=expected,
            current_version=reminder.get("Z_OPT"),
        )


def reread_reminder(con: sqlite3.Connection, reminder_pk: int) -> dict[str, Any]:
    row = con.execute(
        """
        select r.*, l.ZNAME as LIST_NAME
        from ZREMCDREMINDER r
        left join ZREMCDBASELIST l on l.Z_PK=r.ZLIST
        where r.Z_PK=?
        """,
        (reminder_pk,),
    ).fetchone()
    if not row:
        raise AdapterError(
            "Reminder disappeared during read-back",
            code="concurrent_modification",
            reminder_pk=reminder_pk,
        )
    return dict(row)


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


def attachment_sync_capabilities(con: sqlite3.Connection) -> dict[str, Any]:
    object_columns = column_names(con, "ZREMCDOBJECT")
    cloud_columns = column_names(con, "ZREMCKCLOUDSTATE")
    required_object = {"ZCKSERVERRECORDDATA"}
    required_cloud = {
        "ZINCLOUD",
        "ZCURRENTLOCALVERSION",
        "ZLATESTVERSIONSYNCEDTOCLOUD",
    }
    missing = sorted(
        [f"ZREMCDOBJECT.{name}" for name in required_object - object_columns]
        + [f"ZREMCKCLOUDSTATE.{name}" for name in required_cloud - cloud_columns]
    )
    return {"available": not missing, "missing_columns": missing}


def attachment_sync_select(con: sqlite3.Connection) -> str:
    capabilities = attachment_sync_capabilities(con)
    if not capabilities["available"]:
        return """
               0 as SYNC_FIELDS_AVAILABLE,
               null as HAS_SERVER_RECORD,
               null as SERVER_RECORD_BYTES,
               null as ZINCLOUD,
               null as ZCURRENTLOCALVERSION,
               null as ZLATESTVERSIONSYNCEDTOCLOUD
        """
    return """
               1 as SYNC_FIELDS_AVAILABLE,
               o.ZCKSERVERRECORDDATA is not null as HAS_SERVER_RECORD,
               length(o.ZCKSERVERRECORDDATA) as SERVER_RECORD_BYTES,
               cs.ZINCLOUD,cs.ZCURRENTLOCALVERSION,cs.ZLATESTVERSIONSYNCEDTOCLOUD
    """


def attachment_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    ent = int(data["Z_ENT"])
    sync_fields_available = data.get("SYNC_FIELDS_AVAILABLE")
    if sync_fields_available is None:
        sync_fields_available = any(
            key in data
            for key in (
                "HAS_SERVER_RECORD",
                "has_server_record",
                "ZINCLOUD",
                "ZCURRENTLOCALVERSION",
                "ZLATESTVERSIONSYNCEDTOCLOUD",
            )
        )
    has_server_record = (
        bool(data.get("HAS_SERVER_RECORD") or data.get("has_server_record"))
        if sync_fields_available
        else None
    )
    server_record_bytes = data.get("SERVER_RECORD_BYTES") or data.get("server_record_bytes")
    in_cloud = data.get("ZINCLOUD")
    current_local_version = data.get("ZCURRENTLOCALVERSION")
    latest_synced_version = data.get("ZLATESTVERSIONSYNCEDTOCLOUD")
    mobile_visible_likely = None
    if ent == IMAGE_ATTACHMENT_ENT and sync_fields_available:
        mobile_visible_likely = bool(has_server_record and in_cloud == 1)
    payload = {
        "pk": data["Z_PK"],
        "id": data["ZCKIDENTIFIER"],
        "type": attachment_type_for_ent(ent),
        "uti": data["ZUTI"],
        "order": data["Z_FOK_REMINDER1"],
        "marked_for_deletion": bool(data["ZMARKEDFORDELETION"]),
        "sync": {
            "mobile_visible_likely": mobile_visible_likely,
            "has_server_record": has_server_record,
            "server_record_bytes": server_record_bytes,
            "in_cloud": in_cloud,
            "current_local_version": current_local_version,
            "latest_synced_version": latest_synced_version,
            "fields_available": bool(sync_fields_available),
        },
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


def image_attachment_sync_counts(con: sqlite3.Connection) -> dict[str, int | None]:
    capabilities = attachment_sync_capabilities(con)
    if not capabilities["available"]:
        total = con.execute(
            """
            select count(*)
            from ZREMCDOBJECT
            where Z_ENT=? and coalesce(ZMARKEDFORDELETION,0)=0
            """,
            (IMAGE_ATTACHMENT_ENT,),
        ).fetchone()[0]
        return {
            "mobile_visible_image_attachments": None,
            "local_only_image_attachments": None,
            "unverified_image_attachments": int(total),
        }
    row = con.execute(
        """
        select
          count(*) as total,
          coalesce(sum(
            case
              when o.ZCKSERVERRECORDDATA is null or coalesce(cs.ZINCLOUD,0)<>1 then 1
              else 0
            end
          ),0) as local_only
        from ZREMCDOBJECT o
        left join ZREMCKCLOUDSTATE cs on cs.Z_PK=o.ZCKCLOUDSTATE
        where o.Z_ENT=? and coalesce(o.ZMARKEDFORDELETION,0)=0
        """,
        (IMAGE_ATTACHMENT_ENT,),
    ).fetchone()
    return {
        "mobile_visible_image_attachments": int(row["total"] - row["local_only"]),
        "local_only_image_attachments": int(row["local_only"]),
        "unverified_image_attachments": 0,
    }


def active_attachment_rows(
    con: sqlite3.Connection,
    reminder_pk: int,
    attachment_ent: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = [reminder_pk, IMAGE_ATTACHMENT_ENT, URL_ATTACHMENT_ENT]
    where = [
        "o.ZREMINDER2=?",
        "o.Z_ENT in (?,?)",
        "coalesce(o.ZMARKEDFORDELETION,0)=0",
    ]
    if attachment_ent is not None:
        where.append("o.Z_ENT=?")
        params.append(attachment_ent)
    limit_sql = ""
    if limit is not None:
        limit_sql = "limit ?"
        params.append(limit)
    sync_select = attachment_sync_select(con)
    rows = con.execute(
        f"""
        select o.Z_PK,o.Z_ENT,o.ZCKIDENTIFIER,o.ZCKCLOUDSTATE,o.ZREMINDER2,o.Z_FOK_REMINDER1,
               o.ZFILENAME,o.ZSHA512SUM,o.ZUTI,o.ZFILESIZE,o.ZWIDTH,o.ZHEIGHT,o.ZURL,o.ZHOSTURL,
               o.ZMARKEDFORDELETION,
               {sync_select}
        from ZREMCDOBJECT o
        left join ZREMCKCLOUDSTATE cs on cs.Z_PK=o.ZCKCLOUDSTATE
        where {" and ".join(where)}
        order by o.Z_FOK_REMINDER1, o.Z_PK
        {limit_sql}
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def image_attachment_audit_items(
    con: sqlite3.Connection,
    *,
    search: str | None = None,
    list_name: str | None = None,
    problems_only: bool = False,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    params: list[Any] = [IMAGE_ATTACHMENT_ENT]
    where = [
        "o.Z_ENT=?",
        "coalesce(o.ZMARKEDFORDELETION,0)=0",
        "coalesce(r.ZMARKEDFORDELETION,0)=0",
    ]
    if search:
        where.append("(r.ZTITLE like ? or coalesce(r.ZNOTES,'') like ?)")
        needle = f"%{search}%"
        params.extend([needle, needle])
    if list_name:
        where.append("l.ZNAME=?")
        params.append(list_name)
    if problems_only and attachment_sync_capabilities(con)["available"]:
        where.append("(o.ZCKSERVERRECORDDATA is null or coalesce(cs.ZINCLOUD,0)<>1)")
    limit_sql = ""
    if limit is not None:
        limit_sql = "limit ?"
        params.append(limit)
    sync_select = attachment_sync_select(con)
    rows = con.execute(
        f"""
        select r.Z_PK as reminder_pk,r.ZCKIDENTIFIER as reminder_id,r.ZTITLE as reminder_title,
               r.ZACCOUNT as reminder_account,l.ZNAME as list_name,
               o.Z_PK,o.Z_ENT,o.ZCKIDENTIFIER,o.ZCKCLOUDSTATE,o.ZREMINDER2,o.Z_FOK_REMINDER1,
               o.ZFILENAME,o.ZSHA512SUM,o.ZUTI,o.ZFILESIZE,o.ZWIDTH,o.ZHEIGHT,o.ZURL,o.ZHOSTURL,
               o.ZMARKEDFORDELETION,
               {sync_select}
        from ZREMCDOBJECT o
        join ZREMCDREMINDER r on r.Z_PK=o.ZREMINDER2
        left join ZREMCDBASELIST l on l.Z_PK=r.ZLIST
        left join ZREMCKCLOUDSTATE cs on cs.Z_PK=o.ZCKCLOUDSTATE
        where {" and ".join(where)}
        order by lower(l.ZNAME), lower(r.ZTITLE), o.Z_FOK_REMINDER1, o.Z_PK
        {limit_sql}
        """,
        params,
    ).fetchall()
    items: list[dict[str, Any]] = []
    problem_count = 0
    for row in rows:
        attachment = attachment_payload(row)
        mobile_visible = attachment["sync"]["mobile_visible_likely"]
        problem = mobile_visible is not True
        if problem:
            problem_count += 1
        if problems_only and not problem:
            continue
        items.append(
            {
                "reminder": {
                    "pk": row["reminder_pk"],
                    "id": row["reminder_id"],
                    "title": row["reminder_title"],
                    "list": row["list_name"],
                    "account": row["reminder_account"],
                },
                "attachment": attachment,
                "problem": (
                    "image_attachment_local_only"
                    if mobile_visible is False
                    else "image_attachment_sync_unverifiable"
                    if mobile_visible is None
                    else None
                ),
                "_row": dict(row),
            }
        )
    return items, problem_count


def image_attachment_audit_counts(
    con: sqlite3.Connection,
    *,
    search: str | None = None,
    list_name: str | None = None,
) -> dict[str, int]:
    params: list[Any] = [IMAGE_ATTACHMENT_ENT]
    where = [
        "o.Z_ENT=?",
        "coalesce(o.ZMARKEDFORDELETION,0)=0",
        "coalesce(r.ZMARKEDFORDELETION,0)=0",
    ]
    if search:
        where.append("(r.ZTITLE like ? or coalesce(r.ZNOTES,'') like ?)")
        needle = f"%{search}%"
        params.extend([needle, needle])
    if list_name:
        where.append("l.ZNAME=?")
        params.append(list_name)
    capabilities = attachment_sync_capabilities(con)
    problem_expression = (
        "(o.ZCKSERVERRECORDDATA is null or coalesce(cs.ZINCLOUD,0)<>1)"
        if capabilities["available"]
        else "1"
    )
    row = con.execute(
        f"""
        select count(*) as total,
               coalesce(sum(case when {problem_expression} then 1 else 0 end),0) as problems
        from ZREMCDOBJECT o
        join ZREMCDREMINDER r on r.Z_PK=o.ZREMINDER2
        left join ZREMCDBASELIST l on l.Z_PK=r.ZLIST
        left join ZREMCKCLOUDSTATE cs on cs.Z_PK=o.ZCKCLOUDSTATE
        where {" and ".join(where)}
        """,
        params,
    ).fetchone()
    return {"total": int(row["total"]), "problems": int(row["problems"])}


def source_paths_for_attachment(attachment: dict[str, Any]) -> list[Path]:
    sha512 = attachment.get("sha512")
    if not sha512:
        return []
    ext_candidates: list[str] = []
    filename = attachment.get("filename")
    if filename and Path(filename).suffix:
        ext_candidates.append(Path(filename).suffix.lower().lstrip("."))
    if attachment.get("uti") == "public.jpeg":
        ext_candidates.extend(["jpg", "jpeg"])
    if attachment.get("uti") == "public.png":
        ext_candidates.append("png")
    ext_candidates.extend(["png", "jpg", "jpeg", "heic"])
    seen_exts = list(dict.fromkeys(ext for ext in ext_candidates if ext))
    dirs = sorted(FILES.glob("Account-*/Attachments"))
    paths: list[Path] = []
    for directory in dirs:
        for ext in seen_exts:
            candidate = directory / f"{sha512}.{ext}"
            if candidate.exists():
                paths.append(candidate)
        paths.extend(sorted(directory.glob(f"{sha512}.*")))
    return list(dict.fromkeys(paths))


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
    try:
        proc = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdapterError("Image metadata inspection timed out", image=path.name) from exc
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


def reminderkit_attach_helper() -> Path:
    source = Path(__file__).resolve().with_name("remkit_attach_image.m")
    if not source.exists():
        raise AdapterError(f"ReminderKit helper source not found: {source.name}")
    helper = CACHE_DIR / "remkit_attach_image"
    ensure_private_dir(CACHE_DIR)
    lock_path = CACHE_DIR / "remkit_attach_image.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        needs_build = (
            not helper.exists()
            or not os.access(helper, os.X_OK)
            or source.stat().st_mtime > helper.stat().st_mtime
        )
        if not needs_build:
            return helper
        clang = shutil.which("clang")
        if not clang:
            raise AdapterError("clang is required to build the ReminderKit attachment helper")
        temp_handle = tempfile.NamedTemporaryFile(
            prefix=".remkit_attach_image.",
            dir=CACHE_DIR,
            delete=False,
        )
        temp_path = Path(temp_handle.name)
        temp_handle.close()
        try:
            try:
                proc = subprocess.run(
                    [
                        clang,
                        "-x",
                        "objective-c",
                        "-fobjc-arc",
                        "-framework",
                        "Foundation",
                        "-framework",
                        "AppKit",
                        "-o",
                        str(temp_path),
                        str(source),
                    ],
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=SUBPROCESS_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                raise AdapterError("ReminderKit helper build timed out") from exc
            if proc.returncode != 0:
                raise AdapterError(
                    (proc.stderr or proc.stdout).strip()
                    or "Failed to build ReminderKit attachment helper"
                )
            temp_path.chmod(0o700)
            os.replace(temp_path, helper)
        finally:
            temp_path.unlink(missing_ok=True)
    return helper


def attach_image_reminderkit_record(
    con: sqlite3.Connection,
    reminder: dict[str, Any],
    image: Path,
) -> dict[str, Any]:
    if not image.exists():
        raise AdapterError(f"Image not found: {image.name}")
    before_rows = active_attachment_rows(
        con,
        reminder["Z_PK"],
        attachment_ent=IMAGE_ATTACHMENT_ENT,
    )
    before_ids = {row["ZCKIDENTIFIER"] for row in before_rows}
    helper = reminderkit_attach_helper()
    try:
        proc = subprocess.run(
            [str(helper), reminder["ZCKIDENTIFIER"], str(image)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdapterError(
            "ReminderKit image attachment timed out",
            code="sync_pending",
            image=image.name,
            partial_failure=True,
            mutation_outcome_unknown=True,
        ) from exc
    raw = (proc.stdout or "").strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise AdapterError(
            "ReminderKit helper returned invalid JSON after a mutation attempt",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
            helper_output_present=bool(raw or proc.stderr.strip()),
        ) from exc
    if proc.returncode != 0 or not payload.get("ok"):
        detail = payload.get("detail") or proc.stderr.strip()
        message = payload.get("error") or "ReminderKit image attachment failed"
        if detail:
            message = f"{message}: {detail}"
        raise AdapterError(
            message,
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
        )

    db_row = con.execute("pragma database_list").fetchone()
    db_path = Path(db_row["file"] if isinstance(db_row, sqlite3.Row) else db_row[2])
    attachment_id = payload.get("attachment_id")
    wanted = normalize_uuid(str(attachment_id)) if attachment_id else None
    if wanted in before_ids:
        raise AdapterError(
            "ReminderKit helper returned an attachment id that existed before the operation",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
            attachment_id=wanted,
        )
    selected: dict[str, Any] | None = None
    deadline = time.time() + ATTACHMENT_VERIFY_TIMEOUT_SECONDS
    while True:
        fresh = connect(db_path)
        try:
            rows = active_attachment_rows(fresh, reminder["Z_PK"], attachment_ent=IMAGE_ATTACHMENT_ENT)
        finally:
            fresh.close()
        if wanted:
            selected = next((row for row in rows if row["ZCKIDENTIFIER"] == wanted), None)
        else:
            new_rows = [row for row in rows if row["ZCKIDENTIFIER"] not in before_ids]
            if len(new_rows) == 1:
                selected = new_rows[0]
            elif len(new_rows) > 1:
                raise AdapterError(
                    "ReminderKit helper created multiple image attachments; verification is ambiguous",
                    code="sync_pending",
                    partial_failure=True,
                    attachment_ids=[row["ZCKIDENTIFIER"] for row in new_rows],
                )
            else:
                raise AdapterError(
                    "ReminderKit helper reported success without an attachment id or a new database row",
                    code="sync_pending",
                    partial_failure=True,
                    mutation_outcome_unknown=True,
                )
        if selected is not None:
            sync = attachment_payload(selected).get("sync", {})
            if sync.get("mobile_visible_likely") is True:
                break
        if time.time() >= deadline:
            break
        time.sleep(0.25)
    if selected is None:
        raise AdapterError(
            "ReminderKit helper reported success but its image attachment id was not found",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
            attachment_id=attachment_id,
        )

    attachment = attachment_payload(selected)
    if attachment["sync"].get("mobile_visible_likely") is not True:
        raise AttachmentVerificationError(
            "Image attachment was created but mobile visibility could not be verified",
            row=selected,
            partial_failure=True,
            attachment=attachment,
            cleanup_command=(
                "delete_attachment "
                f"--id {reminder['ZCKIDENTIFIER']} "
                f"--attachment-id {attachment['id']}"
            ),
        )
    return {
        "attached": True,
        "backend": "reminderkit",
        "attachment": attachment,
        "helper": payload,
        "sync": attachment.get("sync", {}),
        "_row": selected,
    }


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
        "version": row["Z_OPT"],
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
    ensure_private_dir(path.parent)
    temp_handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    tmp = Path(temp_handle.name)
    try:
        with temp_handle as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, path)
        path.chmod(0o600)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def load_cache_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AdapterError(f"Cache not found: {path}. Run cache_rebuild first.")
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except json.JSONDecodeError as exc:
        raise AdapterError(
            f"Cache contains invalid JSON: {path}. Run cache_rebuild."
        ) from exc
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
                    item["attachment_sync_capabilities"] = attachment_sync_capabilities(con)
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


def create_store_backup(output: str | None = None) -> dict[str, Any]:
    if not GROUP.exists():
        raise AdapterError("Reminders group container does not exist")
    default_dir = APP_SUPPORT / "backups"
    ensure_private_dir(default_dir)
    out = (
        Path(output).expanduser()
        if output
        else default_dir / f"reminders-container-backup-{dt.datetime.now():%Y%m%d-%H%M%S}.tgz"
    )
    out = out.resolve()
    group = GROUP.resolve()
    if out == group or group in out.parents:
        raise AdapterError("Backup output must be outside the Reminders group container")
    ensure_private_dir(out.parent)
    temp_handle = tempfile.NamedTemporaryFile(
        prefix=f".{out.name}.",
        suffix=".tmp",
        dir=out.parent,
        delete=False,
    )
    temp_path = Path(temp_handle.name)
    temp_handle.close()
    try:
        with tarfile.open(temp_path, "w:gz") as tar:
            tar.add(group, arcname="Container_v1")
        temp_path.chmod(0o600)
        os.replace(temp_path, out)
        out.chmod(0o600)
    finally:
        temp_path.unlink(missing_ok=True)
    return {
        "backup": str(out),
        "bytes": out.stat().st_size,
        "consistency": "best_effort_live_container",
        "warning": "Reminders may write while this archive is created; verify the backup before relying on it for recovery.",
    }


def cmd_backup_store(args: argparse.Namespace) -> int:
    result = create_store_backup(args.output)
    log_action("backup_store", {"path": result["backup"]})
    json_out({"ok": True, **result})
    return 0


def cmd_list_lists(args: argparse.Namespace) -> int:
    db = resolve_database(args.db)
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
            limit ?
            """
            ,
            (args.limit + 1,),
        ).fetchall()
        json_out(
            {
                "ok": True,
                "db": str(db),
                "lists": [dict(row) for row in rows[: args.limit]],
                "limit": args.limit,
                "truncated": len(rows) > args.limit,
            }
        )
        return 0
    finally:
        con.close()


def cmd_list_sections(args: argparse.Namespace) -> int:
    db = resolve_database(args.db)
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
            limit ?
            """,
            [*params, args.limit + 1],
        ).fetchall()
        json_out(
            {
                "ok": True,
                "db": str(db),
                "sections": [dict(row) for row in rows[: args.limit]],
                "limit": args.limit,
                "truncated": len(rows) > args.limit,
            }
        )
        return 0
    finally:
        con.close()


def cmd_snapshot(args: argparse.Namespace) -> int:
    db = resolve_database(args.db)
    con = connect(db)
    try:
        list_rows = con.execute(
            """
            select Z_PK,ZCKIDENTIFIER,ZNAME,ZISGROUP,ZPARENTLIST
            from ZREMCDBASELIST
            where coalesce(ZMARKEDFORDELETION,0)=0 and ZNAME is not null
            order by lower(ZNAME)
            limit ?
            """,
            (args.limit + 1,),
        ).fetchall()
        section_rows = con.execute(
            """
            select s.Z_PK,s.ZCKIDENTIFIER,s.ZDISPLAYNAME,s.ZLIST,l.ZNAME as list_name,s.Z_FOK_LIST
            from ZREMCDBASESECTION s
            left join ZREMCDBASELIST l on l.Z_PK=s.ZLIST
            where coalesce(s.ZMARKEDFORDELETION,0)=0
            order by lower(l.ZNAME), s.Z_FOK_LIST
            limit ?
            """,
            (args.limit + 1,),
        ).fetchall()
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
            [*params, args.limit + 1],
        ).fetchall()
        reminders = [
            {k: v for k, v in reminder_payload(con, dict(row), include_attachments=False).items() if k != "notes"}
            for row in rows[: args.limit]
        ]
        lists = [dict(row) for row in list_rows[: args.limit]]
        sections = [dict(row) for row in section_rows[: args.limit]]
        truncation = {
            "lists": len(list_rows) > args.limit,
            "sections": len(section_rows) > args.limit,
            "reminders": len(rows) > args.limit,
        }
        json_out(
            {
                "ok": True,
                "db": str(db),
                "counts": db_counts(db),
                "lists": lists,
                "sections": sections,
                "reminders": reminders,
                "limit": args.limit,
                "truncated": any(truncation.values()),
                "truncation": truncation,
            }
        )
        return 0
    finally:
        con.close()


def cmd_search_reminders(args: argparse.Namespace) -> int:
    db = resolve_database(args.db)
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
            [*params, args.limit + 1],
        ).fetchall()
        json_out(
            {
                "ok": True,
                "db": str(db),
                "matches": [
                    {k: v for k, v in reminder_payload(con, dict(row), include_attachments=False).items() if k != "notes"}
                    for row in rows[: args.limit]
                ],
                "limit": args.limit,
                "truncated": len(rows) > args.limit,
            }
        )
        return 0
    finally:
        con.close()


def cmd_read_reminder(args: argparse.Namespace) -> int:
    require_exact_reminder_selector(
        reminder_id=args.id,
        title=args.title,
        list_name=args.list,
    )
    db = resolve_database(args.db)
    con = connect(db)
    try:
        row = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        json_out({"ok": True, "db": str(db), "reminder": reminder_payload(con, row)})
        return 0
    finally:
        con.close()


def cmd_show_reminder(args: argparse.Namespace) -> int:
    require_exact_reminder_selector(
        reminder_id=args.id,
        title=args.title,
        list_name=args.list,
    )
    operation_id = new_operation_id()
    db = resolve_database(args.db)
    con = connect(db)
    try:
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        target = reminder_mutation_snapshot(reminder)
        rem_id = reminder_url(reminder["ZCKIDENTIFIER"])
    finally:
        con.close()
    script = """
on run argv
  set reminderID to item 1 of argv
  tell application "Reminders"
    activate
    set targetReminder to reminder id reminderID
    show targetReminder
    return id of targetReminder
  end tell
end run
"""
    out = run_osascript(script, [rem_id])
    json_out(
        operation_receipt(
            status="verified",
            operation="show_reminder",
            operation_id=operation_id,
            backend="applescript",
            target={"id": rem_id, "list": target.get("list")},
            before=target,
            after={"native_returned_id": out, "ui_handoff_requested": True},
            verification={
                "state": "native_return",
                "ui_handoff_accepted": True,
                "visual_selection_observed": False,
            },
            recovery={"semantics": "not_applicable"},
        )
    )
    return 0


def cmd_list_tags(args: argparse.Namespace) -> int:
    db = resolve_database(args.db)
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
            [TAG_OBJECT_ENT, *params, args.limit + 1],
        ).fetchall()
        json_out(
            {
                "ok": True,
                "db": str(db),
                "tags": [
                    tag_label_payload(row, active_count=int(row["active_count"] or 0))
                    for row in rows[: args.limit]
                ],
                "limit": args.limit,
                "truncated": len(rows) > args.limit,
            }
        )
        return 0
    finally:
        con.close()


def cmd_add_tag(args: argparse.Namespace) -> int:
    require_exact_reminder_selector(
        reminder_id=args.id,
        title=args.title,
        list_name=args.list,
    )
    db = resolve_database(args.db, write=True)
    tag = normalized_tag_name(args.tag)
    operation_id = new_operation_id()
    con = connect(db)
    try:
        capability = require_command_capability(con, "tag_assignment_db")
        con.execute("begin immediate")
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        require_reminder_version(reminder, getattr(args, "if_version", None))
        before = reminder_mutation_snapshot(reminder)
        now = core_now()
        account_id = account_identifier(con, reminder.get("ZACCOUNT"))
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
                operation_receipt(
                    status="unchanged",
                    operation="add_tag",
                    operation_id=operation_id,
                    backend="sqlite_private",
                    target={
                        "id": reminder_url(reminder["ZCKIDENTIFIER"]),
                        "tag": tag,
                    },
                    before=before,
                    after={
                        "reminder": before,
                        "tag": tag_label_payload(label),
                        "assignment_id": existing["ZCKIDENTIFIER"],
                    },
                    verification={"state": "read_back", "tag_attached": True},
                    recovery={"semantics": "not_applicable"},
                    capability=capability,
                )
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
        verified = con.execute(
            """
            select count(*) from ZREMCDOBJECT
            where Z_PK=? and ZREMINDER3=? and ZHASHTAGLABEL=?
              and Z_ENT=? and coalesce(ZMARKEDFORDELETION,0)=0
            """,
            (object_pk, reminder["Z_PK"], label["Z_PK"], TAG_OBJECT_ENT),
        ).fetchone()[0] == 1
        if not verified:
            raise AdapterError("Tag assignment could not be read back", code="schema_mismatch")
        after = reminder_mutation_snapshot(reread_reminder(con, reminder["Z_PK"]))
        con.commit()
        warning = log_action(
            "add_tag",
            {
                "operation_id": operation_id,
                "reminder": reminder["ZCKIDENTIFIER"],
                "tag": tag,
                "object": object_id,
            },
        )
        json_out(
            operation_receipt(
                status="verified",
                operation="add_tag",
                operation_id=operation_id,
                backend="sqlite_private",
                target={"id": reminder_url(reminder["ZCKIDENTIFIER"]), "tag": tag},
                before=before,
                after={
                    "reminder": after,
                    "tag": tag_label_payload(label),
                    "assignment_id": object_id,
                    "label_created": label_created,
                },
                verification={"state": "read_back", "tag_attached": True},
                recovery={
                    "semantics": "remove_tag",
                    "command": f"remove_tag --id {reminder['ZCKIDENTIFIER']} --tag {json.dumps(tag)}",
                },
                warnings=[warning] if warning else None,
                capability=capability,
            )
        )
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def cmd_remove_tag(args: argparse.Namespace) -> int:
    require_exact_reminder_selector(
        reminder_id=args.id,
        title=args.title,
        list_name=args.list,
    )
    db = resolve_database(args.db, write=True)
    tag = normalized_tag_name(args.tag)
    operation_id = new_operation_id()
    con = connect(db)
    try:
        capability = require_command_capability(con, "tag_assignment_db")
        con.execute("begin immediate")
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        require_reminder_version(reminder, getattr(args, "if_version", None))
        before = reminder_mutation_snapshot(reminder)
        label = find_tag_label(con, tag, account_id=account_identifier(con, reminder.get("ZACCOUNT")))
        if not label:
            con.commit()
            json_out(
                operation_receipt(
                    status="unchanged",
                    operation="remove_tag",
                    operation_id=operation_id,
                    backend="sqlite_private",
                    target={"id": reminder_url(reminder["ZCKIDENTIFIER"]), "tag": tag},
                    before=before,
                    after=before,
                    verification={"state": "read_back", "tag_attached": False},
                    recovery={"semantics": "not_applicable"},
                    capability=capability,
                )
            )
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
            con.commit()
            json_out(
                operation_receipt(
                    status="unchanged",
                    operation="remove_tag",
                    operation_id=operation_id,
                    backend="sqlite_private",
                    target={"id": reminder_url(reminder["ZCKIDENTIFIER"]), "tag": tag},
                    before=before,
                    after={"reminder": before, "tag": tag_label_payload(label)},
                    verification={"state": "read_back", "tag_attached": False},
                    recovery={"semantics": "not_applicable"},
                    capability=capability,
                )
            )
            return 0
        now = core_now()
        removed = []
        for row in rows:
            con.execute(
                "update ZREMCDOBJECT set ZMARKEDFORDELETION=1,Z_OPT=coalesce(Z_OPT,0)+1 where Z_PK=?",
                (row["Z_PK"],),
            )
            bump_cloud_state(con, row["ZCKCLOUDSTATE"], now)
            removed.append({"object_pk": row["Z_PK"], "object_id": row["ZCKIDENTIFIER"]})
        touch_reminder(con, reminder, now)
        remaining = con.execute(
            """
            select count(*) from ZREMCDOBJECT
            where ZREMINDER3=? and ZHASHTAGLABEL=? and Z_ENT=?
              and coalesce(ZMARKEDFORDELETION,0)=0
            """,
            (reminder["Z_PK"], label["Z_PK"], TAG_OBJECT_ENT),
        ).fetchone()[0]
        if remaining:
            raise AdapterError("Tag removal could not be read back", code="schema_mismatch")
        after = reminder_mutation_snapshot(reread_reminder(con, reminder["Z_PK"]))
        con.commit()
        warning = log_action(
            "remove_tag",
            {
                "operation_id": operation_id,
                "reminder": reminder["ZCKIDENTIFIER"],
                "tag": tag,
                "removed": removed,
            },
        )
        json_out(
            operation_receipt(
                status="verified",
                operation="remove_tag",
                operation_id=operation_id,
                backend="sqlite_private",
                target={"id": reminder_url(reminder["ZCKIDENTIFIER"]), "tag": tag},
                before=before,
                after={
                    "reminder": after,
                    "tag": tag_label_payload(label),
                    "removed_assignment_ids": [item["object_id"] for item in removed],
                },
                verification={"state": "read_back", "tag_attached": False},
                recovery={
                    "semantics": "add_tag",
                    "command": f"add_tag --id {reminder['ZCKIDENTIFIER']} --tag {json.dumps(tag)}",
                },
                warnings=[warning] if warning else None,
                capability=capability,
            )
        )
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def escape_like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def cleanup_tag_candidates(
    con: sqlite3.Connection,
    *,
    tag: str | None,
    prefix: str | None,
    account_id: str | None,
    limit: int,
) -> tuple[list[sqlite3.Row], bool]:
    params: list[Any] = [TAG_OBJECT_ENT]
    filters = ["coalesce(reference_count,0)=0"]
    if tag:
        filters.append("lower(ZCANONICALNAME)=?")
        params.append(canonical_tag_name(tag))
    if prefix:
        filters.append("lower(ZNAME) like ? escape '\\'")
        literal_prefix = escape_like_literal(normalized_tag_name(prefix).casefold())
        params.append(f"{literal_prefix}%")
    if account_id:
        filters.append("ZACCOUNTIDENTIFIER=?")
        params.append(account_id)
    rows = con.execute(
        f"""
        select *
        from (
          select l.*,
                 coalesce(sum(case when o.Z_PK is not null and coalesce(o.ZMARKEDFORDELETION,0)=0 then 1 else 0 end),0) as active_count,
                 coalesce(count(o.Z_PK),0) as reference_count
          from ZREMCDHASHTAGLABEL l
          left join ZREMCDOBJECT o
            on o.ZHASHTAGLABEL=l.Z_PK
           and o.Z_ENT=?
          group by l.Z_PK
        )
        where {" and ".join(filters)}
        order by lower(ZNAME), coalesce(ZACCOUNTIDENTIFIER,''), Z_PK
        limit ?
        """,
        [*params, limit + 1],
    ).fetchall()
    return list(rows[:limit]), len(rows) > limit


def cleanup_candidate_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = tag_label_payload(row, active_count=int(row["active_count"] or 0))
    payload["reference_count"] = int(row["reference_count"] or 0)
    return payload


def cleanup_candidate_digest(candidates: list[dict[str, Any]]) -> str:
    stable = [
        {
            "pk": item.get("pk"),
            "uuid": item.get("uuid"),
            "canonical_name": item.get("canonical_name"),
            "account_identifier": item.get("account_identifier"),
            "active_count": item.get("active_count"),
            "reference_count": item.get("reference_count"),
        }
        for item in candidates
    ]
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def cmd_cleanup_tags(args: argparse.Namespace) -> int:
    db = resolve_database(args.db, write=args.apply)
    con = connect(db)
    try:
        capability = require_command_capability(con, "cleanup_tags")
        rows, truncated = cleanup_tag_candidates(
            con,
            tag=args.tag,
            prefix=args.prefix,
            account_id=args.account_id,
            limit=args.limit,
        )
        candidates = [cleanup_candidate_payload(row) for row in rows]
        digest = cleanup_candidate_digest(candidates)
        scope = {
            "tag": normalized_tag_name(args.tag) if args.tag else None,
            "prefix": normalized_tag_name(args.prefix) if args.prefix else None,
            "account_id": args.account_id,
            "limit": args.limit,
        }
        if not args.apply:
            json_out(
                {
                    "ok": True,
                    "status": "unchanged",
                    "operation": "cleanup_tags_preview",
                    "operation_id": new_operation_id(),
                    "backend": "sqlite_private_maintenance",
                    "db": str(db),
                    "scope": scope,
                    "candidates": candidates,
                    "candidate_digest": digest,
                    "truncated": truncated,
                    "capability": capability,
                }
            )
            return 0
        if not args.tag and not args.prefix:
            raise AdapterError(
                "cleanup_tags --apply requires --tag or --prefix",
                code="ambiguous_scope",
            )
        if not args.preview_digest:
            raise AdapterError(
                "cleanup_tags --apply requires the candidate_digest from a dry run",
                code="ambiguous_scope",
            )
        if truncated:
            raise AdapterError(
                "Cleanup candidates exceed the requested limit; narrow the scope",
                code="ambiguous_scope",
                candidate_count_at_least=args.limit + 1,
            )
        if args.preview_digest != digest:
            raise AdapterError(
                "Cleanup candidate set changed since preview",
                code="concurrent_modification",
                expected_digest=args.preview_digest,
                current_digest=digest,
            )

        if not candidates:
            json_out(
                operation_receipt(
                    status="unchanged",
                    operation="cleanup_tags",
                    backend="sqlite_private_maintenance",
                    target={"candidate_digest": digest, "count": 0, "scope": scope},
                    after={"deleted": [], "remaining": 0},
                    verification={"state": "read_back", "remaining_labels": 0},
                    recovery={"semantics": "not_applicable"},
                    db=str(db),
                    capability=capability,
                )
            )
            return 0

        backup = None
        if len(candidates) > 1 and not args.no_backup:
            backup = create_store_backup()

        con.execute("begin immediate")
        locked_rows, locked_truncated = cleanup_tag_candidates(
            con,
            tag=args.tag,
            prefix=args.prefix,
            account_id=args.account_id,
            limit=args.limit,
        )
        locked_candidates = [cleanup_candidate_payload(row) for row in locked_rows]
        locked_digest = cleanup_candidate_digest(locked_candidates)
        if locked_truncated or locked_digest != args.preview_digest:
            raise AdapterError(
                "Cleanup candidate set changed while acquiring the write lock",
                code="concurrent_modification",
                expected_digest=args.preview_digest,
                current_digest=locked_digest,
            )

        deleted: list[dict[str, Any]] = []
        for row, candidate in zip(locked_rows, locked_candidates, strict=True):
            result = con.execute(
                """
                delete from ZREMCDHASHTAGLABEL
                where Z_PK=?
                  and not exists (
                    select 1 from ZREMCDOBJECT
                    where Z_ENT=? and ZHASHTAGLABEL=?
                  )
                """,
                (row["Z_PK"], TAG_OBJECT_ENT, row["Z_PK"]),
            )
            if result.rowcount != 1:
                raise AdapterError(
                    "A tag label gained a reference during cleanup",
                    code="concurrent_modification",
                    label_id=candidate.get("uuid"),
                )
            deleted.append(candidate)

        remaining = con.execute(
            f"select count(*) from ZREMCDHASHTAGLABEL where Z_PK in ({','.join('?' for _ in deleted)})"
            if deleted
            else "select 0",
            [item["pk"] for item in deleted],
        ).fetchone()[0]
        if remaining:
            raise AdapterError(
                "Deleted tag labels could not be verified",
                code="schema_mismatch",
                remaining=remaining,
            )
        con.commit()
        operation_id = new_operation_id()
        warning = log_action(
            "cleanup_tags",
            {
                "operation_id": operation_id,
                "candidate_digest": locked_digest,
                "deleted": deleted,
                "scope": scope,
            },
        )
        receipt = operation_receipt(
            status="verified",
            operation="cleanup_tags",
            backend="sqlite_private_maintenance",
            target={"candidate_digest": locked_digest, "count": len(deleted), "scope": scope},
            after={"deleted": deleted, "remaining": 0},
            verification={
                "state": "read_back",
                "scope": "local_private_store",
                "remaining_labels": 0,
                "orphan_references": 0,
                "icloud_propagation": "not_verified",
            },
            recovery={
                "semantics": "no_native_undo",
                "labels_are_recreatable": True,
                "backup": backup,
            },
            warnings=[
                {
                    "code": "private_label_sync_unverified",
                    "message": (
                        "Unused tag labels were removed from the local private store; "
                        "iCloud propagation was not verified."
                    ),
                },
                *([warning] if warning else []),
            ],
            operation_id=operation_id,
            db=str(db),
            capability=capability,
        )
        json_out(receipt)
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def list_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    return {
        "id": item.get("ZCKIDENTIFIER"),
        "name": item.get("ZNAME"),
        "is_group": bool(item.get("ZISGROUP")),
        "color": item.get("ZCOLOR"),
        "emblem": item.get("ZBADGEEMBLEM"),
    }


def cmd_create_list(args: argparse.Namespace) -> int:
    operation_id = new_operation_id()
    db = resolve_database(args.db, write=True)
    con = connect(db)
    try:
        existing_rows = con.execute(
            """
            select Z_PK,ZCKIDENTIFIER,ZNAME,ZISGROUP,ZBADGEEMBLEM,ZCOLOR
            from ZREMCDBASELIST
            where ZNAME=? and coalesce(ZMARKEDFORDELETION,0)=0
            order by Z_PK
            limit 2
            """,
            (args.name,),
        ).fetchall()
        if existing_rows:
            existing = list_payload(existing_rows[0])
            json_out(
                operation_receipt(
                    status="unchanged",
                    operation="create_list",
                    operation_id=operation_id,
                    backend="native_preflight",
                    target={"id": existing["id"], "name": existing["name"]},
                    after={"list": existing, "created": False},
                    verification={"state": "read_back", "database_row": True},
                    recovery={"semantics": "not_applicable"},
                    warnings=(
                        ["More than one active list already has this name; use stable list IDs for later operations."]
                        if len(existing_rows) > 1
                        else None
                    ),
                )
            )
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
    out = None
    native_error: AdapterError | None = None
    try:
        out = run_osascript(
            script,
            [args.name, args.color or "", args.emblem or ""],
            mutation=True,
        )
    except AdapterError as exc:
        native_error = exc
    lines = out.splitlines() if out else []
    created = {
        "id": lines[0] if len(lines) > 0 else None,
        "name": lines[1] if len(lines) > 1 else args.name,
        "color": lines[2] if len(lines) > 2 else args.color,
        "emblem": lines[3] if len(lines) > 3 else args.emblem,
    }
    if native_error:
        con = connect(db)
        try:
            row = con.execute(
                """
                select Z_PK,ZCKIDENTIFIER,ZNAME,ZISGROUP,ZBADGEEMBLEM,ZCOLOR
                from ZREMCDBASELIST
                where ZNAME=? and coalesce(ZMARKEDFORDELETION,0)=0
                order by Z_PK desc
                limit 1
                """,
                (args.name,),
            ).fetchone()
            if row:
                created = list_payload(row)
        finally:
            con.close()
        if native_error.code == "permission_denied" and not created.get("id"):
            raise native_error
    warning = log_action(
        "create_list_applescript",
        {"operation_id": operation_id, "id": created["id"], "name": created["name"]},
    )
    json_out(
        operation_receipt(
            status="committed_verification_pending" if native_error else "verified",
            operation="create_list",
            operation_id=operation_id,
            backend="applescript",
            target={"id": created["id"], "name": created["name"]},
            after={"list": created, "created": True},
            verification={
                "state": "read_back_after_native_error" if native_error and created.get("id") else "pending" if native_error else "native_return",
                "native_object": bool(created.get("id")),
                "native_error": native_error.code if native_error else None,
            },
            recovery={"semantics": "delete_list_in_reminders"},
            warnings=[
                item
                for item in (
                    (
                        {
                            "code": native_error.code,
                            "message": str(native_error),
                            "mutation_outcome_unknown": native_error.details.get("mutation_outcome_unknown", False),
                        }
                        if native_error
                        else None
                    ),
                    warning,
                )
                if item
            ] or None,
        )
    )
    return 0


def create_reminder_once(args: argparse.Namespace) -> dict[str, Any]:
    operation_id = new_operation_id()
    if args.backend == "db":
        db = resolve_database(args.db, write=True)
        con = connect(db)
        try:
            capability = require_command_capability(con, "create_reminder_db")
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
            created_row = con.execute(
                """
                select r.*, l.ZNAME as LIST_NAME
                from ZREMCDREMINDER r
                left join ZREMCDBASELIST l on l.Z_PK=r.ZLIST
                where r.Z_PK=? and coalesce(r.ZMARKEDFORDELETION,0)=0
                """,
                (reminder_pk,),
            ).fetchone()
            if not created_row or created_row["ZCKIDENTIFIER"] != reminder_id:
                raise AdapterError(
                    "Created reminder could not be read back before commit",
                    code="schema_mismatch",
                )
            con.commit()
            rem_url = f"x-apple-reminder://{reminder_id}"
            sync_warning = None
            text_synced = False
            try:
                sync_reminder_text_applescript(rem_url, title=args.title, notes=args.notes)
                text_synced = True
            except AdapterError as exc:
                sync_warning = {
                    "code": "native_text_sync_failed",
                    "message": str(exc),
                    "repair": f"update_reminder --backend applescript --id {reminder_id}",
                }
            journal_warning = log_action(
                "create_reminder_db",
                {
                    "operation_id": operation_id,
                    "id": rem_url,
                    "list": args.list,
                    "title": args.title,
                    "db": str(db),
                    "text_synced_via_applescript": text_synced,
                },
            )
            warnings = [item for item in (sync_warning, journal_warning) if item]
            return operation_receipt(
                status="verified" if text_synced else "partial_success",
                operation="create_reminder",
                operation_id=operation_id,
                backend="db",
                target={
                    "id": rem_url,
                    "pk": reminder_pk,
                    "list": args.list,
                    "list_id": list_row.get("ZCKIDENTIFIER"),
                },
                after=reminder_mutation_snapshot(dict(created_row)),
                verification={
                    "state": "read_back",
                    "database_row": True,
                    "native_text_sync": text_synced,
                },
                recovery={
                    "semantics": "delete_created_reminder",
                    "command": f"delete_reminder --id {reminder_id}",
                },
                warnings=warnings or None,
                scheduled=bool(sched),
                text_synced_via_applescript=text_synced,
                capability=capability,
                db=str(db),
            )
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    if args.due_at or args.remind_at or args.all_day_due_date or args.flagged is not None or args.priority is not None:
        raise AdapterError("date, flag, and priority options currently require --backend db")
    db = None
    before_ids: set[str] = set()
    try:
        db = resolve_database(args.db)
        con = connect(db)
        try:
            before_ids = {
                row["ZCKIDENTIFIER"]
                for row in con.execute(
                    """
                    select r.ZCKIDENTIFIER
                    from ZREMCDREMINDER r
                    join ZREMCDBASELIST l on l.Z_PK=r.ZLIST
                    where r.ZTITLE=? and l.ZNAME=?
                      and coalesce(r.ZMARKEDFORDELETION,0)=0
                    """,
                    (args.title, args.list),
                ).fetchall()
            }
        finally:
            con.close()
    except (AdapterError, sqlite3.Error):
        db = None
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
    rem_id = None
    native_error: AdapterError | None = None
    try:
        rem_id = run_osascript(
            script,
            [args.list, args.title, args.notes or ""],
            mutation=True,
        )
    except AdapterError as exc:
        native_error = exc
    normalized_id = reminder_url(rem_id) if rem_id else None
    read_back = None
    verification_state = "pending"
    candidate_ids: list[str] = []
    try:
        db = db or resolve_database(args.db)
        con = connect(db)
        try:
            if rem_id:
                row = find_reminder(con, reminder_id=rem_id)
                read_back = reminder_mutation_snapshot(row)
                verification_state = "read_back"
            else:
                rows = con.execute(
                    """
                    select r.*, l.ZNAME as LIST_NAME
                    from ZREMCDREMINDER r
                    join ZREMCDBASELIST l on l.Z_PK=r.ZLIST
                    where r.ZTITLE=? and l.ZNAME=?
                      and coalesce(r.ZMARKEDFORDELETION,0)=0
                    order by r.Z_PK desc
                    """,
                    (args.title, args.list),
                ).fetchall()
                new_rows = [row for row in rows if row["ZCKIDENTIFIER"] not in before_ids]
                candidate_ids = [row["ZCKIDENTIFIER"] for row in new_rows[:10]]
                if len(new_rows) == 1:
                    rem_id = new_rows[0]["ZCKIDENTIFIER"]
                    normalized_id = reminder_url(rem_id)
                    read_back = reminder_mutation_snapshot(dict(new_rows[0]))
                    verification_state = "read_back_after_native_error"
        finally:
            con.close()
    except (AdapterError, sqlite3.Error):
        pass
    if read_back is None:
        read_back = {
            "id": normalize_uuid(rem_id) if rem_id else None,
            "store_read_back_pending": True,
            "candidate_ids": candidate_ids,
        }
    if native_error and native_error.code == "permission_denied" and not rem_id:
        raise native_error
    journal_warning = log_action(
        "create_reminder_applescript",
        {
            "operation_id": operation_id,
            "id": normalized_id,
            "list": args.list,
            "title": args.title,
        },
    )
    return operation_receipt(
        status=(
            "verified"
            if verification_state == "read_back" and not native_error
            else "committed_verification_pending"
        ),
        operation="create_reminder",
        operation_id=operation_id,
        backend="applescript",
        target={"id": normalized_id, "list": args.list},
        after=read_back,
        verification={
            "state": verification_state,
            "database_row": verification_state.startswith("read_back"),
            "native_error": native_error.code if native_error else None,
        },
        recovery={
            "semantics": "delete_created_reminder",
            "command": f"delete_reminder --id {normalize_uuid(rem_id)}" if rem_id else None,
        },
        warnings=[
            item
            for item in (
                (
                    {
                        "code": native_error.code,
                        "message": str(native_error),
                        "mutation_outcome_unknown": native_error.details.get("mutation_outcome_unknown", False),
                    }
                    if native_error
                    else None
                ),
                journal_warning,
            )
            if item
        ] or None,
    )


def cmd_create_reminder(args: argparse.Namespace) -> int:
    input_payload = {
        "backend": args.backend,
        "list": args.list,
        "title": args.title,
        "notes": args.notes,
        "due_at": args.due_at,
        "remind_at": args.remind_at,
        "all_day_due_date": args.all_day_due_date,
        "flagged": args.flagged,
        "priority": args.priority,
    }
    result = execute_idempotent(
        operation="create_reminder",
        key=args.idempotency_key,
        input_payload=input_payload,
        callback=lambda: create_reminder_once(args),
    )
    json_out(result)
    return 0


def cmd_update_reminder(args: argparse.Namespace) -> int:
    require_exact_reminder_selector(
        reminder_id=args.id,
        title=args.title,
        list_name=args.list,
    )
    if args.new_title is not None and not args.new_title.strip():
        raise AdapterError("new title must not be blank")
    if not any(
        value is not None
        for value in (
            args.new_title,
            args.notes,
            args.flagged,
            args.priority,
            args.due_at,
            args.remind_at,
            args.all_day_due_date,
        )
    ) and not args.clear_due:
        raise AdapterError("No update fields provided")
    operation_id = new_operation_id()
    expected_version = getattr(args, "if_version", None)
    if args.backend == "db":
        db = resolve_database(args.db, write=True)
        con = connect(db)
        try:
            capability = require_command_capability(con, "update_reminder_db")
            con.execute("begin immediate")
            reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
            require_reminder_version(reminder, expected_version)
            before = reminder_mutation_snapshot(reminder)
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
            now = core_now()
            updates.extend(["ZLASTMODIFIEDDATE=?", "Z_OPT=coalesce(Z_OPT,0)+1"])
            params.extend([now, reminder["Z_PK"], reminder["Z_OPT"]])
            result = con.execute(
                f"update ZREMCDREMINDER set {', '.join(updates)} where Z_PK=? and Z_OPT=?",
                params,
            )
            if result.rowcount != 1:
                raise AdapterError(
                    "Reminder changed while the update was being applied",
                    code="concurrent_modification",
                )
            bump_cloud_state(con, reminder.get("ZCKCLOUDSTATE"), now)
            refreshed = reread_reminder(con, reminder["Z_PK"])
            after = reminder_mutation_snapshot(refreshed)
            con.commit()
            rem_url = reminder_url(reminder["ZCKIDENTIFIER"])
            text_sync_needed = args.new_title is not None or args.notes is not None
            text_sync_error = None
            if text_sync_needed:
                try:
                    sync_reminder_text_applescript(rem_url, title=args.new_title, notes=args.notes)
                except Exception as exc:
                    text_sync_error = f"{type(exc).__name__}: {exc}"
            warnings: list[dict[str, Any] | str] = []
            if text_sync_error:
                warnings.append(
                    {
                        "code": "native_text_sync_failed",
                        "message": "The database update committed, but native title/notes synchronization failed.",
                        "detail": text_sync_error,
                    }
                )
            journal_warning = log_action(
                "update_reminder_db",
                {
                    "operation_id": operation_id,
                    "id": rem_url,
                    "db": str(db),
                    "fields": [item.split('=')[0] for item in updates],
                    "text_synced_via_applescript": text_sync_needed and not text_sync_error,
                },
            )
            if journal_warning:
                warnings.append(journal_warning)
            json_out(
                operation_receipt(
                    status="partial_success" if text_sync_error else "verified",
                    operation="update_reminder",
                    operation_id=operation_id,
                    backend="db",
                    target={"id": rem_url, "list": before.get("list")},
                    before=before,
                    after=after,
                    verification={
                        "state": "read_back",
                        "database_row": True,
                        "native_text_sync": not text_sync_error if text_sync_needed else "not_required",
                    },
                    recovery={
                        "semantics": "reapply_previous_values",
                        "automatic_restore_available": False,
                    },
                    warnings=warnings or None,
                    capability=capability,
                )
            )
            return 0
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    if args.due_at or args.remind_at or args.all_day_due_date or args.clear_due:
        raise AdapterError(
            "date options currently require --backend db",
            code="unsupported_capability",
        )
    db = resolve_database(args.db)
    con = connect(db)
    try:
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        require_reminder_version(reminder, expected_version)
        before = reminder_mutation_snapshot(reminder)
        rem_id = reminder_url(reminder["ZCKIDENTIFIER"])
        reminder_pk = reminder["Z_PK"]
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
    out = None
    native_error: AdapterError | None = None
    try:
        out = run_osascript(
            script,
            [
                rem_id,
                args.new_title or "",
                args.notes if args.notes is not None else "__NO_CHANGE__",
                "true" if args.flagged is True else "false" if args.flagged is False else "__NO_CHANGE__",
                str(args.priority) if args.priority is not None else "__NO_CHANGE__",
            ],
            mutation=True,
        )
    except AdapterError as exc:
        native_error = exc
    verification_state = "pending"
    after: dict[str, Any] = {"id": normalize_uuid(rem_id), "store_read_back_pending": True}
    try:
        con = connect(db)
        try:
            refreshed = reread_reminder(con, reminder_pk)
            after = reminder_mutation_snapshot(refreshed)
            matches = (
                (args.new_title is None or refreshed.get("ZTITLE") == args.new_title)
                and (args.notes is None or refreshed.get("ZNOTES") == args.notes)
                and (args.flagged is None or bool(refreshed.get("ZFLAGGED")) is args.flagged)
                and (args.priority is None or refreshed.get("ZPRIORITY") == args.priority)
            )
            verification_state = "read_back" if matches else "pending"
        finally:
            con.close()
    except (AdapterError, sqlite3.Error):
        pass
    if native_error and native_error.code == "permission_denied" and verification_state != "read_back":
        raise native_error
    native_warning = (
        {
            "code": native_error.code,
            "message": str(native_error),
            "mutation_outcome_unknown": native_error.details.get("mutation_outcome_unknown", False),
        }
        if native_error
        else None
    )
    warning = log_action(
        "update_reminder_applescript",
        {
            "operation_id": operation_id,
            "id": out or rem_id,
            "new_title": args.new_title,
            "notes_changed": args.notes is not None,
        },
    )
    json_out(
        operation_receipt(
            status="verified" if verification_state == "read_back" else "committed_verification_pending",
            operation="update_reminder",
            operation_id=operation_id,
            backend="applescript",
            target={"id": rem_id, "list": before.get("list")},
            before=before,
            after=after,
            verification={
                "state": verification_state,
                "native_returned_id": out,
                "native_error": native_error.code if native_error else None,
            },
            recovery={
                "semantics": "reapply_previous_values",
                "automatic_restore_available": False,
            },
            warnings=[item for item in (native_warning, warning) if item] or None,
        )
    )
    return 0


def set_completion(args: argparse.Namespace, *, completed: bool) -> int:
    require_exact_reminder_selector(
        reminder_id=args.id,
        title=args.title,
        list_name=args.list,
    )
    operation = "complete_reminder" if completed else "reopen_reminder"
    operation_id = new_operation_id()
    expected_version = getattr(args, "if_version", None)
    if args.backend == "db":
        db = resolve_database(args.db, write=True)
        con = connect(db)
        try:
            capability = require_command_capability(con, "set_completion_db")
            con.execute("begin immediate")
            reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
            require_reminder_version(reminder, expected_version)
            before = reminder_mutation_snapshot(reminder)
            if bool(reminder.get("ZCOMPLETED")) is completed:
                con.commit()
                json_out(
                    operation_receipt(
                        status="unchanged",
                        operation=operation,
                        operation_id=operation_id,
                        backend="db",
                        target={"id": reminder_url(reminder["ZCKIDENTIFIER"]), "list": before.get("list")},
                        before=before,
                        after=before,
                        verification={"state": "read_back", "completed": completed},
                        recovery={"semantics": "not_applicable"},
                        capability=capability,
                    )
                )
                return 0
            now = core_now()
            result = con.execute(
                """
                update ZREMCDREMINDER
                set ZCOMPLETED=?,
                    ZCOMPLETIONDATE=?,
                    ZLASTMODIFIEDDATE=?,
                    Z_OPT=coalesce(Z_OPT,0)+1
                where Z_PK=? and Z_OPT=?
                """,
                (1 if completed else 0, now if completed else None, now, reminder["Z_PK"], reminder["Z_OPT"]),
            )
            if result.rowcount != 1:
                raise AdapterError(
                    "Reminder changed while completion was being applied",
                    code="concurrent_modification",
                )
            bump_cloud_state(con, reminder.get("ZCKCLOUDSTATE"), now)
            refreshed = reread_reminder(con, reminder["Z_PK"])
            if bool(refreshed.get("ZCOMPLETED")) is not completed:
                raise AdapterError("Completion state could not be read back", code="schema_mismatch")
            after = reminder_mutation_snapshot(refreshed)
            con.commit()
            rem_url = reminder_url(reminder["ZCKIDENTIFIER"])
            warning = log_action(
                f"{operation}_db",
                {"operation_id": operation_id, "id": rem_url, "db": str(db)},
            )
            json_out(
                operation_receipt(
                    status="verified",
                    operation=operation,
                    operation_id=operation_id,
                    backend="db",
                    target={"id": rem_url, "list": before.get("list")},
                    before=before,
                    after=after,
                    verification={"state": "read_back", "completed": completed},
                    recovery={
                        "semantics": "reopen_reminder" if completed else "complete_reminder",
                        "command": f"{'reopen_reminder' if completed else 'complete_reminder'} --id {normalize_uuid(rem_url)}",
                    },
                    warnings=[warning] if warning else None,
                    capability=capability,
                )
            )
            return 0
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    db = resolve_database(args.db)
    con = connect(db)
    try:
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        require_reminder_version(reminder, expected_version)
        before = reminder_mutation_snapshot(reminder)
        rem_id = reminder_url(reminder["ZCKIDENTIFIER"])
        reminder_pk = reminder["Z_PK"]
        if bool(reminder.get("ZCOMPLETED")) is completed:
            json_out(
                operation_receipt(
                    status="unchanged",
                    operation=operation,
                    operation_id=operation_id,
                    backend="applescript",
                    target={"id": rem_id, "list": before.get("list")},
                    before=before,
                    after=before,
                    verification={"state": "read_back", "completed": completed},
                    recovery={"semantics": "not_applicable"},
                )
            )
            return 0
    finally:
        con.close()
    script = """
on run argv
  set reminderID to item 1 of argv
  set completedValue to item 2 of argv
  tell application "Reminders"
    set targetReminder to reminder id reminderID
    set completed of targetReminder to (completedValue is "true")
    return id of targetReminder
  end tell
end run
"""
    out = None
    native_error: AdapterError | None = None
    try:
        out = run_osascript(
            script,
            [rem_id, "true" if completed else "false"],
            mutation=True,
        )
    except AdapterError as exc:
        native_error = exc
    verification_state = "pending"
    after: dict[str, Any] = {"id": normalize_uuid(rem_id), "store_read_back_pending": True}
    try:
        con = connect(db)
        try:
            refreshed = reread_reminder(con, reminder_pk)
            after = reminder_mutation_snapshot(refreshed)
            if bool(refreshed.get("ZCOMPLETED")) is completed:
                verification_state = "read_back"
        finally:
            con.close()
    except (AdapterError, sqlite3.Error):
        pass
    if native_error and native_error.code == "permission_denied" and verification_state != "read_back":
        raise native_error
    native_warning = (
        {
            "code": native_error.code,
            "message": str(native_error),
            "mutation_outcome_unknown": native_error.details.get("mutation_outcome_unknown", False),
        }
        if native_error
        else None
    )
    warning = log_action(
        f"{operation}_applescript",
        {"operation_id": operation_id, "id": out or rem_id},
    )
    json_out(
        operation_receipt(
            status="verified" if verification_state == "read_back" else "committed_verification_pending",
            operation=operation,
            operation_id=operation_id,
            backend="applescript",
            target={"id": rem_id, "list": before.get("list")},
            before=before,
            after=after,
            verification={
                "state": verification_state,
                "completed": completed,
                "native_returned_id": out,
                "native_error": native_error.code if native_error else None,
            },
            recovery={
                "semantics": "reopen_reminder" if completed else "complete_reminder",
                "command": f"{'reopen_reminder' if completed else 'complete_reminder'} --id {normalize_uuid(rem_id)}",
            },
            warnings=[item for item in (native_warning, warning) if item] or None,
        )
    )
    return 0


def cmd_complete_reminder(args: argparse.Namespace) -> int:
    return set_completion(args, completed=True)


def cmd_reopen_reminder(args: argparse.Namespace) -> int:
    return set_completion(args, completed=False)


def cmd_delete_reminder(args: argparse.Namespace) -> int:
    require_exact_reminder_selector(
        reminder_id=args.id,
        title=args.title,
        list_name=args.list,
    )
    db = resolve_database(args.db, write=args.backend == "db")
    con = connect(db)
    operation_id = new_operation_id()
    requested_backend = args.backend
    selected_backend = requested_backend
    auto_evidence: dict[str, Any] | None = None
    try:
        if requested_backend == "auto":
            verified, auto_evidence = db_soft_delete_verified(con)
            selected_backend = "db" if verified else "applescript"

        if selected_backend == "db":
            resolved_write_db = resolve_database(args.db, write=True)
            if resolved_write_db != db:
                raise AdapterError(
                    "Resolved write database changed during backend selection",
                    code="concurrent_modification",
                )
            capability = require_command_capability(con, "delete_reminder_db")
            con.execute("begin immediate")
            reminder = find_reminder(
                con,
                reminder_id=args.id,
                title=args.title,
                list_name=args.list,
            )
            require_reminder_version(reminder, args.if_version)
            before = reminder_mutation_snapshot(reminder)
            list_row = None
            if reminder.get("ZLIST"):
                list_row = row_dict(
                    con.execute(
                        "select * from ZREMCDBASELIST where Z_PK=?",
                        (reminder["ZLIST"],),
                    ).fetchone()
                )
            now = core_now()
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
            result = con.execute(
                """
                update ZREMCDREMINDER
                set ZMARKEDFORDELETION=1,
                    ZLIST=null,
                    Z_FOK_LIST=null,
                    ZLASTMODIFIEDDATE=?,
                    Z_OPT=coalesce(Z_OPT,0)+1
                where Z_PK=? and Z_OPT=?
                """,
                (now, reminder["Z_PK"], reminder["Z_OPT"]),
            )
            if result.rowcount != 1:
                raise AdapterError(
                    "Reminder changed while deletion was being applied",
                    code="concurrent_modification",
                )
            bump_cloud_state(con, reminder.get("ZCKCLOUDSTATE"), now)
            deleted_row = con.execute(
                """
                select r.*, l.ZNAME as LIST_NAME
                from ZREMCDREMINDER r
                left join ZREMCDBASELIST l on l.Z_PK=r.ZLIST
                where r.Z_PK=?
                """,
                (reminder["Z_PK"],),
            ).fetchone()
            if not deleted_row or not deleted_row["ZMARKEDFORDELETION"] or deleted_row["ZLIST"] is not None:
                raise AdapterError(
                    "DB soft-delete state could not be read back",
                    code="schema_mismatch",
                )
            if list_row:
                refreshed_list = con.execute(
                    "select ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA from ZREMCDBASELIST where Z_PK=?",
                    (list_row["Z_PK"],),
                ).fetchone()
                refreshed_mapping = membership_map(
                    refreshed_list["ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA"] if refreshed_list else None
                )
                if reminder["ZCKIDENTIFIER"].upper() in refreshed_mapping:
                    raise AdapterError(
                        "Reminder remained in section membership after soft-delete",
                        code="schema_mismatch",
                    )
            con.commit()
            after = reminder_mutation_snapshot(dict(deleted_row))
            parity_verified, parity_evidence = db_soft_delete_verified(con)
            warning = log_action(
                "delete_reminder_db_soft",
                {
                    "operation_id": operation_id,
                    "id": reminder["ZCKIDENTIFIER"],
                    "db": str(db),
                    "backend_requested": requested_backend,
                },
            )
            receipt = operation_receipt(
                    status="verified" if parity_verified else "committed_verification_pending",
                    operation="delete_reminder",
                    operation_id=operation_id,
                    backend="db",
                    target={
                        "id": reminder["ZCKIDENTIFIER"],
                        "list": before.get("list"),
                    },
                    before=before,
                    after=after,
                    verification={
                        "state": "read_back",
                        "db_soft_delete_state": True,
                        "recently_deleted_parity": parity_verified,
                        "evidence": parity_evidence,
                    },
                    recovery={
                        "semantics": (
                            "recently_deleted_verified"
                            if parity_verified
                            else "soft_deleted_unverified"
                        ),
                        "automatic_restore_available": False,
                    },
                    warnings=[warning] if warning else None,
                    backend_requested=requested_backend,
                    capability=capability,
                )
            con.close()
            json_out(receipt)
            return 0

        reminder = find_reminder(
            con,
            reminder_id=args.id,
            title=args.title,
            list_name=args.list,
        )
        require_reminder_version(reminder, args.if_version)
        before = reminder_mutation_snapshot(reminder)
        rem_id = reminder_url(reminder["ZCKIDENTIFIER"])
    except Exception:
        con.rollback()
        con.close()
        raise

    script = """
on run argv
  set reminderID to item 1 of argv
  tell application "Reminders"
    delete reminder id reminderID
  end tell
  return reminderID
end run
    """
    try:
        out = None
        native_error: AdapterError | None = None
        try:
            out = run_osascript(script, [rem_id], mutation=True)
        except AdapterError as exc:
            native_error = exc
        row = con.execute(
            "select ZMARKEDFORDELETION,ZLIST,Z_OPT,ZLASTMODIFIEDDATE from ZREMCDREMINDER where ZCKIDENTIFIER=?",
            (normalize_uuid(rem_id),),
        ).fetchone()
        read_back_deleted = row is None or bool(row["ZMARKEDFORDELETION"])
        if native_error and native_error.code == "permission_denied" and not read_back_deleted:
            raise native_error
        after = (
            {
                "id": normalize_uuid(rem_id),
                "marked_for_deletion": bool(row["ZMARKEDFORDELETION"]),
                "list_pk": row["ZLIST"],
                "version": row["Z_OPT"],
                "last_modified_at": core_to_iso(row["ZLASTMODIFIEDDATE"]),
            }
            if row
            else {"id": normalize_uuid(rem_id), "not_found_in_store": True}
        )
        warning = log_action(
            "delete_reminder_applescript_native",
            {
                "operation_id": operation_id,
                "id": out or rem_id,
                "backend_requested": requested_backend,
            },
        )
        native_warning = (
            {
                "code": native_error.code,
                "message": str(native_error),
                "mutation_outcome_unknown": native_error.details.get("mutation_outcome_unknown", False),
            }
            if native_error
            else None
        )
        json_out(
            operation_receipt(
                status="verified" if read_back_deleted else "committed_verification_pending",
                operation="delete_reminder",
                operation_id=operation_id,
                backend="applescript",
                target={"id": rem_id, "list": before.get("list")},
                before=before,
                after=after,
                verification={
                    "state": "read_back" if read_back_deleted else "pending",
                    "store_no_longer_active": read_back_deleted,
                    "recently_deleted_ui": "not_checked",
                    "native_returned_id": out,
                    "native_error": native_error.code if native_error else None,
                },
                recovery={
                    "semantics": "native_recently_deleted_expected",
                    "automatic_restore_available": False,
                },
                warnings=[item for item in (native_warning, warning) if item] or None,
                backend_requested=requested_backend,
                auto_evidence=auto_evidence,
            )
        )
        return 0
    finally:
        con.close()


def section_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    return {
        "id": item.get("ZCKIDENTIFIER"),
        "name": item.get("ZDISPLAYNAME"),
        "list_id": item.get("LIST_ID") or item.get("list_id"),
        "list": item.get("LIST_NAME") or item.get("list_name"),
        "order": item.get("Z_FOK_LIST"),
    }


def cmd_create_section(args: argparse.Namespace) -> int:
    if not args.list_id and not args.list:
        raise AdapterError(
            "Use an exact list id or list name",
            code="ambiguous_target",
            required_selector="list_id | list",
        )
    operation_id = new_operation_id()
    db = resolve_database(args.db, write=True)
    con = connect(db)
    try:
        capability = require_command_capability(con, "create_section_db")
        con.execute("begin immediate")
        list_row = find_list(con, name=args.list, list_id=args.list_id)
        existing = con.execute(
            """
            select s.*, l.ZCKIDENTIFIER as LIST_ID, l.ZNAME as LIST_NAME
            from ZREMCDBASESECTION s
            join ZREMCDBASELIST l on l.Z_PK=s.ZLIST
            where s.ZLIST=? and s.ZDISPLAYNAME=? and coalesce(s.ZMARKEDFORDELETION,0)=0
            """,
            (list_row["Z_PK"], args.name),
        ).fetchone()
        if existing:
            con.commit()
            existing_payload = section_payload(existing)
            json_out(
                operation_receipt(
                    status="unchanged",
                    operation="create_section",
                    operation_id=operation_id,
                    backend="sqlite_private",
                    target={"id": existing_payload["id"], "list_id": list_row["ZCKIDENTIFIER"]},
                    after={"section": existing_payload, "created": False},
                    verification={"state": "read_back", "database_row": True},
                    recovery={"semantics": "not_applicable"},
                    capability=capability,
                )
            )
            return 0
        now = core_now()
        section_id = str(uuid.uuid4()).upper()
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
        created_row = con.execute(
            """
            select s.*, l.ZCKIDENTIFIER as LIST_ID, l.ZNAME as LIST_NAME
            from ZREMCDBASESECTION s
            join ZREMCDBASELIST l on l.Z_PK=s.ZLIST
            where s.Z_PK=? and coalesce(s.ZMARKEDFORDELETION,0)=0
            """,
            (section_pk,),
        ).fetchone()
        if not created_row:
            raise AdapterError("Section could not be read back", code="schema_mismatch")
        con.commit()
        created = section_payload(created_row)
        warning = log_action(
            "create_section",
            {
                "operation_id": operation_id,
                "db": str(db),
                "list": list_row["ZNAME"],
                "section": args.name,
                "id": section_id,
            },
        )
        json_out(
            operation_receipt(
                status="verified",
                operation="create_section",
                operation_id=operation_id,
                backend="sqlite_private",
                target={"id": section_id, "list_id": list_row["ZCKIDENTIFIER"]},
                after={"section": created, "created": True},
                verification={"state": "read_back", "database_row": True},
                recovery={
                    "semantics": "remove_section_in_reminders",
                    "automatic_restore_available": False,
                },
                warnings=[warning] if warning else None,
                capability=capability,
            )
        )
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def cmd_move_to_section(args: argparse.Namespace) -> int:
    require_exact_reminder_selector(
        reminder_id=args.id,
        title=args.title,
        list_name=args.list,
    )
    db = resolve_database(args.db, write=True)
    if not args.section_id and not args.section:
        raise AdapterError(
            "Use an exact section id or section name",
            code="ambiguous_target",
            required_selector="section_id | section",
        )
    operation_id = new_operation_id()
    con = connect(db)
    try:
        capability = require_command_capability(con, "move_to_section_db")
        con.execute("begin immediate")
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        require_reminder_version(reminder, getattr(args, "if_version", None))
        before_reminder = reminder_mutation_snapshot(reminder)
        if not reminder.get("ZLIST"):
            raise AdapterError("Reminder is not assigned to an active list", code="ambiguous_target")
        list_row_raw = con.execute(
            "select * from ZREMCDBASELIST where Z_PK=?",
            (reminder["ZLIST"],),
        ).fetchone()
        if not list_row_raw:
            raise AdapterError("Reminder list was not found", code="ambiguous_target")
        list_row = dict(list_row_raw)
        section = find_section(con, list_pk=list_row["Z_PK"], name=args.section, section_id=args.section_id)
        mapping = membership_map(list_row.get("ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA"))
        reminder_key = reminder["ZCKIDENTIFIER"].upper()
        previous_section_id = mapping.get(reminder_key)
        if previous_section_id == section["ZCKIDENTIFIER"].upper():
            con.commit()
            json_out(
                operation_receipt(
                    status="unchanged",
                    operation="move_to_section",
                    operation_id=operation_id,
                    backend="sqlite_private",
                    target={
                        "id": reminder_url(reminder["ZCKIDENTIFIER"]),
                        "section_id": section["ZCKIDENTIFIER"],
                    },
                    before={"reminder": before_reminder, "section_id": previous_section_id},
                    after={"reminder": before_reminder, "section_id": previous_section_id},
                    verification={"state": "read_back", "section_membership": True},
                    recovery={"semantics": "not_applicable"},
                    capability=capability,
                )
            )
            return 0
        mapping[reminder["ZCKIDENTIFIER"].upper()] = section["ZCKIDENTIFIER"].upper()
        now = core_now()
        result = con.execute(
            """
            update ZREMCDBASELIST
            set ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA=?, Z_OPT=coalesce(Z_OPT,0)+1
            where Z_PK=? and Z_OPT=?
            """,
            (membership_payload(mapping), list_row["Z_PK"], list_row["Z_OPT"]),
        )
        if result.rowcount != 1:
            raise AdapterError(
                "List membership changed while the move was being applied",
                code="concurrent_modification",
            )
        bump_cloud_state(con, list_row.get("ZCKCLOUDSTATE"), now)
        refreshed_list = con.execute(
            "select ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA from ZREMCDBASELIST where Z_PK=?",
            (list_row["Z_PK"],),
        ).fetchone()
        refreshed_mapping = membership_map(
            refreshed_list["ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA"] if refreshed_list else None
        )
        if refreshed_mapping.get(reminder_key) != section["ZCKIDENTIFIER"].upper():
            raise AdapterError("Section move could not be read back", code="schema_mismatch")
        con.commit()
        warning = log_action(
            "move_to_section",
            {
                "operation_id": operation_id,
                "reminder": reminder["ZCKIDENTIFIER"],
                "section": section["ZCKIDENTIFIER"],
            },
        )
        json_out(
            operation_receipt(
                status="verified",
                operation="move_to_section",
                operation_id=operation_id,
                backend="sqlite_private",
                target={
                    "id": reminder["ZCKIDENTIFIER"],
                    "section_id": section["ZCKIDENTIFIER"],
                },
                before={"reminder": before_reminder, "section_id": previous_section_id},
                after={
                    "reminder": reminder_mutation_snapshot(reread_reminder(con, reminder["Z_PK"])),
                    "section_id": section["ZCKIDENTIFIER"],
                    "section": section["ZDISPLAYNAME"],
                },
                verification={"state": "read_back", "section_membership": True},
                recovery={
                    "semantics": "move_to_previous_section" if previous_section_id else "remove_section_membership",
                    "previous_section_id": previous_section_id,
                    "automatic_restore_available": False,
                },
                warnings=[warning] if warning else None,
                capability=capability,
            )
        )
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def attach_image_record(con: sqlite3.Connection, reminder: dict[str, Any], image: Path) -> dict[str, Any]:
    if not image.exists():
        raise AdapterError(f"Image not found: {image.name}")
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
    stored_created = False
    if not stored.exists():
        shutil.copy2(image, stored)
        stored_created = True
    try:
        now = core_now()
        object_id = str(uuid.uuid4()).upper()
        display_filename = f"{object_id}-codex.{ext}"
        object_pk = con.execute(
            "select Z_MAX + 1 from Z_PRIMARYKEY where Z_NAME='REMCDObject'"
        ).fetchone()[0]
        cloud_pk = con.execute(
            "select Z_MAX + 1 from Z_PRIMARYKEY where Z_NAME='REMCKCloudState'"
        ).fetchone()[0]
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
            "stored_file": stored.name,
            "_stored_path": str(stored),
            "_stored_file_created": stored_created,
            "width": width,
            "height": height,
        }
    except Exception:
        if stored_created:
            stored.unlink(missing_ok=True)
        raise


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


def compensate_new_attachment(
    con: sqlite3.Connection,
    reminder: dict[str, Any],
    result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not result:
        return None
    row = result.get("_row")
    if not isinstance(row, dict):
        return None
    con.rollback()
    con.execute("begin immediate")
    try:
        deleted = soft_delete_attachment_record(con, reminder, row)
        con.commit()
        return deleted
    except Exception:
        con.rollback()
        raise


def attachment_replacement_readback(
    con: sqlite3.Connection,
    *,
    old_attachment_pk: int,
    new_attachment_pk: int,
) -> dict[str, bool]:
    old_state = con.execute(
        "select ZMARKEDFORDELETION from ZREMCDOBJECT where Z_PK=?",
        (old_attachment_pk,),
    ).fetchone()
    active_new = con.execute(
        """
        select count(*) from ZREMCDOBJECT
        where Z_PK=? and coalesce(ZMARKEDFORDELETION,0)=0
        """,
        (new_attachment_pk,),
    ).fetchone()[0]
    return {
        "old_attachment_soft_deleted": bool(old_state and old_state["ZMARKEDFORDELETION"]),
        "new_attachment_active": active_new == 1,
    }


def attach_image_once(args: argparse.Namespace) -> dict[str, Any]:
    require_exact_reminder_selector(
        reminder_id=args.id,
        title=args.title,
        list_name=args.list,
    )
    operation_id = new_operation_id()
    db = resolve_database(args.db, write=True)
    image = Path(args.image).expanduser().resolve()
    con = connect(db)
    try:
        capability = require_command_capability(con, "attachment_mutation_db")
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        require_reminder_version(reminder, getattr(args, "if_version", None))
        before = reminder_mutation_snapshot(reminder)
        if args.backend == "reminderkit":
            try:
                result = attach_image_reminderkit_record(con, reminder, image)
                status = "verified"
                verification = {
                    "state": "read_back",
                    "mobile_visible_likely": True,
                    "sync_status": "verified_mobile_visible",
                }
                recovery = {"semantics": "soft_delete_attachment"}
                pending_warning = None
            except AttachmentVerificationError as exc:
                result = exc.compensation_result()
                status = "committed_verification_pending"
                verification = {
                    "state": "pending",
                    "mobile_visible_likely": False,
                    "sync_status": "persisted_sync_pending",
                }
                recovery = {
                    "semantics": "manual_cleanup_available",
                    "command": exc.details.get("cleanup_command"),
                }
                pending_warning = {
                    "code": "mobile_visibility_pending",
                    "message": str(exc),
                }
            except AdapterError as exc:
                if not exc.details.get("partial_failure"):
                    raise
                result = {
                    "attached": None,
                    "backend": "reminderkit",
                    "attachment": {},
                    "sync": {"mobile_visible_likely": False},
                }
                status = "committed_verification_pending"
                verification = {
                    "state": "pending",
                    "mobile_visible_likely": False,
                    "sync_status": "mutation_outcome_unknown",
                    "mutation_outcome_unknown": exc.details.get(
                        "mutation_outcome_unknown", True
                    ),
                }
                recovery = {
                    "semantics": "inspect_reminder_attachments_before_retry",
                    "automatic_cleanup_available": False,
                }
                pending_warning = {
                    "code": exc.code,
                    "message": str(exc),
                }
        else:
            con.execute("begin immediate")
            result = attach_image_record(con, reminder, image)
            try:
                con.commit()
            except Exception:
                con.rollback()
                if result.get("_stored_file_created") and result.get("_stored_path"):
                    Path(result["_stored_path"]).unlink(missing_ok=True)
                raise
            result["backend"] = "db"
            result["sync"] = {
                "mobile_visible_likely": False,
                "reason": "sqlite_local_attachment_without_cloudkit_server_record",
            }
            result["warning"] = (
                "This image was attached through the SQLite fallback path. It can render on this Mac, "
                "but it is not expected to appear on iPhone until a native Reminders/CloudKit attachment path creates a server record."
            )
            status = "unchanged" if result.get("attached") is False else "verified"
            verification = {
                "state": "read_back",
                "mobile_visible_likely": False,
                "sync_status": "local_only",
            }
            recovery = {"semantics": "soft_delete_attachment"}
            pending_warning = {
                "code": "local_only_attachment",
                "message": result["warning"],
            }
        attachment = result["attachment"]
        journal_warning = log_action(
            "attach_image",
            {
                "operation_id": operation_id,
                "backend": result.get("backend", args.backend),
                "reminder": reminder["ZCKIDENTIFIER"],
                "image": image.name,
                "object": attachment.get("id"),
                "stored": result.get("stored_file"),
                "mobile_visible_likely": result.get("sync", {}).get("mobile_visible_likely"),
            },
        )
        result.pop("_row", None)
        result.pop("_stored_path", None)
        result.pop("_stored_file_created", None)
        try:
            after_reminder = reminder_mutation_snapshot(reread_reminder(con, reminder["Z_PK"]))
        except (AdapterError, sqlite3.Error):
            after_reminder = {
                "id": reminder.get("ZCKIDENTIFIER"),
                "store_read_back_pending": True,
            }
        warnings = [item for item in (pending_warning, journal_warning) if item]
        return operation_receipt(
            status=status,
            operation="attach_image",
            operation_id=operation_id,
            backend=result.get("backend", args.backend),
            target={
                "reminder_id": reminder["ZCKIDENTIFIER"],
                "attachment_id": attachment.get("id"),
            },
            before=before,
            after={
                "reminder": after_reminder,
                "attachment": attachment,
                "sync": result.get("sync", {}),
            },
            verification=verification,
            recovery=recovery,
            warnings=warnings or None,
            db=str(db),
            attached=result.get("attached", True),
            capability=capability,
        )
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def cmd_attach_image(args: argparse.Namespace) -> int:
    image = Path(args.image).expanduser().resolve()
    image_hash = hashlib.sha256(image.read_bytes()).hexdigest() if image.exists() else "missing"
    result = execute_idempotent(
        operation="attach_image",
        key=args.idempotency_key,
        input_payload={
            "id": args.id,
            "title": args.title,
            "list": args.list,
            "backend": args.backend,
            "image_sha256": image_hash,
            "if_version": getattr(args, "if_version", None),
        },
        callback=lambda: attach_image_once(args),
    )
    json_out(result)
    return 0


def cmd_attach_url(args: argparse.Namespace) -> int:
    require_exact_reminder_selector(
        reminder_id=args.id,
        title=args.title,
        list_name=args.list,
    )
    db = resolve_database(args.db, write=True)
    url = normalized_url(args.url)
    operation_id = new_operation_id()
    con = connect(db)
    try:
        capability = require_command_capability(con, "attachment_mutation_db")
        con.execute("begin immediate")
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        require_reminder_version(reminder, getattr(args, "if_version", None))
        before = reminder_mutation_snapshot(reminder)
        result = attach_url_record(con, reminder, url)
        attachment = result["attachment"]
        verified = con.execute(
            """
            select count(*) from ZREMCDOBJECT
            where Z_PK=? and ZREMINDER2=? and Z_ENT=? and ZURL=?
              and coalesce(ZMARKEDFORDELETION,0)=0
            """,
            (attachment["pk"], reminder["Z_PK"], URL_ATTACHMENT_ENT, url),
        ).fetchone()[0] == 1
        if not verified:
            raise AdapterError("URL attachment could not be read back", code="schema_mismatch")
        after = reminder_mutation_snapshot(reread_reminder(con, reminder["Z_PK"]))
        con.commit()
        warning = log_action(
            "attach_url",
            {
                "operation_id": operation_id,
                "reminder": reminder["ZCKIDENTIFIER"],
                "url": url,
                "object": attachment["id"],
            },
        )
        json_out(
            operation_receipt(
                status="verified" if result.get("attached") else "unchanged",
                operation="attach_url",
                operation_id=operation_id,
                backend="sqlite_private",
                target={
                    "id": reminder["ZCKIDENTIFIER"],
                    "attachment_id": attachment["id"],
                },
                before=before,
                after={"reminder": after, "attachment": attachment},
                verification={"state": "read_back", "attachment_active": True},
                recovery=(
                    {
                        "semantics": "delete_attachment",
                        "command": (
                            f"delete_attachment --id {reminder['ZCKIDENTIFIER']} "
                            f"--attachment-id {attachment['id']}"
                        ),
                    }
                    if result.get("attached")
                    else {"semantics": "not_applicable"}
                ),
                warnings=[warning] if warning else None,
                capability=capability,
            )
        )
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def cmd_list_attachments(args: argparse.Namespace) -> int:
    require_exact_reminder_selector(
        reminder_id=args.id,
        title=args.title,
        list_name=args.list,
    )
    db = resolve_database(args.db)
    con = connect(db)
    try:
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        ent = attachment_ent_for_type(args.type)
        rows = active_attachment_rows(
            con,
            reminder["Z_PK"],
            attachment_ent=ent,
            limit=args.limit + 1,
        )
        items = [attachment_payload(row) for row in rows[: args.limit]]
        json_out(
            {
                "ok": True,
                "db": str(db),
                "reminder_id": reminder["ZCKIDENTIFIER"],
                "reminder_version": reminder.get("Z_OPT"),
                "attachments": items,
                "limit": args.limit,
                "truncated": len(rows) > args.limit,
            }
        )
        return 0
    finally:
        con.close()


def cmd_audit_attachments(args: argparse.Namespace) -> int:
    db = resolve_database(args.db)
    con = connect(db)
    try:
        counts = image_attachment_audit_counts(
            con,
            search=args.search,
            list_name=args.list,
        )
        items, _ = image_attachment_audit_items(
            con,
            search=args.search,
            list_name=args.list,
            problems_only=args.problems_only,
            limit=args.limit,
        )
        total_matching = counts["problems"] if args.problems_only else counts["total"]
        for item in items:
            item.pop("_row", None)
        json_out(
            {
                "ok": True,
                "db": str(db),
                "scope": {"search": args.search, "list": args.list},
                "counts": {
                    "image_attachments": counts["total"],
                    "local_only_or_not_mobile_visible": counts["problems"],
                    "returned": len(items),
                },
                "limit": args.limit,
                "truncated": total_matching > len(items),
                "items": items,
            }
        )
        return 0
    finally:
        con.close()


def repair_candidate_digest(candidates: list[dict[str, Any]]) -> str:
    stable = [
        {
            "reminder_id": item.get("reminder", {}).get("id"),
            "attachment_id": item.get("attachment", {}).get("id"),
            "problem": item.get("problem"),
            "source_files": sorted(item.get("source_files", [])),
            "repairable": bool(item.get("repairable")),
        }
        for item in candidates
    ]
    return stable_hash(stable)


def cmd_repair_attachments(args: argparse.Namespace) -> int:
    operation_id = new_operation_id()
    db = resolve_database(args.db, write=args.apply)
    con = connect(db)
    try:
        capabilities = attachment_sync_capabilities(con)
        if args.apply and not capabilities["available"]:
            raise AdapterError(
                "Attachment repair apply is unavailable because sync verification columns are missing",
                missing_columns=capabilities["missing_columns"],
            )
        counts = image_attachment_audit_counts(
            con,
            search=args.search,
            list_name=args.list,
        )
        problem_items, _ = image_attachment_audit_items(
            con,
            search=args.search,
            list_name=args.list,
            problems_only=True,
            limit=args.limit,
        )
        problem_count = counts["problems"]
        selected = problem_items
        candidates: list[dict[str, Any]] = []
        for item in selected:
            attachment = item["attachment"]
            sources = source_paths_for_attachment(attachment)
            public_item = {
                "reminder": item["reminder"],
                "attachment": attachment,
                "problem": item["problem"],
                "source_files": [path.name for path in sources],
                "repairable": bool(sources),
            }
            candidates.append(public_item)
        scope = {"search": args.search, "list": args.list, "limit": args.limit}
        truncated = problem_count > len(selected)
        candidate_digest = repair_candidate_digest(candidates)

        if not args.apply:
            json_out(
                operation_receipt(
                    status="unchanged",
                    operation="repair_attachments_preview",
                    operation_id=operation_id,
                    backend="reminderkit_private_maintenance",
                    target={"scope": scope, "candidate_digest": candidate_digest},
                    after={
                        "counts": {
                            "local_only_or_not_mobile_visible": problem_count,
                            "selected": len(selected),
                            "repairable": sum(1 for item in candidates if item["repairable"]),
                        },
                        "truncated": truncated,
                        "candidates": candidates,
                        "next_step": (
                            "Run again with --apply and --preview-digest using this candidate digest."
                        ),
                    },
                    verification={"state": "candidate_snapshot", "candidate_digest": candidate_digest},
                    recovery={"semantics": "not_applicable"},
                )
            )
            return 0

        preview_digest = getattr(args, "preview_digest", None)
        if not isinstance(preview_digest, str) or not preview_digest:
            raise AdapterError(
                "repair_attachments --apply requires the candidate digest from a dry run",
                code="ambiguous_scope",
            )
        if truncated:
            raise AdapterError(
                "Repair candidates exceed the requested limit; narrow the scope",
                code="ambiguous_scope",
                candidate_count_at_least=args.limit + 1,
            )
        if preview_digest != candidate_digest:
            raise AdapterError(
                "Attachment repair candidate set changed since preview",
                code="concurrent_modification",
                expected_digest=preview_digest,
                current_digest=candidate_digest,
            )

        backup = None
        if selected and not args.no_backup:
            backup = create_store_backup()
            log_action("backup_store", {"path": backup["backup"], "reason": "repair_attachments"})

        repaired: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for item in selected:
            attachment = item["attachment"]
            sources = source_paths_for_attachment(attachment)
            if not sources:
                failed.append(
                    {
                        "reminder": item["reminder"],
                        "attachment": attachment,
                        "error": "source_image_file_not_found",
                    }
                )
                continue
            source = sources[0]
            reminder = None
            new_result = None
            item_committed = False
            try:
                reminder = find_reminder(con, reminder_id=item["reminder"]["id"], title=None, list_name=None)
                try:
                    new_result = attach_image_reminderkit_record(con, reminder, source)
                except AttachmentVerificationError as attach_exc:
                    new_result = attach_exc.compensation_result()
                    raise
                con.execute("begin immediate")
                deleted = soft_delete_attachment_record(con, reminder, item["_row"])
                con.commit()
                item_committed = True
                result = {
                    "reminder": item["reminder"],
                    "old_attachment": attachment,
                    "new_attachment": new_result["attachment"],
                    "source_file": source.name,
                    "deleted_state": deleted,
                    "sync": new_result.get("sync", {}),
                }
                log_action(
                    "repair_image_attachment",
                    {
                        "reminder": item["reminder"]["id"],
                        "old_attachment": attachment["id"],
                        "new_attachment": new_result["attachment"]["id"],
                        "source": source.name,
                        "mobile_visible_likely": new_result.get("sync", {}).get("mobile_visible_likely"),
                    },
                )
                repaired.append(result)
            except Exception as exc:
                con.rollback()
                compensation = None
                compensation_error = None
                if not item_committed and reminder is not None and new_result is not None:
                    try:
                        compensation = compensate_new_attachment(con, reminder, new_result)
                    except Exception as cleanup_exc:
                        compensation_error = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                failed.append(
                    {
                        "reminder": item["reminder"],
                        "attachment": attachment,
                        "source_file": source.name,
                        "error": f"{type(exc).__name__}: {exc}",
                        "partial_failure": new_result is not None,
                        "new_attachment_id": (
                            new_result.get("attachment", {}).get("id")
                            if new_result is not None
                            else None
                        ),
                        "replacement_committed": item_committed,
                        "compensated": compensation is not None,
                        "compensation_error": compensation_error,
                    }
                )

        uncompensated_partial = any(
            item.get("partial_failure")
            and (not item.get("compensated") or item.get("compensation_error"))
            for item in failed
        )
        if not selected:
            status = "unchanged"
        elif not failed:
            status = "verified"
        elif repaired:
            status = "partial_success"
        elif uncompensated_partial:
            status = "failed_manual_repair_required"
        else:
            status = "failed_no_mutation"
        json_out(
            operation_receipt(
                status=status,
                operation="repair_attachments",
                operation_id=operation_id,
                backend="reminderkit_private_maintenance",
                target={"scope": scope, "candidate_digest": candidate_digest},
                before={"candidates": candidates},
                after={
                    "counts": {
                        "local_only_or_not_mobile_visible": problem_count,
                        "selected": len(selected),
                        "repaired": len(repaired),
                        "failed": len(failed),
                    },
                    "repaired": repaired,
                    "failed": failed,
                },
                verification={
                    "state": "per_item_read_back",
                    "verified_repairs": len(repaired),
                    "failed_repairs": len(failed),
                    "manual_repair_required": uncompensated_partial,
                },
                recovery={
                    "semantics": "best_effort_live_container_backup",
                    "backup": backup,
                    "automatic_restore_available": False,
                },
                warnings=(
                    [backup["warning"]]
                    if backup and backup.get("warning")
                    else None
                ),
            )
        )
        return 0 if not failed else 1
    finally:
        con.close()


def cmd_delete_attachment(args: argparse.Namespace) -> int:
    require_exact_reminder_selector(
        reminder_id=args.id,
        title=args.title,
        list_name=args.list,
    )
    db = resolve_database(args.db, write=True)
    operation_id = new_operation_id()
    con = connect(db)
    try:
        capability = require_command_capability(con, "attachment_mutation_db")
        con.execute("begin immediate")
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        require_reminder_version(reminder, getattr(args, "if_version", None))
        before_reminder = reminder_mutation_snapshot(reminder)
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
            raise AdapterError(
                reason or "Attachment selection is ambiguous",
                code="ambiguous_target",
                candidates=candidates,
            )
        before_attachment = attachment_payload(selected)
        deleted = soft_delete_attachment_record(con, reminder, selected)
        refreshed_attachment = con.execute(
            "select ZMARKEDFORDELETION from ZREMCDOBJECT where Z_PK=?",
            (selected["Z_PK"],),
        ).fetchone()
        if not refreshed_attachment or not refreshed_attachment["ZMARKEDFORDELETION"]:
            raise AdapterError("Attachment deletion could not be read back", code="schema_mismatch")
        after_reminder = reminder_mutation_snapshot(reread_reminder(con, reminder["Z_PK"]))
        con.commit()
        warning = log_action(
            "delete_attachment_soft",
            {
                "operation_id": operation_id,
                "reminder": reminder["ZCKIDENTIFIER"],
                "attachment": before_attachment,
            },
        )
        json_out(
            operation_receipt(
                status="verified",
                operation="delete_attachment",
                operation_id=operation_id,
                backend="sqlite_private",
                target={
                    "id": reminder_url(reminder["ZCKIDENTIFIER"]),
                    "attachment_id": before_attachment["id"],
                },
                before={"reminder": before_reminder, "attachment": before_attachment},
                after={"reminder": after_reminder, "attachment": deleted},
                verification={"state": "read_back", "attachment_soft_deleted": True},
                recovery={
                    "semantics": (
                        "reattach_url"
                        if before_attachment.get("type") == "url"
                        else "reattach_from_source_if_available"
                    ),
                    "automatic_restore_available": False,
                },
                warnings=[warning] if warning else None,
                capability=capability,
            )
        )
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def replace_attachment_once(args: argparse.Namespace) -> dict[str, Any]:
    require_exact_reminder_selector(
        reminder_id=args.id,
        title=args.title,
        list_name=args.list,
    )
    operation_id = new_operation_id()
    db = resolve_database(args.db, write=True)
    if bool(args.image) == bool(args.url):
        raise AdapterError("replace_attachment requires exactly one of --image or --url")
    replacement_type = "image" if args.image else "url"
    selector_type = args.type or replacement_type
    con = connect(db)
    reminder = None
    new_result = None
    capability: dict[str, Any] = {}
    before_reminder: dict[str, Any] = {}
    old_attachment: dict[str, Any] = {}
    new_attachment: dict[str, Any] = {}
    committed = False
    try:
        capability = require_command_capability(con, "attachment_mutation_db")
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        expected_version = getattr(args, "if_version", None)
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            expected_version = None
        require_reminder_version(reminder, expected_version)
        before_reminder = reminder_mutation_snapshot(reminder)
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
            raise AdapterError(
                reason or "Attachment selection is ambiguous",
                code="ambiguous_target",
                candidates=candidates,
            )
        old_attachment = attachment_payload(selected)
        if args.image:
            try:
                new_result = attach_image_reminderkit_record(
                    con,
                    reminder,
                    Path(args.image).expanduser().resolve(),
                )
            except AttachmentVerificationError as attach_exc:
                new_result = attach_exc.compensation_result()
                raise
            new_attachment = new_result.get("attachment") or {}
            if int(new_attachment.get("pk", -1)) == int(selected["Z_PK"]):
                raise AdapterError("Replacement source is the same as the selected existing attachment")
            con.execute("begin immediate")
            deleted = soft_delete_attachment_record(con, reminder, selected)
        else:
            con.execute("begin immediate")
            new_result = attach_url_record(con, reminder, args.url)
            new_attachment = new_result.get("attachment") or {}
            if int(new_attachment.get("pk", -1)) == int(selected["Z_PK"]):
                raise AdapterError("Replacement source is the same as the selected existing attachment")
            deleted = soft_delete_attachment_record(con, reminder, selected)
        con.commit()
        committed = True
        readback = attachment_replacement_readback(
            con,
            old_attachment_pk=selected["Z_PK"],
            new_attachment_pk=int(new_attachment.get("pk")),
        )
        if not all(readback.values()):
            public_new_result = dict(new_result)
            public_new_result.pop("_row", None)
            return operation_receipt(
                status="failed_manual_repair_required",
                operation="replace_attachment",
                operation_id=operation_id,
                backend="reminderkit" if args.image else "sqlite_private",
                target={
                    "id": reminder["ZCKIDENTIFIER"],
                    "old_attachment_id": old_attachment.get("id"),
                    "new_attachment_id": new_attachment.get("id"),
                },
                before={"reminder": before_reminder, "attachment": old_attachment},
                after={"new_attachment": public_new_result, "old_attachment": deleted},
                verification={
                    "state": "manual_repair_required",
                    **readback,
                    "replacement_committed": True,
                },
                recovery={
                    "semantics": "inspect_both_attachments_and_restore_manually",
                    "automatic_restore_available": False,
                },
                error={
                    "code": "sync_pending",
                    "message": "Attachment replacement committed but read-back was inconclusive.",
                },
                capability=capability,
            )
        warning = log_action(
            "replace_attachment",
            {
                "operation_id": operation_id,
                "reminder": reminder["ZCKIDENTIFIER"],
                "old": old_attachment,
                "new": new_result.get("attachment"),
            },
        )
        new_result.pop("_row", None)
        return operation_receipt(
            status="verified",
            operation="replace_attachment",
            operation_id=operation_id,
            backend="reminderkit" if args.image else "sqlite_private",
            target={
                "id": reminder["ZCKIDENTIFIER"],
                "old_attachment_id": old_attachment.get("id"),
                "new_attachment_id": new_attachment.get("id"),
            },
            before={"reminder": before_reminder, "attachment": old_attachment},
            after={"new_attachment": new_result, "old_attachment": deleted},
            verification={
                "state": "read_back",
                **readback,
                "mobile_visible_likely": (
                    new_result.get("sync", {}).get("mobile_visible_likely")
                    if args.image
                    else None
                ),
            },
            recovery={
                "semantics": "restore_previous_attachment_manually",
                "automatic_restore_available": False,
            },
            warnings=[warning] if warning else None,
            capability=capability,
        )
    except Exception as exc:
        con.rollback()
        if args.image and not committed and reminder is not None and new_result is not None:
            public_new_result = dict(new_result)
            public_new_result.pop("_row", None)
            new_attachment_id = new_result.get("attachment", {}).get("id")
            try:
                compensated = compensate_new_attachment(con, reminder, new_result)
            except Exception as cleanup_exc:
                return operation_receipt(
                    status="failed_manual_repair_required",
                    operation="replace_attachment",
                    operation_id=operation_id,
                    backend="reminderkit",
                    target={
                        "id": reminder["ZCKIDENTIFIER"],
                        "old_attachment_id": old_attachment.get("id"),
                        "new_attachment_id": new_attachment_id,
                    },
                    before={"reminder": before_reminder, "attachment": old_attachment},
                    after={
                        "new_attachment": public_new_result,
                        "old_attachment_unchanged": True,
                    },
                    verification={
                        "state": "manual_repair_required",
                        "replacement_committed": False,
                        "compensation_succeeded": False,
                    },
                    recovery={
                        "semantics": "delete_new_attachment_manually",
                        "automatic_restore_available": False,
                        "cleanup_command": (
                            f"delete_attachment --id {reminder['ZCKIDENTIFIER']} "
                            f"--attachment-id {new_attachment_id}"
                            if new_attachment_id
                            else None
                        ),
                    },
                    error={
                        "code": "sync_pending",
                        "message": "Image replacement failed and compensating cleanup also failed.",
                        "original_error": f"{type(exc).__name__}: {exc}",
                        "compensation_error": f"{type(cleanup_exc).__name__}: {cleanup_exc}",
                    },
                    capability=capability,
                )
            return operation_receipt(
                status="failed_no_mutation",
                operation="replace_attachment",
                operation_id=operation_id,
                backend="reminderkit",
                target={
                    "id": reminder["ZCKIDENTIFIER"],
                    "old_attachment_id": old_attachment.get("id"),
                    "new_attachment_id": new_attachment_id,
                },
                before={"reminder": before_reminder, "attachment": old_attachment},
                after={
                    "new_attachment": public_new_result,
                    "old_attachment_unchanged": True,
                    "compensated_attachment": compensated,
                },
                verification={
                    "state": "compensated",
                    "replacement_committed": False,
                    "compensation_succeeded": compensated is not None,
                },
                recovery={"semantics": "not_applicable_after_compensation"},
                error={
                    "code": exc.code if isinstance(exc, AdapterError) else "sync_pending",
                    "message": "Image replacement failed; the new attachment was compensated.",
                    "original_error": f"{type(exc).__name__}: {exc}",
                },
                capability=capability,
            )
        raise
    finally:
        con.close()


def cmd_replace_attachment(args: argparse.Namespace) -> int:
    image_hash = None
    if args.image:
        image = Path(args.image).expanduser().resolve()
        image_hash = hashlib.sha256(image.read_bytes()).hexdigest() if image.exists() else "missing"
    key = getattr(args, "idempotency_key", None)
    if not isinstance(key, str):
        key = None
    result = execute_idempotent(
        operation="replace_attachment",
        key=key,
        input_payload={
            "id": args.id,
            "attachment_id": args.attachment_id,
            "attachment_pk": args.attachment_pk,
            "image_sha256": image_hash,
            "url": args.url,
            "if_version": getattr(args, "if_version", None),
        },
        callback=lambda: replace_attachment_once(args),
    )
    json_out(result)
    return 0 if result.get("ok") is True else 1


def cache_path_from_args(args: argparse.Namespace) -> Path:
    return Path(args.cache).expanduser() if getattr(args, "cache", None) else CACHE_FILE


def cmd_cache_rebuild(args: argparse.Namespace) -> int:
    db = resolve_database(args.db)
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
    parser.add_argument("--priority", type=reminder_priority)
    parser.add_argument("--limit", type=positive_int, default=20)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apple Reminders local JSON adapter")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("backup_store")
    p.add_argument("--output")
    p.set_defaults(func=cmd_backup_store)

    p = sub.add_parser("purge_logs")
    p.set_defaults(func=cmd_purge_logs)

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
    p.add_argument("--limit", type=positive_int, default=100)
    p.set_defaults(func=cmd_list_lists)

    p = sub.add_parser("list_sections")
    add_common_db(p)
    p.add_argument("--list")
    p.add_argument("--limit", type=positive_int, default=100)
    p.set_defaults(func=cmd_list_sections)

    p = sub.add_parser("snapshot")
    add_common_db(p)
    p.add_argument("--list")
    p.add_argument("--include-completed", action="store_true")
    p.add_argument("--limit", type=positive_int, default=1000)
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("search_reminders")
    add_common_db(p)
    p.add_argument("query")
    p.add_argument("--list")
    p.add_argument("--include-completed", action="store_true")
    p.add_argument("--limit", type=positive_int, default=20)
    p.set_defaults(func=cmd_search_reminders)

    p = sub.add_parser("read_reminder")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.set_defaults(func=cmd_read_reminder)

    p = sub.add_parser("show_reminder")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.set_defaults(func=cmd_show_reminder)

    p = sub.add_parser("list_tags")
    add_common_db(p)
    p.add_argument("--query")
    p.add_argument("--limit", type=positive_int, default=100)
    p.set_defaults(func=cmd_list_tags)

    p = sub.add_parser("add_tag")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--tag", required=True)
    p.add_argument("--if-version", type=int)
    p.set_defaults(func=cmd_add_tag)

    p = sub.add_parser("remove_tag")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--tag", required=True)
    p.add_argument("--if-version", type=int)
    p.set_defaults(func=cmd_remove_tag)

    p = sub.add_parser("cleanup_tags")
    add_common_db(p)
    p.add_argument("--tag")
    p.add_argument("--prefix")
    p.add_argument("--account-id")
    p.add_argument("--preview-digest")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--limit", type=positive_int, default=100)
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
    p.add_argument("--priority", type=reminder_priority)
    p.add_argument("--idempotency-key")
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
    p.add_argument("--priority", type=reminder_priority)
    p.add_argument("--due-at")
    p.add_argument("--remind-at")
    p.add_argument("--all-day-due-date")
    p.add_argument("--clear-due", action="store_true")
    p.add_argument("--if-version", type=int)
    p.set_defaults(func=cmd_update_reminder)

    p = sub.add_parser("complete_reminder")
    add_common_db(p)
    p.add_argument("--backend", choices=["db", "applescript"], default="db")
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--if-version", type=int)
    p.set_defaults(func=cmd_complete_reminder)

    p = sub.add_parser("reopen_reminder")
    add_common_db(p)
    p.add_argument("--backend", choices=["db", "applescript"], default="db")
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--if-version", type=int)
    p.set_defaults(func=cmd_reopen_reminder)

    p = sub.add_parser("delete_reminder")
    add_common_db(p)
    p.add_argument("--backend", choices=["auto", "db", "applescript"], default="auto")
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--if-version", type=int)
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
    p.add_argument("--if-version", type=int)
    p.set_defaults(func=cmd_move_to_section)

    p = sub.add_parser("attach_image")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--image", required=True)
    p.add_argument("--backend", choices=["reminderkit", "db"], default="reminderkit")
    p.add_argument("--if-version", type=int)
    p.add_argument("--idempotency-key")
    p.set_defaults(func=cmd_attach_image)

    p = sub.add_parser("attach_url")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--url", required=True)
    p.add_argument("--if-version", type=int)
    p.set_defaults(func=cmd_attach_url)

    p = sub.add_parser("list_attachments")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--type", choices=["image", "url"])
    p.add_argument("--limit", type=positive_int, default=100)
    p.set_defaults(func=cmd_list_attachments)

    p = sub.add_parser("audit_attachments")
    add_common_db(p)
    p.add_argument("--search")
    p.add_argument("--list")
    p.add_argument("--problems-only", action="store_true")
    p.add_argument("--limit", type=positive_int, default=100)
    p.set_defaults(func=cmd_audit_attachments)

    p = sub.add_parser("repair_attachments")
    add_common_db(p)
    p.add_argument("--search")
    p.add_argument("--list")
    p.add_argument("--limit", type=positive_int, default=50)
    p.add_argument("--preview-digest")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_repair_attachments)

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
    p.add_argument("--if-version", type=int)
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
    p.add_argument("--if-version", type=int)
    p.add_argument("--idempotency-key")
    p.set_defaults(func=cmd_replace_attachment)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AdapterError as exc:
        details = dict(exc.details)
        explicit_status = details.pop("result_status", None)
        if explicit_status is None and details.get("partial_failure"):
            explicit_status = (
                "failed_no_mutation"
                if details.get("compensated") and not details.get("compensation_error")
                else "failed_manual_repair_required"
            )
        return fail(
            str(exc),
            code=exc.code,
            status=explicit_status or "failed_no_mutation",
            **details,
        )
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}", code="unexpected_error")


if __name__ == "__main__":
    raise SystemExit(main())
