"""Byte binding, as-of inventory, complete decision-grid accounting and fills.

Every expectation here is derived from the documented time conventions and the
hand-computed synthetic arithmetic, not from the first output the code produced.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, timedelta
from decimal import Decimal

import pytest

from src.domain.canonical import canonical_payload
from src.domain.digests import digest_of
from src.replay import event_store
from src.replay.event_store import EVENT_SCHEMA, EventStore, load_events, read_json
from src.replay.fill_probe import current_revisions, effective_profile, validate_probes
from src.replay.research_contract import ResearchContract
from src.replay.session import decision_grid, replay_bundle
from src.tools.replay_research_session import main
from tests.synthetic_replay import (
    FILL_POLICY,
    FUTURE,
    OPTION,
    PRIOR_SESSION,
    SESSION,
    clock,
    encode,
    event,
    example,
    frame_records,
    futures_quote,
    probe,
    write_bundle,
)

SHORT_GRID = 383  # close buffer leaving exactly 09:35, 09:36 and 09:37


def run_changed(tmp_path, change, *, close_buffer_minutes=SHORT_GRID):
    plan, source = example()
    plan["contract"]["close_buffer_minutes"] = close_buffer_minutes
    change(plan, source)
    return replay_bundle(write_bundle(tmp_path / "bundle", plan, source))


def refused(tmp_path, change, match):
    with pytest.raises(ValueError, match=match):
        run_changed(tmp_path, change)


def record(source, kind, key=None):
    return next(
        r for r in source["records"] if r["kind"] == kind and key in (None, r["key"])
    )


def probes_only(plan, *minutes):
    plan["probes"] = [
        p for p in plan["probes"] if p["id"] in {f"demo-{m}" for m in minutes}
    ]


# --- whole-session accounting ------------------------------------------------


def test_synthetic_day_accounts_for_every_minute_and_does_not_claim_authenticity(
    tmp_path,
):
    result = replay_bundle(write_bundle(tmp_path / "bundle"))
    assert result["expected_decisions"] == 371
    assert result["passing_decisions"] == 2
    assert result["blocked_decisions"] == 369
    assert len(result["decisions"]) == 371
    assert result["decisions"][0]["decision_at"] == "2026-09-08T13:35:00+00:00"
    assert result["decisions"][-1]["decision_at"] == "2026-09-08T19:45:00+00:00"
    assert not result["declared_grid_complete"]
    assert result["source_bytes_verified"]
    assert result["synthetic_only"]
    assert result["observed_source_origins"] == ["SYNTHETIC"]
    for flag in (
        "authenticity_verified",
        "normalization_verified",
        "ready_for_backtest",
        "trusted_for_gex",
        "gex_computed",
        "strategy_tested",
        "pnl_computed",
    ):
        assert result[flag] is False
    assert result["orders_placed"] == 0
    assert result["contract"] == ResearchContract().as_dict()
    assert any("research design assumptions" in line for line in result["limitations"])
    # Every blocked minute is blocked by staleness of all three market inputs
    # and nothing else: absent frames are reported, never synthesized.
    blocked = [d for d in result["decisions"] if not d["decision_passed"]]
    assert {tuple(sorted(d["blocker_counts"])) for d in blocked} == {
        ("STALE_GREEKS", "STALE_OPTION_QUOTE", "STALE_SPX_PRICE")
    }
    assert all(d["expected_contracts"] == 1 for d in result["decisions"])
    assert result["probe_counts"] == {
        "total": 3,
        "simulated_fills": 2,
        "unfilled": 1,
        "refusal_reasons": {"RESEARCH_FRAME_BLOCKED": 1},
    }
    assert result["fill_probes"][-1]["reason"] == "RESEARCH_FRAME_BLOCKED"
    assert result["report_hash"] == digest_of(
        canonical_payload({k: v for k, v in result.items() if k != "report_hash"})
    )


def test_buy_takes_the_ask_sell_takes_the_bid_and_costs_are_exact_decimals(tmp_path):
    fills = replay_bundle(write_bundle(tmp_path / "bundle"))["fill_probes"]
    buy, sell = fills[0], fills[1]
    assert (buy["probe"]["side"], sell["probe"]["side"]) == ("BUY", "SELL")
    assert buy["status"] == sell["status"] == "SIMULATED_FILL"
    assert buy["quoted_side_price"] == "100.25"
    assert buy["price"] == "100.50"
    assert sell["quoted_side_price"] == "100.00"
    assert sell["price"] == "99.75"
    for fill in (buy, sell):
        assert fill["spread_points"] == "0.25"
        assert fill["fee"] == "1.40"
        assert fill["additional_slippage_cost"] == "5.00"
        # The spread is already paid by taking the executable side; it is not
        # added a second time into the cost total.
        assert fill["fee_plus_additional_slippage"] == "6.40"
        assert Decimal(fill["fee"]) + Decimal(fill["additional_slippage_cost"]) == (
            Decimal(fill["fee_plus_additional_slippage"])
        )
        assert fill["liquidity_guaranteed"] is False
        assert fill["currency"] == "USD"
        assert fill["fill_at"] == fill["quote_available_at"]
        assert (
            fill["quote_source"]["record_index"] != fill["cost_source"]["record_index"]
        )
    assert buy["fill_at"] == "2026-09-08T13:35:00.400000+00:00"
    assert buy["arrival_at"] == "2026-09-08T13:35:00.250000+00:00"


def test_report_keeps_source_record_references_for_every_selected_input(tmp_path):
    root = write_bundle(tmp_path / "bundle")
    result = replay_bundle(root)
    (source,) = result["sources"]
    passing = [d for d in result["decisions"] if d["decision_passed"]]
    assert len(passing) == 2
    for decision in passing:
        assert decision["inventory_source"] == {
            "source_sha256": source["source_sha256"],
            "record_index": 0,
        }
    fill = result["fill_probes"][0]
    assert fill["instrument_source"]["record_index"] == 3
    assert fill["cost_source"]["record_index"] == 4
    events = read_json((root / "events.json").read_bytes())
    referenced = events["records"][fill["quote_source"]["record_index"]]
    assert referenced["kind"] == "futures_quote"
    assert referenced["available_at"] == "2026-09-08T09:35:00.400000-04:00"


# --- availability and as-of state ------------------------------------------


def test_replay_cannot_borrow_future_inventory_or_unavailable_model(tmp_path):
    other = "SPXW|2026-09-09|6000|PUT"

    def change(plan, source):
        source["records"].append(
            event(
                "contract_list",
                "SPXW",
                {"contracts": [OPTION, other]},
                at=clock(36),
                available=clock(36, 30),
            )
        )
        record(source, "model_evidence")["available_at"] = clock(36).isoformat()

    report = run_changed(tmp_path, change)
    a, b, c = report["decisions"]
    assert a["expected_contracts"] == b["expected_contracts"] == 1
    assert a["blocker_counts"]["MISSING_MODEL_EVIDENCE"] == 1
    assert b["decision_passed"]
    assert c["expected_contracts"] == 2
    assert c["blocker_counts"]["OI_UNAVAILABLE"] == 1
    assert c["excluded_inventory_contracts"] == 0


def test_out_of_scope_inventory_is_counted_as_excluded_not_expected(tmp_path):
    far = "SPXW|2026-10-23|6000|CALL"
    past = "SPXW|2026-09-04|6000|PUT"

    def change(plan, source):
        record(source, "contract_list")["data"]["contracts"] = [OPTION, far, past]

    first = run_changed(tmp_path, change)["decisions"][0]
    assert first["expected_contracts"] == 1
    assert first["excluded_inventory_contracts"] == 2
    assert first["decision_passed"]


def test_late_old_updates_do_not_regress_state_and_revisions_arrive_when_available(
    tmp_path,
):
    plan, source = example()
    source["records"].extend(
        [
            event(
                "open_interest",
                OPTION,
                {"quantity": 12, "as_of": "2026-09-04"},
                at=clock(31),
            ),
            # Older event delivered late: known at 09:35 but must not win.
            event(
                "open_interest",
                OPTION,
                {"quantity": 99, "as_of": "2026-09-04"},
                at=clock(30),
                available=clock(35),
                sequence=1,
            ),
            # Revision of the 09:31 event: usable only from 09:36.
            event(
                "open_interest",
                OPTION,
                {"quantity": 13, "as_of": "2026-09-04"},
                at=clock(31),
                available=clock(36),
                sequence=1,
            ),
        ]
    )
    root = write_bundle(tmp_path / "bundle", plan, source)
    store, _ = load_events(root, plan["sources"])
    assert store.at("open_interest", OPTION, clock(30, 59)).data["quantity"] == 0
    assert store.at("open_interest", OPTION, clock(31)).data["quantity"] == 12
    assert store.at("open_interest", OPTION, clock(35)).data["quantity"] == 12
    assert store.at("open_interest", OPTION, clock(35, 59)).data["quantity"] == 12
    assert store.at("open_interest", OPTION, clock(36)).data["quantity"] == 13
    assert store.at("open_interest", OPTION, clock(29)) is None
    assert [
        e.data["quantity"] for e in store.available("open_interest", OPTION, clock(35))
    ] == [0, 12, 99]
    assert store.available("open_interest", OPTION, clock(29)) == ()


def test_state_availability_is_inclusive_and_updates_are_ordered(tmp_path):
    root = write_bundle(tmp_path / "bundle")
    plan = read_json((root / "replay-plan.json").read_bytes())
    store, _ = load_events(root, plan["sources"])
    at = clock(35)
    assert store.at("option_quote", OPTION, at) is not None
    assert store.at("option_quote", OPTION, at - timedelta(microseconds=1)) is None
    first = clock(35, 0, microsecond=400_000)
    assert store.next_update("futures_quote", FUTURE, at, clock(36)) == first
    assert store.next_update("futures_quote", FUTURE, first, first) == first
    assert (
        store.next_update(
            "futures_quote", FUTURE, first + timedelta(microseconds=1), clock(35, 59)
        )
        is None
    )
    assert (
        store.next_update("futures_quote", "SIMFUT|2027-01-01", at, clock(36)) is None
    )


@pytest.mark.parametrize(
    "kind", ["option_quote", "greeks", "spx_price", "open_interest"]
)
def test_absent_kind_is_blocked_instead_of_synthesized(tmp_path, kind):
    report = run_changed(
        tmp_path,
        lambda _, s: s.update(records=[r for r in s["records"] if r["kind"] != kind]),
    )
    assert report["passing_decisions"] == 0
    assert report["decisions"][0]["blocker_counts"][f"MISSING_{kind.upper()}"] == 1


@pytest.mark.parametrize(
    "data,blocker",
    [
        ({"quantity": None, "as_of": "2026-09-04"}, "OI_UNAVAILABLE"),
        ({"quantity": 1, "as_of": "2026-09-07"}, "OI_NOT_PRIOR_COMPLETED_SESSION"),
        ({"quantity": 1, "as_of": "2026-09-08"}, "OI_NOT_PRIOR_COMPLETED_SESSION"),
        ({"quantity": 1, "as_of": "2026-09-03"}, "OI_NOT_PRIOR_COMPLETED_SESSION"),
    ],
)
def test_zero_oi_is_an_answer_but_missing_or_wrong_session_is_not(
    tmp_path, data, blocker
):
    def change(_, s):
        record(s, "open_interest")["data"] = data

    report = run_changed(tmp_path, change)
    assert report["decisions"][0]["blocker_counts"][blocker] == 1
    assert report["passing_decisions"] == 0


def test_prior_completed_session_on_a_monday_is_the_preceding_friday(tmp_path):
    monday = date(2026, 9, 14)
    option = "SPXW|2026-09-14|6000|CALL"

    def bundle(as_of):
        plan, source = example()
        plan["session_date"] = monday.isoformat()
        plan["contract"]["close_buffer_minutes"] = 385
        plan["probes"] = []
        decision = clock(35, day=monday)
        known = clock(30, day=monday)
        source["records"] = [
            event("contract_list", "SPXW", {"contracts": [option]}, at=known),
            {
                **record(source, "model_evidence"),
                "event_at": known.isoformat(),
                "available_at": known.isoformat(),
            },
            event("open_interest", option, {"quantity": 7, "as_of": as_of}, at=known),
            *frame_records(decision, option),
        ]
        return replay_bundle(write_bundle(tmp_path / as_of, plan, source))

    assert bundle("2026-09-11")["passing_decisions"] == 1
    for wrong in ("2026-09-13", "2026-09-12", "2026-09-14"):
        assert bundle(wrong)["decisions"][0]["blocker_counts"] == {
            "OI_NOT_PRIOR_COMPLETED_SESSION": 1
        }


def test_unknown_model_and_crossed_option_quote_block_the_frame(tmp_path):
    def change(_, s):
        for r in s["records"]:
            if r["kind"] == "greeks":
                r["data"]["model_id"] = "FUTURE_MODEL"
            if r["kind"] == "option_quote":
                r["data"]["bid"] = "20"

    report = run_changed(tmp_path, change)
    assert report["decisions"][0]["blocker_counts"] == {
        "CROSSED_OPTION_QUOTE": 1,
        "MODEL_NOT_AVAILABLE": 1,
    }


@pytest.mark.parametrize("empty", [True, False])
def test_empty_or_missing_inventory_cannot_create_vacuous_completeness(tmp_path, empty):
    def change(_, source):
        if empty:
            record(source, "contract_list")["data"]["contracts"] = []
        else:
            source["records"] = [
                r for r in source["records"] if r["kind"] != "contract_list"
            ]

    report = run_changed(tmp_path, change)
    assert report["passing_decisions"] == 0
    assert not report["declared_grid_complete"]
    first = report["decisions"][0]
    assert first["expected_contracts"] == 0
    assert first["blocker_counts"] == (
        {"EMPTY_AS_OF_SCOPE": 1} if empty else {"INVENTORY_NOT_AVAILABLE": 1}
    )
    if empty:
        assert first["inventory_source"]["record_index"] == 0
    else:
        assert first["inventory_source"] is None
    assert all(p["reason"] == "RESEARCH_FRAME_BLOCKED" for p in report["fill_probes"])


def test_complete_declared_grid_still_does_not_certify_market_data(tmp_path):
    def change(plan, _):
        plan["contract"]["close_buffer_minutes"] = 385
        probes_only(plan, 35)

    report = run_changed(tmp_path, change)
    assert report["expected_decisions"] == report["passing_decisions"] == 1
    assert report["declared_grid_complete"]
    assert not report["ready_for_backtest"]
    assert not report["authenticity_verified"]


def test_calendar_early_close_holiday_and_empty_grid():
    grid = decision_grid(date(2026, 11, 27), ResearchContract())
    assert len(grid) == 191
    assert grid[-1].hour == 17
    assert grid[-1].minute == 45
    assert grid[0] == clock(35, day=date(2026, 11, 27)).astimezone(UTC)
    with pytest.raises(ValueError, match="trading session"):
        decision_grid(date(2026, 9, 7), ResearchContract())
    with pytest.raises(ValueError, match="trading session"):
        decision_grid(date(2026, 9, 6), ResearchContract())
    with pytest.raises(ValueError, match="empty"):
        decision_grid(date(2026, 9, 8), ResearchContract(close_buffer_minutes=500))
    assert (
        len(decision_grid(date(2026, 9, 8), ResearchContract(cadence_seconds=30)))
        == 741
    )


def test_future_model_revision_cannot_change_earlier_decision_result(tmp_path):
    original = replay_bundle(write_bundle(tmp_path / "original"))
    plan, source = example()
    source["records"].append(
        event(
            "model_evidence",
            "MODEL",
            {"model_ids": ["NEW"], "iv_source": "SYNTHETIC", "iv_price_basis": "NEW"},
            at=clock(36) + timedelta(hours=2),
        )
    )
    changed = replay_bundle(write_bundle(tmp_path / "changed", plan, source))
    assert [d["decision_passed"] for d in original["decisions"][:2]] == [
        d["decision_passed"] for d in changed["decisions"][:2]
    ]
    # Hashes bind the selected source bytes, which differ; the accounting and
    # blocker state of the earlier decisions must not.
    stateful = (
        "expected_contracts",
        "passing_contracts",
        "blocked_contracts",
        "blocker_counts",
    )
    assert [[d[k] for k in stateful] for d in original["decisions"][:2]] == [
        [d[k] for k in stateful] for d in changed["decisions"][:2]
    ]
    assert [p["price"] for p in original["fill_probes"][:2]] == [
        p["price"] for p in changed["fill_probes"][:2]
    ]
    # Source identities change, so report hashes legitimately change despite
    # equal early state.
    assert original["plan_sha256"] != changed["plan_sha256"]
    assert original["report_hash"] != changed["report_hash"]


# --- source integrity ------------------------------------------------------


def test_digest_mismatch_and_duplicate_bytes_are_refused(tmp_path):
    root = write_bundle(tmp_path / "bundle")
    plan = read_json((root / "replay-plan.json").read_bytes())
    tampered = (root / "events.json").read_bytes().replace(b'"100.25"', b'"100.00"')
    (root / "events.json").write_bytes(tampered)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_events(root, plan["sources"])
    plan, source = example()
    with pytest.raises(ValueError, match="duplicate source bytes"):
        replay_bundle(
            write_bundle(
                tmp_path / "twice", plan, source, extra_sources={"copy.json": source}
            )
        )


@pytest.mark.parametrize(
    "raw,match",
    [
        (b'{"a": 1, "a": 2}', "duplicate JSON field"),
        (b'{"a": NaN}', "nonfinite"),
        (b'{"a": Infinity}', "nonfinite"),
        (b'\xef\xbb\xbf{"a": 1}', "BOM"),
        (b'{"a": "\xff"}', "utf-8"),
    ],
)
def test_strict_json_reader_refuses_duplicates_nonfinite_and_non_utf8(raw, match):
    with pytest.raises(ValueError, match=match):
        read_json(raw)


def test_duplicate_revision_identity_is_refused_across_sources(tmp_path):
    plan, source = example()
    duplicate = {
        "schema_version": EVENT_SCHEMA,
        "origin": "SYNTHETIC",
        "records": [source["records"][2]],
    }
    with pytest.raises(ValueError, match="duplicate event revision identity"):
        replay_bundle(
            write_bundle(
                tmp_path / "bundle",
                plan,
                source,
                extra_sources={"other.json": duplicate},
            )
        )


@pytest.mark.parametrize(
    "path",
    ["../events.json", "/etc/hosts", "sub\\events.json", "", "missing.json", "."],
)
def test_source_paths_must_stay_inside_the_bundle(tmp_path, path):
    root = write_bundle(tmp_path / "bundle")
    shutil.copy(root / "events.json", tmp_path / "events.json")
    plan = read_json((root / "replay-plan.json").read_bytes())
    plan["sources"][0]["path"] = path
    (root / "replay-plan.json").write_bytes(encode(plan))
    with pytest.raises(ValueError, match=r"source path|invalid source"):
        replay_bundle(root)


def test_plan_without_sources_is_refused(tmp_path):
    root = write_bundle(tmp_path / "bundle")
    plan = read_json((root / "replay-plan.json").read_bytes())
    plan["sources"] = []
    (root / "replay-plan.json").write_bytes(encode(plan))
    with pytest.raises(ValueError, match="nonempty source descriptor"):
        replay_bundle(root)
    plan["sources"] = {"path": "events.json"}
    (root / "replay-plan.json").write_bytes(encode(plan))
    with pytest.raises(ValueError, match="nonempty source descriptor"):
        replay_bundle(root)


def test_symlink_escaping_the_bundle_is_refused(tmp_path):
    root = write_bundle(tmp_path / "bundle")
    outside = tmp_path / "outside.json"
    shutil.move(root / "events.json", outside)
    try:
        (root / "events.json").symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="invalid source path"):
        replay_bundle(root)


def test_oversized_source_is_refused(tmp_path, monkeypatch):
    root = write_bundle(tmp_path / "bundle")
    monkeypatch.setattr(event_store, "MAX_SOURCE_BYTES", 16)
    with pytest.raises(ValueError, match="invalid source path or size"):
        replay_bundle(root)


@pytest.mark.parametrize(
    "change,match",
    [
        (
            lambda p, s: s.update(schema_version="research-events/2.1.33"),
            "unsupported source schema",
        ),
        (
            lambda p, s: s.update(origin="VENDOR_VERIFIED"),
            "unsupported source schema or origin",
        ),
        (lambda p, s: s.update(extra=1), "unexpected fields"),
        (
            lambda p, s: p.update(schema_version="research-replay-plan/2.1.33"),
            "unsupported replay plan",
        ),
        (lambda p, s: p.pop("probes"), "unexpected fields"),
        (lambda p, s: p["contract"].pop("max_dte"), "unexpected fields"),
        (
            lambda p, s: p["contract"].update(max_market_age_seconds=-1),
            "invalid freshness",
        ),
        (lambda p, s: p.update(session_date=20260908), "nonempty string"),
        (lambda p, s: p.update(session_date="2026-09-07"), "trading session"),
        (
            lambda p, s: s["records"][0].update(kind="trades"),
            "unsupported normalized event kind",
        ),
        (lambda p, s: s["records"][0].update(kind=7), "nonempty string"),
        (lambda p, s: s["records"][0].update(key=["SPXW"]), "nonempty string"),
        (lambda p, s: s["records"][0].update(sequence=True), "invalid integer"),
        (lambda p, s: s["records"][0].update(sequence=-1), "invalid integer"),
        (
            lambda p, s: s["records"][0].update(event_at="2026-09-08T09:30:00"),
            "UTC offset",
        ),
        (
            lambda p, s: s["records"][0].update(event_at="yesterday"),
            "Invalid isoformat",
        ),
        (
            lambda p, s: s["records"][0].update(event_at=1757338200),
            "offset-aware string",
        ),
        (
            lambda p, s: s["records"][0].update(
                available_at="2026-09-08T09:29:59-04:00"
            ),
            "after declared availability",
        ),
        (
            lambda p, s: s["records"][0]["data"].update(contracts=[OPTION, OPTION]),
            "duplicate inventory",
        ),
        (
            lambda p, s: s["records"][0]["data"].update(contracts=[OPTION, 6000]),
            "nonempty string",
        ),
        (
            lambda p, s: s["records"][0]["data"].update(
                contracts="SPXW|2026-09-08|6000|CALL"
            ),
            "invalid inventory",
        ),
        (lambda p, s: s["records"][0].update(key="SPX"), "invalid inventory"),
        (
            lambda p, s: record(s, "open_interest")["data"].update(quantity=-1),
            "invalid integer",
        ),
        (
            lambda p, s: record(s, "open_interest")["data"].update(quantity=2.0),
            "invalid integer",
        ),
        (
            lambda p, s: record(s, "open_interest")["data"].update(quantity=10**9 + 1),
            "invalid integer",
        ),
        (
            lambda p, s: record(s, "open_interest")["data"].update(as_of=None),
            "nonempty string",
        ),
        (
            lambda p, s: record(s, "open_interest")["data"].update(as_of="2026-13-01"),
            "month must be",
        ),
        (
            lambda p, s: record(s, "option_quote")["data"].update(bid=10),
            "decimal strings",
        ),
        (
            lambda p, s: record(s, "option_quote")["data"].update(ask="0"),
            "nonnegative decimal",
        ),
        (
            lambda p, s: record(s, "option_quote")["data"].update(ask="-1"),
            "nonnegative decimal",
        ),
        (
            lambda p, s: record(s, "option_quote")["data"].update(ask="NaN"),
            "nonnegative decimal",
        ),
        (
            lambda p, s: record(s, "option_quote")["data"].update(ask="1e13"),
            "precision bounds",
        ),
        (
            lambda p, s: record(s, "option_quote")["data"].update(
                ask="0.0000000000001"
            ),
            "precision bounds",
        ),
        (
            lambda p, s: record(s, "option_quote")["data"].update(ask="ten"),
            "invalid decimal",
        ),
        (
            lambda p, s: record(s, "greeks")["data"].update(implied_vol=9.0),
            "Greek range",
        ),
        (lambda p, s: record(s, "greeks")["data"].update(delta="0.5"), "Greek inputs"),
        (
            lambda p, s: record(s, "greeks")["data"].update(rate=float("inf")),
            "nonfinite",
        ),
        (
            lambda p, s: record(s, "greeks")["data"].update(model_id=""),
            "Greek range or model",
        ),
        (lambda p, s: record(s, "spx_price").update(key="NDX"), "unexpected index"),
        (
            lambda p, s: record(s, "model_evidence")["data"].update(model_ids=[]),
            "invalid model evidence",
        ),
        (
            lambda p, s: record(s, "model_evidence")["data"].update(
                model_ids=["A", "A"]
            ),
            "invalid model evidence",
        ),
        (
            lambda p, s: record(s, "model_evidence")["data"].update(iv_source=""),
            "IV source",
        ),
        (
            lambda p, s: record(s, "instrument")["data"].update(currency="EUR"),
            "unsupported research currency",
        ),
        (
            lambda p, s: record(s, "instrument")["data"].update(
                valid_to=record(s, "instrument")["data"]["valid_from"]
            ),
            "empty effective interval",
        ),
        (
            lambda p, s: record(s, "costs")["data"].update(extra_slippage_ticks=-1),
            "invalid integer",
        ),
        (
            lambda p, s: record(s, "costs")["data"].update(
                fee_per_contract_side="-0.70"
            ),
            "nonnegative decimal",
        ),
        (
            lambda p, s: record(s, "futures_quote")["data"].update(bid_size=0.5),
            "invalid integer",
        ),
        (
            lambda p, s: record(s, "futures_quote")["data"].update(bid="0"),
            "nonnegative decimal",
        ),
        (
            lambda p, s: record(s, "futures_quote")["data"].pop("ask_size"),
            "unexpected fields",
        ),
    ],
)
def test_malformed_schema_numbers_timestamps_and_fields_are_refused(
    tmp_path, change, match
):
    refused(tmp_path, change, match)


@pytest.mark.parametrize(
    "identity",
    [
        "SPXW|2026-09-08|6000.0|CALL",
        "SPXW|2026-09-08|6E+3|CALL",
        "SPXW|2026-09-08|06000|CALL",
        "SPX|2026-09-08|6000|CALL",
        "SPXW|2026-09-08|6000|C",
        "SPXW|20260908|6000|CALL",
        "SPXW|2026-09-08|6000",
        "SPXW|2026-09-08|-5|PUT",
        "SPXW|2026-09-08|0|PUT",
        "spxw|2026-09-08|6000|CALL",
    ],
)
def test_noncanonical_option_identities_are_refused(tmp_path, identity):
    refused(
        tmp_path,
        lambda p, s: record(s, "option_quote").update(key=identity),
        "noncanonical|nonnegative|invalid",
    )


@pytest.mark.parametrize(
    "identity",
    [
        "SIMFUT",
        "SIMFUT|",
        "simfut|2026-09-18",
        "SIMFUT|2026-9-18",
        "SIMFUT|2026-09-18|X",
        "ES",
        "",
    ],
)
def test_futures_identities_require_an_explicit_expiration(tmp_path, identity):
    refused(
        tmp_path,
        lambda p, s: record(s, "futures_quote").update(key=identity),
        "expiry identity|nonempty string",
    )


def test_canonical_strike_rendering_accepts_decimals_and_large_strikes(tmp_path):
    plan, source = example()
    contracts = [OPTION, "SPXW|2026-09-08|6012.5|PUT", "SPXW|2026-09-08|12345|CALL"]
    record(source, "contract_list")["data"]["contracts"] = contracts
    plan["contract"]["close_buffer_minutes"] = 385
    plan["probes"] = []
    report = replay_bundle(write_bundle(tmp_path / "bundle", plan, source))
    first = report["decisions"][0]
    assert first["expected_contracts"] == 3
    assert first["blocked_contracts"] == 2
    assert first["passing_contracts"] == 1


# --- fill probes -----------------------------------------------------------


def probe_result(tmp_path, change, *, minutes=(35,), **kwargs):
    def apply(plan, source):
        probes_only(plan, *minutes)
        change(plan, source)

    report = run_changed(tmp_path, apply, **kwargs)
    return report["fill_probes"]


def test_quote_exactly_at_arrival_or_deadline_is_used_but_outside_is_not(tmp_path):
    def at(available_ms, event_ms=None):
        def change(plan, source):
            source["records"] = [
                r for r in source["records"] if r["kind"] != "futures_quote"
            ]
            source["records"].append(
                futures_quote(
                    clock(35),
                    event_offset_ms=available_ms if event_ms is None else event_ms,
                    available_offset_ms=available_ms,
                )
            )

        return probe_result(tmp_path, change)[0]

    assert at(250)["status"] == "SIMULATED_FILL"  # exactly at arrival
    assert at(2250)["status"] == "SIMULATED_FILL"  # exactly at deadline
    assert at(2251)["reason"] == "NO_QUOTE_WITHIN_WAIT"
    assert at(249)["reason"] == "NO_QUOTE_WITHIN_WAIT"
    # Delivered inside the window but describing the market before arrival.
    assert at(300, event_ms=249)["reason"] == "QUOTE_PREDATES_ARRIVAL"
    # Delivery latency above the policy budget.
    assert at(1500, event_ms=300)["reason"] == "QUOTE_DELIVERY_TOO_OLD"
    assert at(1300, event_ms=300)["status"] == "SIMULATED_FILL"


def test_first_observed_update_is_binding_even_when_a_later_quote_would_be_better(
    tmp_path,
):
    def change(plan, source):
        source["records"] = [
            r for r in source["records"] if r["kind"] != "futures_quote"
        ]
        source["records"].append(futures_quote(clock(35), bid="100.30", ask="100.25"))
        source["records"].append(
            futures_quote(
                clock(35), event_offset_ms=600, available_offset_ms=700, sequence=1
            )
        )

    (fill,) = probe_result(tmp_path, change)
    assert fill["reason"] == "CROSSED_QUOTE"
    assert fill["quote_available_at"] == "2026-09-08T13:35:00.400000+00:00"


def test_delayed_old_quote_cannot_substitute_for_the_known_newer_state(tmp_path):
    def change(plan, source):
        source["records"] = [
            r for r in source["records"] if r["kind"] != "futures_quote"
        ]
        # Newer market state known before arrival ...
        source["records"].append(
            futures_quote(clock(35), event_offset_ms=100, available_offset_ms=200)
        )
        # ... and an older event delivered inside the wait window.
        source["records"].append(
            futures_quote(
                clock(35),
                bid="99.00",
                ask="99.25",
                event_offset_ms=50,
                available_offset_ms=600,
            )
        )

    (fill,) = probe_result(tmp_path, change)
    assert fill["status"] == "UNFILLED"
    assert fill["reason"] == "QUOTE_PREDATES_ARRIVAL"
    assert fill["update_at"] == "2026-09-08T13:35:00.600000+00:00"
    assert fill["quote_event_at"] == "2026-09-08T13:35:00.100000+00:00"
    assert fill["quote_available_at"] == "2026-09-08T13:35:00.200000+00:00"


@pytest.mark.parametrize(
    "quote,reason",
    [
        ({"bid": "100.30", "ask": "100.25"}, "CROSSED_QUOTE"),
        ({"bid": "100.10", "ask": "100.25"}, "OFF_TICK_QUOTE"),
        ({"bid": "100.00", "ask": "100.30"}, "OFF_TICK_QUOTE"),
        ({"ask_size": 1}, "INSUFFICIENT_DISPLAYED_SIZE"),
        ({"ask_size": 0}, "INSUFFICIENT_DISPLAYED_SIZE"),
    ],
)
def test_invalid_quotes_and_inadequate_size_are_refused(tmp_path, quote, reason):
    def change(plan, source):
        record(source, "futures_quote")["data"].update(quote)

    (fill,) = probe_result(tmp_path, change)
    assert fill["status"] == "UNFILLED"
    assert fill["reason"] == reason


def test_locked_quote_and_exact_size_fill_and_bid_size_governs_sells(tmp_path):
    def change(plan, source):
        for r in source["records"]:
            if r["kind"] == "futures_quote":
                r["data"].update(bid="100.25", ask="100.25", ask_size=2, bid_size=1)

    buy, sell = probe_result(tmp_path, change, minutes=(35, 36))
    assert buy["status"] == "SIMULATED_FILL"
    assert buy["spread_points"] == "0.00"
    assert buy["price"] == "100.50"
    assert sell["reason"] == "INSUFFICIENT_DISPLAYED_SIZE"


def test_adverse_slippage_cannot_produce_a_nonpositive_price(tmp_path):
    def change(plan, source):
        for r in source["records"]:
            if r["kind"] == "futures_quote":
                r["data"].update(bid="0.25", ask="0.50")

    buy, sell = probe_result(tmp_path, change, minutes=(35, 36))
    assert buy["price"] == "0.75"
    assert sell["reason"] == "INVALID_ADVERSE_PRICE"


def test_zero_slippage_and_zero_fee_are_legitimate_explicit_assumptions(tmp_path):
    def change(plan, source):
        record(source, "costs")["data"].update(
            fee_per_contract_side="0", extra_slippage_ticks=0
        )

    (fill,) = probe_result(tmp_path, change)
    assert fill["price"] == "100.25"
    assert fill["fee"] == "0"
    assert fill["additional_slippage_cost"] == "0.00"
    assert fill["fee_plus_additional_slippage"] == "0.00"


def test_cost_arithmetic_is_exact_for_bounded_extremes(tmp_path):
    def change(plan, source):
        record(source, "instrument")["data"].update(
            tick_size="0.000000000001", point_value="999999999999.999999999999"
        )
        record(source, "costs")["data"].update(
            fee_per_contract_side="0.123456789012", extra_slippage_ticks=10**9
        )
        for r in source["records"]:
            if r["kind"] == "futures_quote":
                r["data"].update(
                    bid="999999999999.999999999999",
                    ask="999999999999.999999999999",
                    ask_size=10**9,
                )
        plan["probes"][0]["quantity"] = 10**9

    (fill,) = probe_result(tmp_path, change)
    assert fill["status"] == "SIMULATED_FILL"
    slippage = Decimal("0.000000000001") * 10**9
    assert Decimal(fill["price"]) == Decimal("999999999999.999999999999") + slippage
    assert Decimal(fill["fee"]) == Decimal("0.123456789012") * 10**9
    assert Decimal(fill["additional_slippage_cost"]) == (
        slippage * Decimal("999999999999.999999999999") * 10**9
    )
    assert Decimal(fill["fee_plus_additional_slippage"]) == Decimal(
        fill["fee"]
    ) + Decimal(fill["additional_slippage_cost"])


def test_inexact_decimal_arithmetic_is_refused_rather_than_rounded(
    tmp_path, monkeypatch
):
    from src.replay import fill_probe

    monkeypatch.setattr(fill_probe, "DECIMAL_PRECISION", 4)
    (fill,) = probe_result(tmp_path, lambda p, s: None)
    assert fill["status"] == "UNFILLED"
    assert fill["reason"] == "DECIMAL_PRECISION_EXCEEDED"


def test_metadata_and_costs_must_be_known_at_the_decision(tmp_path):
    def late(kind):
        def change(plan, source):
            record(source, kind)["available_at"] = clock(
                35, 0, microsecond=1
            ).isoformat()

        return probe_result(tmp_path, change)[0]

    for kind in ("instrument", "costs"):
        fill = late(kind)
        assert fill["reason"] == "METADATA_OR_COSTS_NOT_AVAILABLE_AT_DECISION"
        assert "instrument_source" not in fill


def test_preannounced_future_profile_does_not_hide_the_effective_one(tmp_path):
    plan, source = example()
    later = clock(34)
    source["records"].append(
        event(
            "costs",
            FUTURE,
            {
                "fee_per_contract_side": "9.99",
                "extra_slippage_ticks": 3,
                "valid_from": clock(0, hour=10).isoformat(),
                "valid_to": clock(0, hour=11).isoformat(),
            },
            at=later,
        )
    )
    plan["contract"]["close_buffer_minutes"] = SHORT_GRID
    probes_only(plan, 35)
    root = write_bundle(tmp_path / "bundle", plan, source)
    report = replay_bundle(root)
    (fill,) = report["fill_probes"]
    assert fill["status"] == "SIMULATED_FILL"
    assert fill["fee"] == "1.40"
    assert fill["cost_source"]["record_index"] == 4
    store, _ = load_events(root, plan["sources"])
    chosen, reason = effective_profile(
        store, "costs", FUTURE, known_at=clock(35), at=clock(35, 0, microsecond=250_000)
    )
    assert reason is None
    assert chosen is not None
    assert chosen.data["fee_per_contract_side"] == "0.70"
    # Once the announced window is reached the newer profile applies ...
    chosen, _ = effective_profile(
        store, "costs", FUTURE, known_at=clock(35), at=clock(0, hour=10)
    )
    assert chosen is not None
    assert chosen.data["fee_per_contract_side"] == "9.99"
    # ... but only if it was known at the decision.
    chosen, _ = effective_profile(
        store, "costs", FUTURE, known_at=clock(33), at=clock(0, hour=10)
    )
    assert chosen is not None
    assert chosen.data["fee_per_contract_side"] == "0.70"


def test_newest_known_effective_revision_wins_among_overlapping_profiles(tmp_path):
    plan, source = example()
    base = record(source, "costs")
    source["records"].append(
        {
            **event(
                "costs",
                FUTURE,
                {**base["data"], "fee_per_contract_side": "0.90"},
                at=clock(31),
            ),
        }
    )
    plan["contract"]["close_buffer_minutes"] = SHORT_GRID
    probes_only(plan, 35)
    (fill,) = replay_bundle(write_bundle(tmp_path / "bundle", plan, source))[
        "fill_probes"
    ]
    assert fill["fee"] == "1.80"


def test_profiles_not_yet_or_no_longer_effective_refuse_the_probe(tmp_path):
    def shifted(valid_from, valid_to, kind="instrument"):
        def change(plan, source):
            record(source, kind)["data"].update(
                valid_from=valid_from.isoformat(), valid_to=valid_to.isoformat()
            )

        return probe_result(tmp_path, change)[0]

    arrival = clock(35, 0, microsecond=250_000)
    fill_at = clock(35, 0, microsecond=400_000)
    # Not yet effective at arrival.
    assert shifted(arrival + timedelta(microseconds=1), clock(0, hour=17))[
        "reason"
    ] == ("METADATA_OR_COSTS_NOT_EFFECTIVE_AT_ARRIVAL")
    # Effective interval already over at arrival (half-open end).
    assert (
        shifted(clock(30), arrival)["reason"]
        == "METADATA_OR_COSTS_NOT_EFFECTIVE_AT_ARRIVAL"
    )
    # Effective at arrival but expired before the fill instant.
    result = shifted(clock(30), fill_at, kind="costs")
    assert result["reason"] == "METADATA_OR_COSTS_EXPIRED_BEFORE_FILL"
    assert result["cost_source"]["record_index"] == 4
    # Effective through the fill instant.
    assert (
        shifted(arrival, fill_at + timedelta(microseconds=1))["status"]
        == "SIMULATED_FILL"
    )


def revised(source, kind, *, available, sequence=1, **data):
    """A revision of the example's ``kind`` declaration: same event, higher sequence."""
    original = record(source, kind)
    revision = {
        **original,
        "sequence": sequence,
        "available_at": available.isoformat(),
        "data": {**original["data"], **data},
    }
    source["records"].append(revision)
    return len(source["records"]) - 1


