"""Certification is derived from raw evidence, or it is not certification.

**§2.** ``CaptureVerification`` was a public dataclass and ``assess_readiness``
accepted one. ``CaptureVerification(manifest=..., confirmed_record_ids=("fake",),
failures=())`` reports ``verified=True``, and the state machine believed it. The
verifier existed and the public API did not have to use it.

**§3.** ``AdapterValidationReport`` accepted any non-empty set of passing checks.
``ValidationCheck(name="anything", passed=True)`` was a validation. Nothing
required the checks to correspond to anything that had been checked.

**§7.** ``ProvenanceEvidence`` proved that a record id existed. It did not prove
the record contained the field, that the field held the claimed value, or that
the endpoint could have supplied it at all -- a Greeks response was accepted as
evidence for open interest.

**§6.** Pricing evidence and the validation report were independent. A dimension
counted as live-observed from a static pipeline attestation, with no validation
check naming it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from src.adapters.certification import (
    AdapterValidationReport,
    AdapterValidator,
    CertificationState,
    OpenInterestProvenance,
    SpotProvenance,
    SpotSource,
    ValidationCheck,
    VerifiedFieldObservation,
    assess_readiness,
)
from src.adapters.errors import (
    ThetaDataCertificationError,
    ThetaDataProvenanceError,
    ThetaDataValidationError,
)
from src.adapters.raw_store import (
    PARSER_VERSION,
    InMemoryRawStore,
    RawCaptureManifest,
)
from src.adapters.thetadata.endpoints import Endpoint
from src.config.compatibility import PricingDimension
from tests.certification_fixtures import (
    AS_OF,
    build_capture,
    readiness,
    resolved_pipeline,
)

NOW = datetime(2026, 3, 17, 15, 0, tzinfo=UTC)


# =============================================================================
# §2 -- a caller cannot hand in a capture verdict
# =============================================================================


def test_readiness_takes_a_manifest_and_a_store_not_a_verdict():
    """The public API cannot be given a conclusion, only the inputs to one."""
    import inspect

    parameters = set(inspect.signature(assess_readiness).parameters)
    assert {"manifest", "raw_store"} <= parameters
    assert "capture" not in parameters


def test_a_hand_built_capture_verification_is_refused():
    """The regression."""
    from src.adapters.certification import CaptureVerification
    from src.adapters.raw_store import ManifestRecord

    forged = CaptureVerification(
        manifest=RawCaptureManifest(
            session_id="s",
            records=(
                ManifestRecord(
                    record_id="fake-record",
                    endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT.value,
                    payload_hash="0" * 64,
                    parameter_hash="0" * 16,
                ),
            ),
        ),
        confirmed_record_ids=("fake-record",),
        failures=(),
        plan_fingerprint="anything",
        expected_pipeline_fingerprint="anything",
    )
    assert forged.verified  # it says so, and that is the point
    with pytest.raises(ThetaDataCertificationError):
        assess_readiness(
            pipeline=resolved_pipeline(),
            as_of=AS_OF,
            manifest=forged,  # type: ignore[arg-type]
            raw_store=InMemoryRawStore(),
        )


def test_a_forged_manifest_does_not_verify_against_a_real_store():
    from src.adapters.raw_store import ManifestRecord

    store, _ = build_capture()
    forged = RawCaptureManifest(
        session_id="s",
        records=(
            ManifestRecord(
                record_id="never-written",
                endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT.value,
                payload_hash="0" * 64,
                parameter_hash="0" * 16,
            ),
        ),
    )
    result = readiness(manifest=forged, raw_store=store)
    assert result.state is CertificationState.READY_FOR_RAW_CAPTURE_ONLY


def test_readiness_recomputes_verification_every_time():
    """Not cached, and not taken on trust from a previous call."""
    store, manifest = build_capture()
    first = readiness(manifest=manifest, raw_store=store)
    assert first.state is not CertificationState.READY_FOR_RAW_CAPTURE_ONLY

    # Same manifest, empty store. If the verdict were cached or trusted from the
    # previous call this would still read as a completed capture. The empty
    # store is durable, so the only thing wrong with it is that it is empty.
    from tests.certification_fixtures import durable_store

    empty = readiness(manifest=manifest, raw_store=durable_store())
    assert empty.state is CertificationState.READY_FOR_RAW_CAPTURE_ONLY


# =============================================================================
# §3 -- a validation report is derived, not written
# =============================================================================


def test_a_hand_built_validation_report_cannot_certify():
    """The regression: one arbitrary passing check was a validation."""
    store, manifest = build_capture()
    handmade = AdapterValidationReport(
        manifest_hash=manifest.manifest_hash,
        checks=(ValidationCheck(name="anything", passed=True),),
        validator="me",
        validated_at="2026-03-17",
    )
    result = readiness(manifest=manifest, raw_store=store, validation=handmade)
    assert result.state is not CertificationState.ADAPTER_CERTIFIED
    assert any("not issued by" in b for b in result.calculation_blockers)


def test_the_validator_produces_a_report_bound_to_the_capture():
    store, manifest = build_capture()
    report = AdapterValidator.validate(
        manifest=manifest, store=store, pipeline=resolved_pipeline()
    )
    assert report.manifest_hash == manifest.manifest_hash
    assert report.pipeline_fingerprint == resolved_pipeline().fingerprint()


def test_the_report_records_what_it_is_and_what_it_ran():
    store, manifest = build_capture()
    payload = AdapterValidator.validate(
        manifest=manifest, store=store, pipeline=resolved_pipeline()
    ).as_dict()
    for key in (
        "manifest_hash",
        "schema_version",
        "validator_version",
        "validated_at",
        "live_capture",
        "required_checks",
        "completed_checks",
        "failed_checks",
        "pricing_observations",
        "spot_observation",
        "open_interest_observation",
        "parser_version",
        "pipeline_fingerprint",
    ):
        assert key in payload, key


def test_a_report_missing_a_required_check_does_not_pass():
    store, manifest = build_capture()
    report = AdapterValidator.validate(
        manifest=manifest, store=store, pipeline=resolved_pipeline()
    )
    assert report.required_checks
    trimmed = AdapterValidationReport(
        manifest_hash=report.manifest_hash,
        checks=report.checks[:1],
        required_checks=report.required_checks,
        validator=report.validator,
        validated_at=report.validated_at,
    )
    assert not trimmed.passed
    assert trimmed.missing_checks


@pytest.mark.parametrize(
    "dropped", ["spot_timestamp", "open_interest_as_of", "vendor_day_count"]
)
def test_dropping_any_required_check_prevents_validation(dropped):
    store, manifest = build_capture()
    report = AdapterValidator.validate(
        manifest=manifest, store=store, pipeline=resolved_pipeline()
    )
    kept = tuple(check for check in report.checks if check.name != dropped)
    trimmed = AdapterValidationReport(
        manifest_hash=report.manifest_hash,
        checks=kept,
        required_checks=report.required_checks,
        validator=report.validator,
        validated_at=report.validated_at,
    )
    assert dropped in trimmed.missing_checks
    assert not trimmed.passed


def test_a_report_for_another_manifest_is_rejected():
    store, manifest = build_capture()
    other_store, other_manifest = build_capture(session_id="other")
    report = AdapterValidator.validate(
        manifest=other_manifest, store=other_store, pipeline=resolved_pipeline()
    )
    result = readiness(manifest=manifest, raw_store=store, validation=report)
    assert any("describes manifest" in b for b in result.calculation_blockers)


def test_a_report_for_another_pipeline_is_rejected():
    store, manifest = build_capture()
    report = AdapterValidator.validate(
        manifest=manifest, store=store, pipeline=resolved_pipeline()
    )
    other = resolved_pipeline(rate_value=3.1)
    result = readiness(
        pipeline=other, manifest=manifest, raw_store=store, validation=report
    )
    assert any("pipeline" in b.lower() for b in result.calculation_blockers)


def test_an_empty_report_cannot_be_constructed():
    with pytest.raises(ThetaDataValidationError, match=r"no checks"):
        AdapterValidationReport(manifest_hash="a" * 64)


# =============================================================================
# §7 -- field-level provenance, re-read from the payload
# =============================================================================


def observation(**overrides) -> VerifiedFieldObservation:
    payload = {
        "record_id": "r",
        "endpoint": Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT.value,
        "payload_hash": "0" * 64,
        "parser_version": PARSER_VERSION,
        "field_path": "open_interest",
        "observed_value": 4200,
    }
    payload.update(overrides)
    return VerifiedFieldObservation(**payload)


def test_an_observation_naming_no_record_is_refused():
    with pytest.raises(ThetaDataProvenanceError, match=r"record_id"):
        observation(record_id="")


def test_an_observation_with_an_unknown_parser_version_is_refused():
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)parser"):
        observation(parser_version="somebody-elses-parser/9")


def test_a_missing_field_cannot_be_observed():
    store, manifest = build_capture()
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)field"):
        AdapterValidator.observe_field(
            manifest=manifest,
            store=store,
            endpoint=Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT,
            field_path="a_field_that_is_not_there",
        )


def test_the_wrong_endpoint_cannot_support_the_claim():
    """A Greeks payload is not evidence about open interest."""
    store, manifest = build_capture()
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)endpoint"):
        AdapterValidator.observe_field(
            manifest=manifest,
            store=store,
            endpoint=Endpoint.OPTION_GREEKS_FIRST_ORDER,
            field_path="open_interest",
        )


def test_a_greeks_record_cannot_prove_the_index_price():
    store, manifest = build_capture()
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)endpoint"):
        AdapterValidator.observe_field(
            manifest=manifest,
            store=store,
            endpoint=Endpoint.OPTION_GREEKS_FIRST_ORDER,
            field_path="price",
        )


def test_a_value_differing_from_the_payload_is_rejected():
    store, manifest = build_capture()
    observed = AdapterValidator.observe_field(
        manifest=manifest,
        store=store,
        endpoint=Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT,
        field_path="open_interest",
    )
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)claimed"):
        AdapterValidator.confirm_field(
            manifest=manifest,
            store=store,
            observation=observation(
                record_id=observed.record_id,
                payload_hash=observed.payload_hash,
                observed_value=999999,
            ),
        )


def test_a_record_from_another_manifest_is_rejected():
    store, manifest = build_capture()
    _, other = build_capture(session_id="other")
    observed = AdapterValidator.observe_field(
        manifest=manifest,
        store=store,
        endpoint=Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT,
        field_path="open_interest",
    )
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)manifest"):
        AdapterValidator.confirm_field(
            manifest=other, store=store, observation=observed
        )


def test_an_observed_field_carries_its_own_bytes():
    store, manifest = build_capture()
    observed = AdapterValidator.observe_field(
        manifest=manifest,
        store=store,
        endpoint=Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT,
        field_path="open_interest",
    )
    assert observed.observed_value == 4200
    assert observed.endpoint == Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT.value
    assert observed.observed_value_hash


# =============================================================================
# §6 -- pricing evidence must be bound to a validation check
# =============================================================================


def test_static_pricing_evidence_alone_cannot_certify():
    """The regression: a pipeline attestation counted as live-observed."""
    store, manifest = build_capture()
    result = readiness(
        manifest=manifest,
        raw_store=store,
        validation=AdapterValidator.validate(
            manifest=manifest, store=store, pipeline=resolved_pipeline()
        ),
    )
    assert result.state is not CertificationState.ADAPTER_CERTIFIED


def test_the_validator_names_the_dimensions_it_observed():
    store, manifest = build_capture()
    report = AdapterValidator.validate(
        manifest=manifest, store=store, pipeline=resolved_pipeline()
    )
    named = {check.dimension for check in report.checks if check.dimension}
    assert PricingDimension.IV_PRICE_BASIS in named


def test_a_pricing_check_carries_the_records_it_read():
    store, manifest = build_capture()
    report = AdapterValidator.validate(
        manifest=manifest, store=store, pipeline=resolved_pipeline()
    )
    for check in report.checks:
        if check.dimension is not None:
            assert check.record_ids, check.name


# =============================================================================
# §12 -- provenance objects are temporally valid
# =============================================================================


def test_a_naive_spot_timestamp_is_refused():
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)timezone|naive"):
        SpotProvenance(
            source=SpotSource.VENDOR_INDEX_SNAPSHOT,
            timestamp=datetime(2026, 3, 17, 11, 0),
        )


def test_a_nan_tolerance_is_refused():
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)tolerance"):
        SpotProvenance(
            source=SpotSource.VENDOR_INDEX_SNAPSHOT,
            timestamp=AS_OF,
            tolerance_seconds=float("nan"),
        )


def test_a_negative_tolerance_is_refused():
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)tolerance"):
        SpotProvenance(
            source=SpotSource.VENDOR_INDEX_SNAPSHOT,
            timestamp=AS_OF,
            tolerance_seconds=-1.0,
        )


def test_an_unrecognised_spot_source_is_refused():
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)source"):
        SpotProvenance(source="whatever_i_like", timestamp=AS_OF)  # type: ignore[arg-type]


def test_a_materially_future_dated_spot_is_refused():
    from datetime import timedelta

    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)future"):
        SpotProvenance(
            source=SpotSource.VENDOR_INDEX_SNAPSHOT,
            timestamp=datetime.now(UTC) + timedelta(days=2),
        )


def test_an_open_interest_date_after_the_chain_is_refused():
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)after"):
        OpenInterestProvenance(
            as_of=date(2026, 3, 18),
            source="vendor_field",
            chain_date=date(2026, 3, 17),
        )


def test_an_unrecognised_open_interest_source_is_refused():
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)source"):
        OpenInterestProvenance(as_of=date(2026, 3, 16), source="made_up")
