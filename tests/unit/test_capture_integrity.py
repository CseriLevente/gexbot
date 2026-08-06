"""The exact path the first paid session runs down, checked against itself.

v2.1.12 made the capture command safe to run. Reproducing that run found that
the safest-looking part of it was writing into the checkout:
``build_thetadata_client`` constructed ``FileRawStore(config.raw_capture_path)``
during pipeline construction, for every caller. The operator writes its capture
to ``<output>/raw`` and passes that store to the session, so the configured one
received nothing -- and the shipped profile says ``artifacts/raw``. The dry run,
which reports ``wrote_files=false``, created it too.

The rest of this file is the other things a real session does that a synthetic
one never did: two operators started at once, a vendor that answers 401, a body
that is not UTF-8, a response larger than the cap, and a Theta Terminal that is
not running.

Every test here fails against v2.1.12, and none of them makes a network request.
"""

from __future__ import annotations

import json
import pathlib
import threading

import pytest

from src.tools.capture_thetadata_once import (
    CaptureRunError,
    ExitCode,
    RawCaptureRunState,
    plan_capture,
    run_capture,
    run_path,
)

CAPTURE_CONFIG = "config/thetadata_capture.yaml"
REPOSITORY = pathlib.Path(__file__).resolve().parents[2]


def repository_tree() -> set[str]:
    """Every tracked-ish path under the checkout, ignoring churn we do not own."""
    skip = {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
    }
    found: set[str] = set()
    for path in REPOSITORY.rglob("*"):
        parts = set(path.relative_to(REPOSITORY).parts)
        if parts & skip:
            continue
        found.add(str(path.relative_to(REPOSITORY)))
    return found


# =============================================================================
# §1 -- no run creates a store nobody asked for
# =============================================================================


def test_a_dry_run_modifies_nothing_in_the_repository(tmp_path):
    """The headline v2.1.13 regression.

    Snapshotted before and after, because the defect was invisible from inside
    the report: v2.1.12 said ``wrote_files=false`` while creating
    ``artifacts/raw`` and ``artifacts/raw.health`` in the working tree.
    """
    before = repository_tree()
    report = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "capture"))
    after = repository_tree()

    assert report["wrote_files"] is False
    assert after == before, sorted(after - before)
    assert not (REPOSITORY / "artifacts" / "raw").exists()


def test_a_dry_run_modifies_nothing_at_the_requested_destination(tmp_path):
    destination = tmp_path / "capture"
    plan_capture(CAPTURE_CONFIG, output=str(destination))
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_live_run_does_not_create_the_configured_fallback_path(tmp_path):
    """``config.raw_capture_path`` names a fallback. It is not an instruction."""
    from src.config.schema import load_config

    configured = load_config(CAPTURE_CONFIG).thetadata.raw_capture_path
    assert configured, "the profile still names a fallback, which is the point"
    before = repository_tree()

    report = live_run(tmp_path)

    assert repository_tree() == before
    assert not (REPOSITORY / configured).exists()
    assert report["effective_raw_store_path"] == str(tmp_path / "capture" / "raw")


def test_exactly_one_raw_store_is_constructed_for_a_live_run(tmp_path, monkeypatch):
    """Two stores meant one of them silently received nothing."""
    import src.adapters.raw_store as raw_store_module

    built: list[str] = []
    original = raw_store_module.FileRawStore.__init__

    def watched(self, root, *args, **kwargs):
        built.append(str(root))
        return original(self, root, *args, **kwargs)

    monkeypatch.setattr(raw_store_module.FileRawStore, "__init__", watched)
    report = live_run(tmp_path)

    # Preflight probes store durability in a temporary directory that is gone
    # before the run claims anything; it is not a place records could land.
    durable = [root for root in built if "gex-preflight-" not in root]
    assert durable == [report["effective_raw_store_path"]], built
    for probe in set(built) - set(durable):
        assert not pathlib.Path(probe).exists(), probe


