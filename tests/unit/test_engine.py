"""End-to-end: ChainSnapshot in, GexSnapshot out.

The critical property tested here is replay determinism -- the same input must
produce a bit-identical output, forever. Everything else in the plan's validation
layer (point-in-time backtesting, replay, regression) rests on it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.domain.contracts import OptionRoot
from src.domain.gex import ExpiryBucket, IVConvention, SignConvention
from src.gex.config import GexEngineConfig, ZeroGammaConfig
from src.gex.engine import compute_gex_snapshot
from src.gex.walls import distance_pct
from tests.fixtures.chains import (
    SyntheticChainSpec,
    build_single_contract_chain,
    build_synthetic_chain,
)


# --- All five views are present ---------------------------------------------


def test_snapshot_carries_all_five_mandatory_views(snapshot):
    """The plan's central requirement: never one GEX number, always five views."""
    assert snapshot.total_unsigned_gex > 0.0  # view 1
    assert snapshot.total_signed_gex != 0.0  # view 2
    assert len(snapshot.buckets) == len(ExpiryBucket)  # view 3
    assert len(snapshot.strikes) > 0  # view 4
    assert len(snapshot.zero_gamma) == 3  # view 5, one per convention
    assert snapshot.confidence.value >= 0.0


def test_snapshot_records_the_sign_convention_it_used(snapshot):
    """A stored signed GEX is meaningless without the assumption behind it."""
    assert snapshot.sign_convention is SignConvention.DEALER_LONG_CALLS_SHORT_PUTS


def test_metadata_records_the_gamma_source_split(snapshot):
    assert snapshot.meta["vendor_gamma_count"] == snapshot.contract_count
    assert snapshot.meta["shadow_gamma_count"] == 0
    assert snapshot.meta["spot_move_pct"] == 0.01


def test_unsigned_total_dominates_signed_total(snapshot):
    """Calls and puts partly cancel in the signed view but never in the unsigned
    one, so |signed| <= unsigned always holds.
    """
    assert abs(snapshot.total_signed_gex) <= snapshot.total_unsigned_gex


# --- Determinism ------------------------------------------------------------


def test_the_same_chain_produces_an_identical_snapshot():
    chain = build_synthetic_chain()
    first = compute_gex_snapshot(chain)
    second = compute_gex_snapshot(chain)
    assert first == second


def test_rebuilding_the_fixture_produces_an_identical_snapshot():
    """No hidden clock read, no dict-ordering dependency, no randomness."""
    first = compute_gex_snapshot(build_synthetic_chain())
    second = compute_gex_snapshot(build_synthetic_chain())
    assert first == second
    assert first.zero_gamma == second.zero_gamma


# --- Zero gamma across conventions -----------------------------------------


def test_every_convention_is_reported_and_lookup_works(snapshot):
    for convention in (
        IVConvention.STICKY_STRIKE,
        IVConvention.FROZEN_IV,
        IVConvention.STICKY_DELTA,
    ):
        result = snapshot.zero_gamma_for(convention)
        assert result is not None
        assert result.resolved


def test_primary_zero_gamma_is_the_first_resolved_convention(snapshot):
    """Run order puts the plan's default research convention first."""
    primary = snapshot.primary_zero_gamma
    assert primary is not None
    assert primary.convention is IVConvention.STICKY_STRIKE


def test_convention_spread_is_reported_as_the_error_bar(snapshot):
    spread = snapshot.zero_gamma_spread_pct
    assert spread is not None
    assert spread >= 0.0


def test_spread_is_undefined_when_only_one_convention_runs():
    chain = build_synthetic_chain()
    single = compute_gex_snapshot(
        chain,
        GexEngineConfig(
            zero_gamma=ZeroGammaConfig(conventions=(IVConvention.STICKY_STRIKE,))
        ),
    )
    assert single.zero_gamma_spread_pct is None


def test_requesting_surface_refit_warns_instead_of_returning_a_number():
    chain = build_synthetic_chain()
    result = compute_gex_snapshot(
        chain,
        GexEngineConfig(
            zero_gamma=ZeroGammaConfig(
                conventions=(IVConvention.STICKY_STRIKE, IVConvention.SURFACE_REFIT)
            )
        ),
    )
    assert result.zero_gamma_for(IVConvention.SURFACE_REFIT).zero_gamma_spot is None
    assert any("surface_refit" in w for w in result.warnings)


# --- Buckets and dominance --------------------------------------------------


def test_bucket_lookup_returns_every_bucket(snapshot):
    for bucket in ExpiryBucket:
        assert snapshot.bucket(bucket) is not None


def test_0dte_bucket_is_populated_by_the_fixture(snapshot):
    zero_dte = snapshot.bucket(ExpiryBucket.DTE_0)
    assert zero_dte.contract_count > 0
    assert zero_dte.unsigned_gex > 0.0


