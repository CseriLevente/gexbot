"""The pilot summary validates every readiness report before reading it.

Review of v2.1.36 r2, finding 3: the summarizer hashed the bytes it received
but never checked a report's embedded semantic ``report_hash`` or reconciled
its counts, so a report whose ``usable_decisions`` had been raised from 7 to
372 (of 371 expected) was summarised -- with its stale hash, and again with a
freshly computed one. These tests hold the positive controls (the recorded
September 2 single-capture report at schema 2.1.35 and the r2 synthetic
session report at schema 2.1.36, both unmodified) and refuse every
manipulation of them. A matching hash is an integrity check on the report as
written; it is not vendor authenticity, and none of this is trading evidence.
"""

from __future__ import annotations

import copy
import json
import pathlib

import pytest

from src.domain.canonical import canonical_payload
from src.domain.digests import digest_of
from src.replay.pilot_readiness import READINESS_SCHEMA
from src.replay.pilot_summary import (
    ATTEMPT_EVIDENCE_BASIS,
    SCHEDULED_SCOPE_BASIS,
    SINGLE_CAPTURE_BASIS,
    SUMMARY_SCHEMA,
    PilotSummaryError,
    render_summary_markdown,
    semantic_report_hash,
    summarize_pilot,
    validate_readiness_report,
)
from src.replay.session_readiness import SESSION_READINESS_SCHEMA_2_1_36

REPO = pathlib.Path(__file__).resolve().parents[2]
RECORDED_2_1_35 = REPO / "docs" / "evidence" / "PILOT_READINESS_2026-09-02.json"
SYNTHETIC_2_1_36 = (
    REPO
    / "tests"
    / "fixtures"
    / "replay"
    / "session_readiness_2_1_36"
    / "session-readiness-2026-09-08-synthetic.json"
)


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_bytes())


def _rehashed(report: dict) -> dict:
    """The same report with a freshly computed semantic hash."""
    return {**report, "report_hash": semantic_report_hash(report)}


def _raw(report: dict) -> bytes:
    return (json.dumps(report) + "\n").encode()


def _refused(report: dict, match: str) -> None:
    with pytest.raises(PilotSummaryError, match=match):
        summarize_pilot([("altered.json", _raw(report))], minimum_sessions=1)


# -- positive controls -------------------------------------------------------------


def test_the_fixtures_are_the_unmodified_reports_their_hashes_describe():
    for path in (RECORDED_2_1_35, SYNTHETIC_2_1_36):
        report = _load(path)
        assert semantic_report_hash(report) == report["report_hash"]
        assert semantic_report_hash(report) == digest_of(
            canonical_payload({k: v for k, v in report.items() if k != "report_hash"})
        )
    assert _load(RECORDED_2_1_35)["schema_version"] == READINESS_SCHEMA
    assert _load(SYNTHETIC_2_1_36)["schema_version"] == SESSION_READINESS_SCHEMA_2_1_36


def test_the_recorded_2_1_35_report_is_accepted_and_restated_as_recorded():
    summary = summarize_pilot([(RECORDED_2_1_35.name, RECORDED_2_1_35.read_bytes())])
    assert summary["schema_version"] == SUMMARY_SCHEMA
    assert summary["synthetic_only"] is False
    assert summary["observed_source_origins"] == ["RECORDED_NORMALIZED"]
    row = summary["sessions"][0]
    assert row["kind"] == "SINGLE_CAPTURE"
    assert row["report_hash_verified"] is True
    assert row["usable_decisions"] == 0
    assert row["expected_decisions"] == 371
    assert row["requests"]["basis"] == SINGLE_CAPTURE_BASIS
    assert row["requests"]["scheduled"] == row["requests"]["attempted"] == 5
    assert row["requests"]["http_attempts"] is None
    assert summary["totals"]["usable_decisions"] == 0
    assert summary["totals"]["expected_decisions"] == 371
    assert summary["totals"]["requests"]["http_attempts"] is None
    assert summary["totals"]["requests"]["sessions_without_attempt_evidence"] == 1
    for flag in ("trusted_for_gex", "ready_for_backtest", "gex_computed"):
        assert summary[flag] is False


def test_the_r2_synthetic_session_report_is_accepted_on_its_own_basis():
    """The unmodified r2 report (the reviewer's positive control) summarises;
    its request count is restated as the scheduled scope it is, with the
    attempted and HTTP counts unknown rather than invented."""
    summary = summarize_pilot(
        [(SYNTHETIC_2_1_36.name, SYNTHETIC_2_1_36.read_bytes())], minimum_sessions=1
    )
    assert summary["synthetic_only"] is True
    assert "SYNTHETIC_SESSIONS_ONLY" in summary["option_side_blocking_reasons"]
    row = summary["sessions"][0]
    assert row["kind"] == "COLLECTION_SESSION"
    assert row["schema_version"] == SESSION_READINESS_SCHEMA_2_1_36
    assert row["report_hash_verified"] is True
    assert row["usable_decisions"] == 7
    assert row["expected_decisions"] == 371
    assert row["requests"]["basis"] == SCHEDULED_SCOPE_BASIS
    assert row["requests"]["scheduled"] == 35
    assert row["requests"]["attempted"] is None
    assert row["requests"]["http_attempts"] is None
    assert row["requests"]["budget"] == 1196
    assert summary["totals"]["usable_decisions"] == 7
    assert summary["totals"]["expected_decisions"] == 371
    assert summary["totals"]["requests"]["scheduled"] == 35
    assert summary["totals"]["requests"]["attempted"] is None
    markdown = render_summary_markdown(summary)
    assert "35 / unknown / unknown" in markdown
    assert "sessions without attempt evidence 1" in markdown
    assert summary["totals"]["requests"]["basis_by_session"] == {
        "2026-09-08": SCHEDULED_SCOPE_BASIS
    }
    assert (
        ATTEMPT_EVIDENCE_BASIS
        not in summary["totals"]["requests"]["basis_by_session"].values()
    )


