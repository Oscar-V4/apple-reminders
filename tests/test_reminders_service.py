from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest import mock


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
    STABLE_USER_FIELDS,
    UnsupportedAction,
    canonical_action_projection,
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


class ReadOnlyProcedureAlarmAdapter(InMemoryAdapter):
    def __init__(self, *, drift_action_metadata: bool = False) -> None:
        super().__init__()
        self._drift_action_metadata = drift_action_metadata
        current = self._reminders["reminder-1"]
        self._reminders["reminder-1"] = Snapshot(
            reminder={
                **dict(current.reminder),
                "alarms": [
                    {
                        "kind": "absolute",
                        "date_time": "2027-08-17T00:00:00.000Z",
                        "read_only": True,
                        "action": {
                            "type": "procedure",
                            "url": "example:before",
                        },
                    }
                ],
            },
            guard=current.guard,
        )

    def apply_action(
        self,
        guard: Guard,
        action: PatchAction | SetCompletionAction | MoveToListAction,
    ) -> MutationOutcome:
        outcome = super().apply_action(guard, action)
        if not self._drift_action_metadata:
            return outcome
        current = self._reminders[guard.reminder_id]
        after = deepcopy(dict(current.reminder))
        after["alarms"][0]["action"]["url"] = "example:after"
        self._reminders[guard.reminder_id] = Snapshot(
            reminder=after,
            guard=current.guard,
        )
        return outcome


