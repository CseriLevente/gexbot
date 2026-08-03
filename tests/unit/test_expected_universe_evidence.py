"""Coverage is what a resolver established, not what a source was called.

v2.1.9 made a universe *resolvable*: the resolver reopened the records it named
and re-derived the identities. That closed "a caller typed a list and labelled
it a vendor listing". It left the harder half open, and this file is about that
half.

**Proving a set of identities occurs in stored records is not proving those
records enumerate the complete universe the request should have returned.** A
truncated response enumerates its own rows perfectly. So:

* any endpoint with one row per contract passed as a ``VENDOR_CONTRACT_LIST``,
  and an ``/v3/option/snapshot/quote`` response established
  ``MEASURED_COMPLETE`` for the whole chain;
* ``CAPTURED_PAGINATION_METADATA`` named a check nobody had written -- its
  resolver re-derived identities and never read a page number;
* the universe resolver looked its evidence id up in the *settlement* registry,
  so a document about open-interest settlement defined a contract universe;
* ``complete_for_request: bool`` was a constructor argument, hashed into the
  universe, which made a caller's Boolean look like a finding;
* ``observed_at`` came from the caller, so a listing captured three weeks ago
  could present itself as observed this morning.

Every test here fails against v2.1.9.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

import pytest

from src.adapters.thetadata.endpoints import Endpoint
from src.adapters.universe_resolvers import (
    check_source_compatibility,
    resolve_expected_universe,
)
from src.config.pipeline import PipelineConsistencyError
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


def scope(**changes):
    """A scope mirroring the fixture pipeline's actual chain request.

    That request sends ``expiration="*"`` with no DTE or strike filter, so the
    unbounded form is what a listing for it would carry. The parametrised
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


def resolved(taken, **changes):
    outcome = resolve_expected_universe(
        declared(taken, **changes), manifest=taken.manifest, store=taken.store
    )
    assert outcome.established, outcome.failure
    return outcome.artifact


# =============================================================================
# §3 -- a market-data snapshot is not a contract list
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
    """The central regression, one endpoint at a time.

    v2.1.9 accepted every one of these as a ``VENDOR_CONTRACT_LIST``, so a
    response returning the contracts the vendor chose to send established
    ``MEASURED_COMPLETE`` for the whole request.
    """
    from src.adapters.thetadata.endpoints import capabilities_of

    capability = capabilities_of(endpoint.value)
    assert capability.enumerates_rows
    assert not capability.is_dedicated_contract_list
    assert not capability.enumerates_request_universe
    assert not capability.can_establish_full_coverage


def test_a_quote_snapshot_labelled_a_contract_list_is_refused():
    """End to end, through the resolver."""
    taken = captured_chain()
    outcome = resolve_expected_universe(
        declared(taken, source_kind=ExpectedUniverseSourceKind.VENDOR_CONTRACT_LIST),
        manifest=taken.manifest,
        store=taken.store,
    )
    assert not outcome.established
    assert "not dedicated contract-list endpoints" in outcome.failure
    assert outcome.coverage_status is UniverseCoverageStatus.UNKNOWN_COVERAGE


def test_the_same_records_resolve_honestly_as_an_observed_subset():
    """The gate leaves a path through it, and the path says what it is."""
    taken = captured_chain()
    artifact = resolved(taken)
    assert artifact.coverage_status is UniverseCoverageStatus.OBSERVED_SUBSET
    assert not artifact.establishes_completeness
    assert artifact.identity_set == FIXTURE_IDENTITIES


def test_an_artifact_cannot_claim_more_than_its_source_supports():
    """Closed at the type, so no resolver bug can produce one either."""
    with pytest.raises(UniverseArtifactError, match=r"(?i)cannot establish"):
        VerifiedExpectedUniverseArtifact(
            identities=FIXTURE_IDENTITIES,
            source_kind=ExpectedUniverseSourceKind.OBSERVED_SNAPSHOT_ROWS,
            coverage_status=UniverseCoverageStatus.FULL_REQUEST_ENUMERATED,
            source_operation_fingerprint="op",
            source_record_ids=("r1",),
            source_request_spec_fingerprint="spec",
            source_scope=scope(),
            observed_at=AS_OF,
            evidence_fingerprint="e" * 64,
        )


