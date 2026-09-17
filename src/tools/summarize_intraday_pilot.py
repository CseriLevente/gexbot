"""Summarise several per-session readiness reports into one pilot summary.

Takes the ``session-readiness.json`` files written by
``assemble_intraday_session.py`` (or ``pilot-readiness.json`` files written by
``normalize_thetadata_capture.py``), one per session, and writes
``pilot-summary.json`` and ``pilot-summary.md``. Reads and verifies bytes;
contacts nothing; computes no GEX; places no orders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from src.replay.pilot_summary import (
    DEFAULT_MINIMUM_SESSIONS,
    PilotSummaryError,
    read_reports,
    render_summary_markdown,
    summarize_pilot,
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
    parser.add_argument("reports", nargs="+", type=Path, help="readiness JSON files")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--minimum-sessions",
        type=int,
        default=DEFAULT_MINIMUM_SESSIONS,
        help="sessions the pilot brief requires on the option side (default 5)",
    )
    parser.add_argument("--label", default=None)
    args = parser.parse_args(argv)
    try:
        out = args.out.resolve()
        if out.exists():
            raise ValueError("output directory must be unused")
        summary = summarize_pilot(
            read_reports([p.resolve() for p in args.reports]),
            minimum_sessions=args.minimum_sessions,
            label=args.label,
        )
        out.mkdir(parents=True)
        summary_sha256 = _write(out / "pilot-summary.json", _pretty(summary))
        _write(
            out / "pilot-summary.md", render_summary_markdown(summary).encode("utf-8")
        )
    except (PilotSummaryError, OSError, ValueError, TypeError, KeyError) as error:
        print(f"pilot summary refused: {error}", file=sys.stderr)
        return 2
    totals = summary["totals"]
    print(f"summary_sha256: {summary_sha256}")
    print(f"summary_report_hash: {summary['report_hash']}")
    print(
        f"sessions: {totals['sessions']}; option_side_usable_sessions: "
        f"{totals['option_side_usable_sessions']}; usable_decisions: "
        f"{totals['usable_decisions']} of {totals['expected_decisions']}; "
        f"option_side_pilot_ready: {summary['option_side_pilot_ready']}; "
        f"usable_for_intraday_pilot: {summary['usable_for_intraday_pilot']}; "
        f"synthetic_only: {summary['synthetic_only']}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
