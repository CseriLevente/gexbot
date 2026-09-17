"""Offline assembly of one collection session into replayable research events.

Reads a session directory written by ``collect_intraday_session.py`` (intent,
log and one verified one-shot capture per executed cycle), verifies its
structure, normalizes every cycle under the scope it was scheduled to issue,
merges the cycles in recorded-availability order into
``research-events/2.1.36`` records with per-record cycle lineage, replays the
session through the existing chronological replay and writes a session
readiness report. No network, no GEX, no trading.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from src.adapters.thetadata.capture_certification import CaptureCertificationError
from src.adapters.thetadata.session_assembly import (
    SessionAssemblyError,
    assemble_session,
)
from src.replay.research_contract import ResearchContract
from src.replay.session import PLAN_SCHEMA, replay_bundle
from src.replay.session_readiness import render_session_markdown, session_readiness
from src.tools.normalize_thetadata_capture import FILL_POLICY

OUTPUTS = (
    "events.json",
    "replay-plan.json",
    "replay-report.json",
    "session-assembly.json",
    "session-readiness.json",
    "session-readiness.md",
)


def _pretty(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _write(path: Path, raw: bytes) -> str:
    with path.open("xb") as stream:
        stream.write(raw)
    return hashlib.sha256(raw).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path, help="session directory to assemble")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--receipt-clock-tolerance-ms",
        type=int,
        default=0,
        help="allow vendor event times this far after the local receipt by "
        "deferring availability to the event time (default 0: exclude)",
    )
    parser.add_argument(
        "--close-buffer-minutes",
        type=int,
        default=ResearchContract().close_buffer_minutes,
        help="research contract close buffer; anything but the default marks "
        "the run diagnostic",
    )
    parser.add_argument("--label", default=None)
    args = parser.parse_args(argv)
    try:
        session, out = args.session.resolve(), args.out.resolve()
        if out.exists() or out.is_relative_to(session) or session.is_relative_to(out):
            raise ValueError("output directory must be unused and outside the session")
        assembled = assemble_session(
            session, receipt_clock_tolerance_ms=args.receipt_clock_tolerance_ms
        )
        contract = ResearchContract(close_buffer_minutes=args.close_buffer_minutes)
        out.mkdir(parents=True)
        events_sha256 = _write(out / "events.json", assembled.encode())
        assembly_sha256 = _write(
            out / "session-assembly.json", _pretty(assembled.report)
        )
        plan = {
            "schema_version": PLAN_SCHEMA,
            "session_date": assembled.session_date.isoformat(),
            "contract": contract.as_dict(),
            "sources": [{"path": "events.json", "sha256": events_sha256}],
            "fill_policy": dict(FILL_POLICY),
            "probes": [],
        }
        plan_sha256 = _write(out / "replay-plan.json", _pretty(plan))
        report = replay_bundle(out)
        _write(out / "replay-report.json", _pretty(report))
        readiness = session_readiness(
            assembled.report,
            report,
            events_sha256=events_sha256,
            plan_sha256=plan_sha256,
            assembly_sha256=assembly_sha256,
            receipt_clock_tolerance_ms=args.receipt_clock_tolerance_ms,
            label=args.label,
        )
        _write(out / "session-readiness.json", _pretty(readiness))
        _write(
            out / "session-readiness.md",
            render_session_markdown(readiness).encode("utf-8"),
        )
    except (
        SessionAssemblyError,
        CaptureCertificationError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ) as error:
        print(f"session assembly refused: {error}", file=sys.stderr)
        return 2
    print(f"events_sha256: {events_sha256}")
    print(f"assembly_sha256: {assembly_sha256}")
    print(f"replay_report_hash: {report['report_hash']}")
    print(f"readiness_report_hash: {readiness['report_hash']}")
    print(
        f"cycles_assembled: {assembled.report['session']['cycles_assembled']} of "
        f"{assembled.report['session']['slots_planned']} slots; records "
        f"{assembled.report['records']}; origin {assembled.origin}"
    )
    print(
        f"usable_decisions: {readiness['usable_decisions']} of "
        f"{report['expected_decisions']}; option_side_usable: "
        f"{readiness['option_side_usable']}; usable_for_intraday_pilot: "
        f"{readiness['usable_for_intraday_pilot']}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
