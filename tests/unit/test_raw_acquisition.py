"""What v2.1.15 fixed, one named regression per defect.

Every test here fails against v2.1.14. None of them makes a network request:
each drives the real code through the deterministic fake transport, a fake
``httpx``, or bytes already on disk.

The headline is the first one. The operator command was described as raw-only
and reached the wire through ``pipeline.fetch_chain()``, which parses each
endpoint in order to build the next request -- so the one thing the first paid
session exists to discover, an unexpected schema, was the thing that stopped it
discovering anything else.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from src.tools.capture_thetadata_once import (
    CaptureRunError,
    RawCaptureRunState,
    plan_capture,
    run_capture,
    run_path,
)

CAPTURE_CONFIG = "config/thetadata_capture.yaml"


# =============================================================================
# §1 -- parsing must not decide what gets requested
# =============================================================================


def mixed_transport(*, broken_endpoint: str, body: bytes):
    """Every endpoint answers; one of them answers with something unusable."""
    from src.adapters.transport import FakeTransport
    from tests.certification_fixtures import payloads

    transport = FakeTransport()
    for endpoint, text in payloads().items():
        if endpoint.value == broken_endpoint:
            transport.register_bytes(
                endpoint.value, body, **{"content-type": "text/html"}
            )
        else:
            transport.register_text(endpoint.value, text)
    return transport


def test_an_index_schema_error_does_not_prevent_the_other_endpoints(tmp_path):
    """The headline regression.

    v2.1.14: the index snapshot is fetched first, parsed to get the spot, and
    the parse raises -- before the quote, open-interest or greeks requests are
    ever built. An operator paid for a session and got one response.

    The required proof: all four endpoints attempted, all four preserved.
    """
    from src.adapters.thetadata.endpoints import Endpoint
    from tests.certification_fixtures import AS_OF

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=mixed_transport(
            broken_endpoint=Endpoint.INDEX_PRICE_SNAPSHOT.value,
            body=b"<html><body>scheduled maintenance</body></html>",
        ),
        as_of=AS_OF,
    )

    acquisition = report["raw_acquisition"]
    planned = set(acquisition["planned_endpoints"])
    assert len(planned) == 4, sorted(planned)
    assert set(acquisition["attempted_endpoints"]) == planned
    assert set(acquisition["acquired_endpoints"]) == planned
    assert acquisition["missing_endpoints"] == []
    assert acquisition["stopped_early"] is False
    assert acquisition["stop_reason"] == "NONE"

    # And the bytes are on disk -- including the unusable ones, which are the
    # most interesting thing this session found.
    stored = sorted(p.name for p in run_path(report, "raw_store_path").glob("*.raw"))
    assert len(stored) == 4, stored
    assert any(
        b"scheduled maintenance"
        in (run_path(report, "raw_store_path") / name).read_bytes()
        for name in stored
    )


def test_a_parser_failure_cannot_downgrade_a_complete_raw_acquisition(tmp_path):
    """Raw state and parser state answer different questions.

    A capture where every endpoint answered and none of them parse is a
    *successful* discovery session. Reporting it as a failed run is what made a
    schema error look like a reason to stop requesting.
    """
    from src.adapters.thetadata.endpoints import Endpoint
    from tests.certification_fixtures import AS_OF

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=mixed_transport(
            broken_endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT.value,
            body=b"<html>not csv</html>",
        ),
        as_of=AS_OF,
    )

    assert report["run_state"] == RawCaptureRunState.COMPLETED_RAW_VERIFIED.value
    assert report["integrity_ok"] is True
    assert report["parser_state"] == "PARSER_FAILED"
    assert report["trusted_gex_computed"] is False

    parser = json.loads(
        run_path(report, "parser_report_path").read_text(encoding="utf-8")
    )
    assert parser["schema_version"].startswith("parser-report/")
    assert parser["chain_assembled"] is False
    failed = [
        entry
        for entry in parser["endpoints"]
        if entry["parser_status"] == "PARSER_FAILED"
    ]
    assert [entry["endpoint"] for entry in failed] == [
        Endpoint.OPTION_QUOTE_SNAPSHOT.value
    ]
    # Every other endpoint parsed, which is only knowable because they were
    # requested at all.
    assert (
        sum(1 for e in parser["endpoints"] if e["parser_status"] == "PARSER_VALID") == 3
    )


def test_requests_are_derivable_without_a_chain_snapshot():
    """Acquisition is built from configuration, not from a parsed result.

    ``fetch_chain`` needed the index response *parsed* to know the spot before
    it would issue the quote request. The raw sweep derives every request from
    the capture plan, the chain request, the index symbol, the Greeks
    parameters and the tier -- all of which exist before anything is sent.
    """
    from tests.certification_fixtures import resolved_pipeline

    pipeline = resolved_pipeline()
    planned = pipeline.raw_request_parameters()

    assert {endpoint for endpoint, _ in planned} == set(
        pipeline.capture_plan.required_endpoints
    )
    for endpoint, params in planned:
        assert params, endpoint
        assert all(isinstance(key, str) for key in params)
    # The greeks request carries the rate and dividend the vendor computes
    # under; the index request carries only the symbol.
    by_endpoint = {endpoint.value: params for endpoint, params in planned}
    assert set(by_endpoint["/v3/index/snapshot/price"]) == {"symbol"}
    assert "symbol" in by_endpoint["/v3/option/snapshot/quote"]


# =============================================================================
# §2 -- every post-claim failure is controlled
# =============================================================================


@pytest.mark.parametrize(
    "target",
    [
        "src.adapters.http_attempts.HttpAttemptLog",
        "src.adapters.raw_store.FileRawStore",
        "src.adapters.artifact_store.ArtifactStore",
    ],
)
def test_a_constructor_failure_leaves_no_ownerless_directory(
    tmp_path, monkeypatch, target
):
    """The named regression: three stores were built after the mkdir.

    v2.1.14 claimed the destination and then constructed the attempt log, the
    raw store, the artifact store, the transport and the pipeline before the
    guard. Any of them raising left an empty directory nobody had written a
    word about -- and the next invocation refused it as an earlier run's.
    """
    module_name, _, attribute = target.rpartition(".")
    module = __import__(module_name, fromlist=[attribute])
    original = getattr(module, attribute)

    # Preflight builds a probe store of its own *before* the claim, and a
    # failure there is a refusal with no directory to speak of -- correct, and
    # a different case from this one. Fail on the construction that happens
    # after the destination is this run's responsibility.
    seen: list[int] = []
    survives = 1 if attribute == "FileRawStore" else 0

    def exploding(*args, **kwargs):
        seen.append(1)
        if len(seen) <= survives:
            return original(*args, **kwargs)
        raise OSError(f"{attribute} could not be constructed")

    monkeypatch.setattr(module, attribute, exploding)

    destination = tmp_path / "claimed"
    report = _live_run(destination)

    assert report["bootstrap_failure"] is True
    assert report["trusted_gex_computed"] is False
    assert report["error_code"], report
    _assert_not_ownerless(destination, report)


def test_a_pipeline_construction_failure_is_controlled(tmp_path, monkeypatch):
    """The real pipeline, failing where the run has already claimed a directory.

    Preflight builds a pipeline too -- with a transport that cannot send -- and
    a failure there is a refusal before anything exists. The one that matters
    here is the build that takes the run's own stores.
    """
    import src.config.pipeline as pipeline_module

    original = pipeline_module.ThetaDataResearchPipeline.from_loaded_config

    def exploding(loaded, **kwargs):
        if kwargs.get("default_raw_store") is None:
            return original(loaded, **kwargs)
        raise RuntimeError("the profile could not be assembled")

    monkeypatch.setattr(
        pipeline_module.ThetaDataResearchPipeline, "from_loaded_config", exploding
    )
    destination = tmp_path / "claimed"
    report = _live_run(destination)
    assert report["bootstrap_failure"] is True
    assert "raw_store" in report["constructed"]
    assert "pipeline" not in report["constructed"]
    _assert_not_ownerless(destination, report)


def test_a_capture_session_failure_is_controlled(tmp_path, monkeypatch):
    import src.config.pipeline as pipeline_module

    def exploding(self, **kwargs):
        raise RuntimeError("the capture operation could not be opened")

    monkeypatch.setattr(
        pipeline_module.ThetaDataResearchPipeline, "capture_session", exploding
    )
    destination = tmp_path / "claimed"
    report = _live_run(destination)
    assert report["bootstrap_failure"] is True
    assert "pipeline" in report["constructed"]
    assert "capture_session" not in report["constructed"]
    _assert_not_ownerless(destination, report)


def test_an_intent_writing_failure_produces_a_report(tmp_path, monkeypatch):
    """The named regression: ``_write_intent`` could raise into nothing.

    It runs after the session is open and before the first request, which is
    precisely the window where a failure produces a directory containing
    stores, no intent, no manifest and no summary.
    """
    import src.tools.capture_thetadata_once as tool

    def exploding(run, *, config_path):
        raise OSError("the intent document could not be written")

    monkeypatch.setattr(tool, "_write_intent", exploding)
    destination = tmp_path / "claimed"
    report = _live_run(destination)

    assert report["bootstrap_failure"] is True
    assert "capture_session" in report["constructed"]
    assert report["run_state"] == RawCaptureRunState.FAILED_BEFORE_REQUEST.value
    _assert_not_ownerless(destination, report)


def test_every_constructed_transport_is_closed(tmp_path, monkeypatch):
    """Whatever fails after the claim, the connection pool is given back."""
    import src.tools.capture_thetadata_once as tool

    closed: list[object] = []
    original = tool._close

    def watched(pipeline):
        closed.append(pipeline)
        return original(pipeline)

    monkeypatch.setattr(tool, "_close", watched)
    monkeypatch.setattr(
        tool,
        "_write_intent",
        lambda run, *, config_path: (_ for _ in ()).throw(OSError("no intent")),
    )
    _live_run(tmp_path / "claimed")
    assert closed, "the transport was never closed"
    assert closed[0] is not None


# =============================================================================
# §3 -- replay consumes the stored bytes
# =============================================================================


@pytest.mark.parametrize(
    ("name", "body", "headers"),
    [
        ("non_utf8", b"a,b\n1,\xff\n", {}),
        # Latin-1 only: "é" is one byte here and two in UTF-8, so a replay
        # that ignores the declared charset produces different text.
        (
            "latin1",
            "prix,societe\n1,Société\n".encode("latin-1"),
            {"content-type": "text/csv; charset=iso-8859-1"},
        ),
        ("bom", b"\xef\xbb\xbfa,b\n1,2\n", {}),
        ("crlf", b"a,b\r\n1,2\r\n", {}),
        ("empty", b"", {}),
    ],
)
def test_replay_reproduces_the_stored_bytes_and_the_captured_reading(
    tmp_path, name, body, headers
):
    """The named regression: ``store.get_payload()`` decoded with replacement.

    Replay went through ``get_payload``, which is UTF-8 with ``errors="replace"``
    -- so a latin-1 body replayed as U+FFFD and a body with one invalid byte was
    re-encoded into something the capture never contained. A replay that changes
    the evidence is not a replay.
    """
    from src.adapters.raw_store import FileRawStore, RawCaptureManifest
    from src.adapters.transport import StoredPayloadTransport, decode_body
    from tests.certification_fixtures import AS_OF

    store = FileRawStore(tmp_path / name)
    captured = decode_body(body, headers)
    record = store.put(
        record_id="r1",
        endpoint="/v3/option/snapshot/quote",
        query_params={"symbol": "SPXW"},
        payload=body,
        request_started_at=AS_OF,
        response_received_at=AS_OF,
        http_status=200,
        decode=captured.as_dict(),
        response_headers=headers,
    )
    manifest = RawCaptureManifest(session_id="s1", records=(_manifest_entry(record),))
    transport = StoredPayloadTransport.from_capture(manifest=manifest, store=store)
    replayed = transport.get(
        "http://x/v3/option/snapshot/quote", {"symbol": "SPXW"}, 1.0
    )

    assert replayed.body == body
    assert replayed.byte_length == len(body)

    derived = replayed.decode_text()
    assert derived.body_hash == captured.body_hash
    assert derived.decode_status is captured.decode_status
    assert derived.selected_charset == captured.selected_charset
    assert derived.decoded_text_hash == captured.decoded_text_hash
    # A real empty body is an empty body, not "supplied as text".
    assert derived.decode_status.value != "SUPPLIED_AS_TEXT"


def test_replay_refuses_when_the_capture_and_its_metadata_disagree(tmp_path):
    """Refused before parsing, because parsing it would answer a different question."""
    import dataclasses

    from src.adapters.raw_store import FileRawStore, RawCaptureManifest
    from src.adapters.transport import (
        ReplayFidelityError,
        StoredPayloadTransport,
        decode_body,
    )
    from tests.certification_fixtures import AS_OF

    body = b"a,b\n1,2\n"
    store = FileRawStore(tmp_path / "raw")
    record = store.put(
        record_id="r1",
        endpoint="/v3/option/snapshot/quote",
        query_params={},
        payload=body,
        request_started_at=AS_OF,
        response_received_at=AS_OF,
        http_status=200,
        decode=decode_body(body, {}).as_dict(),
    )
    lying = dataclasses.replace(record, decoded_text_hash="0" * 64)
    manifest = RawCaptureManifest(session_id="s1", records=(_manifest_entry(lying),))

    class _Lying:
        """A store that returns the tampered record for the same bytes."""

        def records(self):
            return (lying,)

        def get_body(self, record_id):
            return store.get_body(record_id)

    with pytest.raises(ReplayFidelityError, match="decoded-text hash"):
        StoredPayloadTransport.from_capture(manifest=manifest, store=_Lying())


def test_a_real_empty_response_is_not_supplied_as_text():
    """The named regression: ``b""`` and "no body given" were the same value."""
    from src.adapters.transport import HttpResponse

    empty = HttpResponse(status_code=204, body=b"")
    assert empty.body == b""
    assert empty.decode_text().decode_status.value == "EXACT"

    as_text = HttpResponse(status_code=200, text="a,b\n")
    assert as_text.decode_text().decode_status.value == "SUPPLIED_AS_TEXT"


# =============================================================================
# §4 -- attempt evidence survives the process that wrote it
# =============================================================================


def test_a_reopened_attempt_log_detects_a_modified_body(tmp_path):
    """The named regression: a fresh log had no records and so no failures.

    ``HttpAttemptLog(root)`` starts with an empty in-memory list, and
    ``verify_bodies`` iterates over it. Opening an archived capture and asking
    whether it had been tampered with therefore always answered "no".
    """
    from src.adapters.http_attempts import HttpAttemptLog

    report, root = _capture_with_attempts(tmp_path)
    assert report["attempt_evidence"]["ok"] is True
    assert report["attempt_evidence"]["attempt_count"] >= 1
    assert report["attempt_evidence"]["attempt_index_hash"]

    # The original objects are gone; only the directory remains.
    bodies = sorted(root.rglob("*.bin"))
    assert bodies, "the capture preserved no attempt bodies"
    bodies[0].write_bytes(b"a different body entirely")

    # v2.1.14: an empty fresh log, no failures, tampering undetected.
    assert HttpAttemptLog(root).verify_bodies() == ()

    reopened = HttpAttemptLog.open_existing(root)
    assert not reopened.ok
    assert any("hashes to" in finding for finding in reopened.findings), (
        reopened.findings
    )


def test_a_malformed_middle_index_line_is_a_finding(tmp_path):
    """A torn *final* line is an interrupted append. A middle one is damage."""
    from src.adapters.http_attempts import HttpAttemptLog

    _, root = _capture_with_attempts(tmp_path)
    index = root / "index.jsonl"
    lines = index.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2, lines
    lines[0] = '{"logical_request_id": "truncated'
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = HttpAttemptLog.open_existing(root)
    assert not report.ok
    assert any("malformed, not the final line" in f for f in report.findings)

    # And the torn-final-line case is still forgiven, because an append-only
    # index that discarded everything before an interrupted write would be
    # useless for the failure it exists to survive.
    _, other = _capture_with_attempts(tmp_path, name="second")
    other_index = other / "index.jsonl"
    other_index.write_text(
        other_index.read_text(encoding="utf-8") + '{"partial": ', encoding="utf-8"
    )
    torn = HttpAttemptLog.open_existing(other)
    assert any("torn final line" in f for f in torn.findings)
    assert not any("malformed, not the final line" in f for f in torn.findings)


# =============================================================================
# §5 -- a payload location cannot lie
# =============================================================================


def test_a_payload_location_naming_another_file_fails_integrity(tmp_path):
    """The named regression: the location was validated and then ignored.

    v2.1.14 checked that ``payload_location`` was *relative* and then derived
    the path from ``record_id`` instead, so an index could name
    ``missing/other.raw`` and the scan still reported VALID.
    """
    from src.adapters.raw_store import FileRawStore
    from tests.certification_fixtures import AS_OF

    store = FileRawStore(tmp_path / "raw")
    store.put(
        record_id="r1",
        endpoint="/v3/option/snapshot/quote",
        query_params={},
        payload=b"a,b\n1,2\n",
        request_started_at=AS_OF,
        response_received_at=AS_OF,
        http_status=200,
    )
    assert store.verify_integrity().ok

    index = tmp_path / "raw" / "index.jsonl"
    entry = json.loads(index.read_text(encoding="utf-8").strip())
    assert entry["payload_location"] == "r1.raw"
    entry["payload_location"] = "missing/other.raw"
    index.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    report = store.verify_integrity()
    assert not report.ok
    assert any("payload_location" in f.detail for f in report.findings)


# =============================================================================
# §7 -- the disk requirement comes from the configuration
# =============================================================================


def test_the_disk_requirement_reflects_the_configured_capture(tmp_path):
    """The named regression: a flat 64 MiB against a 64 MiB *per-response* cap.

    The shipped profile allows 64 MiB per response across four endpoints with
    four attempts each. A check that asked for 64 MiB total passed on a disk
    that could not hold one endpoint's response.
    """
    from src.tools.capture_thetadata_once import disk_requirement

    report = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "capture"))
    disk = report["disk_space"]

    assert disk["required_endpoint_count"] == 4
    assert disk["max_response_bytes"] == 64 * 1024 * 1024
    assert disk["max_attempts_per_endpoint"] == 4
    assert disk["minimum_required_free_bytes"] > 64 * 1024 * 1024 * 4
    for key in ("available_free_bytes", "safety_margin", "measured_at", "sufficient"):
        assert key in disk, key

    # The arithmetic is a function of the plan, not of a constant.
    doubled = disk_requirement(
        endpoints=4, max_response_bytes=128 * 1024 * 1024, max_attempts=4
    )
    assert (
        doubled["minimum_required_free_bytes"]
        > disk["minimum_required_free_bytes"] * 1.9
    )


def test_a_destination_without_room_is_refused_before_anything_is_claimed(
    tmp_path, monkeypatch
):
    import src.tools.capture_thetadata_once as tool

    monkeypatch.setattr(tool, "_free_bytes", lambda destination: (tmp_path, 1024))
    destination = tmp_path / "capture"
    with pytest.raises(CaptureRunError, match=r"(?i)bytes free"):
        _live_run(destination, expect_report=False)
    assert not destination.exists()


# =============================================================================
# §8 -- a failure says which endpoint it is about
# =============================================================================


@pytest.mark.parametrize(
    ("body", "headers", "expected_code"),
    [
        (b"<html>not csv</html>", {}, "SCHEMA_ERROR"),
        (b'{"error":"no data"}', {}, "VENDOR_HTTP_ERROR"),
    ],
)
def test_a_parser_failure_names_its_endpoint_structurally(body, headers, expected_code):
    """The named regression: ``failed_endpoint`` was empty for schema errors.

    It was recovered with ``str(getattr(error, "url", ""))``, which no adapter
    exception ever set -- so it was blank for exactly the three failures a
    discovery session produces most.
    """
    from src.adapters.errors import ThetaDataError, endpoint_of_error
    from src.adapters.thetadata.client import ThetaDataClient
    from src.adapters.thetadata.endpoints import Endpoint
    from src.adapters.thetadata.raw_acquisition import classify_failure
    from src.adapters.transport import FakeTransport

    transport = FakeTransport()
    transport.register_bytes(
        Endpoint.OPTION_QUOTE_SNAPSHOT.value, body, **{"content-type": "text/html"}
    )
    client = ThetaDataClient(transport=transport)
    acquired = client.acquire(Endpoint.OPTION_QUOTE_SNAPSHOT, {"symbol": "SPXW"})

    with pytest.raises(ThetaDataError) as caught:
        client.interpret(acquired)

    error = caught.value
    assert error.endpoint == Endpoint.OPTION_QUOTE_SNAPSHOT.value
    assert endpoint_of_error(error) == Endpoint.OPTION_QUOTE_SNAPSHOT.value
    assert classify_failure(error) == expected_code
    assert error.failure_identity["endpoint"] == Endpoint.OPTION_QUOTE_SNAPSHOT.value
    # And no credential reaches the identity an operator pastes into a ticket.
    assert "password" not in json.dumps(error.failure_identity).lower()


# =============================================================================
# Shared helpers
# =============================================================================


def _live_run(destination, *, expect_report=True):
    from tests.certification_fixtures import AS_OF, vendor_transport

    return run_capture(
        CAPTURE_CONFIG,
        output=str(destination),
        transport=vendor_transport(),
        as_of=AS_OF,
    )


def _assert_not_ownerless(destination: pathlib.Path, report: dict) -> None:
    """Either a typed failure report is there, or the directory is not."""
    if not destination.exists():
        return
    contents = sorted(p.name for p in destination.iterdir())
    assert "capture-bootstrap-failure.json" in contents, contents
    written = json.loads(
        (destination / "capture-bootstrap-failure.json").read_text(encoding="utf-8")
    )
    assert written["run_id"] == report["run_id"]
    assert written["error_code"]


def _capture_with_attempts(tmp_path, *, name="capture"):
    """A completed capture whose attempt log has at least one preserved body."""
    from tests.certification_fixtures import AS_OF, vendor_transport

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / name),
        transport=vendor_transport(),
        as_of=AS_OF,
    )
    return report, run_path(report, "attempt_store_path")


def _manifest_entry(record):
    from src.adapters.raw_store import ManifestRecord

    return ManifestRecord(
        record_id=record.record_id,
        endpoint=record.endpoint,
        payload_hash=record.payload_hash,
        parameter_hash="",
        payload_location=record.payload_location,
        request_sequence=record.request_sequence,
        http_status=record.http_status,
        byte_length=record.byte_length,
        content_type=record.content_type,
        declared_charset=record.declared_charset,
        selected_charset=record.selected_charset,
        decode_status=record.decode_status,
        decoded_text_hash=record.decoded_text_hash,
        response_headers=dict(record.response_headers),
    )


def test_no_test_in_this_file_reaches_the_network():
    """A rule, checked, rather than a sentence in a docstring."""
    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    # Assembled rather than written out, so this check does not trip over its
    # own list of what it forbids.
    for forbidden in ("httpx" + ".Client(", "socket" + ".", "url" + "open"):
        assert forbidden not in source, forbidden
    assert hashlib.sha256(b"").hexdigest()  # the module imports what it claims to