@pytest.mark.parametrize("kind", ["costs", "instrument"])
@pytest.mark.parametrize("delivery_minute", [34, 35])
def test_known_correction_that_expires_a_profile_supersedes_it(
    tmp_path, kind, delivery_minute
):
    # Independent-review finding: a correction (same event_at, sequence 1)
    # shortening valid_to to the decision instant must not leave the original
    # (sequence 0) declaration usable, even though only the original's interval
    # still covers the arrival. Delivery exactly at the decision counts as known.
    def change(plan, source):
        revised(
            source,
            kind,
            available=clock(delivery_minute),
            valid_to=clock(35).isoformat(),
        )

    (fill,) = probe_result(tmp_path, change)
    assert fill["status"] == "UNFILLED"
    assert fill["reason"] == "METADATA_OR_COSTS_NOT_EFFECTIVE_AT_ARRIVAL"
    assert "cost_source" not in fill


@pytest.mark.parametrize("kind", ["costs", "instrument"])
def test_correction_delivered_after_the_decision_cannot_alter_it(tmp_path, kind):
    def change(plan, source):
        revised(source, kind, available=clock(36), valid_to=clock(35).isoformat())

    (fill,) = probe_result(tmp_path, change)
    assert fill["status"] == "SIMULATED_FILL"
    assert fill["fee"] == "1.40"
    reference = "cost_source" if kind == "costs" else "instrument_source"
    assert fill[reference]["record_index"] == (4 if kind == "costs" else 3)


