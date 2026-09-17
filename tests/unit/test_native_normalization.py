"""Native ThetaData v3 payloads become hash-bound research events, or nothing.

Every case here runs on a synthetic capture written in the vendor's native
column layout and stamped ``OFFLINE_FIXTURE``. The numbers are invented; the
tests are about lineage, availability and refusal, not about markets.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import pathlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from src.adapters.raw_store import CaptureOrigin
from src.adapters.thetadata import research_events
from src.adapters.thetadata.capture_certification import (
    INDEX_PRICE,
    OPTION_CONTRACT_LIST,
    OPTION_GREEKS,
    OPTION_OPEN_INTEREST,
    OPTION_QUOTE,
    load_capture,
)
from src.adapters.thetadata.research_events import (
    BASIS_DEFERRED,
    BASIS_GREEKS_RECEIPT,
    BASIS_RECEIPT,
    KINDS,
    RULES,
    NormalizationError,
    normalize_capture,
)
from src.replay.event_store import (
    LINEAGE_EVENT_SCHEMA,
    load_events,
    option_parts,
    read_json,
)
from src.replay.pilot_readiness import (
    READINESS_SCHEMA,
    pilot_readiness,
    render_markdown,
)
from src.replay.session import replay_bundle
from src.tools.normalize_thetadata_capture import main
from tests.native_capture import (
    CLEAN_EXPIRIES,
    EXPIRED,
    FAR_EXPIRY,
    OBSERVED,
    RECEIPTS,
    canonical,
    scenario,
    write_native_capture,
)
from tests.synthetic_capture import SyntheticVendor, write_capture

pytestmark = pytest.mark.replay


@pytest.fixture(scope="module")
def capture(tmp_path_factory) -> pathlib.Path:
    return write_native_capture(tmp_path_factory.mktemp("native") / "capture")


@pytest.fixture(scope="module")
def normalized(capture):
    return normalize_capture(capture)


def by_kind(records, kind):
    return [r for r in records if r["kind"] == kind]


def utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


# --- what the normalizer emits ---------------------------------------------


def test_the_native_scenario_normalizes_to_the_expected_accounting(normalized):
    coverage = normalized.coverage
    assert normalized.origin == "SYNTHETIC"
    assert coverage["capture_origins"] == ["OFFLINE_FIXTURE"]
    assert normalized.session_date.isoformat() == "2026-09-08"
    assert coverage["records_by_kind"] == {
        "contract_list": 1,
        "greeks": 8,
        "model_evidence": 1,
        "open_interest": 11,
        "option_quote": 14,
        "spx_price": 1,
    }
    endpoints = coverage["endpoints"]
    assert endpoints[OPTION_QUOTE]["excluded"] == {
        "CONFLICTING_DUPLICATE_OBSERVATIONS": 2,
        "IDENTICAL_DUPLICATE_COALESCED": 1,
        "NOT_IN_INVENTORY": 1,
        "UNEXPECTED_SYMBOL": 1,
        "UNPARSEABLE_TIMESTAMP": 1,
        "VENDOR_TIMESTAMP_AFTER_RECEIPT": 1,
        "ZERO_OR_INVALID_ASK": 1,
    }
    assert endpoints[OPTION_QUOTE]["duplicate_groups"] == {
        "identical_coalesced": 1,
        "conflicting_excluded": 1,
        "conflicting_keys": [canonical(FAR_EXPIRY, 6200, "CALL")],
    }
    assert endpoints[OPTION_GREEKS]["excluded"] == {
        "CONFLICTING_DUPLICATE_OBSERVATIONS": 2,
        "DELTA_OUT_OF_RANGE": 1,
        "IV_OUT_OF_RANGE": 3,
        "NON_FINITE_INPUT": 1,
        "VENDOR_IV_ERROR": 1,
    }
    assert endpoints[OPTION_OPEN_INTEREST]["excluded"] == {
        "IDENTICAL_DUPLICATE_COALESCED": 1,
        "INVALID_OPEN_INTEREST": 1,
        "OI_TIMESTAMP_NOT_TRADING_SESSION": 1,
    }
    assert (
        endpoints[OPTION_OPEN_INTEREST]["duplicate_groups"]["identical_coalesced"] == 1
    )
    assert endpoints[INDEX_PRICE]["excluded"] == {}
    assert endpoints[OPTION_CONTRACT_LIST]["excluded"] == {}
    assert endpoints[OPTION_QUOTE]["vendor_clock_lead"] == {
        "rows_after_receipt": 1,
        "max_ms": 200,
    }
    assert endpoints[OPTION_QUOTE]["unused_columns"] == [
        "bid_size",
        "bid_exchange",
        "bid_condition",
        "ask_size",
        "ask_exchange",
        "ask_condition",
    ]
    assert coverage["identities"] == {
        "listed": 19,
        "quoted": 14,
        "greeked": 8,
        "with_open_interest": 11,
        "without_open_interest": 8,
        "listed_by_expiration": {
            "2026-09-04": 2,
            "2026-09-08": 4,
            "2026-09-09": 4,
            "2026-09-18": 9,
        },
    }
    assert coverage["open_interest"]["rows_by_as_of"] == {
        "2026-09-03": 3,
        "2026-09-04": 8,
    }
    assert coverage["open_interest"]["settlement_rule"]["kind"] == (
        "PRIOR_TRADING_SESSION"
    )


def test_every_record_is_bound_to_a_native_row_that_says_the_same_thing(
    capture, normalized
):
    """Follow each lineage back into the raw bytes and re-read the row."""
    payloads = {
        entry["sha256"]: entry["location"]
        for entry in normalized.provenance["payloads"].values()
    }
    tables = {}
    for digest, location in payloads.items():
        raw = (capture / location).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        tables[digest] = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    for record in normalized.records:
        lineage = record["lineage"]
        assert lineage["rule"] == RULES[record["kind"]]
        rows = tables[lineage["raw_sha256"]]
        if lineage["row_index"] is None:
            assert record["kind"] in {"contract_list", "model_evidence"}
            continue
        row = rows[lineage["row_index"] - 1]
        if record["kind"] == "spx_price":
            assert row["symbol"] == "SPX"
            assert row["price"] == record["data"]["price"]
        else:
            root, expiry, strike, right = option_parts(record["key"])
            assert row["symbol"] == root
            assert row["expiration"] == expiry.isoformat()
            assert float(row["strike"]) == float(strike)
            assert row["right"] == right
        if record["kind"] == "option_quote":
            assert (row["bid"], row["ask"]) == (
                record["data"]["bid"],
                record["data"]["ask"],
            )
        if record["kind"] == "open_interest":
            assert int(row["open_interest"]) == record["data"]["quantity"]
        if record["kind"] == "greeks":
            assert float(row["implied_vol"]) == record["data"]["implied_vol"]
            assert float(row["delta"]) == record["data"]["delta"]


def test_identities_are_canonical_and_inventory_membership_is_enforced(normalized):
    inventory = by_kind(normalized.records, "contract_list")[0]["data"]["contracts"]
    assert len(inventory) == 19
    assert len(set(inventory)) == 19
    for key in inventory:
        option_parts(key)
    assert canonical(CLEAN_EXPIRIES[0], 6000, "CALL") in inventory
    for kind in ("option_quote", "greeks", "open_interest"):
        for record in by_kind(normalized.records, kind):
            assert record["key"] in inventory
    quoted = {r["key"] for r in by_kind(normalized.records, "option_quote")}
    assert canonical(FAR_EXPIRY, 7000, "PUT") not in quoted
    assert canonical(FAR_EXPIRY, 6150, "PUT") not in quoted
    assert canonical(FAR_EXPIRY, 6200, "CALL") not in quoted
    assert canonical(FAR_EXPIRY, 6200, "CALL") in inventory


def test_availability_is_the_later_recorded_receipt_and_never_the_event_time(
    normalized,
):
    endpoints = normalized.coverage["endpoints"]
    for endpoint, received in RECEIPTS.items():
        receipt = endpoints[endpoint]["receipt"]
        assert receipt["evidence"] == [
            "manifest.response_received_at",
            "attempts.received_at",
        ]
        assert receipt["available_at"] == utc(received)
        assert receipt["refusal"] is None
    for record in normalized.records:
        assert record["event_at"] <= record["available_at"]
        kind = record["kind"]
        if kind in {"option_quote", "greeks", "open_interest", "spx_price"}:
            endpoint = next(e for e, k in KINDS.items() if k == kind)
            assert record["available_at"] == utc(RECEIPTS[endpoint])
            assert record["lineage"]["availability_basis"] == BASIS_RECEIPT
    inventory = by_kind(normalized.records, "contract_list")[0]
    assert inventory["event_at"] == inventory["available_at"]
    assert inventory["available_at"] == utc(RECEIPTS[OPTION_CONTRACT_LIST])
    model = by_kind(normalized.records, "model_evidence")[0]
    assert model["event_at"] == utc(OBSERVED)
    assert model["available_at"] == utc(RECEIPTS[OPTION_GREEKS])
    assert model["lineage"]["availability_basis"] == BASIS_GREEKS_RECEIPT
    assert (
        model["lineage"]["raw_sha256"]
        == (normalized.provenance["payloads"][OPTION_GREEKS]["sha256"])
    )


def test_open_interest_follows_the_documented_prior_session_rule(normalized):
    records = {r["key"]: r for r in by_kind(normalized.records, "open_interest")}
    clean = records[canonical(CLEAN_EXPIRIES[0], 6000, "CALL")]
    assert clean["data"] == {"quantity": 125, "as_of": "2026-09-04"}
    assert clean["event_at"] == "2026-09-08T10:30:03+00:00"
    # A row stamped a day earlier settles on the session before that day.
    assert records[canonical(FAR_EXPIRY, 6000, "PUT")]["data"]["as_of"] == "2026-09-03"
    # Saturday stamp: excluded. Non-integer: excluded. No row: no record at all.
    assert canonical(FAR_EXPIRY, 6000, "CALL") not in records
    assert canonical(FAR_EXPIRY, 6050, "CALL") not in records
    assert canonical(FAR_EXPIRY, 6050, "PUT") not in records
    assert all(r["data"]["quantity"] is not None for r in records.values())


def test_greeks_carry_the_verified_request_model_not_a_constant(normalized):
    model = normalized.coverage["model_evidence"]
    assert model["model_id"] == (
        "thetadata-v3/option/snapshot/greeks/first_order|rate_type=sofr|"
        "rate_value=0.042|annual_dividend=0.0|version=latest"
    )
    assert model["request_parameters"] == {
        "rate_type": "sofr",
        "rate_value": "0.042",
        "annual_dividend": "0.0",
        "version": "latest",
    }
    for record in by_kind(normalized.records, "greeks"):
        assert record["data"]["rate"] == 0.042
        assert record["data"]["dividend_yield"] == 0.0
        assert record["data"]["model_id"] == model["model_id"]
    evidence = by_kind(normalized.records, "model_evidence")[0]["data"]
    assert evidence == {
        "model_ids": [model["model_id"]],
        "iv_source": "VENDOR_DEFAULT_IV",
        "iv_price_basis": "UNDOCUMENTED_BY_VENDOR",
    }


def test_zero_bids_and_retained_expired_contracts_are_kept_not_dropped(normalized):
    quotes = {r["key"]: r for r in by_kind(normalized.records, "option_quote")}
    assert quotes[canonical(FAR_EXPIRY, 6000, "CALL")]["data"] == {
        "bid": "0.00",
        "ask": "12.60",
    }
    expired = quotes[canonical(EXPIRED, 6000, "PUT")]
    assert expired["event_at"] == "2026-09-04T19:59:31.827000+00:00"
    assert expired["available_at"] == utc(RECEIPTS[OPTION_QUOTE])


def test_normalization_is_deterministic(capture, normalized):
    again = normalize_capture(capture)
    assert again.encode() == normalized.encode()
    assert again.coverage == normalized.coverage
    raw = normalized.encode()
    assert raw.endswith(b"\n")
    assert b"\r" not in raw
    document = read_json(raw)
    assert document["schema_version"] == LINEAGE_EVENT_SCHEMA
    assert document["origin"] == "SYNTHETIC"
    assert set(document["provenance"]["payloads"]) == set(KINDS)


def test_the_emitted_document_loads_into_the_event_store_with_lineage(
    tmp_path, normalized
):
    raw = normalized.encode()
    (tmp_path / "events.json").write_bytes(raw)
    store, receipts = load_events(
        tmp_path, [{"path": "events.json", "sha256": hashlib.sha256(raw).hexdigest()}]
    )
    assert receipts[0]["schema_version"] == LINEAGE_EVENT_SCHEMA
    assert receipts[0]["provenance"] == normalized.provenance
    quote = store.at(
        "option_quote",
        canonical(CLEAN_EXPIRIES[0], 6000, "CALL"),
        RECEIPTS[OPTION_QUOTE],
    )
    assert quote is not None
    assert quote.lineage["rule"] == RULES["option_quote"]
    assert set(quote.reference()) == {"source_sha256", "record_index"}
    assert normalized.records[quote.record_index] is not None
    assert (
        store.at(
            "option_quote",
            canonical(CLEAN_EXPIRIES[0], 6000, "CALL"),
            RECEIPTS[OPTION_QUOTE] - timedelta(microseconds=1),
        )
        is None
    )


# --- clock tolerance ---------------------------------------------------------


def test_a_vendor_clock_lead_is_excluded_unless_a_bounded_tolerance_defers_it(
    capture,
):
    leading = canonical(FAR_EXPIRY, 6100, "CALL")
    strict = normalize_capture(capture, receipt_clock_tolerance_ms=199)
    assert leading not in {r["key"] for r in by_kind(strict.records, "option_quote")}
    assert (
        strict.coverage["endpoints"][OPTION_QUOTE]["excluded"][
            "VENDOR_TIMESTAMP_AFTER_RECEIPT"
        ]
        == 1
    )
    lenient = normalize_capture(capture, receipt_clock_tolerance_ms=200)
    record = next(
        r for r in by_kind(lenient.records, "option_quote") if r["key"] == leading
    )
    assert record["available_at"] == record["event_at"]
    assert record["available_at"] == utc(
        RECEIPTS[OPTION_QUOTE] + timedelta(milliseconds=200)
    )
    assert record["lineage"]["availability_basis"] == BASIS_DEFERRED
    assert (
        "VENDOR_TIMESTAMP_AFTER_RECEIPT"
        not in (lenient.coverage["endpoints"][OPTION_QUOTE]["excluded"])
    )
    assert (
        lenient.coverage["vendor_timestamp_policy"]["vendor_timestamp_after_receipt"]
        == "AVAILABILITY_DEFERRED_WITHIN_200_MS"
    )


@pytest.mark.parametrize("tolerance", [-1, 5001, 1.0, "0", True])
def test_the_tolerance_is_a_bounded_integer(capture, tolerance):
    with pytest.raises(NormalizationError, match="tolerance"):
        normalize_capture(capture, receipt_clock_tolerance_ms=tolerance)


# --- receipt evidence --------------------------------------------------------


def test_without_an_attempt_log_the_hashed_manifest_receipt_still_counts(tmp_path):
    root = write_native_capture(tmp_path / "capture", attempts=False)
    result = normalize_capture(root)
    assert result.coverage["attempt_log"] == {
        "present": False,
        "usable": False,
        "findings": [],
        "index_sha256": None,
    }
    for entry in result.coverage["endpoints"].values():
        assert entry["receipt"]["evidence"] == ["manifest.response_received_at"]
        assert entry["receipt"]["attempt_received_at"] is None
    assert result.coverage["records_by_kind"]["option_quote"] == 14


def test_a_payload_with_no_recorded_receipt_yields_no_events(tmp_path):
    receipts = {k: v for k, v in RECEIPTS.items() if k != OPTION_QUOTE}
    root = write_native_capture(tmp_path / "capture", receipts=receipts)
    result = normalize_capture(root)
    quote = result.coverage["endpoints"][OPTION_QUOTE]
    assert quote["receipt"]["refusal"] == "AVAILABILITY_UNKNOWN"
    assert quote["receipt"]["available_at"] is None
    assert quote["emitted"] == 0
    assert quote["excluded"] == {"AVAILABILITY_UNKNOWN": 22}
    assert "option_quote" not in result.coverage["records_by_kind"]
    assert result.coverage["records_by_kind"]["greeks"] == 8


def test_an_attempt_receipt_alone_is_evidence_when_the_manifest_has_none(tmp_path):
    built = scenario()
    timing = {
        endpoint: (received - timedelta(milliseconds=600), received)
        for endpoint, received in RECEIPTS.items()
        if endpoint != OPTION_QUOTE
    }
    root = write_capture(
        tmp_path / "capture",
        SyntheticVendor(
            valuation=OBSERVED,
            declared_economic_rate=0.042,
            wire_rate_value=0.042,
            expirations=(*CLEAN_EXPIRIES, FAR_EXPIRY),
        ),
        bodies=built.bodies(),
        timing=timing,
        attempts=dict(RECEIPTS),
        origin=CaptureOrigin.OFFLINE_FIXTURE,
    )
    quote = normalize_capture(root).coverage["endpoints"][OPTION_QUOTE]["receipt"]
    assert quote["evidence"] == ["attempts.received_at"]
    assert quote["manifest_received_at"] is None
    assert quote["available_at"] == utc(RECEIPTS[OPTION_QUOTE])


def test_conflicting_receipt_records_refuse_the_payload(tmp_path):
    attempts = dict(RECEIPTS)
    attempts[OPTION_GREEKS] = RECEIPTS[OPTION_GREEKS] + timedelta(seconds=6)
    root = write_native_capture(tmp_path / "capture", attempts=attempts)
    result = normalize_capture(root)
    greeks = result.coverage["endpoints"][OPTION_GREEKS]
    assert greeks["receipt"]["refusal"] == "RECEIVE_TIME_CONFLICT"
    assert greeks["excluded"] == {"RECEIVE_TIME_CONFLICT": 16}
    assert result.coverage["model_evidence"]["excluded"] == {"RECEIVE_TIME_CONFLICT": 1}
    assert "greeks" not in result.coverage["records_by_kind"]
    assert "model_evidence" not in result.coverage["records_by_kind"]


def test_a_small_disagreement_resolves_to_the_later_receipt(tmp_path):
    attempts = dict(RECEIPTS)
    attempts[OPTION_GREEKS] = RECEIPTS[OPTION_GREEKS] + timedelta(seconds=4)
    root = write_native_capture(tmp_path / "capture", attempts=attempts)
    greeks = normalize_capture(root).coverage["endpoints"][OPTION_GREEKS]["receipt"]
    assert greeks["refusal"] is None
    assert greeks["available_at"] == utc(attempts[OPTION_GREEKS])


def test_a_receipt_before_its_request_is_refused(tmp_path):
    built = scenario()
    timing = {
        endpoint: (received - timedelta(milliseconds=600), received)
        for endpoint, received in RECEIPTS.items()
    }
    timing[INDEX_PRICE] = (
        RECEIPTS[INDEX_PRICE] + timedelta(seconds=1),
        RECEIPTS[INDEX_PRICE],
    )
    root = write_capture(
        tmp_path / "capture",
        SyntheticVendor(
            valuation=OBSERVED,
            declared_economic_rate=0.042,
            wire_rate_value=0.042,
            expirations=(*CLEAN_EXPIRIES, FAR_EXPIRY),
        ),
        bodies=built.bodies(),
        timing=timing,
        attempts=None,
        origin=CaptureOrigin.OFFLINE_FIXTURE,
    )
    price = normalize_capture(root).coverage["endpoints"][INDEX_PRICE]
    assert price["receipt"]["refusal"] == "RECEIVED_BEFORE_REQUEST"
    assert price["emitted"] == 0


def test_a_damaged_attempt_log_is_reported_and_not_used(tmp_path):
    root = write_native_capture(tmp_path / "capture")
    bodies = sorted((root / "attempts").rglob("*.bin"))
    bodies[0].write_bytes(bodies[0].read_bytes() + b"x")
    result = normalize_capture(root)
    log = result.coverage["attempt_log"]
    assert log["present"] is True
    assert log["usable"] is False
    assert any("hashes to" in finding for finding in log["findings"])
    for entry in result.coverage["endpoints"].values():
        assert entry["receipt"]["evidence"] == ["manifest.response_received_at"]


# --- refusals ----------------------------------------------------------------


def test_a_tampered_payload_is_refused_before_any_row_is_read(tmp_path):
    root = write_native_capture(tmp_path / "capture")
    # The option *snapshot* quote payload: ``*quote.raw`` also matches the
    # contract listing, and which one a glob yields first depends on the
    # filesystem (Windows returned the listing, which carries no price).
    raw = next((root / "raw").glob("*snapshot-quote.raw"))
    before = raw.read_bytes()
    raw.write_bytes(before.replace(b"12.30", b"12.31", 1))
    assert raw.read_bytes() != before, "the tamper must change the payload"
    with pytest.raises(ValueError, match="not the bytes that were captured"):
        normalize_capture(root)


def test_a_native_schema_drift_refuses_the_whole_normalization(tmp_path):
    bodies = scenario().bodies()
    lines = bodies[OPTION_GREEKS].splitlines()
    column = lines[0].split(",").index("iv_error")
    bodies[OPTION_GREEKS] = (
        "\n".join(
            ",".join(cell for i, cell in enumerate(line.split(",")) if i != column)
            for line in lines
        )
        + "\n"
    )
    root = write_capture(
        tmp_path / "capture",
        SyntheticVendor(valuation=OBSERVED, expirations=(*CLEAN_EXPIRIES, FAR_EXPIRY)),
        bodies=bodies,
        origin=CaptureOrigin.OFFLINE_FIXTURE,
    )
    with pytest.raises(NormalizationError, match="native schema mismatch"):
        normalize_capture(root)


def test_a_ragged_csv_row_is_refused(tmp_path):
    built = scenario()
    bodies = built.bodies()
    bodies[INDEX_PRICE] += '2026-09-08T09:40:11.000,"SPX"\n'
    root = write_capture(
        tmp_path / "capture",
        SyntheticVendor(valuation=OBSERVED, expirations=(*CLEAN_EXPIRIES, FAR_EXPIRY)),
        bodies=bodies,
        origin=CaptureOrigin.OFFLINE_FIXTURE,
    )
    with pytest.raises(NormalizationError, match="ragged"):
        normalize_capture(root)


def test_a_capture_missing_an_endpoint_is_refused(tmp_path):
    bodies = scenario().bodies()
    bodies.pop(INDEX_PRICE)
    root = write_capture(
        tmp_path / "capture",
        SyntheticVendor(valuation=OBSERVED, expirations=(*CLEAN_EXPIRIES, FAR_EXPIRY)),
        bodies=bodies,
        origin=CaptureOrigin.OFFLINE_FIXTURE,
    )
    with pytest.raises(NormalizationError, match="lacks responses"):
        normalize_capture(root)


def test_a_missing_endpoint_is_reported_only_when_partial_acquisition_is_accepted(
    tmp_path,
):
    """v2.1.36: the session assembler reads a cycle whose request failed; the
    single-capture rule above is unchanged for everyone else."""
    root = write_native_capture(tmp_path / "capture", omit=(INDEX_PRICE,))
    with pytest.raises(NormalizationError, match="lacks responses"):
        normalize_capture(root)
    result = normalize_capture(root, allow_unacquired=True)
    coverage = result.coverage
    assert coverage["scheduled_endpoints"] == sorted(KINDS)
    assert coverage["unacquired_endpoints"] == [INDEX_PRICE]
    assert coverage["acquired_endpoints"] == sorted(set(KINDS) - {INDEX_PRICE})
    receipt = coverage["endpoints"][INDEX_PRICE]["receipt"]
    assert receipt["refusal"] == "NOT_ACQUIRED"
    assert receipt["available_at"] is None
    assert coverage["records_by_kind"].get("spx_price", 0) == 0
    assert coverage["records_by_kind"]["option_quote"] > 0
    assert coverage["model_evidence"]["emitted"] == 1
    assert INDEX_PRICE not in result.provenance["payloads"]
    assert all(r["kind"] != "spx_price" for r in result.records)


def test_a_cycle_without_greeks_carries_no_model_evidence(tmp_path):
    root = write_native_capture(tmp_path / "capture", omit=(OPTION_GREEKS,))
    from src.adapters.thetadata.capture_certification import (
        CaptureCertificationError,
    )

    with pytest.raises(CaptureCertificationError, match="no verified request"):
        normalize_capture(root)
    result = normalize_capture(root, allow_unacquired=True)
    model = result.coverage["model_evidence"]
    assert model["emitted"] == 0
    assert model["excluded"] == {"NOT_ACQUIRED": 1}
    assert model["model_id"] is None
    assert model["rate"] is None
    assert {r["kind"] for r in result.records}.isdisjoint({"greeks", "model_evidence"})
    assert result.coverage["endpoints"][OPTION_GREEKS]["receipt"]["refusal"] == (
        "NOT_ACQUIRED"
    )


def test_a_payload_outside_the_declared_scope_is_refused(tmp_path):
    """A cycle scheduled for the market scope cannot carry an extra payload --
    say a listing or a history download -- into the assembly under that
    cycle's receipts; the whole cycle is refused instead."""
    root = write_native_capture(tmp_path / "capture")
    intent_path = root / "run-intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    market = [INDEX_PRICE, OPTION_QUOTE, OPTION_GREEKS]
    intent["scheduled_endpoints"] = market
    intent_path.write_text(json.dumps(intent), encoding="utf-8")
    with pytest.raises(NormalizationError, match="partial scope"):
        normalize_capture(root)
    with pytest.raises(NormalizationError, match="outside its declared scope"):
        normalize_capture(root, expected_endpoints=frozenset(market))
    with pytest.raises(NormalizationError, match="outside its declared scope"):
        normalize_capture(
            root, expected_endpoints=frozenset(market), allow_unacquired=True
        )
    with pytest.raises(NormalizationError, match="declares scope"):
        normalize_capture(root, expected_endpoints=frozenset(KINDS))
    with pytest.raises(NormalizationError, match="nonempty subset"):
        normalize_capture(
            root, expected_endpoints=frozenset({"/v3/option/history/quote"})
        )
    with pytest.raises(NormalizationError, match="nonempty subset"):
        normalize_capture(root, expected_endpoints=frozenset())


