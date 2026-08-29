from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
ADAPTER_PATH = PLUGIN_ROOT / "scripts" / "reminders_adapter.py"
SPEC = importlib.util.spec_from_file_location("reminders_adapter_contract_tests", ADAPTER_PATH)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)

from receipt_contract import (  # noqa: E402
    adapter_receipt_error,
    failed_no_mutation_evidence_error,
    validated_receipt_mutation_state,
)


class ReceiptAndErrorContractTests(unittest.TestCase):
    def test_validated_receipt_state_is_evidence_aware(self) -> None:
        cases = (
            ({"status": "unchanged"}, "not_mutated"),
            (
                {
                    "status": "unchanged",
                    "verification": {"write_performed": False},
                },
                "not_mutated",
            ),
            (
                {
                    "status": "unchanged",
                    "verification": {"write_performed": True},
                },
                "unknown",
            ),
            ({"status": "unchanged", "committed": True}, "unknown"),
            ({"status": "verified"}, "committed"),
            (
                {
                    "status": "verified",
                    "verification": {"write_performed": False},
                },
                "unknown",
            ),
            (
                {
                    "status": "partial_success",
                    "verification": {"write_performed": True},
                },
                "committed",
            ),
            ({"status": "partial_success"}, "unknown"),
        )
        for receipt, expected in cases:
            with self.subTest(receipt=receipt):
                self.assertEqual(
                    validated_receipt_mutation_state(receipt),
                    expected,
                )

    def test_operation_receipt_has_stable_shape(self) -> None:
        payload = adapter.operation_receipt(
            status="verified",
            operation="update_reminder",
            backend="db",
            target={"id": "R-1"},
            verification={"state": "read_back"},
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "verified")
        self.assertEqual(payload["operation"], "update_reminder")
        self.assertTrue(payload["operation_id"])
        self.assertEqual(payload["target"], {"id": "R-1"})

    def test_exact_reminder_selector_rejects_unbounded_or_title_only_delete(self) -> None:
        with self.assertRaises(adapter.AdapterError) as empty:
            adapter.require_exact_reminder_selector(reminder_id=None, title=None, list_name=None)
        with self.assertRaises(adapter.AdapterError) as title_only:
            adapter.require_exact_reminder_selector(reminder_id=None, title="Only title", list_name=None)

        self.assertEqual(empty.exception.code, "ambiguous_target")
        self.assertEqual(title_only.exception.code, "ambiguous_target")

    def test_adapter_error_is_rendered_as_machine_readable_error(self) -> None:
        with mock.patch.object(adapter, "json_out") as output:
            code = adapter.fail("Changed", code="concurrent_modification", current_version=3)

        self.assertEqual(code, 1)
        payload = output.call_args.args[0]
        self.assertEqual(payload["status"], "failed_no_mutation")
        self.assertEqual(payload["error"]["code"], "concurrent_modification")
        self.assertEqual(payload["error"]["current_version"], 3)

    def test_uncompensated_partial_failure_requires_manual_repair(self) -> None:
        args = argparse.Namespace(
            func=mock.Mock(
                side_effect=adapter.AdapterError(
                    "Replacement failed",
                    partial_failure=True,
                    compensated=False,
                    new_attachment_id="A-1",
                )
            )
        )
        parser = mock.Mock()
        parser.parse_args.return_value = args

        with (
            mock.patch.object(adapter, "build_parser", return_value=parser),
            mock.patch.object(adapter, "json_out") as output,
        ):
            code = adapter.main([])

        self.assertEqual(code, 1)
        payload = output.call_args.args[0]
        self.assertEqual(payload["status"], "failed_manual_repair_required")
        self.assertEqual(payload["error"]["new_attachment_id"], "A-1")

    def test_failed_no_mutation_requires_affirmative_no_write_evidence(self) -> None:
        receipt = adapter.operation_receipt(
            status="failed_no_mutation",
            operation="attach_url",
            backend="sqlite_private",
            target={"reminder_id": "R-1"},
            after={},
            verification={
                "state": "not_performed",
                "write_performed": False,
                "final_read": False,
            },
            recovery={"semantics": "fix_input"},
        )
        receipt["error"] = {
            "code": "invalid_input",
            "message": "The write was rejected before dispatch.",
        }

        self.assertIsNone(failed_no_mutation_evidence_error(receipt))
        self.assertIsNone(
            adapter_receipt_error(receipt, expected_operation="attach_url")
        )

        contradictions = (
            lambda value: value["verification"].__setitem__("write_performed", True),
            lambda value: value.__setitem__("after", {"attachment": {"id": "A-1"}}),
            lambda value: value.__setitem__("mutation_attempted", True),
        )
        for contradict in contradictions:
            with self.subTest(contradiction=contradict):
                unsafe = json.loads(json.dumps(receipt))
                contradict(unsafe)
                self.assertIsNotNone(failed_no_mutation_evidence_error(unsafe))
                self.assertIsNotNone(
                    adapter_receipt_error(unsafe, expected_operation="attach_url")
                )
                self.assertEqual(
                    validated_receipt_mutation_state(unsafe),
                    "unknown",
                )


