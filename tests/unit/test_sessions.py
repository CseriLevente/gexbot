"""US Eastern clock and settlement times.

Since v2.1.7 the zone *is* ``zoneinfo.ZoneInfo("America/New_York")``, with
``tzdata`` pinned. What these tests now guard is that the repository asks the
real database rather than re-deriving a rule beside it: the offsets, the
transition dates the calendar depends on, and -- the reason for the change --
that the repeated autumn hour is two distinct instants.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from src.domain.contracts import OptionRoot
from src.gex.sessions import (
    EASTERN,
    calendar_dte,
    dst_end,
    dst_start,
    eastern,
    is_regular_session,
    seconds_to_expiry,
    settlement_datetime,
    to_eastern,
)


def test_dst_transition_dates_match_us_rule():
    # Second Sunday in March, first Sunday in November.
    assert dst_start(2026).date() == date(2026, 3, 8)
    assert dst_end(2026).date() == date(2026, 11, 1)
    assert dst_start(2027).date() == date(2027, 3, 14)
    assert dst_end(2027).date() == date(2027, 11, 7)


@pytest.mark.parametrize(
    ("moment", "expected_offset_hours", "expected_name"),
    [
        (datetime(2026, 1, 15, 12, 0), -5, "EST"),
        (datetime(2026, 3, 7, 12, 0), -5, "EST"),  # day before DST
        (datetime(2026, 3, 9, 12, 0), -4, "EDT"),  # day after DST
        (datetime(2026, 7, 1, 12, 0), -4, "EDT"),
        (datetime(2026, 10, 31, 12, 0), -4, "EDT"),
        (datetime(2026, 11, 2, 12, 0), -5, "EST"),
    ],
)
def test_offsets_across_the_year(moment, expected_offset_hours, expected_name):
    aware = moment.replace(tzinfo=EASTERN)
    assert aware.utcoffset() == timedelta(hours=expected_offset_hours)
    assert aware.tzname() == expected_name


def test_pre_2007_dates_use_the_rule_that_was_actually_in_force():
    """The hand-written zone refused these. The database knows them.

    DST began on the first Sunday in April until 2007, so 1 April 2005 is still
    standard time -- a date the post-2005 rule would have called daylight.
    """
    assert datetime(2005, 4, 1, 12, 0, tzinfo=EASTERN).utcoffset() == timedelta(
        hours=-5
    )
    assert datetime(2005, 7, 1, 12, 0, tzinfo=EASTERN).utcoffset() == timedelta(
        hours=-4
    )


def test_the_repeated_autumn_hour_is_two_instants():
    """The reason ``tzdata`` is now a dependency.

    A wall-clock rule cannot express this: 01:30 occurs twice on the fall-back
    Sunday, an hour apart. The hand-written zone ignored ``fold`` and rendered
    the second occurrence as 02:30, which is the right offset on the wrong hour.
    """
    first = eastern(2026, 11, 1, 1, 30)
    second = eastern(2026, 11, 1, 1, 30, fold=1)

    assert first.astimezone(UTC) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert second.astimezone(UTC) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert second.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=1)

    # And the later one still reads 01:30, on standard time.
    assert (second.hour, second.minute) == (1, 30)
    assert second.utcoffset() == timedelta(hours=-5)
    assert first.utcoffset() == timedelta(hours=-4)


def test_a_round_trip_through_utc_preserves_the_later_occurrence():
    """to_eastern must not collapse the two folds onto one wall clock."""
    later = eastern(2026, 11, 1, 1, 30, fold=1)
    assert to_eastern(later.astimezone(UTC)).isoformat() == "2026-11-01T01:30:00-05:00"


def test_the_zone_is_the_iana_database():
    from src.gex.sessions import EASTERN_ZONE_NAME

    assert EASTERN_ZONE_NAME == "America/New_York"
    assert str(EASTERN) == EASTERN_ZONE_NAME


def test_spxw_settles_at_the_close_and_spx_at_the_open():
    expiry = date(2026, 3, 20)
    assert settlement_datetime(OptionRoot.SPXW, expiry).hour == 16
    assert settlement_datetime(OptionRoot.SPX, expiry).hour == 9
    assert settlement_datetime(OptionRoot.SPX, expiry).minute == 30


def test_seconds_to_expiry_uses_the_root_specific_clock():
    as_of = eastern(2026, 3, 20, 11, 0)
    expiry = date(2026, 3, 20)
    # SPXW still has five hours; the AM-settled SPX series is already done.
    assert seconds_to_expiry(as_of, OptionRoot.SPXW, expiry) == pytest.approx(5 * 3600)
    assert seconds_to_expiry(as_of, OptionRoot.SPX, expiry) == pytest.approx(
        -1.5 * 3600
    )


def test_utc_input_is_converted_not_reinterpreted():
    # 15:00 UTC in March (EDT) is 11:00 ET.
    utc = datetime(2026, 3, 17, 15, 0, tzinfo=UTC)
    assert to_eastern(utc).hour == 11


def test_calendar_dte_counts_in_eastern_terms():
    as_of = eastern(2026, 3, 17, 11, 0)
    assert calendar_dte(as_of, date(2026, 3, 17)) == 0
    assert calendar_dte(as_of, date(2026, 3, 20)) == 3
    assert calendar_dte(as_of, date(2026, 5, 15)) == 59


def test_late_utc_evening_does_not_roll_the_eastern_date():
    """23:30 UTC is still the same US trading day, 19:30 ET.

    A naive ``utcnow().date()`` here would advance DTE by one and shift every
    contract into the wrong expiry bucket for the last few hours of each day.
    """
    utc_late = datetime(2026, 3, 17, 23, 30, tzinfo=UTC)
    assert to_eastern(utc_late).date() == date(2026, 3, 17)
    assert calendar_dte(utc_late, date(2026, 3, 17)) == 0


def test_regular_session_bounds():
    assert not is_regular_session(eastern(2026, 3, 17, 9, 29))
    assert is_regular_session(eastern(2026, 3, 17, 9, 30))
    assert is_regular_session(eastern(2026, 3, 17, 15, 59))
    assert not is_regular_session(eastern(2026, 3, 17, 16, 0))
    assert not is_regular_session(eastern(2026, 3, 21, 12, 0))  # Saturday
