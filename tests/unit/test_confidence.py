"""The eight-component confidence score.

The behaviour that matters most here is the *uncalibrated* path: the score must
still be produced, still be flagged, and still block trading. A confidence number
silently built on invented thresholds is the failure mode these tests exist to
prevent.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.domain.gex import IVConvention, ZeroGammaResult
from src.gex.confidence import (
    ConfidenceInputs,
    compute_confidence,
    score_0dte_dominance,
    score_chain_completeness,
    score_crossed_market_penalty,
    score_oi_freshness,
    score_quote_freshness,
    score_sign_model_agreement,
    score_vendor_lag,
    score_zero_gamma_stability,
)
from src.gex.config import (
    UNSPECIFIED_CALIBRATE,
    ConfidenceConfig,
    is_calibrated,
)
from src.gex.formulas import ContractGexResult, compute_contract_gex
from src.gex.sessions import eastern
from tests.fixtures.chains import build_synthetic_chain

AS_OF = eastern(2026, 3, 17, 11, 0)


def make_result(
    *,
    contracts: int = 100,
    total: int = 100,
    crossed: int = 0,
    expired: int = 0,
    no_oi: int = 0,
    no_gamma: int = 0,
) -> ContractGexResult:
    """A ContractGexResult with only the diagnostic counters populated."""
    real = compute_contract_gex(build_synthetic_chain()).contracts[:contracts]
    return ContractGexResult(
        contracts=real,
        total_quotes=total,
        dropped_expired=expired,
        dropped_no_open_interest=no_oi,
        dropped_no_gamma_source=no_gamma,
        dropped_crossed=crossed,
        vendor_gamma_count=len(real),
        shadow_gamma_count=0,
    )


def make_inputs(**overrides) -> ConfidenceInputs:
    base = dict(
        as_of=AS_OF,
        result=make_result(),
        zero_gamma_results=(
            ZeroGammaResult(
                convention=IVConvention.STICKY_STRIKE,
                zero_gamma_spot=5050.0,
                grid_low=4800.0,
                grid_high=5200.0,
                grid_points=81,
            ),
            ZeroGammaResult(
                convention=IVConvention.FROZEN_IV,
                zero_gamma_spot=5051.0,
                grid_low=4800.0,
                grid_high=5200.0,
                grid_points=81,
            ),
        ),
        spot=5000.0,
        dte0_dominance_ratio=0.35,
        options_feed_timestamp=AS_OF,
        spot_feed_timestamp=AS_OF,
        open_interest_asof=date(2026, 3, 16),
        naive_signed_gex=-1.0e10,
        flow_adjusted_signed_gex=-1.05e10,
    )
    base.update(overrides)
    return ConfidenceInputs(**base)


CFG = ConfidenceConfig()


# --- The sentinel -----------------------------------------------------------


def test_sentinel_is_falsy_and_refuses_comparison():
    """Refusing comparison is the point: an uncalibrated threshold must not be
    able to silently participate in a ``>=`` and pass.
    """
    assert not UNSPECIFIED_CALIBRATE
    assert not is_calibrated(UNSPECIFIED_CALIBRATE)
    assert is_calibrated(0.25)
    with pytest.raises(TypeError, match="calibrate"):
        _ = 5.0 < UNSPECIFIED_CALIBRATE
    with pytest.raises(TypeError):
        _ = UNSPECIFIED_CALIBRATE >= 5.0


def test_sentinel_is_a_singleton():
    assert type(UNSPECIFIED_CALIBRATE)() is UNSPECIFIED_CALIBRATE


def test_repr_names_itself_so_it_is_obvious_in_a_config_dump():
    assert repr(UNSPECIFIED_CALIBRATE) == "UNSPECIFIED_CALIBRATE"


# --- Data-quality components ------------------------------------------------


def test_complete_chain_scores_full_marks():
    component = score_chain_completeness(make_inputs(), CFG)
    assert component.score == pytest.approx(1.0)
    assert not component.uncalibrated


def test_missing_strikes_reduce_completeness():
    inputs = make_inputs(result=make_result(contracts=40, total=100, no_gamma=60))
    assert score_chain_completeness(inputs, CFG).score < 0.5


def test_completeness_uses_the_expected_count_when_the_adapter_states_it():
    """Usable/received says nothing about strikes the vendor never sent."""
    inputs = make_inputs(
        result=make_result(contracts=50, total=50), expected_contract_count=200
    )
    assert score_chain_completeness(inputs, CFG).score == 0.0


def test_fresh_quotes_score_full_and_stale_quotes_score_zero():
    assert score_quote_freshness(make_inputs(), CFG).score == pytest.approx(1.0)
    stale = make_inputs(options_feed_timestamp=AS_OF - timedelta(seconds=60))
    assert score_quote_freshness(stale, CFG).score == 0.0


def test_quote_freshness_decays_linearly():
    half = make_inputs(
        options_feed_timestamp=AS_OF
        - timedelta(seconds=CFG.max_quote_staleness_sec / 2)
    )
    assert score_quote_freshness(half, CFG).score == pytest.approx(0.5)


def test_absent_feed_timestamp_scores_zero_rather_than_being_ignored():
    component = score_quote_freshness(make_inputs(options_feed_timestamp=None), CFG)
    assert component.score == 0.0
    assert "cannot prove freshness" in component.detail


def test_t_minus_one_open_interest_is_full_marks_not_a_penalty():
    """OI is built from the prior settlement, so T-1 is the best achievable."""
    assert score_oi_freshness(make_inputs(), CFG).score == pytest.approx(1.0)


def test_stale_open_interest_is_penalised():
    week_old = make_inputs(open_interest_asof=date(2026, 3, 9))
    assert score_oi_freshness(week_old, CFG).score < 1.0


def test_weekend_does_not_make_friday_open_interest_look_stale():
    """Monday morning reading Friday's settlement is normal, not a stalled job."""
    monday = eastern(2026, 3, 23, 11, 0)
    friday = date(2026, 3, 20)
    inputs = make_inputs(as_of=monday, open_interest_asof=friday)
    assert score_oi_freshness(inputs, CFG).score == pytest.approx(1.0)


