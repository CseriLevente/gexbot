"""Pilot-readiness accounting for one normalized session. Decides nothing else.

Reads the normalizer's coverage and the chronological replay report, and
states which pilot inputs are present, partial or missing, how many declared
decisions were usable, and why the rest were refused. A readiness report is a
statement about data usability. It is not a backtest, not a GEX, not a strategy
result and not evidence of an edge; every trust flag it carries stays false.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from src.domain.canonical import canonical_payload
from src.domain.digests import digest_of
from src.replay.research_contract import ResearchContract

READINESS_SCHEMA = "research-pilot-readiness/2.1.35"
PRESENT, PARTIAL, MISSING = "PRESENT", "PARTIAL", "MISSING"
FUTURES_KINDS = ("futures_quote", "instrument", "costs")
ENDPOINT_KINDS = {
    "contract_list": "/v3/option/list/contracts/quote",
    "option_quote": "/v3/option/snapshot/quote",
    "greeks": "/v3/option/snapshot/greeks/first_order",
    "open_interest": "/v3/option/snapshot/open_interest",
    "spx_price": "/v3/index/snapshot/price",
}
#: What a small multi-session intraday pilot needs, in the order the acceptance
#: brief lists them. Each is judged from recorded evidence only.
REQUIREMENTS = (
    ("option_inventory", "SPXW option inventory with a recorded receipt"),
    ("option_quotes", "Option bid/ask observations with vendor event times"),
    ("iv_model_inputs", "Implied volatility, delta and a model fixed before receipt"),
    ("prior_session_open_interest", "Open interest attributed to the prior session"),
    ("spx_observations", "SPX index observations"),
    ("futures_bid_ask_size", "Executable futures bid/ask with displayed sizes"),
    (
        "futures_instrument_metadata",
        "Effective-dated futures tick size and point value",
    ),
    ("futures_costs", "Effective-dated fees and slippage assumptions"),
    ("receive_times", "A recorded receipt for every payload used"),
    ("intraday_coverage", "Observations available inside the research decision grid"),
    ("multi_session_coverage", "More than one recorded trading session"),
)


def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _endpoint(coverage: dict[str, Any], kind: str) -> dict[str, Any]:
    return dict(coverage["endpoints"][ENDPOINT_KINDS[kind]])


def _requirement(
    name: str, status: str, detail: str, source: str | None
) -> dict[str, Any]:
    description = dict(REQUIREMENTS)[name]
    return {
        "requirement": name,
        "description": description,
        "status": status,
        "detail": detail,
        "source": source,
    }


def _kind_status(coverage: dict[str, Any], kind: str) -> tuple[str, str, str | None]:
    endpoint = _endpoint(coverage, kind)
    receipt = endpoint["receipt"]
    excluded = sum(endpoint["excluded"].values())
    if receipt["refusal"] is not None:
        return MISSING, f"receipt refused: {receipt['refusal']}", receipt["endpoint"]
    if endpoint["emitted"] == 0:
        return (
            MISSING,
            f"{endpoint['rows']} native rows, none usable ({_pairs(endpoint['excluded'])})",
            receipt["endpoint"],
        )
    detail = (
        f"{endpoint['emitted']} of {endpoint['rows']} native rows usable; "
        f"received {receipt['available_at']}"
    )
    if excluded:
        detail += f"; excluded {_pairs(endpoint['excluded'])}"
    return PRESENT, detail, receipt["endpoint"]


def _requirements(
    coverage: dict[str, Any], replay: dict[str, Any], grid_last: str | None
) -> list[dict[str, Any]]:
    result = []
    result.append(
        _requirement("option_inventory", *_kind_status(coverage, "contract_list"))
    )
    result.append(
        _requirement("option_quotes", *_kind_status(coverage, "option_quote"))
    )
    status, detail, source = _kind_status(coverage, "greeks")
    model = coverage["model_evidence"]
    if status == PRESENT and model["emitted"] != 1:
        status, detail = (
            MISSING,
            f"Greeks present but model evidence excluded: {_pairs(model['excluded'])}",
        )
    elif status == PRESENT:
        detail += f"; model {model['model_id']} fixed at {model['model_fixed_at']}"
    result.append(_requirement("iv_model_inputs", status, detail, source))
    status, detail, source = _kind_status(coverage, "open_interest")
    identities = coverage["identities"]
    if status == PRESENT and identities["without_open_interest"]:
        status = PARTIAL
        detail += (
            f"; {identities['without_open_interest']} of {identities['listed']} listed "
            f"identities have no open-interest row (left unavailable, never zero)"
        )
    if status != MISSING:
        detail += (
            f"; rows by as-of {_pairs(coverage['open_interest']['rows_by_as_of'])}"
        )
    result.append(_requirement("prior_session_open_interest", status, detail, source))
    result.append(
        _requirement("spx_observations", *_kind_status(coverage, "spx_price"))
    )
    kinds = coverage["records_by_kind"]
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
                    else f"no {kind} source in this capture; the pinned vendor document "
                    "describes no futures endpoint"
                ),
                None,
            )
        )
    receipts = [e["receipt"] for e in coverage["endpoints"].values()]
    refused = [r["endpoint"] for r in receipts if r["refusal"] is not None]
    corroborated = sum(1 for r in receipts if len(r["evidence"]) >= 2)
    result.append(
        _requirement(
            "receive_times",
            MISSING
            if len(refused) == len(receipts)
            else PARTIAL
            if refused
            else PRESENT,
            (
                f"{len(receipts) - len(refused)} of {len(receipts)} payloads carry a "
                f"recorded receipt ({corroborated} corroborated by the attempt log)"
                + (f"; refused: {refused}" if refused else "")
            ),
            None,
        )
    )
    available = sorted(
        r["available_at"] for r in receipts if r["available_at"] is not None
    )
    with_inventory = sum(
        1 for d in replay["decisions"] if d["inventory_source"] is not None
    )
    if not available or grid_last is None:
        status, detail = MISSING, "no receipt inside any grid instant"
    elif _stamp(available[0]) > _stamp(grid_last):
        status = MISSING
        detail = (
            f"first receipt {available[0]} is after the last grid decision "
            f"{grid_last}; nothing was available during the research window"
        )
    else:
        status = PRESENT if with_inventory == replay["expected_decisions"] else PARTIAL
        detail = (
            f"{with_inventory} of {replay['expected_decisions']} grid decisions had an "
            f"inventory available; first receipt {available[0]}"
        )
    result.append(_requirement("intraday_coverage", status, detail, None))
    result.append(
        _requirement(
            "multi_session_coverage",
            MISSING,
            f"one recorded session ({coverage['session_date']}); a pilot needs several",
            None,
        )
    )
    return result


def pilot_readiness(
    coverage: dict[str, Any],
    replay: dict[str, Any],
    *,
    events_sha256: str,
    plan_sha256: str,
    receipt_clock_tolerance_ms: int,
    label: str | None = None,
) -> dict[str, Any]:
    """Readiness accounting from recorded evidence; every trust flag stays false."""
    contract = replay["contract"]
    research_default = contract == ResearchContract().as_dict()
    diagnostic = not research_default or receipt_clock_tolerance_ms > 0
    decisions = replay["decisions"]
    grid_first = decisions[0]["decision_at"] if decisions else None
    grid_last = decisions[-1]["decision_at"] if decisions else None
    decision_blockers: Counter[str] = Counter()
    contract_blockers: Counter[str] = Counter()
    for decision in decisions:
        for reason, count in decision["blocker_counts"].items():
            decision_blockers[reason] += 1
            contract_blockers[reason] += count
    best = max(
        decisions,
        key=lambda d: (d["passing_contracts"], -_stamp(d["decision_at"]).timestamp()),
        default=None,
    )
    requirements = _requirements(coverage, replay, grid_last)
    reasons = [r["requirement"] for r in requirements if r["status"] == MISSING]
    partial = [r["requirement"] for r in requirements if r["status"] == PARTIAL]
    if replay["passing_decisions"] == 0:
        reasons.append("NO_PASSING_DECISIONS")
    if diagnostic:
        reasons.append("DIAGNOSTIC_PARAMETERS_NOT_RESEARCH_DEFAULT")
    payload = {
        "schema_version": READINESS_SCHEMA,
        "label": label,
        "session_date": replay["session_date"],
        "generated_from": {
            "normalizer": coverage["normalizer"],
            "capture": coverage["capture"],
            "events_sha256": events_sha256,
            "plan_sha256": plan_sha256,
            "replay_report_hash": replay["report_hash"],
            "replay_schema_version": replay["schema_version"],
        },
        "parameters": {
            "receipt_clock_tolerance_ms": receipt_clock_tolerance_ms,
            "contract": contract,
            "contract_hash": replay["contract_hash"],
            "contract_is_research_default": research_default,
            "diagnostic": diagnostic,
        },
        "coverage": coverage,
        "replay": {
            "expected_decisions": replay["expected_decisions"],
            "passing_decisions": replay["passing_decisions"],
            "blocked_decisions": replay["blocked_decisions"],
            "grid": {"first": grid_first, "last": grid_last},
            "decisions_with_inventory": sum(
                1 for d in decisions if d["inventory_source"] is not None
            ),
            "blocker_decision_counts": dict(sorted(decision_blockers.items())),
            "blocker_contract_decision_counts": dict(sorted(contract_blockers.items())),
            "best_decision": (
                {
                    key: best[key]
                    for key in (
                        "decision_at",
                        "expected_contracts",
                        "passing_contracts",
                        "blocked_contracts",
                        "excluded_inventory_contracts",
                        "blocker_counts",
                        "decision_hash",
                    )
                }
                if best is not None
                else None
            ),
            "fill_probes": replay["probe_counts"],
        },
        "requirements": requirements,
        "usable_decisions": replay["passing_decisions"],
        "blocked_decisions": replay["blocked_decisions"],
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
            "Receipts are the capturing process's own clocks, verified as recorded bytes, not independently authenticated time.",
            "A single close-of-session snapshot cannot reconstruct the session's intraday history; missing history stays missing.",
            "Exclusion counts describe rows this normalizer refused under versioned rules; they are not vendor error rates.",
        ],
    }
    return {**payload, "report_hash": digest_of(canonical_payload(payload))}


def _pairs(counts: dict[str, Any]) -> str:
    return ", ".join(f"{key} {value}" for key, value in counts.items()) or "none"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(" --- " for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(str(c) for c in row) + " |" for row in rows)
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    """A readable rendering of the same facts; the JSON is the authority."""
    coverage = report["coverage"]
    replay = report["replay"]
    generated = report["generated_from"]
    capture = generated["capture"]
    parameters = report["parameters"]
    lines = [
        f"# Pilot readiness: session {report['session_date']}",
        "",
        f"Schema `{report['schema_version']}`; report hash `{report['report_hash']}`.",
        f"Label: {report['label'] or '(none)'}.",
        "",
        "**Verdict: "
        + (
            "usable for an intraday pilot replay"
            if report["usable_for_intraday_pilot"]
            else "NOT usable for an intraday pilot"
        )
        + f".** Usable decisions {report['usable_decisions']}, blocked "
        f"{report['blocked_decisions']} of {replay['expected_decisions']}.",
        "",
    ]
    if report["blocking_reasons"]:
        lines.append(
            "Blocking reasons: "
            + ", ".join(f"`{r}`" for r in report["blocking_reasons"])
            + "."
        )
        lines.append("")
    if report["partial_requirements"]:
        lines.append(
            "Partially met (decisions touching the gaps are refused frame by "
            "frame): "
            + ", ".join(f"`{r}`" for r in report["partial_requirements"])
            + "."
        )
        lines.append("")
    if parameters["diagnostic"]:
        lines.append(
            "> Diagnostic run: parameters differ from the research default "
            "(clock tolerance "
            f"{parameters['receipt_clock_tolerance_ms']} ms, contract default "
            f"{parameters['contract_is_research_default']}). Its counts illustrate the "
            "pipeline; they are not the research result."
        )
        lines.append("")
    lines.extend(
        [
            "## Source",
            "",
            f"- Capture session `{capture['session_id']}` (parser `{capture['parser_version']}`, "
            f"intent `{capture['intent_schema_version']}`), valuation instant `{capture['captured_at']}`.",
            f"- Manifest SHA-256 `{capture['manifest_sha256']}`; run intent SHA-256 `{capture['run_intent_sha256']}`.",
            f"- Normalizer `{coverage['normalizer']}`; events SHA-256 `{generated['events_sha256']}`; "
            f"plan SHA-256 `{generated['plan_sha256']}`; replay report hash `{generated['replay_report_hash']}`.",
            f"- Origins {report['observed_source_origins']}; synthetic only: {report['synthetic_only']}.",
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
    lines.extend(["", "## Native payloads and receipts", ""])
    rows = []
    for endpoint, entry in coverage["endpoints"].items():
        receipt = entry["receipt"]
        rows.append(
            [
                f"`{endpoint}`",
                entry["kind"],
                str(entry["rows"]),
                str(entry["emitted"]),
                _pairs(entry["excluded"]) if entry["excluded"] else "-",
                receipt["available_at"] or f"refused: {receipt['refusal']}",
                "+".join(receipt["evidence"]) or "-",
                f"`{receipt['payload_sha256'][:16]}…`",
            ]
        )
    lines.extend(
        _table(
            [
                "Endpoint",
                "Kind",
                "Rows",
                "Emitted",
                "Excluded",
                "Available at (UTC)",
                "Evidence",
                "Payload",
            ],
            rows,
        )
    )
    lines.extend(
        [
            "",
            f"Vendor timestamps read as {coverage['vendor_timestamp_policy']['assumed_timezone']}; "
            f"rows whose vendor time postdates the receipt: "
            + ", ".join(
                f"{e['kind']} {e['vendor_clock_lead']['rows_after_receipt']} (max "
                f"{e['vendor_clock_lead']['max_ms']} ms)"
                for e in coverage["endpoints"].values()
                if e["vendor_clock_lead"]["rows_after_receipt"]
            )
            + ".",
            "",
            "## Identities and open interest",
            "",
            f"- Listed {coverage['identities']['listed']}; quoted {coverage['identities']['quoted']}; "
            f"with usable Greeks {coverage['identities']['greeked']}; with open interest "
            f"{coverage['identities']['with_open_interest']}; without open interest "
            f"{coverage['identities']['without_open_interest']} (unavailable, never zero).",
            f"- Open-interest as-of rule: {coverage['open_interest']['settlement_rule']['description']} "
            f"({coverage['open_interest']['as_of_basis']}); rows by as-of "
            f"{_pairs(coverage['open_interest']['rows_by_as_of'])}.",
            f"- Model `{coverage['model_evidence']['model_id']}` fixed at "
            f"{coverage['model_evidence']['model_fixed_at']}; IV source "
            f"{coverage['model_evidence']['iv_source']}, price basis "
            f"{coverage['model_evidence']['iv_price_basis']}.",
            "",
            "## Replay",
            "",
            f"- Contract hash `{parameters['contract_hash']}`; grid {replay['grid']['first']} → "
            f"{replay['grid']['last']} ({replay['expected_decisions']} decisions).",
            f"- Decisions with an inventory available: {replay['decisions_with_inventory']}.",
            f"- Blockers by decisions affected: {_pairs(replay['blocker_decision_counts'])}.",
            "- Blockers by contract-decisions: "
            f"{_pairs(replay['blocker_contract_decision_counts'])}.",
        ]
    )
    best = replay["best_decision"]
    if best is not None and best["expected_contracts"] == 0:
        lines.append("- No grid decision had an inventory and contracts in scope.")
    elif best is not None:
        lines.append(
            f"- Best decision {best['decision_at']}: {best['passing_contracts']} passing, "
            f"{best['blocked_contracts']} blocked of {best['expected_contracts']} in scope "
            f"({best['excluded_inventory_contracts']} outside the fixed universe); "
            f"decision hash `{best['decision_hash']}`."
        )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {text}" for text in report["limitations"])
    lines.append("")
    return "\n".join(lines)
