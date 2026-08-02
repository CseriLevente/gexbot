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

v2.1.6 unified the two readers. It did not make either of them right on the
autumn transition: the zone was hand-written, resolved its offset from the wall
clock, and so could not represent an hour that occurs twice. v2.1.7 uses
``zoneinfo`` and ``fold``, so 01:30 on the fall-back Sunday is two instants an
hour apart and each converts to the UTC the IANA database says it does.

Everything downstream refuses naive datetimes, which is what confines the
inference to this boundary. What this module adds is that the *result* of the
inference is a value: the raw text, the zone assumed, the normalised instant,
whether an assumption was applied at all, and how a DST ambiguity was resolved.
A reader of a stored observation can see what was decided rather than having to
know which module produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

__all__ = [
    "DEFAULT_VENDOR_TIMEZONE",
    "AmbiguityPolicy",
    "VendorTimestamp",
    "parse_vendor_timestamp",
]

#: The venue's zone. SPX and SPXW trade on Cboe, and the vendor's wall clock
#: follows the exchange rather than the reader's machine.
DEFAULT_VENDOR_TIMEZONE = "America/New_York"


def _zone(name: str) -> tzinfo:
    """The tzinfo for a named zone.

    The default resolves through ``src.gex.sessions`` so the whole repository
    shares one zone object, and one failure message when the timezone database
    is missing.
    """
    if name == DEFAULT_VENDOR_TIMEZONE:
        from src.gex.sessions import EASTERN

        return EASTERN
    if name in ("UTC", "Etc/UTC"):
        return UTC
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
#: on the spring-forward Sunday never happens; ``zoneinfo`` still yields an
#: instant, and the resolution is written down rather than silently accepted.
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

    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return VendorTimestamp(
            raw_value=text,
            assumed_timezone=assumed_timezone,
            normalized_utc=parsed.astimezone(UTC),
            localization_applied=False,
            ambiguity_resolution=NOT_APPLICABLE,
        )

    localised = parsed.replace(tzinfo=_zone(assumed_timezone), fold=ambiguity.fold)
    return VendorTimestamp(
        raw_value=text,
        assumed_timezone=assumed_timezone,
        normalized_utc=localised.astimezone(UTC),
        localization_applied=True,
        ambiguity_resolution=(
            NONEXISTENT if _is_nonexistent(localised) else ambiguity.value
        ),
    )


def _is_nonexistent(localised: datetime) -> bool:
    """Whether a localised wall clock names an instant the zone never had.

    02:30 on the spring-forward Sunday is a reading of a clock that never showed
    it. ``zoneinfo`` still produces an instant, and the round trip is what
    detects the problem: converting to UTC and back gives a *different* wall
    clock, because the original one does not exist.
    """
    round_tripped = localised.astimezone(UTC).astimezone(localised.tzinfo)
    return round_tripped.replace(tzinfo=None) != localised.replace(tzinfo=None)
