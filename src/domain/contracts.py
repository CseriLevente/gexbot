"""Option chain and reference-data value objects.

These types are the boundary between vendor adapters and the GEX engine. An
adapter's only job is to produce a :class:`ChainSnapshot`; the engine never sees
a vendor payload.

Everything here is immutable and dependency-free on purpose -- the engine core
must be runnable and testable without numpy, a database, or network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

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
        return (self.root.value, self.expiry, self.strike, self.right.value)


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """Point-in-time market state for one contract.

    ``gamma`` is optional. When a vendor supplies it we can use it directly; when
    it is absent (e.g. ThetaData Standard tier, which exposes ``implied_vol`` but
    not second-order greeks) the engine derives it from ``implied_vol`` with the
    shadow pricer. See ``docs/handoff/data-requirements.md``.
    """

    contract: OptionContract
    timestamp: datetime
    bid: float | None = None
    ask: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    last: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    # As-of date of the open interest figure. OI is derived from the *previous*
    # day's settlement, so intraday this is always stale by construction.
    open_interest_asof: date | None = None
    implied_vol: float | None = None
    delta: float | None = None
    gamma: float | None = None
    vega: float | None = None
    theta: float | None = None

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
    def is_quotable(self) -> bool:
        return self.mid is not None and not self.is_crossed


@dataclass(frozen=True, slots=True)
class ChainSnapshot:
    """A frozen option chain plus the reference data needed to price it.

    ``as_of`` is the single logical timestamp of the snapshot. Every downstream
    calculation is stamped with it, which is what makes point-in-time replay
    possible: the same ``ChainSnapshot`` must always produce the same
    ``GexSnapshot``.
    """

    as_of: datetime
    spot: float
    quotes: tuple[OptionQuote, ...]
    # Continuously-compounded risk-free rate and dividend yield used by the
    # shadow pricer. SPX carries a material dividend yield, so q != 0.
    risk_free_rate: float = 0.0
    dividend_yield: float = 0.0
    # Vendor timestamps, kept separately so feed drift is measurable rather than
    # silently absorbed into ``as_of``.
    options_feed_timestamp: datetime | None = None
    spot_feed_timestamp: datetime | None = None
    source: str = "unknown"
    meta: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.spot <= 0.0:
            raise ValueError(f"spot must be positive, got {self.spot}")

    @property
    def expiries(self) -> tuple[date, ...]:
        return tuple(sorted({q.contract.expiry for q in self.quotes}))

    @property
    def strikes(self) -> tuple[float, ...]:
        return tuple(sorted({q.contract.strike for q in self.quotes}))

    def filter(
        self,
        *,
        roots: tuple[OptionRoot, ...] | None = None,
        max_dte: int | None = None,
    ) -> "ChainSnapshot":
        selected = self.quotes
        if roots is not None:
            allowed = set(roots)
            selected = tuple(q for q in selected if q.contract.root in allowed)
        if max_dte is not None:
            cutoff = self.as_of.date()
            selected = tuple(
                q for q in selected if (q.contract.expiry - cutoff).days <= max_dte
            )
        return ChainSnapshot(
            as_of=self.as_of,
            spot=self.spot,
            quotes=selected,
            risk_free_rate=self.risk_free_rate,
            dividend_yield=self.dividend_yield,
            options_feed_timestamp=self.options_feed_timestamp,
            spot_feed_timestamp=self.spot_feed_timestamp,
            source=self.source,
            meta=dict(self.meta),
        )
