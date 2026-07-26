"""GEX engine orchestration: ChainSnapshot in, GexSnapshot out.

This is the only public entry point to the engine. It is deliberately a pure
function of its inputs -- no clock reads, no network, no database. That property
is what makes the replay requirement achievable: the same ``ChainSnapshot`` must
produce a bit-identical ``GexSnapshot`` forever.
"""

from __future__ import annotations

from src.domain.contracts import ChainSnapshot
from src.domain.gex import GexSnapshot, IVConvention
from src.gex.confidence import ConfidenceInputs, compute_confidence
from src.gex.config import GexEngineConfig
from src.gex.formulas import (
    aggregate_by_bucket,
    aggregate_by_strike,
    bucket_gex_ratio_0dte_vs_rest,
    compute_contract_gex,
    total_signed_gex,
    total_unsigned_gex,
)
from src.gex.walls import extract_walls
from src.gex.zero_gamma import compute_zero_gamma


def compute_gex_snapshot(
    snapshot: ChainSnapshot,
    config: GexEngineConfig | None = None,
    *,
    expected_contract_count: int | None = None,
    flow_adjusted_signed_gex: float | None = None,
) -> GexSnapshot:
    """Run all five GEX views plus the confidence score.

    ``flow_adjusted_signed_gex`` is the hook for the second sign model. Until a
    Cboe Open-Close feed exists it stays ``None``, and ``sign_model_agreement``
    correctly reports that the naive sign is unverified.
    """
    cfg = config or GexEngineConfig()
    warnings: list[str] = []

    result = compute_contract_gex(snapshot, cfg)
    if not result.contracts:
        warnings.append("no usable contracts in snapshot")

    contracts = result.contracts
    unsigned = total_unsigned_gex(contracts)
    signed = total_signed_gex(contracts)
    buckets = aggregate_by_bucket(contracts)
    strikes = aggregate_by_strike(contracts)
    walls = extract_walls(strikes, spot=snapshot.spot, config=cfg.walls)

    zero_gamma_results = tuple(
        compute_zero_gamma(
            contracts,
            spot=snapshot.spot,
            convention=convention,
            spot_move_pct=cfg.spot_move_pct,
            config=cfg.zero_gamma,
            risk_free_rate=snapshot.risk_free_rate,
            dividend_yield=snapshot.dividend_yield,
        )
        for convention in cfg.zero_gamma.conventions
    )
    for zg in zero_gamma_results:
        if zg.no_crossing:
            warnings.append(
                f"zero-gamma: no sign change inside the grid under {zg.convention.value}"
            )
        elif zg.sign_changes > 1:
            warnings.append(
                f"zero-gamma: {zg.sign_changes} crossings under "
                f"{zg.convention.value} -- level is ambiguous"
            )
    if IVConvention.SURFACE_REFIT in cfg.zero_gamma.conventions:
        warnings.append("surface_refit convention is not implemented in v1")

    if result.dropped_expired:
        warnings.append(
            f"{result.dropped_expired} expired contract(s) excluded -- check that "
            "the snapshot as_of and the settlement clock agree"
        )
    if contracts and result.vendor_gamma_count and result.shadow_gamma_count:
        warnings.append(
            f"mixed gamma sources: {result.vendor_gamma_count} vendor / "
            f"{result.shadow_gamma_count} shadow-priced -- zero-gamma grid always "
            "uses the shadow pricer, so the two are not directly comparable"
        )

    dominance = bucket_gex_ratio_0dte_vs_rest(buckets)
    confidence = compute_confidence(
        ConfidenceInputs(
            as_of=snapshot.as_of,
            result=result,
            zero_gamma_results=zero_gamma_results,
            spot=snapshot.spot,
            dte0_dominance_ratio=dominance,
            expected_contract_count=expected_contract_count,
            options_feed_timestamp=snapshot.options_feed_timestamp,
            spot_feed_timestamp=snapshot.spot_feed_timestamp,
            open_interest_asof=_open_interest_asof(snapshot),
            naive_signed_gex=signed,
            flow_adjusted_signed_gex=flow_adjusted_signed_gex,
        ),
        cfg.confidence,
    )

    return GexSnapshot(
        as_of=snapshot.as_of,
        spot=snapshot.spot,
        source=snapshot.source,
        sign_convention=cfg.sign_convention,
        total_unsigned_gex=unsigned,
        total_signed_gex=signed,
        buckets=buckets,
        strikes=strikes,
        walls=walls,
        zero_gamma=zero_gamma_results,
        confidence=confidence,
        contract_count=len(contracts),
        total_open_interest=sum(c.open_interest for c in contracts),
        warnings=tuple(warnings),
        meta={
            "spot_move_pct": cfg.spot_move_pct,
            "prefer_vendor_gamma": cfg.prefer_vendor_gamma,
            "vendor_gamma_count": result.vendor_gamma_count,
            "shadow_gamma_count": result.shadow_gamma_count,
            "dropped_crossed": result.dropped_crossed,
            "dropped_no_open_interest": result.dropped_no_open_interest,
            "dropped_no_gamma_source": result.dropped_no_gamma_source,
            "dte0_dominance_ratio": dominance,
        },
    )


def _open_interest_asof(snapshot: ChainSnapshot):
    """Oldest OI as-of date in the chain -- the honest age of the OI layer.

    Taking the oldest rather than the newest means a partially-refreshed OI table
    reports as stale, which is the safe direction to be wrong in.
    """
    dates = [
        q.open_interest_asof for q in snapshot.quotes if q.open_interest_asof is not None
    ]
    return min(dates) if dates else None
