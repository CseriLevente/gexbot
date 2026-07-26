"""Wall semantics, node ranking and gamma-void classification.

Two distinctions carry most of the weight here:

* **Observation vs interpretation.** The strike with the most call gamma is a
  fact; "resistance above" is a claim that is only true if the strike is above
  spot. The tests pin that the second never silently degrades into the first.
* **Low gamma vs absent data.** A region the vendor never sent looks identical to
  a quiet region unless it is checked against an expected strike ladder.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.domain.contracts import OptionRoot
from src.domain.gex import GammaVoidKind, StrikeGex
from src.gex.config import WallConfig
from src.gex.formulas import aggregate_by_strike, compute_contract_gex
from src.gex.walls import (
    StrikeLadder,
    classify_void,
    distance_pct,
    extract_walls,
    find_gamma_voids,
)
from src.synthetic.chains import (
    CALL_OI_PEAK_STRIKE,
    PUT_OI_PEAK_STRIKE,
    SyntheticChainSpec,
    build_synthetic_chain,
)

SPOT = 5000.0


def make_strike(
    strike: float,
    *,
    call: float = 0.0,
    put: float = 0.0,
    call_oi: int = 0,
    put_oi: int = 0,
) -> StrikeGex:
    return StrikeGex(
        strike=strike,
        call_gex=call,
        put_gex=put,
        unsigned_gex=call + put,
        signed_gex=call - put,
        call_open_interest=call_oi,
        put_open_interest=put_oi,
    )


def ladder(*strikes: float) -> StrikeLadder:
    return StrikeLadder.from_strikes(strikes)


# --- Neutral observations ---------------------------------------------------


def test_neutral_maxima_come_from_gamma_not_open_interest():
    """A far strike with huge OI but tiny gamma must not win."""
    strikes = (
        make_strike(4900.0, put=8.0, put_oi=1_000),
        make_strike(5000.0, call=3.0, put=3.0),
        make_strike(5100.0, call=9.0, call_oi=1_000),
        make_strike(5200.0, call=1.0, call_oi=500_000),  # OI trap
    )
    walls = extract_walls(strikes, spot=SPOT)
    assert walls.largest_call_gamma_strike == 5100.0
    assert walls.largest_put_gamma_strike == 4900.0
    assert walls.largest_unsigned_gamma_strike == 5100.0


def test_neutral_maxima_are_reported_regardless_of_where_spot_is():
    """They are observations, so they exist even when nothing qualifies as a
    directional wall.
    """
    strikes = (make_strike(4800.0, call=9.0), make_strike(4850.0, call=4.0))
    walls = extract_walls(strikes, spot=SPOT)
    assert walls.largest_call_gamma_strike == 4800.0
    assert walls.upside_call_wall is None


def test_ties_are_broken_deterministically_by_the_lower_strike():
    """``max()`` returns whichever equal element it met first, which depends on
    aggregation order. Replay determinism needs a stated rule.
    """
    strikes = (
        make_strike(5050.0, call=9.0),
        make_strike(5075.0, call=9.0),
        make_strike(5100.0, call=9.0),
    )
    assert extract_walls(strikes, spot=SPOT).largest_call_gamma_strike == 5050.0
    reversed_order = tuple(reversed(strikes))
    assert extract_walls(reversed_order, spot=SPOT).largest_call_gamma_strike == 5050.0


# --- Directional walls ------------------------------------------------------


def test_upside_wall_is_above_spot_and_downside_below():
    strikes = (
        make_strike(4900.0, put=8.0),
        make_strike(5100.0, call=9.0),
    )
    walls = extract_walls(strikes, spot=SPOT)
    assert walls.upside_call_wall == 5100.0
    assert walls.downside_put_wall == 4900.0


def test_all_call_gamma_below_spot_yields_no_upside_wall():
    """The requirement this test exists for: no silent same-side substitution.

    Reporting 4800 as "resistance" when the market is at 5000 would be worse than
    reporting nothing.
    """
    strikes = (
        make_strike(4700.0, call=5.0),
        make_strike(4800.0, call=9.0),
        make_strike(4900.0, call=7.0),
    )
    walls = extract_walls(strikes, spot=SPOT)
    assert walls.largest_call_gamma_strike == 4800.0
    assert walls.upside_call_wall is None


def test_all_put_gamma_above_spot_yields_no_downside_wall():
    strikes = (
        make_strike(5100.0, put=5.0),
        make_strike(5200.0, put=9.0),
    )
    walls = extract_walls(strikes, spot=SPOT)
    assert walls.largest_put_gamma_strike == 5200.0  # the observation still exists
    assert walls.downside_put_wall is None


def test_a_strike_exactly_at_spot_is_not_a_directional_wall():
    strikes = (make_strike(SPOT, call=9.0, put=9.0),)
    walls = extract_walls(strikes, spot=SPOT)
    assert walls.upside_call_wall is None
    assert walls.downside_put_wall is None


def test_minimum_distance_buffer_excludes_a_strike_hugging_spot():
    strikes = (make_strike(5001.0, call=9.0), make_strike(5100.0, call=4.0))
    tight = extract_walls(
        strikes, spot=SPOT, config=WallConfig(directional_wall_min_distance_pct=0.0)
    )
    assert tight.upside_call_wall == 5001.0
    buffered = extract_walls(
        strikes, spot=SPOT, config=WallConfig(directional_wall_min_distance_pct=0.01)
    )
    assert buffered.upside_call_wall == 5100.0


def test_directional_walls_pick_the_side_maximum_not_the_nearest_strike():
    strikes = (
        make_strike(5025.0, call=2.0),
        make_strike(5100.0, call=9.0),
        make_strike(5150.0, call=4.0),
    )
    assert extract_walls(strikes, spot=SPOT).upside_call_wall == 5100.0


def test_multiple_equal_maxima_on_one_side_resolve_to_the_lower_strike():
    strikes = (make_strike(5050.0, call=9.0), make_strike(5100.0, call=9.0))
    assert extract_walls(strikes, spot=SPOT).upside_call_wall == 5050.0


def test_sparse_chain_still_produces_what_it_can():
    """One strike each side: neutral maxima and directional walls all exist."""
    strikes = (make_strike(4500.0, put=3.0), make_strike(5400.0, call=3.0))
    walls = extract_walls(
        strikes,
        spot=SPOT,
        config=WallConfig(band_pct=0.2, directional_wall_band_pct=0.2),
    )
    assert walls.upside_call_wall == 5400.0
    assert walls.downside_put_wall == 4500.0


def test_far_wing_strikes_outside_the_band_are_ignored():
    strikes = (make_strike(5100.0, call=9.0), make_strike(6000.0, call=1_000.0))
    walls = extract_walls(strikes, spot=SPOT, config=WallConfig(band_pct=0.10))
    assert walls.largest_call_gamma_strike == 5100.0
    wide = extract_walls(
        strikes,
        spot=SPOT,
        config=WallConfig(band_pct=0.25, directional_wall_band_pct=0.25),
    )
    assert wide.largest_call_gamma_strike == 6000.0


def test_empty_or_gamma_free_input_yields_nothing():
    empty = extract_walls((), spot=SPOT)
    assert empty.largest_call_gamma_strike is None
    assert empty.upside_call_wall is None

    flat = extract_walls((make_strike(5000.0),), spot=SPOT)
    assert flat.largest_call_gamma_strike is None
    assert flat.largest_unsigned_gamma_strike is None


# --- Nodes ------------------------------------------------------------------


def test_nodes_are_split_by_sign_and_ranked_by_magnitude():
    strikes = (
        make_strike(4900.0, put=10.0),
        make_strike(4950.0, put=6.0),
        make_strike(5050.0, call=7.0),
        make_strike(5100.0, call=9.0),
    )
    walls = extract_walls(strikes, spot=SPOT)
    assert walls.positive_gamma_nodes == (5100.0, 5050.0)
    assert walls.negative_gamma_nodes == (4900.0, 4950.0)


def test_weak_strikes_are_excluded_from_nodes():
    strikes = (make_strike(5100.0, call=100.0), make_strike(5050.0, call=1.0))
    assert extract_walls(strikes, spot=SPOT).positive_gamma_nodes == (5100.0,)


def test_node_count_is_capped_per_side():
    strikes = tuple(make_strike(5000.0 + 10.0 * i, call=100.0 - i) for i in range(1, 9))
    walls = extract_walls(strikes, spot=SPOT, config=WallConfig(max_nodes_per_side=3))
    assert len(walls.positive_gamma_nodes) == 3


def test_equal_magnitude_nodes_are_ordered_by_strike():
    strikes = (
        make_strike(5100.0, call=9.0),
        make_strike(5050.0, call=9.0),
    )
    assert extract_walls(strikes, spot=SPOT).positive_gamma_nodes == (5050.0, 5100.0)


# --- Strike ladder ----------------------------------------------------------


def test_ladder_infers_the_modal_spacing():
    assert ladder(4900.0, 4925.0, 4950.0, 4975.0).modal_spacing == 25.0


def test_ladder_spacing_is_robust_to_a_missing_strike():
    """Median rather than mean: one hole must not move the inferred ladder."""
    assert ladder(4900.0, 4925.0, 4975.0, 5000.0, 5025.0).modal_spacing == 25.0


def test_ladder_reports_no_spacing_for_too_few_strikes():
    assert ladder(5000.0).modal_spacing is None
    assert ladder(5000.0, 5025.0).modal_spacing is None


def test_ladder_expected_count_between():
    full = ladder(*[4900.0 + 25.0 * i for i in range(9)])
    assert full.expected_count_between(4900.0, 5000.0) == 5
    assert full.coverage_between(4900.0, 5000.0) == pytest.approx(1.0)


# --- Void classification ----------------------------------------------------


def test_a_complete_low_gamma_ladder_is_a_true_void():
    strikes = (
        make_strike(4900.0, put=100.0),
        make_strike(4925.0, put=0.5),
        make_strike(4950.0, put=0.5),
        make_strike(4975.0, put=0.5),
        make_strike(5000.0, call=100.0),
    )
    voids = find_gamma_voids(strikes, spot=SPOT, config=WallConfig())
    assert len(voids) == 1
    void = voids[0]
    assert void.kind is GammaVoidKind.TRUE_LOW_GEX_VOID
    assert void.is_tradable_structure
    assert (void.low_strike, void.high_strike) == (4925.0, 4975.0)
    assert void.missing_strike_count == 0


def test_a_region_the_vendor_never_sent_is_not_a_tradable_void():
    """The core requirement: absence of data must not read as absence of gamma.

    The ladder says 25-point strikes, so 4925/4950 are expected but missing. What
    remains looks quiet only because nothing was received for it.
    """
    strikes = (
        make_strike(4850.0, put=100.0),
        make_strike(4875.0, put=100.0),
        make_strike(4900.0, put=0.5),
        # 4925, 4950 omitted by the vendor
        make_strike(4975.0, put=0.5),
        make_strike(5000.0, call=100.0),
        make_strike(5025.0, call=100.0),
    )
    voids = find_gamma_voids(strikes, spot=SPOT, config=WallConfig())
    assert len(voids) == 1
    void = voids[0]
    assert void.kind is GammaVoidKind.MISSING_STRIKE_DATA
    assert not void.is_tradable_structure
    assert void.missing_strike_count == 2
    assert "absence of data" in void.detail


def test_irregular_strike_spacing_is_classified_as_such():
    """SPX genuinely widens its increment in the wings; that is the ladder, not
    an omission.
    """
    run = (
        make_strike(4400.0, put=0.4),
        make_strike(4500.0, put=0.4),
        make_strike(4600.0, put=0.4),
    )
    tight_ladder = ladder(4900.0, 4925.0, 4950.0, 4975.0, 5000.0)
    kind, detail, _ = classify_void(run=run, ladder=tight_ladder, config=WallConfig())
    assert kind is GammaVoidKind.IRREGULAR_STRIKE_SPACING
    assert "coarser increment" in detail


def test_a_single_wide_gap_is_an_omission_not_a_coarser_ladder():
    """One gap is not enough evidence that the increment changed.

    Reporting it as irregular would strip the missing-data warning, so the safe
    classification wins.
    """
    run = (make_strike(4900.0, put=0.4), make_strike(4975.0, put=0.4))
    kind, _, missing = classify_void(
        run=run,
        ladder=ladder(*[4850.0 + 25.0 * i for i in range(8)]),
        config=WallConfig(),
    )
    assert kind is GammaVoidKind.MISSING_STRIKE_DATA
    assert missing == 2


def test_insufficient_coverage_when_the_ladder_cannot_be_inferred():
    run = (make_strike(4900.0, put=0.1), make_strike(4950.0, put=0.1))
    kind, detail, _ = classify_void(run=run, ladder=ladder(4900.0), config=WallConfig())
    assert kind is GammaVoidKind.INSUFFICIENT_COVERAGE
    assert "expected ladder" in detail


def test_explicitly_filtered_regions_are_labelled():
    run = (make_strike(4900.0, put=0.1), make_strike(4950.0, put=0.1))
    kind, _, _ = classify_void(
        run=run,
        ladder=ladder(4900.0, 4925.0, 4950.0),
        config=WallConfig(),
        filtered_region=True,
    )
    assert kind is GammaVoidKind.FILTERED_STRIKE_REGION


def test_low_but_non_zero_gamma_still_counts_as_a_true_void():
    """A void is defined relative to the chain's own maximum, not against zero."""
    strikes = (
        make_strike(4900.0, put=1000.0),
        make_strike(4925.0, put=10.0),
        make_strike(4950.0, put=12.0),
        make_strike(4975.0, put=9.0),
        make_strike(5000.0, call=1000.0),
    )
    voids = find_gamma_voids(strikes, spot=SPOT, config=WallConfig())
    assert voids[0].kind is GammaVoidKind.TRUE_LOW_GEX_VOID
    assert voids[0].max_unsigned_gex_in_range == pytest.approx(12.0)