def test_validation_returns_the_schema_of_a_valid_report():
    assert validate_readiness_report("a", _load(RECORDED_2_1_35)) == READINESS_SCHEMA
    assert (
        validate_readiness_report("b", _load(SYNTHETIC_2_1_36))
        == SESSION_READINESS_SCHEMA_2_1_36
    )


# -- the reviewer's reproduction -----------------------------------------------------


def test_a_report_with_a_stale_hash_is_refused():
    altered = _load(SYNTHETIC_2_1_36)
    altered["usable_decisions"] = altered["replay"]["expected_decisions"] + 1
    assert semantic_report_hash(altered) != altered["report_hash"]
    _refused(altered, "report_hash .* does not match the report's contents")


def test_a_freshly_hashed_report_with_impossible_counts_is_refused():
    altered = _load(SYNTHETIC_2_1_36)
    altered["usable_decisions"] = altered["replay"]["expected_decisions"] + 1
    altered = _rehashed(altered)
    assert semantic_report_hash(altered) == altered["report_hash"]
    _refused(altered, "top-level decision counts .* differ from the replay block")
    # Making the nested block agree exposes the impossible total instead.
    altered["replay"]["passing_decisions"] = altered["usable_decisions"]
    _refused(
        _rehashed(altered), "passing 372 \\+ blocked 364 decisions != expected 371"
    )


def test_the_recorded_report_is_refused_when_its_counts_are_raised_too():
    altered = _load(RECORDED_2_1_35)
    altered["usable_decisions"] = 1
    _refused(altered, "does not match the report's contents")
    altered["replay"]["passing_decisions"] = 1
    _refused(_rehashed(altered), "passing 1 \\+ blocked 371 decisions != expected 371")


# -- count validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("usable_decisions",), -1),
        (("usable_decisions",), 7.0),
        (("usable_decisions",), "7"),
        (("usable_decisions",), True),
        (("blocked_decisions",), -364),
        (("replay", "expected_decisions"), None),
        (("replay", "decisions_with_inventory"), -1),
        (("replay", "decisions_with_stale_inputs"), 2.5),
    ],
)
def test_negative_or_non_integer_counts_are_refused(path, value):
    altered = _load(SYNTHETIC_2_1_36)
    target = altered
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    _refused(_rehashed(altered), "must be a non-negative integer")


def test_derived_per_decision_counts_cannot_exceed_the_blocked_decisions():
    altered = _load(SYNTHETIC_2_1_36)
    blocked = altered["blocked_decisions"]
    altered["replay"]["decisions_with_stale_inputs"] = blocked + 1
    _refused(_rehashed(altered), "decisions_with_stale_inputs exceeds")
    altered = _load(SYNTHETIC_2_1_36)
    altered["replay"]["decisions_with_inventory"] = (
        altered["replay"]["expected_decisions"] + 1
    )
    _refused(_rehashed(altered), "decisions_with_inventory exceeds")
    altered = _load(SYNTHETIC_2_1_36)
    altered["replay"]["blocker_decision_counts"]["STALE_OPTION_QUOTE"] = blocked + 1
    _refused(_rehashed(altered), "counts more decisions than exist")


def test_inconsistent_nested_blocked_counts_are_refused():
    altered = _load(SYNTHETIC_2_1_36)
    altered["replay"]["blocked_decisions"] -= 1
    _refused(_rehashed(altered), "differ from the replay block")


def test_session_coverage_counts_must_agree_with_each_other_and_the_budget():
    altered = _load(SYNTHETIC_2_1_36)
    altered["session"]["cycles_executed"] += 1
    _refused(_rehashed(altered), "session cycle counts disagree")
    altered = _load(SYNTHETIC_2_1_36)
    altered["session"]["cycles_assembled"] = altered["session"]["cycles_executed"] + 1
    _refused(_rehashed(altered), "session cycle counts disagree")
    altered = _load(SYNTHETIC_2_1_36)
    altered["session"]["slots_by_status"]["EXECUTED"] = (
        altered["session"]["slots_planned"] + 1
    )
    _refused(_rehashed(altered), "counts more decisions than exist")
    altered = _load(SYNTHETIC_2_1_36)
    altered["session"]["requests_issued"] = (
        altered["session"]["request_budget"]["requests"] + 1
    )
    _refused(_rehashed(altered), "exceeds the approved budget")