def test_a_whole_plan_capture_cannot_be_read_as_a_partial_scope(capture):
    with pytest.raises(NormalizationError, match="declares no partial scope"):
        normalize_capture(
            capture,
            expected_endpoints=frozenset({INDEX_PRICE, OPTION_QUOTE, OPTION_GREEKS}),
        )
    full = normalize_capture(capture, expected_endpoints=frozenset(KINDS))
    assert full.encode() == normalize_capture(capture).encode()


def test_a_listing_date_that_disagrees_with_the_valuation_is_refused(
    capture, monkeypatch
):
    real = load_capture(capture)
    monkeypatch.setattr(
        research_events,
        "load_capture",
        lambda root, **_: replace(real, captured_at="2026-09-09T13:40:10+00:00"),
    )
    with pytest.raises(NormalizationError, match="disagrees"):
        normalize_capture(capture)


def test_an_undocumented_settlement_convention_is_refused(capture, monkeypatch):
    real = load_capture(capture)
    monkeypatch.setattr(
        research_events,
        "load_capture",
        lambda root, **_: replace(real, documentation=None),
    )
    with pytest.raises(NormalizationError, match="will not assume"):
        normalize_capture(capture)


def test_a_model_fixed_after_the_greeks_receipt_yields_no_model_evidence(
    capture, monkeypatch
):
    real = load_capture(capture)
    late = utc(RECEIPTS[OPTION_GREEKS] + timedelta(seconds=1))
    monkeypatch.setattr(
        research_events,
        "load_capture",
        lambda root, **_: replace(real, captured_at=late),
    )
    result = normalize_capture(capture)
    assert result.coverage["model_evidence"]["excluded"] == {
        "MODEL_FIXED_AFTER_GREEKS_RECEIPT": 1
    }
    assert "model_evidence" not in result.coverage["records_by_kind"]


