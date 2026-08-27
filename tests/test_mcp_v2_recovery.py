from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
sys.path.insert(0, str(PLUGIN_ROOT))

from mcp.v2_contract import validate_public_result
from mcp.v2_recovery import (
    DeletedGuard,
    DeletedSnapshot,
    RecoveryFacade,
    RecoveryOutcome,
)
from mcp.v2_recovery_backend import RecoveryBackend


REMINDER_ID = "11111111-1111-4111-8111-111111111111"
LIST_ID = "22222222-2222-4222-8222-222222222222"
GUARD = DeletedGuard(
    reminder_id=REMINDER_ID,
    store_identity="a" * 64,
    private_version=6,
    deleted_at="2026-08-27T23:00:00+09:00",
    attachment_digest="b" * 64,
    native_guard_digest="c" * 64,
    account_id="ACCOUNT-1",
)
DELETED = {
    "id": REMINDER_ID,
    "title": "Synthetic deleted reminder",
    "completed": False,
    "priority": 0,
    "created_at": "2026-08-26T10:00:00+09:00",
    "deleted_at": GUARD.deleted_at,
    "expires_at": "2026-09-26T23:00:00+09:00",
    "account_id": GUARD.account_id,
    "attachment_count": 1,
    "image_attachment_count": 1,
    "url_attachment_count": 0,
    "attachments": [
        {
            "id": "ATTACHMENT-1",
            "type": "image",
            "filename": "image.png",
            "file_size": 42,
            "width": 2,
            "height": 3,
        }
    ],
    "attachments_truncated": False,
}


def verified_receipt() -> dict[str, Any]:
    return {
        "ok": True,
        "status": "verified",
        "operation": "recover_deleted_reminder",
        "operation_id": "11111111-1111-4111-8111-111111111111",
        "backend": "native_extension",
        "target": {"reminder_id": REMINDER_ID, "list_id": LIST_ID},
        "before": {
            "deleted_reminder": {
                "id": REMINDER_ID,
                "deleted_at": GUARD.deleted_at,
                "attachment_count": 1,
            }
        },
        "after": {
            "reminder": {
                "id": REMINDER_ID,
                "list_id": LIST_ID,
                "attachment_count": 1,
            }
        },
        "verification": {
            "state": "read_back",
            "write_performed": True,
            "final_read": True,
            "matched": True,
            "pre_save_guard_matched": True,
            "destination_list_matched": True,
            "attachments_active": True,
            "attachments_preserved": True,
            "attachment_bytes_verified": True,
            "attachment_counts_match": True,
            "before_attachment_count": 1,
            "native_attachment_count": 1,
            "after_attachment_count": 1,
            "evidence_scope": ["local_native", "local_eventkit"],
        },
        "recovery": {
            "semantics": "recently_deleted_recovery",
            "automatic_retry_safe": False,
        },
    }


def verified_adapter_receipt() -> dict[str, Any]:
    receipt = verified_receipt()
    receipt["backend"] = "reminderkit_private"
    return receipt


def failed_no_mutation_receipt() -> dict[str, Any]:
    receipt = verified_receipt()
    receipt.update(
        {
            "ok": False,
            "status": "failed_no_mutation",
            "after": {},
            "verification": {
                "state": "not_needed",
                "write_performed": False,
                "final_read": False,
            },
            "recovery": {
                "semantics": "inspect_exact_deleted_item_before_retry",
                "automatic_retry_safe": False,
            },
            "error": {
                "code": "unexpected_error",
                "reason_code": "fault_injected_no_mutation_claim",
                "message": "Fault-injected receipt conflicts with the backend mutation fact.",
                "retryable": False,
            },
        }
    )
    return receipt


