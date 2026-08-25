#!/usr/bin/env python3
"""Content-free onboarding and capability doctor for Apple Reminders.

The doctor deliberately avoids reminder rows, list/section/tag names, cached
payloads, journal contents, AppleScript, EventKit, and private-framework loads.
It only inspects application metadata, directory/file metadata, SQLite schema,
anonymous account counts, toolchain availability, and static framework paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from reminders_contracts import (  # noqa: E402
    REQUIRED_TABLES as CONTRACT_REQUIRED_TABLES,
    command_schema_requirements,
)


REPORT_SCHEMA_VERSION = 1
DOCTOR_NAME = "apple-reminders-doctor"

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_BLOCKED = "blocked"
STATUS_UNKNOWN = "unknown"
STATUS_SKIPPED = "skipped"
CHECK_STATUSES = {
    STATUS_OK,
    STATUS_WARNING,
    STATUS_BLOCKED,
    STATUS_UNKNOWN,
    STATUS_SKIPPED,
}

HOME = Path.home()
GROUP = HOME / "Library/Group Containers/group.com.apple.reminders/Container_v1"
STORES = GROUP / "Stores"
FILES = GROUP / "Files"
APP_SUPPORT = HOME / "Library/Application Support/apple-reminders-codex"
JOURNAL = APP_SUPPORT / "actions.jsonl"
BACKUPS = APP_SUPPORT / "backups"
CACHE_DIR = HOME / "Library/Caches/apple-reminders-codex"
CACHE_FILE = CACHE_DIR / "cache.json"
REQUIRED_TABLES = set(CONTRACT_REQUIRED_TABLES)
COMMAND_SCHEMA_REQUIREMENTS = command_schema_requirements("diagnostic")


def default_paths(
    *, home: Path | None = None, script_dir: Path | None = None
) -> dict[str, Any]:
    root = (home or Path.home()).expanduser()
    scripts = script_dir or Path(__file__).resolve().parent
    group = root / "Library/Group Containers/group.com.apple.reminders/Container_v1"
    app_support = root / "Library/Application Support/apple-reminders-codex"
    cache_dir = root / "Library/Caches/apple-reminders-codex"
    return {
        "home": root,
        "group": group,
        "stores": group / "Stores",
        "files": group / "Files",
        "app_support": app_support,
        "journal": app_support / "actions.jsonl",
        "backups": app_support / "backups",
        "cache_dir": cache_dir,
        "cache_file": cache_dir / "cache.json",
        "helper_binary": cache_dir / "remkit_attach_image",
        "helper_source": scripts / "remkit_attach_image.m",
        "adapter_source": scripts / "reminders_adapter.py",
        "reminders_app_candidates": [
            Path("/System/Applications/Reminders.app"),
            Path("/Applications/Reminders.app"),
        ],
        "private_frameworks": {
            "ReminderKit": Path(
                "/System/Library/PrivateFrameworks/ReminderKit.framework/ReminderKit"
            ),
            "ReminderKitInternal": Path(
                "/System/Library/PrivateFrameworks/ReminderKitInternal.framework/ReminderKitInternal"
            ),
        },
        "public_frameworks": {
            "Foundation": Path("/System/Library/Frameworks/Foundation.framework"),
            "AppKit": Path("/System/Library/Frameworks/AppKit.framework"),
        },
    }


def structured_error(
    code: str,
    message: str,
    *,
    exception: BaseException | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    if exception is not None:
        payload["exception_type"] = type(exception).__name__
        error_number = getattr(exception, "errno", None)
        if error_number is not None:
            payload["errno"] = error_number
    if details:
        payload["details"] = details
    return payload


def check_result(
    status: str,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if status not in CHECK_STATUSES:
        raise ValueError(f"Unsupported doctor status: {status}")
    return {
        "status": status,
        "code": code,
        "message": message,
        "details": details or {},
        "errors": errors or [],
    }


def redacted_path(path: Path, home: Path | None = None) -> str:
    candidate = path.expanduser()
    user_home = (home or Path.home()).expanduser()
    try:
        relative = candidate.relative_to(user_home)
    except ValueError:
        return str(candidate)
    return "~" if not relative.parts else f"~/{relative.as_posix()}"


def discover_system_info() -> dict[str, str | None]:
    mac_version, _, _ = platform.mac_ver()
    return {
        "system": platform.system() or None,
        "macos_version": mac_version or None,
        "kernel_release": platform.release() or None,
        "machine": platform.machine() or None,
        "python_version": platform.python_version() or None,
    }


def inspect_platform(system_info: dict[str, Any] | None = None) -> dict[str, Any]:
    info = dict(system_info or discover_system_info())
    if info.get("system") != "Darwin":
        return check_result(
            STATUS_BLOCKED,
            "unsupported_platform",
            "Apple Reminders integration requires macOS.",
            details=info,
        )
    version_status = STATUS_OK if info.get("macos_version") else STATUS_WARNING
    code = "macos_detected" if version_status == STATUS_OK else "macos_version_unknown"
    message = (
        "macOS was detected."
        if version_status == STATUS_OK
        else "macOS was detected, but its product version was unavailable."
    )
    return check_result(version_status, code, message, details=info)


def inspect_reminders_app(candidates: list[Path]) -> dict[str, Any]:
    app = next((candidate for candidate in candidates if candidate.is_dir()), None)
    if app is None:
        return check_result(
            STATUS_BLOCKED,
            "reminders_app_missing",
            "The Reminders application bundle was not found.",
            details={"searched_locations": [str(path) for path in candidates]},
        )
    info_path = app / "Contents/Info.plist"
    try:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        error = structured_error(
            "reminders_metadata_unreadable",
            "The Reminders bundle was found, but its version metadata could not be read.",
            exception=exc,
        )
        return check_result(
            STATUS_WARNING,
            "reminders_metadata_unreadable",
            error["message"],
            details={"application_path": str(app)},
            errors=[error],
        )
    details = {
        "application_path": str(app),
        "bundle_identifier": info.get("CFBundleIdentifier"),
        "version": info.get("CFBundleShortVersionString"),
        "build": info.get("CFBundleVersion"),
    }
    if not details["version"] and not details["build"]:
        return check_result(
            STATUS_WARNING,
            "reminders_version_unknown",
            "The Reminders app exists, but no version/build value was present.",
            details=details,
        )
    return check_result(
        STATUS_OK,
        "reminders_app_detected",
        "The Reminders app and its build metadata are available.",
        details=details,
    )


def _readonly_sqlite_uri(path: Path) -> str:
    quoted = urllib.parse.quote(str(path.expanduser().absolute()), safe="/")
    return f"file:{quoted}?mode=ro"


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    # Table names are from the static contract, never from user data.
    escaped = table.replace('"', '""')
    return {
        str(row[1])
        for row in connection.execute(f'pragma table_info("{escaped}")').fetchall()
    }


def schema_fingerprint(schema: dict[str, set[str]]) -> str:
    normalized = {
        table: sorted(columns) for table, columns in sorted(schema.items())
    }
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def command_schema_capabilities(schema: dict[str, set[str]]) -> dict[str, Any]:
    capabilities: dict[str, Any] = {}
    for command, requirements in sorted(COMMAND_SCHEMA_REQUIREMENTS.items()):
        missing_tables = sorted(set(requirements) - set(schema))
        missing_columns = {
            table: sorted(columns - schema.get(table, set()))
            for table, columns in sorted(requirements.items())
            if table in schema and columns - schema.get(table, set())
        }
        supported = not missing_tables and not missing_columns
        capabilities[command] = {
            "status": STATUS_OK if supported else STATUS_BLOCKED,
            "code": "schema_supported" if supported else "schema_mismatch",
            "supported": supported,
            "contract_level": "minimum_static_fields",
            "runtime_verification_required": True,
            "missing_tables": missing_tables,
            "missing_columns": missing_columns,
        }
    return capabilities


def _classify_sqlite_error(error: sqlite3.Error) -> tuple[str, str]:
    text = str(error).casefold()
    if "permission" in text or "authorized" in text or "readonly" in text:
        return "store_permission_denied", "The Reminders store could not be read."
    if "locked" in text or "busy" in text:
        return "store_busy", "The Reminders store was busy during the static check."
    if "not a database" in text or "malformed" in text:
        return "invalid_store", "A discovered store was not a readable SQLite database."
    return "sqlite_schema_error", "The Reminders SQLite schema could not be inspected."


def inspect_db(
    path: Path,
    *,
    store_ref: str = "store_1",
    connector: Callable[..., sqlite3.Connection] = sqlite3.connect,
) -> dict[str, Any]:
    """Inspect schema and anonymous account count without reading reminder content."""

    size_bytes: int | None = None
    try:
        size_bytes = path.stat().st_size
    except OSError:
        pass
    base: dict[str, Any] = {
        "store_ref": store_ref,
        "size_bytes": size_bytes,
        "reminder_content_rows_read": False,
        "titles_read": False,
    }
    try:
        connection = connector(_readonly_sqlite_uri(path), uri=True, timeout=1.0)
        try:
            table_rows = connection.execute(
                "select name from sqlite_master where type='table'"
            ).fetchall()
            tables = {str(row[0]) for row in table_rows}
            relevant_tables = sorted(
                tables
                & (
                    REQUIRED_TABLES
                    | {"ZREMCDACCOUNT"}
                    | set().union(
                        *(set(req) for req in COMMAND_SCHEMA_REQUIREMENTS.values())
                    )
                )
            )
            schema = {
                table: _table_columns(connection, table) for table in relevant_tables
            }
            missing_required_tables = sorted(REQUIRED_TABLES - tables)
            account_rows: int | None = None
            if "ZREMCDACCOUNT" in tables:
                # A count reveals no account identifier, title, address, or reminder row.
                account_rows = int(
                    connection.execute("select count(*) from ZREMCDACCOUNT").fetchone()[0]
                )
            commands = command_schema_capabilities(schema)
            base.update(
                {
                    "status": STATUS_OK
                    if not missing_required_tables
                    else STATUS_BLOCKED,
                    "code": "schema_available"
                    if not missing_required_tables
                    else "required_tables_missing",
                    "schema_fingerprint": schema_fingerprint(schema),
                    "required_tables_present": sorted(REQUIRED_TABLES & tables),
                    "missing_required_tables": missing_required_tables,
                    "account_metadata": {
                        "status": STATUS_OK
                        if account_rows is not None
                        else STATUS_UNKNOWN,
                        "anonymous_row_count": account_rows,
                        "aggregate_count_query_performed": account_rows is not None,
                        "identifiers_read": False,
                        "names_read": False,
                    },
                    "command_schema": commands,
                    "errors": [],
                }
            )
            return base
        finally:
            connection.close()
    except sqlite3.Error as exc:
        code, message = _classify_sqlite_error(exc)
        base.update(
            {
                "status": STATUS_BLOCKED,
                "code": code,
                "schema_fingerprint": None,
                "required_tables_present": [],
                "missing_required_tables": sorted(REQUIRED_TABLES),
                "account_metadata": {
                    "status": STATUS_UNKNOWN,
                    "anonymous_row_count": None,
                    "aggregate_count_query_performed": False,
                    "identifiers_read": False,
                    "names_read": False,
                },
                "command_schema": {},
                "errors": [structured_error(code, message, exception=exc)],
            }
        )
        return base


def _directory_entries(path: Path) -> tuple[list[Path], OSError | None]:
    try:
        return list(path.iterdir()), None
    except OSError as exc:
        return [], exc


def inspect_store_access(
    paths: dict[str, Any],
    *,
    connector: Callable[..., sqlite3.Connection] = sqlite3.connect,
) -> dict[str, Any]:
    home = paths["home"]
    group = paths["group"]
    stores = paths["stores"]
    details: dict[str, Any] = {
        "group": {
            "path": redacted_path(group, home),
            "exists": group.exists(),
            "titles_read": False,
        },
        "stores": {
            "path": redacted_path(stores, home),
            "exists": stores.exists(),
            "database_count": 0,
            "database_filenames_reported": False,
        },
        "databases": [],
    }
    if not group.exists():
        return check_result(
            STATUS_BLOCKED,
            "group_container_missing",
            "The Reminders group container is not present; launch/configure Reminders first.",
            details=details,
        )
    if not group.is_dir():
        return check_result(
            STATUS_BLOCKED,
            "group_container_invalid",
            "The expected Reminders group-container path is not a directory.",
            details=details,
        )
    _, group_error = _directory_entries(group)
    if group_error is not None:
        error = structured_error(
            "group_container_permission_denied",
            "The Reminders group container exists but cannot be enumerated.",
            exception=group_error,
        )
        return check_result(
            STATUS_BLOCKED,
            error["code"],
            error["message"],
            details=details,
            errors=[error],
        )
    if not stores.is_dir():
        return check_result(
            STATUS_BLOCKED,
            "stores_directory_missing",
            "The Reminders Stores directory is not available.",
            details=details,
        )
    entries, stores_error = _directory_entries(stores)
    if stores_error is not None:
        error = structured_error(
            "stores_permission_denied",
            "The Reminders Stores directory exists but cannot be enumerated.",
            exception=stores_error,
        )
        return check_result(
            STATUS_BLOCKED,
            error["code"],
            error["message"],
            details=details,
            errors=[error],
        )
    databases = sorted(
        (entry for entry in entries if entry.is_file() and entry.suffix == ".sqlite"),
        key=lambda item: item.name,
    )
    details["stores"]["database_count"] = len(databases)
    if not databases:
        return check_result(
            STATUS_BLOCKED,
            "no_sqlite_stores",
            "No local Reminders SQLite store was discovered.",
            details=details,
        )
    results = [
        inspect_db(database, store_ref=f"store_{index}", connector=connector)
        for index, database in enumerate(databases, start=1)
    ]
    details["databases"] = results
    usable = [item for item in results if item["status"] == STATUS_OK]
    errors = [error for item in results for error in item.get("errors", [])]
    if not usable:
        return check_result(
            STATUS_BLOCKED,
            "no_compatible_store",
            "Stores were found, but none passed the minimum content-free schema gate.",
            details=details,
            errors=errors,
        )
    status = STATUS_OK if len(usable) == len(results) else STATUS_WARNING
    code = "compatible_store_available" if status == STATUS_OK else "some_stores_unavailable"
    message = (
        "At least one compatible Reminders store is available."
        if status == STATUS_WARNING
        else "The discovered Reminders stores passed the minimum schema gate."
    )
    return check_result(status, code, message, details=details, errors=errors)


def static_command_requirements() -> dict[str, dict[str, list[str]]]:
    return {
        command: {
            table: sorted(columns) for table, columns in sorted(requirements.items())
        }
        for command, requirements in sorted(COMMAND_SCHEMA_REQUIREMENTS.items())
    }


def aggregate_command_schema(store_check: dict[str, Any]) -> dict[str, Any]:
    databases = store_check.get("details", {}).get("databases", [])
    commands: dict[str, Any] = {}
    for command in sorted(COMMAND_SCHEMA_REQUIREMENTS):
        supporting = [
            item["store_ref"]
            for item in databases
            if item.get("command_schema", {}).get(command, {}).get("supported") is True
        ]
        commands[command] = {
            "status": STATUS_OK if supporting else STATUS_BLOCKED,
            "code": "schema_supported" if supporting else "schema_mismatch",
            "supported": bool(supporting),
            "supporting_store_refs": supporting,
            "contract_level": "minimum_static_fields",
            "runtime_verification_required": True,
            "required_fields": static_command_requirements()[command],
        }
    supported_count = sum(item["supported"] for item in commands.values())
    if not databases:
        return check_result(
            STATUS_BLOCKED,
            "schema_not_inspected",
            "Command-level schema capabilities could not be inspected.",
            details={"commands": commands, "supported_count": 0},
        )
    if supported_count == len(commands):
        status, code = STATUS_OK, "all_command_schemas_supported"
    elif supported_count:
        status, code = STATUS_WARNING, "partial_command_schema_support"
    else:
        status, code = STATUS_BLOCKED, "no_command_schema_support"
    return check_result(
        status,
        code,
        "Static command contracts were evaluated without reading reminder content.",
        details={
            "contract_level": "minimum_static_fields",
            "passing_a_contract_is_not_runtime_write_approval": True,
            "supported_count": supported_count,
            "command_count": len(commands),
            "commands": commands,
        },
    )


def run_static_command(argv: list[str], *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _diagnostic_metadata(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    diagnostic = f"{process.stdout or ''}\n{process.stderr or ''}".encode(
        "utf-8", errors="replace"
    )
    return {
        "returncode": process.returncode,
        "diagnostic_bytes": len(diagnostic),
        "diagnostic_sha256": hashlib.sha256(diagnostic).hexdigest(),
        "diagnostic_text_reported": False,
    }


def inspect_helper_toolchain(
    paths: dict[str, Any],
    *,
    syntax_check: bool = True,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[str]] = run_static_command,
) -> dict[str, Any]:
    home = paths["home"]
    source = paths["helper_source"]
    clang = which("clang")
    source_exists = source.is_file()
    source_readable = source_exists and os.access(source, os.R_OK)
    public_frameworks = {
        name: {
            "path": str(path),
            "exists": path.exists(),
            "readable": path.exists() and os.access(path, os.R_OK),
        }
        for name, path in sorted(paths["public_frameworks"].items())
    }
    details: dict[str, Any] = {
        "clang": {
            "available": clang is not None,
            "path": clang,
        },
        "helper_source": {
            "path": redacted_path(source, home),
            "exists": source_exists,
            "readable": source_readable,
        },
        "public_frameworks": public_frameworks,
        "syntax_check": {
            "attempted": False,
            "mutating_build_attempted": False,
            "executable_written": False,
        },
    }
    missing_frameworks = [
        name
        for name, item in public_frameworks.items()
        if not item["exists"] or not item["readable"]
    ]
    if clang is None or not source_readable or missing_frameworks:
        return check_result(
            STATUS_WARNING,
            "helper_build_prerequisites_missing",
            "The ReminderKit helper cannot pass a static buildability gate yet.",
            details={**details, "missing_public_frameworks": missing_frameworks},
        )
    if not syntax_check:
        details["syntax_check"]["skipped_by_request"] = True
        return check_result(
            STATUS_UNKNOWN,
            "helper_syntax_check_skipped",
            "Helper prerequisites exist, but the non-linking syntax check was skipped.",
            details=details,
        )
    argv = [
        clang,
        "-x",
        "objective-c",
        "-fobjc-arc",
        "-framework",
        "Foundation",
        "-framework",
        "AppKit",
        "-fsyntax-only",
        str(source),
    ]
    details["syntax_check"]["attempted"] = True
    details["syntax_check"]["mode"] = "compile_only_no_output"
    try:
        process = runner(argv, timeout=20.0)
    except subprocess.TimeoutExpired as exc:
        error = structured_error(
            "helper_syntax_check_timeout",
            "The non-linking helper syntax check timed out.",
            exception=exc,
        )
        return check_result(
            STATUS_WARNING,
            error["code"],
            error["message"],
            details=details,
            errors=[error],
        )
    except OSError as exc:
        error = structured_error(
            "clang_invocation_failed",
            "clang could not be invoked for the non-linking helper syntax check.",
            exception=exc,
        )
        return check_result(
            STATUS_WARNING,
            error["code"],
            error["message"],
            details=details,
            errors=[error],
        )
    details["syntax_check"].update(_diagnostic_metadata(process))
    if process.returncode != 0:
        return check_result(
            STATUS_WARNING,
            "helper_syntax_check_failed",
            "The helper source failed the non-linking clang syntax check.",
            details=details,
        )
    return check_result(
        STATUS_OK,
        "helper_statically_buildable",
        "clang, helper source, frameworks, and the non-linking syntax check are available.",
        details=details,
    )


def inspect_private_frameworks(paths: dict[str, Any]) -> dict[str, Any]:
    frameworks = {
        name: {
            "path": str(path),
            "exists": path.exists(),
            "readable": path.exists() and os.access(path, os.R_OK),
        }
        for name, path in sorted(paths["private_frameworks"].items())
    }
    available = [
        name for name, item in frameworks.items() if item["exists"] and item["readable"]
    ]
    details = {
        "check_mode": "filesystem_metadata_only",
        "dlopen_attempted": False,
        "classes_instantiated": False,
        "available_frameworks": available,
        "frameworks": frameworks,
    }
    if not available:
        return check_result(
            STATUS_WARNING,
            "private_framework_unavailable",
            "No readable ReminderKit private-framework binary was found by static path checks.",
            details=details,
        )
    return check_result(
        STATUS_OK,
        "private_framework_available",
        "A ReminderKit private-framework binary is present; runtime compatibility remains unproven.",
        details=details,
    )


def permission_state(status: str, code: str, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "code": code,
        "message": message,
        "prompt_attempted": False,
    }


def inspect_permission_symptoms(
    store_check: dict[str, Any],
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    store_code = store_check.get("code")
    if store_check.get("status") in {STATUS_OK, STATUS_WARNING}:
        full_disk = permission_state(
            STATUS_OK,
            "no_store_access_denial_symptom",
            "The group container and at least one schema were readable; this is "
            "not proof of a system-wide FDA grant.",
        )
    elif store_code in {
        "group_container_permission_denied",
        "stores_permission_denied",
    } or any(
        error.get("code") == "store_permission_denied"
        for error in store_check.get("errors", [])
    ):
        full_disk = permission_state(
            STATUS_BLOCKED,
            "store_access_denied_symptom",
            "A filesystem/SQLite denial is consistent with missing Full Disk Access or TCC permission.",
        )
    else:
        full_disk = permission_state(
            STATUS_UNKNOWN,
            "store_access_not_testable",
            "The local container was absent, so Full Disk Access could not be inferred.",
        )
    osascript = which("osascript")
    automation = permission_state(
        STATUS_UNKNOWN if osascript else STATUS_BLOCKED,
        "automation_not_probed_to_avoid_prompt" if osascript else "osascript_missing",
        (
            "Automation permission was intentionally not exercised because doing so may trigger a TCC prompt."
            if osascript
            else "osascript is unavailable, so AppleScript-backed operations cannot run."
        ),
    )
    reminders = permission_state(
        STATUS_UNKNOWN,
        "reminders_tcc_not_probed_to_avoid_prompt",
        "No EventKit, AppleScript, or Reminders process call was made, so "
        "runtime Reminders authorization is unknown.",
    )
    overall_status = (
        STATUS_BLOCKED
        if STATUS_BLOCKED in {full_disk["status"], automation["status"]}
        else STATUS_OK
    )
    return check_result(
        overall_status,
        "permission_symptoms_collected",
        "Permission symptoms were collected without triggering authorization prompts.",
        details={
            "tcc_prompt_attempted": False,
            "automation_process_invoked": False,
            "eventkit_invoked": False,
            "reminders_app_launched": False,
            "full_disk_access": full_disk,
            "automation": automation,
            "reminders": reminders,
        },
    )


def inspect_account_visibility(
    paths: dict[str, Any], store_check: dict[str, Any]
) -> dict[str, Any]:
    databases = store_check.get("details", {}).get("databases", [])
    counts = [
        item.get("account_metadata", {}).get("anonymous_row_count")
        for item in databases
    ]
    visible_counts = [int(count) for count in counts if count is not None]
    files_dir = paths["files"]
    attachment_account_directories: int | None = None
    files_error: OSError | None = None
    if files_dir.is_dir():
        entries, files_error = _directory_entries(files_dir)
        if files_error is None:
            attachment_account_directories = sum(
                entry.is_dir() and entry.name.startswith("Account-") for entry in entries
            )
    details = {
        "account_metadata": {
            "available_store_count": len(visible_counts),
            "anonymous_row_count_sum": sum(visible_counts) if visible_counts else None,
            "may_include_cross_store_duplicates": True,
            "names_read": False,
            "identifiers_read": False,
        },
        "attachment_account_directories": {
            "count": attachment_account_directories,
            "names_reported": False,
        },
        "icloud_sync": {
            "status": STATUS_UNKNOWN,
            "code": "icloud_sync_not_provable_statically",
            "message": "Local account metadata does not prove that iCloud sync is enabled or current.",
        },
        "reminder_content_read": False,
    }
    errors: list[dict[str, Any]] = []
    if files_error is not None:
        errors.append(
            structured_error(
                "account_files_unreadable",
                "Account attachment-directory metadata could not be enumerated.",
                exception=files_error,
            )
        )
    if visible_counts and sum(visible_counts) > 0:
        return check_result(
            STATUS_OK,
            "account_metadata_visible",
            "Anonymous local account metadata is visible; iCloud sync state remains unknown.",
            details=details,
            errors=errors,
        )
    return check_result(
        STATUS_UNKNOWN,
        "account_metadata_unavailable",
        "No anonymous account-row count could be confirmed without reading reminder content.",
        details=details,
        errors=errors,
    )


def _metadata_tree_size(path: Path, *, max_entries: int = 10_000) -> dict[str, Any]:
    if path.is_file() or path.is_symlink():
        return {
            "size_bytes": path.lstat().st_size,
            "entry_count": 1,
            "truncated": False,
        }
    total = 0
    count = 0
    truncated = False
    pending = [path]
    while pending:
        directory = pending.pop()
        entries, error = _directory_entries(directory)
        if error is not None:
            raise error
        for entry in entries:
            count += 1
            if count > max_entries:
                truncated = True
                pending.clear()
                break
            metadata = entry.lstat()
            total += metadata.st_size
            if stat.S_ISDIR(metadata.st_mode) and not entry.is_symlink():
                pending.append(entry)
    return {"size_bytes": total, "entry_count": count, "truncated": truncated}


def inspect_artifact(
    path: Path,
    *,
    home: Path,
    expected_mode: int,
    expected_kind: str,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "path": redacted_path(path, home),
        "path_redacted": True,
        "exists": path.exists() or path.is_symlink(),
        "expected_kind": expected_kind,
        "expected_mode": f"0o{expected_mode:o}",
        "content_read": False,
        "filenames_reported": False,
    }
    if not base["exists"]:
        base.update(
            {
                "status": STATUS_SKIPPED,
                "code": "not_created",
                "actual_kind": None,
                "actual_mode": None,
                "owner_only": None,
                "size_bytes": 0,
                "entry_count": 0,
                "errors": [],
            }
        )
        return base
    errors: list[dict[str, Any]] = []
    try:
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        actual_kind = (
            "symlink"
            if stat.S_ISLNK(metadata.st_mode)
            else "directory"
            if stat.S_ISDIR(metadata.st_mode)
            else "file"
        )
        size = _metadata_tree_size(path)
        owner_only = mode & 0o077 == 0
        mode_ok = actual_kind == expected_kind and mode == expected_mode
        base.update(
            {
                "status": STATUS_OK if mode_ok else STATUS_WARNING,
                "code": "artifact_private" if mode_ok else "artifact_permissions_too_broad",
                "actual_kind": actual_kind,
                "actual_mode": f"0o{mode:o}",
                "owner_only": owner_only,
                **size,
                "errors": errors,
            }
        )
        return base
    except OSError as exc:
        error = structured_error(
            "artifact_metadata_unreadable",
            "Local artifact metadata could not be read.",
            exception=exc,
        )
        base.update(
            {
                "status": STATUS_WARNING,
                "code": error["code"],
                "actual_kind": None,
                "actual_mode": None,
                "owner_only": None,
                "size_bytes": None,
                "entry_count": None,
                "errors": [error],
            }
        )
        return base


def inspect_local_artifacts(paths: dict[str, Any]) -> dict[str, Any]:
    home = paths["home"]
    definitions = {
        "app_support": (paths["app_support"], 0o700, "directory"),
        "journal": (paths["journal"], 0o600, "file"),
        "backups": (paths["backups"], 0o700, "directory"),
        "cache_directory": (paths["cache_dir"], 0o700, "directory"),
        "cache_file": (paths["cache_file"], 0o600, "file"),
        "helper_binary": (paths["helper_binary"], 0o700, "file"),
    }
    artifacts = {
        name: inspect_artifact(
            path, home=home, expected_mode=mode, expected_kind=kind
        )
        for name, (path, mode, kind) in definitions.items()
    }
    warnings = [
        name for name, artifact in artifacts.items() if artifact["status"] == STATUS_WARNING
    ]
    errors = [error for artifact in artifacts.values() for error in artifact["errors"]]
    return check_result(
        STATUS_WARNING if warnings else STATUS_OK,
        "artifact_metadata_collected" if not warnings else "artifact_privacy_warning",
        "Plugin-owned artifact paths, sizes, and permissions were inspected without reading contents.",
        details={
            "metadata_only": True,
            "content_read": False,
            "home_paths_redacted": True,
            "warning_artifacts": warnings,
            "artifacts": artifacts,
        },
        errors=errors,
    )


def inspect_redaction_contract(paths: dict[str, Any]) -> dict[str, Any]:
    source = paths["adapter_source"]
    details: dict[str, Any] = {
        "adapter_source": redacted_path(source, paths["home"]),
        "static_source_check_only": True,
        "journal_content_inspected": False,
        "cache_content_inspected": False,
        "backup_content_inspected": False,
        "checks": {
            "sensitive_key_filter": False,
            "recursive_payload_redaction": False,
            "bounded_journal_size": False,
            "journal_retention": False,
        },
    }
    try:
        source_text = source.read_text(encoding="utf-8")
    except OSError as exc:
        error = structured_error(
            "adapter_source_unreadable",
            "The adapter source could not be inspected for its logging-redaction contract.",
            exception=exc,
        )
        return check_result(
            STATUS_UNKNOWN,
            error["code"],
            error["message"],
            details=details,
            errors=[error],
        )
    details["checks"] = {
        "sensitive_key_filter": "SENSITIVE_LOG_KEY" in source_text,
        "recursive_payload_redaction": all(
            marker in source_text
            for marker in ("redacted_log_value", "redact_log_payload")
        ),
        "bounded_journal_size": "JOURNAL_MAX_BYTES" in source_text,
        "journal_retention": "JOURNAL_RETENTION_DAYS" in source_text,
    }
    details["source_bytes"] = len(source_text.encode("utf-8"))
    details["source_text_reported"] = False
    missing = [name for name, present in details["checks"].items() if not present]
    details["missing_static_controls"] = missing
    if missing:
        return check_result(
            STATUS_WARNING,
            "redaction_contract_incomplete",
            "One or more expected static logging/privacy controls were not found.",
            details=details,
        )
    return check_result(
        STATUS_OK,
        "redaction_contract_present",
        "Static logging redaction, rotation, and retention controls are present.",
        details=details,
    )


def derive_capabilities(checks: dict[str, Any]) -> dict[str, Any]:
    store_ready = checks["store_access"]["status"] in {STATUS_OK, STATUS_WARNING}
    command_details = checks["command_schema"].get("details", {}).get("commands", {})
    public_tool = checks["permissions"]["details"]["automation"]
    helper_ready = checks["helper_toolchain"]["status"] == STATUS_OK
    framework_check = checks["private_frameworks"]
    framework_items = framework_check.get("details", {}).get("frameworks", {})
    canonical_framework_paths_absent = bool(framework_items) and all(
        item.get("exists") is False for item in framework_items.values()
    )
    framework_statically_available = framework_check["status"] == STATUS_OK
    reminderkit_unknown = helper_ready and (
        framework_statically_available or canonical_framework_paths_absent
    )
    if not helper_ready:
        reminderkit_basis = "static_prerequisites_failed"
    elif framework_statically_available:
        reminderkit_basis = "static_prerequisites_passed_runtime_not_probed"
    elif canonical_framework_paths_absent:
        reminderkit_basis = (
            "canonical_framework_paths_absent_runtime_probe_required"
        )
    else:
        reminderkit_basis = "static_prerequisites_failed"
    return {
        "sqlite_schema_reads": {
            "status": STATUS_OK if store_ready else STATUS_BLOCKED,
            "basis": "content_free_schema_probe",
        },
        "sqlite_writes": {
            "status": STATUS_UNKNOWN if store_ready else STATUS_BLOCKED,
            "basis": "write_access_and_semantics_not_probed",
            "requires_runtime_verification": True,
        },
        "applescript_operations": {
            "status": public_tool["status"],
            "basis": public_tool["code"],
        },
        "reminderkit_image_attachments": {
            "status": STATUS_UNKNOWN if reminderkit_unknown else STATUS_BLOCKED,
            "basis": reminderkit_basis,
            "requires_runtime_verification": True,
        },
        "command_schema": {
            command: {
                "status": item["status"],
                "supported": item["supported"],
            }
            for command, item in command_details.items()
        },
    }


def _top_level_errors(checks: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for check_name, result in checks.items():
        for error in result.get("errors", []):
            errors.append({"check": check_name, **error})
    return errors


def _summary(checks: dict[str, Any]) -> dict[str, int]:
    return {
        status: sum(result.get("status") == status for result in checks.values())
        for status in sorted(CHECK_STATUSES)
    }


def collect_report(
    paths: dict[str, Any] | None = None,
    *,
    system_info: dict[str, Any] | None = None,
    syntax_check: bool = True,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[str]] = run_static_command,
    connector: Callable[..., sqlite3.Connection] = sqlite3.connect,
) -> dict[str, Any]:
    configured = paths or default_paths()
    platform_check = inspect_platform(system_info)
    app_check = inspect_reminders_app(configured["reminders_app_candidates"])
    store_check = inspect_store_access(configured, connector=connector)
    command_check = aggregate_command_schema(store_check)
    helper_check = inspect_helper_toolchain(
        configured, syntax_check=syntax_check, which=which, runner=runner
    )
    framework_check = inspect_private_frameworks(configured)
    permission_check = inspect_permission_symptoms(store_check, which=which)
    account_check = inspect_account_visibility(configured, store_check)
    artifacts_check = inspect_local_artifacts(configured)
    redaction_check = inspect_redaction_contract(configured)
    checks = {
        "platform": platform_check,
        "reminders_app": app_check,
        "store_access": store_check,
        "command_schema": command_check,
        "helper_toolchain": helper_check,
        "private_frameworks": framework_check,
        "permissions": permission_check,
        "account_visibility": account_check,
        "local_artifacts": artifacts_check,
        "redaction": redaction_check,
    }
    blocking_checks = {
        "platform",
        "reminders_app",
        "store_access",
        "command_schema",
        "permissions",
    }
    blocked = any(
        checks[name]["status"] == STATUS_BLOCKED for name in blocking_checks
    )
    degraded = any(
        result["status"] == STATUS_WARNING for result in checks.values()
    )
    overall_status = "blocked" if blocked else "degraded" if degraded else "ready"
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "doctor": DOCTOR_NAME,
        "ok": not blocked,
        "status": overall_status,
        "summary": _summary(checks),
        "privacy": {
            "content_free": True,
            "reminder_rows_read": False,
            "reminder_titles_read": False,
            "list_section_tag_names_read": False,
            "journal_cache_backup_contents_read": False,
            "write_attempted": False,
            "permission_prompt_attempted": False,
            "application_launched": False,
            "private_framework_loaded": False,
        },
        "checks": checks,
        "capabilities": derive_capabilities(checks),
        "errors": _top_level_errors(checks),
    }


def summarize_report(report: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for name, result in report.get("checks", {}).items():
        if not isinstance(result, dict):
            continue
        checks[name] = {
            key: result[key]
            for key in ("status", "code", "message")
            if key in result
        }
        if result.get("errors"):
            checks[name]["error_count"] = len(result["errors"])

    capabilities = dict(report.get("capabilities", {}))
    command_schema = capabilities.get("command_schema", {})
    if isinstance(command_schema, dict):
        supported = sum(
            isinstance(item, dict) and item.get("supported") is True
            for item in command_schema.values()
        )
        capabilities["command_schema"] = {
            "supported": supported,
            "blocked": len(command_schema) - supported,
            "total": len(command_schema),
        }

    errors = []
    for item in report.get("errors", []):
        if not isinstance(item, dict):
            continue
        errors.append(
            {
                key: item[key]
                for key in ("check", "code", "message")
                if key in item
            }
        )

    return {
        key: report[key]
        for key in ("schema_version", "doctor", "ok", "status", "summary", "privacy")
        if key in report
    } | {
        "detail_level": "summary",
        "checks": checks,
        "capabilities": capabilities,
        "errors": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Content-free Apple Reminders onboarding and capability gate"
    )
    parser.add_argument(
        "--skip-helper-syntax-check",
        action="store_true",
        help="Skip the non-linking clang syntax check (no executable is ever built).",
    )
    parser.add_argument(
        "--compact", action="store_true", help="Emit compact JSON instead of pretty JSON."
    )
    parser.add_argument(
        "--detail-level",
        choices=("summary", "full"),
        default="summary",
        help="Emit a concise readiness summary by default, or the full diagnostic report.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    full_report = collect_report(syntax_check=not args.skip_helper_syntax_check)
    report = (
        summarize_report(full_report)
        if args.detail_level == "summary"
        else {**full_report, "detail_level": "full"}
    )
    json.dump(
        report,
        sys.stdout,
        ensure_ascii=False,
        indent=None if args.compact else 2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0 if full_report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
