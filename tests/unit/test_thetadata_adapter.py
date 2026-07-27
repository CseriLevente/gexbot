"""ThetaData v3: parsing, tier gating, the join, and timestamp preservation.

No network and no Theta Terminal -- the transport is the deterministic fake.
What is exercised is the part that will actually be wrong on day one: the join
key, the missing-field behaviour, the schema contract, and above all that every
vendor clock survives the join instead of being back-stamped to the request
instant.
"""

from __future__ import annotations

import pathlib
from datetime import date, datetime, timedelta

import pytest

from src.adapters.raw_store import (
    CaptureSession,
    FileRawStore,
    InMemoryRawStore,
    RawStoreError,
    payload_hash,
)
from src.adapters.thetadata.client import (
    ChainAssemblyInputs,
    ChainRequest,
    GreeksParameters,
    ThetaDataClient,
    ThetaDataError,
    ThetaDataSchemaError,
    ThetaDataSettings,
    assemble_chain,
    check_schema,
    detect_vendor_error,
    parse_csv,
    parse_expiration,
    parse_right,
    parse_root,
)
from src.adapters.thetadata.endpoints import (
    Endpoint,
    Tier,
    build_url,
    endpoints_for_tier,
    requires_shadow_gamma,
    tier_satisfies,
)
from src.adapters.transport import FakeTransport
from src.domain.contracts import OptionRight, OptionRoot, SnapshotClocks
from src.domain.iv import IVQualityFlag, IVSource
from src.domain.model_spec import ModelSpec
from src.gex.config import GexEngineConfig
from src.gex.engine import compute_gex_snapshot
from src.gex.sessions import eastern

FIXTURES = (
    pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "vendor" / "thetadata"
)
AS_OF = eastern(2026, 3, 17, 11, 0)
SPOT = 5000.25


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def rows(name: str) -> list[dict[str, str]]:
    return parse_csv(fixture(name))


def wired_client(**kwargs) -> tuple[ThetaDataClient, FakeTransport]:
    transport = FakeTransport()
    transport.register_text("/snapshot/quote", fixture("quotes.csv"))
    transport.register_text("/snapshot/open_interest", fixture("open_interest.csv"))
    transport.register_text("/greeks/first_order", fixture("greeks_first_order.csv"))
    transport.register_text("/greeks/second_order", fixture("greeks_second_order.csv"))
    transport.register_text("/index/snapshot/price", fixture("index_price.csv"))
    kwargs.setdefault("settings", ThetaDataSettings(tier=Tier.STANDARD))
    kwargs.setdefault("clock", lambda: AS_OF)
    return ThetaDataClient(transport=transport, **kwargs), transport


# --- Tier map ---------------------------------------------------------------


def test_gamma_needs_pro_but_implied_vol_only_needs_standard():
    """The finding that shapes the subscription decision: gamma is a
    second-order greek. Standard gets IV, Pro gets gamma.

    NOTE: this describes tier *access*, not a claim that our own gamma matches
    the vendor's. That comparison has not been run against live data -- see
    docs/OPEN_DECISIONS.md.
    """
    assert not tier_satisfies(Tier.STANDARD, Tier.PRO)
    assert Endpoint.OPTION_GREEKS_FIRST_ORDER in endpoints_for_tier(Tier.STANDARD)
    assert Endpoint.OPTION_GREEKS_SECOND_ORDER not in endpoints_for_tier(Tier.STANDARD)
    assert Endpoint.OPTION_GREEKS_SECOND_ORDER in endpoints_for_tier(Tier.PRO)


def test_quotes_and_open_interest_are_available_from_the_value_tier():
    available = endpoints_for_tier(Tier.VALUE)
    assert Endpoint.OPTION_QUOTE_SNAPSHOT in available
    assert Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT in available
    # ...but no greeks at all, so Value cannot feed the engine.
    assert Endpoint.OPTION_GREEKS_FIRST_ORDER not in available


def test_shadow_gamma_is_required_below_pro():
    assert requires_shadow_gamma(Tier.STANDARD)
    assert not requires_shadow_gamma(Tier.PRO)


def test_url_parameters_are_sorted_for_reproducibility():
    url = build_url(
        Endpoint.OPTION_QUOTE_SNAPSHOT, {"symbol": "SPXW", "expiration": "*"}
    )
    assert url.endswith("/v3/option/snapshot/quote?expiration=%2A&symbol=SPXW")


