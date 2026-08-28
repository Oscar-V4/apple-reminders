from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
SCRIPTS_ROOT = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))

import durable_idempotency  # noqa: E402
from receipt_contract import (  # noqa: E402
    AdapterError,
    MutationNotStartedError,
    build_operation_receipt,
)


STORE_NAME = "idempotency.json"
LOCK_NAME = "idempotency.lock"
MAX_ENTRIES = 500
RETENTION_DAYS = 30
FIXED_RECEIPT_OPERATION_ID = "11111111-1111-4111-8111-111111111111"

PROCESS_LOCK_WORKER = r"""
import json
import sys
import time
from pathlib import Path

scripts_root = Path(sys.argv[1])
storage_dir = Path(sys.argv[2])
role = sys.argv[3]
started_path = Path(sys.argv[4])
blocked_path = Path(sys.argv[5])
escaped_path = Path(sys.argv[6])
release_path = Path(sys.argv[7])
sys.path.insert(0, str(scripts_root))

import durable_idempotency

if role == "follower":
    real_flock = durable_idempotency._fcntl.flock

    def observed_flock(file_descriptor, operation):
        try:
            real_flock(
                file_descriptor,
                operation | durable_idempotency._fcntl.LOCK_NB,
            )
        except BlockingIOError:
            blocked_path.write_text("blocked", encoding="utf-8")
            return real_flock(file_descriptor, operation)
        escaped_path.write_text("escaped", encoding="utf-8")

    durable_idempotency._fcntl.flock = observed_flock

def callback():
    if role != "leader":
        raise RuntimeError("the follower callback was dispatched")
    started_path.write_text("started", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not release_path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("leader release timed out")
        time.sleep(0.01)
    return {
        "ok": True,
        "status": "verified",
        "operation": "create_reminder",
        "operation_id": "33333333-3333-4333-8333-333333333333",
        "backend": "process_lock_test",
        "target": {"id": "R-1"},
    }

result = durable_idempotency.execute_idempotent(
    operation="eventkit_create_reminder",
    key="process-lock-key",
    input_payload={"title": "Synthetic process lock"},
    callback=callback,
    storage_dir=storage_dir,
)
print(json.dumps({
    "replayed": result.get("replayed", False),
    "status": result.get("status"),
}, sort_keys=True))
"""


