"""A capture that actually contains what a session needs.

Every fixture here builds real records in a real store and derives the manifest
from them. Nothing is asserted: the payloads are vendor-shaped CSV, the endpoint
map comes from the records, and the manifest hash is computed rather than
written down.

That is the point. A fixture that states its own manifest hash is claiming
something about a session it never ran, and v2.1.4's tests did exactly that --
which is why the missing evidence checks passed for a year.

The payloads carry **three strikes**, not one. v2.1.5 characterised a
chain-level vendor convention from row zero, so a single agreeing contract stood
for the whole chain; a fixture with one row could not have caught that, and a
fixture is not allowed to be the reason a check looks stronger than it is.
"""

from __future__ import annotations

import pathlib
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from src.adapters.raw_store import (
    CaptureOrigin,
    FileRawStore,
    RawCaptureManifest,
)
from src.adapters.thetadata.capture_plan import capture_plan_for
from src.adapters.thetadata.endpoints import Endpoint, Tier
from src.adapters.transport import FakeTransport
from src.config.pipeline import ThetaDataResearchPipeline
from src.config.thetadata import parse_thetadata_config
from src.gex.sessions import eastern
from tests.pricing_evidence import resolved_settings

AS_OF = eastern(2026, 3, 17, 11, 0)
CAPTURED_AT = datetime(2026, 3, 17, 15, 0, tzinfo=UTC)

#: Raw capture is mandatory for capture readiness (v2.1.4 §6), and a pipeline
#: built from these settings writes real files when it fetches.
#:
#: The path is a per-session temporary directory, **not** ``artifacts/raw``.
#: Pointing it at the configured production destination is what put 573
#: fixture payloads into a v2.1.5 release archive: the tests captured into the
#: same namespace a real session would use, and nothing distinguished them
#: afterwards. A test that writes where production writes is a test that
#: contaminates the audit trail it is checking.
CAPTURE_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="gex-test-capture-"))
CAPTURE_SETTINGS = {
    "raw_capture_enabled": True,
    "raw_capture_path": str(CAPTURE_ROOT / "raw"),
}

#: The three contracts every endpoint reports on, so a chain-level comparison
#: has a chain to range over.
STRIKES = (4990, 5000, 5010)
VENDOR_INSTANT = "2026-03-17T11:00:00.000"
FIXTURE_INDEX_PRICE = 5000.25
#: What the greeks endpoint reports as the underlying it priced against when it
#: disagrees with the index print.
DISSENTING_UNDERLYING = 4999.00
#: The open interest the fixture payload carries, so tests can assert on the
#: value the validator reads back rather than on a number they chose.
FIXTURE_OPEN_INTEREST = 4200


def _underlyings(*, mismatched_underlying: bool, one_row_mismatched: bool):
    """What the greeks rows claim they priced against, strike by strike."""
    if mismatched_underlying:
        return [DISSENTING_UNDERLYING] * len(STRIKES)
    prices = [FIXTURE_INDEX_PRICE] * len(STRIKES)
    if one_row_mismatched:
        # Exactly one contract out of three. Enough to prove the chain is not
        # uniform, and not enough for a majority rule to paper over.
        prices[1] = DISSENTING_UNDERLYING
    return prices


def payloads(
    *, mismatched_underlying: bool = False, one_row_mismatched: bool = False
) -> dict[Endpoint, str]:
    """Vendor-shaped bodies, one per endpoint the plan requires.

    Column names match ``RESPONSE_FIELDS``, because the validator re-reads these
    to observe a field and a payload that does not look like the vendor's proves
    nothing.
    """
    underlyings = _underlyings(
        mismatched_underlying=mismatched_underlying,
        one_row_mismatched=one_row_mismatched,
    )
    quote_rows = "".join(
        f"{VENDOR_INSTANT},SPXW,2026-03-20,{strike},call,10,1,12.30,0,10,1,12.50,0\n"
        for strike in STRIKES
    )
    oi_rows = "".join(
        f"{VENDOR_INSTANT},SPXW,2026-03-20,{strike},call,{FIXTURE_OPEN_INTEREST}\n"
        for strike in STRIKES
    )
    first_order_rows = "".join(
        f"SPXW,2026-03-20,{strike},call,{VENDOR_INSTANT},12.30,12.50,0.52,-1.10,"
        f"0.88,0.31,0.0,0.0,0.1832,0.0,{VENDOR_INSTANT},{underlying}\n"
        for strike, underlying in zip(STRIKES, underlyings, strict=True)
    )
    second_order_rows = "".join(
        f"SPXW,2026-03-20,{strike},call,{VENDOR_INSTANT},12.30,12.50,0.0021,0.0,"
        f"0.0,0.0,0.0,0.1832,0.0,{VENDOR_INSTANT},{underlying}\n"
        for strike, underlying in zip(STRIKES, underlyings, strict=True)
    )
    return {
        Endpoint.OPTION_QUOTE_SNAPSHOT: (
            "timestamp,symbol,expiration,strike,right,bid_size,bid_exchange,bid,"
            "bid_condition,ask_size,ask_exchange,ask,ask_condition\n" + quote_rows
        ),
        Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT: (
            "timestamp,symbol,expiration,strike,right,open_interest\n" + oi_rows
        ),
        Endpoint.OPTION_GREEKS_FIRST_ORDER: (
            "symbol,expiration,strike,right,timestamp,bid,ask,delta,theta,vega,"
            "rho,epsilon,lambda,implied_vol,iv_error,underlying_timestamp,"
            "underlying_price\n" + first_order_rows
        ),
        Endpoint.INDEX_PRICE_SNAPSHOT: (
            "timestamp,symbol,index_price\n"
            f"{VENDOR_INSTANT},SPXW,{FIXTURE_INDEX_PRICE}\n"
        ),
        Endpoint.OPTION_GREEKS_SECOND_ORDER: (
            "symbol,expiration,strike,right,timestamp,bid,ask,gamma,vanna,charm,"
            "vomma,veta,implied_vol,iv_error,underlying_timestamp,"
            "underlying_price\n" + second_order_rows
        ),
    }