def test_client_refuses_an_endpoint_above_its_tier():
    client, _ = wired_client(settings=ThetaDataSettings(tier=Tier.STANDARD))
    with pytest.raises(ThetaDataError, match="requires the pro tier"):
        client.option_second_order_greeks(ChainRequest(symbol="SPXW"))


def test_unconfigured_transport_fails_loudly():
    """Better a clear error than a silent fallback to synthetic data."""
    with pytest.raises(NotImplementedError, match="THETADATA_INTEGRATION"):
        ThetaDataClient().option_quotes(ChainRequest(symbol="SPXW"))


# --- Credentials ------------------------------------------------------------


def test_credentials_come_from_the_environment_never_from_code(monkeypatch):
    settings = ThetaDataSettings(auth_mode="basic")
    monkeypatch.delenv("THETADATA_USERNAME", raising=False)
    monkeypatch.delenv("THETADATA_PASSWORD", raising=False)
    assert settings.credentials() == (None, None)
    assert not settings.has_credentials()
    monkeypatch.setenv("THETADATA_USERNAME", "user")
    monkeypatch.setenv("THETADATA_PASSWORD", "pass")
    assert settings.has_credentials()


def test_local_terminal_mode_needs_no_credential():
    """ThetaData currently routes through a local terminal, but the base URL and
    auth mode are replaceable so the access model can change.
    """
    settings = ThetaDataSettings()
    assert settings.auth_mode == "local_terminal"
    assert settings.credentials() == (None, None)
    assert settings.base_url.startswith("http://127.0.0.1")


def test_serialised_settings_contain_env_var_names_not_secrets(monkeypatch):
    monkeypatch.setenv("THETADATA_PASSWORD", "hunter2")
    payload = ThetaDataSettings(auth_mode="basic").as_dict()
    assert payload["password_env"] == "THETADATA_PASSWORD"
    assert "hunter2" not in str(payload)


# --- Explicit calculation parameters ---------------------------------------


def test_greeks_parameters_are_sent_explicitly_not_left_to_vendor_defaults():
    """Relying on a vendor default means the vendor can change our numbers
    without us changing anything, and the change would be invisible.
    """
    client, transport = wired_client(
        greeks=GreeksParameters(
            greeks_version="1",
            rate_type="treasury_m3",
            rate_value=4.2,
            annual_dividend=1.3,
        )
    )
    client.option_first_order_greeks(ChainRequest(symbol="SPXW"))
    url = transport.urls()[-1]
    assert "version=1" in url
    assert "rate_type=treasury_m3" in url
    assert "rate_value=4.2" in url
    assert "annual_dividend=1.3" in url


def test_unset_calculation_parameters_are_omitted_not_sent_empty():
    """An empty string is not the same as omission, and vendors may differ."""
    client, transport = wired_client(greeks=GreeksParameters())
    client.option_first_order_greeks(ChainRequest(symbol="SPXW"))
    url = transport.urls()[-1]
    assert "rate_value" not in url
    assert "annual_dividend" not in url


def test_calculation_parameters_are_only_sent_to_endpoints_that_accept_them():
    client, transport = wired_client()
    client.option_quotes(ChainRequest(symbol="SPXW"))
    assert "rate_type" not in transport.urls()[-1]


def test_server_side_filters_are_forwarded():
    client, transport = wired_client()
    client.option_quotes(
        ChainRequest(
            symbol="SPXW", max_dte=60, strike_range=20, min_time="09:30:00.000"
        )
    )
    url = transport.urls()[-1]
    assert "max_dte=60" in url
    assert "strike_range=20" in url
    assert "min_time=09%3A30%3A00.000" in url


def test_effective_parameters_are_recorded_in_snapshot_metadata():
    client, _ = wired_client(greeks=GreeksParameters(rate_value=4.2))
    chain = client.fetch_chain(ChainRequest(symbol="SPXW"), as_of=AS_OF, spot=SPOT)
    recorded = chain.meta["thetadata_request"]
    assert recorded["greeks"]["rate_value"] == 4.2
    assert recorded["settings"]["tier"] == "standard"
    assert recorded["needs_shadow_gamma"] is True


# --- Parsing ----------------------------------------------------------------


def test_csv_parsing_reads_the_header_rather_than_assuming_column_order():
    assert parse_csv("symbol,strike\nSPXW,5000.0\n") == [
        {"symbol": "SPXW", "strike": "5000.0"}
    ]