def test_a_live_capture_origin_is_labelled_recorded(tmp_path):
    root = write_native_capture(
        tmp_path / "capture", origin=CaptureOrigin.LOCAL_TERMINAL_CAPTURE
    )
    result = normalize_capture(root)
    assert result.origin == "RECORDED_NORMALIZED"
    assert result.coverage["capture_origins"] == ["LOCAL_TERMINAL_CAPTURE"]


# --- the offline command -----------------------------------------------------


def test_the_command_writes_a_replayable_bundle_and_a_readiness_report(
    tmp_path, capture, capsys
):
    out = tmp_path / "out"
    assert main([str(capture), "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == [
        "events.json",
        "pilot-readiness.json",
        "pilot-readiness.md",
        "replay-plan.json",
        "replay-report.json",
    ]
    printed = capsys.readouterr().out
    events_sha = hashlib.sha256((out / "events.json").read_bytes()).hexdigest()
    assert f"events_sha256: {events_sha}" in printed
    report = read_json((out / "replay-report.json").read_bytes())
    assert report == replay_bundle(out)
    assert (report["expected_decisions"], report["passing_decisions"]) == (371, 1)
    assert report["synthetic_only"] is True
    assert report["sources"][0]["provenance"]["normalizer"] == (
        "thetadata-research-events/2.1.36"
    )
    passing = [d for d in report["decisions"] if d["decision_passed"]]
    assert passing[0]["decision_at"] == "2026-09-08T13:41:00+00:00"
    assert passing[0]["expected_contracts"] == 8
    assert passing[0]["excluded_inventory_contracts"] == 11
    after = report["decisions"][report["decisions"].index(passing[0]) + 1]
    assert set(after["blocker_counts"]) == {
        "STALE_GREEKS",
        "STALE_OPTION_QUOTE",
        "STALE_SPX_PRICE",
    }
    readiness = read_json((out / "pilot-readiness.json").read_bytes())
    assert readiness["schema_version"] == READINESS_SCHEMA
    assert readiness["usable_for_intraday_pilot"] is False
    assert readiness["blocking_reasons"] == [
        "futures_bid_ask_size",
        "futures_instrument_metadata",
        "futures_costs",
        "multi_session_coverage",
    ]
    assert readiness["partial_requirements"] == [
        "prior_session_open_interest",
        "intraday_coverage",
    ]
    assert readiness["usable_decisions"] == 1
    assert readiness["generated_from"]["events_sha256"] == events_sha
    assert readiness["generated_from"]["replay_report_hash"] == report["report_hash"]
    assert readiness["parameters"]["diagnostic"] is False
    assert readiness["replay"]["best_decision"]["decision_at"] == (
        "2026-09-08T13:41:00+00:00"
    )
    for flag in (
        "authenticity_verified",
        "normalization_verified",
        "ready_for_backtest",
        "trusted_for_gex",
        "gex_computed",
        "strategy_tested",
        "pnl_computed",
    ):
        assert readiness[flag] is False
    markdown = (out / "pilot-readiness.md").read_text(encoding="utf-8")
    assert "NOT usable for an intraday pilot" in markdown
    assert readiness["report_hash"] in markdown
    assert "| futures_bid_ask_size | MISSING |" in markdown
    assert render_markdown(readiness) == markdown


def test_the_command_output_is_reproducible(tmp_path, capture):
    first, second = tmp_path / "one", tmp_path / "two"
    assert main([str(capture), "--out", str(first)]) == 0
    assert main([str(capture), "--out", str(second)]) == 0
    for name in (
        "events.json",
        "replay-plan.json",
        "replay-report.json",
        "pilot-readiness.json",
        "pilot-readiness.md",
    ):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_diagnostic_parameters_are_labelled_and_never_the_research_default(
    tmp_path, capture
):
    out = tmp_path / "diag"
    assert (
        main(
            [
                str(capture),
                "--out",
                str(out),
                "--receipt-clock-tolerance-ms",
                "250",
                "--close-buffer-minutes",
                "0",
                "--label",
                "diagnostic only",
            ]
        )
        == 0
    )
    readiness = read_json((out / "pilot-readiness.json").read_bytes())
    assert readiness["label"] == "diagnostic only"
    assert readiness["parameters"]["diagnostic"] is True
    assert readiness["parameters"]["contract_is_research_default"] is False
    assert readiness["parameters"]["contract"]["close_buffer_minutes"] == 0
    assert readiness["replay"]["expected_decisions"] == 386
    assert "DIAGNOSTIC_PARAMETERS_NOT_RESEARCH_DEFAULT" in readiness["blocking_reasons"]
    markdown = (out / "pilot-readiness.md").read_text(encoding="utf-8")
    assert "Diagnostic run" in markdown
    quote = readiness["coverage"]["endpoints"][OPTION_QUOTE]
    assert quote["emitted"] == 15


@pytest.mark.parametrize(
    "arrange",
    [
        lambda capture, tmp: (str(capture), tmp / "exists"),
        lambda capture, tmp: (str(capture), capture / "inside"),
        lambda capture, tmp: (str(tmp / "missing"), tmp / "out"),
    ],
)
def test_the_command_refuses_used_or_nested_outputs_and_missing_captures(
    tmp_path, capture, arrange, capsys
):
    (tmp_path / "exists").mkdir()
    source, out = arrange(capture, tmp_path)
    assert main([source, "--out", str(out)]) == 2
    assert "refused" in capsys.readouterr().err
    assert not (capture / "inside").exists()


def test_the_command_refuses_a_capture_inside_its_output(tmp_path, capsys):
    out = tmp_path / "out"
    root = write_native_capture(out / "capture")
    assert main([str(root), "--out", str(out)]) == 2
    assert "outside the capture" in capsys.readouterr().err


def test_the_command_refuses_an_invalid_tolerance_and_writes_nothing(
    tmp_path, capture, capsys
):
    out = tmp_path / "out"
    assert (
        main([str(capture), "--out", str(out), "--receipt-clock-tolerance-ms", "9000"])
        == 2
    )
    assert "tolerance" in capsys.readouterr().err
    assert not out.exists()


def test_the_command_refuses_a_tampered_capture(tmp_path, capsys):
    root = write_native_capture(tmp_path / "capture")
    raw = next((root / "raw").glob("*open_interest.raw"))
    raw.write_bytes(raw.read_bytes() + b"\n")
    assert main([str(root), "--out", str(tmp_path / "out")]) == 2
    assert "not the bytes that were captured" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


# --- readiness accounting ----------------------------------------------------


def test_readiness_reports_a_refused_receipt_as_missing(tmp_path):
    receipts = {k: v for k, v in RECEIPTS.items() if k != INDEX_PRICE}
    root = write_native_capture(tmp_path / "capture", receipts=receipts)
    out = tmp_path / "out"
    assert main([str(root), "--out", str(out)]) == 0
    readiness = read_json((out / "pilot-readiness.json").read_bytes())
    status = {r["requirement"]: r for r in readiness["requirements"]}
    assert status["spx_observations"]["status"] == "MISSING"
    assert "AVAILABILITY_UNKNOWN" in status["spx_observations"]["detail"]
    assert status["receive_times"]["status"] == "PARTIAL"
    assert "spx_observations" in readiness["blocking_reasons"]
    assert readiness["usable_decisions"] == 0
    assert readiness["replay"]["blocker_decision_counts"]["MISSING_SPX_PRICE"] == 365


def test_readiness_is_a_pure_function_of_its_inputs(tmp_path, capture):
    out = tmp_path / "out"
    assert main([str(capture), "--out", str(out)]) == 0
    readiness = read_json((out / "pilot-readiness.json").read_bytes())
    report = read_json((out / "replay-report.json").read_bytes())
    again = pilot_readiness(
        readiness["coverage"],
        report,
        events_sha256=readiness["generated_from"]["events_sha256"],
        plan_sha256=readiness["generated_from"]["plan_sha256"],
        receipt_clock_tolerance_ms=0,
    )
    assert again == readiness
    encoded = json.dumps(again, sort_keys=True, allow_nan=False)
    assert "NaN" not in encoded


# --- the 2.1.35 source schema in the event store -----------------------------


def _load_mutated(tmp_path, normalized, mutate):
    document = json.loads(normalized.encode())
    mutate(document)
    raw = (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
    (tmp_path / "events.json").write_bytes(raw)
    return load_events(
        tmp_path, [{"path": "events.json", "sha256": hashlib.sha256(raw).hexdigest()}]
    )


def _first(document, kind):
    return next(r for r in document["records"] if r["kind"] == kind)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.pop("provenance"), "unexpected fields"),
        (lambda d: d.update(extra=1), "unexpected fields"),
        (lambda d: d["records"][0].pop("lineage"), "unexpected fields"),
        (
            lambda d: d["records"][0]["lineage"].update(raw_sha256="0" * 64),
            "outside the provenance",
        ),
        (
            lambda d: d["records"][0]["lineage"].update(raw_sha256="ABC"),
            "SHA-256 hex digest",
        ),
        (
            lambda d: _first(d, "option_quote")["lineage"].update(row_index=0),
            "invalid integer",
        ),
        (
            lambda d: _first(d, "option_quote")["lineage"].update(row_index="1"),
            "invalid integer",
        ),
        (
            lambda d: _first(d, "option_quote")["lineage"].update(row_index=True),
            "invalid integer",
        ),
        (
            lambda d: d["records"][0]["lineage"].update(availability_basis=""),
            "nonempty string",
        ),
        (lambda d: d["records"][0]["lineage"].update(rule=None), "nonempty string"),
        (lambda d: d["records"][0]["lineage"].pop("rule"), "unexpected fields"),
        (
            lambda d: d["provenance"]["payloads"][INDEX_PRICE].update(
                location="../raw/x.raw"
            ),
            "inside bundle",
        ),
        (
            lambda d: d["provenance"]["payloads"][INDEX_PRICE].update(
                sha256=d["provenance"]["payloads"][OPTION_QUOTE]["sha256"]
            ),
            "duplicate raw payload digest",
        ),
        (
            lambda d: d["provenance"].update(manifest_sha256="not-a-digest"),
            "SHA-256 hex digest",
        ),
        (lambda d: d["provenance"].update(session_date="2026-13-01"), "month"),
        (lambda d: d["provenance"].update(payloads={}), "verified raw payloads"),
        (lambda d: d["provenance"].pop("normalizer"), "unexpected fields"),
        (
            lambda d: d.update(schema_version="research-events/2.1.37"),
            "unsupported source schema",
        ),
        # A 2.1.35 document relabelled as the session schema lacks the session
        # provenance (v2.1.36 accepts that schema, with its own shape).
        (
            lambda d: d.update(schema_version="research-events/2.1.36"),
            "unexpected fields",
        ),
        (lambda d: d.update(origin="RECORDED"), "unsupported source schema"),
    ],
)
def test_lineage_and_provenance_are_validated_strictly(
    tmp_path, normalized, mutate, message
):
    with pytest.raises(ValueError, match=message):
        _load_mutated(tmp_path, normalized, mutate)


