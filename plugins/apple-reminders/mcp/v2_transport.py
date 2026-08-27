#!/usr/bin/env python3
"""Closed internal result contract for local subprocess transports.

Transport dispatch certainty is independent from both child-process success and
the public mutation outcome.  Only the launcher can prove that dispatch never
started; every other result must be treated as having possibly crossed the
mutation boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any


class DispatchCertainty(Enum):
    """What the parent launcher can prove about subprocess dispatch."""

    PROVEN_NOT_STARTED = auto()
    MAY_HAVE_STARTED = auto()


@dataclass(frozen=True, slots=True)
class TransportResult:
    """One private transport payload plus launcher-owned dispatch evidence."""

    payload: dict[str, Any]
    is_error: bool
    dispatch_certainty: DispatchCertainty

    def __post_init__(self) -> None:
        if (
            self.dispatch_certainty is DispatchCertainty.PROVEN_NOT_STARTED
            and not self.is_error
        ):
            raise ValueError("A non-error transport result cannot be undispatched")

    @property
    def proves_not_started(self) -> bool:
        return self.dispatch_certainty is DispatchCertainty.PROVEN_NOT_STARTED
