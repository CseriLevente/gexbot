"""Illustrative prices and costs only; no recorded vendor or execution evidence.

Everything built here carries ``origin = "SYNTHETIC"``. The single SPXW option,
the ``SIMFUT`` future, its tick size, point value, fees and slippage are
hypothetical inputs chosen to make the arithmetic legible. They are not futures
contract specifications, observed fees or market data, and nothing derived from
them is trading evidence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.gex.sessions import EASTERN
from src.replay.event_store import EVENT_SCHEMA
from src.replay.research_contract import ResearchContract
from src.replay.session import PLAN_SCHEMA

SESSION = date(2026, 9, 8)  # Tuesday after Labor Day: prior session is Friday.
PRIOR_SESSION = date(2026, 9, 4)
OPTION = "SPXW|2026-09-08|6000|CALL"
FUTURE = "SIMFUT|2026-09-18"
FILL_POLICY = {"latency_ms": 250, "max_wait_ms": 2000, "max_quote_age_ms": 1000}


def clock(
    minute: int = 35,
    second: int = 0,
    *,
    hour: int = 9,
    microsecond: int = 0,
    day: date = SESSION,
) -> datetime:
    return datetime(
        day.year, day.month, day.day, hour, minute, second, microsecond, tzinfo=EASTERN
    )


def event(
    kind: str,
    key: str,
    data: dict[str, Any],
    *,
    at: datetime | None = None,
    available: datetime | None = None,
    sequence: int = 0,
) -> dict[str, Any]:
    at = at or clock(30)
    return {
        "kind": kind,
        "key": key,
        "event_at": at.isoformat(),
        "available_at": (available or at).isoformat(),
        "sequence": sequence,
        "data": data,
    }


def frame_records(decision: datetime, option: str = OPTION) -> list[dict[str, Any]]:
    """Fresh option-frame inputs one second before ``decision``."""
    return [
        event(kind, key, data, at=decision - timedelta(seconds=1), available=decision)
        for kind, key, data in (
            ("option_quote", option, {"bid": "10", "ask": "10.5"}),
            (
                "greeks",
                option,
                {
                    "implied_vol": 0.2,
                    "delta": 0.5,
                    "rate": 0.042,
                    "dividend_yield": 0.0,
                    "model_id": "SYNTHETIC_MODEL_1",
                },
            ),
            ("spx_price", "SPX", {"price": "6000"}),
        )
    ]


def futures_quote(
    decision: datetime,
    *,
    bid: str = "100.00",
    ask: str = "100.25",
    bid_size: int = 5,
    ask_size: int = 5,
    event_offset_ms: int = 300,
    available_offset_ms: int = 400,
    sequence: int = 0,
) -> dict[str, Any]:
    return event(
        "futures_quote",
        FUTURE,
        {"bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size},
        at=decision + timedelta(milliseconds=event_offset_ms),
        available=decision + timedelta(milliseconds=available_offset_ms),
        sequence=sequence,
    )


def probe(
    minute: int, side: str, *, quantity: int = 2, day: date = SESSION
) -> dict[str, Any]:
    return {
        "id": f"demo-{minute}",
        "decision_at": clock(minute, day=day).isoformat(),
        "instrument": FUTURE,
        "side": side,
        "quantity": quantity,
    }


def example() -> tuple[dict[str, Any], dict[str, Any]]:
    """One hypothetical option and future on 2026-09-08.

    Only 09:35 and 09:36 receive fresh option frames, so the default grid yields
    371 decisions of which 2 pass. The 09:37 probe is blocked by its frame.
    Expected exact arithmetic with tick 0.25, point value 10, quantity 2, fee
    0.70 per contract per side and one extra adverse tick: BUY at 100.50, SELL
    at 99.75, fee 1.40, additional slippage cost 5.00, total 6.40.
    """
    known = clock(30)
    effective = {
        "valid_from": known.isoformat(),
        "valid_to": clock(0, hour=0, day=SESSION + timedelta(days=1)).isoformat(),
    }
    records = [
        event("contract_list", "SPXW", {"contracts": [OPTION]}),
        event(
            "model_evidence",
            "MODEL",
            {
                "model_ids": ["SYNTHETIC_MODEL_1"],
                "iv_source": "SYNTHETIC",
                "iv_price_basis": "ILLUSTRATIVE",
            },
        ),
        event(
            "open_interest",
            OPTION,
            {"quantity": 0, "as_of": PRIOR_SESSION.isoformat()},
        ),
        event(
            "instrument",
            FUTURE,
            {"tick_size": "0.25", "point_value": "10", "currency": "USD", **effective},
        ),
        event(
            "costs",
            FUTURE,
            {"fee_per_contract_side": "0.70", "extra_slippage_ticks": 1, **effective},
        ),
    ]
    for minute in (35, 36):
        decision = clock(minute)
        records.extend(frame_records(decision))
        records.append(futures_quote(decision))
    source = {"schema_version": EVENT_SCHEMA, "origin": "SYNTHETIC", "records": records}
    plan = {
        "schema_version": PLAN_SCHEMA,
        "session_date": SESSION.isoformat(),
        "contract": ResearchContract().as_dict(),
        "sources": [],
        "fill_policy": dict(FILL_POLICY),
        "probes": [probe(35, "BUY"), probe(36, "SELL"), probe(37, "BUY")],
    }
    return plan, source


def encode(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_bundle(
    root: Path,
    plan: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
    *,
    extra_sources: dict[str, dict[str, Any]] | None = None,
) -> Path:
    """Write ``events.json`` plus ``replay-plan.json`` bound to its digest."""
    if plan is None or source is None:
        plan, source = example()
    root.mkdir(parents=True, exist_ok=True)
    documents = {"events.json": source, **(extra_sources or {})}
    plan["sources"] = []
    for name, document in documents.items():
        raw = encode(document)
        (root / name).write_bytes(raw)
        plan["sources"].append(
            {"path": name, "sha256": hashlib.sha256(raw).hexdigest()}
        )
    (root / "replay-plan.json").write_bytes(encode(plan))
    return root
