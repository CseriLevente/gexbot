"""US trading calendar and session-based ageing.

Dates are checked against the published NYSE schedule rather than against the
implementation, so a rule error surfaces as a wrong date rather than as a
self-consistent mistake.
"""

from __future__ import annotations

from datetime import UTC, date

import pytest

from src.gex.calendar import (
    CALENDAR_VALID_FROM_YEAR,
    EARLY_CLOSE,
    REGULAR_CLOSE,
    add_ad_hoc_closure,
    clear_ad_hoc_closures,
    early_closes,
    easter_sunday,
    good_friday,
    is_early_close,
    is_trading_session,
    next_session,
    previous_session,
    session_close_datetime,
    session_close_time,
    sessions_between,
    sessions_since,
    trading_holidays,
)
from src.gex.sessions import eastern


@pytest.fixture(autouse=True)
def _no_ad_hoc_closures():
    clear_ad_hoc_closures()
    yield
    clear_ad_hoc_closures()


# --- Easter / Good Friday ---------------------------------------------------


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (2022, date(2022, 4, 17)),
        (2023, date(2023, 4, 9)),
        (2024, date(2024, 3, 31)),
        (2025, date(2025, 4, 20)),
        (2026, date(2026, 4, 5)),
        (2027, date(2027, 3, 28)),
    ],
)
def test_easter_sunday_matches_the_published_dates(year, expected):
    assert easter_sunday(year) == expected


def test_good_friday_is_two_days_before_easter():
    assert good_friday(2026) == date(2026, 4, 3)
    assert not is_trading_session(date(2026, 4, 3))


# --- Holidays ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "name"),
    [
        (date(2026, 1, 1), "New Year's Day"),
        (date(2026, 1, 19), "MLK Day"),
        (date(2026, 2, 16), "Washington's Birthday"),
        (date(2026, 4, 3), "Good Friday"),
        (date(2026, 5, 25), "Memorial Day"),
        (date(2026, 6, 19), "Juneteenth"),
        (date(2026, 7, 3), "Independence Day observed (Jul 4 is a Saturday)"),
        (date(2026, 9, 7), "Labor Day"),
        (date(2026, 11, 26), "Thanksgiving"),
        (date(2026, 12, 25), "Christmas"),
    ],
)
def test_2026_holidays_are_closed(day, name):
    assert day in trading_holidays(2026), name
    assert not is_trading_session(day), name


def test_weekend_holiday_observance_shifts_to_the_adjacent_weekday():
    # 2027-01-01 is a Friday, so no shift; 2022-01-01 was a Saturday -> Dec 31.
    assert date(2027, 1, 1) in trading_holidays(2027)
    assert date(2021, 12, 31) == _observed_new_year(2022)


def _observed_new_year(year: int) -> date:
    from src.gex.calendar import _observed

    return _observed(date(year, 1, 1))


def test_calendar_refuses_years_before_the_encoded_rules():
    with pytest.raises(ValueError, match=str(CALENDAR_VALID_FROM_YEAR)):
        trading_holidays(2019)
    with pytest.raises(ValueError, match=str(CALENDAR_VALID_FROM_YEAR)):
        early_closes(2019)


def test_ordinary_weekday_is_a_session():
    assert is_trading_session(date(2026, 3, 17))  # a Tuesday


def test_weekends_are_never_sessions():
    assert not is_trading_session(date(2026, 3, 21))  # Saturday
    assert not is_trading_session(date(2026, 3, 22))  # Sunday


# --- Early closes -----------------------------------------------------------


def test_day_after_thanksgiving_closes_early():
    assert is_early_close(date(2026, 11, 27))
    assert session_close_time(date(2026, 11, 27)) == EARLY_CLOSE


def test_christmas_eve_closes_early_when_it_is_a_session():
    assert is_early_close(date(2026, 12, 24))  # a Thursday


def test_regular_day_closes_at_four():
    assert session_close_time(date(2026, 3, 17)) == REGULAR_CLOSE
    assert session_close_datetime(date(2026, 3, 17)).hour == 16


def test_early_close_datetime_is_timezone_aware_and_at_one_pm():
    close = session_close_datetime(date(2026, 11, 27))
    assert close.hour == 13
    assert close.tzinfo is not None
    assert close.tzname() == "EST"  # after the November DST transition


def test_early_close_datetime_respects_dst_in_summer():
    close = session_close_datetime(date(2026, 7, 2))
    assert close.tzname() == "EDT"


# --- Session walking --------------------------------------------------------


def test_previous_and_next_session_skip_the_weekend():
    monday = date(2026, 3, 23)
    friday = date(2026, 3, 20)
    assert previous_session(monday) == friday
    assert next_session(friday) == monday


def test_session_walking_skips_a_holiday():
    # 2026-05-25 is Memorial Day (Monday); the session before is Friday the 22nd.
    assert previous_session(date(2026, 5, 26)) == date(2026, 5, 22)


# --- Session-based ageing (the reason this module exists) -------------------


def test_friday_settlement_read_on_monday_is_one_session_old():
    """Calendar arithmetic would say three days and flag healthy data as stale."""
    assert sessions_between(date(2026, 3, 20), date(2026, 3, 23)) == 1


def test_consecutive_weekdays_are_one_session_apart():
    assert sessions_between(date(2026, 3, 17), date(2026, 3, 18)) == 1


def test_holiday_weekend_still_counts_as_one_session():
    """Friday before Memorial Day, read on the Tuesday after: one session."""
    assert sessions_between(date(2026, 5, 22), date(2026, 5, 26)) == 1


def test_thanksgiving_gap_counts_only_real_sessions():
    # Wed 2026-11-25 -> Mon 2026-11-30: Thu closed, Fri early (still a session),
    # weekend closed, Monday. So Friday + Monday = 2.
    assert sessions_between(date(2026, 11, 25), date(2026, 11, 30)) == 2


def test_same_day_and_backwards_spans_are_zero():
    assert sessions_between(date(2026, 3, 17), date(2026, 3, 17)) == 0
    assert sessions_between(date(2026, 3, 18), date(2026, 3, 17)) == 0


def test_very_stale_span_saturates_instead_of_walking():
    """An OI date years in the past is a bug; ageing it must not hang."""
    assert sessions_between(date(2020, 1, 1), date(2026, 3, 17)) == 400


def test_sessions_since_uses_eastern_date():
    """23:30 UTC is still the same US session day, 19:30 ET."""
    from datetime import datetime

    late_utc = datetime(2026, 3, 17, 23, 30, tzinfo=UTC)
    assert sessions_since(late_utc, date(2026, 3, 16)) == 1
    assert sessions_since(eastern(2026, 3, 17, 11, 0), date(2026, 3, 16)) == 1


def test_dst_transition_day_is_an_ordinary_session():
    """The spring-forward Sunday is not a session; the Monday after is."""
    assert not is_trading_session(date(2026, 3, 8))  # Sunday
    assert is_trading_session(date(2026, 3, 9))
    assert sessions_between(date(2026, 3, 6), date(2026, 3, 9)) == 1


# --- Ad-hoc closures --------------------------------------------------------


def test_ad_hoc_closure_removes_a_session():
    day = date(2026, 3, 18)
    assert is_trading_session(day)
    add_ad_hoc_closure(day)
    assert not is_trading_session(day)
    assert sessions_between(date(2026, 3, 17), date(2026, 3, 19)) == 1


def test_clearing_ad_hoc_closures_restores_the_session():
    day = date(2026, 3, 18)
    add_ad_hoc_closure(day)
    clear_ad_hoc_closures()
    assert is_trading_session(day)
