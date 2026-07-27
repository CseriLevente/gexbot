"""Replay-hash coverage and cross-convention root topology.

Two v2 defects:

* **The hash dropped the confidence components.** `output_hash()` excluded them
  entirely, so a component score, weight, `hard_failure` or `uncalibrated` flag
  could change without the replay hash moving. A determinism guarantee that
  ignores a deterministic output is not a guarantee.
* **Root stability compared only counts.** Two conventions could each find two
  roots at completely different places and be scored "stable".
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.domain.gex import (
    IVConvention,
    RootSelectionMethod,
    ZeroGammaResult,
)
from src.gex.confidence import (
    ConfidenceInputs,
    match_roots,
    score_root_identity_stability,
)
from src.gex.config import ConfidenceConfig
from src.gex.engine import compute_gex_snapshot
from src.synthetic.chains import build_synthetic_chain

CFG = ConfidenceConfig()


def snapshot():
    return compute_gex_snapshot(build_synthetic_chain())


def with_components(base, components):
    return replace(base, confidence=replace(base.confidence, components=components))


# =============================================================================
# §13 -- confidence structure inside the replay hash
# =============================================================================


def test_baseline_hash_is_stable():
    assert snapshot().output_hash() == snapshot().output_hash()


def test_changing_a_component_score_changes_the_hash():
    """v2 bug: components were excluded from the hash entirely."""
    base = snapshot()
    tweaked = with_components(
        base,
        (
            replace(base.confidence.components[0], score=0.123),
            *base.confidence.components[1:],
        ),
    )
    assert tweaked.output_hash() != base.output_hash()


def test_changing_a_component_weight_changes_the_hash():
    base = snapshot()
    tweaked = with_components(
        base,
        (
            replace(base.confidence.components[0], weight=0.99),
            *base.confidence.components[1:],
        ),
    )
    assert tweaked.output_hash() != base.output_hash()


def test_changing_hard_failure_changes_the_hash():
    base = snapshot()
    tweaked = with_components(
        base,
        (
            replace(base.confidence.components[0], hard_failure=True),
            *base.confidence.components[1:],
        ),
    )
    assert tweaked.output_hash() != base.output_hash()


def test_changing_uncalibrated_changes_the_hash():
    base = snapshot()
    first = base.confidence.components[0]
    tweaked = with_components(
        base,
        (
            replace(first, uncalibrated=not first.uncalibrated),
            *base.confidence.components[1:],
        ),
    )
    assert tweaked.output_hash() != base.output_hash()


def test_changing_the_final_score_changes_the_hash():
    base = snapshot()
    tweaked = replace(base, confidence=replace(base.confidence, value=12.5))
    assert tweaked.output_hash() != base.output_hash()


def test_free_form_component_prose_does_not_change_the_hash():
    """Detail strings are human explanation. A hash that trips on a reworded
    message is a hash nobody trusts, so wording is excluded by design -- while
    every deterministic numeric and state field is included.
    """
    base = snapshot()
    tweaked = with_components(
        base,
        tuple(
            replace(c, detail="completely different prose")
            for c in base.confidence.components
        ),
    )
    assert tweaked.output_hash() == base.output_hash()


def test_snapshot_level_warning_prose_does_not_change_the_hash():
    base = snapshot()
    assert replace(base, warnings=("reworded",)).output_hash() == base.output_hash()


def test_reordering_components_does_not_change_the_hash():
    """Semantically identical output must hash identically."""
    base = snapshot()
    reordered = with_components(base, tuple(reversed(base.confidence.components)))
    assert reordered.output_hash() == base.output_hash()


def test_identical_semantic_output_hashes_identically():
    assert compute_gex_snapshot(build_synthetic_chain()).output_hash() == (
        compute_gex_snapshot(build_synthetic_chain()).output_hash()
    )


def test_the_hash_payload_lists_every_component_by_name():
    base = snapshot()
    payload = base.hash_payload()
    names = {entry["name"] for entry in payload["confidence"]["components"]}
    assert names == {c.name for c in base.confidence.components}
    for entry in payload["confidence"]["components"]:
        assert set(entry) == {"name", "score", "weight", "hard_failure", "uncalibrated"}


def test_hard_failures_and_calibration_state_are_in_the_payload():
    payload = snapshot().hash_payload()
    assert "hard_failures" in payload["confidence"]
    assert "calibrated" in payload["confidence"]
    assert "score" in payload["confidence"]


def test_universe_and_zero_gamma_state_are_in_the_payload():
    payload = snapshot().hash_payload()
    assert payload["chain_universe"]["included_contract_count"] > 0
    assert "zero_gamma_universe" in payload
    assert payload["zero_gamma"]


def test_a_real_confidence_change_moves_the_hash_end_to_end():
    """The integration version: a genuinely worse snapshot must hash differently."""
    from src.synthetic.chains import SyntheticChainSpec

    fresh = compute_gex_snapshot(build_synthetic_chain())
    stale = compute_gex_snapshot(
        build_synthetic_chain(SyntheticChainSpec(quote_age_seconds=45.0))
    )
    assert stale.confidence.value != fresh.confidence.value
    assert stale.output_hash() != fresh.output_hash()


# =============================================================================
# §14 -- root topology
# =============================================================================


def root(
    convention=IVConvention.STICKY_STRIKE,
    *,
    roots=(5050.0,),
    selected=None,
    slope=5.0e8,
):
    chosen = selected if selected is not None else (roots[0] if roots else None)
    return ZeroGammaResult(
        convention=convention,
        selected_root=chosen,
        all_roots=tuple(roots),
        selection_method=(
            RootSelectionMethod.NEAREST_TO_SPOT
            if roots
            else RootSelectionMethod.NONE_FOUND
        ),
        grid_lower_bound=4800.0,
        grid_upper_bound=5200.0,
        grid_points=81,
        spot=5000.0,
        local_slope_at_selected_root=slope,
        max_abs_gex_on_grid=1.0e11,
        no_root_found=not roots,
    )


def inputs_with(results):
    from src.domain.gex import OptionUniverse
    from src.domain.model_spec import ModelSpec
    from src.domain.timestamps import DataQualityLimits
    from src.gex.formulas import compute_contract_gex
    from src.gex.sessions import eastern

    universe = OptionUniverse(
        total_contract_count=1,
        included_contract_count=1,
        included_unsigned_gex=1.0,
        excluded_unsigned_gex=0.0,
    )
    return ConfidenceInputs(
        as_of=eastern(2026, 3, 17, 11, 0),
        result=compute_contract_gex(build_synthetic_chain()),
        zero_gamma_results=tuple(results),
        spot=5000.0,
        dte0_dominance_ratio=0.3,
        model_spec=ModelSpec(),
        limits=DataQualityLimits(),
        chain_universe=universe,
        zero_gamma_universe=universe,
    )


# --- Matching ---------------------------------------------------------------


def test_identical_root_sets_match_completely():
    result = match_roots(
        (5000.0, 5100.0), (5000.0, 5100.0), spot=5000.0, tolerance_pct=0.5
    )
    assert result.matched_root_count == 2
    assert result.unmatched_root_count == 0
    assert result.maximum_matched_root_shift_pct == pytest.approx(0.0)


def test_small_movements_within_tolerance_still_match():
    result = match_roots((5000.0,), (5005.0,), spot=5000.0, tolerance_pct=0.5)
    assert result.matched_root_count == 1
    assert result.unmatched_root_count == 0
    assert result.maximum_matched_root_shift_pct == pytest.approx(0.1)


def test_a_large_shift_is_not_a_match():
    result = match_roots((5000.0,), (5300.0,), spot=5000.0, tolerance_pct=0.5)
    assert result.matched_root_count == 0
    assert result.unmatched_root_count == 2  # one on each side


def test_a_disappearing_root_is_unmatched():
    result = match_roots((5000.0, 5150.0), (5000.0,), spot=5000.0, tolerance_pct=0.5)
    assert result.matched_root_count == 1
    assert result.unmatched_root_count == 1


def test_an_appearing_root_is_unmatched():
    result = match_roots((5000.0,), (5000.0, 5150.0), spot=5000.0, tolerance_pct=0.5)
    assert result.matched_root_count == 1
    assert result.unmatched_root_count == 1


def test_matching_is_order_independent():
    forward = match_roots(
        (5000.0, 5150.0), (5150.0, 5000.0), spot=5000.0, tolerance_pct=0.5
    )
    assert forward.matched_root_count == 2
    assert forward.unmatched_root_count == 0


def test_near_identical_roots_match_deterministically():
    """Two roots a hair apart must not produce a coin-flip pairing."""
    left = (5000.0, 5000.4)
    right = (5000.1, 5000.5)
    first = match_roots(left, right, spot=5000.0, tolerance_pct=0.5)
    second = match_roots(left, right, spot=5000.0, tolerance_pct=0.5)
    assert first == second
    assert first.matched_root_count == 2


def test_each_root_is_matched_at_most_once():
    """Greedy nearest-match must not pair two lefts to the same right."""
    result = match_roots((5000.0, 5001.0), (5000.5,), spot=5000.0, tolerance_pct=0.5)
    assert result.matched_root_count == 1
    assert result.unmatched_root_count == 1


# --- Stability scoring ------------------------------------------------------


def test_exactly_identical_topology_scores_full_marks():
    component = score_root_identity_stability(
        inputs_with(
            [
                root(IVConvention.STICKY_STRIKE, roots=(5050.0,)),
                root(IVConvention.FROZEN_IV, roots=(5050.0,)),
            ]
        ),
        CFG,
    )
    assert component.score == pytest.approx(1.0)
    assert "root_topology_stable=True" in component.detail


def test_a_small_matched_drift_is_stable_but_slightly_penalised():
    """Proportionate, not binary. The roots still match -- the level is the same
    level -- but it did move, and a score of exactly 1.0 would say it had not.
    """
    component = score_root_identity_stability(
        inputs_with(
            [
                root(IVConvention.STICKY_STRIKE, roots=(5050.0,)),
                root(IVConvention.FROZEN_IV, roots=(5050.5,)),  # 0.01% of spot
            ]
        ),
        CFG,
    )
    assert 0.95 < component.score < 1.0
    assert "root_topology_stable=True" in component.detail


def test_a_shifted_secondary_root_is_penalised_despite_equal_counts():
    """v2 bug: this scored a perfect 1.0 because only the counts were compared."""
    component = score_root_identity_stability(
        inputs_with(
            [
                root(
                    IVConvention.STICKY_STRIKE, roots=(5050.0, 5150.0), selected=5050.0
                ),
                root(IVConvention.FROZEN_IV, roots=(5050.0, 4700.0), selected=5050.0),
            ]
        ),
        CFG,
    )
    assert component.score < 1.0


def test_a_disappearing_secondary_root_is_penalised():
    component = score_root_identity_stability(
        inputs_with(
            [
                root(
                    IVConvention.STICKY_STRIKE, roots=(5050.0, 5150.0), selected=5050.0
                ),
                root(IVConvention.FROZEN_IV, roots=(5050.0,), selected=5050.0),
            ]
        ),
        CFG,
    )
    assert component.score < 1.0
    assert "unmatched" in component.detail


def test_the_selected_root_changing_identity_is_penalised_strongly():
    """The strongest signal: the conventions disagree about which level matters."""
    shifted_secondary = score_root_identity_stability(
        inputs_with(
            [
                root(
                    IVConvention.STICKY_STRIKE, roots=(5050.0, 5150.0), selected=5050.0
                ),
                root(IVConvention.FROZEN_IV, roots=(5050.0, 4700.0), selected=5050.0),
            ]
        ),
        CFG,
    )
    selected_moved = score_root_identity_stability(
        inputs_with(
            [
                root(IVConvention.STICKY_STRIKE, roots=(5050.0,), selected=5050.0),
                root(IVConvention.FROZEN_IV, roots=(4700.0,), selected=4700.0),
            ]
        ),
        CFG,
    )
    assert selected_moved.score < shifted_secondary.score
    assert selected_moved.score == 0.0


def test_stability_reports_the_required_metrics():
    component = score_root_identity_stability(
        inputs_with(
            [
                root(
                    IVConvention.STICKY_STRIKE, roots=(5050.0, 5150.0), selected=5050.0
                ),
                root(IVConvention.FROZEN_IV, roots=(5050.0,), selected=5050.0),
            ]
        ),
        CFG,
    )
    for key in (
        "matched_root_count",
        "unmatched_root_count",
        "maximum_matched_root_shift_pct",
        "selected_root_identity_stable",
        "root_topology_stable",
    ):
        assert key in component.detail, key


def test_a_single_convention_cannot_demonstrate_stability():
    component = score_root_identity_stability(
        inputs_with([root(IVConvention.STICKY_STRIKE)]), CFG
    )
    assert component.score == 0.0
    assert "not comparable" in component.detail


def test_stability_is_exposed_on_the_snapshot():
    produced = snapshot()
    topology = produced.root_topology
    assert topology is not None
    assert topology["matched_root_count"] >= 1
    assert topology["selected_root_identity_stable"] is True
    assert topology["root_topology_stable"] is True


def test_topology_state_is_in_the_replay_hash():
    """Root topology is a deterministic diagnostic, so it belongs in the hash."""
    payload = snapshot().hash_payload()
    assert "root_topology" in payload
