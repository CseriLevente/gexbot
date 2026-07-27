"""GEX engine orchestration: ChainSnapshot in, GexSnapshot out.

The only public entry point to the engine. Deliberately a pure function of its
inputs -- no clock reads, no network, no database, no randomness, no dependence
on dict iteration order. That property is what makes replay achievable: the same
``ChainSnapshot`` and config must produce a bit-identical ``GexSnapshot`` forever.

Anything added here that breaks purity breaks the replay test along with it.
"""

from __future__ import annotations

from collections import Counter

from src.domain.completeness import CompletenessStatus
from src.domain.contracts import ChainSnapshot
from src.domain.gex import ExpiryBucket, GexSnapshot, IVConvention
from src.domain.model_spec import (
    SENSITIVITY_FLOORS_MINUTES,
    FloorSensitivityEntry,
    FloorSensitivityReport,
)
from src.gex.confidence import (
    ConfidenceInputs,
    compare_root_topology,
    compute_confidence,
)
from src.gex.config import GexEngineConfig
from src.gex.formulas import (
    ContractGex,
    MixedModelError,
    aggregate_by_bucket,
    aggregate_by_strike,
    bucket_gex_ratio_0dte_vs_rest,
    build_model_completeness,
    build_model_distribution,
    build_universe,
    compute_contract_gex,
    total_signed_gex,
    total_unsigned_gex,
    zero_gamma_eligible,
)
from src.gex.walls import StrikeLadder, extract_walls
from src.gex.zero_gamma import compute_zero_gamma


def _selected_source_counts(snapshot: ChainSnapshot) -> dict[str, dict[str, int]]:
    """How many contracts took each clock from each source."""
    counts: dict[str, Counter[str]] = {}
    for quote in snapshot.quotes:
        for role, detail in quote.timestamps.selected_sources().items():
            label = f"{detail.get('source')}"
            if detail.get("localization_applied"):
                label += "+assumed_tz"
            counts.setdefault(role, Counter())[label] += 1
    return {role: dict(sorted(c.items())) for role, c in sorted(counts.items())}


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

    # The one effective model, taken from a resolved contract. Every contract in
    # a snapshot shares the same model-level assumptions (only spot, strike, IV
    # and expiry vary), so any of them serialises the same canonical record --
    # which is exactly why the fingerprint drops the per-contract fields.
    effective_model = (
        {
            **contracts[0].effective.as_dict(),
            "effective_model_fingerprint": contracts[0].effective.fingerprint(),
            "description": contracts[0].effective.describe(),
        }
        if contracts
        else None
    )
    if contracts and not contracts[0].effective.is_fully_specified:
        warnings.append(
            "effective model is not fully specified: "
            f"{list(contracts[0].effective.missing_inputs)}"
        )

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
    distribution = build_model_distribution(result.contracts)
    if cfg.require_uniform_effective_model and distribution.mixed_effective_models:
        raise MixedModelError(
            "chain is not priced under a uniform effective model: "
            f"{distribution.effective_model_fingerprint_counts}. A single "
            "aggregate over several models is a number with no stated meaning. "
            "Set require_uniform_effective_model=False to allow mixed-model "
            "research, which reports the distribution and marks the snapshot "
            "uncalibrated."
        )
    model_completeness = build_model_completeness(cfg.model_spec, result)

    chain_universe = build_universe(
        included=contracts,
        all_contracts=contracts,
        max_dte_used=cfg.max_dte,
        extra_filter_reasons=result.exclusion_counts(),
    )
    # Eligibility, not just a DTE filter: a contract carried on vendor gamma
    # with no IV contributes to current GEX but cannot be repriced on the grid.
    grid_contracts, exclusion_reasons = zero_gamma_eligible(
        contracts, max_dte=cfg.zero_gamma.max_dte_for_grid
    )
    zero_gamma_universe = build_universe(
        included=grid_contracts,
        all_contracts=contracts,
        max_dte_used=cfg.zero_gamma.max_dte_for_grid,
        extra_filter_reasons=exclusion_reasons,
    )
    excluded_share = zero_gamma_universe.excluded_unsigned_gex_share
    if excluded_share is not None and excluded_share > 0.0:
        warnings.append(
            f"zero-gamma grid excludes {excluded_share:.1%} of chain unsigned GEX "
            f"({zero_gamma_universe.excluded_contract_count} contracts; reasons "
            f"{exclusion_reasons}); chain totals and the zero-gamma level "
            "describe different universes"
        )

    # --- View 5 --------------------------------------------------------------
    zero_gamma_results = tuple(
        compute_zero_gamma(
            grid_contracts,
            spot=snapshot.spot,
            convention=convention,
            spot_move_pct=cfg.spot_move_pct,
            config=cfg.zero_gamma,
        )
        for convention in cfg.zero_gamma.conventions
    )
    warnings.extend(_zero_gamma_warnings(zero_gamma_results))
    root_topology = compare_root_topology(
        zero_gamma_results,
        spot=snapshot.spot,
        tolerance_pct=cfg.confidence.root_match_tolerance_pct,
    )
    if root_topology["comparable"] and not root_topology["root_topology_stable"]:
        warnings.append(
            "zero-gamma root topology is unstable across IV conventions: "
            f"{root_topology['unmatched_root_count']} unmatched root(s), "
            f"worst matched shift "
            f"{root_topology['maximum_matched_root_shift_pct']:.4f}% of spot"
        )

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
            # A caller who overrides the count is asserting an independent
            # universe; without one, the snapshot's own status governs, and
            # UNKNOWN must reach the scorer rather than being smoothed away.
            model_distribution=distribution,
            model_completeness=model_completeness,
            completeness_status=(
                CompletenessStatus.MEASURED_COMPLETE
                if expected_contract_count is not None
                and expected_contract_count <= len(result.contracts)
                else CompletenessStatus.MEASURED_INCOMPLETE
                if expected_contract_count is not None
                else snapshot.completeness_status
            ),
            options_feed_timestamp=snapshot.options_feed_timestamp,
            spot_feed_timestamp=snapshot.spot_timestamp,
            open_interest_as_of=snapshot.open_interest_as_of,
            latest_open_interest_as_of=snapshot.latest_open_interest_as_of,
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
        effective_model=effective_model,
        root_topology=root_topology,
        config_fingerprint=(
            cfg.config_fingerprint
            if cfg.config_fingerprint is not None
            else cfg.fingerprint()
        ),
        meta={
            # What models ACTUALLY priced this chain, rather than what was
            # configured. A chain with per-contract IV fallback has more than
            # one, and v2.1.1 reported whichever came first.
            "model_distribution": distribution.as_dict(),
            "model_completeness": model_completeness.as_dict(),
            "engine_version": cfg.model_spec.model_version,
            # Per-contract provenance, aggregated by role. Counts rather than a
            # per-contract transcript: an SPX chain has tens of thousands of
            # contracts and what a reader needs is "which source, how often".
            "selected_timestamp_sources": _selected_source_counts(snapshot),
            # Provenance the adapter established travels with the snapshot: a
            # replay that cannot see which parser read the bytes, or which
            # source needed a timezone assumed, cannot detect a change in
            # either.
            **{
                key: snapshot.meta[key]
                for key in (
                    "parser_version",
                    "timestamp_localization",
                    "chain_completeness",
                )
                if key in snapshot.meta
            },
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
