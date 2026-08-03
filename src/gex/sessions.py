"""US Eastern time and expiration-settlement clocks.

US Eastern is ``zoneinfo.ZoneInfo("America/New_York")``, and ``tzdata`` is a
pinned dependency so that Windows and minimal containers do not fall back on
whatever the host happens to have.

**This reverses a decision, and the reason is worth stating.** Until v2.1.6 the
zone was implemented here by hand, to keep the engine core free of any wheel:
the DST rule has been stable since 2007 and re-implementing it is twenty lines.
The flaw is not the rule, it is the representation. A hand-written ``tzinfo``
resolves its offset from the *wall clock*, and on the first Sunday in November
the wall clock 01:30 happens twice -- once at ``-04:00`` and again an hour later
at ``-05:00``. A wall-clock rule cannot tell them apart, so converting the
second instant back into Eastern returned ``02:30-05:00``: the right offset on
the wrong hour, an hour of error on an instant the IANA database has always had
correct. ``fold`` is precisely the mechanism for this, and only a real zone
implements it.

Carrying a wrong instant to avoid a data dependency is the wrong trade. ``tzdata``
ships no importable logic -- it is the IANA database and nothing else -- so the
engine core still executes no third-party code, which is what the bare-interpreter
check in CI actually protects.

Everything internal stays in UTC. Eastern is for display, for calendar rules and
for settlement clocks, which is where a wall clock is the thing being described.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.domain.contracts import OptionRoot
from src.domain.model_spec import ExpirationTimestampRule

#: The venue's zone. SPX and SPXW trade on Cboe, whose clock follows the
#: exchange rather than the reader's machine.
EASTERN_ZONE_NAME = "America/New_York"

#: Below this year the US DST rule differs from the current one. ``ZoneInfo``
#: handles the historical rules correctly, so this is no longer a correctness
#: boundary -- but no research window for this system reaches back that far, and
#: a date that old is far more likely to be a mistake than a request.
DST_RULE_VALID_FROM_YEAR = 2007


def eastern_zone() -> ZoneInfo:
    """The Eastern zone, or a message that says what to install.

    ``ZoneInfoNotFoundError`` on its own says "No time zone found with key
    America/New_York", which is true and unhelpful: the reader has to know that
    ``tzdata`` is a separate wheel on Windows before that sentence means
    anything.
    """
    try:
        return ZoneInfo(EASTERN_ZONE_NAME)
    except Exception as error:  # pragma: no cover - only without tzdata
        raise RuntimeError(
            f"the {EASTERN_ZONE_NAME} timezone database is unavailable: {error}. "
            "It is a pinned dependency of this package (`tzdata`); install the "
            "project rather than running from a bare interpreter. Every "
            "time-to-expiry in this engine is measured against this zone."
        ) from error


EASTERN = eastern_zone()


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Date of the ``n``-th ``weekday`` (Mon=0) in ``month``."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def dst_start(year: int) -> datetime:
    """Second Sunday in March, 02:00 local standard time (naive local).

    Kept because the *transition dates* are still worth naming -- the calendar
    and the timestamp tests assert against them. The offset itself now comes
    from ``ZoneInfo``; this is a statement about which Sunday, not about which
    offset.
    """
    day = _nth_weekday(year, 3, 6, 2)
    return datetime(day.year, day.month, day.day, 2, 0)


def dst_end(year: int) -> datetime:
    """First Sunday in November, 02:00 local daylight time (naive local)."""
    day = _nth_weekday(year, 11, 6, 1)
    return datetime(day.year, day.month, day.day, 2, 0)


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
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    *,
    fold: int = 0,
) -> datetime:
    """Build a timezone-aware US Eastern datetime.

    ``fold`` selects between the two occurrences of a repeated wall clock on the
    autumn transition Sunday: 0 is the first (daylight time), 1 the second
    (standard time). It is meaningless on every other instant of the year, and
    the default is the earlier reading.
    """
    return datetime(year, month, day, hour, minute, second, tzinfo=EASTERN, fold=fold)


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


def market_session_date(as_of: datetime) -> date:
    """Which trading session an instant belongs to, in the options market's zone.

    The one place a financial session date is produced. ``as_of.date()`` answers
    a different question -- it names the calendar day in whatever zone the value
    happens to carry -- and the two disagree for six hours out of every
    twenty-four.

    Concretely: 2026-03-18T01:00Z is the 18th in UTC and the *17th* in New York,
    where the options market was open. A settlement rule applied to the 18th
    derives a different prior session than one applied to the 17th, and open
    interest is the linear weight on every GEX term. Callers were reaching for
    ``.date()`` on a UTC instant in v2.1.9, so the answer depended on which
    representation of the same moment somebody happened to hold.

    Refuses naive input for the same reason :func:`to_eastern` does.
    """
    return to_eastern(as_of).date()


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
