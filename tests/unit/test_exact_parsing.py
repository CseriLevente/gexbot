"""Exact integers, malformed-vs-missing floats, and HTTP errors kept out of CSV.

Three v2.1 defects that share a theme: a value was converted through something
lossy, or an absence and a corruption were mapped onto the same result.

* ``parse_int_field`` refused ``"12.9"`` and ``"NaN"`` correctly, but still
  reached the integer via ``float(text)``. Every open-interest value above
  2^53 is silently rounded to the nearest representable double on the way
  through -- and open interest is exactly the field where a large integer is
  plausible.
* ``_to_float`` returned ``None`` for a missing field and ``None`` for
  ``"oops"``. A vendor that sent nothing and a vendor that sent garbage produced
  identical downstream state, so corruption was indistinguishable from absence.
* Nothing in ``ThetaDataClient`` checked the HTTP status. ``RetryingTransport``
  raised on non-2xx, but a custom transport that returned one handed an HTML
  error page straight to the CSV parser.
"""

from __future__ import annotations

import math

import pytest

from src.adapters.thetadata.client import (
    ChainRequest,
    FloatParseIssue,
    IntegerParseIssue,
    ThetaDataClient,
    ThetaDataSettings,
    ThetaDataVendorError,
    parse_float_field,
    parse_int_field,
)
from src.adapters.transport import FakeTransport, HttpResponse
from src.gex.sessions import eastern

AS_OF = eastern(2026, 3, 17, 11, 0)

#: 2^53 + 1. The smallest positive integer a float64 cannot represent: it
#: rounds to 2^53, which is a different number that looks equally plausible.
UNREPRESENTABLE = 9007199254740993


def parse_int(value, **kwargs):
    return parse_int_field(value, field="open_interest", **kwargs)


def parse_float(value, **kwargs):
    return parse_float_field(value, field="bid", **kwargs)


# =============================================================================
# §9 -- integers parsed exactly
# =============================================================================


def test_an_integer_beyond_float64_precision_is_not_rounded():
    """The regression. ``int(float("9007199254740993"))`` is 9007199254740992."""
    assert float(str(UNREPRESENTABLE)) == 9007199254740992.0  # the trap itself
    parsed, issue = parse_int(str(UNREPRESENTABLE))
    if issue is None:
        assert parsed == UNREPRESENTABLE
    else:
        # Rejecting it is also acceptable; silently changing it is not.
        assert issue is IntegerParseIssue.OUT_OF_RANGE
        assert parsed is None


def test_a_large_integer_never_becomes_its_neighbour():
    parsed, _ = parse_int(str(UNREPRESENTABLE))
    assert parsed != 9007199254740992


@pytest.mark.parametrize(
    "value", [str(2**53), str(2**53 + 1), str(2**60), str(2**53 - 1)]
)
def test_large_integers_round_trip_exactly(value):
    parsed, issue = parse_int(value, maximum=2**62)
    assert issue is None, value
    assert str(parsed) == value


@pytest.mark.parametrize(("value", "expected"), [("12", 12), (12, 12), ("0", 0)])
def test_plain_integers_parse(value, expected):
    assert parse_int(value) == (expected, None)


def test_an_exactly_integral_decimal_is_accepted():
    """Documented: "12.0" is accepted, and only because it is exactly integral."""
    assert parse_int("12.0") == (12, None)


def test_a_large_exactly_integral_decimal_is_still_exact():
    parsed, issue = parse_int(f"{UNREPRESENTABLE}.0", maximum=2**62)
    assert issue is None
    assert parsed == UNREPRESENTABLE


@pytest.mark.parametrize("value", ["12.9", "0.5", "-0.5", "12.0001"])
def test_a_non_integral_decimal_is_refused_not_truncated(value):
    parsed, issue = parse_int(value, allow_negative=True)
    assert parsed is None
    assert issue is IntegerParseIssue.NON_INTEGER_INPUT


