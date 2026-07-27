"""Universe separation and gamma comparison.

Two v2 defects:

* **`absolute_difference` was signed.** It returned `local - vendor`, so a
  "magnitude of disagreement" could be negative and any aggregate of it silently
  cancelled instead of accumulating.
* **One universe for two questions.** A contract carried on vendor gamma with no
  IV contributes to current GEX but *cannot be repriced* on the zero-gamma grid.
  Counting it as covered reported 100% coverage for a grid that skipped it.
"""

from __future__ import annotations

import pytest

from src.domain.effective_model import ResolutionIssue, resolve_effective_inputs
from src.domain.iv import GammaComparison, build_iv_quote
from src.domain.model_spec import ModelSpec
from src.gex.config import GexEngineConfig
from src.gex.engine import compute_gex_snapshot
from src.gex.formulas import compute_contract_gex, gamma_comparisons
from src.gex.sessions import eastern
from src.synthetic.chains import (
    SyntheticChainSpec,
    build_single_contract_chain,
    build_synthetic_chain,
    with_quote,
)

AS_OF = eastern(2026, 3, 17, 11, 0)


def effective(**spec_kwargs):
    chain = build_single_contract_chain(as_of=AS_OF)
    return resolve_effective_inputs(
        quote=chain.quotes[0], snapshot=chain, spec=ModelSpec(**spec_kwargs)
    )


# =============================================================================
# §7 -- gamma comparison
# =============================================================================


def test_absolute_difference_is_never_negative():
    """v2 bug: it returned ``local - vendor``, which is signed."""
    comparison = GammaComparison(local_gamma=0.001, vendor_gamma=0.002)
    assert comparison.absolute_difference == pytest.approx(0.001)
    assert comparison.absolute_difference >= 0.0

    flipped = GammaComparison(local_gamma=0.002, vendor_gamma=0.001)
    assert flipped.absolute_difference == pytest.approx(0.001)


@pytest.mark.parametrize(
    ("local", "vendor"),
    [(0.001, 0.002), (0.002, 0.001), (0.0, 0.005), (0.005, 0.0), (1e-9, 1e-3)],
)
def test_absolute_difference_is_non_negative_for_every_ordering(local, vendor):
    assert (
        GammaComparison(local_gamma=local, vendor_gamma=vendor).absolute_difference
        >= 0.0
    )


def test_signed_difference_preserves_direction():
    assert GammaComparison(
        local_gamma=0.001, vendor_gamma=0.002
    ).signed_difference == pytest.approx(-0.001)
    assert GammaComparison(
        local_gamma=0.002, vendor_gamma=0.001
    ).signed_difference == pytest.approx(0.001)


def test_relative_difference_is_defined_against_the_vendor_magnitude():
    comparison = GammaComparison(local_gamma=0.0011, vendor_gamma=0.001)
    assert comparison.relative_absolute_difference == pytest.approx(0.1)


def test_relative_difference_is_none_for_zero_vendor_gamma():
    """Division by zero has no meaningful answer, and 0.0 would claim agreement."""
    comparison = GammaComparison(local_gamma=0.001, vendor_gamma=0.0)
    assert comparison.relative_absolute_difference is None
    assert comparison.comparison_status == "vendor_gamma_zero"


def test_comparison_is_unavailable_when_either_side_is_missing():
    assert (
        GammaComparison(local_gamma=None, vendor_gamma=0.001).comparison_status
        == "unavailable"
    )
    assert (
        GammaComparison(local_gamma=0.001, vendor_gamma=None).comparison_status
        == "unavailable"
    )


def test_comparison_uses_the_complete_effective_model():
    """The comparison must not rebuild a simplified pricing model of its own."""
    chain = build_synthetic_chain(SyntheticChainSpec())
    result = compute_contract_gex(
        chain, GexEngineConfig(prefer_vendor_gamma=True, model_spec=ModelSpec())
    )
    comparisons = gamma_comparisons(result.contracts, observed_at=AS_OF.isoformat())
    assert comparisons
    first = comparisons[0]
    payload = first.as_dict()
    for key in (
        "risk_free_rate",
        "dividend_yield",
        "implied_volatility",
        "time_to_expiry_years",
        "day_count_convention",
        "minimum_time_to_expiry_minutes",
        "expiration_rule",
        "underlying_price_source",
        "spot",
        "strike",
        "right",
    ):
        assert key in payload["effective_model"], key