def test_delayed_lower_sequence_cannot_resurrect_a_superseded_profile(tmp_path):
    # sequence 2 (expiring) is known early; sequence 1 (still valid) arrives
    # later but before the decision. The highest known sequence wins.
    def change(plan, source):
        revised(
            source,
            "costs",
            available=clock(32),
            sequence=2,
            valid_to=clock(35).isoformat(),
        )
        revised(
            source,
            "costs",
            available=clock(34),
            sequence=1,
            fee_per_contract_side="0.10",
        )

    (fill,) = probe_result(tmp_path, change)
    assert fill["reason"] == "METADATA_OR_COSTS_NOT_EFFECTIVE_AT_ARRIVAL"


def test_correction_that_extends_validity_is_used_and_referenced(tmp_path):
    # The original expires at arrival; a known correction extends it and changes
    # the fee. The fill must use the correction, not the superseded original.
    arrival = clock(35, 0, microsecond=250_000)

    def change(plan, source):
        record(source, "costs")["data"]["valid_to"] = arrival.isoformat()
        revised(
            source,
            "costs",
            available=clock(34),
            valid_to=clock(0, hour=17).isoformat(),
            fee_per_contract_side="0.80",
        )

    (fill,) = probe_result(tmp_path, change)
    assert fill["status"] == "SIMULATED_FILL"
    assert fill["fee"] == "1.60"
    assert fill["cost_source"]["record_index"] == 13