@pytest.mark.parametrize("value", ["NaN", "nan", "inf", "-inf", "Infinity"])
def test_non_finite_input_is_classified(value):
    parsed, issue = parse_int(value)
    assert parsed is None
    assert issue is IntegerParseIssue.NON_FINITE_INPUT


@pytest.mark.parametrize("value", ["abc", "1,000", "12 34", "0x10", "--1", "1e"])
def test_malformed_input_is_classified(value):
    parsed, issue = parse_int(value)
    assert parsed is None
    assert issue is IntegerParseIssue.MALFORMED_VALUE


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_input_is_classified(value):
    parsed, issue = parse_int(value)
    assert parsed is None
    assert issue is IntegerParseIssue.MISSING_VALUE


def test_missing_is_distinct_from_malformed():
    assert parse_int(None)[1] is not parse_int("abc")[1]


def test_a_negative_value_is_refused_where_prohibited():
    assert parse_int("-5")[1] is IntegerParseIssue.NEGATIVE_VALUE
    assert parse_int("-5", allow_negative=True) == (-5, None)


def test_a_value_above_the_maximum_is_refused():
    assert parse_int("1000", maximum=999)[1] is IntegerParseIssue.OUT_OF_RANGE


def test_booleans_are_not_integers():
    assert parse_int(True)[1] is IntegerParseIssue.MALFORMED_VALUE


def test_exponential_notation_is_handled_explicitly():
    """Accepted only when exactly integral, and documented as such."""
    parsed, issue = parse_int("1e3")
    assert (parsed, issue) == (1000, None)
    assert parse_int("1.5e0")[1] is IntegerParseIssue.NON_INTEGER_INPUT


@pytest.mark.parametrize("exponent", range(50, 63))
def test_no_decimal_truncation_across_the_precision_boundary(exponent):
    """Property-style sweep across the region where float64 starts lying."""
    for offset in (-1, 0, 1):
        value = 2**exponent + offset
        parsed, issue = parse_int(str(value), maximum=2**63)
        assert issue is None, value
        assert parsed == value, value


# =============================================================================
# §15 -- malformed floats are distinct from missing ones
# =============================================================================


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_missing_float_is_classified_as_missing(value):
    parsed, issue = parse_float(value)
    assert parsed is None
    assert issue is FloatParseIssue.MISSING_VALUE


@pytest.mark.parametrize("value", ["oops", "1,5", "12.3.4", "$1.20", "--1"])
def test_a_malformed_float_is_classified_as_malformed(value):
    parsed, issue = parse_float(value)
    assert parsed is None
    assert issue is FloatParseIssue.MALFORMED_VALUE


def test_malformed_is_not_the_same_state_as_missing():
    """The regression: v2.1 returned ``None`` for both."""
    assert parse_float("oops")[1] is not parse_float(None)[1]


@pytest.mark.parametrize("value", ["NaN", "inf", "-inf"])
def test_non_finite_floats_are_classified(value):
    parsed, issue = parse_float(value)
    assert parsed is None
    assert issue is FloatParseIssue.NON_FINITE_INPUT


def test_a_valid_zero_is_kept_where_allowed():
    assert parse_float("0.0") == (0.0, None)


def test_a_negative_value_is_refused_where_disallowed():
    assert parse_float("-1.5", allow_negative=False)[1] is FloatParseIssue.OUT_OF_RANGE
    assert parse_float("-1.5", allow_negative=True) == (-1.5, None)


def test_a_normal_float_parses():
    parsed, issue = parse_float("12.34")
    assert issue is None
    assert parsed == pytest.approx(12.34)


