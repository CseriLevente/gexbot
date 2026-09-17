"""Assemble one collection session's cycles into a single event stream.

A session directory (written by :mod:`src.ingest.session_collector`) holds one
verified one-shot capture per executed cycle. This module verifies the
session's structure, normalizes every cycle with the single-capture normalizer
under the scope that cycle was scheduled to issue, and merges the results in
recorded-availability order into ``research-events/2.1.36`` records whose
lineage names the session, the cycle, the request, the raw payload digest, the
native row and the rule version.

Merge rule, applied in availability order with a deterministic tie-break
``(available_at, cycle label, kind, key, event_at, row index)``:

- an observation of a ``(kind, key, event_at)`` not yet known is a new event,
  sequence 0, available when its cycle received it -- even when a later
  ``event_at`` of the same ``(kind, key)`` is already known (the replay's
  selection by ``(event_at, sequence)`` keeps the newer state; the late record
  is retained and counted);
- an observation identical to the **current known revision** of that
  ``(kind, key, event_at)`` is a re-observation: nothing is emitted, the
  original record keeps its original availability, and the quote's age is not
  refreshed by having been seen again;
- an observation that differs from the current known revision is the next
  revision, sequence + 1, available when its cycle received it. ``A -> B -> A``
  therefore yields three revisions, because the third observation differs
  from the current revision ``B``.

Ambiguity: a cycle whose payload carries conflicting rows for one identity
excludes that identity (the r2 rule) **for that cycle only**. Previously merged
records are never modified, revised or withdrawn by a later ambiguous
observation, and no superseded revision is revived; the incident is counted
against the identity so the readiness report can say the state was left
standing on older evidence.

Inventory: a cycle that acquired the contract listing was membership-checked by
the normalizer against its own listing. A cycle without a listing is checked
here against the latest listing available before it started; when none is
available yet, its option records are retained and counted as unchecked.

Nothing here parses vendor bytes on its own, computes a GEX or touches a
network. The raw captures are read, never written.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from src.adapters.thetadata.capture_certification import (
    OPTION_CONTRACT_LIST,
    OPTION_GREEKS,
    OPTION_OPEN_INTEREST,
    OPTION_QUOTE,
    CaptureCertificationError,
)
from src.adapters.thetadata.research_events import (
    KINDS,
    NORMALIZER,
    NormalizationError,
    normalize_capture,
)
from src.domain.canonical import canonical_payload
from src.domain.digests import digest_of
from src.ingest.session_collector import (
    INTENT_NAME,
    LOG_NAME,
    SESSION_INTENT_SCHEMA,
    SessionApproval,
    read_log,
)
from src.replay.event_store import SESSION_EVENT_SCHEMA

ASSEMBLER = "thetadata-session-assembly/2.1.36"
ASSEMBLY_SCHEMA = "intraday-session-assembly/2.1.36"
OPTION_KINDS = frozenset({"option_quote", "greeks", "open_interest"})
KIND_ENDPOINTS = {kind: endpoint for endpoint, kind in KINDS.items()}
#: Merge outcomes, counted per kind in the assembly report.
NEW_EVENT = "NEW_EVENT"
REOBSERVED_UNCHANGED = "REOBSERVED_UNCHANGED"
REVISION = "REVISION"
REVERTED_REVISION = "REVERTED_REVISION"
LATE_OLDER_EVENT = "LATE_OLDER_EVENT"
NOT_IN_LATEST_INVENTORY = "NOT_IN_LATEST_INVENTORY"
UNCHECKED_NO_INVENTORY_YET = "UNCHECKED_NO_INVENTORY_YET"
AMBIGUOUS_AFTER_KNOWN_STATE = "AMBIGUOUS_AFTER_KNOWN_STATE"
AMBIGUOUS_WITHOUT_KNOWN_STATE = "AMBIGUOUS_WITHOUT_KNOWN_STATE"


class SessionAssemblyError(ValueError):
    """A session that cannot be assembled honestly."""


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ExecutedCycle:
    """One executed slot, as the log and the schedule describe it."""

    label: str
    index: int
    scheduled_at: datetime
    scope: frozenset[str]
    started_at: datetime
    finished_at: datetime
    entry: dict[str, Any]
    root: pathlib.Path


@dataclass
class SessionVerification:
    """What the session directory proves about itself before any row is read."""

    root: pathlib.Path
    session_date: date
    intent: dict[str, Any]
    intent_sha256: str
    log_sha256: str
    entries: list[dict[str, Any]]
    cycles: list[ExecutedCycle]
    findings: list[str] = field(default_factory=list)
    orphan_cycle_directories: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_root": str(self.root),
            "session_date": self.session_date.isoformat(),
            "intent_sha256": self.intent_sha256,
            "log_sha256": self.log_sha256,
            "log_entries": len(self.entries),
            "executed_cycles": [c.label for c in self.cycles],
            "orphan_cycle_directories": list(self.orphan_cycle_directories),
            "findings": list(self.findings),
            "structure_verified": self.verified,
        }


def verify_session(root: pathlib.Path | str) -> SessionVerification:
    """Prove the session's structure: intent, approvals, schedule, log, cycles.

    Refuses (raises) when the intent or log is unreadable. Records a finding,
    without raising, for every disagreement between what the log claims and
    what the cycle directories hold; :func:`assemble_session` refuses a session
    with findings, because a merged stream built over a disagreement would
    carry lineage to bytes the session does not vouch for.
    """
    root = pathlib.Path(root).resolve()
    intent_path = root / INTENT_NAME
    log_path = root / LOG_NAME
    if not intent_path.is_file():
        raise SessionAssemblyError(f"{root} holds no {INTENT_NAME}; not a session")
    intent = json.loads(intent_path.read_bytes())
    if intent.get("schema_version") != SESSION_INTENT_SCHEMA:
        raise SessionAssemblyError(
            f"unsupported session intent schema {intent.get('schema_version')!r}"
        )
    findings: list[str] = []
    session_date = date.fromisoformat(str(intent["session_date"]))
    recorded = intent["session_approval"]
    approval = SessionApproval(
        session_date=date.fromisoformat(str(recorded["session_date"])),
        cycle_approval_hash=str(recorded["cycle_approval_hash"]),
        schedule_fingerprint=str(recorded["schedule_fingerprint"]),
        destination=str(recorded["destination"]),
        request_budget={k: int(v) for k, v in recorded["request_budget"].items()},
    )
    if approval.approval_hash != recorded.get("approval_hash"):
        findings.append("SESSION_APPROVAL_HASH_MISMATCH")
    if approval.session_date != session_date:
        findings.append("SESSION_APPROVAL_DATE_MISMATCH")
    schedule = dict(intent["schedule"])
    fingerprint = schedule.pop("fingerprint", None)
    if digest_of(canonical_payload(schedule)) != fingerprint:
        findings.append("SCHEDULE_FINGERPRINT_MISMATCH")
    if fingerprint != approval.schedule_fingerprint:
        findings.append("SCHEDULE_NOT_THE_APPROVED_ONE")
    if intent["cycle_approval"]["approval_hash"] != approval.cycle_approval_hash:
        findings.append("CYCLE_APPROVAL_NOT_THE_APPROVED_ONE")
    slots = {slot["label"]: slot for slot in schedule["slots"]}
    if not log_path.is_file():
        raise SessionAssemblyError(f"{root} holds no {LOG_NAME}; nothing was run")
    entries = read_log(root)
    if not entries or entries[0].get("event") != "SESSION_START":
        findings.append("LOG_DOES_NOT_BEGIN_WITH_SESSION_START")
    cycles: list[ExecutedCycle] = []
    seen_labels: set[str] = set()
    cycle_approval = approval.cycle_approval_hash
    for entry in entries:
        if entry.get("event") != "SLOT":
            continue
        label = str(entry.get("label"))
        if label in seen_labels:
            findings.append(f"SLOT_LOGGED_TWICE:{label}")
            continue
        seen_labels.add(label)
        slot = slots.get(label)
        if slot is None:
            findings.append(f"SLOT_NOT_IN_SCHEDULE:{label}")
            continue
        if sorted(entry.get("scope", [])) != sorted(slot["scope"]):
            findings.append(f"SLOT_SCOPE_DIFFERS_FROM_SCHEDULE:{label}")
        if entry.get("status") != "EXECUTED":
            continue
        cycle_root = root / "cycles" / label
        if entry.get("cycle_dir") != f"cycles/{label}" or not cycle_root.is_dir():
            findings.append(f"CYCLE_DIRECTORY_MISSING:{label}")
            continue
        try:
            from src.adapters.thetadata.capture_certification import load_capture

            capture = load_capture(cycle_root, require_greeks_request=False)
        except (CaptureCertificationError, OSError, ValueError, KeyError) as error:
            findings.append(f"CYCLE_NOT_VERIFIABLE:{label}:{str(error)[:160]}")
            continue
        if capture.manifest_hash != entry.get("manifest_hash"):
            findings.append(f"CYCLE_MANIFEST_DIFFERS_FROM_LOG:{label}")
        if capture.session_id != entry.get("capture_session_id"):
            findings.append(f"CYCLE_SESSION_ID_DIFFERS_FROM_LOG:{label}")
        if capture.scheduled_endpoints is None or sorted(
            capture.scheduled_endpoints
        ) != sorted(slot["scope"]):
            findings.append(f"CYCLE_SCOPE_DIFFERS_FROM_SCHEDULE:{label}")
        if capture.market_session_date != session_date:
            findings.append(f"CYCLE_SESSION_DATE_DIFFERS:{label}")
        run_intent = json.loads((cycle_root / "run-intent.json").read_bytes())
        stamped = (run_intent.get("preflight_approval") or {}).get("approval_hash")
        if stamped != cycle_approval:
            findings.append(f"CYCLE_NOT_UNDER_THE_APPROVED_PLAN:{label}")
        acquired = sorted(entry.get("acquired", []))
        if acquired != sorted(set(capture.record_hashes) & set(KINDS)):
            findings.append(f"CYCLE_PAYLOADS_DIFFER_FROM_LOG:{label}")
        cycles.append(
            ExecutedCycle(
                label=label,
                index=int(slot["index"]),
                scheduled_at=_stamp(slot["scheduled_at"]),
                scope=frozenset(slot["scope"]),
                started_at=_stamp(str(entry["started_at"])),
                finished_at=_stamp(str(entry["finished_at"])),
                entry=entry,
                root=cycle_root,
            )
        )
    cycles_root = root / "cycles"
    present = (
        sorted(p.name for p in cycles_root.iterdir() if p.is_dir())
        if cycles_root.is_dir()
        else []
    )
    executed = {c.label for c in cycles}
    logged_executed = {
        str(e.get("label"))
        for e in entries
        if e.get("event") == "SLOT" and e.get("status") == "EXECUTED"
    }
    orphans = [name for name in present if name not in logged_executed]
    cycles.sort(key=lambda c: c.index)
    if len(executed) != len(cycles):
        findings.append("DUPLICATE_EXECUTED_CYCLE")
    return SessionVerification(
        root=root,
        session_date=session_date,
        intent=intent,
        intent_sha256=_sha256(intent_path),
        log_sha256=_sha256(log_path),
        entries=entries,
        cycles=cycles,
        findings=findings,
        orphan_cycle_directories=orphans,
    )


@dataclass
class AssembledSession:
    """The merged stream and its accounting."""

    session_date: date
    origin: str
    provenance: dict[str, Any]
    records: list[dict[str, Any]]
    report: dict[str, Any]

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": SESSION_EVENT_SCHEMA,
            "origin": self.origin,
            "provenance": self.provenance,
            "records": self.records,
        }

    def encode(self) -> bytes:
        return (
            json.dumps(
                self.document(),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
                ensure_ascii=True,
            )
            + "\n"
        ).encode("utf-8")


@dataclass
class _Known:
    sequence: int
    data: dict[str, Any]
    history: list[dict[str, Any]]


class _Merger:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.known: dict[tuple[str, str, str], _Known] = {}
        self.latest_event: dict[tuple[str, str], str] = {}
        self.outcomes: dict[str, Counter[str]] = {}
        #: ``(available_at, contracts)`` per emitted inventory event, in order.
        self.inventories: list[tuple[datetime, frozenset[str]]] = []
        self.ambiguity: list[dict[str, Any]] = []

    def _count(self, kind: str, outcome: str) -> None:
        self.outcomes.setdefault(kind, Counter())[outcome] += 1

    def inventory_before(self, moment: datetime) -> frozenset[str] | None:
        """The latest listing available at or before ``moment``."""
        chosen = None
        for available, contracts in self.inventories:
            if available <= moment:
                chosen = contracts
            else:
                break
        return chosen

    def has_state(self, kind: str, key: str) -> bool:
        return (kind, key) in self.latest_event

    def offer(self, record: dict[str, Any], *, cycle: str, request_id: str) -> str:
        kind, key, event_at = record["kind"], record["key"], record["event_at"]
        identity = (kind, key, event_at)
        data = record["data"]
        known = self.known.get(identity)
        if known is not None and known.data == data:
            self._count(kind, REOBSERVED_UNCHANGED)
            return REOBSERVED_UNCHANGED
        outcome = NEW_EVENT
        latest = self.latest_event.get((kind, key))
        if latest is not None and event_at < latest:
            # Emitted all the same; the replay's selection by (event_at,
            # sequence) keeps the newer state, and the count says it happened.
            self._count(kind, LATE_OLDER_EVENT)
        if known is None:
            sequence = 0
            self.known[identity] = _Known(0, data, [data])
        else:
            sequence = known.sequence + 1
            outcome = REVISION
            if any(previous == data for previous in known.history[:-1]):
                self._count(kind, REVERTED_REVISION)
            known.sequence = sequence
            known.data = data
            known.history.append(data)
        self._count(kind, outcome)
        if latest is None or event_at > latest:
            self.latest_event[(kind, key)] = event_at
        merged = {
            "kind": kind,
            "key": key,
            "event_at": event_at,
            "available_at": record["available_at"],
            "sequence": sequence,
            "data": data,
            "lineage": {**record["lineage"], "cycle": cycle, "request_id": request_id},
        }
        self.records.append(merged)
        if kind == "contract_list":
            self.inventories.append(
                (_stamp(record["available_at"]), frozenset(data["contracts"]))
            )
            self.inventories.sort(key=lambda item: item[0])
        return outcome


def _cycle_summary(
    cycle: ExecutedCycle, normalized: Any, outcomes: Counter[str]
) -> dict[str, Any]:
    coverage = normalized.coverage
    receipts = {
        endpoint: entry["receipt"] for endpoint, entry in coverage["endpoints"].items()
    }
    available = [
        _stamp(r["available_at"]) for r in receipts.values() if r["available_at"]
    ]
    return {
        "label": cycle.label,
        "slot": cycle.index,
        "scheduled_at": _iso(cycle.scheduled_at),
        "started_at": _iso(cycle.started_at),
        "finished_at": _iso(cycle.finished_at),
        "duration_seconds": cycle.entry.get("duration_seconds"),
        "start_delay_seconds": cycle.entry.get("start_delay_seconds"),
        "overran_next_boundary": bool(cycle.entry.get("overran_next_boundary")),
        "run_state": cycle.entry.get("run_state"),
        "scheduled_endpoints": coverage["scheduled_endpoints"],
        "acquired_endpoints": coverage["acquired_endpoints"],
        "unacquired_endpoints": {
            endpoint: receipts[endpoint]["detail"]
            for endpoint in coverage["unacquired_endpoints"]
        },
        "capture_session_id": coverage["capture"]["session_id"],
        "manifest_sha256": coverage["capture"]["manifest_sha256"],
        "first_receipt": _iso(min(available)) if available else None,
        "last_receipt": _iso(max(available)) if available else None,
        "records_normalized": len(normalized.records),
        "records_by_kind": dict(coverage["records_by_kind"]),
        "excluded": {
            endpoint: entry["excluded"]
            for endpoint, entry in coverage["endpoints"].items()
            if entry["excluded"]
        },
        "conflicting_groups": {
            endpoint: entry["duplicate_groups"]["conflicting_keys"]
            for endpoint, entry in coverage["endpoints"].items()
            if entry["duplicate_groups"]["conflicting_excluded"]
        },
        "vendor_clock_lead": {
            endpoint: entry["vendor_clock_lead"]
            for endpoint, entry in coverage["endpoints"].items()
            if entry["vendor_clock_lead"]["rows_after_receipt"]
        },
        "inventory_membership_checked": coverage["inventory_membership_checked"],
        "model_evidence": {
            "emitted": coverage["model_evidence"]["emitted"],
            "excluded": coverage["model_evidence"]["excluded"],
        },
        "merge": dict(sorted(outcomes.items())),
    }


def assemble_session(
    root: pathlib.Path | str, *, receipt_clock_tolerance_ms: int = 0
) -> AssembledSession:
    """Verify, normalize every executed cycle and merge. Deterministic."""
    verification = verify_session(root)
    if not verification.verified:
        raise SessionAssemblyError(
            "session structure not verified: " + "; ".join(verification.findings)
        )
    if not verification.cycles:
        raise SessionAssemblyError("the session executed no cycle; nothing to assemble")
    intent = verification.intent
    merger = _Merger()
    normalized_cycles: list[tuple[ExecutedCycle, Any]] = []
    skipped: list[dict[str, Any]] = []
    provenance_cycles: dict[str, Any] = {}
    origins: set[str] = set()
    capture_origins: set[str] = set()
    #: The whole session's observations, merged strictly in availability order.
    #: Each item: (available_at, cycle label, order, kind, key, event_at,
    #: row_index, payload). ``order`` puts a payload's ambiguity marker before
    #: that payload's records so "known before" means known from earlier receipts.
    stream: list[tuple[str, str, int, str, str, str, int, dict[str, Any]]] = []
    for cycle in verification.cycles:
        if not cycle.entry.get("acquired"):
            skipped.append({"label": cycle.label, "reason": "NOTHING_ACQUIRED"})
            continue
        try:
            normalized = normalize_capture(
                cycle.root,
                receipt_clock_tolerance_ms=receipt_clock_tolerance_ms,
                expected_endpoints=cycle.scope,
                allow_unacquired=True,
            )
        except (NormalizationError, CaptureCertificationError) as error:
            raise SessionAssemblyError(
                f"cycle {cycle.label} cannot be normalized: {error}"
            ) from error
        if normalized.session_date != verification.session_date:
            raise SessionAssemblyError(
                f"cycle {cycle.label} belongs to {normalized.session_date}, not "
                f"{verification.session_date}"
            )
        origins.add(normalized.origin)
        capture_origins.update(normalized.coverage["capture_origins"])
        if len(origins) > 1:
            raise SessionAssemblyError(
                f"cycles of mixed origin {sorted(origins)} cannot form one session"
            )
        coverage = normalized.coverage
        request_ids = {
            payload["sha256"]: coverage["endpoints"][endpoint]["receipt"]["request_id"]
            for endpoint, payload in normalized.provenance["payloads"].items()
        }
        provenance_cycles[cycle.label] = {
            "capture_session_id": normalized.provenance["capture_session_id"],
            "manifest_sha256": normalized.provenance["manifest_sha256"],
            "run_intent_sha256": normalized.provenance["run_intent_sha256"],
            "payloads": {
                endpoint: {
                    "sha256": payload["sha256"],
                    "location": f"cycles/{cycle.label}/{payload['location']}",
                }
                for endpoint, payload in normalized.provenance["payloads"].items()
            },
        }
        checked = bool(coverage["inventory_membership_checked"])
        for endpoint, entry in coverage["endpoints"].items():
            receipt = entry["receipt"]
            for key in entry["duplicate_groups"]["conflicting_keys"]:
                stream.append(
                    (
                        receipt["available_at"] or cycle.finished_at.isoformat(),
                        cycle.label,
                        0,
                        KINDS[endpoint],
                        key,
                        "",
                        0,
                        {"marker": "AMBIGUOUS"},
                    )
                )
        for record in normalized.records:
            stream.append(
                (
                    record["available_at"],
                    cycle.label,
                    1,
                    record["kind"],
                    record["key"],
                    record["event_at"],
                    record["lineage"]["row_index"] or 0,
                    {
                        "record": record,
                        "request_id": request_ids[record["lineage"]["raw_sha256"]],
                        "membership_checked": checked,
                    },
                )
            )
        normalized_cycles.append((cycle, normalized))
    stream.sort(key=lambda item: item[:7])
    outcomes_by_cycle: dict[str, Counter[str]] = {
        cycle.label: Counter() for cycle, _ in normalized_cycles
    }
    records_by_kind: Counter[str] = Counter()
    membership: Counter[str] = Counter()
    identities: dict[str, set[str]] = {
        "quoted": set(),
        "greeked": set(),
        "with_open_interest": set(),
    }
    listed: set[str] = set()
    for available_at, label, _order, kind, key, _event_at, _row, payload in stream:
        outcomes = outcomes_by_cycle[label]
        if "marker" in payload:
            known = merger.has_state(kind, key)
            merger.ambiguity.append(
                {
                    "cycle": label,
                    "kind": kind,
                    "key": key,
                    "observed_at": available_at,
                    "outcome": (
                        AMBIGUOUS_AFTER_KNOWN_STATE
                        if known
                        else AMBIGUOUS_WITHOUT_KNOWN_STATE
                    ),
                    "known_event_at": merger.latest_event.get((kind, key)),
                    "effect": "previous state left standing; nothing revised",
                }
            )
            continue
        record = payload["record"]
        if kind in OPTION_KINDS and not payload["membership_checked"]:
            reference = merger.inventory_before(_stamp(available_at))
            if reference is None:
                membership[UNCHECKED_NO_INVENTORY_YET] += 1
                outcomes[UNCHECKED_NO_INVENTORY_YET] += 1
            elif key not in reference:
                membership[NOT_IN_LATEST_INVENTORY] += 1
                outcomes[NOT_IN_LATEST_INVENTORY] += 1
                continue
        outcome = merger.offer(record, cycle=label, request_id=payload["request_id"])
        outcomes[outcome] += 1
        if outcome != REOBSERVED_UNCHANGED:
            records_by_kind[kind] += 1
        if kind == "option_quote":
            identities["quoted"].add(key)
        elif kind == "greeks":
            identities["greeked"].add(key)
        elif kind == "open_interest":
            identities["with_open_interest"].add(key)
        elif kind == "contract_list":
            listed.update(record["data"]["contracts"])
    clock_lead_rows = 0
    clock_lead_max_ms = 0
    cycle_reports: list[dict[str, Any]] = []
    for cycle, normalized in normalized_cycles:
        for entry in normalized.coverage["endpoints"].values():
            clock_lead_rows += entry["vendor_clock_lead"]["rows_after_receipt"]
            clock_lead_max_ms = max(
                clock_lead_max_ms, entry["vendor_clock_lead"]["max_ms"]
            )
        cycle_reports.append(
            _cycle_summary(cycle, normalized, outcomes_by_cycle[cycle.label])
        )
    if not cycle_reports:
        raise SessionAssemblyError("no executed cycle acquired a payload")
    # Records are emitted in availability order already; the file order is the
    # replay's own canonical order so identical sessions encode identically.
    merger.records.sort(
        key=lambda r: (
            r["available_at"],
            r["kind"],
            r["key"],
            r["event_at"],
            r["sequence"],
        )
    )
    origin = next(iter(origins))
    session_date = verification.session_date
    provenance = {
        "session_date": session_date.isoformat(),
        "session_approval_hash": intent["session_approval"]["approval_hash"],
        "schedule_fingerprint": intent["schedule"]["fingerprint"],
        "session_intent_sha256": verification.intent_sha256,
        "session_log_sha256": verification.log_sha256,
        "collector": str(intent["collector_version"]),
        "normalizer": NORMALIZER,
        "assembler": ASSEMBLER,
        "cycles": provenance_cycles,
    }
    slots = [e for e in verification.entries if e.get("event") == "SLOT"]
    by_status = Counter(str(e["status"]) for e in slots)
    failures: Counter[str] = Counter()
    for entry in slots:
        if entry["status"] == "EXECUTED":
            for endpoint in entry.get("missing", []):
                failures[endpoint] += 1
    oi_cycles = [
        c["label"]
        for c in cycle_reports
        if OPTION_OPEN_INTEREST in c["acquired_endpoints"]
    ]
    inventory_cycles = [
        c["label"]
        for c in cycle_reports
        if OPTION_CONTRACT_LIST in c["acquired_endpoints"]
    ]
    quote_cycles = [
        c["label"] for c in cycle_reports if OPTION_QUOTE in c["acquired_endpoints"]
    ]
    greeks_cycles = [
        c["label"] for c in cycle_reports if OPTION_GREEKS in c["acquired_endpoints"]
    ]
    with_oi = identities["with_open_interest"] & listed
    report = {
        "schema_version": ASSEMBLY_SCHEMA,
        "assembler": ASSEMBLER,
        "normalizer": NORMALIZER,
        "collector": str(intent["collector_version"]),
        "origin": origin,
        "capture_origins": sorted(capture_origins),
        "session_date": session_date.isoformat(),
        "verification": verification.as_dict(),
        "session": {
            "mode": intent.get("mode"),
            "session_approval_hash": intent["session_approval"]["approval_hash"],
            "schedule_fingerprint": intent["schedule"]["fingerprint"],
            "policy": intent["policy"],
            "request_budget": intent["request_budget"],
            "slots_planned": len(intent["schedule"]["slots"]),
            "slots_by_status": dict(sorted(by_status.items())),
            "cycles_executed": by_status.get("EXECUTED", 0),
            "cycles_assembled": len(cycle_reports),
            "cycles_skipped": skipped,
            "cycles_with_every_scheduled_endpoint": sum(
                1 for c in cycle_reports if not c["unacquired_endpoints"]
            ),
            "cycles_overrunning_a_boundary": sum(
                1 for c in cycle_reports if c["overran_next_boundary"]
            ),
            "restarts": sum(
                1 for e in verification.entries if e.get("event") == "RESTART"
            ),
            "stops": [
                {"at": e.get("at"), "reason": e.get("reason")}
                for e in verification.entries
                if e.get("event") == "STOP"
            ],
            "ended": next(
                (
                    {"status": e.get("status"), "reason": e.get("reason")}
                    for e in reversed(verification.entries)
                    if e.get("event") == "SESSION_END"
                ),
                None,
            ),
            "endpoint_failures": dict(sorted(failures.items())),
            "requests_issued": sum(
                len(e.get("scope", [])) for e in slots if e["status"] == "EXECUTED"
            ),
        },
        "receipt_clock_tolerance_ms": receipt_clock_tolerance_ms,
        "merge": {
            "rule": (
                "availability order; identical to current revision -> not emitted; "
                "different -> next sequence; ties by (available_at, cycle, kind, key, "
                "event_at, row_index)"
            ),
            "outcomes_by_kind": {
                kind: dict(sorted(counter.items()))
                for kind, counter in sorted(merger.outcomes.items())
            },
            "membership": dict(sorted(membership.items())),
            "ambiguity": {
                "incidents": merger.ambiguity,
                "after_known_state": sum(
                    1
                    for a in merger.ambiguity
                    if a["outcome"] == AMBIGUOUS_AFTER_KNOWN_STATE
                ),
                "without_known_state": sum(
                    1
                    for a in merger.ambiguity
                    if a["outcome"] == AMBIGUOUS_WITHOUT_KNOWN_STATE
                ),
                "policy": (
                    "an ambiguous observation revises nothing and revives nothing; "
                    "the previously available state stands on its original receipt"
                ),
            },
        },
        "records": len(merger.records),
        "records_by_kind": dict(sorted(records_by_kind.items())),
        "identities": {
            "listed": len(listed),
            "quoted": len(identities["quoted"] & listed),
            "greeked": len(identities["greeked"] & listed),
            "with_open_interest": len(with_oi),
            "without_open_interest": len(listed - with_oi),
        },
        "cadence": {
            "inventory_cycles": inventory_cycles,
            "open_interest_cycles": oi_cycles,
            "quote_cycles": len(quote_cycles),
            "greeks_cycles": len(greeks_cycles),
            "inventory_events": len(merger.inventories),
        },
        "vendor_clock_lead": {
            "rows_after_receipt": clock_lead_rows,
            "max_ms": clock_lead_max_ms,
        },
        "cycles": cycle_reports,
    }
    return AssembledSession(session_date, origin, provenance, merger.records, report)