def test_the_reported_store_is_the_store_that_received_the_records(tmp_path):
    report = live_run(tmp_path)
    root = run_path(report, "effective_raw_store_path")
    written = {path.stem for path in root.glob("*.raw")}
    assert written == set(report["record_ids"]), sorted(written)


# =============================================================================
# §2 -- one run owns its destination
# =============================================================================


def test_two_concurrent_runs_cannot_both_acquire_one_destination(tmp_path):
    """The named regression. Real threads, one directory.

    v2.1.12 checked that the path was empty and *then* created the stores, so
    two processes could both observe an empty path, both proceed, and mix their
    records into one manifest.
    """
    from tests.certification_fixtures import AS_OF, vendor_transport

    destination = str(tmp_path / "contested")
    outcomes: list[tuple[str, object]] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        try:
            report = run_capture(
                CAPTURE_CONFIG,
                output=destination,
                transport=vendor_transport(),
                as_of=AS_OF,
                allow_unsettled_raw_only=True,
            )
        except CaptureRunError as error:
            with lock:
                outcomes.append(("refused", error))
        except BaseException as error:  # any other failure is a test failure
            with lock:
                outcomes.append(("error", error))
        else:
            with lock:
                outcomes.append(("ok", report))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    kinds = sorted(kind for kind, _ in outcomes)
    assert kinds == ["ok", "refused"], [
        (kind, str(value)[:200]) for kind, value in outcomes
    ]

    refusal = next(value for kind, value in outcomes if kind == "refused")
    assert "already exists" in str(refusal) or "already holds" in str(refusal)

    # One run's evidence, not two mixed together.
    report = next(value for kind, value in outcomes if kind == "ok")
    summary = json.loads(run_path(report, "summary_path").read_text(encoding="utf-8"))
    assert summary["run_id"] == report["run_id"]
    stored = {p.stem for p in run_path(report, "raw_store_path").glob("*.raw")}
    assert stored == set(report["record_ids"])


def test_the_refused_run_writes_nothing_into_the_acquired_directory(tmp_path):
    """It is refused before a store, an attempt log or an intent exists."""
    destination = tmp_path / "taken"
    first = live_run(tmp_path, destination=destination)
    before = sorted(p.name for p in destination.iterdir())

    with pytest.raises(CaptureRunError, match=r"(?i)already (exists|holds)"):
        live_run(tmp_path, destination=destination)

    assert sorted(p.name for p in destination.iterdir()) == before
    summary = json.loads(run_path(first, "summary_path").read_text(encoding="utf-8"))
    assert summary["run_id"] == first["run_id"]


# =============================================================================
# §3 -- the bytes that arrived are the bytes that are stored
# =============================================================================


def test_non_utf8_bytes_round_trip_byte_identically(tmp_path):
    """The named regression.

    v2.1.12 decoded with ``errors="replace"`` in the transport and the store
    re-encoded the *string* as UTF-8. One invalid byte became a U+FFFD, and the
    digest was described as the hash of the vendor's response.
    """
    from src.adapters.raw_store import FileRawStore, payload_hash
    from tests.certification_fixtures import AS_OF

    body = b"timestamp,symbol\n2026-03-17,SPX\xff\xfe-not-utf8\n"
    store = FileRawStore(tmp_path / "raw")
    record = store.put(
        record_id="r1",
        endpoint="/v3/option/snapshot/quote",
        query_params={"symbol": "SPXW"},
        payload=body,
        request_started_at=AS_OF,
        response_received_at=AS_OF,
        http_status=200,
    )
    assert store.get_body("r1") == body
    assert record.payload_hash == payload_hash(body)
    assert record.byte_length == len(body)
    # And the text view is a *reading*, which is allowed to be lossy.
    assert "�" in store.get_payload("r1")


