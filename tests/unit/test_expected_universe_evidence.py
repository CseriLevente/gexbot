"""Coverage is what a resolver established, from a source somebody verified.

v2.1.9 made a universe *resolvable*. v2.1.10 made coverage *derived*: each
source kind got its own check, and no check could exceed what its evidence could
support. Both left the same shape of hole one level up, and v2.1.11 is about
that level.

* ``capture_session`` took a ``VerifiedExpectedUniverseArtifact`` and checked
  ``isinstance``. It is a public frozen dataclass, so a caller could construct
  one claiming ``AUTHORITATIVE_DOCUMENTATION`` and ``FULL_REQUEST_ENUMERATED``,
  name a documentation id nobody had registered, and be believed;
* the resolver read from any object with ``records()``, so an HTTP 500 body or a
  half-written record was evidence as long as it hashed to its own descriptor;
* the *scope* came from the declaration -- the caller's account of the request --
  so a sweep taken with ``min_time=15:30:00`` could present itself as unbounded;
* the pipeline comparison received ``self.fingerprint()`` for both sides;
* a documentation rule carried ``identities=frozenset(...)`` beside a hash of a
  real file, so the hash authenticated bytes nobody had read them out of, and
  its effective period was never checked;
* recovery compared two fields of thirteen, so a stale listing edited to look
  current recovered cleanly.

Every test here fails against v2.1.10.
"""

from __future__ import annotations

import dataclasses
import pathlib
from datetime import date, timedelta

import pytest

from src.adapters.thetadata.endpoints import Endpoint
from src.adapters.universe_resolvers import (
    check_source_compatibility,
    verified_universe_source,
)
from src.config.pipeline import PipelineConsistencyError, UniverseResolution
from src.domain.completeness import ChainCompleteness, CompletenessStatus
from src.domain.expected_universe import (
    ExpectedContractUniverse,
    ExpectedUniverseSourceKind,
    UniverseCoverageStatus,
)
from src.domain.universe_artifact import (
    UNIVERSE_RESOLVER_SCHEMA_VERSION,
    UniverseArtifactError,
    VerifiedExpectedUniverseArtifact,
)
from src.domain.universe_scope import UniverseRequestScope
from tests.certification_fixtures import AS_OF, captured_chain, trusted_evidence

FIXTURE_IDENTITIES = frozenset(
    {
        "SPXW:2026-03-20:4990:call",
        "SPXW:2026-03-20:5000:call",
        "SPXW:2026-03-20:5010:call",
    }
)

#: A fixture document containing a machine-readable universe block. Real bytes
#: in this repository, so the content hash is a hash of something.
UNIVERSE_DOCUMENT = "tests/fixtures/universe_listing.md"

#: The rule name inside that document whose block lists the fixture contracts.
LADDER_RULE = "spxw_march_20_ladder"

CONTRACT_TABLE = "contract-table/2.1.11"


def scope(**changes):
    """A scope mirroring the fixture pipeline's actual chain request.

    That request sends ``expiration="*"`` with no DTE, strike or time filter, so
    the unbounded form is what a listing for it carries. The parametrised
    incompatibility tests narrow it deliberately.
    """
    payload = {
        "root": "SPXW",
        "max_dte": None,
        "strike_range": None,
        "rights": ("call", "put"),
        "requested_at": AS_OF,
    }
    payload.update(changes)
    return UniverseRequestScope(**payload)


def quote_record(taken):
    return taken.manifest.records_for(Endpoint.OPTION_QUOTE_SNAPSHOT.value)[0]


def index_record(taken):
    return taken.manifest.records_for(Endpoint.INDEX_PRICE_SNAPSHOT.value)[0]


def declared(taken, *, identities=None, record_ids=None, **changes):
    payload = {
        "identities": frozenset(
            identities
            if identities is not None
            else (q.contract.canonical_id for q in taken.chain.quotes)
        ),
        "source_kind": ExpectedUniverseSourceKind.OBSERVED_SNAPSHOT_ROWS,
        "source_record_ids": tuple(
            record_ids if record_ids is not None else (quote_record(taken),)
        ),
        "scope": scope(),
        "declared_at": AS_OF,
    }
    payload.update(changes)
    return ExpectedContractUniverse(**payload)


def resolution(taken, *, declaration=None, **changes):
    """Resolve through the pipeline, which is the only supported route."""
    return taken.pipeline.resolve_expected_universe(
        declaration=(
            declaration if declaration is not None else declared(taken, **changes)
        ),
        source_manifest=taken.manifest,
        source_store=taken.store,
        as_of=AS_OF,
    )


def resolved(taken, **changes):
    outcome = resolution(taken, **changes)
    assert outcome.established, outcome.failure
    return outcome.artifact


def universe_rule(**changes):
    """A registered rule pointing at the fixture universe document."""
    from src.adapters.evidence_resolvers import content_hash_of
    from src.adapters.universe_evidence import (
        UniverseDocumentationRegistry,
        UniverseDocumentationRule,
    )

    payload = {
        "evidence_id": "listed-universe",
        "document_reference": UNIVERSE_DOCUMENT,
        "document_content_hash": content_hash_of(pathlib.Path(UNIVERSE_DOCUMENT)),
        "rule_identifier": LADDER_RULE,
        "scope": scope(),
        "extractor_version": CONTRACT_TABLE,
        "effective_from": date(2020, 1, 1),
    }
    payload.update(changes)
    registry = UniverseDocumentationRegistry()
    registry.register(UniverseDocumentationRule(**payload), verified_at=AS_OF)
    return registry


def documented(taken, *, registry, session_date=None, identities=None, **changes):
    return taken.pipeline.resolve_expected_universe(
        declaration=declared(
            taken,
            identities=identities if identities is not None else FIXTURE_IDENTITIES,
            record_ids=(),
            source_kind=ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION,
            documentation_evidence_id="listed-universe",
            **changes,
        ),
        registry=registry,
        session_date=session_date if session_date is not None else date(2026, 3, 17),
    )


# =============================================================================
# §1 -- a constructible dataclass is not authority
# =============================================================================


def test_a_hand_built_artifact_cannot_authorize_completeness():
    """The v2.1.11 headline regression.

    Everything here is what a caller can type: a source kind that *could* reach
    full coverage, an evidence id nobody registered, a fingerprint of nothing.
    v2.1.10 checked ``isinstance`` and opened the capture.
    """
    from tests.certification_fixtures import durable_store, resolved_pipeline

    forged = VerifiedExpectedUniverseArtifact(
        identities=FIXTURE_IDENTITIES,
        source_kind=ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION,
        coverage_status=UniverseCoverageStatus.FULL_REQUEST_ENUMERATED,
        source_operation_fingerprint="",
        source_record_ids=(),
        source_request_spec_fingerprint="",
        source_pipeline_fingerprint="",
        source_scope=scope(),
        observed_at=AS_OF,
        evidence_fingerprint="f" * 64,
        documentation_evidence_id="a-document-nobody-registered",
    )
    # It constructs -- it is a report type -- and it establishes nothing.
    assert forged.establishes_completeness

    pipeline = resolved_pipeline()
    with pytest.raises(PipelineConsistencyError, match=r"(?i)of a resolution rather"):
        pipeline.capture_session(
            store=durable_store(),
            session_id="forged",
            as_of=AS_OF,
            universe_resolution=forged,
        )


