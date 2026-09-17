"""Replay every declared decision-grid instant from verified normalized bytes."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from src.domain.canonical import canonical_payload
from src.domain.digests import digest_of
from src.gex.calendar import is_trading_session, session_close_datetime
from src.gex.sessions import EASTERN
from src.replay.event_store import (
    EventStore,
    decimal_value,
    fields,
    load_events,
    option_parts,
    read_json,
    stamp,
    text,
)
from src.replay.fill_probe import simulate_probe, validate_probes
from src.replay.research_contract import (
    Observation,
    ResearchContract,
    ResearchFrame,
    audit_frame,
)

PLAN_SCHEMA = "research-replay-plan/2.1.34"
REPORT_SCHEMA = "research-session-replay/2.1.34"


def decision_grid(session: date, contract: ResearchContract) -> tuple[datetime, ...]:
    if not is_trading_session(session):
        raise ValueError("replay requires a trading session")
    start = datetime.combine(session, time(9, 30), EASTERN) + timedelta(
        minutes=contract.open_delay_minutes
    )
    end = session_close_datetime(session) - timedelta(
        minutes=contract.close_buffer_minutes
    )
    if start > end:
        raise ValueError("empty research decision grid")
    result = []
    while start <= end:
        result.append(start.astimezone(UTC))
        start += timedelta(seconds=contract.cadence_seconds)
    return tuple(result)


def _decision(
    store: EventStore, at: datetime, contract: ResearchContract
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "decision_at": at.isoformat(),
        "inventory_source": None,
        "expected_contracts": 0,
        "passing_contracts": 0,
        "blocked_contracts": 0,
        "excluded_inventory_contracts": 0,
        "blocker_counts": {},
        "frame_hashes": [],
    }
    inventory = store.at("contract_list", "SPXW", at)
    if inventory is None:
        base["blocker_counts"] = {"INVENTORY_NOT_AVAILABLE": 1}
    else:
        base["inventory_source"] = inventory.reference()
        keys = []
        for key in inventory.data["contracts"]:
            root, expiry, _, _ = option_parts(key)
            if (
                root == contract.root
                and 0
                <= (expiry - at.astimezone(EASTERN).date()).days
                <= contract.max_dte
            ):
                keys.append(key)
        base["excluded_inventory_contracts"] = len(inventory.data["contracts"]) - len(
            keys
        )
        base["expected_contracts"] = len(keys)
        blockers: Counter[str] = Counter()
        if not keys:
            blockers["EMPTY_AS_OF_SCOPE"] += 1
        for key in sorted(keys):
            root, expiry, _, _ = option_parts(key)
            selected = {
                "contract_list": inventory,
                "option_quote": store.at("option_quote", key, at),
                "greeks": store.at("greeks", key, at),
                "open_interest": store.at("open_interest", key, at),
                "spx_price": store.at("spx_price", "SPX", at),
                "model_evidence": store.at("model_evidence", "MODEL", at),
            }
            oi = selected["open_interest"]
            frame = ResearchFrame(
                key,
                at,
                root,
                expiry,
                oi.data["quantity"] if oi else None,
                date.fromisoformat(oi.data["as_of"]) if oi else None,
                tuple(
                    Observation(kind, e.event_at, e.available_at, e.source_sha256)
                    for kind, e in sorted(selected.items())
                    if e
                ),
            )
            audit = audit_frame(frame, contract)
            reasons = set(audit["blockers"])
            quote, greeks, model = (
                selected["option_quote"],
                selected["greeks"],
                selected["model_evidence"],
            )
            if quote and decimal_value(quote.data["bid"], zero=True) > decimal_value(
                quote.data["ask"]
            ):
                reasons.add("CROSSED_OPTION_QUOTE")
            if (
                greeks
                and model
                and greeks.data["model_id"] not in model.data["model_ids"]
            ):
                reasons.add("MODEL_NOT_AVAILABLE")
            if reasons:
                base["blocked_contracts"] += 1
                blockers.update(reasons)
            else:
                base["passing_contracts"] += 1
            base["frame_hashes"].append(
                digest_of(
                    canonical_payload(
                        {
                            "audit": audit,
                            "blockers": sorted(reasons),
                            "selected_records": {
                                kind: e.reference()
                                for kind, e in sorted(selected.items())
                                if e
                            },
                        }
                    )
                )
            )
        base["blocker_counts"] = dict(sorted(blockers.items()))
    base["decision_passed"] = (
        base["expected_contracts"] > 0 and not base["blocker_counts"]
    )
    hashes = base.pop("frame_hashes")
    base["frame_set_hash"] = digest_of(hashes)
    return {**base, "decision_hash": digest_of(canonical_payload(base))}


def replay_bundle(root: Path) -> dict[str, Any]:
    """The public entry rereads both the plan and every hash-bound source file."""
    root = root.resolve()
    raw = (root / "replay-plan.json").read_bytes()
    plan = fields(
        read_json(raw),
        {
            "schema_version",
            "session_date",
            "contract",
            "sources",
            "fill_policy",
            "probes",
        },
    )
    if plan["schema_version"] != PLAN_SCHEMA:
        raise ValueError("unsupported replay plan")
    fields(plan["contract"], set(ResearchContract().as_dict()))
    contract = ResearchContract(**plan["contract"])
    session = date.fromisoformat(text(plan["session_date"]))
    grid = decision_grid(session, contract)
    probes = validate_probes(
        plan["probes"], plan["fill_policy"], session.isoformat(), grid
    )
    store, receipts = load_events(root, plan["sources"])
    decisions = [_decision(store, at, contract) for at in grid]
    status = {stamp(d["decision_at"]): d["decision_passed"] for d in decisions}
    fills = [
        simulate_probe(
            store,
            probe,
            plan["fill_policy"],
            frame_allowed=status[stamp(probe["decision_at"])],
        )
        for probe in probes
    ]
    passed = sum(d["decision_passed"] for d in decisions)
    probe_outcomes = Counter(f["status"] for f in fills)
    refusals = Counter(f["reason"] for f in fills if f["reason"] is not None)
    payload = {
        "schema_version": REPORT_SCHEMA,
        "session_date": session.isoformat(),
        "plan_sha256": hashlib.sha256(raw).hexdigest(),
        "contract": contract.as_dict(),
        "contract_hash": contract.fingerprint(),
        "sources": receipts,
        "source_bytes_verified": True,
        "authenticity_verified": False,
        "normalization_verified": False,
        "observed_source_origins": sorted({r["origin"] for r in receipts}),
        "expected_decisions": len(grid),
        "passing_decisions": passed,
        "blocked_decisions": len(grid) - passed,
        "declared_grid_complete": passed == len(grid),
        "decisions": decisions,
        "fill_policy": plan["fill_policy"],
        "fill_probes": fills,
        "probe_counts": {
            "total": len(fills),
            "simulated_fills": probe_outcomes.get("SIMULATED_FILL", 0),
            "unfilled": probe_outcomes.get("UNFILLED", 0),
            "refusal_reasons": dict(sorted(refusals.items())),
        },
        "synthetic_only": all(r["origin"] == "SYNTHETIC" for r in receipts),
        "ready_for_backtest": False,
        "trusted_for_gex": False,
        "gex_computed": False,
        "strategy_tested": False,
        "pnl_computed": False,
        "orders_placed": 0,
        "limitations": [
            "Source digests verify bytes against this plan, not vendor authenticity or normalization correctness.",
            "The inventory is the latest supplied as-of list, not independently proven exchange completeness.",
            "Event and availability timestamps are bound declarations, not independently verified clocks.",
            "The contract's market-age and skew limits are research design assumptions, not calibrated strategy parameters.",
            "Probes use the first quote observed after latency; displayed size cannot guarantee execution.",
            "The research-session cutoff bounds this replay; it is not a model of futures exchange hours.",
            "Fees and slippage are supplied assumptions; no position, portfolio, strategy or profitability is evaluated.",
        ],
    }
    return {**payload, "report_hash": digest_of(canonical_payload(payload))}
