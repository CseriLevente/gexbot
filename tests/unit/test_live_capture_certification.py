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

**No synthetic row appears in any rate-semantics assertion.** The plumbing tests
at the bottom build a small fake capture to exercise hash verification and the
refusal paths, and they are careful not to conclude anything about the vendor
from it -- a fabricated chain can be made to prove whatever its author already
believed, which is the opposite of evidence.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from src.adapters.errors import ThetaDataProvenanceError
from src.adapters.thetadata.capture_certification import (
    CaptureCertificationError,
    ContractKey,
    OpenInterestCoverageState,
    certify_capture,
    load_capture,
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
    intraday = [r for r in readings if r["matches_intraday_1600"]]
    whole = [r for r in readings if r["matches_whole_days"]]
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
    assert "420%" in blockers
    assert "open-interest" in blockers


def test_every_dimension_the_capture_touched_is_recorded(capture):
    recorded = {o["dimension"] for o in capture["vendor_behavior"]["observations"]}
    assert recorded == {d.value for d in BehaviorDimension}
    assert not capture["dimensions_unresolved"]


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
# Plumbing. Fabricated bytes, and no conclusion about the vendor drawn from them.
# ---------------------------------------------------------------------------


def _write_capture(root: pathlib.Path, bodies: dict[str, str]) -> None:
    (root / "raw").mkdir(parents=True)
    records = []
    for endpoint, body in bodies.items():
        name = endpoint.strip("/").replace("/", "-") + ".raw"
        raw = body.encode("utf-8")
        (root / "raw" / name).write_bytes(raw)
        records.append(
            {
                "endpoint": endpoint,
                "payload_location": name,
                "payload_hash": hashlib.sha256(raw).hexdigest(),
                "effective_valuation_timestamp": "2026-08-10T14:01:29.616899+00:00",
            }
        )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "session_id": "capture-test",
                "manifest_hash": "0" * 64,
                "parser_version": "thetadata-v3-parser/2.1.17",
                "records": records,
            }
        ),
        encoding="utf-8",
    )


def test_a_payload_that_no_longer_matches_its_hash_is_refused(tmp_path):
    root = tmp_path / "capture"
    _write_capture(root, {"/v3/index/snapshot/price": "timestamp,symbol,price\n"})
    payload = next((root / "raw").iterdir())
    payload.write_bytes(payload.read_bytes() + b"tampered\n")

    with pytest.raises(CaptureCertificationError, match="not the bytes that were"):
        load_capture(root)


def test_a_capture_without_a_manifest_is_refused(tmp_path):
    root = tmp_path / "capture"
    (root / "raw").mkdir(parents=True)
    with pytest.raises(CaptureCertificationError, match="without its manifest"):
        load_capture(root)


def test_a_manifest_naming_an_absent_payload_is_refused(tmp_path):
    root = tmp_path / "capture"
    _write_capture(root, {"/v3/index/snapshot/price": "timestamp\n"})
    next((root / "raw").iterdir()).unlink()
    with pytest.raises(CaptureCertificationError, match=r"the file is\s+absent"):
        load_capture(root)


def test_certification_refuses_a_capture_missing_an_endpoint(tmp_path):
    root = tmp_path / "capture"
    _write_capture(root, {"/v3/index/snapshot/price": "timestamp,symbol,price\n"})
    with pytest.raises(CaptureCertificationError, match="no /v3/option"):
        certify_capture(root)


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