def test_dominance_ratio_is_recorded_in_metadata(snapshot):
    ratio = snapshot.meta["dte0_dominance_ratio"]
    assert 0.0 < ratio < 1.0


# --- Confidence integration -------------------------------------------------


def test_default_config_yields_an_uncalibrated_untradeable_snapshot(snapshot):
    """The safety interlock: research output flows, live trading cannot."""
    assert not snapshot.confidence.calibrated
    assert "zero_gamma_stability" in snapshot.confidence.uncalibrated_components


def test_stale_feed_lowers_confidence():
    chain = build_synthetic_chain()
    stale = replace(
        chain, options_feed_timestamp=chain.as_of - timedelta(seconds=120)
    )
    assert (
        compute_gex_snapshot(stale).confidence.value
        < compute_gex_snapshot(chain).confidence.value
    )


def test_expected_contract_count_is_honoured():
    chain = build_synthetic_chain()
    optimistic = compute_gex_snapshot(chain)
    pessimistic = compute_gex_snapshot(
        chain, expected_contract_count=len(chain.quotes) * 4
    )
    assert pessimistic.confidence.value < optimistic.confidence.value


def test_flow_adjusted_input_is_threaded_into_sign_agreement():
    chain = build_synthetic_chain()
    without = compute_gex_snapshot(chain)
    naive = without.total_signed_gex
    with_flow = compute_gex_snapshot(chain, flow_adjusted_signed_gex=naive * 1.02)
    agreement = next(
        c
        for c in with_flow.confidence.components
        if c.name == "sign_model_agreement"
    )
    assert agreement.score > 0.9


# --- Degenerate and edge inputs --------------------------------------------


def test_empty_chain_produces_a_warned_zero_snapshot():
    chain = build_synthetic_chain()
    empty = replace(chain, quotes=())
    result = compute_gex_snapshot(empty)
    assert result.total_unsigned_gex == 0.0
    assert result.contract_count == 0
    assert "no usable contracts in snapshot" in result.warnings
    assert result.walls.call_wall is None
    assert all(not zg.resolved for zg in result.zero_gamma)


def test_non_positive_spot_is_rejected_at_construction():
    chain = build_synthetic_chain()
    with pytest.raises(ValueError, match="spot must be positive"):
        replace(chain, spot=0.0)


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
    result = compute_gex_snapshot(mixed)
    assert any("mixed gamma sources" in w for w in result.warnings)


def test_shadow_only_chain_produces_no_mixed_source_warning():
    chain = build_synthetic_chain(SyntheticChainSpec(vendor_gamma=False))
    result = compute_gex_snapshot(chain)
    assert not any("mixed gamma sources" in w for w in result.warnings)
    assert result.meta["vendor_gamma_count"] == 0


def test_single_contract_chain_has_no_zero_gamma_crossing():
    """One call is positive gamma everywhere -- there is nothing to cross."""
    result = compute_gex_snapshot(build_single_contract_chain())
    assert result.contract_count == 1
    assert all(zg.no_crossing for zg in result.zero_gamma)
    assert any("no sign change" in w for w in result.warnings)


def test_oi_age_uses_the_oldest_date_in_the_chain():
    """A partially-refreshed OI table must report as stale, not as fresh."""
    chain = build_synthetic_chain()
    mixed = replace(
        chain,
        quotes=(replace(chain.quotes[0], open_interest_asof=date(2026, 1, 5)),)
        + chain.quotes[1:],
    )
    stale_component = next(
        c
        for c in compute_gex_snapshot(mixed).confidence.components
        if c.name == "oi_freshness"
    )
    assert stale_component.score < 1.0


# --- Walls integration ------------------------------------------------------


def test_distance_features_are_derivable_from_the_snapshot(snapshot):
    """The feature-store fields spot_to_*_distance_pct come straight from here."""
    to_call = distance_pct(snapshot.spot, snapshot.walls.call_wall)
    to_put = distance_pct(snapshot.spot, snapshot.walls.put_wall)
    assert to_call is not None and to_call > 0.0
    assert to_put is not None and to_put < 0.0

    primary = snapshot.primary_zero_gamma
    to_zero_gamma = distance_pct(snapshot.spot, primary.zero_gamma_spot)
    assert to_zero_gamma is not None


def test_far_dated_only_chain_still_produces_walls():
    chain = build_synthetic_chain(
        SyntheticChainSpec(expiries=((OptionRoot.SPXW, date(2026, 5, 15)),))
    )
    result = compute_gex_snapshot(chain)
    assert result.walls.call_wall is not None
    assert result.walls.put_wall is not None
    assert result.bucket(ExpiryBucket.DTE_GT_30).contract_count > 0