def test_wrapping_a_forged_artifact_in_a_resolution_is_re_run_and_refused():
    """The next thing a caller would try: build the receipt around it too."""
    from tests.certification_fixtures import durable_store, resolved_pipeline

    taken = captured_chain()
    honest = resolved(taken)
    forged = dataclasses.replace(
        honest,
        coverage_status=UniverseCoverageStatus.FULL_REQUEST_ENUMERATED,
        source_kind=ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION,
    )
    receipt = UniverseResolution(
        declaration=declared(taken),
        artifact=forged,
        source_manifest=taken.manifest,
        source_store=taken.store,
        source_verification=taken.pipeline.verify_source_capture(
            manifest=taken.manifest, store=taken.store
        ),
    )
    pipeline = resolved_pipeline()
    with pytest.raises(PipelineConsistencyError, match=r"(?i)different artifact"):
        pipeline.capture_session(
            store=durable_store(),
            session_id="forged-receipt",
            as_of=AS_OF,
            universe_resolution=receipt,
        )


def test_a_declaration_is_still_refused_where_a_resolution_belongs():
    from tests.certification_fixtures import durable_store, resolved_pipeline

    taken = captured_chain()
    pipeline = resolved_pipeline()
    with pytest.raises(PipelineConsistencyError, match=r"(?i)is a declaration"):
        pipeline.capture_session(
            store=durable_store(),
            session_id="chain",
            as_of=AS_OF,
            universe_resolution=declared(taken),
        )


def test_an_unestablished_resolution_is_refused():
    from tests.certification_fixtures import durable_store, resolved_pipeline

    taken = captured_chain()
    outcome = resolution(taken, identities={"SPXW:2026-03-20:9999:call"})
    assert not outcome.established
    with pytest.raises(PipelineConsistencyError, match=r"(?i)established nothing"):
        resolved_pipeline().capture_session(
            store=durable_store(),
            session_id="nothing",
            as_of=AS_OF,
            universe_resolution=outcome,
        )


# =============================================================================
# §2 -- the source has to be a capture that verified
# =============================================================================


class _ClaimedVerification:
    """A verification object a caller could hand over, saying everything passed.

    Used to reach the per-record checks. Without it the HTTP-500 record is
    already refused by ``verify_capture``, and the point of the record checks is
    that they do not depend on that.
    """

    def __init__(self, record_ids):
        self.verified = True
        self.confirmed_record_ids = tuple(record_ids)
        self.failures = ()
        self.manifest_hash = "m" * 64
        self.plan_fingerprint = "p" * 64
        self.expected_pipeline_fingerprint = "x" * 64
        self.store_description = "claimed"


def capture_with_extra_record(**record):
    """A listing session holding one deliberately defective response."""
    from src.adapters.raw_store import RawCaptureManifest
    from tests.certification_fixtures import (
        CAPTURED_AT,
        PAYLOADS,
        durable_store,
        resolved_pipeline,
    )

    pipeline = resolved_pipeline()
    store = durable_store()
    session = pipeline.capture_session(store=store, session_id="listing", as_of=AS_OF)
    session.capture(
        endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT.value,
        query_params={"symbol": "SPXW", "expiration": "*"},
        payload=PAYLOADS[Endpoint.OPTION_QUOTE_SNAPSHOT],
        request_started_at=CAPTURED_AT,
        response_received_at=CAPTURED_AT,
        **{"http_status": 200, **record},
    )
    manifest = RawCaptureManifest.from_session(
        session,
        since=0,
        capture_plan_fingerprint=pipeline.capture_plan.fingerprint,
        pipeline_fingerprint=pipeline.fingerprint(),
    )
    return pipeline, store, manifest, session.captured[-1].record_id


def test_an_http_500_response_cannot_become_verified_evidence():
    """The named regression. Rows in an error body are still rows."""
    pipeline, store, manifest, record_id = capture_with_extra_record(http_status=500)
    declaration = ExpectedContractUniverse(
        identities=FIXTURE_IDENTITIES,
        source_kind=ExpectedUniverseSourceKind.OBSERVED_SNAPSHOT_ROWS,
        source_record_ids=(record_id,),
        scope=scope(),
        declared_at=AS_OF,
    )
    outcome = verified_universe_source(
        declaration,
        manifest=manifest,
        store=store,
        verification=_ClaimedVerification([record_id]),
    )
    assert not getattr(outcome, "established", False)
    assert "HTTP 500" in outcome.failure

    # And through the pipeline, where verification runs for real.
    through = pipeline.resolve_expected_universe(
        declaration=declaration, source_manifest=manifest, source_store=store
    )
    assert not through.established
    assert "did not pass verification" in through.failure


def test_a_source_capture_must_pass_capture_verification():
    """The named regression: existing in a store is not having been verified."""
    taken = captured_chain()
    outcome = verified_universe_source(
        declared(taken), manifest=taken.manifest, store=taken.store, verification=None
    )
    assert "no capture verification" in outcome.failure


def test_a_record_outside_the_verified_manifest_is_refused():
    taken = captured_chain()
    outcome = verified_universe_source(
        declared(taken),
        manifest=taken.manifest,
        store=taken.store,
        verification=_ClaimedVerification(["some-other-record"]),
    )
    assert "did not confirm" in outcome.failure


class _StoreWithOneEditedRecord:
    """The real store, with one descriptor changed.

    ``capture_complete`` is set by the store at write time, so an interrupted
    write cannot be produced by calling ``capture()``. What matters is that the
    resolver reads the flag, and this puts a record carrying it in front of the
    resolver.
    """

    def __init__(self, store, record_id, **changes):
        self._store = store
        self._records = tuple(
            dataclasses.replace(record, **changes)
            if record.record_id == record_id
            else record
            for record in store.records()
        )

    def records(self):
        return self._records

    def get_payload(self, record_id):
        return self._store.get_payload(record_id)


def test_an_incomplete_write_cannot_become_verified_evidence():
    _, store, manifest, record_id = capture_with_extra_record()
    outcome = verified_universe_source(
        ExpectedContractUniverse(
            identities=FIXTURE_IDENTITIES,
            source_kind=ExpectedUniverseSourceKind.OBSERVED_SNAPSHOT_ROWS,
            source_record_ids=(record_id,),
            scope=scope(),
            declared_at=AS_OF,
        ),
        manifest=manifest,
        store=_StoreWithOneEditedRecord(store, record_id, capture_complete=False),
        verification=_ClaimedVerification([record_id]),
    )
    assert "marked incomplete" in outcome.failure


