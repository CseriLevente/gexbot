"""A digest that names an object nobody kept is not evidence about anything.

v2.1.8 stamped ``open_interest_date_rule_fingerprint`` and
``expected_universe_fingerprint`` onto every record and stored neither object.
Replay therefore worked only while the caller still held the originals in
memory. A year later the digests would name artifacts nobody could produce, and
the only thing left to do with them would be to compare them against a
reconstruction -- which is the reconstruction the digest was supposed to
authenticate.
"""

from __future__ import annotations

import pytest

from src.adapters.artifact_store import (
    ARTIFACT_SCHEMA_VERSION,
    ArtifactKind,
    ArtifactStore,
    InMemoryArtifactStore,
    artifact_key,
)
from src.adapters.errors import ThetaDataProvenanceError
from tests.certification_fixtures import captured_chain


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryArtifactStore()
    return ArtifactStore(tmp_path / "artifacts")


def test_an_artifact_is_addressed_by_its_own_payload_digest(store):
    """So the digest stamped on a record is the key that looks the object up.

    Keying on the envelope instead would mean the stamped digest could not be
    used to retrieve anything, which is the entire job.
    """
    payload = {"kind": "prior_session", "calendar_id": "US_EQUITY_INDEX_OPTIONS"}
    key = store.put(ArtifactKind.SETTLEMENT_RULE, payload)
    assert key == artifact_key(payload)
    assert store.payload_of(key) == payload


def test_writing_the_same_artifact_twice_is_idempotent(store):
    payload = {"a": 1}
    first = store.put(ArtifactKind.EXPECTED_UNIVERSE, payload)
    second = store.put(ArtifactKind.EXPECTED_UNIVERSE, payload)
    assert first == second
    assert len(store) == 1


def test_two_different_artifacts_do_not_collide(store):
    a = store.put(ArtifactKind.EXPECTED_UNIVERSE, {"identities": ["A"]})
    b = store.put(ArtifactKind.EXPECTED_UNIVERSE, {"identities": ["B"]})
    assert a != b
    assert len(store) == 2


def test_an_unknown_kind_is_refused(store):
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)not an artifact kind"):
        store.put("something_nobody_agreed", {"a": 1})


def test_a_missing_artifact_reads_as_absent_rather_than_raising(store):
    assert store.get("0" * 64) is None
    assert store.payload_of("0" * 64) is None


def test_the_envelope_records_its_schema_and_sources(store):
    key = store.put(
        ArtifactKind.EXPECTED_UNIVERSE, {"identities": ["A"]}, sources=("r2", "r1")
    )
    document = store.get(key)
    assert document["envelope_version"] == ARTIFACT_SCHEMA_VERSION
    assert document["kind"] == ArtifactKind.EXPECTED_UNIVERSE
    assert document["source_references"] == ["r1", "r2"]


def test_a_tampered_file_artifact_is_refused(tmp_path):
    """The key *is* the verification: a document that does not hash to its
    filename did not come from here."""
    import json

    store = ArtifactStore(tmp_path / "artifacts")
    key = store.put(ArtifactKind.SETTLEMENT_RULE, {"kind": "prior_session"})
    target = next((tmp_path / "artifacts").rglob("*.json"))
    document = json.loads(target.read_text(encoding="utf-8"))
    document["payload"]["kind"] = "same_session"
    target.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)hashes to"):
        store.get(key)


def test_the_file_store_is_durable_and_the_memory_one_says_it_is_not():
    """The same distinction ``InMemoryRawStore`` draws, for the same reason."""
    assert ArtifactStore.durability == "DURABLE_APPEND_ONLY"
    assert InMemoryArtifactStore.durability == "TEST_ONLY_VOLATILE"


# =============================================================================
# What a capture actually writes
# =============================================================================


def test_a_capture_persists_the_artifacts_its_records_name():
    taken = captured_chain()
    stamped = {r.open_interest_date_rule_fingerprint for r in taken.manifest.records}
    assert len(stamped) == 1
    key = stamped.pop()
    assert key
    assert taken.artifacts.payload_of(key) is not None


def test_the_persisted_settlement_artifact_round_trips(tmp_path):
    """The point of storing it: the rule can be re-derived, not just named."""
    from src.adapters.artifact_store import ArtifactStore as Durable

    taken = captured_chain()
    durable = Durable(tmp_path / "artifacts")
    key = durable.put(
        ArtifactKind.SETTLEMENT_RULE, taken.settlement_artifact.semantic_payload()
    )
    assert key == taken.settlement_artifact.artifact_hash

    payload = durable.payload_of(key)
    assert payload["resolved_settlement_date"] == "2026-03-16"
    assert payload["normalized_rule"]["kind"] == "PRIOR_TRADING_SESSION"


def test_a_replay_without_the_artifact_store_cannot_recover_the_rule():
    """Which is a refusal, not a silent fall-through to 'no rule'."""
    taken = captured_chain()
    recovered = taken.pipeline.recover_capture_artifacts(
        manifest=taken.manifest, store=taken.store, artifact_store=None
    )
    assert recovered.settlement_artifact is None
    assert any("no artifact store holds it" in f for f in recovered.failures)


def test_a_replay_with_the_artifact_store_recovers_and_rechecks_it():
    taken = captured_chain()
    recovered = taken.pipeline.recover_capture_artifacts(
        manifest=taken.manifest, store=taken.store, artifact_store=taken.artifacts
    )
    assert recovered.failures == ()
    assert recovered.settlement_artifact is not None
    assert (
        recovered.settlement_artifact.artifact_hash
        == taken.settlement_artifact.artifact_hash
    )
    # Re-derived, not trusted: the artifact re-applies its own rule on
    # construction, so a recovered one that did not still produce its date
    # would have raised.
    assert (
        recovered.settlement_artifact.normalized_rule.resolve(
            recovered.settlement_artifact.chain_session_date
        )
        == recovered.settlement_artifact.resolved_settlement_date
    )
