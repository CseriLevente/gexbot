"""Offline quote-based fill probes. No broker, position or strategy execution.

Every probe is an independent counterfactual: "had a research decision at
``decision_at`` sent a ``BUY``/``SELL`` for ``quantity`` contracts of one
explicitly identified futures expiration, what would the first quote observed
after the declared latency have offered?" The answer carries no position,
portfolio, order or PnL. Displayed size is never a guarantee of execution.

Time conventions (all comparisons are on absolute instants):

- ``arrival = decision_at + latency_ms``; ``deadline = arrival + max_wait_ms``.
- The candidate is the first ``futures_quote`` record made *available* in
  ``[arrival, min(deadline, research session close)]``. Nothing later is
  examined, so a probe can never search forward for a better price.
- The state at that instant must describe a market event at or after
  ``arrival`` and must have been delivered within ``max_quote_age_ms`` of its
  event; a standing pre-arrival quote or a delayed old row is refused.
- Instrument metadata and cost profiles are chosen among the records that
  were *available at the decision*. Each declared event is first collapsed to
  its highest known revision (``sequence``); of the surviving declarations,
  the newest whose effective interval ``[valid_from, valid_to)`` covers
  ``arrival`` applies, and it must still be in force at the fill instant.
- The research window ends at the session close of the repository calendar.
  That is a scope restriction on this research, not a model of futures
  exchange hours.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Inexact, Rounded, localcontext
from typing import Any

from src.gex.calendar import session_close_datetime
from src.gex.sessions import EASTERN
from src.replay.event_store import (
    Event,
    EventStore,
    decimal_value,
    fields,
    instrument_expiry,
    integer,
    stamp,
    text,
)

#: Exact arithmetic budget. Inputs are bounded (12 integer and 12 fractional
#: digits per decimal, ``MAX_INTEGER`` per count), so every product here fits;
#: the traps turn any silent rounding into an explicit refusal.
DECIMAL_PRECISION = 120


def validate_probes(
    raw: Any, policy: dict[str, Any], session: str, grid: tuple[datetime, ...]
) -> list[dict[str, Any]]:
    """Structural plan checks. A plan that fails here is refused whole."""
    fields(policy, {"latency_ms", "max_wait_ms", "max_quote_age_ms"})
    for value in policy.values():
        integer(value)
        if value > 86_400_000:
            raise ValueError("fill interval exceeds one day")
    if not isinstance(raw, list):
        raise ValueError("probe list required")
    instants = set(grid)
    seen = set()
    probes = []
    windows: dict[str, list[tuple[datetime, datetime]]] = {}
    for item in raw:
        fields(item, {"id", "decision_at", "instrument", "side", "quantity"})
        if text(item["id"]) in seen:
            raise ValueError("unique probe id required")
        seen.add(item["id"])
        decision = stamp(item["decision_at"])
        if decision.astimezone(EASTERN).date().isoformat() != session or item[
            "side"
        ] not in {"BUY", "SELL"}:
            raise ValueError("invalid probe session or side")
        if decision not in instants:
            raise ValueError("probe decision must be a declared grid instant")
        instrument_expiry(item["instrument"])
        integer(item["quantity"], minimum=1)
        arrival = decision + timedelta(milliseconds=policy["latency_ms"])
        end = arrival + timedelta(milliseconds=policy["max_wait_ms"])
        for prior_start, prior_end in windows.setdefault(item["instrument"], []):
            if arrival <= prior_end and prior_start <= end:
                raise ValueError("overlapping probes could reuse displayed liquidity")
        windows[item["instrument"]].append((arrival, end))
        probes.append({**item, "decision_at": decision.isoformat()})
    return sorted(probes, key=lambda p: (p["decision_at"], p["id"]))


def _in_force(event: Event, at: datetime) -> bool:
    return stamp(event.data["valid_from"]) <= at < stamp(event.data["valid_to"])


def current_revisions(known: tuple[Event, ...]) -> list[Event]:
    """Collapse each declared event to its highest known ``sequence``.

    A revision supersedes every lower sequence of the same ``event_at``
    outright, whatever its interval says and whenever the lower one was
    delivered; only distinct declarations (different ``event_at``) remain side
    by side. Filtering intervals before this collapse would let a superseded
    version survive a correction that shortened or moved its validity.
    """
    current: dict[datetime, Event] = {}
    for event in known:
        latest = current.get(event.event_at)
        if latest is None or event.sequence > latest.sequence:
            current[event.event_at] = event
    return list(current.values())


def effective_profile(
    store: EventStore, kind: str, key: str, *, known_at: datetime, at: datetime
) -> tuple[Event | None, str | None]:
    """The declaration in force at ``at`` among those known at ``known_at``.

    Returns ``(record, None)`` or ``(None, refusal reason)``. Knowledge is
    bounded by availability at the decision; revisions are collapsed first;
    effectiveness is then evaluated at the arrival instant. A preannounced
    future profile is known but not in force, and an older still-effective
    declaration is preferred over it.
    """
    known = store.available(kind, key, known_at)
    if not known:
        return None, "METADATA_OR_COSTS_NOT_AVAILABLE_AT_DECISION"
    effective = [event for event in current_revisions(known) if _in_force(event, at)]
    if not effective:
        return None, "METADATA_OR_COSTS_NOT_EFFECTIVE_AT_ARRIVAL"
    return max(effective, key=lambda e: (e.event_at, e.sequence)), None


def simulate_probe(
    store: EventStore,
    probe: dict[str, Any],
    policy: dict[str, Any],
    *,
    frame_allowed: bool,
) -> dict[str, Any]:
    """First observed update after latency; no search for a more favorable fill."""
    decision = stamp(probe["decision_at"])
    arrival = decision + timedelta(milliseconds=policy["latency_ms"])
    deadline = arrival + timedelta(milliseconds=policy["max_wait_ms"])
    key = probe["instrument"]
    base: dict[str, Any] = {
        "probe": probe,
        "arrival_at": arrival.isoformat(),
        "deadline": deadline.isoformat(),
        "status": "UNFILLED",
        "reason": None,
    }

    def refuse(reason: str) -> dict[str, Any]:
        return {**base, "reason": reason}

    if not frame_allowed:
        return refuse("RESEARCH_FRAME_BLOCKED")
    if arrival.astimezone(EASTERN).date() > instrument_expiry(key):
        return refuse("EXPIRED_INSTRUMENT")
    close = session_close_datetime(decision.astimezone(EASTERN).date())
    if arrival >= close:
        return refuse("OUTSIDE_RESEARCH_SESSION")
    metadata, reason = effective_profile(
        store, "instrument", key, known_at=decision, at=arrival
    )
    costs, cost_reason = effective_profile(
        store, "costs", key, known_at=decision, at=arrival
    )
    if metadata is None or costs is None:
        return refuse(str(reason or cost_reason))
    base["instrument_source"] = metadata.reference()
    base["cost_source"] = costs.reference()
    update = store.next_update(
        "futures_quote", key, arrival, min(deadline, close - timedelta(microseconds=1))
    )
    if update is None:
        return refuse("NO_QUOTE_WITHIN_WAIT")
    quote = store.at("futures_quote", key, update)
    assert quote is not None
    base["update_at"] = update.isoformat()
    base["quote_source"] = quote.reference()
    base["quote_event_at"] = quote.event_at.isoformat()
    base["quote_available_at"] = quote.available_at.isoformat()
    if quote.event_at < arrival:
        return refuse("QUOTE_PREDATES_ARRIVAL")
    if (update - quote.event_at) > timedelta(milliseconds=policy["max_quote_age_ms"]):
        return refuse("QUOTE_DELIVERY_TOO_OLD")
    if not (_in_force(metadata, update) and _in_force(costs, update)):
        return refuse("METADATA_OR_COSTS_EXPIRED_BEFORE_FILL")
    side = probe["side"]
    size = quote.data["ask_size" if side == "BUY" else "bid_size"]
    quantity = probe["quantity"]
    try:
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            tick = decimal_value(metadata.data["tick_size"])
            bid, ask = (decimal_value(quote.data[k]) for k in ("bid", "ask"))
            if bid > ask:
                return refuse("CROSSED_QUOTE")
            if bid % tick or ask % tick:
                return refuse("OFF_TICK_QUOTE")
            if size < quantity:
                return refuse("INSUFFICIENT_DISPLAYED_SIZE")
            slippage = tick * costs.data["extra_slippage_ticks"]
            price = ask + slippage if side == "BUY" else bid - slippage
            if price <= 0:
                return refuse("INVALID_ADVERSE_PRICE")
            point_value = decimal_value(metadata.data["point_value"])
            fee = (
                decimal_value(costs.data["fee_per_contract_side"], zero=True) * quantity
            )
            slip_cost = slippage * point_value * quantity
            total = fee + slip_cost
            spread = ask - bid
    except ArithmeticError:
        return refuse("DECIMAL_PRECISION_EXCEEDED")
    return {
        **base,
        "status": "SIMULATED_FILL",
        "reason": None,
        "fill_at": update.isoformat(),
        "price": format(price, "f"),
        "quoted_side_price": format(ask if side == "BUY" else bid, "f"),
        "spread_points": format(spread, "f"),
        "fee": format(fee, "f"),
        "additional_slippage_cost": format(slip_cost, "f"),
        "fee_plus_additional_slippage": format(total, "f"),
        "currency": metadata.data["currency"],
        "liquidity_guaranteed": False,
        "point_value": format(point_value, "f"),
    }
