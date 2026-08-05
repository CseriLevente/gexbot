"""A chain cannot authorize its own calculation.

v2.1.5 gated `compute_trusted_gex` on facts it read out of `chain.meta`: the
pipeline fingerprint, the raw-capture manifest, the spot provenance. All three
are metadata written into the snapshot by the code that produced it, and a
snapshot is not a witness to its own provenance -- `ChainSnapshot.with_meta`
is public, and a synthetic chain with the right keys satisfied every check.

The manifest was checked the same way: `verify_capture` confirmed that the
records a manifest *named* were in the store, and took the manifest's own
`pipeline_fingerprint`, `capture_plan_fingerprint`, `parser_version` and
`session_id` on trust. An empty fingerprint meant "no claim to check" rather
than "unverifiable".

v2.1.6 moves the authorization out of the snapshot: `compute_trusted_gex`
requires a `VerifiedCalculationContext`, which only
`build_verified_calculation_context` can produce, and which recomputes every
verification from the manifest and the store.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from src.adapters.certification import (
    AdapterValidator,
    build_verified_calculation_context,
    verify_capture,
)
from src.adapters.errors import ThetaDataCertificationError
from src.adapters.raw_store import (
    BODY_REPRESENTATION,
    PARSER_VERSION,
    RAW_RESPONSE_SCHEMA_VERSION,
    InMemoryRawStore,
)
from src.config.pipeline import PipelineConsistencyError
from tests.certification_fixtures import (
    AS_OF,
    build_capture,
    captured_chain,
    context_for,
    durable_store,
    plan_for,
    resolved_pipeline,
    trusted_evidence,
    unresolved_pipeline,
)

# =============================================================================
# §1 -- trusted calculation requires an independently verified context
# =============================================================================


def test_the_trusted_api_takes_evidence_rather_than_a_verdict():
    """The signature itself.

    v2.1.6 accepted a ``VerifiedCalculationContext``. It is a public frozen
    dataclass whose ``context_hash`` any caller can recompute, so an edited
    context with a freshly computed hash was internally consistent and said
    whatever the caller wanted. A hash proves the fields agree with the digest;
    it says nothing about who computed them.
    """
    import inspect

    from src.config.pipeline import ThetaDataResearchPipeline

    signature = inspect.signature(ThetaDataResearchPipeline.compute_trusted_gex)
    assert "context" not in signature.parameters
    for required in ("manifest", "store"):
        assert signature.parameters[required].default is inspect.Parameter.empty


def test_a_caller_edited_context_cannot_authorize_a_calculation():
    """The regression. This exact sequence worked against v2.1.6."""
    import dataclasses as dc

    from src.config.compatibility import PricingCompatibilityReport

    taken = captured_chain(unresolved_pipeline())
    context = context_for(taken)

    forged = dc.replace(
        context,
        effective_pricing_compatibility=PricingCompatibilityReport(),
        context_hash="",
    )
    forged = dc.replace(forged, context_hash=forged.recomputed_hash())
    assert forged.context_hash == forged.recomputed_hash()  # internally consistent

    # And there is nowhere to put it. The trusted path has no parameter that
    # would accept a verdict, and the unresolved pricing it tried to paper over
    # still refuses.
    with pytest.raises(TypeError):
        taken.pipeline.compute_trusted_gex(taken.chain, context=forged)
    with pytest.raises(PipelineConsistencyError, match=r"(?i)unknown|unresolved"):
        taken.pipeline.compute_trusted_gex(taken.chain, **trusted_evidence(taken))


def test_a_forged_chain_cannot_authorize_itself():
    """The regression.

    Every key v2.1.5 read out of ``chain.meta`` to decide trust, written into a
    synthetic chain that has never been near a capture.
    """
    from src.synthetic.chains import build_synthetic_chain

    taken = captured_chain()
    pipeline = taken.pipeline
    forged = dataclasses.replace(
        build_synthetic_chain(),
        as_of=AS_OF,
        meta={
            "pipeline": {
                "pipeline_fingerprint": pipeline.fingerprint(),
                "engine_fingerprint": pipeline.engine_config.fingerprint(),
                "capture_plan_fingerprint": pipeline.capture_plan.fingerprint,
            },
            # The real manifest, copied wholesale. Every v2.1.5 gate read
            # exactly this, and was satisfied by it.
            "raw_capture_manifest": taken.manifest.as_dict(),
            "spot_provenance": {
                "source": "vendor_index_snapshot",
                "timestamp": AS_OF.isoformat(),
                "observation": {"record_id": "anything"},
            },
            "open_interest_as_of": "2026-03-16",
        },
    )
    with pytest.raises(PipelineConsistencyError):
        pipeline.compute_trusted_gex(forged, **trusted_evidence(taken))


def test_evidence_from_another_pipeline_is_refused():
    taken = captured_chain()
    other = captured_chain(resolved_pipeline(rate_value=3.1))
    with pytest.raises(PipelineConsistencyError, match=r"(?i)pipeline|capture"):
        taken.pipeline.compute_trusted_gex(taken.chain, **trusted_evidence(other))


def test_evidence_for_another_capture_is_refused():
    pipeline = resolved_pipeline()
    taken = captured_chain(pipeline)
    other = captured_chain(pipeline)
    with pytest.raises(PipelineConsistencyError, match=r"(?i)manifest"):
        pipeline.compute_trusted_gex(taken.chain, **trusted_evidence(other))


def test_the_context_is_a_report_the_builder_produces():
    """It still exists, and it is no longer an argument to anything."""
    from src.adapters.certification import VerifiedCalculationContext

    taken = captured_chain()
    assert isinstance(context_for(taken), VerifiedCalculationContext)


def test_the_builder_refuses_a_precomputed_verification():
    import inspect

    parameters = set(inspect.signature(build_verified_calculation_context).parameters)
    assert "capture_verification" not in parameters
    assert {"pipeline", "manifest", "store"} <= parameters


def test_a_resolved_pipeline_with_real_evidence_can_compute():
    """The gate must leave a path through it."""
    taken = captured_chain()
    snapshot = taken.pipeline.compute_trusted_gex(
        taken.chain, **trusted_evidence(taken)
    )
    assert snapshot.meta["trusted"] is True
    assert snapshot.meta["evidence_context_hash"]
    assert snapshot.meta["normalized_chain_receipt"]["normalized_chain_hash"]


def test_a_diagnostic_still_needs_no_context():
    taken = captured_chain()
    snapshot = taken.pipeline.compute_diagnostic_gex(taken.chain)
    assert snapshot.meta["trusted"] is False


# =============================================================================
# §2 -- every material manifest field is bound to the store
# =============================================================================


def verified(manifest, store, pipeline=None):
    built = pipeline if pipeline is not None else resolved_pipeline()
    return verify_capture(
        manifest,
        store,
        plan=plan_for(built),
        expected_pipeline_fingerprint=built.fingerprint(),
    )


def test_a_manifest_naming_another_pipeline_does_not_verify():
    pipeline = resolved_pipeline()
    store, manifest = build_capture(pipeline=pipeline)
    tampered = dataclasses.replace(manifest, pipeline_fingerprint="0" * 16)
    assert not verified(tampered, store, pipeline).verified


def test_an_empty_pipeline_fingerprint_does_not_verify():
    """An absent claim is unverifiable, not exempt."""
    pipeline = resolved_pipeline()
    store, manifest = build_capture(pipeline=pipeline)
    tampered = dataclasses.replace(manifest, pipeline_fingerprint="")
    result = verified(tampered, store, pipeline)
    assert not result.verified
    assert any("pipeline_fingerprint" in f for f in result.failures)


def test_an_empty_capture_plan_fingerprint_does_not_verify():
    pipeline = resolved_pipeline()
    store, manifest = build_capture(pipeline=pipeline)
    tampered = dataclasses.replace(manifest, capture_plan_fingerprint="")
    result = verified(tampered, store, pipeline)
    assert not result.verified
    assert any("capture_plan_fingerprint" in f for f in result.failures)


def test_a_wrong_parser_version_does_not_verify():
    pipeline = resolved_pipeline()
    store, manifest = build_capture(pipeline=pipeline)
    tampered = dataclasses.replace(manifest, parser_version="thetadata-v3-parser/1.0.0")
    result = verified(tampered, store, pipeline)
    assert not result.verified
    assert any("parser" in f.lower() for f in result.failures)


def test_a_wrong_session_id_does_not_verify():
    pipeline = resolved_pipeline()
    store, manifest = build_capture(pipeline=pipeline)
    tampered = dataclasses.replace(manifest, session_id="not-this-session")
    result = verified(tampered, store, pipeline)
    assert not result.verified
    assert any("session" in f.lower() for f in result.failures)


def test_an_incorrect_request_parameter_hash_does_not_verify():
    """The parameters that produced a response are part of what it is."""
    pipeline = resolved_pipeline()
    store, manifest = build_capture(pipeline=pipeline)
    records = list(manifest.records)
    records[0] = dataclasses.replace(records[0], parameter_hash="0" * 16)
    tampered = dataclasses.replace(manifest, records=tuple(records))
    result = verified(tampered, store, pipeline)
    assert not result.verified
    assert any("parameter" in f.lower() for f in result.failures)


def test_a_payload_hash_bound_to_the_wrong_record_does_not_verify():
    """v2.1.5 compared the hash *sets*, so two records could swap payloads."""
    pipeline = resolved_pipeline()
    store, manifest = build_capture(pipeline=pipeline)
    records = list(manifest.records)
    first, second = records[0], records[1]
    records[0] = dataclasses.replace(first, payload_hash=second.payload_hash)
    records[1] = dataclasses.replace(second, payload_hash=first.payload_hash)
    tampered = dataclasses.replace(manifest, records=tuple(records))
    assert not verified(tampered, store, pipeline).verified


def test_a_duplicate_record_id_does_not_verify():
    pipeline = resolved_pipeline()
    store, manifest = build_capture(pipeline=pipeline)
    records = list(manifest.records)
    records[1] = dataclasses.replace(records[1], record_id=records[0].record_id)
    tampered = dataclasses.replace(manifest, records=tuple(records))
    assert not verified(tampered, store, pipeline).verified


def test_a_non_2xx_record_cannot_verify_as_a_successful_capture():
    """An error body is not evidence about the market."""
    pipeline = resolved_pipeline()
    store, manifest = build_capture(pipeline=pipeline, http_status=503)
    result = verified(manifest, store, pipeline)
    assert not result.verified
    assert any("status" in f.lower() for f in result.failures)


# =============================================================================
# §3 -- the manifest hash binds per-record semantics
# =============================================================================


def mutated_hash(manifest, **changes):
    records = list(manifest.records)
    records[0] = dataclasses.replace(records[0], **changes)
    return dataclasses.replace(manifest, records=tuple(records)).manifest_hash


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "a-different-request"},
        {"request_sequence": 99},
        {"http_status": 201},
        {"parameter_hash": "f" * 16},
        {"payload_hash": "f" * 64},
        {"endpoint": "/v3/option/snapshot/quote/other"},
        {"vendor_schema_version": "v9"},
        {"request_started_at": datetime(2020, 1, 1, tzinfo=UTC)},
        {"response_received_at": datetime(2020, 1, 1, tzinfo=UTC)},
    ],
)
def test_mutating_any_audit_relevant_field_moves_the_manifest_hash(change):
    _, manifest = build_capture()
    assert mutated_hash(manifest, **change) != manifest.manifest_hash


def test_the_manifest_carries_per_record_descriptors():
    from src.adapters.raw_store import ManifestRecord

    _, manifest = build_capture()
    assert manifest.records
    assert all(isinstance(r, ManifestRecord) for r in manifest.records)


def test_the_manifest_states_its_schema_version():
    _, manifest = build_capture()
    assert manifest.schema_version == "raw-capture-manifest/2.1.15"
    assert manifest.parser_version == PARSER_VERSION


def test_an_old_schema_manifest_is_refused_rather_than_reinterpreted():
    pipeline = resolved_pipeline()
    store, manifest = build_capture(pipeline=pipeline)
    stale = dataclasses.replace(manifest, schema_version="raw-capture-manifest/2.1.7")
    result = verified(stale, store, pipeline)
    assert not result.verified
    assert any("schema" in f.lower() for f in result.failures)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (
            "raw_response_schema_version",
            "raw-response/2.1.12",
            "RAW_RESPONSE_SCHEMA_UNSUPPORTED",
        ),
        (
            "body_representation",
            "decoded-text-reencoded-as-utf8",
            "BODY_REPRESENTATION_UNSUPPORTED",
        ),
    ],
)
def test_a_record_under_older_raw_response_semantics_does_not_verify(
    field, value, expected
):
    """The named regression: what the bytes *are* is part of the evidence.

    A payload hash says the bytes have not changed since they were written. It
    does not say what they are. Through v2.1.12 the stored bytes were a UTF-8
    re-encoding of a lossily decoded string, so a digest from that era covers a
    different thing from a digest today -- and comparing them under v2.1.14
    rules would be reading an answer to a question that was never asked.
    Refused, not reinterpreted.
    """
    pipeline = resolved_pipeline()
    store, manifest = build_capture(pipeline=pipeline)
    older = dataclasses.replace(
        manifest,
        records=(
            dataclasses.replace(manifest.records[0], **{field: value}),
            *manifest.records[1:],
        ),
    )
    result = verified(older, store, pipeline)
    assert not result.verified
    assert any(expected in failure for failure in result.failures), result.failures


# =============================================================================
# §4 -- paid capture needs durable storage
# =============================================================================


def test_an_in_memory_store_is_volatile():
    from src.adapters.raw_store import StoreDurability

    assert InMemoryRawStore().durability is StoreDurability.TEST_ONLY_VOLATILE
    assert not InMemoryRawStore().durable


def test_a_file_store_is_durable(tmp_path):
    from src.adapters.raw_store import FileRawStore, StoreDurability

    store = FileRawStore(tmp_path / "raw")
    assert store.durability is StoreDurability.DURABLE_APPEND_ONLY
    assert store.durable


def test_an_in_memory_store_cannot_be_capture_ready():
    """The regression. A paid session's only copy cannot live in a process."""
    from tests.certification_fixtures import readiness

    result = readiness(raw_store=InMemoryRawStore())
    assert not result.ready
    assert any("durable" in b.lower() for b in result.blockers)


