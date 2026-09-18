"""A multi-session pilot summary from per-session readiness reports.

Reads several readiness reports -- assembled collection sessions
(``research-pilot-readiness/2.1.36-r3`` and the earlier ``/2.1.36``) or
single-capture normalizations (``research-pilot-readiness/2.1.35``) -- and
states, per session and overall: usable decisions, coverage, open-interest
gaps, stale or skewed inputs, vendor clock leads, request activity and
failures, overruns and restarts. It adds nothing that the reports do not
carry. Synthetic and recorded sessions are never summarised together, and a
summary with any synthetic session is itself synthetic.

r3: every report is validated before anything is read from it. Its embedded
semantic ``report_hash`` is recomputed over its contents under the schema's
own rule, its decision counts must be non-negative integers that agree with
each other (``usable + blocked == expected``, top level equal to the nested
replay block), its origins must be the known ones and its verdict fields must
be the booleans its blocking reasons imply. A report that fails any of this
is refused by name. A matching hash is an integrity check on the report's
bytes-as-written, not proof that the vendor data behind it is authentic.

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
from src.replay.event_store import ORIGINS
from src.replay.pilot_readiness import MISSING, READINESS_SCHEMA, _pairs, _table
from src.replay.session_readiness import (
    OPTION_SIDE,
    SESSION_READINESS_SCHEMA,
    SESSION_READINESS_SCHEMA_2_1_36,
)

SUMMARY_SCHEMA = "research-pilot-summary/2.1.36-r3"
SESSION_SCHEMAS = frozenset({SESSION_READINESS_SCHEMA_2_1_36, SESSION_READINESS_SCHEMA})
SUPPORTED = frozenset({READINESS_SCHEMA, *SESSION_SCHEMAS})
DEFAULT_MINIMUM_SESSIONS = 5
#: Flags every readiness report of every supported schema writes as false. A
#: report that claims otherwise was not written by this pipeline.
TRUST_FLAGS = (
    "authenticity_verified",
    "normalization_verified",
    "ready_for_backtest",
    "trusted_for_gex",
    "gex_computed",
    "strategy_tested",
    "pnl_computed",
)
#: Request accounting of a 2.1.36 (r2) session report: the collector summed
#: the scheduled scope of every executed slot and called it "issued". The
#: summary restates it under that name and basis rather than as activity.
SCHEDULED_SCOPE_BASIS = "SCHEDULED_SCOPE_OF_EXECUTED_SLOTS"
ATTEMPT_EVIDENCE_BASIS = "CYCLE_REPORTS_AND_ATTEMPT_LOGS"
SINGLE_CAPTURE_BASIS = "SINGLE_CAPTURE_RECEIPTS"


class PilotSummaryError(ValueError):
    """Reports that cannot be summarised honestly together."""


def semantic_report_hash(report: dict[str, Any]) -> str:
    """The hash every supported readiness schema embeds: the canonical payload
    of everything but ``report_hash`` itself."""
    return digest_of(
        canonical_payload({k: v for k, v in report.items() if k != "report_hash"})
    )


def _count(name: str, where: str, value: Any) -> int:
    if type(value) is not int or value < 0:
        raise PilotSummaryError(
            f"{name}: {where} must be a non-negative integer, got {value!r}"
        )
    return value


def _flag(name: str, where: str, value: Any) -> bool:
    if type(value) is not bool:
        raise PilotSummaryError(f"{name}: {where} must be a boolean, got {value!r}")
    return value


def _names(name: str, where: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PilotSummaryError(f"{name}: {where} must be a list of names")
    return list(value)


def _counts(name: str, where: str, value: Any, *, at_most: int) -> dict[str, int]:
    if not isinstance(value, dict):
        raise PilotSummaryError(f"{name}: {where} must map reasons to counts")
    counted = {str(k): _count(name, f"{where}[{k}]", v) for k, v in value.items()}
    over = sorted(k for k, v in counted.items() if v > at_most)
    if over:
        raise PilotSummaryError(
            f"{name}: {where} counts more decisions than exist for {over}"
        )
    return counted


def _required(name: str, mapping: Any, where: str, keys: tuple[str, ...]) -> None:
    if not isinstance(mapping, dict):
        raise PilotSummaryError(f"{name}: {where} is not a mapping")
    missing = [key for key in keys if key not in mapping]
    if missing:
        raise PilotSummaryError(f"{name}: {where} lacks {missing}")


def validate_readiness_report(name: str, report: dict[str, Any]) -> str:
    """Refuse a readiness report whose contents cannot be trusted as written.

    Returns the report's schema. Checks, in order: a supported schema; the
    embedded semantic ``report_hash`` against the contents; every decision
    count a non-negative integer; the top-level counts equal to the nested
    replay counts and ``passing + blocked == expected``; derived per-decision
    counts no larger than the decisions they are drawn from; origins among
    the known ones with ``synthetic_only`` agreeing; verdicts booleans that
    agree with their blocking reasons; every trust flag false. Nothing here
    authenticates vendor data: a matching hash means the report is the one
    its writer produced, not that the writer's inputs were genuine.
    """
    schema = report.get("schema_version")
    if schema not in SUPPORTED:
        raise PilotSummaryError(f"{name}: unsupported readiness schema {schema!r}")
    assert isinstance(schema, str)
    claimed = report.get("report_hash")
    if not isinstance(claimed, str) or not claimed:
        raise PilotSummaryError(f"{name}: the report carries no report_hash")
    actual = semantic_report_hash(report)
    if actual != claimed:
        raise PilotSummaryError(
            f"{name}: report_hash {claimed[:16]}... does not match the report's "
            f"contents ({actual[:16]}...); the report was changed after it was "
            "written, or is not a readiness report"
        )
    _required(
        name,
        report,
        "report",
        (
            "session_date",
            "replay",
            "requirements",
            "parameters",
            "usable_decisions",
            "blocked_decisions",
            "usable_for_intraday_pilot",
            "blocking_reasons",
            "observed_source_origins",
            "synthetic_only",
            "orders_placed",
            *TRUST_FLAGS,
        ),
    )
    replay = report["replay"]
    _required(
        name,
        replay,
        "replay",
        (
            "expected_decisions",
            "passing_decisions",
            "blocked_decisions",
            "decisions_with_inventory",
            "blocker_decision_counts",
        ),
    )
    usable = _count(name, "usable_decisions", report["usable_decisions"])
    blocked = _count(name, "blocked_decisions", report["blocked_decisions"])
    expected = _count(name, "replay.expected_decisions", replay["expected_decisions"])
    passing = _count(name, "replay.passing_decisions", replay["passing_decisions"])
    replay_blocked = _count(
        name, "replay.blocked_decisions", replay["blocked_decisions"]
    )
    if usable != passing or blocked != replay_blocked:
        raise PilotSummaryError(
            f"{name}: top-level decision counts (usable {usable}, blocked {blocked}) "
            f"differ from the replay block (passing {passing}, blocked {replay_blocked})"
        )
    if passing + blocked != expected:
        raise PilotSummaryError(
            f"{name}: passing {passing} + blocked {blocked} decisions != expected "
            f"{expected}"
        )
    if (
        _count(
            name, "replay.decisions_with_inventory", replay["decisions_with_inventory"]
        )
        > expected
    ):
        raise PilotSummaryError(
            f"{name}: replay.decisions_with_inventory exceeds the {expected} expected"
        )
    _counts(
        name,
        "replay.blocker_decision_counts",
        replay["blocker_decision_counts"],
        at_most=blocked,
    )
    if "blocker_contract_decision_counts" in replay:
        # Contract-decision pairs, not decisions: bounded below, not above.
        for key, value in dict(replay["blocker_contract_decision_counts"]).items():
            _count(name, f"replay.blocker_contract_decision_counts[{key}]", value)
    for key in (
        "decisions_with_stale_inputs",
        "decisions_with_skewed_inputs",
        "decisions_with_open_interest_gaps",
    ):
        if key in replay and _count(name, f"replay.{key}", replay[key]) > blocked:
            raise PilotSummaryError(
                f"{name}: replay.{key} exceeds the {blocked} blocked decisions"
            )
    origins = _names(name, "observed_source_origins", report["observed_source_origins"])
    unknown = sorted(set(origins) - ORIGINS)
    if unknown:
        raise PilotSummaryError(f"{name}: unknown source origins {unknown}")
    synthetic_only = _flag(name, "synthetic_only", report["synthetic_only"])
    if synthetic_only != all(origin == "SYNTHETIC" for origin in origins):
        raise PilotSummaryError(
            f"{name}: synthetic_only {synthetic_only} disagrees with origins {origins}"
        )
    reasons = _names(name, "blocking_reasons", report["blocking_reasons"])
    if _flag(
        name, "usable_for_intraday_pilot", report["usable_for_intraday_pilot"]
    ) != (not reasons):
        raise PilotSummaryError(
            f"{name}: usable_for_intraday_pilot disagrees with blocking_reasons {reasons}"
        )
    if usable == 0 and "NO_PASSING_DECISIONS" not in reasons:
        raise PilotSummaryError(
            f"{name}: no usable decision, yet NO_PASSING_DECISIONS is not a blocking reason"
        )
    if "option_side_usable" in report or "option_side_blocking_reasons" in report:
        _required(
            name,
            report,
            "report",
            ("option_side_usable", "option_side_blocking_reasons"),
        )
        option_reasons = _names(
            name, "option_side_blocking_reasons", report["option_side_blocking_reasons"]
        )
        if _flag(name, "option_side_usable", report["option_side_usable"]) != (
            not option_reasons
        ):
            raise PilotSummaryError(
                f"{name}: option_side_usable disagrees with option_side_blocking_reasons"
            )
    _required(name, report["parameters"], "parameters", ("diagnostic",))
    _flag(name, "parameters.diagnostic", report["parameters"]["diagnostic"])
    for flag in TRUST_FLAGS:
        if _flag(name, flag, report[flag]):
            raise PilotSummaryError(
                f"{name}: {flag} is true; no readiness report of this pipeline claims it"
            )
    if _count(name, "orders_placed", report["orders_placed"]) != 0:
        raise PilotSummaryError(f"{name}: orders_placed is not 0")
    if schema in SESSION_SCHEMAS:
        _validate_session_block(name, schema, report)
    return schema


def _validate_session_block(name: str, schema: str, report: dict[str, Any]) -> None:
    _required(
        name,
        report.get("session"),
        "session",
        (
            "slots_planned",
            "slots_by_status",
            "cycles_executed",
            "cycles_assembled",
            "cycles_overrunning_a_boundary",
            "restarts",
            "request_budget",
            "endpoint_failures",
            "identities",
            "vendor_clock_lead",
            "ambiguity_after_known_state",
            "structure_verified",
        ),
    )
    session = report["session"]
    planned = _count(name, "session.slots_planned", session["slots_planned"])
    executed = _count(name, "session.cycles_executed", session["cycles_executed"])
    assembled = _count(name, "session.cycles_assembled", session["cycles_assembled"])
    by_status = _counts(
        name, "session.slots_by_status", session["slots_by_status"], at_most=planned
    )
    if sum(by_status.values()) > planned:
        raise PilotSummaryError(
            f"{name}: session.slots_by_status counts more slots than planned"
        )
    if by_status.get("EXECUTED", 0) != executed or assembled > executed:
        raise PilotSummaryError(
            f"{name}: session cycle counts disagree (executed {executed}, by status "
            f"{by_status.get('EXECUTED', 0)}, assembled {assembled})"
        )
    _count(
        name,
        "session.cycles_overrunning_a_boundary",
        session["cycles_overrunning_a_boundary"],
    )
    _count(name, "session.restarts", session["restarts"])
    _required(name, session["request_budget"], "session.request_budget", ("requests",))
    budget = _count(
        name, "session.request_budget.requests", session["request_budget"]["requests"]
    )
    _counts(
        name,
        "session.endpoint_failures",
        session["endpoint_failures"],
        at_most=executed,
    )
    _required(
        name, session["identities"], "session.identities", ("without_open_interest",)
    )
    _count(
        name,
        "session.identities.without_open_interest",
        session["identities"]["without_open_interest"],
    )
    _required(
        name,
        session["vendor_clock_lead"],
        "session.vendor_clock_lead",
        ("rows_after_receipt", "max_ms"),
    )
    _count(
        name,
        "session.vendor_clock_lead.rows_after_receipt",
        session["vendor_clock_lead"]["rows_after_receipt"],
    )
    _count(
        name, "session.vendor_clock_lead.max_ms", session["vendor_clock_lead"]["max_ms"]
    )
    _count(
        name,
        "session.ambiguity_after_known_state",
        session["ambiguity_after_known_state"],
    )
    _flag(name, "session.structure_verified", session["structure_verified"])
    if schema == SESSION_READINESS_SCHEMA_2_1_36:
        if (
            _count(name, "session.requests_issued", session.get("requests_issued"))
            > budget
        ):
            raise PilotSummaryError(
                f"{name}: session.requests_issued exceeds the approved budget"
            )
        return
    _required(
        name,
        session.get("requests"),
        "session.requests",
        (
            "scheduled",
            "attempted",
            "http_attempts",
            "http_attempts_failed",
            "with_receipt",
            "acquired",
            "not_attempted",
            "without_receipt",
            "cycles_with_unverified_attempt_evidence",
        ),
    )
    requests = session["requests"]
    counted = {
        key: _count(name, f"session.requests.{key}", requests[key])
        for key in (
            "scheduled",
            "attempted",
            "http_attempts",
            "http_attempts_failed",
            "with_receipt",
            "acquired",
            "not_attempted",
            "without_receipt",
            "cycles_with_unverified_attempt_evidence",
        )
    }
    if counted["scheduled"] > budget:
        raise PilotSummaryError(
            f"{name}: session.requests.scheduled exceeds the approved budget"
        )
    if (
        counted["attempted"] > counted["scheduled"]
        or counted["acquired"] > counted["attempted"]
        or counted["with_receipt"] > counted["attempted"]
        or counted["http_attempts_failed"] > counted["http_attempts"]
        or counted["with_receipt"] > counted["http_attempts"]
        # A slot the one-shot refused before it ran has a scheduled scope but
        # no attempted / not-attempted split (``cycles_without_a_report``), so
        # the split is bounded by the scope, not equal to it.
        or counted["attempted"] + counted["not_attempted"] > counted["scheduled"]
        or counted["with_receipt"] + counted["without_receipt"] != counted["attempted"]
        or counted["cycles_with_unverified_attempt_evidence"] > executed
    ):
        raise PilotSummaryError(
            f"{name}: session.requests accounting is not self-consistent"
        )


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


def _requests_row(schema: str, report: dict[str, Any]) -> dict[str, Any]:
    """Request activity for one row, with the basis the report supports.

    An r3 session report carries activity read from each cycle's report and
    attempt log. A 2.1.36 session report carries only the summed scheduled
    scope, restated as ``scheduled`` with ``attempted`` and ``http_attempts``
    unknown. A single capture's receipts say per endpoint whether it answered.
    """
    if schema == SESSION_READINESS_SCHEMA:
        requests = report["session"]["requests"]
        return {
            "basis": ATTEMPT_EVIDENCE_BASIS,
            "scheduled": requests["scheduled"],
            "attempted": requests["attempted"],
            "http_attempts": requests["http_attempts"],
            "http_attempts_failed": requests["http_attempts_failed"],
            "with_receipt": requests["with_receipt"],
            "acquired": requests["acquired"],
            "not_attempted": requests["not_attempted"],
            "without_receipt": requests["without_receipt"],
            "cycles_with_unverified_attempt_evidence": requests[
                "cycles_with_unverified_attempt_evidence"
            ],
            "operator_cancelled_cycles": list(
                requests.get("operator_cancelled_cycles", [])
            ),
            "budget": report["session"]["request_budget"]["requests"],
        }
    if schema == SESSION_READINESS_SCHEMA_2_1_36:
        return {
            "basis": SCHEDULED_SCOPE_BASIS,
            "scheduled": report["session"]["requests_issued"],
            "attempted": None,
            "http_attempts": None,
            "http_attempts_failed": None,
            "with_receipt": None,
            "acquired": None,
            "not_attempted": None,
            "without_receipt": None,
            "cycles_with_unverified_attempt_evidence": None,
            "operator_cancelled_cycles": [],
            "budget": report["session"]["request_budget"]["requests"],
        }
    endpoints = report["coverage"]["endpoints"]
    answered = sum(1 for e in endpoints.values() if e["receipt"]["refusal"] is None)
    return {
        "basis": SINGLE_CAPTURE_BASIS,
        "scheduled": len(endpoints),
        "attempted": len(endpoints),
        "http_attempts": None,
        "http_attempts_failed": None,
        "with_receipt": answered,
        "acquired": answered,
        "not_attempted": 0,
        "without_receipt": 0,
        "cycles_with_unverified_attempt_evidence": None,
        "operator_cancelled_cycles": [],
        "budget": len(endpoints),
    }


def _session_row(name: str, sha256: str, report: dict[str, Any]) -> dict[str, Any]:
    schema = validate_readiness_report(name, report)
    usable, reasons = _option_side(report)
    replay = report["replay"]
    row: dict[str, Any] = {
        "source": name,
        "sha256": sha256,
        "schema_version": schema,
        "kind": "COLLECTION_SESSION" if schema in SESSION_SCHEMAS else "SINGLE_CAPTURE",
        "session_date": report["session_date"],
        "label": report.get("label"),
        "origins": list(report["observed_source_origins"]),
        "synthetic_only": report["synthetic_only"],
        "diagnostic": report["parameters"]["diagnostic"],
        "option_side_usable": usable,
        "option_side_blocking_reasons": reasons,
        "usable_for_intraday_pilot": report["usable_for_intraday_pilot"],
        "blocking_reasons": list(report["blocking_reasons"]),
        "partial_requirements": list(report.get("partial_requirements", [])),
        "usable_decisions": report["usable_decisions"],
        "expected_decisions": replay["expected_decisions"],
        "decisions_with_inventory": replay["decisions_with_inventory"],
        "blocker_decision_counts": dict(replay["blocker_decision_counts"]),
        "report_hash": report["report_hash"],
        "report_hash_verified": True,
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
    row["requests"] = _requests_row(schema, report)
    if schema in SESSION_SCHEMAS:
        session = report["session"]
        row["coverage"] = {
            "slots_planned": session["slots_planned"],
            "cycles_executed": session["cycles_executed"],
            "cycles_assembled": session["cycles_assembled"],
            "slots_by_status": session["slots_by_status"],
            "cycles_overrunning_a_boundary": session["cycles_overrunning_a_boundary"],
            "restarts": session["restarts"],
            "interruptions": list(session.get("interruptions", [])),
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
            "interruptions": [],
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


def _request_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Request activity across rows; a count stays unknown if any row lacks it."""
    totals: dict[str, Any] = {"basis_by_session": {}}
    for key in (
        "scheduled",
        "attempted",
        "http_attempts",
        "http_attempts_failed",
        "with_receipt",
        "acquired",
        "not_attempted",
        "without_receipt",
        "budget",
    ):
        values = [r["requests"][key] for r in rows]
        totals[key] = None if any(v is None for v in values) else sum(values)
    totals["sessions_without_attempt_evidence"] = sum(
        1 for r in rows if r["requests"]["basis"] != ATTEMPT_EVIDENCE_BASIS
    )
    totals["operator_cancelled_cycles"] = sum(
        len(r["requests"]["operator_cancelled_cycles"]) for r in rows
    )
    totals["basis_by_session"] = {
        r["session_date"]: r["requests"]["basis"] for r in rows
    }
    return totals


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
            "requests": _request_totals(rows),
            "interruptions": sum(len(r["coverage"]["interruptions"]) for r in rows),
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
            "The summary restates per-session readiness reports after recomputing each report's embedded semantic hash and checking its counts, origins and verdicts against each other; it does not verify the sessions, captures or vendor data behind them, and a matching hash is not vendor authenticity.",
            "Request activity is restated on the basis each report supports: r3 session reports count attempts from cycle reports and attempt logs; 2.1.36 session reports carry only the scheduled scope of executed slots, so their attempted and HTTP counts are unknown here; single captures carry per-endpoint receipts.",
            "Usable decisions are declared-contract checks on recorded inputs; they are not trades, signals, GEX values or profitability.",
            "A synthetic session is a pipeline demonstration and never trading evidence; a summary containing one is synthetic.",
            "The option side can be ready while the pilot as a whole is not: no futures data is collected and none is fabricated.",
        ],
    }
    return {**payload, "report_hash": digest_of(canonical_payload(payload))}


