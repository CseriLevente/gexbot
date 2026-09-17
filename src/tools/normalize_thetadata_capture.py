"""Offline normalization of one verified capture into hash-bound research events.

Reads a capture directory already taken (manifest, raw payloads, run intent and,
when present, the attempt log), emits ``research-events/2.1.35`` records with
per-record raw lineage, replays the session through the existing chronological
replay and writes a pilot-readiness report. No network, no GEX, no trading.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from src.adapters.thetadata.research_events import normalize_capture
from src.replay.pilot_readiness import pilot_readiness, render_markdown
from src.replay.research_contract import ResearchContract
from src.replay.session import PLAN_SCHEMA, replay_bundle

#: Research fill policy carried by every plan this command writes. There are
#: no probes: a capture without futures quotes has nothing to probe.
FILL_POLICY = {"latency_ms": 250, "max_wait_ms": 2000, "max_quote_age_ms": 1000}


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
    parser.add_argument("capture", type=Path)
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
        capture, out = args.capture.resolve(), args.out.resolve()
        if out.exists() or out.is_relative_to(capture) or capture.is_relative_to(out):
            raise ValueError("output directory must be unused and outside the capture")
        normalized = normalize_capture(
            capture, receipt_clock_tolerance_ms=args.receipt_clock_tolerance_ms
        )
        contract = ResearchContract(close_buffer_minutes=args.close_buffer_minutes)
        out.mkdir(parents=True)
        events_sha256 = _write(out / "events.json", normalized.encode())
        plan = {
            "schema_version": PLAN_SCHEMA,
            "session_date": normalized.session_date.isoformat(),
            "contract": contract.as_dict(),
            "sources": [{"path": "events.json", "sha256": events_sha256}],
            "fill_policy": dict(FILL_POLICY),
            "probes": [],
        }
        plan_sha256 = _write(out / "replay-plan.json", _pretty(plan))
        report = replay_bundle(out)
        _write(out / "replay-report.json", _pretty(report))
        readiness = pilot_readiness(
            normalized.coverage,
            report,
            events_sha256=events_sha256,
            plan_sha256=plan_sha256,
            receipt_clock_tolerance_ms=args.receipt_clock_tolerance_ms,
            label=args.label,
        )
        _write(out / "pilot-readiness.json", _pretty(readiness))
        _write(out / "pilot-readiness.md", render_markdown(readiness).encode("utf-8"))
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        print(f"capture normalization refused: {error}", file=sys.stderr)
        return 2
    print(f"events_sha256: {events_sha256}")
    print(f"replay_report_hash: {report['report_hash']}")
    print(f"readiness_report_hash: {readiness['report_hash']}")
    print(
        f"usable_decisions: {readiness['usable_decisions']} of "
        f"{report['expected_decisions']}; usable_for_intraday_pilot: "
        f"{readiness['usable_for_intraday_pilot']}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
