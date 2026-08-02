"""One interpretation of a vendor timestamp, for the whole repository.

ThetaData v3 emits timestamps as wall-clock strings with no offset. What zone
they are in is an inference from the venue, not something the payload states --
see ``docs/OPEN_DECISIONS.md`` OD-2. That inference has to live in exactly one
place, because the failure mode when it does not is silent and large.

It did not. v2.1.5 had two:

* the ThetaData adapter localised a naive string to US Eastern;
* ``src/adapters/validation.py``, written later, read the same string as UTC.

So ``"2026-03-17T11:00:00.000"`` was 15:00 UTC when the adapter normalised a
chain and 11:00 UTC when the validator re-read the same bytes to check it. Four
hours. On a 0DTE contract that is not a slightly wrong gamma, and the module
disagreeing was the one whose job is to catch disagreements.

Everything downstream refuses naive datetimes, which is what confines the
inference to this boundary. What this module adds is that the *result* of the
inference is a value: the raw text, the zone assumed, the normalised instant,
whether an assumption was applied at all, and how a DST ambiguity was resolved.
A reader of a stored observation can see what was decided rather than having to
know which module produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from enum import Enum
from typing import Any

__all__ = [
    "DEFAULT_VENDOR_TIMEZONE",
    "AmbiguityPolicy",
    "VendorTimestamp",
    "parse_vendor_timestamp",
]

#: The venue's zone. SPX and SPXW trade on Cboe, and the vendor's wall clock
#: follows the exchange rather than the reader's machine.
#:
#: Resolved through ``src.gex.sessions.USEastern`` rather than ``zoneinfo``:
#: ``ZoneInfo("America/New_York")`` needs the ``tzdata`` wheel on Windows and
#: raises without it, and an engine whose time-to-expiry depends on whether an
#: optional data package happened to be installed is not one to trust on a 0DTE
#: afternoon. See the module docstring of ``src/gex/sessions.py``.
DEFAULT_VENDOR_TIMEZONE = "America/New_York"


def _zone(name: str) -> tzinfo:
    """The tzinfo for a named zone, without depending on ``tzdata``."""
    from src.gex.sessions import EASTERN

    if name == DEFAULT_VENDOR_TIMEZONE:
        return EASTERN
    if name in ("UTC", "Etc/UTC"):
        return UTC
    # Anything else is a deliberate configuration choice, and the caller has
    # accepted the tzdata dependency by naming it.
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


class AmbiguityPolicy(str, Enum):
    """Which of the two readings to take when a wall clock occurs twice.

    On the fall-back Sunday 01:30 happens once at EDT and again an hour later at
    EST. They are different instants and the difference is an hour of
    time-to-expiry, so the choice is recorded rather than left to whichever
    default the library happens to have.
    """

    #: The first occurrence -- still on daylight time.
    EARLIER = "EARLIER"
    #: The second occurrence -- after the clocks went back.
    LATER = "LATER"

    @property
    def fold(self) -> int:
        return 0 if self is AmbiguityPolicy.EARLIER else 1


#: Recorded when the wall clock names an instant the zone does not have. 02:30
#: on the spring-forward Sunday never happens; Python resolves it, and the
#: resolution is written down rather than silently accepted.
NONEXISTENT = "NONEXISTENT_WALL_CLOCK_NORMALISED"

#: Recorded when the payload carried an offset, so nothing had to be assumed.
NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class VendorTimestamp:
    """A vendor timestamp and everything that was decided to read it."""

    raw_value: str
    assumed_timezone: str
    normalized_utc: datetime
    #: False when the payload carried an offset and this module supplied nothing.
    localization_applied: bool
    ambiguity_resolution: str

    def in_zone(self, zone: str | None = None) -> datetime:
        """The same instant, expressed in a zone."""
        return self.normalized_utc.astimezone(_zone(zone or self.assumed_timezone))

    @property
    def vendor_local(self) -> datetime:
        """The instant as the vendor's own clock would read it."""
        return self.in_zone()

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_value": self.raw_value,
            "assumed_timezone": self.assumed_timezone,
            "normalized_utc": self.normalized_utc.isoformat(),
            "localization_applied": self.localization_applied,
            "ambiguity_resolution": self.ambiguity_resolution,
        }


