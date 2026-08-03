"""Deterministic synthetic SPX/SPXW chains.

Lives in ``src/`` rather than ``tests/`` because it is a production capability:
the demo, the offline integration tests and the replay harness all need to build
a chain without a vendor account. Production code must never import from
``tests/`` (enforced by ``tests/unit/test_architecture.py``), so the generator
belongs here.

The design goal is that **the answers are known in advance**: open interest is
placed at chosen strikes, put weight exceeds call weight so signed GEX is
negative at spot and must cross zero above it, and the smile is calibrated to a
realistic SPX skew so ``sticky_strike`` and ``sticky_moneyness`` cannot collapse
onto each other. A test that merely asserts "the engine returned a number" proves
nothing about a GEX engine.

No randomness anywhere. Replay determinism is a hard requirement, so the
generator holds itself to it too.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from src.domain.completeness import CompletenessStatus
from src.domain.contracts import (
    ChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionRight,
    OptionRoot,
    SnapshotClocks,
)
from src.domain.iv import IVSource, build_iv_quote
from src.domain.model_spec import ModelSpec
from src.domain.timestamps import ContractTimestamps
from src.gex.pricing import BlackScholesInputs
from src.gex.pricing import gamma as bs_gamma
from src.gex.sessions import eastern, market_session_date, seconds_to_expiry

# A Tuesday, mid-session. 2026-03-20 is the third Friday of that month, so the
# fixture naturally contains both an AM-settled SPX standard series and several
# PM-settled SPXW series.
DEFAULT_AS_OF = eastern(2026, 3, 17, 11, 0)
# Fifteen minutes before the PM settlement of the 0DTE series. At this point the
# minimum-time-to-expiry floor actually binds (900s remaining against a 1800s or
# 3600s floor), which is the only regime where floor sensitivity is measurable.
# At DEFAULT_AS_OF there are five hours left and every candidate floor is
# inactive, so a sensitivity test run there would pass without testing anything.
LATE_SESSION_AS_OF = eastern(2026, 3, 17, 15, 45)
DEFAULT_SPOT = 5000.0

# One expiry per DTE bucket, so every bucket in the enum is exercised.
DEFAULT_EXPIRIES: tuple[tuple[OptionRoot, date], ...] = (
    (OptionRoot.SPXW, date(2026, 3, 17)),  # 0 DTE
    (OptionRoot.SPXW, date(2026, 3, 18)),  # 1 DTE
    (OptionRoot.SPX, date(2026, 3, 20)),  # 3 DTE, AM-settled
    (OptionRoot.SPXW, date(2026, 3, 31)),  # 14 DTE
    (OptionRoot.SPXW, date(2026, 5, 15)),  # 59 DTE
)

DEFAULT_STRIKE_STEP = 25.0
DEFAULT_STRIKE_SPAN_PCT = 0.06

# Smile in log-moneyness: sigma(m) = BASE_IV + IV_SKEW*m + IV_CURVATURE*m^2.
# Calibrated to a realistic SPX shape rather than a token tilt -- across the
# +/-6% strike band this gives roughly 24.5% on the put wing, 18% at the money
# and 14.7% on the call wing. Steepness matters: with a nearly-flat smile
# sticky_strike and sticky_moneyness collapse onto the same zero-gamma level, and
# the convention-disagreement machinery would look correct while being untested.
BASE_IV = 0.18
IV_SKEW = -0.8
IV_CURVATURE = 4.0
# Half-width of the synthetic bid/ask IV bracket, in vol points.
IV_HALF_SPREAD = 0.004

CALL_OI_PEAK_STRIKE = 5100.0
PUT_OI_PEAK_STRIKE = 4900.0
CALL_OI_PEAK = 12_000
PUT_OI_PEAK = 30_000  # heavier than calls, so signed GEX starts negative
OI_WIDTH = 60.0

DEFAULT_RISK_FREE_RATE = 0.042
DEFAULT_DIVIDEND_YIELD = 0.013


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
    iv_half_spread: float = IV_HALF_SPREAD
    call_oi_peak_strike: float = CALL_OI_PEAK_STRIKE
    put_oi_peak_strike: float = PUT_OI_PEAK_STRIKE
    call_oi_peak: int = CALL_OI_PEAK
    put_oi_peak: int = PUT_OI_PEAK
    oi_width: float = OI_WIDTH
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE
    dividend_yield: float = DEFAULT_DIVIDEND_YIELD
    # When True the quotes carry a ``gamma`` field. False mirrors a ThetaData
    # Standard subscription: implied_vol present, gamma absent.
    vendor_gamma: bool = True
    open_interest_as_of: date | None = date(2026, 3, 16)
    # Age of the vendor clocks relative to ``as_of``, in seconds. Non-zero values
    # exercise the freshness and skew scoring.
    quote_age_seconds: float = 0.0
    greeks_age_seconds: float = 0.0
    underlying_age_seconds: float = 0.0
    # Strikes to omit entirely, to simulate a vendor coverage hole.
    omit_strikes: tuple[float, ...] = ()
    iv_source: IVSource = IVSource.NBBO_MID_IV

    def model_spec(self) -> ModelSpec:
        return ModelSpec(
            risk_free_rate=self.risk_free_rate,
            dividend_yield=self.dividend_yield,
            iv_price_source=self.iv_source,
        )


def log_moneyness(strike: float, spot: float) -> float:
    return math.log(strike / spot)


def synthetic_iv(strike: float, spec: SyntheticChainSpec) -> float:
    """Skewed, curved smile in log-moneyness."""
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
    # dropped by ``require_open_interest`` and silently shrink the ladder.
    return max(1, round(peak * math.exp(-0.5 * z * z)))


def strikes_for(spec: SyntheticChainSpec) -> tuple[float, ...]:
    half = spec.spot * spec.strike_span_pct
    lowest = math.ceil((spec.spot - half) / spec.strike_step) * spec.strike_step
    highest = math.floor((spec.spot + half) / spec.strike_step) * spec.strike_step
    count = round((highest - lowest) / spec.strike_step) + 1
    omitted = set(spec.omit_strikes)
    return tuple(
        lowest + i * spec.strike_step
        for i in range(count)
        if (lowest + i * spec.strike_step) not in omitted
    )


def build_synthetic_chain(spec: SyntheticChainSpec | None = None) -> ChainSnapshot:
    """Assemble a full ChainSnapshot from a spec."""
    s = spec or SyntheticChainSpec()
    quotes: list[OptionQuote] = []

    quote_ts = s.as_of - timedelta(seconds=s.quote_age_seconds)
    greeks_ts = s.as_of - timedelta(seconds=s.greeks_age_seconds)
    underlying_ts = s.as_of - timedelta(seconds=s.underlying_age_seconds)
    clocks = SnapshotClocks(
        request_started_at=s.as_of - timedelta(milliseconds=250),
        response_received_at=s.as_of,
        normalized_at=s.as_of,
    )

    for root, expiry in s.expiries:
        remaining = seconds_to_expiry(s.as_of, root, expiry)
        if remaining <= 0.0:
            continue
        time_to_expiry = s.model_spec().year_fraction(remaining)
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
                        timestamps=ContractTimestamps(
                            quote_timestamp=quote_ts,
                            greeks_timestamp=greeks_ts,
                            iv_timestamp=greeks_ts,
                            underlying_timestamp=underlying_ts,
                            open_interest_as_of=s.open_interest_as_of,
                            request_started_at=clocks.request_started_at,
                            response_received_at=clocks.response_received_at,
                            normalized_at=clocks.normalized_at,
                        ),
                        bid=round(mid - 0.25, 2),
                        ask=round(mid + 0.25, 2),
                        bid_size=10,
                        ask_size=10,
                        volume=100,
                        open_interest=synthetic_open_interest(strike, right, s),
                        iv=build_iv_quote(
                            bid_iv=iv - s.iv_half_spread,
                            mid_iv=iv,
                            ask_iv=iv + s.iv_half_spread,
                            preferred_source=s.iv_source,
                        ),
                        gamma=gamma_value if s.vendor_gamma else None,
                        underlying_price=s.spot,
                    )
                )

    return ChainSnapshot(
        as_of=s.as_of,
        spot=s.spot,
        quotes=tuple(quotes),
        risk_free_rate=s.risk_free_rate,
        dividend_yield=s.dividend_yield,
        clocks=clocks,
        spot_timestamp=underlying_ts,
        source="synthetic",
        # The generator built this chain, so it knows the universe exactly.
        # This is a genuine independent expectation, not the received count
        # dressed up as one -- which is why it may claim MEASURED_COMPLETE.
        expected_contract_count=len(quotes),
        completeness_status=CompletenessStatus.MEASURED_COMPLETE,
    )


def build_single_contract_chain(
    *,
    spot: float = 5000.0,
    strike: float = 5000.0,
    right: OptionRight = OptionRight.CALL,
    gamma: float | None = 0.001,
    open_interest: int | None = 1000,
    as_of: datetime = DEFAULT_AS_OF,
    expiry: date = date(2026, 3, 31),
    implied_vol: float | None = 0.20,
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
                timestamps=ContractTimestamps(
                    quote_timestamp=as_of,
                    greeks_timestamp=as_of,
                    iv_timestamp=as_of,
                    underlying_timestamp=as_of,
                    open_interest_as_of=market_session_date(as_of),
                    normalized_at=as_of,
                ),
                bid=10.0,
                ask=10.5,
                open_interest=open_interest,
                iv=build_iv_quote(
                    bid_iv=implied_vol,
                    mid_iv=implied_vol,
                    ask_iv=implied_vol,
                    preferred_source=IVSource.NBBO_MID_IV,
                ),
                gamma=gamma,
                underlying_price=spot,
            ),
        ),
        clocks=SnapshotClocks(normalized_at=as_of),
        spot_timestamp=as_of,
        source="synthetic-single",
        expected_contract_count=1,
        completeness_status=CompletenessStatus.MEASURED_COMPLETE,
    )


def with_quote(chain: ChainSnapshot, index: int, **changes: object) -> ChainSnapshot:
    """Return a copy of ``chain`` with one quote's fields replaced.

    Convenience for tests that need to corrupt exactly one record without
    rebuilding the whole chain.
    """
    quotes = list(chain.quotes)
    quotes[index] = replace(quotes[index], **changes)  # type: ignore[arg-type]
    return replace(chain, quotes=tuple(quotes))
