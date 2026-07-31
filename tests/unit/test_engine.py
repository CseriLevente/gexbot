"""End-to-end: ChainSnapshot in, GexSnapshot out.

The critical property is replay determinism -- the same input must produce a
bit-identical output. Everything else in the validation layer rests on it.

The second theme is provenance: every number the snapshot reports must carry
enough metadata to be interpretable later. A gamma total without its model spec,
or a zero-gamma level without its option universe, is a number nobody can audit.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.domain.contracts import OptionRoot
from src.domain.gex import (
    ExpiryBucket,
    IVConvention,
    RootSelectionMethod,
    SignConvention,
)
from src.domain.iv import IVSource
from src.domain.model_spec import (
    SENSITIVITY_FLOORS_MINUTES,
    DayCountConvention,
    ModelSpec,
)
from src.gex.config import GexEngineConfig, ZeroGammaConfig
from src.gex.engine import (
    compute_floor_sensitivity,
    compute_gex_snapshot,
    unimplemented_conventions,
)
from src.gex.walls import distance_pct
from src.synthetic.chains import (
    LATE_SESSION_AS_OF,
    SyntheticChainSpec,
    build_single_contract_chain,
    build_synthetic_chain,
    with_quote,
)

# --- All five views ---------------------------------------------------------


def test_snapshot_carries_all_five_mandatory_views(snapshot):
    assert snapshot.total_unsigned_gex > 0.0  # view 1
    assert snapshot.total_signed_gex != 0.0  # view 2
    assert len(snapshot.buckets) == len(ExpiryBucket)  # view 3
    assert len(snapshot.strikes) > 0  # view 4
    assert len(snapshot.zero_gamma) == 3  # view 5
    assert snapshot.confidence.value >= 0.0


def test_snapshot_records_the_sign_convention_it_used(snapshot):
    assert snapshot.sign_convention is SignConvention.DEALER_LONG_CALLS_SHORT_PUTS


def test_unsigned_total_dominates_signed_total(snapshot):
    assert abs(snapshot.total_signed_gex) <= snapshot.total_unsigned_gex


# --- Model specification ----------------------------------------------------


def test_snapshot_embeds_the_full_model_spec(snapshot):
    """A gamma number is only interpretable with the conventions that made it."""
    spec = snapshot.model_spec
    payload = snapshot.as_dict()["model_spec"]
    for key in (
        "pricing_model",
        "day_count_convention",
        "risk_free_rate_source",
        "dividend_yield_source",
        "expiration_timestamp_rule",
        "minimum_time_to_expiry_minutes",
        "underlying_price_source",
        "iv_price_source",
        "model_version",
    ):
        assert key in payload, key
    assert spec.fingerprint() == snapshot.as_dict()["model_fingerprint"]


def test_changing_an_assumption_changes_the_model_fingerprint():
    a = ModelSpec(minimum_time_to_expiry_minutes=30.0)
    b = ModelSpec(minimum_time_to_expiry_minutes=60.0)
    assert a.fingerprint() != b.fingerprint()
    assert ModelSpec().fingerprint() == ModelSpec().fingerprint()


def test_day_count_change_alters_the_numbers_and_the_fingerprint():
    chain = build_synthetic_chain()
    base = GexEngineConfig(model_spec=ModelSpec())
    act252 = GexEngineConfig(
        model_spec=ModelSpec(day_count_convention=DayCountConvention.ACT_252)
    )
    first = compute_gex_snapshot(chain, base)
    second = compute_gex_snapshot(chain, act252)
    assert first.total_unsigned_gex != second.total_unsigned_gex
    assert first.model_spec.fingerprint() != second.model_spec.fingerprint()


def test_config_fingerprint_is_recorded_and_responds_to_config_changes():
    chain = build_synthetic_chain()
    a = compute_gex_snapshot(chain, GexEngineConfig())
    b = compute_gex_snapshot(chain, GexEngineConfig(spot_move_pct=0.02))
    assert a.config_fingerprint
    assert a.config_fingerprint != b.config_fingerprint


def test_explicit_config_fingerprint_is_preserved():
    """Set by the config loader so a snapshot traces back to a file, not just to
    the in-memory object.
    """
    chain = build_synthetic_chain()
    produced = compute_gex_snapshot(
        chain, GexEngineConfig(config_fingerprint="research-abc123")
    )
    assert produced.config_fingerprint == "research-abc123"


# --- 0DTE floor sensitivity -------------------------------------------------


def late_session_chain():
    """Fifteen minutes to the 0DTE settlement, where the floor actually binds."""
    return build_synthetic_chain(SyntheticChainSpec(as_of=LATE_SESSION_AS_OF))


def test_floor_sensitivity_covers_every_candidate_floor():
    """Gamma diverges as T -> 0, so the floor decides how much of that divergence
    reaches the aggregate. Reported, never silently chosen.
    """
    report = compute_floor_sensitivity(late_session_chain())
    assert [e.floor_minutes for e in report.entries] == list(SENSITIVITY_FLOORS_MINUTES)
    assert report.baseline_floor_minutes == 60.0


def test_the_floor_visibly_changes_the_0dte_bucket_near_expiry():
    """With 900s remaining, the 30- and 60-minute floors both bind and a near-zero
    floor does not, so the three answers must differ materially.
    """
    report = compute_floor_sensitivity(late_session_chain())
    dte0 = [e.dte0_unsigned_gex for e in report.entries]
    # A near-zero floor lets the singularity through, so it must be the largest.
    assert dte0[0] == max(dte0)
    assert dte0[0] > dte0[1] > dte0[2]
    assert report.dte0_range_pct is not None
    assert report.dte0_range_pct > 1.0


def test_the_floor_is_inert_when_no_contract_is_close_enough_to_expiry():
    """Mid-session, five hours from settlement, every candidate floor is inactive.

    This is why the sensitivity report has to be run near expiry to mean
    anything -- and why a test run at midday would pass without testing.
    """
    report = compute_floor_sensitivity(build_synthetic_chain())
    assert report.dte0_range_pct == pytest.approx(0.0, abs=1e-9)


def test_the_floor_barely_moves_a_chain_with_no_same_day_expiry():
    """Control: without a 0DTE series there is no singularity to floor, so the
    sensitivity should nearly vanish. If it did not, the floor would be affecting
    contracts it has no business affecting.
    """
    spec = SyntheticChainSpec(expiries=((OptionRoot.SPXW, date(2026, 5, 15)),))
    report = compute_floor_sensitivity(build_synthetic_chain(spec))
    assert report.unsigned_gex_range_pct == pytest.approx(0.0, abs=1e-9)


def test_sensitivity_report_serialises():
    payload = compute_floor_sensitivity(build_synthetic_chain()).as_dict()
    assert len(payload["entries"]) == len(SENSITIVITY_FLOORS_MINUTES)
    assert "dte0_range_pct" in payload


# --- Option universe accounting ---------------------------------------------


def test_chain_and_zero_gamma_universes_are_reported_separately(snapshot):
    """Comparing a chain total against a DTE-capped zero-gamma level without
    saying so is comparing different populations.
    """
    assert snapshot.chain_universe.total_contract_count == snapshot.contract_count
    assert snapshot.zero_gamma_universe.max_dte_used == 60


def test_excluded_contracts_are_quantified_in_gex_not_just_in_count():
    """A count says how many; the share says how much it mattered."""
    spec = SyntheticChainSpec(
        expiries=(
            (OptionRoot.SPXW, date(2026, 3, 17)),
            (OptionRoot.SPXW, date(2026, 9, 18)),  # ~185 DTE, beyond the grid cap
        )
    )
    produced = compute_gex_snapshot(build_synthetic_chain(spec))
    universe = produced.zero_gamma_universe
    assert universe.excluded_contract_count > 0
    assert universe.excluded_unsigned_gex > 0.0
    assert universe.excluded_unsigned_gex_share is not None
    assert 0.0 < universe.excluded_unsigned_gex_share < 1.0
    assert (
        universe.included_unsigned_gex_share + universe.excluded_unsigned_gex_share
        == (pytest.approx(1.0))
    )
    assert "2026-09-18" in universe.excluded_expirations


def test_a_universe_difference_produces_an_explicit_warning():
    spec = SyntheticChainSpec(
        expiries=(
            (OptionRoot.SPXW, date(2026, 3, 17)),
            (OptionRoot.SPXW, date(2026, 9, 18)),
        )
    )
    produced = compute_gex_snapshot(build_synthetic_chain(spec))
    assert any("different universes" in w for w in produced.warnings)


def test_universe_reconciles_with_the_chain_total(snapshot):
    assert snapshot.chain_universe.included_unsigned_gex == pytest.approx(
        snapshot.total_unsigned_gex
    )
    assert snapshot.chain_universe.coverage_ratio == pytest.approx(1.0)


def test_max_dte_filter_is_recorded_with_its_reason():
    chain = build_synthetic_chain()
    produced = compute_gex_snapshot(chain, GexEngineConfig(max_dte=5))
    assert produced.chain_universe.max_dte_used == 5
    assert "beyond_max_dte" in produced.meta["exclusions"]
    assert produced.contract_count < len(chain.quotes)


def test_universe_serialises_every_required_field(snapshot):
    payload = snapshot.as_dict()["zero_gamma_universe"]
    for key in (
        "total_contract_count",
        "included_contract_count",
        "excluded_contract_count",
        "included_expirations",
        "excluded_expirations",
        "max_dte_used",
        "included_unsigned_gex",
        "excluded_unsigned_gex",
        "included_unsigned_gex_share",
        "excluded_unsigned_gex_share",
        "filter_reasons",
    ):
        assert key in payload, key


# --- Determinism ------------------------------------------------------------


def test_the_same_chain_produces_an_identical_snapshot():
    chain = build_synthetic_chain()
    assert compute_gex_snapshot(chain) == compute_gex_snapshot(chain)


def test_rebuilding_the_fixture_produces_an_identical_snapshot():
    """No hidden clock read, no dict-ordering dependence, no randomness."""
    first = compute_gex_snapshot(build_synthetic_chain())
    second = compute_gex_snapshot(build_synthetic_chain())
    assert first == second
    assert first.output_hash() == second.output_hash()


def test_output_hash_changes_when_a_number_changes():
    chain = build_synthetic_chain()
    baseline = compute_gex_snapshot(chain).output_hash()
    nudged = compute_gex_snapshot(replace(chain, spot=5001.0)).output_hash()
    assert baseline != nudged


def test_output_hash_ignores_warning_prose():
    """A hash that trips on a reworded warning is a hash nobody trusts."""
    chain = build_synthetic_chain()
    produced = compute_gex_snapshot(chain)
    reworded = replace(produced, warnings=("completely different text",))
    assert produced.output_hash() == reworded.output_hash()


# --- Zero gamma across conventions -----------------------------------------


def test_every_default_convention_is_reported_and_resolves(snapshot):
    for convention in (
        IVConvention.STICKY_STRIKE,
        IVConvention.FROZEN_IV,
        IVConvention.STICKY_MONEYNESS,
    ):
        result = snapshot.zero_gamma_for(convention)
        assert result is not None
        assert result.resolved


def test_primary_zero_gamma_is_the_first_resolved_convention(snapshot):
    primary = snapshot.primary_zero_gamma
    assert primary is not None
    assert primary.convention is IVConvention.STICKY_STRIKE


def test_convention_spread_is_reported_as_the_error_bar(snapshot):
    spread = snapshot.zero_gamma_spread_pct
    assert spread is not None
    assert spread >= 0.0


def test_root_count_stability_is_exposed(snapshot):
    """Renamed in v2.1.3: the property compares root *counts*.

    Calling it identity stability meant two runs with the same number of roots
    at different levels reported as stable -- the opposite of what happened.
    Genuine identity stability comes from ``compare_root_topology``.
    """
    assert snapshot.zero_gamma_root_count_stable is True


def test_root_count_stability_and_identity_stability_are_distinct(snapshot):
    """Counting roots and matching them are different questions.

    ``match_roots`` is the authoritative identity measure; the snapshot property
    only reports whether the *number* of roots agreed across conventions.
    """
    from src.gex.confidence import match_roots

    assert snapshot.zero_gamma_root_count_stable is True
    # Same count, entirely different levels: the count agrees and no root does.
    matching = match_roots((5050.0,), (5250.0,), spot=5000.0, tolerance_pct=0.25)
    assert matching.matched_root_count == 0
    assert matching.unmatched_root_count == 2


def test_spread_is_undefined_when_only_one_convention_runs():
    single = compute_gex_snapshot(
        build_synthetic_chain(),
        GexEngineConfig(
            zero_gamma=ZeroGammaConfig(conventions=(IVConvention.STICKY_STRIKE,))
        ),
    )
    assert single.zero_gamma_spread_pct is None


@pytest.mark.parametrize(
    "convention", [IVConvention.STICKY_DELTA, IVConvention.SURFACE_REFIT]
)
def test_requesting_an_unimplemented_convention_warns_and_returns_nothing(convention):
    config = GexEngineConfig(
        zero_gamma=ZeroGammaConfig(conventions=(IVConvention.STICKY_STRIKE, convention))
    )
    produced = compute_gex_snapshot(build_synthetic_chain(), config)
    result = produced.zero_gamma_for(convention)
    assert result is not None
    assert result.selected_root is None
    assert result.selection_method is RootSelectionMethod.CONVENTION_UNIMPLEMENTED
    assert any("is not implemented" in w for w in produced.warnings)
    assert unimplemented_conventions(config) == (convention,)


def test_default_config_requests_only_implemented_conventions():
    assert unimplemented_conventions(GexEngineConfig()) == ()


# --- Buckets ----------------------------------------------------------------


def test_bucket_lookup_returns_every_bucket(snapshot):
    for bucket in ExpiryBucket:
        assert snapshot.bucket(bucket) is not None


def test_0dte_bucket_is_populated_by_the_fixture(snapshot):
    zero_dte = snapshot.bucket(ExpiryBucket.DTE_0)
    assert zero_dte.contract_count > 0
    assert zero_dte.unsigned_gex > 0.0


def test_dominance_ratio_is_recorded_in_metadata(snapshot):
    assert 0.0 < snapshot.meta["dte0_dominance_ratio"] < 1.0


# --- Confidence integration -------------------------------------------------


def test_default_config_yields_an_uncalibrated_snapshot(snapshot):
    assert not snapshot.confidence.calibrated
    assert "zero_gamma_stability" in snapshot.confidence.uncalibrated_components


def test_stale_feed_lowers_confidence():
    fresh = compute_gex_snapshot(build_synthetic_chain())
    stale = compute_gex_snapshot(
        build_synthetic_chain(SyntheticChainSpec(quote_age_seconds=45.0))
    )
    assert stale.confidence.value < fresh.confidence.value


def test_future_dated_feed_zeroes_confidence_end_to_end():
    chain = build_synthetic_chain()
    poisoned = replace(chain, spot_timestamp=chain.as_of + timedelta(seconds=120))
    produced = compute_gex_snapshot(poisoned)
    assert produced.confidence.value == 0.0
    assert produced.confidence.hard_failures == ("future_timestamp_penalty",)


def test_flow_adjusted_input_is_threaded_into_sign_agreement():
    chain = build_synthetic_chain()
    naive = compute_gex_snapshot(chain).total_signed_gex
    with_flow = compute_gex_snapshot(chain, flow_adjusted_signed_gex=naive * 1.02)
    agreement = next(
        c for c in with_flow.confidence.components if c.name == "sign_model_agreement"
    )
    assert agreement.score > 0.9


def test_confidence_warnings_reach_the_snapshot(snapshot):
    assert any("uncalibrated" in w for w in snapshot.warnings)


# --- Degenerate and edge inputs --------------------------------------------


def test_empty_chain_produces_a_warned_zero_snapshot():
    chain = build_synthetic_chain()
    result = compute_gex_snapshot(replace(chain, quotes=()))
    assert result.total_unsigned_gex == 0.0
    assert result.contract_count == 0
    assert "no usable contracts in snapshot" in result.warnings
    assert result.walls.largest_call_gamma_strike is None
    assert all(z.identically_zero_curve for z in result.zero_gamma)


def test_validation_failures_are_surfaced_as_a_warning():
    chain = build_synthetic_chain()
    poisoned = with_quote(chain, 0, gamma=float("nan"))
    produced = compute_gex_snapshot(poisoned)
    assert any("failed validation" in w for w in produced.warnings)
    assert produced.validation.rejected == 1


def test_mixed_gamma_sources_are_warned_about():
    """Vendor gamma at spot plus shadow gamma on the grid is a discontinuity the
    operator needs to know about, not something to smooth over.
    """
    chain = build_synthetic_chain()
    half = len(chain.quotes) // 2
    mixed = replace(
        chain,
        quotes=tuple(
            replace(q, gamma=None) if i < half else q
            for i, q in enumerate(chain.quotes)
        ),
    )
    result = compute_gex_snapshot(mixed, GexEngineConfig(prefer_vendor_gamma=True))
    assert any("mixed gamma sources" in w for w in result.warnings)


def test_shadow_only_chain_produces_no_mixed_source_warning():
    chain = build_synthetic_chain(SyntheticChainSpec(vendor_gamma=False))
    result = compute_gex_snapshot(chain)
    assert not any("mixed gamma sources" in w for w in result.warnings)
    assert result.meta["vendor_gamma_count"] == 0


def test_single_contract_chain_has_no_zero_gamma_crossing():
    """One call is positive gamma everywhere -- nothing to cross."""
    result = compute_gex_snapshot(build_single_contract_chain())
    assert result.contract_count == 1
    assert all(z.no_root_found for z in result.zero_gamma)
    assert any("no sign change" in w for w in result.warnings)


def test_oi_age_uses_the_oldest_date_in_the_chain():
    """A partially-refreshed OI table must report as stale, not as fresh."""
    chain = build_synthetic_chain()
    mixed = with_quote(
        chain,
        0,
        timestamps=replace(
            chain.quotes[0].timestamps, open_interest_as_of=date(2026, 1, 5)
        ),
    )
    component = next(
        c
        for c in compute_gex_snapshot(mixed).confidence.components
        if c.name == "oi_freshness"
    )
    assert component.score < 1.0


# --- Walls and serialisation ------------------------------------------------


def test_distance_features_are_derivable_from_the_snapshot(snapshot):
    to_call = distance_pct(snapshot.spot, snapshot.walls.upside_call_wall)
    to_put = distance_pct(snapshot.spot, snapshot.walls.downside_put_wall)
    assert to_call is not None
    assert to_call > 0.0
    assert to_put is not None
    assert to_put < 0.0
    primary = snapshot.primary_zero_gamma
    assert distance_pct(snapshot.spot, primary.selected_root) is not None


def test_far_dated_only_chain_still_produces_walls():
    chain = build_synthetic_chain(
        SyntheticChainSpec(expiries=((OptionRoot.SPXW, date(2026, 5, 15)),))
    )
    result = compute_gex_snapshot(chain)
    assert result.walls.largest_call_gamma_strike is not None
    assert result.bucket(ExpiryBucket.DTE_GT_30).contract_count > 0


def test_snapshot_serialises_to_json_safe_primitives(snapshot):
    import json

    payload = snapshot.as_dict()
    encoded = json.dumps(payload, default=str)
    assert len(encoded) > 1000
    for key in (
        "model_spec",
        "config_fingerprint",
        "chain_universe",
        "zero_gamma_universe",
        "validation",
        "confidence",
        "walls",
        "zero_gamma",
    ):
        assert key in payload, key


def test_iv_source_travels_from_the_spec_into_the_snapshot():
    spec = SyntheticChainSpec(iv_source=IVSource.NBBO_BID_IV)
    produced = compute_gex_snapshot(
        build_synthetic_chain(spec), GexEngineConfig(model_spec=spec.model_spec())
    )
    assert produced.model_spec.iv_price_source is IVSource.NBBO_BID_IV
