from __future__ import annotations

import hashlib
import json
import stat
import sys
import tempfile
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
