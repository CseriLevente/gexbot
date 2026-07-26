"""GEX views 1-4: unsigned concentration, naive signed, expiry buckets, strikes.

The single per-contract quantity everything else is built from:

    GEX_i = gamma_i * OI_i * M * S * dS,    dS = spot_move_pct * S

so with the 1% convention ``GEX_i = gamma_i * OI_i * M * S^2 * 0.01``. Read it
as: dollars of dealer delta that must be re-hedged for a 1% move in spot.

Sign is applied on top of that magnitude and is a *proxy* -- see
:class:`src.domain.gex.SignConvention`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from src.domain.contracts import (
    ChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionRight,
)
from src.domain.gex import BucketGex, ExpiryBucket, SignConvention, StrikeGex
from src.gex.config import BUCKET_BOUNDS, GexEngineConfig
from src.gex.pricing import BlackScholesInputs, gamma as bs_gamma, year_fraction
from src.gex.sessions import calendar_dte, seconds_to_expiry


def bucket_for_dte(dte: int) -> ExpiryBucket:
    """Map calendar DTE to its bucket. Negative DTE is a caller error."""
    if dte < 0:
        raise ValueError(f"expired contract reached bucketing: dte={dte}")
    for bucket, upper in BUCKET_BOUNDS:
        if dte <= upper:
            return bucket
    return ExpiryBucket.DTE_GT_30


def sign_for(right: OptionRight, convention: SignConvention) -> float:
    """Dealer gamma sign under a stated convention.

    ``FLOW_ADJUSTED`` has no static answer -- the sign comes from classified
    flow (Cboe Open-Close), so asking for it here is a programming error.
    """
    if convention is SignConvention.DEALER_LONG_CALLS_SHORT_PUTS:
        return 1.0 if right is OptionRight.CALL else -1.0
    if convention is SignConvention.DEALER_SHORT_CALLS_LONG_PUTS:
        return -1.0 if right is OptionRight.CALL else 1.0
    raise ValueError(
        f"{convention} has no static sign; it requires per-contract classified "
        "flow and must be resolved by the flow-adjusted model"
    )


def notional_gex(
    *,
    gamma: float,
    open_interest: int,
    multiplier: float,
    spot: float,
    spot_move_pct: float,
) -> float:
    """Dollar gamma notional per ``spot_move_pct`` move. Always non-negative."""
    return abs(gamma) * open_interest * multiplier * spot * (spot_move_pct * spot)


class GammaSource(str):
    VENDOR = "vendor"
    SHADOW_PRICER = "shadow_pricer"


@dataclass(frozen=True, slots=True)
class ContractGex:
    """One contract's contribution to every aggregate view."""

    contract: OptionContract
    dte: int
    bucket: ExpiryBucket
    time_to_expiry: float
    gamma: float
    implied_vol: float | None
    open_interest: int
    unsigned_gex: float
    signed_gex: float
    # Dealer sign under the snapshot's convention, stored rather than re-derived.
    # Deriving it from ``signed_gex`` would break for contracts whose gamma
    # rounds to zero at the current spot -- exactly the far-wing strikes that
    # come alive once the zero-gamma grid moves spot toward them.
    sign: float
    gamma_source: str


@dataclass(frozen=True, slots=True)
class ContractGexResult:
    contracts: tuple[ContractGex, ...]
    # Diagnostics feeding chain_completeness / crossed_market_penalty.
    total_quotes: int
    dropped_expired: int
    dropped_no_open_interest: int
    dropped_no_gamma_source: int
    dropped_crossed: int
    vendor_gamma_count: int
    shadow_gamma_count: int

    @property
    def usable_ratio(self) -> float:
        return len(self.contracts) / self.total_quotes if self.total_quotes else 0.0

    @property
    def crossed_ratio(self) -> float:
        return self.dropped_crossed / self.total_quotes if self.total_quotes else 0.0


def _resolve_gamma(
    quote: OptionQuote,
    *,
    snapshot: ChainSnapshot,
    time_to_expiry: float,
    config: GexEngineConfig,
) -> tuple[float, str] | None:
    """Vendor gamma if allowed and present, else Black-Scholes from IV."""
    if config.prefer_vendor_gamma and quote.gamma is not None:
        return quote.gamma, GammaSource.VENDOR
    if quote.implied_vol is not None and quote.implied_vol > 0.0:
        value = bs_gamma(
            BlackScholesInputs(
                spot=snapshot.spot,
                strike=quote.contract.strike,
                time_to_expiry=time_to_expiry,
                implied_vol=quote.implied_vol,
                rate=snapshot.risk_free_rate,
                dividend_yield=snapshot.dividend_yield,
            )
        )
        return value, GammaSource.SHADOW_PRICER
    # Last resort: vendor gamma even though we would have preferred our own.
    if quote.gamma is not None:
        return quote.gamma, GammaSource.VENDOR
    return None