def test_revised_profile_expiring_between_arrival_and_fill_still_refuses(tmp_path):
    fill_at = clock(35, 0, microsecond=400_000)

    def change(plan, source):
        revised(source, "instrument", available=clock(34), valid_to=fill_at.isoformat())

    (fill,) = probe_result(tmp_path, change)
    assert fill["reason"] == "METADATA_OR_COSTS_EXPIRED_BEFORE_FILL"
    assert fill["instrument_source"]["record_index"] == 13


def test_future_declaration_and_revision_semantics_stay_distinct(tmp_path):
    plan, source = example()
    later = clock(34)
    # A separate preannounced declaration (different event_at) for 10:00-11:00 ...
    source["records"].append(
        event(
            "costs",
            FUTURE,
            {
                "fee_per_contract_side": "9.99",
                "extra_slippage_ticks": 3,
                "valid_from": clock(0, hour=10).isoformat(),
                "valid_to": clock(0, hour=11).isoformat(),
            },
            at=later,
        )
    )
    # ... and a revision of that declaration that moves it to 12:00-13:00.
    source["records"].append(
        event(
            "costs",
            FUTURE,
            {
                "fee_per_contract_side": "9.99",
                "extra_slippage_ticks": 3,
                "valid_from": clock(0, hour=12).isoformat(),
                "valid_to": clock(0, hour=13).isoformat(),
            },
            at=later,
            available=clock(34, 30),
            sequence=1,
        )
    )
    root = write_bundle(tmp_path / "bundle", plan, source)
    store, _ = load_events(root, plan["sources"])
    known = store.available("costs", FUTURE, clock(35))
    assert [e.sequence for e in known] == [0, 0, 1]
    assert [e.sequence for e in current_revisions(known)] == [0, 1]
    # 09:35 arrival: the original 09:30 declaration is still the one in force.
    chosen, _ = effective_profile(
        store, "costs", FUTURE, known_at=clock(35), at=clock(35, 0, microsecond=250_000)
    )
    assert chosen is not None
    assert chosen.data["fee_per_contract_side"] == "0.70"
    # 10:30: the moved window no longer covers it; the 09:30 declaration applies.
    chosen, _ = effective_profile(
        store, "costs", FUTURE, known_at=clock(35), at=clock(30, hour=10)
    )
    assert chosen is not None
    assert chosen.data["fee_per_contract_side"] == "0.70"
    # 12:30: the revised future declaration applies.
    chosen, _ = effective_profile(
        store, "costs", FUTURE, known_at=clock(35), at=clock(30, hour=12)
    )
    assert chosen is not None
    assert chosen.data["fee_per_contract_side"] == "9.99"
    assert chosen.sequence == 1
    # Known only at 09:34:00, the unrevised window still covered 10:30.
    chosen, _ = effective_profile(
        store, "costs", FUTURE, known_at=clock(34), at=clock(30, hour=10)
    )
    assert chosen is not None
    assert chosen.data["fee_per_contract_side"] == "9.99"
    assert chosen.sequence == 0