def test_a_record_read_by_an_unsupported_parser_is_refused():
    _, store, manifest, record_id = capture_with_extra_record()
    outcome = verified_universe_source(
        ExpectedContractUniverse(
            identities=FIXTURE_IDENTITIES,
            source_kind=ExpectedUniverseSourceKind.OBSERVED_SNAPSHOT_ROWS,
            source_record_ids=(record_id,),
            scope=scope(),
            declared_at=AS_OF,
        ),
        manifest=manifest,
        store=_StoreWithOneEditedRecord(
            store, record_id, parser_version="thetadata-v3-parser/1.0"
        ),
        verification=_ClaimedVerification([record_id]),
    )
    assert "different rules" in outcome.failure


def test_a_record_backed_artifact_must_name_its_verification():
    """Closed at the type, so no resolver path can omit it."""
    with pytest.raises(UniverseArtifactError, match=r"(?i)no capture verification"):
        VerifiedExpectedUniverseArtifact(
            identities=FIXTURE_IDENTITIES,
            source_kind=ExpectedUniverseSourceKind.OBSERVED_SNAPSHOT_ROWS,
            coverage_status=UniverseCoverageStatus.OBSERVED_SUBSET,
            source_operation_fingerprint="op",
            source_record_ids=("r1",),
            source_request_spec_fingerprint="spec",
            source_pipeline_fingerprint="pipe",
            source_scope=scope(),
            observed_at=AS_OF,
            evidence_fingerprint="e" * 64,
        )


# =============================================================================
# §3 -- the source pipeline is derived and compared
# =============================================================================


def test_the_artifact_records_the_pipeline_that_captured_its_source():
    taken = captured_chain()
    artifact = resolved(taken)
    assert artifact.source_pipeline_fingerprint == taken.pipeline.fingerprint()
    assert len(artifact.source_verification_fingerprint) == 64


def test_source_and_target_pipeline_fingerprints_are_compared():
    """The named regression. v2.1.10 passed the same string for both sides."""
    taken = captured_chain()
    artifact = dataclasses.replace(
        resolved(taken), source_pipeline_fingerprint="b" * 64
    )
    reasons = check_source_compatibility(
        artifact,
        chain_scope=scope(),
        chain_requested_at=AS_OF,
        chain_pipeline_fingerprint="a" * 64,
    )
    assert any("IDENTICAL_PIPELINE" in reason for reason in reasons)


def waived(taken, *, source_configuration, target_configuration, approved):
    """Run the compatibility check under a documented waiver."""
    from src.adapters.universe_resolvers import (
        PipelineCompatibilityPolicy,
        UniverseOnlyCompatibilityRule,
    )

    captured_at = record_instant(taken)
    artifact = dataclasses.replace(
        resolved(taken),
        source_pipeline_fingerprint="b" * 64,
        source_scope=scope(requested_at=captured_at),
    )
    waiver = UniverseOnlyCompatibilityRule(
        rule_id="ur-1",
        source_pipeline_fingerprint="b" * 64,
        target_pipeline_fingerprint="a" * 64,
        approved_diff_hash=approved,
        rationale="a longer timeout does not change which contracts come back",
    )
    return check_source_compatibility(
        artifact,
        chain_scope=scope(),
        chain_requested_at=captured_at,
        chain_pipeline_fingerprint="a" * 64,
        policy=PipelineCompatibilityPolicy.UNIVERSE_ONLY_DOCUMENTED,
        waiver=waiver,
        source_configuration=source_configuration,
        target_configuration=target_configuration,
    )


def test_a_min_time_difference_cannot_be_waived():
    """The named v2.1.12 regression.

    v2.1.11 took ``differing_parameters`` from the caller and checked only the
    names it was given, so a waiver claiming the two pipelines differ in
    ``timeout_seconds`` was accepted while the real difference was ``min_time``
    -- which decides which contracts come back.
    """
    import inspect

    from src.adapters.universe_resolvers import (
        UniverseOnlyCompatibilityRule,
        derive_parameter_diff,
        diff_fingerprint,
    )

    # The caller cannot state the difference at all any more.
    parameters = set(inspect.signature(UniverseOnlyCompatibilityRule).parameters)
    assert "differing_parameters" not in parameters
    assert "approved_diff_hash" in parameters

    source = {"config": {"timeout_seconds": 60.0, "min_time": "15:30:00"}}
    target = {"config": {"timeout_seconds": 30.0, "min_time": None}}
    diff = derive_parameter_diff(source, target)
    assert {entry.key for entry in diff} == {
        "config.timeout_seconds",
        "config.min_time",
    }

    taken = captured_chain()
    # The waiver approves the *whole* derived difference and is still refused,
    # because part of that difference decides the contract set.
    reasons = waived(
        taken,
        source_configuration=source,
        target_configuration=target,
        approved=diff_fingerprint(diff),
    )
    assert any("which contracts a request returns" in reason for reason in reasons)
    assert any("min_time" in reason for reason in reasons)


def test_a_waiver_for_a_different_difference_is_refused():
    from src.adapters.universe_resolvers import derive_parameter_diff, diff_fingerprint

    taken = captured_chain()
    source = {"config": {"timeout_seconds": 60.0}}
    target = {"config": {"timeout_seconds": 30.0}}
    elsewhere = derive_parameter_diff({"config": {"a": 1}}, {"config": {"a": 2}})
    reasons = waived(
        taken,
        source_configuration=source,
        target_configuration=target,
        approved=diff_fingerprint(elsewhere),
    )
    assert any("approves difference" in reason for reason in reasons)


def test_a_waiver_without_the_two_configurations_is_refused():
    taken = captured_chain()
    reasons = waived(
        taken,
        source_configuration=None,
        target_configuration=None,
        approved="d" * 64,
    )
    assert any("never computed" in reason for reason in reasons)


def test_a_documented_waiver_permits_a_derived_difference():
    from src.adapters.universe_resolvers import derive_parameter_diff, diff_fingerprint

    taken = captured_chain()
    source = {"config": {"timeout_seconds": 60.0}}
    target = {"config": {"timeout_seconds": 30.0}}
    diff = derive_parameter_diff(source, target)
    assert not any(entry.affects_contract_set for entry in diff)
    assert (
        waived(
            taken,
            source_configuration=source,
            target_configuration=target,
            approved=diff_fingerprint(diff),
        )
        == ()
    )


# =============================================================================
# §4 -- the scope is reconstructed from the stored request
# =============================================================================


def test_the_source_scope_is_derived_from_the_stored_query_parameters():
    taken = captured_chain()
    artifact = resolved(taken)
    # The fixture request sends expiration="*" with no narrowing filters, and
    # that is what comes back out of the records.
    assert artifact.source_scope.root == "SPXW"
    assert artifact.source_scope.expirations_unbounded
    assert artifact.source_scope.request_filters == ()


