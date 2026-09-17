"""One consistent schedule for an intraday collection session.

Three things the pilot specification described separately, derived here from
one calendar and one policy so they cannot disagree:

- **preparation**: the part of the session day before the open, when the dry
  run must be taken and the approval read -- the approval binds the market
  session date, so it cannot be produced the day before;
- **allowed collection**: the slots inside the capture window the existing
  one-shot command accepts without any override, ``[open, close)`` on the
  repository calendar, early closes included;
- **decision coverage**: which instants of the research contract's decision
  grid are preceded by at least one collection slot.

The first slot is the session open, 09:30:00 ET. The pilot specification's
09:29 first cycle was outside the capture window and would have needed the
out-of-session override; it is not silently enabled here, it is corrected.

Cycle scopes: option quotes, Greeks and the index print every slot (the market
cadence); open interest and the contract listing on the first slot and every
refresh interval thereafter. One cycle is in flight at a time; a cycle that
spans later boundaries causes those slots to be recorded as missed, and
collection resumes at the next boundary that is still in the future. That
policy lives in the collector; the schedule only says what the slots are.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from src.adapters.thetadata.capture_certification import (
    INDEX_PRICE,
    OPTION_CONTRACT_LIST,
    OPTION_GREEKS,
    OPTION_OPEN_INTEREST,
    OPTION_QUOTE,
)
from src.domain.canonical import canonical_payload
from src.domain.digests import digest_of
from src.gex.calendar import is_early_close, is_trading_session
from src.gex.capture_window import assess_capture_window
from src.gex.sessions import EASTERN, market_session_date
from src.replay.research_contract import ResearchContract
from src.replay.session import decision_grid

SCHEDULE_SCHEMA = "intraday-collection-schedule/2.1.36"
MARKET_ENDPOINTS: tuple[str, ...] = (INDEX_PRICE, OPTION_QUOTE, OPTION_GREEKS)
REFRESH_ENDPOINTS: tuple[str, ...] = (OPTION_OPEN_INTEREST, OPTION_CONTRACT_LIST)
FULL_SCOPE: frozenset[str] = frozenset(MARKET_ENDPOINTS + REFRESH_ENDPOINTS)
MARKET_SCOPE: frozenset[str] = frozenset(MARKET_ENDPOINTS)
#: Bounds on what a policy may ask for. A cadence below ten seconds or a
#: session of more than a thousand cycles is not the pilot this repository
#: specified, and refusing it is cheaper than discovering it during a session.
MIN_CADENCE_SECONDS = 10
MAX_SLOTS = 1000


class ScheduleError(ValueError):
    """A schedule that cannot be built honestly from the calendar and policy."""


@dataclass(frozen=True, slots=True)
class CollectionPolicy:
    """What the operator chose; every field enters the session approval."""

    cadence_seconds: int = 60
    refresh_every_seconds: int = 1800
    #: A cycle may still start this many seconds after its slot boundary --
    #: sleep granularity and the previous cycle's tail. Later than this the slot
    #: is missed, never caught up.
    start_tolerance_seconds: int = 5
    #: Consecutive cycles that fail to acquire anything before the session
    #: stops itself. Bounded, so an unreachable terminal cannot spend the whole
    #: day producing empty directories.
    max_consecutive_failed_cycles: int = 5
    contract: ResearchContract = field(default_factory=ResearchContract)

    def __post_init__(self) -> None:
        for name in (
            "cadence_seconds",
            "refresh_every_seconds",
            "start_tolerance_seconds",
            "max_consecutive_failed_cycles",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ScheduleError(f"{name} must be a nonnegative integer")
        if self.cadence_seconds < MIN_CADENCE_SECONDS:
            raise ScheduleError(f"cadence must be at least {MIN_CADENCE_SECONDS} s")
        if (
            self.refresh_every_seconds < self.cadence_seconds
            or self.refresh_every_seconds % self.cadence_seconds
        ):
            raise ScheduleError("refresh interval must be a multiple of the cadence")
        if self.start_tolerance_seconds >= self.cadence_seconds:
            raise ScheduleError("start tolerance must be shorter than the cadence")
        if self.max_consecutive_failed_cycles < 1:
            raise ScheduleError("at least one failed cycle must be tolerated")
        if self.cadence_seconds != self.contract.cadence_seconds:
            raise ScheduleError(
                "collection cadence must equal the research contract cadence"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "cadence_seconds": self.cadence_seconds,
            "refresh_every_seconds": self.refresh_every_seconds,
            "start_tolerance_seconds": self.start_tolerance_seconds,
            "max_consecutive_failed_cycles": self.max_consecutive_failed_cycles,
            "contract": self.contract.as_dict(),
            "one_cycle_in_flight": True,
            "missed_slot_policy": "RECORD_AND_RESUME_AT_NEXT_FUTURE_BOUNDARY",
        }

    @classmethod
    def from_specification(cls, path: pathlib.Path | str) -> CollectionPolicy:
        """The policy ``config/intraday_pilot.json`` declares.

        Reads the ``collection`` block and the ``research_contract`` block; the
        contract must be the repository default (the replay's grid and budgets
        are defined against it) and the two cadences must agree.
        """
        document = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        block = document.get("collection")
        if not isinstance(block, dict):
            raise ScheduleError(f"{path} declares no collection block")
        try:
            contract = ResearchContract(**document["research_contract"])
        except (KeyError, TypeError, ValueError) as error:
            raise ScheduleError(f"{path}: unusable research contract") from error
        if contract != ResearchContract():
            raise ScheduleError(
                f"{path} names a research contract that is not the repository default"
            )
        expected = {
            "cadence_seconds",
            "refresh_every_seconds",
            "start_tolerance_seconds",
            "max_consecutive_failed_cycles",
        }
        if set(block) != expected:
            raise ScheduleError(
                f"{path}: collection block must declare exactly {sorted(expected)}"
            )
        return cls(contract=contract, **block)


@dataclass(frozen=True, slots=True)
class Slot:
    index: int
    scheduled_at: datetime
    label: str
    scope: frozenset[str]

    @property
    def kind(self) -> str:
        return "FULL" if self.scope == FULL_SCOPE else "MARKET"

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label,
            "scheduled_at": self.scheduled_at.isoformat(),
            "kind": self.kind,
            "scope": sorted(self.scope),
        }


@dataclass(frozen=True, slots=True)
class CollectionSchedule:
    session_date: date
    policy: CollectionPolicy
    session_open: datetime
    session_close: datetime
    early_close: bool
    slots: tuple[Slot, ...]
    decision_grid: tuple[datetime, ...]
    max_attempts_per_request: int

    @classmethod
    def build(
        cls,
        session_date: date,
        policy: CollectionPolicy,
        *,
        max_attempts_per_request: int,
    ) -> CollectionSchedule:
        if not isinstance(session_date, date) or not is_trading_session(session_date):
            raise ScheduleError(f"{session_date} is not a trading session")
        opening = datetime.combine(session_date, time(9, 30), EASTERN)
        window = assess_capture_window(opening)
        if not window.inside_capture_window or window.session_close is None:
            raise ScheduleError(f"no capture window opens on {session_date}")
        session_open = opening.astimezone(UTC)
        session_close = window.session_close.astimezone(UTC)
        cadence = timedelta(seconds=policy.cadence_seconds)
        per_refresh = policy.refresh_every_seconds // policy.cadence_seconds
        slots: list[Slot] = []
        at = session_open
        while at < session_close:
            index = len(slots)
            if index >= MAX_SLOTS:
                raise ScheduleError("schedule exceeds the slot bound")
            scope = FULL_SCOPE if index % per_refresh == 0 else MARKET_SCOPE
            slots.append(
                Slot(index, at, at.astimezone(EASTERN).strftime("%H%M%S"), scope)
            )
            at += cadence
        if not slots:
            raise ScheduleError("empty collection schedule")
        return cls(
            session_date=session_date,
            policy=policy,
            session_open=session_open,
            session_close=session_close,
            early_close=is_early_close(session_date),
            slots=tuple(slots),
            decision_grid=decision_grid(session_date, policy.contract),
            max_attempts_per_request=max_attempts_per_request,
        )

    # -- derived facts ---------------------------------------------------------

    @property
    def preparation_opens(self) -> datetime:
        """Midnight Eastern of the session day: the earliest a dry run can
        produce this session's approval."""
        return datetime.combine(self.session_date, time(0, 0), EASTERN).astimezone(UTC)

    @property
    def request_budget(self) -> dict[str, int]:
        requests = sum(len(slot.scope) for slot in self.slots)
        return {
            "cycles": len(self.slots),
            "requests": requests,
            "max_attempts_per_request": self.max_attempts_per_request,
            "max_attempts": requests * self.max_attempts_per_request,
        }

    def decision_coverage(self) -> dict[str, Any]:
        """Which decision instants have at least one slot at or before them."""
        first = self.slots[0].scheduled_at
        covered = [d for d in self.decision_grid if d >= first]
        return {
            "decisions": len(self.decision_grid),
            "decisions_preceded_by_a_slot": len(covered),
            "first_decision": self.decision_grid[0].isoformat(),
            "last_decision": self.decision_grid[-1].isoformat(),
            "slots_before_first_decision": sum(
                1 for s in self.slots if s.scheduled_at < self.decision_grid[0]
            ),
            "slots_after_last_decision": sum(
                1 for s in self.slots if s.scheduled_at > self.decision_grid[-1]
            ),
        }

    def phase(self, now: datetime) -> str:
        """Where an instant falls relative to this schedule."""
        if market_session_date(now) != self.session_date:
            return "OTHER_SESSION_DATE"
        if now < self.preparation_opens:
            return "BEFORE_PREPARATION"
        if now < self.session_open:
            return "PREPARATION"
        last_start = self.slots[-1].scheduled_at + timedelta(
            seconds=self.policy.start_tolerance_seconds
        )
        if now <= last_start:
            return "COLLECTION"
        return "AFTER_COLLECTION"

    def slot(self, label: str) -> Slot:
        for slot in self.slots:
            if slot.label == label:
                return slot
        raise ScheduleError(f"no slot labelled {label!r}")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEDULE_SCHEMA,
            "session_date": self.session_date.isoformat(),
            "policy": self.policy.as_dict(),
            "session_open": self.session_open.isoformat(),
            "session_close": self.session_close.isoformat(),
            "early_close": self.early_close,
            "preparation_opens": self.preparation_opens.isoformat(),
            "slots": [slot.as_dict() for slot in self.slots],
            "request_budget": self.request_budget,
            "decision_coverage": self.decision_coverage(),
            "endpoint_cadence": {
                "market": sorted(MARKET_SCOPE),
                "refresh": sorted(REFRESH_ENDPOINTS),
            },
        }

    @property
    def fingerprint(self) -> str:
        return digest_of(canonical_payload(self.semantic_payload()))

    def as_dict(self) -> dict[str, Any]:
        return {**self.semantic_payload(), "fingerprint": self.fingerprint}
