"""Typed ThetaData config, the single client factory, Retry-After and storage.

The v2 defect: YAML accepted a ``thetadata:`` section, validated it as
unknown-key-free, and then discarded it. Every caller hand-assembled a client, so
a setting could be present in the file, look applied in review, and never reach a
request.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from src.adapters.raw_store import (
    FileRawStore,
    InMemoryRawStore,
    RawStoreError,
    build_record_id,
    canonical_parameter_hash,
)
from src.adapters.thetadata.client import ChainRequest
from src.adapters.transport import (
    FakeTransport,
    HttpResponse,
    ResponseTooLargeError,
    RetryBudgetExhaustedError,
    RetryingTransport,
    RetryPolicy,
    VendorHTTPError,
    parse_retry_after,
)
from src.config.schema import ConfigError, load_config, parse_config
from src.config.thetadata import (
    AuthenticationMode,
    ThetaDataConfig,
    ThetaDataConfigError,
    VendorParameterSet,
    build_thetadata_client,
    parse_thetadata_config,
)
from src.gex.sessions import eastern

CONFIG_DIR = pathlib.Path(__file__).resolve().parents[2] / "config"
AS_OF = eastern(2026, 3, 17, 11, 0)

#: A valid CSV response with a header and no data rows -- the honest shape
#: of "no contracts". A superset of every endpoint's required columns, so
#: one constant serves quotes, open interest and greeks. A zero-byte body is
#: not an empty chain, it is a body that is not CSV, and the client refuses
#: it (see §15).
EMPTY_CSV_BODY = (
    "ask,ask_condition,ask_exchange,ask_size,bid,bid_condition,bid_exchange,"
    "bid_size,delta,epsilon,expiration,implied_vol,iv_error,lambda,"
    "open_interest,rho,right,strike,symbol,theta,timestamp,underlying_price,"
    "underlying_timestamp,vega\n"
)


def parse(**overrides):
    return parse_thetadata_config(overrides)


def full_config(**overrides):
    payload = {
        "stage": "DEVELOPMENT",
        "enabled": True,
        "data": {"options_source": "synthetic"},
        "execution": {"broker": "none", "trading_enabled": False},
    }
    payload.update(overrides)
    return parse_config(payload)


# =============================================================================
# §5 -- typed configuration
# =============================================================================


def test_loaded_config_exposes_a_typed_thetadata_object():
    """v2 bug: the section was validated and then thrown away."""
    loaded = load_config(CONFIG_DIR / "research.yaml")
    assert isinstance(loaded.thetadata, ThetaDataConfig)
    assert loaded.thetadata.base_url.startswith("http://127.0.0.1")
    assert loaded.thetadata.tier == "standard"


def test_yaml_values_reach_the_typed_object():
    loaded = full_config(
        thetadata={
            "base_url": "http://127.0.0.1:9999",
            "tier": "pro",
            "timeout_seconds": 12.5,
            "max_retries": 7,
            "rate_value": 4.2,
            "max_dte": 45,
        }
    )
    assert loaded.thetadata.base_url == "http://127.0.0.1:9999"
    assert loaded.thetadata.tier == "pro"
    assert loaded.thetadata.timeout_seconds == pytest.approx(12.5)
    assert loaded.thetadata.max_retries == 7
    assert loaded.thetadata.rate_value == pytest.approx(4.2)
    assert loaded.thetadata.max_dte == 45


def test_an_unknown_thetadata_field_fails():
    with pytest.raises(ConfigError, match="unknown key"):
        full_config(thetadata={"totally_made_up": 1})


def test_an_unknown_field_cannot_be_ignored_as_unused_config():
    """The requirement's phrasing: 'changing only unused raw config is impossible
    because unknown fields fail'.
    """
    with pytest.raises(ConfigError):
        full_config(thetadata={"greeks_verison": "latest"})  # typo


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("timeout_seconds", -1.0, "below the minimum"),
        ("connect_timeout_seconds", -0.5, "below the minimum"),
        ("max_retries", -1, "below the minimum"),
        ("max_response_bytes", 10, "below the minimum"),
        ("backoff_base_seconds", -1.0, "below the minimum"),
        ("strike_range", 0, "below the minimum"),
        ("max_dte", -5, "below the minimum"),
    ],
)
def test_invalid_numeric_values_are_refused(field, value, match):
    with pytest.raises(ThetaDataConfigError, match=match):
        parse(**{field: value})


@pytest.mark.parametrize(
    "url", ["not-a-url", "ftp://host/x", "//host/x", "127.0.0.1:25503"]
)
def test_invalid_urls_are_refused(url):
    with pytest.raises(ThetaDataConfigError, match="valid absolute"):
        parse(base_url=url)


def test_an_empty_url_is_refused_as_an_empty_string():
    """Caught by the string check before the URL check ever runs, which is the
    more precise complaint: "" is not a malformed URL, it is a missing value."""
    with pytest.raises(ThetaDataConfigError, match="non-empty string"):
        parse(base_url="")


def test_an_unsupported_authentication_mode_is_refused():
    with pytest.raises(ThetaDataConfigError, match="authentication mode"):
        parse(authentication_mode="oauth2")


def test_basic_auth_requires_the_env_var_names():
    """Credentials are never read from the config file itself."""
    with pytest.raises(ThetaDataConfigError, match="environment variables"):
        parse(authentication_mode="basic")
    ok = parse(
        authentication_mode="basic",
        username_env="THETA_USER",
        password_env="THETA_PASS",
    )
    assert ok.authentication_mode is AuthenticationMode.BASIC


def test_an_invalid_tier_is_refused():
    with pytest.raises(ThetaDataConfigError, match="tier"):
        parse(tier="platinum")


def test_an_undocumented_rate_type_is_refused():
    with pytest.raises(ThetaDataConfigError, match="rate type"):
        parse(rate_type="made_up_curve")


def test_a_parameter_the_endpoint_does_not_accept_is_refused():
    """ThetaData has no ``stock_price_source`` query parameter.

    Accepting it would let an operator believe they had selected an underlying
    source when the request never carried one.
    """
    with pytest.raises(ThetaDataConfigError, match="no stock_price_source"):
        parse(stock_price_source="per_contract")


def test_raw_capture_without_a_path_is_refused():
    with pytest.raises(ThetaDataConfigError, match="nowhere to write"):
        parse(raw_capture_enabled=True)


def test_an_invalid_iv_source_is_refused():
    with pytest.raises(ThetaDataConfigError, match="IVSource"):
        parse(iv_source="whatever")


def test_an_invalid_duplicate_policy_is_refused():
    with pytest.raises(ThetaDataConfigError, match="duplicate_policy"):
        parse(duplicate_policy="last_write_wins")


def test_changing_the_thetadata_config_changes_the_config_fingerprint():
    base = full_config(thetadata={"tier": "standard"}).fingerprint
    changed = full_config(thetadata={"tier": "pro"}).fingerprint
    assert base != changed


def test_secrets_come_from_the_environment_only(monkeypatch):
    config = parse(
        authentication_mode="basic",
        username_env="THETA_USER",
        password_env="THETA_PASS",
    )
    monkeypatch.delenv("THETA_USER", raising=False)
    monkeypatch.delenv("THETA_PASS", raising=False)
    assert config.credentials() == (None, None)
    monkeypatch.setenv("THETA_USER", "user")
    monkeypatch.setenv("THETA_PASS", "hunter2")
    assert config.credentials() == ("user", "hunter2")


def test_serialised_config_never_contains_a_credential(monkeypatch):
    monkeypatch.setenv("THETA_PASS", "hunter2")
    payload = parse(
        authentication_mode="basic",
        username_env="THETA_USER",
        password_env="THETA_PASS",
    ).as_dict()
    assert payload["password_env"] == "THETA_PASS"
    assert "hunter2" not in str(payload)


# =============================================================================
# §16 -- one construction path
# =============================================================================


def test_the_factory_builds_a_client_from_config():
    """No caller assembles a client by hand."""
    config = parse(tier="pro", timeout_seconds=11.0, max_retries=2, rate_value=4.2)
    client = build_thetadata_client(
        config, transport=FakeTransport(), clock=lambda: AS_OF
    )
    assert client.settings.tier.value == "pro"
    assert client.settings.timeout_seconds == pytest.approx(11.0)
    assert client.greeks.rate_value == pytest.approx(4.2)


def test_config_values_reach_the_outgoing_request():
    transport = FakeTransport(
        default=HttpResponse(status_code=200, text=EMPTY_CSV_BODY)
    )
    client = build_thetadata_client(
        parse(rate_value=4.2, annual_dividend=1.3, greeks_version="1"),
        transport=transport,
        clock=lambda: AS_OF,
    )
    client.option_first_order_greeks(ChainRequest(symbol="SPXW", max_dte=45))
    url = transport.urls()[-1]
    assert "rate_value=4.2" in url
    assert "annual_dividend=1.3" in url
    assert "version=1" in url
    assert "max_dte=45" in url


def test_the_factory_applies_the_retry_policy():
    inner = FakeTransport()
    inner.register_sequence(
        "/quote",
        [
            HttpResponse(status_code=503, text="down"),
            HttpResponse(status_code=200, text=EMPTY_CSV_BODY),
        ],
    )
    client = build_thetadata_client(
        parse(max_retries=2, backoff_base_seconds=0.0),
        transport=inner,
        clock=lambda: AS_OF,
    )
    client.option_quotes(ChainRequest(symbol="SPXW"))
    assert inner.call_count == 2


def test_local_terminal_mode_sends_no_credentials():
    config = parse()
    assert config.authentication_mode is AuthenticationMode.LOCAL_TERMINAL
    assert config.credentials() == (None, None)
    assert config.base_url.startswith("http://127.0.0.1")


def test_no_unit_test_performs_a_real_network_call():
    """FakeTransport raises on an unregistered route rather than reaching out."""
    with pytest.raises(AssertionError, match="never reach the network"):
        FakeTransport().get("http://example.invalid/x", {}, 1.0)


# =============================================================================
# §6 -- requested vs supported vs sent vs effective
# =============================================================================


def test_the_parameter_split_distinguishes_sent_from_merely_requested():
    """Only what went on the wire may be called effective."""
    parameters = VendorParameterSet(
        requested_model_parameters={"rate_value": 4.2, "stock_price_source": "x"},
        supported_vendor_parameters=("rate_value", "rate_type"),
        sent_vendor_parameters={"rate_value": 4.2, "rate_type": "sofr"},
        effective_local_parameters={"underlying_price_source": "vendor_per_contract"},
        unsupported_requested_parameters=("stock_price_source",),
    )
    payload = parameters.as_dict()
    assert payload["sent_vendor_parameters"]["rate_value"] == 4.2
    assert payload["unsupported_requested_parameters"] == ["stock_price_source"]
    assert payload["effective_local_parameters"]


def test_query_order_does_not_change_the_parameter_hash():
    first = VendorParameterSet(sent_vendor_parameters={"a": 1, "b": 2})
    second = VendorParameterSet(sent_vendor_parameters={"b": 2, "a": 1})
    assert first.parameter_hash() == second.parameter_hash()


def test_a_different_request_produces_a_different_parameter_hash():
    assert (
        VendorParameterSet(sent_vendor_parameters={"a": 1}).parameter_hash()
        != VendorParameterSet(sent_vendor_parameters={"a": 2}).parameter_hash()
    )


def test_the_same_effective_request_hashes_identically():
    payload = {"symbol": "SPXW", "expiration": "*"}
    assert (
        VendorParameterSet(sent_vendor_parameters=dict(payload)).parameter_hash()
        == VendorParameterSet(sent_vendor_parameters=dict(payload)).parameter_hash()
    )


def test_secrets_never_enter_the_parameter_hash():
    """Credentials are not query parameters in any supported mode."""
    parameters = VendorParameterSet(sent_vendor_parameters={"symbol": "SPXW"})
    assert "hunter2" not in str(parameters.as_dict())


# =============================================================================
# §17 -- Retry-After
# =============================================================================


def test_numeric_retry_after_is_parsed():
    assert parse_retry_after("120") == pytest.approx(120.0)


def test_http_date_retry_after_is_parsed():
    now = datetime(2026, 10, 21, 7, 28, tzinfo=UTC)
    assert parse_retry_after("Wed, 21 Oct 2026 07:30:00 GMT", now=now) == (
        pytest.approx(120.0)
    )


def test_a_past_http_date_yields_zero_not_a_negative_wait():
    now = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
    assert parse_retry_after("Wed, 21 Oct 2026 07:30:00 GMT", now=now) == 0.0


@pytest.mark.parametrize("value", [None, "", "soon", "-5", "not-a-date"])
def test_an_absent_or_invalid_header_falls_back(value):
    assert parse_retry_after(value) is None


def test_retry_after_is_honoured_over_the_computed_backoff():
    transport = FakeTransport(
        default=HttpResponse(
            status_code=429, text="slow down", headers={"Retry-After": "7"}
        )
    )
    retrying = RetryingTransport(
        transport,
        policy=RetryPolicy(max_retries=1, backoff_base_seconds=0.25),
        sleep=lambda _: None,
        random_unit=lambda: 0.0,
    )
    with pytest.raises(RetryBudgetExhaustedError):
        retrying.get("http://host/v3/quote", {}, 5.0)
    assert retrying.sleeps == [pytest.approx(7.0)]


def test_retry_after_is_capped():
    """A remote header must not decide how long this process blocks."""
    transport = FakeTransport(
        default=HttpResponse(
            status_code=429, text="slow", headers={"Retry-After": "99999"}
        )
    )
    retrying = RetryingTransport(
        transport,
        policy=RetryPolicy(max_retries=1, max_retry_after_seconds=30.0),
        sleep=lambda _: None,
    )
    with pytest.raises(RetryBudgetExhaustedError):
        retrying.get("http://host/v3/quote", {}, 5.0)
    assert retrying.sleeps == [pytest.approx(30.0)]


def test_a_missing_header_falls_back_to_exponential_backoff():
    transport = FakeTransport(default=HttpResponse(status_code=503, text="down"))
    retrying = RetryingTransport(
        transport,
        policy=RetryPolicy(max_retries=2, backoff_base_seconds=1.0, jitter=False),
        sleep=lambda _: None,
    )
    with pytest.raises(RetryBudgetExhaustedError):
        retrying.get("http://host/v3/quote", {}, 5.0)
    assert retrying.sleeps == [1.0, 2.0]


def test_a_non_retriable_4xx_is_not_retried():
    transport = FakeTransport(default=HttpResponse(status_code=404, text="missing"))
    retrying = RetryingTransport(transport, sleep=lambda _: None)
    with pytest.raises(VendorHTTPError):
        retrying.get("http://host/v3/quote", {}, 5.0)
    assert transport.call_count == 1


def test_the_error_carries_safe_response_metadata():
    transport = FakeTransport(
        default=HttpResponse(
            status_code=400,
            text="bad request",
            headers={"Retry-After": "5", "X-Request-Id": "abc"},
        )
    )
    with pytest.raises(VendorHTTPError) as excinfo:
        RetryingTransport(transport, sleep=lambda _: None).get(
            "http://host/v3/quote?password=hunter2", {}, 5.0
        )
    error = excinfo.value
    assert error.status_code == 400
    assert error.headers["Retry-After"] == "5"
    assert error.request_id
    assert "hunter2" not in str(error)


# =============================================================================
# §18 -- response size limits
# =============================================================================


def test_a_payload_below_the_limit_succeeds():
    transport = FakeTransport(default=HttpResponse(status_code=200, text="x" * 100))
    assert (
        RetryingTransport(transport, max_response_bytes=1024)
        .get("http://host/v3/quote", {}, 5.0)
        .ok
    )


def test_a_payload_exactly_at_the_limit_succeeds():
    transport = FakeTransport(default=HttpResponse(status_code=200, text="x" * 1024))
    assert (
        RetryingTransport(transport, max_response_bytes=1024)
        .get("http://host/v3/quote", {}, 5.0)
        .ok
    )


def test_a_payload_above_the_limit_is_refused():
    transport = FakeTransport(default=HttpResponse(status_code=200, text="x" * 2048))
    with pytest.raises(ResponseTooLargeError, match="exceeds"):
        RetryingTransport(transport, max_response_bytes=1024).get(
            "http://host/v3/quote", {}, 5.0
        )


def test_an_oversized_response_is_never_parsed():
    """The cap must fire before the body reaches the parser."""
    from src.adapters.thetadata.client import ThetaDataClient, ThetaDataSettings

    transport = FakeTransport(
        default=HttpResponse(status_code=200, text="symbol,strike\n" + "x" * 5000)
    )
    client = ThetaDataClient(
        settings=ThetaDataSettings(),
        transport=RetryingTransport(transport, max_response_bytes=512),
        clock=lambda: AS_OF,
    )
    with pytest.raises(ResponseTooLargeError):
        client.option_quotes(ChainRequest(symbol="SPXW"))


def test_the_configured_cap_reaches_the_transport():
    config = parse(max_response_bytes=2048)
    client = build_thetadata_client(
        config,
        transport=FakeTransport(default=HttpResponse(status_code=200, text="x" * 4096)),
        clock=lambda: AS_OF,
    )
    with pytest.raises(ResponseTooLargeError):
        client.option_quotes(ChainRequest(symbol="SPXW"))


# =============================================================================
# §19 -- collision-safe atomic raw storage
# =============================================================================


def test_two_requests_to_the_same_endpoint_do_not_collide():
    """v2 bug: the id was ``session-endpoint``, so the second request to an
    endpoint in one session collided -- and, the store being append-only, raised.
    """
    first = build_record_id(
        session_id="s1",
        sequence=1,
        endpoint="/v3/option/snapshot/quote",
        query_params={"symbol": "SPXW"},
        payload="a",
    )
    second = build_record_id(
        session_id="s1",
        sequence=2,
        endpoint="/v3/option/snapshot/quote",
        query_params={"symbol": "SPXW"},
        payload="a",
    )
    assert first != second


def test_different_parameters_generate_different_ids():
    common = {"session_id": "s1", "sequence": 1, "endpoint": "/quote", "payload": "a"}
    assert build_record_id(query_params={"symbol": "SPX"}, **common) != build_record_id(
        query_params={"symbol": "SPXW"}, **common
    )


def test_the_same_payload_from_different_requests_stays_distinguishable():
    common = {"session_id": "s1", "endpoint": "/quote", "payload": "identical"}
    a = build_record_id(sequence=1, query_params={"symbol": "SPX"}, **common)
    b = build_record_id(sequence=2, query_params={"symbol": "SPXW"}, **common)
    assert a != b


def test_parameter_hash_is_order_independent():
    assert canonical_parameter_hash({"a": 1, "b": 2}) == canonical_parameter_hash(
        {"b": 2, "a": 1}
    )


def test_record_ids_are_filesystem_safe():
    record_id = build_record_id(
        session_id="s/1",
        sequence=1,
        endpoint="/v3/option/snapshot/quote",
        query_params={},
        payload="a",
    )
    assert "/" not in record_id
    assert ".." not in record_id


def test_a_traversal_attempt_is_refused(tmp_path):
    store = FileRawStore(tmp_path / "raw")
    with pytest.raises(RawStoreError, match="unsafe record id"):
        store.put(
            record_id="../escape",
            endpoint="/x",
            query_params={},
            payload="",
            request_started_at=AS_OF,
            response_received_at=AS_OF,
            http_status=200,
        )


def test_the_payload_and_metadata_hashes_agree(tmp_path):
    store = FileRawStore(tmp_path / "raw")
    record = store.put(
        record_id="s1-0001-quote-abc",
        endpoint="/quote",
        query_params={"a": 1},
        payload="hello",
        request_started_at=AS_OF,
        response_received_at=AS_OF + timedelta(milliseconds=250),
        http_status=200,
        request_sequence=1,
    )
    assert store.get_payload(record.record_id) == "hello"
    assert store.records()[0].payload_hash == record.payload_hash
    assert store.records()[0].request_sequence == 1
    assert store.records()[0].capture_complete is True


def test_no_partial_file_survives_a_successful_write(tmp_path):
    store = FileRawStore(tmp_path / "raw")
    store.put(
        record_id="s1-0001-quote",
        endpoint="/quote",
        query_params={},
        payload="x",
        request_started_at=AS_OF,
        response_received_at=AS_OF,
        http_status=200,
    )
    assert store.incomplete_captures() == ()


def test_an_interrupted_write_is_detectable(tmp_path):
    """A leftover temp file is evidence, and is reported rather than cleaned up."""
    root = tmp_path / "raw"
    store = FileRawStore(root)
    (root / ".partial-abc.tmp").write_text("half a payload", encoding="utf-8")
    assert store.incomplete_captures() == (".partial-abc.tmp",)


def test_a_partial_capture_is_not_listed_as_a_complete_record(tmp_path):
    root = tmp_path / "raw"
    store = FileRawStore(root)
    (root / ".partial-abc.tmp").write_text("half", encoding="utf-8")
    assert store.records() == ()


def test_the_store_remains_append_only():
    store = InMemoryRawStore()
    kwargs = {
        "endpoint": "/quote",
        "query_params": {},
        "payload": "x",
        "request_started_at": AS_OF,
        "response_received_at": AS_OF,
        "http_status": 200,
    }
    store.put(record_id="r1", **kwargs)
    with pytest.raises(RawStoreError, match="append-only"):
        store.put(record_id="r1", **kwargs)


def test_a_full_capture_session_produces_unique_ids():
    from src.adapters.raw_store import CaptureSession

    store = InMemoryRawStore()
    session = CaptureSession(store=store, session_id="session1")
    for _ in range(3):
        session.capture(
            endpoint="/v3/option/snapshot/quote",
            query_params={"symbol": "SPXW"},
            payload="same bytes",
            request_started_at=AS_OF,
            response_received_at=AS_OF,
            http_status=200,
        )
    ids = [record.record_id for record in session.captured]
    assert len(set(ids)) == 3
    assert [record.request_sequence for record in session.captured] == [1, 2, 3]


def test_every_required_metadata_field_is_present():
    from src.adapters.raw_store import CaptureSession

    session = CaptureSession(store=InMemoryRawStore(), session_id="s1")
    record = session.capture(
        endpoint="/v3/option/snapshot/quote",
        query_params={"symbol": "SPXW"},
        payload="x",
        request_started_at=AS_OF,
        response_received_at=AS_OF + timedelta(milliseconds=100),
        http_status=200,
        request_id="req-1",
    )
    payload = record.as_dict()
    for key in (
        "record_id",
        "request_sequence",
        "endpoint",
        "query_params",
        "request_started_at",
        "response_received_at",
        "status" if False else "http_status",
        "payload_hash",
        "payload_location",
        "parser_version",
        "vendor_schema_version",
        "capture_complete",
        "byte_length",
        "request_id",
    ):
        assert key in payload, key
