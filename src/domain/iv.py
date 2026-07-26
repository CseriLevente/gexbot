"""Implied volatility with explicit provenance.

A bare field called ``implied_vol`` is ambiguous in a way that changes the answer:
bid IV, mid IV, ask IV and trade IV can differ by several volatility points on a
wide 0DTE wing option, and gamma is a function of whichever one you picked. If
the source is not recorded, a gamma disagreement with a vendor is
uninvestigable -- you cannot tell a model bug from a different IV convention.

So IV is always carried together with where it came from, and when the vendor
gives us the whole book we keep all three legs plus their spread.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any


class IVSource(str, Enum):
    """Which price the volatility was implied from."""

    NBBO_BID_IV = "NBBO_BID_IV"
    NBBO_MID_IV = "NBBO_MID_IV"
    NBBO_ASK_IV = "NBBO_ASK_IV"
    TRADE_IV = "TRADE_IV"
    # The vendor returned an IV without documenting which price it used. Usable,
    # but it is a known unknown and is labelled as one rather than assumed to be
    # mid.
    VENDOR_DEFAULT_IV = "VENDOR_DEFAULT_IV"
    LOCALLY_SOLVED_MID_IV = "LOCALLY_SOLVED_MID_IV"

    @property
    def is_vendor_supplied(self) -> bool:
        return self is not IVSource.LOCALLY_SOLVED_MID_IV


class IVQualityFlag(str, Enum):
    OK = "OK"
    MISSING = "MISSING"
    # Only one side of the book had a usable price, so no spread is measurable.
    SINGLE_SIDED = "SINGLE_SIDED"
    ZERO_BID = "ZERO_BID"
    CROSSED_MARKET = "CROSSED_MARKET"
    WIDE_SPREAD = "WIDE_SPREAD"
    SOLVER_FAILED = "SOLVER_FAILED"
    VENDOR_ERROR = "VENDOR_ERROR"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    # The vendor sent NaN or an infinity. Sanitised to None so it cannot reach
    # the pricer, but recorded here so the validation pass can report it rather
    # than letting a corrupt field vanish and look like "not supplied".
    NON_FINITE_INPUT = "NON_FINITE_INPUT"

    @property
    def is_usable(self) -> bool:
        return self in (
            IVQualityFlag.OK,
            IVQualityFlag.SINGLE_SIDED,
            IVQualityFlag.WIDE_SPREAD,
            IVQualityFlag.ZERO_BID,
        )


# Above this bid/ask IV gap (in absolute vol points) the mid is not a meaningful
# central estimate -- the market is telling us it does not know the price.
# A data-quality threshold, not a market hypothesis.
DEFAULT_WIDE_IV_SPREAD = 0.10


@dataclass(frozen=True, slots=True)
class ImpliedVolQuote:
    """An IV reading plus everything needed to audit it."""

    value: float | None
    source: IVSource
    quality: IVQualityFlag = IVQualityFlag.OK
    bid_iv: float | None = None
    mid_iv: float | None = None
    ask_iv: float | None = None
    # ThetaData returns an ``iv_error`` column alongside its IV: the residual of
    # its own solver. A large residual means the vendor did not converge either.
    vendor_iv_error: float | None = None

    @property
    def iv_spread(self) -> float | None:
        """ask_iv - bid_iv. ``None`` when the book is one-sided."""
        if self.bid_iv is None or self.ask_iv is None:
            return None
        return self.ask_iv - self.bid_iv

    @property
    def is_usable(self) -> bool:
        return (
            self.value is not None
            and math.isfinite(self.value)
            and self.value > 0.0
            and self.quality.is_usable
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source": self.source.value,
            "quality": self.quality.value,
            "bid_iv": self.bid_iv,
            "mid_iv": self.mid_iv,
            "ask_iv": self.ask_iv,
            "iv_spread": self.iv_spread,
            "vendor_iv_error": self.vendor_iv_error,
        }


def missing_iv(quality: IVQualityFlag = IVQualityFlag.MISSING) -> ImpliedVolQuote:
    return ImpliedVolQuote(
        value=None, source=IVSource.VENDOR_DEFAULT_IV, quality=quality
    )


def build_iv_quote(
    *,
    bid_iv: float | None,
    mid_iv: float | None,
    ask_iv: float | None,
    vendor_iv: float | None = None,
    vendor_iv_error: float | None = None,
    preferred_source: IVSource = IVSource.NBBO_MID_IV,
    wide_spread_threshold: float = DEFAULT_WIDE_IV_SPREAD,
    zero_bid: bool = False,
    crossed: bool = False,
) -> ImpliedVolQuote:
    """Assemble an IV reading, choosing a source and flagging its quality.

    Selection order is explicit rather than "whatever is not None": the caller
    states which leg it wants, and the fallbacks are recorded in ``source`` so a
    downstream comparison can tell that this contract used a different basis from
    its neighbour.
    """
    legs: dict[IVSource, float | None] = {
        IVSource.NBBO_BID_IV: bid_iv,
        IVSource.NBBO_MID_IV: mid_iv,
        IVSource.NBBO_ASK_IV: ask_iv,
        IVSource.VENDOR_DEFAULT_IV: vendor_iv,
    }

    saw_non_finite = False

    def finite(value: float | None) -> float | None:
        nonlocal saw_non_finite
        if value is None:
            return None
        if not math.isfinite(value):
            saw_non_finite = True
            return None
        return value

    legs = {source: finite(value) for source, value in legs.items()}

    chosen_source = preferred_source
    value = legs.get(preferred_source)
    if value is None:
        for fallback in (
            IVSource.VENDOR_DEFAULT_IV,
            IVSource.NBBO_MID_IV,
            IVSource.NBBO_ASK_IV,
            IVSource.NBBO_BID_IV,
        ):
            if legs.get(fallback) is not None:
                chosen_source, value = fallback, legs[fallback]
                break

    resolved_vendor_error = finite(vendor_iv_error)
    quality = _classify(
        value=value,
        bid_iv=legs[IVSource.NBBO_BID_IV],
        ask_iv=legs[IVSource.NBBO_ASK_IV],
        vendor_iv_error=resolved_vendor_error,
        wide_spread_threshold=wide_spread_threshold,
        zero_bid=zero_bid,
        crossed=crossed,
        saw_non_finite=saw_non_finite,
    )
    return ImpliedVolQuote(
        value=value,
        source=chosen_source,
        quality=quality,
        bid_iv=legs[IVSource.NBBO_BID_IV],
        mid_iv=legs[IVSource.NBBO_MID_IV],
        ask_iv=legs[IVSource.NBBO_ASK_IV],
        vendor_iv_error=resolved_vendor_error,
    )


def _classify(
    *,
    value: float | None,
    bid_iv: float | None,
    ask_iv: float | None,
    vendor_iv_error: float | None,
    wide_spread_threshold: float,
    zero_bid: bool,
    crossed: bool,
    saw_non_finite: bool = False,
) -> IVQualityFlag:
    # Order matters: the most disqualifying condition wins, so a crossed market
    # is never reported as merely "wide". Non-finite input outranks everything --
    # a corrupt field must not be reported as a merely absent one.
    if saw_non_finite:
        return IVQualityFlag.NON_FINITE_INPUT
    if value is None:
        return IVQualityFlag.MISSING
    if crossed:
        return IVQualityFlag.CROSSED_MARKET
    if value <= 0.0:
        return IVQualityFlag.OUT_OF_RANGE
    if vendor_iv_error is not None and abs(vendor_iv_error) > 0.5:
        return IVQualityFlag.VENDOR_ERROR
    if zero_bid:
        return IVQualityFlag.ZERO_BID
    if bid_iv is None or ask_iv is None:
        return IVQualityFlag.SINGLE_SIDED
    if ask_iv - bid_iv > wide_spread_threshold:
        return IVQualityFlag.WIDE_SPREAD
    return IVQualityFlag.OK


@dataclass(frozen=True, slots=True)
class GammaComparison:
    """Local gamma against vendor gamma, for a validation run.

    Not required for normal operation -- vendor gamma sits behind a higher
    ThetaData tier. When it *is* available this is the structure that turns "the
    numbers look close" into a report that can be sliced by DTE, moneyness, right,
    time of day and IV level.
    """

    local_gamma: float | None
    vendor_gamma: float | None
    # Slice keys, carried so a report can group without re-joining the chain.
    dte: int | None = None
    moneyness: float | None = None
    right: str | None = None
    implied_vol: float | None = None
    observed_at: str | None = None

    @property
    def absolute_difference(self) -> float | None:
        if self.local_gamma is None or self.vendor_gamma is None:
            return None
        return self.local_gamma - self.vendor_gamma

    @property
    def relative_difference(self) -> float | None:
        difference = self.absolute_difference
        if difference is None:
            return None
        scale = max(abs(self.local_gamma or 0.0), abs(self.vendor_gamma or 0.0))
        return difference / scale if scale > 0.0 else None

    @property
    def comparison_status(self) -> str:
        """``unavailable`` / ``match`` / ``minor_difference`` / ``mismatch``.

        Thresholds here describe numerical agreement between two implementations
        of the same closed-form formula, not a market claim -- 0.1% is roughly
        the rounding a vendor's own CSV output introduces.
        """
        relative = self.relative_difference
        if relative is None:
            return "unavailable"
        magnitude = abs(relative)
        if magnitude <= 1e-3:
            return "match"
        if magnitude <= 1e-2:
            return "minor_difference"
        return "mismatch"

    def as_dict(self) -> dict[str, Any]:
        return {
            "local_gamma": self.local_gamma,
            "vendor_gamma": self.vendor_gamma,
            "absolute_difference": self.absolute_difference,
            "relative_difference": self.relative_difference,
            "comparison_status": self.comparison_status,
            "dte": self.dte,
            "moneyness": self.moneyness,
            "right": self.right,
            "implied_vol": self.implied_vol,
            "observed_at": self.observed_at,
        }