def test_expired_instrument_and_session_close_boundaries(tmp_path):
    def change(plan, source):
        for r in source["records"]:
            if r["key"] == FUTURE:
                r["key"] = "SIMFUT|2026-09-04"
        plan["probes"][0]["instrument"] = "SIMFUT|2026-09-04"

    (fill,) = probe_result(tmp_path, change)
    assert fill["reason"] == "EXPIRED_INSTRUMENT"

    def same_day(plan, source):
        for r in source["records"]:
            if r["key"] == FUTURE:
                r["key"] = "SIMFUT|2026-09-08"
        plan["probes"][0]["instrument"] = "SIMFUT|2026-09-08"

    assert probe_result(tmp_path, same_day)[0]["status"] == "SIMULATED_FILL"


def test_research_close_caps_the_wait_window_and_arrival(tmp_path):
    last = clock(45, hour=15)  # final default grid instant, 15 minutes before close

    def build(latency_ms, quote_offset_ms):
        plan, source = example()
        source["records"].extend(frame_records(last))
        source["records"].append(
            futures_quote(
                last,
                event_offset_ms=quote_offset_ms,
                available_offset_ms=quote_offset_ms,
            )
        )
        plan["fill_policy"] = {
            "latency_ms": latency_ms,
            "max_wait_ms": 86_400_000,
            "max_quote_age_ms": 1000,
        }
        plan["probes"] = [{**probe(45, "BUY"), "decision_at": last.isoformat()}]
        return replay_bundle(
            write_bundle(tmp_path / f"{latency_ms}-{quote_offset_ms}", plan, source)
        )["fill_probes"][0]

    fifteen_minutes = 15 * 60 * 1000
    # A quote at the close itself is outside the research session.
    assert build(0, fifteen_minutes)["reason"] == "NO_QUOTE_WITHIN_WAIT"
    assert build(0, fifteen_minutes - 1)["status"] == "SIMULATED_FILL"
    assert (
        build(fifteen_minutes, fifteen_minutes)["reason"] == "OUTSIDE_RESEARCH_SESSION"
    )
    assert build(fifteen_minutes - 1, fifteen_minutes - 1)["status"] == "SIMULATED_FILL"


