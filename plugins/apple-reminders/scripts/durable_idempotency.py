#!/usr/bin/env python3
"""Content-free durable idempotency for commit-capable local mutations.

The public interface intentionally exposes one operation. Hashing, retention,
locking, privacy projection, persistence, and replay stay inside this module so
callers cannot reorder the write-ahead fence protocol.
"""

from __future__ import annotations

import fcntl as _fcntl
import hashlib as _hashlib
import json as _json
import math as _math
import os as _os
import sys as _sys
import tempfile as _tempfile
import time as _time
import uuid as _uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in _sys.path:
    _sys.path.insert(0, str(_SCRIPT_DIR))

from receipt_contract import (  # noqa: E402
    AdapterError as _AdapterError,
    MutationNotStartedError as _MutationNotStartedError,
    RESULT_RECEIPT_STATUSES as _RESULT_RECEIPT_STATUSES,
    SUCCESS_RECEIPT_STATUSES as _SUCCESS_RECEIPT_STATUSES,
    build_operation_receipt as _build_operation_receipt,
)


__all__ = ["execute_idempotent"]


_DEFAULT_STORAGE_DIR = (
    Path.home() / "Library/Application Support/apple-reminders-codex"
)
_STORE_NAME = "idempotency.json"
_LOCK_NAME = "idempotency.lock"
_RETENTION_DAYS = 30
_MAX_ENTRIES = 500


def _new_operation_id() -> str:
    return str(_uuid.uuid4()).upper()


def _stable_hash(value: Any) -> str:
    return _hashlib.sha256(
        _json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _result_snapshot(
    value: Any,
    *,
    key: str | None = None,
    retain_sequence_scalars: bool = False,
) -> Any:
    """Keep retry-critical identifiers/status while excluding user content."""

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
                    "state",
                    "semantics",
                    "reason",
                    "verified",
                    "replayed",
                    "attachment_active",
                    "automatic_retry_safe",
                    "code",
                    "final_read",
                    "final_attachment_content_hash",
                    "final_attachment_content_matched",
                    "matched",
                    "mobile_visible_likely",
                    "reason_code",
                    "retryable",
                    "destination_attachment_active",
                    "destination_content_matched",
                    "destination_mobile_visible_likely",
                    "source_bytes_matched",
                    "source_unchanged",
                    "write_performed",
                }
                or normalized.endswith("_id")
                or normalized.endswith("_ids")
                or normalized.endswith("_pk")
                or normalized.endswith("_count")
            )
            if keep:
                result[str(item_key)] = _result_snapshot(
                    item,
                    key=str(item_key),
                    retain_sequence_scalars=normalized.endswith("_ids"),
                )
            elif isinstance(item, (dict, list, tuple)):
                nested = _result_snapshot(item, key=str(item_key))
                preserve_empty_receipt_object = (
                    isinstance(item, dict)
                    and normalized
                    in {"target", "before", "after", "verification", "recovery"}
                )
                if nested not in ({}, []) or preserve_empty_receipt_object:
                    result[str(item_key)] = nested
        return result
    if isinstance(value, (list, tuple)):
        result: list[Any] = []
        for item in value:
            if isinstance(item, dict):
                nested = _result_snapshot(item, key=key)
            elif isinstance(item, (list, tuple)):
                nested = _result_snapshot(
                    item,
                    key=key,
                    retain_sequence_scalars=retain_sequence_scalars,
                )
            elif retain_sequence_scalars:
                nested = item
            else:
                continue
            if nested not in ({}, []):
                result.append(nested)
        return result
    return value


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _load_store(store_path: Path) -> dict[str, Any]:
    try:
        with store_path.open("r", encoding="utf-8") as fh:
            payload = _json.load(fh)
    except FileNotFoundError:
        return {"version": 1, "entries": {}}
    except (OSError, _json.JSONDecodeError) as exc:
        raise _MutationNotStartedError(
            "The durable idempotency store could not be read safely.",
            code="unexpected_error",
            reason_code="idempotency_store_unreadable",
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), dict):
        raise _MutationNotStartedError(
            "The durable idempotency store has an unsupported structure.",
            code="unexpected_error",
            reason_code="idempotency_store_unreadable",
        )
    version = payload.get("version")
    if type(version) is not int or version != 1:
        raise _MutationNotStartedError(
            "The durable idempotency store has an unsupported version.",
            code="unexpected_error",
            reason_code="idempotency_store_unreadable",
        )
    return payload


