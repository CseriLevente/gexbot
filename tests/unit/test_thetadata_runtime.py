"""Every configured value must reach the behaviour it names.

v2.1 fixed the *first* half of this: the ``thetadata:`` YAML section became a
typed object instead of being validated and discarded. But five of its fields --
``iv_source``, ``duplicate_policy``, ``max_dte``, ``strike_range``, ``min_time``
-- stopped at that object. ``build_thetadata_client`` never read them, and no
caller could reasonably know it had to re-supply them by hand.

A setting that is parsed, validated, type-checked, hashed into the config
fingerprint and then ignored is worse than one that is missing: it survives
review. Somebody reads the YAML, sees ``max_dte: 45``, and believes it.

These tests assert against the *outgoing request* and the *assembled chain*, not
against the configuration object. Asserting that a config field holds the value
it was given proves only that dataclasses work.
"""

from __future__ import annotations

import math
import pathlib
from datetime import date, timedelta

import pytest

from src.adapters.thetadata.client import ChainRequest
from src.adapters.transport import FakeTransport, HttpResponse, ResponseTooLargeError
from src.config.thetadata import (
    ThetaDataConfig,
    ThetaDataConfigError,
    ThetaDataRuntime,
    parse_thetadata_config,
)
from src.domain.iv import IVSource
from src.gex.sessions import eastern
from tests.unit.test_chain_completeness import greek_row, oi_row, quote_row

AS_OF = eastern(2026, 3, 17, 11, 0)


def parse(**overrides) -> ThetaDataConfig:
    return parse_thetadata_config(overrides)


#: Supplied so that a configured NBBO_MID_IV can actually be satisfied. Without
#: a usable mid leg the requested source legitimately falls back to the vendor
#: default, and the test would prove nothing about whether config propagated.
IV_LEGS = {"bid_iv": "0.190000", "mid_iv": "0.200000", "ask_iv": "0.210000"}


def csv_response(strikes=(4900, 4910)) -> HttpResponse:
    columns = sorted(
        set(quote_row(0)) | set(oi_row(0)) | set(greek_row(0)) | set(IV_LEGS)
    )
    lines = [",".join(columns)]
    for strike in strikes:
        row = {**quote_row(strike), **oi_row(strike), **greek_row(strike), **IV_LEGS}
        lines.append(",".join(row[column] for column in columns))
    return HttpResponse(status_code=200, text="\n".join(lines) + "\n")


def runtime_for(transport=None, **overrides) -> ThetaDataRuntime:
    return ThetaDataRuntime.from_config(
        parse(**overrides),
        transport=transport
        if transport is not None
        else FakeTransport(default=csv_response()),
        clock=lambda: AS_OF,
    )


def fetch(runtime: ThetaDataRuntime):
    return runtime.fetch_chain(
        as_of=AS_OF,
        spot=5000.25,
        spot_timestamp=AS_OF - timedelta(milliseconds=500),
        open_interest_as_of=date(2026, 3, 16),
    )


# =============================================================================
# §2 -- one canonical runtime
# =============================================================================


def test_the_runtime_is_built_from_config_alone():
    runtime = runtime_for()
    assert isinstance(runtime.default_chain_request, ChainRequest)
    assert runtime.client is not None


def test_configured_max_dte_reaches_the_chain_request():
    runtime = runtime_for(max_dte=45)
    assert runtime.default_chain_request.max_dte == 45


def test_configured_max_dte_reaches_the_outgoing_query():
    transport = FakeTransport(default=csv_response())
    fetch(runtime_for(transport, max_dte=45))
    assert any("max_dte=45" in url for url in transport.urls())


def test_configured_strike_range_reaches_the_outgoing_query():
    transport = FakeTransport(default=csv_response())
    fetch(runtime_for(transport, strike_range=20))
    assert any("strike_range=20" in url for url in transport.urls())


def test_configured_min_time_reaches_the_outgoing_query():
    transport = FakeTransport(default=csv_response())
    fetch(runtime_for(transport, min_time="09:30:00"))
    assert any(
        "min_time=09%3A30%3A00" in url or "min_time=09:30:00" in url
        for url in transport.urls()
    )


def test_configured_iv_source_reaches_the_assembled_chain():
    runtime = runtime_for(iv_source="NBBO_MID_IV")
    assert runtime.iv_source is IVSource.NBBO_MID_IV
    chain = fetch(runtime)
    assert chain.quotes[0].iv.source is IVSource.NBBO_MID_IV


