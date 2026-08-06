"""Whether now is a moment worth spending a paid session on.

A snapshot capture taken at 03:00 ET on a Sunday is bytes. It is not a chain:
the quotes are stale, the open interest belongs to a session two days back, and
the index print is whatever the vendor last had. v2.1.16 would have run it
without comment, and the resulting capture would have looked exactly like a good
one -- same manifest, same integrity scan, same verified state.

So the run refuses by default, and the override is a flag with a name that says
what it is. An operator who genuinely wants an out-of-session diagnostic can
have one; they cannot get one by accident, and the capture says so afterwards.

Everything here comes from the existing Eastern-timezone helpers and the
existing trading calendar. No new calendar, no new timezone rule: a second
opinion about when the market is open is exactly the kind of thing that drifts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any

from src.gex.calendar import (
    CALENDAR_VALID_FROM_YEAR,
    is_early_close,
    is_trading_session,
)
from src.gex.sessions import RTH_CLOSE, RTH_OPEN, market_session_date, to_eastern

__all__ = [
    "EARLY_CLOSE_TIME",
    "CaptureWindow",
    "SessionStatus",
    "assess_capture_window",
]

#: US equity/option markets close at 13:00 ET on an early-close session.
EARLY_CLOSE_TIME = time(13, 0)


class SessionStatus(str, Enum):
    """Where an instant falls relative to the options market's own day.

    Five outcomes rather than a boolean, because an operator does different
    things about each: wait twenty minutes, wait until Tuesday, or accept that
    the capture will be a diagnostic.
    """

    #: Inside the regular session. The only status a capture runs under by
    #: default.
    REGULAR_SESSION = "REGULAR_SESSION"
    #: A trading day, and the session has not opened yet.
    BEFORE_OPEN = "BEFORE_OPEN"
    #: A trading day, and the session has closed.
    AFTER_CLOSE = "AFTER_CLOSE"
    #: A weekend or an exchange holiday.
    NON_TRADING_DAY = "NON_TRADING_DAY"
    #: Outside the years the bundled calendar covers. Refused rather than
    #: guessed: a holiday table that has run out is not a table that says "open".
    CALENDAR_UNKNOWN = "CALENDAR_UNKNOWN"

    @property
    def inside_capture_window(self) -> bool:
        return self is SessionStatus.REGULAR_SESSION


@dataclass(frozen=True, slots=True)
class CaptureWindow:
    """The market's own clock, as the operator report prints it."""

    market_time_et: datetime
    market_session_date: date
    status: SessionStatus
    early_close: bool
    #: ``None`` when the day is not a trading day: there is no session to bound.
    session_open: datetime | None
    session_close: datetime | None
    next_open: datetime | None

    @property
    def inside_capture_window(self) -> bool:
        return self.status.inside_capture_window

    @property
    def refusal(self) -> str:
        """Why a live capture would be refused, or ``""`` when it would not."""
        if self.inside_capture_window:
            return ""
        window = (
            f" The next regular session opens {self.next_open.isoformat()}."
            if self.next_open is not None
            else ""
        )
        return (
            f"{self.market_time_et.isoformat()} is {self.status.value} for the "
            f"{self.market_session_date.isoformat()} session. A snapshot taken "
            "outside the regular session is a picture of stale quotes against "
            "open interest from another day; it looks exactly like a good "
            f"capture afterwards.{window} Pass --allow-out-of-session to take "
            "one deliberately."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "market_time_et": self.market_time_et.isoformat(),
            "market_session_date": self.market_session_date.isoformat(),
            "session_status": self.status.value,
            "early_close": self.early_close,
            "market_session_open": (
                self.session_open.isoformat() if self.session_open else None
            ),
            "market_session_close": (
                self.session_close.isoformat() if self.session_close else None
            ),
            "inside_capture_window": self.inside_capture_window,
            "next_valid_capture_window": (
                self.next_open.isoformat() if self.next_open else None
            ),
        }


def _bounds(day: date, zone: Any) -> tuple[datetime, datetime, bool]:
    """Open and close for one trading day, honouring an early close."""
    early = is_early_close(day)
    close_at = EARLY_CLOSE_TIME if early else time(*RTH_CLOSE)
    opens = datetime.combine(day, time(*RTH_OPEN), tzinfo=zone)
    closes = datetime.combine(day, close_at, tzinfo=zone)
    return opens, closes, early


def _next_open(after: date, zone: Any, *, horizon: int = 10) -> datetime | None:
    """The next session open at or after a day, within the calendar's reach."""
    day = after
    for _ in range(horizon):
        if day.year < CALENDAR_VALID_FROM_YEAR:
            return None
        if is_trading_session(day):
            return datetime.combine(day, time(*RTH_OPEN), tzinfo=zone)
        day = day + timedelta(days=1)
    return None


def assess_capture_window(as_of: datetime) -> CaptureWindow:
    """Where this instant falls in the options market's day.

    Refuses naive input by way of :func:`to_eastern`, for the same reason every
    other clock in this repository does: an instant with no zone is a number
    somebody will interpret in whichever zone they are standing in.
    """
    et = to_eastern(as_of)
    session_day = market_session_date(as_of)
    zone = et.tzinfo

    if session_day.year < CALENDAR_VALID_FROM_YEAR:
        return CaptureWindow(
            market_time_et=et,
            market_session_date=session_day,
            status=SessionStatus.CALENDAR_UNKNOWN,
            early_close=False,
            session_open=None,
            session_close=None,
            next_open=None,
        )

    if not is_trading_session(session_day):
        return CaptureWindow(
            market_time_et=et,
            market_session_date=session_day,
            status=SessionStatus.NON_TRADING_DAY,
            early_close=False,
            session_open=None,
            session_close=None,
            next_open=_next_open(session_day + timedelta(days=1), zone),
        )

    opens, closes, early = _bounds(session_day, zone)
    if et < opens:
        status = SessionStatus.BEFORE_OPEN
        upcoming: datetime | None = opens
    elif et >= closes:
        status = SessionStatus.AFTER_CLOSE
        upcoming = _next_open(session_day + timedelta(days=1), zone)
    else:
        status = SessionStatus.REGULAR_SESSION
        upcoming = opens

    return CaptureWindow(
        market_time_et=et,
        market_session_date=session_day,
        status=status,
        early_close=early,
        session_open=opens,
        session_close=closes,
        next_open=upcoming,
    )
