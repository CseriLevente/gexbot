"""Compare two immutable ThetaData captures' open-interest coverage. Offline.

    python -m src.tools.compare_thetadata_captures <earlier-root> <later-root>

Makes no network request. Both captures are certified first -- payload digests,
manifest reconstruction, documentation re-derivation, the lot -- and only then
are their contract identities compared.

The question this answers is the one a single capture cannot: whether a contract
with no open-interest row is a contract whose figure is permanently unavailable,
or one that had not settled yet. Two captures of the same underlying, taken
days apart, contain the same contracts and can be asked directly.

What it does **not** answer is what to do about it. An observed association
between missing rows and newly listed contracts is evidence; a rule that a
missing row may be read as zero, or that the contract may be dropped, is an
analytical decision. This command reports the first and names the second as
open.

Exit codes:

===  ======================================================================
0    the two captures were compared; the report was produced
2    a capture could not be read, verified, or compared with the other
===  ======================================================================
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Sequence
from typing import Any

from src.adapters.thetadata.capture_certification import CaptureCertificationError
from src.adapters.thetadata.oi_transition import (
    CaptureComparisonError,
    OpenInterestTransitionReport,
    compare_captures,
)

EXIT_OK = 0
EXIT_UNCOMPARABLE = 2


def _render(report: OpenInterestTransitionReport) -> str:
    payload: dict[str, Any] = report.as_dict()
    payload["transition_report_hash"] = report.transition_report_hash()
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _summarise(report: OpenInterestTransitionReport) -> list[str]:
    """The report as an operator reads it. Returned rather than printed."""
    data = report.as_dict()
    earlier, later = data["earlier_capture"], data["later_capture"]
    counts = data["transition_counts"]
    rolled = data["transition_rollups"]

    lines = [
        f"earlier         {earlier['session_id']}",
        f"                session {earlier['session_date']} | manifest "
        f"{earlier['manifest_hash'][:16]} | universe "
        f"{earlier['expected_universe_count']:,} | missing OI "
        f"{earlier['oi_missing_count']:,}",
        f"later           {later['session_id']}",
        f"                session {later['session_date']} | manifest "
        f"{later['manifest_hash'][:16]} | universe "
        f"{later['expected_universe_count']:,} | missing OI "
        f"{later['oi_missing_count']:,}",
        f"distance        {data['session_distance_days']} calendar days",
    ]
    for difference in data["scope_differences"]:
        lines.append(f"scope note      {difference}")

    lines.append(
        f"accounting      {data['classified_identity_count']:,} identities "
        f"classified over a {data['union_universe_count']:,}-identity union "
        f"-> exhaustive={data['accounting_is_exhaustive']}"
    )
    lines.append("transitions:")
    for name, count in counts.items():
        if count:
            lines.append(f"    {name:<52} {count:>6,}")
    absent = [name for name, count in counts.items() if not count]
    if absent:
        lines.append(f"    (zero in every other class: {len(absent)} classes)")
    lines.append("rollups:")
    lines.extend(f"    {name:<52} {count:>6,}" for name, count in rolled.items())

    lines.append("per expiration (later universe):")
    for row in data["per_expiration"]:
        lines.append(
            f"    {row['expiration']}  expected {row['expected_contracts']:>5,}  "
            f"new {row['new_contracts']:>5,}  existing "
            f"{row['existing_contracts']:>5,}  present {row['oi_present']:>5,}  "
            f"zero {row['oi_explicit_zero']:>5,}  positive "
            f"{row['oi_positive']:>5,}  missing {row['oi_missing']:>5,}"
        )

    lines.append("findings:")
    lines.extend(f"    {sentence}" for sentence in data["longitudinal_findings"])
    lines.append("evidence status:")
    lines.extend(f"    {status}" for status in data["analytical_evidence_status"])
    lines.append(f"imputation      {data['imputation_policy']}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.compare_thetadata_captures",
        description=(
            "Compare open-interest coverage between two certified captures. "
            "Offline; makes no network request."
        ),
    )
    parser.add_argument("earlier_capture_root", help="the earlier capture directory")
    parser.add_argument("later_capture_root", help="the later capture directory")
    parser.add_argument(
        "--earlier-archive-path",
        default="",
        help=(
            "the earlier capture's archive. Hashed, opened and checked to hold "
            "that capture before anything is compared"
        ),
    )
    parser.add_argument(
        "--later-archive-path",
        default="",
        help="the later capture's archive, verified the same way",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default="",
        help="write the full report here instead of stdout",
    )
    args = parser.parse_args(argv)

    try:
        report = compare_captures(
            pathlib.Path(args.earlier_capture_root),
            pathlib.Path(args.later_capture_root),
            earlier_archive=(
                pathlib.Path(args.earlier_archive_path)
                if args.earlier_archive_path
                else None
            ),
            later_archive=(
                pathlib.Path(args.later_archive_path)
                if args.later_archive_path
                else None
            ),
        )
    except (CaptureComparisonError, CaptureCertificationError) as error:
        print(f"captures cannot be compared: {error}", file=sys.stderr)
        return EXIT_UNCOMPARABLE

    rendered = _render(report)
    if args.json_path:
        pathlib.Path(args.json_path).write_text(rendered, encoding="utf-8")
        for line in _summarise(report):
            print(line)
        print(f"report          {args.json_path}")
    else:
        print(rendered)
    print(
        f"transition_report_hash  {report.transition_report_hash()}",
        file=sys.stderr,
    )
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
