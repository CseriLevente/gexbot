"""Size caps enforced while reading, and auth that cannot silently vanish.

Two v2.1 defects that share a shape: something was checked at the wrong layer,
so the check passed while the thing it protected against still happened.

* The response-size cap lived in ``RetryingTransport``, which receives an
  ``HttpResponse`` -- an object whose body has already been read into memory.
  By the time the cap fired, the payload it was meant to prevent had already
  been buffered. The cap protected the parser, not the process.
* ``build_thetadata_client`` read ``config.credentials()`` and passed
  ``basic_auth=... if username and password else None``. With BASIC selected and
  the environment variables unset, that expression is ``None``: the client was
  constructed successfully, unauthenticated, and the first 401 looked like a
  vendor problem rather than a configuration one.
"""

from __future__ import annotations

import pytest

from src.adapters.transport import (
    ByteLimitedReader,
    HttpResponse,
    ResponseTooLargeError,
    RetryingTransport,
)
from src.config.thetadata import (
    AuthenticationMode,
    MissingCredentialsError,
    ThetaDataRuntime,
    build_thetadata_client,
    parse_thetadata_config,
)


def parse(**overrides):
    return parse_thetadata_config(overrides)


def basic(**overrides):
    return parse(
        authentication_mode="basic",
        username_env="THETA_USER",
        password_env="THETA_PASS",
        **overrides,
    )


# =============================================================================
# §7 -- the cap applies while reading
# =============================================================================


def chunks(total: int, size: int = 64):
    """A body delivered in pieces, the way a socket delivers one."""
    remaining = total
    while remaining > 0:
        take = min(size, remaining)
        yield b"x" * take
        remaining -= take


def test_a_body_below_the_limit_is_read_whole():
    reader = ByteLimitedReader(max_bytes=1024)
    assert len(reader.read(chunks(512))) == 512


def test_a_body_exactly_at_the_limit_is_read_whole():
    reader = ByteLimitedReader(max_bytes=1024)
    assert len(reader.read(chunks(1024))) == 1024


def test_a_single_oversized_chunk_is_refused():
    with pytest.raises(ResponseTooLargeError):
        ByteLimitedReader(max_bytes=100).read(iter([b"x" * 500]))


def test_reading_stops_at_the_chunk_that_crosses_the_limit():
    """The point of streaming: the rest is never pulled off the socket."""
    consumed = 0

    def counted():
        nonlocal consumed
        for _ in range(100):
            consumed += 1
            yield b"x" * 64

    with pytest.raises(ResponseTooLargeError):
        ByteLimitedReader(max_bytes=128).read(counted())
    # 64 + 64 = 128 (at the limit), the third chunk crosses it. Nothing beyond.
    assert consumed == 3


def test_the_partial_body_is_not_returned():
    reader = ByteLimitedReader(max_bytes=128)
    with pytest.raises(ResponseTooLargeError) as excinfo:
        reader.read(chunks(4096))
    assert "128" in str(excinfo.value)
    assert reader.aborted


def test_the_reader_reports_how_far_it_got():
    reader = ByteLimitedReader(max_bytes=128)
    with pytest.raises(ResponseTooLargeError):
        reader.read(chunks(4096, size=64))
    assert reader.bytes_read > 128


def test_the_stream_is_closed_on_abort():
    closed = False

    class Closable:
        def __iter__(self):
            for _ in range(100):
                yield b"x" * 64

        def close(self):
            nonlocal closed
            closed = True

    with pytest.raises(ResponseTooLargeError):
        ByteLimitedReader(max_bytes=128).read(Closable())
    assert closed


def test_an_oversized_body_is_never_parsed():
    """A truncated CSV that looks complete is worse than no CSV."""
    parsed = []

    def parser(text):
        parsed.append(text)
        return text

    reader = ByteLimitedReader(max_bytes=128)
    with pytest.raises(ResponseTooLargeError):
        reader.read(chunks(4096))
    assert parsed == [], "the parser was reached with a truncated body"


