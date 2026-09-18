"""Collect one intraday session: the approved one-shot capture, once per slot.

Three modes, in the order an operator uses them:

``--show-schedule DATE``
    Print the slots, scopes, request budget and decision coverage a session on
    that date would have. No configuration is contacted beyond the retry
    setting; nothing is written.

dry run (the default)
    Plan a session for *today's* market session: the one-shot's own dry run
    (readiness, request plan, per-cycle approval) plus the schedule, the budget
    and the **session approval** that binds the session date, every slot's
    endpoint scope, the request budget, the policy and the destination. Prints
    both approvals; writes nothing; sends nothing.

``--execute-live --approve HASH``
    Run the session: one cycle per slot, each a full one-shot capture with its
    preflight, per-request authorisation, manifest, attempt log and
    verification. Refuses an approval taken for another day, another schedule,
    another budget or another destination. ``--resume`` reopens an interrupted
    session at the next future slot; slots that passed meanwhile are recorded
    as missed, never caught up.

Never passes the out-of-session or unsettled overrides. Computes no GEX and
places no orders; the repository has no broker adapter.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import UTC, date, datetime
from typing import Any

from src.ingest.clock import SystemClock
from src.ingest.schedule import (
    CollectionPolicy,
    CollectionSchedule,
    ScheduleError,
)
from src.ingest.session_collector import (
    SessionCollectionError,
    collect_session,
    plan_session,
)
from src.tools.capture_thetadata_once import CaptureRunError

DEFAULT_CONFIG = "config/thetadata_intraday.yaml"
DEFAULT_POLICY = "config/intraday_pilot.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.collect_intraday_session", description=__doc__
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="capture profile")
    parser.add_argument(
        "--policy",
        default=DEFAULT_POLICY,
        help="collection specification whose `collection` block is the policy",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="session directory to create (must not exist; with --resume, the "
        "session to reopen)",
    )
    parser.add_argument(
        "--show-schedule",
        default=None,
        metavar="DATE",
        help="print the schedule for this session date (YYYY-MM-DD) and exit",
    )
    parser.add_argument(
        "--execute-live",
        action="store_true",
        help="contact the vendor; without it this is a dry run",
    )
    parser.add_argument(
        "--approve",
        default="",
        help="the session approval printed by today's dry run",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reopen the session at --output after an interruption",
    )
    return parser


def _print_schedule(schedule: CollectionSchedule) -> None:
    payload = schedule.as_dict()
    slots = payload["slots"]
    print(f"session {payload['session_date']}  early close: {payload['early_close']}")
    print(f"open {payload['session_open']}  close {payload['session_close']}")
    print(f"preparation opens {payload['preparation_opens']} (dry run + approval)")
    print(
        f"slots {len(slots)}: first {slots[0]['label']} ET, last {slots[-1]['label']} ET, "
        f"cadence {payload['policy']['cadence_seconds']} s, start tolerance "
        f"{payload['policy']['start_tolerance_seconds']} s"
    )
    full = [s["label"] for s in slots if s["kind"] == "FULL"]
    print(
        f"FULL cycles (quotes, Greeks, index, open interest, listing) at "
        f"{', '.join(full)} ET; every other slot is MARKET (quotes, Greeks, index)"
    )
    budget = payload["request_budget"]
    print(
        f"request budget: {budget['requests']} requests in {budget['cycles']} cycles, "
        f"at most {budget['max_attempts_per_request']} attempts each "
        f"({budget['max_attempts']} attempts)"
    )
    coverage = payload["decision_coverage"]
    print(
        f"decision grid {coverage['first_decision']} -> {coverage['last_decision']}: "
        f"{coverage['decisions_preceded_by_a_slot']} of {coverage['decisions']} "
        f"decisions preceded by a slot; {coverage['slots_before_first_decision']} "
        f"slots before the first decision, {coverage['slots_after_last_decision']} after "
        "the last"
    )
    print(f"schedule fingerprint {payload['fingerprint']}")
    print(
        "policy: one cycle in flight; a slot that passes by more than the start "
        "tolerance is recorded as missed and never caught up"
    )


def _print_plan(
    plan: dict[str, Any], *, output: str, policy_path: str, config_path: str
) -> None:
    print(f"DRY RUN for session {plan['session_date']} planned at {plan['planned_at']}")
    if "schedule_refusal" in plan:
        print(f"schedule refused: {plan['schedule_refusal']}")
        return
    print(f"phase now: {plan['phase_now']}")
    print(
        f"capture readiness: {plan['capture_readiness']} (ready: {plan['capture_ready']})"
    )
    for blocker in plan.get("capture_blockers", []):
        print(f"  blocker: {blocker}")
    for refusal in plan.get("destination_refusals", []):
        print(f"  destination: {refusal}")
    print(f"expected capture origin: {plan['expected_capture_origin']}")
    schedule = CollectionSchedule.build(
        date.fromisoformat(plan["session_date"]),
        CollectionPolicy.from_specification(policy_path),
        max_attempts_per_request=plan["request_budget"]["max_attempts_per_request"],
    )
    _print_schedule(schedule)
    print(f"per-cycle approval (one-shot): {plan['cycle_approval']['approval_hash']}")
    approval = plan["session_approval"]
    print(
        "session approval binds: date "
        f"{approval['session_date']}, schedule {approval['schedule_fingerprint'][:16]}..., "
        f"budget {approval['request_budget']}, destination {approval['destination']}"
    )
    print(f"SESSION APPROVAL: {approval['approval_hash']}")
    print(
        "To collect, on the session day and before the open:\n"
        f"    python -m src.tools.collect_intraday_session --config {config_path} "
        f"--policy {policy_path} --output {output} "
        f"--execute-live --approve {approval['approval_hash']}"
    )
    print(
        "Nothing was sent and nothing was written. This command computes no "
        "trusted GEX and places no orders."
    )


def _console(entry: dict[str, Any]) -> None:
    if entry.get("event") == "SLOT":
        detail = ""
        if entry["status"] == "EXECUTED":
            requests = entry.get("requests") or {}
            detail = (
                f" {entry.get('run_state')} acquired {len(entry.get('acquired', []))}"
                f" missing {entry.get('missing') or '-'} in {entry.get('duration_seconds')} s;"
                f" requests scheduled {requests.get('scheduled')} attempted "
                f"{requests.get('attempted')} http {requests.get('http_attempts')}"
            )
            if entry.get("operator_cancelled"):
                detail += " OPERATOR_CANCELLED (partial capture preserved)"
            elif entry.get("stop_reason"):
                detail += f" stop {entry.get('stop_reason')}"
        elif entry["status"].startswith("MISSED"):
            detail = f" late by {entry.get('late_by_seconds')} s ({entry.get('cause')})"
        elif entry["status"] == "FAILED_TO_START":
            detail = f" {entry.get('error_message', '')[:120]}"
            if entry.get("operator_cancelled"):
                detail += " OPERATOR_CANCELLED before the first request"
        print(f"{entry['label']} {entry['status']}{detail}", flush=True)
    else:
        print(
            f"{entry['event']} {entry.get('at', '')} {entry.get('status', '')} "
            f"{entry.get('reason', '')}".rstrip(),
            flush=True,
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        policy = CollectionPolicy.from_specification(args.policy)
    except (ScheduleError, OSError, ValueError) as error:
        print(f"REFUSED: policy: {error}", file=sys.stderr)
        return 2
    if args.show_schedule:
        try:
            from src.config.schema import load_config

            loaded = load_config(args.config)
            schedule = CollectionSchedule.build(
                date.fromisoformat(args.show_schedule),
                policy,
                max_attempts_per_request=int(loaded.thetadata.max_retries) + 1,
            )
        except (ScheduleError, ValueError, OSError) as error:
            print(f"REFUSED: {error}", file=sys.stderr)
            return 2
        _print_schedule(schedule)
        return 0
    if not args.output:
        print("REFUSED: --output is required", file=sys.stderr)
        return 2
    if not args.execute_live:
        try:
            plan = plan_session(
                args.config,
                output=args.output,
                now=datetime.now(UTC),
                policy=policy,
            )
        except (CaptureRunError, ScheduleError, OSError, ValueError) as error:
            print(f"REFUSED: {error}", file=sys.stderr)
            return 2
        _print_plan(
            plan, output=args.output, policy_path=args.policy, config_path=args.config
        )
        return 0 if "schedule_refusal" not in plan else 2
    try:
        summary = collect_session(
            args.config,
            output=args.output,
            approved=args.approve,
            clock=SystemClock(),
            policy=policy,
            transport=None,
            resume=args.resume,
            on_entry=_console,
        )
    except SessionCollectionError as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt as interrupt:
        # Raised by the collector after it logged the stop, wrote the summary
        # and released the lock -- whether the operator struck during a wait,
        # between cycles or in the middle of a request (in which case the
        # cycle's partial capture and attempt evidence are preserved and the
        # slot is not retaken). Nothing continues without an explicit --resume.
        label = getattr(interrupt, "label", None)
        phase = str(getattr(interrupt, "phase", "UNKNOWN"))
        partial = bool(getattr(interrupt, "partial_capture", False))
        where = {
            "REQUEST": f"during cycle {label}; "
            + (
                f"its partial capture and attempt log under cycles/{label} are "
                "preserved"
                if partial
                else f"it was stopped before its first request (cycles/{label} "
                "holds only the bootstrap-failure report)"
            )
            + " and the slot is not retaken",
            "WAIT": f"while waiting for slot {label}",
            "BETWEEN_CYCLES": f"between cycles, before slot {label}",
        }.get(phase, "at an unrecorded point")
        print(
            f"INTERRUPTED by the operator {where}. The session log, its summary "
            f"(status INTERRUPTED, reason OPERATOR_INTERRUPT) and the released lock "
            f"are under {args.output}. Nothing continues on its own; to continue at "
            f"the next future slot run again with --resume --approve {args.approve}.",
            file=sys.stderr,
        )
        return 130
    print(json.dumps(summary, indent=2, sort_keys=True))
    requests = summary["requests"]
    print(
        f"session {summary['status']} ({summary['stop_reason'] or 'no stop reason'}): "
        f"{summary['cycles_executed']} cycles executed of {summary['slots_planned']} "
        f"slots; requests scheduled {requests['scheduled']}, attempted "
        f"{requests['attempted']}, HTTP attempts {requests['http_attempts']} "
        f"(retries included) of the approved budget "
        f"{summary['request_budget']['requests']} logical / "
        f"{summary['request_budget']['max_attempts']} HTTP; "
        f"{requests['without_receipt']} begun without a receipt. Raw captures only, "
        "no GEX computed, no orders placed. Assemble with "
        f"python -m src.tools.assemble_intraday_session {pathlib.Path(args.output)} "
        "--out <new directory>"
    )
    return 0 if summary["status"] == "COMPLETED" else 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
