"""Piecewise strike-spacing inference.

The v2 defect: a single global median gap. Real SPX/SPXW chains use 5-point
strikes near the money and 25- or 50-point strikes in the wings, so one median
is wrong nearly everywhere -- it declares the fine region sparse and the coarse
region complete, which is the opposite of the truth on both counts.
"""

from __future__ import annotations

import pytest

from src.domain.gex import GammaVoidKind, StrikeGex
from src.gex.config import WallConfig
from src.gex.walls import StrikeLadder, classify_void, find_gamma_voids

SPOT = 5000.0
CFG = WallConfig()


def make_strike(strike: float, *, call: float = 0.0, put: float = 0.0) -> StrikeGex:
    return StrikeGex(
        strike=strike,
        call_gex=call,
        put_gex=put,
        unsigned_gex=call + put,
        signed_gex=call - put,
        call_open_interest=0,
        put_open_interest=0,
    )


def mixed_ladder() -> tuple[float, ...]:
    """A realistic SPX shape: 25-point wings, 5-point core, 25-point wings."""
    wing_low = [4600.0 + 25.0 * i for i in range(16)]  # 4600..4975
    core = [4980.0 + 5.0 * i for i in range(9)]  # 4980..5020
    wing_high = [5025.0 + 25.0 * i for i in range(16)]  # 5025..5400
    return tuple(wing_low + core + wing_high)


# --- Local spacing inference ------------------------------------------------


def test_a_uniform_ladder_reports_one_spacing_everywhere():
    ladder = StrikeLadder.from_strikes(tuple(4900.0 + 25.0 * i for i in range(9)))
    assert ladder.modal_spacing == 25.0
    assert ladder.local_spacing(4950.0) == 25.0
    assert ladder.local_spacing(5050.0) == 25.0


def test_a_mixed_ladder_reports_different_spacing_by_region():
    """v2 bug: one global median for the whole chain.

    Here the median is 25, so the 5-point core would have been judged against a
    25-point expectation -- reporting five times as many strikes as expected and
    masking any genuine omission inside it.
    """
    ladder = StrikeLadder.from_strikes(mixed_ladder())
    assert ladder.local_spacing(5000.0) == 5.0
    assert ladder.local_spacing(4700.0) == 25.0
    assert ladder.local_spacing(5300.0) == 25.0


def test_expected_count_uses_local_spacing_not_the_global_median():
    ladder = StrikeLadder.from_strikes(mixed_ladder())
    # 4980..5020 at 5-point spacing is 9 strikes, not 3.
    assert ladder.expected_count_between(4980.0, 5020.0) == 9
    # 4600..4700 at 25-point spacing is 5.
    assert ladder.expected_count_between(4600.0, 4700.0) == 5


def test_a_legitimate_five_to_ten_point_transition_is_not_an_omission():
    strikes = tuple(
        [4900.0 + 5.0 * i for i in range(11)]  # 4900..4950 at 5
        + [4960.0 + 10.0 * i for i in range(11)]  # 4960..5060 at 10
    )
    ladder = StrikeLadder.from_strikes(strikes)
    assert ladder.local_spacing(4925.0) == 5.0
    assert ladder.local_spacing(5000.0) == 10.0


def test_coverage_is_measured_against_the_local_expectation():
    ladder = StrikeLadder.from_strikes(mixed_ladder())
    assert ladder.coverage_between(4980.0, 5020.0) == pytest.approx(1.0)
    assert ladder.coverage_between(4600.0, 4700.0) == pytest.approx(1.0)


def test_too_few_strikes_yields_no_inferable_spacing():
    assert StrikeLadder.from_strikes((5000.0,)).modal_spacing is None
    assert StrikeLadder.from_strikes((5000.0, 5025.0)).modal_spacing is None


def test_spacing_is_robust_to_a_single_missing_strike():
    """Median, not mean: one hole must not move the inferred ladder."""
    ladder = StrikeLadder.from_strikes((4900.0, 4925.0, 4975.0, 5000.0, 5025.0, 5050.0))
    assert ladder.local_spacing(4950.0) == 25.0


# --- Void classification with local spacing ---------------------------------


def test_a_missing_strike_inside_a_fine_region_is_detected():
    """The case the global median could not see.

    In a 5-point region, a 10-point gap is a missing strike. Against a 25-point
    global median it looks like normal spacing.
    """
    strikes = tuple(
        make_strike(s, put=0.1)
        for s in (4980.0, 4985.0, 4995.0, 5000.0)  # 4990 omitted
    )
    ladder = StrikeLadder.from_strikes(mixed_ladder())
    kind, detail, missing = classify_void(run=strikes, ladder=ladder, config=CFG)
    assert kind is GammaVoidKind.MISSING_STRIKE_DATA
    assert missing == 1
    assert "absence of data" in detail


def test_multiple_consecutive_missing_strikes_are_counted():
    strikes = tuple(
        make_strike(s, put=0.1) for s in (4980.0, 5005.0)
    )  # 4985..5000 all absent
    ladder = StrikeLadder.from_strikes(mixed_ladder())
    kind, _, missing = classify_void(run=strikes, ladder=ladder, config=CFG)
    assert kind is GammaVoidKind.MISSING_STRIKE_DATA
    assert missing == 4