def test_min_time_is_read_back_out_of_the_source_request():
    """The named regression.

    A sweep taken with ``min_time=15:30:00`` contains the contracts that traded
    after 15:30 -- a smaller set than the same request without it, and one that
    re-derives perfectly. v2.1.10 carried the declaration's scope, so the filter
    never reached the comparison.
    """
    from src.adapters.universe_resolvers import derive_source_scope

    _, store, _, record_id = capture_with_extra_record()
    record = next(r for r in store.records() if r.record_id == record_id)
    derived = derive_source_scope(
        {
            record_id: dataclasses.replace(
                record,
                query_params={
                    "symbol": "SPXW",
                    "expiration": "*",
                    "min_time": "15:30:00",
                },
            )
        }
    )
    assert isinstance(derived, UniverseRequestScope)
    assert ("min_time", "15:30:00") in derived.request_filters
    # And it cannot serve a chain that asked without the filter.
    assert not derived.covers(scope()).compatible


def test_a_declaration_cannot_widen_the_source_scope():
    """The named regression, through the resolver."""
    taken = captured_chain()
    outcome = resolution(taken, scope=scope(root="SPY"))
    assert not outcome.established
    assert "wider request than the records" in outcome.failure


def test_the_chain_scope_names_every_contract_set_parameter():
    """So a filter on the chain side is compared rather than assumed absent."""
    from src.adapters.universe_resolvers import CONTRACT_SET_PARAMETERS
    from tests.certification_fixtures import resolved_pipeline

    chain_scope = resolved_pipeline().request_scope(requested_at=AS_OF)
    assert chain_scope.root == "SPXW"
    assert "min_time" in CONTRACT_SET_PARAMETERS
    assert "strike" in CONTRACT_SET_PARAMETERS


# =============================================================================
# §3 (endpoints) -- a market-data snapshot is not a contract list
# =============================================================================


@pytest.mark.parametrize(
    "endpoint",
    [
        Endpoint.OPTION_QUOTE_SNAPSHOT,
        Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT,
        Endpoint.OPTION_GREEKS_FIRST_ORDER,
        Endpoint.OPTION_GREEKS_SECOND_ORDER,
        Endpoint.OPTION_GREEKS_ALL,
    ],
)
def test_no_snapshot_can_act_as_a_vendor_contract_list(endpoint):
    from src.adapters.thetadata.endpoints import capabilities_of

    capability = capabilities_of(endpoint.value)
    assert capability.enumerates_rows
    assert not capability.is_dedicated_contract_list
    assert not capability.enumerates_request_universe
    assert not capability.can_establish_full_coverage


def test_a_quote_snapshot_labelled_a_contract_list_is_refused():
    taken = captured_chain()
    outcome = resolution(
        taken, source_kind=ExpectedUniverseSourceKind.VENDOR_CONTRACT_LIST
    )
    assert not outcome.established
    assert "not dedicated contract-list endpoints" in outcome.failure
    assert outcome.coverage_status is UniverseCoverageStatus.UNKNOWN_COVERAGE


def test_the_same_records_resolve_honestly_as_an_observed_subset():
    taken = captured_chain()
    artifact = resolved(taken)
    assert artifact.coverage_status is UniverseCoverageStatus.OBSERVED_SUBSET
    assert not artifact.establishes_completeness
    assert artifact.identity_set == FIXTURE_IDENTITIES


def test_an_artifact_cannot_claim_more_than_its_source_supports():
    with pytest.raises(UniverseArtifactError, match=r"(?i)cannot establish"):
        VerifiedExpectedUniverseArtifact(
            identities=FIXTURE_IDENTITIES,
            source_kind=ExpectedUniverseSourceKind.OBSERVED_SNAPSHOT_ROWS,
            coverage_status=UniverseCoverageStatus.FULL_REQUEST_ENUMERATED,
            source_operation_fingerprint="op",
            source_record_ids=("r1",),
            source_request_spec_fingerprint="spec",
            source_pipeline_fingerprint="pipe",
            source_scope=scope(),
            observed_at=AS_OF,
            evidence_fingerprint="e" * 64,
        )


def test_an_index_print_enumerates_nothing():
    taken = captured_chain()
    outcome = resolution(taken, record_ids=(index_record(taken),))
    assert not outcome.established
    assert "reconstructed" in outcome.failure or "enumerate" in outcome.failure


# =============================================================================
# §11 -- pagination coverage is read, and read strictly
# =============================================================================


def test_an_ordinary_quote_response_cannot_satisfy_pagination_evidence():
    taken = captured_chain()
    outcome = resolution(
        taken, source_kind=ExpectedUniverseSourceKind.CAPTURED_PAGINATION_METADATA
    )
    assert not outcome.established
    assert "return no pagination metadata" in outcome.failure


def test_pagination_evidence_is_built_from_the_bytes():
    from src.adapters.universe_evidence import read_pagination_metadata

    page_one = "page,total_pages,next_page_token,symbol\n1,2,abc,SPXW\n"
    page_two = "page,total_pages,next_page_token,symbol\n2,2,,SPXW\n"
    evidence = read_pagination_metadata({"r1": page_one, "r2": page_two})
    assert evidence is not None
    assert evidence.total_pages == 2
    assert evidence.captured_pages == frozenset({1, 2})
    assert evidence.continuation_complete
    assert evidence.complete


def test_one_page_of_two_is_not_complete():
    from src.adapters.universe_evidence import read_pagination_metadata

    evidence = read_pagination_metadata(
        {"r1": "page,total_pages,next_page_token\n1,2,\n"}
    )
    assert evidence is not None
    assert evidence.missing_pages == (2,)
    assert not evidence.complete


def test_two_responses_claiming_the_same_page_are_refused():
    """v2.1.10 put them in a set, so one page counted as two."""
    from src.adapters.errors import ThetaDataProvenanceError
    from src.adapters.universe_evidence import read_pagination_metadata

    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)both claim to be page"):
        read_pagination_metadata(
            {
                "r1": "page,total_pages,next_page_token\n1,2,abc\n",
                "r2": "page,total_pages,next_page_token\n1,2,\n",
            }
        )


def test_disagreeing_totals_are_refused_rather_than_discarded():
    """v2.1.10 dropped ``total_results`` when the pages disagreed."""
    from src.adapters.errors import ThetaDataProvenanceError
    from src.adapters.universe_evidence import read_pagination_metadata

    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)disagree about total_res"):
        read_pagination_metadata(
            {
                "r1": "page,total_pages,total_results,next_page_token\n1,2,10,abc\n",
                "r2": "page,total_pages,total_results,next_page_token\n2,2,11,\n",
            }
        )


def test_more_than_one_terminal_page_is_refused():
    from src.adapters.errors import ThetaDataProvenanceError
    from src.adapters.universe_evidence import read_pagination_metadata

    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)carry no continuation"):
        read_pagination_metadata(
            {
                "r1": "page,total_pages,next_page_token\n1,2,\n",
                "r2": "page,total_pages,next_page_token\n2,2,\n",
            }
        )


def test_a_missing_continuation_prevents_completeness():
    from src.adapters.errors import ThetaDataProvenanceError
    from src.adapters.universe_evidence import read_pagination_metadata

    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)carry no continuation"):
        read_pagination_metadata(
            {
                "r1": "page,total_pages,next_page_token\n1,2,abc\n",
                "r2": "page,total_pages,next_page_token\n2,2,def\n",
            }
        )