def test_a_utf8_bom_round_trips_byte_identically(tmp_path):
    from src.adapters.raw_store import FileRawStore
    from tests.certification_fixtures import AS_OF

    body = "﻿timestamp,symbol\n2026-03-17,SPX\n".encode()
    assert body.startswith(b"\xef\xbb\xbf")
    store = FileRawStore(tmp_path / "raw")
    store.put(
        record_id="r1",
        endpoint="/v3/option/snapshot/quote",
        query_params={},
        payload=body,
        request_started_at=AS_OF,
        response_received_at=AS_OF,
        http_status=200,
    )
    assert store.get_body("r1") == body


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("lf", b"timestamp,symbol\n2026-03-17,SPX\n"),
        # The one that failed. Text mode translates CRLF to LF on read, so the
        # scan hashed different bytes from the ones it had written.
        ("crlf", b"timestamp,symbol\r\n2026-03-17,SPX\r\n"),
        ("cr", b"timestamp,symbol\r2026-03-17,SPX\r"),
        ("bom", b"\xef\xbb\xbftimestamp,symbol\n2026-03-17,SPX\n"),
        ("empty", b""),
        # The one that took the whole scan down: not decodable, so text mode
        # raised UnicodeDecodeError and every later record went unchecked.
        ("latin1", "prix,symbole\n4 900,50 €,SPX\n".encode("latin-1", "replace")),
        # Not named "nul": that is a reserved Windows device name, and the
        # store root is a directory named after the case.
        ("nul_bytes", b"a,b\n\x00\x00,SPX\n"),
        ("binary", bytes(range(256)) * 4),
    ],
)
def test_integrity_is_a_statement_about_bytes(tmp_path, name, body):
    """The named regression: whatever the vendor sends, verify the bytes.

    ``verify_integrity`` read payloads through ``read_text`` until v2.1.14. A
    vendor that sends CRLF had every record report HASH_MISMATCH, and a body
    that is not valid UTF-8 raised. Both are failures of the *reader*, reported
    as findings against the evidence. Decoding belongs to the parser; this
    layer answers "are these the bytes we wrote?" and nothing else.
    """
    from src.adapters.raw_store import FileRawStore, payload_hash
    from tests.certification_fixtures import AS_OF

    store = FileRawStore(tmp_path / name)
    record = store.put(
        record_id="r1",
        endpoint="/v3/option/snapshot/quote",
        query_params={"symbol": "SPXW"},
        payload=body,
        request_started_at=AS_OF,
        response_received_at=AS_OF,
        http_status=200,
    )
    assert store.get_body("r1") == body
    assert record.payload_hash == payload_hash(body)
    assert record.byte_length == len(body)

    report = store.verify_integrity()
    assert report.ok, report.counts()
    assert [f.status.value for f in report.findings] == ["VALID"]


def test_a_response_carries_its_bytes_and_a_separate_reading():
    from src.adapters.transport import DecodeStatus, HttpResponse

    body = b"a,b\n1,\xff\n"
    response = HttpResponse(status_code=200, body=body)
    assert response.body == body
    assert response.byte_length == len(body)

    decoded = response.decode_text()
    assert decoded.decode_status is DecodeStatus.REPLACED
    assert decoded.body_hash != decoded.decoded_text_hash
    assert decoded.byte_length == len(body)


def test_a_captured_record_hashes_the_bytes_on_disk(tmp_path):
    """End to end: every manifest digest is over the file, byte for byte."""
    from src.adapters.raw_store import payload_hash

    report = live_run(tmp_path)
    manifest = json.loads(run_path(report, "manifest_path").read_text(encoding="utf-8"))
    root = run_path(report, "raw_store_path")
    for record in manifest["records"]:
        stored = (root / f"{record['record_id']}.raw").read_bytes()
        assert payload_hash(stored) == record["payload_hash"]
        assert len(stored) == record["byte_length"]