def test_an_index_print_enumerates_nothing():
    taken = captured_chain()
    outcome = resolve_expected_universe(
        declared(taken, record_ids=(index_record(taken),)),
        manifest=taken.manifest,
        store=taken.store,
    )
    assert not outcome.established
    assert "do not enumerate contracts" in outcome.failure


# =============================================================================
# §4 -- pagination coverage is read, not asserted
# =============================================================================


def test_an_ordinary_quote_response_cannot_satisfy_pagination_evidence():
    """The named regression. v2.1.9's resolver never read a page number."""
    taken = captured_chain()
    outcome = resolve_expected_universe(
        declared(
            taken,
            source_kind=ExpectedUniverseSourceKind.CAPTURED_PAGINATION_METADATA,
        ),
        manifest=taken.manifest,
        store=taken.store,
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
        {"r1": "page,total_pages,next_page_token\n1,2,abc\n"}
    )
    assert evidence is not None
    assert evidence.missing_pages == (2,)
    assert not evidence.complete


def test_a_missing_continuation_prevents_completeness():
    """Every declared page present, and no page saying it is the last."""
    from src.adapters.universe_evidence import read_pagination_metadata

    evidence = read_pagination_metadata(
        {
            "r1": "page,total_pages,next_page_token\n1,2,abc\n",
            "r2": "page,total_pages,next_page_token\n2,2,def\n",
        }
    )
    assert evidence is not None
    assert evidence.missing_pages == ()
    assert not evidence.continuation_complete
    assert not evidence.complete


def test_a_response_with_no_pagination_columns_yields_no_evidence():
    """Which the resolver turns into a refusal, not into a coverage claim."""
    from src.adapters.universe_evidence import read_pagination_metadata

    assert read_pagination_metadata({"r1": "symbol,strike\nSPXW,5000\n"}) is None


def test_partial_pagination_metadata_is_refused():
    from src.adapters.errors import ThetaDataProvenanceError
    from src.adapters.universe_evidence import read_pagination_metadata

    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)partial pagination"):
        read_pagination_metadata({"r1": "page,symbol\n1,SPXW\n"})


def test_a_caller_supplied_page_total_is_not_evidence():
    """The object can be built by hand and the resolver builds its own."""
    from src.adapters.universe_evidence import (
        PaginationCoverageEvidence,
        read_pagination_metadata,
    )

    asserted = PaginationCoverageEvidence(
        total_pages=1,
        captured_pages=frozenset({1}),
        source_record_ids=("r1",),
        continuation_complete=True,
    )
    assert asserted.complete
    # And what the bytes actually say disagrees.
    from_bytes = read_pagination_metadata(
        {"r1": "page,total_pages,next_page_token\n1,4,abc\n"}
    )
    assert from_bytes is not None
    assert from_bytes.total_pages == 4
    assert not from_bytes.complete


# =============================================================================
# §5 -- universe documentation is not settlement documentation
# =============================================================================


def test_a_settlement_rule_cannot_establish_a_universe():
    """The named regression.

    ``fixture-oi-settlement-convention`` is a real, content-verified document
    about when open interest settles. It says nothing about which options
    exist, and v2.1.9 looked universe evidence up in that registry.
    """
    from tests.certification_fixtures import (
        FIXTURE_OI_EVIDENCE_ID,
        register_fixture_documentation_rule,
    )

    register_fixture_documentation_rule()
    taken = captured_chain()
    outcome = resolve_expected_universe(
        declared(
            taken,
            source_kind=ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION,
            documentation_evidence_id=FIXTURE_OI_EVIDENCE_ID,
            source_record_ids=(),
        )
    )
    assert not outcome.established
    assert "not a registered *universe* documentation rule" in outcome.failure


def test_the_production_universe_registry_is_empty():
    """No document stating which SPX/SPXW contracts exist has been read. OD-11."""
    from src.adapters.universe_evidence import UNIVERSE_DOCUMENTATION_RULES

    assert UNIVERSE_DOCUMENTATION_RULES.registered_ids() == ()


