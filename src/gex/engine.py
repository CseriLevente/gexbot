"""GEX engine orchestration: ChainSnapshot in, GexSnapshot out.

The only public entry point to the engine. Deliberately a pure function of its
inputs -- no clock reads, no network, no database, no randomness, no dependence
on dict iteration order. That property is what makes replay achievable: the same
``ChainSnapshot`` and config must produce a bit-identical ``GexSnapshot`` forever.

Anything added here that breaks purity breaks the replay test along with it.
"""

from __future__ import annotations

from src.domain.contracts import ChainSnapshot
from src.domain.gex import ExpiryBucket, GexSnapshot, IVConvention
from src.domain.model_spec import (
    SENSITIVITY_FLOORS_MINUTES,
    FloorSensitivityEntry,
    FloorSensitivityReport,
)
from src.gex.confidence import ConfidenceInputs, compute_confidence
from src.gex.config import GexEngineConfig
from src.gex.formulas import (
    ContractGex,
    aggregate_by_bucket,
    aggregate_by_strike,
    bucket_gex_ratio_0dte_vs_rest,
    build_universe,
    compute_contract_gex,
    total_signed_gex,
    total_unsigned_gex,
)
from src.gex.walls import StrikeLadder, extract_walls
from src.gex.zero_gamma import compute_zero_gamma


def compute_gex_snapshot(
    snapshot: ChainSnapshot,
    config: GexEngineConfig | None = None,
    *,
    expected_contract_count: int | None = None,
    flow_adjusted_signed_gex: float | None = None,
) -> GexSnapshot:
    """Run all five GEX views, the universe accounting and the confidence score.

    ``flow_adjusted_signed_gex`` is the hook for the second sign model. Until a
    Cboe Open-Close feed exists it stays ``None``, and ``sign_model_agreement``
    correctly reports that the naive sign is unverified.
    """
    cfg = config or GexEngineConfig()
    warnings: list[str] = []

    result = compute_contract_gex(snapshot, cfg)
    contracts = result.contracts
    if not contracts:
        warnings.append("no usable contracts in snapshot")

    unsigned = total_unsigned_gex(contracts)
    signed = total_signed_gex(contracts)
    buckets = aggregate_by_bucket(contracts)
    strikes = aggregate_by_strike(contracts)
    ladder = StrikeLadder.from_strikes(tuple(s.strike for s in strikes))
    walls = extract_walls(
        strikes, spot=snapshot.spot, config=cfg.walls, full_ladder=ladder
    )

    # --- Universe accounting -------------------------------------------------
    # Two universes, reported separately: the chain totals cover everything that
    # survived validation, while the zero-gamma grid runs on a DTE-capped subset.
    # Comparing the two numbers without knowing that is comparing populations.
    chain_universe = build_universe(
        included=contracts,
        all_contracts=contracts,
        max_dte_used=cfg.max_dte,
        extra_filter_reasons=result.exclusion_counts(),
    )
    grid_contracts = tuple(
        c for c in contracts if c.dte <= cfg.zero_gamma.max_dte_for_grid
    )
    zero_gamma_universe = build_universe(
        included=grid_contracts,
        all_contracts=contracts,
        max_dte_used=cfg.zero_gamma.max_dte_for_grid,
    )
    excluded_share = zero_gamma_universe.excluded_unsigned_gex_share
    if excluded_share is not None and excluded_share > 0.0:
        warnings.append(
            f"zero-gamma grid excludes {excluded_share:.1%} of chain unsigned GEX "
            f"(contracts beyond {cfg.zero_gamma.max_dte_for_grid} DTE); chain "
            "totals and the zero-gamma level describe different universes"
        )

    # --- View 5 --------------------------------------------------------------
    zero_gamma_results = tuple(
        compute_zero_gamma(
            contracts,
            spot=snapshot.spot,
            convention=convention,
            spot_move_pct=cfg.spot_move_pct,
            config=cfg.zero_gamma,
            risk_free_rate=cfg.model_spec.risk_free_rate or snapshot.risk_free_rate,
            dividend_yield=cfg.model_spec.dividend_yield or snapshot.dividend_yield,
        )
        for convention in cfg.zero_gamma.conventions
    )
    warnings.extend(_zero_gamma_warnings(zero_gamma_results))

    if result.exclusions:
        warnings.append(f"contract exclusions: {result.exclusion_counts()}")
    if result.validation.rejected:
        warnings.append(
            f"{result.validation.rejected} of {result.validation.total} records "
            f"failed validation: {dict(sorted((c.value, n) for c, n in result.validation.error_counts.items()))}"
        )
    if contracts and result.vendor_gamma_count and result.shadow_gamma_count:
        warnings.append(
            f"mixed gamma sources: {result.vendor_gamma_count} vendor / "
            f"{result.shadow_gamma_count} shadow-priced -- the zero-gamma grid "
            "always uses the shadow pricer, so the two are not directly comparable"
        )

    dominance = bucket_gex_ratio_0dte_vs_rest(buckets)
    confidence = compute_confidence(
        ConfidenceInputs(
            as_of=snapshot.as_of,
            result=result,
            zero_gamma_results=zero_gamma_results,
            spot=snapshot.spot,
            dte0_dominance_ratio=dominance,
            model_spec=cfg.model_spec,
            limits=cfg.data_quality,
            chain_universe=chain_universe,
            zero_gamma_universe=zero_gamma_universe,
            quotes=snapshot.quotes,
            expected_contract_count=(
                expected_contract_count
                if expected_contract_count is not None
                else snapshot.expected_contract_count
            ),
            options_feed_timestamp=snapshot.options_feed_timestamp,
            spot_feed_timestamp=snapshot.spot_timestamp,
            open_interest_as_of=snapshot.open_interest_as_of,
            naive_signed_gex=signed,
            flow_adjusted_signed_gex=flow_adjusted_signed_gex,
        ),
        cfg.confidence,
    )
    warnings.extend(confidence.warnings)

    return GexSnapshot(
        as_of=snapshot.as_of,
        spot=snapshot.spot,
        source=snapshot.source,
        sign_convention=cfg.sign_convention,
        model_spec=cfg.model_spec,
        total_unsigned_gex=unsigned,
        total_signed_gex=signed,
        buckets=buckets,
        strikes=strikes,
        walls=walls,
        zero_gamma=zero_gamma_results,
        confidence=confidence,
        chain_universe=chain_universe,
        zero_gamma_universe=zero_gamma_universe,
        validation=result.validation,
        contract_count=len(contracts),
        total_open_interest=sum(c.open_interest for c in contracts),
        warnings=tuple(warnings),
        config_fingerprint=cfg.config_fingerprint or cfg.fingerprint(),
        meta={
            "spot_move_pct": cfg.spot_move_pct,
            "prefer_vendor_gamma": cfg.prefer_vendor_gamma,
            "vendor_gamma_count": result.vendor_gamma_count,
            "shadow_gamma_count": result.shadow_gamma_count,
            "dte0_dominance_ratio": dominance,
            "exclusions": result.exclusion_counts(),
            "strike_ladder_modal_spacing": ladder.modal_spacing,
            "model_fingerprint": cfg.model_spec.fingerprint(),
        },
    )