def test_probe_plan_structure_is_refused_when_ambiguous(tmp_path):
    grid = decision_grid(SESSION, ResearchContract())
    ok = [probe(35, "BUY")]
    assert (
        validate_probes(ok, FILL_POLICY, SESSION.isoformat(), grid)[0]["quantity"] == 2
    )
    cases = [
        ([probe(35, "BUY"), probe(35, "SELL")], "unique probe id"),
        ([{**probe(35, "BUY"), "id": ""}], "nonempty string"),
        ([{**probe(35, "BUY"), "side": "SHORT"}], "session or side"),
        ([probe(35, "BUY", day=date(2026, 9, 9))], "session or side"),
        (
            [{**probe(35, "BUY"), "decision_at": clock(35, 30).isoformat()}],
            "declared grid instant",
        ),
        (
            [{**probe(35, "BUY"), "decision_at": clock(34).isoformat()}],
            "declared grid instant",
        ),
        ([{**probe(35, "BUY"), "decision_at": "2026-09-08T09:35:00"}], "UTC offset"),
        ([{**probe(35, "BUY"), "instrument": "SIMFUT"}], "expiry identity"),
        ([probe(35, "BUY", quantity=0)], "invalid integer"),
        ([{**probe(35, "BUY"), "quantity": 2.0}], "invalid integer"),
        ([{**probe(35, "BUY"), "extra": 1}], "unexpected fields"),
        ({"id": "x"}, "probe list required"),
    ]
    for probes, match in cases:
        with pytest.raises(ValueError, match=match):
            validate_probes(probes, FILL_POLICY, SESSION.isoformat(), grid)
    for policy, match in [
        ({**FILL_POLICY, "latency_ms": 86_400_001}, "one day"),
        ({**FILL_POLICY, "latency_ms": -1}, "invalid integer"),
        ({"latency_ms": 0}, "unexpected fields"),
    ]:
        with pytest.raises(ValueError, match=match):
            validate_probes(ok, policy, SESSION.isoformat(), grid)