def test_a_universe_rule_must_say_something_about_contracts():
    """A content-verified document that lists no contracts establishes none."""
    import pathlib

    from src.adapters.errors import ThetaDataProvenanceError
    from src.adapters.evidence_resolvers import content_hash_of
    from src.adapters.universe_evidence import UniverseDocumentationRule
    from tests.certification_fixtures import FIXTURE_DOCUMENT

    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)neither an identity"):
        UniverseDocumentationRule(
            evidence_id="rule-1",
            document_reference=FIXTURE_DOCUMENT,
            document_content_hash=content_hash_of(pathlib.Path(FIXTURE_DOCUMENT)),
            rule_identifier="says_nothing_about_contracts",
            effective_from=date(2020, 1, 1),
            scope=scope(),
            extraction_version="test/1",
        )


def test_a_registered_universe_rule_derives_its_identities():
    """The gate leaves a path through it, and the path derives rather than asserts."""
    import pathlib

    from src.adapters.evidence_resolvers import content_hash_of
    from src.adapters.universe_evidence import (
        UniverseDocumentationRegistry,
        UniverseDocumentationRule,
    )
    from tests.certification_fixtures import FIXTURE_DOCUMENT

    registry = UniverseDocumentationRegistry()
    registry.register(
        UniverseDocumentationRule(
            evidence_id="listed-universe",
            document_reference=FIXTURE_DOCUMENT,
            document_content_hash=content_hash_of(pathlib.Path(FIXTURE_DOCUMENT)),
            rule_identifier="spxw_march_20_ladder",
            effective_from=date(2020, 1, 1),
            scope=scope(),
            extraction_version="test/1",
            identities=FIXTURE_IDENTITIES,
        )
    )
    taken = captured_chain()
    outcome = resolve_expected_universe(
        declared(
            taken,
            source_kind=ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION,
            documentation_evidence_id="listed-universe",
            source_record_ids=(),
        ),
        registry=registry,
    )
    assert outcome.established, outcome.failure
    assert outcome.artifact.coverage_status is (
        UniverseCoverageStatus.FULL_REQUEST_ENUMERATED
    )


def test_a_document_that_derives_other_identities_is_refused():
    import pathlib

    from src.adapters.evidence_resolvers import content_hash_of
    from src.adapters.universe_evidence import (
        UniverseDocumentationRegistry,
        UniverseDocumentationRule,
    )
    from tests.certification_fixtures import FIXTURE_DOCUMENT

    registry = UniverseDocumentationRegistry()
    registry.register(
        UniverseDocumentationRule(
            evidence_id="listed-universe",
            document_reference=FIXTURE_DOCUMENT,
            document_content_hash=content_hash_of(pathlib.Path(FIXTURE_DOCUMENT)),
            rule_identifier="a_different_ladder",
            effective_from=date(2020, 1, 1),
            scope=scope(),
            extraction_version="test/1",
            identities=frozenset({"SPXW:2026-03-20:9999:call"}),
        )
    )
    taken = captured_chain()
    outcome = resolve_expected_universe(
        declared(
            taken,
            source_kind=ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION,
            documentation_evidence_id="listed-universe",
            source_record_ids=(),
        ),
        registry=registry,
    )
    assert not outcome.established
    assert "has not established them" in outcome.failure


# =============================================================================
# §6/§7 -- source records, scope and timing
# =============================================================================


def test_a_fake_record_id_fails_verification():
    taken = captured_chain()
    outcome = resolve_expected_universe(
        declared(taken, record_ids=("fake-record",)),
        manifest=taken.manifest,
        store=taken.store,
    )
    assert not outcome.established
    assert "does not hold" in outcome.failure


def test_a_universe_its_records_do_not_produce_fails():
    taken = captured_chain()
    outcome = resolve_expected_universe(
        declared(taken, identities={"SPXW:2026-03-20:9999:call"}),
        manifest=taken.manifest,
        store=taken.store,
    )
    assert not outcome.established
    assert "claimed but not present" in outcome.failure


