"""Offline end-to-end run: vendor fixture -> snapshot metadata.

    ThetaData fixture -> parser -> normalized contracts -> validation
    -> GEX aggregation -> zero-gamma -> confidence -> persisted metadata

Everything runs through the deterministic fake transport. No network, no Theta
Terminal, no credential. The point is to exercise the seams the unit tests
stub out: that the parsed rows really do join, that the joined chain really does
validate, and that the resulting snapshot really does carry the provenance a
later audit would need.
"""

from __future__ import annotations

import json
import pathlib
from datetime import date, timedelta

import pytest

from src.adapters.raw_store import CaptureSession, InMemoryRawStore
from src.adapters.thetadata.client import (
    ChainRequest,
    GreeksParameters,
    ThetaDataClient,
    ThetaDataSettings,
)
from src.adapters.thetadata.endpoints import Tier
from src.adapters.transport import (
    FakeTransport,
    HttpResponse,
    RetryingTransport,
    RetryPolicy,
)
from src.config.schema import load_config
from src.domain.gex import IVConvention
from src.domain.iv import IVSource
from src.gex.engine import compute_gex_snapshot
from src.gex.sessions import eastern

pytestmark = pytest.mark.integration

FIXTURES = (
    pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "vendor" / "thetadata"
)
CONFIG_DIR = pathlib.Path(__file__).resolve().parents[2] / "config"
AS_OF = eastern(2026, 3, 17, 11, 0)
SPOT = 5000.25


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_transport() -> FakeTransport:
    transport = FakeTransport()
    transport.register_text("/snapshot/quote", fixture("quotes.csv"))
    transport.register_text("/snapshot/open_interest", fixture("open_interest.csv"))
    transport.register_text("/greeks/first_order", fixture("greeks_first_order.csv"))
    transport.register_text("/greeks/second_order", fixture("greeks_second_order.csv"))
    return transport


def make_client(tier: Tier = Tier.STANDARD, **kwargs) -> ThetaDataClient:
    kwargs.setdefault("transport", make_transport())
    return ThetaDataClient(
        settings=ThetaDataSettings(tier=tier),
        greeks=GreeksParameters(rate_value=4.2, annual_dividend=1.3),
        clock=lambda: AS_OF,
        **kwargs,
    )


def fetch(client: ThetaDataClient, **kwargs):
    return client.fetch_chain(
        ChainRequest(symbol="SPXW", max_dte=60),
        as_of=AS_OF,
        spot=SPOT,
        spot_timestamp=AS_OF - timedelta(milliseconds=500),
        open_interest_as_of=date(2026, 3, 16),
        risk_free_rate=0.042,
        dividend_yield=0.013,
        iv_source=IVSource.VENDOR_DEFAULT_IV,
        **kwargs,
    )


# --- The full pipeline ------------------------------------------------------


def test_standard_tier_pipeline_produces_a_complete_snapshot():
    """The $80/mo path: IV but no vendor gamma, everything else derived."""
    config = load_config(CONFIG_DIR / "research.yaml").engine
    chain = fetch(make_client(Tier.STANDARD))
    snapshot = compute_gex_snapshot(chain, config)

    assert snapshot.contract_count == 20
    assert snapshot.total_unsigned_gex > 0.0
    assert len(snapshot.strikes) == 5
    assert len(snapshot.zero_gamma) == 3
    assert snapshot.meta["vendor_gamma_count"] == 0
    assert snapshot.meta["shadow_gamma_count"] == 20
    assert snapshot.validation.rejected == 0
    assert snapshot.validation.total == 20


def test_pro_tier_pipeline_also_pulls_vendor_gamma():
    chain = fetch(make_client(Tier.PRO))
    assert all(q.gamma is not None for q in chain.quotes)


def test_pipeline_carries_provenance_all_the_way_to_the_snapshot():
    """A number nobody can trace is a number nobody can audit."""
    config = load_config(CONFIG_DIR / "research.yaml")
    snapshot = compute_gex_snapshot(fetch(make_client()), config.engine)
    payload = snapshot.as_dict()

    assert payload["source"] == "thetadata"
    assert payload["config_fingerprint"] == config.fingerprint
    assert payload["model_fingerprint"] == snapshot.model_spec.fingerprint()
    assert payload["model_spec"]["risk_free_rate"] == pytest.approx(0.042)
    assert payload["validation"]["total"] == 20
    assert payload["chain_universe"]["included_contract_count"] == 20


def test_snapshot_metadata_is_json_serialisable_for_persistence():
    snapshot = compute_gex_snapshot(fetch(make_client()))
    encoded = json.dumps(snapshot.as_dict(), default=str)
    restored = json.loads(encoded)
    assert restored["contract_count"] == 20
    assert restored["confidence"]["score"] >= 0.0


def test_effective_request_parameters_survive_into_the_chain():
    chain = fetch(make_client())
    recorded = chain.meta["thetadata_request"]
    assert recorded["greeks"]["rate_value"] == 4.2
    assert recorded["greeks"]["annual_dividend"] == 1.3
    assert "hunter2" not in json.dumps(recorded)


# --- Timestamp integrity across the seam ------------------------------------


def test_vendor_timestamps_reach_the_confidence_score_unmodified():
    """The fixture's underlying print is half a second behind its quotes. That
    drift must survive parsing, joining and validation to be scored.
    """
    snapshot = compute_gex_snapshot(fetch(make_client()))
    lag = next(
        c for c in snapshot.confidence.components if c.name == "vendor_lag_alert"
    )
    assert "0.50s" in lag.detail