# -- origin and verdict validation ----------------------------------------------------


def test_unknown_origins_and_a_disagreeing_synthetic_flag_are_refused():
    altered = _load(SYNTHETIC_2_1_36)
    altered["observed_source_origins"] = ["LIVE_VENDOR"]
    _refused(_rehashed(altered), "unknown source origins")
    altered = _load(SYNTHETIC_2_1_36)
    altered["synthetic_only"] = False
    _refused(_rehashed(altered), "synthetic_only False disagrees with origins")
    altered = _load(RECORDED_2_1_35)
    altered["synthetic_only"] = True
    _refused(_rehashed(altered), "synthetic_only True disagrees with origins")
    altered = _load(SYNTHETIC_2_1_36)
    altered["synthetic_only"] = "false"
    _refused(_rehashed(altered), "synthetic_only must be a boolean")
    altered = _load(SYNTHETIC_2_1_36)
    altered["observed_source_origins"] = "SYNTHETIC"
    _refused(_rehashed(altered), "must be a list of names")


def test_a_synthetic_report_relabelled_as_recorded_is_refused_not_promoted():
    """The separation of synthetic and recorded sessions holds: a synthetic
    report cannot be made recorded by editing one field, whichever field."""
    altered = _load(SYNTHETIC_2_1_36)
    altered["observed_source_origins"] = ["RECORDED_NORMALIZED"]
    _refused(_rehashed(altered), "synthetic_only True disagrees with origins")
    altered["synthetic_only"] = False
    # Now consistent as a document -- and it still is not summarised with a
    # recorded session of the same date twice, nor does the summary invent
    # authenticity: the report says recorded, so the summary says recorded,
    # and every trust flag stays false.
    summary = summarize_pilot(
        [("relabelled.json", _raw(_rehashed(altered)))], minimum_sessions=1
    )
    assert summary["authenticity_verified"] is False
    assert summary["normalization_verified"] is False
    assert summary["trusted_for_gex"] is False


def test_verdicts_must_be_booleans_that_agree_with_their_reasons():
    altered = _load(SYNTHETIC_2_1_36)
    altered["usable_for_intraday_pilot"] = True
    _refused(_rehashed(altered), "usable_for_intraday_pilot disagrees")
    altered = _load(SYNTHETIC_2_1_36)
    altered["usable_for_intraday_pilot"] = "no"
    _refused(_rehashed(altered), "usable_for_intraday_pilot must be a boolean")
    altered = _load(SYNTHETIC_2_1_36)
    altered["option_side_usable"] = False
    _refused(_rehashed(altered), "option_side_usable disagrees")
    altered = _load(SYNTHETIC_2_1_36)
    altered["blocking_reasons"] = "futures_costs"
    _refused(_rehashed(altered), "blocking_reasons must be a list of names")
    altered = _load(RECORDED_2_1_35)
    altered["blocking_reasons"] = [
        r for r in altered["blocking_reasons"] if r != "NO_PASSING_DECISIONS"
    ]
    _refused(_rehashed(altered), "NO_PASSING_DECISIONS is not a blocking reason")


def test_a_report_claiming_trust_or_orders_is_refused():
    for flag in ("trusted_for_gex", "ready_for_backtest", "authenticity_verified"):
        altered = _load(SYNTHETIC_2_1_36)
        altered[flag] = True
        _refused(_rehashed(altered), f"{flag} is true")
    altered = _load(SYNTHETIC_2_1_36)
    altered["orders_placed"] = 1
    _refused(_rehashed(altered), "orders_placed is not 0")


def test_a_report_without_a_hash_or_of_another_schema_is_refused():
    altered = _load(SYNTHETIC_2_1_36)
    del altered["report_hash"]
    _refused(altered, "carries no report_hash")
    altered = _load(SYNTHETIC_2_1_36)
    altered["schema_version"] = "research-pilot-readiness/2.1.37"
    _refused(_rehashed(altered), "unsupported readiness schema")
    altered = _load(SYNTHETIC_2_1_36)
    del altered["replay"]["decisions_with_inventory"]
    _refused(_rehashed(altered), "replay lacks")


def test_the_check_is_the_report_as_written_not_its_serialisation():
    """Re-serialising the same content (key order, whitespace) is not a change:
    the byte hash differs, the semantic hash and the verdict do not."""
    report = _load(SYNTHETIC_2_1_36)
    reordered = json.dumps(
        dict(reversed(list(report.items()))), indent=3, sort_keys=False
    ).encode()
    assert reordered != SYNTHETIC_2_1_36.read_bytes()
    summary = summarize_pilot([("reordered.json", reordered)], minimum_sessions=1)
    assert summary["sessions"][0]["report_hash"] == report["report_hash"]
    assert summary["sessions"][0]["usable_decisions"] == 7
    untouched = copy.deepcopy(report)
    assert validate_readiness_report("x", untouched) == SESSION_READINESS_SCHEMA_2_1_36
