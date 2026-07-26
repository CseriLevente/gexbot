"""HTTP transport: retries, backoff, size caps, redaction.

Every test here runs against the deterministic fake. Nothing in the unit suite
is allowed to touch the network, and ``FakeTransport`` raises rather than
silently succeeding when a route is unregistered, so an accidental real call
surfaces as a loud failure.
"""

from __future__ import annotations

import logging

import pytest

from src.adapters.transport import (
    DEFAULT_MAX_RESPONSE_BYTES,
    RETRYABLE_STATUS_CODES,
    FakeTransport,
    HttpResponse,
    ResponseTooLargeError,
    RetryBudgetExhaustedError,
    RetryingTransport,
    RetryPolicy,
    TransportError,
    VendorHTTPError,
    _redact,
)

URL = "http://127.0.0.1:25503/v3/option/snapshot/quote"


def ok(text: str = "a,b\n1,2\n") -> HttpResponse:
    return HttpResponse(status_code=200, text=text)


def make(inner: FakeTransport, **kwargs) -> RetryingTransport:
    """Retrying transport with sleeping stubbed out, so tests stay fast."""
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("random_unit", lambda: 0.5)
    return RetryingTransport(inner, **kwargs)


# --- The fake ---------------------------------------------------------------


def test_fake_transport_never_reaches_the_network_and_says_so():
    with pytest.raises(AssertionError, match="never reach the network"):
        FakeTransport().get(URL, {}, 5.0)


def test_fake_transport_routes_by_path_fragment():
    fake = FakeTransport()
    fake.register_text("/snapshot/quote", "quote-body")
    fake.register_text("/snapshot/open_interest", "oi-body")
    assert fake.get(URL, {}, 5.0).text == "quote-body"
    assert (
        fake.get("http://x/v3/option/snapshot/open_interest", {}, 5.0).text == "oi-body"
    )


def test_fake_transport_records_calls_for_assertions():
    fake = FakeTransport(default=ok())
    fake.get(URL, {"symbol": "SPXW"}, 7.5)
    assert fake.call_count == 1
    assert fake.calls[0].params == {"symbol": "SPXW"}
    assert fake.calls[0].timeout_seconds == 7.5


def test_fake_transport_can_script_a_sequence():
    fake = FakeTransport()
    fake.register_sequence(
        "/quote", [HttpResponse(status_code=503, text="down"), ok("recovered")]
    )
    fake.register("/quote", ok("fallback"))
    assert fake.get(URL, {}, 5.0).status_code == 503
    assert fake.get(URL, {}, 5.0).text == "recovered"
    assert fake.get(URL, {}, 5.0).text == "fallback"


# --- Success path -----------------------------------------------------------


def test_successful_request_returns_immediately():
    fake = FakeTransport(default=ok())
    response = make(fake).get(URL, {}, 5.0)
    assert response.ok
    assert response.attempts == 1
    assert response.request_id
    assert fake.call_count == 1


def test_request_id_is_generated_per_request():
    fake = FakeTransport(default=ok())
    transport = make(fake)
    first = transport.get(URL, {}, 5.0).request_id
    second = transport.get(URL, {}, 5.0).request_id
    assert first != second


# --- Retry behaviour --------------------------------------------------------


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUS_CODES))
def test_retryable_statuses_are_retried_then_succeed(status):
    fake = FakeTransport()
    fake.register_sequence(
        "/quote", [HttpResponse(status_code=status, text="transient"), ok()]
    )
    response = make(fake).get(URL, {}, 5.0)
    assert response.ok
    assert response.attempts == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_non_retryable_statuses_fail_immediately(status):
    """A malformed request stays malformed; retrying only burns the rate limit."""
    fake = FakeTransport(default=HttpResponse(status_code=status, text="nope"))
    with pytest.raises(VendorHTTPError) as excinfo:
        make(fake).get(URL, {}, 5.0)
    assert excinfo.value.status_code == status
    assert fake.call_count == 1


def test_transport_errors_are_retried():
    fake = FakeTransport()
    fake.register_sequence("/quote", [TransportError("connection refused"), ok()])
    assert make(fake).get(URL, {}, 5.0).attempts == 2


def test_retries_are_bounded():
    """An unbounded retry loop against a rate-limited vendor is how one slow
    morning becomes a ban.
    """
    fake = FakeTransport(default=HttpResponse(status_code=503, text="down"))
    transport = make(fake, policy=RetryPolicy(max_retries=2))
    with pytest.raises(RetryBudgetExhaustedError) as excinfo:
        transport.get(URL, {}, 5.0)
    assert fake.call_count == 3  # initial + 2 retries
    assert excinfo.value.attempts == 3