class JournalPrivacyTests(unittest.TestCase):
    def test_sensitive_action_fields_are_redacted(self) -> None:
        raw = {
            "id": "R-1",
            "title": "Private health reminder",
            "list": "Medical",
            "url": "https://secret.example/path",
            "source_path": "/Users/example/private.png",
            "nested": {"filename": "private.png", "count": 2},
        }

        redacted = adapter.redact_log_payload(raw)
        encoded = json.dumps(redacted, ensure_ascii=False)

        self.assertEqual(redacted["id"], "R-1")
        self.assertTrue(redacted["title"]["redacted"])
        self.assertTrue(redacted["nested"]["filename"]["redacted"])
        self.assertNotIn("Private health reminder", encoded)
        self.assertNotIn("secret.example", encoded)
        self.assertNotIn("private.png", encoded)

    def test_journal_failure_is_returned_as_warning_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            journal = support / "actions.jsonl"
            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "JOURNAL", journal),
                mock.patch.object(Path, "open", side_effect=OSError("denied")),
            ):
                warning = adapter.log_action("create_reminder", {"title": "Private"})

        self.assertIsNotNone(warning)
        self.assertEqual(warning["code"], "journal_write_failed")


class IdempotencyContractTests(unittest.TestCase):
    def test_repeated_key_replays_once_and_store_contains_no_user_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            calls = 0

            def callback() -> dict[str, object]:
                nonlocal calls
                calls += 1
                return adapter.operation_receipt(
                    status="verified",
                    operation="create_reminder",
                    backend="db",
                    target={"id": "R-1", "title": "Sensitive title", "list": "Private list"},
                )

            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "IDEMPOTENCY_STORE", support / "idempotency.json"),
                mock.patch.object(adapter, "IDEMPOTENCY_LOCK", support / "idempotency.lock"),
            ):
                first = adapter.execute_idempotent(
                    operation="create_reminder",
                    key="request-1",
                    input_payload={"title": "Sensitive title"},
                    callback=callback,
                )
                second = adapter.execute_idempotent(
                    operation="create_reminder",
                    key="request-1",
                    input_payload={"title": "Sensitive title"},
                    callback=callback,
                )
                stored = (support / "idempotency.json").read_text(encoding="utf-8")

        self.assertEqual(calls, 1)
        self.assertFalse(first.get("replayed", False))
        self.assertTrue(second["replayed"])
        self.assertEqual(second["target"]["id"], "R-1")
        self.assertNotIn("Sensitive title", stored)
        self.assertNotIn("Private list", stored)
        self.assertNotIn("request-1", stored)

    def test_reusing_key_with_different_input_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "IDEMPOTENCY_STORE", support / "idempotency.json"),
                mock.patch.object(adapter, "IDEMPOTENCY_LOCK", support / "idempotency.lock"),
            ):
                adapter.execute_idempotent(
                    operation="create_reminder",
                    key="request-1",
                    input_payload={"title": "A"},
                    callback=lambda: {"ok": True, "status": "verified", "id": "R-1"},
                )
                with self.assertRaises(adapter.AdapterError) as raised:
                    adapter.execute_idempotent(
                        operation="create_reminder",
                        key="request-1",
                        input_payload={"title": "B"},
                        callback=lambda: {"ok": True, "status": "verified", "id": "R-2"},
                    )

        self.assertEqual(raised.exception.code, "concurrent_modification")

    def test_in_progress_fence_prevents_redispatch_when_final_receipt_write_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            calls = 0
            writes = 0
            real_write = adapter.write_idempotency_store

            def callback() -> dict[str, object]:
                nonlocal calls
                calls += 1
                return adapter.operation_receipt(
                    status="verified",
                    operation="create_reminder",
                    backend="eventkit_public_sdk",
                    target={"id": f"R-{calls}", "title": "Sensitive title"},
                )

            def fail_final_write(payload: dict[str, object]) -> None:
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("disk full")
                real_write(payload)

            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "IDEMPOTENCY_STORE", support / "idempotency.json"),
                mock.patch.object(adapter, "IDEMPOTENCY_LOCK", support / "idempotency.lock"),
                mock.patch.object(
                    adapter,
                    "write_idempotency_store",
                    side_effect=fail_final_write,
                ),
            ):
                first = adapter.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="request-1",
                    input_payload={"title": "Sensitive title"},
                    callback=callback,
                )
                replay = adapter.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="request-1",
                    input_payload={"title": "Sensitive title"},
                    callback=callback,
                )
                stored = (support / "idempotency.json").read_text(encoding="utf-8")

        self.assertEqual(calls, 1)
        self.assertEqual(first["status"], "verified")
        self.assertEqual(first["warnings"][-1]["code"], "idempotency_receipt_write_failed")
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
            support = Path(temp_dir) / "support"
            calls = 0

            def callback() -> dict[str, object]:
                nonlocal calls
                calls += 1
                raise adapter.MutationNotStartedError(
                    "Reminder changed before dispatch",
                    code="concurrent_modification",
                    expected_version=1,
                    current_version=2,
                )

            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "IDEMPOTENCY_STORE", support / "idempotency.json"),
                mock.patch.object(adapter, "IDEMPOTENCY_LOCK", support / "idempotency.lock"),
            ):
                for _ in range(2):
                    with self.assertRaises(adapter.AdapterError):
                        adapter.execute_idempotent(
                            operation="attach_image",
                            key="preflight-retry",
                            input_payload={"if_version": 1},
                            callback=callback,
                        )
                stored = json.loads(
                    (support / "idempotency.json").read_text(encoding="utf-8")
                )

        self.assertEqual(calls, 2)
        self.assertEqual(stored["entries"], {})

    def test_untyped_no_mutation_flag_cannot_clear_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            calls = 0

            def callback() -> dict[str, object]:
                nonlocal calls
                calls += 1
                raise adapter.AdapterError(
                    "A plain error cannot manufacture dispatch proof",
                    code="unexpected_error",
                    mutation_not_started=True,
                )

            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(
                    adapter,
                    "IDEMPOTENCY_STORE",
                    support / "idempotency.json",
                ),
                mock.patch.object(
                    adapter,
                    "IDEMPOTENCY_LOCK",
                    support / "idempotency.lock",
                ),
            ):
                with self.assertRaises(adapter.AdapterError):
                    adapter.execute_idempotent(
                        operation="attach_image",
                        key="untyped-proof",
                        input_payload={"if_version": 1},
                        callback=callback,
                    )
                replay = adapter.execute_idempotent(
                    operation="attach_image",
                    key="untyped-proof",
                    input_payload={"if_version": 1},
                    callback=callback,
                )

        self.assertEqual(calls, 1)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "committed_verification_pending")

    def test_unclassified_callback_failure_keeps_fence_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            calls = 0

            def callback() -> dict[str, object]:
                nonlocal calls
                calls += 1
                raise adapter.AdapterError(
                    "Failure timing was not proven",
                    code="unexpected_error",
                )

            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(
                    adapter,
                    "IDEMPOTENCY_STORE",
                    support / "idempotency.json",
                ),
                mock.patch.object(
                    adapter,
                    "IDEMPOTENCY_LOCK",
                    support / "idempotency.lock",
                ),
            ):
                with self.assertRaises(adapter.AdapterError):
                    adapter.execute_idempotent(
                        operation="attach_image",
                        key="unclassified-failure",
                        input_payload={"if_version": 1},
                        callback=callback,
                    )
                replay = adapter.execute_idempotent(
                    operation="attach_image",
                    key="unclassified-failure",
                    input_payload={"if_version": 1},
                    callback=callback,
                )

        self.assertEqual(calls, 1)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "committed_verification_pending")

    def test_possible_commit_callback_failure_remains_fenced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            calls = 0

            def callback() -> dict[str, object]:
                nonlocal calls
                calls += 1
                raise adapter.AdapterError(
                    "Native helper disappeared after dispatch",
                    code="sync_pending",
                    partial_failure=True,
                    mutation_outcome_unknown=True,
                )

            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "IDEMPOTENCY_STORE", support / "idempotency.json"),
                mock.patch.object(adapter, "IDEMPOTENCY_LOCK", support / "idempotency.lock"),
            ):
                with self.assertRaises(adapter.AdapterError):
                    adapter.execute_idempotent(
                        operation="attach_image",
                        key="possible-commit",
                        input_payload={"if_version": 1},
                        callback=callback,
                    )
                replay = adapter.execute_idempotent(
                    operation="attach_image",
                    key="possible-commit",
                    input_payload={"if_version": 1},
                    callback=callback,
                )

        self.assertEqual(calls, 1)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "committed_verification_pending")

    def test_idempotency_fence_write_failure_stops_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            callback = mock.Mock(return_value={"ok": True, "status": "verified"})

            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "IDEMPOTENCY_STORE", support / "idempotency.json"),
                mock.patch.object(adapter, "IDEMPOTENCY_LOCK", support / "idempotency.lock"),
                mock.patch.object(
                    adapter,
                    "write_idempotency_store",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaises(adapter.AdapterError) as raised,
            ):
                adapter.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="request-1",
                    input_payload={"title": "Sensitive title"},
                    callback=callback,
                )

        callback.assert_not_called()
        self.assertEqual(raised.exception.code, "unexpected_error")
        self.assertEqual(
            raised.exception.details["reason_code"],
            "idempotency_fence_write_failed",
        )
        self.assertTrue(raised.exception.details["mutation_not_started"])

    def test_new_fence_cannot_be_pruned_before_dispatch_if_clock_moves_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            calls = 0

            def callback() -> dict[str, object]:
                nonlocal calls
                calls += 1
                return adapter.operation_receipt(
                    status="verified",
                    operation="create_reminder",
                    backend="eventkit_public_sdk",
                    target={"id": f"R-{calls}"},
                )

            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "IDEMPOTENCY_STORE", support / "idempotency.json"),
                mock.patch.object(adapter, "IDEMPOTENCY_LOCK", support / "idempotency.lock"),
                mock.patch.object(adapter, "IDEMPOTENCY_MAX_ENTRIES", 1),
            ):
                adapter.write_idempotency_store(
                    {
                        "version": 1,
                        "entries": {
                            "future-entry": {
                                "operation": "create_reminder",
                                "input_hash": "f" * 64,
                                "created_at_epoch": 200.0,
                                "result": {"ok": True, "status": "verified"},
                            }
                        },
                    }
                )
                with mock.patch.object(adapter.time, "time", return_value=100.0):
                    first = adapter.execute_idempotent(
                        operation="eventkit_create_reminder",
                        key="request-1",
                        input_payload={"title": "Bounded"},
                        callback=callback,
                    )
                    replay = adapter.execute_idempotent(
                        operation="eventkit_create_reminder",
                        key="request-1",
                        input_payload={"title": "Bounded"},
                        callback=callback,
                    )

        self.assertEqual(calls, 1)
        self.assertEqual(first["status"], "verified")
        self.assertTrue(replay["replayed"])

    def test_same_key_survives_forward_clock_jump_across_fence_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            calls = 0
            future = 100.0 + (adapter.IDEMPOTENCY_RETENTION_DAYS + 1) * 86400

            def callback() -> dict[str, object]:
                nonlocal calls
                calls += 1
                return adapter.operation_receipt(
                    status="verified",
                    operation="create_reminder",
                    backend="eventkit_public_sdk",
                    target={"id": f"R-{calls}"},
                )

            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "IDEMPOTENCY_STORE", support / "idempotency.json"),
                mock.patch.object(adapter, "IDEMPOTENCY_LOCK", support / "idempotency.lock"),
                mock.patch.object(
                    adapter.time,
                    "time",
                    side_effect=[100.0, 100.0, future, future, future],
                ),
            ):
                first = adapter.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="request-forward-clock",
                    input_payload={"title": "Bounded"},
                    callback=callback,
                )
                replay = adapter.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="request-forward-clock",
                    input_payload={"title": "Bounded"},
                    callback=callback,
                )

        self.assertEqual(calls, 1)
        self.assertEqual(first["status"], "verified")
        self.assertTrue(replay["replayed"])

    def test_capacity_never_evicts_an_unresolved_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            now = 1_000.0
            old_key = "old-unresolved"
            old_operation = "eventkit_create_reminder"
            old_input = {"title": "Old"}
            old_hash = adapter.stable_hash(
                {"operation": old_operation, "key": old_key}
            )
            callback = mock.Mock(return_value={"ok": True, "status": "verified"})

            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "IDEMPOTENCY_STORE", support / "idempotency.json"),
                mock.patch.object(adapter, "IDEMPOTENCY_LOCK", support / "idempotency.lock"),
                mock.patch.object(adapter, "IDEMPOTENCY_MAX_ENTRIES", 1),
                mock.patch.object(adapter.time, "time", return_value=now),
            ):
                adapter.write_idempotency_store(
                    {
                        "version": 1,
                        "entries": {
                            old_hash: {
                                "operation": old_operation,
                                "input_hash": adapter.stable_hash(old_input),
                                "created_at_epoch": now,
                                "state": "in_progress",
                                "operation_id": "22222222-2222-4222-8222-222222222222",
                            }
                        },
                    }
                )
                with self.assertRaises(adapter.AdapterError) as raised:
                    adapter.execute_idempotent(
                        operation=old_operation,
                        key="new-request",
                        input_payload={"title": "New"},
                        callback=callback,
                    )
                replay = adapter.execute_idempotent(
                    operation=old_operation,
                    key=old_key,
                    input_payload=old_input,
                    callback=callback,
                )

        callback.assert_not_called()
        self.assertEqual(
            raised.exception.details["reason_code"],
            "idempotency_capacity_exhausted",
        )
        self.assertTrue(raised.exception.details["mutation_not_started"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "committed_verification_pending")

    def test_corrupt_existing_idempotency_store_fails_closed_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            support.mkdir()
            store = support / "idempotency.json"
            store.write_text("{not-json", encoding="utf-8")
            callback = mock.Mock(return_value={"ok": True, "status": "verified"})

            with (
                mock.patch.object(adapter, "APP_SUPPORT", support),
                mock.patch.object(adapter, "IDEMPOTENCY_STORE", store),
                mock.patch.object(adapter, "IDEMPOTENCY_LOCK", support / "idempotency.lock"),
                self.assertRaises(adapter.AdapterError) as raised,
            ):
                adapter.execute_idempotent(
                    operation="eventkit_create_reminder",
                    key="request-1",
                    input_payload={"title": "Bounded"},
                    callback=callback,
                )

        callback.assert_not_called()
        self.assertEqual(raised.exception.code, "unexpected_error")
        self.assertEqual(
            raised.exception.details["reason_code"],
            "idempotency_store_unreadable",
        )
        self.assertTrue(raised.exception.details["mutation_not_started"])


if __name__ == "__main__":
    unittest.main()
