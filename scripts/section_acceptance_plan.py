#!/usr/bin/env python3
"""Prepare section acceptance evidence; never authorize or dispatch mutations.

Default: --account-id EXACT emits a plan with expected (not observed) OS/app
identity, a caller-selected (not ownership-verified) account, and no object IDs.
--collect-schema --plan FILE --schema-snapshot FILE reads only fixed schema
PRAGMAs from an explicitly supplied standalone SQLite snapshot. It never locates
or copies a live Reminders database. Output is private preparatory evidence,
not runtime admission, a compatibility promotion, or mutation authority.
Keep output outside the repository: plans include the supplied account selector.
Snapshot bytes are hashed for identity; only table/column metadata is queried.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/apple-reminders/scripts"
sys.path.insert(0, str(SCRIPTS))
import native_helper
from experimental_capabilities import detect_runtime_identity
from reminders_contracts import command_schema_requirements

EXPECTED_RUNTIME = dict(macos_version="26.5.2", macos_build="25F84", reminders_version="7.0", reminders_build="3976")
COMMANDS = ("create_section_db", "move_to_section_db")
MAX_JSON_BYTES = 65536
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_COLUMNS = 256
HEX = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class PlanError(ValueError):
    pass


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def bounded_file(path: Path, limit: int) -> bytes:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise PlanError("An exact absolute regular file is required")
    if path.stat().st_size > limit:
        raise PlanError("Input exceeds the collection bound")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise PlanError("Input exceeds the collection bound")
    return data


def read_json(path: Path) -> dict:
    value = json.loads(bounded_file(path, MAX_JSON_BYTES))
    if not isinstance(value, dict):
        raise PlanError("Expected a JSON object")
    return value


def static_contracts() -> dict:
    contracts = command_schema_requirements("runtime")
    return {command: {"required_tables": {table: sorted(columns) for table, columns in sorted(contracts[command].items())},
                      "requirements_sha256": digest({table: sorted(columns) for table, columns in contracts[command].items()})}
            for command in COMMANDS}


def verified_helper_binding() -> dict:
    # Production resolver verifies signatures and bytes; it never launches a
    # helper, probes a compiler, or opens a store. No fallback is permitted here.
    native_helper.resolve_helper("sections")
    path = native_helper.PLUGIN_ROOT / "native/native-helper-build.json"
    raw = bounded_file(path, MAX_JSON_BYTES)
    manifest = json.loads(raw)
    binding = {key: manifest[key] for key in ("plugin_version", "source_commit", "workflow_commit", "source_files", "binary_sha256")}
    binding["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    validate_helper_binding(binding)
    # Bind the manifest read to a second complete resolution as well.
    native_helper.resolve_helper("sections")
    if bounded_file(path, MAX_JSON_BYTES) != raw:
        raise PlanError("Native provenance changed during collection")
    return binding


def validate_helper_binding(value: dict) -> None:
    expected = {"plugin_version", "source_commit", "workflow_commit", "source_files", "binary_sha256", "manifest_sha256"}
    if not isinstance(value, dict) or set(value) != expected:
        raise PlanError("Invalid helper binding")
    if not isinstance(value["plugin_version"], str) or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value["plugin_version"]):
        raise PlanError("Invalid helper version")
    for key in ("source_commit", "workflow_commit"):
        if not isinstance(value[key], str) or not COMMIT.fullmatch(value[key]):
            raise PlanError("Invalid helper commit")
    if not isinstance(value["manifest_sha256"], str) or not HEX.fullmatch(value["manifest_sha256"]):
        raise PlanError("Invalid helper manifest hash")
    for key, names in (("source_files", set(native_helper.SOURCES)), ("binary_sha256", set(native_helper.EXECUTABLES))):
        hashes = value[key]
        if not isinstance(hashes, dict) or set(hashes) != names or any(not isinstance(v, str) or not HEX.fullmatch(v) for v in hashes.values()):
            raise PlanError("Invalid helper source or binary binding")


def make_plan(account_id: str, helper: dict, *, nonce: str | None = None) -> dict:
    if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", account_id):
        raise PlanError("One explicit bounded account selector is required")
    validate_helper_binding(helper)
    nonce = nonce or str(uuid.uuid4())
    try:
        parsed = uuid.UUID(nonce)
    except (ValueError, TypeError, AttributeError) as exc:
        raise PlanError("Invalid plan nonce") from exc
    if parsed.version != 4 or str(parsed) != nonce:
        raise PlanError("Invalid plan nonce")
    return {"schema_version": 1, "kind": "section_acceptance_preparation",
            "nonce": nonce, "expected_runtime": dict(EXPECTED_RUNTIME),
            "runtime_observed": False, "account_selection": {"id": account_id, "ownership_verified": False},
            "helper_binding": helper, "contracts": static_contracts(),
            "object_registry": {"list_ids": [], "reminder_ids": [], "section_ids": []},
            "permitted_operation": "collect_schema_snapshot", "mutation_authority": False,
            "runtime_admission": False}


def validate_plan(plan: dict, helper: dict) -> None:
    try:
        expected = make_plan(plan["account_selection"]["id"], helper, nonce=plan["nonce"])
    except (KeyError, TypeError) as exc:
        raise PlanError("Malformed plan") from exc
    # Exact shape/value comparison rejects injected IDs, extra operations,
    # ownership claims, altered contracts and stale artifact bindings.
    if digest(plan) != digest(expected):
        raise PlanError("Plan drift or unsupported authority")


def observed_runtime() -> dict:
    identity = detect_runtime_identity()
    return {key: getattr(identity, key) for key in EXPECTED_RUNTIME}


def collect_schema(plan: dict, snapshot: Path, *, helper: dict, runtime: dict) -> dict:
    validate_plan(plan, helper)
    if runtime != plan["expected_runtime"]:
        raise PlanError("Observed OS/app does not match the expected identity")
    before = bounded_file(snapshot, MAX_SNAPSHOT_BYTES)
    sidecars = [Path(str(snapshot) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    if any(path.exists() or path.is_symlink() for path in sidecars):
        raise PlanError("Only a standalone schema snapshot without sidecars is supported")
    tables = set().union(*(item["required_tables"] for item in plan["contracts"].values()))
    schema = {}
    # immutable=1 is appropriate only for the explicit standalone snapshot;
    # unlike a live WAL reader, this connection cannot create shared-memory files.
    connection = sqlite3.connect(f"file:{quote(str(snapshot))}?mode=ro&immutable=1", uri=True, timeout=1)
    try:
        connection.set_authorizer(lambda action, first, second, db, trigger:
            sqlite3.SQLITE_OK if action == sqlite3.SQLITE_PRAGMA and first == "table_info" and second in tables else sqlite3.SQLITE_DENY)
        deadline = time.monotonic() + 2
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        for table in sorted(tables):
            rows = connection.execute(f'PRAGMA table_info("{table}")').fetchmany(MAX_COLUMNS + 1)
            if len(rows) > MAX_COLUMNS:
                raise PlanError("Schema exceeds the column bound")
            columns = [row[1] for row in rows]
            if any(not isinstance(column, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", column) for column in columns):
                raise PlanError("Invalid schema column metadata")
            if columns:
                schema[table] = sorted(columns)
    finally:
        connection.close()
    if any(path.exists() or path.is_symlink() for path in sidecars) or bounded_file(snapshot, MAX_SNAPSHOT_BYTES) != before:
        raise PlanError("Schema snapshot changed during collection")
    observations = {}
    for command, contract in plan["contracts"].items():
        requirements = contract["required_tables"]
        projection = {table: schema[table] for table in requirements if table in schema}
        observations[command] = {"projection": projection, "schema_fingerprint": digest(projection),
            "minimum_fields_present": all(set(columns) <= set(schema.get(table, [])) for table, columns in requirements.items())}
    return {"schema_version": 1, "kind": "section_schema_snapshot_evidence", "plan_sha256": digest(plan),
            "observed_runtime": runtime, "helper_binding": helper, "snapshot_sha256": hashlib.sha256(before).hexdigest(),
            "snapshot_origin_verified": False, "account_ownership_verified": False,
            "observations": observations, "reminder_rows_read": False, "account_rows_read": False,
            "mutation_attempted": False, "runtime_admission": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id")
    parser.add_argument("--collect-schema", action="store_true")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--schema-snapshot", type=Path)
    args = parser.parse_args(argv)
    if args.collect_schema:
        if args.account_id or not args.plan or not args.schema_snapshot:
            parser.error("collection requires only --plan and --schema-snapshot")
    elif not args.account_id or args.plan or args.schema_snapshot:
        parser.error("plan-only mode requires only --account-id")
    try:
        plan = read_json(args.plan) if args.collect_schema else None
        helper = verified_helper_binding()
        result = collect_schema(plan, args.schema_snapshot, helper=helper, runtime=observed_runtime()) if args.collect_schema else make_plan(args.account_id, helper)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (PlanError, native_helper.NativeHelperUnavailable, OSError, sqlite3.Error, ValueError, KeyError):
        print("Section preparation failed; no mutation was attempted. Check explicit inputs and artifact/identity bindings.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