def test_a_complete_low_gamma_region_is_a_true_void():
    strikes = tuple(
        make_strike(s, put=0.1) for s in (4980.0, 4985.0, 4990.0, 4995.0, 5000.0)
    )
    ladder = StrikeLadder.from_strikes(mixed_ladder())
    kind, _, missing = classify_void(run=strikes, ladder=ladder, config=CFG)
    assert kind is GammaVoidKind.TRUE_LOW_GEX_VOID
    assert missing == 0


def test_a_complete_coarse_region_is_also_a_true_void():
    """The wings are legitimately coarse; that is not missing data."""
    strikes = tuple(make_strike(4600.0 + 25.0 * i, put=0.1) for i in range(5))
    ladder = StrikeLadder.from_strikes(mixed_ladder())
    kind, _, _ = classify_void(run=strikes, ladder=ladder, config=CFG)
    assert kind is GammaVoidKind.TRUE_LOW_GEX_VOID


def test_a_sparse_vendor_response_is_insufficient_coverage():
    strikes = (make_strike(4900.0, put=0.1), make_strike(5100.0, put=0.1))
    ladder = StrikeLadder.from_strikes((4900.0, 5100.0))
    kind, detail, _ = classify_void(run=strikes, ladder=ladder, config=CFG)
    assert kind is GammaVoidKind.INSUFFICIENT_COVERAGE
    assert "expected ladder" in detail


def test_a_repeated_wider_gap_is_a_coarser_increment():
    run = tuple(make_strike(4400.0 + 100.0 * i, put=0.1) for i in range(3))
    ladder = StrikeLadder.from_strikes(tuple(4900.0 + 25.0 * i for i in range(9)))
    kind, detail, _ = classify_void(run=run, ladder=ladder, config=CFG)
    assert kind is GammaVoidKind.IRREGULAR_STRIKE_SPACING
    assert "coarser increment" in detail


def test_a_single_wide_gap_remains_an_omission():
    run = (make_strike(4900.0, put=0.1), make_strike(4975.0, put=0.1))
    ladder = StrikeLadder.from_strikes(tuple(4850.0 + 25.0 * i for i in range(8)))
    kind, _, missing = classify_void(run=run, ladder=ladder, config=CFG)
    assert kind is GammaVoidKind.MISSING_STRIKE_DATA
    assert missing == 2


# --- Expiry and root isolation ----------------------------------------------


def test_mixed_expirations_do_not_contaminate_each_others_spacing():
    """Two expiries with different ladders must be inferred separately.

    Pooling them would produce a median that describes neither, and the finer
    expiry would be judged against the coarser one's expectation.
    """
    from datetime import date

    fine = tuple(4990.0 + 5.0 * i for i in range(9))
    coarse = tuple(4900.0 + 50.0 * i for i in range(9))
    ladders = StrikeLadder.by_group(
        {
            ("SPXW", date(2026, 3, 17)): fine,
            ("SPXW", date(2026, 5, 15)): coarse,
        }
    )
    assert ladders[("SPXW", date(2026, 3, 17))].modal_spacing == 5.0
    assert ladders[("SPXW", date(2026, 5, 15))].modal_spacing == 50.0


def test_different_roots_are_grouped_separately():
    from datetime import date

    ladders = StrikeLadder.by_group(
        {
            ("SPX", date(2026, 3, 20)): tuple(4900.0 + 25.0 * i for i in range(9)),
            ("SPXW", date(2026, 3, 20)): tuple(4990.0 + 5.0 * i for i in range(9)),
        }
    )
    assert ladders[("SPX", date(2026, 3, 20))].modal_spacing == 25.0
    assert ladders[("SPXW", date(2026, 3, 20))].modal_spacing == 5.0


# --- End to end -------------------------------------------------------------


def test_find_gamma_voids_uses_local_spacing():
    strikes = tuple(
        make_strike(s, put=(0.1 if 4980.0 <= s <= 5020.0 else 100.0))
        for s in mixed_ladder()
    )
    voids = find_gamma_voids(
        strikes, spot=SPOT, config=WallConfig(band_pct=0.2), ladder=None
    )
    assert voids
    assert all(v.kind is GammaVoidKind.TRUE_LOW_GEX_VOID for v in voids)


def test_an_omission_inside_the_fine_core_is_not_reported_as_tradable():
    present = tuple(s for s in mixed_ladder() if s not in (4990.0, 4995.0))
    strikes = tuple(
        make_strike(s, put=(0.1 if 4980.0 <= s <= 5020.0 else 100.0)) for s in present
    )
    voids = find_gamma_voids(strikes, spot=SPOT, config=WallConfig(band_pct=0.2))
    assert voids
    assert any(v.kind is GammaVoidKind.MISSING_STRIKE_DATA for v in voids)
    assert not any(v.is_tradable_structure for v in voids)
