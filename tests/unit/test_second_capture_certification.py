"""What the second live capture established, held in place.

The first capture proved the rate semantics by getting them wrong: it sent
``rate_value=4.2`` meaning 4.2% and the vendor priced 420%. This one sends
``0.042`` under the measured decimal reading, and the vendor prices 4.2% --
which is the same finding confirmed forwards, on a capture that is
economically valid.

As with the first capture, the raw payloads are not committed; what is
committed is ``tests/fixtures/live_capture/second_capture.json``, emitted by
the certification command rather than typed in.

This file also holds the v2.1.27 defect shut: a capture's evidence scope must
name *its own* session. v2.1.26 labelled this capture's contract universe
``"SPXW, session 2026-08-10, max_dte=60"`` -- correct identity sets, wrong
sentence about them, because the sentence was a constant.
"""

from __future__ import annotations

import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "live_capture"


@pytest.fixture(scope="module")
def second() -> dict:
    return json.loads((FIXTURES / "second_capture.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def first() -> dict:
    return json.loads((FIXTURES / "first_capture.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The corrected rate, confirmed forwards
# ---------------------------------------------------------------------------


def test_the_second_capture_priced_the_rate_it_meant(second):
    economics = second["rate_economics"]
    assert economics["wire_rate_value"] == 0.042
    assert economics["vendor_effective_rate"] == 0.042
    assert economics["intended_economic_rate"] == 0.042
    assert economics["capture_effective_rate_matches_intended_rate"] is True
    assert economics["intended_rate_source"] == "BOUND_TO_PREFLIGHT_APPROVAL"
    assert economics["magnitude_ratio"] == 1.0


def test_the_documentation_conflict_survives_a_correct_capture(second):
    """The vendor still documents a percent and still reads a decimal.

    A correct request does not repair the documentation, and the two facts have
    different lifetimes -- which is why v2.1.23 split them.
    """
    economics = second["rate_economics"]
    assert economics["observed_vendor_rate_unit"] == "DECIMAL_ANNUAL_RATE"
    assert economics["documented_rate_unit"] == "PERCENT_ANNUAL_RATE"
    assert economics["rate_units_documentation_live_conflict"] is True
    assert "RATE_UNITS" in second["documentation_live_conflicts"]


def test_the_decimal_reading_is_overwhelmingly_the_better_fit(second):
    scores = {row["hypothesis"]: row["delta_rmse"] for row in second["rate_semantics"]}
    decimal = next(v for k, v in scores.items() if "DECIMAL_ANNUAL_RATE" in k)
    percent = next(v for k, v in scores.items() if "PERCENT_ANNUAL_RATE" in k)
    assert decimal == pytest.approx(9.60473e-05, rel=1e-3)
    assert percent == pytest.approx(0.0179953, rel=1e-3)
    assert percent / decimal > 100
    assert second["inference_decisions"]["RATE_UNITS"] == "RESOLVED"


def test_the_pricing_conventions_are_reconfirmed(second):
    assert second["resolved_day_count"] == "ACT_365"
    assert second["resolved_expiration_clock"] == "16:00 America/New_York"
    assert second["inference_decisions"]["DAY_COUNT"] == "RESOLVED"
    assert second["inference_decisions"]["EXPIRATION_TIMESTAMP"] == "RESOLVED"
    assert second["inference_decisions"]["IV_PRICE_BASIS"] == "RESOLVED"
    basis = min(
        second["iv_basis_comparison"], key=lambda row: row["median_abs_iv_error"]
    )
    assert basis["basis"] == "NBBO_MID"


def test_the_intraday_clock_is_sixteen_hundred_by_a_clear_margin(second):
    scores = {
        row["hypothesis"]: row["delta_rmse"]
        for row in second["expiration_time_comparison"]
    }
    assert scores["16:00 America/New_York"] == pytest.approx(7.68263e-05, rel=1e-3)
    assert scores["15:55 America/New_York"] == pytest.approx(0.000323716, rel=1e-3)
    assert scores["16:05 America/New_York"] == pytest.approx(0.000285698, rel=1e-3)
    assert scores["16:00 America/New_York"] == min(scores.values())


def test_the_universe_matches_exactly(second):
    universe = second["universe"]
    assert universe["contract_list_count"] == 14670
    assert universe["quote_count"] == 14670
    assert universe["greeks_count"] == 14670
    assert universe["quote_matches_list"] is True
    assert universe["greeks_matches_list"] is True
    assert universe["state"] == "DEDICATED_CONTRACT_LIST_MATCHED_SNAPSHOT_UNIVERSE"


def test_the_open_interest_gap_is_smaller_but_still_there(second):
    coverage = second["open_interest_coverage"]
    assert coverage["oi_present"] + coverage["oi_explicit_zero"] == 14254
    assert coverage["oi_explicit_zero"] == 3596
    assert coverage["oi_missing"] == 416
    assert coverage["unexpected_oi_count"] == 0
    assert coverage["coverage_ratio"] == pytest.approx(0.971643, rel=1e-5)
    assert coverage["coverage_state"] == "OI_MISSING"
    assert coverage["permits_trusted_aggregate"] is False
    assert "2026-10-02" in coverage["fully_missing_expirations"]


def test_the_second_capture_is_still_not_trusted_for_gex(second):
    """One blocker left, and it is the open interest."""
    assert second["trusted_for_gex"] is False
    assert second["analytical_readiness"] == "ADAPTER_CERTIFICATION_EVIDENCE"
    blockers = second["gex_blockers"]
    assert len(blockers) == 1
    assert "416 contract identities have no open-interest row" in blockers[0]
    # The rate blocker that disqualified the first capture is gone.
    assert not any("factor of" in blocker for blocker in blockers)


def test_the_risk_free_rate_is_established_for_a_bound_capture(second):
    """Unlike the first capture, this one declared what it meant to buy."""
    assert second["rate_intent_binding"]["bound"] is True
    assert "RISK_FREE_RATE" in second["pricing_dimensions_supported_by_evidence"]
    assert "DIVIDEND_CONVENTION" in second["pricing_dimensions_still_unresolved"]


# ---------------------------------------------------------------------------
# Evidence scope is derived from the capture, not written into the code
# ---------------------------------------------------------------------------


def _universe_scope(report: dict) -> str:
    return next(
        observation["scope"]
        for observation in report["vendor_behavior"]["observations"]
        if observation["dimension"] == "CONTRACT_LIST_UNIVERSE"
    )


def test_each_capture_names_its_own_session(first, second):
    """The v2.1.26 defect: capture #2 labelled with capture #1's session."""
    assert "2026-08-12" in _universe_scope(second)
    assert "2026-08-10" not in _universe_scope(second)
    assert "2026-08-10" in _universe_scope(first)
    assert "2026-08-12" not in _universe_scope(first)


def test_the_scope_carries_the_request_it_was_derived_from(second):
    scope = _universe_scope(second)
    assert "SPXW" in scope
    assert "max_dte=60" in scope
    assert "capture-20260812T153415Z-a3c594606c4c64c0" in scope


def test_greeks_scopes_name_their_own_capture(first, second):
    for report, session in (
        (first, "capture-20260810T140129Z-2ef4f56270c1447b"),
        (second, "capture-20260812T153415Z-a3c594606c4c64c0"),
    ):
        for observation in report["vendor_behavior"]["observations"]:
            scope = observation.get("scope", "")
            if "first-order greeks in" in scope:
                assert session in scope


def test_the_two_captures_hash_differently(first, second):
    assert first["report_hash"] != second["report_hash"]
    assert len(second["report_hash"]) == 64
    assert second["schema_version"] == "capture-certification/2.1.27"


#: Values that belong to one particular capture. Fixtures are full of them --
#: that is what a fixture is. Implementation code containing one is code that
#: will be wrong about the next capture, and was: the universe scope named
#: 2026-08-10 for a capture taken on the 12th.
FIRST_CAPTURE_LITERALS = (
    "2026-08-10",
    "7759.27",
    "7759.54",
    "14556",
    "14,556",
    "14130",
    "14,130",
    "capture-20260810T140129Z",
)

#: The one place a session id may legitimately appear in a value: a citation
#: saying *where a reading was measured*. Nothing derives behaviour from it,
#: and removing the id would remove the provenance. Declared here rather than
#: pattern-matched, so adding a second exemption is a decision somebody makes
#: on purpose in a diff.
CITATION_EXEMPTIONS = frozenset({("config/pipeline.py", "LIVE_RATE_UNIT_REFERENCE")})


def test_no_first_capture_literal_survives_in_implementation_code():
    """Prose may cite the capture that motivated a fix. Code may not use it.

    The distinction is mechanical: a literal inside a string that some
    expression consumes is a value; a literal inside a comment or a docstring
    is a citation. This walks the AST so the two are actually told apart,
    rather than grepping and drowning in the docstrings that explain *why*
    every one of these numbers matters.
    """
    import ast

    source_root = pathlib.Path(__file__).resolve().parents[2] / "src"
    offenders: list[str] = []
    for module in sorted(source_root.rglob("*.py")):
        relative = module.relative_to(source_root).as_posix()
        tree = ast.parse(module.read_text(encoding="utf-8"))
        # Docstrings are expression statements holding a bare constant; every
        # other string constant is one the code actually uses.
        docstrings = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        # Assignments whose target is an exempted citation constant.
        exempt: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if any((relative, name) in CITATION_EXEMPTIONS for name in names):
                exempt.update(id(child) for child in ast.walk(node.value))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or id(node) in docstrings:
                continue
            if id(node) in exempt or not isinstance(node.value, str):
                continue
            for literal in FIRST_CAPTURE_LITERALS:
                if literal in node.value:
                    offenders.append(f"{relative}:{node.lineno} contains {literal!r}")
    assert not offenders, "first-capture literals in implementation code: " + "; ".join(
        offenders
    )