def test_attempt_bodies_are_hashed_over_their_bytes(tmp_path):
    from src.adapters.http_attempts import HttpAttemptLog, HttpAttemptRecord
    from tests.certification_fixtures import AS_OF

    log = HttpAttemptLog(tmp_path / "attempts")
    body = b"vendor said: \xff\xfe"
    log.observe(
        HttpAttemptRecord(
            logical_request_id="r1",
            attempt_number=1,
            endpoint="/v3/option/snapshot/quote",
            safe_url="http://127.0.0.1:25503/v3/option/snapshot/quote",
            request_parameters_hash="p" * 64,
            started_at=AS_OF,
            status_code=500,
        ),
        body,
    )
    record = log.records[0]
    assert record.response_byte_length == len(body)
    assert not pathlib.Path(record.response_body_location).is_absolute()
    assert log.body_path(record.response_body_location).read_bytes() == body
    assert log.verify_bodies() == ()


# =============================================================================
# §4/§5 -- oversized responses and silence are both attempts
# =============================================================================


def test_an_oversized_response_produces_an_attempt_record():
    """The named regression. v2.1.12 raised before the observer ran."""
    from src.adapters.http_attempts import HttpAttemptLog
    from src.adapters.transport import (
        FakeTransport,
        HttpResponse,
        ResponseTooLargeError,
        RetryingTransport,
    )

    log = HttpAttemptLog()
    inner = FakeTransport(default=HttpResponse(status_code=200, body=b"x" * 4096))
    transport = RetryingTransport(inner, max_response_bytes=16, attempt_observer=log)
    with pytest.raises(ResponseTooLargeError):
        transport.get("http://127.0.0.1:25503/v3/option/snapshot/quote", {}, 1.0)

    assert len(log.records) == 1
    record = log.records[0]
    assert record.transport_error_code == "RESPONSE_TOO_LARGE"
    assert record.endpoint == "/v3/option/snapshot/quote"
    assert record.detail["configured_max_response_bytes"] == 16
    assert record.detail["bytes_read_before_abort"] == 4096
    assert record.status_code == 200
    assert not record.succeeded


def test_a_connection_that_never_answers_is_not_failed_before_request():
    """The named regression.

    Four attempts against a Theta Terminal that is not running produced
    ``FAILED_BEFORE_REQUEST`` in v2.1.12, because the state was derived from
    stored records. "Nothing was sent" and "nothing answered" are different
    things and an operator does different things about them.
    """
    assert (
        RawCaptureRunState.from_evidence(attempts=4, responses=0, records=0)
        is RawCaptureRunState.FAILED_NO_RESPONSE
    )
    assert (
        RawCaptureRunState.from_evidence(attempts=0, responses=0, records=0)
        is RawCaptureRunState.FAILED_BEFORE_REQUEST
    )
    assert (
        RawCaptureRunState.from_evidence(attempts=2, responses=1, records=0)
        is RawCaptureRunState.FAILED_PARTIAL
    )
    assert (
        RawCaptureRunState.from_evidence(attempts=1, responses=0, records=1)
        is RawCaptureRunState.FAILED_PARTIAL
    )


def test_a_refused_connection_run_reports_no_response(tmp_path):
    from src.adapters.transport import TransportError
    from tests.certification_fixtures import AS_OF

    class _Refusing:
        capture_origin = "OFFLINE_FIXTURE"

        def origin_for(self, url: str) -> str:
            return self.capture_origin

        def get(self, url, params, timeout_seconds):
            raise TransportError("ConnectError for http://127.0.0.1:25503")

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=_Refusing(),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
    )
    assert report["run_state"] == RawCaptureRunState.FAILED_NO_RESPONSE.value
    assert report["http_attempts"]["attempt_count"] >= 1
    assert report["record_ids"] == []
    assert run_path(report, "manifest_path").exists()


# =============================================================================
# §6 -- a vendor's refusal is not an internal error
# =============================================================================


class _StatusTransport:
    """Answers every request with one status and body."""

    capture_origin = "OFFLINE_FIXTURE"

    def __init__(self, status: int, body: bytes = b"nope", **headers: str) -> None:
        self.status = status
        self.body = body
        self.headers = headers

    def origin_for(self, url: str) -> str:
        return self.capture_origin

    def get(self, url, params, timeout_seconds):
        from src.adapters.transport import HttpResponse

        return HttpResponse(
            status_code=self.status, body=self.body, headers=dict(self.headers), url=url
        )