@pytest.mark.parametrize(
    "spec_change",
    [
        {"risk_free_rate": 0.09},
        {"dividend_yield": 0.09},
        {"minimum_time_to_expiry_minutes": 1.0},
    ],
    ids=["rate", "dividend", "floor"],
)
def test_comparison_changes_when_a_model_input_changes(spec_change):
    """If the comparison ignored these, the v2 bug would still be present.

    Built at 15:45 so the time floor actually binds -- at midday every candidate
    floor is inactive and the "floor" case would pass without testing anything.
    """
    from src.synthetic.chains import LATE_SESSION_AS_OF

    chain = build_synthetic_chain(SyntheticChainSpec(as_of=LATE_SESSION_AS_OF))
    base = gamma_comparisons(
        compute_contract_gex(
            chain, GexEngineConfig(prefer_vendor_gamma=True, model_spec=ModelSpec())
        ).contracts
    )
    changed = gamma_comparisons(
        compute_contract_gex(
            chain,
            GexEngineConfig(
                prefer_vendor_gamma=True, model_spec=ModelSpec(**spec_change)
            ),
        ).contracts
    )

    # Compare the 0DTE contract specifically. Comparisons are in canonical
    # contract order, so index 0 is a 3-DTE series the time floor cannot bind on
    # -- picking it would make the "floor" case pass without exercising anything.
    def first_0dte(comparisons):
        return next(c for c in comparisons if c.dte == 0)

    assert first_0dte(base).local_gamma != first_0dte(changed).local_gamma


def test_comparison_carries_slice_keys():
    chain = build_synthetic_chain()
    comparisons = gamma_comparisons(
        compute_contract_gex(
            chain, GexEngineConfig(prefer_vendor_gamma=True)
        ).contracts,
        observed_at=AS_OF.isoformat(),
    )
    first = comparisons[0]
    assert first.dte is not None
    assert first.moneyness is not None
    assert first.right in ("call", "put")
    assert first.implied_vol is not None
    assert first.observed_at == AS_OF.isoformat()


def test_no_comparisons_when_the_vendor_supplies_no_gamma():
    """The normal Standard-tier case: nothing to compare against."""
    chain = build_synthetic_chain(SyntheticChainSpec(vendor_gamma=False))
    assert gamma_comparisons(compute_contract_gex(chain).contracts) == ()


# =============================================================================
# §8 -- current-GEX vs zero-gamma-eligible universes
# =============================================================================


def vendor_gamma_without_iv_chain():
    """A chain where every contract has vendor gamma but no usable IV.

    This is the exact shape that used to be reported as fully covered: the
    contracts contribute to current GEX via vendor gamma, yet the zero-gamma grid
    has nothing to reprice them with.
    """
    chain = build_synthetic_chain()
    stripped = chain
    for index in range(len(chain.quotes)):
        stripped = with_quote(
            stripped,
            index,
            iv=build_iv_quote(bid_iv=None, mid_iv=None, ask_iv=None),
        )
    return stripped


def half_stripped_chain():
    """Half the contracts keep IV, half carry vendor gamma only."""
    chain = build_synthetic_chain()
    stripped = chain
    for index in range(0, len(chain.quotes), 2):
        stripped = with_quote(
            stripped,
            index,
            iv=build_iv_quote(bid_iv=None, mid_iv=None, ask_iv=None),
        )
    return stripped


def test_a_vendor_gamma_contract_without_iv_still_contributes_to_current_gex():
    produced = compute_gex_snapshot(
        vendor_gamma_without_iv_chain(),
        GexEngineConfig(prefer_vendor_gamma=True),
    )
    assert produced.contract_count > 0
    assert produced.total_unsigned_gex > 0.0


def test_but_it_is_excluded_from_the_zero_gamma_universe():
    """v2 bug: one universe answered both questions, so this read as covered."""
    produced = compute_gex_snapshot(
        vendor_gamma_without_iv_chain(),
        GexEngineConfig(prefer_vendor_gamma=True),
    )
    universe = produced.zero_gamma_universe
    assert universe.included_contract_count == 0
    assert universe.excluded_contract_count == produced.contract_count


def test_excluded_unsigned_gex_is_quantified_not_just_counted():
    """A count says how many; the share says how much it mattered."""
    produced = compute_gex_snapshot(
        half_stripped_chain(), GexEngineConfig(prefer_vendor_gamma=True)
    )
    universe = produced.zero_gamma_universe
    assert universe.excluded_unsigned_gex > 0.0
    assert universe.excluded_unsigned_gex_share is not None
    assert 0.0 < universe.excluded_unsigned_gex_share < 1.0
    assert universe.included_unsigned_gex + universe.excluded_unsigned_gex == (
        pytest.approx(produced.total_unsigned_gex)
    )


