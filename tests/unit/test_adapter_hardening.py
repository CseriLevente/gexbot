"""Adapter infrastructure that must not fail quietly before a paid capture.

Seven v2.1.1 defects, all of the same family: something that looked wired up,
wasn't.

* **§6** ``max_response_bytes`` reached ``RetryingTransport`` but not
  ``HttpxTransport``, which is the layer that actually reads chunks. The
  configured cap did not govern the streaming read.
* **§7** Automatic capture session ids were built from the market ``as_of``.
  Two fetches at the same market timestamp produced the same session id, and
  the store is append-only, so the second one raised.
* **§11** The integrity scanner resolved a file path from metadata before
  checking that the metadata was well-formed. Malformed metadata crashed the
  scanner that exists to report malformed metadata.
* **§12** ``base_url`` was checked for scheme and netloc only, so
  ``http://user:secret@host`` passed -- putting a credential in every logged
  URL. ``raw_capture_path`` was ``str()``-converted, so ``42`` became a path.
* **§13** ``rate_type: null`` was replaced with ``"sofr"`` after loading, so the
  stored config and the outgoing request disagreed.
* **§14** Replay hashing excluded warnings entirely. A snapshot that started
  emitting a new warning code hashed identically to one that did not.
* **§18** Transport, parser and raw-store failures had unrelated base classes,
  so a caller had to know which layer failed in order to catch it.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from src.adapters.raw_store import FileRawStore, IntegrityStatus, build_record_id
from src.adapters.thetadata.client import ThetaDataError
from src.adapters.transport import FakeTransport, HttpResponse
from src.config.thetadata import (
    ThetaDataConfigError,
    ThetaDataRuntime,
    parse_thetadata_config,
)
from src.gex.sessions import eastern

AS_OF = eastern(2026, 3, 17, 11, 0)


def config(**overrides):
    return parse_thetadata_config(overrides)


# =============================================================================
# §6 -- the cap reaches the transport that reads chunks
# =============================================================================


def test_the_configured_cap_reaches_httpx_transport():
    """The regression: v2.1.1 passed it only to the retry wrapper."""
    from src.config.thetadata import httpx_transport_kwargs

    kwargs = httpx_transport_kwargs(config(max_response_bytes=4096))
    assert kwargs["max_response_bytes"] == 4096


def test_the_default_cap_is_not_used_when_config_states_another():
    from src.adapters.transport import DEFAULT_MAX_RESPONSE_BYTES
    from src.config.thetadata import httpx_transport_kwargs

    kwargs = httpx_transport_kwargs(config(max_response_bytes=2048))
    assert kwargs["max_response_bytes"] != DEFAULT_MAX_RESPONSE_BYTES


def test_the_runtime_exposes_the_effective_cap():
    runtime = ThetaDataRuntime.from_config(
        config(max_response_bytes=8192), transport=FakeTransport()
    )
    assert runtime.effective_limits()["max_response_bytes"] == 8192


def test_the_inner_and_outer_limits_cannot_disagree():
    """One authoritative number, read by both layers."""
    built = config(max_response_bytes=5000)
    runtime = ThetaDataRuntime.from_config(built, transport=FakeTransport())
    from src.config.thetadata import httpx_transport_kwargs

    assert (
        runtime.effective_limits()["max_response_bytes"]
        == httpx_transport_kwargs(built)["max_response_bytes"]
        == built.max_response_bytes
    )


def test_timeouts_reach_the_transport_too():
    from src.config.thetadata import httpx_transport_kwargs

    kwargs = httpx_transport_kwargs(config(timeout_seconds=17.0))
    assert kwargs["read_timeout_seconds"] == pytest.approx(17.0)


# =============================================================================
# §7 -- capture sessions cannot collide
# =============================================================================


def csv_body() -> HttpResponse:
    from tests.unit.test_thetadata_runtime import csv_response

    return csv_response()


def runtime_with_capture(tmp_path):
    return ThetaDataRuntime.from_config(
        config(raw_capture_enabled=True, raw_capture_path=str(tmp_path / "raw")),
        transport=FakeTransport(default=csv_body()),
        clock=lambda: AS_OF,
    )


def fetch(runtime):
    from datetime import date

    return runtime.fetch_chain(
        as_of=AS_OF,
        spot=5000.25,
        spot_timestamp=AS_OF - timedelta(milliseconds=500),
        open_interest_as_of=date(2026, 3, 16),
    )


def test_two_fetches_at_the_same_market_timestamp_both_succeed(tmp_path):
    """The regression: the id was derived from as_of, so the second collided."""
    runtime = runtime_with_capture(tmp_path)
    fetch(runtime)
    fetch(runtime)  # must not raise


def test_two_runtimes_created_in_the_same_second_do_not_collide(tmp_path):
    first = runtime_with_capture(tmp_path)
    second = runtime_with_capture(tmp_path)
    fetch(first)
    fetch(second)


def test_session_ids_differ_across_runs():
    from src.adapters.raw_store import new_capture_session_id

    ids = {new_capture_session_id(as_of=AS_OF) for _ in range(50)}
    assert len(ids) == 50


def test_the_session_id_keeps_market_time_as_audit_metadata():
    from src.adapters.raw_store import new_capture_session_id

    generated = new_capture_session_id(as_of=AS_OF)
    assert "2026" in generated


def test_session_ids_are_filesystem_safe():
    from src.adapters.raw_store import new_capture_session_id

    generated = new_capture_session_id(as_of=AS_OF)
    assert not (set(generated) & set('/\\:*?"<>|'))


def test_request_sequence_stays_deterministic_within_a_session():
    from src.adapters.raw_store import CaptureSession, InMemoryRawStore

    session = CaptureSession(store=InMemoryRawStore(), session_id="fixed")
    for _ in range(3):
        session.capture(
            endpoint="/v3/option/snapshot/quote",
            query_params={"symbol": "SPXW"},
            payload="x",
            request_started_at=AS_OF,
            response_received_at=AS_OF,
            http_status=200,
        )
    assert [r.request_sequence for r in session.captured] == [1, 2, 3]


# =============================================================================
# §11 -- the integrity scanner validates metadata before touching paths
# =============================================================================


def store_with_record(tmp_path):
    store = FileRawStore(tmp_path / "raw")
    record = store.put(
        record_id=build_record_id(
            session_id="s1",
            sequence=1,
            endpoint="/v3/option/snapshot/quote",
            query_params={"symbol": "SPXW"},
            payload="hello",
        ),
        endpoint="/v3/option/snapshot/quote",
        query_params={"symbol": "SPXW"},
        payload="hello",
        request_started_at=AS_OF,
        response_received_at=AS_OF + timedelta(milliseconds=5),
        http_status=200,
        request_sequence=1,
    )
    return store, record


def append_metadata(store, payload: dict) -> None:
    with (store.root / "index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def test_an_unsafe_record_id_is_a_structured_finding(tmp_path):
    """The regression: v2.1.1 resolved the path first and raised."""
    store, record = store_with_record(tmp_path)
    bad = record.as_dict() | {"record_id": "../../etc/passwd"}
    append_metadata(store, bad)
    report = store.verify_integrity()
    assert any(f.status is IntegrityStatus.UNSAFE_RECORD_ID for f in report.findings)


def test_a_path_traversal_attempt_never_resolves_a_path(tmp_path):
    store, record = store_with_record(tmp_path)
    append_metadata(store, record.as_dict() | {"record_id": "..\\..\\secrets"})
    store.verify_integrity()  # must not raise


@pytest.mark.parametrize(
    "field",
    ["record_id", "endpoint", "payload_hash", "byte_length", "request_started_at"],
)
def test_a_missing_required_field_is_reported(tmp_path, field):
    store, record = store_with_record(tmp_path)
    payload = record.as_dict()
    payload.pop(field)
    append_metadata(store, payload)
    report = store.verify_integrity()
    assert any(f.status is IntegrityStatus.INVALID_METADATA for f in report.findings)


def test_a_wrongly_typed_byte_length_is_reported(tmp_path):
    store, record = store_with_record(tmp_path)
    append_metadata(
        store, record.as_dict() | {"byte_length": "many", "record_id": "b1"}
    )
    statuses = {f.status for f in store.verify_integrity().findings}
    assert IntegrityStatus.INVALID_BYTE_LENGTH in statuses


def test_a_negative_byte_length_is_reported(tmp_path):
    store, record = store_with_record(tmp_path)
    append_metadata(store, record.as_dict() | {"byte_length": -1, "record_id": "b2"})
    statuses = {f.status for f in store.verify_integrity().findings}
    assert IntegrityStatus.INVALID_BYTE_LENGTH in statuses


def test_an_invalid_payload_hash_is_reported(tmp_path):
    store, record = store_with_record(tmp_path)
    append_metadata(
        store, record.as_dict() | {"payload_hash": "zzz", "record_id": "h1"}
    )
    statuses = {f.status for f in store.verify_integrity().findings}
    assert IntegrityStatus.INVALID_HASH in statuses


def test_an_invalid_timestamp_is_reported(tmp_path):
    store, record = store_with_record(tmp_path)
    append_metadata(
        store, record.as_dict() | {"request_started_at": "yesterday", "record_id": "t1"}
    )
    statuses = {f.status for f in store.verify_integrity().findings}
    assert IntegrityStatus.INVALID_TIMESTAMP in statuses


def test_invalid_json_is_reported_not_raised(tmp_path):
    store, _ = store_with_record(tmp_path)
    with (store.root / "index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    report = store.verify_integrity()
    assert any(f.status is IntegrityStatus.INVALID_METADATA for f in report.findings)


def test_a_duplicate_record_id_is_still_reported(tmp_path):
    store, record = store_with_record(tmp_path)
    append_metadata(store, record.as_dict())
    statuses = {f.status for f in store.verify_integrity().findings}
    assert IntegrityStatus.DUPLICATE_ID in statuses


def test_the_scanner_never_raises_on_any_malformed_metadata(tmp_path):
    """One test to state the property directly."""
    store, record = store_with_record(tmp_path)
    for index, mutation in enumerate(
        [
            {"record_id": None},
            {"record_id": ""},
            {"endpoint": 42},
            {"payload_hash": None},
            {"byte_length": None},
            {"request_started_at": None},
            {"capture_complete": "maybe"},
            {"parser_version": 7},
        ]
    ):
        append_metadata(store, record.as_dict() | mutation | {"request_id": str(index)})
    report = store.verify_integrity()  # must not raise
    assert not report.ok


# =============================================================================
# §12 -- unsafe URLs and paths
# =============================================================================


@pytest.mark.parametrize(
    "url",
    [
        "http://user:secret@localhost:25503",
        "http://user@localhost:25503",
        "http://localhost:25503?token=secret",
        "http://localhost:25503#secret",
        "file:///tmp/vendor",
        "ftp://vendor.example.com",
    ],
)
def test_an_unsafe_base_url_is_refused(url):
    with pytest.raises(ThetaDataConfigError):
        config(base_url=url)


def test_the_error_does_not_echo_the_secret():
    with pytest.raises(ThetaDataConfigError) as excinfo:
        config(base_url="http://user:hunter2@localhost:25503")
    assert "hunter2" not in str(excinfo.value)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:25503", "https://vendor.example.com", "http://localhost"]
)
def test_a_safe_base_url_is_accepted(url):
    assert config(base_url=url).base_url == url


@pytest.mark.parametrize("value", [42, True, 3.5, [], {}])
def test_a_non_string_raw_capture_path_is_refused(value):
    with pytest.raises(ThetaDataConfigError, match=r"(?i)raw_capture_path"):
        config(raw_capture_enabled=True, raw_capture_path=value)


def test_an_empty_raw_capture_path_is_refused_when_capture_is_enabled():
    with pytest.raises(ThetaDataConfigError):
        config(raw_capture_enabled=True, raw_capture_path="")


def test_a_valid_raw_capture_path_is_accepted(tmp_path):
    built = config(raw_capture_enabled=True, raw_capture_path=str(tmp_path / "raw"))
    assert built.raw_capture_path is not None


# =============================================================================
# §13 -- no hidden rate_type substitution
# =============================================================================


def test_a_null_rate_type_is_not_replaced_with_sofr():
    """The regression: the stored config said None, the request said sofr."""
    built = config(rate_type=None)
    assert built.rate_type is None


def test_a_null_rate_type_omits_the_parameter_entirely():
    transport = FakeTransport(default=csv_body())
    runtime = ThetaDataRuntime.from_config(
        config(rate_type=None), transport=transport, clock=lambda: AS_OF
    )
    fetch(runtime)
    assert not any("rate_type=" in url for url in transport.urls())


def test_the_stored_config_matches_the_effective_request():
    transport = FakeTransport(default=csv_body())
    runtime = ThetaDataRuntime.from_config(
        config(rate_type="sofr"), transport=transport, clock=lambda: AS_OF
    )
    fetch(runtime)
    assert any("rate_type=sofr" in url for url in transport.urls())


def test_omission_is_recorded_as_the_vendor_default_applying():
    built = config(rate_type=None)
    assert "vendor default" in built.rate_type_policy().lower()


# =============================================================================
# §14 -- warning codes participate in the replay hash
# =============================================================================


def snapshot_with(**overrides):
    from src.gex.engine import compute_gex_snapshot
    from src.synthetic.chains import build_synthetic_chain

    return compute_gex_snapshot(build_synthetic_chain(), **overrides)


def test_changing_a_warning_code_changes_the_hash():
    from dataclasses import replace as dc_replace

    snapshot = snapshot_with()
    baseline = snapshot.output_hash()
    mutated = dc_replace(
        snapshot,
        confidence=dc_replace(
            snapshot.confidence,
            components=tuple(
                dc_replace(c, warning_code="A_NEW_CODE") if i == 0 else c
                for i, c in enumerate(snapshot.confidence.components)
            ),
        ),
    )
    assert mutated.output_hash() != baseline


def test_changing_only_human_detail_does_not_change_the_hash():
    from dataclasses import replace as dc_replace

    snapshot = snapshot_with()
    baseline = snapshot.output_hash()
    reworded = dc_replace(
        snapshot,
        confidence=dc_replace(
            snapshot.confidence,
            components=tuple(
                dc_replace(c, detail="entirely different prose")
                for c in snapshot.confidence.components
            ),
        ),
    )
    assert reworded.output_hash() == baseline


def test_reordering_warning_codes_does_not_change_the_hash():
    from dataclasses import replace as dc_replace

    snapshot = snapshot_with()
    baseline = snapshot.output_hash()
    shuffled = dc_replace(
        snapshot,
        confidence=dc_replace(
            snapshot.confidence, warnings=tuple(reversed(snapshot.confidence.warnings))
        ),
    )
    assert shuffled.output_hash() == baseline


def test_duplicate_warning_codes_are_canonicalised():
    from dataclasses import replace as dc_replace

    snapshot = snapshot_with()
    baseline = snapshot.output_hash()
    doubled = dc_replace(
        snapshot,
        confidence=dc_replace(
            snapshot.confidence, warnings=snapshot.confidence.warnings * 2
        ),
    )
    assert doubled.output_hash() == baseline


# =============================================================================
# §18 -- one exception hierarchy
# =============================================================================


def client_for(response: HttpResponse):
    from src.adapters.thetadata.client import ThetaDataClient, ThetaDataSettings

    return ThetaDataClient(
        settings=ThetaDataSettings(),
        transport=FakeTransport(default=response),
        clock=lambda: AS_OF,
    )


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_every_http_status_maps_into_the_adapter_hierarchy(status):
    from src.adapters.thetadata.client import ChainRequest

    with pytest.raises(ThetaDataError):
        client_for(HttpResponse(status_code=status, text="err")).option_quotes(
            ChainRequest(symbol="SPXW")
        )


@pytest.mark.parametrize(
    ("status", "expected"), [(401, "Authentication"), (403, "Authentication")]
)
def test_auth_statuses_map_to_an_authentication_error(status, expected):
    from src.adapters.thetadata.client import ChainRequest, ThetaDataAuthenticationError

    with pytest.raises(ThetaDataAuthenticationError):
        client_for(HttpResponse(status_code=status, text="nope")).option_quotes(
            ChainRequest(symbol="SPXW")
        )
    assert expected


def test_429_maps_to_a_rate_limit_error():
    from src.adapters.thetadata.client import ChainRequest, ThetaDataRateLimitError

    with pytest.raises(ThetaDataRateLimitError):
        client_for(HttpResponse(status_code=429, text="slow")).option_quotes(
            ChainRequest(symbol="SPXW")
        )


def test_retry_exhaustion_maps_to_an_adapter_error():
    from src.adapters.thetadata.client import (
        ChainRequest,
        ThetaDataClient,
        ThetaDataRetryExhaustedError,
        ThetaDataSettings,
    )
    from src.adapters.transport import RetryingTransport, RetryPolicy

    inner = FakeTransport(default=HttpResponse(status_code=503, text="down"))
    client = ThetaDataClient(
        settings=ThetaDataSettings(),
        transport=RetryingTransport(
            inner, policy=RetryPolicy(max_retries=1), sleep=lambda _: None
        ),
        clock=lambda: AS_OF,
    )
    with pytest.raises(ThetaDataRetryExhaustedError):
        client.option_quotes(ChainRequest(symbol="SPXW"))


def test_a_schema_failure_is_an_adapter_error():
    from src.adapters.thetadata.client import ChainRequest, ThetaDataSchemaError

    with pytest.raises(ThetaDataSchemaError):
        client_for(
            HttpResponse(status_code=200, text="wrong,columns\n1,2\n")
        ).option_quotes(ChainRequest(symbol="SPXW"))


def test_a_raw_store_failure_is_an_adapter_error(tmp_path):
    from src.adapters.raw_store import RawStoreError

    assert issubclass(RawStoreError, ThetaDataError)


@pytest.mark.parametrize(
    "name",
    [
        "ThetaDataConfigurationError",
        "ThetaDataAuthenticationError",
        "ThetaDataHTTPError",
        "ThetaDataRateLimitError",
        "ThetaDataRetryExhaustedError",
        "ThetaDataSchemaError",
        "ThetaDataVendorError",
    ],
)
def test_every_declared_subclass_derives_from_the_base(name):
    import src.adapters.thetadata.client as client_module

    assert issubclass(getattr(client_module, name), ThetaDataError), name


def test_the_error_preserves_the_status_and_request_id():
    from src.adapters.thetadata.client import ChainRequest, ThetaDataHTTPError

    with pytest.raises(ThetaDataHTTPError) as excinfo:
        client_for(
            HttpResponse(status_code=500, text="boom", request_id="req-42")
        ).option_quotes(ChainRequest(symbol="SPXW"))
    assert excinfo.value.status_code == 500
    assert excinfo.value.request_id == "req-42"


def test_the_error_redacts_secrets():
    from src.adapters.thetadata.client import ChainRequest

    with pytest.raises(ThetaDataError) as excinfo:
        client_for(
            HttpResponse(status_code=500, text="token=hunter2 failed")
        ).option_quotes(ChainRequest(symbol="SPXW"))
    assert "hunter2" not in str(excinfo.value)


def test_a_caller_needs_only_the_base_class():
    """The property the section exists for."""
    from src.adapters.thetadata.client import ChainRequest

    for status in (400, 401, 429, 500):
        try:
            client_for(HttpResponse(status_code=status, text="x")).option_quotes(
                ChainRequest(symbol="SPXW")
            )
        except ThetaDataError:
            continue
        pytest.fail(f"status {status} escaped the adapter hierarchy")