def test_the_identity_count_must_match_total_results():
    from src.adapters.universe_evidence import PaginationCoverageEvidence

    evidence = PaginationCoverageEvidence(
        total_pages=1,
        captured_pages=frozenset({1}),
        source_record_ids=("r1",),
        total_results=250,
        continuation_complete=True,
    )
    assert evidence.complete
    assert evidence.identity_count_refusals(250) == ()
    assert "does not contain the number" in evidence.identity_count_refusals(3)[0]
    # And a sweep with no stated total cannot reach full coverage at all.
    without = dataclasses.replace(evidence, total_results=None)
    assert "no total_results" in without.identity_count_refusals(3)[0]


def test_overlapping_partitions_are_refused_unless_allowed():
    from src.adapters.errors import ThetaDataProvenanceError
    from src.adapters.universe_evidence import PaginationCoverageEvidence

    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)same fingerprint"):
        PaginationCoverageEvidence(
            total_pages=1,
            captured_pages=frozenset({1}),
            source_record_ids=("r1",),
            partition_fingerprints=("a", "a"),
        )


def test_a_response_with_no_pagination_columns_yields_no_evidence():
    from src.adapters.universe_evidence import read_pagination_metadata

    assert read_pagination_metadata({"r1": "symbol,strike\nSPXW,5000\n"}) is None


def test_partial_pagination_metadata_is_refused():
    from src.adapters.errors import ThetaDataProvenanceError
    from src.adapters.universe_evidence import read_pagination_metadata

    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)partial pagination"):
        read_pagination_metadata({"r1": "page,symbol\n1,SPXW\n"})


# =============================================================================
# §5/§6/§7 -- documentation identities are extracted, dated and time-bounded
# =============================================================================


def test_a_settlement_rule_cannot_establish_a_universe():
    """``fixture-oi-settlement-convention`` is a real, content-verified document
    about when open interest settles. It says nothing about which options exist.
    """
    from tests.certification_fixtures import (
        FIXTURE_OI_EVIDENCE_ID,
        register_fixture_documentation_rule,
    )

    register_fixture_documentation_rule()
    taken = captured_chain()
    outcome = taken.pipeline.resolve_expected_universe(
        declaration=declared(
            taken,
            record_ids=(),
            source_kind=ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION,
            documentation_evidence_id=FIXTURE_OI_EVIDENCE_ID,
        ),
        session_date=date(2026, 3, 17),
    )
    assert not outcome.established
    assert "not a registered *universe* documentation rule" in outcome.failure


def test_the_production_universe_registry_is_empty():
    """No document stating which SPX/SPXW contracts exist has been read. OD-11."""
    from src.adapters.universe_evidence import UNIVERSE_DOCUMENTATION_RULES

    assert UNIVERSE_DOCUMENTATION_RULES.registered_ids() == ()


def test_a_rule_cannot_carry_a_caller_supplied_identity_list():
    """The named regression. A hash proves which bytes; it does not source a list."""
    import inspect

    from src.adapters.universe_evidence import UniverseDocumentationRule

    parameters = set(inspect.signature(UniverseDocumentationRule).parameters)
    assert "identities" not in parameters
    assert "derivation" not in parameters
    assert "extractor_version" in parameters


def test_an_unrelated_content_hashed_document_establishes_no_identities():
    """The named regression.

    The settlement-convention document is genuine, and it contains no
    universe-rule block, so the extractor has nothing to read.
    """
    from src.adapters.errors import ThetaDataProvenanceError
    from tests.certification_fixtures import FIXTURE_DOCUMENT

    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)no <!-- universe-rule"):
        universe_rule(
            document_reference=FIXTURE_DOCUMENT,
            document_content_hash=__import__(
                "src.adapters.evidence_resolvers", fromlist=["content_hash_of"]
            ).content_hash_of(pathlib.Path(FIXTURE_DOCUMENT)),
        ).get("listed-universe").extract(executed_at=AS_OF)


def test_a_registered_universe_rule_extracts_its_identities_from_the_bytes():
    """The path through the gate, and it reads rather than asserts."""
    taken = captured_chain()
    registry = universe_rule()
    outcome = documented(taken, registry=registry)
    assert outcome.established, outcome.failure
    assert outcome.artifact.coverage_status is (
        UniverseCoverageStatus.FULL_REQUEST_ENUMERATED
    )
    extraction = outcome.extraction
    assert extraction.identities == FIXTURE_IDENTITIES
    assert extraction.extractor_version == CONTRACT_TABLE
    # And it can point at the characters the identities came from.
    start, end = extraction.source_ranges[0]
    text = pathlib.Path(UNIVERSE_DOCUMENT).read_text(encoding="utf-8")
    assert "SPXW,2026-03-20,5000,C" in text[start:end]


def test_naming_a_different_rule_extracts_a_different_contract_set():
    taken = captured_chain()
    registry = universe_rule(rule_identifier="spxw_march_20_puts")
    outcome = documented(taken, registry=registry)
    assert not outcome.established
    assert "has not established them" in outcome.failure


def test_a_future_documentation_rule_cannot_establish_a_2026_universe():
    """The named regression. v2.1.10 had ``covers()`` and never called it."""
    taken = captured_chain()
    registry = universe_rule(effective_from=date(2030, 1, 1))
    outcome = documented(taken, registry=registry, session_date=date(2026, 3, 17))
    assert not outcome.established
    assert "takes effect 2030-01-01" in outcome.failure


def test_an_expired_documentation_rule_cannot_establish_a_universe():
    taken = captured_chain()
    registry = universe_rule(
        effective_from=date(2020, 1, 1), effective_to=date(2021, 12, 31)
    )
    outcome = documented(taken, registry=registry, session_date=date(2026, 3, 17))
    assert not outcome.established
    assert "expired 2021-12-31" in outcome.failure


def test_a_rule_with_no_effective_period_establishes_nothing():
    taken = captured_chain()
    registry = universe_rule(effective_from=None)
    outcome = documented(taken, registry=registry)
    assert not outcome.established
    assert "states no effective date" in outcome.failure


def test_the_observation_time_is_the_extraction_not_the_declaration():
    """The named regression. v2.1.10 used ``universe.declared_at``."""
    taken = captured_chain()
    registry = universe_rule()
    long_ago = AS_OF - timedelta(days=900)
    outcome = documented(taken, registry=registry, declared_at=long_ago)
    assert outcome.established, outcome.failure
    assert outcome.artifact.observed_at != long_ago
    assert outcome.artifact.observed_at == outcome.extraction.extraction_executed_at


