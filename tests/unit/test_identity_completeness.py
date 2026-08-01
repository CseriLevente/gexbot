"""Completeness is a statement about identities, not about cardinality.

The v2.1.1 defect. ``ChainCompleteness.status`` derived from
``joined_contract_count / expected_contract_count``, so a chain that received
exactly as many contracts as it expected reported ``MEASURED_COMPLETE`` --
regardless of whether they were the contracts it expected.

Two received where two were expected, but at different strikes, is not a
complete chain. It is a chain that is missing two contracts and has gained two
others, and the arithmetic cannot tell the difference because it never looked
at the identities. This is the same failure mode as v2.1's "expectation taken
from the response", one level down: a measure that cannot distinguish the thing
it measures from a coincidence of the same size.
"""

from __future__ import annotations

import json

import pytest

from src.adapters.thetadata.client import ChainCompleteness, CompletenessStatus


def measure(*, expected=None, received=(), source="contract_list", **overrides):
    """Build a measure from identity sets rather than from counts."""
    payload = {
        "received_quote_count": len(received),
        "received_oi_count": len(received),
        "received_iv_count": len(received),
        "received_greeks_count": 0,
        "expected_contract_ids": tuple(expected) if expected is not None else None,
        "received_contract_ids": tuple(received),
        "expected_source": source,
    }
    payload.update(overrides)
    return ChainCompleteness(**payload)


A, B, C, D = (
    "SPXW:2026-03-20:4900:call",
    ("SPXW:2026-03-20:4910.0000:call"),
    "SPXW:2026-03-20:4920.0000:call",
    "SPXW:2026-03-20:4930.0000:call",
)


# =============================================================================
# The defect
# =============================================================================


def test_equal_counts_with_completely_different_identities_is_not_complete():
    """The regression. v2.1.1 reported MEASURED_COMPLETE for this."""
    result = measure(expected=[A, B], received=[C, D])
    assert result.status is not CompletenessStatus.MEASURED_COMPLETE
    assert result.status is CompletenessStatus.MEASURED_INCOMPLETE


def test_one_missing_and_one_unexpected_is_not_complete():
    """The subtler version: the counts match because the errors cancel."""
    result = measure(expected=[A, B], received=[A, C])
    assert result.status is CompletenessStatus.MEASURED_INCOMPLETE
    assert result.missing_expected_identities == (B,)
    assert result.unexpected_received_identities == (C,)


def test_count_based_completeness_can_never_return():
    """A guard against the arithmetic creeping back in."""
    result = measure(expected=[A, B], received=[C, D])
    assert result.expected_identity_count == result.received_identity_count == 2
    assert result.matched_identity_count == 0
    assert result.identity_completeness_ratio == 0.0


def test_matching_counts_do_not_produce_a_ratio_of_one():
    assert measure(expected=[A, B], received=[C, D]).identity_completeness_ratio == 0.0


# =============================================================================
# The rule
# =============================================================================


def test_every_expected_identity_present_is_complete():
    assert measure(expected=[A, B], received=[A, B]).status is (
        CompletenessStatus.MEASURED_COMPLETE
    )


def test_extras_alongside_every_expected_identity_are_explicit():
    """Extras are a fact about the expectation, not about the chain."""
    result = measure(expected=[A, B], received=[A, B, C])
    assert result.status is CompletenessStatus.MEASURED_COMPLETE_WITH_EXTRAS
    assert result.unexpected_received_identities == (C,)
    assert result.missing_expected_identities == ()


def test_extras_never_hide_a_missing_expected_identity():
    """Two extras and one missing must not net out to complete."""
    result = measure(expected=[A, B], received=[A, C, D])
    assert result.status is CompletenessStatus.MEASURED_INCOMPLETE
    assert result.missing_expected_identities == (B,)


def test_a_subset_is_incomplete():
    result = measure(expected=[A, B, C], received=[A])
    assert result.status is CompletenessStatus.MEASURED_INCOMPLETE
    assert result.identity_completeness_ratio == pytest.approx(1 / 3)


def test_the_ratio_measures_matched_over_expected():
    result = measure(expected=[A, B, C, D], received=[A, B])
    assert result.identity_completeness_ratio == pytest.approx(0.5)


def test_extras_do_not_inflate_the_ratio_above_one():
    result = measure(expected=[A, B], received=[A, B, C, D])
    assert result.identity_completeness_ratio == pytest.approx(1.0)


# =============================================================================
# Determinism
# =============================================================================


def test_identity_order_does_not_change_the_verdict():
    forward = measure(expected=[A, B, C], received=[C, A])
    backward = measure(expected=[C, B, A], received=[A, C])
    assert forward.status is backward.status
    assert forward.as_dict() == backward.as_dict()