def test_empty_response_parses_to_no_rows():
    assert parse_csv(fixture("empty.csv")) == []
    assert parse_csv("   \n  ") == []


def test_unknown_extra_columns_do_not_break_parsing():
    """A vendor adding a field must not shift every value by one column."""
    parsed = parse_csv(fixture("quotes_extra_columns.csv"))
    assert parsed[0]["bid"] == "12.30"
    assert parsed[0]["new_vendor_field"] == "surprise"
    assert parsed[0]["another_new_field"] == "42"


def test_missing_required_column_fails_clearly():
    """Silently producing ``None`` for every contract would look like an empty
    market rather than a broken response.
    """
    parsed = parse_csv(fixture("quotes_missing_column.csv"))
    with pytest.raises(ThetaDataSchemaError, match="missing required column"):
        check_schema(parsed, Endpoint.OPTION_QUOTE_SNAPSHOT)


def test_schema_check_passes_on_a_good_response():
    check_schema(rows("quotes.csv"), Endpoint.OPTION_QUOTE_SNAPSHOT)
    check_schema(rows("open_interest.csv"), Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT)
    check_schema(rows("greeks_first_order.csv"), Endpoint.OPTION_GREEKS_FIRST_ORDER)
    check_schema(rows("greeks_second_order.csv"), Endpoint.OPTION_GREEKS_SECOND_ORDER)


def test_schema_check_is_a_no_op_on_an_empty_response():
    check_schema([], Endpoint.OPTION_QUOTE_SNAPSHOT)


def test_vendor_error_body_is_detected_even_with_a_200_status():
    assert detect_vendor_error(fixture("vendor_error.json")) is not None
    assert detect_vendor_error(fixture("quotes.csv")) is None


def test_client_raises_on_a_vendor_error_body():
    transport = FakeTransport(default=None)
    transport.register_text("/snapshot/quote", fixture("vendor_error.json"))
    client = ThetaDataClient(transport=transport, clock=lambda: AS_OF)
    with pytest.raises(ThetaDataError, match="vendor error body"):
        client.option_quotes(ChainRequest(symbol="SPXW"))


@pytest.mark.parametrize("raw", ["2026-03-20", "20260320"])
def test_both_expiration_formats_are_accepted(raw):
    assert parse_expiration(raw) == date(2026, 3, 20)


@pytest.mark.parametrize("raw", ["March 20 2026", "2026-13-45", ""])
def test_unrecognised_expiration_is_rejected(raw):
    with pytest.raises(ThetaDataError, match="expiration format"):
        parse_expiration(raw)


@pytest.mark.parametrize("raw", ["call", "CALL", "c", "C"])
def test_call_right_variants(raw):
    assert parse_right(raw) is OptionRight.CALL


def test_unknown_right_is_rejected():
    with pytest.raises(ThetaDataError, match="unrecognised right"):
        parse_right("straddle")


def test_spx_and_spxw_roots_are_distinguished():
    assert parse_root("SPX") is OptionRoot.SPX
    assert parse_root("spxw") is OptionRoot.SPXW


def test_unmodelled_root_is_rejected_rather_than_coerced():
    with pytest.raises(ThetaDataError, match="only SPX and SPXW"):
        parse_root("XSP")


# --- The join ---------------------------------------------------------------


def build(second_order: bool = True, **overrides):
    base = {
        "as_of": AS_OF,
        "spot": SPOT,
        "quote_rows": rows("quotes.csv"),
        "open_interest_rows": rows("open_interest.csv"),
        "first_order_rows": rows("greeks_first_order.csv"),
        "second_order_rows": rows("greeks_second_order.csv") if second_order else [],
        "open_interest_as_of": date(2026, 3, 16),
        "risk_free_rate": 0.042,
        "dividend_yield": 0.013,
        "spot_timestamp": AS_OF,
        "clocks": SnapshotClocks(
            request_started_at=AS_OF - timedelta(milliseconds=300),
            response_received_at=AS_OF,
            normalized_at=AS_OF,
        ),
        "iv_source": IVSource.VENDOR_DEFAULT_IV,
    }
    base.update(overrides)
    return assemble_chain(ChainAssemblyInputs(**base))