def read_reports(paths: list[pathlib.Path]) -> list[tuple[str, bytes]]:
    return [(path.name, path.read_bytes()) for path in paths]


def _unknown(value: Any) -> str:
    return "unknown" if value is None else str(value)


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
                "Requests sched / att / HTTP",
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
                    f"{r['requests']['scheduled']} / "
                    f"{_unknown(r['requests']['attempted'])} / "
                    f"{_unknown(r['requests']['http_attempts'])}",
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
            f"interruptions {totals['interruptions']}; request failures "
            f"{totals['request_failures']}.",
            f"- Requests: scheduled {_unknown(totals['requests']['scheduled'])} of budget "
            f"{_unknown(totals['requests']['budget'])}; attempted "
            f"{_unknown(totals['requests']['attempted'])}; HTTP attempts "
            f"{_unknown(totals['requests']['http_attempts'])} ("
            f"{_unknown(totals['requests']['http_attempts_failed'])} failed); acquired "
            f"{_unknown(totals['requests']['acquired'])}; begun without a receipt "
            f"{_unknown(totals['requests']['without_receipt'])}; operator-cancelled cycles "
            f"{totals['requests']['operator_cancelled_cycles']}; sessions without attempt "
            f"evidence {totals['requests']['sessions_without_attempt_evidence']} "
            f"(basis {_pairs(totals['requests']['basis_by_session'])}).",
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
