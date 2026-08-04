"""A documentation universe that survives leaving the process that made it.

v2.1.11 made documentation identities *extractive*: a rule names a document and
an extractor version, and the identities are what that extractor reads out of
the verified bytes. It also made ``capture_session`` re-run the whole resolution
before opening the chain operation.

Those two changes did not fit together. The re-run consulted the process-global
``UNIVERSE_DOCUMENTATION_RULES``, and the resolution being re-run had been made
with a registry the *caller* supplied -- so the capture refused every
documentation universe that was not in the global registry, and production keeps
that registry empty. There was no combination of arguments that worked.

Recovery had the same shape one level further on: it re-read the rule from the
global registry, so a universe recovered in a fresh process found nothing.

v2.1.12 persists the whole documentation resolution content-addressed: the rule
with no host path, the exact verified bytes, and the extraction. Every test here
fails against v2.1.11.
"""

from __future__ import annotations

import pathlib
from datetime import date

import pytest

from src.adapters.artifact_store import ArtifactKind, InMemoryArtifactStore
from src.adapters.raw_store import RawCaptureManifest
from src.domain.expected_universe import (
    ExpectedContractUniverse,
    ExpectedUniverseSourceKind,
    UniverseCoverageStatus,
)
from src.domain.universe_scope import UniverseRequestScope
from tests.certification_fixtures import (
    AS_OF,
    documented_settlement_rule,
    durable_store,
    resolved_pipeline,
)
from tests.unit.test_expected_universe_evidence import (
    FIXTURE_IDENTITIES,
    UNIVERSE_DOCUMENT,
    universe_rule,
)

SESSION = date(2026, 3, 17)


def scope():
    return UniverseRequestScope(root="SPXW", rights=("call", "put"), requested_at=AS_OF)


def declaration():
    return ExpectedContractUniverse(
        identities=FIXTURE_IDENTITIES,
        source_kind=ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION,
        source_record_ids=(),
        scope=scope(),
        documentation_evidence_id="listed-universe",
        declared_at=AS_OF,
    )


def documented_capture(**changes):
    """Resolve a documentation universe with a *caller's* registry, and capture.

    Exactly the combination v2.1.11 could not perform: the registry is local to
    this test, and ``capture_session`` re-runs the resolution with no registry
    at all.
    """
    pipeline = resolved_pipeline()
    store = durable_store()
    artifacts = InMemoryArtifactStore()

    registry = universe_rule(**changes)
    rule = registry.get("listed-universe")
    # The extraction instant is when the extractor ran, and a universe cannot be
    # observed after the chain that is measured against it. The fixture chain is
    # a March 2026 capture, so the reading has to be dated inside that session
    # rather than at this machine's wall clock -- the same reason every other
    # fixture pins ``AS_OF``. ``rule.confirm`` still re-reads the bytes.
    try:
        extraction = rule.extract(executed_at=AS_OF)
    except Exception:
        extraction = None

    resolution = pipeline.resolve_expected_universe(
        declaration=declaration(),
        registry=registry,
        session_date=SESSION,
        extraction=extraction,
    )
    if not resolution.established:
        return None, resolution, None, None

    rule = documented_settlement_rule()
    session = pipeline.capture_session(
        store=store,
        session_id="documented-chain",
        as_of=AS_OF,
        universe_resolution=resolution,
        settlement_rule=rule,
        artifact_store=artifacts,
    )
    mark = session.mark()
    chain = pipeline.fetch_chain(as_of=AS_OF, capture=session)
    manifest = RawCaptureManifest.from_session(
        session,
        since=mark,
        capture_plan_fingerprint=pipeline.capture_plan.fingerprint,
        pipeline_fingerprint=pipeline.fingerprint(),
    )
    return pipeline, resolution, (manifest, store, artifacts), chain


def test_a_custom_registry_resolution_can_open_a_capture():
    """The named regression.

    v2.1.11: the resolution succeeded, ``capture_session`` re-ran it without the
    registry, the rule was not found, and the capture was refused.
    """
    _, resolution, capture, chain = documented_capture()
    assert resolution.established, resolution.failure
    assert capture is not None, "the capture refused a resolution it had accepted"

    manifest, _, _ = capture
    assert resolution.artifact.coverage_status is (
        UniverseCoverageStatus.FULL_REQUEST_ENUMERATED
    )
    # The chain operation really was stamped with it.
    assert all(
        record.expected_universe_fingerprint == resolution.artifact.artifact_hash
        for record in manifest.records
    )
    assert chain.completeness.universe_artifact_hash == (
        resolution.artifact.artifact_hash
    )


def test_the_document_and_its_reading_are_persisted_beside_the_capture():
    _, resolution, capture, _ = documented_capture()
    _, _, artifacts = capture

    kinds = {artifacts.get(key)["kind"] for key in artifacts.keys()}  # noqa: SIM118
    assert ArtifactKind.UNIVERSE_DOCUMENTATION_EVIDENCE in kinds
    assert ArtifactKind.DOCUMENT_BYTES in kinds
    assert ArtifactKind.DOCUMENTATION_EVIDENCE in kinds

    # The stored bytes are the document, byte for byte.
    evidence = resolution.documentation_evidence
    stored = artifacts.payload_of(evidence.document_bytes_artifact_hash)
    assert stored["text"] == pathlib.Path(UNIVERSE_DOCUMENT).read_text(encoding="utf-8")


def test_recovery_works_with_an_empty_global_registry():
    """The named regression. Production keeps that registry empty."""
    from src.adapters.universe_evidence import UNIVERSE_DOCUMENTATION_RULES

    pipeline, resolution, capture, _ = documented_capture()
    manifest, store, artifacts = capture

    assert UNIVERSE_DOCUMENTATION_RULES.registered_ids() == ()
    recovered = pipeline.recover_capture_artifacts(
        manifest=manifest, store=store, artifact_store=artifacts
    )
    assert recovered.failures == (), recovered.failures
    assert recovered.expected_universe.artifact_hash == (
        resolution.artifact.artifact_hash
    )