def test_join_matches_on_root_expiry_strike_and_right():
    chain = build()
    assert len(chain.quotes) == 20  # 5 strikes x 2 rights x 2 expiries
    by_key = {q.contract.key: q for q in chain.quotes}
    call = by_key[("SPXW", date(2026, 3, 20), 5000.0, "call")]
    put = by_key[("SPXW", date(2026, 3, 20), 5000.0, "put")]
    assert call.open_interest == 4200
    assert put.open_interest == 9100
    assert call.gamma is not None
    assert call.effective_iv is not None


def test_calls_and_puts_at_the_same_strike_are_not_conflated():
    """Without ``right`` in the join key one leg overwrites the other and every
    strike loses half its open interest.
    """
    at_5000 = [
        q
        for q in build().quotes
        if q.contract.strike == 5000.0 and q.contract.expiry == date(2026, 3, 20)
    ]
    assert {q.contract.right for q in at_5000} == {OptionRight.CALL, OptionRight.PUT}
    assert {q.open_interest for q in at_5000} == {4200, 9100}


def test_expiries_are_not_conflated():
    chain = build()
    assert len(chain.expiries) == 2


def test_standard_tier_join_yields_implied_vol_but_no_gamma():
    chain = build(second_order=False)
    assert all(q.gamma is None for q in chain.quotes)
    assert all(q.effective_iv is not None for q in chain.quotes)


def test_standard_tier_chain_still_produces_a_full_gex_snapshot():
    """The $80/mo tier is sufficient: no vendor gamma, and the engine fills every
    view via the shadow pricer.
    """
    snapshot = compute_gex_snapshot(build(second_order=False))
    assert snapshot.contract_count == 20
    assert snapshot.total_unsigned_gex > 0.0
    assert snapshot.meta["vendor_gamma_count"] == 0
    assert snapshot.meta["shadow_gamma_count"] == 20


# The fixture's gamma column was generated at these rates. The model spec has to
# state them, because an explicitly configured zero now genuinely means zero --
# see the regression test immediately below.
FIXTURE_MODEL = ModelSpec(risk_free_rate=0.042, dividend_yield=0.013)


def test_vendor_gamma_and_shadow_gamma_agree_on_fixture_shaped_input():
    """The fixture's gamma column was generated from the documented inputs with
    the project's own pricer, so a drift in the settlement clock, the day count
    or the floor would break this.

    This is a *fixture* consistency check, NOT evidence that our gamma matches
    live ThetaData output. That comparison has not been run.
    """
    vendor = compute_gex_snapshot(
        build(),
        GexEngineConfig(prefer_vendor_gamma=True, model_spec=FIXTURE_MODEL),
    )
    shadow = compute_gex_snapshot(
        build(),
        GexEngineConfig(prefer_vendor_gamma=False, model_spec=FIXTURE_MODEL),
    )
    assert vendor.meta["vendor_gamma_count"] == 20
    assert shadow.meta["shadow_gamma_count"] == 20
    assert vendor.total_unsigned_gex == pytest.approx(
        shadow.total_unsigned_gex, rel=1e-6
    )


def test_a_default_zero_rate_no_longer_silently_borrows_the_snapshot_rate():
    """Regression for the v2 falsy-fallback bug, caught by this very test file.

    Before v2.1 the test above passed with the *default* model spec, whose
    ``risk_free_rate`` is an explicit ``0.0``. It passed because
    ``spec.risk_free_rate or snapshot.risk_free_rate`` discarded the zero and
    silently used the snapshot's 4.2% -- the same rate the fixture was generated
    at. The agreement was an artefact of the bug.

    Now an explicit zero means zero, so the shadow price genuinely differs from a
    vendor gamma computed at 4.2%. If this assertion ever starts finding the two
    equal again, the fallback bug has returned.
    """
    zero_rate = compute_gex_snapshot(
        build(),
        GexEngineConfig(prefer_vendor_gamma=False, model_spec=ModelSpec()),
    )
    vendor = compute_gex_snapshot(
        build(),
        GexEngineConfig(prefer_vendor_gamma=True, model_spec=FIXTURE_MODEL),
    )
    assert zero_rate.total_unsigned_gex != pytest.approx(
        vendor.total_unsigned_gex, rel=1e-6
    )
    assert zero_rate.model_spec.risk_free_rate == 0.0


def test_missing_open_interest_leaves_the_field_none_and_is_counted():
    chain = build(open_interest_rows=[])
    assert all(q.open_interest is None for q in chain.quotes)
    snapshot = compute_gex_snapshot(chain)
    assert snapshot.contract_count == 0
    assert snapshot.validation.rejected == 20


