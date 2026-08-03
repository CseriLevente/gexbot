"""An expected universe is re-derived from its source, or it establishes nothing.

v2.1.8 bound the universe to the capture operation, which stopped a replay being
measured against a different one. That was the right binding around an unchecked
object:

    ExpectedContractUniverse(
        identities=frozenset({...}),
        source="vendor_contract_list",
        source_record_ids=("some-record",),
    )

``source`` was a string a caller typed. ``source_record_ids`` was read as a
boolean -- non-empty meant "independently observed" -- and no record was ever
opened. A hand-written list labelled as a vendor listing established
``MEASURED_COMPLETE`` exactly as a real listing would, and the confidence score
moved with it.

Two further things were wrong. There were **two** ``ExpectedContractUniverse``
classes, and the one the engine read carried no provenance at all. And
``complete_for_request`` existed on the type and was read nowhere, so page one
of a paginated listing whose members all arrived reported the whole chain
complete.

Every test here fails against v2.1.8.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.adapters.thetadata.endpoints import Endpoint
from src.adapters.universe_resolvers import resolve_expected_universe
from src.config.pipeline import PipelineConsistencyError
from src.domain.completeness import ChainCompleteness, CompletenessStatus
from src.domain.expected_universe import (
    ExpectedContractUniverse,
    ExpectedUniverseSourceKind,
)
from tests.certification_fixtures import AS_OF, captured_chain, trusted_evidence


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
        "source_kind": ExpectedUniverseSourceKind.VENDOR_CONTRACT_LIST,
        "observed_at": AS_OF,
        "source_record_ids": tuple(
            record_ids if record_ids is not None else (quote_record(taken),)
        ),
    }
    payload.update(changes)
    return ExpectedContractUniverse(**payload)


def capture_with_universe(**changes):
    """A capture whose expected universe was read from its own listing response.

    Two phases, because that is the real shape of the problem: a contract
    listing has to be captured *before* the chain it describes, so the universe
    is built from records that already exist and then declared on the operation
    that fetches the chain.

    The listing here is a quote response captured into a first session, which is
    a stand-in and says so: no ThetaData contract-list endpoint is wired, which
    is OPEN_DECISIONS OD-11. What it exercises is real -- the resolver reopens
    those exact bytes and re-derives the identities.
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

    listing_session = pipeline.capture_session(
        store=store, session_id="listing", as_of=AS_OF
    )
    listing_session.capture(
        endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT.value,
        query_params={"root": "SPXW", "listing": "1"},
        payload=PAYLOADS[Endpoint.OPTION_QUOTE_SNAPSHOT],
        request_started_at=CAPTURED_AT,
        response_received_at=CAPTURED_AT,
        http_status=200,
    )
    listing_id = listing_session.captured[-1].record_id

    identities = changes.pop(
        "identities",
        frozenset(
            {
                "SPXW:2026-03-20:4990:call",
                "SPXW:2026-03-20:5000:call",
                "SPXW:2026-03-20:5010:call",
            }
        ),
    )
    universe = ExpectedContractUniverse(
        identities=frozenset(identities),
        source_kind=ExpectedUniverseSourceKind.VENDOR_CONTRACT_LIST,
        observed_at=AS_OF,
        source_record_ids=(listing_id,),
        **changes,
    )

    rule = documented_settlement_rule()
    session = pipeline.capture_session(
        store=store,
        session_id="chain",
        as_of=AS_OF,
        expected_universe=universe,
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
    return CapturedChain(
        chain=chain,
        store=store,
        manifest=manifest,
        pipeline=pipeline,
        artifacts=artifacts,
        settlement_artifact=rule,
        expected_universe=universe,
    )


# =============================================================================
# §5 -- there is exactly one of these types
# =============================================================================


def test_only_one_expected_contract_universe_class_exists():
    """The regression. Two classes of the same name, different fields.

    ``src.domain.completeness`` defined one and ``src.domain.expected_universe``
    another. The engine read the first; the capture path built the second; only
    the second carried provenance. So the object that decided completeness was
    the object nobody had verified.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "src"
    definitions = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        definitions.extend(
            f"{path.relative_to(root).as_posix()}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and node.name == "ExpectedContractUniverse"
        )
    assert definitions == ["domain/expected_universe.py:87"], definitions


def test_the_completeness_module_no_longer_exports_one():
    import src.domain.completeness as completeness

    assert not hasattr(completeness, "ExpectedContractUniverse")
    assert "ExpectedContractUniverse" not in completeness.__all__


def test_the_engine_refuses_an_untyped_universe():
    """A duck-typed stand-in is how two of them coexisted."""
    from src.gex.engine import resolve_chain_completeness

    taken = captured_chain()

    class Lookalike:
        identity_set = frozenset({"SPXW:2026-03-20:5000:call"})
        source = "vendor_contract_list"
        complete_for_request = True

    with pytest.raises(TypeError, match=r"(?i)ExpectedContractUniverse"):
        resolve_chain_completeness(taken.chain, Lookalike())


# =============================================================================
# §6 -- the evidence is re-derived from the records it names
# =============================================================================


def test_a_fake_record_id_fails_verification():
    """The regression. ``source_record_ids`` was a boolean, never an address."""
    taken = captured_chain()
    resolved = resolve_expected_universe(
        declared(taken, record_ids=("fake-record",)),
        manifest=taken.manifest,
        store=taken.store,
    )
    assert not resolved.established
    assert "does not hold" in resolved.failure


def test_a_universe_its_records_do_not_produce_fails():
    """A list somebody typed, labelled as a vendor listing."""
    taken = captured_chain()
    resolved = resolve_expected_universe(
        declared(taken, identities={"SPXW:2026-03-20:9999:call"}),
        manifest=taken.manifest,
        store=taken.store,
    )
    assert not resolved.established
    assert "claimed but not present" in resolved.failure


def test_a_universe_missing_a_listed_contract_fails():
    taken = captured_chain()
    all_ids = [q.contract.canonical_id for q in taken.chain.quotes]
    resolved = resolve_expected_universe(
        declared(taken, identities=all_ids[:1]),
        manifest=taken.manifest,
        store=taken.store,
    )
    assert not resolved.established
    assert "present but not claimed" in resolved.failure


def test_an_endpoint_that_enumerates_nothing_cannot_state_a_universe():
    """One index print cannot say which options exist."""
    taken = captured_chain()
    resolved = resolve_expected_universe(
        declared(taken, record_ids=(index_record(taken),)),
        manifest=taken.manifest,
        store=taken.store,
    )
    assert not resolved.established
    assert "does not enumerate contracts" in resolved.failure


def test_a_universe_with_no_capture_cannot_be_verified():
    taken = captured_chain()
    resolved = resolve_expected_universe(declared(taken))
    assert not resolved.established
    assert "never opened" in resolved.failure


def test_a_verified_universe_carries_an_evidence_fingerprint():
    taken = captured_chain()
    resolved = resolve_expected_universe(
        declared(taken), manifest=taken.manifest, store=taken.store
    )
    assert resolved.established
    assert len(resolved.universe.evidence_fingerprint) == 64
    assert resolved.universe.independently_observed
    assert resolved.universe.establishes_completeness


def test_an_unverified_universe_is_not_independently_observed():
    """Naming records is a claim about records; reading them is the evidence."""
    taken = captured_chain()
    assert not declared(taken).verified
    assert not declared(taken).independently_observed


def test_a_universe_naming_records_no_store_holds_fails():
    """Provenance to bytes, not to a plausible-looking id."""
    taken = captured_chain()
    other = captured_chain()
    resolved = resolve_expected_universe(
        dataclasses.replace(declared(taken), source_record_ids=(quote_record(other),)),
        manifest=taken.manifest,
        store=taken.store,
    )
    assert not resolved.established
    assert "does not hold" in resolved.failure


# =============================================================================
# §6 -- a caller-declared universe resolves and establishes nothing
# =============================================================================


def test_a_caller_declared_universe_is_not_independently_observed():
    taken = captured_chain()
    caller = ExpectedContractUniverse(
        identities=frozenset(q.contract.canonical_id for q in taken.chain.quotes),
        source_kind=ExpectedUniverseSourceKind.CALLER_DECLARED,
        observed_at=AS_OF,
    )
    resolved = resolve_expected_universe(
        caller, manifest=taken.manifest, store=taken.store
    )
    # It resolves -- a caller really did state a list -- and states nothing.
    assert resolved.established
    assert not resolved.universe.independently_observed
    assert not resolved.universe.establishes_completeness
    assert not resolved.establishes_completeness


def test_a_caller_declared_universe_cannot_measure_completeness():
    """Even with every identity right, it is not a measurement of the vendor."""
    from src.gex.engine import resolve_chain_completeness

    taken = captured_chain()
    caller = ExpectedContractUniverse(
        identities=frozenset(q.contract.canonical_id for q in taken.chain.quotes),
        source_kind=ExpectedUniverseSourceKind.CALLER_DECLARED,
        observed_at=AS_OF,
    )
    measured = resolve_chain_completeness(taken.chain, caller)
    assert not measured.independently_observed
    assert measured.status is CompletenessStatus.PARTIALLY_OBSERVED


def test_documentation_sourced_universes_need_a_registered_rule():
    taken = captured_chain()
    documented = ExpectedContractUniverse(
        identities=frozenset(q.contract.canonical_id for q in taken.chain.quotes),
        source_kind=ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION,
        observed_at=AS_OF,
        documentation_evidence_id="nobody-registered-this",
    )
    resolved = resolve_expected_universe(documented)
    assert not resolved.established
    assert "not a registered documentation rule" in resolved.failure


def test_a_documented_universe_must_name_its_evidence():
    with pytest.raises(ValueError, match=r"(?i)registered evidence id"):
        ExpectedContractUniverse(
            identities=frozenset({"SPXW:2026-03-20:5000:call"}),
            source_kind=ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION,
            observed_at=AS_OF,
        )


# =============================================================================
# §7 -- a partial universe cannot establish full completeness
# =============================================================================


def test_a_partial_universe_cannot_report_measured_complete():
    """The regression. ``complete_for_request`` was on the type and read nowhere."""
    received = ("A", "B", "C")
    partial = ChainCompleteness(
        received_quote_count=3,
        received_oi_count=3,
        received_iv_count=3,
        received_greeks_count=3,
        expected_contract_ids=("A", "B"),
        received_contract_ids=received,
        expected_source="VENDOR_CONTRACT_LIST",
        expected_complete_for_request=False,
    )
    assert partial.status is CompletenessStatus.PARTIAL_UNIVERSE_ALL_LISTED_PRESENT
    assert not partial.status.implies_complete
    assert not partial.status.measured_against_the_whole_request
    # And it is still a measurement: it would have found a hole.
    assert partial.status.is_measured


def test_a_partial_universe_still_finds_a_missing_identity():
    partial = ChainCompleteness(
        received_quote_count=1,
        received_oi_count=1,
        received_iv_count=1,
        received_greeks_count=1,
        expected_contract_ids=("A", "B"),
        received_contract_ids=("A",),
        expected_source="VENDOR_CONTRACT_LIST",
        expected_complete_for_request=False,
    )
    assert partial.status is CompletenessStatus.PARTIAL_UNIVERSE_MISSING_IDENTITIES
    assert partial.status.has_missing_identities
    assert not partial.status.implies_complete


def test_the_same_identities_complete_for_the_request_do_report_complete():
    """The control: the flag is what changed, not the identities."""
    whole = ChainCompleteness(
        received_quote_count=2,
        received_oi_count=2,
        received_iv_count=2,
        received_greeks_count=2,
        expected_contract_ids=("A", "B"),
        received_contract_ids=("A", "B"),
        expected_source="VENDOR_CONTRACT_LIST",
        expected_complete_for_request=True,
    )
    assert whole.status is CompletenessStatus.MEASURED_COMPLETE
    assert whole.status.implies_complete


def test_a_partial_universe_reaches_the_chain_through_the_capture():
    paged = capture_with_universe(complete_for_request=False)
    assert paged.chain.completeness is not None
    assert not paged.chain.completeness.expected_complete_for_request
    assert not paged.chain.completeness.status.implies_complete


# =============================================================================
# §8 -- the capture operation owns the universe
# =============================================================================


def test_the_capture_owned_universe_reaches_fetch_chain_automatically():
    """No repetition of ``expected_contract_ids`` after declaring one."""
    owned = capture_with_universe()
    universe = owned.expected_universe

    assert owned.chain.completeness is not None
    assert owned.chain.completeness.expected_contract_ids == tuple(
        sorted(universe.identity_set)
    )
    assert owned.chain.completeness.expected_source == universe.source


def test_a_fetch_cannot_supply_a_second_universe():
    """Two universes on one fetch is a choice about which answer to get."""
    from src.adapters.artifact_store import InMemoryArtifactStore
    from tests.certification_fixtures import (
        documented_settlement_rule,
        durable_store,
        resolved_pipeline,
    )

    pipeline = resolved_pipeline()
    store = durable_store()
    taken = captured_chain()
    session = pipeline.capture_session(
        store=store,
        session_id="owns-a-universe",
        as_of=AS_OF,
        expected_universe=declared(taken),
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


def test_the_trusted_api_accepts_no_expected_universe():
    import inspect

    from src.config.pipeline import ThetaDataResearchPipeline

    parameters = set(
        inspect.signature(ThetaDataResearchPipeline.compute_trusted_gex).parameters
    )
    assert "expected_universe" not in parameters
    assert "expected_contract_ids" not in parameters


def test_trusted_replay_recovers_and_reverifies_the_capture_universe():
    owned = capture_with_universe()
    universe = owned.expected_universe

    recovered = owned.pipeline.recover_capture_artifacts(
        manifest=owned.manifest, store=owned.store, artifact_store=owned.artifacts
    )
    assert recovered.failures == ()
    assert recovered.expected_universe is not None
    assert recovered.expected_universe.universe_hash == universe.universe_hash

    owned.pipeline.compute_trusted_gex(owned.chain, **trusted_evidence(owned))


def test_a_universe_its_own_records_do_not_produce_fails_at_replay():
    """Declared honestly, refused at replay: the listing says something else."""
    owned = capture_with_universe(identities={"SPXW:2026-03-20:9999:call"})
    recovered = owned.pipeline.recover_capture_artifacts(
        manifest=owned.manifest, store=owned.store, artifact_store=owned.artifacts
    )
    assert recovered.failures
    assert any("does not follow from its source" in f for f in recovered.failures)