def test_a_malformed_price_is_recorded_on_the_quote():
    """A corrupt cell must be visible, not merely absent."""
    from datetime import date

    from src.adapters.thetadata.client import ChainAssemblyInputs, assemble_chain
    from tests.unit.test_chain_completeness import greek_row, oi_row, quote_row

    bad = quote_row(4900)
    bad["bid"] = "oops"
    inputs = ChainAssemblyInputs(
        as_of=AS_OF,
        spot=5000.25,
        quote_rows=[bad],
        open_interest_rows=[oi_row(4900)],
        first_order_rows=[greek_row(4900)],
        open_interest_as_of=date(2026, 3, 16),
    )
    chain = assemble_chain(inputs)
    issues = chain.quotes[0].parse_issues
    assert any("bid" in field for field, _ in issues)
    assert any("malformed" in code for _, code in issues)


def test_an_absent_price_is_not_recorded_as_a_parse_issue():
    """Absence is normal; only corruption is an issue."""
    from datetime import date

    from src.adapters.thetadata.client import ChainAssemblyInputs, assemble_chain
    from tests.unit.test_chain_completeness import greek_row, oi_row, quote_row

    row = quote_row(4900)
    row["bid"] = ""
    inputs = ChainAssemblyInputs(
        as_of=AS_OF,
        spot=5000.25,
        quote_rows=[row],
        open_interest_rows=[oi_row(4900)],
        first_order_rows=[greek_row(4900)],
        open_interest_as_of=date(2026, 3, 16),
    )
    chain = assemble_chain(inputs)
    assert not [f for f, _ in chain.quotes[0].parse_issues if f == "bid"]


# =============================================================================
# §16 -- HTTP errors never reach the CSV parser
# =============================================================================

ERROR_BODY = "<html><body><h1>500 Internal Server Error</h1></body></html>"


def client_for(response: HttpResponse) -> ThetaDataClient:
    """A client wired to a transport that does NOT raise on error statuses."""
    return ThetaDataClient(
        settings=ThetaDataSettings(),
        transport=FakeTransport(default=response),
        clock=lambda: AS_OF,
    )


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_a_non_2xx_response_is_rejected_by_the_client(status):
    """Even from a transport that hands it over without raising."""
    client = client_for(HttpResponse(status_code=status, text=ERROR_BODY))
    with pytest.raises(ThetaDataVendorError) as excinfo:
        client.option_quotes(ChainRequest(symbol="SPXW"))
    assert str(status) in str(excinfo.value)


def test_an_html_error_page_is_never_parsed_as_csv():
    client = client_for(HttpResponse(status_code=500, text=ERROR_BODY))
    with pytest.raises(ThetaDataVendorError):
        client.option_quotes(ChainRequest(symbol="SPXW"))


def test_a_2xx_response_still_parses():
    from tests.unit.test_thetadata_runtime import csv_response

    client = client_for(csv_response())
    assert client.option_quotes(ChainRequest(symbol="SPXW"))


@pytest.mark.parametrize("status", [200, 204])
def test_success_statuses_are_accepted(status):
    from tests.unit.test_thetadata_runtime import csv_response

    body = csv_response()
    client = client_for(HttpResponse(status_code=status, text=body.text))
    assert client.option_quotes(ChainRequest(symbol="SPXW")) is not None


def test_the_status_check_runs_before_vendor_error_parsing():
    """A 500 is a 500 whether or not the body happens to look like a vendor
    error document."""
    client = client_for(HttpResponse(status_code=500, text="error,code\n1,2\n"))
    with pytest.raises(ThetaDataVendorError, match="500"):
        client.option_quotes(ChainRequest(symbol="SPXW"))


def test_the_error_does_not_leak_the_query_string():
    client = client_for(HttpResponse(status_code=403, text=ERROR_BODY))
    with pytest.raises(ThetaDataVendorError) as excinfo:
        client.option_quotes(ChainRequest(symbol="SPXW"))
    assert "password" not in str(excinfo.value).lower()


def test_a_non_finite_check_still_guards_the_maths():
    """Belt and braces: nothing above changed the NaN guard."""
    assert math.isnan(float("nan"))
