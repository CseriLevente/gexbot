"""Every vendor float is classified, every body is validated, every strike exact.

Four v2.1.1 defects with one shape: a value crossed the vendor boundary without
being told apart from its failure modes.

* **§5** ``parse_float_field`` existed but only ``bid`` and ``ask`` used it.
  IV, gamma, delta, theta, vega, the underlying price and the strike all still
  went through the lenient ``_to_float``, so a malformed vendor gamma became
  ``None`` -- indistinguishable from "the vendor sent no gamma" -- and silently
  triggered local fallback.
* **§15** A 200 response with an HTML body parsed into zero rows, and zero rows
  is a legitimate outcome. A vendor error page therefore became an empty chain.
* **§16** ``float(row["strike"])`` built the canonical identity, so ``"5000"``,
  ``"5000.0"`` and ``"5000.00"`` were the same by luck of float formatting and
  ``"NaN"`` produced an identity containing a NaN.
* **§17** Timestamp provenance recorded every source *inspected*, not the one
  actually *selected* for each contract.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.adapters.thetadata.client import (
    ChainAssemblyInputs,
    CsvBodyStatus,
    FloatParseIssue,
    ThetaDataSchemaError,
    assemble_chain,
    parse_strike,
    validate_csv_body,
)
from src.gex.sessions import eastern
from tests.unit.test_chain_completeness import greek_row, oi_row, quote_row

AS_OF = eastern(2026, 3, 17, 11, 0)


def chain_with(**row_overrides):
    quote, oi, greek = quote_row(4900), oi_row(4900), greek_row(4900)
    for key, value in row_overrides.items():
        for row in (quote, oi, greek):
            if key in row:
                row[key] = value
    return ChainAssemblyInputs(
        as_of=AS_OF,
        spot=5000.25,
        quote_rows=[quote],
        open_interest_rows=[oi],
        first_order_rows=[greek],
        open_interest_as_of=date(2026, 3, 16),
    )


def issues_for(**row_overrides) -> set[tuple[str, str]]:
    chain = assemble_chain(chain_with(**row_overrides))
    return set(chain.quotes[0].parse_issues) if chain.quotes else set()


# =============================================================================
# §5 -- every vendor float is structured
# =============================================================================


@pytest.mark.parametrize(
    "field",
    [
        "bid",
        "ask",
        "implied_vol",
        "delta",
        "theta",
        "vega",
        "underlying_price",
        "iv_error",
    ],
)
def test_a_malformed_value_is_recorded_for_every_float_field(field):
    """The regression: only bid and ask were structured in v2.1.1."""
    issues = issues_for(**{field: "oops"})
    assert any(name == field and "malformed" in code for name, code in issues), (
        f"{field} accepted a malformed value silently"
    )


@pytest.mark.parametrize("field", ["bid", "implied_vol", "delta", "underlying_price"])
def test_a_missing_value_is_not_recorded_as_an_issue(field):
    """Absence is ordinary; only corruption is a finding."""
    issues = issues_for(**{field: ""})
    assert not [name for name, _ in issues if name == field]


@pytest.mark.parametrize("value", ["NaN", "inf", "-inf"])
def test_a_non_finite_value_is_classified(value):
    issues = issues_for(implied_vol=value)
    assert any(
        name == "implied_vol" and code == FloatParseIssue.NON_FINITE_INPUT.value
        for name, code in issues
    )


def test_a_malformed_iv_is_distinct_from_a_missing_iv():
    assert issues_for(implied_vol="oops") != issues_for(implied_vol="")


# --- vendor gamma gets its own vocabulary -----------------------------------


def gamma_chain(second_order_value):
    quote, oi, greek = quote_row(4900), oi_row(4900), greek_row(4900)
    second = dict(greek)
    second["gamma"] = second_order_value
    return ChainAssemblyInputs(
        as_of=AS_OF,
        spot=5000.25,
        quote_rows=[quote],
        open_interest_rows=[oi],
        first_order_rows=[greek],
        second_order_rows=[second],
        open_interest_as_of=date(2026, 3, 16),
    )


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("oops", "VENDOR_GAMMA_MALFORMED"),
        ("NaN", "VENDOR_GAMMA_NON_FINITE"),
        ("", "VENDOR_GAMMA_MISSING"),
    ],
)
def test_vendor_gamma_failures_are_told_apart(value, code):
    """A corrupt gamma must not look like an absent one.

    v2.1.1 mapped all three to ``None``, so a malformed vendor gamma silently
    triggered local fallback and the snapshot recorded a shadow gamma with no
    indication that a vendor gamma had arrived and been unreadable.
    """
    chain = assemble_chain(gamma_chain(value))
    issues = set(chain.quotes[0].parse_issues)
    assert any(entry == code for _, entry in issues), (value, issues)


def test_a_malformed_gamma_does_not_silently_become_a_fallback():
    chain = assemble_chain(gamma_chain("oops"))
    assert chain.quotes[0].gamma is None
    assert any("MALFORMED" in code for _, code in chain.quotes[0].parse_issues)


def test_a_valid_gamma_produces_no_issue():
    chain = assemble_chain(gamma_chain("0.0021"))
    assert chain.quotes[0].gamma == pytest.approx(0.0021)
    assert not [c for _, c in chain.quotes[0].parse_issues if "GAMMA" in c]


def test_no_lenient_helper_remains_on_the_vendor_boundary():
    """Guard: assembly must not reach for the permissive reader."""
    import inspect

    from src.adapters.thetadata import client

    source = inspect.getsource(client.assemble_chain)
    assert "_to_float(" not in source, "assembly still uses the lenient float reader"


# =============================================================================
# §15 -- a 200 body must be valid CSV
# =============================================================================


def test_a_header_only_response_is_valid_and_empty():
    status, detail = validate_csv_body(
        "timestamp,symbol,expiration,strike,right,bid,ask\n", required=("symbol",)
    )
    assert status is CsvBodyStatus.VALID_EMPTY_CSV
    assert detail == ""


def test_an_empty_body_is_not_valid_csv():
    assert validate_csv_body("", required=("symbol",))[0] is CsvBodyStatus.INVALID_BODY


def test_a_whitespace_body_is_not_valid_csv():
    assert (
        validate_csv_body("   \n  ", required=("symbol",))[0]
        is CsvBodyStatus.INVALID_BODY
    )


def test_an_html_error_page_with_200_is_refused():
    """The regression: this parsed into zero rows and looked like an empty
    chain."""
    body = "<html><body><h1>500 Internal Server Error</h1></body></html>"
    assert (
        validate_csv_body(body, required=("symbol",))[0] is CsvBodyStatus.INVALID_BODY
    )


def test_a_plain_text_error_with_200_is_refused():
    assert (
        validate_csv_body("Service temporarily unavailable", required=("symbol",))[0]
        is CsvBodyStatus.INVALID_BODY
    )


def test_a_missing_required_header_is_reported():
    status, detail = validate_csv_body("a,b,c\n1,2,3\n", required=("symbol", "strike"))
    assert status is CsvBodyStatus.MISSING_HEADER
    assert "symbol" in detail


def test_a_blank_header_cell_is_malformed():
    status, _ = validate_csv_body("symbol,,strike\n", required=("symbol",))
    assert status is CsvBodyStatus.MALFORMED_HEADER


def test_a_duplicated_header_is_malformed():
    status, _ = validate_csv_body("symbol,symbol,strike\n", required=("symbol",))
    assert status is CsvBodyStatus.MALFORMED_HEADER


def test_unknown_extra_headers_are_tolerated():
    status, _ = validate_csv_body(
        "symbol,strike,brand_new_vendor_column\n", required=("symbol", "strike")
    )
    assert status is CsvBodyStatus.VALID_EMPTY_CSV


def test_a_utf8_bom_does_not_break_the_header():
    status, _ = validate_csv_body("﻿symbol,strike\n", required=("symbol", "strike"))
    assert status is CsvBodyStatus.VALID_EMPTY_CSV


def test_a_vendor_error_body_is_classified():
    status, _ = validate_csv_body(
        "error\nNo data for the requested symbol\n", required=("symbol",)
    )
    # A single-column body is not CSV in any useful sense, whichever label
    # it gets; what matters is that it is never mistaken for an empty chain.
    assert status is not CsvBodyStatus.VALID_EMPTY_CSV


def test_the_client_refuses_an_html_body_with_status_200():
    from src.adapters.thetadata.client import (
        ChainRequest,
        ThetaDataClient,
        ThetaDataSettings,
    )
    from src.adapters.transport import FakeTransport, HttpResponse

    client = ThetaDataClient(
        settings=ThetaDataSettings(),
        transport=FakeTransport(
            default=HttpResponse(status_code=200, text="<html>oh no</html>")
        ),
        clock=lambda: AS_OF,
    )
    with pytest.raises(ThetaDataSchemaError, match=r"(?i)csv|body"):
        client.option_quotes(ChainRequest(symbol="SPXW"))


def test_the_client_accepts_a_header_only_response():
    from src.adapters.thetadata.client import (
        ChainRequest,
        ThetaDataClient,
        ThetaDataSettings,
    )
    from src.adapters.transport import FakeTransport, HttpResponse

    header = ",".join(sorted(quote_row(0))) + "\n"
    client = ThetaDataClient(
        settings=ThetaDataSettings(),
        transport=FakeTransport(default=HttpResponse(status_code=200, text=header)),
        clock=lambda: AS_OF,
    )
    assert client.option_quotes(ChainRequest(symbol="SPXW")) == []


# =============================================================================
# §16 -- strike identity is exact
# =============================================================================


@pytest.mark.parametrize("text", ["5000", "5000.0", "5000.00", "5000.000", "+5000"])
def test_equivalent_strike_spellings_produce_one_identity(text):
    assert parse_strike(text)[0] == parse_strike("5000")[0]


def test_the_canonical_strike_is_deterministic():
    from decimal import Decimal

    value, issue = parse_strike("5000.50")
    assert issue is None
    assert value == Decimal("5000.50")


@pytest.mark.parametrize("text", ["NaN", "nan", "Infinity", "inf", "-inf"])
def test_a_non_finite_strike_is_refused(text):
    value, issue = parse_strike(text)
    assert value is None
    assert issue is not None


@pytest.mark.parametrize("text", ["abc", "5,000", "5000x", "", "  "])
def test_a_malformed_strike_is_refused(text):
    assert parse_strike(text)[0] is None


@pytest.mark.parametrize("text", ["-5000", "0", "0.00"])
def test_a_non_positive_strike_is_refused(text):
    assert parse_strike(text)[0] is None


def test_a_large_strike_stays_exact():
    from decimal import Decimal

    value, issue = parse_strike("9007199254740993.25")
    assert issue is None
    assert value == Decimal("9007199254740993.25")


def test_a_bad_strike_never_reaches_a_contract_identity():
    chain = assemble_chain(chain_with(strike="NaN"))
    assert chain.quotes == ()


def test_equivalent_formatting_is_one_contract_after_assembly():
    """Duplicate detection must be stable across formatting."""
    quote_a, quote_b = quote_row(4900), quote_row(4900)
    quote_b["strike"] = "4900.000"
    inputs = ChainAssemblyInputs(
        as_of=AS_OF,
        spot=5000.25,
        quote_rows=[quote_a, quote_b],
        open_interest_rows=[oi_row(4900)],
        first_order_rows=[greek_row(4900)],
        open_interest_as_of=date(2026, 3, 16),
        duplicate_policy="collapse_exact",
    )
    assert len(assemble_chain(inputs).quotes) == 1


# =============================================================================
# §17 -- provenance names the source actually selected
# =============================================================================


def selected(chain, index: int = 0) -> dict:
    return chain.quotes[index].timestamps.selected_sources()


def test_the_selected_iv_timestamp_source_is_recorded():
    chain = assemble_chain(chain_with())
    assert "implied_vol" in selected(chain)


def test_each_selected_source_is_named():
    payload = selected(assemble_chain(chain_with()))
    for key in ("quote", "implied_vol", "underlying", "open_interest"):
        assert key in payload, key


def test_the_recorded_source_matches_the_one_used():
    chain = assemble_chain(chain_with())
    payload = selected(chain)
    assert payload["implied_vol"]["source"] in (
        "first_order_greeks",
        "second_order_greeks",
        "implied_vol",
    )


def test_localisation_is_recorded_per_selected_source():
    payload = selected(assemble_chain(chain_with()))
    assert payload["quote"]["localization_applied"] is True


def test_an_aware_inspected_source_does_not_hide_a_naive_selected_one():
    """The regression this section exists for."""
    quote, oi, greek = quote_row(4900), oi_row(4900), greek_row(4900)
    quote["timestamp"] = "2026-03-17T11:00:00.000-04:00"  # aware
    greek["timestamp"] = "2026-03-17T11:00:00.000"  # naive, and it carries the IV
    inputs = ChainAssemblyInputs(
        as_of=AS_OF,
        spot=5000.25,
        quote_rows=[quote],
        open_interest_rows=[oi],
        first_order_rows=[greek],
        open_interest_as_of=date(2026, 3, 16),
    )
    payload = selected(assemble_chain(inputs))
    assert payload["quote"]["localization_applied"] is False
    assert payload["implied_vol"]["localization_applied"] is True


def test_a_fallback_underlying_source_is_named():
    payload = selected(assemble_chain(chain_with()))
    assert payload["underlying"]["source"]


def test_per_contract_provenance_reaches_the_snapshot():
    from src.gex.engine import compute_gex_snapshot

    produced = compute_gex_snapshot(assemble_chain(chain_with()))
    assert "selected_timestamp_sources" in produced.meta
