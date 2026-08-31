#!/usr/bin/env python3
"""Dependency-light Apple Reminders adapter.

This is the core local adapter. It remains a JSON CLI/library behind the
bundled MCP server; the transport layer does not own its business logic.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from receipt_contract import (  # noqa: E402
    AdapterError,
    FAILURE_RECEIPT_STATUSES,
    MutationNotStartedError,
    SUCCESS_RECEIPT_STATUSES,
    build_operation_receipt,
)
from bounded_process import (  # noqa: E402
    ProcessError,
    ProcessLaunchError,
    ProcessTimeoutError,
    run as run_bounded_process,
)
from durable_idempotency import execute_idempotent  # noqa: E402
from reminders_contracts import (  # noqa: E402
    REQUIRED_TABLES as CONTRACT_REQUIRED_TABLES,
    command_schema_requirements,
)
from reminders_image_input import (  # noqa: E402
    ImageInputError,
    ValidatedImage,
    validate_image_input,
)
from experimental_capabilities import (  # noqa: E402
    capability_for_adapter_command,
    detect_runtime_identity,
    evaluate_capability,
    resolve_selected_clang,
)


HOME = Path.home()
GROUP = HOME / "Library/Group Containers/group.com.apple.reminders/Container_v1"
STORES = GROUP / "Stores"
FILES = GROUP / "Files"
APP_SUPPORT = HOME / "Library/Application Support/apple-reminders-codex"
JOURNAL = APP_SUPPORT / "actions.jsonl"
CACHE_DIR = HOME / "Library/Caches/apple-reminders-codex"
APPLE_EPOCH_OFFSET = 978307200
IMAGE_ATTACHMENT_ENT = 25
URL_ATTACHMENT_ENT = 26
TAG_OBJECT_ENT = 32
SUBPROCESS_TIMEOUT_SECONDS = 30
BUILDER_STDOUT_LIMIT_BYTES = 256 * 1024
BUILDER_STDERR_LIMIT_BYTES = 1024 * 1024
NATIVE_STDOUT_LIMIT_BYTES = 256 * 1024
NATIVE_STDERR_LIMIT_BYTES = 256 * 1024
IMAGE_METADATA_OUTPUT_LIMIT_BYTES = 64 * 1024
ATTACHMENT_VERIFY_TIMEOUT_SECONDS = 10
REMINDERKIT_REMOVAL_SETTLE_SECONDS = 0.5
REMINDERKIT_REMOVAL_VERIFY_TIMEOUT_SECONDS = 10
SECTION_SYNC_VERIFY_TIMEOUT_SECONDS = 10
JOURNAL_MAX_BYTES = 1_000_000
JOURNAL_RETENTION_DAYS = 30
RECENTLY_DELETED_RETENTION_DAYS = 30
RECENTLY_DELETED_SNAPSHOT_LIMIT = 10_000

REQUIRED_TABLES = set(CONTRACT_REQUIRED_TABLES)
COMMAND_SCHEMA_REQUIREMENTS = command_schema_requirements("runtime")
MUTATION_COMMANDS = frozenset(
    {
        "recover_deleted_reminder",
        "add_tag",
        "remove_tag",
        "create_section",
        "move_to_section",
        "attach_image",
        "copy_image_attachment",
        "attach_url",
        "delete_attachment",
        "replace_attachment",
    }
)


class AttachmentVerificationError(AdapterError):
    def __init__(self, message: str, row: dict[str, Any], **details: Any) -> None:
        code = details.pop("code", "sync_pending")
        self.reason_code = str(details.pop("reason_code", code))
        self.retryable = bool(details.pop("retryable", True))
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
    if status not in FAILURE_RECEIPT_STATUSES:
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
    return build_operation_receipt(
        status=status,
        operation=operation,
        operation_id=operation_id or new_operation_id(),
        backend=backend,
        target=target,
        before=before,
        after=after,
        verification=verification,
        recovery=recovery,
        warnings=warnings,
        **extra,
    )


def command_failure_receipt(
    args: argparse.Namespace,
    message: str,
    *,
    code: str,
    status: str,
    operation: str | None = None,
    **details: Any,
) -> dict[str, Any]:
    target = {
        name: value
        for name in ("id", "list_id", "section_id", "attachment_id")
        if (value := getattr(args, name, None)) is not None
    }
    mutation_not_performed = status == "failed_no_mutation"
    return operation_receipt(
        status=status,
        operation=operation or args.command,
        backend="adapter_boundary",
        target=target,
        after={},
        verification={
            "state": "not_performed" if mutation_not_performed else "manual_repair_required",
            "write_performed": False if mutation_not_performed else None,
        },
        recovery={
            "semantics": "not_applicable"
            if mutation_not_performed
            else "manual_inspection_required"
        },
        error={"code": code, "message": message, **details},
    )


_EXCEPTION_RESULT_STATUSES = FAILURE_RECEIPT_STATUSES | frozenset(
    {"committed_verification_pending", "partial_success"}
)
_IDEMPOTENT_ADAPTER_RECEIPT_OPERATIONS = {
    "recover_deleted_reminder": "recover_deleted_reminder",
    "attach_image": "attach_image",
    "copy_image_attachment": "copy_image",
    "replace_attachment": "replace_attachment",
}


def _json_safe_error_details(details: dict[str, Any]) -> dict[str, Any]:
    """Keep the failure boundary serializable even for malformed internals."""

    try:
        encoded = json.dumps(
            details,
            ensure_ascii=False,
            allow_nan=False,
            default=lambda value: f"<{type(value).__name__}>",
        )
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):
            raise TypeError("error details must remain an object")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        decoded = {
            "detail_redacted": True,
            "detail_error_type": type(exc).__name__,
        }
    for reserved in ("code", "message", "operation", "status"):
        if reserved in decoded:
            decoded[f"detail_{reserved}"] = decoded.pop(reserved)
    return decoded


def _command_exception_receipt(
    args: argparse.Namespace,
    exc: Exception,
    *,
    operation: str | None = None,
    default_adapter_status: str,
    honor_adapter_status: bool = True,
) -> dict[str, Any]:
    if isinstance(exc, AdapterError):
        details = dict(exc.details)
        explicit_status = details.pop("result_status", None)
        explicit_status_recognized = (
            isinstance(explicit_status, str)
            and explicit_status in _EXCEPTION_RESULT_STATUSES
        )
        if not honor_adapter_status:
            status = default_adapter_status
            if explicit_status is not None:
                details["reported_result_status"] = explicit_status
                details.setdefault(
                    "reason_code",
                    (
                        "untyped_adapter_result_status_ignored"
                        if explicit_status_recognized
                        else "invalid_adapter_result_status"
                    ),
                )
        elif explicit_status_recognized:
            status = explicit_status
        elif explicit_status is not None:
            status = "failed_manual_repair_required"
            details.setdefault("reason_code", "invalid_adapter_result_status")
            details["reported_result_status"] = explicit_status
        elif details.get("partial_failure"):
            status = (
                "failed_no_mutation"
                if details.get("compensated")
                and not details.get("compensation_error")
                else "failed_manual_repair_required"
            )
        else:
            status = default_adapter_status
        return command_failure_receipt(
            args,
            str(exc),
            code=exc.code,
            status=status,
            operation=operation,
            **_json_safe_error_details(details),
        )
    return command_failure_receipt(
        args,
        f"{type(exc).__name__}: {exc}",
        code="unexpected_error",
        status="failed_manual_repair_required",
        operation=operation,
    )


def command_exception_receipt(
    args: argparse.Namespace,
    exc: Exception,
    *,
    operation: str | None = None,
) -> dict[str, Any]:
    """Normalize one outer-boundary exception using the legacy CLI policy."""

    return _command_exception_receipt(
        args,
        exc,
        operation=operation,
        default_adapter_status="failed_no_mutation",
    )


def execute_idempotent_adapter_command(
    *,
    args: argparse.Namespace,
    operation: str,
    key: str | None,
    input_payload: dict[str, Any],
    callback: Callable[[], dict[str, Any]],
    storage_dir: Path | None = None,
) -> dict[str, Any]:
    """Persist a complete adapter Receipt before a keyed callback can escape."""

    receipt_operation = _IDEMPOTENT_ADAPTER_RECEIPT_OPERATIONS.get(operation)
    command = getattr(args, "command", None)
    if receipt_operation is None or (
        isinstance(command, str) and command != operation
    ):
        raise ValueError("Unsupported or mismatched idempotent adapter command")
    if not key:
        return callback()

    def receipt_returning_callback() -> dict[str, Any]:
        try:
            return callback()
        except MutationNotStartedError:
            # This typed proof remains the sole authority to clear a new fence.
            raise
        except AdapterError as exc:
            return _command_exception_receipt(
                args,
                exc,
                operation=receipt_operation,
                default_adapter_status="failed_manual_repair_required",
                honor_adapter_status=False,
            )
        except Exception as exc:
            return _command_exception_receipt(
                args,
                exc,
                operation=receipt_operation,
                default_adapter_status="failed_manual_repair_required",
                honor_adapter_status=False,
            )

    return execute_idempotent(
        operation=operation,
        key=key,
        input_payload=input_payload,
        callback=receipt_returning_callback,
        storage_dir=storage_dir,
    )


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


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
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


def connect(db: Path) -> sqlite3.Connection:
    path = Path(db).expanduser()
    if not path.exists():
        raise AdapterError(f"Reminders database not found: {path}")
    uri = f"file:{urllib.parse.quote(str(path.resolve()))}?mode=rw"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("pragma busy_timeout=5000")
    return con


def connect_read_only(db: Path) -> sqlite3.Connection:
    path = Path(db).expanduser()
    if not path.exists():
        raise AdapterError(f"Reminders database not found: {path}")
    uri = f"file:{urllib.parse.quote(str(path.resolve()))}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("pragma query_only=on")
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


def _experimental_capability_failure(
    decision: Any,
    *,
    mutation: bool,
) -> None:
    public = decision.to_public_dict()
    reason_code = str(public["reason_code"])
    code = (
        "schema_mismatch"
        if reason_code in {"schema_unverified", "schema_fingerprint_mismatch"}
        else "unsupported_capability"
    )
    messages = {
        "runtime_unverified": (
            "This Experimental capability has no exact runtime compatibility evidence."
        ),
        "unsupported_build": (
            "This macOS and Reminders build is not allowlisted for the Experimental capability."
        ),
        "compiler_required": (
            "This Experimental capability requires Xcode Command Line Tools."
        ),
        "schema_unverified": (
            "The private Reminders schema could not be matched to reviewed compatibility evidence."
        ),
        "schema_fingerprint_mismatch": (
            "The private Reminders schema differs from the allowlisted build evidence."
        ),
    }
    error_type = MutationNotStartedError if mutation else AdapterError
    raise error_type(
        messages.get(reason_code, "The Experimental capability is unavailable."),
        code=code,
        reason_code=reason_code,
        capability=public,
    )


def preflight_experimental_command(args: argparse.Namespace) -> dict[str, Any] | None:
    """Enforce build/toolchain/schema evidence before an Experimental dispatch.

    Unknown builds and missing helper toolchains fail before the Reminders store
    is even opened.  An allowlisted build then receives a read-only command
    schema check; mutating command functions repeat their schema guard at the
    transaction boundary to close the preflight-to-write race.
    """

    spec = capability_for_adapter_command(
        str(getattr(args, "command", "")),
        backend=getattr(args, "backend", None),
        image=getattr(args, "image", None),
        url=getattr(args, "url", None),
    )
    if spec is None:
        return None
    identity = detect_runtime_identity()
    build_decision = evaluate_capability(
        spec.capability_id,
        identity,
        schema_fingerprint=None,
        compiler_available=True,
    )
    if build_decision.reason_code != "schema_unverified":
        _experimental_capability_failure(build_decision, mutation=spec.mutation)
    compiler_probe = (
        resolve_selected_clang()
        if spec.compiler_requirement == "required"
        else None
    )
    compiler_available = bool(compiler_probe and compiler_probe.available)
    if compiler_probe and compiler_probe.compiler_path is not None:
        setattr(args, "_experimental_compiler_path", compiler_probe.compiler_path)
    if spec.compiler_requirement == "required" and not compiler_available:
        compiler_decision = evaluate_capability(
            spec.capability_id,
            identity,
            schema_fingerprint=None,
            compiler_available=False,
        )
        _experimental_capability_failure(compiler_decision, mutation=spec.mutation)

    db = resolve_database(getattr(args, "db", None))
    con = connect_read_only(db)
    try:
        schema = command_capability(con, spec.schema_command)
    finally:
        con.close()
    decision = evaluate_capability(
        spec.capability_id,
        identity,
        schema_fingerprint=str(schema.get("schema_fingerprint") or "") or None,
        compiler_available=compiler_available,
    )
    if not schema.get("supported") or not decision.allowed:
        _experimental_capability_failure(decision, mutation=spec.mutation)
    public = decision.to_public_dict()
    public["schema_gate"] = "minimum_fields_and_exact_fingerprint"
    setattr(args, "_experimental_capability", public)
    return public


def receipt_capability(
    args: argparse.Namespace,
    schema_capability: dict[str, Any],
) -> dict[str, Any]:
    runtime = getattr(args, "_experimental_capability", None)
    if not isinstance(runtime, dict):
        return schema_capability
    return {**schema_capability, **runtime}


def require_image_helper_compiler(args: argparse.Namespace) -> None:
    """Resolve the conditional delete route before any image helper dispatch."""

    compiler_probe = resolve_selected_clang()
    if compiler_probe.compiler_path is not None:
        setattr(args, "_experimental_compiler_path", compiler_probe.compiler_path)
        return
    capability = dict(getattr(args, "_experimental_capability", {}) or {})
    capability.update(
        {
            "capability": "image_attachment_mutation",
            "support_tier": "experimental_internals",
            "compiler_requirement": "required",
            "runtime_state": "runtime_unverified",
            "reason_code": "compiler_required",
            "available": False,
        }
    )
    raise MutationNotStartedError(
        "This Experimental image capability requires Xcode Command Line Tools.",
        code="unsupported_capability",
        reason_code="compiler_required",
        capability=capability,
    )


def require_private_helper_compiler() -> Path:
    """Return the fixed selected clang path or fail before helper preparation."""

    compiler_probe = resolve_selected_clang()
    if compiler_probe.compiler_path is not None:
        return compiler_probe.compiler_path
    raise MutationNotStartedError(
        "This Experimental helper requires selected Xcode Command Line Tools.",
        code="unsupported_capability",
        reason_code="compiler_required",
        compiler_probe=compiler_probe.reason_code,
    )


def usable_dbs() -> list[Path]:
    paths: list[Path] = []
    for db in sorted(STORES.glob("*.sqlite")):
        try:
            con = connect_read_only(db)
            try:
                if REQUIRED_TABLES <= table_names(con):
                    paths.append(db)
            finally:
                con.close()
        except sqlite3.Error:
            continue
    return paths


def db_counts(db: Path) -> dict[str, int | None]:
    con = connect_read_only(db)
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


def stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


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


def recently_deleted_cutoff() -> float:
    return core_now() - RECENTLY_DELETED_RETENTION_DAYS * 86400


def deleted_attachment_rows(
    con: sqlite3.Connection,
    reminder_pk: int,
) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        select Z_PK,Z_ENT,ZCKIDENTIFIER,ZREMINDER2,Z_FOK_REMINDER1,
               ZFILENAME,ZSHA512SUM,ZUTI,ZFILESIZE,ZWIDTH,ZHEIGHT,
               ZURL,ZHOSTURL,ZMARKEDFORDELETION
        from ZREMCDOBJECT
        where ZREMINDER2=? and Z_ENT in (?,?)
        order by Z_FOK_REMINDER1,Z_PK
        """,
        (reminder_pk, IMAGE_ATTACHMENT_ENT, URL_ATTACHMENT_ENT),
    ).fetchall()
    return [dict(row) for row in rows]