def test_crossed_quotes_are_penalised_in_proportion():
    clean = score_crossed_market_penalty(make_inputs(), CFG)
    assert clean.score == pytest.approx(1.0)
    dirty = make_inputs(result=make_result(crossed=20, total=100))
    assert score_crossed_market_penalty(dirty, CFG).score == 0.0
    half = make_inputs(result=make_result(crossed=5, total=100))
    assert score_crossed_market_penalty(half, CFG).score == pytest.approx(0.5)


def test_feed_drift_between_options_and_spot_is_penalised():
    assert score_vendor_lag(make_inputs(), CFG).score == pytest.approx(1.0)
    drifted = make_inputs(spot_feed_timestamp=AS_OF - timedelta(seconds=10))
    assert score_vendor_lag(drifted, CFG).score == 0.0


def test_drift_is_penalised_in_both_directions():
    ahead = make_inputs(spot_feed_timestamp=AS_OF + timedelta(seconds=10))
    assert score_vendor_lag(ahead, CFG).score == 0.0


# --- Calibration-target components ------------------------------------------


def test_zero_gamma_stability_is_flagged_uncalibrated_by_default():
    component = score_zero_gamma_stability(make_inputs(), CFG)
    assert component.uncalibrated
    assert 0.0 < component.score <= 1.0


def test_zero_gamma_stability_is_not_flagged_once_calibrated():
    calibrated = replace(CFG, max_zero_gamma_shift_pct=0.25)
    component = score_zero_gamma_stability(make_inputs(), calibrated)
    assert not component.uncalibrated


def test_wide_convention_disagreement_collapses_stability():
    """The whole point of running several IV conventions."""
    inputs = make_inputs(
        zero_gamma_results=(
            ZeroGammaResult(
                convention=IVConvention.STICKY_STRIKE,
                zero_gamma_spot=4900.0,
                grid_low=4800.0,
                grid_high=5200.0,
                grid_points=81,
            ),
            ZeroGammaResult(
                convention=IVConvention.STICKY_DELTA,
                zero_gamma_spot=5150.0,
                grid_low=4800.0,
                grid_high=5200.0,
                grid_points=81,
            ),
        )
    )
    assert score_zero_gamma_stability(inputs, CFG).score == 0.0


def test_a_single_resolved_convention_cannot_demonstrate_stability():
    inputs = make_inputs(
        zero_gamma_results=(
            ZeroGammaResult(
                convention=IVConvention.STICKY_STRIKE,
                zero_gamma_spot=5050.0,
                grid_low=4800.0,
                grid_high=5200.0,
                grid_points=81,
            ),
        )
    )
    component = score_zero_gamma_stability(inputs, CFG)
    assert component.score == 0.0
    assert "unmeasurable" in component.detail


def test_missing_flow_model_scores_zero_and_says_why():
    """No second sign model means the naive sign is unverified -- which is a
    penalty, not a free pass.
    """
    component = score_sign_model_agreement(
        make_inputs(flow_adjusted_signed_gex=None), CFG
    )
    assert component.score == 0.0
    assert "Cboe Open-Close" in component.detail


