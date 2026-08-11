"""What the first live ThetaData capture established, held in place.

The raw payloads are not in this repository -- they are paid vendor data, and
5 MB of it under a git remote is a licensing question nobody has answered. What
is committed is ``tests/fixtures/live_capture/first_capture.json``: the archive
digest, the manifest hash, the per-record payload hashes, the identity-set
hashes and every statistic the reconstruction produced, all of it emitted by
``python -m src.tools.certify_thetadata_capture`` rather than typed in.

So these tests are not a re-run of the reconstruction. They are the assertion
that the conclusions drawn from it are the conclusions still encoded in the
code, and that a later edit which quietly changes one of them fails here.

**No synthetic row appears anywhere in this file.** A fabricated chain can be
made to prove whatever its author already believed, which is the opposite of
evidence. The machinery -- hash verification, refusal paths, and whether the
inference works at all on conventions it was not given -- is exercised in
``test_capture_certification_machinery.py`` against a generated capture, and
nothing there concludes anything about ThetaData.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from src.adapters.errors import ThetaDataProvenanceError
from src.adapters.thetadata.capture_certification import (
    ContractKey,
    OpenInterestCoverageState,
)
from src.adapters.thetadata.live_behavior import (
    BehaviorDimension,
    CaptureIdentity,
    EvidenceStatus,
    LiveBehaviorObservation,
    ObservationBasis,
    VendorBehaviorLedger,
    VendorRateSemantics,
)

FIXTURE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "fixtures"
    / "live_capture"
    / "first_capture.json"
)

FIRST_CAPTURE_ARCHIVE = (
    "5fc258007a3390b11960d7f3fa46a329f1277a899faf5a9a0a4f56598882d638"
)
FIRST_CAPTURE_MANIFEST = (
    "2f45534bbb569dfeb3e251b4fe3e27a8bdebbb716d5c0ac5b22f821d43ecbd20"
)


@pytest.fixture(scope="module")
def capture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _scores(capture: dict, key: str) -> dict[str, float]:
    return {row["hypothesis"]: row["delta_rmse"] for row in capture[key]}


# ---------------------------------------------------------------------------
# The rate, which is what this release is about
# ---------------------------------------------------------------------------


def test_the_first_capture_priced_at_four_hundred_and_twenty_percent(capture):
    """``rate_value=4.2`` reproduces under r=4.2, not under r=0.042.

    The request was built to mean 4.2% on the authority of the pinned OpenAPI
    description. The returned Greeks say the implementation used the number
    unchanged.
    """
    scores = _scores(capture, "rate_semantics")
    decimal = next(v for k, v in scores.items() if "r=4.2" in k)
    percent = next(v for k, v in scores.items() if "r=0.042" in k)

    assert decimal < percent
    # Not a close call, and the test says so numerically rather than trusting
    # the ordering: a change that halved the gap would still pass `<`.
    assert percent / decimal > 100.0
    assert decimal < 1e-3
    assert percent > 0.1


def test_the_documented_and_observed_rate_units_disagree_and_both_survive(capture):
    """Neither reading silently overwrites the other."""
    observation = next(
        o
        for o in capture["vendor_behavior"]["observations"]
        if o["dimension"] == "RATE_UNITS"
    )
    assert observation["documented_value"] == "PERCENT_ANNUAL_RATE"
    assert observation["observed_value"] == "DECIMAL_ANNUAL_RATE"
    assert observation["status"] == "DOCUMENTATION_LIVE_CONFLICT"
    # The measured reading governs what goes on the wire ...
    assert observation["governing_value"] == "DECIMAL_ANNUAL_RATE"
    # ... and the documentation is still on record as contradicted, which is a
    # fact about the vendor that outlives any one config file.
    assert observation["documentation_matched"] is False
    assert observation["documentation_reference"]
    assert "RATE_UNITS" in capture["vendor_behavior"]["conflicts"]


def test_a_conflict_cannot_be_recorded_with_one_side_missing():
    with pytest.raises(ThetaDataProvenanceError, match="both readings"):
        LiveBehaviorObservation(
            dimension=BehaviorDimension.RATE_UNITS,
            status=EvidenceStatus.DOCUMENTATION_LIVE_CONFLICT,
            basis=ObservationBasis.LIVE_NUMERICAL_RECONSTRUCTION,
            observed_value="DECIMAL_ANNUAL_RATE",
            capture=_identity(),
            rows_used=10,
        )


def test_agreement_cannot_be_filed_as_a_conflict():
    with pytest.raises(
        ThetaDataProvenanceError, match="Agreement recorded as conflict"
    ):
        LiveBehaviorObservation(
            dimension=BehaviorDimension.RATE_UNITS,
            status=EvidenceStatus.DOCUMENTATION_LIVE_CONFLICT,
            basis=ObservationBasis.LIVE_NUMERICAL_RECONSTRUCTION,
            documented_value="DECIMAL_ANNUAL_RATE",
            observed_value="DECIMAL_ANNUAL_RATE",
            documentation_reference="somewhere",
            capture=_identity(),
            rows_used=10,
        )


def test_a_document_may_not_witness_an_implementation():
    with pytest.raises(ThetaDataProvenanceError, match="cannot witness"):
        LiveBehaviorObservation(
            dimension=BehaviorDimension.RATE_UNITS,
            status=EvidenceStatus.LIVE_ONLY,
            basis=ObservationBasis.PINNED_DOCUMENTATION,
            observed_value="DECIMAL_ANNUAL_RATE",
        )


def test_live_evidence_must_name_the_capture_it_came_from():
    with pytest.raises(ThetaDataProvenanceError, match="names no capture"):
        LiveBehaviorObservation(
            dimension=BehaviorDimension.DAY_COUNT,
            status=EvidenceStatus.LIVE_ONLY,
            basis=ObservationBasis.LIVE_NUMERICAL_RECONSTRUCTION,
            observed_value="ACT_365",
            rows_used=100,
        )


# ---------------------------------------------------------------------------
# The corrected request
# ---------------------------------------------------------------------------


def test_the_corrected_request_sends_zero_point_zero_four_two():
    """4.2% economic, under the measured decimal semantics, is 0.042 on the wire."""
    semantics = VendorRateSemantics(
        economic_rate_percent=4.2,
        vendor_observed_rate_unit="DECIMAL_ANNUAL_RATE",
        documented_rate_unit="PERCENT_ANNUAL_RATE",
    )
    assert semantics.economic_rate_percent == 4.2
    assert semantics.economic_rate_decimal == pytest.approx(0.042)
    assert semantics.local_model_rate == pytest.approx(0.042)
    assert semantics.vendor_request_rate_value == pytest.approx(0.042)
    assert semantics.conflict is True


def test_under_the_documented_semantics_the_wire_value_would_have_been_four_point_two():
    """The same economic rate, the other unit, a hundred times the number.

    Kept as a test rather than a comment because it is the arithmetic the first
    capture got wrong, and it should fail loudly if the mapping is ever
    inverted.
    """
    semantics = VendorRateSemantics(
        economic_rate_percent=4.2,
        vendor_observed_rate_unit="PERCENT_ANNUAL_RATE",
        documented_rate_unit="PERCENT_ANNUAL_RATE",
    )
    assert semantics.vendor_request_rate_value == pytest.approx(4.2)
    assert semantics.conflict is False


def test_an_unknown_rate_unit_refuses_to_produce_a_wire_value():
    semantics = VendorRateSemantics(
        economic_rate_percent=4.2,
        vendor_observed_rate_unit="UNKNOWN",
        documented_rate_unit="UNKNOWN",
    )
    with pytest.raises(ThetaDataProvenanceError, match="no value that can"):
        _ = semantics.vendor_request_rate_value


def test_the_capture_profile_sends_the_corrected_value():
    """The committed profile, not a constructed object."""
    from src.config.schema import load_config

    config = load_config("config/thetadata_capture.yaml")
    assert config.thetadata.rate_value == pytest.approx(0.042)
    assert config.thetadata.rate_units.value == "DECIMAL_ANNUAL_RATE"
    # The local model is unchanged: the economic rate never moved. Only the
    # number that expresses it to the vendor did.
    assert config.engine.model_spec.risk_free_rate == pytest.approx(0.042)


def test_the_dry_run_predicts_the_second_capture_is_economically_valid(tmp_path):
    """The block an operator reads before authorising the next paid session.

    Every quantity the decision needs, with the two verdicts kept apart: the
    documentation conflict is still true, and this request will nonetheless buy
    the rate it means to.
    """
    from src.tools.capture_thetadata_once import plan_capture
    from tests.certification_fixtures import DOCUMENTED_SESSION

    report = plan_capture(
        "config/thetadata_capture.yaml",
        output=str(tmp_path / "capture-next"),
        as_of=DOCUMENTED_SESSION,
    )
    rates = report["rate_semantics"]

    assert rates["economic_rate_percent"] == pytest.approx(4.2)
    assert rates["economic_rate_decimal"] == pytest.approx(0.042)
    assert rates["local_model_rate"] == pytest.approx(0.042)
    assert rates["wire_value"] == pytest.approx(0.042)
    assert rates["vendor_request_rate_value"] == pytest.approx(0.042)
    assert rates["documented_rate_unit"] == "PERCENT_ANNUAL_RATE"
    assert rates["vendor_observed_rate_unit"] == "DECIMAL_ANNUAL_RATE"
    # wire 0.042 + observed DECIMAL semantics => the vendor prices 0.042 ...
    assert rates["predicted_vendor_effective_rate"] == pytest.approx(0.042)
    assert rates["predicted_effective_rate_matches_intended_rate"] is True
    # ... while the documentation still says something else entirely.
    assert rates["documentation_live_conflict"] is True

    # And the value reaches the wire, not only the report.
    greeks = next(
        request
        for request in report["planned_requests"]["requests"]
        if "greeks" in request["endpoint"]
    )
    assert dict(greeks["canonical_query_parameters"])["rate_value"] == "0.042"


# ---------------------------------------------------------------------------
# Day count, expiration clock, IV basis, underlying
# ---------------------------------------------------------------------------


def test_act_365_wins_the_captured_day_count_comparison(capture):
    scores = _scores(capture, "day_count_comparison")
    assert min(scores, key=lambda k: scores[k]) == "ACT/365"
    assert scores["ACT/365"] < scores["ACT/365.25"]
    assert scores["ACT/365"] * 50 < scores["ACT/360"]
    assert scores["ACT/365"] * 100 < scores["ACT/252"]


def test_sixteen_hundred_eastern_wins_the_spxw_expiration_comparison(capture):
    scores = _scores(capture, "expiration_time_comparison")
    assert min(scores, key=lambda k: scores[k]) == "16:00 America/New_York"
    for other in (
        "15:30 America/New_York",
        "16:15 America/New_York",
        "16:30 America/New_York",
    ):
        assert scores["16:00 America/New_York"] < scores[other]


def test_the_expiration_rule_is_scoped_to_where_it_actually_holds(capture):
    """The finding is front-week only, and the record says so.

    Beyond the capture week the vendor prices on whole calendar days. An
    unscoped ``EXPIRATION_TIMESTAMP = 16:00 ET`` would be a false
    generalisation with a capture hash attached to it.
    """
    observation = next(
        o
        for o in capture["vendor_behavior"]["observations"]
        if o["dimension"] == "EXPIRATION_TIMESTAMP"
    )
    assert "2026-08-14" in observation["scope"]
    assert "NOT established" in observation["scope"]

    readings = capture["vendor_clock_by_expiration"]
    intraday = [r for r in readings if r["is_intraday_regime"]]
    whole = [r for r in readings if r["is_whole_day_regime"]]
    assert {r["expiration"] for r in intraday} == {
        "2026-08-10",
        "2026-08-11",
        "2026-08-12",
        "2026-08-13",
        "2026-08-14",
    }
    assert len(whole) > len(intraday)
    # Every expiration is one or the other; none is unexplained.
    assert len(intraday) + len(whole) == len(readings)


def test_the_expiration_scope_is_structured_not_only_prose(capture):
    """A consumer can establish the scope without reading a sentence.

    v2.1.22 put the whole distinction in a ``scope`` string. Anything
    downstream that wanted to know whether 16:00 applied to a given expiration
    had to parse English or guess, and guessing would have given it a global
    rule.
    """
    evidence = capture["expiration_clock_evidence"]
    assert evidence["intraday_expirations"] == [
        "2026-08-10",
        "2026-08-11",
        "2026-08-12",
        "2026-08-13",
        "2026-08-14",
    ]
    # The 26 that contradict a global 16:00, named rather than counted.
    assert evidence["contradicting_count"] == 26
    assert len(evidence["whole_day_expirations"]) == 26
    assert "2026-09-30" in evidence["whole_day_expirations"]
    assert evidence["scope_is_global"] is False
    # And the transition itself was never observed: the sample jumps from four
    # days out to seven with nothing in between.
    assert evidence["boundary_last_intraday"] == "2026-08-14"
    assert evidence["boundary_first_whole_day"] == "2026-08-17"
    assert evidence["boundary_gap_days"] == 3
    assert evidence["boundary_status"] == "OPEN"
    assert not evidence["unexplained_expirations"]


def test_the_first_capture_rate_is_bound_to_its_own_request(capture):
    """The wire value came from the capture, proved against the manifest."""
    request = capture["capture_request"]
    assert request["greeks_rate_value"] == pytest.approx(4.2)
    assert request["binding"] == "RECOMPUTED_AGAINST_MANIFEST_PLANNED_REQUEST_HASH"
    assert len(request["verified_endpoints"]) == 5


def test_the_documentation_conflict_and_the_economic_error_are_separate(capture):
    """Both true here, and for different reasons."""
    economics = capture["rate_economics"]
    assert economics["wire_rate_value"] == pytest.approx(4.2)
    assert economics["vendor_effective_rate"] == pytest.approx(4.2)
    assert economics["intended_economic_rate"] == pytest.approx(0.042)
    assert economics["magnitude_ratio"] == pytest.approx(100.0)
    # The vendor does not read its own parameter as it documents it ...
    assert economics["rate_units_documentation_live_conflict"] is True
    # ... and separately, this capture did not buy the rate it meant to.
    assert economics["capture_effective_rate_matches_intended_rate"] is False
    # The first capture predates the bound intent, and says so explicitly. The
    # documented unit it is read under comes from the capture's *own* recorded
    # extraction, not from a constant in today's certification code.
    assert economics["intended_rate_source"] == "LEGACY_CAPTURE_DOCUMENTATION_DERIVED"
    assert capture["rate_intent_binding"]["bound"] is False
    assert capture["rate_intent_binding"]["capture_records_binding_schema"] is False


def test_the_resolved_labels_are_the_winning_hypotheses(capture):
    assert capture["resolved_day_count"] == "ACT_365"
    assert capture["resolved_expiration_clock"] == "16:00 America/New_York"
    best_day = min(capture["day_count_comparison"], key=lambda r: r["delta_rmse"])
    best_clock = min(
        capture["expiration_time_comparison"], key=lambda r: r["delta_rmse"]
    )
    assert best_day["hypothesis"] == "ACT/365"
    assert best_clock["hypothesis"] == "16:00 America/New_York"


def test_nbbo_midpoint_reproduces_the_captured_implied_volatility(capture):
    rows = {r["basis"]: r for r in capture["iv_basis_comparison"]}
    mid = rows["NBBO_MID"]["median_abs_iv_error"]
    assert mid < rows["BID"]["median_abs_iv_error"]
    assert mid < rows["ASK"]["median_abs_iv_error"]
    # The residual is half the reporting tick of implied_vol -- the floor. There
    # is nothing left for a better hypothesis to explain.
    assert mid <= 1e-4 / 2 * 1.05
    assert rows["BID"]["median_abs_iv_error"] > 50 * mid


def test_the_embedded_greeks_underlying_is_authoritative_not_the_index_snapshot(
    capture,
):
    under = capture["underlying_synchronization"]
    assert under["vendor_greeks_underlying_price"] == pytest.approx(7759.27)
    assert under["vendor_greeks_underlying_timestamp"] == "2026-08-10T10:01:34.000"
    assert under["index_snapshot_price"] == pytest.approx(7759.54)
    assert under["index_snapshot_timestamp"] == "2026-08-10T10:01:33.000"
    assert under["synchronized"] is False
    assert (
        under["authoritative_for_vendor_model"]
        == "GREEKS_RESPONSE_EMBEDDED_VENDOR_UNDERLYING"
    )


def test_quote_and_greeks_are_not_atomic(capture):
    sync = capture["endpoint_synchrony"]
    assert sync["is_atomic"] is False
    assert sync["identical_timestamp_ratio"] < 0.80
    assert sync["max_gap_seconds"] > 30.0


# ---------------------------------------------------------------------------
# Universe and open interest
# ---------------------------------------------------------------------------


def test_the_contract_list_matched_the_snapshot_universe_by_set_hash(capture):
    universe = capture["universe"]
    assert universe["contract_list_count"] == 14_556
    assert universe["quote_count"] == 14_556
    assert universe["greeks_count"] == 14_556
    # Counts agreeing is not the claim. The identity sets agreeing is.
    assert universe["contract_list_set_hash"] == universe["quote_set_hash"]
    assert universe["contract_list_set_hash"] == universe["greeks_set_hash"]
    assert universe["quote_matches_list"] is True
    assert universe["greeks_matches_list"] is True
    assert not universe["only_in_list"]
    assert not universe["only_in_snapshots"]
    assert universe["state"] == "DEDICATED_CONTRACT_LIST_MATCHED_SNAPSHOT_UNIVERSE"


def test_the_universe_certification_is_scoped_to_this_request_form(capture):
    observation = next(
        o
        for o in capture["vendor_behavior"]["observations"]
        if o["dimension"] == "CONTRACT_LIST_UNIVERSE"
    )
    for token in ("SPXW", "2026-08-10", "max_dte=60"):
        assert token in observation["scope"]


def test_the_four_hundred_and_twenty_six_absent_identities_are_not_filled_with_zero(
    capture,
):
    oi = capture["open_interest_coverage"]
    assert oi["universe_count"] == 14_556
    assert oi["oi_missing"] == 426
    assert oi["oi_present"] + oi["oi_explicit_zero"] == 14_130
    assert oi["coverage_ratio"] == pytest.approx(0.97073, abs=1e-5)
    # The missing identities are accounted for by name, not just counted.
    assert oi["missing_identities_hash"]
    assert sum(e["missing"] for e in oi["missing_by_expiration"]) == 426
    # And they block a trusted aggregate rather than being defaulted away.
    assert oi["permits_trusted_aggregate"] is False


def test_explicit_zero_open_interest_stays_distinct_from_missing(capture):
    oi = capture["open_interest_coverage"]
    assert oi["oi_explicit_zero"] == 3_692
    assert oi["oi_missing"] == 426
    assert oi["oi_explicit_zero"] != oi["oi_missing"]
    # Three states, and the vendor answered for two of them.
    assert OpenInterestCoverageState.OI_EXPLICIT_ZERO.is_observed is True
    assert OpenInterestCoverageState.OI_PRESENT.is_observed is True
    assert OpenInterestCoverageState.OI_MISSING.is_observed is False


def test_an_entire_expiration_can_be_missing_open_interest(capture):
    """2026-09-16 was listed, quoted and greeked, and has no open interest at all.

    Worth its own test because a per-contract coverage ratio of 97% reads like
    scattered gaps, and one of these gaps is a whole expiration.
    """
    oi = capture["open_interest_coverage"]
    assert "2026-09-16" in oi["fully_missing_expirations"]
    entry = next(
        e for e in oi["missing_by_expiration"] if e["expiration"] == "2026-09-16"
    )
    assert entry["listed"] == 150
    assert entry["missing"] == 150


# ---------------------------------------------------------------------------
# Identity and readiness
# ---------------------------------------------------------------------------


def test_the_fixture_pins_the_capture_it_was_derived_from(capture):
    assert capture["capture"]["manifest_hash"] == FIRST_CAPTURE_MANIFEST
    assert capture["capture"]["archive_sha256"] == FIRST_CAPTURE_ARCHIVE
    assert (
        capture["capture"]["session_id"] == "capture-20260810T140129Z-2ef4f56270c1447b"
    )
    assert capture["raw_record_verification"]["records_verified"] == 5
    assert len(capture["raw_record_verification"]["record_payload_hashes"]) == 5


def test_the_first_capture_is_evidence_and_not_a_gex_input(capture):
    assert capture["analytical_readiness"] == "ADAPTER_CERTIFICATION_EVIDENCE"
    assert capture["trusted_for_gex"] is False
    blockers = " ".join(capture["gex_blockers"])
    # Two independent reasons, and fixing the rate does not fix the second.
    assert "factor of 100" in blockers
    assert "open-interest" in blockers
    assert len(capture["gex_blockers"]) == 2


def test_every_dimension_the_capture_touched_is_recorded(capture):
    recorded = {o["dimension"] for o in capture["vendor_behavior"]["observations"]}
    assert recorded == {d.value for d in BehaviorDimension}
    assert not capture["behavior_dimensions_unresolved"]
    # And the behaviour vocabulary is *not* the pricing vocabulary, so an empty
    # unresolved list here says nothing about analytical readiness.
    assert capture["pricing_dimensions_still_unresolved"]


def test_a_ledger_refuses_two_answers_for_one_dimension():
    def observation(value: str) -> LiveBehaviorObservation:
        return LiveBehaviorObservation(
            dimension=BehaviorDimension.DAY_COUNT,
            status=EvidenceStatus.LIVE_ONLY,
            basis=ObservationBasis.LIVE_NUMERICAL_RECONSTRUCTION,
            observed_value=value,
            capture=_identity(),
            rows_used=100,
        )

    with pytest.raises(ThetaDataProvenanceError, match="appears twice"):
        VendorBehaviorLedger(
            observations=(observation("ACT_365"), observation("ACT_360"))
        )


def test_a_capture_identity_refuses_a_truncated_digest():
    with pytest.raises(ThetaDataProvenanceError, match="full lowercase"):
        CaptureIdentity(session_id="s", manifest_hash="abc123", archive_sha256="a" * 64)


def _identity() -> CaptureIdentity:
    return CaptureIdentity(
        session_id="capture-20260810T140129Z-2ef4f56270c1447b",
        manifest_hash=FIRST_CAPTURE_MANIFEST,
        archive_sha256=FIRST_CAPTURE_ARCHIVE,
    )


# ---------------------------------------------------------------------------
# One pure unit check that belongs beside the findings
# ---------------------------------------------------------------------------
#
# The capture-directory plumbing -- hash mismatches, absent payloads, mutated
# manifests, missing endpoints -- moved to
# `test_capture_certification_machinery.py` in v2.1.23, where the synthetic
# builder produces a capture realistic enough to exercise the verification
# rather than a stub that skips past it.


def test_a_contract_key_keeps_the_vendors_own_strike_text():
    """7600.000 and 7600.0 are one contract on the wire and must stay one here."""
    key = ContractKey.from_row(
        {
            "symbol": '"SPXW"',
            "expiration": '"2026-09-16"',
            "strike": "7600.000",
            "right": '"PUT"',
        }
    )
    assert key.canonical == "SPXW|2026-09-16|7600.000|PUT"
    assert key.option_right.value.upper().startswith("P")