def test_a_durable_store_can_be_capture_ready(tmp_path):
    from tests.certification_fixtures import readiness

    assert readiness(raw_store=durable_store(tmp_path)).ready


def test_readiness_requires_free_space(tmp_path, monkeypatch):
    import shutil

    from tests.certification_fixtures import readiness

    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(1, 1, 1024)
    )
    result = readiness(raw_store=durable_store(tmp_path))
    assert not result.ready
    assert any("space" in b.lower() for b in result.blockers)


def test_the_probe_does_not_enter_the_capture_index(tmp_path):
    from src.adapters.raw_store import FileRawStore, probe_raw_store

    store = FileRawStore(tmp_path / "raw")
    probe_raw_store(store)
    assert store.records() == ()
    assert store.next_request_sequence() == 1


# =============================================================================
# §10 -- capture origin is derived, not asserted
# =============================================================================


def test_an_offline_fixture_capture_is_labelled_as_one():
    from src.adapters.raw_store import CaptureOrigin

    _, manifest = build_capture()
    assert manifest.capture_origin is CaptureOrigin.OFFLINE_FIXTURE
    assert all(
        r.capture_origin is CaptureOrigin.OFFLINE_FIXTURE for r in manifest.records
    )


def test_relabelling_the_manifest_does_not_change_what_the_records_say():
    """v2.1.7: the origin is derived. A declaration is not evidence.

    v2.1.6 stored it as a field on the manifest, so calling an offline fixture
    a live capture was one assignment -- and every live-capture check
    downstream read the relabelled value.
    """
    from src.adapters.raw_store import CaptureOrigin

    _, manifest = build_capture()
    relabelled = dataclasses.replace(
        manifest, declared_capture_origin=CaptureOrigin.LIVE_HTTP_CAPTURE
    )
    assert relabelled.capture_origin is CaptureOrigin.OFFLINE_FIXTURE
    assert not relabelled.declared_origin_matches_records
    assert relabelled.manifest_hash != manifest.manifest_hash


