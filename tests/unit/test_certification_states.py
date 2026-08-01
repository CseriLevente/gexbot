"""Certification must not be able to bless what it has not checked.

The v2.1.2 defect. ``assess_readiness`` read ``pipeline.pricing_mode`` and, for
any mode that did not "mix vendor and local", returned compatible without
looking further. The default configuration claimed ``LOCAL_IV_LOCAL_GAMMA``
while fetching vendor-computed IV, so the check it needed most was the one it
skipped -- and the report said ready.

The v2.1.4 defects are worse, and both live in the states themselves:

* ``capture_manifest`` and ``validation_report`` were ``Any``, tested with ``is
  not None``. ``assess_readiness(capture_manifest=object(),
  validation_report=object())`` returned ``ADAPTER_CERTIFIED``.
* Capture readiness and calculation trust shared one ladder, so an unresolved
  vendor convention blocked the capture that would have resolved it.

Both are regression-tested below.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.adapters.certification import (
    AdapterValidationReport,
    CaptureVerification,
    CertificationState,
    OpenInterestProvenance,
    ProvenanceEvidence,
    ProvenanceGrade,
    SpotProvenance,
    ValidationCheck,
    assess_readiness,
    grade_claim,
    verify_capture,
)
from src.adapters.raw_store import InMemoryRawStore, RawCaptureManifest
from src.adapters.transport import FakeTransport
from src.config.compatibility import EvidenceSource, PricingDimension
from src.config.pipeline import ThetaDataResearchPipeline
from src.config.thetadata import parse_thetadata_config
from src.gex.sessions import eastern
from tests.pricing_evidence import attestations, resolved_settings

AS_OF = eastern(2026, 3, 17, 11, 0)

#: Raw capture is mandatory for capture readiness (§6), so every pipeline here
#: has it switched on and a store to write to.
CAPTURE_SETTINGS = {"raw_capture_enabled": True, "raw_capture_path": "artifacts/raw"}


def pipeline(**overrides):
    settings = {**CAPTURE_SETTINGS}
    settings.update(overrides)
    return ThetaDataResearchPipeline.from_config(
        parse_thetadata_config(settings), transport=FakeTransport()
    )


def resolved_pipeline(**overrides):
    """Everything a vendor-IV session needs, stated explicitly.

    Built through the production configuration path: the attestations are
    evidence objects the loader validates, and ``assess_pricing_compatibility``
    decides what follows from them. v2.1.3 reached the same place by replacing
    the finished report with ``PricingCompatibilityReport(compatible=True)``,
    which tested nothing.
    """
    return pipeline(**resolved_settings(**{**CAPTURE_SETTINGS, **overrides}))


RECORD_ID = "session-0001-bulk-snapshot"


def captured(store: InMemoryRawStore | None = None) -> CaptureVerification:
    """A real capture: bytes in a store, and a manifest the store agrees with."""
    from datetime import UTC, datetime

    store = store if store is not None else InMemoryRawStore()
    now = datetime(2026, 3, 17, 15, 0, tzinfo=UTC)
    record = store.put(
        record_id=RECORD_ID,
        endpoint="/v3/option/snapshot/greeks",
        query_params={"root": "SPXW"},
        payload="ms_of_day,implied_vol\n1,0.2\n",
        request_started_at=now,
        response_received_at=now,
        http_status=200,
    )
    manifest = RawCaptureManifest(
        session_id="session",
        record_ids=(record.record_id,),
        payload_hashes=(record.payload_hash,),
    )
    return verify_capture(manifest, store)


def evidence_from(capture: CaptureVerification, field: str) -> ProvenanceEvidence:
    """Evidence that actually points at this capture.

    Derived, not written down. A literal manifest hash here would be a claim
    about a session the fixture never made -- which is the defect these tests
    exist to catch, and which the first cut of them contained: ``observed_oi``
    carried ``manifest_hash="deadbeefdeadbeef"`` while the capture produced
    something else, and every assertion still passed.
    """
    return ProvenanceEvidence(
        raw_record_id=capture.confirmed_record_ids[0],
        field_path=field,
        manifest_hash=capture.manifest_hash,
    )


def observed_oi(capture: CaptureVerification) -> OpenInterestProvenance:
    return OpenInterestProvenance(
        as_of=date(2026, 3, 16),
        source="vendor_field",
        evidence=evidence_from(capture, "open_interest"),
    )


def planned_oi() -> OpenInterestProvenance:
    """A date somebody typed in. Accepted; never called observed."""
    return OpenInterestProvenance(as_of=date(2026, 3, 16), source="caller")


def observed_spot(capture: CaptureVerification) -> SpotProvenance:
    return SpotProvenance(
        source="vendor_index_snapshot",
        timestamp=AS_OF - timedelta(milliseconds=200),
        tolerance_seconds=1.0,
        evidence=evidence_from(capture, "index_price"),
    )


def planned_spot() -> SpotProvenance:
    return SpotProvenance(
        source="vendor_index_snapshot",
        timestamp=AS_OF - timedelta(milliseconds=200),
        tolerance_seconds=1.0,
    )


def readiness(**overrides):
    """Provenance follows the capture, because observation requires one.

    With no capture there is nothing to have observed, so the default claims
    nothing. With a capture, the evidence is derived from it rather than
    asserted alongside it.
    """
    capture = overrides.get("capture")
    # An unverified capture confirms no records, so there is still nothing that
    # could have been observed.
    usable = (
        capture
        if isinstance(capture, CaptureVerification) and capture.verified
        else None
    )
    payload = {
        "pipeline": resolved_pipeline(),
        "as_of": AS_OF,
        "open_interest": observed_oi(usable) if usable else planned_oi(),
        "spot": observed_spot(usable) if usable else planned_spot(),
        "raw_store": InMemoryRawStore(),
    }
    payload.update(overrides)
    return assess_readiness(**payload)


def validation_for(
    capture: CaptureVerification, *, dimensions: bool = True, passed: bool = True
) -> AdapterValidationReport:
    checks = [
        ValidationCheck(name="open_interest_as_of", passed=passed),
        ValidationCheck(name="spot_timestamp", passed=passed),
    ]
    if dimensions:
        checks.extend(
            ValidationCheck(
                name=f"vendor_{dimension.value.lower()}",
                passed=passed,
                dimension=dimension,
            )
            for dimension in PricingDimension
        )
    return AdapterValidationReport(
        manifest_hash=capture.manifest_hash,
        checks=tuple(checks),
        validator="tests",
        validated_at="2026-03-17",
    )


# =============================================================================
# §2/§3 -- unknowns that change gamma block the calculation, not the capture
# =============================================================================


def test_the_default_configuration_cannot_be_trusted_to_calculate():
    """The v2.1.2 regression. Still a refusal, now on the right axis."""
    result = readiness(pipeline=pipeline())
    assert not result.calculation_trusted
    assert result.state is CertificationState.READY_FOR_RAW_CAPTURE_ONLY


def test_unknown_pricing_does_not_block_a_raw_capture():
    """The v2.1.4 correction.

    v2.1.3 refused to capture until the vendor's conventions were known, and
    capturing is how several of them get answered. Bytes are worth having
    whatever we understand about the day count.
    """
    result = readiness(pipeline=pipeline())
    assert result.ready, result.blockers
    assert result.blockers == ()


def test_the_default_blocks_specifically_on_load_bearing_unknowns():
    result = readiness(pipeline=pipeline())
    assert any("load-bearing" in blocker for blocker in result.calculation_blockers)


def test_an_unknown_vendor_rate_blocks_the_calculation():
    result = readiness(pipeline=pipeline(rate_value=4.2, rate_units="UNKNOWN"))
    assert not result.calculation_trusted


def test_a_vendor_default_rate_blocks_the_calculation():
    """No rate_value sent means the vendor used *something* we cannot name."""
    result = readiness(pipeline=pipeline(rate_value=None))
    assert not result.calculation_trusted


def test_an_unknown_dividend_convention_blocks_the_calculation():
    result = readiness(
        pipeline=pipeline(
            annual_dividend=1.3, dividend_convention="UNKNOWN_VENDOR_CONVENTION"
        )
    )
    assert not result.calculation_trusted


def test_a_resolved_configuration_can_become_capture_ready():
    """The fix must leave a path to ready, or it is just a refusal."""
    result = readiness()
    assert result.ready, result.blockers
    assert result.calculation_trusted, result.calculation_blockers
    assert result.state is CertificationState.READY_FOR_RAW_CAPTURE_ONLY


def test_an_attestation_cannot_overturn_a_measured_mismatch():
    """Resolution is for open questions, not for disagreements."""
    from src.config.compatibility import (
        CompatibilityEvidence,
        CompatibilityStatus,
        PricingAssumptionAttestation,
        PricingCompatibilityReport,
        PricingDimensionResult,
        apply_attestations,
    )

    measured = PricingCompatibilityReport(
        dimensions=(
            PricingDimensionResult(
                dimension=PricingDimension.RISK_FREE_RATE,
                status=CompatibilityStatus.MISMATCHED,
                code="RATE_VALUE_DIFFERS",
                vendor_value=0.05,
                local_value=0.042,
            ),
        )
    )
    after = apply_attestations(
        measured,
        (
            PricingAssumptionAttestation(
                dimension=PricingDimension.RISK_FREE_RATE,
                evidence=CompatibilityEvidence(
                    source=EvidenceSource.VENDOR_DOCUMENTATION,
                    reference="tests/fixtures/vendor_conventions.md",
                    observed_at="2026-07-31",
                ),
            ),
        ),
    )
    assert not after.compatible
    assert any("CANNOT_OVERRIDE_MISMATCH" in f for f in after.hard_failures)


def test_an_attestation_without_a_reference_cannot_be_constructed():
    from src.config.compatibility import (
        AttestationError,
        CompatibilityEvidence,
        PricingAssumptionAttestation,
    )

    with pytest.raises(AttestationError, match=r"reference"):
        PricingAssumptionAttestation(
            dimension=PricingDimension.DAY_COUNT,
            evidence=CompatibilityEvidence(
                source=EvidenceSource.VENDOR_DOCUMENTATION, observed_at="2026-07-31"
            ),
        )


def test_an_attestation_without_a_date_cannot_be_constructed():
    from src.config.compatibility import (
        AttestationError,
        CompatibilityEvidence,
        PricingAssumptionAttestation,
    )

    with pytest.raises(AttestationError, match=r"observed_at"):
        PricingAssumptionAttestation(
            dimension=PricingDimension.DAY_COUNT,
            evidence=CompatibilityEvidence(
                source=EvidenceSource.VENDOR_DOCUMENTATION, reference="somewhere"
            ),
        )


def test_a_partial_attestation_set_leaves_the_rest_blocking():
    """Answering six of seven questions is not answering the seventh."""
    partial = pipeline(
        **resolved_settings(
            pricing_attestations=attestations((PricingDimension.DAY_COUNT,)),
            **CAPTURE_SETTINGS,
        )
    )
    result = readiness(pipeline=partial)
    assert not result.calculation_trusted
    assert PricingDimension.DAY_COUNT not in (
        partial.pricing_compatibility.load_bearing_unknowns
    )


# =============================================================================
# §6 -- raw capture is mandatory for capture readiness
# =============================================================================


def test_capture_disabled_is_not_capture_ready():
    """The one thing READY_FOR_RAW_CAPTURE_ONLY has to mean."""
    result = readiness(pipeline=resolved_pipeline(raw_capture_enabled=False))
    assert not result.ready
    assert any("raw_capture_enabled" in blocker for blocker in result.blockers)
    assert result.state is CertificationState.NOT_READY


def test_capture_enabled_with_no_path_never_reaches_readiness():
    """Refused at load time, which is earlier and therefore better."""
    from src.config.thetadata import ThetaDataConfigError

    with pytest.raises(ThetaDataConfigError, match=r"raw_capture_path"):
        resolved_pipeline(raw_capture_enabled=True, raw_capture_path=None)


def test_capture_configured_with_no_store_to_check_is_not_capture_ready():
    result = readiness(raw_store=None)
    assert not result.ready
    assert any("no store was supplied" in blocker for blocker in result.blockers)


# =============================================================================
# §3/§4/§5 -- the state machine and the evidence it needs
# =============================================================================


def test_an_untyped_capture_is_refused_outright():
    """The v2.1.4 regression, stated as plainly as it deserves.

    ``assess_readiness(capture_manifest=object(), validation_report=object())``
    returned ADAPTER_CERTIFIED.
    """
    with pytest.raises(TypeError, match=r"not a capture"):
        readiness(capture=object())


def test_an_untyped_validation_is_refused_outright():
    with pytest.raises(TypeError, match=r"not a validation"):
        readiness(capture=captured(), validation=object())


def test_a_manifest_the_store_cannot_support_is_not_a_capture():
    store = InMemoryRawStore()
    orphan = RawCaptureManifest(
        session_id="session",
        record_ids=("never-written",),
        payload_hashes=("0" * 64,),
    )
    verification = verify_capture(orphan, store)
    assert not verification.verified
    assert any(f.startswith("MISSING_RECORD:") for f in verification.failures)

    result = readiness(capture=verification)
    assert result.state is CertificationState.READY_FOR_RAW_CAPTURE_ONLY


def test_a_disabled_capture_manifest_is_not_a_capture():
    verification = verify_capture(RawCaptureManifest.disabled(), InMemoryRawStore())
    assert not verification.verified
    assert any("CAPTURE_DISABLED" in f for f in verification.failures)


def test_a_verified_capture_with_unknown_pricing_stops_at_raw_capture():
    result = readiness(pipeline=pipeline(), capture=captured())
    assert result.state is CertificationState.RAW_CAPTURE_COMPLETED
    assert not result.calculation_trusted


def test_a_verified_capture_with_resolved_pricing_permits_an_unvalidated_calculation():
    result = readiness(capture=captured())
    assert result.state is CertificationState.CALCULATION_NOT_VALIDATED
    assert result.calculation_trusted


def test_a_validation_report_must_describe_this_capture():
    capture = captured()
    foreign = AdapterValidationReport(
        manifest_hash="0000000000000000",
        checks=(ValidationCheck(name="anything", passed=True),),
    )
    result = readiness(capture=capture, validation=foreign)
    assert any(
        "describes manifest" in blocker for blocker in result.calculation_blockers
    )
    assert result.state is CertificationState.RAW_CAPTURE_COMPLETED


def test_an_empty_validation_report_cannot_be_constructed():
    with pytest.raises(ValueError, match=r"no checks"):
        AdapterValidationReport(manifest_hash="0000000000000000")


def test_a_validation_report_with_no_manifest_cannot_be_constructed():
    with pytest.raises(ValueError, match=r"manifest_hash"):
        AdapterValidationReport(
            manifest_hash="  ", checks=(ValidationCheck(name="x", passed=True),)
        )


def test_a_failing_validation_does_not_advance_the_state():
    capture = captured()
    result = readiness(
        capture=capture, validation=validation_for(capture, passed=False)
    )
    assert result.state is CertificationState.RAW_CAPTURE_COMPLETED


def test_documented_conventions_cannot_reach_certified():
    """The ceiling this release is under.

    Every attestation here rests on ``VENDOR_DOCUMENTATION``. Documentation
    records what the vendor says it does; only a live comparison observes what
    it did, and no such comparison has been run.
    """
    capture = captured()
    result = readiness(
        capture=capture, validation=validation_for(capture, dimensions=False)
    )
    assert result.state is CertificationState.CALCULATION_VALIDATED
    assert result.state is not CertificationState.ADAPTER_CERTIFIED


def test_planned_provenance_cannot_reach_certified():
    capture = captured()
    result = readiness(
        open_interest=planned_oi(),
        capture=capture,
        validation=validation_for(capture),
    )
    assert result.state is CertificationState.CALCULATION_VALIDATED
    assert dict(result.provenance_grades)["open_interest_as_of"] == "PLANNED"


def test_documented_conventions_can_never_reach_certified():
    """The ceiling this repository is under, whatever else is supplied.

    Every attestation it ships rests on ``VENDOR_DOCUMENTATION``. No arrangement
    of captures and validation reports promotes a claim about what the vendor
    says into an observation of what the vendor did.
    """
    capture = captured()
    for validation in (None, validation_for(capture)):
        result = readiness(capture=capture, validation=validation)
        assert result.state is not CertificationState.ADAPTER_CERTIFIED


def test_live_comparison_evidence_is_what_reaches_certified():
    """The top rung is real, not decoration -- and it needs a real comparison."""
    live = pipeline(
        **resolved_settings(
            pricing_attestations=attestations(source=EvidenceSource.LIVE_COMPARISON),
            **CAPTURE_SETTINGS,
        )
    )
    capture = captured()
    result = readiness(
        pipeline=live, capture=capture, validation=validation_for(capture)
    )
    assert result.state is CertificationState.ADAPTER_CERTIFIED


def test_blockers_override_every_other_state():
    capture = captured()
    result = readiness(spot=None, capture=capture, validation=validation_for(capture))
    assert result.state is CertificationState.NOT_READY


def test_missing_credentials_prevent_capture_readiness(monkeypatch):
    monkeypatch.delenv("THETA_USER", raising=False)
    monkeypatch.delenv("THETA_PASS", raising=False)

    # The client factory already refuses to build, which is the stronger
    # guarantee; swapping the config in afterwards is how the readiness report's
    # own credential blocker gets exercised at all.
    from dataclasses import replace

    needs_auth = replace(
        resolved_pipeline(),
        config=parse_thetadata_config(
            resolved_settings(
                authentication_mode="basic",
                username_env="THETA_USER",
                password_env="THETA_PASS",
                **CAPTURE_SETTINGS,
            )
        ),
    )
    result = readiness(pipeline=needs_auth)
    assert not result.ready
    assert any("credential" in blocker.lower() for blocker in result.blockers)


def test_the_state_is_serialised():
    payload = readiness().as_dict()
    assert payload["state"] == "READY_FOR_RAW_CAPTURE_ONLY"
    assert payload["schema_version"] == "adapter-certification/2.1.4"
    # No capture yet, so nothing can have been observed.
    assert payload["provenance_grades"]["spot"] == "PLANNED"
    assert readiness(capture=captured()).as_dict()["provenance_grades"]["spot"] == (
        "OBSERVED"
    )


@pytest.mark.parametrize("state", list(CertificationState))
def test_every_state_is_declared(state):
    assert state.value


def test_the_ladder_has_a_rung_for_each_distinct_claim():
    assert [state.value for state in CertificationState] == [
        "NOT_READY",
        "READY_FOR_RAW_CAPTURE_ONLY",
        "RAW_CAPTURE_COMPLETED",
        "CALCULATION_NOT_VALIDATED",
        "CALCULATION_VALIDATED",
        "ADAPTER_CERTIFIED",
    ]


def test_unknown_chain_completeness_is_still_only_a_warning():
    """It is a reason to capture, not a reason to refuse."""
    result = readiness()
    assert result.ready
    assert any("completeness" in warning.lower() for warning in result.warnings)


def test_documentation_backed_dimensions_are_called_out():
    result = readiness()
    assert any("vendor documentation" in warning for warning in result.warnings)


def test_a_tier_that_cannot_serve_the_mode_blocks():
    """Enforced at config load, so the pipeline cannot even be built."""
    from src.config.thetadata import ThetaDataConfigError

    with pytest.raises(ThetaDataConfigError, match=r"(?i)tier"):
        pipeline(tier="value")


# =============================================================================
# §14 -- provenance grades are derived, not asserted
# =============================================================================


def test_provenance_without_evidence_claims_nothing():
    assert not planned_oi().claims_observation
    assert grade_claim(planned_oi(), capture=captured(), validated=False) == (
        ProvenanceGrade.PLANNED,
        "",
    )


def test_a_claim_is_observed_only_once_a_capture_confirms_the_record():
    """The regression.

    The first cut of v2.1.4 graded a claim OBSERVED for carrying three
    non-empty strings. Nothing compared them against a capture, so evidence
    naming a record that does not exist, in a session that never happened,
    reached VALIDATED — and with it ADAPTER_CERTIFIED. Well-formed is not true.
    """
    capture = captured()
    assert grade_claim(observed_oi(capture), capture=capture, validated=False) == (
        ProvenanceGrade.OBSERVED,
        "",
    )


def test_evidence_naming_another_session_is_refused():
    capture = captured()
    transplanted = OpenInterestProvenance(
        as_of=date(2026, 3, 16),
        source="vendor_field",
        evidence=ProvenanceEvidence(
            raw_record_id=RECORD_ID,
            field_path="open_interest",
            manifest_hash="0000000000000000",
        ),
    )
    grade, complaint = grade_claim(transplanted, capture=capture, validated=True)
    assert grade is ProvenanceGrade.PLANNED
    assert "transplanted" in complaint

    result = readiness(capture=capture, open_interest=transplanted)
    assert any("manifest" in b for b in result.calculation_blockers)
    assert not result.calculation_trusted


def test_evidence_naming_an_unconfirmed_record_is_refused():
    capture = captured()
    invented = OpenInterestProvenance(
        as_of=date(2026, 3, 16),
        source="vendor_field",
        evidence=ProvenanceEvidence(
            raw_record_id="no-such-record",
            field_path="open_interest",
            manifest_hash=capture.manifest_hash,
        ),
    )
    grade, complaint = grade_claim(invented, capture=capture, validated=True)
    assert grade is ProvenanceGrade.PLANNED
    assert "does not confirm" in complaint


def test_evidence_without_a_capture_cannot_be_observed():
    capture = captured()
    grade, complaint = grade_claim(observed_oi(capture), capture=None, validated=True)
    assert grade is ProvenanceGrade.PLANNED
    assert "no verified capture" in complaint


def test_a_validation_report_that_binds_to_nothing_upgrades_nothing():
    """It read VALIDATED beside a blocker saying there was no capture to bind to."""
    capture = captured()
    result = readiness(
        open_interest=observed_oi(capture),
        spot=observed_spot(capture),
        validation=AdapterValidationReport(
            manifest_hash=capture.manifest_hash,
            checks=(ValidationCheck(name="open_interest_as_of", passed=True),),
        ),
    )
    assert dict(result.provenance_grades)["open_interest_as_of"] == "PLANNED"
    assert any("no capture" in b for b in result.calculation_blockers)


def test_local_evidence_cannot_settle_a_vendor_convention():
    """A YAML edit is not an observation of what the vendor did.

    ``LOCAL_CONFIGURATION`` means "both sides are ours", which is true of the
    rate and the dividend and false of the vendor's day count. The first cut
    accepted it everywhere, so writing seven attestations reached
    ADAPTER_CERTIFIED with no comparison having been run.
    """
    local = pipeline(
        **resolved_settings(
            pricing_attestations=attestations(
                source=EvidenceSource.LOCAL_CONFIGURATION
            ),
            **CAPTURE_SETTINGS,
        )
    )
    report = local.pricing_compatibility
    assert not report.compatible
    assert any(
        f.startswith("LOCAL_EVIDENCE_CANNOT_SETTLE_A_VENDOR_CONVENTION:")
        for f in report.hard_failures
    )

    capture = captured()
    result = readiness(
        pipeline=local, capture=capture, validation=validation_for(capture)
    )
    assert result.state is not CertificationState.ADAPTER_CERTIFIED


def test_a_duplicated_payload_cannot_satisfy_two_manifest_claims():
    """Membership is too weak: one retry written under two ids passed both."""
    from datetime import UTC, datetime

    store = InMemoryRawStore()
    now = datetime(2026, 3, 17, 15, 0, tzinfo=UTC)
    for record_id in ("r1", "r2"):
        store.put(
            record_id=record_id,
            endpoint="/v3/x",
            query_params={},
            payload="identical bytes",
            request_started_at=now,
            response_received_at=now,
            http_status=200,
        )
    shared = store.records()[0].payload_hash
    verification = verify_capture(
        RawCaptureManifest(
            session_id="s", record_ids=("r1", "r2"), payload_hashes=(shared, "b" * 64)
        ),
        store,
    )
    assert not verification.verified
    assert "PAYLOAD_HASHES_DO_NOT_PAIR_WITH_RECORDS" in verification.failures


def test_evidence_that_names_no_record_cannot_be_constructed():
    with pytest.raises(ValueError, match=r"raw_record_id"):
        ProvenanceEvidence(
            raw_record_id="", field_path="open_interest", manifest_hash="abc"
        )


def test_evidence_that_names_no_manifest_cannot_be_constructed():
    with pytest.raises(ValueError, match=r"manifest_hash"):
        ProvenanceEvidence(
            raw_record_id="r", field_path="open_interest", manifest_hash=""
        )


def test_a_validation_report_upgrades_observed_to_validated():
    capture = captured()
    result = readiness(capture=capture, validation=validation_for(capture))
    grades = dict(result.provenance_grades)
    assert grades["open_interest_as_of"] == "VALIDATED"
    assert grades["spot"] == "VALIDATED"


def test_readiness_never_implies_trading_readiness():
    result = readiness()
    assert result.trading_enabled is False
    assert "not a trading" in result.scope.lower()