def _zero_gamma_warnings(results: tuple) -> list[str]:  # type: ignore[type-arg]
    warnings: list[str] = []
    for zg in results:
        if zg.unimplemented_reason:
            warnings.append(
                f"zero-gamma convention {zg.convention.value} is not implemented: "
                f"{zg.unimplemented_reason}"
            )
            continue
        if zg.identically_zero_curve:
            warnings.append(
                f"zero-gamma: curve is identically zero under "
                f"{zg.convention.value}; no level exists"
            )
        elif zg.no_root_found:
            warnings.append(
                f"zero-gamma: no sign change inside the grid under "
                f"{zg.convention.value} after {zg.grid_expansions} expansion(s)"
            )
        else:
            if zg.root_count > 1:
                warnings.append(
                    f"zero-gamma: {zg.root_count} crossings under "
                    f"{zg.convention.value} at "
                    f"{[round(r, 2) for r in zg.all_roots]} -- the selected root "
                    "is the nearest to spot by convention, not the only one"
                )
            if zg.root_near_boundary:
                warnings.append(
                    f"zero-gamma: selected root under {zg.convention.value} sits "
                    "near the grid boundary and may be an artefact of the search "
                    "window"
                )
    return warnings


def compute_floor_sensitivity(
    snapshot: ChainSnapshot,
    config: GexEngineConfig | None = None,
    *,
    floors_minutes: tuple[float, ...] = SENSITIVITY_FLOORS_MINUTES,
) -> FloorSensitivityReport:
    """Re-run the engine across several minimum-time-to-expiry floors.

    Gamma diverges as T -> 0, so the floor decides how much of that divergence
    reaches the aggregate. If the 0DTE bucket weight or the zero-gamma level
    swings wildly across these floors, the floor is doing the modelling and the
    aggregate should not be leaned on.

    Reported, never resolved: choosing a floor is a research decision recorded in
    ``docs/OPEN_DECISIONS.md``, not something the engine should decide silently.
    """
    cfg = config or GexEngineConfig()
    entries: list[FloorSensitivityEntry] = []
    for minutes in floors_minutes:
        variant = cfg.with_(model_spec=cfg.model_spec.with_floor_minutes(minutes))
        produced = compute_gex_snapshot(snapshot, variant)
        primary = produced.primary_zero_gamma
        dte0 = produced.bucket(ExpiryBucket.DTE_0)
        entries.append(
            FloorSensitivityEntry(
                floor_minutes=minutes,
                total_unsigned_gex=produced.total_unsigned_gex,
                total_signed_gex=produced.total_signed_gex,
                zero_gamma_spot=primary.selected_root if primary else None,
                dte0_unsigned_gex=dte0.unsigned_gex if dte0 else 0.0,
            )
        )
    return FloorSensitivityReport(
        baseline_floor_minutes=cfg.model_spec.minimum_time_to_expiry_minutes,
        entries=tuple(entries),
    )


def unimplemented_conventions(config: GexEngineConfig) -> tuple[IVConvention, ...]:
    """Conventions requested in config that the engine cannot actually run."""
    return tuple(
        convention
        for convention in config.zero_gamma.conventions
        if not convention.is_implemented
    )


def contracts_for_grid(
    contracts: tuple[ContractGex, ...], *, max_dte: int
) -> tuple[ContractGex, ...]:
    return tuple(c for c in contracts if c.dte <= max_dte)
