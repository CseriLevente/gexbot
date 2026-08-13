"""What two captures establish about open-interest availability.

Two halves. The mechanism is exercised against generated captures, where the
transitions are arranged and the classification can be checked against what was
put in. The *conclusions* come from the two real captures, through the
committed fixture, and no synthetic row appears anywhere near them.

The distinction this module's tests exist to protect is the one between an
observation and a rule. Every count below is a thing that was seen. None of
them is permission to treat a missing open-interest row as a zero.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta

import pytest

from src.adapters.thetadata.capture_certification import (
    capture_universe,
    load_capture,
)
from src.adapters.thetadata.oi_transition import (
    LONGITUDINAL_OI_SCHEMA_VERSION,
    OI_TRANSITION_ALGORITHM_VERSION,
    CaptureComparisonError,
    TransitionClass,
    compare_captures,
)
from tests.synthetic_capture import EASTERN, SyntheticVendor, write_capture

FIXTURE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "fixtures"
    / "live_capture"
    / "oi_transition_first_to_second.json"
)


@pytest.fixture(scope="module")
def transition() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The real longitudinal findings
# ---------------------------------------------------------------------------


def test_the_two_captures_are_the_ones_that_were_taken(transition):
    earlier, later = transition["earlier_capture"], transition["later_capture"]
    assert earlier["session_id"] == "capture-20260810T140129Z-2ef4f56270c1447b"
    assert later["session_id"] == "capture-20260812T153415Z-a3c594606c4c64c0"
    assert earlier["manifest_hash"] == (
        "2f45534bbb569dfeb3e251b4fe3e27a8bdebbb716d5c0ac5b22f821d43ecbd20"
    )
    assert later["manifest_hash"] == (
        "8d713b91a9d6260510efb16bf961fad56aaacd1fbc1c86d24a8afa97f20dad99"
    )
    assert transition["session_distance_days"] == 2


def test_the_first_captures_missing_identities_are_accounted_for(transition):
    """426 unanswered on 2026-08-10, and where each one went."""
    counts = transition["transition_counts"]
    rolled = transition["transition_rollups"]

    assert transition["earlier_capture"]["oi_missing_count"] == 426
    # 422 were still listed two days later; 4 were not.
    assert rolled["PREVIOUSLY_MISSING_SURVIVING"] == 422
    assert counts["REMOVED_OR_EXPIRED_PREVIOUS_OI_MISSING"] == 4
    assert rolled["PREVIOUSLY_MISSING_SURVIVING"] + 4 == 426


def test_every_surviving_missing_identity_was_answered_two_days_later(transition):
    """422 of 422. The observation the whole release exists to obtain."""
    counts = transition["transition_counts"]
    assert counts["PREVIOUSLY_MISSING_OI_NOW_ZERO"] == 206
    assert counts["PREVIOUSLY_MISSING_OI_NOW_POSITIVE"] == 216
    assert counts["PREVIOUSLY_MISSING_OI_STILL_MISSING"] == 0
    assert transition["transition_rollups"]["PREVIOUSLY_MISSING_RESOLVED"] == 422


def test_the_second_captures_missing_identities_are_all_new_contracts(transition):
    """416 unanswered on 2026-08-12, none of them previously listed."""
    counts = transition["transition_counts"]
    assert transition["later_capture"]["oi_missing_count"] == 416
    assert counts["NEW_CONTRACT_OI_MISSING"] == 416
    # Nothing that had been answered stopped being answered ...
    assert counts["PRESENT_BOTH_OI_NOW_MISSING"] == 0
    # ... and nothing stayed unanswered across both.
    assert counts["PREVIOUSLY_MISSING_OI_STILL_MISSING"] == 0


def test_the_new_contract_accounting(transition):
    rolled = transition["transition_rollups"]
    counts = transition["transition_counts"]
    assert rolled["NEW_CONTRACTS"] == 676
    assert rolled["NEW_CONTRACTS_WITH_OI_ROW"] == 260
    assert (
        counts["NEW_CONTRACT_OI_PRESENT_ZERO"]
        + counts["NEW_CONTRACT_OI_PRESENT_POSITIVE"]
        == 260
    )
    assert rolled["NEW_CONTRACTS"] - rolled["NEW_CONTRACTS_WITH_OI_ROW"] == 416


def test_the_accounting_is_exhaustive_and_mutually_exclusive(transition):
    """Every identity in either universe lands in exactly one class."""
    assert transition["accounting_is_exhaustive"] is True
    counts = transition["transition_counts"]
    assert sum(counts.values()) == transition["union_universe_count"]
    assert transition["classified_identity_count"] == transition["union_universe_count"]
    # 14,556 and 14,670 universes overlapping in 13,994.
    assert transition["union_universe_count"] == 15232


def test_a_wholly_new_expiration_has_no_open_interest_at_all(transition):
    """2026-10-02: 218 listed, 218 new, 218 unanswered."""
    row = next(
        entry
        for entry in transition["per_expiration"]
        if entry["expiration"] == "2026-10-02"
    )
    assert row["expected_contracts"] == 218
    assert row["new_contracts"] == 218
    assert row["existing_contracts"] == 0
    assert row["oi_present"] == 0
    assert row["oi_missing"] == 218
    assert row["transition_counts"] == {"NEW_CONTRACT_OI_MISSING": 218}


def test_missing_open_interest_only_occurs_where_contracts_are_new(transition):
    """The association, stated as what it is -- an association.

    In every expiration carrying unanswered open interest, the count of
    unanswered contracts never exceeds the count of newly listed ones. That is
    a fact about these two captures. It is not a rule, and
    :func:`test_the_report_refuses_to_become_a_policy` holds the line.
    """
    for row in transition["per_expiration"]:
        if row["oi_missing"]:
            assert row["new_contracts"] >= row["oi_missing"], row["expiration"]


def test_every_class_carries_a_set_hash(transition):
    """Counts alone would let two different sets of 426 look identical."""
    hashes = transition["transition_identity_hashes"]
    assert set(hashes) == {member.value for member in TransitionClass}
    for name, count in transition["transition_counts"].items():
        assert len(hashes[name]) == 64
        if count:
            assert hashes[name] != hashes["PREVIOUSLY_MISSING_OI_STILL_MISSING"]


def test_the_report_refuses_to_become_a_policy(transition):
    """Observation is not imputation, and the report has to say so."""
    assert transition["analytical_evidence_status"] == [
        "OI_SEMANTICS_LONGITUDINAL_EVIDENCE_AVAILABLE",
        "OI_IMPUTATION_POLICY_UNRESOLVED",
    ]
    policy = transition["imputation_policy"]
    assert policy.startswith("NONE.")
    assert "does not establish" in policy
    assert "does not authorise dropping" in policy


def test_the_findings_are_sentences_about_these_two_captures(transition):
    joined = " ".join(transition["longitudinal_findings"])
    for count in ("426", "422", "416", "676", "260"):
        assert count in joined
    # Nothing in the findings tells a reader what to do about it.
    for forbidden in ("may be treated as", "can be assumed", "safe to drop"):
        assert forbidden not in joined


def test_the_report_is_content_addressed(transition):
    assert len(transition["transition_report_hash"]) == 64
    assert transition["schema_version"] == LONGITUDINAL_OI_SCHEMA_VERSION
    assert transition["algorithm_version"] == OI_TRANSITION_ALGORITHM_VERSION


# ---------------------------------------------------------------------------
# The mechanism, on generated captures
# ---------------------------------------------------------------------------

_MONDAY = datetime(2026, 8, 10, 10, 1, 34, tzinfo=EASTERN)


def _pair(tmp_path: pathlib.Path, *, later_overrides: dict) -> tuple:
    """Two captures two days apart, differing only as instructed."""
    earlier = write_capture(
        tmp_path / "t0",
        SyntheticVendor(
            wire_rate_value=0.042, declared_economic_rate=0.042, valuation=_MONDAY
        ),
    )
    later = write_capture(
        tmp_path / "t1",
        SyntheticVendor(
            wire_rate_value=0.042,
            declared_economic_rate=0.042,
            valuation=_MONDAY + timedelta(days=2),
            **later_overrides,
        ),
    )
    return earlier, later


def test_resolving_every_missing_row_is_classified_as_resolution(tmp_path):
    earlier, later = _pair(tmp_path, later_overrides={"complete_open_interest": True})
    report = compare_captures(earlier, later)
    counts = report.class_counts

    assert len(report.earlier.missing) > 0
    assert len(report.later.missing) == 0
    # Every earlier-missing identity is either resolved, still missing, or
    # gone from the universe -- and those three exhaust it. Two days apart the
    # universes genuinely differ, so "all of them resolved" is the wrong
    # invariant even here; the accounting one is what has to hold.
    assert counts["PREVIOUSLY_MISSING_OI_NOW_ZERO"] + counts[
        "PREVIOUSLY_MISSING_OI_NOW_POSITIVE"
    ] + counts["PREVIOUSLY_MISSING_OI_STILL_MISSING"] + counts[
        "REMOVED_OR_EXPIRED_PREVIOUS_OI_MISSING"
    ] == len(report.earlier.missing)
    # Nothing that survived stayed unanswered, because the later capture
    # answers every identity it lists.
    assert counts["PREVIOUSLY_MISSING_OI_STILL_MISSING"] == 0
    assert report.rollups["PREVIOUSLY_MISSING_RESOLVED"] > 0
    assert report.accounting_is_exhaustive


def test_a_row_that_disappears_is_classified_as_a_regression(tmp_path):
    """Answered before, unanswered now. The class exists so it cannot hide."""
    earlier, later = _pair(
        tmp_path, later_overrides={"complete_open_interest": False, "oi_gap": 11}
    )
    report = compare_captures(earlier, later)
    # A denser gap leaves identities unanswered that the earlier capture had
    # answered, which is exactly the regression class.
    assert report.class_counts["PRESENT_BOTH_OI_NOW_MISSING"] > 0
    assert report.accounting_is_exhaustive


def test_new_and_removed_expirations_are_classified(tmp_path):
    from datetime import date as _date

    earlier, later = _pair(
        tmp_path,
        later_overrides={
            "expirations": (_date(2026, 8, 24), _date(2026, 9, 16), _date(2026, 10, 2)),
            "front_week_end": _date(2026, 8, 14),
        },
    )
    report = compare_captures(earlier, later)
    counts = report.class_counts
    rolled = report.rollups
    # 2026-08-12 vanished; 2026-10-02 appeared.
    assert rolled["REMOVED_OR_EXPIRED"] > 0
    assert rolled["NEW_CONTRACTS"] > 0
    assert counts["NEW_CONTRACT_OI_MISSING"] >= 0
    assert report.accounting_is_exhaustive
    expirations = {row.expiration for row in report.per_expiration}
    assert "2026-10-02" in expirations
    assert "2026-08-12" not in expirations


def test_every_identity_is_classified_exactly_once(tmp_path):
    earlier, later = _pair(tmp_path, later_overrides={"complete_open_interest": True})
    report = compare_captures(earlier, later)
    union = set(report.earlier.expected) | set(report.later.expected)
    assert set(report.classified) == union
    assert sum(report.class_counts.values()) == len(union)
    # And the per-class identity lists partition the union.
    seen: set = set()
    for member in TransitionClass:
        identities = set(report.identities_in(member))
        assert not (seen & identities), member
        seen |= identities
    assert seen == union


# ---------------------------------------------------------------------------
# Determinism and integrity
# ---------------------------------------------------------------------------


def test_the_hash_is_stable_across_runs_and_insertion_order(tmp_path):
    earlier, later = _pair(tmp_path, later_overrides={"complete_open_interest": True})
    first = compare_captures(earlier, later)
    second = compare_captures(earlier, later)
    assert first.transition_report_hash() == second.transition_report_hash()

    # Reordering the classification dict must not move the digest: identities
    # are sorted before hashing, so insertion order is not evidence.
    shuffled = dict(reversed(list(first.classified.items())))
    reordered = type(first)(
        schema_version=first.schema_version,
        algorithm_version=first.algorithm_version,
        earlier=first.earlier,
        later=first.later,
        session_distance_days=first.session_distance_days,
        classified=shuffled,
        per_expiration=first.per_expiration,
        scope_differences=first.scope_differences,
    )
    assert reordered.transition_report_hash() == first.transition_report_hash()


def test_the_hash_moves_when_the_captures_do(tmp_path):
    a, b = _pair(tmp_path, later_overrides={"complete_open_interest": True})
    c, d = _pair(tmp_path / "other", later_overrides={"oi_gap": 23})
    assert (
        compare_captures(a, b).transition_report_hash()
        != compare_captures(c, d).transition_report_hash()
    )


def test_comparing_a_capture_with_itself_is_refused(tmp_path):
    earlier, _later = _pair(tmp_path, later_overrides={"complete_open_interest": True})
    with pytest.raises(CaptureComparisonError, match="same manifest hash"):
        compare_captures(earlier, earlier)


def test_the_wrong_way_round_is_refused(tmp_path):
    earlier, later = _pair(tmp_path, later_overrides={"complete_open_interest": True})
    with pytest.raises(CaptureComparisonError, match="earlier capture to be earlier"):
        compare_captures(later, earlier)


def test_different_underlyings_are_refused(tmp_path):
    earlier, later = _pair(
        tmp_path, later_overrides={"symbol": "NDXP", "complete_open_interest": True}
    )
    with pytest.raises(CaptureComparisonError, match="do not form a series"):
        compare_captures(earlier, later)


def test_a_different_scope_is_reported_rather_than_refused(tmp_path):
    """A wider ``max_dte`` explains part of the universe change. Say so."""
    earlier, later = _pair(
        tmp_path, later_overrides={"max_dte": 90, "complete_open_interest": True}
    )
    report = compare_captures(earlier, later)
    assert any("max_dte differs" in note for note in report.scope_differences)
    assert report.accounting_is_exhaustive


def test_an_uncertifiable_capture_is_refused(tmp_path):
    earlier, later = _pair(tmp_path, later_overrides={"complete_open_interest": True})
    (later / "run-intent.json").unlink()
    with pytest.raises(CaptureComparisonError, match="does not certify"):
        compare_captures(earlier, later)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def test_the_command_writes_a_report_and_summarises_it(tmp_path, capsys):
    from src.tools.compare_thetadata_captures import EXIT_OK
    from src.tools.compare_thetadata_captures import main as compare_main

    earlier, later = _pair(tmp_path, later_overrides={"complete_open_interest": True})
    target = tmp_path / "transition.json"
    assert compare_main([str(earlier), str(later), "--json", str(target)]) == EXIT_OK

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["accounting_is_exhaustive"] is True
    assert len(payload["transition_report_hash"]) == 64
    assert payload["analytical_evidence_status"][1] == "OI_IMPUTATION_POLICY_UNRESOLVED"

    printed = capsys.readouterr().out
    for expected in (
        "earlier",
        "later",
        "accounting",
        "transitions:",
        "rollups:",
        "per expiration",
        "findings:",
        "evidence status:",
        "imputation",
    ):
        assert expected in printed
    # The summary must not quietly suggest an answer to the open question.
    assert "OI_IMPUTATION_POLICY_UNRESOLVED" in printed


def test_the_command_prints_the_report_when_no_path_is_given(tmp_path, capsys):
    from src.tools.compare_thetadata_captures import main as compare_main

    earlier, later = _pair(tmp_path, later_overrides={"complete_open_interest": True})
    assert compare_main([str(earlier), str(later)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == LONGITUDINAL_OI_SCHEMA_VERSION


def test_the_command_verifies_archives_when_given_them(tmp_path, capsys):
    import zipfile

    from src.tools.compare_thetadata_captures import main as compare_main

    earlier, later = _pair(tmp_path, later_overrides={"complete_open_interest": True})
    archives = []
    for index, root in enumerate((earlier, later)):
        target = tmp_path / f"archive-{index}.zip"
        with zipfile.ZipFile(target, "w") as bundle:
            bundle.write(root / "manifest.json", "manifest.json")
            bundle.write(root / "run-intent.json", "run-intent.json")
            for payload in sorted((root / "raw").iterdir()):
                bundle.write(payload, f"raw/{payload.name}")
        archives.append(target)

    assert (
        compare_main(
            [
                str(earlier),
                str(later),
                "--earlier-archive-path",
                str(archives[0]),
                "--later-archive-path",
                str(archives[1]),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["accounting_is_exhaustive"] is True


def test_the_command_reports_an_incomparable_pair_rather_than_crashing(
    tmp_path, capsys
):
    from src.tools.compare_thetadata_captures import EXIT_UNCOMPARABLE
    from src.tools.compare_thetadata_captures import main as compare_main

    earlier, later = _pair(tmp_path, later_overrides={"complete_open_interest": True})
    # The wrong way round.
    assert compare_main([str(later), str(earlier)]) == EXIT_UNCOMPARABLE
    assert "cannot be compared" in capsys.readouterr().err


def test_the_command_reports_an_uncertifiable_capture(tmp_path, capsys):
    from src.tools.compare_thetadata_captures import EXIT_UNCOMPARABLE
    from src.tools.compare_thetadata_captures import main as compare_main

    earlier, later = _pair(tmp_path, later_overrides={"complete_open_interest": True})
    (later / "manifest.json").unlink()
    assert compare_main([str(earlier), str(later)]) == EXIT_UNCOMPARABLE
    assert "does not certify" in capsys.readouterr().err


def test_a_scope_difference_reaches_the_summary(tmp_path, capsys):
    from src.tools.compare_thetadata_captures import main as compare_main

    earlier, later = _pair(
        tmp_path, later_overrides={"max_dte": 90, "complete_open_interest": True}
    )
    assert (
        compare_main([str(earlier), str(later), "--json", str(tmp_path / "r.json")])
        == 0
    )
    assert "scope note" in capsys.readouterr().out


def test_the_universe_reads_open_interest_exactly_as_certification_does(tmp_path):
    """One parser. Two would eventually disagree, plausibly and invisibly."""
    earlier, _later = _pair(tmp_path, later_overrides={"complete_open_interest": True})
    universe = capture_universe(load_capture(earlier))
    assert universe.expected
    assert set(universe.answered) <= set(universe.expected)
    assert universe.unexpected == frozenset()
    for key in universe.expected:
        state = universe.state_of(key)
        assert state in {"OI_POSITIVE", "OI_ZERO", "OI_MISSING"}
        if state == "OI_MISSING":
            assert key not in universe.answered