#: The agreeing set, for call sites that do not care about the variants.
PAYLOADS = payloads()


def vendor_transport(**variants: bool) -> FakeTransport:
    """A transport that answers every endpoint the plan requires."""
    transport = FakeTransport()
    for endpoint, body in payloads(**variants).items():
        transport.register_text(endpoint.value, body)
    return transport


def pipeline_from(**settings: Any) -> ThetaDataResearchPipeline:
    """A pipeline from an explicit settings mapping."""
    return ThetaDataResearchPipeline.from_config(
        parse_thetadata_config(settings), transport=vendor_transport()
    )


def resolved_pipeline(**overrides: Any) -> ThetaDataResearchPipeline:
    """A session whose pricing questions are answered by observed values."""
    variants = {
        name: overrides.pop(name)
        for name in ("mismatched_underlying", "one_row_mismatched")
        if name in overrides
    }
    settings = resolved_settings(**{**CAPTURE_SETTINGS, **overrides})
    return ThetaDataResearchPipeline.from_config(
        parse_thetadata_config(settings), transport=vendor_transport(**variants)
    )


def unresolved_pipeline(**overrides: Any) -> ThetaDataResearchPipeline:
    """The default: vendor conventions undocumented."""
    variants = {
        name: overrides.pop(name)
        for name in ("mismatched_underlying", "one_row_mismatched")
        if name in overrides
    }
    settings = {**CAPTURE_SETTINGS}
    settings.update(overrides)
    return ThetaDataResearchPipeline.from_config(
        parse_thetadata_config(settings), transport=vendor_transport(**variants)
    )


def plan_for(pipeline: ThetaDataResearchPipeline | None = None):
    built = pipeline if pipeline is not None else resolved_pipeline()
    return capture_plan_for(
        pricing_mode=built.pricing_mode,
        vendor_gamma_policy=built.vendor_gamma_policy,
        underlying_price_source=built.config.underlying_price_source,
        tier=Tier(built.config.tier),
    )


def durable_store(root: pathlib.Path | None = None) -> FileRawStore:
    """A store a paid session's evidence could actually survive in.

    ``InMemoryRawStore`` is deliberately *not* this: it is a working store that
    forgets everything when the process exits, and v2.1.5 let it satisfy
    ``READY_FOR_RAW_CAPTURE_ONLY``.
    """
    base = (
        root
        if root is not None
        else pathlib.Path(tempfile.mkdtemp(prefix="gex-durable-"))
    )
    return FileRawStore(base / "raw")