def test_a_universe_with_no_capture_cannot_be_verified():
    taken = captured_chain()
    outcome = resolve_expected_universe(declared(taken))
    assert not outcome.established
    assert "never opened" in outcome.failure


def test_a_universe_without_a_scope_cannot_be_verified():
    """A listing that does not say what it asked for cannot be compared."""
    taken = captured_chain()
    outcome = resolve_expected_universe(
        declared(taken, scope=None), manifest=taken.manifest, store=taken.store
    )
    assert not outcome.established
    assert "no request scope" in outcome.failure


def record_instant(taken):
    """When the source record was actually received."""
    named = {quote_record(taken)}
    return max(
        r.response_received_at for r in taken.manifest.records if r.record_id in named
    )


def test_observed_at_is_derived_from_the_source_records():
    """The named regression. v2.1.9 took it from the declaration.

    A listing captured three weeks ago could therefore present itself as
    observed this morning, and staleness is measured against that instant.
    """
    taken = captured_chain()
    long_ago = AS_OF - timedelta(days=90)
    artifact = resolve_expected_universe(
        declared(taken, declared_at=long_ago),
        manifest=taken.manifest,
        store=taken.store,
    ).artifact
    assert artifact.observed_at != long_ago
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
    """Identities matching is not reassurance when the scopes differ.

    On a narrower sweep a perfect re-derivation is exactly what a false
    ``MEASURED_COMPLETE`` looks like: every contract outside the window reads as
    an unexpected extra rather than as one that was never enumerated.
    """
    taken = captured_chain()
    artifact = dataclasses.replace(resolved(taken), source_scope=scope(**change))
    reasons = check_source_compatibility(
        artifact, chain_scope=scope(), chain_requested_at=AS_OF
    )
    assert reasons
    assert any(fragment in reason for reason in reasons)


def test_a_stale_universe_source_is_refused():
    taken = captured_chain()
    captured_at = record_instant(taken)
    artifact = dataclasses.replace(
        resolved(taken),
        source_scope=scope(requested_at=captured_at - timedelta(days=30)),
    )
    reasons = check_source_compatibility(
        artifact, chain_scope=scope(), chain_requested_at=captured_at
    )
    assert any("beyond the" in reason for reason in reasons)


def test_a_universe_observed_after_the_chain_is_refused():
    taken = captured_chain()
    captured_at = record_instant(taken)
    artifact = dataclasses.replace(
        resolved(taken), observed_at=captured_at + timedelta(hours=2)
    )
    reasons = check_source_compatibility(
        artifact, chain_scope=scope(), chain_requested_at=captured_at
    )
    assert any("after the chain request" in reason for reason in reasons)


def test_a_universe_resolved_by_another_version_is_refused():
    taken = captured_chain()
    captured_at = record_instant(taken)
    artifact = dataclasses.replace(
        resolved(taken),
        resolver_version="universe-resolver/1.0",
        source_scope=scope(requested_at=captured_at),
    )
    reasons = check_source_compatibility(
        artifact, chain_scope=scope(), chain_requested_at=captured_at
    )
    assert any("this repository reads" in reason for reason in reasons)


def test_a_compatible_source_passes():
    taken = captured_chain()
    captured_at = record_instant(taken)
    artifact = dataclasses.replace(
        resolved(taken), source_scope=scope(requested_at=captured_at)
    )
    assert (
        check_source_compatibility(
            artifact, chain_scope=scope(), chain_requested_at=captured_at
        )
        == ()
    )


# =============================================================================
# §1/§8 -- coverage decides completeness, and it is typed
# =============================================================================


def test_a_caller_declared_universe_establishes_nothing():
    taken = captured_chain()
    outcome = resolve_expected_universe(
        declared(
            taken,
            source_kind=ExpectedUniverseSourceKind.CALLER_DECLARED,
            source_record_ids=(),
        )
    )
    # It resolves -- a caller really did state a list -- and states nothing.
    assert outcome.established
    assert outcome.artifact.coverage_status is UniverseCoverageStatus.UNKNOWN_COVERAGE
    assert not outcome.artifact.independently_observed
    assert not outcome.establishes_completeness


