"""Compare fixed pricing hypotheses against an existing capture, offline."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Sequence

from src.adapters.thetadata.capture_certification import CaptureCertificationError
from src.adapters.thetadata.pricing_diagnostics import diagnose_capture


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root")
    parser.add_argument("--archive-path", default=None)
    parser.add_argument("--json", dest="json_path", required=True)
    args = parser.parse_args(argv)
    root = pathlib.Path(args.capture_root).resolve()
    output = pathlib.Path(args.json_path)
    try:
        if output.resolve().is_relative_to(root):
            raise ValueError("output must be outside the preserved capture directory")
        if output.exists():
            raise ValueError("output already exists; use a new report path")
        report = diagnose_capture(root, archive_path=args.archive_path)
        rendered = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
        # Exclusive creation also refuses a destination created during analysis.
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
    except (CaptureCertificationError, OSError, ValueError) as error:
        print(f"pricing diagnostic refused: {error}", file=sys.stderr)
        return 2
    print(f"report: {output}")
    print(f"diagnostic_report_hash: {report['diagnostic_report_hash']}")
    print(f"comparable_rows: {report['pricing_admission']['comparable_rows']}")
    print(f"candidates: {report['pricing']['candidate_count']}")
    print(f"numerical_best: {report['pricing']['numerical_best_candidate']}")
    print("vendor_model_identified: False; trusted_for_gex: False; orders_placed: 0")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