@pytest.mark.parametrize(
    ("status", "code", "exit_code"),
    [
        (400, "VENDOR_HTTP_ERROR", ExitCode.VENDOR_HTTP_ERROR),
        (401, "AUTHENTICATION_REJECTED", ExitCode.AUTHENTICATION_REJECTED),
        (403, "AUTHENTICATION_REJECTED", ExitCode.AUTHENTICATION_REJECTED),
        (429, "RATE_LIMITED", ExitCode.RATE_LIMITED),
        (500, "RETRY_EXHAUSTED", ExitCode.RETRY_EXHAUSTED),
    ],
)
def test_a_vendor_status_gets_its_own_classification(tmp_path, status, code, exit_code):
    """The named regressions: 400, 401 and 403 were all ``INTERNAL_ERROR``."""
    from tests.certification_fixtures import AS_OF

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / f"capture-{status}"),
        transport=_StatusTransport(status),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
    )
    assert report["error_code"] == code, report["error_code"]
    assert "INTERNAL_ERROR" not in report["error_code"]
    from src.tools.capture_thetadata_once import _EXIT_FOR

    assert _EXIT_FOR[report["error_code"]] is exit_code
    # The vendor answered and the transport refused to hand a non-success body
    # on as data, so no endpoint produced a raw record. Every endpoint was
    # still *attempted* -- see §1 -- which is what separates this from v2.1.14.
    assert report["run_state"] == RawCaptureRunState.FAILED_PARTIAL_ACQUISITION.value
    assert report["raw_acquisition"]["attempted_endpoints"], report["raw_acquisition"]


def test_a_two_hundred_vendor_error_document_is_captured_then_reported(tmp_path):
    """A 200 carrying an error document is *evidence*, and it is stored.

    v2.1.14 raised out of the fetch path, so the bytes reached the store for
    one endpoint and the rest were never requested. Now the body is acquired
    like any other, the run completes, and the parser report is where the
    finding lives.
    """
    from tests.certification_fixtures import AS_OF

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=_StatusTransport(200, b'{"error":"no data for that symbol"}'),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
    )
    assert report["run_state"] == RawCaptureRunState.COMPLETED_RAW_VERIFIED.value
    assert report["parser_state"] == "PARSER_FAILED"
    assert "INTERNAL_ERROR" not in report["error_code"]
    findings = json.loads(
        run_path(report, "parser_report_path").read_text(encoding="utf-8")
    )
    assert {entry["parser_status"] for entry in findings["endpoints"]} == {
        "PARSER_FAILED"
    }


def test_a_two_hundred_malformed_csv_is_a_parser_finding_not_a_lost_capture(tmp_path):
    """The headline v2.1.15 regression, at the operator's level.

    An HTML error page on a 200 used to raise inside ``fetch_chain``, which is
    the call that decides whether the *next* endpoint is requested. So the one
    thing the first paid session exists to discover -- an unexpected schema --
    was the thing that stopped it discovering anything else.
    """
    from tests.certification_fixtures import AS_OF

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=_StatusTransport(200, b"<html><body>not csv</body></html>"),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
    )
    acquisition = report["raw_acquisition"]
    assert acquisition["missing_endpoints"] == []
    assert sorted(acquisition["acquired_endpoints"]) == sorted(
        acquisition["planned_endpoints"]
    )
    assert report["run_state"] == RawCaptureRunState.COMPLETED_RAW_VERIFIED.value
    assert report["parser_state"] == "PARSER_FAILED"


def test_an_oversized_live_response_has_its_own_exit_code(tmp_path):
    from tests.certification_fixtures import AS_OF

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=_StatusTransport(200, b"x" * (80 * 1024 * 1024)),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
    )
    from src.tools.capture_thetadata_once import _EXIT_FOR

    assert report["error_code"] == "RESPONSE_TOO_LARGE"
    assert _EXIT_FOR[report["error_code"]] is ExitCode.RESPONSE_TOO_LARGE
    assert _EXIT_FOR[report["error_code"]] is not ExitCode.SCHEMA_ERROR
    assert report["http_attempts"]["attempt_count"] >= 1


