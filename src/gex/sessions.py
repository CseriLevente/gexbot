"""US Eastern time and expiration-settlement clocks.

Why this file exists instead of ``zoneinfo.ZoneInfo("America/New_York")``:

* Time-to-expiry drives gamma, and on 0DTE it dominates it. Being one hour out
  on an SPXW expiration afternoon does not produce a slightly wrong number, it
  produces a completely wrong one.
* ``zoneinfo`` needs the ``tzdata`` package on Windows, and it raises
  ``ZoneInfoNotFoundError`` when it is absent. A trading engine must not depend
  on whether an optional data wheel happened to get installed.

So US Eastern is implemented here directly. The rule has been stable since the
Energy Policy Act of 2005 took effect in 2007: DST runs from the second Sunday
in March at 02:00 local standard time to the first Sunday in November at 02:00
local daylight time. ``tests/unit/test_sessions.py`` cross-checks this against
``ZoneInfo`` whenever ``tzdata`` *is* present, so the two can never silently
diverge in production.

Pre-2007 dates use the same rule and are therefore wrong; the engine rejects
them rather than pretending otherwise, since no research window for this system
reaches back that far.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo

from src.domain.contracts import OptionRoot

_EST = timedelta(hours=-5)
_EDT = timedelta(hours=-4)
_ZERO = timedelta(0)

DST_RULE_VALID_FROM_YEAR = 2007


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Date of the ``n``-th ``weekday`` (Mon=0) in ``month``."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def dst_start(year: int) -> datetime:
    """Second Sunday in March, 02:00 local standard time (naive local)."""
    day = _nth_weekday(year, 3, 6, 2)
    return datetime(day.year, day.month, day.day, 2, 0)


def dst_end(year: int) -> datetime:
    """First Sunday in November, 02:00 local daylight time (naive local)."""
    day = _nth_weekday(year, 11, 6, 1)
    return datetime(day.year, day.month, day.day, 2, 0)


class USEastern(tzinfo):
    """America/New_York, post-2007 rule, no external data required."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "USEastern()"

    def _is_dst(self, dt: datetime) -> bool:
        if dt.year < DST_RULE_VALID_FROM_YEAR:
            raise ValueError(
                f"US Eastern DST rule in this module is only valid from "
                f"{DST_RULE_VALID_FROM_YEAR}; got {dt.year}"
            )
        naive = dt.replace(tzinfo=None)
        # The gap hour (02:00-03:00 in March) and the repeated hour (01:00-02:00
        # in November) are handled by treating the wall clock as monotonic. Both
        # windows sit outside any US equity or index-option session, so the
        # ambiguity never touches a real expiration or bar timestamp.
        return dst_start(dt.year) <= naive < dst_end(dt.year)

    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return _EST
        return _EDT if self._is_dst(dt) else _EST

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return _ZERO
        return timedelta(hours=1) if self._is_dst(dt) else _ZERO

    def tzname(self, dt: datetime | None) -> str:
        if dt is None:
            return "EST"
        return "EDT" if self._is_dst(dt) else "EST"


EASTERN = USEastern()

# --- Session and settlement clocks -----------------------------------------

RTH_OPEN = (9, 30)
RTH_CLOSE = (16, 0)

# SPXW is PM-settled and the expiring series trades until 16:00 ET on its
# expiration day. SPX standard series are AM-settled: SET is struck from
# expiration-morning opening prices, so for gamma purposes the series dies at the
# 09:30 ET open, not at the close.
SETTLEMENT_TIME_ET: dict[OptionRoot, tuple[int, int]] = {
    OptionRoot.SPXW: RTH_CLOSE,
    OptionRoot.SPX: RTH_OPEN,
}


def eastern(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    """Build a timezone-aware US Eastern datetime."""
    return datetime(year, month, day, hour, minute, second, tzinfo=EASTERN)


def to_eastern(dt: datetime) -> datetime:
    """Convert to US Eastern. Naive input is *assumed* to already be Eastern.

    Assuming rather than guessing is deliberate: every timestamp entering the
    engine is stamped by an adapter, and adapters are required to attach tzinfo.
    A naive datetime reaching here means the snapshot was hand-built (tests,
    fixtures), where Eastern is the useful default.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=EASTERN)
    return dt.astimezone(EASTERN)


def settlement_datetime(root: OptionRoot, expiry: date) -> datetime:
    """The moment the series stops accruing gamma."""
    hour, minute = SETTLEMENT_TIME_ET[root]
    return eastern(expiry.year, expiry.month, expiry.day, hour, minute)


def seconds_to_expiry(as_of: datetime, root: OptionRoot, expiry: date) -> float:
    """Seconds from ``as_of`` to settlement. Negative once the series has died."""
    return (settlement_datetime(root, expiry) - to_eastern(as_of)).total_seconds()


def calendar_dte(as_of: datetime, expiry: date) -> int:
    """Calendar days to expiry in Eastern terms.

    Bucketing uses calendar DTE (0DTE means "expires today"), which is what the
    plan's buckets describe -- deliberately distinct from the continuous
    ``seconds_to_expiry`` used for pricing.
    """
    return (expiry - to_eastern(as_of).date()).days


def weekdays_between(start: date, end: date) -> int:
    """Weekdays strictly after ``start`` up to and including ``end``.

    Used to age open interest. Calendar-day arithmetic would report Friday's
    settlement read on Monday morning as three days stale, when it is one session
    stale and perfectly normal.

    Exchange holidays are *not* handled here -- that needs the holiday calendar
    owned by ``reference_service`` in the plan. The effect is that OI can look one
    session older than it is across a holiday, which errs toward caution.
    """
    total = (end - start).days
    if total <= 0:
        return 0
    full_weeks, extra = divmod(total, 7)
    count = full_weeks * 5
    for offset in range(1, extra + 1):
        if (start + timedelta(days=full_weeks * 7 + offset)).weekday() < 5:
            count += 1
    return count


def is_regular_session(dt: datetime) -> bool:
    """Inside 09:30-16:00 ET on a weekday. Holiday calendar is a separate
    concern -- see ``reference_service`` in the plan; this is the time-of-day
    guard only.
    """
    et = to_eastern(dt)
    if et.weekday() >= 5:
        return False
    open_minutes = RTH_OPEN[0] * 60 + RTH_OPEN[1]
    close_minutes = RTH_CLOSE[0] * 60 + RTH_CLOSE[1]
    return open_minutes <= et.hour * 60 + et.minute < close_minutes
