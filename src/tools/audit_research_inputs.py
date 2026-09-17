"""Offline declaration audits and retrospective gamma-model sensitivity."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from src.adapters.thetadata.capture_certification import CaptureCertificationError
from src.adapters.thetadata.gamma_sensitivity import analyze_captures
from src.replay.research_contract import audit_document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    frames = sub.add_parser("frames")
    frames.add_argument("input", type=Path)
    frames.add_argument("--json", required=True, type=Path)
    models = sub.add_parser("gamma")
    models.add_argument("--capture", nargs=2, action="append", required=True)
    models.add_argument("--json", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        inputs = (
            tuple((Path(r).resolve(), Path(z).resolve()) for r, z in args.capture)
            if args.mode == "gamma"
            else ()
        )
        out = args.json.resolve()
        if out.exists() or any(out.is_relative_to(root) for root, _ in inputs):
            raise ValueError("output must be unused and outside capture evidence")
        report = (
            analyze_captures(inputs)
            if inputs
            else audit_document(json.loads(args.input.read_bytes()))
        )
        with out.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
            )
    except (
        CaptureCertificationError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ) as error:
        print(f"research audit refused: {error}", file=sys.stderr)
        return 2
    print(f"report_hash: {report['report_hash']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
