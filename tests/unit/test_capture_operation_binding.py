"""Every non-payload input is bound to the operation that produced the chain.

v2.1.7 re-derived the chain from the raw bytes and compared the two, which
caught every *payload* mutation. It did not bind the inputs that are not in the
payload, and the sharpest of those is the instant. The rebuild did this:

    recipe = self.normalization_recipe(as_of=chain.as_of)

The chain under test chose the timestamp it was tested against, so shifting
``chain.as_of`` shifted the rebuild with it and the two agreed. Time to expiry
is measured from that instant and drives every gamma.

The same shape of gap ran through spot provenance (a caller-supplied timestamp
and tolerance), open-interest date evidence (an enum that authorized itself),
chain completeness (an open metadata key that moved the confidence score) and
record consumption (a replay that never checked it had used everything).

Every test here fails against v2.1.7.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta

import pytest

from src.adapters.capture_operation import (
    CaptureOperationIdentity,
    ValuationTimestampRule,
)
from src.adapters.thetadata.endpoints import Endpoint
from src.config.pipeline import PipelineConsistencyError
from src.domain.normalization import canonical_chain_hash
from tests.certification_fixtures import (
    AS_OF,
    captured_chain,
    resolved_pipeline,
    trusted_evidence,
)


def refuses(taken, chain, match=r"(?i)."):
    with pytest.raises(PipelineConsistencyError, match=match):
        taken.pipeline.compute_trusted_gex(chain, **trusted_evidence(taken))


# =============================================================================
# §2 -- the valuation instant comes from the capture, not from the chain
# =============================================================================


@pytest.mark.parametrize(
    "shift",
    [
        timedelta(milliseconds=100),
        timedelta(milliseconds=500),
        timedelta(seconds=1),
        timedelta(hours=1),
    ],
    ids=["0.1s", "0.5s", "1s", "1h"],
)
def test_shifting_the_chain_instant_invalidates_trust(shift):
    """The regression. All four of these were trusted against v2.1.7.

    A tenth of a second is not a rounding difference on a 0DTE afternoon -- it
    is a real change in time to expiry, and therefore in every gamma. An hour is
    a different market.
    """
    taken = captured_chain()
    refuses(
        taken,
        dataclasses.replace(taken.chain, as_of=taken.chain.as_of + shift),
        match=r"(?i)priced against|valuation",
    )


def test_the_valuation_instant_comes_from_the_index_print():
    taken = captured_chain()
    operation = taken.pipeline.resolve_operation(
        manifest=taken.manifest, store=taken.store
    )
    assert (
        operation.valuation_timestamp_rule
        is ValuationTimestampRule.INDEX_PRINT_TIMESTAMP
    )
    assert operation.effective_valuation_timestamp == taken.chain.as_of
    # And it is not simply the requested instant echoed back: the rule says the
    # value was read out of a payload, and the payload is what was read.
    assert operation.valuation_timestamp_rule.derives_from_verified_data


def test_the_rebuild_does_not_take_its_instant_from_the_chain():
    """The shape of the v2.1.7 defect, checked at the signature.

    ``rebuild_from_capture`` takes an operation. Handing it a chain would be
    handing it the thing under test.
    """
    import inspect

    from src.config.pipeline import ThetaDataResearchPipeline

    parameters = set(
        inspect.signature(ThetaDataResearchPipeline.rebuild_from_capture).parameters
    )
    assert "operation" in parameters
    assert "chain" not in parameters


# =============================================================================
# §1 -- the operation is an identity, and records belong to exactly one
# =============================================================================


def operation_for(taken, **changes):
    resolved = taken.pipeline.resolve_operation(
        manifest=taken.manifest, store=taken.store
    )
    return dataclasses.replace(resolved, **changes) if changes else resolved


def test_changing_the_requested_instant_changes_the_operation_identity():
    taken = captured_chain()
    base = operation_for(taken)
    shifted = operation_for(taken, requested_as_of=base.requested_as_of + timedelta(1))
    assert shifted.operation_fingerprint != base.operation_fingerprint


def test_changing_the_valuation_instant_changes_the_operation_identity():
    taken = captured_chain()
    base = operation_for(taken)
    shifted = operation_for(
        taken,
        effective_valuation_timestamp=base.effective_valuation_timestamp
        + timedelta(milliseconds=100),
    )
    assert shifted.operation_fingerprint != base.operation_fingerprint


def test_two_operations_in_one_session_stay_distinct():
    """A session may run several. They are not interchangeable."""
    pipeline = resolved_pipeline()
    first = pipeline.begin_operation(requested_as_of=AS_OF, session_id="s")
    second = pipeline.begin_operation(
        requested_as_of=AS_OF + timedelta(minutes=5), session_id="s"
    )
    assert first.session_id == second.session_id
    assert first.operation_id != second.operation_id
    assert first.operation_fingerprint != second.operation_fingerprint


def test_records_from_one_operation_cannot_verify_under_another():
    taken = captured_chain()
    other = captured_chain()
    mixed = dataclasses.replace(
        taken.manifest,
        records=(*taken.manifest.records, *other.manifest.records),
    )
    with pytest.raises(PipelineConsistencyError, match=r"(?i)different operations"):
        taken.pipeline.resolve_operation(manifest=mixed, store=taken.store)


def test_every_record_names_its_operation():
    taken = captured_chain()
    stamps = {r.operation_fingerprint for r in taken.store.records()}
    assert len(stamps) == 1
    assert len(stamps.pop()) == 64
    for record in taken.store.records():
        assert record.capture_identity.names_an_operation
        assert record.requested_as_of is not None
        assert record.valuation_timestamp_rule == "INDEX_PRINT_TIMESTAMP"


def test_a_capture_with_no_operation_stamp_is_refused():
    """Captures written before v2.1.8 are refused, not given a timestamp."""
    taken = captured_chain()
    stripped = dataclasses.replace(
        taken.manifest,
        records=tuple(
            dataclasses.replace(r, requested_as_of=None) for r in taken.manifest.records
        ),
    )
    with pytest.raises(PipelineConsistencyError, match=r"(?i)requested capture"):
        taken.pipeline.resolve_operation(manifest=stripped, store=taken.store)


def test_an_operation_refuses_a_naive_instant():
    with pytest.raises(ValueError, match=r"(?i)timezone-aware"):
        CaptureOperationIdentity(
            operation_id="op-1",
            session_id="s",
            pipeline_fingerprint="p",
            capture_plan_fingerprint="c",
            request_spec_fingerprint="r",
            normalization_recipe_hash="n",
            requested_as_of=datetime(2026, 3, 17, 11, 0),
            effective_valuation_timestamp=datetime(2026, 3, 17, 11, 0),
            valuation_timestamp_rule=ValuationTimestampRule.CAPTURE_REQUEST_INSTANT,
            spot_synchronization_policy_fingerprint="s",
        )


# =============================================================================
# §3 -- spot provenance is derived, never supplied
# =============================================================================


def test_the_trusted_api_takes_no_spot_provenance():
    """The regression, closed at the signature.

    v2.1.7 accepted a caller-built ``SpotProvenance``. Its ``timestamp`` and
    ``tolerance_seconds`` were the two numbers the synchronisation check
    compared, so a caller could claim 12:00 for a print the vendor stamped
    11:00, set ``chain.as_of`` to match, and be trusted.
    """
    import inspect

    from src.config.pipeline import ThetaDataResearchPipeline

    parameters = set(
        inspect.signature(ThetaDataResearchPipeline.compute_trusted_gex).parameters
    )
    assert "spot_provenance" not in parameters


def test_the_spot_timestamp_is_read_from_the_verified_record():
    taken = captured_chain()
    spot = taken.pipeline.derive_spot_provenance(
        manifest=taken.manifest, store=taken.store
    )
    observation = spot.observation
    assert observation is not None
    assert spot.timestamp == observation.source_timestamp
    assert observation.record_id in taken.manifest.record_ids
    assert observation.payload_hash


def test_the_synchronisation_tolerance_comes_from_configuration():
    """A caller cannot widen the window for one calculation."""
    taken = captured_chain()
    spot = taken.pipeline.derive_spot_provenance(
        manifest=taken.manifest, store=taken.store
    )
    assert spot.tolerance_seconds == taken.pipeline.config.max_spot_skew_seconds

    wider = resolved_pipeline(max_spot_skew_seconds=30.0)
    assert wider.fingerprint() != taken.pipeline.fingerprint()
    assert (
        wider.spot_synchronization_policy_fingerprint
        != taken.pipeline.spot_synchronization_policy_fingerprint
    )


def test_a_claimed_spot_instant_cannot_replace_the_one_the_vendor_sent():
    """The named v2.1.7 bypass: raw print 11:00, claim 12:00, chain 12:00.

    Every piece agreed with every other piece. The index snapshot in the store
    carried 11:00; the caller's ``SpotProvenance`` said 12:00; ``chain.as_of``
    said 12:00; the skew check compared the caller's number against the caller's
    number and passed. Nothing in the trusted path went back to the bytes.

    Both halves are asserted here: the derived provenance reports what the
    vendor sent, and the chain claiming otherwise is refused.
    """
    taken = captured_chain()
    an_hour_on = taken.chain.as_of + timedelta(hours=1)

    derived = taken.pipeline.derive_spot_provenance(
        manifest=taken.manifest, store=taken.store
    )
    assert derived.timestamp == taken.chain.as_of
    assert derived.timestamp != an_hour_on

    refuses(
        taken,
        dataclasses.replace(taken.chain, as_of=an_hour_on),
        match=r"(?i)priced against",
    )


def test_widening_the_tolerance_is_a_configuration_change_records_disagree_with():
    """So a capture taken under one policy cannot be verified under another."""
    from src.adapters.certification import verify_capture

    taken = captured_chain()
    wider = resolved_pipeline(max_spot_skew_seconds=30.0)
    result = verify_capture(
        taken.manifest,
        taken.store,
        plan=wider.capture_plan,
        expected_pipeline_fingerprint=wider.fingerprint(),
    )
    assert not result.verified


# =============================================================================
# §5 -- forged completeness cannot move a trusted confidence score
# =============================================================================


def test_forged_completeness_cannot_alter_a_trusted_confidence():
    """The reproduced regression: 52.0619 -> 57.3394 with trusted=True.

    The forgery has to be a better one since v2.1.10. A made-up
    ``expected_source`` string no longer moves anything -- independence is
    decided from the artifact hash and the coverage status -- so this supplies
    those too, which is what a forger would now have to do.
    """
    from src.domain.completeness import ChainCompleteness

    taken = captured_chain()
    honest = taken.pipeline.compute_trusted_gex(taken.chain, **trusted_evidence(taken))

    received = tuple(sorted(q.contract.canonical_id for q in taken.chain.quotes))
    forged = ChainCompleteness(
        received_quote_count=len(received),
        received_oi_count=len(received),
        received_iv_count=len(received),
        received_greeks_count=len(received),
        expected_contract_ids=received,
        received_contract_ids=received,
        expected_source="a_source_i_made_up",
        universe_artifact_hash="f" * 64,
        universe_evidence_fingerprint="f" * 64,
        coverage_status="FULL_REQUEST_ENUMERATED",
        resolver_version="universe-resolver/2.1.10",
    )
    tampered = dataclasses.replace(taken.chain, completeness=forged)

    # It really does move the score -- that is why it was worth closing.
    assert (
        taken.pipeline.compute_diagnostic_gex(tampered).confidence.score
        != honest.confidence.score
    )
    # And it moves the chain hash, so the re-derivation catches it.
    assert canonical_chain_hash(tampered) != canonical_chain_hash(taken.chain)
    refuses(taken, tampered, match=r"(?i)re-derived|completeness")


def test_a_made_up_source_label_is_now_inert():
    """Strictly stronger than the refusal above, and worth stating separately.

    A label cannot move the score at all any more, so the forgery has to
    fabricate a hash and a coverage state -- both of which the re-derivation
    compares.
    """
    from src.domain.completeness import ChainCompleteness

    taken = captured_chain()
    honest = taken.pipeline.compute_diagnostic_gex(taken.chain)
    received = tuple(sorted(q.contract.canonical_id for q in taken.chain.quotes))
    relabelled = dataclasses.replace(
        taken.chain,
        completeness=ChainCompleteness(
            received_quote_count=len(received),
            received_oi_count=len(received),
            received_iv_count=len(received),
            received_greeks_count=len(received),
            expected_contract_ids=received,
            received_contract_ids=received,
            expected_source="VENDOR_CONTRACT_LIST",
        ),
    )
    assert (
        taken.pipeline.compute_diagnostic_gex(relabelled).confidence.score
        == honest.confidence.score
    )


def test_completeness_is_a_typed_field_not_a_metadata_key():
    taken = captured_chain()
    assert taken.chain.completeness is not None
    assert "chain_completeness_object" not in taken.chain.meta


def test_the_completeness_payload_is_not_truncated():
    """A hash cannot sample. ``as_dict`` may; ``semantic_payload`` may not."""
    from src.domain.completeness import ChainCompleteness

    many = tuple(f"SPXW:2026-03-20:{i}:C" for i in range(150))
    completeness = ChainCompleteness(
        received_quote_count=0,
        received_oi_count=0,
        received_iv_count=0,
        received_greeks_count=0,
        expected_contract_ids=many,
        received_contract_ids=(),
        expected_source="test",
    )
    assert len(completeness.as_dict()["missing_expected_identities"]) == 100
    assert len(completeness.semantic_payload()["missing_expected_identities"]) == 150


# =============================================================================
# §7 -- replay consumes exactly the records assigned to the operation
# =============================================================================


def test_an_extra_unused_record_invalidates_replay():
    """The regression, with a real second response in the store.

    v2.1.7 replayed the first quote response, matched the original chain, and
    verified -- so a manifest could claim bytes nobody had ever parsed, sitting
    inside a capture that said they produced the number.

    A second response to the same endpoint is not hypothetical: a retained
    retry, a paginated sweep or a partitioned request all produce one. The
    capture plan has to *declare* why, and this one does not.
    """
    from src.adapters.artifact_store import InMemoryArtifactStore
    from src.adapters.raw_store import RawCaptureManifest
    from src.adapters.thetadata.endpoints import Endpoint
    from tests.certification_fixtures import (
        CAPTURED_AT,
        PAYLOADS,
        documented_settlement_rule,
        durable_store,
    )

    pipeline = resolved_pipeline()
    store = durable_store()
    artifacts = InMemoryArtifactStore()
    session = pipeline.capture_session(
        store=store,
        session_id="two-quote-responses",
        as_of=AS_OF,
        settlement_rule=documented_settlement_rule(),
        artifact_store=artifacts,
    )
    mark = session.mark()
    chain = pipeline.fetch_chain(as_of=AS_OF, capture=session)
    # One more quote response, captured honestly into the same operation and
    # never consumed by normalization.
    session.capture(
        endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT.value,
        query_params={"symbol": "SPXW", "expiration": "*"},
        payload=PAYLOADS[Endpoint.OPTION_QUOTE_SNAPSHOT],
        request_started_at=CAPTURED_AT,
        response_received_at=CAPTURED_AT,
        http_status=200,
    )
    manifest = RawCaptureManifest.from_session(
        session,
        since=mark,
        capture_plan_fingerprint=pipeline.capture_plan.fingerprint,
        pipeline_fingerprint=pipeline.fingerprint(),
    )
    assert (
        len(manifest.records) == len(chain.meta["raw_capture_manifest"]["records"]) + 1
    )

    with pytest.raises(PipelineConsistencyError, match=r"(?i)never parsed|consume"):
        pipeline.compute_trusted_gex(
            chain, manifest=manifest, store=store, artifact_store=artifacts
        )


def test_every_assigned_record_is_consumed_exactly_once():
    taken = captured_chain()
    operation = taken.pipeline.resolve_operation(
        manifest=taken.manifest, store=taken.store
    )
    recipe = taken.pipeline.normalization_recipe(
        as_of=operation.effective_valuation_timestamp
    )
    _, consumption = taken.pipeline.rebuild_from_capture(
        manifest=taken.manifest,
        store=taken.store,
        recipe=recipe,
        operation=operation,
    )
    assert consumption.exact
    assert not consumption.unconsumed
    assert not consumption.repeated
    assert set(consumption.consumed_record_ids) == set(taken.manifest.record_ids)
    assert len(consumption.consumption_hash) == 64


def test_the_consumption_report_reaches_the_receipt():
    taken = captured_chain()
    snapshot = taken.pipeline.compute_trusted_gex(
        taken.chain, **trusted_evidence(taken)
    )
    receipt = snapshot.meta["normalized_chain_receipt"]
    assert len(receipt["consumption_hash"]) == 64
    assert len(receipt["operation_fingerprint"]) == 64
    assert snapshot.meta["capture_operation"]["valuation_timestamp_rule"] == (
        "INDEX_PRINT_TIMESTAMP"
    )


# =============================================================================
# §9 -- internal trust identities are full digests
# =============================================================================


def test_every_trust_identity_is_a_full_sha256():
    from src.config.schema import load_config

    taken = captured_chain()
    loaded = load_config("config/research.yaml")
    operation = taken.pipeline.resolve_operation(
        manifest=taken.manifest, store=taken.store
    )
    for label, digest in (
        ("pipeline", taken.pipeline.fingerprint()),
        ("model", taken.pipeline.model_spec.fingerprint()),
        ("engine config", taken.pipeline.engine_config.fingerprint()),
        ("capture plan", taken.pipeline.capture_plan.fingerprint),
        ("request spec", taken.pipeline.request_spec().fingerprint),
        ("config", loaded.fingerprint),
        ("operation", operation.operation_fingerprint),
        ("spot policy", taken.pipeline.spot_synchronization_policy_fingerprint),
        ("manifest", taken.manifest.manifest_hash),
        ("chain", canonical_chain_hash(taken.chain)),
    ):
        assert len(digest) == 64, f"{label} is {len(digest)} characters: {digest}"
        assert set(digest) <= set("0123456789abcdef"), label


def test_short_id_is_available_and_is_not_what_gets_compared():
    from src.domain.digests import digest_of, short_id

    full = digest_of({"a": 1})
    assert len(full) == 64
    assert len(short_id(full)) == 16
    assert full.startswith(short_id(full))


# =============================================================================
# §6 -- an expected universe is capture-bound, and it is a *verified artifact*
# =============================================================================
#
# The verification half -- what a source actually covers, and whether a snapshot
# can stand in for a contract list -- lives in
# tests/unit/test_expected_universe_evidence.py. What is checked here is the
# binding: which universe a replay is measured against.


def universe_artifact(taken, *, identities=None, **changes):
    """A verified artifact resolved from this capture's quote response."""
    from src.adapters.thetadata.endpoints import Endpoint
    from src.adapters.universe_resolvers import resolve_expected_universe
    from src.domain.expected_universe import (
        ExpectedContractUniverse,
        ExpectedUniverseSourceKind,
    )
    from src.domain.universe_scope import UniverseRequestScope

    quote_records = taken.manifest.records_for(Endpoint.OPTION_QUOTE_SNAPSHOT.value)
    declaration = ExpectedContractUniverse(
        identities=frozenset(
            identities
            if identities is not None
            else (q.contract.canonical_id for q in taken.chain.quotes)
        ),
        source_kind=ExpectedUniverseSourceKind.OBSERVED_SNAPSHOT_ROWS,
        source_record_ids=tuple(quote_records[:1]),
        scope=UniverseRequestScope(root="SPXW", requested_at=AS_OF),
        declared_at=AS_OF,
        **changes,
    )
    outcome = resolve_expected_universe(
        declaration, manifest=taken.manifest, store=taken.store
    )
    assert outcome.established, outcome.failure
    return outcome.artifact