def test_missing_greeks_leaves_iv_none_and_is_counted():
    chain = build(first_order_rows=[], second_order=False)
    assert all(q.effective_iv is None for q in chain.quotes)
    assert compute_gex_snapshot(chain).contract_count == 0


def test_partial_chain_produces_only_the_contracts_that_were_sent():
    chain = build(quote_rows=parse_csv(fixture("quotes_partial_chain.csv")))
    assert len(chain.quotes) == 2
    assert chain.expected_contract_count == 2


def test_extra_open_interest_rows_without_a_quote_are_ignored():
    """Quotes drive the iteration; an OI row for a contract with no book cannot
    contribute a spread or a crossed-market signal.
    """
    extra = [
        *rows("open_interest.csv"),
        {
            "timestamp": "2026-03-17T11:00:00.000",
            "symbol": "SPXW",
            "expiration": "2026-03-20",
            "strike": "9999.00",
            "right": "call",
            "open_interest": "999",
        },
    ]
    chain = build(open_interest_rows=extra)
    assert 9999.0 not in {q.contract.strike for q in chain.quotes}


def test_blank_numeric_fields_become_none_not_zero():
    """Zero open interest and unknown open interest mean different things."""
    blanked = [{**row, "open_interest": ""} for row in rows("open_interest.csv")]
    assert all(
        q.open_interest is None for q in build(open_interest_rows=blanked).quotes
    )


def test_empty_chain_assembles_without_error():
    chain = build(quote_rows=[])
    assert chain.quotes == ()
    assert compute_gex_snapshot(chain).contract_count == 0


# --- Timestamp preservation (the core requirement) --------------------------


def test_every_vendor_clock_survives_the_join():
    """Nothing is back-stamped to ``as_of``.

    The quote and the underlying print carry different timestamps in the fixture
    (11:00:00.000 vs 10:59:59.500). If the join collapsed them, that half-second
    of drift -- the thing ``vendor_lag_alert`` exists to measure -- would vanish.
    """
    quote = build().quotes[0]
    stamps = quote.timestamps
    assert stamps.quote_timestamp is not None
    assert stamps.iv_timestamp is not None
    assert stamps.underlying_timestamp is not None
    assert stamps.underlying_timestamp != stamps.quote_timestamp
    assert stamps.underlying_timestamp < stamps.quote_timestamp


def test_measured_skew_is_the_real_half_second_not_zero():
    stamps = build().quotes[0].timestamps
    assert stamps.skew_seconds("quote_timestamp", "underlying_timestamp") == (
        pytest.approx(0.5)
    )


def test_request_and_response_clocks_are_recorded_per_contract():
    stamps = build().quotes[0].timestamps
    assert stamps.request_started_at is not None
    assert stamps.response_received_at is not None
    assert stamps.normalized_at is not None
    assert stamps.round_trip_seconds() == pytest.approx(0.3)


def test_open_interest_is_a_date_not_a_quote_clock():
    """OI is a settlement artefact, so modelling it as an instant would invite
    comparing it against quote clocks, which is meaningless.
    """
    stamps = build().quotes[0].timestamps
    assert stamps.open_interest_as_of == date(2026, 3, 16)
    assert isinstance(stamps.open_interest_as_of, date)
    assert not isinstance(stamps.open_interest_as_of, datetime)


def test_parsed_timestamps_are_timezone_aware():
    """The engine refuses naive datetimes, so the adapter must attach a zone.

    Attaching Eastern is a documented adapter assumption -- ThetaData emits wall
    clock without an offset. See docs/OPEN_DECISIONS.md.
    """
    stamps = build().quotes[0].timestamps
    assert stamps.quote_timestamp.tzinfo is not None
    assert stamps.quote_timestamp.hour == 11


def test_fetch_chain_records_its_own_request_and_response_clocks():
    clock_values = iter([AS_OF, AS_OF + timedelta(seconds=1)] * 20)
    client, _ = wired_client(clock=lambda: next(clock_values))
    chain = client.fetch_chain(ChainRequest(symbol="SPXW"), as_of=AS_OF, spot=SPOT)
    assert chain.clocks.request_started_at is not None
    assert chain.clocks.response_received_at is not None


# --- IV provenance ----------------------------------------------------------


def test_iv_carries_its_source():
    chain = build(iv_source=IVSource.VENDOR_DEFAULT_IV)
    assert chain.quotes[0].iv.source is IVSource.VENDOR_DEFAULT_IV