# =============================================================================
# §7 -- Retry-After never shortens the backoff
# =============================================================================


def test_retry_after_cannot_reduce_a_later_exponential_delay():
    """The named regression.

    v2.1.12 returned ``max(retry_after, backoff_base_seconds)`` -- the *first*
    delay -- so on a later attempt a ``Retry-After: 1`` shortened an eight-second
    computed backoff to one second. The vendor asking us to wait longer is
    information; the vendor asking us to hammer sooner is not.
    """
    from src.adapters.transport import RetryingTransport, RetryPolicy

    policy = RetryPolicy(max_retries=5, backoff_base_seconds=0.5)
    transport = RetryingTransport(object(), policy=policy, random_unit=lambda: 1.0)

    computed = policy.delay_for(4, random_unit=1.0)
    assert computed > 1.0, computed
    assert transport._retry_delay(4, None, 1.0) >= computed
    # A longer instruction is still honoured.
    assert transport._retry_delay(4, None, computed * 4) >= computed * 4


# =============================================================================
# §8/§9 -- evidence that survives the run that produced it
# =============================================================================


def test_attempt_metadata_is_readable_without_the_process_that_wrote_it(tmp_path):
    from src.adapters.http_attempts import HttpAttemptLog

    report = live_run(tmp_path)
    recovered = HttpAttemptLog.recovered_from(run_path(report, "attempt_store_path"))
    assert len(recovered) == report["http_attempts"]["attempt_count"]
    assert {entry["endpoint"] for entry in recovered} == set(
        report["http_attempts"]["attempts_per_endpoint"]
    )
    assert all(
        entry["schema_version"].startswith("http-attempt/") for entry in recovered
    )


def test_a_finalization_failure_still_closes_the_transport(tmp_path, monkeypatch):
    """The named regression: v2.1.12 called ``_close`` after ``_finalize``."""
    import src.tools.capture_thetadata_once as tool
    from tests.certification_fixtures import AS_OF, vendor_transport

    closed: list[bool] = []
    original_close = tool._close

    def watched(pipeline):
        closed.append(True)
        return original_close(pipeline)

    def exploding(run, *, chain):
        raise OSError("the disk filled while writing the manifest")

    monkeypatch.setattr(tool, "_close", watched)
    monkeypatch.setattr(tool, "_finalize", exploding)

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=vendor_transport(),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
    )
    assert closed == [True]
    assert report["emergency"] is True
    assert report["manifest_written"] is False
    assert report["run_id"]
    assert report["attempt_count"] >= 1
    assert report["output_root"] == str(tmp_path / "capture")
    assert run_path(report, "summary_path").exists()
    assert report["trusted_gex_computed"] is False


def test_an_emergency_summary_reports_the_state_the_evidence_supports(
    tmp_path, monkeypatch
):
    """The named regression: ``FAILED_PARTIAL`` was hardcoded.

    A finalization that fails is a second problem, not a reclassification of the
    first. v2.1.13 stamped FAILED_PARTIAL on every emergency summary, so a run
    that never got a response reported the same state as one that lost the disk
    after three endpoints -- and an operator reading "partial" goes looking for
    bytes that are not there.
    """
    import src.tools.capture_thetadata_once as tool
    from src.adapters.transport import TransportError
    from tests.certification_fixtures import AS_OF, vendor_transport

    def exploding(run, *, chain):
        raise OSError("the disk filled while writing the manifest")

    monkeypatch.setattr(tool, "_finalize", exploding)

    class _Refused:
        """Nothing answers: attempts, no responses, no records."""

        capture_origin = "OFFLINE_FIXTURE"

        def origin_for(self, url: str) -> str:
            return self.capture_origin

        def get(self, url, params, timeout_seconds):
            raise TransportError("ConnectError for http://127.0.0.1:25503")

    refused = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "nothing-answered"),
        transport=_Refused(),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
    )
    assert refused["emergency"] is True
    assert refused["attempt_count"] >= 1
    assert refused["records_known_in_memory"] == []
    assert refused["run_state"] == RawCaptureRunState.FAILED_NO_RESPONSE.value

    # Everything answered, and finalization is what failed. That is partial.
    answered = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "answered"),
        transport=vendor_transport(),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
    )
    assert answered["emergency"] is True
    assert answered["records_known_in_memory"]
    assert answered["run_state"] == RawCaptureRunState.FAILED_PARTIAL.value