def test_a_verified_artifact_hashes_its_identities_and_its_coverage():
    taken = captured_chain()
    artifact = universe_artifact(taken)
    assert len(artifact.artifact_hash) == 64
    assert len(artifact.evidence_fingerprint) == 64

    # Coverage is part of the identity, so two artifacts differing only in what
    # they claim to cover are different artifacts.
    from src.domain.expected_universe import UniverseCoverageStatus

    assert artifact.coverage_status is UniverseCoverageStatus.OBSERVED_SUBSET
    relabelled = dataclasses.replace(
        artifact, coverage_status=UniverseCoverageStatus.PARTIAL_PAGE
    )
    assert relabelled.artifact_hash != artifact.artifact_hash


def test_a_universe_from_records_must_name_them():
    from src.domain.expected_universe import (
        ExpectedContractUniverse,
        ExpectedUniverseSourceKind,
    )

    with pytest.raises(ValueError, match=r"(?i)names no records"):
        ExpectedContractUniverse(
            identities=frozenset({"SPXW:2026-03-20:5000:call"}),
            source_kind=ExpectedUniverseSourceKind.OBSERVED_SNAPSHOT_ROWS,
            declared_at=AS_OF,
        )


def test_a_verified_universe_survives_replay():
    """Original and replay measure the same completeness from the same bytes."""
    from src.domain.completeness import CompletenessStatus

    taken = captured_chain()
    artifact = universe_artifact(taken)
    operation = taken.pipeline.resolve_operation(
        manifest=taken.manifest, store=taken.store
    )
    recipe = taken.pipeline.normalization_recipe(
        as_of=operation.effective_valuation_timestamp,
        open_interest_as_of=taken.settlement_artifact.resolved_settlement_date,
        expected_universe_fingerprint=artifact.artifact_hash,
    )
    rebuilt, _ = taken.pipeline.rebuild_from_capture(
        manifest=taken.manifest,
        store=taken.store,
        recipe=recipe,
        operation=operation,
        expected_universe=artifact,
    )
    assert rebuilt.completeness is not None
    assert rebuilt.completeness.universe_artifact_hash == artifact.artifact_hash
    # An observed subset, honestly reported: the rows arrived and the response
    # never claimed to be the whole request.
    assert rebuilt.completeness.status is CompletenessStatus.PARTIALLY_OBSERVED
    assert rebuilt.completeness.missing_expected_count == 0