def test_a_single_low_gamma_strike_is_not_a_void():
    strikes = (
        make_strike(4900.0, put=100.0),
        make_strike(4950.0, put=0.5),
        make_strike(5000.0, call=100.0),
    )
    assert find_gamma_voids(strikes, spot=SPOT, config=WallConfig()) == ()


def test_a_run_narrower_than_the_minimum_width_is_rejected():
    strikes = (
        make_strike(4900.0, put=100.0),
        make_strike(4901.0, put=0.5),
        make_strike(4902.0, put=0.5),
        make_strike(5000.0, call=100.0),
    )
    assert find_gamma_voids(strikes, spot=SPOT, config=WallConfig()) == ()


def test_two_separate_voids_are_reported_separately():
    strikes = (
        make_strike(4800.0, put=100.0),
        make_strike(4825.0, put=0.1),
        make_strike(4850.0, put=0.1),
        make_strike(4875.0, put=100.0),
        make_strike(4900.0, put=0.1),
        make_strike(4925.0, put=0.1),
        make_strike(4950.0, call=100.0),
    )
    voids = find_gamma_voids(strikes, spot=SPOT, config=WallConfig())
    assert [(v.low_strike, v.high_strike) for v in voids] == [
        (4825.0, 4850.0),
        (4900.0, 4925.0),
    ]