def test_missing_identity_sets_are_sorted():
    result = measure(expected=[D, C, B, A], received=[A])
    assert result.missing_expected_identities == (B, C, D)
    assert list(result.missing_expected_identities) == sorted(
        result.missing_expected_identities
    )


def test_unexpected_identity_sets_are_sorted():
    result = measure(expected=[A], received=[D, C, B, A])
    assert result.unexpected_received_identities == (B, C, D)


def test_serialised_metadata_is_deterministic():
    first = measure(expected=[C, A, B], received=[B, A])
    second = measure(expected=[A, B, C], received=[A, B])
    assert json.dumps(first.as_dict(), sort_keys=True) == json.dumps(
        second.as_dict(), sort_keys=True
    )


def test_duplicate_received_rows_do_not_improve_completeness():
    """Receiving the same contract three times is still one contract."""
    result = measure(expected=[A, B], received=[A, A, A])
    assert result.status is CompletenessStatus.MEASURED_INCOMPLETE
    assert result.matched_identity_count == 1
    assert result.missing_expected_identities == (B,)


def test_duplicate_expected_identities_are_deduplicated():
    result = measure(expected=[A, A, B], received=[A, B])
    assert result.expected_identity_count == 2
    assert result.status is CompletenessStatus.MEASURED_COMPLETE


# =============================================================================
# The reported fields
# =============================================================================


@pytest.mark.parametrize(
    "field",
    [
        "expected_identity_count",
        "received_identity_count",
        "matched_identity_count",
        "missing_expected_count",
        "unexpected_received_count",
        "missing_expected_identities",
        "unexpected_received_identities",
        "identity_completeness_ratio",
    ],
)
def test_every_required_field_is_reported(field):
    assert field in measure(expected=[A], received=[A]).as_dict(), field


def test_the_counts_agree_with_the_identity_sets():
    result = measure(expected=[A, B, C], received=[A, D])
    assert result.missing_expected_count == len(result.missing_expected_identities)
    assert result.unexpected_received_count == len(
        result.unexpected_received_identities
    )
    assert result.matched_identity_count == 1


def test_large_identity_sets_are_bounded_in_serialisation():
    """A 5,000-contract mismatch must not produce a 5,000-entry metadata blob."""
    expected = [f"SPXW:2026-03-20:{k}.0000:call" for k in range(5000)]
    payload = measure(expected=expected, received=[]).as_dict()
    assert payload["missing_expected_count"] == 5000
    assert len(payload["missing_expected_identities"]) <= 100
    assert payload["missing_expected_identities_truncated"] is True


def test_a_small_identity_set_is_not_marked_truncated():
    payload = measure(expected=[A, B], received=[A]).as_dict()
    assert payload["missing_expected_identities_truncated"] is False


# =============================================================================
# Interaction with the v2.1.1 behaviour that must survive
# =============================================================================


def test_no_independent_universe_is_still_partially_observed():
    result = measure(expected=None, received=[A, B], source="none")
    assert result.status is CompletenessStatus.PARTIALLY_OBSERVED
    assert result.expected_contract_count is None


def test_an_expectation_from_the_response_is_still_not_independent():
    result = measure(expected=[A, B], received=[A, B], source="quote_response")
    assert result.status is CompletenessStatus.PARTIALLY_OBSERVED


def test_an_empty_expectation_is_still_unknown():
    assert measure(expected=[], received=[]).status is CompletenessStatus.UNKNOWN


def test_measured_statuses_are_still_measured():
    for status in (
        CompletenessStatus.MEASURED_COMPLETE,
        CompletenessStatus.MEASURED_COMPLETE_WITH_EXTRAS,
        CompletenessStatus.MEASURED_INCOMPLETE,
    ):
        assert status.is_measured, status


def test_only_full_coverage_implies_complete():
    assert CompletenessStatus.MEASURED_COMPLETE.implies_complete
    assert CompletenessStatus.MEASURED_COMPLETE_WITH_EXTRAS.implies_complete
    assert not CompletenessStatus.MEASURED_INCOMPLETE.implies_complete
    assert not CompletenessStatus.PARTIALLY_OBSERVED.implies_complete


def test_extras_still_score_full_completeness_confidence():
    """Extras mean the expectation was wrong, not that the chain is short."""
    from src.adapters.thetadata.client import assemble_chain
    from src.gex.confidence import score_chain_completeness
    from src.gex.config import ConfidenceConfig
    from tests.unit.test_chain_completeness import build, cid
    from tests.unit.test_completeness_confidence import confidence_inputs

    snapshot = assemble_chain(
        build([4900, 4910], expected=(cid(4900),), source="contract_list")
    )
    assert snapshot.completeness_status is (
        CompletenessStatus.MEASURED_COMPLETE_WITH_EXTRAS
    )
    component = score_chain_completeness(
        confidence_inputs(snapshot), ConfidenceConfig()
    )
    assert component.score == pytest.approx(1.0)