def test_a_source_label_alone_cannot_make_completeness_independent():
    """The named regression, at the measure itself."""
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


def test_an_observed_subset_is_partially_observed():
    """Every identity in it arrived; it never claimed to be exhaustive."""
    measure = ChainCompleteness(
        received_quote_count=3,
        received_oi_count=3,
        received_iv_count=3,
        received_greeks_count=3,
        expected_contract_ids=tuple(sorted(FIXTURE_IDENTITIES)),
        received_contract_ids=tuple(sorted(FIXTURE_IDENTITIES)),
        expected_source="OBSERVED_SNAPSHOT_ROWS",
        universe_artifact_hash="a" * 64,
        universe_evidence_fingerprint="e" * 64,
        coverage_status=UniverseCoverageStatus.OBSERVED_SUBSET.value,
        resolver_version=UNIVERSE_RESOLVER_SCHEMA_VERSION,
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
    measure = ChainCompleteness(
        received_quote_count=3,
        received_oi_count=3,
        received_iv_count=3,
        received_greeks_count=3,
        expected_contract_ids=tuple(sorted(FIXTURE_IDENTITIES)),
        received_contract_ids=tuple(sorted(FIXTURE_IDENTITIES)),
        expected_source="AUTHORITATIVE_DOCUMENTATION",
        universe_artifact_hash="a" * 64,
        universe_evidence_fingerprint="e" * 64,
        coverage_status=UniverseCoverageStatus.FULL_REQUEST_ENUMERATED.value,
        resolver_version=UNIVERSE_RESOLVER_SCHEMA_VERSION,
    )
    assert measure.status is CompletenessStatus.MEASURED_COMPLETE
    assert measure.status.implies_complete


def test_the_engine_refuses_an_unverified_declaration():
    """A declaration is what somebody expects, and the engine measures against
    what a resolver established."""
    from src.gex.engine import resolve_chain_completeness

    taken = captured_chain()
    with pytest.raises(TypeError, match=r"(?i)VerifiedExpectedUniverseArtifact"):
        resolve_chain_completeness(taken.chain, declared(taken))


# =============================================================================
# §9/§10 -- verified before the chain opens, and recovered afterwards
# =============================================================================


def capture_with_universe(**changes):
    """A capture whose universe was resolved *before* the chain operation opened.

    Two phases, because that is the real shape of the problem: the source has to
    be captured first, resolved, and only then may the chain operation be
    stamped with the resulting artifact's hash. The source here is a quote
    response captured into a first session -- a stand-in, and it says so: no
    ThetaData contract-list endpoint is wired (OD-11). What it exercises is
    real, since the resolver reopens those exact bytes.
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
        query_params={"root": "SPXW", "listing": "1"},
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

    identities = changes.pop("identities", FIXTURE_IDENTITIES)
    outcome = resolve_expected_universe(
        ExpectedContractUniverse(
            identities=frozenset(identities),
            source_kind=ExpectedUniverseSourceKind.OBSERVED_SNAPSHOT_ROWS,
            source_record_ids=(listing_id,),
            scope=scope(**changes.pop("scope_changes", {})),
            declared_at=AS_OF,
        ),
        manifest=listing_manifest,
        store=store,
    )
    if not outcome.established:
        return None, outcome

    rule = documented_settlement_rule()
    session = pipeline.capture_session(
        store=store,
        session_id="chain",
        as_of=AS_OF,
        verified_expected_universe=outcome.artifact,
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


def test_a_verified_artifact_is_required_before_chain_capture():
    """A declaration is refused where evidence is expected."""
    from tests.certification_fixtures import durable_store, resolved_pipeline

    taken = captured_chain()
    pipeline = resolved_pipeline()
    with pytest.raises(PipelineConsistencyError, match=r"(?i)is a declaration"):
        pipeline.capture_session(
            store=durable_store(),
            session_id="chain",
            as_of=AS_OF,
            verified_expected_universe=declared(taken),
        )


def test_the_capture_owned_universe_reaches_fetch_chain_automatically():
    owned, _ = capture_with_universe()
    assert owned.chain.completeness is not None
    assert owned.chain.completeness.expected_contract_ids == tuple(
        sorted(FIXTURE_IDENTITIES)
    )
    # And it carries the typed evidence, not merely the label.
    assert owned.chain.completeness.universe_artifact_hash == (
        owned.expected_universe.artifact_hash
    )
    assert owned.chain.completeness.coverage_status == "OBSERVED_SUBSET"
    assert owned.chain.completeness.status is CompletenessStatus.PARTIALLY_OBSERVED


def test_a_fetch_cannot_supply_a_second_universe():
    from src.adapters.artifact_store import InMemoryArtifactStore
    from tests.certification_fixtures import (
        documented_settlement_rule,
        durable_store,
        resolved_pipeline,
    )

    owned, _ = capture_with_universe()
    pipeline = resolved_pipeline()
    session = pipeline.capture_session(
        store=durable_store(),
        session_id="owns-a-universe",
        as_of=AS_OF,
        verified_expected_universe=owned.expected_universe,
        settlement_rule=documented_settlement_rule(),
        artifact_store=InMemoryArtifactStore(),
    )
    with pytest.raises(PipelineConsistencyError, match=r"(?i)supplied another"):
        pipeline.fetch_chain(
            as_of=AS_OF,
            capture=session,
            expected_contract_ids=("SPXW:2026-03-20:5000:call",),
            expected_source="somewhere_else",
        )


def test_recovery_returns_the_verified_artifact():
    """The named regression. v2.1.9 returned the *declaration* it re-resolved."""
    owned, _ = capture_with_universe()
    recovered = owned.pipeline.recover_capture_artifacts(
        manifest=owned.manifest, store=owned.store, artifact_store=owned.artifacts
    )
    assert recovered.failures == ()
    artifact = recovered.expected_universe
    assert isinstance(artifact, VerifiedExpectedUniverseArtifact)
    assert artifact.artifact_hash == owned.expected_universe.artifact_hash
    assert artifact.coverage_status is UniverseCoverageStatus.OBSERVED_SUBSET
    assert artifact.evidence_fingerprint


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
    # And it does not claim completeness it did not measure.
    assert snapshot.meta["chain_completeness"]["status"] == "PARTIALLY_OBSERVED"


# =============================================================================
# §12 -- raw capture does not need a full universe; an analytical dataset does
# =============================================================================


def test_raw_capture_readiness_does_not_require_a_full_universe():
    """Bytes are worth collecting whatever their coverage.

    Conflating the two axes would block the first capture on a question only a
    capture can answer, which is the v2.1.3 defect one level up.
    """
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
def test_analytical_readiness_requires_full_request_enumeration(
    coverage, expected_ready
):
    from src.adapters.certification import (
        AnalyticalReadiness,
        analytical_readiness_of,
    )

    measure = ChainCompleteness(
        received_quote_count=3,
        received_oi_count=3,
        received_iv_count=3,
        received_greeks_count=3,
        expected_contract_ids=tuple(sorted(FIXTURE_IDENTITIES)),
        received_contract_ids=tuple(sorted(FIXTURE_IDENTITIES)),
        expected_source="AUTHORITATIVE_DOCUMENTATION",
        universe_artifact_hash="a" * 64,
        universe_evidence_fingerprint="e" * 64,
        coverage_status=coverage.value,
        resolver_version=UNIVERSE_RESOLVER_SCHEMA_VERSION,
    )
    expected = (
        AnalyticalReadiness.READY_FOR_ANALYTICAL_DATASET
        if expected_ready
        else AnalyticalReadiness.NOT_ANALYTICALLY_READY
    )
    assert analytical_readiness_of(measure) is expected


def test_the_shipped_capture_is_not_analytically_ready():
    """The honest state, and the reason: no verified full universe exists."""
    from src.adapters.certification import (
        AnalyticalReadiness,
        analytical_readiness_of,
    )

    owned, _ = capture_with_universe()
    assert (
        analytical_readiness_of(owned.chain.completeness)
        is AnalyticalReadiness.NOT_ANALYTICALLY_READY
    )
