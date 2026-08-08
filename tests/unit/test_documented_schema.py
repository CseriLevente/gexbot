"""The repository against the vendor's documented v3 contract.

Every test here fails against v2.1.16, and none of them contacts ThetaData:
each drives the real adapter, the real dry run, or the deterministic fake
transport.

The headline is the first one. The documented index response is
``timestamp,symbol,price``; the adapter read ``index_price``. So against a
*correct* vendor response the parser reported valid and
``fetch_index_snapshot()`` returned ``None`` -- two true statements about the
same bytes, one of which was useless. Every gamma in the chain is divided by
the number that lookup was supposed to produce.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime

import pytest

from src.tools.capture_thetadata_once import (
    CaptureRunError,
    plan_capture,
    run_capture,
    run_path,
)
from tests.certification_fixtures import DOCUMENTED_SESSION, approval_hash_for

CAPTURE_CONFIG = "config/thetadata_capture.yaml"
INDEX = "/v3/index/snapshot/price"
CONTRACT_LIST = "/v3/option/list/contracts/quote"

#: The documented v3 response, verbatim from the brief.
DOCUMENTED_INDEX = b"timestamp,symbol,price\n2026-03-17T11:00:00.000,SPX,5000.25\n"

#: What v2.1.16's adapter expected. Not what the vendor sends.
LEGACY_INDEX = b"timestamp,symbol,index_price\n2026-03-17T11:00:00.000,SPX,5000.25\n"


# =============================================================================
# §1 -- the documented index schema
# =============================================================================


def test_the_documented_index_response_produces_a_snapshot():
    """The principal reproduced defect.

    v2.1.16: ``PARSER_VALID`` and ``fetch_index_snapshot() is None``.
    """
    from src.adapters.thetadata.client import ThetaDataClient
    from src.adapters.thetadata.endpoints import Endpoint
    from src.adapters.transport import FakeTransport

    transport = FakeTransport()
    transport.register_bytes(Endpoint.INDEX_PRICE_SNAPSHOT.value, DOCUMENTED_INDEX)
    client = ThetaDataClient(transport=transport)

    snapshot = client.fetch_index_snapshot(
        symbol="SPX", as_of=datetime(2026, 3, 17, 15, 0, tzinfo=UTC)
    )
    assert snapshot is not None
    assert snapshot.spot == 5000.25
    assert snapshot.symbol == "SPX"
    assert snapshot.timestamp is not None


def test_the_documented_index_response_is_semantically_valid():
    """And the parser agrees, which is the half that used to be true alone."""
    from src.adapters.thetadata.client import ThetaDataClient
    from src.adapters.thetadata.endpoints import Endpoint
    from src.adapters.thetadata.semantics import (
        SemanticStatus,
        validate_endpoint_semantics,
    )
    from src.adapters.transport import FakeTransport

    transport = FakeTransport()
    transport.register_bytes(Endpoint.INDEX_PRICE_SNAPSHOT.value, DOCUMENTED_INDEX)
    client = ThetaDataClient(transport=transport)
    rows = client.interpret(
        client.acquire(Endpoint.INDEX_PRICE_SNAPSHOT, {"symbol": "SPX"})
    )

    report = validate_endpoint_semantics(
        endpoint=INDEX, rows=rows, expected_symbol="SPX"
    )
    assert report.status is SemanticStatus.SEMANTIC_VALID
    assert report.findings == ()


def test_the_legacy_index_column_is_refused_under_the_v3_parser():
    """``index_price`` without ``price`` is not a v3 response.

    Refused at the schema gate, so the failure names the missing column rather
    than surfacing as a snapshot that quietly is not there.
    """
    from src.adapters.errors import ThetaDataSchemaError
    from src.adapters.thetadata.client import ThetaDataClient
    from src.adapters.thetadata.endpoints import Endpoint
    from src.adapters.transport import FakeTransport

    transport = FakeTransport()
    transport.register_bytes(Endpoint.INDEX_PRICE_SNAPSHOT.value, LEGACY_INDEX)
    client = ThetaDataClient(transport=transport)

    with pytest.raises(ThetaDataSchemaError, match=r"(?i)price"):
        client.fetch_index_snapshot(
            symbol="SPX", as_of=datetime(2026, 3, 17, 15, 0, tzinfo=UTC)
        )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"timestamp,symbol,price\n2026-03-17T11:00:00.000,SPX,-1\n", r"(?i)positive"),
        (b"timestamp,symbol,price\n2026-03-17T11:00:00.000,SPX,nan\n", r"(?i)finite"),
        (
            b"timestamp,symbol,price\n2026-03-17T11:00:00.000,NDX,5000.25\n",
            r"(?i)none of them",
        ),
        (
            b"timestamp,symbol,price\n2026-03-17T11:00:00.000,SPX,1\n"
            b"2026-03-17T11:00:01.000,SPX,2\n",
            r"(?i)which one is the spot",
        ),
    ],
)
def test_an_unusable_index_response_raises_rather_than_returning_none(body, expected):
    """Silence is the one answer nobody can check."""
    from src.adapters.errors import ThetaDataSchemaError
    from src.adapters.thetadata.client import ThetaDataClient
    from src.adapters.thetadata.endpoints import Endpoint
    from src.adapters.transport import FakeTransport

    transport = FakeTransport()
    transport.register_bytes(Endpoint.INDEX_PRICE_SNAPSHOT.value, body)
    client = ThetaDataClient(transport=transport)

    with pytest.raises(ThetaDataSchemaError, match=expected):
        client.fetch_index_snapshot(
            symbol="SPX", as_of=datetime(2026, 3, 17, 15, 0, tzinfo=UTC)
        )


# =============================================================================
# §2 -- the documented subscription tier
# =============================================================================


def test_the_index_endpoint_requires_standard():
    from src.adapters.thetadata.endpoints import (
        MINIMUM_TIER,
        Endpoint,
        Tier,
        tier_satisfies,
    )

    assert MINIMUM_TIER[Endpoint.INDEX_PRICE_SNAPSHOT] is Tier.STANDARD
    assert not tier_satisfies(Tier.VALUE, MINIMUM_TIER[Endpoint.INDEX_PRICE_SNAPSHOT])
    # And the shipped profile still reaches it.
    assert tier_satisfies(Tier.STANDARD, MINIMUM_TIER[Endpoint.INDEX_PRICE_SNAPSHOT])


def test_a_value_tier_index_profile_is_refused_at_plan_derivation():
    """v2.1.16 modelled the index endpoint at Value, so this passed."""
    from src.adapters.thetadata.capture_plan import capture_plan_for
    from src.adapters.thetadata.endpoints import Tier
    from src.adapters.thetadata.instruments import InstrumentMapping
    from tests.certification_fixtures import resolved_pipeline

    pipeline = resolved_pipeline()
    assert pipeline.config.underlying_price_source == "vendor_index_snapshot"

    with pytest.raises(ValueError, match=r"(?i)cannot serve"):
        capture_plan_for(
            pricing_mode=pipeline.pricing_mode,
            vendor_gamma_policy=pipeline.vendor_gamma_policy,
            underlying_price_source="vendor_index_snapshot",
            tier=Tier.VALUE,
            instruments=InstrumentMapping(
                option_symbol="SPXW", underlying_index_symbol="SPX"
            ),
        )


def test_the_shipped_profile_is_still_ready_for_raw_capture(tmp_path):
    report = plan_capture(
        CAPTURE_CONFIG, output=str(tmp_path / "capture"), as_of=DOCUMENTED_SESSION
    )
    assert report["capture_readiness"] == "READY_FOR_RAW_CAPTURE_ONLY"
    assert report["destination_refusals"] == []


# =============================================================================
# §3 -- documentation is pinned or it is nothing
# =============================================================================


def test_the_production_documentation_bundle_is_loaded_and_verified():
    """**Changed in v2.1.18, and the change is the point.**

    v2.1.17 asserted the production registry was empty and gave a reason: the
    v3 operation URLs 404, and no path available could produce a hash of the
    source bytes rather than of a rendering. The first half was about pages
    under ``http-docs.thetadata.us``; the OpenAPI description is served at
    ``docs.thetadata.us/openapiv3.yaml`` and always was. So the conclusion was
    wrong, the registry should not have been empty, and the test that asserted
    it was empty is the one that had to fail.

    There is no mutable registry now. There is an immutable bundle, rederived
    from the pinned bytes on every load.
    """
    from src.adapters.thetadata.openapi_evidence import production_bundle
    from src.adapters.thetadata.vendor_documentation import DocumentedRule

    bundle = production_bundle()
    assert bundle.verify_against(bundle.verified_root) == ()
    for rule in DocumentedRule:
        assert bundle.extraction_for(rule) is not None, rule


def test_the_mutable_documentation_registry_is_gone():
    """Authority with no gate is not authority anyone checked.

    A module-level ``dict`` could be written to by any importer, and nothing in
    the pipeline read it -- so an entry changed no behaviour, and adding one
    looked like progress.
    """
    from src.adapters.thetadata import vendor_documentation

    for name in (
        "PRODUCTION_VENDOR_DOCUMENTATION",
        "register_vendor_documentation",
        "resolve_documented_rule",
        "VendorDocumentationArtifact",
    ):
        assert not hasattr(vendor_documentation, name), name


@pytest.mark.parametrize(
    "dimension",
    ["DAY_COUNT", "DIVIDEND_CONVENTION"],
)
def test_undocumented_pricing_dimensions_remain_unknown(tmp_path, dimension):
    """Nothing is bound that no pinned document establishes.

    ``RATE_UNITS`` and ``MINIMUM_TIME_FLOOR`` left this list in v2.1.18 because
    the pinned document settles both. These two are still here because it does
    not settle them, and no amount of having *a* document changes that.
    """
    report = plan_capture(
        CAPTURE_CONFIG, output=str(tmp_path / "capture"), as_of=DOCUMENTED_SESSION
    )
    blockers = " ".join(report["calculation_blockers"])
    assert dimension in blockers, blockers


@pytest.mark.parametrize("dimension", ["RATE_UNITS", "MINIMUM_TIME_FLOOR"])
def test_documented_pricing_dimensions_no_longer_block(tmp_path, dimension):
    """The two the document settles stop blocking a trusted calculation."""
    report = plan_capture(
        CAPTURE_CONFIG, output=str(tmp_path / "capture"), as_of=DOCUMENTED_SESSION
    )
    blockers = " ".join(report["calculation_blockers"])
    assert dimension not in blockers, blockers


def test_a_pinned_document_verifies_against_its_bytes(tmp_path):
    """Content addressing, checked: swap the bytes and the pin stops holding."""
    import shutil

    from src.adapters.thetadata.openapi_evidence import (
        production_bundle,
        repository_documentation_root,
    )

    bundle = production_bundle()
    held = repository_documentation_root() / bundle.document.content_location
    copy = tmp_path / bundle.document.content_location
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(held, copy)
    assert bundle.verify_against(tmp_path) == ()

    copy.write_bytes(b"openapi: 3.1.0\npaths: {}\n")
    assert bundle.verify_against(tmp_path)


# =============================================================================
# §5 -- a capture is refused outside the market session
# =============================================================================


@pytest.mark.parametrize(
    ("moment", "status"),
    [
        (datetime(2026, 3, 15, 15, 0, tzinfo=UTC), "NON_TRADING_DAY"),
        (datetime(2026, 3, 17, 10, 0, tzinfo=UTC), "BEFORE_OPEN"),
        (datetime(2026, 3, 17, 21, 30, tzinfo=UTC), "AFTER_CLOSE"),
    ],
)
def test_a_live_capture_outside_the_session_is_refused(tmp_path, moment, status):
    """v2.1.16 ran it without comment, and the capture looked identical."""
    from src.gex.capture_window import assess_capture_window

    window = assess_capture_window(moment)
    assert window.status.value == status
    assert not window.inside_capture_window
    assert "--allow-out-of-session" in window.refusal

    with pytest.raises(CaptureRunError, match=r"(?i)allow-out-of-session"):
        run_capture(
            CAPTURE_CONFIG,
            output=str(tmp_path / "capture"),
            transport=None,
            as_of=moment,
            allow_unsettled_raw_only=True,
            approved=approval_hash_for(CAPTURE_CONFIG, as_of=moment),
        )
    assert not (tmp_path / "capture").exists()


def test_the_override_is_recorded_everywhere_it_matters(tmp_path, capsys):
    """Never silently enabled: intent, summary, and a warning on stderr."""
    from tests.certification_fixtures import vendor_transport

    sunday = datetime(2026, 3, 15, 15, 0, tzinfo=UTC)
    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=vendor_transport(),
        as_of=sunday,
        allow_unsettled_raw_only=True,
        approved=approval_hash_for(CAPTURE_CONFIG, as_of=sunday),
        allow_out_of_session=True,
    )
    assert report["out_of_session_capture"] is True
    assert report["market_session"]["session_status"] == "NON_TRADING_DAY"
    assert report["market_session"]["inside_capture_window"] is False

    intent = json.loads(run_path(report, "intent_path").read_text(encoding="utf-8"))
    assert intent["out_of_session_capture"] is True
    assert "WARNING" in capsys.readouterr().err


def test_the_dry_run_prints_the_market_clock(tmp_path):
    report = plan_capture(
        CAPTURE_CONFIG, output=str(tmp_path / "capture"), as_of=DOCUMENTED_SESSION
    )
    for key in (
        "market_time_et",
        "market_session_date",
        "market_session_open",
        "market_session_close",
        "inside_capture_window",
        "next_valid_capture_window",
    ):
        assert key in report, key


def test_the_command_exposes_the_override_flag():
    from src.tools.capture_thetadata_once import build_parser

    args = build_parser().parse_args(["--output", "out"])
    assert args.allow_out_of_session is False
    args = build_parser().parse_args(["--output", "out", "--allow-out-of-session"])
    assert args.allow_out_of_session is True


# =============================================================================
# §6/§7/§10 -- the report says what happened
# =============================================================================


def test_a_refused_contract_list_is_not_reported_as_observed(tmp_path):
    """The named regression: v2.1.16 reported a constant.

    A 400 on the listing meant no bytes at all, and the run still said
    ``OBSERVED``. Only an acquired body may be described that way.
    """
    from src.adapters.transport import FakeTransport, HttpResponse
    from tests.certification_fixtures import AS_OF, payloads

    transport = FakeTransport()
    for endpoint, text in payloads().items():
        if endpoint.value == CONTRACT_LIST:
            transport.register(
                endpoint.value,
                HttpResponse(status_code=400, body=b'{"error":"bad request"}'),
            )
        else:
            transport.register_text(endpoint.value, text)

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=transport,
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
        approved=approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF),
        allow_out_of_session=True,
    )
    assert report["contract_list_evidence_state"] == "VENDOR_REFUSED"
    assert "OBSERVED" not in report["contract_list_evidence_state"]


def test_a_failed_evidence_endpoint_does_not_contradict_itself(tmp_path):
    """The named regression: three fields, one capture, three stories.

    v2.1.16 produced ``run_state = FAILED_PARTIAL_ACQUISITION`` beside
    ``partial: false`` and ``missing_endpoints: []`` -- because the state was
    measured against every planned endpoint and ``partial`` against the
    required ones.
    """
    from src.adapters.transport import FakeTransport, HttpResponse
    from tests.certification_fixtures import AS_OF, payloads

    transport = FakeTransport()
    for endpoint, text in payloads().items():
        if endpoint.value == CONTRACT_LIST:
            transport.register(
                endpoint.value, HttpResponse(status_code=400, body=b"nope")
            )
        else:
            transport.register_text(endpoint.value, text)

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=transport,
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
        approved=approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF),
        allow_out_of_session=True,
    )

    # A chain does not need the listing, so the core capture is complete.
    assert report["core_capture_state"] == "CORE_ACQUIRED"
    assert report["missing_required_endpoints"] == []
    assert report["partial"] is False
    assert report["run_state"].startswith("COMPLETED_RAW")

    # And the evidence gap is visible rather than absorbed.
    assert report["evidence_capture_state"] == "EVIDENCE_INCOMPLETE"
    assert report["missing_evidence_endpoints"] == [CONTRACT_LIST]
    assert report["planned_acquisition_complete"] is False


def test_the_summary_exposes_each_layer_separately(tmp_path):
    from tests.certification_fixtures import AS_OF, vendor_transport

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=vendor_transport(),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
        approved=approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF),
        allow_out_of_session=True,
    )
    for key in (
        "required_manifest_verified",
        "planned_acquisition_complete",
        "attempt_evidence_verified",
        "parser_semantics_valid",
    ):
        assert isinstance(report[key], bool), key
    assert report["required_manifest_verified"] is True
    assert report["planned_acquisition_complete"] is True


# =============================================================================
# §8 -- the approved plan is bound to the bytes
# =============================================================================


def test_every_raw_record_names_the_plan_it_was_captured_under(tmp_path):
    """The named regression: v2.1.16 stored nothing tying the two together."""
    from src.adapters.raw_store import FileRawStore
    from tests.certification_fixtures import AS_OF, vendor_transport

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=vendor_transport(),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
        approved=approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF),
        allow_out_of_session=True,
    )
    plan_hash = report["request_plan"]["request_plan_hash"]
    assert plan_hash

    store = FileRawStore(run_path(report, "raw_store_path"))
    records = store.records()
    assert len(records) == 5
    for record in records:
        assert record.request_plan_hash == plan_hash, record.record_id
        assert record.planned_request_hash, record.record_id
        assert record.request_plan_schema_version.startswith("raw-request-plan/")

    # And the manifest carries the same identity, inside its hash.
    manifest = json.loads(run_path(report, "manifest_path").read_text(encoding="utf-8"))
    for entry in manifest["records"]:
        assert entry["request_plan_hash"] == plan_hash


def test_a_record_captured_under_another_plan_does_not_verify(tmp_path):
    """A contract-list record from another session date is refused."""
    import dataclasses

    from src.adapters.certification import verify_capture
    from src.adapters.raw_store import FileRawStore, RawCaptureManifest
    from tests.certification_fixtures import AS_OF, vendor_transport

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=vendor_transport(),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
        approved=approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF),
        allow_out_of_session=True,
    )
    store = FileRawStore(run_path(report, "raw_store_path"))
    manifest = RawCaptureManifest.rebuilt_from(
        json.loads(run_path(report, "manifest_path").read_text(encoding="utf-8"))
    )

    from tests.certification_fixtures import resolved_pipeline

    plan = resolved_pipeline().raw_request_plan(as_of=AS_OF)

    # A listing captured for a different session date carries a different
    # planned-request hash, and the plan will not own it.
    tampered = dataclasses.replace(
        manifest,
        records=tuple(
            dataclasses.replace(entry, planned_request_hash="0" * 64)
            if entry.endpoint == CONTRACT_LIST
            else entry
            for entry in manifest.records
        ),
    )
    result = verify_capture(
        tampered,
        store,
        plan=resolved_pipeline().capture_plan,
        expected_pipeline_fingerprint=resolved_pipeline().fingerprint(),
        expected_request_plan=plan,
    )
    assert not result.verified
    assert any("PLANNED_REQUEST_MISMATCH" in f for f in result.failures), (
        result.failures
    )


def test_this_file_contacts_no_vendor():
    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    for forbidden in ("httpx" + ".Client(", "socket" + ".", "url" + "open"):
        assert forbidden not in source, forbidden