def build_capture(
    *,
    session_id: str = "session",
    endpoints: tuple[Endpoint, ...] | None = None,
    pipeline: ThetaDataResearchPipeline | None = None,
    store: Any = None,
    http_status: int = 200,
    mismatched_underlying: bool = False,
    one_row_mismatched: bool = False,
) -> tuple[Any, RawCaptureManifest]:
    """A real capture: bytes in a store, and a manifest derived from them.

    The payload variants are taken from the *pipeline's own transport* when one
    is supplied, so a capture and a fetch through the same pipeline always see
    the same bytes.
    """
    built = pipeline if pipeline is not None else resolved_pipeline()
    plan = plan_for(built)
    wanted = endpoints if endpoints is not None else plan.required_endpoints
    if mismatched_underlying or one_row_mismatched:
        bodies = payloads(
            mismatched_underlying=mismatched_underlying,
            one_row_mismatched=one_row_mismatched,
        )
    else:
        bodies = _transport_payloads(built)

    # Durable by default. A paid session's evidence cannot live in a process,
    # and a fixture that certifies against a volatile store is testing a
    # configuration the release refuses.
    raw_store = store if store is not None else durable_store()
    # Opened through the pipeline, so the records carry the same capture-time
    # stamps a real session would write. A fixture that skipped them would be
    # testing a capture the production path cannot produce.
    session = built.capture_session(
        store=raw_store,
        session_id=session_id,
        as_of=AS_OF,
        # Fixture bytes, labelled as fixture bytes. Nothing here is evidence
        # about the vendor, and the manifest hash says so.
        capture_origin=CaptureOrigin.OFFLINE_FIXTURE,
    )
    mark = session.mark()
    for endpoint in wanted:
        session.capture(
            endpoint=endpoint.value,
            query_params={"root": "SPXW", "session": session_id},
            payload=bodies[endpoint],
            request_started_at=CAPTURED_AT,
            response_received_at=CAPTURED_AT,
            http_status=http_status,
        )
    manifest = RawCaptureManifest.from_session(
        session,
        since=mark,
        capture_plan_fingerprint=plan.fingerprint,
        pipeline_fingerprint=built.fingerprint(),
    )
    return raw_store, manifest


def _transport_payloads(pipeline: ThetaDataResearchPipeline) -> dict[Endpoint, str]:
    """What this pipeline's transport actually answers with."""
    transport = pipeline.runtime.client.transport
    bodies: dict[Endpoint, str] = {}
    for endpoint in Endpoint:
        text = getattr(transport, "registered_text", lambda _: None)(endpoint.value)
        if text is not None:
            bodies[endpoint] = text
    return bodies or payloads()


@dataclass(frozen=True, slots=True)
class CapturedChain:
    """One fetch: the chain, the store it landed in, and its own manifest."""

    chain: Any
    store: Any
    manifest: RawCaptureManifest
    pipeline: ThetaDataResearchPipeline


def captured_chain(
    pipeline: ThetaDataResearchPipeline | None = None,
    *,
    store: Any = None,
    expected_universe: Any = None,
) -> CapturedChain:
    """Fetch a chain and keep the manifest of the responses that built it.

    The manifest is derived the same way ``_assemble`` derives the one it puts
    in ``chain.meta``, so the two agree by construction rather than by a fixture
    saying they do.

    ``expected_universe`` is declared on the *session*, not on the calculation:
    since v2.1.8 the universe a replay is measured against is the one the
    capture operation was opened with.
    """
    built = pipeline if pipeline is not None else resolved_pipeline()
    raw_store = store if store is not None else durable_store()
    session = built.capture_session(
        store=raw_store,
        session_id=f"fetch-{id(raw_store):x}",
        as_of=AS_OF,
        expected_universe=expected_universe,
    )
    mark = session.mark()
    chain = built.fetch_chain(as_of=AS_OF, capture=session)
    manifest = RawCaptureManifest.from_session(
        session,
        since=mark,
        capture_plan_fingerprint=built.capture_plan.fingerprint,
        pipeline_fingerprint=built.fingerprint(),
    )
    return CapturedChain(
        chain=chain, store=raw_store, manifest=manifest, pipeline=built
    )


#: The id a fixture references. Registered against a document that really
#: exists in this repository, so the content hash is a hash of real bytes rather
#: than a plausible-looking constant.
FIXTURE_OI_EVIDENCE_ID = "fixture-oi-settlement-convention"


def register_fixture_documentation_rule():
    """Put one registered rule in the registry, bound to a real document.

    Since v2.1.8 a documentation *reference* authorizes nothing: the resolver
    looks the id up in a registry and uses the rule it finds, so
    ``reference="lol"`` -- which satisfied v2.1.7 -- resolves to nothing at all.
    Registering is a deliberate act, and this is the one place a test performs
    it.
    """
    import pathlib

    from src.adapters.evidence_resolvers import (
        DOCUMENTATION_RULES,
        DocumentationRule,
        content_hash_of,
    )

    if FIXTURE_OI_EVIDENCE_ID not in DOCUMENTATION_RULES:
        DOCUMENTATION_RULES.register(
            DocumentationRule(
                evidence_id=FIXTURE_OI_EVIDENCE_ID,
                document_reference="tests/fixtures/vendor_conventions.md",
                document_content_hash=content_hash_of(
                    pathlib.Path("tests/fixtures/vendor_conventions.md")
                ),
                rule_identifier="open_interest_settles_on_the_prior_session",
                effective_from=date(2020, 1, 1),
                derivation_version="fixture/1",
                normalized_value="prior_session",
                observed_on=date(2026, 8, 1),
            )
        )
    return DOCUMENTATION_RULES.get(FIXTURE_OI_EVIDENCE_ID)