def test_overlapping_windows_on_one_instrument_are_refused_but_adjacent_ones_pass(
    tmp_path,
):
    grid = decision_grid(SESSION, ResearchContract())
    session = SESSION.isoformat()
    # Consecutive decisions are 60 s apart and share the latency, so a wait of
    # 59_999 ms leaves the windows disjoint and 60_000 ms makes the first
    # deadline touch the second arrival.
    disjoint = {"latency_ms": 250, "max_wait_ms": 59_999, "max_quote_age_ms": 1000}
    touching = {**disjoint, "max_wait_ms": 60_000}
    pair = [probe(35, "BUY"), probe(36, "SELL")]
    assert len(validate_probes(pair, disjoint, session, grid)) == 2
    with pytest.raises(ValueError, match="overlapping probes"):
        validate_probes(pair, touching, session, grid)
    other = {**probe(36, "SELL"), "instrument": "SIMFUT|2026-12-18"}
    assert len(validate_probes([pair[0], other], touching, session, grid)) == 2
    probes = validate_probes(list(reversed(pair)), disjoint, session, grid)
    assert [p["id"] for p in probes] == ["demo-35", "demo-36"]


def test_frame_blocked_probe_reports_no_prices_and_no_sources(tmp_path):
    def change(plan, source):
        source["records"] = [r for r in source["records"] if r["kind"] != "spx_price"]

    (fill,) = probe_result(tmp_path, change)
    assert fill["status"] == "UNFILLED"
    assert fill["reason"] == "RESEARCH_FRAME_BLOCKED"
    assert set(fill) == {"probe", "arrival_at", "deadline", "status", "reason"}