def parse_vendor_timestamp(
    value: str | None,
    *,
    assumed_timezone: str = DEFAULT_VENDOR_TIMEZONE,
    ambiguity: AmbiguityPolicy = AmbiguityPolicy.EARLIER,
) -> VendorTimestamp | None:
    """Read one vendor timestamp. The only place a zone may be assumed.

    Returns ``None`` for anything unparseable, rather than a guess: a timestamp
    nobody can read is not a timestamp somebody should invent.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    zone = _zone(assumed_timezone)

    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return VendorTimestamp(
            raw_value=text,
            assumed_timezone=assumed_timezone,
            normalized_utc=parsed.astimezone(UTC),
            localization_applied=False,
            ambiguity_resolution=NOT_APPLICABLE,
        )

    transition = _transition_kind(parsed, assumed_timezone)

    if transition is _Transition.AMBIGUOUS:
        # The hour occurs twice. ``USEastern`` is written by hand and does not
        # honour ``fold`` -- deliberately, see src/gex/sessions.py -- so the two
        # readings are constructed from their offsets rather than requested from
        # the zone, and the one taken is recorded.
        offset = _DAYLIGHT if ambiguity is AmbiguityPolicy.EARLIER else _STANDARD
        return VendorTimestamp(
            raw_value=text,
            assumed_timezone=assumed_timezone,
            normalized_utc=(parsed - offset).replace(tzinfo=UTC),
            localization_applied=True,
            ambiguity_resolution=ambiguity.value,
        )

    if transition is _Transition.NONEXISTENT:
        # 02:30 on the spring-forward Sunday is a reading of a clock that never
        # showed it. Normalised through standard time, which maps it into the
        # hour the clock jumped to, and labelled so a reader knows the input was
        # not a real instant.
        return VendorTimestamp(
            raw_value=text,
            assumed_timezone=assumed_timezone,
            normalized_utc=(parsed - _STANDARD).replace(tzinfo=UTC),
            localization_applied=True,
            ambiguity_resolution=NONEXISTENT,
        )

    localised = parsed.replace(tzinfo=zone)
    return VendorTimestamp(
        raw_value=text,
        assumed_timezone=assumed_timezone,
        normalized_utc=localised.astimezone(UTC),
        localization_applied=True,
        ambiguity_resolution=ambiguity.value,
    )


#: US Eastern offsets. Named rather than inlined because the ambiguous-hour
#: readings are built from them directly.
_STANDARD = timedelta(hours=-5)
_DAYLIGHT = timedelta(hours=-4)


class _Transition(Enum):
    """Whether a wall clock reading is real, doubled, or skipped."""

    NORMAL = "NORMAL"
    AMBIGUOUS = "AMBIGUOUS"
    NONEXISTENT = "NONEXISTENT"


def _transition_kind(naive: datetime, assumed_timezone: str) -> _Transition:
    """Classify a naive reading against the zone's DST transitions."""
    if assumed_timezone != DEFAULT_VENDOR_TIMEZONE:
        # Another zone means the caller accepted ``tzdata``, and ``fold`` works
        # there, so there is nothing to reconstruct by hand.
        return _Transition.NORMAL

    from src.gex.sessions import dst_end, dst_start

    try:
        start, end = dst_start(naive.year), dst_end(naive.year)
    except ValueError:
        # Pre-2007, which the engine refuses elsewhere. Nothing to classify.
        return _Transition.NORMAL

    if start <= naive < start + timedelta(hours=1):
        return _Transition.NONEXISTENT
    if end - timedelta(hours=1) <= naive < end:
        return _Transition.AMBIGUOUS
    return _Transition.NORMAL