def test_a_fresh_process_recovers_from_the_stores_alone():
    """Only the raw store and the artifact store, in a pipeline built from scratch.

    The point is that no object from the capturing pipeline survives: the rule,
    the registry and the document path are all gone, and the universe still
    recovers because the bytes and the rule are content-addressed beside the
    capture.
    """
    _, resolution, capture, _ = documented_capture()
    manifest, store, artifacts = capture

    fresh = resolved_pipeline()
    recovered = fresh.recover_capture_artifacts(
        manifest=manifest, store=store, artifact_store=artifacts
    )
    assert recovered.failures == (), recovered.failures
    universe = recovered.expected_universe
    assert universe.artifact_hash == resolution.artifact.artifact_hash
    assert universe.identity_set == FIXTURE_IDENTITIES
    assert universe.coverage_status is UniverseCoverageStatus.FULL_REQUEST_ENUMERATED


def test_the_rebuilt_rule_carries_no_host_path():
    """A path is a fact about one machine on one day."""
    from src.adapters.universe_evidence import RECOVERED_DOCUMENT_LOCATION

    _, resolution, _, _ = documented_capture()
    rebuilt = resolution.documentation_evidence.rule
    assert rebuilt.verified_location == RECOVERED_DOCUMENT_LOCATION
    assert not pathlib.Path(rebuilt.verified_location).exists()
    # And it is still established: registration happened, and this records when.
    assert rebuilt.established
    assert rebuilt.document_verified_at is not None


def test_changed_document_bytes_fail_recovery():
    """The bytes are what the extractor reads, so they are what must not move."""
    pipeline, resolution, capture, _ = documented_capture()
    manifest, store, artifacts = capture

    from src.adapters.universe_evidence import document_bytes_payload

    evidence = resolution.documentation_evidence
    tampered = (
        pathlib.Path(UNIVERSE_DOCUMENT)
        .read_text(encoding="utf-8")
        .replace("SPXW,2026-03-20,5010,C", "SPXW,2026-03-20,9999,C")
    )
    # Overwrite the stored bytes under the key the evidence names. A real store
    # would refuse the collision; this reaches past it to prove the *resolver*
    # refuses too, rather than relying on the store to notice.
    artifacts._documents[evidence.document_bytes_artifact_hash] = {
        "envelope_version": "capture-artifact/2.1.10",
        "kind": ArtifactKind.DOCUMENT_BYTES,
        "source_references": [],
        "payload": document_bytes_payload(tampered),
    }

    recovered = pipeline.recover_capture_artifacts(
        manifest=manifest, store=store, artifact_store=artifacts
    )
    assert recovered.expected_universe is None
    assert any(
        "different document" in failure or "different contract set" in failure
        for failure in recovered.failures
    ), recovered.failures


def test_a_tampered_rule_fails_recovery():
    """Editing the rule moves the evidence hash the universe names."""
    import dataclasses

    pipeline, resolution, capture, _ = documented_capture()
    manifest, store, artifacts = capture

    evidence = resolution.documentation_evidence
    edited = dataclasses.replace(
        evidence,
        rule_semantic_payload={
            **evidence.rule_semantic_payload,
            "rule_identifier": "spxw_march_20_puts",
        },
    )
    assert edited.artifact_hash != evidence.artifact_hash
    artifacts._documents[evidence.artifact_hash] = {
        "envelope_version": "capture-artifact/2.1.10",
        "kind": ArtifactKind.UNIVERSE_DOCUMENTATION_EVIDENCE,
        "source_references": [],
        "payload": edited.semantic_payload(),
    }

    recovered = pipeline.recover_capture_artifacts(
        manifest=manifest, store=store, artifact_store=artifacts
    )
    assert recovered.expected_universe is None
    assert recovered.failures


def test_a_missing_evidence_artifact_is_named_rather_than_ignored():
    pipeline, resolution, capture, _ = documented_capture()
    manifest, store, artifacts = capture

    del artifacts._documents[resolution.documentation_evidence.artifact_hash]
    recovered = pipeline.recover_capture_artifacts(
        manifest=manifest, store=store, artifact_store=artifacts
    )
    assert recovered.expected_universe is None
    assert any("not in the artifact store" in failure for failure in recovered.failures)


def test_an_out_of_period_rule_still_cannot_open_a_capture():
    """The v2.1.11 effective-period check survives the v2.1.12 rework."""
    _, resolution, capture, _ = documented_capture(effective_from=date(2030, 1, 1))
    assert not resolution.established
    assert "takes effect 2030-01-01" in resolution.failure
    assert capture is None


def test_the_production_registry_is_still_empty():
    """No document stating which SPX/SPXW contracts exist has been read. OD-11."""
    from src.adapters.universe_evidence import UNIVERSE_DOCUMENTATION_RULES

    assert UNIVERSE_DOCUMENTATION_RULES.registered_ids() == ()


@pytest.mark.parametrize("field", ["document_content_hash", "extraction_artifact_hash"])
def test_the_evidence_hash_covers_every_field_that_matters(field):
    import dataclasses

    _, resolution, _, _ = documented_capture()
    evidence = resolution.documentation_evidence
    if field == "document_content_hash":
        edited = dataclasses.replace(
            evidence,
            document_content_hash="e" * 64,
            verified_document_bytes_hash="e" * 64,
        )
    else:
        edited = dataclasses.replace(evidence, **{field: "e" * 64})
    assert edited.artifact_hash != evidence.artifact_hash