def stable_hash(value: Any) -> str:
    """Independent golden implementation of the persisted SHA-256 contract."""

    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def verified_receipt(
    *,
    operation: str = "create_reminder",
    backend: str = "eventkit_public_sdk",
    target: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    recovery: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return build_operation_receipt(
        status="verified",
        operation=operation,
        operation_id=FIXED_RECEIPT_OPERATION_ID,
        backend=backend,
        target=target or {},
        verification=verification or {
            "state": "read_back",
            "write_performed": True,
            "final_read": True,
            "matched": True,
        },
        recovery=recovery or {
            "semantics": "not_applicable",
            "automatic_retry_safe": False,
        },
        **extra,
    )


def read_store(storage_dir: Path) -> dict[str, Any]:
    return json.loads((storage_dir / STORE_NAME).read_text(encoding="utf-8"))


def seed_store(storage_dir: Path, payload: dict[str, Any]) -> None:
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_dir.chmod(0o700)
    store = storage_dir / STORE_NAME
    store.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    store.chmod(0o600)


def unresolved_record(
    *,
    operation: str,
    input_hash: str,
    created_at_epoch: float,
    operation_id: str = "22222222-2222-4222-8222-222222222222",
) -> dict[str, Any]:
    return {
        "operation": operation,
        "input_hash": input_hash,
        "created_at_epoch": created_at_epoch,
        "state": "in_progress",
        "operation_id": operation_id,
    }


def wait_for_path(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {path.name}")
        time.sleep(0.01)


def wait_for_any(paths: tuple[Path, ...], *, timeout: float = 5.0) -> Path:
    deadline = time.monotonic() + timeout
    while True:
        for path in paths:
            if path.exists():
                return path
        if time.monotonic() >= deadline:
            names = ", ".join(path.name for path in paths)
            raise AssertionError(f"Timed out waiting for one of: {names}")
        time.sleep(0.01)


def reap_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=2)


class DurableIdempotencyContractTests(unittest.TestCase):
    def test_repeated_key_replays_once_and_store_contains_no_user_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            calls = 0

            def callback() -> dict[str, Any]:
                nonlocal calls
                calls += 1
                return verified_receipt(
                    backend="db",
                    target={
                        "id": "R-1",
                        "title": "Sensitive title",
                        "list": "Private list",
                    },
                )

            first = durable_idempotency.execute_idempotent(
                operation="create_reminder",
                key="request-1",
                input_payload={"title": "Sensitive title"},
                callback=callback,
                storage_dir=storage_dir,
            )
            second = durable_idempotency.execute_idempotent(
                operation="create_reminder",
                key="request-1",
                input_payload={"title": "Sensitive title"},
                callback=callback,
                storage_dir=storage_dir,
            )
            stored = (storage_dir / STORE_NAME).read_text(encoding="utf-8")

        self.assertEqual(calls, 1)
        self.assertFalse(first.get("replayed", False))
        self.assertTrue(second["replayed"])
        self.assertEqual(second["target"]["id"], "R-1")
        self.assertNotIn("Sensitive title", stored)
        self.assertNotIn("Private list", stored)
        self.assertNotIn("request-1", stored)

    def test_scalar_sequences_and_recurrence_content_are_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            callback = mock.Mock(
                return_value=verified_receipt(
                    target={"reminder_id": "R-1"},
                    after={
                        "reminder_id": "R-1",
                        "source_attachment_ids": ["A-1", "A-2"],
                        "recurrence_rules": [
                            {
                                "frequency": "monthly",
                                "days_of_month": [5, 20],
                                "months_of_year": [1, 7],
                                "end": {"count": 12},
                            }
                        ],
                    },
                    verification={
                        "state": "read_back",
                        "write_performed": True,
                        "final_read": True,
                        "matched": True,
                        "target_fields": ["title", "recurrence_rules"],
                    },
                    warnings=["Private warning text"],
                )
            )
            first = durable_idempotency.execute_idempotent(
                operation="eventkit_create_reminder",
                key="recurrence-redaction",
                input_payload={"title": "Private schedule"},
                callback=callback,
                storage_dir=storage_dir,
            )
            replay = durable_idempotency.execute_idempotent(
                operation="eventkit_create_reminder",
                key="recurrence-redaction",
                input_payload={"title": "Private schedule"},
                callback=callback,
                storage_dir=storage_dir,
            )
            record = next(iter(read_store(storage_dir)["entries"].values()))
            snapshot = record["result"]

        callback.assert_called_once_with()
        self.assertIn("recurrence_rules", first["after"])
        self.assertEqual(
            snapshot["after"],
            {
                "reminder_id": "R-1",
                "source_attachment_ids": ["A-1", "A-2"],
            },
        )
        self.assertNotIn("target_fields", snapshot["verification"])
        self.assertNotIn("warnings", snapshot)
        self.assertEqual(replay["after"], snapshot["after"])
        self.assertTrue(replay["replayed"])

    def test_replay_scrubs_modern_and_legacy_complete_records_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            current_input = {"title": "Current"}
            legacy_input = {"title": "Legacy"}
            current_key = stable_hash(
                {"operation": "eventkit_create_reminder", "key": "current-key"}
            )
            legacy_key = stable_hash(
                {"operation": "eventkit_create_reminder", "key": "legacy-key"}
            )
            current_metadata = {
                "operation": "eventkit_create_reminder",
                "input_hash": stable_hash(current_input),
                "created_at_epoch": time.time(),
                "state": "complete",
                "operation_id": "44444444-4444-4444-8444-444444444444",
            }
            legacy_metadata = {
                "operation": "eventkit_create_reminder",
                "input_hash": stable_hash(legacy_input),
                "created_at_epoch": time.time(),
                "operation_id": "55555555-5555-4555-8555-555555555555",
            }
            private_result = verified_receipt(
                target={"reminder_id": "R-1"},
                after={
                    "reminder_id": "R-1",
                    "recurrence_rules": [
                        {"days_of_month": [5, 20], "end": {"count": 12}}
                    ],
                },
            )
            seed_store(
                storage_dir,
                {
                    "version": 1,
                    "entries": {
                        current_key: {**current_metadata, "result": private_result},
                        legacy_key: {**legacy_metadata, "result": private_result},
                    },
                },
            )
            callback = mock.Mock(return_value=verified_receipt())

            replay = durable_idempotency.execute_idempotent(
                operation="eventkit_create_reminder",
                key="current-key",
                input_payload=current_input,
                callback=callback,
                storage_dir=storage_dir,
            )
            legacy_replay = durable_idempotency.execute_idempotent(
                operation="eventkit_create_reminder",
                key="legacy-key",
                input_payload=legacy_input,
                callback=callback,
                storage_dir=storage_dir,
            )
            stored = read_store(storage_dir)["entries"]

        callback.assert_not_called()
        self.assertTrue(replay["replayed"])
        self.assertNotIn("recurrence_rules", replay["after"])
        self.assertTrue(legacy_replay["replayed"])
        self.assertEqual(legacy_replay["status"], "verified")
        self.assertEqual(
            {key: stored[current_key][key] for key in current_metadata},
            current_metadata,
        )
        self.assertEqual(
            {key: stored[legacy_key][key] for key in legacy_metadata},
            legacy_metadata,
        )
        self.assertNotIn("state", stored[legacy_key])
        for record in stored.values():
            self.assertNotIn("recurrence_rules", record["result"]["after"])

    def test_legacy_record_without_state_or_result_replays_outcome_unknown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            input_payload = {"title": "Legacy pending"}
            key_hash = stable_hash(
                {"operation": "eventkit_create_reminder", "key": "legacy-pending"}
            )
            seed_store(
                storage_dir,
                {
                    "version": 1,
                    "entries": {
                        key_hash: {
                            "operation": "eventkit_create_reminder",
                            "input_hash": stable_hash(input_payload),
                            "created_at_epoch": time.time(),
                        }
                    },
                },
            )
            callback = mock.Mock(return_value=verified_receipt())

            replay = durable_idempotency.execute_idempotent(
                operation="eventkit_create_reminder",
                key="legacy-pending",
                input_payload=input_payload,
                callback=callback,
                storage_dir=storage_dir,
            )

        callback.assert_not_called()
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "committed_verification_pending")
        self.assertEqual(
            replay["error"]["reason_code"],
            "idempotency_outcome_unknown",
        )

    def test_corrupt_complete_results_replay_outcome_unknown_without_dispatch(
        self,
    ) -> None:
        missing = object()
        cases: tuple[tuple[str, Any], ...] = (
            ("string", "corrupt"),
            ("list", ["private"]),
            ("null", None),
            ("missing", missing),
            ("empty_object", {}),
        )
        for name, stored_result in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                storage_dir = Path(temp_dir) / "support"
                input_payload = {"title": "Corrupt complete result"}
                key_hash = stable_hash(
                    {
                        "operation": "eventkit_create_reminder",
                        "key": "corrupt-complete",
                    }
                )
                record: dict[str, Any] = {
                    "operation": "eventkit_create_reminder",
                    "input_hash": stable_hash(input_payload),
                    "created_at_epoch": time.time(),
                    "state": "complete",
                    "operation_id": "12121212-1212-4212-8212-121212121212",
                }
                if stored_result is not missing:
                    record["result"] = stored_result
                seed_store(
                    storage_dir,
                    {"version": 1, "entries": {key_hash: record}},
                )
                callback = mock.Mock(return_value=verified_receipt())

                replay = durable_idempotency.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="corrupt-complete",
                    input_payload=input_payload,
                    callback=callback,
                    storage_dir=storage_dir,
                )
                stored = read_store(storage_dir)["entries"][key_hash]

                callback.assert_not_called()
                self.assertTrue(replay["replayed"])
                self.assertEqual(
                    replay["status"],
                    "committed_verification_pending",
                )
                self.assertEqual(
                    replay["operation_id"],
                    "12121212-1212-4212-8212-121212121212",
                )
                self.assertEqual(
                    replay["error"]["reason_code"],
                    "idempotency_outcome_unknown",
                )
                expected_record = dict(record)
                if "result" in expected_record and not expected_record["result"]:
                    expected_record.pop("result")
                elif "result" in expected_record and not isinstance(
                    expected_record["result"], dict
                ):
                    expected_record.pop("result")
                self.assertEqual(stored, expected_record)

    def test_corrupt_non_current_result_is_scrubbed_before_new_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            old_key_hash = stable_hash(
                {
                    "operation": "eventkit_create_reminder",
                    "key": "old-complete",
                }
            )
            old_record = {
                "operation": "eventkit_create_reminder",
                "input_hash": stable_hash({"title": "Old"}),
                "created_at_epoch": time.time(),
                "state": "complete",
                "operation_id": "14141414-1414-4414-8414-141414141414",
                "result": ["PRIVATE-LEGACY-ARRAY"],
            }
            seed_store(
                storage_dir,
                {"version": 1, "entries": {old_key_hash: old_record}},
            )
            callback = mock.Mock(return_value=verified_receipt())

            result = durable_idempotency.execute_idempotent(
                operation="eventkit_create_reminder",
                key="new-request",
                input_payload={"title": "New"},
                callback=callback,
                storage_dir=storage_dir,
            )
            payload = read_store(storage_dir)

        callback.assert_called_once_with()
        self.assertEqual(result["status"], "verified")
        self.assertNotIn("result", payload["entries"][old_key_hash])
        self.assertNotIn("PRIVATE-LEGACY-ARRAY", json.dumps(payload))
        self.assertEqual(len(payload["entries"]), 2)

    def test_complete_non_empty_result_replays_stored_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            input_payload = {"title": "Valid complete result"}
            key_hash = stable_hash(
                {
                    "operation": "eventkit_create_reminder",
                    "key": "valid-complete",
                }
            )
            result = verified_receipt(target={"reminder_id": "R-VALID"})
            record = {
                "operation": "eventkit_create_reminder",
                "input_hash": stable_hash(input_payload),
                "created_at_epoch": time.time(),
                "state": "complete",
                "operation_id": "13131313-1313-4313-8313-131313131313",
                "result": result,
            }
            seed_store(
                storage_dir,
                {"version": 1, "entries": {key_hash: record}},
            )
            callback = mock.Mock(return_value=verified_receipt())

            replay = durable_idempotency.execute_idempotent(
                operation="eventkit_create_reminder",
                key="valid-complete",
                input_payload=input_payload,
                callback=callback,
                storage_dir=storage_dir,
            )

        callback.assert_not_called()
        self.assertEqual(replay["status"], "verified")
        self.assertEqual(replay["target"], {"reminder_id": "R-VALID"})
        self.assertTrue(replay["replayed"])

    def test_existing_replay_stays_redacted_when_privacy_scrub_write_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            input_payload = {"title": "Existing"}
            key_hash = stable_hash(
                {"operation": "eventkit_create_reminder", "key": "existing-key"}
            )
            record = {
                "operation": "eventkit_create_reminder",
                "input_hash": stable_hash(input_payload),
                "created_at_epoch": time.time(),
                "state": "complete",
                "operation_id": "66666666-6666-4666-8666-666666666666",
                "result": verified_receipt(
                    target={"reminder_id": "R-1"},
                    after={
                        "reminder_id": "R-1",
                        "recurrence_rules": [{"days_of_month": [1, 15]}],
                    },
                ),
            }
            seed_store(
                storage_dir,
                {"version": 1, "entries": {key_hash: record}},
            )
            callback = mock.Mock(return_value=verified_receipt())
            with mock.patch.object(
                durable_idempotency,
                "_write_store",
                side_effect=OSError("read-only volume"),
            ):
                replay = durable_idempotency.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="existing-key",
                    input_payload=input_payload,
                    callback=callback,
                    storage_dir=storage_dir,
                )
            unchanged = read_store(storage_dir)["entries"][key_hash]

        callback.assert_not_called()
        self.assertNotIn("recurrence_rules", replay["after"])
        self.assertEqual(
            replay["warnings"][-1]["code"],
            "idempotency_privacy_scrub_failed",
        )
        self.assertEqual(unchanged, record)

    def test_new_key_does_not_dispatch_when_combined_scrub_and_fence_write_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            old_key = stable_hash(
                {"operation": "eventkit_create_reminder", "key": "old-key"}
            )
            seed_store(
                storage_dir,
                {
                    "version": 1,
                    "entries": {
                        old_key: {
                            "operation": "eventkit_create_reminder",
                            "input_hash": stable_hash({"title": "Old"}),
                            "created_at_epoch": time.time(),
                            "state": "complete",
                            "operation_id": "77777777-7777-4777-8777-777777777777",
                            "result": verified_receipt(
                                after={
                                    "reminder_id": "R-OLD",
                                    "recurrence_rules": [
                                        {"days_of_month": [3, 17]}
                                    ],
                                }
                            ),
                        }
                    },
                },
            )
            callback = mock.Mock(return_value=verified_receipt())
            with (
                mock.patch.object(
                    durable_idempotency,
                    "_write_store",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaises(MutationNotStartedError) as raised,
            ):
                durable_idempotency.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="new-key",
                    input_payload={"title": "New"},
                    callback=callback,
                    storage_dir=storage_dir,
                )

        callback.assert_not_called()
        self.assertEqual(
            raised.exception.details["reason_code"],
            "idempotency_fence_write_failed",
        )

    def test_in_progress_record_is_never_rewritten_or_redispatched_by_scrub(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            input_payload = {"title": "Pending"}
            key_hash = stable_hash(
                {"operation": "eventkit_create_reminder", "key": "pending-key"}
            )
            record = {
                "operation": "eventkit_create_reminder",
                "input_hash": stable_hash(input_payload),
                "created_at_epoch": time.time(),
                "state": "in_progress",
                "operation_id": "88888888-8888-4888-8888-888888888888",
                "result": {
                    "after": {"recurrence_rules": [{"days_of_month": [9]}]}
                },
            }
            seed_store(
                storage_dir,
                {"version": 1, "entries": {key_hash: record}},
            )
            callback = mock.Mock(return_value=verified_receipt())

            replay = durable_idempotency.execute_idempotent(
                operation="eventkit_create_reminder",
                key="pending-key",
                input_payload=input_payload,
                callback=callback,
                storage_dir=storage_dir,
            )
            unchanged = read_store(storage_dir)["entries"][key_hash]

        callback.assert_not_called()
        self.assertEqual(replay["status"], "committed_verification_pending")
        self.assertNotIn("recurrence_rules", repr(replay))
        self.assertEqual(unchanged, record)

    def test_reusing_key_with_different_input_fails_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            durable_idempotency.execute_idempotent(
                operation="create_reminder",
                key="request-1",
                input_payload={"title": "A"},
                callback=lambda: {"ok": True, "status": "verified", "id": "R-1"},
                storage_dir=storage_dir,
            )
            callback = mock.Mock(
                return_value={"ok": True, "status": "verified", "id": "R-2"}
            )
            with self.assertRaises(MutationNotStartedError) as raised:
                durable_idempotency.execute_idempotent(
                    operation="create_reminder",
                    key="request-1",
                    input_payload={"title": "B"},
                    callback=callback,
                    storage_dir=storage_dir,
                )

        callback.assert_not_called()
        self.assertEqual(raised.exception.code, "concurrent_modification")
        self.assertTrue(raised.exception.details["mutation_not_started"])

    def test_in_progress_fence_prevents_redispatch_when_final_receipt_write_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            calls = 0
            writes = 0
            real_write = durable_idempotency._write_store

            def callback() -> dict[str, Any]:
                nonlocal calls
                calls += 1
                return verified_receipt(
                    target={"id": f"R-{calls}", "title": "Sensitive title"}
                )

            def fail_final_write(
                payload: dict[str, Any],
                *,
                storage_dir: Path,
                store_path: Path,
            ) -> None:
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("disk full")
                real_write(
                    payload,
                    storage_dir=storage_dir,
                    store_path=store_path,
                )

            with mock.patch.object(
                durable_idempotency,
                "_write_store",
                side_effect=fail_final_write,
            ):
                first = durable_idempotency.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="request-1",
                    input_payload={"title": "Sensitive title"},
                    callback=callback,
                    storage_dir=storage_dir,
                )
                replay = durable_idempotency.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="request-1",
                    input_payload={"title": "Sensitive title"},
                    callback=callback,
                    storage_dir=storage_dir,
                )
            stored = (storage_dir / STORE_NAME).read_text(encoding="utf-8")

        self.assertEqual(calls, 1)
        self.assertEqual(first["status"], "verified")
        self.assertEqual(
            first["warnings"][-1]["code"],
            "idempotency_receipt_write_failed",
        )
        self.assertEqual(replay["status"], "committed_verification_pending")
        self.assertTrue(replay["replayed"])
        self.assertIsNone(replay["verification"]["write_performed"])
        self.assertFalse(replay["recovery"]["automatic_retry_safe"])
        self.assertEqual(
            replay["error"]["reason_code"],
            "idempotency_outcome_unknown",
        )
        self.assertNotIn("Sensitive title", stored)
        self.assertNotIn("request-1", stored)

    def test_known_preflight_callback_failure_clears_fence_for_safe_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            calls = 0

            def callback() -> dict[str, Any]:
                nonlocal calls
                calls += 1
                raise MutationNotStartedError(
                    "Reminder changed before dispatch",
                    code="concurrent_modification",
                    expected_version=1,
                    current_version=2,
                )

            for _ in range(2):
                with self.assertRaises(AdapterError):
                    durable_idempotency.execute_idempotent(
                        operation="attach_image",
                        key="preflight-retry",
                        input_payload={"if_version": 1},
                        callback=callback,
                        storage_dir=storage_dir,
                    )
            stored = read_store(storage_dir)

        self.assertEqual(calls, 2)
        self.assertEqual(stored["entries"], {})

    def test_untyped_no_mutation_flag_cannot_clear_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            calls = 0

            def callback() -> dict[str, Any]:
                nonlocal calls
                calls += 1
                raise AdapterError(
                    "A plain error cannot manufacture dispatch proof",
                    code="unexpected_error",
                    mutation_not_started=True,
                )

            with self.assertRaises(AdapterError):
                durable_idempotency.execute_idempotent(
                    operation="attach_image",
                    key="untyped-proof",
                    input_payload={"if_version": 1},
                    callback=callback,
                    storage_dir=storage_dir,
                )
            replay = durable_idempotency.execute_idempotent(
                operation="attach_image",
                key="untyped-proof",
                input_payload={"if_version": 1},
                callback=callback,
                storage_dir=storage_dir,
            )

        self.assertEqual(calls, 1)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "committed_verification_pending")

    def test_unclassified_callback_failure_keeps_fence_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            calls = 0

            def callback() -> dict[str, Any]:
                nonlocal calls
                calls += 1
                raise AdapterError(
                    "Failure timing was not proven",
                    code="unexpected_error",
                )

            with self.assertRaises(AdapterError):
                durable_idempotency.execute_idempotent(
                    operation="attach_image",
                    key="unclassified-failure",
                    input_payload={"if_version": 1},
                    callback=callback,
                    storage_dir=storage_dir,
                )
            replay = durable_idempotency.execute_idempotent(
                operation="attach_image",
                key="unclassified-failure",
                input_payload={"if_version": 1},
                callback=callback,
                storage_dir=storage_dir,
            )

        self.assertEqual(calls, 1)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "committed_verification_pending")

    def test_possible_commit_callback_failure_remains_fenced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            calls = 0

            def callback() -> dict[str, Any]:
                nonlocal calls
                calls += 1
                raise AdapterError(
                    "Native helper disappeared after dispatch",
                    code="sync_pending",
                    partial_failure=True,
                    mutation_outcome_unknown=True,
                )

            with self.assertRaises(AdapterError):
                durable_idempotency.execute_idempotent(
                    operation="attach_image",
                    key="possible-commit",
                    input_payload={"if_version": 1},
                    callback=callback,
                    storage_dir=storage_dir,
                )
            replay = durable_idempotency.execute_idempotent(
                operation="attach_image",
                key="possible-commit",
                input_payload={"if_version": 1},
                callback=callback,
                storage_dir=storage_dir,
            )

        self.assertEqual(calls, 1)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "committed_verification_pending")

    def test_fence_write_failure_stops_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            callback = mock.Mock(return_value={"ok": True, "status": "verified"})

            with (
                mock.patch.object(
                    durable_idempotency,
                    "_write_store",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaises(MutationNotStartedError) as raised,
            ):
                durable_idempotency.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="request-1",
                    input_payload={"title": "Sensitive title"},
                    callback=callback,
                    storage_dir=storage_dir,
                )

        callback.assert_not_called()
        self.assertEqual(raised.exception.code, "unexpected_error")
        self.assertEqual(
            raised.exception.details["reason_code"],
            "idempotency_fence_write_failed",
        )
        self.assertTrue(raised.exception.details["mutation_not_started"])

    def test_new_fence_cannot_be_pruned_if_clock_moves_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            calls = 0
            entries: dict[str, Any] = {}
            for index in range(MAX_ENTRIES):
                key = f"legacy-{index}"
                entries[f"{index:064x}"] = {
                    "operation": "create_reminder",
                    "input_hash": stable_hash({"title": key}),
                    "created_at_epoch": 200.0 + index,
                    "state": "complete",
                    "operation_id": FIXED_RECEIPT_OPERATION_ID,
                    "result": {"ok": True, "status": "verified", "id": key},
                }
            seed_store(storage_dir, {"version": 1, "entries": entries})

            def callback() -> dict[str, Any]:
                nonlocal calls
                calls += 1
                return verified_receipt(target={"id": f"R-{calls}"})

            with mock.patch.object(
                durable_idempotency._time,
                "time",
                return_value=100.0,
            ):
                first = durable_idempotency.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="request-1",
                    input_payload={"title": "Bounded"},
                    callback=callback,
                    storage_dir=storage_dir,
                )
                replay = durable_idempotency.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="request-1",
                    input_payload={"title": "Bounded"},
                    callback=callback,
                    storage_dir=storage_dir,
                )

        self.assertEqual(calls, 1)
        self.assertEqual(first["status"], "verified")
        self.assertTrue(replay["replayed"])

    def test_same_key_survives_forward_clock_jump_across_fence_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            calls = 0
            future = 100.0 + (RETENTION_DAYS + 1) * 86400

            def callback() -> dict[str, Any]:
                nonlocal calls
                calls += 1
                return verified_receipt(target={"id": f"R-{calls}"})

            with mock.patch.object(
                durable_idempotency._time,
                "time",
                side_effect=[100.0, 100.0, future, future, future],
            ):
                first = durable_idempotency.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="request-forward-clock",
                    input_payload={"title": "Bounded"},
                    callback=callback,
                    storage_dir=storage_dir,
                )
                replay = durable_idempotency.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="request-forward-clock",
                    input_payload={"title": "Bounded"},
                    callback=callback,
                    storage_dir=storage_dir,
                )

        self.assertEqual(calls, 1)
        self.assertEqual(first["status"], "verified")
        self.assertTrue(replay["replayed"])

    def test_capacity_never_evicts_an_unresolved_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            now = 1_000.0
            old_key = "old-unresolved"
            old_operation = "eventkit_create_reminder"
            old_input = {"title": "Old"}
            old_hash = stable_hash({"operation": old_operation, "key": old_key})
            entries = {
                old_hash: unresolved_record(
                    operation=old_operation,
                    input_hash=stable_hash(old_input),
                    created_at_epoch=now,
                )
            }
            index = 0
            while len(entries) < MAX_ENTRIES:
                entry_hash = f"{index:064x}"
                index += 1
                if entry_hash in entries:
                    continue
                entries[entry_hash] = unresolved_record(
                    operation="attach_image",
                    input_hash=f"{index:064x}",
                    created_at_epoch=now,
                )
            seed_store(storage_dir, {"version": 1, "entries": entries})
            callback = mock.Mock(return_value={"ok": True, "status": "verified"})

            with mock.patch.object(
                durable_idempotency._time,
                "time",
                return_value=now,
            ):
                with self.assertRaises(MutationNotStartedError) as raised:
                    durable_idempotency.execute_idempotent(
                        operation=old_operation,
                        key="new-request",
                        input_payload={"title": "New"},
                        callback=callback,
                        storage_dir=storage_dir,
                    )
                replay = durable_idempotency.execute_idempotent(
                    operation=old_operation,
                    key=old_key,
                    input_payload=old_input,
                    callback=callback,
                    storage_dir=storage_dir,
                )

        callback.assert_not_called()
        self.assertEqual(
            raised.exception.details["reason_code"],
            "idempotency_capacity_exhausted",
        )
        self.assertTrue(raised.exception.details["mutation_not_started"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "committed_verification_pending")

    def test_corrupt_existing_store_fails_closed_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            storage_dir.mkdir()
            (storage_dir / STORE_NAME).write_text("{not-json", encoding="utf-8")
            callback = mock.Mock(return_value={"ok": True, "status": "verified"})

            with self.assertRaises(MutationNotStartedError) as raised:
                durable_idempotency.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="request-1",
                    input_payload={"title": "Bounded"},
                    callback=callback,
                    storage_dir=storage_dir,
                )

        callback.assert_not_called()
        self.assertEqual(raised.exception.code, "unexpected_error")
        self.assertEqual(
            raised.exception.details["reason_code"],
            "idempotency_store_unreadable",
        )
        self.assertTrue(raised.exception.details["mutation_not_started"])

    def test_unknown_or_missing_store_version_fails_closed_without_rewrite(
        self,
    ) -> None:
        missing = object()
        cases: tuple[tuple[str, Any], ...] = (
            ("missing", missing),
            ("future", 2),
            ("zero", 0),
            ("float", 1.0),
            ("string", "1"),
            ("boolean", True),
            ("null", None),
        )
        for name, version in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                storage_dir = Path(temp_dir) / "support"
                storage_dir.mkdir()
                payload: dict[str, Any] = {"entries": {}}
                if version is not missing:
                    payload["version"] = version
                store_path = storage_dir / STORE_NAME
                original_bytes = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                store_path.write_bytes(original_bytes)
                callback = mock.Mock(return_value=verified_receipt())

                with self.assertRaises(MutationNotStartedError) as raised:
                    durable_idempotency.execute_idempotent(
                        operation="eventkit_create_reminder",
                        key="request-1",
                        input_payload={"title": "Bounded"},
                        callback=callback,
                        storage_dir=storage_dir,
                    )

                callback.assert_not_called()
                self.assertEqual(store_path.read_bytes(), original_bytes)
                self.assertEqual(raised.exception.code, "unexpected_error")
                self.assertEqual(
                    raised.exception.details["reason_code"],
                    "idempotency_store_unreadable",
                )
                self.assertTrue(raised.exception.details["mutation_not_started"])

    def test_corrupt_entry_shapes_fail_typed_before_dispatch(self) -> None:
        key_hash = stable_hash(
            {"operation": "eventkit_create_reminder", "key": "request-1"}
        )
        base_record = {
            "operation": "eventkit_create_reminder",
            "input_hash": stable_hash({"title": "Bounded"}),
            "created_at_epoch": 100.0,
            "state": "complete",
            "operation_id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            "result": verified_receipt(),
        }
        cases: tuple[tuple[str, Any], ...] = (
            ("non_object_record", "corrupt"),
            ("non_numeric_timestamp", {**base_record, "created_at_epoch": "bad"}),
            ("non_finite_timestamp", {**base_record, "created_at_epoch": float("nan")}),
        )
        for name, bad_record in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                storage_dir = Path(temp_dir) / "support"
                seed_store(
                    storage_dir,
                    {"version": 1, "entries": {key_hash: bad_record}},
                )
                callback = mock.Mock(return_value=verified_receipt())

                with self.assertRaises(MutationNotStartedError) as raised:
                    durable_idempotency.execute_idempotent(
                        operation="eventkit_create_reminder",
                        key="request-1",
                        input_payload={"title": "Bounded"},
                        callback=callback,
                        storage_dir=storage_dir,
                    )

                callback.assert_not_called()
                self.assertEqual(raised.exception.code, "unexpected_error")
                self.assertEqual(
                    raised.exception.details["reason_code"],
                    "idempotency_store_unreadable",
                )
                self.assertTrue(raised.exception.details["mutation_not_started"])

    def test_no_write_cleanup_failure_keeps_fence_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            writes = 0
            calls = 0
            real_write = durable_idempotency._write_store

            def callback() -> dict[str, Any]:
                nonlocal calls
                calls += 1
                raise MutationNotStartedError(
                    "Rejected before dispatch",
                    code="permission_denied",
                )

            def fail_cleanup(
                payload: dict[str, Any],
                *,
                storage_dir: Path,
                store_path: Path,
            ) -> None:
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("disk full")
                real_write(
                    payload,
                    storage_dir=storage_dir,
                    store_path=store_path,
                )

            with (
                mock.patch.object(
                    durable_idempotency,
                    "_write_store",
                    side_effect=fail_cleanup,
                ),
                self.assertRaises(MutationNotStartedError) as raised,
            ):
                durable_idempotency.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="cleanup-failure",
                    input_payload={"title": "Bounded"},
                    callback=callback,
                    storage_dir=storage_dir,
                )

            replay = durable_idempotency.execute_idempotent(
                operation="eventkit_create_reminder",
                key="cleanup-failure",
                input_payload={"title": "Bounded"},
                callback=callback,
                storage_dir=storage_dir,
            )

        self.assertEqual(calls, 1)
        self.assertEqual(
            raised.exception.details["reason_code"],
            "idempotency_fence_cleanup_failed",
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "committed_verification_pending")