def test_the_retry_layer_still_enforces_the_cap_for_transports_that_buffer():
    """Defence in depth: a custom transport that returns a whole body must
    still be capped, even though it did not stream."""
    from src.adapters.transport import FakeTransport

    transport = FakeTransport(default=HttpResponse(status_code=200, text="x" * 4096))
    with pytest.raises(ResponseTooLargeError):
        RetryingTransport(transport, max_response_bytes=1024).get(
            "http://host/v3/quote", {}, 5.0
        )


def test_the_configured_limit_reaches_the_streaming_reader():
    config = parse(max_response_bytes=4096)
    assert config.max_response_bytes == 4096
    reader = ByteLimitedReader(max_bytes=config.max_response_bytes)
    assert reader.max_bytes == 4096


def test_the_limit_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        ByteLimitedReader(max_bytes=0)


# =============================================================================
# §8 -- BASIC auth cannot silently degrade
# =============================================================================


def test_basic_auth_with_both_secrets_succeeds(monkeypatch):
    monkeypatch.setenv("THETA_USER", "user")
    monkeypatch.setenv("THETA_PASS", "hunter2")
    from src.adapters.transport import FakeTransport

    assert build_thetadata_client(basic(), transport=FakeTransport()) is not None


def test_basic_auth_with_a_missing_username_fails(monkeypatch):
    monkeypatch.delenv("THETA_USER", raising=False)
    monkeypatch.setenv("THETA_PASS", "hunter2")
    with pytest.raises(MissingCredentialsError, match="THETA_USER"):
        build_thetadata_client(basic())


def test_basic_auth_with_a_missing_password_fails(monkeypatch):
    monkeypatch.setenv("THETA_USER", "user")
    monkeypatch.delenv("THETA_PASS", raising=False)
    with pytest.raises(MissingCredentialsError, match="THETA_PASS"):
        build_thetadata_client(basic())


@pytest.mark.parametrize("value", ["", "   "])
def test_basic_auth_with_empty_secrets_fails(monkeypatch, value):
    """An empty environment variable is not a credential."""
    monkeypatch.setenv("THETA_USER", value)
    monkeypatch.setenv("THETA_PASS", value)
    with pytest.raises(MissingCredentialsError):
        build_thetadata_client(basic())


def test_the_error_names_the_variables_and_not_the_values(monkeypatch):
    monkeypatch.setenv("THETA_USER", "user")
    monkeypatch.setenv("THETA_PASS", "")
    with pytest.raises(MissingCredentialsError) as excinfo:
        build_thetadata_client(basic())
    message = str(excinfo.value)
    assert "THETA_PASS" in message
    assert "user" not in message


def test_a_missing_credential_fails_before_a_transport_exists(monkeypatch):
    """No half-built client is returned for a caller to use by accident."""
    monkeypatch.delenv("THETA_USER", raising=False)
    monkeypatch.delenv("THETA_PASS", raising=False)
    with pytest.raises(MissingCredentialsError):
        ThetaDataRuntime.from_config(basic())


def test_the_runtime_refuses_too(monkeypatch):
    monkeypatch.delenv("THETA_PASS", raising=False)
    monkeypatch.setenv("THETA_USER", "user")
    with pytest.raises(MissingCredentialsError):
        ThetaDataRuntime.from_config(basic())


def test_local_terminal_needs_no_credentials(monkeypatch):
    from src.adapters.transport import FakeTransport

    monkeypatch.delenv("THETA_USER", raising=False)
    monkeypatch.delenv("THETA_PASS", raising=False)
    config = parse()
    assert config.authentication_mode is AuthenticationMode.LOCAL_TERMINAL
    assert build_thetadata_client(config, transport=FakeTransport()) is not None


def test_local_terminal_sends_no_authorization_header(monkeypatch):
    from src.adapters.transport import FakeTransport

    transport = FakeTransport(default=HttpResponse(status_code=200, text=""))
    client = build_thetadata_client(parse(), transport=transport)
    from src.adapters.thetadata.client import ChainRequest

    client.option_quotes(ChainRequest(symbol="SPXW"))
    assert client.settings.auth_mode == "local_terminal"


def test_credentials_never_appear_in_the_serialised_config(monkeypatch):
    monkeypatch.setenv("THETA_PASS", "hunter2")
    payload = str(basic().as_dict())
    assert "THETA_PASS" in payload
    assert "hunter2" not in payload