def test_a_2_1_34_document_may_not_carry_lineage_and_loads_without_it(
    tmp_path, normalized
):
    def downgrade(document):
        document["schema_version"] = "research-events/2.1.34"
        document.pop("provenance")
        for record in document["records"]:
            record.pop("lineage")

    store, receipts = _load_mutated(tmp_path, normalized, downgrade)
    assert set(receipts[0]) == {"source_sha256", "bytes", "origin", "records"}
    event = store.at("spx_price", "SPX", RECEIPTS[INDEX_PRICE])
    assert event is not None
    assert event.lineage is None

    def half_downgrade(document):
        document["schema_version"] = "research-events/2.1.34"
        document.pop("provenance")

    with pytest.raises(ValueError, match="unexpected fields"):
        _load_mutated(tmp_path, normalized, half_downgrade)


def test_a_non_object_source_document_is_refused(tmp_path):
    raw = b"[]\n"
    (tmp_path / "events.json").write_bytes(raw)
    with pytest.raises(ValueError, match="unsupported source schema"):
        load_events(
            tmp_path,
            [{"path": "events.json", "sha256": hashlib.sha256(raw).hexdigest()}],
        )


# --- repeated identities inside one snapshot (v2.1.35 review finding) ---------


def _without(built, expiry, strike, right, *, greeks=False, open_interest=False):
    """Strip one identity's quote rows (and optionally its Greeks / OI rows)."""
    ident = (expiry.isoformat(), f"{strike}.000", right)

    def keep(row):
        return (row["expiration"], row["strike"], row["right"]) != ident

    built.quotes = [r for r in built.quotes if keep(r)]
    if greeks:
        built.greeks = [r for r in built.greeks if keep(r)]
    if open_interest:
        built.open_interest = [r for r in built.open_interest if keep(r)]
    return built


