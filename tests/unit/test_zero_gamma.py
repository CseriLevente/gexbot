"""View 5: the zero-gamma spot grid and its diagnostics.

The most model-sensitive part of the engine, so the tests target the properties
that must hold under *any* correct implementation: the grid contains spot, every
root is retained rather than only the selected one, the interpolated root sits
between its bracketing grid points, slope and boundary status are measured, and
unresolvable cases say so instead of guessing.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.domain.contracts import OptionRoot
from src.domain.gex import IVConvention, RootSelectionMethod
from src.gex.config import GexEngineConfig, ZeroGammaConfig
from src.gex.formulas import compute_contract_gex, total_signed_gex
from src.gex.zero_gamma import (
    SmileFit,
    build_spot_grid,
    compute_zero_gamma,
    find_sign_change_roots,
    fit_smile,
    fit_smiles_by_expiry,
    local_slope_at,
    signed_gex_curve,
    standardised_moneyness,
)
from src.synthetic.chains import (
    CALL_OI_PEAK_STRIKE,
    PUT_OI_PEAK_STRIKE,
    SyntheticChainSpec,
    build_synthetic_chain,
    synthetic_iv,
)

SPOT = 5000.0


def contracts_from(spec: SyntheticChainSpec | None = None):
    chain = build_synthetic_chain(spec)
    config = GexEngineConfig(model_spec=(spec or SyntheticChainSpec()).model_spec())
    return chain, compute_contract_gex(chain, config).contracts


def run(convention: IVConvention, spec: SyntheticChainSpec | None = None, **kwargs):
    chain, contracts = contracts_from(spec)
    return compute_zero_gamma(
        contracts,
        spot=chain.spot,
        convention=convention,
        spot_move_pct=0.01,
        risk_free_rate=chain.risk_free_rate,
        dividend_yield=chain.dividend_yield,
        **kwargs,
    )


# --- Grid construction ------------------------------------------------------


def test_grid_is_symmetric_and_contains_spot():
    grid = build_spot_grid(SPOT, span_pct=0.04, step_pct=0.001)
    assert len(grid) == 81
    assert grid[0] == pytest.approx(SPOT * 0.96)
    assert grid[-1] == pytest.approx(SPOT * 1.04)
    assert min(abs(point - SPOT) for point in grid) == pytest.approx(0.0)


def test_grid_rejects_non_positive_parameters():
    for span, step in ((0.0, 0.001), (0.04, 0.0), (-0.04, 0.001)):
        with pytest.raises(ValueError):
            build_spot_grid(SPOT, span_pct=span, step_pct=step)


# --- Root finding -----------------------------------------------------------


def test_root_is_linearly_interpolated_between_bracketing_points():
    assert find_sign_change_roots([(100.0, -1.0), (104.0, 3.0)]) == [
        pytest.approx(101.0)
    ]


def test_exact_zero_on_a_grid_point_is_reported_once():
    assert find_sign_change_roots([(99.0, -1.0), (100.0, 0.0), (101.0, 1.0)]) == [100.0]


def test_no_crossing_yields_no_roots():
    assert find_sign_change_roots([(100.0, 1.0), (101.0, 2.0), (102.0, 3.0)]) == []
    assert find_sign_change_roots([(100.0, -1.0), (101.0, -2.0)]) == []


def test_all_zero_curve_has_no_roots():
    """Regression: an all-zero curve once reported a root at every grid point,
    and the nearest-to-spot pick then returned spot itself -- a fabricated level
    that looked entirely reasonable downstream.
    """
    assert find_sign_change_roots([(100.0, 0.0), (101.0, 0.0), (102.0, 0.0)]) == []


def test_multiple_crossings_are_all_reported():
    curve = [(100.0, -1.0), (101.0, 1.0), (102.0, -1.0), (103.0, 1.0)]
    assert len(find_sign_change_roots(curve)) == 3


def test_trailing_zero_endpoint_is_captured():
    assert find_sign_change_roots([(100.0, -1.0), (101.0, 0.0)]) == [
        pytest.approx(101.0)
    ]


# --- Slope ------------------------------------------------------------------


def test_local_slope_is_the_finite_difference_across_the_bracketing_interval():
    curve = [(100.0, -2.0), (102.0, 2.0)]
    assert local_slope_at(curve, 101.0) == pytest.approx(2.0)


def test_local_slope_sign_follows_the_curve_direction():
    rising = [(100.0, -1.0), (101.0, 1.0)]
    falling = [(100.0, 1.0), (101.0, -1.0)]
    assert local_slope_at(rising, 100.5) > 0
    assert local_slope_at(falling, 100.5) < 0


def test_local_slope_is_none_for_a_degenerate_curve():
    assert local_slope_at([(100.0, 1.0)], 100.0) is None


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
    steep = SmileFit(a=0.20, b=0.0, c=-10.0)
    assert steep.at(5.0) > 0.0
    assert steep.at(0.0) == pytest.approx(0.20)
    assert SmileFit(a=0.20, b=0.0, c=1_000.0).at(5.0) <= 5.0


def test_smile_fit_needs_at_least_three_points():
    assert fit_smile([(0.0, 0.2), (0.1, 0.21)]) is None


def test_degenerate_points_do_not_crash_the_solver():
    assert fit_smile([(0.0, 0.2)] * 5) is None


def test_moneyness_is_zero_at_the_money_and_signed_by_side():
    assert standardised_moneyness(5000.0, 5000.0, 0.25) == pytest.approx(0.0)
    assert standardised_moneyness(5200.0, 5000.0, 0.25) > 0.0
    assert standardised_moneyness(4800.0, 5000.0, 0.25) < 0.0


def test_smiles_are_fitted_per_expiry_and_recover_the_fixture_skew():
    chain, contracts = contracts_from()
    fits = fit_smiles_by_expiry(contracts, spot=chain.spot, min_points=5)
    assert len(fits) == len(chain.expiries)
    for fit in fits.values():
        assert fit.at(-0.5) > fit.at(0.5)


def test_am_and_pm_series_sharing_an_expiry_date_get_separate_smiles():
    """SPX and SPXW can both expire on the third Friday but settle hours apart."""
    spec = SyntheticChainSpec(
        expiries=(
            (OptionRoot.SPX, date(2026, 3, 20)),
            (OptionRoot.SPXW, date(2026, 3, 20)),
        )
    )
    chain, contracts = contracts_from(spec)
    assert len(fit_smiles_by_expiry(contracts, spot=chain.spot, min_points=5)) == 2


# --- The curve --------------------------------------------------------------


def test_curve_at_spot_reproduces_the_direct_signed_total():
    """Bridge between view 2 and view 5: at the current spot the grid must return
    the same number the plain aggregation does.
    """
    chain, contracts = contracts_from(SyntheticChainSpec(vendor_gamma=False))
    eligible = tuple(
        c for c in contracts if c.dte <= ZeroGammaConfig().max_dte_for_grid
    )
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
    chain, contracts = contracts_from()
    return signed_gex_curve(
        tuple(c for c in contracts if c.dte <= 60),
        grid=build_spot_grid(chain.spot, span_pct=0.04, step_pct=step_pct),
        base_spot=chain.spot,
        convention=IVConvention.FROZEN_IV,
        spot_move_pct=0.01,
    )


def test_signed_gex_troughs_near_the_put_open_interest_peak():
    """The curve is U-shaped, not monotone: put gamma contribution is largest when
    spot is *at* the put strikes. This is why the search counts crossings instead
    of assuming a single monotone flip.
    """
    trough = min(_fixture_curve(), key=lambda point: point[1])[0]
    assert trough == pytest.approx(PUT_OI_PEAK_STRIKE, abs=50.0)


def test_curve_rises_monotonically_between_the_two_open_interest_peaks():
    curve = [
        point
        for point in _fixture_curve()
        if PUT_OI_PEAK_STRIKE <= point[0] <= CALL_OI_PEAK_STRIKE
    ]
    values = [value for _, value in curve]
    assert values == sorted(values), values
    assert values[0] < 0.0 < values[-1]


def test_curve_turns_back_down_above_the_call_open_interest_peak():
    """Double-humped, so "signed GEX is monotone in spot" is false in general."""
    curve = _fixture_curve()
    peak_spot = max(curve, key=lambda point: point[1])[0]
    assert peak_spot == pytest.approx(CALL_OI_PEAK_STRIKE, abs=50.0)
    above = [value for spot, value in curve if spot >= peak_spot]
    assert above == sorted(above, reverse=True), above


# --- Full diagnostics -------------------------------------------------------


@pytest.mark.parametrize(
    "convention",
    [
        IVConvention.FROZEN_IV,
        IVConvention.STICKY_STRIKE,
        IVConvention.STICKY_MONEYNESS,
    ],
)
def test_every_implemented_convention_reports_full_diagnostics(convention):
    result = run(convention)
    assert result.resolved
    assert result.selection_method is RootSelectionMethod.NEAREST_TO_SPOT
    assert result.root_count == 1
    assert result.all_roots == (result.selected_root,)
    assert result.grid_lower_bound < result.selected_root < result.grid_upper_bound
    assert result.local_slope_at_selected_root is not None
    assert result.max_abs_gex_on_grid > 0.0
    assert result.selected_root_distance_from_spot_pct is not None
    assert not result.identically_zero_curve
    assert not result.no_root_found
    # Put-heavy book: signed GEX is negative at spot, so the flip is above it.
    assert result.selected_root > SPOT


def test_all_roots_are_retained_not_just_the_selected_one():
    """The requirement: the other roots must not be implied to be irrelevant."""
    result = run(IVConvention.FROZEN_IV)
    assert result.selected_root in result.all_roots
    assert result.root_count == len(result.all_roots)


def test_selection_method_states_that_nearest_to_spot_is_a_convention():
    result = run(IVConvention.FROZEN_IV)
    assert result.selection_method.value == "nearest_to_spot"


def test_slope_normalisation_is_scale_free():
    """Normalised slope compares across chains of different size, so it is what
    the confidence component consumes.
    """
    result = run(IVConvention.FROZEN_IV)
    assert result.normalised_slope is not None
    assert abs(result.normalised_slope) > 0.0


def test_serialised_result_carries_every_required_diagnostic():
    payload = run(IVConvention.STICKY_STRIKE).as_dict()
    for key in (
        "selected_root",
        "all_roots",
        "root_count",
        "selection_method",
        "selected_root_distance_from_spot_pct",
        "local_slope_at_selected_root",
        "nearest_root_spacing_pct",
        "grid_lower_bound",
        "grid_upper_bound",
        "root_near_boundary",
        "identically_zero_curve",
        "no_root_found",
        "max_abs_gex_on_grid",
        "convention",
    ):
        assert key in payload, key


# --- Unimplemented conventions ---------------------------------------------


@pytest.mark.parametrize(
    "convention", [IVConvention.STICKY_DELTA, IVConvention.SURFACE_REFIT]
)
def test_unimplemented_conventions_refuse_rather_than_approximate(convention):
    """``STICKY_DELTA`` must not quietly alias onto the log-moneyness
    approximation. Naming an approximation after the method it approximates is
    how a known simplification becomes an assumed capability.
    """
    result = run(convention)
    assert result.selected_root is None
    assert result.selection_method is RootSelectionMethod.CONVENTION_UNIMPLEMENTED
    assert result.unimplemented_reason
    assert result.curve == ()


def test_sticky_delta_reason_explains_the_missing_iterative_solve():
    reason = IVConvention.STICKY_DELTA.unimplemented_reason
    assert reason is not None
    assert "delta-coordinate" in reason
    assert "STICKY_MONEYNESS" in reason


def test_only_the_three_implemented_conventions_report_as_implemented():
    implemented = {c for c in IVConvention if c.is_implemented}
    assert implemented == {
        IVConvention.FROZEN_IV,
        IVConvention.STICKY_STRIKE,
        IVConvention.STICKY_MONEYNESS,
    }


# --- Convention behaviour ---------------------------------------------------


def test_sticky_moneyness_disagrees_with_sticky_strike_on_a_skewed_smile():
    """The disagreement is the error bar the confidence score consumes."""
    sticky = run(IVConvention.STICKY_STRIKE).selected_root
    moneyness = run(IVConvention.STICKY_MONEYNESS).selected_root
    assert sticky != moneyness


def test_flat_smile_collapses_sticky_moneyness_onto_sticky_strike():
    """Control experiment: with no skew there is nothing for a translating smile
    to change, so the two conventions must agree.
    """
    flat = SyntheticChainSpec(iv_skew=0.0, iv_curvature=0.0, iv_half_spread=0.0)
    assert synthetic_iv(4800.0, flat) == pytest.approx(synthetic_iv(5200.0, flat))
    a = run(IVConvention.STICKY_STRIKE, flat).selected_root
    b = run(IVConvention.STICKY_MONEYNESS, flat).selected_root
    assert a == pytest.approx(b, rel=1e-9)


def test_sticky_strike_tracks_frozen_iv_closely_on_a_smooth_smile():
    """Both hold IV at the strike; sticky_strike just reads it off a fit."""
    frozen = run(IVConvention.FROZEN_IV).selected_root
    sticky = run(IVConvention.STICKY_STRIKE).selected_root
    assert abs(frozen - sticky) / SPOT < 0.005


# --- Boundary handling and adaptive expansion -------------------------------


def test_a_one_sided_book_reports_no_root_after_bounded_expansion():
    """Calls only: signed GEX is positive everywhere. Reporting that beats
    extrapolating a level outside the grid.
    """
    _, contracts = contracts_from()
    calls_only = tuple(c for c in contracts if c.sign > 0.0)
    result = compute_zero_gamma(
        calls_only,
        spot=SPOT,
        convention=IVConvention.FROZEN_IV,
        spot_move_pct=0.01,
    )
    assert result.selected_root is None
    assert result.no_root_found
    assert result.selection_method is RootSelectionMethod.NONE_FOUND
    assert result.grid_expansions == ZeroGammaConfig().max_grid_expansions


def test_grid_expansion_is_bounded():
    """An unbounded search on a one-sided book would widen until the float range
    gave out, and a root found 30% from spot is not a level worth reporting.
    """
    _, contracts = contracts_from()
    calls_only = tuple(c for c in contracts if c.sign > 0.0)
    config = ZeroGammaConfig(max_grid_expansions=2)
    result = compute_zero_gamma(
        calls_only,
        spot=SPOT,
        convention=IVConvention.FROZEN_IV,
        spot_move_pct=0.01,
        config=config,
    )
    assert result.grid_expansions == 2
    # The realised span is quantised to a whole number of grid steps, so it lands
    # near -- not exactly on -- span * factor^2. Keeping the step exact matters
    # more than keeping the span exact: the step is what interpolation accuracy
    # depends on.
    expected_span = 0.04 * config.grid_expansion_factor**2
    assert result.grid_upper_bound == pytest.approx(
        SPOT * (1 + expected_span), rel=config.grid_step_pct
    )
    assert result.grid_upper_bound > SPOT * 1.04  # it really did widen


def test_expansion_does_not_trigger_when_the_root_is_comfortably_interior():
    result = run(IVConvention.FROZEN_IV)
    assert result.grid_expansions == 0
    assert not result.root_near_boundary


def test_a_root_near_the_edge_is_flagged_when_expansion_is_disabled():
    """A narrow grid forces the crossing to the boundary; with expansion turned
    off the flag must survive so the level is not treated as reliable.
    """
    _, contracts = contracts_from()
    narrow = ZeroGammaConfig(
        grid_span_pct=0.009, grid_step_pct=0.0005, max_grid_expansions=0
    )
    result = compute_zero_gamma(
        contracts,
        spot=SPOT,
        convention=IVConvention.FROZEN_IV,
        spot_move_pct=0.01,
        config=narrow,
    )
    assert result.root_near_boundary or result.no_root_found


def test_expansion_recovers_a_root_a_narrow_grid_would_have_missed():
    _, contracts = contracts_from()
    without = compute_zero_gamma(
        contracts,
        spot=SPOT,
        convention=IVConvention.FROZEN_IV,
        spot_move_pct=0.01,
        config=ZeroGammaConfig(grid_span_pct=0.002, max_grid_expansions=0),
    )
    with_expansion = compute_zero_gamma(
        contracts,
        spot=SPOT,
        convention=IVConvention.FROZEN_IV,
        spot_move_pct=0.01,
        config=ZeroGammaConfig(grid_span_pct=0.002, max_grid_expansions=5),
    )
    assert without.no_root_found
    assert with_expansion.resolved
    assert with_expansion.grid_expansions > 0


# --- Degenerate curves ------------------------------------------------------


def test_empty_contract_set_produces_a_flat_identically_zero_result():
    result = compute_zero_gamma(
        (), spot=SPOT, convention=IVConvention.FROZEN_IV, spot_move_pct=0.01
    )
    assert result.identically_zero_curve
    assert result.selected_root is None
    assert result.all_roots == ()
    assert result.root_count == 0
    assert result.selection_method is RootSelectionMethod.CURVE_IDENTICALLY_ZERO
    assert result.max_abs_gex_on_grid == 0.0
    # An identically-zero curve is not a search-window problem, so it must not
    # burn expansions trying to widen its way out.
    assert result.grid_expansions == 0


def test_identically_zero_result_serialises_explicitly():
    payload = compute_zero_gamma(
        (), spot=SPOT, convention=IVConvention.FROZEN_IV, spot_move_pct=0.01
    ).as_dict()
    assert payload["identically_zero_curve"] is True
    assert payload["selected_root"] is None


# --- Universe filtering and determinism -------------------------------------


def test_grid_excludes_contracts_beyond_max_dte_for_grid():
    _, contracts = contracts_from()
    narrow = compute_zero_gamma(
        contracts,
        spot=SPOT,
        convention=IVConvention.FROZEN_IV,
        spot_move_pct=0.01,
        config=ZeroGammaConfig(max_dte_for_grid=2),
    )
    wide = compute_zero_gamma(
        contracts,
        spot=SPOT,
        convention=IVConvention.FROZEN_IV,
        spot_move_pct=0.01,
        config=ZeroGammaConfig(max_dte_for_grid=60),
    )
    assert narrow.selected_root != wide.selected_root


def test_result_is_deterministic_across_repeated_runs():
    """Replay determinism, checked where the floating-point surface area is
    largest.
    """
    runs = [run(IVConvention.STICKY_MONEYNESS) for _ in range(3)]
    assert len({r.selected_root for r in runs}) == 1
    assert len({r.curve for r in runs}) == 1
    assert len({r.local_slope_at_selected_root for r in runs}) == 1


def test_engine_config_runs_three_implemented_conventions_by_default():
    conventions = GexEngineConfig().zero_gamma.conventions
    assert len(conventions) == 3
    assert all(c.is_implemented for c in conventions)
    assert IVConvention.STICKY_DELTA not in conventions