def test_a_manifest_whose_declaration_contradicts_its_records_does_not_verify():
    from src.adapters.raw_store import CaptureOrigin

    pipeline = resolved_pipeline()
    store, manifest = build_capture(pipeline=pipeline)
    relabelled = dataclasses.replace(
        manifest, declared_capture_origin=CaptureOrigin.LIVE_HTTP_CAPTURE
    )
    result = verified(relabelled, store, pipeline)
    assert not result.verified
    assert any("origin" in f.lower() for f in result.failures)


def test_relabelling_every_record_still_cannot_make_a_fixture_live():
    """Even editing the records does not survive verification.

    The descriptors would then agree with each other and with the declaration,
    so the derived origin *does* read live -- and the store still holds the
    original bytes, whose own ``capture_origin`` is what the record-level
    comparison checks against.
    """
    from src.adapters.raw_store import CaptureOrigin

    pipeline = resolved_pipeline()
    store, manifest = build_capture(pipeline=pipeline)
    forged = dataclasses.replace(
        manifest,
        records=tuple(
            dataclasses.replace(r, capture_origin=CaptureOrigin.LIVE_HTTP_CAPTURE)
            for r in manifest.records
        ),
        declared_capture_origin=CaptureOrigin.LIVE_HTTP_CAPTURE,
    )
    assert forged.capture_origin is CaptureOrigin.LIVE_HTTP_CAPTURE
    result = verified(forged, store, pipeline)
    assert not result.verified
    assert any("CAPTURE_ORIGIN_MISMATCH" in f for f in result.failures)


