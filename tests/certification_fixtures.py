"""A capture that actually contains what a session needs.

Every fixture here builds real records in a real store and derives the manifest
from them. Nothing is asserted: the payloads are vendor-shaped CSV, the endpoint
map comes from the records, and the manifest hash is computed rather than
written down.

That is the point. A fixture that states its own manifest hash is claiming
something about a session it never ran, and v2.1.4's tests did exactly that --
which is why the missing evidence checks passed for a year.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from src.adapters.raw_store import CaptureSession, InMemoryRawStore, RawCaptureManifest
from src.adapters.thetadata.capture_plan import capture_plan_for
from src.adapters.thetadata.endpoints import Endpoint, Tier
from src.adapters.transport import FakeTransport
from src.config.pipeline import ThetaDataResearchPipeline
from src.config.thetadata import parse_thetadata_config
from src.gex.sessions import eastern
from tests.pricing_evidence import resolved_settings

AS_OF = eastern(2026, 3, 17, 11, 0)
CAPTURED_AT = datetime(2026, 3, 17, 15, 0, tzinfo=UTC)

#: Raw capture is mandatory for capture readiness (v2.1.4 §6).
CAPTURE_SETTINGS = {"raw_capture_enabled": True, "raw_capture_path": "artifacts/raw"}

#: Vendor-shaped bodies, one per endpoint the plan requires. Column names match
#: ``RESPONSE_FIELDS``, because the validator re-reads these to observe a field
#: and a payload that does not look like the vendor's proves nothing.
PAYLOADS: dict[Endpoint, str] = {
    Endpoint.OPTION_QUOTE_SNAPSHOT: (
        "timestamp,symbol,expiration,strike,right,bid_size,bid_exchange,bid,"
        "bid_condition,ask_size,ask_exchange,ask,ask_condition\n"
        "2026-03-17T11:00:00.000,SPXW,2026-03-20,5000,call,10,1,12.30,0,"
        "10,1,12.50,0\n"
    ),
    Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT: (
        "timestamp,symbol,expiration,strike,right,open_interest\n"
        "2026-03-17T11:00:00.000,SPXW,2026-03-20,5000,call,4200\n"
    ),
    Endpoint.OPTION_GREEKS_FIRST_ORDER: (
        "symbol,expiration,strike,right,timestamp,bid,ask,delta,theta,vega,rho,"
        "epsilon,lambda,implied_vol,iv_error,underlying_timestamp,"
        "underlying_price\n"
        "SPXW,2026-03-20,5000,call,2026-03-17T11:00:00.000,12.30,12.50,0.52,"
        "-1.10,0.88,0.31,0.0,0.0,0.1832,0.0,2026-03-17T11:00:00.000,5000.25\n"
    ),
    Endpoint.INDEX_PRICE_SNAPSHOT: (
        "timestamp,symbol,index_price\n2026-03-17T11:00:00.000,SPXW,5000.25\n"
    ),
    Endpoint.OPTION_GREEKS_SECOND_ORDER: (
        "symbol,expiration,strike,right,timestamp,bid,ask,gamma,vanna,charm,"
        "vomma,veta,implied_vol,iv_error,underlying_timestamp,underlying_price\n"
        "SPXW,2026-03-20,5000,call,2026-03-17T11:00:00.000,12.30,12.50,0.0021,"
        "0.0,0.0,0.0,0.0,0.1832,0.0,2026-03-17T11:00:00.000,5000.25\n"
    ),
}

#: The open interest the fixture payload carries, so tests can assert on the
#: value the validator reads back rather than on a number they chose.
FIXTURE_OPEN_INTEREST = 4200
FIXTURE_INDEX_PRICE = 5000.25


def vendor_transport() -> FakeTransport:
    """A transport that answers every endpoint the plan requires.

    Every payload is the vendor's documented column set, because the validator
    re-reads these to observe a field -- a body that does not look like the
    vendor's would prove nothing about the code that reads the vendor's.
    """
    transport = FakeTransport()
    for endpoint, body in PAYLOADS.items():
        transport.register_text(endpoint.value, body)
    return transport


def resolved_pipeline(**overrides: Any) -> ThetaDataResearchPipeline:
    """A session whose pricing questions are answered by observed values."""
    settings = resolved_settings(**{**CAPTURE_SETTINGS, **overrides})
    return ThetaDataResearchPipeline.from_config(
        parse_thetadata_config(settings), transport=vendor_transport()
    )


def unresolved_pipeline(**overrides: Any) -> ThetaDataResearchPipeline:
    """The default: vendor conventions undocumented."""
    settings = {**CAPTURE_SETTINGS}
    settings.update(overrides)
    return ThetaDataResearchPipeline.from_config(
        parse_thetadata_config(settings), transport=vendor_transport()
    )


def plan_for(pipeline: ThetaDataResearchPipeline | None = None):
    built = pipeline if pipeline is not None else resolved_pipeline()
    return capture_plan_for(
        pricing_mode=built.pricing_mode,
        vendor_gamma_policy=built.vendor_gamma_policy,
        underlying_price_source=built.config.underlying_price_source,
        tier=Tier(built.config.tier),
    )


def build_capture(
    *,
    session_id: str = "session",
    endpoints: tuple[Endpoint, ...] | None = None,
    pipeline: ThetaDataResearchPipeline | None = None,
    store: InMemoryRawStore | None = None,
) -> tuple[InMemoryRawStore, RawCaptureManifest]:
    """A real capture: bytes in a store, and a manifest derived from them."""
    built = pipeline if pipeline is not None else resolved_pipeline()
    plan = plan_for(built)
    wanted = endpoints if endpoints is not None else plan.required_endpoints

    raw_store = store if store is not None else InMemoryRawStore()
    session = CaptureSession(store=raw_store, session_id=session_id)
    mark = session.mark()
    for endpoint in wanted:
        session.capture(
            endpoint=endpoint.value,
            query_params={"root": "SPXW", "session": session_id},
            payload=PAYLOADS[endpoint],
            request_started_at=CAPTURED_AT,
            response_received_at=CAPTURED_AT,
            http_status=200,
        )
    manifest = RawCaptureManifest.from_session(
        session,
        since=mark,
        capture_plan_fingerprint=plan.fingerprint,
        pipeline_fingerprint=built.fingerprint(),
    )
    return raw_store, manifest


def verified_oi(chain_date: date | None = None):
    from src.adapters.certification import OpenInterestProvenance

    return OpenInterestProvenance(
        as_of=date(2026, 3, 16),
        source="vendor_field",
        chain_date=chain_date or AS_OF.date(),
    )


def verified_spot():
    from datetime import timedelta

    from src.adapters.certification import SpotProvenance, SpotSource

    return SpotProvenance(
        source=SpotSource.VENDOR_INDEX_SNAPSHOT,
        timestamp=AS_OF - timedelta(milliseconds=200),
        tolerance_seconds=1.0,
    )


def readiness(**overrides: Any):
    """Assess readiness with a real store and, when asked, a real capture."""
    from src.adapters.certification import assess_readiness

    payload: dict[str, Any] = {
        "pipeline": resolved_pipeline(),
        "as_of": AS_OF,
        "open_interest": verified_oi(),
        "spot": verified_spot(),
        "raw_store": InMemoryRawStore(),
    }
    payload.update(overrides)
    return assess_readiness(**payload)