def test_a_different_iv_source_produces_a_different_assembled_chain():
    vendor = fetch(runtime_for(iv_source="VENDOR_DEFAULT_IV"))
    nbbo = fetch(runtime_for(iv_source="NBBO_MID_IV"))
    assert vendor.quotes[0].iv.source is not nbbo.quotes[0].iv.source


def test_configured_duplicate_policy_reaches_chain_assembly():
    """Duplicated rows must be handled the way the config asked."""
    duplicated = csv_response()
    body = duplicated.text.splitlines()
    doubled = HttpResponse(
        status_code=200, text="\n".join([body[0], body[1], body[1], body[2]]) + "\n"
    )
    collapsing = ThetaDataRuntime.from_config(
        parse(duplicate_policy="collapse_exact"),
        transport=FakeTransport(default=doubled),
        clock=lambda: AS_OF,
    )
    # Byte-identical rows carry no conflicting information, so they collapse
    # rather than fail -- there is nothing to arbitrate between.
    assert len(fetch(collapsing).quotes) == 2
    assert collapsing.duplicate_policy == "collapse_exact"

    # A *conflicting* duplicate is a different matter, and the policy governs it.
    conflicting = body[1].replace("12.30", "99.99")
    disagreeing = HttpResponse(
        status_code=200,
        text="\n".join([body[0], body[1], conflicting, body[2]]) + "\n",
    )
    rejecting = ThetaDataRuntime.from_config(
        parse(duplicate_policy="reject"),
        transport=FakeTransport(default=disagreeing),
        clock=lambda: AS_OF,
    )
    with pytest.raises(Exception, match=r"(?i)conflicting"):
        fetch(rejecting)


def test_configured_timeout_reaches_the_transport():
    transport = FakeTransport(default=csv_response())
    fetch(runtime_for(transport, timeout_seconds=17.5))
    assert transport.timeouts()
    assert all(t == pytest.approx(17.5) for t in transport.timeouts())


def test_configured_response_cap_reaches_the_transport():
    transport = FakeTransport(default=HttpResponse(status_code=200, text="x" * 4096))
    with pytest.raises(ResponseTooLargeError):
        fetch(runtime_for(transport, max_response_bytes=1024))


def test_configured_greeks_parameters_reach_the_outgoing_query():
    transport = FakeTransport(default=csv_response())
    fetch(
        runtime_for(transport, rate_value=4.2, annual_dividend=1.3, greeks_version="1")
    )
    joined = " ".join(transport.urls())
    assert "rate_value=4.2" in joined
    assert "annual_dividend=1.3" in joined
    assert "version=1" in joined


def test_configured_raw_capture_reaches_the_store(tmp_path):
    runtime = ThetaDataRuntime.from_config(
        parse(raw_capture_enabled=True, raw_capture_path=str(tmp_path / "raw")),
        transport=FakeTransport(default=csv_response()),
        clock=lambda: AS_OF,
    )
    fetch(runtime)
    assert list((tmp_path / "raw").iterdir())


def test_raw_capture_disabled_writes_nothing(tmp_path):
    runtime = ThetaDataRuntime.from_config(
        parse(raw_capture_enabled=False),
        transport=FakeTransport(default=csv_response()),
        clock=lambda: AS_OF,
    )
    fetch(runtime)
    assert not list(tmp_path.iterdir())


def test_changing_config_changes_effective_behaviour():
    """The property the whole section is about."""
    a = FakeTransport(default=csv_response())
    b = FakeTransport(default=csv_response())
    fetch(runtime_for(a, max_dte=10))
    fetch(runtime_for(b, max_dte=90))
    assert a.urls() != b.urls()


def test_the_caller_never_repeats_a_configured_value():
    """fetch_chain takes only the things config cannot know."""
    import inspect

    parameters = set(inspect.signature(ThetaDataRuntime.fetch_chain).parameters)
    for configured in (
        "max_dte",
        "strike_range",
        "min_time",
        "iv_source",
        "duplicate_policy",
    ):
        assert configured not in parameters, configured


def test_an_explicit_request_still_overrides_the_default():
    transport = FakeTransport(default=csv_response())
    runtime = runtime_for(transport, max_dte=45)
    runtime.fetch_chain(
        request=ChainRequest(symbol="SPX", max_dte=7),
        as_of=AS_OF,
        spot=5000.25,
        spot_timestamp=AS_OF - timedelta(milliseconds=500),
        open_interest_as_of=date(2026, 3, 16),
    )
    assert any("max_dte=7" in url for url in transport.urls())


