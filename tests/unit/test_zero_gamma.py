"""View 5: the zero-gamma spot grid.

The most model-sensitive part of the engine, so the tests are about behaviour
that must hold under *any* correct implementation: the grid contains spot, the
interpolated root sits between its bracketing grid points, the curve responds to
the fixture's open-interest bumps in the right direction, and unresolvable cases
say so instead of guessing.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.domain.contracts import OptionRoot
from src.domain.gex import IVConvention
from src.gex.config import GexEngineConfig, ZeroGammaConfig
from src.gex.formulas import compute_contract_gex, total_signed_gex
from src.gex.zero_gamma import (
    SmileFit,
    build_spot_grid,
    compute_zero_gamma,
    find_sign_change_roots,
    fit_smile,
    fit_smiles_by_expiry,
    signed_gex_curve,
    standardised_moneyness,
)
from tests.fixtures.chains import (
    CALL_OI_PEAK_STRIKE,
    PUT_OI_PEAK_STRIKE,
    SyntheticChainSpec,
    build_synthetic_chain,
    synthetic_iv,
)

SPOT = 5000.0


# --- Grid construction ------------------------------------------------------


def test_grid_is_symmetric_and_contains_spot():
    grid = build_spot_grid(SPOT, span_pct=0.04, step_pct=0.001)
    assert len(grid) == 81
    assert grid[0] == pytest.approx(SPOT * 0.96)
    assert grid[-1] == pytest.approx(SPOT * 1.04)
    assert grid[len(grid) // 2] == pytest.approx(SPOT)
    assert min(abs(point - SPOT) for point in grid) == pytest.approx(0.0)


def test_grid_rejects_non_positive_parameters():
    for span, step in ((0.0, 0.001), (0.04, 0.0), (-0.04, 0.001)):
        with pytest.raises(ValueError):
            build_spot_grid(SPOT, span_pct=span, step_pct=step)


# --- Root finding -----------------------------------------------------------


def test_root_is_linearly_interpolated_between_bracketing_points():
    # From -1 at 100 to +3 at 104, the crossing sits a quarter of the way in.
    assert find_sign_change_roots([(100.0, -1.0), (104.0, 3.0)]) == [
        pytest.approx(101.0)
    ]


def test_exact_zero_on_a_grid_point_is_reported_once():
    assert find_sign_change_roots([(99.0, -1.0), (100.0, 0.0), (101.0, 1.0)]) == [100.0]


def test_no_crossing_yields_no_roots():
    assert find_sign_change_roots([(100.0, 1.0), (101.0, 2.0), (102.0, 3.0)]) == []
    assert find_sign_change_roots([(100.0, -1.0), (101.0, -2.0)]) == []


def test_multiple_crossings_are_all_reported():
    curve = [(100.0, -1.0), (101.0, 1.0), (102.0, -1.0), (103.0, 1.0)]
    assert len(find_sign_change_roots(curve)) == 3


def test_trailing_zero_endpoint_is_captured():
    assert find_sign_change_roots([(100.0, -1.0), (101.0, 0.0)]) == [
        pytest.approx(101.0)
    ]


# --- Smile fitting ----------------------------------------------------------


def test_quadratic_smile_is_recovered_exactly_from_noise_free_points():
    truth = SmileFit(a=0.20, b=-0.03, c=0.05)
    points = [(m, truth.at(m)) for m in (-0.4, -0.2, 0.0, 0.2, 0.4)]
    fitted = fit_smile(points)
    assert fitted is not None
    assert fitted.a == pytest.approx(truth.a, abs=1e-9)
    assert fitted.b == pytest.approx(truth.b, abs=1e-9)
    assert fitted.c == pytest.approx(truth.c, abs=1e-9)


def test_smile_output_is_clamped_to_the_usable_vol_range():
    """A quadratic extrapolates badly in the wings; clamping keeps the pricer
    inside the range where its inputs mean something.
    """
    steep = SmileFit(a=0.20, b=0.0, c=-10.0)
    assert steep.at(5.0) > 0.0
    assert steep.at(0.0) == pytest.approx(0.20)
    exploding = SmileFit(a=0.20, b=0.0, c=1_000.0)
    assert exploding.at(5.0) <= 5.0


def test_smile_fit_needs_at_least_three_points():
    assert fit_smile([(0.0, 0.2), (0.1, 0.21)]) is None


def test_degenerate_points_do_not_crash_the_solver():
    """All-identical moneyness makes the normal equations singular."""
    assert fit_smile([(0.0, 0.2)] * 5) is None


def test_moneyness_is_zero_at_the_money_and_signed_by_side():
    assert standardised_moneyness(5000.0, 5000.0, 0.25) == pytest.approx(0.0)
    assert standardised_moneyness(5200.0, 5000.0, 0.25) > 0.0
    assert standardised_moneyness(4800.0, 5000.0, 0.25) < 0.0


def test_smiles_are_fitted_per_expiry_and_recover_the_fixture_skew():
    chain = build_synthetic_chain()
    contracts = compute_contract_gex(chain).contracts
    fits = fit_smiles_by_expiry(contracts, spot=chain.spot, min_points=5)
    assert len(fits) == len(chain.expiries)
    # The fixture smile is downward-sloping in log-moneyness, so every fitted
    # curve must price the put wing above the call wing.
    for fit in fits.values():
        assert fit.at(-0.5) > fit.at(0.5)


def test_am_and_pm_series_sharing_an_expiry_date_get_separate_smiles():
    """SPX and SPXW can both expire on the third Friday but settle hours apart.
    Pooling them would blend two different time-to-expiry surfaces.
    """
    chain = build_synthetic_chain(
        SyntheticChainSpec(
            expiries=(
                (OptionRoot.SPX, date(2026, 3, 20)),
                (OptionRoot.SPXW, date(2026, 3, 20)),
            )
        )
    )
    contracts = compute_contract_gex(chain).contracts
    fits = fit_smiles_by_expiry(contracts, spot=chain.spot, min_points=5)
    assert len(fits) == 2


# --- The curve --------------------------------------------------------------


def test_curve_at_spot_reproduces_the_direct_signed_total():
    """Sanity bridge between view 2 and view 5: evaluated at the current spot, the
    grid must return the same number the plain aggregation does.
    """
    chain = build_synthetic_chain(SyntheticChainSpec(vendor_gamma=False))
    contracts = compute_contract_gex(chain).contracts
    eligible = tuple(c for c in contracts if c.dte <= ZeroGammaConfig().max_dte_for_grid)
    curve = signed_gex_curve(
        eligible,
        grid=[chain.spot],
        base_spot=chain.spot,
        convention=IVConvention.FROZEN_IV,
        spot_move_pct=0.01,
        risk_free_rate=chain.risk_free_rate,
        dividend_yield=chain.dividend_yield,
    )
    assert curve[0][1] == pytest.approx(total_signed_gex(eligible), rel=1e-9)


def _fixture_curve(step_pct: float = 0.005) -> list[tuple[float, float]]:
    chain = build_synthetic_chain()
    contracts = compute_contract_gex(chain).contracts
    return signed_gex_curve(
        tuple(c for c in contracts if c.dte <= 60),
        grid=build_spot_grid(chain.spot, span_pct=0.04, step_pct=step_pct),
        base_spot=chain.spot,
        convention=IVConvention.FROZEN_IV,
        spot_move_pct=0.01,
    )


def test_signed_gex_troughs_near_the_put_open_interest_peak():
    """The curve is U-shaped, not monotone, and the trough sits at the put bump.

    Put gamma contribution is largest when spot is *at* the put strikes, so below
    4900 the book becomes less negative again. This is why a zero-gamma search
    must count crossings instead of assuming a single monotone flip -- a book with
    bumps on both sides can genuinely cross twice.
    """
    curve = _fixture_curve()
    trough_spot = min(curve, key=lambda point: point[1])[0]
    assert trough_spot == pytest.approx(PUT_OI_PEAK_STRIKE, abs=50.0)


def test_curve_rises_monotonically_between_the_two_open_interest_peaks():
    """Between the put bump and the call bump -- the region containing the
    crossing -- signed GEX increases with spot: put gamma decays as price leaves
    the puts behind, call gamma builds as it approaches the calls. A break here
    means the sign handling in the grid is wrong.
    """
    curve = [
        point
        for point in _fixture_curve()
        if PUT_OI_PEAK_STRIKE <= point[0] <= CALL_OI_PEAK_STRIKE
    ]
    values = [value for _, value in curve]
    assert values == sorted(values), values
    assert values[0] < 0.0 < values[-1]


def test_curve_turns_back_down_above_the_call_open_interest_peak():
    """The mirror image of the put trough. Together with it the curve is
    double-humped, so "signed GEX is monotone in spot" is false in general --
    which is exactly why the search counts crossings instead of assuming one.
    """
    curve = _fixture_curve()
    peak_spot = max(curve, key=lambda point: point[1])[0]
    assert peak_spot == pytest.approx(CALL_OI_PEAK_STRIKE, abs=50.0)
    above = [value for spot, value in curve if spot >= peak_spot]
    assert above == sorted(above, reverse=True), above


# --- End to end -------------------------------------------------------------


@pytest.mark.parametrize(
    "convention",
    [IVConvention.FROZEN_IV, IVConvention.STICKY_STRIKE, IVConvention.STICKY_DELTA],
)
def test_every_implemented_convention_resolves_a_crossing(convention):
    chain = build_synthetic_chain()
    contracts = compute_contract_gex(chain).contracts
    result = compute_zero_gamma(
        contracts,
        spot=chain.spot,
        convention=convention,
        spot_move_pct=0.01,
        risk_free_rate=chain.risk_free_rate,
        dividend_yield=chain.dividend_yield,
    )
    assert result.resolved, result
    assert not result.no_crossing
    assert result.sign_changes == 1
    assert result.grid_low < result.zero_gamma_spot < result.grid_high
    # Put-heavy book: signed GEX is negative at spot, so the flip is above it.
    assert result.zero_gamma_spot > chain.spot


def test_surface_refit_is_declared_unimplemented_rather_than_faked():
    chain = build_synthetic_chain()
    contracts = compute_contract_gex(chain).contracts
    result = compute_zero_gamma(
        contracts,
        spot=chain.spot,
        convention=IVConvention.SURFACE_REFIT,
        spot_move_pct=0.01,
    )
    assert result.zero_gamma_spot is None
    assert result.no_crossing
    assert result.curve == ()


def test_sticky_delta_disagrees_with_sticky_strike_on_a_skewed_smile():
    """The disagreement is the error bar the confidence score consumes. If a
    translating smile gave the identical level, the convention machinery would be
    doing nothing.
    """
    chain = build_synthetic_chain()
    contracts = compute_contract_gex(chain).contracts
    levels = {}
    for convention in (IVConvention.STICKY_STRIKE, IVConvention.STICKY_DELTA):
        levels[convention] = compute_zero_gamma(
            contracts,
            spot=chain.spot,
            convention=convention,
            spot_move_pct=0.01,
            risk_free_rate=chain.risk_free_rate,
            dividend_yield=chain.dividend_yield,
        ).zero_gamma_spot
    assert levels[IVConvention.STICKY_STRIKE] != levels[IVConvention.STICKY_DELTA]


def test_sticky_strike_tracks_frozen_iv_closely_on_a_smooth_smile():
    """Both hold IV at the strike; sticky_strike just reads it off a fit. On a
    noise-free fixture smile they should nearly coincide -- that is what makes
    sticky_strike the denoised sibling rather than a different model.
    """
    chain = build_synthetic_chain()
    contracts = compute_contract_gex(chain).contracts
    frozen, sticky = (
        compute_zero_gamma(
            contracts,
            spot=chain.spot,
            convention=convention,
            spot_move_pct=0.01,
            risk_free_rate=chain.risk_free_rate,
            dividend_yield=chain.dividend_yield,
        ).zero_gamma_spot
        for convention in (IVConvention.FROZEN_IV, IVConvention.STICKY_STRIKE)
    )
    assert frozen is not None and sticky is not None
    assert abs(frozen - sticky) / chain.spot < 0.005  # within 0.5% of spot


def test_no_crossing_is_reported_when_the_book_is_one_sided():
    """Calls only: signed GEX is positive everywhere, so there is no zero-gamma
    level. Reporting that beats extrapolating one outside the grid.
    """
    chain = build_synthetic_chain()
    calls_only = tuple(
        c
        for c in compute_contract_gex(chain).contracts
        if c.sign > 0.0
    )
    result = compute_zero_gamma(
        calls_only, spot=chain.spot, convention=IVConvention.FROZEN_IV, spot_move_pct=0.01
    )
    assert result.zero_gamma_spot is None
    assert result.no_crossing
    assert result.sign_changes == 0


def test_empty_contract_set_produces_a_flat_unresolved_curve():
    """Regression: an all-zero curve used to report a root at every grid point,
    and the nearest-to-spot pick then returned spot itself -- a fabricated level
    that looked entirely reasonable downstream.
    """
    result = compute_zero_gamma(
        (), spot=SPOT, convention=IVConvention.FROZEN_IV, spot_move_pct=0.01
    )
    assert result.no_crossing
    assert result.zero_gamma_spot is None
    assert result.sign_changes == 0
    assert all(value == 0.0 for _, value in result.curve)


def test_all_zero_curve_has_no_roots():
    assert find_sign_change_roots([(100.0, 0.0), (101.0, 0.0), (102.0, 0.0)]) == []


def test_grid_excludes_contracts_beyond_max_dte_for_grid():
    chain = build_synthetic_chain()
    contracts = compute_contract_gex(chain).contracts
    narrow = compute_zero_gamma(
        contracts,
        spot=chain.spot,
        convention=IVConvention.FROZEN_IV,
        spot_move_pct=0.01,
        config=ZeroGammaConfig(max_dte_for_grid=2),
    )
    wide = compute_zero_gamma(
        contracts,
        spot=chain.spot,
        convention=IVConvention.FROZEN_IV,
        spot_move_pct=0.01,
        config=ZeroGammaConfig(max_dte_for_grid=60),
    )
    assert narrow.zero_gamma_spot != wide.zero_gamma_spot


def test_result_is_deterministic_across_repeated_runs():
    """Replay determinism, checked at the level that has the most floating-point
    surface area.
    """
    chain = build_synthetic_chain()
    contracts = compute_contract_gex(chain).contracts
    runs = [
        compute_zero_gamma(
            contracts,
            spot=chain.spot,
            convention=IVConvention.STICKY_DELTA,
            spot_move_pct=0.01,
            risk_free_rate=chain.risk_free_rate,
            dividend_yield=chain.dividend_yield,
        )
        for _ in range(3)
    ]
    assert len({run.zero_gamma_spot for run in runs}) == 1
    assert len({run.curve for run in runs}) == 1


def test_flat_smile_collapses_sticky_delta_onto_sticky_strike():
    """Control experiment: with no skew there is nothing for a translating smile
    to change, so the two conventions must agree.
    """
    flat = SyntheticChainSpec(iv_skew=0.0, iv_curvature=0.0)
    chain = build_synthetic_chain(flat)
    assert synthetic_iv(4800.0, flat) == pytest.approx(synthetic_iv(5200.0, flat))
    contracts = compute_contract_gex(chain).contracts
    levels = [
        compute_zero_gamma(
            contracts,
            spot=chain.spot,
            convention=convention,
            spot_move_pct=0.01,
            risk_free_rate=chain.risk_free_rate,
            dividend_yield=chain.dividend_yield,
        ).zero_gamma_spot
        for convention in (IVConvention.STICKY_STRIKE, IVConvention.STICKY_DELTA)
    ]
    assert levels[0] == pytest.approx(levels[1], rel=1e-9)


def test_engine_config_runs_three_conventions_by_default():
    assert len(GexEngineConfig().zero_gamma.conventions) == 3
    assert IVConvention.SURFACE_REFIT not in GexEngineConfig().zero_gamma.conventions