def test_a_different_universe_produces_a_different_chain():
    """So it cannot be swapped for a quieter one between run and replay."""
    taken = captured_chain()
    operation = taken.pipeline.resolve_operation(
        manifest=taken.manifest, store=taken.store
    )

    def rebuilt_with(universe):
        recipe = taken.pipeline.normalization_recipe(
            as_of=operation.effective_valuation_timestamp,
            open_interest_as_of=taken.settlement_artifact.resolved_settlement_date,
            expected_universe_fingerprint=(
                universe.artifact_hash if universe else None
            ),
        )
        chain, _ = taken.pipeline.rebuild_from_capture(
            manifest=taken.manifest,
            store=taken.store,
            recipe=recipe,
            operation=operation,
            expected_universe=universe,
        )
        return canonical_chain_hash(chain)

    full = universe_artifact(taken)
    narrowed = dataclasses.replace(
        full, identities=frozenset(list(full.identity_set)[:1])
    )
    assert rebuilt_with(full) != rebuilt_with(narrowed)
    assert rebuilt_with(full) != rebuilt_with(None)


def test_the_universe_hash_reaches_the_receipt():
    taken = captured_chain()
    artifact = universe_artifact(taken)
    operation = taken.pipeline.resolve_operation(
        manifest=taken.manifest, store=taken.store
    )
    recipe = taken.pipeline.normalization_recipe(
        as_of=operation.effective_valuation_timestamp,
        expected_universe_fingerprint=artifact.artifact_hash,
    )
    receipt = taken.pipeline.normalized_chain_receipt(
        taken.chain,
        manifest=taken.manifest,
        recipe=recipe,
        operation=operation,
        expected_universe=artifact,
    )
    assert receipt.expected_universe_hash == artifact.artifact_hash
    assert receipt.operation_fingerprint == operation.operation_fingerprint


