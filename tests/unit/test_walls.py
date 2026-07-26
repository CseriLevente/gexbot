"""Wall, node and void extraction."""

from __future__ import annotations

from datetime import date

import pytest

from src.domain.contracts import OptionRoot
from src.domain.gex import StrikeGex
from src.gex.config import WallConfig
from src.gex.formulas import aggregate_by_strike, compute_contract_gex
from src.gex.walls import distance_pct, extract_walls, find_gamma_voids
from tests.fixtures.chains import (
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


# --- Wall selection ---------------------------------------------------------


def test_walls_come_from_hand_built_gamma_not_open_interest():
    """A far strike with huge OI but tiny gamma must not become the call wall."""
    strikes = (
        make_strike(4900.0, put=8.0, put_oi=1_000),
        make_strike(5000.0, call=3.0, put=3.0),
        make_strike(5100.0, call=9.0, call_oi=1_000),
        make_strike(5200.0, call=1.0, call_oi=500_000),  # OI trap
    )
    walls = extract_walls(strikes, spot=SPOT)
    assert walls.call_wall == 5100.0
    assert walls.put_wall == 4900.0
    assert walls.largest_abs_gamma_strike == 5100.0


def test_far_wing_strikes_outside_the_band_are_ignored():
    """A 20%-away strike cannot be a wall, however much gamma it carries."""
    strikes = (
        make_strike(5100.0, call=9.0),
        make_strike(6000.0, call=1_000.0),
    )
    walls = extract_walls(strikes, spot=SPOT, config=WallConfig(band_pct=0.10))
    assert walls.call_wall == 5100.0
    # Widening the band lets it back in, proving the band is what excluded it.
    wide = extract_walls(strikes, spot=SPOT, config=WallConfig(band_pct=0.25))
    assert wide.call_wall == 6000.0


def test_empty_or_gamma_free_input_yields_no_walls():
    empty = extract_walls((), spot=SPOT)
    assert empty.call_wall is None and empty.put_wall is None
    assert empty.largest_abs_gamma_strike is None

    flat = extract_walls((make_strike(5000.0),), spot=SPOT)
    assert flat.call_wall is None
    assert flat.largest_abs_gamma_strike is None


def test_side_specific_walls_are_independent():
    """Put-only gamma must give a put wall and no call wall."""
    walls = extract_walls(
        (make_strike(4900.0, put=5.0), make_strike(4950.0, put=9.0)), spot=SPOT
    )
    assert walls.put_wall == 4950.0
    assert walls.call_wall is None


# --- Nodes ------------------------------------------------------------------


def test_nodes_are_split_by_sign_and_ranked_by_magnitude():
    strikes = (
        make_strike(4900.0, put=10.0),  # signed -10
        make_strike(4950.0, put=6.0),  # signed -6
        make_strike(5050.0, call=7.0),  # signed +7
        make_strike(5100.0, call=9.0),  # signed +9
    )
    walls = extract_walls(strikes, spot=SPOT)
    assert walls.positive_gamma_nodes == (5100.0, 5050.0)
    assert walls.negative_gamma_nodes == (4900.0, 4950.0)


def test_weak_strikes_are_excluded_from_nodes():
    strikes = (
        make_strike(5100.0, call=100.0),
        make_strike(5050.0, call=1.0),  # 1% of max -- below the 25% floor
    )
    walls = extract_walls(strikes, spot=SPOT)
    assert walls.positive_gamma_nodes == (5100.0,)


def test_node_count_is_capped_per_side():
    strikes = tuple(
        make_strike(5000.0 + 10.0 * i, call=100.0 - i) for i in range(1, 9)
    )
    walls = extract_walls(strikes, spot=SPOT, config=WallConfig(max_nodes_per_side=3))
    assert len(walls.positive_gamma_nodes) == 3


# --- Voids ------------------------------------------------------------------


def test_a_contiguous_low_gamma_run_is_reported_as_a_void():
    strikes = (
        make_strike(4900.0, put=100.0),
        make_strike(4925.0, put=0.5),
        make_strike(4950.0, put=0.5),
        make_strike(4975.0, put=0.5),
        make_strike(5000.0, call=100.0),
    )
    voids = find_gamma_voids(strikes, spot=SPOT, config=WallConfig())
    assert len(voids) == 1
    assert voids[0].low_strike == 4925.0
    assert voids[0].high_strike == 4975.0
    assert voids[0].width == 50.0
    assert voids[0].max_unsigned_gex_in_range == pytest.approx(0.5)


def test_a_single_low_gamma_strike_is_not_a_void():
    """One quiet strike is noise; a void has to be traversable space."""
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
    # min width is 0.2% of spot = 10 points; this run spans 1 point.
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


# --- Distance helper --------------------------------------------------------


def test_distance_pct_is_signed_relative_to_spot():
    assert distance_pct(5000.0, 5100.0) == pytest.approx(2.0)
    assert distance_pct(5000.0, 4900.0) == pytest.approx(-2.0)
    assert distance_pct(5000.0, 5000.0) == pytest.approx(0.0)
    assert distance_pct(5000.0, None) is None


# --- Against the synthetic chain -------------------------------------------


def test_synthetic_chain_places_walls_on_the_expected_sides(snapshot):
    walls = snapshot.walls
    assert walls.call_wall is not None and walls.call_wall > snapshot.spot
    assert walls.put_wall is not None and walls.put_wall < snapshot.spot
    assert walls.largest_abs_gamma_strike is not None


def test_far_dated_chain_puts_walls_exactly_at_the_open_interest_peaks():
    """Where gamma is flat across the band, the walls land on the OI bumps -- the
    end-to-end version of the same check in test_formulas.
    """
    chain = build_synthetic_chain(
        SyntheticChainSpec(expiries=((OptionRoot.SPXW, date(2026, 5, 15)),))
    )
    strikes = aggregate_by_strike(compute_contract_gex(chain).contracts)
    walls = extract_walls(strikes, spot=chain.spot)
    assert walls.call_wall == pytest.approx(CALL_OI_PEAK_STRIKE)
    assert walls.put_wall == pytest.approx(PUT_OI_PEAK_STRIKE)