class FakeBackend:
    def __init__(self) -> None:
        self.recoveries: list[tuple[DeletedGuard, str, str]] = []

    def list_deleted(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"items": [DELETED], "truncated": False}

    def read_deleted(self, reminder_id: str, attachment_limit: int) -> DeletedSnapshot:
        assert reminder_id == REMINDER_ID
        assert attachment_limit == 100
        return DeletedSnapshot(copy.deepcopy(DELETED), GUARD)

    def recover(
        self,
        guard: DeletedGuard,
        list_id: str,
        idempotency_key: str,
    ) -> RecoveryOutcome:
        self.recoveries.append((guard, list_id, idempotency_key))
        return RecoveryOutcome(verified_receipt(), "committed")


class PagedBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot_fingerprint = "d" * 64
        self.offsets: list[int] = []
        second = copy.deepcopy(DELETED)
        second["id"] = "33333333-3333-4333-8333-333333333333"
        second["title"] = "Second synthetic deleted reminder"
        self.items = [copy.deepcopy(DELETED), second]

    def list_deleted(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        offset = int(arguments.get("offset", 0))
        limit = int(arguments.get("limit", 20))
        self.offsets.append(offset)
        page = self.items[offset : offset + limit]
        next_offset = offset + len(page)
        has_more = next_offset < len(self.items)
        return {
            "items": copy.deepcopy(page),
            "returned": len(page),
            "limit": limit,
            "total_matched": len(self.items),
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "snapshot_fingerprint": self.snapshot_fingerprint,
        }


class RecoveryFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeBackend()
        tokens = iter(["A" * 32, "B" * 32])
        self.now = [10.0]
        self.facade = RecoveryFacade(
            self.backend,
            token_source=lambda: next(tokens),
            clock=lambda: self.now[0],
        )

    def test_list_is_bounded_and_never_issues_recovery_reference(self) -> None:
        result = self.facade.call(
            "inspect_recently_deleted",
            {"kind": "list", "limit": 20},
        )

        validate_public_result("inspect_recently_deleted", result)
        self.assertEqual(result["data"]["returned"], 1)
        self.assertNotIn("reference", result["data"]["items"][0])
        self.assertNotIn("attachments", result["data"]["items"][0])

    def test_list_cursor_reaches_later_deleted_items_without_exposing_snapshot(self) -> None:
        backend = PagedBackend()
        facade = RecoveryFacade(backend)

        first = facade.call(
            "inspect_recently_deleted",
            {"kind": "list", "account_id": "ACCOUNT-1", "limit": 1},
        )
        validate_public_result("inspect_recently_deleted", first)
        cursor = first["data"]["next_cursor"]
        self.assertTrue(first["data"]["has_more"])
        self.assertIsInstance(cursor, str)
        self.assertNotIn(backend.snapshot_fingerprint, repr(first))

        second = facade.call(
            "inspect_recently_deleted",
            {
                "kind": "list",
                "account_id": "ACCOUNT-1",
                "limit": 1,
                "cursor": cursor,
            },
        )
        validate_public_result("inspect_recently_deleted", second)
        self.assertEqual(backend.offsets, [0, 1])
        self.assertEqual(
            second["data"]["items"][0]["id"],
            "33333333-3333-4333-8333-333333333333",
        )
        self.assertFalse(second["data"]["has_more"])
        self.assertIsNone(second["data"]["next_cursor"])

    def test_list_cursor_rejects_snapshot_drift_without_returning_a_page(self) -> None:
        backend = PagedBackend()
        facade = RecoveryFacade(backend)
        first = facade.call(
            "inspect_recently_deleted",
            {"kind": "list", "limit": 1},
        )
        backend.snapshot_fingerprint = "e" * 64

        stale = facade.call(
            "inspect_recently_deleted",
            {
                "kind": "list",
                "limit": 1,
                "cursor": first["data"]["next_cursor"],
            },
        )

        validate_public_result("inspect_recently_deleted", stale, "not_mutated")
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["status"], "failed_no_mutation")
        self.assertEqual(stale["error"]["code"], "concurrent_modification")
        self.assertEqual(
            stale["error"]["reason_code"],
            "pagination_snapshot_stale",
        )
        self.assertEqual(
            stale["next_action"]["tool"],
            "inspect_recently_deleted",
        )
        self.assertFalse(stale["next_action"]["retry_original_once"])
        self.assertIn("without a cursor", stale["next_action"]["message"])
        self.assertNotIn("data", stale)

    def test_exact_read_issues_one_opaque_del1_without_private_guard(self) -> None:
        result = self.facade.call(
            "inspect_recently_deleted",
            {"kind": "item", "reminder_id": REMINDER_ID, "attachment_limit": 100},
        )

        validate_public_result("inspect_recently_deleted", result)
        deleted = result["data"]["deleted_reminder"]
        self.assertEqual(deleted["reference"], "del1." + "A" * 32)
        encoded = repr(result)
        self.assertNotIn(GUARD.store_identity, encoded)
        self.assertNotIn(GUARD.attachment_digest, encoded)
        self.assertNotIn(GUARD.native_guard_digest, encoded)
        self.assertNotIn("private_version", encoded)

    def test_recovery_consumes_reference_but_idempotent_replay_is_safe(self) -> None:
        read = self.facade.call(
            "inspect_recently_deleted",
            {"kind": "item", "reminder_id": REMINDER_ID},
        )
        arguments = {
            "reference": read["data"]["deleted_reminder"]["reference"],
            "list_id": LIST_ID,
            "idempotency_key": "recover-flow-0001",
        }

        first = self.facade.call("recover_deleted_reminder", arguments)
        second = self.facade.call("recover_deleted_reminder", arguments)

        validate_public_result("recover_deleted_reminder", first, "committed")
        validate_public_result("recover_deleted_reminder", second, "committed")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(len(self.backend.recoveries), 1)

        different_key = self.facade.call(
            "recover_deleted_reminder",
            {**arguments, "idempotency_key": "recover-flow-0002"},
        )
        validate_public_result(
            "recover_deleted_reminder", different_key, "not_mutated"
        )
        self.assertEqual(different_key["error"]["code"], "concurrent_modification")
        self.assertEqual(
            different_key["next_action"]["tool"], "inspect_recently_deleted"
        )

    def test_expired_reference_requires_a_fresh_exact_deleted_read(self) -> None:
        read = self.facade.call(
            "inspect_recently_deleted",
            {"kind": "item", "reminder_id": REMINDER_ID},
        )
        self.now[0] += 301
        result = self.facade.call(
            "recover_deleted_reminder",
            {
                "reference": read["data"]["deleted_reminder"]["reference"],
                "list_id": LIST_ID,
                "idempotency_key": "recover-flow-0003",
            },
        )

        validate_public_result("recover_deleted_reminder", result, "not_mutated")
        self.assertEqual(
            result["error"]["reason_code"],
            "expired_or_consumed_deleted_reference",
        )

    def test_call_with_state_preserves_conflicting_backend_fact_and_replay(self) -> None:
        class ConflictingStateBackend(FakeBackend):
            def recover(
                self,
                guard: DeletedGuard,
                list_id: str,
                idempotency_key: str,
            ) -> RecoveryOutcome:
                self.recoveries.append((guard, list_id, idempotency_key))
                return RecoveryOutcome(failed_no_mutation_receipt(), "committed")

        backend = ConflictingStateBackend()
        facade = RecoveryFacade(backend, token_source=lambda: "C" * 32)
        read = facade.call(
            "inspect_recently_deleted",
            {"kind": "item", "reminder_id": REMINDER_ID},
        )
        arguments = {
            "reference": read["data"]["deleted_reminder"]["reference"],
            "list_id": LIST_ID,
            "idempotency_key": "conflicting-state-key",
        }

        first, first_state = facade.call_with_state(
            "recover_deleted_reminder", arguments
        )
        replay, replay_state = facade.call_with_state(
            "recover_deleted_reminder", arguments
        )

        self.assertEqual(first["status"], "failed_no_mutation")
        self.assertEqual(first_state, "committed")
        self.assertEqual(replay_state, "committed")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertNotIn("mutation_state", first)
        self.assertNotIn("mutation_state", replay)
        self.assertEqual(len(backend.recoveries), 1)


