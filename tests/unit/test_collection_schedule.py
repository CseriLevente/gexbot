"""One schedule for preparation, allowed collection and decision coverage.

The schedule is derived from the repository calendar and the collection
policy, so the first slot is the calendar open (09:30:00 ET, inside the
one-shot's capture window without any override), early closes shorten the
day, and the research decision grid is covered by construction.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, date, datetime, timedelta

import pytest

from src.gex.sessions import EASTERN
from src.ingest.clock import FakeClock, SystemClock
from src.ingest.schedule import (
    FULL_SCOPE,
    MARKET_SCOPE,
    SCHEDULE_SCHEMA,
    CollectionPolicy,
    CollectionSchedule,
    ScheduleError,
)
from src.replay.research_contract import ResearchContract

pytestmark = pytest.mark.replay

REPO = pathlib.Path(__file__).resolve().parents[2]
SPEC = REPO / "config" / "intraday_pilot.json"
REGULAR = date(2026, 9, 8)
EARLY_CLOSE = date(2026, 11, 27)


def et(day: date, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=EASTERN)


@pytest.fixture(scope="module")
def regular() -> CollectionSchedule:
    return CollectionSchedule.build(
        REGULAR, CollectionPolicy(), max_attempts_per_request=4
    )


def test_the_first_slot_is_the_calendar_open_not_0929(regular):
    first = regular.slots[0]
    assert first.label == "093000"
    assert first.scheduled_at == et(REGULAR, 9, 30).astimezone(UTC)
    assert first.scope == FULL_SCOPE
    assert regular.slots[-1].label == "155900"
    assert len(regular.slots) == 390
    assert all(
        slot.scheduled_at.astimezone(EASTERN).time().second == 0
        for slot in regular.slots
    )


def test_full_scope_every_refresh_interval_market_scope_otherwise(regular):
    full = [slot.label for slot in regular.slots if slot.scope == FULL_SCOPE]
    assert full == [
        f"{h:02d}{m:02d}00" for h in range(9, 16) for m in (0, 30) if (h, m) >= (9, 30)
    ]
    assert len(full) == 13
    market = [slot for slot in regular.slots if slot.scope == MARKET_SCOPE]
    assert len(market) == 390 - 13
    assert {slot.kind for slot in regular.slots} == {"FULL", "MARKET"}


def test_the_budget_counts_requests_and_attempts(regular):
    budget = regular.request_budget
    assert budget == {
        "cycles": 390,
        "requests": 13 * 5 + (390 - 13) * 3,
        "max_attempts_per_request": 4,
        "max_attempts": (13 * 5 + (390 - 13) * 3) * 4,
    }


def test_every_grid_decision_is_preceded_by_a_slot(regular):
    coverage = regular.decision_coverage()
    assert coverage["decisions"] == coverage["decisions_preceded_by_a_slot"] == 371
    assert coverage["first_decision"] == et(REGULAR, 9, 35).astimezone(UTC).isoformat()
    assert coverage["slots_before_first_decision"] == 5
    assert coverage["slots_after_last_decision"] == 14


def test_an_early_close_shortens_the_schedule_from_the_calendar():
    schedule = CollectionSchedule.build(
        EARLY_CLOSE, CollectionPolicy(), max_attempts_per_request=1
    )
    assert schedule.early_close is True
    assert len(schedule.slots) == 210
    assert schedule.slots[-1].label == "125900"
    assert schedule.session_close == et(EARLY_CLOSE, 13, 0).astimezone(UTC)
    assert schedule.decision_coverage()["decisions_preceded_by_a_slot"] == 191


@pytest.mark.parametrize(
    "day", [date(2026, 9, 13), date(2026, 9, 7), date(2026, 12, 25)]
)
def test_a_non_trading_day_has_no_schedule(day):
    with pytest.raises(ScheduleError, match="not a trading session"):
        CollectionSchedule.build(day, CollectionPolicy(), max_attempts_per_request=1)


def test_phases_are_derived_from_the_same_schedule(regular):
    assert regular.phase(et(REGULAR, 0, 0)) == "PREPARATION"
    assert regular.phase(et(REGULAR, 9, 29, 59)) == "PREPARATION"
    assert regular.phase(et(REGULAR, 9, 30)) == "COLLECTION"
    assert regular.phase(et(REGULAR, 15, 59, 5)) == "COLLECTION"
    assert regular.phase(et(REGULAR, 15, 59, 6)) == "AFTER_COLLECTION"
    assert regular.phase(et(REGULAR, 16, 30)) == "AFTER_COLLECTION"
    assert regular.phase(et(date(2026, 9, 7), 12, 0)) == "OTHER_SESSION_DATE"
    assert regular.phase(et(date(2026, 9, 9), 9, 31)) == "OTHER_SESSION_DATE"
    assert regular.preparation_opens == et(REGULAR, 0, 0).astimezone(UTC)


def test_the_fingerprint_binds_every_slot_and_the_policy(regular):
    same = CollectionSchedule.build(
        REGULAR, CollectionPolicy(), max_attempts_per_request=4
    )
    assert same.fingerprint == regular.fingerprint
    other_policy = CollectionSchedule.build(
        REGULAR, CollectionPolicy(refresh_every_seconds=900), max_attempts_per_request=4
    )
    assert other_policy.fingerprint != regular.fingerprint
    other_budget = CollectionSchedule.build(
        REGULAR, CollectionPolicy(), max_attempts_per_request=2
    )
    assert other_budget.fingerprint != regular.fingerprint
    payload = regular.as_dict()
    assert payload["schema_version"] == SCHEDULE_SCHEMA
    assert payload["policy"]["one_cycle_in_flight"] is True
    assert payload["policy"]["missed_slot_policy"] == (
        "RECORD_AND_RESUME_AT_NEXT_FUTURE_BOUNDARY"
    )
    assert regular.slot("100000").scope == FULL_SCOPE
    with pytest.raises(ScheduleError, match="no slot"):
        regular.slot("093030")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("cadence_seconds", 5, "at least 10"),
        ("cadence_seconds", 90, "must equal the research contract cadence"),
        ("refresh_every_seconds", 90, "multiple of the cadence"),
        ("refresh_every_seconds", 30, "multiple of the cadence"),
        ("start_tolerance_seconds", 60, "shorter than the cadence"),
        ("max_consecutive_failed_cycles", 0, "at least one failed cycle"),
        ("cadence_seconds", 60.0, "nonnegative integer"),
        ("start_tolerance_seconds", -1, "nonnegative integer"),
    ],
)
def test_policies_that_cannot_be_honoured_are_refused(field, value, message):
    with pytest.raises(ScheduleError, match=message):
        CollectionPolicy(**{field: value})


def test_the_repository_specification_yields_the_default_policy():
    policy = CollectionPolicy.from_specification(SPEC)
    assert policy == CollectionPolicy()
    assert policy.contract == ResearchContract()
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    assert spec["cycle"]["first_cycle_et"] == "09:30:00"
    assert spec["cycle"]["last_cycle_et"] == "15:59:00"


def test_a_specification_with_another_contract_or_block_is_refused(tmp_path):
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    spec["research_contract"]["max_dte"] = 9
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(ScheduleError, match="not the repository default"):
        CollectionPolicy.from_specification(path)
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    spec["collection"]["extra"] = 1
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(ScheduleError, match="exactly"):
        CollectionPolicy.from_specification(path)
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    del spec["collection"]
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(ScheduleError, match="no collection block"):
        CollectionPolicy.from_specification(path)


def test_the_fake_clock_only_moves_forward():
    start = datetime(2026, 9, 8, 13, 0, tzinfo=UTC)
    clock = FakeClock(start, tick=timedelta(milliseconds=100))
    assert clock.peek() == start
    assert clock.now() == start + timedelta(milliseconds=100)
    clock.sleep_until(start)  # already past: no movement
    assert clock.peek() == start + timedelta(milliseconds=100)
    clock.sleep_until(start + timedelta(minutes=1))
    assert clock.peek() == start + timedelta(minutes=1)
    clock.advance(timedelta(seconds=2))
    assert clock.peek() == start + timedelta(minutes=1, seconds=2)
    assert clock.sleeps == [start, start + timedelta(minutes=1)]
    assert clock.reads == 1
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError, match="backwards"):
        FakeClock(start, tick=timedelta(seconds=-1))
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 9, 8, 13, 0))


def test_the_system_clock_returns_promptly_for_a_past_instant():
    clock = SystemClock()
    before = clock.now()
    clock.sleep_until(before - timedelta(seconds=5))
    assert clock.now() - before < timedelta(seconds=1)
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.sleep_until(datetime(2026, 9, 8, 13, 0))