def test_zero_retries_means_one_attempt():
    fake = FakeTransport(default=HttpResponse(status_code=503, text="down"))
    with pytest.raises(RetryBudgetExhaustedError):
        make(fake, policy=RetryPolicy(max_retries=0)).get(URL, {}, 5.0)
    assert fake.call_count == 1


def test_persistent_transport_error_surfaces_the_last_cause():
    fake = FakeTransport(default=TransportError("dns failure"))
    with pytest.raises(RetryBudgetExhaustedError) as excinfo:
        make(fake, policy=RetryPolicy(max_retries=1)).get(URL, {}, 5.0)
    assert isinstance(excinfo.value.last_error, TransportError)


# --- Backoff ----------------------------------------------------------------


def test_backoff_grows_exponentially_and_is_capped():
    policy = RetryPolicy(
        backoff_base_seconds=1.0, backoff_max_seconds=4.0, jitter=False
    )
    delays = [policy.delay_for(attempt, random_unit=1.0) for attempt in range(1, 6)]
    assert delays == [1.0, 2.0, 4.0, 4.0, 4.0]


def test_jitter_scales_the_delay_and_is_injectable():
    """Deterministic backoff synchronises a fleet onto the same retry instants;
    the jitter source is injected so tests stay deterministic anyway.
    """
    policy = RetryPolicy(backoff_base_seconds=2.0, jitter=True)
    assert policy.delay_for(1, random_unit=0.0) == 0.0
    assert policy.delay_for(1, random_unit=1.0) == pytest.approx(2.0)
    assert policy.delay_for(1, random_unit=0.5) == pytest.approx(1.0)


def test_sleeps_are_recorded_and_grow_between_attempts():
    fake = FakeTransport(default=HttpResponse(status_code=503, text="down"))
    transport = make(
        fake,
        policy=RetryPolicy(max_retries=3, backoff_base_seconds=1.0, jitter=False),
    )
    with pytest.raises(RetryBudgetExhaustedError):
        transport.get(URL, {}, 5.0)
    assert transport.sleeps == [1.0, 2.0, 4.0]


def test_rate_limit_response_never_sleeps_below_the_base_backoff():
    """A vendor's hint is a floor, not a licence to hammer."""
    fake = FakeTransport(default=HttpResponse(status_code=429, text="slow down"))
    transport = make(
        fake,
        policy=RetryPolicy(max_retries=1, backoff_base_seconds=1.5),
        random_unit=lambda: 0.0,  # jitter would otherwise yield a zero delay
    )
    with pytest.raises(RetryBudgetExhaustedError):
        transport.get(URL, {}, 5.0)
    assert transport.sleeps == [1.5]


# --- Size cap ---------------------------------------------------------------


def test_oversized_response_is_refused():
    """An unbounded read is a memory-exhaustion path driven by a remote party."""
    fake = FakeTransport(default=ok("x" * 2048))
    with pytest.raises(ResponseTooLargeError, match="exceeds"):
        make(fake, max_response_bytes=1024).get(URL, {}, 5.0)


def test_default_size_cap_is_generous_enough_for_a_full_chain():
    assert DEFAULT_MAX_RESPONSE_BYTES >= 16 * 1024 * 1024


# --- Redaction --------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://host/v3?password=hunter2&symbol=SPXW",
        "http://host/v3?symbol=SPXW&api_key=abcdef",
        "http://host/v3?auth_token=zzz",
        "http://host/v3?secret=zzz",
    ],
)
def test_credential_shaped_parameters_are_redacted(url):
    """Credentials in a URL are a common accident; a log line is forever."""
    redacted = _redact(url)
    assert "***" in redacted
    for leaked in ("hunter2", "abcdef", "zzz"):
        assert leaked not in redacted


def test_redaction_preserves_ordinary_parameters():
    assert _redact("http://host/v3?symbol=SPXW&expiration=*") == (
        "http://host/v3?symbol=SPXW&expiration=*"
    )


def test_redaction_handles_a_url_without_a_query():
    assert _redact("http://host/v3/quote") == "http://host/v3/quote"


def test_no_credential_reaches_the_logs(caplog):
    fake = FakeTransport(default=HttpResponse(status_code=503, text="down"))
    transport = make(fake, policy=RetryPolicy(max_retries=1))
    with caplog.at_level(logging.WARNING), pytest.raises(RetryBudgetExhaustedError):
        transport.get(f"{URL}?password=hunter2", {}, 5.0)
    assert "hunter2" not in caplog.text
    assert caplog.records


def test_error_message_is_also_redacted():
    fake = FakeTransport(default=HttpResponse(status_code=404, text="missing"))
    with pytest.raises(VendorHTTPError) as excinfo:
        make(fake).get(f"{URL}?token=supersecret", {}, 5.0)
    assert "supersecret" not in str(excinfo.value)
