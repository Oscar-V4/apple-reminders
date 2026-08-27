from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from reminders_service import (  # noqa: E402
    ActionRejected,
    AdapterConflict,
    AdapterContractError,
    CoreModule,
    Guard,
    MoveToListAction,
    MutationOutcome,
    MutationOutcomeRejected,
    MutationOutcomeUnknown,
    PatchAction,
    ReferenceRejected,
    SetCompletionAction,
    Snapshot,
    reminder_matches_fields,
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
        self.next_outcome: MutationOutcome | None = None
        self.fail_next_read = False
        self._reminders: dict[str, Snapshot] = {
            "reminder-1": Snapshot(
                reminder={"id": "reminder-1", "title": "Buy milk", "notes": "2%"},
                guard=Guard(
                    reminder_id="reminder-1",
                    store_identity="store-alpha",
                    public_concurrency_value="public-revision-7",
                ),
            )
        }

    def read_exact(self, reminder_id: str) -> Snapshot:
        if self.fail_next_read:
            self.fail_next_read = False
            raise RuntimeError("final read unavailable")
        return self._reminders[reminder_id]

    def apply_action(
        self,
        guard: Guard,
        action: PatchAction | SetCompletionAction | MoveToListAction,
    ) -> MutationOutcome:
        current = self._reminders[guard.reminder_id]
        if current.guard != guard:
            code = (
                "invalid_reference"
                if current.guard.store_identity != guard.store_identity
                else "concurrent_modification"
            )
            raise AdapterConflict(code)
        before = deepcopy(dict(current.reminder))
        updated = dict(current.reminder)
        operation = "update_reminder"
        if isinstance(action, PatchAction):
            updated.update(action.patch)
        elif isinstance(action, SetCompletionAction):
            operation = "complete_reminder" if action.completed else "reopen_reminder"
            updated["completed"] = action.completed
        elif isinstance(action, MoveToListAction):
            operation = "move_reminder"
            updated["list_id"] = action.list_id
        preset_outcome = self.next_outcome
        self.next_outcome = None
        if (
            preset_outcome is not None
            and preset_outcome.mutation_state == "not_mutated"
        ):
            return preset_outcome
        revision_number = int(
            current.guard.public_concurrency_value.rsplit("-", 1)[1]
        ) + 1
        self._reminders[guard.reminder_id] = Snapshot(
            reminder=updated,
            guard=Guard(
                reminder_id=guard.reminder_id,
                store_identity=self.store_identity,
                public_concurrency_value=f"public-revision-{revision_number}",
            ),
        )
        if preset_outcome is not None:
            return preset_outcome
        return MutationOutcome(
            receipt={
                "ok": True,
                "status": "verified",
                "operation": operation,
                "operation_id": "operation-1",
                "backend": "eventkit_public_sdk",
                "target": {"id": guard.reminder_id, "store_identity": guard.store_identity},
                "before": before,
                "after": deepcopy(updated),
                "verification": {"state": "read_back", "matched": True},
                "recovery": {"semantics": "reapply_previous_values"},
            },
            mutation_state="committed",
        )

    def external_patch(self, reminder_id: str, patch: dict[str, Any]) -> None:
        current = self._reminders[reminder_id]
        self.apply_action(current.guard, PatchAction(patch))

    def external_store_change(self, reminder_id: str, store_identity: str) -> None:
        current = self._reminders[reminder_id]
        self.store_identity = store_identity
        self._reminders[reminder_id] = Snapshot(
            reminder=current.reminder,
            guard=Guard(
                reminder_id=reminder_id,
                store_identity=store_identity,
                public_concurrency_value=current.guard.public_concurrency_value,
            ),
        )


class CoreModuleTests(unittest.TestCase):
    def test_field_matcher_accepts_equivalent_timezones_and_alarm_order(self) -> None:
        expected = {
            "due": {
                "kind": "timed",
                "date_time": "2026-08-28T09:00:00+09:00",
                "time_zone": "Asia/Seoul",
            },
            "alarms": [
                {"kind": "absolute", "date_time": "2026-08-28T00:00:00Z"},
                {"kind": "relative", "offset_seconds": -900},
            ],
        }
        actual = {
            "due": {
                "kind": "timed",
                "date_time": "2026-08-28T00:00:00Z",
                "time_zone": "Asia/Seoul",
                "local_date_time": "2026-08-28T09:00:00",
            },
            "alarms": [
                {"kind": "relative", "offset_seconds": -900, "read_only": False},
                {"kind": "absolute", "date_time": "2026-08-28T09:00:00+09:00"},
            ],
        }

        self.assertTrue(reminder_matches_fields(actual, expected))
        self.assertTrue(
            reminder_matches_fields(
                {"alarms": [], "recurrence_rules": []},
                {"alarms": None, "recurrence_rules": None},
            )
        )

    def test_adapter_exception_after_dispatch_consumes_the_reference(self) -> None:
        class ThrowingAdapter(InMemoryAdapter):
            def apply_action(
                self,
                guard: Guard,
                action: PatchAction | SetCompletionAction | MoveToListAction,
            ) -> MutationOutcome:
                raise RuntimeError("native process disappeared after dispatch")

        module = CoreModule(
            ThrowingAdapter(),
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        exact = module.read_exact("reminder-1")

        with self.assertRaises(MutationOutcomeUnknown):
            module.change(
                exact.reference,
                {"kind": "patch", "patch": {"title": "Possibly committed"}},
            )

        with self.assertRaises(ReferenceRejected) as consumed:
            module.revalidate_reference(exact.reference)
        self.assertEqual(consumed.exception.code, "invalid_reference")

    def test_reference_port_revalidates_and_can_invalidate_a_shared_guard(self) -> None:
        adapter = InMemoryAdapter()
        module = CoreModule(
            adapter,
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        exact = module.read_exact("reminder-1")

        guard = module.revalidate_reference(exact.reference)

        self.assertEqual(guard.reminder_id, "reminder-1")
        self.assertEqual(guard.store_identity, "store-alpha")
        self.assertEqual(guard.public_concurrency_value, "public-revision-7")
        module.invalidate_reference(exact.reference)
        with self.assertRaises(ReferenceRejected) as raised:
            module.revalidate_reference(exact.reference)
        self.assertEqual(raised.exception.code, "invalid_reference")

    def test_reference_port_rejects_and_consumes_a_concurrently_changed_guard(self) -> None:
        adapter = InMemoryAdapter()
        module = CoreModule(
            adapter,
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        exact = module.read_exact("reminder-1")
        adapter.external_patch("reminder-1", {"title": "Changed elsewhere"})

        with self.assertRaises(ReferenceRejected) as raised:
            module.revalidate_reference(exact.reference)

        self.assertEqual(raised.exception.code, "concurrent_modification")
        with self.assertRaises(ReferenceRejected) as consumed:
            module.revalidate_reference(exact.reference)
        self.assertEqual(consumed.exception.code, "invalid_reference")

    def test_reference_port_consumes_a_grant_when_revalidation_cannot_finish(self) -> None:
        adapter = InMemoryAdapter()
        module = CoreModule(
            adapter,
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        exact = module.read_exact("reminder-1")
        adapter.fail_next_read = True

        with self.assertRaises(RuntimeError):
            module.revalidate_reference(exact.reference)

        with self.assertRaises(ReferenceRejected) as consumed:
            module.revalidate_reference(exact.reference)
        self.assertEqual(consumed.exception.code, "invalid_reference")

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
        self.assertEqual(result.reference, "rev1.opaque-1")
        self.assertNotIn("reminder-1", result.reference)
        self.assertNotIn("store-alpha", result.reference)
        self.assertNotIn("public-revision-7", result.reference)

    def test_default_reference_uses_the_versioned_public_prefix(self) -> None:
        result = CoreModule(InMemoryAdapter()).read_exact("reminder-1")

        self.assertRegex(result.reference, r"^rev1\.[A-Za-z0-9_-]{32,}$")

    def test_exact_read_rejects_a_snapshot_for_a_different_reminder(self) -> None:
        class WrongIdentityAdapter(InMemoryAdapter):
            def read_exact(self, reminder_id: str) -> Snapshot:
                return Snapshot(
                    reminder={"id": "reminder-2", "title": "Wrong Reminder"},
                    guard=Guard(
                        reminder_id=reminder_id,
                        store_identity="store-alpha",
                        public_concurrency_value="public-revision-7",
                    ),
                )

        module = CoreModule(
            WrongIdentityAdapter(),
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )

        with self.assertRaises(AdapterContractError):
            module.read_exact("reminder-1")

    def test_change_preserves_omitted_fields_and_returns_verified_read_back(self) -> None:
        module = CoreModule(
            InMemoryAdapter(),
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
            reference_ttl_seconds=30.0,
        )
        initial = module.read_exact("reminder-1")

        result = module.change(
            initial.reference,
            {"kind": "patch", "patch": {"title": "Buy oat milk"}},
        )

        self.assertEqual(
            result.receipt,
            {
                "ok": True,
                "status": "verified",
                "operation": "update_reminder",
                "operation_id": "operation-1",
                "backend": "eventkit_public_sdk",
                "target": {"id": "reminder-1", "store_identity": "store-alpha"},
                "before": {"id": "reminder-1", "title": "Buy milk", "notes": "2%"},
                "after": {
                    "id": "reminder-1",
                    "title": "Buy oat milk",
                    "notes": "2%",
                },
                "verification": {"state": "read_back", "matched": True},
                "recovery": {"semantics": "reapply_previous_values"},
            },
        )
        self.assertEqual(result.reference, "rev1.opaque-2")
        self.assertEqual(result.final_reminder, result.receipt["after"])
        self.assertNotEqual(result.reference, initial.reference)
        follow_up = module.change(
            result.reference,
            {"kind": "patch", "patch": {"notes": "Use the fresh Guard"}},
        )
        self.assertEqual(follow_up.receipt["status"], "verified")

    def test_change_rejects_a_canonical_final_read_that_misses_the_action(self) -> None:
        class DriftingAdapter(InMemoryAdapter):
            def apply_action(
                self,
                guard: Guard,
                action: PatchAction | SetCompletionAction | MoveToListAction,
            ) -> MutationOutcome:
                outcome = super().apply_action(guard, action)
                current = self._reminders[guard.reminder_id]
                self._reminders[guard.reminder_id] = Snapshot(
                    reminder={**dict(current.reminder), "title": "Conflicting title"},
                    guard=current.guard,
                )
                return outcome

        module = CoreModule(
            DriftingAdapter(),
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        initial = module.read_exact("reminder-1")

        result = module.change(
            initial.reference,
            {"kind": "patch", "patch": {"title": "Requested title"}},
        )

        self.assertIsNone(result.reference)
        self.assertIsNone(result.final_reminder)
        self.assertEqual(result.reference_error, "final_state_mismatch")
        with self.assertRaises(ReferenceRejected):
            module.revalidate_reference(initial.reference)

    def test_closed_completion_and_list_move_actions_reach_the_adapter(self) -> None:
        cases = (
            (
                {"kind": "set_completion", "completed": True},
                "complete_reminder",
                {"completed": True},
            ),
            (
                {"kind": "set_completion", "completed": False},
                "reopen_reminder",
                {"completed": False},
            ),
            (
                {"kind": "move_to_list", "list_id": "list-beta"},
                "move_reminder",
                {"list_id": "list-beta"},
            ),
        )
        for action, expected_operation, expected_fields in cases:
            with self.subTest(action=action):
                module = CoreModule(
                    InMemoryAdapter(),
                    clock=DeterministicClock(),
                    token_source=DeterministicTokens(),
                )
                initial = module.read_exact("reminder-1")

                result = module.change(initial.reference, action)

                self.assertEqual(result.receipt["operation"], expected_operation)
                for field, value in expected_fields.items():
                    self.assertEqual(result.receipt["after"][field], value)
                self.assertEqual(result.reference, "rev1.opaque-2")

    def test_unknown_or_empty_actions_are_rejected_without_mutation(self) -> None:
        invalid_actions = (
            {},
            {"kind": "delete"},
            {"type": "patch", "patch": {"title": "Wrong discriminator"}},
            {"kind": "patch", "patch": {}},
            {"kind": "patch", "patch": {"id": "another-reminder"}},
            {"kind": "set_completion"},
            {"kind": "move_to_list", "list_id": ""},
        )
        for action in invalid_actions:
            with self.subTest(action=action):
                module = CoreModule(
                    InMemoryAdapter(),
                    clock=DeterministicClock(),
                    token_source=DeterministicTokens(),
                )
                initial = module.read_exact("reminder-1")

                with self.assertRaises(ActionRejected) as raised:
                    module.change(initial.reference, action)

                self.assertEqual(raised.exception.code, "invalid_action")
                self.assertEqual(
                    module.read_exact("reminder-1").reminder,
                    {"id": "reminder-1", "title": "Buy milk", "notes": "2%"},
                )

    def test_url_partial_and_pending_receipts_are_preserved_without_a_reference(self) -> None:
        for status in ("partial_success", "committed_verification_pending"):
            with self.subTest(status=status):
                adapter = InMemoryAdapter()
                receipt = {
                    "ok": True,
                    "status": status,
                    "operation": "update_reminder",
                    "operation_id": "hybrid-operation-9",
                    "backend": "eventkit_public_sdk+reminderkit_private",
                    "target": {"id": "reminder-1", "store_identity": "store-alpha"},
                    "before": {
                        "id": "reminder-1",
                        "title": "Buy milk",
                        "notes": "2%",
                    },
                    "after": {
                        "id": "reminder-1",
                        "title": "Buy milk",
                        "notes": "2%",
                        "url": "https://example.com",
                    },
                    "verification": {
                        "state": "pending",
                        "url_attachment": {"state": "failed"},
                    },
                    "recovery": {
                        "semantics": "retry_visible_url_attachment_after_fresh_read"
                    },
                    "warnings": [
                        {
                            "code": "native_url_attachment_failed",
                            "message": "Visible URL attachment was not verified.",
                        }
                    ],
                    "error": {
                        "code": "native_url_attachment_failed",
                        "message": "EventKit committed before the native step failed.",
                    },
                }
                adapter.next_outcome = MutationOutcome(
                    receipt=receipt,
                    mutation_state="committed",
                )
                module = CoreModule(
                    adapter,
                    clock=DeterministicClock(),
                    token_source=DeterministicTokens(),
                )
                initial = module.read_exact("reminder-1")

                result = module.change(
                    initial.reference,
                    {
                        "kind": "patch",
                        "patch": {"url": "https://example.com"},
                    },
                )

                self.assertEqual(result.receipt, receipt)
                self.assertIsNot(result.receipt, receipt)
                self.assertIsNone(result.reference)
                self.assertIsNone(result.final_reminder)
                with self.assertRaises(ReferenceRejected):
                    module.change(
                        initial.reference,
                        {"kind": "patch", "patch": {"notes": "unsafe retry"}},
                    )

    def test_unknown_mutation_outcome_consumes_the_previous_reference(self) -> None:
        adapter = InMemoryAdapter()
        receipt = {
            "ok": True,
            "status": "committed_verification_pending",
            "operation": "update_reminder",
            "operation_id": "unknown-operation-4",
            "backend": "eventkit_public_sdk",
            "target": {"id": "reminder-1", "store_identity": "store-alpha"},
            "before": {"id": "reminder-1", "title": "Buy milk", "notes": "2%"},
            "after": {},
            "verification": {"state": "pending"},
            "recovery": {"semantics": "read_before_retry"},
            "warnings": [{"code": "bridge_timeout", "message": "Outcome unknown."}],
            "error": {
                "code": "bridge_timeout",
                "message": "The mutation outcome is unknown.",
                "mutation_outcome_unknown": True,
            },
        }
        adapter.next_outcome = MutationOutcome(
            receipt=receipt,
            mutation_state="unknown",
        )
        module = CoreModule(
            adapter,
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        initial = module.read_exact("reminder-1")

        result = module.change(
            initial.reference,
            {"kind": "patch", "patch": {"title": "Possibly written"}},
        )

        self.assertEqual(result.receipt, receipt)
        self.assertIsNone(result.reference)
        self.assertIsNone(result.final_reminder)
        self.assertEqual(result.reference_error, "mutation_outcome_unknown")
        with self.assertRaises(ReferenceRejected) as raised:
            module.change(
                initial.reference,
                {"kind": "patch", "patch": {"title": "Unsafe retry"}},
            )
        self.assertEqual(raised.exception.code, "invalid_reference")

    def test_committed_change_with_final_read_failure_consumes_reference(self) -> None:
        adapter = InMemoryAdapter()
        module = CoreModule(
            adapter,
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        initial = module.read_exact("reminder-1")
        adapter.fail_next_read = True

        result = module.change(
            initial.reference,
            {"kind": "patch", "patch": {"title": "Committed title"}},
        )

        self.assertEqual(result.receipt["status"], "verified")
        self.assertIsNone(result.reference)
        self.assertIsNone(result.final_reminder)
        self.assertEqual(result.reference_error, "final_read_failed")
        with self.assertRaises(ReferenceRejected) as raised:
            module.change(
                initial.reference,
                {"kind": "patch", "patch": {"notes": "Unsafe retry"}},
            )
        self.assertEqual(raised.exception.code, "invalid_reference")

    def test_malformed_committed_outcome_still_consumes_reference(self) -> None:
        adapter = InMemoryAdapter()
        adapter.next_outcome = MutationOutcome(
            receipt={"status": "verified"},
            mutation_state="committed",
        )
        module = CoreModule(
            adapter,
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        initial = module.read_exact("reminder-1")

        with self.assertRaises(AdapterContractError):
            module.change(
                initial.reference,
                {"kind": "patch", "patch": {"title": "Committed title"}},
            )

        with self.assertRaises(ReferenceRejected) as raised:
            module.change(
                initial.reference,
                {"kind": "patch", "patch": {"notes": "Unsafe retry"}},
            )
        self.assertEqual(raised.exception.code, "invalid_reference")

    def test_receipt_status_must_agree_with_ok_and_mutation_state(self) -> None:
        malformed_receipts = (
            MutationOutcome(
                receipt={
                    "ok": True,
                    "status": "verified",
                    "operation": "update_reminder",
                    "operation_id": "operation-state-mismatch",
                    "backend": "eventkit_public_sdk",
                    "target": {"id": "reminder-1"},
                    "before": {"id": "reminder-1"},
                    "after": {"id": "reminder-1"},
                    "verification": {"state": "read_back"},
                    "recovery": {"semantics": "not_applicable"},
                },
                mutation_state="not_mutated",
            ),
            MutationOutcome(
                receipt={
                    "ok": False,
                    "status": "verified",
                    "operation": "update_reminder",
                    "operation_id": "operation-ok-mismatch",
                    "backend": "eventkit_public_sdk",
                    "target": {"id": "reminder-1"},
                    "before": {"id": "reminder-1"},
                    "after": {"id": "reminder-1"},
                    "verification": {"state": "read_back"},
                    "recovery": {"semantics": "not_applicable"},
                },
                mutation_state="committed",
            ),
        )
        for outcome in malformed_receipts:
            with self.subTest(outcome=outcome):
                adapter = InMemoryAdapter()
                adapter.next_outcome = outcome
                module = CoreModule(
                    adapter,
                    clock=DeterministicClock(),
                    token_source=DeterministicTokens(),
                )
                initial = module.read_exact("reminder-1")

                with self.assertRaises(MutationOutcomeRejected) as raised:
                    module.change(
                        initial.reference,
                        {"kind": "patch", "patch": {"title": "Unsafe"}},
                    )
                self.assertEqual(
                    raised.exception.mutation_state,
                    outcome.mutation_state,
                )

                with self.assertRaises(ReferenceRejected):
                    module.change(
                        initial.reference,
                        {"kind": "patch", "patch": {"title": "No retry"}},
                    )

    def test_unchanged_outcome_gets_a_fresh_reference_after_canonical_read(self) -> None:
        adapter = InMemoryAdapter()
        receipt = {
            "ok": True,
            "status": "unchanged",
            "operation": "update_reminder",
            "operation_id": "unchanged-operation-2",
            "backend": "eventkit_public_sdk",
            "target": {"id": "reminder-1", "store_identity": "store-alpha"},
            "before": {"id": "reminder-1", "title": "Buy milk", "notes": "2%"},
            "after": {"id": "reminder-1", "title": "Buy milk", "notes": "2%"},
            "verification": {"state": "not_needed", "write_performed": False},
            "recovery": {"semantics": "not_applicable"},
        }
        adapter.next_outcome = MutationOutcome(
            receipt=receipt,
            mutation_state="not_mutated",
        )
        module = CoreModule(
            adapter,
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        initial = module.read_exact("reminder-1")

        result = module.change(
            initial.reference,
            {"kind": "patch", "patch": {"title": "Buy milk"}},
        )

        self.assertEqual(result.receipt, receipt)
        self.assertEqual(result.reference, "rev1.opaque-2")
        self.assertNotEqual(result.reference, initial.reference)

    def test_failure_receipt_states_are_preserved_with_their_mutation_semantics(self) -> None:
        cases = (
            ("failed_no_mutation", "not_mutated", True),
            ("failed_manual_repair_required", "committed", False),
        )
        for status, mutation_state, old_reference_reusable in cases:
            with self.subTest(status=status):
                adapter = InMemoryAdapter()
                receipt = {
                    "ok": False,
                    "status": status,
                    "operation": "update_reminder",
                    "operation_id": f"{status}-operation",
                    "backend": "eventkit_public_sdk",
                    "target": {"id": "reminder-1", "store_identity": "store-alpha"},
                    "before": {
                        "id": "reminder-1",
                        "title": "Buy milk",
                        "notes": "2%",
                    },
                    "after": {},
                    "verification": {"state": "failed"},
                    "recovery": {"semantics": "read_before_retry"},
                    "error": {"code": status, "message": "Synthetic failure."},
                }
                adapter.next_outcome = MutationOutcome(
                    receipt=receipt,
                    mutation_state=mutation_state,
                )
                module = CoreModule(
                    adapter,
                    clock=DeterministicClock(),
                    token_source=DeterministicTokens(),
                )
                initial = module.read_exact("reminder-1")

                result = module.change(
                    initial.reference,
                    {"kind": "patch", "patch": {"title": "Requested title"}},
                )

                self.assertEqual(result.receipt, receipt)
                self.assertIsNone(result.reference)
                if old_reference_reusable:
                    retry = module.change(
                        initial.reference,
                        {"kind": "patch", "patch": {"title": "Safe retry"}},
                    )
                    self.assertEqual(retry.receipt["status"], "verified")
                else:
                    with self.assertRaises(ReferenceRejected) as raised:
                        module.change(
                            initial.reference,
                            {"kind": "patch", "patch": {"title": "Unsafe retry"}},
                        )
                    self.assertEqual(raised.exception.code, "invalid_reference")

    def test_change_rejects_an_unknown_reference_without_mutation(self) -> None:
        module = CoreModule(
            InMemoryAdapter(),
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
            reference_ttl_seconds=30.0,
        )

        with self.assertRaises(ReferenceRejected) as raised:
            module.change(
                "forged-reference",
                {"kind": "patch", "patch": {"title": "Do not write"}},
            )

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
            module.change(
                initial.reference,
                {"kind": "patch", "patch": {"title": "Do not write"}},
            )

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
        adapter.external_patch("reminder-1", {"title": "Changed on iPhone"})

        with self.assertRaises(ReferenceRejected) as raised:
            module.change(
                initial.reference,
                {"kind": "patch", "patch": {"notes": "Do not overwrite"}},
            )

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
        adapter.external_store_change("reminder-1", "store-beta")

        with self.assertRaises(ReferenceRejected) as raised:
            module.change(
                initial.reference,
                {"kind": "patch", "patch": {"title": "Do not write"}},
            )

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
                module.change(
                    rejected_reference,
                    {"kind": "patch", "patch": {"title": "Do not write"}},
                )
            self.assertEqual(raised.exception.code, "invalid_reference")

        result = module.change(
            newest.reference,
            {"kind": "patch", "patch": {"title": "Newest wins"}},
        )
        self.assertEqual(result.receipt["status"], "verified")
        self.assertEqual(result.receipt["after"]["title"], "Newest wins")


if __name__ == "__main__":
    unittest.main()
