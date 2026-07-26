"""Black-Scholes shadow pricing engine.

Two jobs:

1. Derive ``gamma`` when the vendor does not supply it. On ThetaData, ``gamma``
   is a second-order greek and sits behind the Pro tier, while ``implied_vol``
   is available one tier down -- so computing gamma ourselves from IV is both
   cheaper and, more importantly, *consistent* with (2).
2. Reprice gamma on the zero-gamma spot grid. The grid needs gamma at
   hypothetical spot levels that no vendor will ever quote, so this code path is
   mandatory regardless of subscription tier.

Because (1) and (2) share one implementation, the gamma at the current spot and
the gamma on the grid are produced by the same model. Mixing a vendor gamma at
spot with an in-house gamma on the grid would put a discontinuity right at the
point the root-finder cares about most.

Pure stdlib: no numpy, no scipy. The normal CDF uses ``math.erf``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.domain.contracts import OptionRight
from src.domain.model_spec import (
    FLOOR_30_MINUTES,
    FLOOR_60_MINUTES,
    DayCountConvention,
    ModelSpec,
)

# Retained for readability in formulas below. The *authoritative* day count comes
# from :class:`~src.domain.model_spec.ModelSpec`, which can select ACT/360 or
# ACT/252; these constants are only the ACT/365F case.
DAYS_PER_YEAR = DayCountConvention.ACT_365_FIXED.days_per_year
SECONDS_PER_YEAR = DayCountConvention.ACT_365_FIXED.seconds_per_year

# Gamma diverges as T -> 0 for an at-the-money option. On expiration day that
# singularity is real, not a bug -- but it makes aggregate GEX explode and the
# zero-gamma root-finder unstable, so time-to-expiry is floored.
#
# The floor is a MODEL PARAMETER, not a constant: it lives on ModelSpec, is
# configurable, travels in the snapshot fingerprint, and the engine reports
# sensitivity across several values. These two named levels exist so the
# alternatives can be referred to by name.
#
# NOTE: the default is 60 minutes. This has NOT been verified to match
# ThetaData's short-dated handling -- see docs/OPEN_DECISIONS.md. Do not describe
# the engine as vendor-compatible on this point.
MIN_TIME_TO_EXPIRY_YEARS_30M = FLOOR_30_MINUTES * 60.0 / SECONDS_PER_YEAR
MIN_TIME_TO_EXPIRY_YEARS_60M = FLOOR_60_MINUTES * 60.0 / SECONDS_PER_YEAR
DEFAULT_MIN_TIME_TO_EXPIRY_YEARS = MIN_TIME_TO_EXPIRY_YEARS_60M

# Below this vol the formulas are numerically meaningless.
MIN_IMPLIED_VOL = 1e-4
MAX_IMPLIED_VOL = 5.0


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def year_fraction(
    seconds_to_expiry: float,
    *,
    floor: float | None = None,
    spec: ModelSpec | None = None,
) -> float:
    """Convert seconds-to-expiry into a floored year fraction.

    Precedence is explicit: ``spec`` wins, then ``floor``, then the documented
    default. Callers inside the engine always pass a ``spec`` so the day count
    and the floor cannot diverge from what the snapshot reports. Pass
    ``floor=0.0`` to disable flooring, which is only useful in tests that probe
    the singularity directly.
    """
    if spec is not None:
        return spec.year_fraction(seconds_to_expiry)
    if floor is None:
        floor = DEFAULT_MIN_TIME_TO_EXPIRY_YEARS
    return max(seconds_to_expiry / SECONDS_PER_YEAR, floor)


@dataclass(frozen=True, slots=True)
class BlackScholesInputs:
    spot: float
    strike: float
    time_to_expiry: float  # in years, already floored
    implied_vol: float
    rate: float = 0.0
    dividend_yield: float = 0.0

    def is_degenerate(self) -> bool:
        return (
            self.spot <= 0.0
            or self.strike <= 0.0
            or self.time_to_expiry <= 0.0
            or self.implied_vol < MIN_IMPLIED_VOL
        )


def _d1_d2(inputs: BlackScholesInputs) -> tuple[float, float]:
    sigma_sqrt_t = inputs.implied_vol * math.sqrt(inputs.time_to_expiry)
    d1 = (
        math.log(inputs.spot / inputs.strike)
        + (inputs.rate - inputs.dividend_yield + 0.5 * inputs.implied_vol**2)
        * inputs.time_to_expiry
    ) / sigma_sqrt_t
    return d1, d1 - sigma_sqrt_t


def gamma(inputs: BlackScholesInputs) -> float:
    """d(delta)/d(spot). Identical for calls and puts at the same strike/expiry.

    That identity is why the zero-gamma grid can reprice a whole chain cheaply:
    only (strike, expiry, sigma) matter, not the right.
    """
    if inputs.is_degenerate():
        return 0.0
    d1, _ = _d1_d2(inputs)
    return (
        math.exp(-inputs.dividend_yield * inputs.time_to_expiry)
        * norm_pdf(d1)
        / (inputs.spot * inputs.implied_vol * math.sqrt(inputs.time_to_expiry))
    )


def delta(inputs: BlackScholesInputs, right: OptionRight) -> float:
    if inputs.is_degenerate():
        # Deep in-the-money at expiry still has unit delta; report the intrinsic
        # limit rather than zero.
        intrinsic_call = 1.0 if inputs.spot > inputs.strike else 0.0
        return intrinsic_call if right is OptionRight.CALL else intrinsic_call - 1.0
    d1, _ = _d1_d2(inputs)
    discount = math.exp(-inputs.dividend_yield * inputs.time_to_expiry)
    if right is OptionRight.CALL:
        return discount * norm_cdf(d1)
    return -discount * norm_cdf(-d1)


def vega(inputs: BlackScholesInputs) -> float:
    """dPrice/dSigma, per 1.0 (=100 vol points) change in sigma."""
    if inputs.is_degenerate():
        return 0.0
    d1, _ = _d1_d2(inputs)
    return (
        inputs.spot
        * math.exp(-inputs.dividend_yield * inputs.time_to_expiry)
        * norm_pdf(d1)
        * math.sqrt(inputs.time_to_expiry)
    )


def price(inputs: BlackScholesInputs, right: OptionRight) -> float:
    if inputs.is_degenerate():
        if right is OptionRight.CALL:
            return max(inputs.spot - inputs.strike, 0.0)
        return max(inputs.strike - inputs.spot, 0.0)
    d1, d2 = _d1_d2(inputs)
    spot_discounted = inputs.spot * math.exp(
        -inputs.dividend_yield * inputs.time_to_expiry
    )
    strike_discounted = inputs.strike * math.exp(-inputs.rate * inputs.time_to_expiry)
    if right is OptionRight.CALL:
        return spot_discounted * norm_cdf(d1) - strike_discounted * norm_cdf(d2)
    return strike_discounted * norm_cdf(-d2) - spot_discounted * norm_cdf(-d1)


def implied_vol_from_price(
    target_price: float,
    *,
    spot: float,
    strike: float,
    time_to_expiry: float,
    right: OptionRight,
    rate: float = 0.0,
    dividend_yield: float = 0.0,
    tolerance: float = 1e-7,
    max_iterations: int = 100,
) -> float | None:
    """Invert Black-Scholes for sigma. Returns ``None`` when no solution exists.

    Newton with a vega step, falling back to bisection when vega collapses (deep
    wings, near-expiry). The bracket is [MIN_IMPLIED_VOL, MAX_IMPLIED_VOL]; a
    target outside the reachable price range returns ``None`` rather than a
    clamped lie, so callers can drop the contract and register the miss in
    ``chain_completeness``.
    """
    if target_price <= 0.0 or spot <= 0.0 or strike <= 0.0 or time_to_expiry <= 0.0:
        return None

    def price_at(sigma: float) -> float:
        return price(
            BlackScholesInputs(
                spot=spot,
                strike=strike,
                time_to_expiry=time_to_expiry,
                implied_vol=sigma,
                rate=rate,
                dividend_yield=dividend_yield,
            ),
            right,
        )

    low, high = MIN_IMPLIED_VOL, MAX_IMPLIED_VOL
    price_low, price_high = price_at(low), price_at(high)
    if not (price_low - tolerance <= target_price <= price_high + tolerance):
        return None

    sigma = 0.20
    for _ in range(max_iterations):
        current = price_at(sigma)
        diff = current - target_price
        if abs(diff) < tolerance:
            return sigma
        if diff > 0.0:
            high = sigma
        else:
            low = sigma
        v = vega(
            BlackScholesInputs(
                spot=spot,
                strike=strike,
                time_to_expiry=time_to_expiry,
                implied_vol=sigma,
                rate=rate,
                dividend_yield=dividend_yield,
            )
        )
        if v < 1e-8:
            sigma = 0.5 * (low + high)
            continue
        step = diff / v
        candidate = sigma - step
        # Keep Newton inside the live bracket; otherwise bisect.
        sigma = candidate if low < candidate < high else 0.5 * (low + high)

    return sigma if abs(price_at(sigma) - target_price) < 1e-4 else None
