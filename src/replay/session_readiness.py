"""Pilot-readiness accounting for one *assembled collection session*.

The single-capture report (:mod:`src.replay.pilot_readiness`) judges one
five-endpoint snapshot. A session is many sparse cycles, so this report judges
the assembled stream: which inputs were collected at all, in how many cycles,
with what failures, and how many declared decisions the chronological replay
could use. Two verdicts are kept apart on purpose:

- ``option_side_usable`` -- the option-side inputs (inventory, quotes, Greeks
  with model evidence, prior-session open interest, index prints, receipts)
  were present, and at least one declared decision passed the replay;
- ``usable_for_intraday_pilot`` -- everything the pilot brief requires,
  futures and multi-session coverage included. Without recorded futures data
  it stays false; nothing here fabricates futures.

A readiness report is a statement about data usability. It is not a backtest,
not a GEX, not a strategy result and not evidence of an edge; every trust flag
stays false.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from src.domain.canonical import canonical_payload
from src.domain.digests import digest_of
from src.replay.pilot_readiness import (
    ENDPOINT_KINDS,
    FUTURES_KINDS,
    MISSING,
    PARTIAL,
    PRESENT,
    REQUIREMENTS,
    _pairs,
    _requirement,
    _table,
)
from src.replay.research_contract import ResearchContract

SESSION_READINESS_SCHEMA = "research-pilot-readiness/2.1.36"
OPTION_SIDE = (
    "option_inventory",
    "option_quotes",
    "iv_model_inputs",
    "prior_session_open_interest",
    "spx_observations",
    "receive_times",
    "intraday_coverage",
)


def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _cycles_with(assembly: dict[str, Any], endpoint: str) -> list[dict[str, Any]]:
    return [c for c in assembly["cycles"] if endpoint in c["acquired_endpoints"]]


def _requirements(
    assembly: dict[str, Any], replay: dict[str, Any], grid_last: str | None
) -> list[dict[str, Any]]:
    cycles = assembly["cycles"]
    kinds = assembly["records_by_kind"]
    identities = assembly["identities"]
    cadence = assembly["cadence"]
    result = []
    inventory = cadence["inventory_events"]
    result.append(
        _requirement(
            "option_inventory",
            PRESENT if inventory else MISSING,
            (
                f"{inventory} listing events from cycles {cadence['inventory_cycles']}; "
                f"{identities['listed']} identities listed"
                if inventory
                else "no cycle acquired a contract listing"
            ),
            ENDPOINT_KINDS["contract_list"],
        )
    )
    quotes = kinds.get("option_quote", 0)
    quote_cycles = cadence["quote_cycles"]
    result.append(
        _requirement(
            "option_quotes",
            PRESENT if quotes else MISSING,
            (
                f"{quotes} quote records from {quote_cycles} of {len(cycles)} assembled "
                f"cycles; merge {_pairs(assembly['merge']['outcomes_by_kind'].get('option_quote', {}))}"
                if quotes
                else "no usable quote row in any cycle"
            ),
            ENDPOINT_KINDS["option_quote"],
        )
    )
    greeks = kinds.get("greeks", 0)
    model_cycles = sum(1 for c in cycles if c["model_evidence"]["emitted"] == 1)
    greeks_cycles = cadence["greeks_cycles"]
    if not greeks or not model_cycles:
        status = MISSING
        detail = "no cycle yielded Greeks with model evidence"
    elif model_cycles < len(cycles):
        status = PARTIAL
        detail = (
            f"{greeks} Greeks records; model evidence in {model_cycles} of {len(cycles)} "
            f"cycles (Greeks acquired in {greeks_cycles})"
        )
    else:
        status = PRESENT
        detail = f"{greeks} Greeks records; model evidence fixed before receipt in every cycle"
    result.append(
        _requirement("iv_model_inputs", status, detail, ENDPOINT_KINDS["greeks"])
    )
    oi = kinds.get("open_interest", 0)
    oi_cycles = cadence["open_interest_cycles"]
    if not oi:
        status, detail = MISSING, "no cycle yielded prior-session open interest"
    else:
        status = PARTIAL if identities["without_open_interest"] else PRESENT
        detail = (
            f"{oi} open-interest records from refresh cycles {oi_cycles}; "
            f"{identities['with_open_interest']} of {identities['listed']} listed "
            f"identities covered"
        )
        if identities["without_open_interest"]:
            detail += (
                f"; {identities['without_open_interest']} without a row "
                "(left unavailable, never zero)"
            )
    result.append(
        _requirement(
            "prior_session_open_interest",
            status,
            detail,
            ENDPOINT_KINDS["open_interest"],
        )
    )
    spx = kinds.get("spx_price", 0)
    result.append(
        _requirement(
            "spx_observations",
            PRESENT if spx else MISSING,
            f"{spx} index records" if spx else "no usable index print",
            ENDPOINT_KINDS["spx_price"],
        )
    )
    for name, kind in zip(
        ("futures_bid_ask_size", "futures_instrument_metadata", "futures_costs"),
        FUTURES_KINDS,
        strict=True,
    ):
        count = kinds.get(kind, 0)
        result.append(
            _requirement(
                name,
                PRESENT if count else MISSING,
                (
                    f"{count} normalized {kind} records"
                    if count
                    else f"no {kind} source in this session; the collector requests "
                    "no futures endpoint and none is fabricated"
                ),
                None,
            )
        )
    acquired = sum(len(c["acquired_endpoints"]) for c in cycles)
    unacquired = sum(len(c["unacquired_endpoints"]) for c in cycles)
    result.append(
        _requirement(
            "receive_times",
            PRESENT
            if acquired and not unacquired
            else PARTIAL
            if acquired
            else MISSING,
            (
                f"{acquired} acquired payloads, every one with a verified receipt; "
                f"{unacquired} scheduled requests produced no payload "
                f"({_pairs(assembly['session']['endpoint_failures'])})"
            ),
            None,
        )
    )
    receipts = sorted(c["first_receipt"] for c in cycles if c["first_receipt"])
    with_inventory = sum(
        1 for d in replay["decisions"] if d["inventory_source"] is not None
    )
    if not receipts or grid_last is None:
        status, detail = MISSING, "no receipt inside any grid instant"
    elif _stamp(receipts[0]) > _stamp(grid_last):
        status = MISSING
        detail = (
            f"first receipt {receipts[0]} is after the last grid decision {grid_last}"
        )
    else:
        status = PRESENT if with_inventory == replay["expected_decisions"] else PARTIAL
        detail = (
            f"{with_inventory} of {replay['expected_decisions']} grid decisions had an "
            f"inventory available; first receipt {receipts[0]}; "
            f"{assembly['session']['cycles_executed']} of "
            f"{assembly['session']['slots_planned']} slots executed"
        )
    result.append(_requirement("intraday_coverage", status, detail, None))
    result.append(
        _requirement(
            "multi_session_coverage",
            MISSING,
            f"one recorded session ({assembly['session_date']}); the multi-session "
            "summary judges several",
            None,
        )
    )
    return result


def session_readiness(
    assembly: dict[str, Any],
    replay: dict[str, Any],
    *,
    events_sha256: str,
    plan_sha256: str,
    assembly_sha256: str,
    receipt_clock_tolerance_ms: int,
    label: str | None = None,
) -> dict[str, Any]:
    """Readiness accounting for an assembled session; every trust flag stays false."""
    contract = replay["contract"]
    research_default = contract == ResearchContract().as_dict()
    diagnostic = not research_default or receipt_clock_tolerance_ms > 0
    decisions = replay["decisions"]
    grid_first = decisions[0]["decision_at"] if decisions else None
    grid_last = decisions[-1]["decision_at"] if decisions else None
    decision_blockers: Counter[str] = Counter()
    contract_blockers: Counter[str] = Counter()
    stale_decisions = 0
    skewed_decisions = 0
    oi_gap_decisions = 0
    for decision in decisions:
        reasons = decision["blocker_counts"]
        for reason, count in reasons.items():
            decision_blockers[reason] += 1
            contract_blockers[reason] += count
        if any(r.startswith("STALE_") for r in reasons):
            stale_decisions += 1
        if "MARKET_INPUTS_MISALIGNED" in reasons:
            skewed_decisions += 1
        if "OI_UNAVAILABLE" in reasons or "OI_NOT_PRIOR_COMPLETED_SESSION" in reasons:
            oi_gap_decisions += 1
    requirements = _requirements(assembly, replay, grid_last)
    by_name = {r["requirement"]: r["status"] for r in requirements}
    reasons = [r["requirement"] for r in requirements if r["status"] == MISSING]
    partial = [r["requirement"] for r in requirements if r["status"] == PARTIAL]
    option_reasons = [name for name in OPTION_SIDE if by_name[name] == MISSING]
    if replay["passing_decisions"] == 0:
        reasons.append("NO_PASSING_DECISIONS")
        option_reasons.append("NO_PASSING_DECISIONS")
    if diagnostic:
        reasons.append("DIAGNOSTIC_PARAMETERS_NOT_RESEARCH_DEFAULT")
        option_reasons.append("DIAGNOSTIC_PARAMETERS_NOT_RESEARCH_DEFAULT")
    if not assembly["verification"]["structure_verified"]:
        reasons.append("SESSION_STRUCTURE_NOT_VERIFIED")
        option_reasons.append("SESSION_STRUCTURE_NOT_VERIFIED")
    session = assembly["session"]
    payload = {
        "schema_version": SESSION_READINESS_SCHEMA,
        "label": label,
        "session_date": replay["session_date"],
        "generated_from": {
            "assembler": assembly["assembler"],
            "normalizer": assembly["normalizer"],
            "collector": assembly["collector"],
            "session_approval_hash": session["session_approval_hash"],
            "schedule_fingerprint": session["schedule_fingerprint"],
            "assembly_sha256": assembly_sha256,
            "events_sha256": events_sha256,
            "plan_sha256": plan_sha256,
            "replay_report_hash": replay["report_hash"],
            "replay_schema_version": replay["schema_version"],
            "cycles": [
                {
                    "label": c["label"],
                    "capture_session_id": c["capture_session_id"],
                    "manifest_sha256": c["manifest_sha256"],
                }
                for c in assembly["cycles"]
            ],
        },
        "parameters": {
            "receipt_clock_tolerance_ms": receipt_clock_tolerance_ms,
            "contract": contract,
            "contract_hash": replay["contract_hash"],
            "contract_is_research_default": research_default,
            "diagnostic": diagnostic,
        },
        "session": {
            "mode": session["mode"],
            "slots_planned": session["slots_planned"],
            "slots_by_status": session["slots_by_status"],
            "cycles_executed": session["cycles_executed"],
            "cycles_assembled": session["cycles_assembled"],
            "cycles_with_every_scheduled_endpoint": session[
                "cycles_with_every_scheduled_endpoint"
            ],
            "cycles_overrunning_a_boundary": session["cycles_overrunning_a_boundary"],
            "restarts": session["restarts"],
            "stops": session["stops"],
            "ended": session["ended"],
            "request_budget": session["request_budget"],
            "requests_issued": session["requests_issued"],
            "endpoint_failures": session["endpoint_failures"],
            "cadence": assembly["cadence"],
            "merge": assembly["merge"]["outcomes_by_kind"],
            "membership": assembly["merge"]["membership"],
            "ambiguity_after_known_state": assembly["merge"]["ambiguity"][
                "after_known_state"
            ],
            "ambiguity_without_known_state": assembly["merge"]["ambiguity"][
                "without_known_state"
            ],
            "vendor_clock_lead": assembly["vendor_clock_lead"],
            "identities": assembly["identities"],
            "records": assembly["records"],
            "records_by_kind": assembly["records_by_kind"],
            "structure_verified": assembly["verification"]["structure_verified"],
        },
        "replay": {
            "expected_decisions": replay["expected_decisions"],
            "passing_decisions": replay["passing_decisions"],
            "blocked_decisions": replay["blocked_decisions"],
            "grid": {"first": grid_first, "last": grid_last},
            "decisions_with_inventory": sum(
                1 for d in decisions if d["inventory_source"] is not None
            ),
            "decisions_with_stale_inputs": stale_decisions,
            "decisions_with_skewed_inputs": skewed_decisions,
            "decisions_with_open_interest_gaps": oi_gap_decisions,
            "blocker_decision_counts": dict(sorted(decision_blockers.items())),
            "blocker_contract_decision_counts": dict(sorted(contract_blockers.items())),
            "fill_probes": replay["probe_counts"],
        },
        "requirements": requirements,
        "usable_decisions": replay["passing_decisions"],
        "blocked_decisions": replay["blocked_decisions"],
        "option_side_usable": not option_reasons,
        "option_side_blocking_reasons": option_reasons,
        "usable_for_intraday_pilot": not reasons,
        "blocking_reasons": reasons,
        "partial_requirements": partial,
        "observed_source_origins": replay["observed_source_origins"],
        "synthetic_only": replay["synthetic_only"],
        "authenticity_verified": False,
        "normalization_verified": False,
        "ready_for_backtest": False,
        "trusted_for_gex": False,
        "gex_computed": False,
        "strategy_tested": False,
        "pnl_computed": False,
        "orders_placed": 0,
        "limitations": [
            "Readiness measures whether recorded inputs satisfy the declared research contract; it is not a strategy, GEX or profitability result.",
            "Receipts are the collecting process's own clock, verified as recorded bytes, not independently authenticated time.",
            "A re-observed unchanged quote keeps its original event time and receipt; staleness is measured from the event time, never refreshed by re-observation.",
            "Open interest and the contract listing are refreshed at their declared cadence; between refreshes the replay uses the last original receipt, never a later one.",
            "Exclusion and merge counts describe rows this normalizer and assembler treated under versioned rules; they are not vendor error rates.",
            "The option side can be usable while the pilot as a whole is not: no futures data is collected, and none is fabricated.",
        ],
    }
    return {**payload, "report_hash": digest_of(canonical_payload(payload))}


def render_session_markdown(report: dict[str, Any]) -> str:
    """A readable rendering of the same facts; the JSON is the authority."""
    session = report["session"]
    replay = report["replay"]
    generated = report["generated_from"]
    parameters = report["parameters"]
    lines = [
        f"# Pilot readiness: collection session {report['session_date']}",
        "",
        f"Schema `{report['schema_version']}`; report hash `{report['report_hash']}`.",
        f"Label: {report['label'] or '(none)'}.",
        "",
        "**Option side: "
        + ("usable" if report["option_side_usable"] else "NOT usable")
        + f".** Usable decisions {report['usable_decisions']}, blocked "
        f"{report['blocked_decisions']} of {replay['expected_decisions']}.",
        "",
        "**Pilot as a whole: "
        + (
            "usable for an intraday pilot replay"
            if report["usable_for_intraday_pilot"]
            else "NOT usable for an intraday pilot"
        )
        + ".**",
        "",
    ]
    if report["option_side_blocking_reasons"]:
        lines.append(
            "Option-side blocking reasons: "
            + ", ".join(f"`{r}`" for r in report["option_side_blocking_reasons"])
            + "."
        )
        lines.append("")
    if report["blocking_reasons"]:
        lines.append(
            "Pilot blocking reasons: "
            + ", ".join(f"`{r}`" for r in report["blocking_reasons"])
            + "."
        )
        lines.append("")
    if report["partial_requirements"]:
        lines.append(
            "Partially met: "
            + ", ".join(f"`{r}`" for r in report["partial_requirements"])
            + "."
        )
        lines.append("")
    if parameters["diagnostic"]:
        lines.append(
            "> Diagnostic run: parameters differ from the research default "
            f"(clock tolerance {parameters['receipt_clock_tolerance_ms']} ms, contract "
            f"default {parameters['contract_is_research_default']}). Its counts "
            "illustrate the pipeline; they are not the research result."
        )
        lines.append("")
    lines.extend(
        [
            "## Source",
            "",
            f"- Session approval `{generated['session_approval_hash']}`; schedule "
            f"fingerprint `{generated['schedule_fingerprint']}`; mode {session['mode']}.",
            f"- Collector `{generated['collector']}`; normalizer `{generated['normalizer']}`; "
            f"assembler `{generated['assembler']}`.",
            f"- Assembly SHA-256 `{generated['assembly_sha256']}`; events SHA-256 "
            f"`{generated['events_sha256']}`; plan SHA-256 `{generated['plan_sha256']}`; "
            f"replay report hash `{generated['replay_report_hash']}`.",
            f"- Origins {report['observed_source_origins']}; synthetic only: "
            f"{report['synthetic_only']}; structure verified: "
            f"{session['structure_verified']}.",
            "",
            "## Session",
            "",
            f"- Slots planned {session['slots_planned']}; by status "
            f"{_pairs(session['slots_by_status'])}.",
            f"- Cycles executed {session['cycles_executed']}, assembled "
            f"{session['cycles_assembled']}, with every scheduled endpoint "
            f"{session['cycles_with_every_scheduled_endpoint']}, overrunning a boundary "
            f"{session['cycles_overrunning_a_boundary']}; restarts {session['restarts']}.",
            f"- Requests issued {session['requests_issued']} of budget "
            f"{session['request_budget']['requests']}; endpoint failures "
            f"{_pairs(session['endpoint_failures'])}.",
            f"- Inventory cycles {session['cadence']['inventory_cycles']}; open-interest "
            f"cycles {session['cadence']['open_interest_cycles']}; quote cycles "
            f"{session['cadence']['quote_cycles']}; Greeks cycles "
            f"{session['cadence']['greeks_cycles']}.",
            "- Merge outcomes: "
            + "; ".join(
                f"{kind} {_pairs(counts)}" for kind, counts in session["merge"].items()
            )
            + ".",
            f"- Membership against the latest listing: {_pairs(session['membership'])}; "
            f"ambiguous observations after a known state "
            f"{session['ambiguity_after_known_state']} (state left standing), without "
            f"{session['ambiguity_without_known_state']}.",
            f"- Vendor clock leads: {session['vendor_clock_lead']['rows_after_receipt']} rows "
            f"(max {session['vendor_clock_lead']['max_ms']} ms).",
            f"- Identities: listed {session['identities']['listed']}; quoted "
            f"{session['identities']['quoted']}; with Greeks {session['identities']['greeked']}; "
            f"with open interest {session['identities']['with_open_interest']}; without "
            f"{session['identities']['without_open_interest']}.",
            f"- Records {session['records']}: {_pairs(session['records_by_kind'])}.",
            "- Ended: "
            + (
                f"{session['ended']['status']} ({session['ended']['reason'] or 'no reason'})"
                if session["ended"]
                else "no SESSION_END entry"
            )
            + ".",
            "",
            "## Requirements",
            "",
        ]
    )
    lines.extend(
        _table(
            ["Requirement", "Status", "Detail"],
            [
                [r["requirement"], r["status"], r["detail"]]
                for r in report["requirements"]
            ],
        )
    )
    lines.extend(
        [
            "",
            "## Replay",
            "",
            f"- Contract hash `{parameters['contract_hash']}`; grid {replay['grid']['first']} → "
            f"{replay['grid']['last']} ({replay['expected_decisions']} decisions).",
            f"- Decisions with an inventory available: {replay['decisions_with_inventory']}; "
            f"with stale inputs {replay['decisions_with_stale_inputs']}; with skewed inputs "
            f"{replay['decisions_with_skewed_inputs']}; with open-interest gaps "
            f"{replay['decisions_with_open_interest_gaps']}.",
            f"- Blockers by decisions affected: {_pairs(replay['blocker_decision_counts'])}.",
            "- Blockers by contract-decisions: "
            f"{_pairs(replay['blocker_contract_decision_counts'])}.",
            "",
            "## Limitations",
            "",
        ]
    )
    lines.extend(f"- {text}" for text in report["limitations"])
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "OPTION_SIDE",
    "REQUIREMENTS",
    "SESSION_READINESS_SCHEMA",
    "render_session_markdown",
    "session_readiness",
]