def test_a_mixed_origin_capture_is_not_any_origin():
    from src.adapters.raw_store import CaptureOrigin

    _, manifest = build_capture()
    records = list(manifest.records)
    records[0] = dataclasses.replace(
        records[0], capture_origin=CaptureOrigin.LIVE_HTTP_CAPTURE
    )
    mixed = dataclasses.replace(manifest, records=tuple(records))
    assert mixed.capture_origin is CaptureOrigin.UNKNOWN_ORIGIN
    assert not mixed.origin_is_uniform


def test_an_offline_fixture_never_reads_as_a_live_capture():
    store, manifest = build_capture()
    report = AdapterValidator.validate(
        manifest=manifest, store=store, pipeline=resolved_pipeline()
    )
    assert report.live_capture is False


def test_an_offline_fixture_cannot_reach_certified():
    from src.adapters.certification import CertificationState
    from tests.certification_fixtures import readiness

    store, manifest = build_capture()
    result = readiness(
        manifest=manifest,
        raw_store=store,
        validation=AdapterValidator.validate(
            manifest=manifest, store=store, pipeline=resolved_pipeline()
        ),
    )
    assert result.state is not CertificationState.ADAPTER_CERTIFIED


# =============================================================================
# §9 -- raw metadata integrity
# =============================================================================