def test_a_document_edited_after_registration_is_refused(tmp_path):
    from src.adapters.evidence_resolvers import content_hash_of

    copy = tmp_path / "universe.md"
    original = pathlib.Path(UNIVERSE_DOCUMENT).read_text(encoding="utf-8")
    copy.write_text(original, encoding="utf-8")
    # Registered against a document inside the repository, then the bytes move.
    registry = universe_rule()
    rule = registry.get("listed-universe")
    edited = dataclasses.replace(
        rule, document_content_hash=content_hash_of(copy).replace("a", "b", 1)
    )
    assert edited.document_content_hash != content_hash_of(
        pathlib.Path(UNIVERSE_DOCUMENT)
    )
    from src.adapters.errors import ThetaDataProvenanceError

    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)bytes changed"):
        edited.extract(executed_at=AS_OF)


# =============================================================================
# §6/§7 (records) -- source records, and timing
# =============================================================================


def test_a_fake_record_id_fails_verification():
    taken = captured_chain()
    outcome = resolution(taken, record_ids=("fake-record",))
    assert not outcome.established
    assert "did not confirm" in outcome.failure


def test_a_universe_its_records_do_not_produce_fails():
    taken = captured_chain()
    outcome = resolution(taken, identities={"SPXW:2026-03-20:9999:call"})
    assert not outcome.established
    assert "claimed but not present" in outcome.failure


def test_a_universe_with_no_capture_cannot_be_verified():
    taken = captured_chain()
    outcome = taken.pipeline.resolve_expected_universe(declaration=declared(taken))
    assert not outcome.established
    assert "never opened" in outcome.failure


def record_instant(taken):
    """When the source record was actually received."""
    named = {quote_record(taken)}
    return max(
        r.response_received_at for r in taken.manifest.records if r.record_id in named
    )


def test_observed_at_is_derived_from_the_source_records():
    """v2.1.9 took it from the declaration, so a listing captured three weeks
    ago could present itself as observed this morning."""
    taken = captured_chain()
    artifact = resolved(taken, declared_at=AS_OF - timedelta(days=90))
    assert artifact.observed_at == record_instant(taken)


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"root": "SPY"}, "enumerated SPY"),
        ({"max_dte": 5}, "5 days to expiry"),
        ({"strike_range": 10}, "10-point window"),
        ({"rights": ("call",)}, "never listed"),
    ],
)
def test_an_incompatible_source_scope_is_refused(change, fragment):
    """Identities matching is not reassurance when the scopes differ."""
    taken = captured_chain()
    artifact = dataclasses.replace(resolved(taken), source_scope=scope(**change))
    reasons = check_source_compatibility(
        artifact,
        chain_scope=scope(),
        chain_requested_at=AS_OF,
        chain_pipeline_fingerprint=artifact.source_pipeline_fingerprint,
    )
    assert reasons
    assert any(fragment in reason for reason in reasons)


def compatibility(artifact, *, chain_requested_at, **changes):
    return check_source_compatibility(
        artifact,
        chain_scope=scope(),
        chain_requested_at=chain_requested_at,
        chain_pipeline_fingerprint=artifact.source_pipeline_fingerprint,
        **changes,
    )


def test_a_stale_universe_source_is_refused():
    taken = captured_chain()
    captured_at = record_instant(taken)
    artifact = dataclasses.replace(
        resolved(taken),
        source_scope=scope(requested_at=captured_at - timedelta(days=30)),
    )
    reasons = compatibility(artifact, chain_requested_at=captured_at)
    assert any("beyond the" in reason for reason in reasons)


def test_a_universe_observed_after_the_chain_is_refused():
    taken = captured_chain()
    captured_at = record_instant(taken)
    artifact = dataclasses.replace(
        resolved(taken), observed_at=captured_at + timedelta(hours=2)
    )
    reasons = compatibility(artifact, chain_requested_at=captured_at)
    assert any("after the chain request" in reason for reason in reasons)


def test_a_universe_resolved_by_another_version_is_refused():
    taken = captured_chain()
    captured_at = record_instant(taken)
    artifact = dataclasses.replace(
        resolved(taken),
        resolver_version="universe-resolver/1.0",
        source_scope=scope(requested_at=captured_at),
    )
    reasons = compatibility(artifact, chain_requested_at=captured_at)
    assert any("this repository reads" in reason for reason in reasons)


def test_a_compatible_source_passes():
    taken = captured_chain()
    captured_at = record_instant(taken)
    artifact = dataclasses.replace(
        resolved(taken), source_scope=scope(requested_at=captured_at)
    )
    assert compatibility(artifact, chain_requested_at=captured_at) == ()


# =============================================================================
# §1/§8 -- coverage decides completeness, and it is typed
# =============================================================================


def test_a_caller_declared_universe_establishes_nothing():
    taken = captured_chain()
    outcome = taken.pipeline.resolve_expected_universe(
        declaration=declared(
            taken,
            source_kind=ExpectedUniverseSourceKind.CALLER_DECLARED,
            record_ids=(),
        )
    )
    # It resolves -- a caller really did state a list -- and states nothing.
    assert outcome.established
    assert outcome.artifact.coverage_status is UniverseCoverageStatus.UNKNOWN_COVERAGE
    assert not outcome.artifact.independently_observed


def test_a_source_label_alone_cannot_make_completeness_independent():
    measure = ChainCompleteness(
        received_quote_count=3,
        received_oi_count=3,
        received_iv_count=3,
        received_greeks_count=3,
        expected_contract_ids=tuple(sorted(FIXTURE_IDENTITIES)),
        received_contract_ids=tuple(sorted(FIXTURE_IDENTITIES)),
        expected_source="VENDOR_CONTRACT_LIST",
    )
    assert not measure.independently_observed
    assert measure.status is CompletenessStatus.PARTIALLY_OBSERVED


def measured(**changes):
    payload = {
        "received_quote_count": 3,
        "received_oi_count": 3,
        "received_iv_count": 3,
        "received_greeks_count": 3,
        "expected_contract_ids": tuple(sorted(FIXTURE_IDENTITIES)),
        "received_contract_ids": tuple(sorted(FIXTURE_IDENTITIES)),
        "expected_source": "AUTHORITATIVE_DOCUMENTATION",
        "universe_artifact_hash": "a" * 64,
        "universe_evidence_fingerprint": "e" * 64,
        "coverage_status": UniverseCoverageStatus.FULL_REQUEST_ENUMERATED.value,
        "resolver_version": UNIVERSE_RESOLVER_SCHEMA_VERSION,
    }
    payload.update(changes)
    return ChainCompleteness(**payload)


def test_an_observed_subset_is_partially_observed():
    measure = measured(
        expected_source="OBSERVED_SNAPSHOT_ROWS",
        coverage_status=UniverseCoverageStatus.OBSERVED_SUBSET.value,
    )
    assert measure.independently_observed
    assert measure.status is CompletenessStatus.PARTIALLY_OBSERVED
    assert not measure.status.implies_complete