def _decision(out, at="2026-09-08T13:41:00+00:00"):
    report = read_json((out / "replay-report.json").read_bytes())
    return next(d for d in report["decisions"] if d["decision_at"] == at)


CLEAN = (CLEAN_EXPIRIES[0], 6000, "CALL")
CLEAN_KEY = canonical(*CLEAN)


def _clean_quote(**changes):
    from tests.native_capture import identity, quote_row

    return quote_row(identity(*CLEAN), **changes)


@pytest.mark.parametrize(
    ("rows", "label"),
    [
        ([_clean_quote(), _clean_quote(bid="99.00")], "valid then crossed"),
        ([_clean_quote(bid="99.00"), _clean_quote()], "crossed then valid"),
        ([_clean_quote(), _clean_quote(bid="20.30", ask="20.60")], "valid a then b"),
        ([_clean_quote(bid="20.30", ask="20.60"), _clean_quote()], "valid b then a"),
        (
            [_clean_quote(), _clean_quote(at=OBSERVED + timedelta(seconds=1))],
            "same prices, different vendor times",
        ),
        (
            [_clean_quote(), _clean_quote(), _clean_quote(bid="12.31")],
            "two identical then one different",
        ),
    ],
)
def test_conflicting_quote_observations_are_excluded_whatever_their_order(
    tmp_path, rows, label
):
    built = _without(scenario(), *CLEAN)
    built.quotes = rows + built.quotes
    capture = write_native_capture(tmp_path / "capture", built)
    normalized = normalize_capture(capture)
    quotes = {r["key"] for r in by_kind(normalized.records, "option_quote")}
    assert CLEAN_KEY not in quotes, label
    assert (
        CLEAN_KEY
        in by_kind(normalized.records, "contract_list")[0]["data"]["contracts"]
    )
    quote = normalized.coverage["endpoints"][OPTION_QUOTE]
    assert quote["excluded"]["CONFLICTING_DUPLICATE_OBSERVATIONS"] == len(rows) + 2
    assert quote["duplicate_groups"]["conflicting_keys"] == [
        CLEAN_KEY,
        canonical(FAR_EXPIRY, 6200, "CALL"),
    ]
    out = tmp_path / "out"
    assert main([str(capture), "--out", str(out)]) == 0
    decision = _decision(out)
    assert decision["decision_passed"] is False
    assert decision["expected_contracts"] == 8
    assert decision["blocker_counts"] == {"MISSING_OPTION_QUOTE": 1}
    assert decision["passing_contracts"] == 7


