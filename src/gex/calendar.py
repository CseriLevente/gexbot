"""US equity/index-option trading calendar.

Needed for one specific job: ageing open interest in *sessions* rather than
calendar days. OI is a settlement artefact published once per session, so
"3 days old" is meaningless across a weekend and actively misleading across a
holiday — Thursday's OI read on the Tuesday after a long weekend is one session
old, not five days stale.

Rule-based rather than a hard-coded date list, so it keeps working in future
years without maintenance. The rules encoded here are the NYSE/Cboe equity
holiday schedule as it has stood since Juneteenth was added in 2022; anything
before that is out of range for this project's research window and the calendar
says so rather than guessing.

Ad-hoc closures (national days of mourning, weather, 9/11) are not predictable
from rules. :func:`add_ad_hoc_closure` exists so they can be injected from
configuration when a research window needs one.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache

from src.gex.sessions import EASTERN, to_eastern

CALENDAR_VALID_FROM_YEAR = 2022

REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
REGULAR_OPEN = time(9, 30)

# Injected closures that no rule can predict. Keyed by date.
_AD_HOC_CLOSURES: set[date] = set()


def add_ad_hoc_closure(day: date) -> None:
    """Register an unscheduled full-day market closure."""
    _AD_HOC_CLOSURES.add(day)
    trading_holidays.cache_clear()
    is_trading_session.cache_clear()


def clear_ad_hoc_closures() -> None:
    _AD_HOC_CLOSURES.clear()
    trading_holidays.cache_clear()
    is_trading_session.cache_clear()


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """Weekend holidays move to the adjacent weekday, per NYSE practice."""
    if day.weekday() == 5:  # Saturday -> Friday before
        return day - timedelta(days=1)
    if day.weekday() == 6:  # Sunday -> Monday after
        return day + timedelta(days=1)
    return day


def easter_sunday(year: int) -> date:
    """Anonymous Gregorian computus.

    Needed only to locate Good Friday, which is the one moveable US market
    holiday and the one most often missed by hand-written calendars.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lunar = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lunar) // 451
    month, day = divmod(h + lunar - 7 * m + 114, 31)
    return date(year, month, day + 1)


def good_friday(year: int) -> date:
    return easter_sunday(year) - timedelta(days=2)


@lru_cache(maxsize=64)
def trading_holidays(year: int) -> frozenset[date]:
    """Full-day market closures for a calendar year."""
    if year < CALENDAR_VALID_FROM_YEAR:
        raise ValueError(
            f"trading calendar rules are only encoded from "
            f"{CALENDAR_VALID_FROM_YEAR}; got {year}"
        )
    days = {
        _observed(date(year, 1, 1)),  # New Year's Day
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        good_friday(year),
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed(date(year, 6, 19)),  # Juneteenth
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas
    }
    days |= {day for day in _AD_HOC_CLOSURES if day.year == year}
    return frozenset(days)


@lru_cache(maxsize=4096)
def is_trading_session(day: date) -> bool:
    """True when the equity/index-option market is open at all that day."""
    if day.weekday() >= 5:
        return False
    return day not in trading_holidays(day.year)


def early_closes(year: int) -> frozenset[date]:
    """Sessions ending at 13:00 ET instead of 16:00 ET.

    Matters for expiration modelling: a PM-settled SPXW series expiring on an
    early-close day stops accruing gamma three hours sooner than the regular
    rule implies.
    """
    if year < CALENDAR_VALID_FROM_YEAR:
        raise ValueError(
            f"trading calendar rules are only encoded from "
            f"{CALENDAR_VALID_FROM_YEAR}; got {year}"
        )
    days: set[date] = set()

    # Day after Thanksgiving.
    day_after_thanksgiving = _nth_weekday(year, 11, 3, 4) + timedelta(days=1)
    if is_trading_session(day_after_thanksgiving):
        days.add(day_after_thanksgiving)

    # July 3, when Independence Day itself falls on a regular weekday.
    july_third = date(year, 7, 3)
    if date(year, 7, 4).weekday() < 5 and is_trading_session(july_third):
        days.add(july_third)

    # Christmas Eve, when it is itself a session.
    christmas_eve = date(year, 12, 24)
    if is_trading_session(christmas_eve):
        days.add(christmas_eve)

    return frozenset(days)


def is_early_close(day: date) -> bool:
    return day in early_closes(day.year)


def session_close_time(day: date) -> time:
    return EARLY_CLOSE if is_early_close(day) else REGULAR_CLOSE


def session_close_datetime(day: date) -> datetime:
    """Timezone-aware close instant, honouring early closes and DST."""
    close = session_close_time(day)
    return datetime(
        day.year, day.month, day.day, close.hour, close.minute, tzinfo=EASTERN
    )


def previous_session(day: date) -> date:
    cursor = day - timedelta(days=1)
    while not is_trading_session(cursor):
        cursor -= timedelta(days=1)
    return cursor


def next_session(day: date) -> date:
    cursor = day + timedelta(days=1)
    while not is_trading_session(cursor):
        cursor += timedelta(days=1)
    return cursor


def sessions_between(start: date, end: date, *, max_lookback_days: int = 400) -> int:
    """Trading sessions strictly after ``start`` up to and including ``end``.

    This is the OI ageing function. Friday settlement read on Monday gives 1, not
    3. A holiday Monday pushes that to Tuesday, still 1.

    Returns 0 when ``end <= start``. The walk is bounded: an ``open_interest_as_of``
    that is years stale is a bug, and looping over it should not hang the
    ingest loop.
    """
    if end <= start:
        return 0
    span = (end - start).days
    if span > max_lookback_days:
        # Saturate rather than walk. The caller only needs "very stale".
        return max_lookback_days
    count = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        if is_trading_session(cursor):
            count += 1
    return count


def sessions_since(as_of: datetime, reference_date: date) -> int:
    """Sessions between a settlement date and an instant, in Eastern terms."""
    return sessions_between(reference_date, to_eastern(as_of).date())


__all__ = [
    "CALENDAR_VALID_FROM_YEAR",
    "EARLY_CLOSE",
    "REGULAR_CLOSE",
    "REGULAR_OPEN",
    "add_ad_hoc_closure",
    "clear_ad_hoc_closures",
    "early_closes",
    "easter_sunday",
    "good_friday",
    "is_early_close",
    "is_trading_session",
    "next_session",
    "previous_session",
    "session_close_datetime",
    "session_close_time",
    "sessions_between",
    "sessions_since",
    "trading_holidays",
]
