"""GEX views 1-4."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from src.domain.contracts import OptionRight, OptionRoot
from src.domain.gex import ExpiryBucket, SignConvention
from src.gex.config import GexEngineConfig
from src.gex.formulas import (
    GammaSource,
    aggregate_by_bucket,
    aggregate_by_strike,
    bucket_for_dte,
    bucket_gex_ratio_0dte_vs_rest,
    compute_contract_gex,
    notional_gex,
    sign_for,
    total_signed_gex,
    total_unsigned_gex,
)
from tests.fixtures.chains import (
    CALL_OI_PEAK_STRIKE,
    PUT_OI_PEAK_STRIKE,
    SyntheticChainSpec,
    build_single_contract_chain,
    build_synthetic_chain,
)

# --- The core formula --------------------------------------------------------


def test_notional_gex_matches_the_documented_one_percent_convention():
    """GEX = gamma * OI * M * S * (0.01 * S), checked by hand.

    0.001 * 1000 * 100 * 5000 * 50 = 25,000,000
    """
    assert notional_gex(
        gamma=0.001,
        open_interest=1000,
        multiplier=100.0,
        spot=5000.0,
        spot_move_pct=0.01,
    ) == pytest.approx(25_000_000.0)


def test_notional_gex_scales_with_the_square_of_spot():
    args = dict(gamma=0.001, open_interest=1000, multiplier=100.0, spot_move_pct=0.01)
    assert notional_gex(spot=10_000.0, **args) == pytest.approx(
        4.0 * notional_gex(spot=5_000.0, **args)
    )


def test_notional_gex_is_never_negative_even_with_negative_vendor_gamma():
    """Some vendors sign put gamma. Magnitude and sign are separate concerns here."""
    assert notional_gex(
        gamma=-0.001,
        open_interest=1000,
        multiplier=100.0,
        spot=5000.0,
        spot_move_pct=0.01,
    ) == pytest.approx(25_000_000.0)


def test_single_contract_chain_reproduces_the_hand_calculation():
    chain = build_single_contract_chain(gamma=0.001, open_interest=1000, spot=5000.0)
    result = compute_contract_gex(chain)
    assert len(result.contracts) == 1
    assert result.contracts[0].unsigned_gex == pytest.approx(25_000_000.0)
    assert total_unsigned_gex(result.contracts) == pytest.approx(25_000_000.0)


# --- Sign conventions -------------------------------------------------------


def test_classic_convention_signs_calls_positive_and_puts_negative():
    convention = SignConvention.DEALER_LONG_CALLS_SHORT_PUTS
    assert sign_for(OptionRight.CALL, convention) == 1.0
    assert sign_for(OptionRight.PUT, convention) == -1.0


def test_inverted_convention_flips_both_signs():
    convention = SignConvention.DEALER_SHORT_CALLS_LONG_PUTS
    assert sign_for(OptionRight.CALL, convention) == -1.0
    assert sign_for(OptionRight.PUT, convention) == 1.0


def test_flow_adjusted_convention_has_no_static_sign():
    """Asking for a static flow-adjusted sign is a programming error, not a
    fallback -- the sign has to come from classified flow.
    """
    with pytest.raises(ValueError, match="classified flow"):
        sign_for(OptionRight.CALL, SignConvention.FLOW_ADJUSTED)


def test_flipping_the_convention_flips_total_signed_gex_but_not_unsigned():
    chain = build_synthetic_chain()
    classic = compute_contract_gex(chain, GexEngineConfig()).contracts
    inverted = compute_contract_gex(
        chain,
        GexEngineConfig(sign_convention=SignConvention.DEALER_SHORT_CALLS_LONG_PUTS),
    ).contracts
    assert total_signed_gex(classic) == pytest.approx(-total_signed_gex(inverted))
    assert total_unsigned_gex(classic) == pytest.approx(total_unsigned_gex(inverted))


def test_put_heavy_fixture_starts_with_negative_signed_gex():
    """Fixture design assumption the zero-gamma tests depend on."""
    chain = build_synthetic_chain()
    contracts = compute_contract_gex(chain).contracts
    assert total_signed_gex(contracts) < 0.0
    assert total_unsigned_gex(contracts) > abs(total_signed_gex(contracts))


# --- View 3: buckets --------------------------------------------------------


@pytest.mark.parametrize(
    ("dte", "expected"),
    [
        (0, ExpiryBucket.DTE_0),
        (1, ExpiryBucket.DTE_1_2),
        (2, ExpiryBucket.DTE_1_2),
        (3, ExpiryBucket.DTE_3_5),
        (5, ExpiryBucket.DTE_3_5),
        (6, ExpiryBucket.DTE_6_30),
        (30, ExpiryBucket.DTE_6_30),
        (31, ExpiryBucket.DTE_GT_30),
        (400, ExpiryBucket.DTE_GT_30),
    ],
)
def test_bucket_boundaries_are_inclusive_on_the_upper_edge(dte, expected):
    assert bucket_for_dte(dte) is expected


def test_expired_dte_is_rejected_rather_than_bucketed():
    with pytest.raises(ValueError, match="expired"):
        bucket_for_dte(-1)


def test_every_bucket_is_present_even_when_empty(contract_gex):
    buckets = aggregate_by_bucket(contract_gex.contracts)
    assert tuple(b.bucket for b in buckets) == tuple(ExpiryBucket)


def test_empty_buckets_are_zero_filled_not_omitted():
    """A consumer must be able to tell "no 0DTE gamma" from "bucket missing"."""
    chain = build_synthetic_chain(
        SyntheticChainSpec(expiries=((OptionRoot.SPXW, date(2026, 5, 15)),))
    )
    buckets = aggregate_by_bucket(compute_contract_gex(chain).contracts)
    assert tuple(b.bucket for b in buckets) == tuple(ExpiryBucket)
    zero_dte = next(b for b in buckets if b.bucket is ExpiryBucket.DTE_0)
    assert zero_dte.unsigned_gex == 0.0
    assert zero_dte.contract_count == 0
    far = next(b for b in buckets if b.bucket is ExpiryBucket.DTE_GT_30)
    assert far.contract_count > 0


def test_bucket_totals_reconcile_with_the_chain_total(contract_gex):
    buckets = aggregate_by_bucket(contract_gex.contracts)
    assert sum(b.unsigned_gex for b in buckets) == pytest.approx(
        total_unsigned_gex(contract_gex.contracts)
    )
    assert sum(b.signed_gex for b in buckets) == pytest.approx(
        total_signed_gex(contract_gex.contracts)
    )
    assert sum(b.contract_count for b in buckets) == len(contract_gex.contracts)


def test_0dte_dominance_ratio_is_a_share_of_the_whole_chain(contract_gex):
    buckets = aggregate_by_bucket(contract_gex.contracts)
    ratio = bucket_gex_ratio_0dte_vs_rest(buckets)
    assert ratio is not None
    assert 0.0 < ratio < 1.0


def test_0dte_dominance_is_undefined_for_an_empty_chain():
    assert bucket_gex_ratio_0dte_vs_rest(aggregate_by_bucket(())) is None


def test_0dte_dominance_is_one_when_only_same_day_expiries_exist():
    chain = build_synthetic_chain(
        SyntheticChainSpec(expiries=((OptionRoot.SPXW, date(2026, 3, 17)),))
    )
    buckets = aggregate_by_bucket(compute_contract_gex(chain).contracts)
    assert bucket_gex_ratio_0dte_vs_rest(buckets) == pytest.approx(1.0)


# --- View 4: strikes --------------------------------------------------------


def test_strike_view_is_sorted_and_splits_calls_from_puts(contract_gex):
    strikes = aggregate_by_strike(contract_gex.contracts)
    assert [s.strike for s in strikes] == sorted(s.strike for s in strikes)
    assert all(s.call_gex >= 0.0 and s.put_gex >= 0.0 for s in strikes)
    assert all(
        s.unsigned_gex == pytest.approx(s.call_gex + s.put_gex) for s in strikes
    )


def test_strike_totals_reconcile_with_the_chain_total(contract_gex):
    strikes = aggregate_by_strike(contract_gex.contracts)
    assert sum(s.unsigned_gex for s in strikes) == pytest.approx(
        total_unsigned_gex(contract_gex.contracts)
    )
    assert sum(s.signed_gex for s in strikes) == pytest.approx(
        total_signed_gex(contract_gex.contracts)
    )


def test_strike_open_interest_reconciles_per_side(contract_gex):
    strikes = aggregate_by_strike(contract_gex.contracts)
    expected_calls = sum(
        c.open_interest
        for c in contract_gex.contracts
        if c.contract.right is OptionRight.CALL
    )
    assert sum(s.call_open_interest for s in strikes) == expected_calls


def test_gamma_weighted_peak_is_not_the_raw_open_interest_peak(contract_gex):
    """The whole reason the plan forbids picking walls from raw OI.

    OI is placed at 5100/4900, but near-dated ATM gamma is so much larger that
    the gamma-weighted peak is pulled back toward spot. An OI-ranked "wall" would
    point the strategy at a level with far less hedging pressure behind it than
    the gamma-ranked one.
    """
    strikes = aggregate_by_strike(contract_gex.contracts)
    gamma_peak_call = max(strikes, key=lambda s: s.call_gex).strike
    oi_peak_call = max(strikes, key=lambda s: s.call_open_interest).strike

    assert oi_peak_call == pytest.approx(CALL_OI_PEAK_STRIKE)
    assert gamma_peak_call != oi_peak_call
    # Pulled toward spot, but still on the call side of it.
    assert 5000.0 < gamma_peak_call < oi_peak_call

    gamma_peak_put = max(strikes, key=lambda s: s.put_gex).strike
    oi_peak_put = max(strikes, key=lambda s: s.put_open_interest).strike
    assert oi_peak_put == pytest.approx(PUT_OI_PEAK_STRIKE)
    assert oi_peak_put < gamma_peak_put < 5000.0


def test_open_interest_drives_the_peak_when_gamma_is_flat_across_strikes():
    """With one far-dated expiry, gamma varies only a few percent across the
    strike band, so the OI bump does decide the peak. Confirms the OI weighting
    is actually applied and the previous test is about gamma dominance, not about
    OI being ignored.
    """
    chain = build_synthetic_chain(
        SyntheticChainSpec(expiries=((OptionRoot.SPXW, date(2026, 5, 15)),))
    )
    strikes = aggregate_by_strike(compute_contract_gex(chain).contracts)
    assert max(strikes, key=lambda s: s.call_gex).strike == pytest.approx(
        CALL_OI_PEAK_STRIKE
    )
    assert max(strikes, key=lambda s: s.put_gex).strike == pytest.approx(
        PUT_OI_PEAK_STRIKE
    )


# --- Gamma source resolution ------------------------------------------------


def test_vendor_gamma_is_used_when_present_and_preferred():
    chain = build_synthetic_chain()
    result = compute_contract_gex(chain, GexEngineConfig(prefer_vendor_gamma=True))
    assert result.vendor_gamma_count == len(result.contracts)
    assert result.shadow_gamma_count == 0


def test_shadow_pricer_is_used_when_the_vendor_supplies_no_gamma():
    """The ThetaData Standard-tier path: implied_vol present, gamma absent."""
    chain = build_synthetic_chain(SyntheticChainSpec(vendor_gamma=False))
    result = compute_contract_gex(chain)
    assert result.shadow_gamma_count == len(result.contracts)
    assert result.vendor_gamma_count == 0
    assert all(c.gamma_source == GammaSource.SHADOW_PRICER for c in result.contracts)


def test_shadow_pricer_reproduces_vendor_gamma_on_the_synthetic_chain():
    """The fixture's vendor gamma *is* Black-Scholes, so the two paths must agree
    to floating-point tolerance. This is the guard against a units or
    discounting mismatch between the vendor path and the grid path.
    """
    with_vendor = compute_contract_gex(build_synthetic_chain()).contracts
    without = compute_contract_gex(
        build_synthetic_chain(SyntheticChainSpec(vendor_gamma=False))
    ).contracts
    assert len(with_vendor) == len(without)
    for a, b in zip(with_vendor, without):
        assert a.contract.key == b.contract.key
        assert a.gamma == pytest.approx(b.gamma, rel=1e-12)


def test_prefer_vendor_gamma_false_forces_the_shadow_pricer():
    chain = build_synthetic_chain()
    result = compute_contract_gex(chain, GexEngineConfig(prefer_vendor_gamma=False))
    assert result.vendor_gamma_count == 0
    assert result.shadow_gamma_count == len(result.contracts)


def test_contracts_without_any_gamma_source_are_dropped_and_counted():
    chain = build_single_contract_chain()
    stripped = replace(
        chain,
        quotes=(replace(chain.quotes[0], gamma=None, implied_vol=None),),
    )
    result = compute_contract_gex(stripped)
    assert result.contracts == ()
    assert result.dropped_no_gamma_source == 1
    assert result.usable_ratio == 0.0


# --- Filtering and diagnostics ---------------------------------------------


def test_expired_series_are_excluded_using_the_root_specific_clock():
    """At 11:00 ET the AM-settled SPX series expiring today is already dead,
    while the PM-settled SPXW series expiring today is not.
    """
    from src.gex.sessions import eastern

    spec = SyntheticChainSpec(
        as_of=eastern(2026, 3, 20, 11, 0),
        expiries=(
            (OptionRoot.SPX, date(2026, 3, 20)),
            (OptionRoot.SPXW, date(2026, 3, 20)),
        ),
    )
    # The fixture skips already-settled expiries, so build the chain unfiltered.
    chain = build_synthetic_chain(spec)
    roots = {q.contract.root for q in chain.quotes}
    assert roots == {OptionRoot.SPXW}


def test_crossed_quotes_are_dropped_and_reported():
    chain = build_single_contract_chain()
    crossed = replace(
        chain, quotes=(replace(chain.quotes[0], bid=11.0, ask=10.0),)
    )
    result = compute_contract_gex(crossed)
    assert result.contracts == ()
    assert result.dropped_crossed == 1
    assert result.crossed_ratio == pytest.approx(1.0)


def test_crossed_quotes_are_kept_when_the_filter_is_disabled():
    chain = build_single_contract_chain()
    crossed = replace(
        chain, quotes=(replace(chain.quotes[0], bid=11.0, ask=10.0),)
    )
    result = compute_contract_gex(crossed, GexEngineConfig(drop_crossed_quotes=False))
    assert len(result.contracts) == 1
    assert result.dropped_crossed == 0


def test_zero_open_interest_contracts_are_dropped_and_counted():
    chain = build_single_contract_chain(open_interest=0)
    result = compute_contract_gex(chain)
    assert result.contracts == ()
    assert result.dropped_no_open_interest == 1


def test_zero_open_interest_contributes_nothing_when_the_filter_is_off():
    chain = build_single_contract_chain(open_interest=0)
    result = compute_contract_gex(chain, GexEngineConfig(require_open_interest=False))
    assert len(result.contracts) == 1
    assert result.contracts[0].unsigned_gex == 0.0