class SemanticReminderAdapter(InMemoryAdapter):
    def __init__(
        self,
        *,
        alarm: dict[str, Any],
        completed: bool = False,
        recurring: bool = True,
        provider_drift: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._provider_drift = deepcopy(provider_drift)
        current = self._reminders["reminder-1"]
        self._reminders["reminder-1"] = Snapshot(
            reminder={
                "id": "reminder-1",
                "title": "Original title",
                "notes": "Stable notes",
                "url": "https://example.test/reminder",
                "location": "Stable location",
                "priority": 5,
                "completed": completed,
                "due": {"kind": "all_day", "date": "2027-08-31"},
                "start": None,
                "alarms": [deepcopy(alarm)],
                "recurrence_rules": [{"frequency": "weekly", "interval": 1}] if recurring else [],
                "list_id": "list-alpha",
            },
            guard=current.guard,
        )

    def apply_action(
        self,
        guard: Guard,
        action: PatchAction | SetCompletionAction | MoveToListAction,
    ) -> MutationOutcome:
        outcome = super().apply_action(guard, action)
        if self._provider_drift is None:
            return outcome
        current = self._reminders[guard.reminder_id]
        self._reminders[guard.reminder_id] = Snapshot(
            reminder={
                **deepcopy(dict(current.reminder)),
                **deepcopy(self._provider_drift),
            },
            guard=current.guard,
        )
        return outcome


SEMANTIC_ALARMS: dict[str, dict[str, Any]] = {
    "absolute": {
        "kind": "absolute",
        "date_time": "2027-08-17T00:00:00.000Z",
    },
    "location": {
        "kind": "location",
        "proximity": "enter",
        "location": {
            "title": "Office",
            "latitude": 37.5,
            "longitude": 127.0,
            "radius_meters": 100.0,
        },
    },
    "writable_relative": {"kind": "relative", "offset_seconds": -900},
    "read_only": {
        "kind": "absolute",
        "date_time": "2027-08-17T00:00:00.000Z",
        "read_only": True,
        "action": {"type": "procedure", "url": "example:before"},
    },
}


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

    def test_only_alarm_arrays_are_order_insensitive(self) -> None:
        expected = {
            "recurrence_rules": [
                {
                    "frequency": "weekly",
                    "interval": 1,
                    "days_of_week": [{"day": "monday"}, {"day": "tuesday"}],
                }
            ]
        }
        reordered = {
            "recurrence_rules": [
                {
                    "frequency": "weekly",
                    "interval": 1,
                    "days_of_week": [{"day": "tuesday"}, {"day": "monday"}],
                }
            ]
        }

        self.assertFalse(reminder_matches_fields(reordered, expected))

    def test_relative_alarm_action_drift_never_matches_display_write(self) -> None:
        expected = {
            "alarms": [{"kind": "relative", "offset_seconds": -900}]
        }
        actual = {
            "alarms": [
                {
                    "kind": "relative",
                    "offset_seconds": -900,
                    "read_only": True,
                    "action": {"type": "audio", "sound_name": "Glass"},
                }
            ]
        }

        self.assertFalse(reminder_matches_fields(actual, expected))

    def test_lossy_read_only_alarm_projection_is_never_verifiable(self) -> None:
        alarm = {
            "kind": "relative",
            "offset_seconds": None,
            "read_only": True,
            "_verification_unavailable": True,
            "action": {"type": "display"},
        }

        self.assertFalse(
            reminder_matches_fields(
                {"alarms": [deepcopy(alarm)]},
                {"alarms": [deepcopy(alarm)]},
            )
        )

    def test_newly_lossy_actual_alarm_projection_is_never_verifiable(self) -> None:
        expected = {
            "kind": "absolute",
            "date_time": "2027-08-17T00:00:00.000Z",
            "read_only": True,
            "action": {"type": "procedure", "url": "example:run"},
        }
        actual = {**deepcopy(expected), "_verification_unavailable": True}

        self.assertFalse(
            reminder_matches_fields(
                {"alarms": [actual]},
                {"alarms": [expected]},
            )
        )

    def test_nonempty_alarm_replacement_rejects_existing_read_only_alarm(self) -> None:
        class ReadOnlyAlarmAdapter(InMemoryAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.apply_calls = 0
                current = self._reminders["reminder-1"]
                self._reminders["reminder-1"] = Snapshot(
                    reminder={
                        **dict(current.reminder),
                        "due": {"kind": "all_day", "date": "2027-08-31"},
                        "alarms": [
                            {
                                "kind": "relative",
                                "offset_seconds": -900,
                                "read_only": True,
                                "action": {
                                    "type": "audio",
                                    "sound_name": "Glass",
                                },
                            }
                        ],
                    },
                    guard=current.guard,
                )

            def apply_action(
                self,
                guard: Guard,
                action: PatchAction | SetCompletionAction | MoveToListAction,
            ) -> MutationOutcome:
                self.apply_calls += 1
                return super().apply_action(guard, action)

        adapter = ReadOnlyAlarmAdapter()
        module = CoreModule(
            adapter,
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        initial = module.read_exact("reminder-1")

        with self.assertRaises(ActionRejected):
            module.change(
                initial.reference,
                {
                    "kind": "patch",
                    "patch": {
                        "alarms": [
                            {"kind": "relative", "offset_seconds": -1_800}
                        ]
                    },
                },
            )

        self.assertEqual(adapter.apply_calls, 0)

    def test_due_clear_requires_joint_relative_alarm_clear_or_replacement(self) -> None:
        class RelativeAlarmAdapter(InMemoryAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.apply_calls = 0
                current = self._reminders["reminder-1"]
                self._reminders["reminder-1"] = Snapshot(
                    reminder={
                        **dict(current.reminder),
                        "due": {"kind": "all_day", "date": "2027-08-31"},
                        "alarms": [
                            {"kind": "relative", "offset_seconds": -900}
                        ],
                    },
                    guard=current.guard,
                )

            def apply_action(
                self,
                guard: Guard,
                action: PatchAction | SetCompletionAction | MoveToListAction,
            ) -> MutationOutcome:
                self.apply_calls += 1
                return super().apply_action(guard, action)

        adapter = RelativeAlarmAdapter()
        module = CoreModule(
            adapter,
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        initial = module.read_exact("reminder-1")

        with self.assertRaises(ActionRejected):
            module.change(
                initial.reference,
                {"kind": "patch", "patch": {"due": None}},
            )

        self.assertEqual(adapter.apply_calls, 0)

        preserving_adapter = RelativeAlarmAdapter()
        preserving_module = CoreModule(
            preserving_adapter,
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        preserving_initial = preserving_module.read_exact("reminder-1")
        preserving_result = preserving_module.change(
            preserving_initial.reference,
            {
                "kind": "patch",
                "patch": {
                    "due": {"kind": "all_day", "date": "2027-09-30"}
                },
            },
        )
        self.assertEqual(preserving_result.receipt["status"], "verified")
        self.assertEqual(
            preserving_result.final_reminder["alarms"],
            [{"kind": "relative", "offset_seconds": -900}],
        )

        allowed_alarm_values = (
            None,
            [],
            [
                {
                    "kind": "absolute",
                    "date_time": "2027-09-30T09:00:00.000Z",
                }
            ],
        )
        for alarms in allowed_alarm_values:
            with self.subTest(alarms=alarms):
                allowed_adapter = RelativeAlarmAdapter()
                allowed_module = CoreModule(
                    allowed_adapter,
                    clock=DeterministicClock(),
                    token_source=DeterministicTokens(),
                )
                allowed_initial = allowed_module.read_exact("reminder-1")
                result = allowed_module.change(
                    allowed_initial.reference,
                    {
                        "kind": "patch",
                        "patch": {"due": None, "alarms": alarms},
                    },
                )

                self.assertEqual(result.receipt["status"], "verified")
                self.assertEqual(allowed_adapter.apply_calls, 1)

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

    def test_title_patch_rejects_final_read_that_loses_absolute_alarm(self) -> None:
        class AbsoluteAlarmDriftingAdapter(InMemoryAdapter):
            def __init__(self) -> None:
                super().__init__()
                current = self._reminders["reminder-1"]
                self._reminders["reminder-1"] = Snapshot(
                    reminder={
                        **dict(current.reminder),
                        "alarms": [
                            {
                                "kind": "absolute",
                                "date_time": "2027-08-17T00:00:00.000Z",
                            }
                        ],
                    },
                    guard=current.guard,
                )

            def apply_action(
                self,
                guard: Guard,
                action: PatchAction | SetCompletionAction | MoveToListAction,
            ) -> MutationOutcome:
                outcome = super().apply_action(guard, action)
                current = self._reminders[guard.reminder_id]
                self._reminders[guard.reminder_id] = Snapshot(
                    reminder={**dict(current.reminder), "alarms": []},
                    guard=current.guard,
                )
                return outcome

        module = CoreModule(
            AbsoluteAlarmDriftingAdapter(),
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        initial = module.read_exact("reminder-1")

        result = module.change(
            initial.reference,
            {"kind": "patch", "patch": {"title": "Changed title"}},
        )

        self.assertIsNone(result.reference)
        self.assertIsNone(result.final_reminder)
        self.assertEqual(result.reference_error, "final_state_mismatch")

    def test_closed_actions_preserve_every_alarm_semantic_kind(self) -> None:
        actions = (
            (
                "title_patch",
                False,
                {"kind": "patch", "patch": {"title": "Changed title"}},
            ),
            (
                "completion",
                False,
                {"kind": "set_completion", "completed": True},
            ),
            (
                "reopen",
                True,
                {"kind": "set_completion", "completed": False},
            ),
            (
                "move",
                False,
                {"kind": "move_to_list", "list_id": "list-beta"},
            ),
        )

        for alarm_name, alarm in SEMANTIC_ALARMS.items():
            for action_name, completed, action in actions:
                with self.subTest(alarm=alarm_name, action=action_name):
                    module = CoreModule(
                        SemanticReminderAdapter(
                            alarm=alarm,
                            completed=completed,
                            recurring=action_name != "completion",
                        ),
                        clock=DeterministicClock(),
                        token_source=DeterministicTokens(),
                    )
                    initial = module.read_exact("reminder-1")

                    result = module.change(initial.reference, action)

                    self.assertEqual(result.reference, "rev1.opaque-2")
                    self.assertEqual(result.final_reminder["alarms"], [alarm])

    def test_alarm_drift_across_closed_actions_never_issues_a_reference(self) -> None:
        actions = (
            (
                "title_patch",
                False,
                {"kind": "patch", "patch": {"title": "Changed title"}},
            ),
            (
                "completion",
                False,
                {"kind": "set_completion", "completed": True},
            ),
            (
                "reopen",
                True,
                {"kind": "set_completion", "completed": False},
            ),
            (
                "move",
                False,
                {"kind": "move_to_list", "list_id": "list-beta"},
            ),
        )

        for alarm_name, alarm in SEMANTIC_ALARMS.items():
            for action_name, completed, action in actions:
                with self.subTest(alarm=alarm_name, action=action_name):
                    module = CoreModule(
                        SemanticReminderAdapter(
                            alarm=alarm,
                            completed=completed,
                            recurring=action_name != "completion",
                            provider_drift={"alarms": []},
                        ),
                        clock=DeterministicClock(),
                        token_source=DeterministicTokens(),
                    )
                    initial = module.read_exact("reminder-1")

                    result = module.change(initial.reference, action)

                    self.assertIsNone(result.reference)
                    self.assertIsNone(result.final_reminder)
                    self.assertEqual(result.reference_error, "final_state_mismatch")

    def test_provider_alarm_transformations_never_issue_a_reference(self) -> None:
        transformed = {
            "absolute": {
                "kind": "absolute",
                "date_time": "2027-08-18T00:00:00.000Z",
            },
            "location": {
                "kind": "location",
                "proximity": "leave",
                "location": {
                    "title": "Office",
                    "latitude": 37.5,
                    "longitude": 127.0,
                    "radius_meters": 200.0,
                },
            },
            "writable_relative": {
                "kind": "relative",
                "offset_seconds": -1_800,
            },
            "read_only": {
                "kind": "absolute",
                "date_time": "2027-08-17T00:00:00.000Z",
                "read_only": True,
                "action": {"type": "procedure", "url": "example:after"},
            },
        }

        for alarm_name, alarm in SEMANTIC_ALARMS.items():
            with self.subTest(alarm=alarm_name):
                module = CoreModule(
                    SemanticReminderAdapter(
                        alarm=alarm,
                        provider_drift={"alarms": [transformed[alarm_name]]},
                    ),
                    clock=DeterministicClock(),
                    token_source=DeterministicTokens(),
                )
                initial = module.read_exact("reminder-1")

                result = module.change(
                    initial.reference,
                    {"kind": "patch", "patch": {"title": "Changed title"}},
                )

                self.assertIsNone(result.reference)
                self.assertEqual(result.reference_error, "final_state_mismatch")

    def test_alarm_order_is_semantic_but_duplicate_multiplicity_is_preserved(
        self,
    ) -> None:
        absolute = SEMANTIC_ALARMS["absolute"]
        location = SEMANTIC_ALARMS["location"]
        initial_alarms = [deepcopy(absolute), deepcopy(absolute), deepcopy(location)]
        cases = (
            (
                "permuted",
                [deepcopy(location), deepcopy(absolute), deepcopy(absolute)],
                True,
            ),
            (
                "duplicate_lost",
                [deepcopy(location), deepcopy(absolute)],
                False,
            ),
        )

        for label, final_alarms, should_match in cases:
            with self.subTest(label=label):
                adapter = SemanticReminderAdapter(
                    alarm=absolute,
                    provider_drift={"alarms": final_alarms},
                )
                current = adapter._reminders["reminder-1"]
                adapter._reminders["reminder-1"] = Snapshot(
                    reminder={**dict(current.reminder), "alarms": initial_alarms},
                    guard=current.guard,
                )
                module = CoreModule(
                    adapter,
                    clock=DeterministicClock(),
                    token_source=DeterministicTokens(),
                )
                initial = module.read_exact("reminder-1")

                result = module.change(
                    initial.reference,
                    {"kind": "patch", "patch": {"title": "Changed title"}},
                )

                if should_match:
                    self.assertEqual(result.reference, "rev1.opaque-2")
                else:
                    self.assertIsNone(result.reference)
                    self.assertEqual(result.reference_error, "final_state_mismatch")

    def test_due_and_recurrence_are_stable_dependencies_for_unrelated_actions(
        self,
    ) -> None:
        cases = (
            (
                "due",
                {"due": {"kind": "all_day", "date": "2027-09-01"}},
            ),
            ("recurrence", {"recurrence_rules": []}),
        )
        for label, provider_drift in cases:
            with self.subTest(field=label):
                module = CoreModule(
                    SemanticReminderAdapter(
                        alarm=SEMANTIC_ALARMS["absolute"],
                        provider_drift=provider_drift,
                    ),
                    clock=DeterministicClock(),
                    token_source=DeterministicTokens(),
                )
                initial = module.read_exact("reminder-1")

                result = module.change(
                    initial.reference,
                    {"kind": "patch", "patch": {"title": "Changed title"}},
                )

                self.assertIsNone(result.reference)
                self.assertEqual(result.reference_error, "final_state_mismatch")

    def test_provider_owned_fields_are_excluded_from_semantic_projection(self) -> None:
        before = {
            "title": "Original title",
            "alarms": [SEMANTIC_ALARMS["absolute"]],
            "external_id": "provider-external-before",
            "completion_date": None,
            "created": "2027-01-01T00:00:00.000Z",
            "last_modified": "2027-08-01T00:00:00.000Z",
            "list_title": "Personal",
            "source_id": "provider-source",
            "source_title": "iCloud",
        }

        projection = canonical_action_projection(
            before,
            PatchAction({"title": "Changed title"}),
        )

        self.assertEqual(
            STABLE_USER_FIELDS,
            (
                "title",
                "notes",
                "url",
                "location",
                "priority",
                "completed",
                "due",
                "start",
                "alarms",
                "recurrence_rules",
                "list_id",
            ),
        )
        self.assertEqual(
            projection,
            {
                "title": "Changed title",
                "alarms": [SEMANTIC_ALARMS["absolute"]],
            },
        )

    def test_due_only_change_rejects_final_read_that_loses_relative_alarm(self) -> None:
        class RelativeAlarmDriftingAdapter(InMemoryAdapter):
            def __init__(self) -> None:
                super().__init__()
                current = self._reminders["reminder-1"]
                self._reminders["reminder-1"] = Snapshot(
                    reminder={
                        **dict(current.reminder),
                        "list_id": "list-alpha",
                        "due": {"kind": "all_day", "date": "2027-08-31"},
                        "alarms": [
                            {"kind": "relative", "offset_seconds": -1_209_600}
                        ],
                    },
                    guard=current.guard,
                )

            def apply_action(
                self,
                guard: Guard,
                action: PatchAction | SetCompletionAction | MoveToListAction,
            ) -> MutationOutcome:
                outcome = super().apply_action(guard, action)
                current = self._reminders[guard.reminder_id]
                self._reminders[guard.reminder_id] = Snapshot(
                    reminder={**dict(current.reminder), "alarms": []},
                    guard=current.guard,
                )
                return outcome

        module = CoreModule(
            RelativeAlarmDriftingAdapter(),
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        initial = module.read_exact("reminder-1")

        result = module.change(
            initial.reference,
            {
                "kind": "patch",
                "patch": {"due": {"kind": "all_day", "date": "2027-09-30"}},
            },
        )

        self.assertIsNone(result.reference)
        self.assertIsNone(result.final_reminder)
        self.assertEqual(result.reference_error, "final_state_mismatch")

    def test_alarm_only_change_rejects_final_read_that_moves_due_anchor(self) -> None:
        class DueAnchorDriftingAdapter(InMemoryAdapter):
            def __init__(self) -> None:
                super().__init__()
                current = self._reminders["reminder-1"]
                self._reminders["reminder-1"] = Snapshot(
                    reminder={
                        **dict(current.reminder),
                        "list_id": "list-alpha",
                        "due": {"kind": "all_day", "date": "2027-08-31"},
                        "alarms": [
                            {"kind": "relative", "offset_seconds": -1_209_600}
                        ],
                    },
                    guard=current.guard,
                )

            def apply_action(
                self,
                guard: Guard,
                action: PatchAction | SetCompletionAction | MoveToListAction,
            ) -> MutationOutcome:
                outcome = super().apply_action(guard, action)
                current = self._reminders[guard.reminder_id]
                self._reminders[guard.reminder_id] = Snapshot(
                    reminder={
                        **dict(current.reminder),
                        "due": {"kind": "all_day", "date": "2027-09-01"},
                    },
                    guard=current.guard,
                )
                return outcome

        module = CoreModule(
            DueAnchorDriftingAdapter(),
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        initial = module.read_exact("reminder-1")

        result = module.change(
            initial.reference,
            {
                "kind": "patch",
                "patch": {
                    "alarms": [
                        {"kind": "relative", "offset_seconds": -604_800}
                    ]
                },
            },
        )

        self.assertIsNone(result.reference)
        self.assertIsNone(result.final_reminder)
        self.assertEqual(result.reference_error, "final_state_mismatch")

    def test_move_rejects_final_read_that_converts_relative_alarm(self) -> None:
        class MoveDriftingAdapter(InMemoryAdapter):
            def __init__(self) -> None:
                super().__init__()
                current = self._reminders["reminder-1"]
                self._reminders["reminder-1"] = Snapshot(
                    reminder={
                        **dict(current.reminder),
                        "list_id": "list-alpha",
                        "due": {"kind": "all_day", "date": "2027-08-31"},
                        "alarms": [
                            {"kind": "relative", "offset_seconds": -1_209_600}
                        ],
                    },
                    guard=current.guard,
                )

            def apply_action(
                self,
                guard: Guard,
                action: PatchAction | SetCompletionAction | MoveToListAction,
            ) -> MutationOutcome:
                outcome = super().apply_action(guard, action)
                current = self._reminders[guard.reminder_id]
                self._reminders[guard.reminder_id] = Snapshot(
                    reminder={
                        **dict(current.reminder),
                        "alarms": [
                            {
                                "kind": "absolute",
                                "date_time": "2027-08-17T00:00:00.000Z",
                            }
                        ],
                    },
                    guard=current.guard,
                )
                return outcome

        module = CoreModule(
            MoveDriftingAdapter(),
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        initial = module.read_exact("reminder-1")

        result = module.change(
            initial.reference,
            {"kind": "move_to_list", "list_id": "list-beta"},
        )

        self.assertIsNone(result.reference)
        self.assertIsNone(result.final_reminder)
        self.assertEqual(result.reference_error, "final_state_mismatch")

    def test_move_rejects_read_only_procedure_alarm_metadata_drift(self) -> None:
        module = CoreModule(
            ReadOnlyProcedureAlarmAdapter(drift_action_metadata=True),
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        initial = module.read_exact("reminder-1")

        result = module.change(
            initial.reference,
            {"kind": "move_to_list", "list_id": "list-beta"},
        )

        self.assertIsNone(result.reference)
        self.assertIsNone(result.final_reminder)
        self.assertEqual(result.reference_error, "final_state_mismatch")

    def test_unrelated_patch_rejects_read_only_procedure_alarm_metadata_drift(
        self,
    ) -> None:
        module = CoreModule(
            ReadOnlyProcedureAlarmAdapter(drift_action_metadata=True),
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        initial = module.read_exact("reminder-1")

        result = module.change(
            initial.reference,
            {"kind": "patch", "patch": {"title": "Changed title"}},
        )

        self.assertIsNone(result.reference)
        self.assertIsNone(result.final_reminder)
        self.assertEqual(result.reference_error, "final_state_mismatch")

    def test_completion_rejects_read_only_procedure_alarm_metadata_drift(
        self,
    ) -> None:
        module = CoreModule(
            ReadOnlyProcedureAlarmAdapter(drift_action_metadata=True),
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        initial = module.read_exact("reminder-1")

        result = module.change(
            initial.reference,
            {"kind": "set_completion", "completed": True},
        )

        self.assertIsNone(result.reference)
        self.assertIsNone(result.final_reminder)
        self.assertEqual(result.reference_error, "final_state_mismatch")

    def test_explicit_alarm_clear_does_not_preserve_a_read_only_alarm(self) -> None:
        for cleared in (None, []):
            with self.subTest(cleared=cleared):
                module = CoreModule(
                    ReadOnlyProcedureAlarmAdapter(),
                    clock=DeterministicClock(),
                    token_source=DeterministicTokens(),
                )
                initial = module.read_exact("reminder-1")

                result = module.change(
                    initial.reference,
                    {"kind": "patch", "patch": {"alarms": cleared}},
                )

                self.assertEqual(result.reference, "rev1.opaque-2")
                self.assertIsNotNone(result.final_reminder)
                self.assertEqual(result.final_reminder["alarms"], cleared)

    def test_implicit_alarm_preservation_accepts_unchanged_read_only_metadata(
        self,
    ) -> None:
        actions = (
            {"kind": "patch", "patch": {"title": "Changed title"}},
            {"kind": "set_completion", "completed": True},
            {"kind": "move_to_list", "list_id": "list-beta"},
        )

        for action in actions:
            with self.subTest(action=action):
                module = CoreModule(
                    ReadOnlyProcedureAlarmAdapter(),
                    clock=DeterministicClock(),
                    token_source=DeterministicTokens(),
                )
                initial = module.read_exact("reminder-1")

                result = module.change(initial.reference, action)

                self.assertEqual(result.reference, "rev1.opaque-2")
                self.assertIsNotNone(result.final_reminder)
                self.assertEqual(
                    result.final_reminder["alarms"][0]["action"],
                    {"type": "procedure", "url": "example:before"},
                )

    def test_recurring_completion_is_rejected_before_any_adapter_write(self) -> None:
        adapter = SemanticReminderAdapter(alarm=SEMANTIC_ALARMS["absolute"])
        module = CoreModule(adapter, token_source=DeterministicTokens())
        initial = module.read_exact("reminder-1")

        with mock.patch.object(adapter, "apply_action", wraps=adapter.apply_action) as apply:
            for _ in range(2):
                with self.assertRaises(UnsupportedAction) as raised:
                    module.change(initial.reference, {"kind": "set_completion", "completed": True})
                self.assertEqual(raised.exception.code, "unsupported_recurring_completion")
                self.assertIn("Reminders app", str(raised.exception))
            apply.assert_not_called()

        self.assertEqual(module.read_exact("reminder-1").reminder, initial.reminder)

    def test_recurring_completion_revalidates_staleness_before_preflight(self) -> None:
        adapter = SemanticReminderAdapter(alarm=SEMANTIC_ALARMS["absolute"])
        module = CoreModule(adapter, token_source=DeterministicTokens())
        initial = module.read_exact("reminder-1")
        adapter.external_patch("reminder-1", {"title": "Edited in Reminders"})

        with mock.patch.object(adapter, "apply_action", wraps=adapter.apply_action) as apply:
            with self.assertRaises(ReferenceRejected) as raised:
                module.change(initial.reference, {"kind": "set_completion", "completed": True})
            self.assertEqual(raised.exception.code, "concurrent_modification")
            apply.assert_not_called()

    def test_recurring_completion_gate_preserves_other_exact_actions(self) -> None:
        cases = (
            (False, {"kind": "set_completion", "completed": False}),
            (True, {"kind": "set_completion", "completed": True}),
            (True, {"kind": "set_completion", "completed": False}),
            (False, {"kind": "patch", "patch": {"title": "Rename recurring item"}}),
            (False, {"kind": "move_to_list", "list_id": "list-beta"}),
        )
        for completed, action in cases:
            with self.subTest(completed=completed, action=action):
                adapter = SemanticReminderAdapter(alarm=SEMANTIC_ALARMS["absolute"], completed=completed)
                module = CoreModule(adapter, token_source=DeterministicTokens())
                initial = module.read_exact("reminder-1")
                result = module.change(initial.reference, action)
                self.assertIsNotNone(result.reference)
                self.assertEqual(result.final_reminder["recurrence_rules"], initial.reminder["recurrence_rules"])

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
        class FinalReadFailingAdapter(InMemoryAdapter):
            def apply_action(
                self,
                guard: Guard,
                action: PatchAction | SetCompletionAction | MoveToListAction,
            ) -> MutationOutcome:
                outcome = super().apply_action(guard, action)
                self.fail_next_read = True
                return outcome

        adapter = FinalReadFailingAdapter()
        module = CoreModule(
            adapter,
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
        )
        initial = module.read_exact("reminder-1")

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

    def test_stale_reference_is_consumed_before_cached_read_only_alarm_preflight(
        self,
    ) -> None:
        adapter = ReadOnlyProcedureAlarmAdapter()
        module = CoreModule(
            adapter,
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
            reference_ttl_seconds=30.0,
        )
        initial = module.read_exact("reminder-1")
        adapter.external_patch("reminder-1", {"alarms": []})

        with self.assertRaises(ReferenceRejected) as raised:
            module.change(
                initial.reference,
                {
                    "kind": "patch",
                    "patch": {
                        "alarms": [
                            {
                                "kind": "absolute",
                                "date_time": "2027-08-18T00:00:00.000Z",
                            }
                        ]
                    },
                },
            )

        self.assertEqual(raised.exception.code, "concurrent_modification")
        with self.assertRaises(ReferenceRejected) as replay:
            module.change(
                initial.reference,
                {"kind": "patch", "patch": {"title": "No replay"}},
            )
        self.assertEqual(replay.exception.code, "invalid_reference")

    def test_stale_reference_is_consumed_before_cached_relative_alarm_preflight(
        self,
    ) -> None:
        adapter = SemanticReminderAdapter(
            alarm=SEMANTIC_ALARMS["writable_relative"]
        )
        module = CoreModule(
            adapter,
            clock=DeterministicClock(),
            token_source=DeterministicTokens(),
            reference_ttl_seconds=30.0,
        )
        initial = module.read_exact("reminder-1")
        adapter.external_patch("reminder-1", {"alarms": [], "due": None})

        with self.assertRaises(ReferenceRejected) as raised:
            module.change(
                initial.reference,
                {"kind": "patch", "patch": {"due": None}},
            )

        self.assertEqual(raised.exception.code, "concurrent_modification")
        with self.assertRaises(ReferenceRejected) as replay:
            module.change(
                initial.reference,
                {"kind": "patch", "patch": {"title": "No replay"}},
            )
        self.assertEqual(replay.exception.code, "invalid_reference")

    def test_cross_store_reference_precedes_cached_alarm_preflight(self) -> None:
        adapter = ReadOnlyProcedureAlarmAdapter()
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
                {
                    "kind": "patch",
                    "patch": {
                        "alarms": [
                            {
                                "kind": "absolute",
                                "date_time": "2027-08-18T00:00:00.000Z",
                            }
                        ]
                    },
                },
            )

        self.assertEqual(raised.exception.code, "invalid_reference")

    def test_expired_reference_precedes_cached_alarm_preflight(self) -> None:
        clock = DeterministicClock()
        module = CoreModule(
            ReadOnlyProcedureAlarmAdapter(),
            clock=clock,
            token_source=DeterministicTokens(),
            reference_ttl_seconds=30.0,
        )
        initial = module.read_exact("reminder-1")
        clock.now = 130.0

        with self.assertRaises(ReferenceRejected) as raised:
            module.change(
                initial.reference,
                {
                    "kind": "patch",
                    "patch": {
                        "alarms": [
                            {
                                "kind": "absolute",
                                "date_time": "2027-08-18T00:00:00.000Z",
                            }
                        ]
                    },
                },
            )

        self.assertEqual(raised.exception.code, "expired_reference")

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