def test_a_partial_page_can_find_a_hole_and_cannot_close_one():
    common = {
        "received_quote_count": 1,
        "received_oi_count": 1,
        "received_iv_count": 1,
        "received_greeks_count": 1,
        "expected_source": "CAPTURED_PAGINATION_METADATA",
        "universe_artifact_hash": "a" * 64,
        "universe_evidence_fingerprint": "e" * 64,
        "coverage_status": UniverseCoverageStatus.PARTIAL_PAGE.value,
        "resolver_version": UNIVERSE_RESOLVER_SCHEMA_VERSION,
    }
    missing = ChainCompleteness(
        expected_contract_ids=("A", "B"), received_contract_ids=("A",), **common
    )
    assert missing.status is CompletenessStatus.PARTIAL_UNIVERSE_MISSING_IDENTITIES

    present = ChainCompleteness(
        expected_contract_ids=("A",), received_contract_ids=("A", "B"), **common
    )
    assert present.status is CompletenessStatus.PARTIAL_UNIVERSE_ALL_LISTED_PRESENT
    assert not present.status.implies_complete


def test_only_full_request_enumerated_reports_measured_complete():
    assert measured().status is CompletenessStatus.MEASURED_COMPLETE
    assert measured().status.implies_complete


def test_the_engine_refuses_an_unverified_declaration():
    from src.gex.engine import resolve_chain_completeness

    taken = captured_chain()
    with pytest.raises(TypeError, match=r"(?i)VerifiedExpectedUniverseArtifact"):
        resolve_chain_completeness(taken.chain, declared(taken))


# =============================================================================
# §8/§9 -- verified before the chain opens, recovered whole afterwards
# =============================================================================


def capture_with_universe(**changes):
    """A capture whose universe was resolved *before* the chain operation opened.

    Two phases, because that is the shape of the problem: the source has to be
    captured first, resolved against a verified manifest, and only then may the
    chain operation be stamped with the resulting artifact's hash. The source
    here is a quote response captured into a first session -- a stand-in, and it
    says so: no ThetaData contract-list endpoint is wired (OD-11). What it
    exercises is real, since the resolver reopens those exact bytes.
    """
    from src.adapters.artifact_store import InMemoryArtifactStore
    from src.adapters.raw_store import RawCaptureManifest
    from tests.certification_fixtures import (
        CAPTURED_AT,
        PAYLOADS,
        CapturedChain,
        documented_settlement_rule,
        durable_store,
        resolved_pipeline,
    )

    pipeline = resolved_pipeline()
    store = durable_store()
    artifacts = InMemoryArtifactStore()

    listing = pipeline.capture_session(store=store, session_id="listing", as_of=AS_OF)
    listing.capture(
        endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT.value,
        query_params={"symbol": "SPXW", "expiration": "*"},
        payload=PAYLOADS[Endpoint.OPTION_QUOTE_SNAPSHOT],
        request_started_at=CAPTURED_AT,
        response_received_at=CAPTURED_AT,
        http_status=200,
    )
    listing_id = listing.captured[-1].record_id
    listing_manifest = RawCaptureManifest.from_session(
        listing,
        since=0,
        capture_plan_fingerprint=pipeline.capture_plan.fingerprint,
        pipeline_fingerprint=pipeline.fingerprint(),
    )

    outcome = pipeline.resolve_expected_universe(
        declaration=ExpectedContractUniverse(
            identities=frozenset(changes.pop("identities", FIXTURE_IDENTITIES)),
            source_kind=ExpectedUniverseSourceKind.OBSERVED_SNAPSHOT_ROWS,
            source_record_ids=(listing_id,),
            scope=scope(**changes.pop("scope_changes", {})),
            declared_at=AS_OF,
        ),
        source_manifest=listing_manifest,
        source_store=store,
        as_of=AS_OF,
    )
    if not outcome.established:
        return None, outcome

    rule = documented_settlement_rule()
    session = pipeline.capture_session(
        store=store,
        session_id="chain",
        as_of=AS_OF,
        universe_resolution=outcome,
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
    return (
        CapturedChain(
            chain=chain,
            store=store,
            manifest=manifest,
            pipeline=pipeline,
            artifacts=artifacts,
            settlement_artifact=rule,
            expected_universe=outcome.artifact,
        ),
        outcome,
    )


def test_the_capture_owned_universe_reaches_fetch_chain_automatically():
    owned, _ = capture_with_universe()
    assert owned.chain.completeness is not None
    assert owned.chain.completeness.expected_contract_ids == tuple(
        sorted(FIXTURE_IDENTITIES)
    )
    assert owned.chain.completeness.universe_artifact_hash == (
        owned.expected_universe.artifact_hash
    )
    assert owned.chain.completeness.coverage_status == "OBSERVED_SUBSET"
    assert owned.chain.completeness.status is CompletenessStatus.PARTIALLY_OBSERVED


def test_a_fetch_cannot_supply_a_second_universe():
    owned, outcome = capture_with_universe()
    from src.adapters.artifact_store import InMemoryArtifactStore
    from tests.certification_fixtures import documented_settlement_rule, durable_store

    session = owned.pipeline.capture_session(
        store=durable_store(),
        session_id="owns-a-universe",
        as_of=AS_OF,
        universe_resolution=outcome,
        settlement_rule=documented_settlement_rule(),
        artifact_store=InMemoryArtifactStore(),
    )
    with pytest.raises(PipelineConsistencyError, match=r"(?i)supplied another"):
        owned.pipeline.fetch_chain(
            as_of=AS_OF,
            capture=session,
            expected_contract_ids=("SPXW:2026-03-20:5000:call",),
            expected_source="somewhere_else",
        )


def test_recovery_returns_the_verified_artifact():
    owned, _ = capture_with_universe()
    recovered = owned.pipeline.recover_capture_artifacts(
        manifest=owned.manifest, store=owned.store, artifact_store=owned.artifacts
    )
    assert recovered.failures == ()
    artifact = recovered.expected_universe
    assert isinstance(artifact, VerifiedExpectedUniverseArtifact)
    assert artifact.artifact_hash == owned.expected_universe.artifact_hash
    assert artifact.coverage_status is UniverseCoverageStatus.OBSERVED_SUBSET


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("observed_at", AS_OF + timedelta(days=1)),
        ("source_scope", "widened"),
        ("source_pipeline_fingerprint", "z" * 64),
        ("source_verification_fingerprint", "z" * 64),
        ("evidence_fingerprint", "z" * 64),
        ("documentation_evidence_id", "something-else"),
    ],
)
def test_recovery_compares_the_whole_semantic_artifact(field, value):
    """The named regression.

    v2.1.10 compared the identity set and the coverage status -- two fields of
    thirteen. A stale listing whose ``observed_at`` and scope were replaced to
    look current recovered cleanly and reached a trusted calculation.
    """
    owned, _ = capture_with_universe()
    stored = owned.pipeline.recover_capture_artifacts(
        manifest=owned.manifest, store=owned.store, artifact_store=owned.artifacts
    ).expected_universe
    edited = dataclasses.replace(
        stored,
        **{field: scope(max_dte=999) if value == "widened" else value},
    )
    assert edited.artifact_hash != stored.artifact_hash

    # Written into the store under its own (edited) key, and stamped as though
    # the capture had been opened under it.
    from src.adapters.artifact_store import ArtifactKind

    owned.artifacts.put(ArtifactKind.EXPECTED_UNIVERSE, edited.semantic_payload())
    artifact, failure = owned.pipeline._recover_universe(
        edited.semantic_payload(),
        universe_key=edited.artifact_hash,
        manifest=owned.manifest,
        store=owned.store,
        artifact_store=owned.artifacts,
    )
    assert artifact is None
    # And the refusal names what moved rather than printing two digests.
    assert field in failure or "capture verification" in failure