def base_metadata(**overrides):
    payload = {
        "record_id": "r1",
        "endpoint": "/v3/option/snapshot/quote",
        "payload_hash": "0" * 64,
        "byte_length": 12,
        "payload_location": "memory://r1",
        "parser_version": PARSER_VERSION,
        "request_started_at": "2026-03-17T15:00:00+00:00",
        "response_received_at": "2026-03-17T15:00:01+00:00",
        "capture_complete": True,
        "query_params": {"root": "SPXW"},
        "http_status": 200,
        "request_id": "req-1",
        "request_sequence": 1,
        # What the stored bytes are. Required since v2.1.14: a record without it
        # predates the byte-preserving store, so its digest may cover a UTF-8
        # re-encoding of decoded text rather than the response.
        "raw_response_schema_version": RAW_RESPONSE_SCHEMA_VERSION,
        "body_representation": BODY_REPRESENTATION,
    }
    payload.update(overrides)
    return payload


def test_well_formed_metadata_validates():
    from src.adapters.raw_store import validate_metadata

    assert validate_metadata(base_metadata()) == (None, "")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("query_params", "not-a-mapping"),
        ("http_status", "200"),
        ("request_id", 7),
        ("request_sequence", -1),
        ("request_sequence", "1"),
        ("parser_version", "somebody-elses/1"),
        ("payload_location", ""),
        ("vendor_schema_version", 3),
        ("request_started_at", "2026-03-17T15:00:00"),
        ("response_received_at", "2026-03-17T14:00:00+00:00"),
        # v2.1.14: what the bytes are, and under which rules.
        ("raw_response_schema_version", "raw-response/2.1.12"),
        ("body_representation", "decoded-text-reencoded-as-utf8"),
    ],
)
def test_malformed_metadata_is_rejected(field, value):
    from src.adapters.raw_store import validate_metadata

    status, detail = validate_metadata(base_metadata(**{field: value}))
    assert status is not None, (field, value, detail)