# --- CLI and determinism ---------------------------------------------------


def test_same_bytes_relocated_produce_identical_report_and_cli_refuses_overwrite(
    tmp_path, capsys
):
    root = write_bundle(tmp_path / "first")
    second = tmp_path / "second"
    shutil.copytree(root, second)
    assert replay_bundle(root) == replay_bundle(second)
    out = tmp_path / "result.json"
    args = [str(root), "--json", str(out)]
    assert main(args) == 0
    result = out.read_bytes()
    assert b"\r" not in result
    assert result.endswith(b"}\n")
    report = json.loads(result.decode("utf-8"))
    assert report["schema_version"] == "research-session-replay/2.1.34"
    assert report["report_hash"] == replay_bundle(root)["report_hash"]
    assert capsys.readouterr().out.strip() == f"report_hash: {report['report_hash']}"
    assert json.dumps(report, indent=2, sort_keys=True).encode() + b"\n" == result
    # Refuses to overwrite and refuses to write into the evidence bundle.
    assert main(args) == 2
    assert out.read_bytes() == result
    assert main([str(root), "--json", str(root / "pollution.json")]) == 2
    assert main([str(root), "--json", str(root / "sub" / "pollution.json")]) == 2
    assert sorted(p.name for p in root.iterdir()) == ["events.json", "replay-plan.json"]
    assert "refused" in capsys.readouterr().err


def test_cli_refuses_tampered_malformed_and_missing_bundles_without_output(
    tmp_path, capsys
):
    root = write_bundle(tmp_path / "bundle")
    (root / "events.json").write_text("{}")
    target = tmp_path / "tampered.json"
    assert main([str(root), "--json", str(target)]) == 2
    assert not target.exists()
    assert "SHA-256 mismatch" in capsys.readouterr().err
    (root / "replay-plan.json").write_text("not json")
    assert main([str(root), "--json", str(target)]) == 2
    assert not target.exists()
    assert main([str(tmp_path / "absent"), "--json", str(target)]) == 2
    assert not target.exists()
    assert main([str(root), "--json", str(tmp_path / "no" / "such" / "dir.json")]) == 2


def test_recorded_origin_is_distinguishable_from_synthetic_and_never_trusted(tmp_path):
    plan, source = example()
    recorded = {
        **source,
        "origin": "RECORDED_NORMALIZED",
        "records": [record(source, "spx_price")],
    }
    source["records"] = [r for r in source["records"] if r["kind"] != "spx_price"]
    plan["contract"]["close_buffer_minutes"] = SHORT_GRID
    probes_only(plan, 35)
    report = replay_bundle(
        write_bundle(
            tmp_path / "bundle", plan, source, extra_sources={"recorded.json": recorded}
        )
    )
    assert report["observed_source_origins"] == ["RECORDED_NORMALIZED", "SYNTHETIC"]
    assert report["synthetic_only"] is False
    assert report["authenticity_verified"] is False
    assert report["normalization_verified"] is False
    assert report["source_bytes_verified"] is True
    assert {r["origin"] for r in report["sources"]} == {
        "RECORDED_NORMALIZED",
        "SYNTHETIC",
    }
    assert report["decisions"][0]["decision_passed"]


def test_event_store_state_selection_is_independent_of_input_order():
    _, source = example()
    root_records = source["records"]
    events = []
    for index, item in enumerate(root_records):
        events.append(
            event_store.Event(
                item["kind"],
                item["key"],
                event_store.stamp(item["event_at"]),
                event_store.stamp(item["available_at"]),
                item["sequence"],
                item["data"],
                "0" * 64,
                index,
            )
        )
    forward, backward = EventStore(tuple(events)), EventStore(tuple(reversed(events)))
    at = clock(36, 30)
    for item in root_records:
        assert forward.at(item["kind"], item["key"], at) == backward.at(
            item["kind"], item["key"], at
        )
        assert forward.available(item["kind"], item["key"], at) == backward.available(
            item["kind"], item["key"], at
        )
    assert (
        forward.at("option_quote", OPTION, at).record_index
        == backward.at("option_quote", OPTION, at).record_index
    )
    assert date(2026, 9, 4) == PRIOR_SESSION
