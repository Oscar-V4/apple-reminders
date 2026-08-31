#!/usr/bin/env python3
"""Core Reminder workflow behind a small, production-neutral Interface."""

from __future__ import annotations

import copy
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping, Protocol


@dataclass(frozen=True)
class Guard:
    """Atomic identity and concurrency precondition for one exact Reminder."""

    reminder_id: str
    store_identity: str
    public_concurrency_value: str


@dataclass(frozen=True)
class Snapshot:
    """Canonical exact Adapter read with its write Guard."""

    reminder: Mapping[str, Any]
    guard: Guard


@dataclass(frozen=True)
class PatchAction:
    patch: Mapping[str, Any]


@dataclass(frozen=True)
class SetCompletionAction:
    completed: bool


@dataclass(frozen=True)
class MoveToListAction:
    list_id: str


CoreAction = PatchAction | SetCompletionAction | MoveToListAction
MutationState = Literal["not_mutated", "committed", "unknown"]


def mutation_state_after_unverified_projection(
    state: MutationState,
) -> MutationState:
    return "committed" if state == "committed" else "unknown"


def unverified_mutation_projection(state: MutationState) -> dict[str, Any]:
    no_write = state == "not_mutated"
    result: dict[str, Any] = {
        "ok": not no_write,
        "status": (
            "failed_no_mutation"
            if no_write
            else "committed_verification_pending"
        ),
        "verification": {
            "state": "not_needed" if no_write else "pending",
            "write_performed": (
                False if no_write else True if state == "committed" else None
            ),
            "final_read": False,
            "matched": None,
        },
        "recovery": {
            "semantics": "read_before_retry",
            "automatic_retry_safe": False,
        },
    }
    if not no_write:
        result["warnings"] = [
            {
                "code": "verification_pending",
                "message": "The mutation may have committed; read before retrying.",
            }
        ]
    return result


@dataclass(frozen=True)
class MutationOutcome:
    receipt: Mapping[str, Any]
    mutation_state: MutationState


class ReminderAdapter(Protocol):
    """Port implemented by native production and in-memory test Adapters."""

    def read_exact(self, reminder_id: str) -> Snapshot:
        ...

    def apply_action(self, guard: Guard, action: CoreAction) -> MutationOutcome:
        ...


class AdapterConflict(Exception):
    """The Adapter rejected an atomic change because its revision was stale."""

    def __init__(self, code: str = "concurrent_modification") -> None:
        super().__init__(code)
        self.code = code


class ReferenceRejected(Exception):
    """A change reference was not safe to use."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ReferenceRevalidationFailed(RuntimeError):
    """The fresh exact read failed before mutation dispatch."""


class ActionRejected(ValueError):
    """A requested Core action was not part of the closed Interface."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "invalid_action"


class AdapterContractError(RuntimeError):
    """An Adapter response violated the Core seam."""


class MutationOutcomeUnknown(RuntimeError):
    """The Adapter failed after dispatch, so commit state cannot be assumed."""


class MutationOutcomeRejected(AdapterContractError):
    def __init__(self, message: str, mutation_state: MutationState) -> None:
        super().__init__(message)
        self.mutation_state = mutation_state


@dataclass(frozen=True)
class ExactRead:
    reminder: dict[str, Any]
    reference: str


@dataclass(frozen=True)
class ChangeResult:
    receipt: dict[str, Any]
    reference: str | None
    final_reminder: dict[str, Any] | None
    mutation_state: MutationState
    reference_error: str | None = None


@dataclass(frozen=True)
class _ReferenceGrant:
    guard: Guard
    reminder: Mapping[str, Any]
    expires_at: float


def _new_token() -> str:
    return secrets.token_urlsafe(32)


PATCH_FIELDS = frozenset(
    {"title", "notes", "url", "priority", "due", "alarms", "recurrence_rules"}
)
RECEIPT_STATUSES = frozenset(
    {
        "unchanged",
        "verified",
        "committed_verification_pending",
        "partial_success",
        "failed_no_mutation",
        "failed_manual_repair_required",
    }
)
REFERENCE_ELIGIBLE_STATUSES = frozenset({"unchanged", "verified"})

# The semantic projection contains user-authored state that Core must either
# change deliberately or preserve exactly. Identity is guarded separately by
# Snapshot/Guard. Provider-owned and derived fields such as external_id,
# completion_date, created, last_modified, list/source titles, and source_id are
# intentionally outside this projection because a provider may refresh them
# without changing the user's Reminder semantics.
STABLE_USER_FIELDS = (
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
)


