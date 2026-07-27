"""Unknown chain completeness must survive the trip to the confidence score.

The v2.1 defect. The adapter got this right:

    status = PARTIALLY_OBSERVED
    expected_contract_count = None

and then ``assemble_chain`` overwrote the count with ``len(quote_rows)`` before
handing the snapshot on, and ``score_chain_completeness`` fell back to
``result.usable_ratio`` -- which is received/received. Both layers independently
converted "we do not know the universe" into "we received everything we
expected", and the confidence model reported a perfect completeness score for a
chain that may have been truncated.

A fix at the adapter layer alone is not a fix. The property that matters is
end-to-end: an unknown universe cannot produce a full score.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.adapters.thetadata.client import (
    ChainCompleteness,
    CompletenessStatus,
    assemble_chain,
)
from src.domain.gex import OptionUniverse
from src.domain.model_spec import ModelSpec
from src.gex.confidence import ConfidenceInputs, score_chain_completeness
from src.gex.config import ConfidenceConfig, DataQualityLimits
from src.gex.engine import compute_gex_snapshot
from src.gex.formulas import compute_contract_gex
from src.gex.sessions import eastern
from src.synthetic.chains import build_synthetic_chain
from tests.unit.test_chain_completeness import build, cid

AS_OF = eastern(2026, 3, 17, 11, 0)


def confidence_inputs(snapshot, **overrides) -> ConfidenceInputs:
    """Build scorer inputs from a chain the way the engine does."""
    result = compute_contract_gex(snapshot)
    universe = OptionUniverse(
        total_contract_count=len(snapshot.quotes),
        included_contract_count=len(result.contracts),
        included_unsigned_gex=1.0e11,
        excluded_unsigned_gex=0.0,
    )
    base: dict[str, object] = {
        "as_of": snapshot.as_of,
        "result": result,
        "zero_gamma_results": (),
        "spot": snapshot.spot,
        "dte0_dominance_ratio": 0.35,
        "model_spec": ModelSpec(),
        "limits": DataQualityLimits(),
        "chain_universe": universe,
        "zero_gamma_universe": universe,
        "quotes": snapshot.quotes,
        "expected_contract_count": snapshot.expected_contract_count,
        "completeness_status": snapshot.completeness_status,
    }
    base.update(overrides)
    return ConfidenceInputs(**base)  # type: ignore[arg-type]


def score_for(snapshot, **overrides):
    return score_chain_completeness(
        confidence_inputs(snapshot, **overrides), ConfidenceConfig()
    )


# =============================================================================
# The count must survive assembly
# =============================================================================


def test_expected_contract_count_stays_none_without_an_independent_universe():
    """v2.1 substituted ``len(inputs.quote_rows)`` here."""
    snapshot = assemble_chain(build([4900, 4910, 4920]))
    assert snapshot.expected_contract_count is None


def test_the_snapshot_carries_the_completeness_status():
    snapshot = assemble_chain(build([4900, 4910]))
    assert snapshot.completeness_status is CompletenessStatus.PARTIALLY_OBSERVED


def test_an_independent_universe_is_preserved_as_a_number():
    ids = tuple(cid(k) for k in (4900, 4910))
    snapshot = assemble_chain(build([4900, 4910], expected=ids, source="contract_list"))
    assert snapshot.expected_contract_count == 2
    assert snapshot.completeness_status is CompletenessStatus.MEASURED_COMPLETE


def test_a_truncated_chain_is_measured_incomplete():
    full = tuple(cid(k) for k in range(4900, 5000, 10))
    snapshot = assemble_chain(
        build([4900, 4910], expected=full, source="contract_list")
    )
    assert snapshot.completeness_status is CompletenessStatus.MEASURED_INCOMPLETE


# =============================================================================
# The score must respect it
# =============================================================================


def test_partially_observed_never_scores_one():
    """The end-to-end property. This is the regression."""
    component = score_for(assemble_chain(build([4900, 4910, 4920])))
    assert component.score is None or component.score < 1.0


def test_unknown_never_scores_one():
    snapshot = assemble_chain(build([4900, 4910]))
    component = score_for(snapshot, completeness_status=CompletenessStatus.UNKNOWN)
    assert component.score is None or component.score < 1.0


def test_partially_observed_is_marked_uncalibrated():
    assert score_for(assemble_chain(build([4900, 4910]))).uncalibrated


def test_partially_observed_carries_a_deterministic_warning_code():
    first = score_for(assemble_chain(build([4900, 4910]))).warning_code
    second = score_for(assemble_chain(build([4900, 4920]))).warning_code
    assert first == "CHAIN_COMPLETENESS_NOT_INDEPENDENTLY_OBSERVED"
    assert first == second


def test_the_detail_does_not_imply_completeness():
    detail = score_for(assemble_chain(build([4900, 4910]))).detail.lower()
    assert "unknown" in detail or "not independently observed" in detail
    assert "100%" not in detail


def test_received_equals_usable_does_not_imply_completeness():
    """Every received quote being usable says nothing about what was not sent."""
    snapshot = assemble_chain(build([4900, 4910, 4920]))
    inputs = confidence_inputs(snapshot)
    assert inputs.result.usable_ratio == pytest.approx(1.0)
    component = score_chain_completeness(inputs, ConfidenceConfig())
    assert component.score is None or component.score < 1.0


def test_a_truncated_chain_cannot_report_full_completeness():
    full = tuple(cid(k) for k in range(4900, 5000, 10))
    component = score_for(
        assemble_chain(build([4900, 4910], expected=full, source="contract_list"))
    )
    assert component.score is not None
    assert component.score < 1.0
    assert not component.uncalibrated  # this one IS measured


def test_measured_completeness_still_scores_full_marks():
    """The fix must not punish a chain that genuinely is complete."""
    ids = tuple(cid(k) for k in (4900, 4910))
    component = score_for(
        assemble_chain(build([4900, 4910], expected=ids, source="contract_list"))
    )
    assert component.score == pytest.approx(1.0)
    assert not component.uncalibrated


def test_a_measured_chain_produces_no_completeness_warning_code():
    ids = tuple(cid(k) for k in (4900, 4910))
    component = score_for(
        assemble_chain(build([4900, 4910], expected=ids, source="contract_list"))
    )
    assert component.warning_code == ""


# =============================================================================
# The engine end-to-end
# =============================================================================


def test_a_synthetic_chain_declares_its_own_universe():
    """The generator knows exactly what it built, so it can say so.

    This is what keeps the fix from punishing every chain indiscriminately: an
    unknown universe is penalised, a known one is not.
    """
    chain = build_synthetic_chain()
    assert chain.expected_contract_count == len(chain.quotes)
    assert chain.completeness_status is CompletenessStatus.MEASURED_COMPLETE


def test_the_engine_warns_when_completeness_is_unknown():
    snapshot = compute_gex_snapshot(assemble_chain(build([4900, 4910, 4920])))
    assert any(
        "CHAIN_COMPLETENESS_NOT_INDEPENDENTLY_OBSERVED" in warning
        for warning in snapshot.confidence.warnings
    )


def test_unknown_completeness_lowers_the_overall_confidence_score():
    known = compute_gex_snapshot(build_synthetic_chain())
    unknown = compute_gex_snapshot(
        build_synthetic_chain().with_completeness(CompletenessStatus.UNKNOWN)
    )
    assert unknown.confidence.value < known.confidence.value


def test_unknown_completeness_makes_the_snapshot_uncalibrated():
    snapshot = compute_gex_snapshot(
        build_synthetic_chain().with_completeness(CompletenessStatus.UNKNOWN)
    )
    assert "chain_completeness" in snapshot.confidence.uncalibrated_components


# =============================================================================
# The measure itself
# =============================================================================


@pytest.mark.parametrize(
    ("expected", "source", "joined", "status"),
    [
        (None, "none", 5, CompletenessStatus.PARTIALLY_OBSERVED),
        (5, "quote_response", 5, CompletenessStatus.PARTIALLY_OBSERVED),
        (5, "contract_list", 5, CompletenessStatus.MEASURED_COMPLETE),
        (5, "contract_list", 3, CompletenessStatus.MEASURED_INCOMPLETE),
        (0, "contract_list", 0, CompletenessStatus.UNKNOWN),
    ],
)
def test_status_matrix(expected, source, joined, status):
    measure = ChainCompleteness(
        received_quote_count=joined,
        received_oi_count=joined,
        received_iv_count=joined,
        received_greeks_count=0,
        joined_contract_count=joined,
        expected_contract_count=expected,
        expected_source=source,
    )
    assert measure.status is status


def test_the_status_is_serialised_as_its_name():
    payload = ChainCompleteness(
        received_quote_count=1,
        received_oi_count=1,
        received_iv_count=1,
        received_greeks_count=0,
        joined_contract_count=1,
    ).as_dict()
    assert payload["status"] == "PARTIALLY_OBSERVED"


def test_open_interest_as_of_is_unaffected():
    """Guard against collateral damage in the assembly path."""
    snapshot = assemble_chain(build([4900, 4910]))
    assert snapshot.quotes[0].timestamps.open_interest_as_of == date(2026, 3, 16)