def test_no_voids_when_gamma_is_uniform():
    strikes = tuple(make_strike(4900.0 + 25.0 * i, call=50.0) for i in range(9))
    assert find_gamma_voids(strikes, spot=SPOT, config=WallConfig()) == ()


def test_only_true_voids_appear_in_the_tradable_list():
    strikes = (
        make_strike(4850.0, put=100.0),
        make_strike(4875.0, put=100.0),
        make_strike(4900.0, put=0.5),
        make_strike(4975.0, put=0.5),
        make_strike(5000.0, call=100.0),
    )
    walls = extract_walls(strikes, spot=SPOT)
    assert walls.gamma_voids
    assert walls.tradable_voids == ()


def test_vendor_omission_on_the_synthetic_chain_is_detected_end_to_end():
    """Simulated vendor coverage hole, straight through the engine."""
    spec = SyntheticChainSpec(omit_strikes=(4900.0, 4925.0, 4950.0))
    chain = build_synthetic_chain(spec)
    strikes = aggregate_by_strike(compute_contract_gex(chain).contracts)
    assert 4925.0 not in {s.strike for s in strikes}
    ladder_from_chain = StrikeLadder.from_strikes(tuple(s.strike for s in strikes))
    assert ladder_from_chain.modal_spacing == 25.0
    assert ladder_from_chain.coverage_between(4875.0, 4975.0) is not None