class RecoveryBackendTests(unittest.TestCase):
    def test_private_adapter_error_detail_never_reaches_public_read(self) -> None:
        backend = RecoveryBackend(
            adapter_call=lambda _argv: (
                {
                    "ok": False,
                    "error": {
                        "code": "unexpected_error",
                        "reason_code": "/Users/example/Library/Reminders/Stores.sqlite",
                        "message": (
                            "Open /Users/example/Library/Reminders/"
                            "Container_v1/Stores/Stores.sqlite"
                        ),
                    },
                },
                True,
            ),
            bridge_call=mock.Mock(),
            receipt_validator=lambda payload, **_: None,
        )

        public = RecoveryFacade(backend).inspect(
            {"kind": "item", "reminder_id": REMINDER_ID}
        )

        encoded = repr(public)
        self.assertEqual(public["status"], "failed_no_mutation")
        self.assertNotIn("/Users/", encoded)
        self.assertNotIn("Stores.sqlite", encoded)
        self.assertNotIn("Container_v1", encoded)
        self.assertNotIn("users_example", encoded)
        self.assertEqual(
            public["error"]["reason_code"],
            "recently_deleted_operation_failed",
        )
        self.assertEqual(
            public["error"]["message"],
            "The local Recently Deleted operation failed without a safe public detail.",
        )
        validate_public_result("inspect_recently_deleted", public, "not_mutated")

    def test_proven_adapter_not_started_remains_failed_no_mutation(self) -> None:
        backend = RecoveryBackend(
            adapter_call=lambda _argv: (
                {
                    "ok": False,
                    "__dispatch_phase": "not_started",
                    "error": {
                        "code": "adapter_unavailable",
                        "message": "Private launch detail",
                    },
                },
                True,
            ),
            bridge_call=mock.Mock(),
            receipt_validator=lambda payload, **_: "invalid receipt",
        )

        outcome = backend.recover(GUARD, LIST_ID, "backend-key-not-started")

        self.assertEqual(outcome.mutation_state, "not_mutated")
        self.assertEqual(outcome.receipt["status"], "failed_no_mutation")
        self.assertFalse(outcome.receipt["verification"]["write_performed"])
        self.assertEqual(outcome.receipt["next_action"]["tool"], "diagnose_reminders")
        self.assertNotIn("Private launch detail", repr(outcome.receipt))
        validate_public_result(
            "recover_deleted_reminder",
            {"schema_version": 2, **outcome.receipt},
            "not_mutated",
        )

    def test_started_invalid_adapter_receipt_remains_unknown(self) -> None:
        backend = RecoveryBackend(
            adapter_call=lambda _argv: (
                {
                    "ok": False,
                    "__dispatch_phase": "started_unknown",
                    "error": {"code": "invalid_adapter_response"},
                },
                True,
            ),
            bridge_call=mock.Mock(),
            receipt_validator=lambda payload, **_: "invalid receipt",
        )

        outcome = backend.recover(GUARD, LIST_ID, "backend-key-started")

        self.assertEqual(outcome.mutation_state, "unknown")
        self.assertEqual(outcome.receipt["status"], "committed_verification_pending")
        self.assertIsNone(outcome.receipt["verification"]["write_performed"])

    def test_contradictory_failed_no_mutation_receipt_remains_unknown(self) -> None:
        for field_present, value in ((True, True), (True, None), (False, None)):
            with self.subTest(field_present=field_present, value=value):
                adapter_payload = failed_no_mutation_receipt()
                if field_present:
                    adapter_payload["verification"]["write_performed"] = value
                else:
                    del adapter_payload["verification"]["write_performed"]
                backend = RecoveryBackend(
                    adapter_call=lambda _argv, payload=adapter_payload: (payload, True),
                    bridge_call=mock.Mock(),
                    receipt_validator=lambda payload, **_: None,
                )

                outcome = backend.recover(
                    GUARD,
                    LIST_ID,
                    "backend-key-contradictory-no-write",
                )

                self.assertEqual(outcome.mutation_state, "unknown")
                self.assertEqual(
                    outcome.receipt["status"], "committed_verification_pending"
                )
                self.assertIsNone(outcome.receipt["verification"]["write_performed"])
                self.assertEqual(
                    outcome.receipt["error"]["reason_code"],
                    "invalid_recovery_receipt",
                )
                self.assertEqual(
                    outcome.receipt["next_action"]["tool"],
                    "read_reminder",
                )

    def test_failed_no_mutation_with_after_evidence_remains_unknown(self) -> None:
        adapter_payload = failed_no_mutation_receipt()
        adapter_payload["after"] = {
            "reminder": {
                "id": REMINDER_ID,
                "list_id": LIST_ID,
                "attachment_count": 1,
            }
        }
        backend = RecoveryBackend(
            adapter_call=lambda _argv: (adapter_payload, True),
            bridge_call=mock.Mock(),
            receipt_validator=lambda payload, **_: None,
        )

        outcome = backend.recover(
            GUARD,
            LIST_ID,
            "backend-key-after-is-commit-evidence",
        )

        self.assertEqual(outcome.mutation_state, "unknown")
        self.assertEqual(
            outcome.receipt["status"],
            "committed_verification_pending",
        )
        self.assertIsNone(outcome.receipt["verification"]["write_performed"])
        self.assertEqual(
            outcome.receipt["error"]["reason_code"],
            "invalid_recovery_receipt",
        )

    def test_actual_adapter_pre_dispatch_no_write_shape_stays_not_mutated(self) -> None:
        adapter_payload = failed_no_mutation_receipt()
        adapter_payload["verification"] = {
            "state": "not_performed",
            "write_performed": False,
        }
        backend = RecoveryBackend(
            adapter_call=lambda _argv: (adapter_payload, True),
            bridge_call=mock.Mock(),
            receipt_validator=lambda payload, **_: None,
        )

        outcome = backend.recover(GUARD, LIST_ID, "backend-key-known-no-write")

        self.assertEqual(outcome.mutation_state, "not_mutated")
        self.assertEqual(outcome.receipt["status"], "failed_no_mutation")
        self.assertFalse(outcome.receipt["verification"]["write_performed"])

    def test_exact_deleted_read_requires_and_keeps_native_guard_private(self) -> None:
        adapter_payload = {
            "ok": True,
            "deleted_reminder": copy.deepcopy(DELETED),
            "guard": {
                "reminder_id": GUARD.reminder_id,
                "store_identity": GUARD.store_identity,
                "private_version": GUARD.private_version,
                "deleted_at": GUARD.deleted_at,
                "attachment_digest": GUARD.attachment_digest,
                "native_guard_digest": GUARD.native_guard_digest,
                "account_id": GUARD.account_id,
            },
        }
        backend = RecoveryBackend(
            adapter_call=lambda _argv: (adapter_payload, False),
            bridge_call=mock.Mock(),
            receipt_validator=lambda payload, **_: None,
        )

        snapshot = backend.read_deleted(REMINDER_ID, 100)

        self.assertEqual(snapshot.guard, GUARD)
        public = RecoveryFacade(
            backend,
            token_source=lambda: "D" * 32,
        ).inspect({"kind": "item", "reminder_id": REMINDER_ID})
        self.assertNotIn(GUARD.native_guard_digest, repr(public))

    def test_verified_recovery_without_complete_native_proof_fails_closed(self) -> None:
        proof_fields = (
            "pre_save_guard_matched",
            "destination_list_matched",
            "attachments_active",
            "attachments_preserved",
            "attachment_bytes_verified",
            "attachment_counts_match",
        )
        for field in proof_fields:
            for field_present, value in ((True, False), (False, None)):
                with self.subTest(
                    field=field,
                    field_present=field_present,
                    value=value,
                ):
                    adapter_payload = verified_adapter_receipt()
                    if field_present:
                        adapter_payload["verification"][field] = value
                    else:
                        del adapter_payload["verification"][field]
                    bridge_call = mock.Mock()
                    backend = RecoveryBackend(
                        adapter_call=lambda _argv, payload=adapter_payload: (payload, False),
                        bridge_call=bridge_call,
                        receipt_validator=lambda payload, **_: None,
                    )

                    outcome = backend.recover(GUARD, LIST_ID, "backend-key-unsafe")

                    self.assertEqual(outcome.mutation_state, "unknown")
                    self.assertEqual(
                        outcome.receipt["status"], "committed_verification_pending"
                    )
                    self.assertEqual(
                        outcome.receipt["error"]["reason_code"],
                        "invalid_recovery_receipt",
                    )
                    self.assertEqual(
                        outcome.receipt["next_action"]["tool"], "read_reminder"
                    )
                    self.assertTrue(outcome.receipt["verification"]["write_performed"])
                    validate_public_result(
                        "recover_deleted_reminder",
                        {"schema_version": 2, **outcome.receipt},
                        "unknown",
                    )
                    bridge_call.assert_not_called()

        for field, value in (
            ("before_attachment_count", None),
            ("native_attachment_count", True),
            ("after_attachment_count", -1),
            ("after_attachment_count", 2),
        ):
            with self.subTest(field=field, value=value):
                adapter_payload = verified_adapter_receipt()
                adapter_payload["verification"][field] = value
                bridge_call = mock.Mock()
                backend = RecoveryBackend(
                    adapter_call=lambda _argv, payload=adapter_payload: (payload, False),
                    bridge_call=bridge_call,
                    receipt_validator=lambda payload, **_: None,
                )

                outcome = backend.recover(GUARD, LIST_ID, "backend-key-unsafe")

                self.assertEqual(outcome.mutation_state, "unknown")
                self.assertEqual(
                    outcome.receipt["status"], "committed_verification_pending"
                )
                self.assertEqual(
                    outcome.receipt["error"]["reason_code"],
                    "invalid_recovery_receipt",
                )
                self.assertEqual(
                    outcome.receipt["next_action"]["tool"], "read_reminder"
                )
                self.assertTrue(outcome.receipt["verification"]["write_performed"])
                validate_public_result(
                    "recover_deleted_reminder",
                    {"schema_version": 2, **outcome.receipt},
                    "unknown",
                )
                bridge_call.assert_not_called()

    def test_verified_recovery_requires_raw_identity_and_count_consistency(self) -> None:
        def wrong_target_reminder(payload):
            payload["target"]["reminder_id"] = "OTHER-REMINDER"

        def wrong_target_list(payload):
            payload["target"]["list_id"] = "OTHER-LIST"

        def wrong_before_id(payload):
            payload["before"]["deleted_reminder"]["id"] = "OTHER-REMINDER"

        def wrong_before_deleted_at(payload):
            payload["before"]["deleted_reminder"]["deleted_at"] = "2020-01-01T00:00:00Z"

        def wrong_before_count(payload):
            payload["before"]["deleted_reminder"]["attachment_count"] = 2

        def wrong_after_id(payload):
            payload["after"]["reminder"]["id"] = "OTHER-REMINDER"

        def wrong_after_list(payload):
            payload["after"]["reminder"]["list_id"] = "OTHER-LIST"

        def wrong_after_count(payload):
            payload["after"]["reminder"]["attachment_count"] = 0

        def wrong_backend(payload):
            payload["backend"] = "native_extension"

        def verified_with_error(payload):
            payload["error"] = {
                "code": "unexpected_error",
                "reason_code": "contradictory_verified_error",
                "message": "A verified receipt cannot also be an error.",
                "retryable": False,
            }

        def automatic_retry(payload):
            payload["recovery"]["automatic_retry_safe"] = True

        contradictions = (
            wrong_target_reminder,
            wrong_target_list,
            wrong_before_id,
            wrong_before_deleted_at,
            wrong_before_count,
            wrong_after_id,
            wrong_after_list,
            wrong_after_count,
            wrong_backend,
            verified_with_error,
            automatic_retry,
        )
        for mutate in contradictions:
            with self.subTest(contradiction=mutate.__name__):
                adapter_payload = verified_adapter_receipt()
                mutate(adapter_payload)
                bridge_call = mock.Mock()
                backend = RecoveryBackend(
                    adapter_call=lambda _argv, payload=adapter_payload: (payload, False),
                    bridge_call=bridge_call,
                    receipt_validator=lambda payload, **_: None,
                )

                outcome = backend.recover(
                    GUARD,
                    LIST_ID,
                    "backend-key-raw-identity-proof",
                )

                self.assertEqual(outcome.mutation_state, "unknown")
                self.assertEqual(
                    outcome.receipt["status"],
                    "committed_verification_pending",
                )
                self.assertEqual(
                    outcome.receipt["error"]["reason_code"],
                    "invalid_recovery_receipt",
                )
                bridge_call.assert_not_called()

    def test_recovery_passes_full_private_guard_and_requires_eventkit_readback(self) -> None:
        adapter_argv: list[list[str]] = []

        def adapter_call(argv: list[str]) -> tuple[dict[str, Any], bool]:
            adapter_argv.append(argv)
            return (
                {
                    "ok": True,
                    "status": "verified",
                    "operation": "recover_deleted_reminder",
                    "operation_id": "22222222-2222-4222-8222-222222222222",
                    "backend": "reminderkit_private",
                    "target": {"reminder_id": REMINDER_ID, "list_id": LIST_ID},
                    "before": {
                        "deleted_reminder": {
                            "id": REMINDER_ID,
                            "deleted_at": GUARD.deleted_at,
                            "attachment_count": 1,
                            "attachment_digest": GUARD.attachment_digest,
                        }
                    },
                    "after": {
                        "reminder": {
                            "id": REMINDER_ID,
                            "list_id": LIST_ID,
                            "attachment_count": 1,
                            "attachment_digest": GUARD.attachment_digest,
                        }
                    },
                    "verification": {
                        "state": "read_back",
                        "write_performed": True,
                        "final_read": True,
                        "matched": True,
                        "pre_save_guard_matched": True,
                        "destination_list_matched": True,
                        "attachments_active": True,
                        "attachments_preserved": True,
                        "attachment_bytes_verified": True,
                        "attachment_counts_match": True,
                        "before_attachment_count": 1,
                        "native_attachment_count": 1,
                        "after_attachment_count": 1,
                    },
                    "recovery": {"semantics": "native_recently_deleted_recovery"},
                },
                False,
            )

        def bridge_call(
            operation: str, arguments: dict[str, Any]
        ) -> tuple[dict[str, Any], bool]:
            self.assertEqual(operation, "read_reminder")
            self.assertEqual(arguments, {"reminder_id": REMINDER_ID})
            return (
                {
                    "ok": True,
                    "status": "verified",
                    "data": {
                        "reminder": {
                            "id": REMINDER_ID,
                            "title": "Recovered",
                            "list_id": LIST_ID,
                            "last_modified": "2026-08-28T00:00:00+09:00",
                        }
                    },
                },
                False,
            )

        backend = RecoveryBackend(
            adapter_call=adapter_call,
            bridge_call=bridge_call,
            receipt_validator=lambda payload, **_: None,
        )

        outcome = backend.recover(GUARD, LIST_ID, "backend-key-0001")

        self.assertEqual(outcome.mutation_state, "committed")
        validate_public_result(
            "recover_deleted_reminder",
            {"schema_version": 2, **outcome.receipt},
            "committed",
        )
        argv = adapter_argv[0]
        self.assertIn(GUARD.store_identity, argv)
        self.assertIn(str(GUARD.private_version), argv)
        self.assertIn(GUARD.attachment_digest, argv)
        self.assertIn(GUARD.native_guard_digest, argv)
        encoded = repr(outcome.receipt)
        self.assertNotIn(GUARD.store_identity, encoded)
        self.assertNotIn(GUARD.attachment_digest, encoded)
        self.assertNotIn(GUARD.native_guard_digest, encoded)


if __name__ == "__main__":
    unittest.main()
