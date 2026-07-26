"""The confidence model.

Two behaviours carry the most weight:

* **The uncalibrated path.** The score must still be produced, still be flagged,
  and the sentinel must be incapable of entering a numeric comparison. A
  confidence number silently built on invented thresholds is the failure mode
  this module exists to prevent.
* **Hard failures.** Some conditions invalidate a snapshot rather than merely
  lowering its score. A future-dated feed scoring 85/100 because everything else
  looked fine would be exactly the averaged-away warning we are guarding against.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.domain.gex import (
    IVConvention,
    OptionUniverse,
    RootSelectionMethod,
    ZeroGammaResult,
)
from src.domain.iv import IVSource, build_iv_quote
from src.domain.model_spec import ModelSpec
from src.domain.timestamps import DataQualityLimits
from src.gex.confidence import (
    COMPONENT_NAMES,
    ConfidenceInputs,
    compute_confidence,
    score_0dte_dominance,
    score_chain_completeness,
    score_crossed_market_penalty,
    score_future_timestamp,
    score_iv_spread_quality,
    score_model_parameter_completeness,
    score_multiple_root_penalty,
    score_oi_freshness,
    score_option_universe_coverage,
    score_quote_freshness,
    score_root_boundary,
    score_root_identity_stability,
    score_root_slope,
    score_sign_model_agreement,
    score_timestamp_alignment,
    score_vendor_lag,
    score_zero_gamma_stability,
)
from src.gex.config import (
    UNSPECIFIED_CALIBRATE,
    ConfidenceConfig,
    ConfidenceWeights,
    GexEngineConfig,
    calibrated_value,
    is_calibrated,
)
from src.gex.formulas import compute_contract_gex
from src.gex.sessions import eastern
from src.synthetic.chains import SyntheticChainSpec, build_synthetic_chain, with_quote

AS_OF = eastern(2026, 3, 17, 11, 0)
CFG = ConfidenceConfig()
LIMITS = DataQualityLimits()
CALIBRATED = replace(
    CFG,
    max_zero_gamma_shift_pct=0.25,
    max_sign_model_disagreement=0.30,
    max_0dte_dominance_ratio=0.60,
)


def make_root(
    *,
    convention: IVConvention = IVConvention.STICKY_STRIKE,
    root: float | None = 5050.0,
    all_roots: tuple[float, ...] | None = None,
    slope: float | None = 5.0e8,
    max_abs: float = 1.0e11,
    boundary: bool = False,
    spacing: float | None = None,
    expansions: int = 0,
) -> ZeroGammaResult:
    roots = all_roots if all_roots is not None else ((root,) if root else ())
    return ZeroGammaResult(
        convention=convention,
        selected_root=root,
        all_roots=roots,
        selection_method=(
            RootSelectionMethod.NEAREST_TO_SPOT
            if root
            else RootSelectionMethod.NONE_FOUND
        ),
        grid_lower_bound=4800.0,
        grid_upper_bound=5200.0,
        grid_points=81,
        spot=5000.0,
        local_slope_at_selected_root=slope,
        max_abs_gex_on_grid=max_abs,
        root_near_boundary=boundary,
        nearest_root_spacing_pct=spacing,
        no_root_found=root is None,
        grid_expansions=expansions,
    )


def make_inputs(**overrides: object) -> ConfidenceInputs:
    chain = build_synthetic_chain()
    result = compute_contract_gex(chain)
    universe = OptionUniverse(
        total_contract_count=250,
        included_contract_count=250,
        included_unsigned_gex=1.0e11,
        excluded_unsigned_gex=0.0,
    )
    base: dict[str, object] = {
        "as_of": AS_OF,
        "result": result,
        "zero_gamma_results": (
            make_root(convention=IVConvention.STICKY_STRIKE, root=5050.0),
            make_root(convention=IVConvention.FROZEN_IV, root=5051.0),
        ),
        "spot": 5000.0,
        "dte0_dominance_ratio": 0.35,
        "model_spec": ModelSpec(
            risk_free_rate=0.042,
            dividend_yield=0.013,
            iv_price_source=IVSource.NBBO_MID_IV,
        ),
        "limits": LIMITS,
        "chain_universe": universe,
        "zero_gamma_universe": universe,
        "quotes": chain.quotes,
        "options_feed_timestamp": AS_OF,
        "spot_feed_timestamp": AS_OF,
        "open_interest_as_of": date(2026, 3, 16),
        "naive_signed_gex": -1.0e10,
        "flow_adjusted_signed_gex": -1.05e10,
    }
    base.update(overrides)
    return ConfidenceInputs(**base)  # type: ignore[arg-type]


# --- The sentinel -----------------------------------------------------------


def test_sentinel_is_falsy_and_refuses_ordering():
    """Refusing comparison is the point: an uncalibrated threshold must not be
    able to silently participate in a ``>=`` and pass.
    """
    assert not UNSPECIFIED_CALIBRATE
    assert not is_calibrated(UNSPECIFIED_CALIBRATE)
    assert is_calibrated(0.25)
    for expression in (
        lambda: UNSPECIFIED_CALIBRATE > 5.0,
        lambda: UNSPECIFIED_CALIBRATE < 5.0,
        lambda: UNSPECIFIED_CALIBRATE >= 5.0,
        lambda: UNSPECIFIED_CALIBRATE <= 5.0,
    ):
        with pytest.raises(TypeError, match="calibrate"):
            expression()


def test_sentinel_refuses_numeric_coercion():
    """The most likely accident is a caller doing ``float(threshold)`` to make
    the types line up, which converts a loud "not researched" into a quiet,
    arbitrary number.
    """
    with pytest.raises(TypeError, match="float"):
        float(UNSPECIFIED_CALIBRATE)
    with pytest.raises(TypeError, match="int"):
        int(UNSPECIFIED_CALIBRATE)


def test_sentinel_refuses_arithmetic():
    for expression in (
        lambda: UNSPECIFIED_CALIBRATE + 1,
        lambda: 1 + UNSPECIFIED_CALIBRATE,
        lambda: UNSPECIFIED_CALIBRATE * 2,
        lambda: UNSPECIFIED_CALIBRATE / 2,
        lambda: UNSPECIFIED_CALIBRATE - 1,
    ):
        with pytest.raises(TypeError):
            expression()


def test_calibrated_value_is_the_only_sanctioned_extraction():
    assert calibrated_value(UNSPECIFIED_CALIBRATE) is None
    assert calibrated_value(0.25) == pytest.approx(0.25)


def test_sentinel_is_a_singleton_and_self_describing():
    assert type(UNSPECIFIED_CALIBRATE)() is UNSPECIFIED_CALIBRATE
    assert repr(UNSPECIFIED_CALIBRATE) == "UNSPECIFIED_CALIBRATE"
    assert str(UNSPECIFIED_CALIBRATE) == "UNSPECIFIED_CALIBRATE"


def test_sentinel_equality_still_works_for_config_round_trips():
    assert UNSPECIFIED_CALIBRATE == UNSPECIFIED_CALIBRATE
    assert UNSPECIFIED_CALIBRATE != 0.25


# --- Data-quality components ------------------------------------------------


def test_complete_chain_scores_full_marks():
    component = score_chain_completeness(make_inputs(), CFG)
    assert component.score == pytest.approx(1.0)
    assert not component.uncalibrated


def test_completeness_uses_the_expected_count_when_the_adapter_states_it():
    """Usable/received says nothing about strikes the vendor never sent."""
    inputs = make_inputs(expected_contract_count=100_000)
    assert score_chain_completeness(inputs, CFG).score == 0.0


def test_fresh_quotes_score_full_and_stale_quotes_score_zero():
    assert score_quote_freshness(make_inputs(), CFG).score == pytest.approx(1.0)
    stale = make_inputs(options_feed_timestamp=AS_OF - timedelta(seconds=300))
    assert score_quote_freshness(stale, CFG).score == 0.0


def test_quote_freshness_decays_linearly():
    half = make_inputs(
        options_feed_timestamp=AS_OF
        - timedelta(seconds=LIMITS.max_snapshot_age_seconds / 2)
    )
    assert score_quote_freshness(half, CFG).score == pytest.approx(0.5)


def test_a_future_dated_feed_does_not_earn_a_perfect_freshness_score():
    """The specific bug this guards: ``max(age, 0)`` would clamp a future
    timestamp to zero age and hand it full marks.
    """
    ahead = make_inputs(options_feed_timestamp=AS_OF + timedelta(seconds=90))
    component = score_quote_freshness(ahead, CFG)
    assert component.score == 0.0
    assert "AHEAD" in component.detail


def test_absent_feed_timestamp_scores_zero_rather_than_being_ignored():
    component = score_quote_freshness(make_inputs(options_feed_timestamp=None), CFG)
    assert component.score == 0.0
    assert "cannot prove freshness" in component.detail


def test_t_minus_one_open_interest_is_full_marks_not_a_penalty():
    assert score_oi_freshness(make_inputs(), CFG).score == pytest.approx(1.0)


def test_weekend_does_not_make_friday_open_interest_look_stale():
    """Session-based ageing: Monday morning reading Friday's settlement is
    normal, not a stalled job.
    """
    monday = eastern(2026, 3, 23, 11, 0)
    inputs = make_inputs(as_of=monday, open_interest_as_of=date(2026, 3, 20))
    component = score_oi_freshness(inputs, CFG)
    assert component.score == pytest.approx(1.0)
    assert "1 trading session(s) old" in component.detail


def test_holiday_weekend_does_not_make_open_interest_look_stale():
    """Friday before Memorial Day, read on the Tuesday after."""
    tuesday = eastern(2026, 5, 26, 11, 0)
    inputs = make_inputs(as_of=tuesday, open_interest_as_of=date(2026, 5, 22))
    assert score_oi_freshness(inputs, CFG).score == pytest.approx(1.0)


def test_genuinely_stale_open_interest_is_penalised():
    inputs = make_inputs(open_interest_as_of=date(2026, 3, 2))
    assert score_oi_freshness(inputs, CFG).score < 1.0


def test_missing_oi_date_scores_zero():
    assert score_oi_freshness(make_inputs(open_interest_as_of=None), CFG).score == 0.0


def test_crossed_quotes_are_penalised_in_proportion():
    assert score_crossed_market_penalty(make_inputs(), CFG).score == pytest.approx(1.0)
    chain = build_synthetic_chain()
    dirty = chain
    for index in range(30):
        dirty = with_quote(dirty, index, bid=99.0, ask=1.0)
    inputs = make_inputs(result=compute_contract_gex(dirty))
    assert score_crossed_market_penalty(inputs, CFG).score == 0.0


def test_feed_drift_between_options_and_spot_is_penalised():
    assert score_vendor_lag(make_inputs(), CFG).score == pytest.approx(1.0)
    drifted = make_inputs(spot_feed_timestamp=AS_OF - timedelta(seconds=10))
    assert score_vendor_lag(drifted, CFG).score == 0.0


def test_drift_is_penalised_in_both_directions():
    ahead = make_inputs(spot_feed_timestamp=AS_OF + timedelta(seconds=10))
    assert score_vendor_lag(ahead, CFG).score == 0.0


# --- New v2 components ------------------------------------------------------


def test_timestamp_alignment_is_full_when_no_record_is_skewed():
    assert score_timestamp_alignment(make_inputs(), CFG).score == pytest.approx(1.0)


def test_timestamp_alignment_falls_when_records_are_skewed():
    skewed_chain = build_synthetic_chain(SyntheticChainSpec(greeks_age_seconds=45.0))
    inputs = make_inputs(result=compute_contract_gex(skewed_chain))
    component = score_timestamp_alignment(inputs, CFG)
    assert component.score < 0.5
    assert "join skew tolerance" in component.detail


def test_future_timestamp_is_a_hard_failure():
    """DATA_HALT-eligible: the snapshot is not merely worse, it is untrustworthy."""
    inputs = make_inputs(options_feed_timestamp=AS_OF + timedelta(seconds=120))
    component = score_future_timestamp(inputs, CFG)
    assert component.score == 0.0
    assert component.hard_failure
    assert "DATA_HALT" in component.detail


def test_small_clock_skew_is_not_a_hard_failure():
    """Two machines disagreeing by a second is ordinary."""
    inputs = make_inputs(options_feed_timestamp=AS_OF + timedelta(seconds=1))
    component = score_future_timestamp(inputs, CFG)
    assert component.score == pytest.approx(1.0)
    assert not component.hard_failure


def test_future_dated_contract_records_also_trip_the_hard_failure():
    chain = build_synthetic_chain()
    poisoned = with_quote(
        chain,
        0,
        timestamps=replace(
            chain.quotes[0].timestamps,
            quote_timestamp=AS_OF + timedelta(minutes=10),
        ),
    )
    inputs = make_inputs(result=compute_contract_gex(poisoned))
    assert score_future_timestamp(inputs, CFG).hard_failure


def test_universe_coverage_is_full_when_the_grid_sees_everything():
    assert score_option_universe_coverage(make_inputs(), CFG).score == pytest.approx(
        1.0
    )


def test_universe_coverage_falls_when_the_grid_excludes_gamma():
    partial = OptionUniverse(
        total_contract_count=250,
        included_contract_count=100,
        included_unsigned_gex=3.0e10,
        excluded_unsigned_gex=7.0e10,
        max_dte_used=60,
    )
    component = score_option_universe_coverage(
        make_inputs(zero_gamma_universe=partial), CFG
    )
    assert component.score < 0.5
    assert "30.0%" in component.detail


def test_iv_spread_quality_is_full_on_a_clean_book():
    assert score_iv_spread_quality(make_inputs(), CFG).score == pytest.approx(1.0)


def test_iv_spread_quality_falls_on_wide_or_one_sided_books():
    chain = build_synthetic_chain()
    degraded = chain
    for index in range(len(chain.quotes)):
        degraded = with_quote(
            degraded,
            index,
            iv=build_iv_quote(bid_iv=0.05, mid_iv=0.30, ask_iv=0.60),
        )
    component = score_iv_spread_quality(make_inputs(quotes=degraded.quotes), CFG)
    assert component.score == 0.0
    assert "WIDE_SPREAD" in component.detail


def test_iv_spread_quality_reports_when_unmeasurable():
    assert score_iv_spread_quality(make_inputs(quotes=()), CFG).score == 0.0


def test_model_parameter_completeness_is_full_when_everything_is_stated():
    assert score_model_parameter_completeness(
        make_inputs(), CFG
    ).score == pytest.approx(1.0)


def test_zero_rate_and_dividend_are_treated_as_unspecified():
    """Zero rate and zero dividend on an SPX chain are almost certainly "nobody
    configured this", and both bias gamma.
    """
    component = score_model_parameter_completeness(
        make_inputs(model_spec=ModelSpec()), CFG
    )
    assert component.score < 0.5
    assert "risk_free_rate=0" in component.detail
    assert "dividend_yield=0" in component.detail


# --- Root diagnostics -------------------------------------------------------


def test_a_single_root_per_convention_scores_full():
    assert score_multiple_root_penalty(make_inputs(), CFG).score == pytest.approx(1.0)


def test_multiple_roots_are_penalised():
    inputs = make_inputs(
        zero_gamma_results=(
            make_root(all_roots=(4950.0, 5050.0, 5150.0), root=5050.0, spacing=2.0),
        )
    )
    component = score_multiple_root_penalty(inputs, CFG)
    assert component.score < 1.0
    assert "worst root count 3" in component.detail


def test_closely_spaced_roots_are_penalised_harder_than_distant_ones():
    """Two crossings 5% apart is a readable state; 0.1% apart is a coin flip."""
    distant = make_inputs(
        zero_gamma_results=(make_root(all_roots=(5050.0, 5300.0), spacing=5.0),)
    )
    tight = make_inputs(
        zero_gamma_results=(make_root(all_roots=(5050.0, 5055.0), spacing=0.1),)
    )
    assert (
        score_multiple_root_penalty(tight, CFG).score
        < score_multiple_root_penalty(distant, CFG).score
    )


def test_steep_roots_score_higher_than_shallow_ones():
    steep = make_inputs(zero_gamma_results=(make_root(slope=5.0e8, max_abs=1.0e11),))
    shallow = make_inputs(zero_gamma_results=(make_root(slope=1.0e5, max_abs=1.0e11),))
    assert score_root_slope(steep, CFG).score > score_root_slope(shallow, CFG).score
    assert score_root_slope(shallow, CFG).score < 0.1


def test_slope_component_reports_its_units():
    detail = score_root_slope(make_inputs(), CFG).detail
    assert "fraction of max |GEX| per 1% of spot" in detail


def test_interior_roots_score_full_on_the_boundary_component():
    assert score_root_boundary(make_inputs(), CFG).score == pytest.approx(1.0)


def test_boundary_roots_are_penalised_and_report_expansions():
    inputs = make_inputs(zero_gamma_results=(make_root(boundary=True, expansions=3),))
    component = score_root_boundary(inputs, CFG)
    assert component.score == 0.0
    assert "3 expansion(s)" in component.detail
    assert "artefact" in component.detail


def test_root_identity_is_stable_when_conventions_agree_on_the_count():
    assert score_root_identity_stability(make_inputs(), CFG).score == pytest.approx(1.0)


def test_root_identity_is_unstable_when_conventions_find_different_counts():
    inputs = make_inputs(
        zero_gamma_results=(
            make_root(convention=IVConvention.STICKY_STRIKE, all_roots=(5050.0,)),
            make_root(
                convention=IVConvention.FROZEN_IV,
                all_roots=(4950.0, 5050.0),
                root=5050.0,
            ),
        )
    )
    component = score_root_identity_stability(inputs, CFG)
    assert component.score == 0.0
    assert "disagree on curve shape" in component.detail


def test_root_components_report_zero_when_nothing_resolved():
    inputs = make_inputs(zero_gamma_results=(make_root(root=None),))
    assert score_multiple_root_penalty(inputs, CFG).score == 0.0
    assert score_root_slope(inputs, CFG).score == 0.0
    assert score_root_boundary(inputs, CFG).score == 0.0


# --- Market-claim components ------------------------------------------------


def test_zero_gamma_stability_is_flagged_uncalibrated_by_default():
    component = score_zero_gamma_stability(make_inputs(), CFG)
    assert component.uncalibrated
    assert 0.0 < component.score <= 1.0


def test_zero_gamma_stability_is_not_flagged_once_calibrated():
    assert not score_zero_gamma_stability(make_inputs(), CALIBRATED).uncalibrated


def test_wide_convention_disagreement_collapses_stability():
    inputs = make_inputs(
        zero_gamma_results=(
            make_root(convention=IVConvention.STICKY_STRIKE, root=4900.0),
            make_root(convention=IVConvention.STICKY_MONEYNESS, root=5150.0),
        )
    )
    assert score_zero_gamma_stability(inputs, CFG).score == 0.0


def test_a_single_resolved_convention_cannot_demonstrate_stability():
    inputs = make_inputs(zero_gamma_results=(make_root(),))
    component = score_zero_gamma_stability(inputs, CFG)
    assert component.score == 0.0
    assert "unmeasurable" in component.detail


def test_missing_flow_model_scores_zero_and_says_why():
    component = score_sign_model_agreement(
        make_inputs(flow_adjusted_signed_gex=None), CFG
    )
    assert component.score == 0.0
    assert "Cboe Open-Close" in component.detail


def test_agreeing_sign_models_score_high():
    assert score_sign_model_agreement(make_inputs(), CFG).score > 0.8


def test_a_sign_flip_between_models_is_a_hard_zero():
    flipped = make_inputs(naive_signed_gex=-1.0e10, flow_adjusted_signed_gex=1.0e9)
    component = score_sign_model_agreement(flipped, CFG)
    assert component.score == 0.0
    assert "disagree on sign" in component.detail


def test_0dte_dominance_below_the_alert_level_scores_full():
    assert score_0dte_dominance(
        make_inputs(dte0_dominance_ratio=0.2), CFG
    ).score == pytest.approx(1.0)


def test_total_0dte_dominance_collapses_the_component():
    assert score_0dte_dominance(make_inputs(dte0_dominance_ratio=1.0), CFG).score == 0.0


def test_undefined_dominance_scores_zero():
    component = score_0dte_dominance(make_inputs(dte0_dominance_ratio=None), CFG)
    assert component.score == 0.0
    assert "undefined" in component.detail


# --- Aggregation ------------------------------------------------------------


def test_every_component_is_always_present_and_in_a_fixed_order():
    score = compute_confidence(make_inputs(), CFG)
    assert tuple(c.name for c in score.components) == COMPONENT_NAMES


def test_output_object_exposes_the_required_fields():
    score = compute_confidence(make_inputs(), CFG)
    payload = score.as_dict()
    for key in ("score", "calibrated", "components", "warnings", "hard_failures"):
        assert key in payload
    assert score.score == score.value


def test_score_is_bounded_to_the_zero_hundred_range():
    assert 0.0 <= compute_confidence(make_inputs(), CFG).value <= 100.0


def test_default_config_leaves_the_score_uncalibrated():
    """The engine produces research output on day one and flags that its market
    thresholds are unresearched. Note this is a *flag*, not enforcement -- no
    risk engine exists in this repository.
    """
    score = compute_confidence(make_inputs(), CFG)
    assert not score.calibrated
    assert set(score.uncalibrated_components) == {
        "zero_gamma_stability",
        "sign_model_agreement",
        "0dte_dominance_alert",
    }
    assert any("uncalibrated" in w for w in score.warnings)


def test_calibrating_every_threshold_clears_the_flag():
    score = compute_confidence(make_inputs(), CALIBRATED)
    assert score.calibrated
    assert score.uncalibrated_components == ()


def test_a_hard_failure_zeroes_the_whole_score():
    """Averaging a hard failure away is exactly what this model must not do."""
    inputs = make_inputs(options_feed_timestamp=AS_OF + timedelta(seconds=120))
    score = compute_confidence(inputs, CALIBRATED)
    assert score.value == 0.0
    assert score.hard_failures == ("future_timestamp_penalty",)
    assert not score.calibrated
    assert any("HARD FAILURE" in w for w in score.warnings)


def test_a_pristine_snapshot_scores_near_the_top():
    assert compute_confidence(make_inputs(), CALIBRATED).value > 90.0


def test_a_broken_snapshot_scores_near_zero():
    chain = build_synthetic_chain()
    broken_result = compute_contract_gex(
        build_synthetic_chain(SyntheticChainSpec(greeks_age_seconds=600.0))
    )
    broken = make_inputs(
        result=broken_result,
        options_feed_timestamp=AS_OF - timedelta(seconds=3000),
        spot_feed_timestamp=AS_OF - timedelta(seconds=1000),
        open_interest_as_of=date(2026, 1, 2),
        dte0_dominance_ratio=1.0,
        flow_adjusted_signed_gex=None,
        zero_gamma_results=(),
        expected_contract_count=100_000,
        model_spec=ModelSpec(),
        quotes=(),
        zero_gamma_universe=OptionUniverse(
            total_contract_count=len(chain.quotes),
            included_contract_count=1,
            included_unsigned_gex=1.0,
            excluded_unsigned_gex=1.0e11,
        ),
    )
    assert compute_confidence(broken, CFG).value < 10.0


def test_weights_are_normalised_so_the_scale_survives_reweighting():
    reweighted = replace(CALIBRATED, weights=ConfidenceWeights(chain_completeness=5.0))
    baseline = compute_confidence(make_inputs(), CALIBRATED).value
    shifted = compute_confidence(make_inputs(), reweighted).value
    assert 0.0 <= shifted <= 100.0
    assert shifted != baseline


def test_zero_total_weight_is_rejected():
    zeroed = replace(
        CFG,
        weights=ConfidenceWeights(
            **{name: 0.0 for name in ConfidenceWeights().as_dict()}
        ),
    )
    with pytest.raises(ValueError, match="weights sum to zero"):
        compute_confidence(make_inputs(), zeroed)


def test_config_serialises_sentinels_as_their_name_not_a_number():
    payload = GexEngineConfig().as_dict()
    assert payload["confidence"]["max_zero_gamma_shift_pct"] == "UNSPECIFIED_CALIBRATE"