def deleted_image_byte_sha512(row: dict[str, Any]) -> str:
    """Verify one deleted image's backing bytes without exposing its path."""

    expected = row.get("ZSHA512SUM")
    if not isinstance(expected, str) or not re.fullmatch(r"[A-Fa-f0-9]{128}", expected):
        raise AdapterError(
            "The deleted image attachment has no trustworthy content digest",
            code="schema_mismatch",
            reason_code="deleted_image_digest_unavailable",
        )
    attachment = {
        "type": "image",
        "filename": row.get("ZFILENAME"),
        "sha512": expected,
        "uti": row.get("ZUTI"),
        # Deleted Reminder rows can retain an attachment-level deletion marker.
        # Byte verification is intentionally independent from that marker.
        "marked_for_deletion": False,
    }
    try:
        path = exact_source_image_path(attachment)
        digest = hashlib.sha512()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except (AdapterError, OSError) as exc:
        reason = (
            exc.details.get("reason_code")
            if isinstance(exc, AdapterError)
            else "deleted_image_bytes_unreadable"
        )
        raise AdapterError(
            "The deleted image attachment's backing bytes are unavailable",
            code="sync_pending",
            reason_code=str(reason or "deleted_image_bytes_unavailable"),
        ) from exc
    actual = digest.hexdigest()
    if actual.casefold() != expected.casefold():
        raise AdapterError(
            "The deleted image attachment's backing bytes do not match its stored digest",
            code="sync_pending",
            reason_code="deleted_image_bytes_mismatch",
        )
    return actual


def deleted_attachment_digest(
    rows: list[dict[str, Any]],
    *,
    verify_image_bytes: bool = False,
) -> str:
    stable = []
    for row in rows:
        item = {
            "id": row.get("ZCKIDENTIFIER"),
            "type": row.get("Z_ENT"),
            "order": row.get("Z_FOK_REMINDER1"),
            "filename": row.get("ZFILENAME"),
            "sha512": row.get("ZSHA512SUM"),
            "uti": row.get("ZUTI"),
            "file_size": row.get("ZFILESIZE"),
            "width": row.get("ZWIDTH"),
            "height": row.get("ZHEIGHT"),
            "url": row.get("ZURL"),
            "host_url": row.get("ZHOSTURL"),
        }
        if verify_image_bytes and row.get("Z_ENT") == IMAGE_ATTACHMENT_ENT:
            item["actual_sha512"] = deleted_image_byte_sha512(row)
        stable.append(item)
    return stable_hash(stable)