def test_unsupported_parameters_fail_before_any_request():
    """A filter the endpoint will not honour must not be sent and forgotten."""
    transport = FakeTransport(default=csv_response())
    with pytest.raises(ThetaDataConfigError, match=r"no stock_price_source"):
        ThetaDataRuntime.from_config(
            parse_thetadata_config({"stock_price_source": "per_contract"}),
            transport=transport,
        )
    assert transport.call_count == 0


# =============================================================================
# §3 -- strict validation
# =============================================================================


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    "field", ["timeout_seconds", "connect_timeout_seconds", "backoff_base_seconds"]
)
def test_non_finite_floats_are_refused(field, value):
    with pytest.raises(ThetaDataConfigError, match=r"(?i)finite"):
        parse(**{field: value})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["rate_value", "annual_dividend"])
def test_non_finite_optional_floats_are_refused(field, value):
    with pytest.raises(ThetaDataConfigError, match=r"(?i)finite"):
        parse(**{field: value})


def test_nan_can_never_reach_the_runtime():
    """NaN compares false against every bound, so a range check alone lets it
    through. isfinite is the only guard that catches it."""
    with pytest.raises(ThetaDataConfigError):
        parse(timeout_seconds=float("nan"))
    assert not math.isfinite(float("nan"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("annual_dividend", "oops"),
        ("rate_value", "4.2%"),
        ("greeks_version", 123),
        ("min_time", 123),
        ("username_env", 123),
        ("password_env", False),
        ("rate_type", 7),
        ("base_url", 9),
        ("tier", 1),
    ],
)
def test_wrong_types_are_refused(field, value):
    with pytest.raises(ThetaDataConfigError):
        parse(**{field: value})


@pytest.mark.parametrize(
    "field", ["max_retries", "max_dte", "strike_range", "max_response_bytes"]
)
def test_booleans_are_not_integers(field):
    """``isinstance(True, int)`` is True in Python. ``max_retries: true`` must
    not silently become one retry."""
    with pytest.raises(ThetaDataConfigError, match=r"(?i)bool"):
        parse(**{field: True})


@pytest.mark.parametrize(
    "field",
    [
        "base_url",
        "greeks_version",
        "rate_type",
        "min_time",
        "username_env",
        "password_env",
    ],
)
def test_empty_strings_are_refused(field):
    with pytest.raises(ThetaDataConfigError):
        parse(**{field: ""})


def test_an_empty_environment_variable_name_is_refused():
    with pytest.raises(ThetaDataConfigError):
        parse(authentication_mode="basic", username_env="", password_env="THETA_PASS")


@pytest.mark.parametrize(
    "value", ["nine thirty", "25:00:00", "09:30", "0930", "09:30:00.5x"]
)
def test_an_invalid_min_time_grammar_is_refused(value):
    with pytest.raises(ThetaDataConfigError, match=r"(?i)min_time"):
        parse(min_time=value)


@pytest.mark.parametrize("value", ["09:30:00", "00:00:00", "23:59:59", "16:00:00.500"])
def test_a_valid_min_time_is_accepted(value):
    assert parse(min_time=value).min_time == value


def test_negative_retries_are_refused():
    with pytest.raises(ThetaDataConfigError):
        parse(max_retries=-1)


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_response_limit_is_refused(value):
    with pytest.raises(ThetaDataConfigError):
        parse(max_response_bytes=value)


def test_an_unsupported_authentication_mode_is_refused():
    with pytest.raises(ThetaDataConfigError, match=r"(?i)authentication"):
        parse(authentication_mode="kerberos")


def test_an_unknown_field_is_refused():
    with pytest.raises(ThetaDataConfigError, match=r"(?i)unknown"):
        parse(nonexistent_setting=1)


def test_a_valid_config_still_parses():
    """Guard against a validator so strict it rejects the real config file."""
    config = parse(
        timeout_seconds=30.0,
        rate_value=4.2,
        annual_dividend=1.3,
        max_dte=60,
        strike_range=25,
        min_time="09:30:00",
        greeks_version="latest",
    )
    assert config.max_dte == 60
    assert config.min_time == "09:30:00"


def test_the_shipped_research_config_still_loads():
    from src.config.schema import load_config

    root = pathlib.Path(__file__).resolve().parents[2]
    assert load_config(root / "config" / "research.yaml").thetadata is not None
