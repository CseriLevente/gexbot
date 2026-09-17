"""Infer model support from verified captures, with floor-censored clock evidence."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from src.adapters.thetadata.capture_certification import CaptureCertificationError
from src.adapters.thetadata.floor_inference import infer_capture, validate_captures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capture",
        nargs=2,
        action="append",
        required=True,
        metavar=("DIRECTORY", "ZIP"),
    )
    parser.add_argument("--json", dest="json_path", required=True)
    args = parser.parse_args(argv)
    inputs = tuple(
        (Path(root).resolve(), Path(archive).resolve())
        for root, archive in args.capture
    )
    output = Path(args.json_path)
    try:
        if output.exists() or any(
            output.resolve().is_relative_to(root) for root, _ in inputs
        ):
            raise ValueError(
                "report path must be unused and outside every capture directory"
            )
        report = (
            infer_capture(inputs[0][0], archive_path=inputs[0][1])
            if len(inputs) == 1
            else validate_captures(inputs)
        )
        text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except (CaptureCertificationError, OSError, ValueError) as error:
        print(f"pricing inference refused: {error}", file=sys.stderr)
        return 2
    print(f"report_hash: {report['report_hash']}")
    print(
        "trusted_for_gex: False; full_vendor_model_identified: False; orders_placed: 0"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
