"""Per-record timestamps and synchronisation tolerances.

The failure this module exists to prevent: stamping every normalised contract
with the request's ``as_of`` instant. That makes a five-minute-old quote and a
fresh one indistinguishable, and it makes the whole chain look perfectly fresh
no matter what the vendor actually returned. Freshness that is assigned rather
than measured is worse than no freshness metric at all.

So every source record keeps its own timestamp, joins measure the gap between
them, and nothing is ever back-stamped to ``as_of``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class ContractTimestamps:
    """Every clock that contributed to one normalised contract.

    ``None`` means the vendor did not supply it, which is information in itself
    and is scored as such -- it is never silently replaced with a nearby value.
    """

    # Source-record clocks, straight from the vendor payload.
    quote_timestamp: datetime | None = None
    greeks_timestamp: datetime | None = None
    iv_timestamp: datetime | None = None
    underlying_timestamp: datetime | None = None
    # Open interest is a settlement artefact, not intraday market data, so it is
    # a date rather than an instant. Modelling it as a timestamp would invite
    # comparing it against quote clocks, which is meaningless.
    open_interest_as_of: date | None = None

    # Our own clocks, for the audit trail.
    request_started_at: datetime | None = None
    response_received_at: datetime | None = None
    normalized_at: datetime | None = None

    #: Which vendor response each selected clock actually came from, and whether
    #: a timezone was assumed for *that* record.
    #:
    #: The chain-level ledger records every source inspected. That is a fact
    #: about the response set, not about this contract: a chain whose quotes are
    #: timezone-aware and whose greeks are naive reports both, and nothing said
    #: which one supplied *this* contract's IV clock. Provenance that does not
    #: name the record actually used is not provenance.
    #:
    #: Keyed by role (``quote``, ``implied_vol``, ``underlying``,
    #: ``open_interest``, ``gamma``); each value is
    #: ``{"source": <endpoint role>, "localization_applied": bool}``.
    selected_timestamp_sources: dict[str, dict[str, object]] = field(
        default_factory=dict
    )

    def selected_sources(self) -> dict[str, dict[str, object]]:
        """The recorded provenance, sorted for deterministic serialisation."""
        return {
            role: dict(sorted(detail.items()))
            for role, detail in sorted(self.selected_timestamp_sources.items())
        }

    @property
    def source_clocks(self) -> dict[str, datetime | None]:
        return {
            "quote_timestamp": self.quote_timestamp,
            "greeks_timestamp": self.greeks_timestamp,
            "iv_timestamp": self.iv_timestamp,
            "underlying_timestamp": self.underlying_timestamp,
        }

    @property
    def newest_source(self) -> datetime | None:
        present = [ts for ts in self.source_clocks.values() if ts is not None]
        return max(present) if present else None

    @property
    def oldest_source(self) -> datetime | None:
        present = [ts for ts in self.source_clocks.values() if ts is not None]
        return min(present) if present else None

    @property
    def internal_spread_seconds(self) -> float | None:
        """Widest gap between this contract's own source clocks."""
        oldest, newest = self.oldest_source, self.newest_source
        if oldest is None or newest is None:
            return None
        return (newest - oldest).total_seconds()

    def skew_seconds(self, left: str, right: str) -> float | None:
        clocks = self.source_clocks
        a, b = clocks.get(left), clocks.get(right)
        if a is None or b is None:
            return None
        return abs((a - b).total_seconds())

    def round_trip_seconds(self) -> float | None:
        if self.request_started_at is None or self.response_received_at is None:
            return None
        return (self.response_received_at - self.request_started_at).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        def render(value: datetime | date | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "quote_timestamp": render(self.quote_timestamp),
            "greeks_timestamp": render(self.greeks_timestamp),
            "iv_timestamp": render(self.iv_timestamp),
            "underlying_timestamp": render(self.underlying_timestamp),
            "open_interest_as_of": render(self.open_interest_as_of),
            "request_started_at": render(self.request_started_at),
            "response_received_at": render(self.response_received_at),
            "normalized_at": render(self.normalized_at),
        }


@dataclass(frozen=True, slots=True)
class DataQualityLimits:
    """Synchronisation tolerances.

    These are *research* limits describing when a joined record stops
    representing a single instant. They are not trading parameters, and nothing
    in this repository turns them into an order.

    Defaults are deliberately loose enough not to reject an ordinary snapshot and
    tight enough to catch a stalled feed. They are data-plumbing facts rather
    than market claims, which is why they carry real values instead of the
    UNSPECIFIED_CALIBRATE sentinel used for market thresholds.
    """

    # Quote vs greeks: the vendor computes greeks from a quote, so a large gap
    # means the greeks describe a book that no longer exists.
    max_quote_greeks_skew_seconds: float = 5.0
    max_quote_iv_skew_seconds: float = 5.0
    # Quote vs underlying: the gamma input pair. Tightest of the three.
    max_quote_underlying_skew_seconds: float = 2.0
    # Clock drift between our host and the vendor's. Beyond this, a
    # "future" timestamp is a fault rather than skew.
    max_future_timestamp_seconds: float = 2.0
    # Beyond this the snapshot is not describing the current market at all.
    max_snapshot_age_seconds: float = 60.0
    # Open interest older than this many *sessions* indicates a stalled job.
    # T-1 is normal and expected, so 1 session is not stale.
    max_open_interest_age_sessions: int = 2

    def as_dict(self) -> dict[str, float | int]:
        return {
            "max_quote_greeks_skew_seconds": self.max_quote_greeks_skew_seconds,
            "max_quote_iv_skew_seconds": self.max_quote_iv_skew_seconds,
            "max_quote_underlying_skew_seconds": (
                self.max_quote_underlying_skew_seconds
            ),
            "max_future_timestamp_seconds": self.max_future_timestamp_seconds,
            "max_snapshot_age_seconds": self.max_snapshot_age_seconds,
            "max_open_interest_age_sessions": self.max_open_interest_age_sessions,
        }
