"""The Sep 2 live capture proves v2.1.28 eligibility on vendor bytes.

The paid raw payloads remain outside the repository. The committed fixture is
the deterministic report emitted by certify_thetadata_capture.
"""

from __future__ import annotations

import json
import pathlib

import pytest

FIXTURE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "fixtures"
    / "live_capture"
    / "third_capture.json"
)


@pytest.fixture(scope="module")
def third() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_third_capture_is_bound_to_the_preserved_archive(third):
    capture = third["capture"]
    assert third["schema_version"] == "capture-certification/2.1.27"
    assert (
        third["report_hash"]
        == "07dc84dd153e61638b0aece24956b8c5765c31c7093c4e9d115ca8474f626d7b"
    )
    assert (
        capture["archive_sha256"]
        == "3a206097ada2fe269c206f3e03bc49c8eceaa5849b85a6ad9f5bdcd318b1612d"
    )
    assert (
        capture["manifest_hash"]
        == "78ea018613022e546001744be1b71b8d4ec853fc1bc627cc0d1f4222c5f2898d"
    )
    assert capture["archive_matches_capture"] is True
    assert capture["archive_payloads_verified"] == 5
    assert capture["session_id"] == "capture-20260902T195700Z-0bccc563691a2ad9"


def test_all_three_snapshot_universes_match_exactly(third):
    universe = third["universe"]
    assert universe["contract_list_count"] == 13_536
    assert universe["quote_count"] == 13_536
    assert universe["greeks_count"] == 13_536
    assert universe["quote_matches_list"] is True
    assert universe["greeks_matches_list"] is True
    assert universe["duplicate_identity_count"] == 0
    assert universe["state"] == "DEDICATED_CONTRACT_LIST_MATCHED_SNAPSHOT_UNIVERSE"


def test_open_interest_answers_and_silence_stay_distinct(third):
    coverage = third["open_interest_coverage"]
    assert coverage["oi_present"] == 9_435
    assert coverage["oi_explicit_zero"] == 3_373
    assert coverage["oi_missing"] == 728
    assert coverage["oi_covered"] == 12_808
    assert coverage["oi_covered"] + coverage["oi_missing"] == 13_536
    assert coverage["unexpected_oi_count"] == 0
    assert coverage["coverage_state"] == "OI_MISSING"
    assert coverage["permits_trusted_aggregate"] is False


def test_the_live_capture_reproduces_the_v2_1_28_partition(third):
    report = third["analytical_universe"]
    assert report["schema_version"] == "analytical-universe-report/2.1.28"
    assert report["eligibility_schema_version"] == "analytical-universe/2.1.28"
    assert report["algorithm_version"] == "analytical-universe/1"
    assert (
        report["analytical_universe_report_hash"]
        == "27c3f69510bfe8b8f6aae6191ba8e004be37eb759096055b8f47cbfb36242b06"
    )
    assert report["sessions_agree"] is True
    assert report["accounting_is_exhaustive"] is True
    assert report["listed_count"] == 13_536
    assert report["eligible_count"] == 12_312
    assert report["excluded_count"] == 1_224
    assert report["class_counts"] == {
        "ELIGIBLE_CURRENT": 11_816,
        "ELIGIBLE_EXPIRING_THIS_SESSION": 496,
        "EXCLUDED_EXPIRED_BEFORE_SESSION": 496,
        "EXCLUDED_OPEN_INTEREST_NOT_REPORTED": 728,
    }


def test_same_session_expiry_is_kept_and_prior_session_expiry_is_not(third):
    report = third["analytical_universe"]
    by_expiration = {row["expiration"]: row for row in report["per_expiration"]}
    assert by_expiration["2026-09-01"]["class_counts"] == {
        "EXCLUDED_EXPIRED_BEFORE_SESSION": 496
    }
    assert by_expiration["2026-09-02"]["class_counts"] == {
        "ELIGIBLE_EXPIRING_THIS_SESSION": 496,
        "EXCLUDED_OPEN_INTEREST_NOT_REPORTED": 16,
    }


def test_live_evidence_does_not_relax_the_trusted_gex_gate(third):
    assert third["trusted_for_gex"] is False
    assert third["analytical_readiness"] == "ADAPTER_CERTIFICATION_EVIDENCE"
    assert third["analytical_universe"]["permits_trusted_analytical_universe"] is False
    blockers = third["gex_blockers"]
    assert len(blockers) == 2
    assert any(
        "rate this vendor actually priced with" in blocker for blocker in blockers
    )
    assert any("728 contract identities" in blocker for blocker in blockers)
