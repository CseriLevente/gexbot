"""Bounded intraday collection: one approved cycle at a time, everything recorded.

A session is a directory the collector creates and owns::

    <root>/session-intent.json     schedule, policy, both approvals, budget -- written first
    <root>/session-log.jsonl       one line per slot and per lifecycle event, append-only
    <root>/cycles/<HHMMSS>/        one complete one-shot capture per executed slot
    <root>/session-summary.json    counts derived from the log when the session ends
    <root>/session.lock            held while a collector process owns the root

Every cycle is the existing one-shot command (:func:`run_capture`) with its
preflight, its per-request authorisation against the approved plan, its
manifest, attempt log and verification untouched. The collector adds the
schedule, the budget and a second approval that binds what the one-shot's
approval deliberately does not: the session date, the endpoint scope of every
slot, the request budget and the destination. It never passes the
out-of-session or unsettled overrides.

Policy for time: one cycle in flight. A slot may start until
``start_tolerance_seconds`` after its boundary; a slot whose boundary has passed
by more than that -- because the previous cycle overran, because the process
started late, or because it was restarted -- is recorded as missed and never
caught up. Collection resumes at the next boundary still in the future. A
session stops itself after the configured number of consecutive cycles that
acquired nothing, or when the one-shot reports a systemic reason that every
later request would share.

Nothing here parses market data, computes anything or places an order.
"""

from __future__ import annotations

import json
import os
import pathlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from src.domain.canonical import canonical_payload
from src.domain.digests import digest_of
from src.gex.sessions import market_session_date
from src.ingest.clock import Clock
from src.ingest.schedule import CollectionPolicy, CollectionSchedule, ScheduleError
from src.tools.capture_thetadata_once import (
    CaptureRunError,
    destination_refusals,
    plan_capture,
    run_capture,
)

COLLECTOR_VERSION = "intraday-session-collector/2.1.36-r3"
SESSION_INTENT_SCHEMA = "intraday-session-intent/2.1.36"
SESSION_APPROVAL_SCHEMA = "intraday-session-approval/2.1.36"
#: r3: slot entries carry the request accounting (scheduled, attempted, HTTP
#: attempts, with receipt, acquired, not attempted, without receipt) read from
#: the cycle's own report and attempt log, and the log records where an
#: interruption struck. The 2.1.36 log summed scheduled scopes as "issued".
SESSION_LOG_SCHEMA = "intraday-session-log/2.1.36-r3"
SESSION_SUMMARY_SCHEMA = "intraday-session-summary/2.1.36-r3"
#: Systemic stop reasons of the one-shot sweep after which the session stops:
#: every later request would fail the same way, and the budget is bounded.
SESSION_STOPPING_REASONS = frozenset({"AUTHENTICATION_REJECTED", "STORAGE_FAILURE"})
#: The one-shot's stop reason when the operator interrupted a request in
#: flight. The one-shot preserves the partial capture and returns a report
#: rather than raising; the session must still stop, because the operator
#: asked it to (independent review of v2.1.36 r2, finding 1).
OPERATOR_CANCELLED = "OPERATOR_CANCELLED"
#: The one-shot's typed error codes when the interrupt struck outside the
#: sweep -- between claiming the cycle directory and the first request
#: (``bootstrap_failure``) or while the manifest was being written
#: (``finalization_error_code``). Those reports have no
#: ``raw_acquisition.stop_reason``; the code is the only trace of the operator.
INTERRUPT_ERROR_CODES = frozenset(
    {"INTERNAL_ERROR:KeyboardInterrupt", "INTERNAL_ERROR:SystemExit"}
)
LOCK_NAME = "session.lock"
INTENT_NAME = "session-intent.json"
LOG_NAME = "session-log.jsonl"
SUMMARY_NAME = "session-summary.json"


class SessionCollectionError(RuntimeError):
    """A session that must not start, or must not continue."""


class OperatorInterrupt(KeyboardInterrupt):
    """The operator interrupted the session -- during a wait, between cycles,
    or in the middle of a request (in which case the one-shot preserved the
    partial capture and reported ``OPERATOR_CANCELLED``). Raised after the
    session has logged the stop, written its summary and released its lock, so
    every caller sees one kind of interruption. Continuing needs ``--resume``.
    """

    def __init__(
        self, label: str | None, phase: str, *, partial_capture: bool = False
    ) -> None:
        super().__init__(
            f"operator interrupt during {phase.lower()}"
            + (f" of slot {label}" if label else "")
        )
        self.label = label
        self.phase = phase
        #: True when the interrupted cycle left a verified partial capture
        #: (manifest, attempt log) under ``cycles/<label>``; False when it was
        #: interrupted before its first request or outside any cycle.
        self.partial_capture = partial_capture