def public_deleted_attachment(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("Z_ENT") == IMAGE_ATTACHMENT_ENT:
        return {
            "id": row.get("ZCKIDENTIFIER"),
            "type": "image",
            "filename": row.get("ZFILENAME"),
            "file_size": row.get("ZFILESIZE"),
            "width": row.get("ZWIDTH"),
            "height": row.get("ZHEIGHT"),
        }
    return {
        "id": row.get("ZCKIDENTIFIER"),
        "type": "url",
        "url": row.get("ZURL"),
    }


def find_deleted_reminder(
    con: sqlite3.Connection,
    reminder_id: str,
) -> dict[str, Any]:
    row = con.execute(
        """
        select * from ZREMCDREMINDER
        where ZCKIDENTIFIER=?
          and coalesce(ZMARKEDFORDELETION,0)=1
          and ZLASTMODIFIEDDATE>=?
        """,
        (normalize_uuid(reminder_id), recently_deleted_cutoff()),
    ).fetchone()
    if not row:
        raise AdapterError(
            "Deleted Reminder was not found within the recoverable 30-day window",
            code="not_found",
            reason_code="deleted_reminder_not_recoverable",
        )
    return dict(row)


def deleted_store_identity(
    db: Path,
    con: sqlite3.Connection,
) -> str:
    capability = require_command_capability(con, "recover_deleted_reminder")
    stat = db.stat()
    return stable_hash(
        {
            "database": str(db.resolve()),
            "inode": stat.st_ino,
            "device": stat.st_dev,
            "schema": capability["schema_fingerprint"],
        }
    )


def deleted_reminder_snapshot(
    con: sqlite3.Connection,
    row: dict[str, Any],
    *,
    attachment_limit: int = 0,
    verify_attachment_bytes: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    attachments = deleted_attachment_rows(con, int(row["Z_PK"]))
    image_count = sum(item.get("Z_ENT") == IMAGE_ATTACHMENT_ENT for item in attachments)
    url_count = sum(item.get("Z_ENT") == URL_ATTACHMENT_ENT for item in attachments)
    deleted_at = core_to_iso(row.get("ZLASTMODIFIEDDATE"))
    expires_at = core_to_iso(
        float(row["ZLASTMODIFIEDDATE"]) + RECENTLY_DELETED_RETENTION_DAYS * 86400
    )
    public: dict[str, Any] = {
        "id": row.get("ZCKIDENTIFIER"),
        "title": row.get("ZTITLE"),
        "completed": bool(row.get("ZCOMPLETED")),
        "priority": row.get("ZPRIORITY"),
        "created_at": core_to_iso(row.get("ZCREATIONDATE")),
        "deleted_at": deleted_at,
        "expires_at": expires_at,
        "account_id": account_identifier(con, row.get("ZACCOUNT")),
        "attachment_count": len(attachments),
        "image_attachment_count": image_count,
        "url_attachment_count": url_count,
    }
    if attachment_limit:
        public["attachments"] = [
            public_deleted_attachment(item) for item in attachments[:attachment_limit]
        ]
        public["attachments_truncated"] = len(attachments) > attachment_limit
    guard = {
        "reminder_id": row.get("ZCKIDENTIFIER"),
        "private_version": row.get("Z_OPT"),
        "deleted_at": deleted_at,
        "attachment_digest": deleted_attachment_digest(
            attachments,
            verify_image_bytes=verify_attachment_bytes,
        ),
        "account_id": public["account_id"],
    }
    return public, guard


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


def require_reminder_version(
    reminder: dict[str, Any],
    expected: int | None,
    *,
    required: bool = False,
) -> None:
    if expected is None:
        if required:
            raise AdapterError(
                "A fresh reminder version is required for this private-store mutation",
                code="invalid_input",
                required_field="if_version",
                current_version=reminder.get("Z_OPT"),
            )
        return
    if reminder.get("Z_OPT") != expected:
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


def image_copy_identity(attachment: dict[str, Any]) -> dict[str, Any]:
    """Return content/identity fields that must not change during a copy."""

    return {
        name: attachment.get(name)
        for name in (
            "id",
            "type",
            "uti",
            "filename",
            "sha512",
            "file_size",
            "width",
            "height",
            "marked_for_deletion",
        )
    }


def image_copy_content_identity(attachment: dict[str, Any]) -> dict[str, Any]:
    """Return byte-identity evidence for cross-Reminder verification.

    SHA-512 proves the copied bytes. ReminderKit may normalize a stale source
    UTI to the type decoded from those same bytes, so UTI equality is not a
    content requirement; the native attachment helper verifies the decoded
    destination UTI independently.
    """

    return {
        name: attachment.get(name)
        for name in ("type", "sha512", "file_size", "width", "height")
    }


def image_attachment_content_hash(attachment: dict[str, Any]) -> str:
    """Hash direct-attachment byte/type evidence without persisting raw fields."""

    digest = attachment.get("sha512")
    return stable_hash(
        {
            "type": attachment.get("type"),
            "uti": attachment.get("uti"),
            "sha512": digest.casefold() if isinstance(digest, str) else digest,
            "file_size": attachment.get("file_size"),
            "width": attachment.get("width"),
            "height": attachment.get("height"),
        }
    )


def exact_source_image_path(attachment: dict[str, Any]) -> Path:
    """Resolve one active image's private backing bytes without exposing their path.

    Reminders can retain the same content-addressed file under more than one
    extension or account attachment directory. Multiple candidates are safe to
    collapse only when every regular in-container file matches the attachment's
    stored SHA-512 digest; byte-divergent candidates remain ambiguous.
    """

    if attachment.get("type") != "image" or attachment.get("marked_for_deletion") is True:
        raise AdapterError(
            "The selected source attachment is not an active image",
            code="invalid_input",
            reason_code="source_attachment_not_active_image",
        )
    paths = source_paths_for_attachment(attachment)
    if not paths:
        raise AdapterError(
            "The selected source image bytes are unavailable",
            code="invalid_input",
            reason_code="source_image_bytes_missing",
        )
    resolved_paths: list[Path] = []
    try:
        files_root = FILES.resolve(strict=True)
    except OSError as exc:
        raise AdapterError(
            "The Reminders attachment container is unavailable",
            code="invalid_input",
            reason_code="source_image_container_unavailable",
        ) from exc
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise AdapterError(
                "The selected source image is not a stable regular file",
                code="invalid_input",
                reason_code="source_image_file_not_regular",
            )
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(files_root)
        except (OSError, ValueError) as exc:
            raise AdapterError(
                "The selected source image is outside the Reminders file container",
                code="invalid_input",
                reason_code="source_image_path_outside_container",
            ) from exc
        if resolved not in resolved_paths:
            resolved_paths.append(resolved)
    if len(resolved_paths) == 1:
        return resolved_paths[0]

    expected_digest = attachment.get("sha512")
    if not isinstance(expected_digest, str) or not re.fullmatch(
        r"[A-Fa-f0-9]{128}", expected_digest
    ):
        raise AdapterError(
            "Multiple source files require an exact stored image digest",
            code="schema_mismatch",
            reason_code="source_image_digest_unavailable",
        )
    for path in resolved_paths:
        digest = hashlib.sha512()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise AdapterError(
                "A selected source image file became unreadable",
                code="concurrent_modification",
                reason_code="source_image_file_changed",
            ) from exc
        if digest.hexdigest().casefold() != expected_digest.casefold():
            raise AdapterError(
                "The selected source image files do not share the stored content digest",
                code="ambiguous_target",
                reason_code="source_image_files_diverge",
                matching_file_count=len(resolved_paths),
            )
    return min(resolved_paths, key=lambda path: str(path))


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
        proc = run_bounded_process(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            timeout_s=SUBPROCESS_TIMEOUT_SECONDS,
            stdout_limit=IMAGE_METADATA_OUTPUT_LIMIT_BYTES,
            stderr_limit=IMAGE_METADATA_OUTPUT_LIMIT_BYTES,
            output="utf8",
        )
    except ProcessTimeoutError as exc:
        raise AdapterError("Image metadata inspection timed out", image=path.name) from exc
    except ProcessLaunchError as exc:
        raise AdapterError("Image metadata inspection could not start", image=path.name) from exc
    except ProcessError as exc:
        raise AdapterError("Image metadata inspection returned invalid output", image=path.name) from exc
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
    clang = require_private_helper_compiler()
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
        temp_handle = tempfile.NamedTemporaryFile(
            prefix=".remkit_attach_image.",
            dir=CACHE_DIR,
            delete=False,
        )
        temp_path = Path(temp_handle.name)
        temp_handle.close()
        try:
            try:
                proc = run_bounded_process(
                    [
                        clang,
                        "-x",
                        "objective-c",
                        "-fobjc-arc",
                        "-framework",
                        "Foundation",
                        "-framework",
                        "AppKit",
                        "-framework",
                        "ImageIO",
                        "-o",
                        str(temp_path),
                        str(source),
                    ],
                    timeout_s=SUBPROCESS_TIMEOUT_SECONDS,
                    stdout_limit=BUILDER_STDOUT_LIMIT_BYTES,
                    stderr_limit=BUILDER_STDERR_LIMIT_BYTES,
                    output="utf8",
                )
            except ProcessTimeoutError as exc:
                raise AdapterError("ReminderKit helper build timed out") from exc
            except ProcessLaunchError as exc:
                raise AdapterError("ReminderKit helper build could not start") from exc
            except ProcessError as exc:
                raise AdapterError("ReminderKit helper build returned invalid output") from exc
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


def reminderkit_sections_helper() -> Path:
    source = Path(__file__).resolve().with_name("remkit_sections.m")
    if not source.exists():
        raise AdapterError(f"ReminderKit section helper source not found: {source.name}")
    clang = require_private_helper_compiler()
    helper = CACHE_DIR / "remkit_sections"
    ensure_private_dir(CACHE_DIR)
    lock_path = CACHE_DIR / "remkit_sections.lock"
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
        temp_handle = tempfile.NamedTemporaryFile(
            prefix=".remkit_sections.",
            dir=CACHE_DIR,
            delete=False,
        )
        temp_path = Path(temp_handle.name)
        temp_handle.close()
        try:
            try:
                proc = run_bounded_process(
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
                    timeout_s=SUBPROCESS_TIMEOUT_SECONDS,
                    stdout_limit=BUILDER_STDOUT_LIMIT_BYTES,
                    stderr_limit=BUILDER_STDERR_LIMIT_BYTES,
                    output="utf8",
                )
            except ProcessTimeoutError as exc:
                raise AdapterError("ReminderKit section helper build timed out") from exc
            except ProcessLaunchError as exc:
                raise AdapterError("ReminderKit section helper build could not start") from exc
            except ProcessError as exc:
                raise AdapterError("ReminderKit section helper build returned invalid output") from exc
            if proc.returncode != 0:
                raise AdapterError(
                    (proc.stderr or proc.stdout).strip()
                    or "Failed to build ReminderKit section helper"
                )
            temp_path.chmod(0o700)
            os.replace(temp_path, helper)
        finally:
            temp_path.unlink(missing_ok=True)
    return helper


def reminderkit_recover_helper() -> Path:
    source = Path(__file__).resolve().with_name("remkit_recover.m")
    if not source.exists():
        raise AdapterError(f"ReminderKit recovery helper source not found: {source.name}")
    clang = require_private_helper_compiler()
    helper = CACHE_DIR / "remkit_recover"
    ensure_private_dir(CACHE_DIR)
    lock_path = CACHE_DIR / "remkit_recover.lock"
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
        temp_handle = tempfile.NamedTemporaryFile(
            prefix=".remkit_recover.", dir=CACHE_DIR, delete=False
        )
        temp_path = Path(temp_handle.name)
        temp_handle.close()
        try:
            try:
                proc = run_bounded_process(
                    [
                        clang,
                        "-x",
                        "objective-c",
                        "-fobjc-arc",
                        "-framework",
                        "Foundation",
                        "-o",
                        str(temp_path),
                        str(source),
                    ],
                    timeout_s=SUBPROCESS_TIMEOUT_SECONDS,
                    stdout_limit=BUILDER_STDOUT_LIMIT_BYTES,
                    stderr_limit=BUILDER_STDERR_LIMIT_BYTES,
                    output="utf8",
                )
            except ProcessTimeoutError as exc:
                raise AdapterError("ReminderKit recovery helper build timed out") from exc
            except ProcessLaunchError as exc:
                raise AdapterError("ReminderKit recovery helper build could not start") from exc
            except ProcessError as exc:
                raise AdapterError("ReminderKit recovery helper build returned invalid output") from exc
            if proc.returncode != 0:
                raise AdapterError(
                    (proc.stderr or proc.stdout).strip()
                    or "Failed to build ReminderKit recovery helper"
                )
            temp_path.chmod(0o700)
            os.replace(temp_path, helper)
        finally:
            temp_path.unlink(missing_ok=True)
    return helper


def _prepare_helper_before_mutation(
    builder: Callable[[], Path],
    *,
    label: str,
    reason_code: str,
) -> Path:
    """Resolve/build a helper while no target mutation can have started."""

    try:
        return builder()
    except MutationNotStartedError:
        raise
    except Exception as exc:
        raise MutationNotStartedError(
            f"{label} could not be prepared",
            code="unexpected_error",
            reason_code=reason_code,
        ) from exc


def invoke_reminderkit_recovery_guard(reminder_id: str) -> str:
    helper = reminderkit_recover_helper()
    try:
        proc = run_bounded_process(
            [str(helper), "guard", normalize_uuid(reminder_id)],
            timeout_s=SUBPROCESS_TIMEOUT_SECONDS,
            stdout_limit=NATIVE_STDOUT_LIMIT_BYTES,
            stderr_limit=NATIVE_STDERR_LIMIT_BYTES,
            output="utf8",
        )
    except ProcessTimeoutError as exc:
        raise AdapterError(
            "ReminderKit recovery guard read timed out",
            code="sync_pending",
            reason_code="native_recovery_guard_timeout",
        ) from exc
    except ProcessLaunchError as exc:
        raise AdapterError(
            "ReminderKit recovery guard could not start",
            code="unexpected_error",
            reason_code="native_recovery_guard_launch_failed",
        ) from exc
    except ProcessError as exc:
        raise AdapterError(
            "ReminderKit recovery guard returned invalid output",
            code="unexpected_error",
            reason_code="invalid_native_recovery_guard",
        ) from exc
    raw = (proc.stdout or "").strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise AdapterError(
            "ReminderKit recovery guard returned invalid JSON",
            code="unexpected_error",
            reason_code="invalid_native_recovery_guard",
        ) from exc
    digest = payload.get("native_guard_digest")
    if (
        proc.returncode != 0
        or payload.get("ok") is not True
        or payload.get("operation") != "read_recovery_guard"
        or payload.get("reminder_id") != normalize_uuid(reminder_id)
        or payload.get("mutation_attempted") is not False
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        reason = str(payload.get("error") or "invalid_native_recovery_guard")
        detail = payload.get("detail") or proc.stderr.strip()
        stable_code = (
            "not_found"
            if reason == "deleted_reminder_not_found"
            else "unsupported_capability"
            if reason
            in {
                "native_guard_unavailable",
                "required_reminderkit_classes_missing",
                "required_reminderkit_selectors_missing",
            }
            else "unexpected_error"
        )
        raise AdapterError(
            f"{reason}: {detail}" if detail else reason,
            code=stable_code,
            reason_code=reason,
        )
    return digest


def invoke_reminderkit_recovery(
    reminder_id: str,
    destination_list_id: str,
    native_guard_digest: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", native_guard_digest):
        raise AdapterError(
            "A valid native recovery guard is required",
            code="invalid_input",
            reason_code="invalid_native_recovery_guard",
        )
    helper = _prepare_helper_before_mutation(
        reminderkit_recover_helper,
        label="ReminderKit recovery helper",
        reason_code="native_recovery_helper_build_failed",
    )
    try:
        proc = run_bounded_process(
            [
                str(helper),
                "recover",
                normalize_uuid(reminder_id),
                normalize_uuid(destination_list_id),
                native_guard_digest,
            ],
            timeout_s=SUBPROCESS_TIMEOUT_SECONDS,
            stdout_limit=NATIVE_STDOUT_LIMIT_BYTES,
            stderr_limit=NATIVE_STDERR_LIMIT_BYTES,
            output="utf8",
        )
    except ProcessLaunchError as exc:
        raise MutationNotStartedError(
            "ReminderKit recovery could not start",
            code="unexpected_error",
            reason_code="native_recovery_launch_failed",
        ) from exc
    except ProcessTimeoutError as exc:
        raise AdapterError(
            "ReminderKit recovery timed out after dispatch",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
        ) from exc
    except ProcessError as exc:
        raise AdapterError(
            "ReminderKit recovery returned invalid output after dispatch",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
            reason_code="invalid_native_recovery_output",
        ) from exc
    raw = (proc.stdout or "").strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise AdapterError(
            "ReminderKit recovery returned invalid JSON after dispatch",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
            helper_output_present=bool(raw or proc.stderr.strip()),
        ) from exc
    if proc.returncode != 0 or payload.get("ok") is not True:
        # A clean helper failure always emits its pre/post-dispatch marker.  No
        # output means the helper died before it could report, so conservatively
        # preserve a possible write instead of inventing failed_no_mutation.
        # Only an explicit false marker is proof that the helper never saved.
        # Missing or malformed provenance remains a possible write.
        mutation_attempted = payload.get("mutation_attempted") is not False
        reason = str(payload.get("error") or "reminderkit_recovery_failed")
        detail = payload.get("detail") or proc.stderr.strip()
        message = f"{reason}: {detail}" if detail else reason
        if reason == "concurrent_modification":
            stable_code = "concurrent_modification"
        elif reason in {
            "native_guard_unavailable",
            "required_reminderkit_classes_missing",
            "required_reminderkit_selectors_missing",
            "undelete_selector_missing",
            "recently_deleted_not_supported",
            "cross_account_restore_not_supported",
        }:
            stable_code = "unsupported_capability"
        elif reason == "deleted_reminder_not_found":
            stable_code = "not_found"
        elif reason in {"invalid_arguments", "usage"}:
            stable_code = "invalid_input"
        elif mutation_attempted:
            stable_code = "sync_pending"
        else:
            stable_code = "unexpected_error"
        raise AdapterError(
            message,
            code=stable_code,
            partial_failure=mutation_attempted,
            mutation_outcome_unknown=mutation_attempted,
            reason_code=reason,
        )
    if (
        payload.get("reminder_id") != normalize_uuid(reminder_id)
        or payload.get("destination_list_id") != normalize_uuid(destination_list_id)
        or payload.get("mutation_attempted") is not True
        or payload.get("saved") is not True
        or payload.get("pre_save_guard_matched") is not True
    ):
        raise AdapterError(
            "ReminderKit recovery returned mismatched read-back identity",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
        )
    return payload


def invoke_reminderkit_section(operation: str, *arguments: str) -> dict[str, Any]:
    helper = _prepare_helper_before_mutation(
        reminderkit_sections_helper,
        label="ReminderKit section helper",
        reason_code="native_section_helper_build_failed",
    )
    try:
        proc = run_bounded_process(
            [str(helper), operation, *arguments],
            timeout_s=SUBPROCESS_TIMEOUT_SECONDS,
            stdout_limit=NATIVE_STDOUT_LIMIT_BYTES,
            stderr_limit=NATIVE_STDERR_LIMIT_BYTES,
            output="utf8",
        )
    except ProcessLaunchError as exc:
        raise MutationNotStartedError(
            "ReminderKit section operation could not start",
            code="unexpected_error",
            reason_code="native_section_launch_failed",
            operation=operation,
        ) from exc
    except ProcessTimeoutError as exc:
        raise AdapterError(
            "ReminderKit section operation timed out",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
            operation=operation,
        ) from exc
    except ProcessError as exc:
        raise AdapterError(
            "ReminderKit section operation returned invalid output after dispatch",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
            reason_code="invalid_native_section_output",
            operation=operation,
        ) from exc
    raw = (proc.stdout or "").strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise AdapterError(
            "ReminderKit section helper returned invalid JSON",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
            helper_output_present=bool(raw or proc.stderr.strip()),
            operation=operation,
        ) from exc
    if proc.returncode != 0 or not payload.get("ok"):
        detail = payload.get("detail") or proc.stderr.strip()
        message = payload.get("error") or "ReminderKit section operation failed"
        if detail:
            message = f"{message}: {detail}"
        # Only an explicit false marker can prove that this launched helper did
        # not attempt its target mutation. Missing or malformed provenance is
        # an unknown post-launch outcome.
        mutation_attempted = payload.get("mutation_attempted") is not False
        raise AdapterError(
            message,
            code="sync_pending" if mutation_attempted else "unexpected_error",
            partial_failure=mutation_attempted,
            mutation_outcome_unknown=mutation_attempted,
            operation=operation,
        )
    if (
        payload.get("operation") != operation
        or payload.get("mutation_attempted") is not True
        or payload.get("saved") is not True
    ):
        raise AdapterError(
            "ReminderKit section operation returned incomplete mutation proof",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
            reason_code="invalid_native_section_receipt",
            operation=operation,
        )
    return payload


def attach_image_reminderkit_record(
    con: sqlite3.Connection,
    reminder: dict[str, Any],
    image: Path,
    *,
    validated_image: ValidatedImage | None = None,
) -> dict[str, Any]:
    if not image.exists():
        raise AdapterError(f"Image not found: {image.name}")
    if validated_image is None:
        try:
            validated_image = validate_image_input(image)
        except ImageInputError as exc:
            raise AdapterError(
                str(exc),
                code="invalid_input",
                reason_code=exc.reason_code,
            ) from exc
    before_rows = active_attachment_rows(
        con,
        reminder["Z_PK"],
        attachment_ent=IMAGE_ATTACHMENT_ENT,
    )
    before_ids = {row["ZCKIDENTIFIER"] for row in before_rows}
    helper = _prepare_helper_before_mutation(
        reminderkit_attach_helper,
        label="ReminderKit image attachment helper",
        reason_code="native_image_helper_build_failed",
    )
    ensure_private_dir(CACHE_DIR)
    suffix = ".png" if validated_image.format == "png" else ".jpg"
    with tempfile.TemporaryDirectory(prefix="attach-image.", dir=CACHE_DIR) as temp_dir:
        snapshot_path = Path(temp_dir) / f"input{suffix}"
        shutil.copyfile(validated_image.path, snapshot_path)
        snapshot_path.chmod(0o600)
        try:
            snapshot_bytes = snapshot_path.read_bytes()
        except OSError as exc:
            raise AdapterError(
                "The validated image snapshot could not be read",
                code="invalid_input",
                reason_code="image_snapshot_unreadable",
            ) from exc
        snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
        if (
            snapshot_sha256 != validated_image.sha256
            or len(snapshot_bytes) != validated_image.bytes
        ):
            raise AdapterError(
                "The image changed before native attachment dispatch",
                code="concurrent_modification",
                reason_code="image_changed_before_dispatch",
            )
        expected_sha512 = hashlib.sha512(snapshot_bytes).hexdigest()
        expected_uti = (
            "public.png" if validated_image.format == "png" else "public.jpeg"
        )
        try:
            proc = run_bounded_process(
                [str(helper), reminder["ZCKIDENTIFIER"], str(snapshot_path)],
                timeout_s=SUBPROCESS_TIMEOUT_SECONDS,
                stdout_limit=NATIVE_STDOUT_LIMIT_BYTES,
                stderr_limit=NATIVE_STDERR_LIMIT_BYTES,
                output="utf8",
            )
        except ProcessLaunchError as exc:
            raise MutationNotStartedError(
                "ReminderKit image attachment could not start",
                code="unexpected_error",
                reason_code="native_image_attachment_launch_failed",
                image=image.name,
            ) from exc
        except ProcessTimeoutError as exc:
            raise AdapterError(
                "ReminderKit image attachment timed out",
                code="sync_pending",
                image=image.name,
                partial_failure=True,
                mutation_outcome_unknown=True,
            ) from exc
        except ProcessError as exc:
            raise AdapterError(
                "ReminderKit image attachment returned invalid output after dispatch",
                code="sync_pending",
                image=image.name,
                partial_failure=True,
                mutation_outcome_unknown=True,
                reason_code="invalid_native_image_attachment_output",
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
        fresh = connect_read_only(db_path)
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
    attachment_transport = payload.get("attachment_transport")
    if attachment_transport != "data":
        raise AttachmentVerificationError(
            "Image attachment helper did not use the native image-data transport",
            row=selected,
            reason_code="native_image_transport_mismatch",
            retryable=False,
            partial_failure=True,
            attachment=attachment,
            attachment_transport=(
                attachment_transport
                if isinstance(attachment_transport, str)
                else "missing"
            ),
            cleanup_command=(
                "delete_attachment "
                f"--id {reminder['ZCKIDENTIFIER']} "
                f"--attachment-id {attachment['id']}"
            ),
        )
    helper_image_uti = payload.get("image_uti")
    if (
        helper_image_uti != expected_uti
        or attachment.get("uti") != expected_uti
    ):
        raise AttachmentVerificationError(
            "Image attachment content type did not survive native read-back",
            row=selected,
            reason_code="native_image_content_type_mismatch",
            retryable=False,
            partial_failure=True,
            attachment=attachment,
            helper_image_uti=(
                helper_image_uti
                if isinstance(helper_image_uti, str)
                else "missing"
            ),
            stored_image_uti=attachment.get("uti"),
            cleanup_command=(
                "delete_attachment "
                f"--id {reminder['ZCKIDENTIFIER']} "
                f"--attachment-id {attachment['id']}"
            ),
        )
    if (
        not isinstance(attachment.get("sha512"), str)
        or attachment["sha512"].casefold() != expected_sha512.casefold()
        or attachment.get("file_size") != validated_image.bytes
        or attachment.get("width") != validated_image.width
        or attachment.get("height") != validated_image.height
    ):
        raise AttachmentVerificationError(
            "Image attachment bytes or decoded dimensions did not match the dispatched snapshot",
            row=selected,
            reason_code="native_image_content_mismatch",
            retryable=False,
            partial_failure=True,
            attachment=attachment,
            cleanup_command=(
                "delete_attachment "
                f"--id {reminder['ZCKIDENTIFIER']} "
                f"--attachment-id {attachment['id']}"
            ),
        )
    if attachment["sync"].get("mobile_visible_likely") is not True:
        raise AttachmentVerificationError(
            "Image attachment was created but mobile visibility could not be verified",
            row=selected,
            reason_code="mobile_visibility_pending",
            retryable=True,
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


def remove_image_reminderkit_record(
    db_path: Path,
    reminder: dict[str, Any],
    attachment: dict[str, Any],
) -> dict[str, Any]:
    if int(attachment.get("Z_ENT", -1)) != IMAGE_ATTACHMENT_ENT:
        raise AdapterError(
            "ReminderKit image removal requires an exact image attachment",
            code="invalid_input",
        )
    try:
        reminder_id = normalize_uuid(str(reminder.get("ZCKIDENTIFIER") or ""))
        attachment_id = normalize_uuid(str(attachment.get("ZCKIDENTIFIER") or ""))
        attachment_pk = int(attachment["Z_PK"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AdapterError(
            "ReminderKit image removal requires exact reminder and attachment ids",
            code="invalid_input",
        ) from exc
    before = attachment_payload(attachment)
    cloud_state_pk = attachment.get("ZCKCLOUDSTATE")
    db_path = Path(db_path).expanduser().resolve()
    helper = _prepare_helper_before_mutation(
        reminderkit_attach_helper,
        label="ReminderKit image removal helper",
        reason_code="native_image_helper_build_failed",
    )
    try:
        proc = run_bounded_process(
            [str(helper), "remove", reminder_id, attachment_id],
            timeout_s=SUBPROCESS_TIMEOUT_SECONDS,
            stdout_limit=NATIVE_STDOUT_LIMIT_BYTES,
            stderr_limit=NATIVE_STDERR_LIMIT_BYTES,
            output="utf8",
        )
    except ProcessLaunchError as exc:
        raise MutationNotStartedError(
            "ReminderKit image removal could not start",
            code="unexpected_error",
            reason_code="native_image_removal_launch_failed",
        ) from exc
    except ProcessTimeoutError as exc:
        raise AdapterError(
            "ReminderKit image removal timed out",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
        ) from exc
    except ProcessError as exc:
        raise AdapterError(
            "ReminderKit image removal returned invalid output after dispatch",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
            reason_code="invalid_native_image_removal_output",
        ) from exc
    raw = (proc.stdout or "").strip()
    if not raw:
        raise AdapterError(
            "ReminderKit image removal returned no result",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
            helper_returncode=proc.returncode,
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdapterError(
            "ReminderKit image removal returned invalid JSON",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
            helper_output_present=bool(raw or proc.stderr.strip()),
        ) from exc
    if not isinstance(payload, dict):
        raise AdapterError(
            "ReminderKit image removal returned a non-object result",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
        )
    if proc.returncode != 0 or payload.get("ok") is not True:
        # A helper failure may be emitted after save by a fallback path. Only
        # an explicit false marker proves that removal never started.
        mutation_attempted = payload.get("mutation_attempted") is not False
        detail = payload.get("detail") or proc.stderr.strip()
        message = payload.get("error") or "ReminderKit image removal failed"
        if detail:
            message = f"{message}: {detail}"
        raise AdapterError(
            message,
            code="sync_pending" if mutation_attempted else "unexpected_error",
            partial_failure=mutation_attempted,
            mutation_outcome_unknown=mutation_attempted,
        )
    try:
        reported_reminder_id = normalize_uuid(
            str(payload.get("reminder_id") or "")
        )
        reported_attachment_id = normalize_uuid(
            str(payload.get("attachment_id") or "")
        )
    except (TypeError, ValueError):
        reported_reminder_id = None
        reported_attachment_id = None
    if (
        payload.get("operation") != "remove_attachment"
        or reported_reminder_id != reminder_id
        or reported_attachment_id != attachment_id
    ):
        raise AdapterError(
            "ReminderKit image removal returned mismatched read-back identity",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
        )

    row_deleted = False
    exact_row_identity = False
    detached_from_reminder = False
    cloud_state_tombstone_retained: bool | None = None
    cloud_state_verified = cloud_state_pk is None
    try:
        if REMINDERKIT_REMOVAL_SETTLE_SECONDS > 0:
            time.sleep(REMINDERKIT_REMOVAL_SETTLE_SECONDS)
        deadline = time.monotonic() + REMINDERKIT_REMOVAL_VERIFY_TIMEOUT_SECONDS
        while True:
            fresh = connect_read_only(db_path)
            try:
                remaining = fresh.execute(
                    """
                    select Z_PK,Z_ENT,ZCKIDENTIFIER,ZCKCLOUDSTATE,ZREMINDER2
                    from ZREMCDOBJECT where Z_PK=?
                    """,
                    (attachment_pk,),
                ).fetchone()
                linked_duplicate = fresh.execute(
                    """
                    select Z_PK from ZREMCDOBJECT
                    where ZCKIDENTIFIER=? and ZREMINDER2=?
                    limit 1
                    """,
                    (attachment_id, reminder["Z_PK"]),
                ).fetchone()
                cloud_state = (
                    fresh.execute(
                        """
                        select Z_PK,ZOBJECT,Z13_OBJECT
                        from ZREMCKCLOUDSTATE where Z_PK=?
                        """,
                        (cloud_state_pk,),
                    ).fetchone()
                    if cloud_state_pk is not None
                    else None
                )
            finally:
                fresh.close()
            row_deleted = remaining is None
            if row_deleted:
                exact_row_identity = True
            else:
                try:
                    stored_attachment_id = normalize_uuid(
                        str(remaining["ZCKIDENTIFIER"] or "")
                    )
                except (TypeError, ValueError):
                    stored_attachment_id = None
                exact_row_identity = (
                    remaining["Z_PK"] == attachment_pk
                    and remaining["Z_ENT"] == IMAGE_ATTACHMENT_ENT
                    and stored_attachment_id == attachment_id
                    and remaining["ZCKCLOUDSTATE"] == cloud_state_pk
                )
            detached_from_reminder = linked_duplicate is None
            if cloud_state_pk is None:
                cloud_state_tombstone_retained = None
                cloud_state_verified = True
            else:
                cloud_state_tombstone_retained = bool(
                    cloud_state
                    and cloud_state["ZOBJECT"] == attachment_pk
                    and cloud_state["Z13_OBJECT"] == IMAGE_ATTACHMENT_ENT
                )
                cloud_state_verified = cloud_state_tombstone_retained
            if exact_row_identity and detached_from_reminder and cloud_state_verified:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
    except Exception as exc:
        raise AdapterError(
            "ReminderKit image removal committed but native read-back failed",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
        ) from exc
    if not exact_row_identity or not detached_from_reminder or not cloud_state_verified:
        raise AdapterError(
            "ReminderKit image removal committed but native read-back was inconclusive",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
            row_deleted=row_deleted,
            exact_row_identity=exact_row_identity,
            detached_from_reminder=detached_from_reminder,
            cloud_state_tombstone_retained=cloud_state_tombstone_retained,
        )
    return {
        **before,
        "row_deleted": row_deleted,
        "detached_from_reminder": True,
        "cloud_state_tombstone_retained": cloud_state_tombstone_retained,
        "native_reminderkit": True,
        "helper": payload,
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


def update_primary_key(con: sqlite3.Connection, ent: int, new_max: int) -> None:
    con.execute("update Z_PRIMARYKEY set Z_MAX=max(Z_MAX, ?) where Z_ENT=?", (new_max, ent))


def bump_cloud_state(con: sqlite3.Connection, cloud_pk: int | None, now: float) -> bool:
    if cloud_pk is None:
        return False
    result = con.execute(
        """
        update ZREMCKCLOUDSTATE
        set Z_OPT=coalesce(Z_OPT,0)+1,
            ZCURRENTLOCALVERSION=coalesce(ZCURRENTLOCALVERSION,0)+1,
            ZLOCALVERSIONDATE=?
        where Z_PK=?
        """,
        (now, cloud_pk),
    )
    return result.rowcount == 1


def cloud_sync_evidence(
    con: sqlite3.Connection,
    *,
    table: str,
    identifier: str,
) -> dict[str, Any]:
    if table not in {"ZREMCDBASELIST", "ZREMCDBASESECTION"}:
        raise ValueError(f"Unsupported CloudKit owner table: {table}")
    row = con.execute(
        f"""
        select cs.ZCURRENTLOCALVERSION as current_local_version,
               cs.ZLATESTVERSIONSYNCEDTOCLOUD as latest_synced_version,
               cs.ZINCLOUD as in_cloud
        from {table} owner
        left join ZREMCKCLOUDSTATE cs on cs.Z_PK=owner.ZCKCLOUDSTATE
        where upper(owner.ZCKIDENTIFIER)=?
        """,
        (normalize_uuid(identifier),),
    ).fetchone()
    if not row:
        return {
            "object_found": False,
            "in_cloud": None,
            "current_local_version": None,
            "latest_synced_version": None,
            "icloud_sync_verified": False,
        }
    current = row["current_local_version"]
    synced = row["latest_synced_version"]
    in_cloud = row["in_cloud"]
    verified = (
        in_cloud == 1
        and current is not None
        and synced is not None
        and int(synced) >= int(current)
    )
    return {
        "object_found": True,
        "in_cloud": in_cloud,
        "current_local_version": current,
        "latest_synced_version": synced,
        "icloud_sync_verified": verified,
    }


def wait_for_cloud_sync(
    db: Path,
    *,
    table: str,
    identifier: str,
    timeout: float = SECTION_SYNC_VERIFY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    evidence: dict[str, Any] = {}
    while True:
        fresh = connect(db)
        try:
            evidence = cloud_sync_evidence(
                fresh,
                table=table,
                identifier=identifier,
            )
        finally:
            fresh.close()
        if evidence.get("icloud_sync_verified") is True or time.time() >= deadline:
            return evidence
        time.sleep(0.25)


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


def cmd_list_sections(args: argparse.Namespace) -> int:
    db = resolve_database(args.db)
    con = connect(db)
    try:
        params: list[Any] = [args.list_id]
        where = [
            "coalesce(s.ZMARKEDFORDELETION,0)=0",
            "l.ZCKIDENTIFIER=?",
        ]
        rows = con.execute(
            f"""
            select s.Z_PK,s.ZCKIDENTIFIER,s.ZDISPLAYNAME,s.ZLIST,
                   l.ZCKIDENTIFIER as list_id,l.ZNAME as list_name,s.Z_FOK_LIST
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


def cmd_list_deleted_reminders(args: argparse.Namespace) -> int:
    if args.limit > 200:
        raise AdapterError("Deleted Reminder reads are limited to 200 items")
    if args.offset > RECENTLY_DELETED_SNAPSHOT_LIMIT:
        raise AdapterError("Deleted Reminder cursor offset is out of range")
    db = resolve_database(args.db)
    con = connect_read_only(db)
    try:
        require_command_capability(con, "recover_deleted_reminder")
        # Python's sqlite3 driver does not open a transaction for SELECTs. Pin
        # both the bounded identity snapshot and the page hydration query to
        # one read transaction so the emitted rows cannot drift from the
        # fingerprint between statements.
        con.execute("begin")
        where = [
            "coalesce(r.ZMARKEDFORDELETION,0)=1",
            "r.ZLASTMODIFIEDDATE>=?",
        ]
        params: list[Any] = [recently_deleted_cutoff()]
        if args.account_id:
            where.append("a.ZCKIDENTIFIER=?")
            params.append(normalize_uuid(args.account_id))
        snapshot_rows = con.execute(
            f"""
            select r.Z_PK,r.ZCKIDENTIFIER,r.Z_OPT,r.ZLASTMODIFIEDDATE
            from ZREMCDREMINDER r
            left join ZREMCDOBJECT a on a.Z_PK=r.ZACCOUNT and a.Z_ENT=14
            where {" and ".join(where)}
            order by r.ZLASTMODIFIEDDATE desc,r.Z_PK desc
            limit ?
            """,
            [*params, RECENTLY_DELETED_SNAPSHOT_LIMIT + 1],
        ).fetchall()
        if len(snapshot_rows) > RECENTLY_DELETED_SNAPSHOT_LIMIT:
            raise AdapterError(
                "Recently Deleted is too large for one safe ordered snapshot",
                code="ambiguous_scope",
                reason_code="deleted_snapshot_too_large",
            )
        snapshot_fingerprint = stable_hash(
            [
                {
                    "id": row["ZCKIDENTIFIER"],
                    "version": row["Z_OPT"],
                    "modified": row["ZLASTMODIFIEDDATE"],
                    "pk": row["Z_PK"],
                }
                for row in snapshot_rows
            ]
        )
        page_snapshot = snapshot_rows[args.offset : args.offset + args.limit]
        page_pks = [int(row["Z_PK"]) for row in page_snapshot]
        if page_pks:
            placeholders = ",".join("?" for _ in page_pks)
            page_rows = con.execute(
                f"select r.* from ZREMCDREMINDER r where r.Z_PK in ({placeholders})",
                page_pks,
            ).fetchall()
            rows_by_pk = {int(row["Z_PK"]): row for row in page_rows}
            if set(rows_by_pk) != set(page_pks):
                raise AdapterError(
                    "Recently Deleted changed while reading the requested page",
                    code="concurrent_modification",
                    reason_code="pagination_snapshot_stale",
                )
            snapshot_by_pk = {int(row["Z_PK"]): row for row in page_snapshot}
            for pk, row in rows_by_pk.items():
                expected = snapshot_by_pk[pk]
                if (
                    row["ZCKIDENTIFIER"],
                    row["Z_OPT"],
                    row["ZLASTMODIFIEDDATE"],
                ) != (
                    expected["ZCKIDENTIFIER"],
                    expected["Z_OPT"],
                    expected["ZLASTMODIFIEDDATE"],
                ):
                    raise AdapterError(
                        "Recently Deleted changed while reading the requested page",
                        code="concurrent_modification",
                        reason_code="pagination_snapshot_stale",
                    )
            page = [rows_by_pk[pk] for pk in page_pks]
        else:
            page = []
        items = [
            deleted_reminder_snapshot(con, dict(row))[0]
            for row in page
        ]
        next_offset = args.offset + len(items)
        has_more = next_offset < len(snapshot_rows)
        json_out(
            {
                "ok": True,
                "deleted_reminders": items,
                "returned": len(items),
                "limit": args.limit,
                "offset": args.offset,
                "total_matched": len(snapshot_rows),
                "has_more": has_more,
                "next_offset": next_offset if has_more else None,
                "truncated": has_more,
                "snapshot_fingerprint": snapshot_fingerprint,
                "retention_days": RECENTLY_DELETED_RETENTION_DAYS,
            }
        )
        return 0
    finally:
        con.close()


def cmd_read_deleted_reminder(args: argparse.Namespace) -> int:
    db = resolve_database(args.db)
    con = connect_read_only(db)
    try:
        row = find_deleted_reminder(con, args.id)
        public, guard = deleted_reminder_snapshot(
            con,
            row,
            attachment_limit=args.attachment_limit,
            verify_attachment_bytes=True,
        )
        guard["store_identity"] = deleted_store_identity(db, con)
        guard["native_guard_digest"] = invoke_reminderkit_recovery_guard(args.id)
        json_out({"ok": True, "deleted_reminder": public, "guard": guard})
        return 0
    finally:
        con.close()


def recovery_post_write_pending(
    args: argparse.Namespace,
    before: dict[str, Any],
    *,
    reason_code: str,
    message: str,
) -> dict[str, Any]:
    """Preserve a confirmed recovery save when later verification cannot finish."""

    return operation_receipt(
        status="committed_verification_pending",
        operation="recover_deleted_reminder",
        backend="reminderkit_private",
        target={
            "reminder_id": normalize_uuid(args.id),
            "list_id": normalize_uuid(args.list_id),
        },
        before=before,
        after={},
        verification={
            "state": "pending",
            "write_performed": True,
            "final_read": False,
            "reason_code": reason_code,
        },
        recovery={"semantics": "read_before_retry", "automatic_retry_safe": False},
        warnings=[
            {
                "code": "verification_pending",
                "message": message,
            }
        ],
        error={
            "code": "sync_pending",
            "reason_code": reason_code,
            "message": message,
            "retryable": False,
        },
    )


def recover_deleted_reminder_once(args: argparse.Namespace) -> dict[str, Any]:
    db = resolve_database(args.db)
    con = connect_read_only(db)
    try:
        row = find_deleted_reminder(con, args.id)
        destination = find_list(con, list_id=args.list_id)
        public_before, guard = deleted_reminder_snapshot(
            con,
            row,
            verify_attachment_bytes=True,
        )
        guard["store_identity"] = deleted_store_identity(db, con)
        expected = {
            "store_identity": args.if_store_identity,
            "private_version": args.if_version,
            "deleted_at": args.if_deleted_at,
            "attachment_digest": args.if_attachment_digest,
        }
        mismatched = [key for key, value in expected.items() if guard.get(key) != value]
        if mismatched:
            raise AdapterError(
                "Deleted Reminder changed; inspect it again before recovery",
                code="concurrent_modification",
                mismatched_fields=mismatched,
            )
        if row.get("ZACCOUNT") != destination.get("ZACCOUNT"):
            raise AdapterError(
                "Recovery to another account is not supported",
                code="unsupported_capability",
                reason_code="cross_account_restore_not_supported",
            )
        before = {
            "deleted_reminder": {
                "id": public_before["id"],
                "deleted_at": public_before["deleted_at"],
                "attachment_count": public_before["attachment_count"],
                "attachment_digest": guard["attachment_digest"],
            }
        }
    finally:
        con.close()

    try:
        native = invoke_reminderkit_recovery(
            args.id,
            args.list_id,
            args.if_native_guard_digest,
        )
    except AdapterError as exc:
        if not exc.details.get("partial_failure"):
            raise
        return operation_receipt(
            status="committed_verification_pending",
            operation="recover_deleted_reminder",
            backend="reminderkit_private",
            target={
                "reminder_id": normalize_uuid(args.id),
                "list_id": normalize_uuid(args.list_id),
            },
            before=before,
            after={},
            verification={
                "state": "pending",
                "write_performed": None,
                "final_read": False,
                "reason_code": "native_recovery_outcome_unknown",
            },
            recovery={
                "semantics": "read_before_retry",
                "automatic_retry_safe": False,
            },
            warnings=[
                {
                    "code": "verification_pending",
                    "message": "Recovery may have committed; read the exact Reminder before retrying.",
                }
            ],
            error={
                "code": "sync_pending",
                "reason_code": str(
                    exc.details.get("reason_code") or "native_recovery_outcome_unknown"
                ),
                "message": str(exc),
                "retryable": False,
            },
        )
    try:
        deadline = time.time() + 10
        active: dict[str, Any] | None = None
        final_attachments: list[dict[str, Any]] = []
        while True:
            fresh = connect_read_only(db)
            try:
                active_row = fresh.execute(
                    """
                    select * from ZREMCDREMINDER
                    where ZCKIDENTIFIER=? and coalesce(ZMARKEDFORDELETION,0)=0
                    """,
                    (normalize_uuid(args.id),),
                ).fetchone()
                if active_row:
                    active = dict(active_row)
                    final_attachments = deleted_attachment_rows(
                        fresh,
                        int(active["Z_PK"]),
                    )
            finally:
                fresh.close()
            if active is not None or time.time() >= deadline:
                break
            time.sleep(0.25)

        if active is None:
            return recovery_post_write_pending(
                args,
                before,
                reason_code="active_readback_pending",
                message="Recovery saved, but the active Reminder is not readable yet.",
            )

        list_row = connect_read_only(db)
        try:
            destination = find_list(list_row, list_id=args.list_id)
            list_matches = active.get("ZLIST") == destination.get("Z_PK")
        finally:
            list_row.close()
        attachment_digest = deleted_attachment_digest(
            final_attachments,
            verify_image_bytes=True,
        )
    except Exception:
        return recovery_post_write_pending(
            args,
            before,
            reason_code="recovery_post_write_verification_failed",
            message=(
                "Recovery saved, but its exact destination or attachment integrity "
                "could not be read back. Read the exact Reminder before retrying."
            ),
        )

    attachments_active = all(
        not bool(item.get("ZMARKEDFORDELETION")) for item in final_attachments
    )
    attachments_preserved = attachment_digest == args.if_attachment_digest
    pre_save_guard_matched = native.get("pre_save_guard_matched") is True
    before_attachment_count = before["deleted_reminder"].get("attachment_count")
    native_attachment_count = native.get("attachment_count")
    after_attachment_count = len(final_attachments)
    attachment_counts_match = (
        isinstance(before_attachment_count, int)
        and not isinstance(before_attachment_count, bool)
        and isinstance(native_attachment_count, int)
        and not isinstance(native_attachment_count, bool)
        and before_attachment_count
        == native_attachment_count
        == after_attachment_count
    )
    verified = (
        list_matches
        and pre_save_guard_matched
        and attachments_active
        and attachments_preserved
        and attachment_counts_match
    )
    status = "verified" if verified else "partial_success"
    result = operation_receipt(
        status=status,
        operation="recover_deleted_reminder",
        backend="reminderkit_private",
        target={"reminder_id": normalize_uuid(args.id), "list_id": normalize_uuid(args.list_id)},
        before=before,
        after={
            "reminder": {
                "id": active.get("ZCKIDENTIFIER"),
                "list_id": normalize_uuid(args.list_id) if list_matches else None,
                "attachment_count": after_attachment_count,
                "attachment_digest": attachment_digest,
            }
        },
        verification={
            "state": "read_back",
            "write_performed": True,
            "final_read": True,
            "matched": verified,
            "pre_save_guard_matched": pre_save_guard_matched,
            "destination_list_matched": list_matches,
            "attachments_active": attachments_active,
            "attachments_preserved": attachments_preserved,
            "attachment_bytes_verified": True,
            "attachment_counts_match": attachment_counts_match,
            "before_attachment_count": before_attachment_count,
            "native_attachment_count": native_attachment_count,
            "after_attachment_count": after_attachment_count,
        },
        recovery={
            "semantics": "native_recently_deleted_recovery",
            "automatic_retry_safe": False,
        },
    )
    if not verified:
        result["warnings"] = [
            {
                "code": "partial_recovery_verification",
                "message": "The Reminder was recovered, but exact destination or attachment preservation needs inspection.",
            }
        ]
        result["error"] = {
            "code": "sync_pending",
            "reason_code": "recovery_readback_mismatch",
            "message": "Inspect the recovered Reminder before another mutation.",
            "retryable": False,
        }
    return result


def cmd_recover_deleted_reminder(args: argparse.Namespace) -> int:
    result = execute_idempotent_adapter_command(
        args=args,
        operation="recover_deleted_reminder",
        key=args.idempotency_key,
        input_payload={
            "id": normalize_uuid(args.id),
            "list_id": normalize_uuid(args.list_id),
            "if_store_identity": args.if_store_identity,
            "if_version": args.if_version,
            "if_deleted_at": args.if_deleted_at,
            "if_attachment_digest": args.if_attachment_digest,
            "if_native_guard_digest": args.if_native_guard_digest,
        },
        callback=lambda: recover_deleted_reminder_once(args),
    )
    runtime_capability = getattr(args, "_experimental_capability", None)
    if isinstance(runtime_capability, dict):
        result.setdefault("capability", runtime_capability)
    json_out(result)
    return 0 if result.get("status") in SUCCESS_RECEIPT_STATUSES else 1


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
        if args.account_id:
            where.append("l.ZACCOUNTIDENTIFIER=?")
            params.append(args.account_id)
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
        capability = receipt_capability(
            args, require_command_capability(con, "tag_assignment_db")
        )
        con.execute("begin immediate")
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        require_reminder_version(
            reminder,
            getattr(args, "if_version", None),
            required=True,
        )
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
        capability = receipt_capability(
            args, require_command_capability(con, "tag_assignment_db")
        )
        con.execute("begin immediate")
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        require_reminder_version(
            reminder,
            getattr(args, "if_version", None),
            required=True,
        )
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


def section_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    return {
        "id": item.get("ZCKIDENTIFIER"),
        "name": item.get("ZDISPLAYNAME"),
        "list_id": item.get("LIST_ID") or item.get("list_id"),
        "list": item.get("LIST_NAME") or item.get("list_name"),
        "order": item.get("Z_FOK_LIST"),
    }


def use_native_section_backend(args: argparse.Namespace) -> bool:
    return getattr(args, "db", None) is None


def cmd_create_section_reminderkit(args: argparse.Namespace) -> int:
    operation_id = new_operation_id()
    db = resolve_database(None)
    con = connect(db)
    try:
        capability = receipt_capability(
            args, require_command_capability(con, "create_section_db")
        )
        list_row = find_list(con, name=args.list, list_id=args.list_id)
        existing_row = con.execute(
            """
            select s.*, l.ZCKIDENTIFIER as LIST_ID, l.ZNAME as LIST_NAME
            from ZREMCDBASESECTION s
            join ZREMCDBASELIST l on l.Z_PK=s.ZLIST
            where s.ZLIST=? and s.ZDISPLAYNAME=? and coalesce(s.ZMARKEDFORDELETION,0)=0
            """,
            (list_row["Z_PK"], args.name),
        ).fetchone()
        existing = dict(existing_row) if existing_row else None
        existing_sync = (
            cloud_sync_evidence(
                con,
                table="ZREMCDBASESECTION",
                identifier=existing["ZCKIDENTIFIER"],
            )
            if existing
            else None
        )
    finally:
        con.close()

    if existing and existing_sync and existing_sync.get("icloud_sync_verified") is True:
        payload = section_payload(existing)
        json_out(
            operation_receipt(
                status="unchanged",
                operation="create_section",
                operation_id=operation_id,
                backend="reminderkit_private",
                target={"id": payload["id"], "list_id": list_row["ZCKIDENTIFIER"]},
                after={"section": payload, "created": False},
                verification={
                    "state": "cloud_read_back",
                    "database_row": True,
                    "icloud_sync": "verified",
                    "cloud": existing_sync,
                },
                recovery={"semantics": "not_applicable"},
                capability=capability,
            )
        )
        return 0

    repaired_existing = existing is not None
    if existing:
        section_id = existing["ZCKIDENTIFIER"]
        temporary_name = f"{args.name[:511]}\u2060"
        invoke_reminderkit_section("repair", section_id, temporary_name)
        try:
            helper_payload = invoke_reminderkit_section("repair", section_id, args.name)
        except MutationNotStartedError as exc:
            raise AdapterError(
                "Section repair started, but restoring the requested name could not start",
                code="sync_pending",
                partial_failure=True,
                mutation_outcome_unknown=False,
                reason_code="section_name_restore_not_started",
                section_id=section_id,
                recovery="repair_section_display_name",
            ) from exc
        except AdapterError as exc:
            exc.details.setdefault("section_id", section_id)
            exc.details.setdefault("recovery", "repair_section_display_name")
            raise
    else:
        helper_payload = invoke_reminderkit_section(
            "create",
            list_row["ZCKIDENTIFIER"],
            args.name,
        )
        section_id = normalize_uuid(str(helper_payload.get("section_id") or ""))
        if not section_id:
            raise AdapterError(
                "ReminderKit created a section without returning its identifier",
                code="sync_pending",
                partial_failure=True,
                mutation_outcome_unknown=True,
            )

    sync = wait_for_cloud_sync(
        db,
        table="ZREMCDBASESECTION",
        identifier=section_id,
    )
    fresh = connect(db)
    try:
        created_row = fresh.execute(
            """
            select s.*, l.ZCKIDENTIFIER as LIST_ID, l.ZNAME as LIST_NAME
            from ZREMCDBASESECTION s
            join ZREMCDBASELIST l on l.Z_PK=s.ZLIST
            where upper(s.ZCKIDENTIFIER)=? and s.ZLIST=?
              and s.ZDISPLAYNAME=? and coalesce(s.ZMARKEDFORDELETION,0)=0
            """,
            (section_id, list_row["Z_PK"], args.name),
        ).fetchone()
    finally:
        fresh.close()
    if not created_row:
        raise AdapterError(
            "ReminderKit section save could not be read back",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
            section_id=section_id,
        )

    status = "verified" if sync.get("icloud_sync_verified") is True else "committed_verification_pending"
    created = section_payload(created_row)
    warning = log_action(
        "create_section",
        {
            "operation_id": operation_id,
            "db": str(db),
            "list": list_row["ZNAME"],
            "section": args.name,
            "id": section_id,
            "backend": "reminderkit",
            "repaired_existing": repaired_existing,
        },
    )
    warnings: list[dict[str, Any] | str] = []
    if warning:
        warnings.append(warning)
    if status != "verified":
        warnings.append("The section exists locally, but iCloud upload is still pending.")
    json_out(
        operation_receipt(
            status=status,
            operation="create_section",
            operation_id=operation_id,
            backend="reminderkit_private",
            target={"id": section_id, "list_id": list_row["ZCKIDENTIFIER"]},
            after={
                "section": created,
                "created": not repaired_existing,
                "repaired_existing": repaired_existing,
            },
            verification={
                "state": "cloud_read_back" if status == "verified" else "local_read_back",
                "database_row": True,
                "icloud_sync": "verified" if status == "verified" else "pending",
                "cloud": sync,
                "helper_saved": helper_payload.get("saved") is True,
            },
            recovery={
                "semantics": "remove_section_in_reminders" if not repaired_existing else "not_applicable",
                "automatic_restore_available": False,
            },
            warnings=warnings or None,
            capability=capability,
        )
    )
    return 0


def cmd_create_section(args: argparse.Namespace) -> int:
    if not args.list_id and not args.list:
        raise AdapterError(
            "Use an exact list id or list name",
            code="ambiguous_target",
            required_selector="list_id | list",
        )
    if use_native_section_backend(args):
        return cmd_create_section_reminderkit(args)
    operation_id = new_operation_id()
    db = resolve_database(args.db, write=True)
    con = connect(db)
    try:
        capability = receipt_capability(
            args, require_command_capability(con, "create_section_db")
        )
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
                status="committed_verification_pending",
                operation="create_section",
                operation_id=operation_id,
                backend="sqlite_private",
                target={"id": section_id, "list_id": list_row["ZCKIDENTIFIER"]},
                after={"section": created, "created": True},
                verification={
                    "state": "local_read_back",
                    "database_row": True,
                    "icloud_sync": "not_verified",
                },
                recovery={
                    "semantics": "remove_section_in_reminders",
                    "automatic_restore_available": False,
                },
                warnings=[
                    *([warning] if warning else []),
                    "Direct SQLite section creation is local-only until a native Reminders save repairs it.",
                ],
                capability=capability,
            )
        )
        return 0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def cmd_move_to_section_reminderkit(args: argparse.Namespace) -> int:
    operation_id = new_operation_id()
    db = resolve_database(None)
    con = connect(db)
    try:
        capability = receipt_capability(
            args, require_command_capability(con, "move_to_section_db")
        )
        reminder = find_reminder(
            con,
            reminder_id=args.id,
            title=args.title,
            list_name=args.list,
        )
        require_reminder_version(
            reminder,
            getattr(args, "if_version", None),
            required=True,
        )
        before_reminder = reminder_mutation_snapshot(reminder)
        if not reminder.get("ZLIST"):
            raise AdapterError(
                "Reminder is not assigned to an active list",
                code="ambiguous_target",
            )
        list_row_raw = con.execute(
            "select * from ZREMCDBASELIST where Z_PK=?",
            (reminder["ZLIST"],),
        ).fetchone()
        if not list_row_raw:
            raise AdapterError("Reminder list was not found", code="ambiguous_target")
        list_row = dict(list_row_raw)
        section = find_section(
            con,
            list_pk=list_row["Z_PK"],
            name=args.section,
            section_id=args.section_id,
        )
        mapping = membership_map(list_row.get("ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA"))
        reminder_key = reminder["ZCKIDENTIFIER"].upper()
        previous_section_id = mapping.get(reminder_key)
        target_section_id = section["ZCKIDENTIFIER"].upper()
        list_sync = cloud_sync_evidence(
            con,
            table="ZREMCDBASELIST",
            identifier=list_row["ZCKIDENTIFIER"],
        )
    finally:
        con.close()

    if (
        previous_section_id == target_section_id
        and list_sync.get("icloud_sync_verified") is True
    ):
        json_out(
            operation_receipt(
                status="unchanged",
                operation="move_to_section",
                operation_id=operation_id,
                backend="reminderkit_private",
                target={"id": reminder_key, "section_id": target_section_id},
                before={"reminder": before_reminder, "section_id": previous_section_id},
                after={"reminder": before_reminder, "section_id": previous_section_id},
                verification={
                    "state": "cloud_read_back",
                    "section_membership": True,
                    "icloud_sync": "verified",
                    "cloud": list_sync,
                },
                recovery={"semantics": "not_applicable"},
                capability=capability,
            )
        )
        return 0

    helper_payload = invoke_reminderkit_section(
        "move",
        reminder_key,
        target_section_id,
    )
    sync = wait_for_cloud_sync(
        db,
        table="ZREMCDBASELIST",
        identifier=list_row["ZCKIDENTIFIER"],
    )
    fresh = connect(db)
    try:
        refreshed_reminder = find_reminder(fresh, reminder_id=reminder_key)
        refreshed_list = fresh.execute(
            "select * from ZREMCDBASELIST where Z_PK=?",
            (list_row["Z_PK"],),
        ).fetchone()
        refreshed_mapping = membership_map(
            refreshed_list["ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA"]
            if refreshed_list
            else None
        )
    finally:
        fresh.close()
    if refreshed_mapping.get(reminder_key) != target_section_id:
        raise AdapterError(
            "ReminderKit section move could not be read back",
            code="sync_pending",
            partial_failure=True,
            mutation_outcome_unknown=True,
            reminder_id=reminder_key,
            section_id=target_section_id,
        )

    status = "verified" if sync.get("icloud_sync_verified") is True else "committed_verification_pending"
    warning = log_action(
        "move_to_section",
        {
            "operation_id": operation_id,
            "reminder": reminder_key,
            "section": target_section_id,
            "backend": "reminderkit",
            "repaired_existing": previous_section_id == target_section_id,
        },
    )
    warnings: list[dict[str, Any] | str] = []
    if warning:
        warnings.append(warning)
    if status != "verified":
        warnings.append("The section membership exists locally, but iCloud upload is still pending.")
    recovery = (
        {"semantics": "not_applicable"}
        if previous_section_id == target_section_id
        else {
            "semantics": "move_to_previous_section"
            if previous_section_id
            else "remove_section_membership",
            "previous_section_id": previous_section_id,
            "automatic_restore_available": False,
        }
    )
    json_out(
        operation_receipt(
            status=status,
            operation="move_to_section",
            operation_id=operation_id,
            backend="reminderkit_private",
            target={"id": reminder_key, "section_id": target_section_id},
            before={"reminder": before_reminder, "section_id": previous_section_id},
            after={
                "reminder": reminder_mutation_snapshot(refreshed_reminder),
                "section_id": target_section_id,
                "section": section["ZDISPLAYNAME"],
                "repaired_existing": previous_section_id == target_section_id,
            },
            verification={
                "state": "cloud_read_back" if status == "verified" else "local_read_back",
                "section_membership": True,
                "icloud_sync": "verified" if status == "verified" else "pending",
                "cloud": sync,
                "helper_saved": helper_payload.get("saved") is True,
            },
            recovery=recovery,
            warnings=warnings or None,
            capability=capability,
        )
    )
    return 0


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
    if use_native_section_backend(args):
        return cmd_move_to_section_reminderkit(args)
    operation_id = new_operation_id()
    con = connect(db)
    try:
        capability = receipt_capability(
            args, require_command_capability(con, "move_to_section_db")
        )
        con.execute("begin immediate")
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        require_reminder_version(
            reminder,
            getattr(args, "if_version", None),
            required=True,
        )
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
                status="committed_verification_pending",
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
                verification={
                    "state": "local_read_back",
                    "section_membership": True,
                    "icloud_sync": "not_verified",
                },
                recovery={
                    "semantics": "move_to_previous_section" if previous_section_id else "remove_section_membership",
                    "previous_section_id": previous_section_id,
                    "automatic_restore_available": False,
                },
                warnings=[
                    *([warning] if warning else []),
                    "Direct SQLite section membership is local-only until a native Reminders save repairs it.",
                ],
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


def attach_url_record(
    con: sqlite3.Connection,
    reminder: dict[str, Any],
    url: str,
    *,
    preferred_order: int | None = None,
    preserve_preferred_order: bool = False,
) -> dict[str, Any]:
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
    sort_order = preferred_order
    if not preserve_preferred_order:
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


def delete_url_attachment_record(
    con: sqlite3.Connection,
    reminder: dict[str, Any],
    attachment: dict[str, Any],
) -> dict[str, Any]:
    if int(attachment["Z_ENT"]) != URL_ATTACHMENT_ENT:
        raise AdapterError("Only URL attachment rows use native row-deletion semantics")
    now = core_now()
    cloud_pk = attachment.get("ZCKCLOUDSTATE")
    cloud_before = con.execute(
        """
        select ZCURRENTLOCALVERSION,ZOBJECT,Z13_OBJECT
        from ZREMCKCLOUDSTATE where Z_PK=?
        """,
        (cloud_pk,),
    ).fetchone()
    if (
        cloud_before is None
        or cloud_before["ZOBJECT"] != attachment["Z_PK"]
        or cloud_before["Z13_OBJECT"] != URL_ATTACHMENT_ENT
    ):
        raise AdapterError(
            "URL attachment cloud-state tombstone is missing or owns another object",
            code="schema_mismatch",
        )
    result = con.execute(
        "delete from ZREMCDOBJECT where Z_PK=? and Z_ENT=?",
        (attachment["Z_PK"], URL_ATTACHMENT_ENT),
    )
    if result.rowcount != 1:
        raise AdapterError("URL attachment row could not be deleted", code="schema_mismatch")
    if not bump_cloud_state(con, cloud_pk, now):
        raise AdapterError(
            "URL attachment cloud-state tombstone could not be updated",
            code="schema_mismatch",
        )
    cloud_after = con.execute(
        """
        select ZCURRENTLOCALVERSION,ZOBJECT,Z13_OBJECT
        from ZREMCKCLOUDSTATE where Z_PK=?
        """,
        (cloud_pk,),
    ).fetchone()
    expected_cloud_version = int(cloud_before["ZCURRENTLOCALVERSION"] or 0) + 1
    if (
        cloud_after is None
        or cloud_after["ZCURRENTLOCALVERSION"] != expected_cloud_version
        or cloud_after["ZOBJECT"] != attachment["Z_PK"]
        or cloud_after["Z13_OBJECT"] != URL_ATTACHMENT_ENT
    ):
        raise AdapterError(
            "URL attachment cloud-state tombstone update could not be verified",
            code="schema_mismatch",
        )
    touch_reminder(con, reminder, now)
    return {
        **attachment_payload(attachment),
        "row_deleted": True,
        "cloud_state_tombstone_retained": True,
    }


def delete_attachment_record(
    con: sqlite3.Connection,
    reminder: dict[str, Any],
    attachment: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    if int(attachment["Z_ENT"]) == URL_ATTACHMENT_ENT:
        return delete_url_attachment_record(con, reminder, attachment), "row_deleted"
    raise AdapterError(
        "Image attachment removal requires a closed SQLite connection",
        code="unsupported_capability",
    )


def compensate_new_attachment(
    db_path: Path,
    reminder: dict[str, Any],
    result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not result:
        return None
    row = result.get("_row")
    if not isinstance(row, dict):
        return None
    return remove_image_reminderkit_record(db_path, reminder, row)


def attachment_replacement_readback(
    con: sqlite3.Connection,
    *,
    reminder_pk: int,
    old_attachment_pk: int,
    new_attachment_pk: int,
    expected_new_order: int | None = None,
    verify_new_order: bool = False,
) -> dict[str, bool | str]:
    old_state = con.execute(
        "select ZMARKEDFORDELETION,ZREMINDER2 from ZREMCDOBJECT where Z_PK=?",
        (old_attachment_pk,),
    ).fetchone()
    new_state = con.execute(
        """
        select Z_FOK_REMINDER1 from ZREMCDOBJECT
        where Z_PK=? and ZREMINDER2=? and coalesce(ZMARKEDFORDELETION,0)=0
        """,
        (new_attachment_pk, reminder_pk),
    ).fetchone()
    old_row_deleted = old_state is None
    old_soft_deleted = bool(old_state and old_state["ZMARKEDFORDELETION"])
    old_detached = old_row_deleted or (
        old_state is not None and old_state["ZREMINDER2"] != reminder_pk
    )
    old_removed = old_detached
    order_preserved = not verify_new_order or (
        new_state is not None and new_state["Z_FOK_REMINDER1"] == expected_new_order
    )
    old_removal = (
        "row_deleted"
        if old_row_deleted
        else "detached"
        if old_detached
        else "soft_deleted"
        if old_soft_deleted
        else "active"
    )
    return {
        "old_attachment_removed": old_removed,
        "old_attachment_removal": old_removal,
        "old_attachment_row_deleted": old_row_deleted,
        "old_attachment_soft_deleted": old_soft_deleted,
        "old_attachment_detached_from_reminder": old_detached,
        "new_attachment_active": new_state is not None,
        "replacement_order_preserved": order_preserved,
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
        capability = receipt_capability(
            args, require_command_capability(con, "attachment_mutation_db")
        )
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        require_reminder_version(
            reminder,
            getattr(args, "if_version", None),
            required=True,
        )
        before = reminder_mutation_snapshot(reminder)
        if args.backend == "reminderkit":
            try:
                result = attach_image_reminderkit_record(
                    con,
                    reminder,
                    image,
                    validated_image=getattr(args, "_validated_image", None),
                )
                status = "verified"
                verification = {
                    "state": "read_back",
                    "mobile_visible_likely": True,
                    "sync_status": "verified_mobile_visible",
                    "final_attachment_content_hash": image_attachment_content_hash(
                        result["attachment"]
                    ),
                }
                recovery = {
                    "semantics": "delete_attachment_with_fresh_reference",
                    "automatic_restore_available": False,
                }
                pending_warning = None
                pending_error = None
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
                    "code": exc.reason_code,
                    "message": str(exc),
                }
                pending_error = {
                    "code": "sync_pending",
                    "reason_code": exc.reason_code,
                    "message": str(exc),
                    "retryable": exc.retryable,
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
                pending_error = {
                    "code": "sync_pending",
                    "reason_code": exc.code,
                    "message": str(exc),
                    "retryable": True,
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
            recovery = {
                "semantics": "delete_attachment_with_fresh_reference",
                "automatic_restore_available": False,
            }
            pending_warning = {
                "code": "local_only_attachment",
                "message": result["warning"],
            }
            pending_error = None
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
            **({"error": pending_error} if pending_error is not None else {}),
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
    image_hash = getattr(args, "_validated_image_sha256", None)
    if not isinstance(image_hash, str):
        image_hash = hashlib.sha256(image.read_bytes()).hexdigest() if image.exists() else "missing"
    result = execute_idempotent_adapter_command(
        args=args,
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
    return 0 if result.get("status") in SUCCESS_RECEIPT_STATUSES else 1


def copy_image_attachment_once(args: argparse.Namespace) -> dict[str, Any]:
    """Copy one exact active source image into a different Reminder."""

    require_exact_reminder_selector(
        reminder_id=args.id,
        title=None,
        list_name=None,
    )
    require_exact_reminder_selector(
        reminder_id=args.source_id,
        title=None,
        list_name=None,
    )
    destination_id = normalize_uuid(args.id)
    source_id = normalize_uuid(args.source_id)
    if destination_id == source_id:
        raise AdapterError(
            "Image copy requires different source and destination Reminders",
            code="invalid_input",
            reason_code="copy_source_matches_destination",
        )

    operation_id = new_operation_id()
    db = resolve_database(args.db, write=True)
    con = connect(db)
    result: dict[str, Any] | None = None
    pending_warning: dict[str, Any] | None = None
    pending_error: dict[str, Any] | None = None
    try:
        capability = receipt_capability(
            args, require_command_capability(con, "attachment_mutation_db")
        )
        source = find_reminder(con, reminder_id=source_id)
        destination = find_reminder(con, reminder_id=destination_id)
        require_reminder_version(source, args.if_source_version, required=True)
        require_reminder_version(destination, args.if_version, required=True)
        selected, candidates, reason = resolve_attachment_selection(
            con,
            source,
            attachment_id=args.attachment_id,
            attachment_type="image",
        )
        if not selected:
            raise AdapterError(
                reason or "The exact source image attachment was not found",
                code="ambiguous_target" if candidates else "invalid_input",
                reason_code="source_image_attachment_not_exact",
                candidate_count=len(candidates),
            )
        source_attachment = attachment_payload(selected)
        source_identity = image_copy_identity(source_attachment)
        source_image = exact_source_image_path(source_attachment)
        try:
            validated_source = validate_image_input(source_image)
        except ImageInputError as exc:
            raise AdapterError(
                str(exc),
                code="invalid_input",
                reason_code=f"source_{exc.reason_code}",
            ) from exc
        stored_sha512 = source_attachment.get("sha512")
        if not isinstance(stored_sha512, str) or not re.fullmatch(
            r"[A-Fa-f0-9]{128}", stored_sha512
        ):
            raise AdapterError(
                "The source image content digest is unavailable",
                code="schema_mismatch",
                reason_code="source_image_digest_unavailable",
            )

        before_destination = reminder_mutation_snapshot(destination)
        ensure_private_dir(CACHE_DIR)
        with tempfile.TemporaryDirectory(
            prefix="copy-image.",
            dir=CACHE_DIR,
        ) as temp_dir:
            suffix = ".png" if validated_source.format == "png" else ".jpg"
            snapshot_path = Path(temp_dir) / f"source{suffix}"
            shutil.copyfile(validated_source.path, snapshot_path)
            snapshot_path.chmod(0o600)
            try:
                validated_snapshot = validate_image_input(snapshot_path)
            except ImageInputError as exc:
                raise AdapterError(
                    str(exc),
                    code="invalid_input",
                    reason_code=f"source_snapshot_{exc.reason_code}",
                ) from exc
            snapshot_bytes = snapshot_path.read_bytes()
            if (
                validated_snapshot.sha256 != validated_source.sha256
                or hashlib.sha512(snapshot_bytes).hexdigest().casefold()
                != stored_sha512.casefold()
            ):
                raise AdapterError(
                    "The source image changed while its private snapshot was created",
                    code="concurrent_modification",
                    reason_code="source_image_bytes_changed",
                )

            # Recheck both private versions and the exact source attachment after
            # byte snapshotting, immediately before the native destination write.
            source = reread_reminder(con, int(source["Z_PK"]))
            destination = reread_reminder(con, int(destination["Z_PK"]))
            require_reminder_version(source, args.if_source_version, required=True)
            require_reminder_version(destination, args.if_version, required=True)
            selected_again, candidates, reason = resolve_attachment_selection(
                con,
                source,
                attachment_id=args.attachment_id,
                attachment_type="image",
            )
            if not selected_again:
                raise AdapterError(
                    reason or "The source image changed before copy dispatch",
                    code="concurrent_modification",
                    reason_code="source_attachment_changed_before_copy",
                    candidate_count=len(candidates),
                )
            if image_copy_identity(attachment_payload(selected_again)) != source_identity:
                raise AdapterError(
                    "The source image changed before copy dispatch",
                    code="concurrent_modification",
                    reason_code="source_attachment_changed_before_copy",
                )

            try:
                result = attach_image_reminderkit_record(
                    con,
                    destination,
                    validated_snapshot.path,
                )
                status = "verified"
            except AttachmentVerificationError as exc:
                result = exc.compensation_result()
                status = "committed_verification_pending"
                pending_message = (
                    "The destination attachment exists, but native image-data or "
                    "mobile-visibility verification did not complete."
                )
                pending_warning = {
                    "code": exc.reason_code,
                    "message": pending_message,
                }
                pending_error = {
                    "code": "sync_pending",
                    "reason_code": exc.reason_code,
                    "message": pending_message,
                    "retryable": False,
                }
            except AdapterError as exc:
                if not exc.details.get("partial_failure"):
                    raise
                pending_message = (
                    "The destination may contain the copied image, but native "
                    "verification did not complete."
                )
                result = {
                    "attachment": {},
                    "sync": {"mobile_visible_likely": False},
                }
                status = "committed_verification_pending"
                pending_warning = {"code": exc.code, "message": pending_message}
                pending_error = {
                    "code": "sync_pending",
                    "reason_code": str(exc.details.get("reason_code") or exc.code),
                    "message": pending_message,
                    "retryable": False,
                }

        attachment = dict(result.get("attachment") or {})
        result.pop("_row", None)
        destination_content_matched = (
            image_copy_content_identity(attachment)
            == image_copy_content_identity(source_attachment)
        )
        if status == "verified" and not destination_content_matched:
            status = "committed_verification_pending"
            pending_warning = {
                "code": "destination_image_content_mismatch",
                "message": (
                    "The copied destination attachment exists, but its image digest or "
                    "decoded metadata does not match the exact source image."
                ),
            }
            pending_error = {
                "code": "sync_pending",
                "reason_code": "destination_image_content_mismatch",
                "message": pending_warning["message"],
                "retryable": False,
            }
        source_unchanged = False
        destination_after: dict[str, Any] = {
            "id": destination_id,
            "store_read_back_pending": True,
        }
        try:
            source_after = reread_reminder(con, int(source["Z_PK"]))
            destination_after = reminder_mutation_snapshot(
                reread_reminder(con, int(destination["Z_PK"]))
            )
            require_reminder_version(
                source_after,
                args.if_source_version,
                required=True,
            )
            selected_after, _, _ = resolve_attachment_selection(
                con,
                source_after,
                attachment_id=args.attachment_id,
                attachment_type="image",
            )
            source_unchanged = bool(
                selected_after
                and image_copy_identity(attachment_payload(selected_after))
                == source_identity
            )
        except (AdapterError, sqlite3.Error):
            source_unchanged = False

        if not source_unchanged:
            status = "committed_verification_pending"
            pending_warning = {
                "code": "source_final_read_pending",
                "message": (
                    "The destination may contain the copied image, but the source "
                    "could not be proven unchanged by final read-back."
                ),
            }
            pending_error = {
                "code": "sync_pending",
                "reason_code": "source_final_read_pending",
                "message": pending_warning["message"],
                "retryable": False,
            }

        journal_warning = log_action(
            "copy_image_attachment",
            {
                "operation_id": operation_id,
                "source_reminder_id": source_id,
                "reminder_id": destination_id,
                "source_attachment_id": source_attachment["id"],
                "attachment_id": attachment.get("id"),
            },
        )
        warnings = [item for item in (pending_warning, journal_warning) if item]
        return operation_receipt(
            status=status,
            operation="copy_image",
            operation_id=operation_id,
            backend="reminderkit_private",
            target={
                "source_reminder_id": source_id,
                "reminder_id": destination_id,
                "source_attachment_id": source_attachment["id"],
                "attachment_id": attachment.get("id"),
            },
            before={"reminder": before_destination},
            after={
                "reminder": destination_after,
                "attachment": attachment,
            },
            verification={
                "state": "read_back" if status == "verified" else "pending",
                "write_performed": True if attachment.get("id") else None,
                "final_read": status == "verified",
                "matched": status == "verified",
                "source_unchanged": source_unchanged,
                "source_bytes_matched": True,
                "destination_attachment_active": bool(attachment.get("id")),
                "destination_content_matched": destination_content_matched,
            },
            recovery={
                "semantics": (
                    "delete_copied_attachment_with_fresh_reference"
                    if status == "verified"
                    else "read_both_reminders_before_retry"
                ),
                "automatic_retry_safe": False,
            },
            warnings=warnings or None,
            **({"error": pending_error} if pending_error is not None else {}),
            capability=capability,
        )
    finally:
        con.close()


def cmd_copy_image_attachment(args: argparse.Namespace) -> int:
    result = execute_idempotent_adapter_command(
        args=args,
        operation="copy_image_attachment",
        key=args.idempotency_key,
        input_payload={
            "source_reminder_id": normalize_uuid(args.source_id),
            "reminder_id": normalize_uuid(args.id),
            "source_attachment_id": str(args.attachment_id),
            "if_source_version": args.if_source_version,
            "if_version": args.if_version,
        },
        callback=lambda: copy_image_attachment_once(args),
    )
    json_out(result)
    return 0 if result.get("status") in SUCCESS_RECEIPT_STATUSES else 1


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
        capability = receipt_capability(
            args, require_command_capability(con, "attachment_mutation_db")
        )
        con.execute("begin immediate")
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        require_reminder_version(
            reminder,
            getattr(args, "if_version", None),
            required=True,
        )
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


def cmd_delete_attachment(args: argparse.Namespace) -> int:
    require_exact_reminder_selector(
        reminder_id=args.id,
        title=args.title,
        list_name=args.list,
    )
    db = resolve_database(args.db, write=True)
    operation_id = new_operation_id()
    con: sqlite3.Connection | None = connect(db)
    native_removal_verified = False
    try:
        capability = receipt_capability(
            args, require_command_capability(con, "attachment_mutation_db")
        )
        con.execute("begin immediate")
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        require_reminder_version(
            reminder,
            getattr(args, "if_version", None),
            required=True,
        )
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
        if int(selected["Z_ENT"]) == IMAGE_ATTACHMENT_ENT:
            require_image_helper_compiler(args)
            con.rollback()
            con.close()
            con = None
            try:
                deleted = remove_image_reminderkit_record(db, reminder, selected)
            except Exception:
                raise
            native_removal_verified = True
            try:
                con = connect(db)
            except Exception as exc:
                raise AdapterError(
                    "Image removal was verified but final store read-back failed",
                    code="sync_pending",
                    partial_failure=True,
                    mutation_outcome_unknown=True,
                    native_removal_verified=True,
                ) from exc
            deletion_mode = "native_detached"
        else:
            deleted, deletion_mode = delete_attachment_record(con, reminder, selected)
        refreshed_attachment = con.execute(
            "select ZMARKEDFORDELETION from ZREMCDOBJECT where Z_PK=?",
            (selected["Z_PK"],),
        ).fetchone()
        attachment_removed = (
            refreshed_attachment is None
            if deletion_mode == "row_deleted"
            else (
                deleted.get("detached_from_reminder") is True
                if deletion_mode == "native_detached"
                else bool(
                    refreshed_attachment
                    and refreshed_attachment["ZMARKEDFORDELETION"]
                )
            )
        )
        if not attachment_removed:
            raise AdapterError("Attachment deletion could not be read back", code="schema_mismatch")
        after_reminder = reminder_mutation_snapshot(reread_reminder(con, reminder["Z_PK"]))
        con.commit()
        warning = log_action(
            "delete_attachment",
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
                backend=(
                    "reminderkit"
                    if before_attachment.get("type") == "image"
                    else "sqlite_private"
                ),
                target={
                    "id": reminder_url(reminder["ZCKIDENTIFIER"]),
                    "attachment_id": before_attachment["id"],
                },
                before={"reminder": before_reminder, "attachment": before_attachment},
                after={"reminder": after_reminder, "attachment": deleted},
                verification={
                    "state": "read_back",
                    "attachment_removed": True,
                    "attachment_row_deleted": deletion_mode == "row_deleted",
                    "attachment_soft_deleted": deletion_mode == "soft_deleted",
                    "attachment_detached_from_reminder": deletion_mode
                    == "native_detached",
                    "cloud_state_tombstone_retained": deleted.get(
                        "cloud_state_tombstone_retained"
                    ),
                    "native_reminderkit": deleted.get("native_reminderkit"),
                },
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
    except Exception as exc:
        if con is not None:
            con.rollback()
        if native_removal_verified:
            raise AdapterError(
                "Image removal was verified but final receipt read-back failed",
                code="sync_pending",
                partial_failure=True,
                mutation_outcome_unknown=True,
                native_removal_verified=True,
                original_error=f"{type(exc).__name__}: {exc}",
            ) from exc
        raise
    finally:
        if con is not None:
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
    con: sqlite3.Connection | None = connect(db)
    reminder = None
    new_result = None
    capability: dict[str, Any] = {}
    before_reminder: dict[str, Any] = {}
    old_attachment: dict[str, Any] = {}
    new_attachment: dict[str, Any] = {}
    committed = False
    native_removal_verified = False
    try:
        capability = receipt_capability(
            args, require_command_capability(con, "attachment_mutation_db")
        )
        if args.url:
            con.execute("begin immediate")
        reminder = find_reminder(con, reminder_id=args.id, title=args.title, list_name=args.list)
        expected_version = getattr(args, "if_version", None)
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            expected_version = None
        require_reminder_version(reminder, expected_version, required=True)
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
        if args.url:
            replacement_url = normalized_url(args.url)
            existing_url = con.execute(
                """
                select Z_PK,ZCKIDENTIFIER
                from ZREMCDOBJECT
                where ZREMINDER2=? and Z_ENT=? and ZURL=?
                  and coalesce(ZMARKEDFORDELETION,0)=0
                """,
                (reminder["Z_PK"], URL_ATTACHMENT_ENT, replacement_url),
            ).fetchone()
            if existing_url is not None:
                raise AdapterError(
                    "Replacement URL is already attached to this reminder",
                    code="invalid_input",
                    existing_attachment_id=existing_url["ZCKIDENTIFIER"],
                )
        if args.image:
            try:
                new_result = attach_image_reminderkit_record(
                    con,
                    reminder,
                    Path(args.image).expanduser().resolve(),
                    validated_image=getattr(args, "_validated_image", None),
                )
            except AttachmentVerificationError as attach_exc:
                new_result = attach_exc.compensation_result()
                raise
            new_attachment = new_result.get("attachment") or {}
            if int(new_attachment.get("pk", -1)) == int(selected["Z_PK"]):
                raise AdapterError(
                    "Replacement result reused the selected attachment identity",
                    code="sync_pending",
                    partial_failure=True,
                    mutation_outcome_unknown=True,
                )
            con.close()
            con = None
            try:
                deleted = remove_image_reminderkit_record(db, reminder, selected)
            except Exception:
                raise
            native_removal_verified = True
            try:
                con = connect(db)
            except Exception as exc:
                raise AdapterError(
                    "Image replacement committed but final store read-back failed",
                    code="sync_pending",
                    partial_failure=True,
                    mutation_outcome_unknown=True,
                    native_removal_verified=True,
                ) from exc
        else:
            new_result = attach_url_record(
                con,
                reminder,
                args.url,
                preferred_order=selected.get("Z_FOK_REMINDER1"),
                preserve_preferred_order=True,
            )
            new_attachment = new_result.get("attachment") or {}
            if int(new_attachment.get("pk", -1)) == int(selected["Z_PK"]):
                raise AdapterError(
                    "Replacement result reused the selected attachment identity",
                    code="sync_pending",
                    partial_failure=True,
                    mutation_outcome_unknown=True,
                )
            deleted, _ = delete_attachment_record(con, reminder, selected)
        if args.image:
            committed = True
        else:
            con.commit()
            committed = True
        readback = attachment_replacement_readback(
            con,
            reminder_pk=reminder["Z_PK"],
            old_attachment_pk=selected["Z_PK"],
            new_attachment_pk=int(new_attachment.get("pk")),
            expected_new_order=(selected.get("Z_FOK_REMINDER1") if args.url else None),
            verify_new_order=bool(args.url),
        )
        replacement_verified = (
            bool(readback.get("old_attachment_removed"))
            and bool(readback.get("new_attachment_active"))
            and readback.get("replacement_order_preserved") is not False
        )
        if not replacement_verified:
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
                "final_attachment_content_hash": (
                    image_attachment_content_hash(new_attachment)
                    if args.image
                    else None
                ),
                "old_attachment_cloud_state_retained": deleted.get(
                    "cloud_state_tombstone_retained"
                ),
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
        if con is not None:
            con.rollback()
        error_details = exc.details if isinstance(exc, AdapterError) else {}
        mutation_outcome_unknown = (
            error_details.get("mutation_outcome_unknown") is True
        )
        if committed or native_removal_verified:
            public_new_result = dict(new_result or {})
            public_new_result.pop("_row", None)
            return operation_receipt(
                status="committed_verification_pending",
                operation="replace_attachment",
                operation_id=operation_id,
                backend="reminderkit" if args.image else "sqlite_private",
                target={
                    "id": reminder.get("ZCKIDENTIFIER") if reminder else args.id,
                    "old_attachment_id": old_attachment.get("id"),
                    "new_attachment_id": new_attachment.get("id"),
                },
                before={"reminder": before_reminder, "attachment": old_attachment},
                after={
                    "new_attachment": public_new_result,
                    "old_attachment": deleted,
                },
                verification={
                    "state": "pending",
                    "write_performed": True,
                    "replacement_committed": True,
                    "native_removal_verified": native_removal_verified,
                },
                recovery={
                    "semantics": "inspect_attachments_before_retry",
                    "automatic_retry_safe": False,
                },
                error={
                    "code": "sync_pending",
                    "message": (
                        "Attachment replacement committed but final read-back "
                        "failed; inspect exact attachments before any retry."
                    ),
                    "original_error": f"{type(exc).__name__}: {exc}",
                },
                capability=capability,
            )
        if args.image and not committed and mutation_outcome_unknown:
            public_new_result = dict(new_result or {})
            public_new_result.pop("_row", None)
            new_attachment_id = (
                new_result.get("attachment", {}).get("id")
                if new_result is not None
                else None
            )
            return operation_receipt(
                status="committed_verification_pending",
                operation="replace_attachment",
                operation_id=operation_id,
                backend="reminderkit",
                target={
                    "id": reminder.get("ZCKIDENTIFIER") if reminder else args.id,
                    "old_attachment_id": old_attachment.get("id"),
                    "new_attachment_id": new_attachment_id,
                },
                before={"reminder": before_reminder, "attachment": old_attachment},
                after={
                    "new_attachment": public_new_result,
                    "old_attachment_state": "unknown",
                },
                verification={
                    "state": "pending",
                    "write_performed": None,
                    "replacement_committed": None,
                    "mutation_outcome_unknown": True,
                    "native_removal_verified": error_details.get(
                        "native_removal_verified"
                    )
                    is True,
                },
                recovery={
                    "semantics": "inspect_attachments_before_retry",
                    "automatic_retry_safe": False,
                },
                error={
                    "code": exc.code
                    if isinstance(exc, AdapterError)
                    else "sync_pending",
                    "message": (
                        "Image replacement outcome is unknown; inspect exact "
                        "attachments before any retry."
                    ),
                    "original_error": f"{type(exc).__name__}: {exc}",
                },
                capability=capability,
            )
        if args.image and not committed and reminder is not None and new_result is not None:
            public_new_result = dict(new_result)
            public_new_result.pop("_row", None)
            new_attachment_id = new_result.get("attachment", {}).get("id")
            try:
                if con is not None:
                    con.rollback()
                    con.close()
                    con = None
                compensated = compensate_new_attachment(db, reminder, new_result)
                if compensated is None:
                    raise AdapterError(
                        "Compensating attachment cleanup could not be verified",
                        code="sync_pending",
                    )
                con = connect(db)
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
        if con is not None:
            con.close()


def cmd_replace_attachment(args: argparse.Namespace) -> int:
    image_hash = None
    if args.image:
        image = Path(args.image).expanduser().resolve()
        image_hash = getattr(args, "_validated_image_sha256", None)
        if not isinstance(image_hash, str):
            image_hash = hashlib.sha256(image.read_bytes()).hexdigest() if image.exists() else "missing"
    key = getattr(args, "idempotency_key", None)
    if not isinstance(key, str):
        key = None
    result = execute_idempotent_adapter_command(
        args=args,
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


def add_common_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", help="Specific Reminders sqlite database path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apple Reminders local JSON adapter")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("read_reminder")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.set_defaults(func=cmd_read_reminder)

    p = sub.add_parser("list_sections")
    add_common_db(p)
    p.add_argument("--list-id", required=True)
    p.add_argument("--limit", type=positive_int, default=100)
    p.set_defaults(func=cmd_list_sections)

    p = sub.add_parser("list_tags")
    add_common_db(p)
    p.add_argument("--account-id")
    p.add_argument("--query")
    p.add_argument("--limit", type=positive_int, default=100)
    p.set_defaults(func=cmd_list_tags)

    p = sub.add_parser("add_tag")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--tag", required=True)
    p.add_argument("--if-version", type=int, required=True)
    p.set_defaults(func=cmd_add_tag)

    p = sub.add_parser("remove_tag")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--tag", required=True)
    p.add_argument("--if-version", type=int, required=True)
    p.set_defaults(func=cmd_remove_tag)

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
    p.add_argument("--if-version", type=int, required=True)
    p.set_defaults(func=cmd_move_to_section)

    p = sub.add_parser("attach_image")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--image", required=True)
    p.add_argument("--backend", choices=["reminderkit", "db"], default="reminderkit")
    p.add_argument("--if-version", type=int, required=True)
    p.add_argument("--idempotency-key")
    p.set_defaults(func=cmd_attach_image)

    p = sub.add_parser("copy_image_attachment")
    add_common_db(p)
    p.add_argument("--source-id", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--attachment-id", required=True)
    p.add_argument("--if-source-version", type=int, required=True)
    p.add_argument("--if-version", type=int, required=True)
    p.add_argument("--idempotency-key", required=True)
    p.set_defaults(func=cmd_copy_image_attachment)

    p = sub.add_parser("attach_url")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--url", required=True)
    p.add_argument("--if-version", type=int, required=True)
    p.set_defaults(func=cmd_attach_url)

    p = sub.add_parser("list_attachments")
    add_common_db(p)
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--list")
    p.add_argument("--type", choices=["image", "url"])
    p.add_argument("--limit", type=positive_int, default=100)
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
    p.add_argument("--if-version", type=int, required=True)
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
    p.add_argument("--if-version", type=int, required=True)
    p.add_argument("--idempotency-key")
    p.set_defaults(func=cmd_replace_attachment)

    p = sub.add_parser("list_deleted_reminders")
    add_common_db(p)
    p.add_argument("--account-id")
    p.add_argument("--limit", type=positive_int, default=20)
    p.add_argument("--offset", type=nonnegative_int, default=0)
    p.set_defaults(func=cmd_list_deleted_reminders)

    p = sub.add_parser("read_deleted_reminder")
    add_common_db(p)
    p.add_argument("--id", required=True)
    p.add_argument("--attachment-limit", type=positive_int, default=100)
    p.set_defaults(func=cmd_read_deleted_reminder)

    p = sub.add_parser("recover_deleted_reminder")
    add_common_db(p)
    p.add_argument("--id", required=True)
    p.add_argument("--list-id", required=True)
    p.add_argument("--if-store-identity", required=True)
    p.add_argument("--if-version", type=int, required=True)
    p.add_argument("--if-deleted-at", required=True)
    p.add_argument("--if-attachment-digest", required=True)
    p.add_argument("--if-native-guard-digest", required=True)
    p.add_argument("--idempotency-key", required=True)
    p.set_defaults(func=cmd_recover_deleted_reminder)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "command", None) in {
            "attach_image",
            "replace_attachment",
        } and getattr(args, "image", None):
            try:
                validated_image = validate_image_input(args.image)
            except ImageInputError as exc:
                raise AdapterError(
                    str(exc),
                    code="invalid_input",
                    reason_code=exc.reason_code,
                ) from exc
            args.image = str(validated_image.path)
            args._validated_image = validated_image
            args._validated_image_sha256 = validated_image.sha256
        preflight_experimental_command(args)
        return args.func(args)
    except AdapterError as exc:
        if getattr(args, "command", None) in MUTATION_COMMANDS:
            json_out(command_exception_receipt(args, exc))
            return 1
        details = dict(exc.details)
        explicit_status = details.pop("result_status", None)
        if explicit_status is None and details.get("partial_failure"):
            explicit_status = (
                "failed_no_mutation"
                if details.get("compensated") and not details.get("compensation_error")
                else "failed_manual_repair_required"
            )
        status = explicit_status or "failed_no_mutation"
        return fail(
            str(exc),
            code=exc.code,
            status=status,
            **details,
        )
    except Exception as exc:
        if getattr(args, "command", None) in MUTATION_COMMANDS:
            json_out(command_exception_receipt(args, exc))
            return 1
        return fail(f"{type(exc).__name__}: {exc}", code="unexpected_error")


if __name__ == "__main__":
    raise SystemExit(main())