def test_zero_bid_contracts_are_flagged_on_their_iv():
    zeroed = [{**row, "bid": "0.00"} for row in rows("quotes.csv")]
    chain = build(quote_rows=zeroed)
    assert all(q.iv.quality is IVQualityFlag.ZERO_BID for q in chain.quotes)


def test_crossed_books_are_flagged_on_their_iv():
    crossed = [{**row, "bid": "99.00", "ask": "1.00"} for row in rows("quotes.csv")]
    chain = build(quote_rows=crossed)
    assert all(q.iv.quality is IVQualityFlag.CROSSED_MARKET for q in chain.quotes)
    assert all(q.effective_iv is None for q in chain.quotes)


def test_vendor_iv_error_is_preserved():
    assert build().quotes[0].iv.vendor_iv_error == pytest.approx(0.0001)


# --- Raw response store -----------------------------------------------------


def test_capture_records_every_request_with_a_payload_hash():
    client, _ = wired_client()
    store = InMemoryRawStore()
    session = CaptureSession(store=store, session_id="s1")
    client.fetch_chain(
        ChainRequest(symbol="SPXW"), as_of=AS_OF, spot=SPOT, capture=session
    )
    # Standard tier: quotes + OI + first-order greeks, no second order.
    assert len(session.captured) == 3
    record = session.captured[0]
    assert record.payload_hash == payload_hash(fixture("quotes.csv"))
    assert record.http_status == 200
    assert record.parser_version
    assert record.endpoint == Endpoint.OPTION_QUOTE_SNAPSHOT.value


def test_pro_tier_capture_includes_second_order_greeks():
    client, _ = wired_client(settings=ThetaDataSettings(tier=Tier.PRO))
    session = CaptureSession(store=InMemoryRawStore(), session_id="s2")
    client.fetch_chain(
        ChainRequest(symbol="SPXW"), as_of=AS_OF, spot=SPOT, capture=session
    )
    assert len(session.captured) == 4


def test_raw_store_is_append_only():
    """Silently replacing a stored response would destroy the only copy of the
    evidence a parser bug could be diagnosed against.
    """
    store = InMemoryRawStore()
    kwargs = {
        "endpoint": "/v3/option/snapshot/quote",
        "query_params": {"symbol": "SPXW"},
        "payload": "a,b\n1,2\n",
        "request_started_at": AS_OF,
        "response_received_at": AS_OF,
        "http_status": 200,
    }
    store.put(record_id="r1", **kwargs)
    with pytest.raises(RawStoreError, match="append-only"):
        store.put(record_id="r1", **kwargs)


def test_stored_payload_round_trips_byte_for_byte():
    store = InMemoryRawStore()
    payload = fixture("quotes.csv")
    store.put(
        record_id="r1",
        endpoint="/v3/option/snapshot/quote",
        query_params={},
        payload=payload,
        request_started_at=AS_OF,
        response_received_at=AS_OF,
        http_status=200,
    )
    assert store.get_payload("r1") == payload


def test_file_raw_store_writes_a_readable_index(tmp_path):
    """The audit trail should be readable without this codebase."""
    store = FileRawStore(tmp_path / "raw")
    record = store.put(
        record_id="session1-quote",
        endpoint="/v3/option/snapshot/quote",
        query_params={"symbol": "SPXW"},
        payload=fixture("quotes.csv"),
        request_started_at=AS_OF,
        response_received_at=AS_OF + timedelta(milliseconds=250),
        http_status=200,
    )
    assert pathlib.Path(record.payload_location).exists()
    assert store.get_payload("session1-quote") == fixture("quotes.csv")
    reloaded = store.records()
    assert len(reloaded) == 1
    assert reloaded[0].payload_hash == record.payload_hash
    assert reloaded[0].round_trip_seconds == pytest.approx(0.25)


def test_file_raw_store_refuses_an_unsafe_record_id(tmp_path):
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


def test_capture_manifest_lists_every_record():
    client, _ = wired_client()
    session = CaptureSession(store=InMemoryRawStore(), session_id="s3")
    client.fetch_chain(
        ChainRequest(symbol="SPXW"), as_of=AS_OF, spot=SPOT, capture=session
    )
    manifest = session.manifest()
    assert manifest["session_id"] == "s3"
    assert len(manifest["records"]) == 3
    assert all(r["payload_hash"] for r in manifest["records"])