def test_a_declared_universe_reaches_diagnostics_and_not_completeness():
    """The explicitly diagnostic form. It stamps nothing."""
    taken = captured_chain()
    diagnostic = captured_chain(declared_expected_universe=declared(taken))
    assert all(not r.expected_universe_fingerprint for r in diagnostic.manifest.records)
    assert diagnostic.chain.completeness is not None
    assert diagnostic.chain.completeness.universe_artifact_hash is None
    assert diagnostic.chain.completeness.status is (
        CompletenessStatus.PARTIALLY_OBSERVED
    )


def test_a_verified_capture_still_computes_a_trusted_gex():
    """The gate leaves a path through it."""
    owned, _ = capture_with_universe()
    snapshot = owned.pipeline.compute_trusted_gex(
        owned.chain, **trusted_evidence(owned)
    )
    assert snapshot.meta["trusted"] is True
    assert snapshot.meta["chain_completeness"]["status"] == "PARTIALLY_OBSERVED"


def test_the_evidence_chain_is_persisted_beside_the_capture():
    """Recovery must not need a registry somebody populated in this process."""
    from src.adapters.artifact_store import ArtifactKind

    owned, _ = capture_with_universe()
    kinds = {
        owned.artifacts.get(key)["kind"]
        for key in owned.artifacts.keys()  # noqa: SIM118
    }
    assert ArtifactKind.EXPECTED_UNIVERSE in kinds
    assert ArtifactKind.UNIVERSE_RESOLUTION in kinds
    assert ArtifactKind.CAPTURE_VERIFICATION in kinds
    assert ArtifactKind.SETTLEMENT_RULE in kinds


# =============================================================================
# §10 -- readiness names what it checked
# =============================================================================


def test_raw_capture_readiness_does_not_require_a_full_universe():
    """Bytes are worth collecting whatever their coverage."""
    from src.adapters.certification import CertificationState
    from tests.certification_fixtures import readiness

    result = readiness()
    assert result.ready, result.blockers
    assert result.state is CertificationState.READY_FOR_RAW_CAPTURE_ONLY


@pytest.mark.parametrize(
    ("coverage", "expected_ready"),
    [
        (UniverseCoverageStatus.FULL_REQUEST_ENUMERATED, True),
        (UniverseCoverageStatus.PARTIAL_PAGE, False),
        (UniverseCoverageStatus.OBSERVED_SUBSET, False),
        (UniverseCoverageStatus.UNKNOWN_COVERAGE, False),
    ],
)
def test_universe_readiness_requires_full_request_enumeration(coverage, expected_ready):
    from src.adapters.certification import UniverseReadiness, universe_readiness_of

    expected = (
        UniverseReadiness.UNIVERSE_READY
        if expected_ready
        else UniverseReadiness.UNIVERSE_NOT_READY
    )
    assert universe_readiness_of(measured(coverage_status=coverage.value)) is expected


def test_the_completeness_only_function_no_longer_returns_a_dataset_verdict():
    """The named regression: one of six checks may not produce the whole state."""
    import src.adapters.certification as certification

    assert not hasattr(certification, "analytical_readiness_of")
    values = {
        state.value
        for state in certification.UniverseReadiness  # type: ignore[attr-defined]
    }
    assert values == {"UNIVERSE_READY", "UNIVERSE_NOT_READY"}
    assert "READY_FOR_ANALYTICAL_DATASET" not in values


def test_fabricated_analytical_inputs_cannot_return_ready():
    """The named v2.1.12 regression.

    v2.1.11 took six loose ``Any`` arguments, and every one was satisfiable by a
    ``SimpleNamespace``: a receipt whose ``matches()`` always returned true, a
    report with an empty ``blocking_dimensions``, a completeness object with the
    right ``coverage_status`` string. Six fabricated objects were ready.
    """
    import types

    from src.adapters.certification import assess_analytical_readiness
    from src.adapters.errors import ThetaDataCertificationError

    forged = types.SimpleNamespace(
        normalization_matches=True,
        settlement_established=True,
        pricing_dimensions_unresolved=(),
        universe="UNIVERSE_READY",
        excluded_records=(),
        capture_pipeline_fingerprint="a" * 64,
        reading_pipeline_fingerprint="a" * 64,
        derivation_failures=(),
        matches=lambda *_: True,
        blocking_dimensions=(),
    )
    with pytest.raises(ThetaDataCertificationError, match=r"(?i)VerifiedAnalytical"):
        assess_analytical_readiness(forged)


def test_analytical_readiness_requires_every_condition_it_names():
    """A complete universe is one of six, and alone it decides nothing."""
    from src.adapters.certification import (
        ANALYTICAL_DATASET_REQUIREMENTS,
        AnalyticalReadiness,
        UniverseReadiness,
        VerifiedAnalyticalEvidenceContext,
        assess_analytical_readiness,
    )

    report = assess_analytical_readiness(
        VerifiedAnalyticalEvidenceContext(
            normalization_matches=False,
            settlement_established=False,
            pricing_dimensions_unresolved=("DAY_COUNT",),
            universe=UniverseReadiness.UNIVERSE_READY,
            excluded_records=(),
            capture_pipeline_fingerprint="",
            reading_pipeline_fingerprint="",
        )
    )
    assert report.state is AnalyticalReadiness.NOT_ANALYTICALLY_READY
    assert "chain completeness is FULL_REQUEST_ENUMERATED" in report.satisfied
    assert len(ANALYTICAL_DATASET_REQUIREMENTS) == 6
    joined = " ".join(report.blockers)
    for fragment in (
        "re-derived from its raw records",
        "settlement rule",
        "pricing compatibility",
        "pipeline fingerprints",
    ):
        assert fragment in joined


def test_the_shipped_capture_is_not_analytically_ready():
    """The honest state, derived from the capture rather than described."""
    from src.adapters.certification import (
        AnalyticalReadiness,
        assess_analytical_readiness,
        build_analytical_evidence,
    )

    owned, _ = capture_with_universe()
    context = build_analytical_evidence(
        pipeline=owned.pipeline,
        chain=owned.chain,
        manifest=owned.manifest,
        store=owned.store,
        artifact_store=owned.artifacts,
        pricing_compatibility=owned.pipeline.pricing_compatibility,
    )
    report = assess_analytical_readiness(context)
    assert report.state is AnalyticalReadiness.NOT_ANALYTICALLY_READY
    assert any("FULL_REQUEST_ENUMERATED" in blocker for blocker in report.blockers)