def documented_oi_date(chain_date: date | None = None):
    """Settlement-date evidence strong enough for a trusted calculation.

    ``CALLER_ASSUMPTION`` -- the honest state of this repository today -- blocks
    a trusted GEX by design, so a fixture that wants to exercise the *rest* of
    the trusted path has to state a stronger kind explicitly, and since v2.1.8
    has to register the document behind it. Doing both here, in one named
    function, keeps the concession visible: nothing in the production
    configuration registers a ThetaData settlement rule, and OD-26 is open.
    """
    from src.adapters.open_interest import EvidenceKind, OpenInterestAsOfEvidence

    register_fixture_documentation_rule()
    return OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="vendor_field",
        evidence_kind=EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION,
        reference=FIXTURE_OI_EVIDENCE_ID,
        chain_date=chain_date or AS_OF.date(),
    )


def trusted_evidence(taken: CapturedChain, **overrides: Any) -> dict[str, Any]:
    """The primitive evidence ``compute_trusted_gex`` derives its authority from.

    Deliberately a mapping of raw inputs rather than a context object: since
    v2.1.7 the trusted API takes evidence and does the deriving itself, because
    a derived verdict is one a caller can construct.
    """
    payload: dict[str, Any] = {
        "manifest": taken.manifest,
        "store": taken.store,
        "open_interest_provenance": verified_oi(taken.store, taken.manifest),
        "open_interest_as_of_evidence": documented_oi_date(),
    }
    payload.update(overrides)
    return payload


def context_for(taken: CapturedChain, **overrides: Any):
    """The verified evidence report for the chain ``taken`` produced.

    Built from one fetch, so the chain and the manifest describe the same
    responses. Deriving them from separate captures would make every gate test
    pass for the uninteresting reason that two captures never share a hash.

    Since v2.1.7 this is a *report*: it is what the trusted path produces, not
    what it accepts.
    """
    from src.adapters.certification import build_verified_calculation_context

    payload: dict[str, Any] = {
        "pipeline": taken.pipeline,
        "manifest": taken.manifest,
        "store": taken.store,
        # Derived, not supplied: since v2.1.8 the trusted path reads the spot's
        # timestamp out of the verified index record and its tolerance out of
        # the pipeline configuration. This mirrors that so a context built here
        # says what a trusted calculation would say.
        "spot": taken.pipeline.derive_spot_provenance(
            manifest=taken.manifest, store=taken.store
        ),
        "open_interest": verified_oi(taken.store, taken.manifest),
        "open_interest_as_of_evidence": documented_oi_date(),
    }
    payload.update(overrides)
    return build_verified_calculation_context(**payload)


def _observation(store: Any, manifest: RawCaptureManifest, endpoint, field_path):
    from src.adapters.certification import AdapterValidator
    from src.adapters.errors import ThetaDataProvenanceError

    try:
        return AdapterValidator.observe_field(
            manifest=manifest, store=store, endpoint=endpoint, field_path=field_path
        )
    except ThetaDataProvenanceError:
        return None


def verified_oi(
    store: Any = None,
    manifest: RawCaptureManifest | None = None,
    *,
    chain_date: date | None = None,
):
    from src.adapters.certification import OpenInterestProvenance

    observation = (
        _observation(
            store,
            manifest,
            Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT,
            "open_interest",
        )
        if store is not None and manifest is not None
        else None
    )
    return OpenInterestProvenance(
        as_of=date(2026, 3, 16),
        source="vendor_field",
        chain_date=chain_date or AS_OF.date(),
        observation=observation,
    )


def verified_spot(store: Any = None, manifest: RawCaptureManifest | None = None):
    from datetime import timedelta

    from src.adapters.certification import SpotProvenance, SpotSource

    observation = (
        _observation(store, manifest, Endpoint.INDEX_PRICE_SNAPSHOT, "index_price")
        if store is not None and manifest is not None
        else None
    )
    timestamp = (
        observation.source_timestamp
        if observation is not None and observation.source_timestamp is not None
        else AS_OF - timedelta(milliseconds=200)
    )
    return SpotProvenance(
        source=SpotSource.VENDOR_INDEX_SNAPSHOT,
        timestamp=timestamp,
        tolerance_seconds=1.0,
        observation=observation,
    )


def readiness(**overrides: Any):
    """Assess readiness with a real store and, when asked, a real capture.

    The default store is *durable*. A volatile one is a legitimate thing to pass
    deliberately -- and it must not be capture-ready, which is what
    ``test_an_in_memory_store_cannot_be_capture_ready`` pins down.
    """
    from src.adapters.certification import assess_readiness

    payload: dict[str, Any] = {
        "pipeline": resolved_pipeline(),
        "as_of": AS_OF,
        "open_interest": verified_oi(),
        "spot": verified_spot(),
        "raw_store": durable_store(),
    }
    payload.update(overrides)
    return assess_readiness(**payload)