def test_a_crlf_vendor_completes_a_verified_run(tmp_path):
    """The named regression, end to end and at the operator's level.

    A vendor sending Windows line endings is not a defect and not exotic. Until
    v2.1.14 it produced a run that fetched everything, stored everything, and
    then reported every single record as HASH_MISMATCH -- an operator would have
    concluded the capture was corrupt and paid for another one.
    """
    from src.adapters.transport import FakeTransport
    from tests.certification_fixtures import payloads

    transport = FakeTransport()
    for endpoint, body in payloads().items():
        crlf = body.replace("\n", "\r\n")
        assert b"\r\n" in crlf.encode()
        transport.register_bytes(endpoint.value, crlf.encode())

    report = live_run(tmp_path, transport=transport)

    assert report["run_state"] == "COMPLETED_RAW_VERIFIED"
    assert report["integrity_ok"] is True
    assert report["capture_verified"] is True
    assert report["verification_failures"] == []
    # The stored bytes are the vendor's, carriage returns included.
    for path in run_path(report, "raw_store_path").glob("*.raw"):
        assert b"\r\n" in path.read_bytes()


def test_a_run_directory_verifies_after_it_has_been_moved(tmp_path):
    """The named regression: evidence must not depend on where it was made.

    Every location a run recorded was absolute through v2.1.13, so a capture
    archived to a NAS, restored on a different host, or simply renamed carried
    an index full of paths to a directory that was not there. The point of a
    raw capture is that somebody else can check it; a directory that only
    verifies on the machine that produced it does not do that.
    """
    import shutil

    from src.adapters.http_attempts import HttpAttemptLog
    from src.adapters.raw_store import FileRawStore

    report = live_run(tmp_path, destination=tmp_path / "original")
    assert report["run_state"] == "COMPLETED_RAW_VERIFIED"

    archive = tmp_path / "archive" / "renamed-capture"
    shutil.copytree(tmp_path / "original", archive)
    shutil.rmtree(tmp_path / "original")
    assert not (tmp_path / "original").exists()

    # Nothing inside points back at where it was written.
    index = (archive / "raw" / "index.jsonl").read_text(encoding="utf-8")
    for line in index.splitlines():
        location = json.loads(line)["payload_location"]
        assert not pathlib.Path(location).is_absolute(), location
        assert (archive / "raw" / location).is_file()

    integrity = FileRawStore(archive / "raw").verify_integrity()
    assert integrity.ok, integrity.counts()
    moved_attempts = HttpAttemptLog.open_existing(archive / "attempts")
    assert moved_attempts.ok, moved_attempts.findings

    # And the summary still describes a directory, once told where it now is.
    moved = dict(
        json.loads((archive / "capture-summary.json").read_text(encoding="utf-8")),
        output_root=str(archive),
    )
    assert run_path(moved, "manifest_path").is_file()
    assert run_path(moved, "intent_path").is_file()


# =============================================================================
# Shared helper
# =============================================================================


def live_run(tmp_path, *, destination=None, transport=None):
    from tests.certification_fixtures import AS_OF, vendor_transport

    return run_capture(
        CAPTURE_CONFIG,
        output=str(destination if destination is not None else tmp_path / "capture"),
        transport=transport if transport is not None else vendor_transport(),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
    )
