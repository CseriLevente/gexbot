"""Parser and boundary integrity: duplicates, strict integers, future OI.

Written before the fixes. Each group names the v2 defect it reproduces.

The common theme: **corruption must not become absence.** A truncated decimal, a
silently-overwritten duplicate and a `None` returned from a crash all look like
"the vendor sent nothing", which is the one thing they are not.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date

import pytest

from src.adapters.thetadata.client import (
    DuplicateRowError,
    IntegerParseIssue,
    index_rows,
    parse_int_field,
)
from src.domain.validation import ValidationCode
from src.gex.confidence import ConfidenceInputs, score_oi_freshness
from src.gex.config import ConfidenceConfig
from src.gex.engine import compute_gex_snapshot
from src.gex.formulas import compute_contract_gex
from src.gex.sessions import eastern
from src.synthetic.chains import build_synthetic_chain, with_quote

AS_OF = eastern(2026, 3, 17, 11, 0)
CFG = ConfidenceConfig()


def oi_row(strike: str = "5000.00", **overrides: str) -> dict[str, str]:
    base = {
        "timestamp": "2026-03-17T11:00:00.000",
        "symbol": "SPXW",
        "expiration": "2026-03-20",
        "strike": strike,
        "right": "call",
        "open_interest": "4200",
    }
    base.update(overrides)
    return base


# =============================================================================
# DEFECT: duplicate rows silently resolved by last-write-wins
# =============================================================================


def test_exact_duplicates_are_collapsed_deterministically():
    """Identical payloads are not a conflict -- there is nothing to choose."""
    rows = [oi_row(), oi_row()]
    indexed = index_rows(rows, endpoint="open_interest")
    assert len(indexed.rows) == 1
    assert indexed.duplicate_count == 1
    assert indexed.selection_rule == "exact_duplicate_collapsed"


def test_conflicting_duplicates_are_rejected_by_default():
    """v2 bug: ``{row_key(r): r for r in rows}`` kept whichever row came last.

    Two rows disagreeing about open interest is a real conflict. Silently taking
    the last one means the answer depends on vendor row order, which is exactly
    what the replay guarantee forbids.
    """
    rows = [oi_row(open_interest="4200"), oi_row(open_interest="9999")]
    with pytest.raises(DuplicateRowError, match="conflicting"):
        index_rows(rows, endpoint="open_interest")


def test_duplicate_row_order_does_not_change_the_outcome():
    forward = [oi_row(open_interest="4200"), oi_row(open_interest="9999")]
    reversed_rows = list(reversed(forward))
    with pytest.raises(DuplicateRowError) as first:
        index_rows(forward, endpoint="open_interest")
    with pytest.raises(DuplicateRowError) as second:
        index_rows(reversed_rows, endpoint="open_interest")
    assert str(first.value) == str(second.value)


def test_timestamp_resolution_selects_the_newest_when_enabled():
    rows = [
        oi_row(open_interest="4200", timestamp="2026-03-17T10:59:00.000"),
        oi_row(open_interest="9999", timestamp="2026-03-17T11:00:00.000"),
    ]
    indexed = index_rows(
        rows, endpoint="open_interest", duplicate_policy="newest_timestamp"
    )
    assert len(indexed.rows) == 1
    assert next(iter(indexed.rows.values()))["open_interest"] == "9999"
    assert indexed.selection_rule == "newest_timestamp"


def test_timestamp_resolution_is_order_independent():
    rows = [
        oi_row(open_interest="4200", timestamp="2026-03-17T10:59:00.000"),
        oi_row(open_interest="9999", timestamp="2026-03-17T11:00:00.000"),
    ]
    forward = index_rows(
        rows, endpoint="open_interest", duplicate_policy="newest_timestamp"
    )
    backward = index_rows(
        list(reversed(rows)),
        endpoint="open_interest",
        duplicate_policy="newest_timestamp",
    )
    assert forward.rows == backward.rows


def test_missing_timestamps_do_not_permit_silent_resolution():
    """Without a discriminator there is no deterministic winner."""
    rows = [
        oi_row(open_interest="4200", timestamp=""),
        oi_row(open_interest="9999", timestamp=""),
    ]
    with pytest.raises(DuplicateRowError, match="no usable timestamp"):
        index_rows(rows, endpoint="open_interest", duplicate_policy="newest_timestamp")


def test_tied_timestamps_are_rejected_rather_than_arbitrated():
    rows = [
        oi_row(open_interest="4200", timestamp="2026-03-17T11:00:00.000"),
        oi_row(open_interest="9999", timestamp="2026-03-17T11:00:00.000"),
    ]
    with pytest.raises(DuplicateRowError, match="tie"):
        index_rows(rows, endpoint="open_interest", duplicate_policy="newest_timestamp")


def test_duplicate_report_names_the_key_and_the_discarded_records():
    rows = [oi_row(), oi_row()]
    indexed = index_rows(rows, endpoint="open_interest")
    report = indexed.duplicates[0]
    assert report.duplicate_key
    assert report.duplicate_count == 2
    assert report.selection_rule
    assert report.selected_record_identifier
    assert report.discarded_record_identifiers


def test_distinct_contracts_are_not_duplicates():
    rows = [oi_row(strike="5000.00"), oi_row(strike="5050.00")]
    assert len(index_rows(rows, endpoint="open_interest").rows) == 2


def test_duplicates_across_every_endpoint_are_checked():
    for endpoint in ("quote", "open_interest", "first_order", "second_order"):
        with pytest.raises(DuplicateRowError):
            index_rows(
                [oi_row(open_interest="1"), oi_row(open_interest="2")],
                endpoint=endpoint,
            )


# =============================================================================
# DEFECT: int(float(value)) truncates and crashes
# =============================================================================


@pytest.mark.parametrize("raw", ["12", " 12 ", "+12"])
def test_integer_forms_are_accepted(raw):
    value, issue = parse_int_field(raw, field="open_interest")
    assert value == 12
    assert issue is None


def test_exact_integer_decimal_is_accepted_and_documented():
    """``"12.0"`` is exactly representable as an integer, so it is accepted.

    ``"12.9"`` is not, and truncating it would invent data.
    """
    assert parse_int_field("12.0", field="open_interest") == (12, None)


def test_non_integer_decimal_is_rejected_not_truncated():
    """v2 bug: ``int(float("12.9"))`` silently returned 12."""
    value, issue = parse_int_field("12.9", field="open_interest")
    assert value is None
    assert issue is IntegerParseIssue.NON_INTEGER_INPUT


@pytest.mark.parametrize("raw", ["NaN", "nan", "inf", "-inf", "Infinity"])
def test_non_finite_integer_input_is_classified(raw):
    """v2 bug: ``int(float("NaN"))`` raised ValueError -- an uncaught crash in the
    adapter, not a parse failure.
    """
    value, issue = parse_int_field(raw, field="open_interest")
    assert value is None
    assert issue is IntegerParseIssue.NON_FINITE_INPUT


@pytest.mark.parametrize("raw", ["abc", "1,2", "1e", "--3", "0x10"])
def test_malformed_integer_input_is_classified(raw):
    value, issue = parse_int_field(raw, field="open_interest")
    assert value is None
    assert issue is IntegerParseIssue.MALFORMED_VALUE


def test_missing_value_is_distinct_from_malformed():
    """Absence and corruption are different facts and must not share a code."""
    assert parse_int_field(None, field="open_interest")[1] is (
        IntegerParseIssue.MISSING_VALUE
    )
    assert parse_int_field("", field="open_interest")[1] is (
        IntegerParseIssue.MISSING_VALUE
    )
    assert parse_int_field("   ", field="open_interest")[1] is (
        IntegerParseIssue.MISSING_VALUE
    )


def test_negative_value_is_rejected_where_disallowed():
    value, issue = parse_int_field("-5", field="open_interest", allow_negative=False)
    assert value is None
    assert issue is IntegerParseIssue.NEGATIVE_VALUE


def test_negative_value_is_accepted_where_allowed():
    assert parse_int_field("-5", field="sequence", allow_negative=True) == (-5, None)


def test_out_of_range_value_is_classified():
    value, issue = parse_int_field(
        "99999999999", field="open_interest", maximum=1_000_000
    )
    assert value is None
    assert issue is IntegerParseIssue.OUT_OF_RANGE


def test_scientific_notation_that_is_an_exact_integer_is_accepted():
    assert parse_int_field("1e3", field="open_interest") == (1000, None)


def test_scientific_notation_that_is_not_an_integer_is_rejected():
    assert parse_int_field("1.5e0", field="open_interest")[1] is (
        IntegerParseIssue.NON_INTEGER_INPUT
    )


@pytest.mark.parametrize("raw", ["0.1", "0.5", "1.0000001", "99.999", "-0.5", "2.5"])
def test_no_decimal_is_ever_truncated(raw):
    """Property-style sweep: every non-integral decimal must be refused."""
    value, issue = parse_int_field(raw, field="open_interest", allow_negative=True)
    if float(raw).is_integer():
        assert value == int(float(raw))
    else:
        assert value is None
        assert issue is IntegerParseIssue.NON_INTEGER_INPUT


# =============================================================================
# DEFECT: future-dated open interest scored perfect freshness
# =============================================================================


def make_inputs(*, as_of, oi_as_of):
    chain = build_synthetic_chain()
    result = compute_contract_gex(chain)
    from src.domain.gex import OptionUniverse
    from src.domain.model_spec import ModelSpec
    from src.domain.timestamps import DataQualityLimits

    universe = OptionUniverse(
        total_contract_count=1,
        included_contract_count=1,
        included_unsigned_gex=1.0,
        excluded_unsigned_gex=0.0,
    )
    return ConfidenceInputs(
        as_of=as_of,
        result=result,
        zero_gamma_results=(),
        spot=5000.0,
        dte0_dominance_ratio=0.3,
        model_spec=ModelSpec(),
        limits=DataQualityLimits(),
        chain_universe=universe,
        zero_gamma_universe=universe,
        open_interest_as_of=oi_as_of,
    )


def test_same_day_open_interest_is_fresh():
    component = score_oi_freshness(
        make_inputs(as_of=AS_OF, oi_as_of=date(2026, 3, 17)), CFG
    )
    assert component.score == pytest.approx(1.0)
    assert not component.hard_failure


def test_previous_trading_day_open_interest_is_fresh():
    component = score_oi_freshness(
        make_inputs(as_of=AS_OF, oi_as_of=date(2026, 3, 16)), CFG
    )
    assert component.score == pytest.approx(1.0)


def test_friday_settlement_read_on_monday_is_fresh():
    monday = eastern(2026, 3, 23, 11, 0)
    component = score_oi_freshness(
        make_inputs(as_of=monday, oi_as_of=date(2026, 3, 20)), CFG
    )
    assert component.score == pytest.approx(1.0)


def test_holiday_weekend_open_interest_is_fresh():
    tuesday_after_memorial_day = eastern(2026, 5, 26, 11, 0)
    component = score_oi_freshness(
        make_inputs(as_of=tuesday_after_memorial_day, oi_as_of=date(2026, 5, 22)), CFG
    )
    assert component.score == pytest.approx(1.0)


def test_future_calendar_day_open_interest_is_a_hard_failure():
    """v2 bug: ``sessions_between`` returns 0 when end <= start, so an OI date in
    the future aged to "0 sessions" and scored a perfect 1.0.

    Settlement data cannot be from the future. Unlike a quote clock, this gets no
    sub-second skew tolerance: OI is a *date*, and tomorrow's date is not clock
    drift.
    """
    component = score_oi_freshness(
        make_inputs(as_of=AS_OF, oi_as_of=date(2026, 3, 18)), CFG
    )
    assert component.score == 0.0
    assert component.hard_failure
    assert "future" in component.detail.lower()


def test_future_trading_day_open_interest_is_a_hard_failure():
    component = score_oi_freshness(
        make_inputs(as_of=AS_OF, oi_as_of=date(2026, 3, 20)), CFG
    )
    assert component.score == 0.0
    assert component.hard_failure


def test_far_future_open_interest_is_a_hard_failure():
    component = score_oi_freshness(
        make_inputs(as_of=AS_OF, oi_as_of=date(2027, 1, 4)), CFG
    )
    assert component.score == 0.0
    assert component.hard_failure


def test_future_open_interest_zeroes_the_whole_confidence_score():
    """DATA_HALT-eligible, so it must not be averaged away."""
    chain = build_synthetic_chain()
    poisoned = chain
    for index in range(len(chain.quotes)):
        poisoned = with_quote(
            poisoned,
            index,
            timestamps=replace(
                chain.quotes[index].timestamps,
                open_interest_as_of=date(2026, 3, 25),
            ),
        )
    produced = compute_gex_snapshot(poisoned)
    assert produced.confidence.value == 0.0
    assert "oi_freshness" in produced.confidence.hard_failures


def test_future_open_interest_is_machine_readable_in_warnings():
    chain = build_synthetic_chain()
    poisoned = with_quote(
        chain,
        0,
        timestamps=replace(
            chain.quotes[0].timestamps, open_interest_as_of=date(2026, 3, 25)
        ),
    )
    produced = compute_gex_snapshot(poisoned)
    assert any("HARD FAILURE" in w for w in produced.warnings)


def test_future_open_interest_is_flagged_by_record_validation():
    chain = build_synthetic_chain()
    poisoned = with_quote(
        chain,
        0,
        timestamps=replace(
            chain.quotes[0].timestamps, open_interest_as_of=date(2026, 3, 25)
        ),
    )
    result = compute_contract_gex(poisoned)
    assert result.validation.count(ValidationCode.FUTURE_OPEN_INTEREST) >= 1


def test_replay_cannot_normalise_future_oi_into_zero_sessions_stale():
    """The specific normalisation the v2 code performed."""
    from src.gex.calendar import sessions_between

    assert sessions_between(date(2026, 3, 25), date(2026, 3, 17)) == 0
    # ...which is why the freshness component must check the ordering itself
    # rather than trusting the session count.
    component = score_oi_freshness(
        make_inputs(as_of=AS_OF, oi_as_of=date(2026, 3, 25)), CFG
    )
    assert component.hard_failure


def test_dst_transition_does_not_make_valid_oi_look_future_dated():
    """Spring forward: 2026-03-09 is the Monday after the transition."""
    monday = eastern(2026, 3, 9, 11, 0)
    component = score_oi_freshness(
        make_inputs(as_of=monday, oi_as_of=date(2026, 3, 6)), CFG
    )
    assert component.score == pytest.approx(1.0)
    assert not component.hard_failure


def test_late_utc_evening_does_not_make_todays_oi_look_future_dated():
    """23:30 UTC is 19:30 ET on the same session day."""
    from datetime import datetime

    late_utc = datetime(2026, 3, 17, 23, 30, tzinfo=UTC)
    component = score_oi_freshness(
        make_inputs(as_of=late_utc, oi_as_of=date(2026, 3, 17)), CFG
    )
    assert not component.hard_failure
    assert component.score == pytest.approx(1.0)