def _result_replayable(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    status = value.get("status")
    if status not in _RESULT_RECEIPT_STATUSES:
        return False
    return value.get("ok") is (status in _SUCCESS_RECEIPT_STATUSES)


def _record_unresolved(value: Any) -> bool:
    if not isinstance(value, dict):
        return True
    state = value.get("state")
    stored_result = value.get("result")
    has_replayable_result = _result_replayable(stored_result)
    if state == "in_progress":
        return True
    if state == "complete":
        return not has_replayable_result
    if "state" not in value:
        # Legacy state-less v1 records are complete only when their final
        # Receipt is actually replayable.
        return not has_replayable_result
    # An unknown explicit state is an unresolved fence even if it happens to
    # carry a result object whose future semantics this runtime cannot know.
    return True


def _sanitize_completed_results(
    entries: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Scrub old complete snapshots without changing fence authorization."""

    changed = False
    sanitized: dict[str, Any] = {}
    for entry_key, value in entries.items():
        if not isinstance(value, dict) or value.get("state") == "in_progress":
            sanitized[entry_key] = value
            continue
        record = dict(value)
        stored_result = record.get("result")
        if _result_replayable(stored_result):
            safe_result = _result_snapshot(stored_result)
            if not _result_replayable(safe_result):
                record.pop("result", None)
                changed = True
            elif safe_result != stored_result:
                record["result"] = safe_result
                changed = True
        elif "result" in record:
            record.pop("result")
            changed = True
        sanitized[entry_key] = record
    return sanitized, changed


def _privacy_scrub_warning(exc: OSError) -> dict[str, Any]:
    return {
        "code": "idempotency_privacy_scrub_failed",
        "message": (
            "This replay was redacted, but older local retry content could not "
            "be scrubbed from disk."
        ),
        "detail": type(exc).__name__,
    }


def _entry_created_at_epoch(value: Any) -> float:
    if not isinstance(value, dict):
        raise _MutationNotStartedError(
            "The durable idempotency store has an unsupported entry.",
            code="unexpected_error",
            reason_code="idempotency_store_unreadable",
        )
    raw_created_at_epoch = value.get("created_at_epoch", 0)
    if isinstance(raw_created_at_epoch, bool) or not isinstance(
        raw_created_at_epoch,
        (int, float),
    ):
        raise _MutationNotStartedError(
            "The durable idempotency store has an invalid entry timestamp.",
            code="unexpected_error",
            reason_code="idempotency_store_unreadable",
        )
    try:
        created_at_epoch = float(raw_created_at_epoch)
    except OverflowError as exc:
        raise _MutationNotStartedError(
            "The durable idempotency store has an invalid entry timestamp.",
            code="unexpected_error",
            reason_code="idempotency_store_unreadable",
        ) from exc
    if not _math.isfinite(created_at_epoch):
        raise _MutationNotStartedError(
            "The durable idempotency store has an invalid entry timestamp.",
            code="unexpected_error",
            reason_code="idempotency_store_unreadable",
        )
    return created_at_epoch


def _prune_entries(
    entries: dict[str, Any],
    *,
    now: float | None = None,
    protected_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    for value in entries.values():
        _entry_created_at_epoch(value)
    current = now if now is not None else _time.time()
    cutoff = current - _RETENTION_DAYS * 86400
    retained = {
        key: value
        for key, value in entries.items()
        if (
            key in protected_keys
            or _entry_created_at_epoch(value) >= cutoff
        )
    }
    unresolved = sorted(
        (
            (key, value)
            for key, value in retained.items()
            if _record_unresolved(value)
        ),
        key=lambda item: _entry_created_at_epoch(item[1]),
        reverse=True,
    )
    replayable = sorted(
        (
            (key, value)
            for key, value in retained.items()
            if not _record_unresolved(value)
        ),
        key=lambda item: _entry_created_at_epoch(item[1]),
        reverse=True,
    )
    remaining_slots = max(0, _MAX_ENTRIES - len(unresolved))
    return dict([*unresolved, *replayable[:remaining_slots]])


def _write_store(
    payload: dict[str, Any],
    *,
    storage_dir: Path,
    store_path: Path,
) -> None:
    _ensure_private_dir(storage_dir)
    temp_handle = _tempfile.NamedTemporaryFile(
        prefix=".idempotency.",
        suffix=".tmp",
        dir=storage_dir,
        mode="w",
        encoding="utf-8",
        delete=False,
    )
    temp_path = Path(temp_handle.name)
    try:
        with temp_handle as fh:
            _json.dump(
                payload,
                fh,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            fh.flush()
            _os.fsync(fh.fileno())
        temp_path.chmod(0o600)
        _os.replace(temp_path, store_path)
        store_path.chmod(0o600)
        directory_fd = _os.open(storage_dir, _os.O_RDONLY)
        try:
            _os.fsync(directory_fd)
        finally:
            _os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)


def _outcome_unknown_receipt(
    operation: str,
    operation_id: str,
) -> dict[str, Any]:
    receipt_operation = {
        "eventkit_create_reminder": "create_reminder",
        "copy_image_attachment": "copy_image",
    }.get(operation, operation)
    message = (
        "A prior call crossed its durable idempotency fence, but its final "
        "Receipt was not persisted. Read the exact target before retrying."
    )
    return _build_operation_receipt(
        status="committed_verification_pending",
        operation=receipt_operation,
        operation_id=operation_id,
        backend="idempotency_fence",
        target={},
        before={},
        after={},
        verification={
            "state": "pending",
            "write_performed": None,
            "final_read": False,
        },
        recovery={
            "semantics": "read_before_retry",
            "automatic_retry_safe": False,
        },
        warnings=[
            {
                "code": "verification_pending",
                "message": message,
            }
        ],
        error={
            "code": "sync_pending",
            "reason_code": "idempotency_outcome_unknown",
            "message": message,
            "retryable": False,
        },
    )


def execute_idempotent(
    *,
    operation: str,
    key: str | None,
    input_payload: dict[str, Any],
    callback: Callable[[], dict[str, Any]],
    storage_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute once behind a content-free, process-safe durable fence."""

    if not key:
        return callback()

    support = storage_dir if storage_dir is not None else _DEFAULT_STORAGE_DIR
    store_path = support / _STORE_NAME
    lock_path = support / _LOCK_NAME
    _ensure_private_dir(support)
    key_hash = _stable_hash({"operation": operation, "key": key})
    input_hash = _stable_hash(input_payload)
    with lock_path.open("a+", encoding="utf-8") as lock:
        lock_path.chmod(0o600)
        _fcntl.flock(lock.fileno(), _fcntl.LOCK_EX)
        payload = _load_store(store_path)
        sanitized_entries, privacy_scrub_required = _sanitize_completed_results(
            payload.get("entries", {})
        )
        entries = _prune_entries(
            sanitized_entries,
            protected_keys=frozenset({key_hash}),
        )
        if key_hash in entries:
            record = entries[key_hash]
            privacy_warning: dict[str, Any] | None = None
            if privacy_scrub_required:
                try:
                    _write_store(
                        {"version": 1, "entries": entries},
                        storage_dir=support,
                        store_path=store_path,
                    )
                except OSError as exc:
                    privacy_warning = _privacy_scrub_warning(exc)
            if record.get("input_hash") != input_hash:
                raise _MutationNotStartedError(
                    "Idempotency key was already used with different input",
                    code="concurrent_modification",
                    operation=operation,
                )
            stored_result = record.get("result")
            if (
                (record.get("state") == "complete" or "state" not in record)
                and _result_replayable(stored_result)
            ):
                replay = dict(stored_result)
            else:
                operation_id = record.get("operation_id")
                if not isinstance(operation_id, str) or not operation_id:
                    operation_id = _new_operation_id()
                replay = _outcome_unknown_receipt(operation, operation_id)
            if privacy_warning is not None:
                warnings = replay.get("warnings")
                if not isinstance(warnings, list):
                    warnings = []
                    replay["warnings"] = warnings
                warnings.append(privacy_warning)
            replay["replayed"] = True
            replay["idempotency_key_hash"] = key_hash
            return replay

        while len(entries) >= _MAX_ENTRIES:
            replayable_keys = [
                entry_key
                for entry_key, entry in entries.items()
                if not _record_unresolved(entry)
            ]
            if not replayable_keys:
                raise _MutationNotStartedError(
                    "The durable idempotency fence capacity is occupied by unresolved operations.",
                    code="unexpected_error",
                    reason_code="idempotency_capacity_exhausted",
                )
            oldest_key = min(
                replayable_keys,
                key=lambda item: _entry_created_at_epoch(entries[item]),
            )
            entries.pop(oldest_key)

        created_at_epoch = _time.time()
        fence_operation_id = _new_operation_id()
        entries[key_hash] = {
            "operation": operation,
            "input_hash": input_hash,
            "created_at_epoch": created_at_epoch,
            "state": "in_progress",
            "operation_id": fence_operation_id,
        }
        try:
            _write_store(
                {
                    "version": 1,
                    "entries": _prune_entries(
                        entries,
                        protected_keys=frozenset({key_hash}),
                    ),
                },
                storage_dir=support,
                store_path=store_path,
            )
        except OSError as exc:
            raise _MutationNotStartedError(
                "The idempotency fence could not be persisted before dispatch.",
                code="unexpected_error",
                reason_code="idempotency_fence_write_failed",
            ) from exc

        try:
            result = callback()
        except _AdapterError as exc:
            if not isinstance(exc, _MutationNotStartedError):
                raise
            entries.pop(key_hash, None)
            try:
                _write_store(
                    {"version": 1, "entries": _prune_entries(entries)},
                    storage_dir=support,
                    store_path=store_path,
                )
            except OSError as cleanup_exc:
                # The callback proved no write, but cleanup could not be
                # persisted. Keep the on-disk fence fail-closed.
                raise _MutationNotStartedError(
                    "The no-write callback failed and its idempotency fence could not be cleared.",
                    code="unexpected_error",
                    reason_code="idempotency_fence_cleanup_failed",
                ) from cleanup_exc
            raise

        entries[key_hash] = {
            "operation": operation,
            "input_hash": input_hash,
            "created_at_epoch": created_at_epoch,
            "state": "complete",
            "operation_id": fence_operation_id,
            "result": _result_snapshot(result),
        }
        try:
            _write_store(
                {
                    "version": 1,
                    "entries": _prune_entries(
                        entries,
                        protected_keys=frozenset({key_hash}),
                    ),
                },
                storage_dir=support,
                store_path=store_path,
            )
        except OSError as exc:
            result.setdefault("warnings", []).append(
                {
                    "code": "idempotency_receipt_write_failed",
                    "message": (
                        "The mutation completed, but its local retry receipt "
                        "could not be persisted."
                    ),
                    "detail": type(exc).__name__,
                }
            )
        result["idempotency_key_hash"] = key_hash
        return result