# =============================================================================
# §7 -- an endpoint answers twice only where the plan says why
# =============================================================================


def test_no_shipped_plan_declares_multiple_records():
    """The honest default. A snapshot plan issues one request per endpoint."""
    taken = captured_chain()
    assert taken.pipeline.capture_plan.declared_multiple_records == ()
    assert not taken.pipeline.capture_plan.permits_multiple_records(
        Endpoint.OPTION_QUOTE_SNAPSHOT.value
    )


def test_declaring_a_reason_changes_the_plan_fingerprint():
    """Whether an endpoint may answer twice decides which bytes built a chain."""
    from dataclasses import replace

    from src.adapters.thetadata.capture_plan import MultipleRecordReason

    plan = captured_chain().pipeline.capture_plan
    paginated = replace(
        plan,
        declared_multiple_records=(
            (
                Endpoint.OPTION_QUOTE_SNAPSHOT.value,
                MultipleRecordReason.PAGINATION.value,
            ),
        ),
    )
    assert paginated.fingerprint != plan.fingerprint
    assert paginated.permits_multiple_records(Endpoint.OPTION_QUOTE_SNAPSHOT.value)
    assert (
        paginated.multiple_record_reason(Endpoint.OPTION_QUOTE_SNAPSHOT.value)
        == "PAGINATION"
    )


def test_an_undeclared_second_response_is_named_as_such():
    """Not merely 'a record went unconsumed'. A different fault, a different fix."""
    from src.domain.normalization import RecordConsumptionReport

    quote = Endpoint.OPTION_QUOTE_SNAPSHOT.value
    report = RecordConsumptionReport(
        assigned_record_ids=("r1", "r2"),
        consumed_record_ids=("r1", "r2"),
        assigned_endpoints=(("r1", quote), ("r2", quote)),
    )
    assert report.undeclared_multiples == (quote,)
    assert not report.exact

    declared = dataclasses.replace(report, declared_multiples=((quote, "PAGINATION"),))
    assert declared.undeclared_multiples == ()
    assert declared.exact
