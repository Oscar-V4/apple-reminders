#!/usr/bin/env python3
"""Core Reminder workflow behind a small, production-neutral Interface."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


@dataclass(frozen=True)
class AdapterReminder:
    """Exact Adapter read, including concurrency data hidden from callers."""

    data: Mapping[str, Any]
    public_concurrency_value: str


class ReminderAdapter(Protocol):
    """Port implemented by native production and in-memory test Adapters."""

    store_identity: str

    def read_exact(self, reminder_id: str) -> AdapterReminder:
        ...

    def apply_patch(
        self,
        reminder_id: str,
        expected_public_concurrency_value: str,
        patch: Mapping[str, Any],
    ) -> None:
        ...


class AdapterConflict(Exception):
    """The Adapter rejected an atomic change because its revision was stale."""


class ReferenceRejected(Exception):
    """A change reference was not safe to use."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExactRead:
    reminder: dict[str, Any]
    reference: str


@dataclass(frozen=True)
class Receipt:
    status: str
    after: dict[str, Any]
    verification: dict[str, str]
    reference: str


@dataclass(frozen=True)
class _ReferenceGrant:
    reminder_id: str
    store_identity: str
    public_concurrency_value: str
    expires_at: float


def _new_token() -> str:
    return secrets.token_urlsafe(32)


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
        snapshot = self._adapter.read_exact(reminder_id)
        now = self._clock()
        for reference, grant in list(self._references.items()):
            if now >= grant.expires_at:
                self._references.pop(reference, None)
        while len(self._references) >= self._max_active_references:
            oldest_reference = next(iter(self._references))
            self._references.pop(oldest_reference)
        reference = self._token_source()
        if not reference or reference in self._references:
            raise RuntimeError("token_source must return a unique, non-empty token")
        self._references[reference] = _ReferenceGrant(
            reminder_id=reminder_id,
            store_identity=self._adapter.store_identity,
            public_concurrency_value=snapshot.public_concurrency_value,
            expires_at=now + self._reference_ttl_seconds,
        )
        return ExactRead(reminder=dict(snapshot.data), reference=reference)

    def change(self, reference: str, patch: Mapping[str, Any]) -> Receipt:
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
        if self._adapter.store_identity != grant.store_identity:
            self._references.pop(reference, None)
            raise ReferenceRejected(
                "invalid_reference",
                "The change reference belongs to a different Reminder store",
            )
        try:
            self._adapter.apply_patch(
                grant.reminder_id,
                grant.public_concurrency_value,
                dict(patch),
            )
        except AdapterConflict:
            self._references.pop(reference, None)
            raise ReferenceRejected(
                "concurrent_modification",
                "The Reminder changed; read it again before applying a change",
            ) from None
        read_back = self.read_exact(grant.reminder_id)
        if any(read_back.reminder.get(key) != value for key, value in patch.items()):
            raise RuntimeError("Adapter change did not match its exact read-back")
        self._references.pop(reference, None)
        return Receipt(
            status="verified",
            after=read_back.reminder,
            verification={"state": "exact_read_back"},
            reference=read_back.reference,
        )