# --- Distance helper --------------------------------------------------------


def test_distance_pct_is_signed_relative_to_spot():
    assert distance_pct(5000.0, 5100.0) == pytest.approx(2.0)
    assert distance_pct(5000.0, 4900.0) == pytest.approx(-2.0)
    assert distance_pct(5000.0, None) is None


# --- Against the synthetic chain -------------------------------------------


def test_synthetic_chain_places_directional_walls_on_the_correct_sides(snapshot):
    walls = snapshot.walls
    assert walls.upside_call_wall is not None
    assert walls.upside_call_wall > snapshot.spot
    assert walls.downside_put_wall is not None
    assert walls.downside_put_wall < snapshot.spot


def test_far_dated_chain_puts_maxima_exactly_at_the_open_interest_peaks():
    """Where gamma is flat across the band, the OI bumps decide the maxima."""
    chain = build_synthetic_chain(
        SyntheticChainSpec(expiries=((OptionRoot.SPXW, date(2026, 5, 15)),))
    )
    strikes = aggregate_by_strike(compute_contract_gex(chain).contracts)
    walls = extract_walls(strikes, spot=chain.spot)
    assert walls.largest_call_gamma_strike == pytest.approx(CALL_OI_PEAK_STRIKE)
    assert walls.largest_put_gamma_strike == pytest.approx(PUT_OI_PEAK_STRIKE)