def compute_contract_gex(
    snapshot: ChainSnapshot, config: GexEngineConfig | None = None
) -> ContractGexResult:
    """Explode a chain snapshot into per-contract GEX contributions."""
    cfg = config or GexEngineConfig()
    contracts: list[ContractGex] = []
    dropped_expired = 0
    dropped_no_oi = 0
    dropped_no_gamma = 0
    dropped_crossed = 0
    vendor_count = 0
    shadow_count = 0

    for quote in snapshot.quotes:
        contract = quote.contract
        remaining = seconds_to_expiry(snapshot.as_of, contract.root, contract.expiry)
        if remaining <= 0.0:
            dropped_expired += 1
            continue
        if cfg.drop_crossed_quotes and quote.is_crossed:
            dropped_crossed += 1
            continue
        open_interest = quote.open_interest or 0
        if cfg.require_open_interest and open_interest <= 0:
            dropped_no_oi += 1
            continue

        time_to_expiry = year_fraction(remaining)
        resolved = _resolve_gamma(
            quote, snapshot=snapshot, time_to_expiry=time_to_expiry, config=cfg
        )
        if resolved is None:
            dropped_no_gamma += 1
            continue
        gamma_value, source = resolved
        if source == GammaSource.VENDOR:
            vendor_count += 1
        else:
            shadow_count += 1

        magnitude = notional_gex(
            gamma=gamma_value,
            open_interest=open_interest,
            multiplier=contract.multiplier,
            spot=snapshot.spot,
            spot_move_pct=cfg.spot_move_pct,
        )
        dte = calendar_dte(snapshot.as_of, contract.expiry)
        sign = sign_for(contract.right, cfg.sign_convention)
        contracts.append(
            ContractGex(
                contract=contract,
                dte=dte,
                bucket=bucket_for_dte(dte),
                time_to_expiry=time_to_expiry,
                gamma=gamma_value,
                implied_vol=quote.implied_vol,
                open_interest=open_interest,
                unsigned_gex=magnitude,
                signed_gex=magnitude * sign,
                sign=sign,
                gamma_source=source,
            )
        )

    return ContractGexResult(
        contracts=tuple(contracts),
        total_quotes=len(snapshot.quotes),
        dropped_expired=dropped_expired,
        dropped_no_open_interest=dropped_no_oi,
        dropped_no_gamma_source=dropped_no_gamma,
        dropped_crossed=dropped_crossed,
        vendor_gamma_count=vendor_count,
        shadow_gamma_count=shadow_count,
    )


# --- View 1 & 2: totals ------------------------------------------------------


def total_unsigned_gex(contracts: tuple[ContractGex, ...]) -> float:
    """View 1. Where gamma is concentrated, with no directional claim.

    This is the least model-sensitive view in the engine: it needs no dealer
    positioning assumption at all, which is why the plan makes it mandatory for
    intraday level mapping.
    """
    return sum(c.unsigned_gex for c in contracts)


def total_signed_gex(contracts: tuple[ContractGex, ...]) -> float:
    """View 2. The classic public proxy. Not dealer inventory truth."""
    return sum(c.signed_gex for c in contracts)


# --- View 3: expiry buckets --------------------------------------------------


def aggregate_by_bucket(contracts: tuple[ContractGex, ...]) -> tuple[BucketGex, ...]:
    """View 3. Every bucket is always present, zero-filled when empty.

    Zero-filling matters: a consumer reading ``bucket(DTE_0)`` must be able to
    distinguish "no 0DTE gamma" from "bucket missing from this snapshot".
    """
    unsigned: dict[ExpiryBucket, float] = defaultdict(float)
    signed: dict[ExpiryBucket, float] = defaultdict(float)
    counts: dict[ExpiryBucket, int] = defaultdict(int)
    open_interest: dict[ExpiryBucket, int] = defaultdict(int)

    for c in contracts:
        unsigned[c.bucket] += c.unsigned_gex
        signed[c.bucket] += c.signed_gex
        counts[c.bucket] += 1
        open_interest[c.bucket] += c.open_interest

    return tuple(
        BucketGex(
            bucket=bucket,
            unsigned_gex=unsigned[bucket],
            signed_gex=signed[bucket],
            contract_count=counts[bucket],
            open_interest=open_interest[bucket],
        )
        for bucket in ExpiryBucket
    )


def bucket_gex_ratio_0dte_vs_rest(buckets: tuple[BucketGex, ...]) -> float | None:
    """0DTE unsigned GEX as a share of the whole chain.

    Feature-store field of the same name, and the input to the
    ``0dte_dominance_alert`` confidence component.
    """
    total = sum(b.unsigned_gex for b in buckets)
    if total <= 0.0:
        return None
    zero_dte = next(
        (b.unsigned_gex for b in buckets if b.bucket is ExpiryBucket.DTE_0), 0.0
    )
    return zero_dte / total


# --- View 4: strike level ----------------------------------------------------


def aggregate_by_strike(contracts: tuple[ContractGex, ...]) -> tuple[StrikeGex, ...]:
    """View 4. Sorted ascending by strike; call and put legs kept separate."""
    call_gex: dict[float, float] = defaultdict(float)
    put_gex: dict[float, float] = defaultdict(float)
    signed: dict[float, float] = defaultdict(float)
    call_oi: dict[float, int] = defaultdict(int)
    put_oi: dict[float, int] = defaultdict(int)

    for c in contracts:
        strike = c.contract.strike
        signed[strike] += c.signed_gex
        if c.contract.right is OptionRight.CALL:
            call_gex[strike] += c.unsigned_gex
            call_oi[strike] += c.open_interest
        else:
            put_gex[strike] += c.unsigned_gex
            put_oi[strike] += c.open_interest

    return tuple(
        StrikeGex(
            strike=strike,
            call_gex=call_gex[strike],
            put_gex=put_gex[strike],
            unsigned_gex=call_gex[strike] + put_gex[strike],
            signed_gex=signed[strike],
            call_open_interest=call_oi[strike],
            put_open_interest=put_oi[strike],
        )
        for strike in sorted(set(call_gex) | set(put_gex))
    )


def signed_gex_at_spot(
    contracts: tuple[ContractGex, ...], *, as_of: datetime
) -> float:
    """Convenience wrapper used by tests and the zero-gamma sanity check."""
    del as_of  # signature symmetry with the grid path
    return total_signed_gex(contracts)