def operator_cancelled(report: Mapping[str, Any]) -> bool:
    """Whether a cycle's one-shot report says the operator interrupted it.

    The sweep reports an interrupt during a request as
    ``raw_acquisition.stop_reason == OPERATOR_CANCELLED``. An interrupt before
    the first request or during finalization never reaches the sweep and
    surfaces as the one-shot's typed error code instead. Either way the
    one-shot preserved what it had and returned a report rather than raising,
    so the session has to read the report to notice.
    """
    acquisition = report.get("raw_acquisition") or {}
    if str(acquisition.get("stop_reason", "")) == OPERATOR_CANCELLED:
        return True
    codes = {
        str(report.get("error_code", "")),
        str(report.get("finalization_error_code", "")),
    }
    return bool(codes & INTERRUPT_ERROR_CODES)


def request_accounting(
    scope: frozenset[str] | set[str], report: Mapping[str, Any] | None
) -> dict[str, Any]:
    """What a cycle actually did on the wire, from its own evidence.

    ``scheduled`` is the slot's scope. ``attempted`` are the logical requests
    the sweep began (``raw_acquisition.attempted_endpoints``). ``http_attempts``
    counts every attempt record in the cycle's attempt log, retries included;
    ``with_receipt`` the endpoints that have at least one such record;
    ``acquired`` the endpoints whose payload verified. ``not_attempted`` were
    scheduled and never begun (a systemic stop, a cancellation); an endpoint in
    ``without_receipt`` was begun but has no attempt record -- a request in
    flight when the operator interrupted, or a transport that died before
    observing -- and is the explicit uncertainty rather than a count of
    anything. ``attempt_evidence_verified`` says whether the attempt log
    itself verified. A cycle that never started has ``None`` for everything
    but ``scheduled``.
    """
    scheduled = sorted(scope)
    if report is None:
        return {
            "scheduled": len(scheduled),
            "attempted": None,
            "http_attempts": None,
            "http_attempts_failed": None,
            "with_receipt": None,
            "acquired": None,
            "not_attempted": None,
            "without_receipt": None,
            "attempt_evidence_verified": False,
            "basis": "CYCLE_DID_NOT_START",
        }
    acquisition = report.get("raw_acquisition") or {}
    attempted = sorted(set(acquisition.get("attempted_endpoints", [])) & set(scheduled))
    attempts = report.get("http_attempts") or {}
    per_endpoint = attempts.get("attempts_per_endpoint") or {}
    with_receipt = sorted(e for e in per_endpoint if e in scope)
    acquired = sorted(set(report.get("completed_endpoints", [])) & set(scheduled))
    evidence = report.get("attempt_evidence") or {}
    return {
        "scheduled": len(scheduled),
        "attempted": len(attempted),
        "http_attempts": int(attempts.get("attempt_count", 0) or 0),
        "http_attempts_failed": int(attempts.get("failed_attempt_count", 0) or 0),
        "with_receipt": len(with_receipt),
        "acquired": len(acquired),
        "not_attempted": sorted(set(scheduled) - set(attempted)),
        "without_receipt": sorted(set(attempted) - set(with_receipt)),
        "attempt_evidence_verified": bool(evidence.get("ok")),
        "basis": "CYCLE_REPORT_AND_ATTEMPT_LOG",
    }