def _timestamp_matches(expected: str, actual: str) -> bool:
    try:
        expected_date = datetime.fromisoformat(expected.replace("Z", "+00:00"))
        actual_date = datetime.fromisoformat(actual.replace("Z", "+00:00"))
    except ValueError:
        return expected == actual
    if expected_date.tzinfo is None or actual_date.tzinfo is None:
        return expected == actual
    return expected_date.astimezone(timezone.utc) == actual_date.astimezone(
        timezone.utc
    )


def _requested_value_matches(expected: Any, actual: Any, *, field: str) -> bool:
    if field in {"alarms", "recurrence_rules"} and expected is None:
        return actual is None or actual == [] or actual == ()
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        if field.endswith("alarms[]"):
            if (
                expected.get("_verification_unavailable") is True
                or actual.get("_verification_unavailable") is True
            ):
                return False
            expected_read_only = expected.get("read_only") is True
            actual_read_only = actual.get("read_only") is True
            if expected_read_only != actual_read_only:
                return False
            if expected_read_only and expected.get("action") != actual.get("action"):
                return False
        return all(
            key in actual
            and _requested_value_matches(
                value,
                actual[key],
                field=f"{field}.{key}",
            )
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return False
        if field != "alarms":
            return all(
                _requested_value_matches(
                    expected_item,
                    actual_item,
                    field=f"{field}[]",
                )
                for expected_item, actual_item in zip(expected, actual)
            )
        unmatched = list(actual)
        for expected_item in expected:
            for index, actual_item in enumerate(unmatched):
                if _requested_value_matches(
                    expected_item,
                    actual_item,
                    field=f"{field}[]",
                ):
                    unmatched.pop(index)
                    break
            else:
                return False
        return True
    if (
        field.endswith("date_time")
        and isinstance(expected, str)
        and isinstance(actual, str)
    ):
        return _timestamp_matches(expected, actual)
    return actual == expected


def reminder_matches_fields(
    reminder: Mapping[str, Any],
    expected_fields: Mapping[str, Any],
) -> bool:
    return all(
        field in reminder
        and _requested_value_matches(expected, reminder[field], field=field)
        for field, expected in expected_fields.items()
    )


def _alarms_contain_relative(value: Any) -> bool:
    return isinstance(value, list) and any(
        isinstance(alarm, Mapping) and alarm.get("kind") == "relative"
        for alarm in value
    )


def _alarms_contain_read_only(value: Any) -> bool:
    return isinstance(value, list) and any(
        isinstance(alarm, Mapping) and alarm.get("read_only") is True
        for alarm in value
    )


def canonical_action_projection(
    before: Mapping[str, Any],
    action: CoreAction,
) -> dict[str, Any]:
    """Return requested delta plus every stable user field to preserve."""

    expected = {
        field: copy.deepcopy(before[field])
        for field in STABLE_USER_FIELDS
        if field in before
    }
    if isinstance(action, PatchAction):
        expected.update(copy.deepcopy(dict(action.patch)))
    elif isinstance(action, SetCompletionAction):
        expected["completed"] = action.completed
    elif isinstance(action, MoveToListAction):
        expected["list_id"] = action.list_id
    return expected


def reminder_matches_action(
    reminder: Mapping[str, Any],
    before: Mapping[str, Any],
    action: CoreAction,
) -> bool:
    if not isinstance(action, (PatchAction, SetCompletionAction, MoveToListAction)):
        return False
    return reminder_matches_fields(
        reminder,
        canonical_action_projection(before, action),
    )


class CoreModule:
    """Issue opaque revisions for exact reads and safe Reminder changes."""

    def __init__(
        self,
        adapter: ReminderAdapter,
        *,
        clock: Callable[[], float] = time.monotonic,
        token_source: Callable[[], str] = _new_token,
        reference_ttl_seconds: float = 300.0,
        max_active_references: int = 1024,
    ) -> None:
        if reference_ttl_seconds <= 0:
            raise ValueError("reference_ttl_seconds must be positive")
        if max_active_references <= 0:
            raise ValueError("max_active_references must be positive")
        self._adapter = adapter
        self._clock = clock
        self._token_source = token_source
        self._reference_ttl_seconds = reference_ttl_seconds
        self._max_active_references = max_active_references
        self._references: dict[str, _ReferenceGrant] = {}

    def read_exact(self, reminder_id: str) -> ExactRead:
        snapshot = self._read_snapshot(reminder_id)
        reference = self._issue_reference(snapshot)
        return ExactRead(reminder=copy.deepcopy(dict(snapshot.reminder)), reference=reference)

    def _issue_reference(self, snapshot: Snapshot) -> str:
        now = self._clock()
        for reference, grant in list(self._references.items()):
            if now >= grant.expires_at:
                self._references.pop(reference, None)
        while len(self._references) >= self._max_active_references:
            oldest_reference = next(iter(self._references))
            self._references.pop(oldest_reference)
        entropy = self._token_source()
        if not entropy:
            raise RuntimeError("token_source must return a unique, non-empty token")
        reference = f"rev1.{entropy}"
        if reference in self._references:
            raise RuntimeError("token_source must return a unique, non-empty token")
        self._references[reference] = _ReferenceGrant(
            guard=snapshot.guard,
            reminder=copy.deepcopy(dict(snapshot.reminder)),
            expires_at=now + self._reference_ttl_seconds,
        )
        return reference

    def _read_snapshot(self, reminder_id: str) -> Snapshot:
        snapshot = self._adapter.read_exact(reminder_id)
        if not isinstance(snapshot, Snapshot):
            raise AdapterContractError("Exact Adapter read must return a Snapshot")
        if not isinstance(snapshot.reminder, Mapping) or not isinstance(
            snapshot.guard, Guard
        ):
            raise AdapterContractError("Exact Adapter read returned invalid Snapshot fields")
        reminder = dict(snapshot.reminder)
        guard = snapshot.guard
        if (
            not isinstance(reminder_id, str)
            or not reminder_id
            or reminder.get("id") != reminder_id
            or guard.reminder_id != reminder_id
            or not isinstance(guard.store_identity, str)
            or not guard.store_identity
            or not isinstance(guard.public_concurrency_value, str)
            or not guard.public_concurrency_value
        ):
            raise AdapterContractError("Exact Adapter read returned an invalid identity Guard")
        return Snapshot(reminder=copy.deepcopy(reminder), guard=guard)

    def _active_grant(self, reference: str) -> _ReferenceGrant:
        grant = self._references.get(reference)
        if grant is None:
            raise ReferenceRejected(
                "invalid_reference",
                "The change reference is not recognized",
            )
        if self._clock() >= grant.expires_at:
            self._references.pop(reference, None)
            raise ReferenceRejected(
                "expired_reference",
                "The change reference has expired; read the Reminder again",
            )
        return grant

    def revalidate_reference(self, reference: str) -> Guard:
        """Resolve one opaque reference only after an exact Adapter re-read.

        Native-extension Modules use this internal port before obtaining their
        own private-store concurrency value. The public token remains opaque;
        a stale or cross-store grant is consumed before any native write.
        """

        current = self._revalidated_snapshot(reference)
        return current.guard

    def _revalidated_snapshot(
        self,
        reference: str,
    ) -> Snapshot:
        grant = self._active_grant(reference)
        try:
            current = self._read_snapshot(grant.guard.reminder_id)
        except Exception as exc:
            self._references.pop(reference, None)
            raise ReferenceRevalidationFailed(
                "The change reference could not be revalidated; read the Reminder again"
            ) from exc
        if current.guard != grant.guard:
            self._references.pop(reference, None)
            code = (
                "invalid_reference"
                if current.guard.store_identity != grant.guard.store_identity
                else "concurrent_modification"
            )
            raise ReferenceRejected(
                code,
                "The Reminder changed; read it again before applying a change",
            )
        return current

    def invalidate_reference(self, reference: str) -> None:
        """Consume a grant after a committed or outcome-unknown native write."""

        self._references.pop(reference, None)

    def change(self, reference: str, raw_action: Mapping[str, Any]) -> ChangeResult:
        action = self._parse_action(raw_action)
        current = self._revalidated_snapshot(reference)
        before = current.reminder
        if isinstance(action, PatchAction):
            replacement = action.patch.get("alarms")
            if (
                isinstance(replacement, list)
                and bool(replacement)
                and _alarms_contain_read_only(before.get("alarms"))
            ):
                raise ActionRejected(
                    "A non-empty alarms replacement cannot preserve an existing "
                    "read-only alarm; omit alarms or explicitly clear the array"
                )
            resulting_due = action.patch.get("due", before.get("due"))
            resulting_alarms = action.patch.get(
                "alarms", before.get("alarms")
            )
            if resulting_due is None and _alarms_contain_relative(
                resulting_alarms
            ):
                raise ActionRejected(
                    "A relative alarm requires a due anchor; retain or set due, "
                    "or clear/replace the relative alarm in the same patch"
                )
        try:
            outcome = self._adapter.apply_action(current.guard, action)
        except AdapterConflict as exc:
            self._references.pop(reference, None)
            raise ReferenceRejected(
                exc.code,
                "The Reminder changed; read it again before applying a change",
            ) from None
        except Exception as exc:
            self._references.pop(reference, None)
            raise MutationOutcomeUnknown(
                "The native mutation outcome is unknown; read before retrying"
            ) from exc
        if outcome.mutation_state in {"committed", "unknown"}:
            self._references.pop(reference, None)
        try:
            receipt = self._validated_receipt(outcome)
        except AdapterContractError as exc:
            self._references.pop(reference, None)
            raise MutationOutcomeRejected(
                str(exc),
                outcome.mutation_state,
            ) from exc
        if outcome.mutation_state == "unknown":
            return ChangeResult(
                receipt=receipt,
                reference=None,
                final_reminder=None,
                mutation_state=outcome.mutation_state,
                reference_error="mutation_outcome_unknown",
            )
        if receipt["status"] not in REFERENCE_ELIGIBLE_STATUSES:
            return ChangeResult(
                receipt=receipt,
                reference=None,
                final_reminder=None,
                mutation_state=outcome.mutation_state,
            )
        try:
            final_snapshot = self._read_snapshot(current.guard.reminder_id)
        except Exception:
            return ChangeResult(
                receipt=receipt,
                reference=None,
                final_reminder=None,
                mutation_state=outcome.mutation_state,
                reference_error="final_read_failed",
            )
        if not reminder_matches_action(final_snapshot.reminder, before, action):
            self._references.pop(reference, None)
            return ChangeResult(
                receipt=receipt,
                reference=None,
                final_reminder=None,
                mutation_state=outcome.mutation_state,
                reference_error="final_state_mismatch",
            )
        replacement = self._issue_reference(final_snapshot)
        self._references.pop(reference, None)
        return ChangeResult(
            receipt=receipt,
            reference=replacement,
            final_reminder=copy.deepcopy(dict(final_snapshot.reminder)),
            mutation_state=outcome.mutation_state,
        )

    @staticmethod
    def _parse_action(raw_action: Mapping[str, Any]) -> CoreAction:
        if not isinstance(raw_action, Mapping):
            raise ActionRejected("Core action must be an object")
        action_type = raw_action.get("kind")
        if action_type == "patch":
            if set(raw_action) != {"kind", "patch"}:
                raise ActionRejected("patch action contains unsupported fields")
            patch = raw_action.get("patch")
            if not isinstance(patch, Mapping) or not patch:
                raise ActionRejected("patch action requires at least one field")
            unknown = set(patch) - PATCH_FIELDS
            if unknown:
                raise ActionRejected("patch action contains unsupported Reminder fields")
            return PatchAction(copy.deepcopy(dict(patch)))
        if action_type == "set_completion":
            if set(raw_action) != {"kind", "completed"} or not isinstance(
                raw_action.get("completed"), bool
            ):
                raise ActionRejected("set_completion requires one boolean completed field")
            return SetCompletionAction(raw_action["completed"])
        if action_type == "move_to_list":
            list_id = raw_action.get("list_id")
            if (
                set(raw_action) != {"kind", "list_id"}
                or not isinstance(list_id, str)
                or not list_id
            ):
                raise ActionRejected("move_to_list requires one non-empty list_id")
            return MoveToListAction(list_id)
        raise ActionRejected("Core action type is not supported")

    @staticmethod
    def _validated_receipt(outcome: MutationOutcome) -> dict[str, Any]:
        if outcome.mutation_state not in {"not_mutated", "committed", "unknown"}:
            raise AdapterContractError("Adapter returned an invalid mutation state")
        receipt = copy.deepcopy(dict(outcome.receipt))
        required = {
            "ok",
            "status",
            "operation",
            "operation_id",
            "backend",
            "target",
            "before",
            "after",
            "verification",
            "recovery",
        }
        if required - set(receipt) or receipt.get("status") not in RECEIPT_STATUSES:
            raise AdapterContractError("Adapter returned an invalid mutation Receipt")
        status = receipt["status"]
        expected_ok = status not in {
            "failed_no_mutation",
            "failed_manual_repair_required",
        }
        if receipt.get("ok") is not expected_ok:
            raise AdapterContractError("Adapter mutation Receipt status and ok disagree")
        allowed_states = {
            "not_mutated": {"unchanged", "failed_no_mutation"},
            "committed": {
                "verified",
                "committed_verification_pending",
                "partial_success",
                "failed_manual_repair_required",
            },
            "unknown": {
                "committed_verification_pending",
                "partial_success",
                "failed_manual_repair_required",
            },
        }
        if status not in allowed_states[outcome.mutation_state]:
            raise AdapterContractError(
                "Adapter mutation Receipt status disagrees with mutation state"
            )
        for key in ("target", "before", "after", "verification", "recovery"):
            if not isinstance(receipt[key], dict):
                raise AdapterContractError("Adapter mutation Receipt objects are required")
        if "warnings" in receipt and not isinstance(receipt["warnings"], list):
            raise AdapterContractError("Adapter mutation Receipt warnings must be an array")
        if "error" in receipt and not isinstance(receipt["error"], dict):
            raise AdapterContractError("Adapter mutation Receipt error must be an object")
        return receipt
