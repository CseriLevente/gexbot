"""A multi-session pilot summary from per-session readiness reports.

Reads several readiness reports -- assembled collection sessions
(``research-pilot-readiness/2.1.36``) or single-capture normalizations
(``research-pilot-readiness/2.1.35``) -- and states, per session and overall:
usable decisions, coverage, open-interest gaps, stale or skewed inputs, vendor
clock leads, request failures, overruns and restarts. It adds nothing that the
reports do not carry. Synthetic and recorded sessions are never summarised
together, and a summary with any synthetic session is itself synthetic.

The option side is judged separately from the pilot as a whole: without
recorded futures data the whole stays unusable, and nothing here fabricates
futures. Every trust flag stays false.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any

from src.domain.canonical import canonical_payload
from src.domain.digests import digest_of
from src.replay.pilot_readiness import MISSING, READINESS_SCHEMA, _pairs, _table
from src.replay.session_readiness import OPTION_SIDE, SESSION_READINESS_SCHEMA

SUMMARY_SCHEMA = "research-pilot-summary/2.1.36"
SUPPORTED = frozenset({READINESS_SCHEMA, SESSION_READINESS_SCHEMA})
DEFAULT_MINIMUM_SESSIONS = 5


class PilotSummaryError(ValueError):
    """Reports that cannot be summarised honestly together."""


def _option_side(report: dict[str, Any]) -> tuple[bool, list[str]]:
    """The option-side verdict; recomputed for 2.1.35 reports that lack it."""
    if "option_side_usable" in report:
        return bool(report["option_side_usable"]), list(
            report["option_side_blocking_reasons"]
        )
    status = {r["requirement"]: r["status"] for r in report["requirements"]}
    reasons = [name for name in OPTION_SIDE if status.get(name) == MISSING]
    if report["usable_decisions"] == 0:
        reasons.append("NO_PASSING_DECISIONS")
    if report["parameters"]["diagnostic"]:
        reasons.append("DIAGNOSTIC_PARAMETERS_NOT_RESEARCH_DEFAULT")
    return not reasons, reasons


def _session_row(name: str, sha256: str, report: dict[str, Any]) -> dict[str, Any]:
    schema = report["schema_version"]
    if schema not in SUPPORTED:
        raise PilotSummaryError(f"{name}: unsupported readiness schema {schema!r}")
    usable, reasons = _option_side(report)
    replay = report["replay"]
    row: dict[str, Any] = {
        "source": name,
        "sha256": sha256,
        "schema_version": schema,
        "kind": (
            "COLLECTION_SESSION"
            if schema == SESSION_READINESS_SCHEMA
            else "SINGLE_CAPTURE"
        ),
        "session_date": report["session_date"],
        "label": report.get("label"),
        "origins": list(report["observed_source_origins"]),
        "synthetic_only": bool(report["synthetic_only"]),
        "diagnostic": bool(report["parameters"]["diagnostic"]),
        "option_side_usable": usable,
        "option_side_blocking_reasons": reasons,
        "usable_for_intraday_pilot": bool(report["usable_for_intraday_pilot"]),
        "blocking_reasons": list(report["blocking_reasons"]),
        "partial_requirements": list(report.get("partial_requirements", [])),
        "usable_decisions": int(report["usable_decisions"]),
        "expected_decisions": int(replay["expected_decisions"]),
        "decisions_with_inventory": int(replay["decisions_with_inventory"]),
        "blocker_decision_counts": dict(replay["blocker_decision_counts"]),
        "report_hash": report["report_hash"],
    }
    blockers = replay["blocker_decision_counts"]
    #: 2.1.35 reports carry blocker counts per reason, not per decision; the
    #: derived stale/skew/gap counts are then lower bounds and say so.
    row["counts_basis"] = (
        "EXACT" if "decisions_with_stale_inputs" in replay else "LOWER_BOUND"
    )
    row["decisions_with_stale_inputs"] = replay.get(
        "decisions_with_stale_inputs",
        max((v for k, v in blockers.items() if k.startswith("STALE_")), default=0),
    )
    row["decisions_with_skewed_inputs"] = replay.get(
        "decisions_with_skewed_inputs", blockers.get("MARKET_INPUTS_MISALIGNED", 0)
    )
    row["decisions_with_open_interest_gaps"] = replay.get(
        "decisions_with_open_interest_gaps",
        max(
            blockers.get("OI_UNAVAILABLE", 0),
            blockers.get("OI_NOT_PRIOR_COMPLETED_SESSION", 0),
        ),
    )
    if schema == SESSION_READINESS_SCHEMA:
        session = report["session"]
        row["coverage"] = {
            "slots_planned": session["slots_planned"],
            "cycles_executed": session["cycles_executed"],
            "cycles_assembled": session["cycles_assembled"],
            "slots_by_status": session["slots_by_status"],
            "cycles_overrunning_a_boundary": session["cycles_overrunning_a_boundary"],
            "restarts": session["restarts"],
            "requests_issued": session["requests_issued"],
            "request_budget": session["request_budget"]["requests"],
            "endpoint_failures": session["endpoint_failures"],
            "ended": session["ended"],
        }
        row["identities"] = session["identities"]
        row["vendor_clock_lead"] = session["vendor_clock_lead"]
        row["ambiguity_after_known_state"] = session["ambiguity_after_known_state"]
        row["structure_verified"] = session["structure_verified"]
    else:
        coverage = report["coverage"]
        endpoints = coverage["endpoints"]
        row["coverage"] = {
            "slots_planned": 1,
            "cycles_executed": 1,
            "cycles_assembled": 1,
            "slots_by_status": {"EXECUTED": 1},
            "cycles_overrunning_a_boundary": 0,
            "restarts": 0,
            "requests_issued": len(endpoints),
            "request_budget": len(endpoints),
            "endpoint_failures": {
                endpoint: 1
                for endpoint, entry in endpoints.items()
                if entry["receipt"]["refusal"] is not None
            },
            "ended": {"status": "SINGLE_CAPTURE", "reason": ""},
        }
        row["identities"] = coverage["identities"]
        row["vendor_clock_lead"] = {
            "rows_after_receipt": sum(
                e["vendor_clock_lead"]["rows_after_receipt"] for e in endpoints.values()
            ),
            "max_ms": max(
                (e["vendor_clock_lead"]["max_ms"] for e in endpoints.values()),
                default=0,
            ),
        }
        row["ambiguity_after_known_state"] = 0
        row["structure_verified"] = True
    return row


def summarize_pilot(
    reports: list[tuple[str, bytes]],
    *,
    minimum_sessions: int = DEFAULT_MINIMUM_SESSIONS,
    label: str | None = None,
) -> dict[str, Any]:
    """Summarise ``(name, raw report bytes)`` pairs. Deterministic; refuses mixes."""
    if not reports:
        raise PilotSummaryError("at least one readiness report is required")
    if type(minimum_sessions) is not int or minimum_sessions < 1:
        raise PilotSummaryError("minimum_sessions must be a positive integer")
    rows = []
    for name, raw in reports:
        try:
            report = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise PilotSummaryError(f"{name}: not a JSON readiness report") from error
        if not isinstance(report, dict):
            raise PilotSummaryError(f"{name}: not a readiness report")
        try:
            rows.append(_session_row(name, hashlib.sha256(raw).hexdigest(), report))
        except (KeyError, TypeError) as error:
            raise PilotSummaryError(f"{name}: incomplete readiness report") from error
    rows.sort(key=lambda r: (r["session_date"], r["source"]))
    dates = [r["session_date"] for r in rows]
    if len(set(dates)) != len(dates):
        raise PilotSummaryError(
            "two reports describe the same session date; summarise one report per session"
        )
    synthetic = {r["synthetic_only"] for r in rows}
    if len(synthetic) > 1:
        raise PilotSummaryError(
            "synthetic and recorded sessions cannot be summarised together"
        )
    synthetic_only = synthetic.pop()
    option_usable = [r for r in rows if r["option_side_usable"]]
    usable = sum(r["usable_decisions"] for r in rows)
    expected = sum(r["expected_decisions"] for r in rows)
    reasons = []
    if len(option_usable) < minimum_sessions:
        reasons.append("FEWER_OPTION_SIDE_USABLE_SESSIONS_THAN_MINIMUM")
    if any(r["diagnostic"] for r in rows):
        reasons.append("DIAGNOSTIC_SESSION_INCLUDED")
    if synthetic_only:
        reasons.append("SYNTHETIC_SESSIONS_ONLY")
    pilot_reasons = list(reasons)
    if not all(r["usable_for_intraday_pilot"] for r in rows):
        pilot_reasons.append("SESSIONS_NOT_USABLE_FOR_THE_WHOLE_PILOT")
    payload = {
        "schema_version": SUMMARY_SCHEMA,
        "label": label,
        "sessions": rows,
        "totals": {
            "sessions": len(rows),
            "distinct_session_dates": len(set(dates)),
            "first_session": dates[0],
            "last_session": dates[-1],
            "option_side_usable_sessions": len(option_usable),
            "usable_decisions": usable,
            "expected_decisions": expected,
            "decisions_with_stale_inputs": sum(
                r["decisions_with_stale_inputs"] for r in rows
            ),
            "decisions_with_skewed_inputs": sum(
                r["decisions_with_skewed_inputs"] for r in rows
            ),
            "decisions_with_open_interest_gaps": sum(
                r["decisions_with_open_interest_gaps"] for r in rows
            ),
            "identities_without_open_interest": sum(
                r["identities"]["without_open_interest"] for r in rows
            ),
            "vendor_clock_lead_rows": sum(
                r["vendor_clock_lead"]["rows_after_receipt"] for r in rows
            ),
            "vendor_clock_lead_max_ms": max(
                r["vendor_clock_lead"]["max_ms"] for r in rows
            ),
            "cycles_executed": sum(r["coverage"]["cycles_executed"] for r in rows),
            "slots_planned": sum(r["coverage"]["slots_planned"] for r in rows),
            "cycles_overrunning_a_boundary": sum(
                r["coverage"]["cycles_overrunning_a_boundary"] for r in rows
            ),
            "restarts": sum(r["coverage"]["restarts"] for r in rows),
            "request_failures": sum(
                sum(r["coverage"]["endpoint_failures"].values()) for r in rows
            ),
            "ambiguity_after_known_state": sum(
                r["ambiguity_after_known_state"] for r in rows
            ),
        },
        "minimum_sessions": minimum_sessions,
        "option_side_pilot_ready": not reasons,
        "option_side_blocking_reasons": reasons,
        "usable_for_intraday_pilot": not pilot_reasons,
        "blocking_reasons": pilot_reasons,
        "observed_source_origins": sorted({o for r in rows for o in r["origins"]}),
        "synthetic_only": synthetic_only,
        "authenticity_verified": False,
        "normalization_verified": False,
        "ready_for_backtest": False,
        "trusted_for_gex": False,
        "gex_computed": False,
        "strategy_tested": False,
        "pnl_computed": False,
        "orders_placed": 0,
        "limitations": [
            "The summary restates per-session readiness reports; it verifies their bytes, not the sessions behind them.",
            "Usable decisions are declared-contract checks on recorded inputs; they are not trades, signals, GEX values or profitability.",
            "A synthetic session is a pipeline demonstration and never trading evidence; a summary containing one is synthetic.",
            "The option side can be ready while the pilot as a whole is not: no futures data is collected and none is fabricated.",
        ],
    }
    return {**payload, "report_hash": digest_of(canonical_payload(payload))}


def read_reports(paths: list[pathlib.Path]) -> list[tuple[str, bytes]]:
    return [(path.name, path.read_bytes()) for path in paths]


def render_summary_markdown(summary: dict[str, Any]) -> str:
    totals = summary["totals"]
    lines = [
        "# Intraday pilot summary",
        "",
        f"Schema `{summary['schema_version']}`; report hash `{summary['report_hash']}`.",
        f"Label: {summary['label'] or '(none)'}.",
        "",
        "**Option side: "
        + (
            f"ready across {totals['option_side_usable_sessions']} sessions"
            if summary["option_side_pilot_ready"]
            else "NOT ready"
        )
        + f".** {totals['option_side_usable_sessions']} of {totals['sessions']} sessions "
        f"usable on the option side (minimum {summary['minimum_sessions']}); usable "
        f"decisions {totals['usable_decisions']} of {totals['expected_decisions']}.",
        "",
        "**Pilot as a whole: "
        + (
            "usable for an intraday pilot"
            if summary["usable_for_intraday_pilot"]
            else "NOT usable for an intraday pilot"
        )
        + ".**",
        "",
        f"Origins {summary['observed_source_origins']}; synthetic only: "
        f"{summary['synthetic_only']}.",
        "",
    ]
    if summary["option_side_blocking_reasons"]:
        lines.append(
            "Option-side blocking reasons: "
            + ", ".join(f"`{r}`" for r in summary["option_side_blocking_reasons"])
            + "."
        )
        lines.append("")
    if summary["blocking_reasons"]:
        lines.append(
            "Pilot blocking reasons: "
            + ", ".join(f"`{r}`" for r in summary["blocking_reasons"])
            + "."
        )
        lines.append("")
    lines.extend(["## Sessions", ""])
    lines.extend(
        _table(
            [
                "Session",
                "Kind",
                "Option side",
                "Usable / expected",
                "Cycles / slots",
                "Missed",
                "Failures",
                "Overruns",
                "Restarts",
                "OI gaps",
                "Stale",
                "Skewed",
                "Clock leads",
            ],
            [
                [
                    r["session_date"],
                    r["kind"],
                    "usable" if r["option_side_usable"] else "not usable",
                    f"{r['usable_decisions']} / {r['expected_decisions']}",
                    f"{r['coverage']['cycles_executed']} / {r['coverage']['slots_planned']}",
                    str(
                        sum(
                            v
                            for k, v in r["coverage"]["slots_by_status"].items()
                            if k.startswith("MISSED")
                        )
                    ),
                    _pairs(r["coverage"]["endpoint_failures"]),
                    str(r["coverage"]["cycles_overrunning_a_boundary"]),
                    str(r["coverage"]["restarts"]),
                    f"{r['identities']['without_open_interest']} ids / "
                    f"{r['decisions_with_open_interest_gaps']} decisions",
                    str(r["decisions_with_stale_inputs"]),
                    str(r["decisions_with_skewed_inputs"]),
                    f"{r['vendor_clock_lead']['rows_after_receipt']} rows",
                ]
                for r in summary["sessions"]
            ],
        )
    )
    lines.extend(["", "## Per-session blocking reasons", ""])
    for r in summary["sessions"]:
        lines.append(
            f"- {r['session_date']} (`{r['source']}`, report hash `{r['report_hash'][:16]}…`): "
            f"option side {', '.join(r['option_side_blocking_reasons']) or 'no blocker'}; "
            f"pilot {', '.join(r['blocking_reasons']) or 'no blocker'}."
        )
    lines.extend(["", "## Totals", ""])
    lines.extend(
        [
            f"- Cycles executed {totals['cycles_executed']} of {totals['slots_planned']} slots; "
            f"overruns {totals['cycles_overrunning_a_boundary']}; restarts {totals['restarts']}; "
            f"request failures {totals['request_failures']}.",
            f"- Decisions with stale inputs {totals['decisions_with_stale_inputs']}; with skewed "
            f"inputs {totals['decisions_with_skewed_inputs']}; with open-interest gaps "
            f"{totals['decisions_with_open_interest_gaps']}; identities without open interest "
            f"{totals['identities_without_open_interest']}.",
            f"- Vendor clock leads {totals['vendor_clock_lead_rows']} rows (max "
            f"{totals['vendor_clock_lead_max_ms']} ms); ambiguous observations after a known "
            f"state {totals['ambiguity_after_known_state']}.",
            "",
            "## Limitations",
            "",
        ]
    )
    lines.extend(f"- {text}" for text in summary["limitations"])
    lines.append("")
    return "\n".join(lines)
