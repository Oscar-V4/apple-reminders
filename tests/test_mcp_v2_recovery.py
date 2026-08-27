from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from typing import Any, Mapping


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
            "attachments_preserved": True,
            "evidence_scope": ["local_native", "local_eventkit"],
        },
        "recovery": {
            "semantics": "recently_deleted_recovery",
            "automatic_retry_safe": False,
        },
    }


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


class RecoveryBackendTests(unittest.TestCase):
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
                        "attachments_preserved": True,
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
        encoded = repr(outcome.receipt)
        self.assertNotIn(GUARD.store_identity, encoded)
        self.assertNotIn(GUARD.attachment_digest, encoded)


if __name__ == "__main__":
    unittest.main()
