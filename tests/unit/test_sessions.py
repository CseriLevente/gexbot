"""US Eastern clock and settlement times.

The cross-check against ``zoneinfo`` is the important test here: it is what stops
the hand-rolled DST rule from drifting away from the real tz database on a machine
where ``tzdata`` happens to be installed.
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


def test_rejects_years_before_the_2007_rule():
    with pytest.raises(ValueError, match="2007"):
        datetime(2005, 7, 1, tzinfo=EASTERN).utcoffset()


def test_matches_zoneinfo_when_tzdata_is_available():
    """Skipped where tzdata is absent (e.g. a bare Windows install)."""
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        reference = zoneinfo.ZoneInfo("America/New_York")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("tzdata not installed; hand-rolled rule cannot be cross-checked")

    probe = datetime(2026, 1, 1, 12, 0)
    for _ in range(365 * 2):
        assert probe.replace(tzinfo=EASTERN).utcoffset() == reference.utcoffset(
            probe
        ), f"offset mismatch at {probe}"
        probe += timedelta(days=1)


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