def test_agreeing_sign_models_score_high():
    assert score_sign_model_agreement(make_inputs(), CFG).score > 0.8


def test_a_sign_flip_between_models_is_a_hard_zero():
    """Magnitude agreement is irrelevant if the two models disagree on direction."""
    flipped = make_inputs(naive_signed_gex=-1.0e10, flow_adjusted_signed_gex=1.0e9)
    component = score_sign_model_agreement(flipped, CFG)
    assert component.score == 0.0
    assert "disagree on sign" in component.detail


def test_0dte_dominance_below_the_alert_level_scores_full():
    assert score_0dte_dominance(make_inputs(dte0_dominance_ratio=0.2), CFG).score == (
        pytest.approx(1.0)
    )


def test_total_0dte_dominance_collapses_the_component():
    assert score_0dte_dominance(make_inputs(dte0_dominance_ratio=1.0), CFG).score == 0.0


def test_undefined_dominance_scores_zero():
    component = score_0dte_dominance(make_inputs(dte0_dominance_ratio=None), CFG)
    assert component.score == 0.0
    assert "undefined" in component.detail


# --- Aggregation ------------------------------------------------------------


def test_all_eight_components_are_always_present():
    score = compute_confidence(make_inputs(), CFG)
    assert [c.name for c in score.components] == [
        "chain_completeness",
        "quote_freshness",
        "oi_freshness",
        "crossed_market_penalty",
        "zero_gamma_stability",
        "sign_model_agreement",
        "0dte_dominance_alert",
        "vendor_lag_alert",
    ]


def test_score_is_bounded_to_the_zero_hundred_range():
    assert 0.0 <= compute_confidence(make_inputs(), CFG).value <= 100.0


def test_default_config_leaves_the_score_uncalibrated_and_untradeable():
    """The central safety property: the engine produces research output on day
    one, but ``calibrated`` stays False so the risk engine blocks live orders.
    """
    score = compute_confidence(make_inputs(), CFG)
    assert not score.calibrated
    assert set(score.uncalibrated_components) == {
        "zero_gamma_stability",
        "sign_model_agreement",
        "0dte_dominance_alert",
    }


def test_calibrating_every_threshold_clears_the_flag():
    calibrated = replace(
        CFG,
        max_zero_gamma_shift_pct=0.25,
        max_sign_model_disagreement=0.30,
        max_0dte_dominance_ratio=0.60,
    )
    score = compute_confidence(make_inputs(), calibrated)
    assert score.calibrated
    assert score.uncalibrated_components == ()


def test_a_pristine_snapshot_scores_near_the_top():
    calibrated = replace(
        CFG,
        max_zero_gamma_shift_pct=0.25,
        max_sign_model_disagreement=0.30,
        max_0dte_dominance_ratio=0.60,
    )
    assert compute_confidence(make_inputs(), calibrated).value > 90.0


def test_a_broken_snapshot_scores_near_zero():
    broken = make_inputs(
        result=make_result(contracts=5, total=100, crossed=50, no_gamma=45),
        options_feed_timestamp=AS_OF - timedelta(seconds=300),
        spot_feed_timestamp=AS_OF - timedelta(seconds=100),
        open_interest_asof=date(2026, 1, 2),
        dte0_dominance_ratio=1.0,
        flow_adjusted_signed_gex=None,
        zero_gamma_results=(),
    )
    assert compute_confidence(broken, CFG).value < 5.0


def test_weights_are_normalised_so_the_scale_survives_reweighting():
    """Changing one weight must not change the achievable maximum."""
    from src.gex.config import ConfidenceWeights

    calibrated = replace(
        CFG,
        max_zero_gamma_shift_pct=0.25,
        max_sign_model_disagreement=0.30,
        max_0dte_dominance_ratio=0.60,
    )
    reweighted = replace(
        calibrated, weights=ConfidenceWeights(chain_completeness=5.0)
    )
    baseline = compute_confidence(make_inputs(), calibrated).value
    shifted = compute_confidence(make_inputs(), reweighted).value
    assert 0.0 <= shifted <= 100.0
    assert shifted != baseline


def test_zero_total_weight_is_rejected():
    from src.gex.config import ConfidenceWeights

    zeroed = replace(
        CFG,
        weights=ConfidenceWeights(
            chain_completeness=0.0,
            quote_freshness=0.0,
            oi_freshness=0.0,
            crossed_market_penalty=0.0,
            zero_gamma_stability=0.0,
            sign_model_agreement=0.0,
            dte0_dominance_alert=0.0,
            vendor_lag_alert=0.0,
        ),
    )
    with pytest.raises(ValueError, match="weights sum to zero"):
        compute_confidence(make_inputs(), zeroed)