# =============================================================================
# §8 -- typed provenance values
# =============================================================================


def test_a_datetime_as_an_open_interest_date_is_refused():
    from src.adapters.certification import OpenInterestProvenance
    from src.adapters.errors import ThetaDataProvenanceError

    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)date"):
        OpenInterestProvenance(
            as_of=datetime(2026, 3, 16, tzinfo=UTC), source="vendor_field"
        )


def test_a_datetime_as_a_chain_date_is_refused():
    from src.adapters.certification import OpenInterestProvenance
    from src.adapters.errors import ThetaDataProvenanceError

    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)date"):
        OpenInterestProvenance(
            as_of=datetime(2026, 3, 16, tzinfo=UTC).date(),
            source="vendor_field",
            chain_date=datetime(2026, 3, 17, tzinfo=UTC),
        )


@pytest.mark.parametrize("value", ["2026-03-17T11:00:00+00:00", 1773000000, 3.5])
def test_a_non_datetime_spot_timestamp_is_refused(value):
    from src.adapters.certification import SpotProvenance, SpotSource
    from src.adapters.errors import ThetaDataProvenanceError

    with pytest.raises(ThetaDataProvenanceError):
        SpotProvenance(source=SpotSource.VENDOR_INDEX_SNAPSHOT, timestamp=value)


@pytest.mark.parametrize(
    "observed_at", ["", "not-a-date", "2026-02-30", "2999-01-01", "17/03/2026"]
)
def test_an_invalid_observed_at_fails_configuration(observed_at):
    from src.config.thetadata import ThetaDataConfigError, parse_thetadata_config

    with pytest.raises(ThetaDataConfigError, match=r"(?i)observed_at|date"):
        parse_thetadata_config(
            {
                "pricing_attestations": [
                    {
                        "dimension": "DAY_COUNT",
                        "source": "VENDOR_DOCUMENTATION",
                        "reference": "tests/fixtures/vendor_conventions.md",
                        "observed_at": observed_at,
                        "vendor_value": "ACT/365F",
                    }
                ]
            }
        )


def test_a_valid_observed_at_is_stored_as_a_date():
    from datetime import date

    from src.config.thetadata import parse_thetadata_config

    built = parse_thetadata_config(
        {
            "pricing_attestations": [
                {
                    "dimension": "DAY_COUNT",
                    "source": "VENDOR_DOCUMENTATION",
                    "reference": "tests/fixtures/vendor_conventions.md",
                    "observed_at": "2026-08-01",
                    "vendor_value": "ACT/365F",
                }
            ]
        }
    )
    assert built.pricing_attestations[0].observed_on == date(2026, 8, 1)


def test_no_public_provenance_path_leaks_an_untyped_error():
    """Every refusal is catchable with the adapter's own base class."""
    from src.adapters.certification import OpenInterestProvenance, SpotProvenance
    from src.adapters.errors import ThetaDataError

    for build in (
        lambda: OpenInterestProvenance(as_of="2026-03-16", source="vendor_field"),
        lambda: SpotProvenance(source="made_up", timestamp=AS_OF),
        lambda: SpotProvenance(source="vendor_index_snapshot", timestamp="now"),
    ):
        with pytest.raises(ThetaDataError):
            build()


def test_a_spot_timestamp_far_in_the_future_is_refused():
    from src.adapters.certification import SpotProvenance, SpotSource
    from src.adapters.errors import ThetaDataProvenanceError

    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)future"):
        SpotProvenance(
            source=SpotSource.VENDOR_INDEX_SNAPSHOT,
            timestamp=datetime.now(UTC) + timedelta(days=3),
        )


def test_capture_verification_refuses_a_non_manifest():
    with pytest.raises(ThetaDataCertificationError):
        verify_capture(object(), InMemoryRawStore(), plan=plan_for())  # type: ignore[arg-type]