def test_conflicting_observations_produce_identical_output_in_either_order(
    tmp_path,
):
    outputs = []
    for order in (
        (_clean_quote(), _clean_quote(bid="99.00")),
        (_clean_quote(bid="99.00"), _clean_quote()),
    ):
        built = _without(scenario(), *CLEAN)
        built.quotes = list(order) + built.quotes
        normalized = normalize_capture(
            write_native_capture(tmp_path / str(len(outputs)) / "capture", built)
        )
        outputs.append(
            (
                [
                    {**r, "lineage": {**r["lineage"], "raw_sha256": None}}
                    for r in normalized.records
                ],
                {
                    k: v
                    for k, v in normalized.coverage["endpoints"][OPTION_QUOTE].items()
                    if k != "receipt"
                },
            )
        )
    assert outputs[0] == outputs[1]


def test_exact_duplicates_coalesce_to_the_lowest_row_and_the_control_passes(
    tmp_path,
):
    built = _without(scenario(), *CLEAN)
    built.quotes = [_clean_quote(), _clean_quote(), _clean_quote(), *built.quotes]
    capture = write_native_capture(tmp_path / "capture", built)
    normalized = normalize_capture(capture)
    record = next(
        r for r in by_kind(normalized.records, "option_quote") if r["key"] == CLEAN_KEY
    )
    assert record["lineage"]["row_index"] == 1
    assert record["data"] == {"bid": "12.30", "ask": "12.60"}
    quote = normalized.coverage["endpoints"][OPTION_QUOTE]
    assert quote["excluded"]["IDENTICAL_DUPLICATE_COALESCED"] == 2
    assert quote["duplicate_groups"]["identical_coalesced"] == 1
    out = tmp_path / "out"
    assert main([str(capture), "--out", str(out)]) == 0
    assert _decision(out)["decision_passed"] is True