def total_request_accounting(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Session totals over executed slot entries; unknowns stay unknown."""
    executed = [e for e in entries if e.get("event") == "SLOT" and e.get("requests")]
    totals: dict[str, Any] = {
        "scheduled": 0,
        "attempted": 0,
        "http_attempts": 0,
        "http_attempts_failed": 0,
        "with_receipt": 0,
        "acquired": 0,
        "not_attempted": 0,
        "without_receipt": 0,
        "cycles_with_unverified_attempt_evidence": 0,
        "cycles_without_a_report": 0,
    }
    for entry in executed:
        requests = entry["requests"]
        totals["scheduled"] += int(requests["scheduled"])
        if requests.get("attempted") is None:
            totals["cycles_without_a_report"] += 1
            continue
        for key in (
            "attempted",
            "http_attempts",
            "http_attempts_failed",
            "with_receipt",
            "acquired",
        ):
            totals[key] += int(requests[key])
        totals["not_attempted"] += len(requests["not_attempted"])
        totals["without_receipt"] += len(requests["without_receipt"])
        if requests["attempt_evidence_verified"] is False:
            totals["cycles_with_unverified_attempt_evidence"] += 1
    totals["basis"] = (
        "sum of per-cycle accounting read from each cycle's report and verified "
        "attempt log; scheduled counts the approved scope, attempted the logical "
        "requests begun, http_attempts every attempt record including retries"
    )
    return totals


@dataclass(frozen=True, slots=True)
class SessionApproval:
    """What the operator approves for a whole session, beyond one cycle's plan.

    The one-shot approval says which requests one capture may send. This binds
    that approval to the market session date, the schedule (every slot, its
    instant and its endpoint scope), the request budget the schedule implies,
    the collection policy and the destination root. Change any of them and the
    value an operator pasted stops matching.
    """

    session_date: date
    cycle_approval_hash: str
    schedule_fingerprint: str
    destination: str
    request_budget: dict[str, int]
    schema_version: str = SESSION_APPROVAL_SCHEMA

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_date": self.session_date.isoformat(),
            "cycle_approval_hash": self.cycle_approval_hash,
            "schedule_fingerprint": self.schedule_fingerprint,
            "destination": self.destination,
            "request_budget": dict(sorted(self.request_budget.items())),
        }

    @property
    def approval_hash(self) -> str:
        return digest_of(canonical_payload(self.semantic_payload()))

    def matches(self, approved: str) -> bool:
        return str(approved or "").strip().lower() == self.approval_hash

    def as_dict(self) -> dict[str, Any]:
        return {**self.semantic_payload(), "approval_hash": self.approval_hash}


def _aware(moment: datetime, name: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise SessionCollectionError(f"{name} must be timezone-aware")
    return moment.astimezone(UTC)


def _write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _append(path: pathlib.Path, entry: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(entry, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_log(root: pathlib.Path) -> list[dict[str, Any]]:
    """Every well-formed line of the session log, in order."""
    path = root / LOG_NAME
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text:
            entries.append(json.loads(text))
    return entries


def plan_session(
    config_path: str,
    *,
    output: str,
    now: datetime,
    policy: CollectionPolicy,
) -> dict[str, Any]:
    """The dry run: what a session at ``now`` would do. Writes nothing.

    The session date is the market session ``now`` falls in -- as with the
    one-shot, there is no way to approve another day's session, because the
    per-cycle approval binds the date. Run it on the session morning, read the
    schedule and both approvals, then pass the session approval to the live
    command.
    """
    now = _aware(now, "now")
    destination = pathlib.Path(output).expanduser()
    session_date = market_session_date(now)
    cycle_plan = plan_capture(config_path, output=str(destination), as_of=now)
    settings = cycle_plan.get("effective_transport", {})
    max_attempts = int(settings.get("max_retries", 0)) + 1
    try:
        schedule = CollectionSchedule.build(
            session_date, policy, max_attempts_per_request=max_attempts
        )
    except ScheduleError as error:
        return {
            "schema_version": SESSION_INTENT_SCHEMA,
            "mode": "DRY_RUN",
            "collector_version": COLLECTOR_VERSION,
            "session_date": session_date.isoformat(),
            "planned_at": now.isoformat(),
            "schedule_refusal": str(error),
            "destination": str(destination),
            "destination_refusals": list(destination_refusals(destination)),
            "cycle_plan": cycle_plan,
            "wrote_files": False,
        }
    cycle_approval = cycle_plan["preflight_approval"]
    approval = SessionApproval(
        session_date=session_date,
        cycle_approval_hash=str(cycle_approval["approval_hash"]),
        schedule_fingerprint=schedule.fingerprint,
        destination=str(destination.resolve(strict=False)),
        request_budget=schedule.request_budget,
    )
    return {
        "schema_version": SESSION_INTENT_SCHEMA,
        "mode": "DRY_RUN",
        "collector_version": COLLECTOR_VERSION,
        "session_date": session_date.isoformat(),
        "planned_at": now.isoformat(),
        "phase_now": schedule.phase(now),
        "schedule": schedule.as_dict(),
        "policy": policy.as_dict(),
        "cycle_approval": cycle_approval,
        "session_approval": approval.as_dict(),
        "request_budget": schedule.request_budget,
        "destination": str(destination),
        "destination_refusals": list(destination_refusals(destination)),
        "capture_readiness": cycle_plan.get("capture_readiness"),
        "capture_ready": cycle_plan.get("capture_ready"),
        "capture_blockers": cycle_plan.get("capture_blockers", []),
        "expected_capture_origin": cycle_plan.get("expected_capture_origin"),
        "market_session": cycle_plan.get("market_session", {}),
        "cycle_plan": cycle_plan,
        "wrote_files": False,
        "would_place_orders": False,
    }


@dataclass
class _Session:
    root: pathlib.Path
    schedule: CollectionSchedule
    intent: dict[str, Any]
    log: pathlib.Path
    started_at: datetime
    executed: int = 0
    consecutive_failures: int = 0
    last_finished_at: datetime | None = None
    ran_a_cycle: bool = False
    status: str = "RUNNING"
    stop_reason: str = ""
    on_entry: Callable[[dict[str, Any]], None] | None = None
    #: Where the process is, so an interruption can say what it interrupted.
    current_label: str | None = None
    current_phase: str = "BETWEEN_CYCLES"
    interruption: dict[str, Any] | None = None


def _log(session: _Session, entry: dict[str, Any]) -> None:
    written = {"schema_version": SESSION_LOG_SCHEMA, **entry}
    _append(session.log, written)
    if session.on_entry is not None:
        session.on_entry(written)


def _slot_entry(slot: Any, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "event": "SLOT",
        "slot": slot.index,
        "label": slot.label,
        "scheduled_at": slot.scheduled_at.isoformat(),
        "kind": slot.kind,
        "scope": sorted(slot.scope),
        "status": status,
        **extra,
    }


def _open_session(
    config_path: str,
    *,
    output: str,
    approved: str,
    now: datetime,
    policy: CollectionPolicy,
    resume: bool,
    live: bool,
    on_entry: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[_Session, list[dict[str, Any]]]:
    """Refuse everything refusable, then claim the root or reopen it."""
    root = pathlib.Path(output).expanduser()
    plan = plan_session(config_path, output=str(root), now=now, policy=policy)
    if "schedule_refusal" in plan:
        raise SessionCollectionError(plan["schedule_refusal"])
    approval = SessionApproval(
        session_date=date.fromisoformat(plan["session_date"]),
        cycle_approval_hash=plan["cycle_approval"]["approval_hash"],
        schedule_fingerprint=plan["schedule"]["fingerprint"],
        destination=plan["session_approval"]["destination"],
        request_budget=plan["request_budget"],
    )
    if not str(approved or "").strip():
        raise SessionCollectionError(
            "a collection session requires --approve with the session approval "
            "printed by a dry run taken on this session day:\n"
            f"    --approve {approval.approval_hash}"
        )
    if not approval.matches(approved):
        raise SessionCollectionError(
            f"--approve {approved.strip()} does not authorise this session; the "
            f"approval for what would run now is {approval.approval_hash}. Rerun "
            "the dry run, read the schedule and the budget, and approve that."
        )
    if plan.get("capture_ready") is not True:
        raise SessionCollectionError(
            f"the capture configuration is {plan.get('capture_readiness')}: "
            f"{plan.get('capture_blockers')}"
        )
    schedule = CollectionSchedule.build(
        approval.session_date,
        policy,
        max_attempts_per_request=plan["request_budget"]["max_attempts_per_request"],
    )
    phase = schedule.phase(now)
    if phase in {"OTHER_SESSION_DATE", "AFTER_COLLECTION"}:
        raise SessionCollectionError(
            f"{now.isoformat()} is {phase} for the {schedule.session_date} "
            "schedule; nothing can be collected"
        )
    entries: list[dict[str, Any]] = []
    if resume:
        if not (root / INTENT_NAME).is_file():
            raise SessionCollectionError(
                f"{root} holds no {INTENT_NAME}; there is no session to resume"
            )
        intent = json.loads((root / INTENT_NAME).read_text(encoding="utf-8"))
        recorded = intent.get("session_approval", {}).get("approval_hash")
        if recorded != approval.approval_hash:
            raise SessionCollectionError(
                f"the session at {root} was approved as {recorded}; the session "
                f"that would run now is {approval.approval_hash}. A resumed "
                "session must be the same session."
            )
        entries = read_log(root)
    else:
        refusals = destination_refusals(root)
        if refusals:
            raise SessionCollectionError("; ".join(refusals))
        try:
            root.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise SessionCollectionError(f"{root} already exists") from error
        intent = {
            "schema_version": SESSION_INTENT_SCHEMA,
            "collector_version": COLLECTOR_VERSION,
            "mode": "LIVE" if live else "OFFLINE_TRANSPORT",
            "session_date": schedule.session_date.isoformat(),
            "created_at": now.isoformat(),
            "config_path": str(pathlib.Path(config_path).resolve()),
            "schedule": schedule.as_dict(),
            "policy": policy.as_dict(),
            "cycle_approval": plan["cycle_approval"],
            "session_approval": approval.as_dict(),
            "request_budget": schedule.request_budget,
            "destination": str(root.resolve(strict=False)),
            "expected_capture_origin": plan.get("expected_capture_origin"),
            "pipeline_fingerprint": plan["cycle_plan"].get("pipeline_fingerprint"),
            "capture_plan_fingerprint": plan["cycle_plan"].get(
                "capture_plan_fingerprint"
            ),
            "market_session": plan.get("market_session", {}),
            "overrides": {"allow_out_of_session": False, "allow_unsettled": False},
        }
        _write_json(root / INTENT_NAME, intent)
        (root / "cycles").mkdir()
    lock = root / LOCK_NAME
    try:
        with lock.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "at": now.isoformat()}))
    except FileExistsError as error:
        raise SessionCollectionError(
            f"{lock} exists, so a collector may still own this session. If none "
            "is running, remove the lock file and resume again."
        ) from error
    session = _Session(root, schedule, intent, root / LOG_NAME, now, on_entry=on_entry)
    _log(
        session,
        {
            "event": "RESTART" if resume else "SESSION_START",
            "at": now.isoformat(),
            "phase": phase,
            "previous_entries": len(entries),
            "collector_version": COLLECTOR_VERSION,
        },
    )
    return session, entries


def _summary(session: _Session, entries: list[dict[str, Any]]) -> dict[str, Any]:
    slots = [e for e in entries if e.get("event") == "SLOT"]
    by_status: dict[str, int] = {}
    for entry in slots:
        by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1
    executed = [e for e in slots if e["status"] == "EXECUTED"]
    durations = sorted(float(e["duration_seconds"]) for e in executed)
    failures: dict[str, int] = {}
    for entry in executed:
        for endpoint in entry.get("missing", []):
            failures[endpoint] = failures.get(endpoint, 0) + 1
    return {
        "schema_version": SESSION_SUMMARY_SCHEMA,
        "collector_version": COLLECTOR_VERSION,
        "session_date": session.schedule.session_date.isoformat(),
        "status": session.status,
        "stop_reason": session.stop_reason,
        "slots_planned": len(session.schedule.slots),
        "slots_by_status": dict(sorted(by_status.items())),
        "cycles_executed": len(executed),
        "cycles_with_every_scheduled_endpoint": sum(
            1 for e in executed if not e.get("missing")
        ),
        "cycles_overrunning_a_boundary": sum(
            1 for e in executed if e.get("overran_next_boundary")
        ),
        "endpoint_failures": dict(sorted(failures.items())),
        "requests": total_request_accounting(entries),
        "request_budget": session.schedule.request_budget,
        "interruption": session.interruption,
        "restarts": sum(1 for e in entries if e.get("event") == "RESTART"),
        "cycle_duration_seconds": (
            {
                "min": durations[0],
                "median": durations[len(durations) // 2],
                "max": durations[-1],
            }
            if durations
            else None
        ),
        "log_entries": len(entries),
        "session_root": str(session.root),
    }


def _executed_entry(
    session: _Session,
    slot: Any,
    report: Mapping[str, Any],
    started: datetime,
    finished: datetime,
    tolerance: timedelta,
) -> dict[str, Any]:
    """The log entry of a cycle whose one-shot ran; updates the session tallies."""
    schedule = session.schedule
    session.executed += 1
    session.ran_a_cycle = True
    acquired = sorted(report.get("completed_endpoints", []))
    missing = sorted(slot.scope - set(acquired))
    session.consecutive_failures = 0 if acquired else session.consecutive_failures + 1
    stop = str((report.get("raw_acquisition") or {}).get("stop_reason", ""))
    if stop in SESSION_STOPPING_REASONS:
        session.status, session.stop_reason = "STOPPED", stop
    return _slot_entry(
        slot,
        "EXECUTED",
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        duration_seconds=round((finished - started).total_seconds(), 3),
        start_delay_seconds=round((started - slot.scheduled_at).total_seconds(), 3),
        cycle_dir=f"cycles/{slot.label}",
        run_state=report.get("run_state"),
        acquired=acquired,
        missing=missing,
        manifest_hash=report.get("manifest_hash"),
        capture_session_id=report.get("session_id"),
        stop_reason=stop,
        error_code=report.get("error_code", ""),
        operator_cancelled=operator_cancelled(report),
        overran_next_boundary=bool(
            slot.index + 1 < len(schedule.slots)
            and finished > schedule.slots[slot.index + 1].scheduled_at + tolerance
        ),
        requests=request_accounting(slot.scope, report),
    )


def collect_session(
    config_path: str,
    *,
    output: str,
    approved: str,
    clock: Clock,
    policy: CollectionPolicy,
    transport: Any = None,
    resume: bool = False,
    stop_after_label: str | None = None,
    on_entry: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run (or resume) one collection session and return its summary.

    ``transport`` is ``None`` for the live vendor path -- every cycle then
    builds the configured transport, and the one-shot's own window check runs
    on every cycle. Tests pass a deterministic fake and a fake clock.
    ``stop_after_label`` ends the process after that slot as an interruption
    would; it exists so a restart can be exercised offline. ``on_entry`` sees
    every log line as it is written, for an operator's console.
    """
    now = _aware(clock.now(), "clock.now()")
    session, entries = _open_session(
        config_path,
        output=output,
        approved=approved,
        now=now,
        policy=policy,
        resume=resume,
        live=transport is None,
        on_entry=on_entry,
    )
    done = {e["label"] for e in entries if e.get("event") == "SLOT"}
    tolerance = timedelta(seconds=policy.start_tolerance_seconds)
    schedule = session.schedule
    cycle_hash = session.intent["cycle_approval"]["approval_hash"]
    try:
        for slot in schedule.slots:
            if slot.label in done:
                continue
            session.current_label, session.current_phase = slot.label, "BETWEEN_CYCLES"
            now = clock.now()
            if now < slot.scheduled_at:
                session.current_phase = "WAIT"
                clock.sleep_until(slot.scheduled_at)
                session.current_phase = "BETWEEN_CYCLES"
                now = clock.now()
            if now > slot.scheduled_at + tolerance:
                if (
                    session.last_finished_at is not None
                    and session.last_finished_at > slot.scheduled_at
                ):
                    status, cause = "MISSED_OVERRUN", "previous cycle finished later"
                elif resume and not session.ran_a_cycle:
                    status, cause = "MISSED_RESTART_GAP", "slot passed before resume"
                else:
                    status, cause = "MISSED_LATE_START", "slot passed before start"
                _log(
                    session,
                    _slot_entry(
                        slot,
                        status,
                        observed_at=now.isoformat(),
                        late_by_seconds=round(
                            (now - slot.scheduled_at).total_seconds(), 3
                        ),
                        cause=cause,
                    ),
                )
                continue
            if session.executed >= schedule.request_budget["cycles"]:
                session.status, session.stop_reason = "STOPPED", "BUDGET_EXHAUSTED"
                _log(session, _slot_entry(slot, "STOPPED", cause="budget exhausted"))
                break
            started = now
            cycle_dir = session.root / "cycles" / slot.label
            entry: dict[str, Any]
            session.current_phase = "REQUEST"
            try:
                report = run_capture(
                    config_path,
                    output=str(cycle_dir),
                    transport=transport,
                    as_of=started,
                    approved=cycle_hash,
                    clock=clock.now,
                    scheduled_endpoints=slot.scope,
                )
            except CaptureRunError as error:
                finished = clock.now()
                session.current_phase = "BETWEEN_CYCLES"
                session.consecutive_failures += 1
                entry = _slot_entry(
                    slot,
                    "FAILED_TO_START",
                    started_at=started.isoformat(),
                    finished_at=finished.isoformat(),
                    start_delay_seconds=round(
                        (started - slot.scheduled_at).total_seconds(), 3
                    ),
                    error_message=str(error)[:400],
                    requests=request_accounting(slot.scope, None),
                )
            else:
                finished = clock.now()
                session.current_phase = "BETWEEN_CYCLES"
                if report.get("bootstrap_failure") and not report.get("attempt_count"):
                    # The one-shot claimed the cycle directory and failed -- or
                    # was interrupted -- before its first request. It wrote
                    # ``capture-bootstrap-failure.json`` and no manifest, so
                    # there is no capture to assemble: the slot did not start,
                    # and its accounting is certain rather than unknown.
                    session.consecutive_failures += 1
                    entry = _slot_entry(
                        slot,
                        "FAILED_TO_START",
                        started_at=started.isoformat(),
                        finished_at=finished.isoformat(),
                        start_delay_seconds=round(
                            (started - slot.scheduled_at).total_seconds(), 3
                        ),
                        cycle_dir=f"cycles/{slot.label}",
                        report_path=str(report.get("summary_path", "")),
                        run_state=report.get("run_state"),
                        error_code=report.get("error_code", ""),
                        error_message=str(report.get("error_message", ""))[:400],
                        operator_cancelled=operator_cancelled(report),
                        requests={
                            **request_accounting(slot.scope, None),
                            "attempted": 0,
                            "http_attempts": 0,
                            "http_attempts_failed": 0,
                            "with_receipt": 0,
                            "acquired": 0,
                            "not_attempted": sorted(slot.scope),
                            "without_receipt": [],
                            # No request, no attempt log: nothing to verify.
                            "attempt_evidence_verified": None,
                            "basis": "BOOTSTRAP_FAILURE_REPORT_BEFORE_ANY_REQUEST",
                        },
                    )
                else:
                    entry = _executed_entry(
                        session, slot, report, started, finished, tolerance
                    )
            session.last_finished_at = finished
            _log(session, entry)
            if entry.get("operator_cancelled"):
                # The operator interrupted the cycle -- a request in flight,
                # or the moments before or after the sweep. The one-shot kept
                # the partial capture and its attempt evidence; the session
                # honours the interruption instead of taking the next slot.
                # Continuing is an explicit --resume.
                raise OperatorInterrupt(
                    slot.label,
                    "REQUEST",
                    partial_capture=entry["status"] == "EXECUTED",
                )
            if session.status == "STOPPED":
                _log(
                    session,
                    {
                        "event": "STOP",
                        "at": finished.isoformat(),
                        "reason": session.stop_reason,
                    },
                )
                break
            if session.consecutive_failures >= policy.max_consecutive_failed_cycles:
                session.status = "STOPPED"
                session.stop_reason = "CONSECUTIVE_FAILED_CYCLES"
                _log(
                    session,
                    {
                        "event": "STOP",
                        "at": finished.isoformat(),
                        "reason": session.stop_reason,
                        "consecutive_failed_cycles": session.consecutive_failures,
                    },
                )
                break
            if stop_after_label == slot.label:
                session.status, session.stop_reason = "INTERRUPTED", "STOP_AFTER_LABEL"
                break
        else:
            session.status = "COMPLETED"
    except KeyboardInterrupt as interrupt:
        label = getattr(interrupt, "label", session.current_label)
        phase = getattr(interrupt, "phase", session.current_phase)
        partial = bool(getattr(interrupt, "partial_capture", False))
        session.status, session.stop_reason = "INTERRUPTED", "OPERATOR_INTERRUPT"
        session.interruption = {
            "slot": label,
            "phase": phase,
            "partial_capture_preserved": partial,
            "cycle_dir": f"cycles/{label}" if phase == "REQUEST" and label else None,
            "continuing_requires": "an explicit --resume with the same session "
            "approval; a slot that already has a log entry is never retaken",
        }
        _log(
            session,
            {
                "event": "STOP",
                "at": clock.now().isoformat(),
                "reason": session.stop_reason,
                "interruption": session.interruption,
            },
        )
        if isinstance(interrupt, OperatorInterrupt):
            raise
        raise OperatorInterrupt(label, phase) from interrupt
    finally:
        ended = clock.now()
        _log(
            session,
            {
                "event": "SESSION_END",
                "at": ended.isoformat(),
                "status": session.status,
                "reason": session.stop_reason,
            },
        )
        summary = _summary(session, read_log(session.root))
        summary["ended_at"] = ended.isoformat()
        _write_json(session.root / SUMMARY_NAME, summary)
        (session.root / LOCK_NAME).unlink(missing_ok=True)
    return summary