class DurableIdempotencyGoldenTests(unittest.TestCase):
    def test_exact_key_and_input_hashes_are_persisted(self) -> None:
        expected_key_hash = (
            "7f07173859f85f03dfba2a4581de6b4f7be230ae31353bed7b72b673d9de4867"
        )
        expected_input_hash = (
            "bd095b10a2f083dc0dd867dab2f9036775310350d140a3e89bdf2f1340c31aed"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            result = durable_idempotency.execute_idempotent(
                operation="eventkit_create_reminder",
                key="request-1",
                input_payload={"title": "Sensitive title"},
                callback=verified_receipt,
                storage_dir=storage_dir,
            )
            stored = read_store(storage_dir)

        self.assertEqual(result["idempotency_key_hash"], expected_key_hash)
        self.assertEqual(set(stored["entries"]), {expected_key_hash})
        self.assertEqual(
            stored["entries"][expected_key_hash]["input_hash"],
            expected_input_hash,
        )
        self.assertEqual(
            stored["entries"][expected_key_hash]["operation"],
            "eventkit_create_reminder",
        )

    def test_pre_extraction_v1_store_bytes_are_a_literal_golden(self) -> None:
        expected = (
            '{"entries":{"7f07173859f85f03dfba2a4581de6b4f7be230ae31353bed7b72b673d9de4867":'
            '{"created_at_epoch":100.0,"input_hash":"bd095b10a2f083dc0dd867dab2f9036775310350d140a3e89bdf2f1340c31aed",'
            '"operation":"eventkit_create_reminder","operation_id":"11111111-1111-4111-8111-111111111111",'
            '"result":{"backend":"eventkit_public_sdk","ok":true,"operation":"create_reminder",'
            '"operation_id":"22222222-2222-4222-8222-222222222222","recovery":{"semantics":"not_applicable"},'
            '"status":"verified","target":{"id":"R-1"},"verification":{"final_read":true,"state":"read_back",'
            '"write_performed":true}},"state":"complete"}},"version":1}'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            with (
                mock.patch.object(
                    durable_idempotency,
                    "_new_operation_id",
                    return_value="11111111-1111-4111-8111-111111111111",
                ),
                mock.patch.object(durable_idempotency._time, "time", return_value=100.0),
            ):
                durable_idempotency.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="request-1",
                    input_payload={"title": "Sensitive title"},
                    callback=lambda: {
                        "ok": True,
                        "status": "verified",
                        "operation": "create_reminder",
                        "operation_id": "22222222-2222-4222-8222-222222222222",
                        "backend": "eventkit_public_sdk",
                        "target": {"id": "R-1", "title": "Sensitive title"},
                        "verification": {
                            "state": "read_back",
                            "write_performed": True,
                            "final_read": True,
                        },
                        "recovery": {"semantics": "not_applicable"},
                    },
                    storage_dir=storage_dir,
                )
            raw = (storage_dir / STORE_NAME).read_text(encoding="utf-8")

        self.assertEqual(raw, expected)

    def test_store_is_exact_compact_sorted_version_one_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            durable_idempotency.execute_idempotent(
                operation="eventkit_create_reminder",
                key="compact-json",
                input_payload={"z": 1, "a": "한글"},
                callback=lambda: verified_receipt(reason="한글"),
                storage_dir=storage_dir,
            )
            raw = (storage_dir / STORE_NAME).read_text(encoding="utf-8")
            document = json.loads(raw)

        self.assertEqual(
            raw,
            json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        self.assertEqual(set(document), {"entries", "version"})
        self.assertEqual(document["version"], 1)
        self.assertFalse(raw.endswith("\n"))
        self.assertNotIn(": ", raw)
        self.assertIn("한글", raw)
        record = next(iter(document["entries"].values()))
        self.assertEqual(
            set(record),
            {
                "created_at_epoch",
                "input_hash",
                "operation",
                "operation_id",
                "result",
                "state",
            },
        )
        self.assertEqual(record["state"], "complete")
        uuid.UUID(record["operation_id"])
        self.assertEqual(record["operation_id"], record["operation_id"].upper())

    def test_storage_directory_store_and_lock_use_private_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            durable_idempotency.execute_idempotent(
                operation="create_reminder",
                key="private-modes",
                input_payload={"title": "Bounded"},
                callback=verified_receipt,
                storage_dir=storage_dir,
            )
            directory_mode = stat.S_IMODE(storage_dir.stat().st_mode)
            store_mode = stat.S_IMODE((storage_dir / STORE_NAME).stat().st_mode)
            lock_mode = stat.S_IMODE((storage_dir / LOCK_NAME).stat().st_mode)
            temporary_files = list(storage_dir.glob(".idempotency.*.tmp"))

        self.assertEqual(directory_mode, 0o700)
        self.assertEqual(store_mode, 0o600)
        self.assertEqual(lock_mode, 0o600)
        self.assertEqual(temporary_files, [])

    def test_falsy_key_bypasses_filesystem_and_persistence(self) -> None:
        for key in (None, ""):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temp_dir:
                storage_dir = Path(temp_dir) / "must-not-exist"
                callback = mock.Mock(
                    return_value={"ok": True, "status": "verified", "id": "R-1"}
                )
                with mock.patch.object(durable_idempotency, "_write_store") as write:
                    result = durable_idempotency.execute_idempotent(
                        operation="create_reminder",
                        key=key,
                        input_payload={"title": "Bounded"},
                        callback=callback,
                        storage_dir=storage_dir,
                    )

                callback.assert_called_once_with()
                write.assert_not_called()
                self.assertEqual(result["id"], "R-1")
                self.assertNotIn("idempotency_key_hash", result)
                self.assertFalse(storage_dir.exists())

    def test_process_lock_remains_held_until_callback_and_receipt_finish(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            storage_dir = base / "support"
            started_path = base / "leader-started"
            blocked_path = base / "follower-blocked"
            escaped_path = base / "follower-escaped"
            release_path = base / "release-leader"
            command = [
                sys.executable,
                "-c",
                PROCESS_LOCK_WORKER,
                str(SCRIPTS_ROOT),
                str(storage_dir),
            ]
            environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            leader = subprocess.Popen(
                [
                    *command,
                    "leader",
                    str(started_path),
                    str(blocked_path),
                    str(escaped_path),
                    str(release_path),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            follower: subprocess.Popen[str] | None = None
            leader_stdout = leader_stderr = ""
            follower_stdout = follower_stderr = ""
            try:
                wait_for_path(started_path)
                follower = subprocess.Popen(
                    [
                        *command,
                        "follower",
                        str(started_path),
                        str(blocked_path),
                        str(escaped_path),
                        str(release_path),
                    ],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                )
                lock_outcome = wait_for_any((blocked_path, escaped_path))
                self.assertEqual(
                    lock_outcome,
                    blocked_path,
                    "the follower acquired the lock while the leader callback ran",
                )
                release_path.write_text("release", encoding="utf-8")
                leader_stdout, leader_stderr = leader.communicate(timeout=10)
                follower_stdout, follower_stderr = follower.communicate(timeout=10)
            finally:
                release_path.write_text("release", encoding="utf-8")
                reap_process(leader)
                reap_process(follower)

            assert follower is not None
            self.assertEqual(leader.returncode, 0, leader_stderr)
            self.assertEqual(follower.returncode, 0, follower_stderr)
            self.assertEqual(
                [json.loads(leader_stdout), json.loads(follower_stdout)],
                [
                    {"replayed": False, "status": "verified"},
                    {"replayed": True, "status": "verified"},
                ],
            )

    def test_snapshot_keeps_receipt_proof_but_drops_private_paths(self) -> None:
        destination_id = "7718459E-2672-4E99-9E6A-B9AA430E570F"
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir) / "support"
            durable_idempotency.execute_idempotent(
                operation="attach_image",
                key="snapshot-redaction",
                input_payload={"id": destination_id, "image_sha256": "b" * 64},
                callback=lambda: verified_receipt(
                    operation="attach_image",
                    backend="reminderkit",
                    target={"reminder_id": destination_id},
                    verification={
                        "state": "read_back",
                        "write_performed": True,
                        "final_read": True,
                        "matched": True,
                        "source_unchanged": True,
                        "final_attachment_content_hash": "a" * 64,
                    },
                    recovery={
                        "semantics": "not_applicable",
                        "automatic_retry_safe": False,
                    },
                    private_path="/Users/example/Library/private.png",
                ),
                storage_dir=storage_dir,
            )
            record = next(iter(read_store(storage_dir)["entries"].values()))
            snapshot = record["result"]

        self.assertTrue(snapshot["verification"]["write_performed"])
        self.assertTrue(snapshot["verification"]["final_read"])
        self.assertTrue(snapshot["verification"]["matched"])
        self.assertEqual(
            snapshot["verification"]["final_attachment_content_hash"],
            "a" * 64,
        )
        self.assertFalse(snapshot["recovery"]["automatic_retry_safe"])
        self.assertNotIn("private_path", snapshot)


if __name__ == "__main__":
    unittest.main()
