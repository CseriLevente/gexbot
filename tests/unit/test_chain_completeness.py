"""Chain completeness measured against an independent expectation.

The v2 defect: ``expected_contract_count`` was ``len(quote_rows)`` -- the length
of the very response being judged. A vendor that silently truncated a chain to
its first page scored 100% complete, because the measure and the measured were
the same number. A completeness check that cannot fail is not a check.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.adapters.thetadata.client import (
    ChainAssemblyInputs,
    ChainCompleteness,
    assemble_chain,
)
from src.gex.sessions import eastern

AS_OF = eastern(2026, 3, 17, 11, 0)
SPOT = 5000.25
EXPIRY = "2026-03-20"
TS = "2026-03-17T11:00:00.000"


def cid(strike: int, right: str = "call") -> str:
    """The canonical id the joiner produces, spelled out once.

    ``4900``, not ``4900.0000``: v2.1.4 puts both sides of the identity through
    ``canonical_strike`` instead of a float and a ``.4f`` format. Written as a
    literal on purpose -- calling the production formatter here would make the
    test agree with itself whatever that formatter produced.
    """
    return f"SPXW:{EXPIRY}:{strike}:{right}"


def quote_row(strike: int, right: str = "call") -> dict[str, str]:
    return {
        "timestamp": TS,
        "symbol": "SPXW",
        "expiration": EXPIRY,
        "strike": f"{strike}.00",
        "right": right,
        "bid_size": "10",
        "bid_exchange": "1",
        "bid": "12.30",
        "bid_condition": "0",
        "ask_size": "12",
        "ask_exchange": "1",
        "ask": "12.80",
        "ask_condition": "0",
    }


def oi_row(strike: int, right: str = "call") -> dict[str, str]:
    return {
        "timestamp": TS,
        "symbol": "SPXW",
        "expiration": EXPIRY,
        "strike": f"{strike}.00",
        "right": right,
        "open_interest": "4200",
    }


def greek_row(strike: int, right: str = "call") -> dict[str, str]:
    return {
        "symbol": "SPXW",
        "expiration": EXPIRY,
        "strike": f"{strike}.00",
        "right": right,
        "timestamp": TS,
        "bid": "12.30",
        "ask": "12.80",
        "delta": "0.86745",
        "theta": "-2.1",
        "vega": "100.447603",
        "rho": "0.9",
        "epsilon": "0.0",
        "lambda": "12.0",
        "implied_vol": "0.197843",
        "iv_error": "0.0001",
        "underlying_timestamp": "2026-03-17T10:59:59.500",
        "underlying_price": "5000.25",
    }


def build(strikes, *, expected=None, source="none") -> ChainAssemblyInputs:
    return ChainAssemblyInputs(
        as_of=AS_OF,
        spot=SPOT,
        quote_rows=[quote_row(k) for k in strikes],
        open_interest_rows=[oi_row(k) for k in strikes],
        first_order_rows=[greek_row(k) for k in strikes],
        open_interest_as_of=date(2026, 3, 16),
        expected_contract_ids=expected,
        expected_source=source,
    )


def csv_for(strikes) -> str:
    """A response wide enough to satisfy every endpoint's required columns."""
    columns = sorted(set(quote_row(0)) | set(oi_row(0)) | set(greek_row(0)))
    lines = [",".join(columns)]
    for strike in strikes:
        row = {**quote_row(strike), **oi_row(strike), **greek_row(strike)}
        lines.append(",".join(row[column] for column in columns))
    return "\n".join(lines) + "\n"


def completeness_of(snapshot) -> dict:
    return snapshot.meta["chain_completeness"]


# =============================================================================
# The defect itself
# =============================================================================


def test_a_truncated_chain_is_not_reported_as_complete():
    """The regression the whole section exists for."""
    full = tuple(cid(k) for k in range(4950, 5060, 10))
    truncated = assemble_chain(
        build([4950, 4960], expected=full, source="contract_list")
    )
    payload = completeness_of(truncated)
    assert payload["status"] == "MEASURED_INCOMPLETE"
    assert payload["completeness_ratio"] < 1.0


def test_expectation_derived_from_the_response_is_not_treated_as_independent():
    """v2 inferred the expectation from the quote response. Refuse to score it."""
    ids = tuple(cid(k) for k in (4950, 4960))
    payload = completeness_of(
        assemble_chain(build([4950, 4960], expected=ids, source="quote_response"))
    )
    assert payload["independently_observed"] is False
    assert payload["status"] == "PARTIALLY_OBSERVED"


def test_without_an_independent_source_completeness_is_partially_observed():
    """The honest answer when no contract-list endpoint is wired."""
    payload = completeness_of(assemble_chain(build([4950, 4960, 4970])))
    assert payload["status"] == "PARTIALLY_OBSERVED"
    assert payload["expected_contract_count"] is None
    assert payload["completeness_ratio"] is None


def test_partially_observed_is_never_reported_as_complete():
    payload = completeness_of(assemble_chain(build([4950])))
    assert payload["status"] != "MEASURED_COMPLETE"


# =============================================================================
# Per-source counts
# =============================================================================


def test_each_source_is_counted_separately():
    inputs = ChainAssemblyInputs(
        as_of=AS_OF,
        spot=SPOT,
        quote_rows=[quote_row(k) for k in (4950, 4960, 4970)],
        open_interest_rows=[oi_row(k) for k in (4950, 4960)],
        first_order_rows=[greek_row(k) for k in (4950,)],
        open_interest_as_of=date(2026, 3, 16),
    )
    payload = completeness_of(assemble_chain(inputs))
    assert payload["received_quote_count"] == 3
    assert payload["received_oi_count"] == 2
    assert payload["received_iv_count"] == 1
    assert payload["received_greeks_count"] == 0


