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

from datetime import date, datetime, timedelta, tzinfo

from src.domain.contracts import OptionRoot
from src.domain.model_spec import ExpirationTimestampRule

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


class NaiveTimestampError(ValueError):
    """A naive datetime reached a layer that is not allowed to guess a zone."""


def require_aware(dt: datetime | None, *, field: str) -> datetime | None:
    """Assert a timestamp carries a timezone. ``None`` passes through.

    The explicit guard for domain boundaries. Absence is a legitimate state that
    the validation layer scores; a *naive* value is not, because accepting it
    means silently choosing a zone.
    """
    if dt is None:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise NaiveTimestampError(
            f"{field}: naive datetime {dt.isoformat()} is not accepted here. "
            "Vendor timestamps may be localised only by the adapter, which "
            "records the assumption it applied; see docs/VALIDATION.md."
        )
    return dt


def to_eastern(dt: datetime) -> datetime:
    """Convert an aware datetime to US Eastern.

    **Refuses naive input.** Until v2.1 this silently reinterpreted a naive
    datetime as Eastern, which is a guess of up to five hours made in a
    general-purpose utility called from the engine, the confidence model and the
    calendar. A guess made here leaves no record that any assumption was applied
    and is indistinguishable from a real conversion.

    Localisation now happens at exactly one place -- the vendor parser in
    ``src/adapters/thetadata/client.py`` -- which records the assumption in
    snapshot metadata.
    """
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise NaiveTimestampError(
            f"to_eastern received a naive datetime ({dt.isoformat()}). Only the "
            "vendor adapter may localise, and it records the assumption; "
            "everything downstream must already be timezone-aware."
        )
    return dt.astimezone(EASTERN)


def settlement_datetime(
    root: OptionRoot, expiry: date, *, honour_early_close: bool = False
) -> datetime:
    """The moment the series stops accruing gamma.

    ``honour_early_close`` shortens a PM-settled expiration to 13:00 ET on an
    early-close session. It is opt-in rather than automatic because it pulls in
    the trading calendar, and the engine records which rule it used in
    ``ModelSpec.expiration_timestamp_rule`` so the two can never silently
    disagree.
    """
    hour, minute = SETTLEMENT_TIME_ET[root]
    if honour_early_close and (hour, minute) == RTH_CLOSE:
        # Imported lazily: sessions is the lower-level module and the calendar
        # depends on it, so a module-level import would be circular.
        from src.gex.calendar import CALENDAR_VALID_FROM_YEAR, is_early_close

        if expiry.year >= CALENDAR_VALID_FROM_YEAR and is_early_close(expiry):
            hour, minute = 13, 0
    return eastern(expiry.year, expiry.month, expiry.day, hour, minute)


def seconds_to_expiry(
    as_of: datetime,
    root: OptionRoot,
    expiry: date,
    *,
    honour_early_close: bool = False,
) -> float:
    """Seconds from ``as_of`` to settlement. Negative once the series has died."""
    settlement = settlement_datetime(
        root, expiry, honour_early_close=honour_early_close
    )
    return (settlement - to_eastern(as_of)).total_seconds()


def expiration_timestamp(
    *,
    root: OptionRoot,
    expiry: date,
    rule: ExpirationTimestampRule,
) -> datetime:
    """Resolve the effective expiration instant under a stated rule.

    Every supported rule produces a genuinely different timestamp -- and hence a
    different time-to-expiry and a different gamma. Before v2.1 two of these
    rules changed only metadata, which let a fingerprint move while every number
    stayed identical. A rule that does not affect the calculation is worse than
    no rule, because it makes the audit trail claim a distinction that the maths
    never made.

    ``CALENDAR_MIDNIGHT`` raises. Options do not expire at midnight, and the name
    never disclosed whether it meant the start or the end of the expiration date.
    Config validation rejects it too, so this is a defence in depth rather than
    the only guard.
    """
    if rule is ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT:
        return settlement_datetime(root, expiry, honour_early_close=False)

    if rule is ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT_WITH_EARLY_CLOSE:
        return settlement_datetime(root, expiry, honour_early_close=True)

    if rule is ExpirationTimestampRule.FIXED_1600_ET:
        # Deliberately ignores the root: this rule exists to quantify what the
        # AM/PM distinction is worth, so applying AM settlement here would defeat
        # its only purpose. Early closes are also ignored, for the same reason.
        # A weekend or holiday expiry date is left as given -- the rule describes
        # a clock, not a calendar, and silently moving the date would be a bigger
        # assumption than the one being measured.
        return eastern(expiry.year, expiry.month, expiry.day, *RTH_CLOSE)

    raise ValueError(
        f"expiration rule {rule.value} is not supported: {rule.unsupported_reason}"
    )


def seconds_to_expiry_at(as_of: datetime, expiration: datetime) -> float:
    """Seconds between an instant and an already-resolved expiration timestamp."""
    return (to_eastern(expiration) - to_eastern(as_of)).total_seconds()


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
