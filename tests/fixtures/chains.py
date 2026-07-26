"""Deterministic synthetic SPX/SPXW chains.

The point of these fixtures is that the *answers are known in advance*. Open
interest is placed at chosen strikes, so the call wall and put wall are known;
put weight exceeds call weight, so signed GEX is negative at spot and must cross
zero somewhere above it. A test that merely asserts "the engine returned a
number" proves nothing about a GEX engine -- these fixtures let the tests assert
where the number should be.

No randomness anywhere. Replay determinism is a hard requirement of the plan, so
the fixtures hold themselves to it too.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

from src.domain.contracts import (
    ChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionRight,
    OptionRoot,
)
from src.gex.pricing import BlackScholesInputs, gamma as bs_gamma, year_fraction
from src.gex.sessions import eastern, seconds_to_expiry

# A Tuesday, mid-session. 2026-03-20 is the third Friday of that month, so the
# fixture naturally contains both an AM-settled SPX standard series and several
# PM-settled SPXW series.
DEFAULT_AS_OF = eastern(2026, 3, 17, 11, 0)
DEFAULT_SPOT = 5000.0

# One expiry per DTE bucket, so every bucket in the enum is exercised.
DEFAULT_EXPIRIES: tuple[tuple[OptionRoot, date], ...] = (
    (OptionRoot.SPXW, date(2026, 3, 17)),  # 0 DTE
    (OptionRoot.SPXW, date(2026, 3, 18)),  # 1 DTE
    (OptionRoot.SPX, date(2026, 3, 20)),  # 3 DTE, AM-settled
    (OptionRoot.SPXW, date(2026, 3, 31)),  # 14 DTE
    (OptionRoot.SPXW, date(2026, 5, 15)),  # 59 DTE
)

# Strike grid: +/- 6% of spot at 25-point intervals, the SPX convention.
DEFAULT_STRIKE_STEP = 25.0
DEFAULT_STRIKE_SPAN_PCT = 0.06

# Smile in log-moneyness: sigma(m) = BASE_IV + IV_SKEW*m + IV_CURVATURE*m^2.
# Calibrated to a realistic SPX shape rather than a token tilt -- across the
# +/-6% strike band this gives roughly 24.5% on the put wing, 18% at the money and
# 14.7% on the call wing. Steepness matters: with a nearly-flat smile
# sticky_strike and sticky_delta collapse onto the same zero-gamma level, and the
# convention-disagreement machinery would look correct while being untested.
BASE_IV = 0.18
IV_SKEW = -0.8  # negative: put wing carries higher IV
IV_CURVATURE = 4.0

# Open interest is a Gaussian bump around a chosen strike. Peak strikes are what
# the wall tests assert against.
CALL_OI_PEAK_STRIKE = 5100.0
PUT_OI_PEAK_STRIKE = 4900.0
CALL_OI_PEAK = 12_000
PUT_OI_PEAK = 30_000  # heavier than calls, so signed GEX starts negative
OI_WIDTH = 60.0


@dataclass(frozen=True, slots=True)
class SyntheticChainSpec:
    as_of: datetime = DEFAULT_AS_OF
    spot: float = DEFAULT_SPOT
    expiries: tuple[tuple[OptionRoot, date], ...] = DEFAULT_EXPIRIES
    strike_step: float = DEFAULT_STRIKE_STEP
    strike_span_pct: float = DEFAULT_STRIKE_SPAN_PCT
    base_iv: float = BASE_IV
    iv_skew: float = IV_SKEW
    iv_curvature: float = IV_CURVATURE
    call_oi_peak_strike: float = CALL_OI_PEAK_STRIKE
    put_oi_peak_strike: float = PUT_OI_PEAK_STRIKE
    call_oi_peak: int = CALL_OI_PEAK
    put_oi_peak: int = PUT_OI_PEAK
    oi_width: float = OI_WIDTH
    risk_free_rate: float = 0.042
    dividend_yield: float = 0.013
    # When True the quotes carry no ``gamma`` field, forcing the engine down the
    # shadow-pricer path -- which is what a ThetaData Standard subscription looks
    # like. See docs/handoff/data-requirements.md.
    vendor_gamma: bool = True
    open_interest_asof: date | None = date(2026, 3, 16)


def log_moneyness(strike: float, spot: float) -> float:
    return math.log(strike / spot)


def synthetic_iv(strike: float, spec: SyntheticChainSpec) -> float:
    """Skewed, curved smile in log-moneyness.

    Non-flat on purpose: with a flat smile ``sticky_strike`` and ``sticky_delta``
    collapse onto the same answer and the convention-disagreement machinery would
    look correct while being untested.
    """
    m = log_moneyness(strike, spec.spot)
    return max(0.05, spec.base_iv + spec.iv_skew * m + spec.iv_curvature * m * m)


def synthetic_open_interest(
    strike: float, right: OptionRight, spec: SyntheticChainSpec
) -> int:
    peak_strike = (
        spec.call_oi_peak_strike
        if right is OptionRight.CALL
        else spec.put_oi_peak_strike
    )
    peak = spec.call_oi_peak if right is OptionRight.CALL else spec.put_oi_peak
    z = (strike - peak_strike) / spec.oi_width
    # Floor of 1 keeps every strike in the chain; a strike with zero OI would be
    # dropped by ``require_open_interest`` and silently shrink the grid.
    return max(1, int(round(peak * math.exp(-0.5 * z * z))))


def strikes_for(spec: SyntheticChainSpec) -> tuple[float, ...]:
    half = spec.spot * spec.strike_span_pct
    lowest = math.ceil((spec.spot - half) / spec.strike_step) * spec.strike_step
    highest = math.floor((spec.spot + half) / spec.strike_step) * spec.strike_step
    count = int(round((highest - lowest) / spec.strike_step)) + 1
    return tuple(lowest + i * spec.strike_step for i in range(count))


def build_synthetic_chain(spec: SyntheticChainSpec | None = None) -> ChainSnapshot:
    """Assemble a full ChainSnapshot from a spec."""
    s = spec or SyntheticChainSpec()
    quotes: list[OptionQuote] = []

    for root, expiry in s.expiries:
        remaining = seconds_to_expiry(s.as_of, root, expiry)
        if remaining <= 0.0:
            continue
        time_to_expiry = year_fraction(remaining)
        for strike in strikes_for(s):
            iv = synthetic_iv(strike, s)
            gamma_value = bs_gamma(
                BlackScholesInputs(
                    spot=s.spot,
                    strike=strike,
                    time_to_expiry=time_to_expiry,
                    implied_vol=iv,
                    rate=s.risk_free_rate,
                    dividend_yield=s.dividend_yield,
                )
            )
            for right in (OptionRight.CALL, OptionRight.PUT):
                # A plausible book rather than a realistic one: wide enough that
                # the mid is never zero, tight enough that nothing looks crossed.
                mid = max(0.05, gamma_value * 5000.0 + 1.0)
                quotes.append(
                    OptionQuote(
                        contract=OptionContract(
                            root=root, expiry=expiry, strike=strike, right=right
                        ),
                        timestamp=s.as_of,
                        bid=round(mid - 0.25, 2),
                        ask=round(mid + 0.25, 2),
                        bid_size=10,
                        ask_size=10,
                        volume=100,
                        open_interest=synthetic_open_interest(strike, right, s),
                        open_interest_asof=s.open_interest_asof,
                        implied_vol=iv,
                        gamma=gamma_value if s.vendor_gamma else None,
                    )
                )

    return ChainSnapshot(
        as_of=s.as_of,
        spot=s.spot,
        quotes=tuple(quotes),
        risk_free_rate=s.risk_free_rate,
        dividend_yield=s.dividend_yield,
        options_feed_timestamp=s.as_of,
        spot_feed_timestamp=s.as_of,
        source="synthetic",
    )


def build_single_contract_chain(
    *,
    spot: float = 5000.0,
    strike: float = 5000.0,
    right: OptionRight = OptionRight.CALL,
    gamma: float = 0.001,
    open_interest: int = 1000,
    as_of: datetime = DEFAULT_AS_OF,
    expiry: date = date(2026, 3, 31),
) -> ChainSnapshot:
    """One contract with a hand-picked gamma, for exact arithmetic assertions.

    With ``gamma=0.001``, ``OI=1000``, ``M=100``, ``S=5000`` and the 1%
    convention the unsigned GEX is exactly
    ``0.001 * 1000 * 100 * 5000 * 50 = 25_000_000``.
    """
    return ChainSnapshot(
        as_of=as_of,
        spot=spot,
        quotes=(
            OptionQuote(
                contract=OptionContract(
                    root=OptionRoot.SPXW, expiry=expiry, strike=strike, right=right
                ),
                timestamp=as_of,
                bid=10.0,
                ask=10.5,
                open_interest=open_interest,
                open_interest_asof=as_of.date(),
                implied_vol=0.20,
                gamma=gamma,
            ),
        ),
        options_feed_timestamp=as_of,
        spot_feed_timestamp=as_of,
        source="synthetic-single",
    )
