from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from reminders_service import (  # noqa: E402
    AdapterConflict,
    AdapterReminder,
    CoreModule,
    ReferenceRejected,
)


class DeterministicClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class DeterministicTokens:
    def __init__(self) -> None:
        self._next = 1

    def __call__(self) -> str:
        token = f"opaque-{self._next}"
        self._next += 1
        return token


class InMemoryAdapter:
    def __init__(self) -> None:
        self.store_identity = "store-alpha"
        self._reminders: dict[str, AdapterReminder] = {
            "reminder-1": AdapterReminder(
                data={"id": "reminder-1", "title": "Buy milk", "notes": "2%"},
                public_concurrency_value="public-revision-7",
            )
        }

    def read_exact(self, reminder_id: str) -> AdapterReminder:
        return self._reminders[reminder_id]

    def apply_patch(
        self,
        reminder_id: str,
        expected_public_concurrency_value: str,
        patch: dict[str, Any],
    ) -> None:
        current = self._reminders[reminder_id]
        if current.public_concurrency_value != expected_public_concurrency_value:
            raise AdapterConflict
        updated = dict(current.data)
        updated.update(patch)
        revision_number = int(current.public_concurrency_value.rsplit("-", 1)[1]) + 1
        self._reminders[reminder_id] = AdapterReminder(
            data=updated,
            public_concurrency_value=f"public-revision-{revision_number}",
        )


class CoreModuleTests(unittest.TestCase):
    def test_exact_read_returns_reminder_and_an_opaque_reference(self) -> None:
        module = CoreModule(
            InMemoryAdapter(),
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
            reference_ttl_seconds=30.0,
        )

        result = module.read_exact("reminder-1")

        self.assertEqual(
            result.reminder,
            {"id": "reminder-1", "title": "Buy milk", "notes": "2%"},
        )
        self.assertEqual(result.reference, "opaque-1")
        self.assertNotIn("reminder-1", result.reference)
        self.assertNotIn("store-alpha", result.reference)
        self.assertNotIn("public-revision-7", result.reference)

    def test_change_preserves_omitted_fields_and_returns_verified_read_back(self) -> None:
        module = CoreModule(
            InMemoryAdapter(),
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
            reference_ttl_seconds=30.0,
        )
        initial = module.read_exact("reminder-1")

        receipt = module.change(initial.reference, {"title": "Buy oat milk"})

        self.assertEqual(receipt.status, "verified")
        self.assertEqual(
            receipt.after,
            {"id": "reminder-1", "title": "Buy oat milk", "notes": "2%"},
        )
        self.assertEqual(receipt.verification, {"state": "exact_read_back"})
        self.assertEqual(receipt.reference, "opaque-2")
        self.assertNotEqual(receipt.reference, initial.reference)

    def test_change_rejects_an_unknown_reference_without_mutation(self) -> None:
        module = CoreModule(
            InMemoryAdapter(),
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
            reference_ttl_seconds=30.0,
        )

        with self.assertRaises(ReferenceRejected) as raised:
            module.change("forged-reference", {"title": "Do not write"})

        self.assertEqual(raised.exception.code, "invalid_reference")
        self.assertEqual(
            module.read_exact("reminder-1").reminder,
            {"id": "reminder-1", "title": "Buy milk", "notes": "2%"},
        )

    def test_change_rejects_an_expired_reference_without_mutation(self) -> None:
        clock = DeterministicClock()
        module = CoreModule(
            InMemoryAdapter(),
            clock=clock,
            token_source=DeterministicTokens(),
            reference_ttl_seconds=30.0,
        )
        initial = module.read_exact("reminder-1")
        clock.now = 130.0

        with self.assertRaises(ReferenceRejected) as raised:
            module.change(initial.reference, {"title": "Do not write"})

        self.assertEqual(raised.exception.code, "expired_reference")
        self.assertEqual(
            module.read_exact("reminder-1").reminder,
            {"id": "reminder-1", "title": "Buy milk", "notes": "2%"},
        )

    def test_change_rejects_a_stale_reference_without_overwriting_newer_data(self) -> None:
        adapter = InMemoryAdapter()
        module = CoreModule(
            adapter,
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
            reference_ttl_seconds=30.0,
        )
        initial = module.read_exact("reminder-1")
        adapter.apply_patch(
            "reminder-1",
            "public-revision-7",
            {"title": "Changed on iPhone"},
        )

        with self.assertRaises(ReferenceRejected) as raised:
            module.change(initial.reference, {"notes": "Do not overwrite"})

        self.assertEqual(raised.exception.code, "concurrent_modification")
        self.assertEqual(
            module.read_exact("reminder-1").reminder,
            {"id": "reminder-1", "title": "Changed on iPhone", "notes": "2%"},
        )

    def test_reference_is_bound_to_the_store_that_issued_it(self) -> None:
        adapter = InMemoryAdapter()
        module = CoreModule(
            adapter,
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
            reference_ttl_seconds=30.0,
        )
        initial = module.read_exact("reminder-1")
        adapter.store_identity = "store-beta"

        with self.assertRaises(ReferenceRejected) as raised:
            module.change(initial.reference, {"title": "Do not write"})

        self.assertEqual(raised.exception.code, "invalid_reference")
        self.assertEqual(
            module.read_exact("reminder-1").reminder,
            {"id": "reminder-1", "title": "Buy milk", "notes": "2%"},
        )

    def test_reference_storage_prunes_expiry_and_evicts_the_oldest_grant(self) -> None:
        clock = DeterministicClock()
        module = CoreModule(
            InMemoryAdapter(),
            clock=clock,
            token_source=DeterministicTokens(),
            reference_ttl_seconds=30.0,
            max_active_references=2,
        )
        expired = module.read_exact("reminder-1")
        clock.now = 130.0
        oldest_active = module.read_exact("reminder-1")
        module.read_exact("reminder-1")
        newest = module.read_exact("reminder-1")

        for rejected_reference in (expired.reference, oldest_active.reference):
            with self.assertRaises(ReferenceRejected) as raised:
                module.change(rejected_reference, {"title": "Do not write"})
            self.assertEqual(raised.exception.code, "invalid_reference")

        receipt = module.change(newest.reference, {"title": "Newest wins"})
        self.assertEqual(receipt.status, "verified")
        self.assertEqual(receipt.after["title"], "Newest wins")


if __name__ == "__main__":
    unittest.main()
