"""Option chain and reference-data value objects.

These types are the boundary between vendor adapters and the GEX engine. An
adapter's only job is to produce a :class:`ChainSnapshot`; the engine never sees
a vendor payload.

Everything here is immutable and dependency-free on purpose -- the engine core
must be runnable and testable without PyYAML, httpx, numpy, a database, or
network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import Enum
from typing import Any

from src.domain.iv import ImpliedVolQuote, IVQualityFlag, missing_iv
from src.domain.timestamps import ContractTimestamps

# SPX/SPXW are cash-settled index options with a $100 multiplier.
INDEX_OPTION_MULTIPLIER = 100.0


class OptionRight(str, Enum):
    CALL = "call"
    PUT = "put"


class OptionRoot(str, Enum):
    """Root symbol.

    SPX and SPXW are deliberately *not* interchangeable: SPX standard series are
    AM-settled, SPXW weeklies/end-of-month are PM-settled and trade until 16:00
    ET on their expiration day. Intraday GEX must be able to tell them apart.
    """

    SPX = "SPX"
    SPXW = "SPXW"


@dataclass(frozen=True, slots=True)
class OptionContract:
    """Identity of a single option series."""

    root: OptionRoot
    expiry: date
    strike: float
    right: OptionRight
    multiplier: float = INDEX_OPTION_MULTIPLIER

    @property
    def key(self) -> tuple[str, date, float, str]:
        """Canonical identity, used as the join key across vendor responses.

        Root is part of the key: SPX and SPXW can share an expiry date and a
        strike while being different instruments with different settlement times.
        """
        return (self.root.value, self.expiry, self.strike, self.right.value)

    @property
    def canonical_id(self) -> str:
        return (
            f"{self.root.value}:{self.expiry.isoformat()}:"
            f"{self.strike:.4f}:{self.right.value}"
        )


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """Point-in-time market state for one contract.

    ``gamma`` is optional. When a vendor supplies it we can use it directly; when
    it is absent (e.g. ThetaData Standard tier, which exposes ``implied_vol`` but
    not second-order greeks) the engine derives it from the IV reading with the
    shadow pricer. See ``docs/THETADATA_INTEGRATION.md``.

    ``timestamps`` carries every source clock separately. Nothing here is ever
    back-stamped to the request instant -- see ``src/domain/timestamps.py``.
    """

    contract: OptionContract
    timestamps: ContractTimestamps = field(default_factory=ContractTimestamps)
    bid: float | None = None
    ask: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    last: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    iv: ImpliedVolQuote = field(default_factory=missing_iv)
    delta: float | None = None
    gamma: float | None = None
    vega: float | None = None
    theta: float | None = None
    # Underlying price as reported alongside *this* record. Kept per contract
    # because a vendor can return different underlying prints for different
    # expirations in one chain pull, and that disagreement is measurable only if
    # it is preserved.
    underlying_price: float | None = None

    @property
    def effective_iv(self) -> float | None:
        """The IV the engine will price with. Never used without its source.

        Named ``effective_iv`` rather than ``implied_vol`` so that reading a bare
        volatility number off a quote is impossible without also having
        ``quote.iv.source`` available.
        """
        return self.iv.value if self.iv.is_usable else None

    @property
    def open_interest_as_of(self) -> date | None:
        return self.timestamps.open_interest_as_of

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        if self.bid <= 0.0 and self.ask <= 0.0:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def spread_pct_of_mid(self) -> float | None:
        mid = self.mid
        if mid is None or mid <= 0.0:
            return None
        spread = self.spread
        return None if spread is None else spread / mid

    @property
    def is_crossed(self) -> bool:
        """Ask below bid -- a torn or stale book."""
        return self.bid is not None and self.ask is not None and self.ask < self.bid

    @property
    def is_locked(self) -> bool:
        """Bid equals ask at a non-zero price."""
        return (
            self.bid is not None
            and self.ask is not None
            and self.bid == self.ask
            and self.bid > 0.0
        )

    @property
    def is_zero_bid(self) -> bool:
        return self.bid is not None and self.bid <= 0.0

    @property
    def is_quotable(self) -> bool:
        return self.mid is not None and not self.is_crossed


@dataclass(frozen=True, slots=True)
class SnapshotClocks:
    """Request-level clocks, shared by every contract in one pull."""

    request_started_at: datetime | None = None
    response_received_at: datetime | None = None
    normalized_at: datetime | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "request_started_at": (
                self.request_started_at.isoformat() if self.request_started_at else None
            ),
            "response_received_at": (
                self.response_received_at.isoformat()
                if self.response_received_at
                else None
            ),
            "normalized_at": (
                self.normalized_at.isoformat() if self.normalized_at else None
            ),
        }


@dataclass(frozen=True, slots=True)
class ChainSnapshot:
    """A frozen option chain plus the reference data needed to price it.

    ``as_of`` is the single logical timestamp of the *request*, used as the
    reference instant for freshness and future-drift checks. It is explicitly
    **not** the timestamp of any individual record -- those live on each quote.

    The same ``ChainSnapshot`` must always produce the same ``GexSnapshot``; that
    property is what makes replay possible.
    """

    as_of: datetime
    spot: float
    quotes: tuple[OptionQuote, ...]
    # Continuously-compounded risk-free rate and dividend yield used by the
    # shadow pricer. SPX carries a material dividend yield, so q != 0.
    risk_free_rate: float = 0.0
    dividend_yield: float = 0.0
    clocks: SnapshotClocks = field(default_factory=SnapshotClocks)
    # Timestamp attached to the spot print itself, distinct from ``as_of``.
    spot_timestamp: datetime | None = None
    source: str = "unknown"
    # Number of contracts the adapter expected to receive, when knowable. Feeds
    # chain_completeness; ``None`` falls back to the usable/received ratio.
    expected_contract_count: int | None = None
    meta: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Only the checks that make the object meaningless are enforced here.
        # Everything else is reported through the validation pass rather than
        # raised, because one bad contract must not lose the whole chain.
        import math

        if not isinstance(self.spot, int | float) or isinstance(self.spot, bool):
            raise TypeError(f"spot must be a real number, got {type(self.spot)!r}")
        if not math.isfinite(self.spot):
            raise ValueError(f"spot must be finite, got {self.spot!r}")
        if self.spot <= 0.0:
            raise ValueError(f"spot must be positive, got {self.spot}")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError(
                "ChainSnapshot.as_of must be timezone-aware; a naive instant "
                "cannot be compared against vendor timestamps"
            )

    @property
    def expiries(self) -> tuple[date, ...]:
        return tuple(sorted({q.contract.expiry for q in self.quotes}))

    @property
    def strikes(self) -> tuple[float, ...]:
        return tuple(sorted({q.contract.strike for q in self.quotes}))

    @property
    def options_feed_timestamp(self) -> datetime | None:
        """Oldest quote clock in the chain.

        Oldest rather than newest: a partially-refreshed chain must report as
        stale, which is the safe direction to be wrong in.
        """
        present = [
            q.timestamps.quote_timestamp
            for q in self.quotes
            if q.timestamps.quote_timestamp is not None
        ]
        return min(present) if present else None

    @property
    def open_interest_as_of(self) -> date | None:
        present = [
            q.timestamps.open_interest_as_of
            for q in self.quotes
            if q.timestamps.open_interest_as_of is not None
        ]
        return min(present) if present else None

    def filter(
        self,
        *,
        roots: tuple[OptionRoot, ...] | None = None,
        max_dte: int | None = None,
    ) -> ChainSnapshot:
        selected = self.quotes
        if roots is not None:
            allowed = set(roots)
            selected = tuple(q for q in selected if q.contract.root in allowed)
        if max_dte is not None:
            cutoff = self.as_of.date()
            selected = tuple(
                q for q in selected if (q.contract.expiry - cutoff).days <= max_dte
            )
        return replace(self, quotes=selected)

    def metadata(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "spot": self.spot,
            "spot_timestamp": (
                self.spot_timestamp.isoformat() if self.spot_timestamp else None
            ),
            "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "source": self.source,
            "quote_count": len(self.quotes),
            "expected_contract_count": self.expected_contract_count,
            "clocks": self.clocks.as_dict(),
        }


__all__ = [
    "INDEX_OPTION_MULTIPLIER",
    "ChainSnapshot",
    "ContractTimestamps",
    "IVQualityFlag",
    "ImpliedVolQuote",
    "OptionContract",
    "OptionQuote",
    "OptionRight",
    "OptionRoot",
    "SnapshotClocks",
]
