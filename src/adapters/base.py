"""Adapter protocols.

The engine depends on these shapes, never on a vendor SDK. Two consequences:

* The GEX engine is testable with no subscription, no network and no keys -- see
  ``src/adapters/fixtures``.
* Swapping ThetaData for Cboe DataShop, or Databento for IBKR, is a new adapter
  rather than a change to the engine.

Every adapter must attach timezone-aware timestamps. A naive datetime crossing
this boundary is a bug: the engine cannot tell whether it was ET or UTC, and on
0DTE that difference is the whole answer.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from src.domain.contracts import ChainSnapshot, OptionRoot


@runtime_checkable
class OptionsDataSource(Protocol):
    """Produces option chain snapshots for the GEX engine.

    Implementations must return the *whole requested chain* or say what is
    missing via ``expected_contract_count``, because silently returning fewer
    strikes looks identical to a market with fewer strikes.
    """

    name: str

    def fetch_chain(
        self,
        *,
        as_of: datetime,
        roots: tuple[OptionRoot, ...],
        max_dte: int | None = None,
    ) -> ChainSnapshot:
        """Snapshot of the chain as it was at ``as_of``.

        For live use ``as_of`` is "now". For point-in-time backtesting it is a
        historical instant, and the implementation must return only what was
        knowable then -- no later corrections, no same-day settlement OI.
        """
        ...

    def expected_contract_count(
        self,
        *,
        as_of: datetime,
        roots: tuple[OptionRoot, ...],
        max_dte: int | None = None,
    ) -> int | None:
        """How many contracts the chain *should* contain, if knowable.

        Feeds ``chain_completeness``. Returning ``None`` is acceptable and makes
        the confidence component fall back to the usable/received ratio.
        """
        ...


@runtime_checkable
class FuturesDataSource(Protocol):
    """Futures bars and contract metadata for execution and features."""

    name: str

    def fetch_bars(
        self,
        *,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str = "1m",
    ) -> tuple[tuple[datetime, float, float, float, float, int], ...]:
        """``(timestamp, open, high, low, close, volume)`` tuples, ascending."""
        ...

    def front_contract(self, *, symbol: str, as_of: date) -> str:
        """The lead contract as of a date.

        Must decide using information available on ``as_of`` only. Resolving the
        roll with hindsight ("whichever contract turned out to be most liquid")
        is the classic look-ahead leak in futures backtests.
        """
        ...


@runtime_checkable
class BrokerAdapter(Protocol):
    """Order placement. Deliberately minimal for v1.

    Note what is absent: there is no ``place_order`` that a strategy could reach.
    The plan requires that strategies never send orders directly, so the risk
    engine is the only intended caller of any implementation of this protocol.
    """

    name: str
    is_paper: bool

    def is_connected(self) -> bool: ...

    def positions(self) -> tuple[tuple[str, int, float], ...]:
        """``(symbol, signed_quantity, average_price)`` as the broker sees it.

        Used by reconciliation. The broker's view is authoritative; local state
        disagreeing with it is an incident, not a rounding difference.
        """
        ...