def test_duplicates_differing_only_in_unread_columns_are_identical_observations(
    tmp_path,
):
    built = _without(scenario(), *CLEAN)
    built.quotes = [
        _clean_quote(),
        _clean_quote(bid_size="1", ask_size="2"),
        *built.quotes,
    ]
    normalized = normalize_capture(write_native_capture(tmp_path / "capture", built))
    assert CLEAN_KEY in {r["key"] for r in by_kind(normalized.records, "option_quote")}
    quote = normalized.coverage["endpoints"][OPTION_QUOTE]
    assert quote["excluded"]["IDENTICAL_DUPLICATE_COALESCED"] == 1
    assert quote["duplicate_groups"]["conflicting_keys"] == [
        canonical(FAR_EXPIRY, 6200, "CALL")
    ]


def test_an_invalid_row_does_not_make_its_valid_twin_authoritative(tmp_path):
    built = _without(scenario(), *CLEAN)
    built.quotes = [
        _clean_quote(),
        {**_clean_quote(), "timestamp": "garbage"},
        *built.quotes,
    ]
    normalized = normalize_capture(write_native_capture(tmp_path / "capture", built))
    assert CLEAN_KEY not in {
        r["key"] for r in by_kind(normalized.records, "option_quote")
    }
    quote = normalized.coverage["endpoints"][OPTION_QUOTE]
    assert quote["excluded"]["CONFLICTING_DUPLICATE_OBSERVATIONS"] == 4
    # The scenario's own unparseable row is the only one counted as such: the
    # twin's malformed timestamp is a disagreement, not a reason to trust its pair.
    assert quote["excluded"]["UNPARSEABLE_TIMESTAMP"] == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_greeks_and_open_interest_groups_block_the_frame(tmp_path, reverse):
    from tests.native_capture import greeks_row, identity, oi_row

    built = _without(scenario(), *CLEAN, greeks=True, open_interest=True)
    ident = identity(*CLEAN)
    greeks = [greeks_row(ident), greeks_row(ident, delta="0.4900")]
    ois = [oi_row(ident), oi_row(ident, quantity="126")]
    if reverse:
        greeks.reverse()
        ois.reverse()
    built.quotes = [_clean_quote(), *built.quotes]
    built.greeks = greeks + built.greeks
    built.open_interest = ois + built.open_interest
    capture = write_native_capture(tmp_path / "capture", built)
    normalized = normalize_capture(capture)
    assert CLEAN_KEY not in {r["key"] for r in by_kind(normalized.records, "greeks")}
    assert CLEAN_KEY not in {
        r["key"] for r in by_kind(normalized.records, "open_interest")
    }
    assert (
        normalized.coverage["endpoints"][OPTION_GREEKS]["excluded"][
            "CONFLICTING_DUPLICATE_OBSERVATIONS"
        ]
        == 4
    )
    assert (
        normalized.coverage["endpoints"][OPTION_OPEN_INTEREST]["excluded"][
            "CONFLICTING_DUPLICATE_OBSERVATIONS"
        ]
        == 2
    )
    assert normalized.coverage["identities"]["without_open_interest"] == 9
    out = tmp_path / "out"
    assert main([str(capture), "--out", str(out)]) == 0
    decision = _decision(out)
    assert decision["decision_passed"] is False
    assert decision["expected_contracts"] == 8
    assert decision["blocker_counts"] == {
        "MISSING_GREEKS": 1,
        "MISSING_OPEN_INTEREST": 1,
        "OI_NOT_PRIOR_COMPLETED_SESSION": 1,
        "OI_UNAVAILABLE": 1,
    }


@pytest.mark.parametrize(
    ("second", "expected"),
    [
        ({"price": "6001.20"}, "coalesced"),
        ({"price": "6002.20"}, "conflicting"),
        ({"timestamp": "2026-09-08T09:40:09.000"}, "conflicting"),
    ],
)
def test_repeated_index_rows_follow_the_same_policy(tmp_path, second, expected):
    for reverse in (False, True):
        built = scenario()
        rows = [dict(built.index[0]), {**built.index[0], **second}]
        built.index = rows[::-1] if reverse else rows
        normalized = normalize_capture(
            write_native_capture(tmp_path / str(reverse) / "capture", built)
        )
        prices = by_kind(normalized.records, "spx_price")
        index = normalized.coverage["endpoints"][INDEX_PRICE]
        if expected == "coalesced":
            assert [p["data"]["price"] for p in prices] == ["6001.20"]
            assert index["excluded"] == {"IDENTICAL_DUPLICATE_COALESCED": 1}
        else:
            assert prices == []
            assert index["excluded"] == {"CONFLICTING_DUPLICATE_OBSERVATIONS": 2}
            assert index["duplicate_groups"]["conflicting_keys"] == ["SPX"]
