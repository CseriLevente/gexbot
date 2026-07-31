"""Every number should be able to show what produced it and what allowed it.

Five v2.1.2 gaps, all about a result that could not account for itself:

* **§12** The pipeline knew which compatibility decision permitted a
  calculation, and the GEX output did not carry it. A snapshot could not say
  under which assumptions it was computed.
* **§13** Raw payloads were captured and normalized chains were produced, and
  nothing linked the two. Reconstructing which bytes produced which number --
  the entire point of capturing raw payloads -- meant guessing from filenames.
* **§14** Both versions lagged the behaviour they describe.
* **§16** The BOM was stripped for header validation but not for parsing, so a
  BOM'd response validated and then lost its first column.
* **§17** Static completeness never consulted ``source.is_available``.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.adapters.raw_store import (
    PARSER_VERSION,
    CaptureSession,
    InMemoryRawStore,
    RawCaptureManifest,
)
from src.adapters.thetadata.client import (
    normalize_response_body,
    parse_csv,
    validate_csv_body,
)
from src.adapters.transport import FakeTransport, HttpResponse
from src.config.thetadata import ThetaDataRuntime, parse_thetadata_config
from src.domain.model_spec import MODEL_VERSION, DividendSource, ModelSpec, RateSource
from src.gex.engine import compute_gex_snapshot
from src.gex.formulas import static_model_missing_inputs
from src.gex.sessions import eastern

AS_OF = eastern(2026, 3, 17, 11, 0)


def runtime(tmp_path=None, **overrides):
    from tests.unit.test_thetadata_runtime import csv_response

    settings = dict(overrides)
    if tmp_path is not None:
        settings.update(
            raw_capture_enabled=True, raw_capture_path=str(tmp_path / "raw")
        )
    return ThetaDataRuntime.from_config(
        parse_thetadata_config(settings),
        transport=FakeTransport(default=csv_response()),
        clock=lambda: AS_OF,
    )


def fetch(rt):
    return rt.fetch_chain(
        as_of=AS_OF,
        spot=5000.25,
        spot_timestamp=AS_OF - timedelta(milliseconds=500),
        open_interest_as_of=date(2026, 3, 16),
    )


# =============================================================================
# §13 -- normalized snapshots point at the raw records they came from
# =============================================================================


def test_a_captured_chain_links_every_endpoint(tmp_path):
    """Standard tier fetches quotes, OI and first-order greeks."""
    chain = fetch(runtime(tmp_path))
    manifest = chain.meta["raw_capture_manifest"]
    assert manifest["capture_enabled"] is True
    assert manifest["record_count"] >= 3
    assert len(manifest["record_ids"]) == manifest["record_count"]


def test_the_manifest_carries_payload_hashes(tmp_path):
    manifest = fetch(runtime(tmp_path)).meta["raw_capture_manifest"]
    assert manifest["payload_hashes"]
    assert len(manifest["payload_hashes"]) == manifest["record_count"]


def test_the_snapshot_can_locate_its_source_payloads(tmp_path):
    rt = runtime(tmp_path)
    chain = fetch(rt)
    for record_id in chain.meta["raw_capture_manifest"]["record_ids"]:
        assert rt.client.raw_store.get_payload(record_id)


def test_the_manifest_hash_is_deterministic():
    first = RawCaptureManifest(
        session_id="s1", record_ids=("a", "b"), payload_hashes=("h1", "h2")
    )
    second = RawCaptureManifest(
        session_id="s1", record_ids=("b", "a"), payload_hashes=("h2", "h1")
    )
    assert first.manifest_hash == second.manifest_hash


def test_a_different_source_set_changes_the_manifest_hash():
    base = RawCaptureManifest(
        session_id="s1", record_ids=("a",), payload_hashes=("h1",)
    )
    other = RawCaptureManifest(
        session_id="s1", record_ids=("a", "b"), payload_hashes=("h1", "h2")
    )
    assert base.manifest_hash != other.manifest_hash


def test_disabled_capture_is_recorded_explicitly():
    """Absent metadata reads the same as forgotten metadata."""
    chain = fetch(runtime())
    manifest = chain.meta["raw_capture_manifest"]
    assert manifest["capture_enabled"] is False
    assert manifest["record_ids"] == []


def test_the_manifest_reaches_the_gex_snapshot(tmp_path):
    produced = compute_gex_snapshot(fetch(runtime(tmp_path)))
    assert "raw_capture_manifest" in produced.meta


def test_the_manifest_enters_the_replay_hash(tmp_path):
    produced = compute_gex_snapshot(fetch(runtime(tmp_path)))
    assert "raw_capture_manifest" in produced.hash_payload()["meta"]


def test_a_session_manifest_lists_what_it_captured():
    session = CaptureSession(store=InMemoryRawStore(), session_id="s1")
    for _ in range(2):
        session.capture(
            endpoint="/v3/option/snapshot/quote",
            query_params={"symbol": "SPXW"},
            payload="x",
            request_started_at=AS_OF,
            response_received_at=AS_OF,
            http_status=200,
        )
    manifest = RawCaptureManifest.from_session(session)
    assert manifest.session_id == "s1"
    assert len(manifest.record_ids) == 2


# =============================================================================
# §12 -- the compatibility decision travels with the number
# =============================================================================


def test_pipeline_metadata_reaches_the_chain(tmp_path):
    from src.config.pipeline import ThetaDataResearchPipeline
    from tests.unit.test_thetadata_runtime import csv_response

    pipeline = ThetaDataResearchPipeline.from_config(
        parse_thetadata_config({}),
        transport=FakeTransport(default=csv_response()),
        clock=lambda: AS_OF,
    )
    chain = pipeline.runtime.fetch_chain(
        as_of=AS_OF,
        spot=5000.25,
        spot_timestamp=AS_OF - timedelta(milliseconds=500),
        open_interest_as_of=date(2026, 3, 16),
        pipeline=pipeline,
    )
    assert "pipeline" in chain.meta
    assert "pricing_compatibility" in chain.meta["pipeline"]


def test_pipeline_metadata_reaches_the_gex_snapshot(tmp_path):
    from src.config.pipeline import ThetaDataResearchPipeline
    from tests.unit.test_thetadata_runtime import csv_response

    pipeline = ThetaDataResearchPipeline.from_config(
        parse_thetadata_config({}),
        transport=FakeTransport(default=csv_response()),
        clock=lambda: AS_OF,
    )
    chain = pipeline.runtime.fetch_chain(
        as_of=AS_OF,
        spot=5000.25,
        spot_timestamp=AS_OF - timedelta(milliseconds=500),
        open_interest_as_of=date(2026, 3, 16),
        pipeline=pipeline,
    )
    produced = compute_gex_snapshot(chain)
    assert "pipeline" in produced.meta
    assert produced.meta["pipeline"]["pipeline_fingerprint"]


def test_human_prose_alone_does_not_change_the_hash():
    from dataclasses import replace as dc_replace

    from src.synthetic.chains import build_synthetic_chain

    produced = compute_gex_snapshot(build_synthetic_chain())
    baseline = produced.output_hash()
    reworded = dc_replace(
        produced,
        confidence=dc_replace(
            produced.confidence,
            components=tuple(
                dc_replace(c, detail="different words entirely")
                for c in produced.confidence.components
            ),
        ),
    )
    assert reworded.output_hash() == baseline


# =============================================================================
# §14 -- both versions describe this release
# =============================================================================


def test_the_parser_version_is_2_1_3():
    assert PARSER_VERSION == "thetadata-v3-parser/2.1.3"


def test_the_engine_version_is_2_1_3():
    assert MODEL_VERSION == "gex-engine/2.1.3"


def test_the_two_versions_stay_distinct():
    assert PARSER_VERSION != MODEL_VERSION


def test_both_versions_reach_snapshot_metadata(tmp_path):
    produced = compute_gex_snapshot(fetch(runtime(tmp_path)))
    assert produced.meta["parser_version"] == PARSER_VERSION
    assert produced.meta["engine_version"] == MODEL_VERSION


# =============================================================================
# §16 -- one BOM normalisation
# =============================================================================

BOM = "﻿"


def test_a_bom_header_validates():
    from src.adapters.thetadata.client import CsvBodyStatus

    status, _ = validate_csv_body(
        BOM + "symbol,strike\n", required=("symbol", "strike")
    )
    assert status is CsvBodyStatus.VALID_EMPTY_CSV


def test_a_bom_header_parses_as_the_column_name_not_bom_plus_name():
    """The regression: validation stripped it, parsing did not."""
    rows = parse_csv(normalize_response_body(BOM + "symbol,strike\nSPXW,5000\n"))
    assert rows[0]["symbol"] == "SPXW"
    assert BOM + "symbol" not in rows[0]


def test_bom_and_plain_payloads_produce_identical_rows():
    plain = parse_csv(normalize_response_body("symbol,strike\nSPXW,5000\n"))
    with_bom = parse_csv(normalize_response_body(BOM + "symbol,strike\nSPXW,5000\n"))
    assert plain == with_bom


def test_the_client_reads_a_bom_response_correctly():
    from src.adapters.thetadata.client import (
        ChainRequest,
        ThetaDataClient,
        ThetaDataSettings,
    )
    from tests.unit.test_thetadata_runtime import csv_response

    body = BOM + csv_response().text
    client = ThetaDataClient(
        settings=ThetaDataSettings(),
        transport=FakeTransport(default=HttpResponse(status_code=200, text=body)),
        clock=lambda: AS_OF,
    )
    rows = client.option_quotes(ChainRequest(symbol="SPXW"))
    assert rows
    assert "symbol" in rows[0]


# =============================================================================
# §17 -- static completeness sees unimplemented sources
# =============================================================================


def test_an_unsupported_rate_source_is_statically_missing():
    missing = static_model_missing_inputs(
        ModelSpec(risk_free_rate_source=RateSource.VENDOR_SOFR)
    )
    assert any("risk_free_rate" in entry for entry in missing)


def test_an_unsupported_treasury_rate_source_is_statically_missing():
    missing = static_model_missing_inputs(
        ModelSpec(risk_free_rate_source=RateSource.VENDOR_TREASURY)
    )
    assert any("not implemented" in entry for entry in missing)


def test_an_unsupported_dividend_source_is_statically_missing():
    missing = static_model_missing_inputs(
        ModelSpec(dividend_yield_source=DividendSource.VENDOR_ANNUAL_DIVIDEND)
    )
    assert any("dividend_yield" in entry for entry in missing)


def test_zero_contracts_do_not_hide_an_unsupported_source():
    from dataclasses import replace

    from src.domain.iv import missing_iv
    from src.gex.config import GexEngineConfig
    from src.synthetic.chains import build_synthetic_chain

    chain = build_synthetic_chain()
    empty = replace(
        chain,
        quotes=tuple(replace(q, gamma=None, iv=missing_iv()) for q in chain.quotes),
    )
    produced = compute_gex_snapshot(
        empty,
        GexEngineConfig(
            model_spec=ModelSpec(risk_free_rate_source=RateSource.VENDOR_SOFR)
        ),
    )
    assert produced.contract_count == 0
    assert any(
        "risk_free_rate" in entry
        for entry in produced.meta["model_completeness"]["static_missing_inputs"]
    )


def test_implemented_explicit_zero_sources_stay_complete():
    assert (
        static_model_missing_inputs(
            ModelSpec(
                risk_free_rate_source=RateSource.ZERO,
                dividend_yield_source=DividendSource.ZERO,
            )
        )
        == ()
    )


# =============================================================================
# §15 -- the response-size error is an adapter error
# =============================================================================


def test_a_response_size_failure_derives_from_the_adapter_base():
    from src.adapters.errors import ThetaDataError
    from src.adapters.transport import ResponseTooLargeError

    assert issubclass(ResponseTooLargeError, ThetaDataError)


def test_an_oversized_response_is_catchable_with_one_base_class():
    from src.adapters.errors import ThetaDataError
    from src.adapters.transport import RetryingTransport

    transport = FakeTransport(default=HttpResponse(status_code=200, text="x" * 4096))
    with pytest.raises(ThetaDataError):
        RetryingTransport(transport, max_response_bytes=1024).get(
            "http://host/v3/quote", {}, 5.0
        )
