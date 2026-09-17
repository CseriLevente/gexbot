"""The collector's clock, injectable so the schedule policy is testable.

Two implementations: the wall clock, and a fake that only moves when told.
Every instant is timezone-aware UTC. The collector reads time from nothing
else, and hands the same clock to the capture command so the receipts it
records come from one source.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def sleep_until(self, moment: datetime) -> None: ...


def _aware(moment: datetime, name: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return moment.astimezone(UTC)


class SystemClock:
    """The wall clock. Sleeps in short slices so an interrupt is prompt."""

    slice_seconds = 1.0

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep_until(self, moment: datetime) -> None:
        target = _aware(moment, "moment")
        while True:
            remaining = (target - self.now()).total_seconds()
            if remaining <= 0:
                return
            time.sleep(min(remaining, self.slice_seconds))


class FakeClock:
    """A clock that advances only through ``advance``, ``sleep_until`` and,
    when a ``tick`` is given, every read.

    ``sleep_until`` never moves backwards, so a caller that asks to sleep until
    an instant already past returns immediately -- exactly what a real clock
    does. A ``tick`` makes each read land a little after the previous one, the
    way a transport's request and receipt stamps do, without a test scripting
    every instant; a fake transport can also call ``advance`` per request to
    simulate the time a cycle takes.
    """

    def __init__(self, start: datetime, *, tick: timedelta = timedelta(0)) -> None:
        if tick < timedelta(0):
            raise ValueError("a clock does not run backwards")
        self._now = _aware(start, "start")
        self._tick = tick
        self.sleeps: list[datetime] = []
        self.reads = 0

    def now(self) -> datetime:
        self.reads += 1
        self._now = self._now + self._tick
        return self._now

    def peek(self) -> datetime:
        """The current instant without ticking."""
        return self._now

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("a clock does not run backwards")
        self._now = self._now + delta

    def sleep_until(self, moment: datetime) -> None:
        target = _aware(moment, "moment")
        self.sleeps.append(target)
        if target > self._now:
            self._now = target