def test_the_grid_is_not_reported_as_fully_covered():
    produced = compute_gex_snapshot(
        half_stripped_chain(), GexEngineConfig(prefer_vendor_gamma=True)
    )
    assert produced.zero_gamma_universe.included_unsigned_gex_share < 1.0


def test_confidence_declines_as_excluded_gex_share_rises():
    full = compute_gex_snapshot(
        build_synthetic_chain(), GexEngineConfig(prefer_vendor_gamma=True)
    )
    partial = compute_gex_snapshot(
        half_stripped_chain(), GexEngineConfig(prefer_vendor_gamma=True)
    )
    none_eligible = compute_gex_snapshot(
        vendor_gamma_without_iv_chain(), GexEngineConfig(prefer_vendor_gamma=True)
    )

    def coverage(snapshot):
        return next(
            c.score
            for c in snapshot.confidence.components
            if c.name == "option_universe_coverage_score"
        )

    assert coverage(full) > coverage(partial) > coverage(none_eligible)
    assert coverage(none_eligible) == 0.0
    # The specific regression: partial coverage must not score a perfect 1.0.
    assert coverage(full) == pytest.approx(1.0)
    assert coverage(partial) < 1.0


def test_exclusion_reasons_are_deterministic_and_machine_readable():
    produced = compute_gex_snapshot(
        half_stripped_chain(), GexEngineConfig(prefer_vendor_gamma=True)
    )
    reasons = produced.zero_gamma_universe.filter_reasons
    assert reasons
    assert all(isinstance(k, str) and isinstance(v, int) for k, v in reasons.items())
    assert ResolutionIssue.IV_MISSING.value in reasons
    # Determinism: same input, same reasons.
    again = compute_gex_snapshot(
        half_stripped_chain(), GexEngineConfig(prefer_vendor_gamma=True)
    )
    assert again.zero_gamma_universe.filter_reasons == reasons


def test_dte_exclusions_stay_separate_from_missing_input_exclusions():
    """Two different reasons a contract is absent from the grid, kept apart."""
    from datetime import date

    from src.domain.contracts import OptionRoot

    spec = SyntheticChainSpec(
        expiries=(
            (OptionRoot.SPXW, date(2026, 3, 17)),
            (OptionRoot.SPXW, date(2026, 9, 18)),  # beyond the 60-DTE grid cap
        )
    )
    chain = build_synthetic_chain(spec)
    stripped = with_quote(
        chain, 0, iv=build_iv_quote(bid_iv=None, mid_iv=None, ask_iv=None)
    )
    produced = compute_gex_snapshot(stripped, GexEngineConfig(prefer_vendor_gamma=True))
    reasons = produced.zero_gamma_universe.filter_reasons
    assert "beyond_max_dte" in reasons
    assert ResolutionIssue.IV_MISSING.value in reasons
    assert reasons["beyond_max_dte"] != reasons[ResolutionIssue.IV_MISSING.value]


def test_a_fully_eligible_chain_reports_complete_coverage():
    """The control: nothing missing means the grid really does cover everything."""
    produced = compute_gex_snapshot(
        build_synthetic_chain(SyntheticChainSpec(vendor_gamma=False))
    )
    universe = produced.zero_gamma_universe
    assert universe.excluded_contract_count == 0
    assert universe.included_unsigned_gex_share == pytest.approx(1.0)


def test_the_snapshot_exposes_both_universes_by_name():
    produced = compute_gex_snapshot(
        half_stripped_chain(), GexEngineConfig(prefer_vendor_gamma=True)
    )
    payload = produced.as_dict()
    assert (
        payload["chain_universe"]["included_contract_count"]
        > (payload["zero_gamma_universe"]["included_contract_count"])
    )


def test_a_material_exclusion_produces_a_warning():
    produced = compute_gex_snapshot(
        half_stripped_chain(), GexEngineConfig(prefer_vendor_gamma=True)
    )
    assert any("zero-gamma" in w and "%" in w for w in produced.warnings)


def test_the_zero_gamma_curve_ignores_ineligible_contracts():
    """Repricing a contract with no IV would mean inventing a volatility."""
    from src.domain.gex import IVConvention
    from src.gex.zero_gamma import compute_zero_gamma

    result = compute_contract_gex(
        vendor_gamma_without_iv_chain(), GexEngineConfig(prefer_vendor_gamma=True)
    )
    curve = compute_zero_gamma(
        result.contracts,
        spot=5000.0,
        convention=IVConvention.FROZEN_IV,
        spot_move_pct=0.01,
    )
    assert curve.identically_zero_curve
    assert curve.selected_root is None