def test_the_joined_count_is_reported_separately_from_the_received_counts():
    """Three quotes that join into three contracts is a different fact from
    three quotes that join into one."""
    payload = completeness_of(assemble_chain(build([4950, 4960, 4970])))
    assert payload["received_quote_count"] == 3
    assert payload["joined_contract_count"] == 3


def test_missing_fields_are_attributed_to_the_source_that_lacked_them():
    inputs = ChainAssemblyInputs(
        as_of=AS_OF,
        spot=SPOT,
        quote_rows=[quote_row(k) for k in (4950, 4960)],
        open_interest_rows=[oi_row(4950)],
        first_order_rows=[greek_row(k) for k in (4950, 4960)],
        open_interest_as_of=date(2026, 3, 16),
    )
    payload = completeness_of(assemble_chain(inputs))
    assert payload["missing_by_source"]["open_interest"] == 1


def test_contracts_the_expectation_did_not_predict_are_surfaced():
    """An unexpected identity means the expectation is wrong, not the chain."""
    expected = (cid(4950),)
    payload = completeness_of(
        assemble_chain(build([4950, 4960], expected=expected, source="contract_list"))
    )
    assert payload["unexpected_identities"]


# =============================================================================
# The measure itself
# =============================================================================


def test_a_matching_universe_scores_complete():
    ids = tuple(cid(k) for k in (4950, 4960))
    payload = completeness_of(
        assemble_chain(build([4950, 4960], expected=ids, source="contract_list"))
    )
    assert payload["status"] == "MEASURED_COMPLETE"
    assert payload["completeness_ratio"] == pytest.approx(1.0)


def test_the_ratio_never_exceeds_one():
    """More contracts than expected is an expectation problem; it must not read
    as 120% complete."""
    # Twelve received where ten were expected, all ten of them present.
    expected = tuple(cid(k) for k in range(4900, 5000, 10))
    measure = ChainCompleteness(
        received_quote_count=12,
        received_oi_count=12,
        received_iv_count=12,
        received_greeks_count=0,
        expected_contract_ids=expected,
        received_contract_ids=(*expected, cid(5100), cid(5110)),
        expected_source="contract_list",
    )
    assert measure.completeness_ratio == pytest.approx(1.0)
    assert measure.unexpected_received_count == 2


def test_an_empty_expectation_does_not_divide_by_zero():
    """An empty universe claims nothing, so nothing is measured."""
    measure = ChainCompleteness(
        received_quote_count=0,
        received_oi_count=0,
        received_iv_count=0,
        received_greeks_count=0,
        expected_contract_ids=(),
        received_contract_ids=(),
        expected_source="contract_list",
    )
    assert measure.completeness_ratio is None
    assert measure.status.value == "UNKNOWN"


def test_the_expected_source_is_recorded_so_the_claim_can_be_audited():
    payload = completeness_of(
        assemble_chain(
            build([4950], expected=(cid(4950),), source="requested_universe")
        )
    )
    assert payload["expected_source"] == "requested_universe"


def test_completeness_travels_with_the_snapshot():
    snapshot = assemble_chain(build([4950, 4960]))
    assert "chain_completeness" in snapshot.meta


def test_the_client_passes_an_independent_universe_through(monkeypatch):
    """fetch_chain must not invent the expectation itself."""
    from src.adapters.thetadata.client import (
        ChainRequest,
        ThetaDataClient,
        ThetaDataSettings,
    )
    from src.adapters.transport import FakeTransport, HttpResponse

    transport = FakeTransport(
        default=HttpResponse(status_code=200, text=csv_for((4950, 4960)))
    )
    client = ThetaDataClient(
        settings=ThetaDataSettings(), transport=transport, clock=lambda: AS_OF
    )
    snapshot = client.fetch_chain(
        ChainRequest(symbol="SPXW"),
        as_of=AS_OF,
        spot=SPOT,
        spot_timestamp=AS_OF - timedelta(milliseconds=500),
        open_interest_as_of=date(2026, 3, 16),
        expected_contract_ids=tuple(cid(k) for k in (4950, 4960, 4970)),
        expected_source="contract_list",
    )
    payload = snapshot.meta["chain_completeness"]
    assert payload["expected_contract_count"] == 3
    assert payload["status"] == "MEASURED_INCOMPLETE"


def test_without_a_universe_the_client_reports_partially_observed():
    from src.adapters.thetadata.client import (
        ChainRequest,
        ThetaDataClient,
        ThetaDataSettings,
    )
    from src.adapters.transport import FakeTransport, HttpResponse

    transport = FakeTransport(
        default=HttpResponse(status_code=200, text=csv_for((4950,)))
    )
    snapshot = ThetaDataClient(
        settings=ThetaDataSettings(), transport=transport, clock=lambda: AS_OF
    ).fetch_chain(
        ChainRequest(symbol="SPXW"),
        as_of=AS_OF,
        spot=SPOT,
        spot_timestamp=AS_OF - timedelta(milliseconds=500),
        open_interest_as_of=date(2026, 3, 16),
    )
    assert snapshot.meta["chain_completeness"]["status"] == "PARTIALLY_OBSERVED"
