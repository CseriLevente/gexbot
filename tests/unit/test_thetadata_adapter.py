"""ThetaData v3 parsing, tier gating and the three-way chain join.

No network and no Theta Terminal: the transport is a stub. What is exercised is
the part that will actually be wrong on day one -- the join key, the missing-field
behaviour, and the tier guard.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.adapters.thetadata.client import (
    ThetaDataClient,
    ThetaDataError,
    assemble_chain,
    parse_csv,
    parse_expiration,
    parse_right,
    parse_root,
)
from src.adapters.thetadata.endpoints import (
    Endpoint,
    Tier,
    build_url,
    endpoints_for_tier,
    requires_shadow_gamma,
    tier_satisfies,
)
from src.domain.contracts import OptionRight, OptionRoot
from src.gex.config import GexEngineConfig
from src.gex.engine import compute_gex_snapshot
from src.gex.sessions import eastern

AS_OF = eastern(2026, 3, 17, 11, 0)

QUOTE_CSV = """timestamp,symbol,expiration,strike,right,bid_size,bid_exchange,bid,bid_condition,ask_size,ask_exchange,ask,ask_condition
2026-03-17T11:00:00.000,SPXW,2026-03-20,5000.00,call,10,1,12.30,0,12,1,12.60,0
2026-03-17T11:00:00.000,SPXW,2026-03-20,5000.00,put,8,1,10.10,0,9,1,10.40,0
2026-03-17T11:00:00.000,SPXW,2026-03-20,5050.00,call,5,1,6.20,0,6,1,6.50,0
"""

OI_CSV = """timestamp,symbol,expiration,strike,right,open_interest
2026-03-17T11:00:00.000,SPXW,2026-03-20,5000.00,call,4200
2026-03-17T11:00:00.000,SPXW,2026-03-20,5000.00,put,9100
2026-03-17T11:00:00.000,SPXW,2026-03-20,5050.00,call,3300
"""

FIRST_ORDER_CSV = """symbol,expiration,strike,right,timestamp,bid,ask,delta,theta,vega,rho,epsilon,lambda,implied_vol,iv_error,underlying_timestamp,underlying_price
SPXW,2026-03-20,5000.00,call,2026-03-17T11:00:00.000,12.30,12.60,0.51,-2.1,3.4,0.9,0.0,12.0,0.1812,0.0001,2026-03-17T11:00:00.000,5000.25
SPXW,2026-03-20,5000.00,put,2026-03-17T11:00:00.000,10.10,10.40,-0.49,-2.0,3.4,-0.8,0.0,-11.0,0.1834,0.0001,2026-03-17T11:00:00.000,5000.25
SPXW,2026-03-20,5050.00,call,2026-03-17T11:00:00.000,6.20,6.50,0.33,-1.8,3.0,0.6,0.0,15.0,0.1795,0.0001,2026-03-17T11:00:00.000,5000.25
"""

# The gamma column holds genuine Black-Scholes values for these inputs
# (S=5000.25, T=77h to the 16:00 ET SPXW settlement, r=4.2%, q=1.3%), not
# plausible-looking placeholders. Sanity check for the ATM rows:
# phi(0) / (S * sigma * sqrt(T)) = 0.3989 / (5000.25 * 0.1812 * 0.09376)
# = 0.004696, against the 0.004694 below -- the small gap is the drift term in
# d1. That makes the vendor-vs-shadow comparison below a real cross-check on the
# whole path: expiry parsing, settlement clock, year fraction and gamma.
SECOND_ORDER_CSV = """symbol,expiration,strike,right,timestamp,bid,ask,gamma,vanna,charm,vomma,veta,implied_vol,iv_error,underlying_timestamp,underlying_price
SPXW,2026-03-20,5000.00,call,2026-03-17T11:00:00.000,12.30,12.60,0.004694,1.1,0.2,4.0,0.3,0.1812,0.0001,2026-03-17T11:00:00.000,5000.25
SPXW,2026-03-20,5000.00,put,2026-03-17T11:00:00.000,10.10,10.40,0.004638,1.1,0.2,4.0,0.3,0.1834,0.0001,2026-03-17T11:00:00.000,5000.25
SPXW,2026-03-20,5050.00,call,2026-03-17T11:00:00.000,6.20,6.50,0.004042,1.0,0.2,3.6,0.3,0.1795,0.0001,2026-03-17T11:00:00.000,5000.25
"""


# --- Tier map ---------------------------------------------------------------


def test_gamma_needs_pro_but_implied_vol_only_needs_standard():
    """The finding that decides the subscription: gamma is a second-order greek.

    Standard gets IV, Pro gets gamma. Since the zero-gamma grid must reprice gamma
    in-house regardless, Standard + own Black-Scholes is $80/mo cheaper and
    internally consistent.
    """
    assert not tier_satisfies(Tier.STANDARD, Tier.PRO)
    assert Endpoint.OPTION_GREEKS_FIRST_ORDER in endpoints_for_tier(Tier.STANDARD)
    assert Endpoint.OPTION_GREEKS_SECOND_ORDER not in endpoints_for_tier(Tier.STANDARD)
    assert Endpoint.OPTION_GREEKS_SECOND_ORDER in endpoints_for_tier(Tier.PRO)


def test_quotes_and_open_interest_are_available_from_the_value_tier():
    available = endpoints_for_tier(Tier.VALUE)
    assert Endpoint.OPTION_QUOTE_SNAPSHOT in available
    assert Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT in available
    assert Endpoint.INDEX_PRICE_SNAPSHOT in available
    # ...but no greeks at all, so Value cannot feed the engine.
    assert Endpoint.OPTION_GREEKS_FIRST_ORDER not in available


def test_shadow_gamma_is_required_below_pro():
    assert requires_shadow_gamma(Tier.VALUE)
    assert requires_shadow_gamma(Tier.STANDARD)
    assert not requires_shadow_gamma(Tier.PRO)


def test_url_parameters_are_sorted_for_reproducibility():
    url = build_url(
        Endpoint.OPTION_QUOTE_SNAPSHOT, {"symbol": "SPXW", "expiration": "*"}
    )
    assert url.endswith("/v3/option/snapshot/quote?expiration=%2A&symbol=SPXW")


def test_client_refuses_an_endpoint_above_its_tier():
    client = ThetaDataClient(tier=Tier.STANDARD, transport=lambda url: "")
    with pytest.raises(ThetaDataError, match="requires the pro tier"):
        client.option_second_order_greeks(symbol="SPXW")


def test_client_allows_an_endpoint_at_its_tier():
    client = ThetaDataClient(tier=Tier.STANDARD, transport=lambda url: FIRST_ORDER_CSV)
    assert len(client.option_first_order_greeks(symbol="SPXW")) == 3


def test_unconfigured_transport_fails_loudly():
    """Better a clear error than a silent fallback to fake data."""
    with pytest.raises(NotImplementedError, match="data-requirements.md"):
        ThetaDataClient().option_quotes(symbol="SPXW")


# --- Parsing ----------------------------------------------------------------


def test_csv_parsing_reads_the_header_rather_than_assuming_column_order():
    rows = parse_csv("symbol,strike\nSPXW,5000.0\n")
    assert rows == [{"symbol": "SPXW", "strike": "5000.0"}]


def test_empty_response_parses_to_no_rows():
    assert parse_csv("") == []
    assert parse_csv("   \n  ") == []


@pytest.mark.parametrize("raw", ["2026-03-20", "20260320"])
def test_both_expiration_formats_are_accepted(raw):
    assert parse_expiration(raw) == date(2026, 3, 20)


def test_unrecognised_expiration_is_rejected():
    with pytest.raises(ThetaDataError, match="expiration format"):
        parse_expiration("March 20 2026")


@pytest.mark.parametrize("raw", ["call", "CALL", "c", "C"])
def test_call_right_variants(raw):
    assert parse_right(raw) is OptionRight.CALL


@pytest.mark.parametrize("raw", ["put", "P"])
def test_put_right_variants(raw):
    assert parse_right(raw) is OptionRight.PUT


def test_unknown_right_is_rejected():
    with pytest.raises(ThetaDataError, match="unrecognised right"):
        parse_right("straddle")


def test_spx_and_spxw_roots_are_distinguished():
    assert parse_root("SPX") is OptionRoot.SPX
    assert parse_root("spxw") is OptionRoot.SPXW


def test_unmodelled_root_is_rejected_rather_than_coerced():
    with pytest.raises(ThetaDataError, match="only SPX and SPXW"):
        parse_root("XSP")


# --- The join ---------------------------------------------------------------


def _chain(second_order: bool):
    return assemble_chain(
        as_of=AS_OF,
        spot=5000.25,
        quote_rows=parse_csv(QUOTE_CSV),
        open_interest_rows=parse_csv(OI_CSV),
        first_order_rows=parse_csv(FIRST_ORDER_CSV),
        second_order_rows=parse_csv(SECOND_ORDER_CSV) if second_order else None,
        open_interest_asof=date(2026, 3, 16),
        risk_free_rate=0.042,
        dividend_yield=0.013,
        options_feed_timestamp=AS_OF,
        spot_feed_timestamp=AS_OF,
    )


def test_join_matches_on_symbol_expiry_strike_and_right():
    chain = _chain(second_order=True)
    assert len(chain.quotes) == 3
    by_key = {q.contract.key: q for q in chain.quotes}
    call = by_key[("SPXW", date(2026, 3, 20), 5000.0, "call")]
    put = by_key[("SPXW", date(2026, 3, 20), 5000.0, "put")]
    assert call.open_interest == 4200
    assert put.open_interest == 9100
    assert call.bid == pytest.approx(12.30)
    assert call.implied_vol == pytest.approx(0.1812)
    assert put.implied_vol == pytest.approx(0.1834)
    assert call.gamma == pytest.approx(0.004694)


def test_calls_and_puts_at_the_same_strike_are_not_conflated():
    """The join key must include ``right``; without it one leg overwrites the
    other and every strike loses half its open interest.
    """
    chain = _chain(second_order=True)
    at_5000 = [q for q in chain.quotes if q.contract.strike == 5000.0]
    assert {q.contract.right for q in at_5000} == {OptionRight.CALL, OptionRight.PUT}
    assert {q.open_interest for q in at_5000} == {4200, 9100}


def test_standard_tier_join_yields_implied_vol_but_no_gamma():
    chain = _chain(second_order=False)
    assert all(q.gamma is None for q in chain.quotes)
    assert all(q.implied_vol is not None for q in chain.quotes)


def test_standard_tier_chain_still_produces_a_full_gex_snapshot():
    """End-to-end proof that the $80/mo tier is sufficient: no vendor gamma, and
    the engine still fills every view via the shadow pricer.
    """
    snapshot = compute_gex_snapshot(_chain(second_order=False))
    assert snapshot.contract_count == 3
    assert snapshot.total_unsigned_gex > 0.0
    assert snapshot.meta["vendor_gamma_count"] == 0
    assert snapshot.meta["shadow_gamma_count"] == 3


def test_vendor_gamma_and_shadow_gamma_agree_on_real_shaped_input():
    """ThetaData computes greeks with Black-Scholes, so the two paths must agree.

    This is the guard against a day-count, settlement-clock or discounting
    mismatch: the fixture's gamma column was computed from the documented inputs,
    so if the engine's own year-fraction or settlement time drifts, the shadow
    path stops matching it. Tolerance is set by the fixture's 6-decimal rounding.
    """
    vendor = compute_gex_snapshot(_chain(second_order=True), GexEngineConfig())
    shadow = compute_gex_snapshot(
        _chain(second_order=True), GexEngineConfig(prefer_vendor_gamma=False)
    )
    assert vendor.meta["vendor_gamma_count"] == 3
    assert shadow.meta["shadow_gamma_count"] == 3
    assert vendor.total_unsigned_gex == pytest.approx(
        shadow.total_unsigned_gex, rel=1e-3
    )


def test_wrong_settlement_clock_would_break_the_gamma_cross_check():
    """Negative control for the test above.

    Treating the SPXW series as AM-settled shortens time-to-expiry by seven
    hours, which visibly changes gamma. Without this, the cross-check could pass
    on a coincidence rather than on the clock being right.
    """
    from src.gex.pricing import BlackScholesInputs, gamma as bs_gamma, year_fraction
    from src.gex.sessions import seconds_to_expiry
    from src.domain.contracts import OptionRoot as Root

    pm = year_fraction(seconds_to_expiry(AS_OF, Root.SPXW, date(2026, 3, 20)))
    am = year_fraction(seconds_to_expiry(AS_OF, Root.SPX, date(2026, 3, 20)))
    assert am < pm
    args = dict(spot=5000.25, strike=5000.0, implied_vol=0.1812, rate=0.042,
                dividend_yield=0.013)
    correct = bs_gamma(BlackScholesInputs(time_to_expiry=pm, **args))
    wrong = bs_gamma(BlackScholesInputs(time_to_expiry=am, **args))
    assert correct == pytest.approx(0.004694, rel=1e-3)
    assert wrong != pytest.approx(correct, rel=1e-3)


def test_missing_open_interest_row_leaves_the_field_none():
    """A partial OI response must be visible to the engine, not papered over."""
    chain = assemble_chain(
        as_of=AS_OF,
        spot=5000.25,
        quote_rows=parse_csv(QUOTE_CSV),
        open_interest_rows=[],
        first_order_rows=parse_csv(FIRST_ORDER_CSV),
    )
    assert all(q.open_interest is None for q in chain.quotes)
    # ...and the engine then drops them and counts the loss.
    snapshot = compute_gex_snapshot(chain)
    assert snapshot.contract_count == 0
    assert snapshot.meta["dropped_no_open_interest"] == 3


def test_missing_greeks_row_leaves_iv_none():
    chain = assemble_chain(
        as_of=AS_OF,
        spot=5000.25,
        quote_rows=parse_csv(QUOTE_CSV),
        open_interest_rows=parse_csv(OI_CSV),
        first_order_rows=[],
    )
    assert all(q.implied_vol is None for q in chain.quotes)
    assert compute_gex_snapshot(chain).meta["dropped_no_gamma_source"] == 3


def test_extra_open_interest_rows_without_a_quote_are_ignored():
    """Quotes drive the iteration; an OI row for a contract with no book cannot
    contribute a spread or a crossed-market signal.
    """
    extra = OI_CSV + "2026-03-17T11:00:00.000,SPXW,2026-03-20,5100.00,call,999\n"
    chain = assemble_chain(
        as_of=AS_OF,
        spot=5000.25,
        quote_rows=parse_csv(QUOTE_CSV),
        open_interest_rows=parse_csv(extra),
        first_order_rows=parse_csv(FIRST_ORDER_CSV),
    )
    assert len(chain.quotes) == 3
    assert 5100.0 not in {q.contract.strike for q in chain.quotes}


def test_blank_numeric_fields_become_none_not_zero():
    """Zero open interest and unknown open interest mean different things."""
    blanked = (
        "timestamp,symbol,expiration,strike,right,open_interest\n"
        "2026-03-17T11:00:00.000,SPXW,2026-03-20,5000.00,call,\n"
    )
    chain = assemble_chain(
        as_of=AS_OF,
        spot=5000.25,
        quote_rows=parse_csv(QUOTE_CSV),
        open_interest_rows=parse_csv(blanked),
        first_order_rows=parse_csv(FIRST_ORDER_CSV),
    )
    call = next(
        q
        for q in chain.quotes
        if q.contract.strike == 5000.0 and q.contract.right is OptionRight.CALL
    )
    assert call.open_interest is None


def test_assembled_chain_carries_the_oi_asof_date_for_the_confidence_score():
    chain = _chain(second_order=True)
    assert all(q.open_interest_asof == date(2026, 3, 16) for q in chain.quotes)
    component = next(
        c
        for c in compute_gex_snapshot(chain).confidence.components
        if c.name == "oi_freshness"
    )
    assert component.score == pytest.approx(1.0)  # T-1 is the best achievable


def test_assembled_chain_is_stamped_eastern():
    chain = _chain(second_order=True)
    assert chain.as_of.tzinfo is not None
    assert chain.as_of.hour == 11
    assert chain.source == "thetadata"