def test_a_stale_vendor_clock_lowers_confidence_end_to_end():
    stale_quotes = fixture("quotes.csv").replace(
        "2026-03-17T11:00:00.000", "2026-03-17T10:55:00.000"
    )
    transport = make_transport()
    transport.register_text("/snapshot/quote", stale_quotes)
    stale = compute_gex_snapshot(fetch(make_client(transport=transport)))
    fresh = compute_gex_snapshot(fetch(make_client()))
    assert stale.confidence.value < fresh.confidence.value


def test_a_future_vendor_clock_is_a_hard_failure_end_to_end():
    future_quotes = fixture("quotes.csv").replace(
        "2026-03-17T11:00:00.000", "2026-03-17T11:30:00.000"
    )
    transport = make_transport()
    transport.register_text("/snapshot/quote", future_quotes)
    snapshot = compute_gex_snapshot(fetch(make_client(transport=transport)))
    assert snapshot.confidence.value == 0.0
    assert "future_timestamp_penalty" in snapshot.confidence.hard_failures


# --- Degraded vendor responses ---------------------------------------------


def test_a_partial_chain_is_processed_and_the_shortfall_is_visible():
    transport = make_transport()
    transport.register_text("/snapshot/quote", fixture("quotes_partial_chain.csv"))
    snapshot = compute_gex_snapshot(fetch(make_client(transport=transport)))
    assert snapshot.contract_count == 2
    assert snapshot.chain_universe.total_contract_count == 2


def test_an_empty_chain_produces_a_warned_empty_snapshot():
    transport = make_transport()
    transport.register_text("/snapshot/quote", fixture("empty.csv"))
    snapshot = compute_gex_snapshot(fetch(make_client(transport=transport)))
    assert snapshot.contract_count == 0
    assert "no usable contracts in snapshot" in snapshot.warnings


def test_a_vendor_error_body_stops_the_pipeline_rather_than_producing_zeros():
    """Silently returning an empty snapshot would look like a quiet market."""
    from src.adapters.thetadata.client import ThetaDataError

    transport = make_transport()
    transport.register_text("/snapshot/quote", fixture("vendor_error.json"))
    with pytest.raises(ThetaDataError, match="vendor error"):
        fetch(make_client(transport=transport))


def test_a_missing_column_stops_the_pipeline():
    from src.adapters.thetadata.client import ThetaDataSchemaError

    transport = make_transport()
    transport.register_text("/snapshot/quote", fixture("quotes_missing_column.csv"))
    with pytest.raises(ThetaDataSchemaError, match="missing required column"):
        fetch(make_client(transport=transport))


def test_a_corrupt_row_is_dropped_while_the_rest_of_the_chain_survives():
    """One bad record must cost one record, not the whole snapshot."""
    corrupted = fixture("open_interest.csv").replace(
        "4900.00,call,4200", "4900.00,call,-9999"
    )
    transport = make_transport()
    transport.register_text("/snapshot/open_interest", corrupted)
    snapshot = compute_gex_snapshot(fetch(make_client(transport=transport)))
    assert snapshot.validation.rejected == 2  # one per expiry
    assert snapshot.contract_count == 18
    assert snapshot.total_unsigned_gex > 0.0


def test_transient_vendor_failure_is_retried_transparently():
    inner = make_transport()
    inner.register_sequence(
        "/snapshot/quote",
        [
            HttpResponse(status_code=503, text="temporarily unavailable"),
            HttpResponse(status_code=200, text=fixture("quotes.csv")),
        ],
    )
    retrying = RetryingTransport(
        inner, policy=RetryPolicy(max_retries=2), sleep=lambda _: None
    )
    snapshot = compute_gex_snapshot(fetch(make_client(transport=retrying)))
    assert snapshot.contract_count == 20


# --- Raw capture ------------------------------------------------------------


def test_the_whole_pull_is_captured_for_audit():
    store = InMemoryRawStore()
    session = CaptureSession(store=store, session_id="integration")
    fetch(make_client(), capture=session)

    manifest = session.manifest()
    assert len(manifest["records"]) == 3  # Standard tier: no second-order call
    endpoints = {record["endpoint"] for record in manifest["records"]}
    assert endpoints == {
        "/v3/option/snapshot/quote",
        "/v3/option/snapshot/open_interest",
        "/v3/option/snapshot/greeks/first_order",
    }
    for record in manifest["records"]:
        assert record["payload_hash"]
        assert record["parser_version"]
        assert record["http_status"] == 200


def test_captured_payloads_can_be_replayed_into_the_same_snapshot():
    """The audit trail is the raw payload, and it has to be usable as one."""
    store = InMemoryRawStore()
    session = CaptureSession(store=store, session_id="replayable")
    original = compute_gex_snapshot(fetch(make_client(), capture=session))

    replay_transport = FakeTransport()
    for record in session.captured:
        replay_transport.register_text(
            record.endpoint, store.get_payload(record.record_id)
        )
    replayed = compute_gex_snapshot(fetch(make_client(transport=replay_transport)))
    assert replayed.output_hash() == original.output_hash()


# --- Config-driven run ------------------------------------------------------


def test_research_config_drives_the_pipeline():
    loaded = load_config(CONFIG_DIR / "research.yaml")
    snapshot = compute_gex_snapshot(fetch(make_client()), loaded.engine)
    assert snapshot.config_fingerprint == loaded.fingerprint
    assert tuple(z.convention for z in snapshot.zero_gamma) == (
        IVConvention.STICKY_STRIKE,
        IVConvention.FROZEN_IV,
        IVConvention.STICKY_MONEYNESS,
    )
    assert not snapshot.confidence.calibrated  # market thresholds are sentinels
