"""Certify an immutable ThetaData capture, offline.

    python -m src.tools.certify_thetadata_capture <capture-root>

Makes no network request. It reads a capture directory, re-verifies every raw
payload against the manifest that describes it, and derives what the vendor's
implementation actually does from the bytes -- rate semantics, day count, the
expiration clock, the implied-volatility price basis, the underlying the Greeks
were computed against, universe coverage and open-interest coverage.

The output is content-addressed. Two runs over the same untouched capture
produce the same ``report_hash``; a run over an edited capture does not, and a
run over a capture whose payloads no longer match their manifest hashes refuses
before it computes anything.

Exit codes:

===  ======================================================================
0    the capture was certified; the report was produced
2    the capture could not be read or verified
3    the capture was certified and carries a documentation/live conflict
===  ======================================================================

Exit 3 is not a failure. It is the state the first capture is in, and it is
reported separately so a script cannot mistake "certified, and the vendor's
documentation is wrong" for "certified, everything agrees".
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Sequence
from typing import Any

from src.adapters.thetadata.capture_certification import (
    CaptureCertificationError,
    CaptureCertificationReport,
    certify_capture,
)

EXIT_OK = 0
EXIT_UNREADABLE = 2
EXIT_CONFLICT = 3


def _render(report: CaptureCertificationReport) -> str:
    payload: dict[str, Any] = report.as_dict()
    payload["report_hash"] = report.report_hash()
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _summarise(report: CaptureCertificationReport) -> list[str]:
    """The report as an operator reads it. Returned rather than printed.

    A function that formats and emits cannot be checked without capturing
    stdout, and this is the output somebody decides whether to spend money on.
    """
    data = report.as_dict()
    cap = data["capture"]
    request = data["capture_request"]
    economics = data["rate_economics"]
    lines = [
        f"capture         {cap['session_id']}",
        f"manifest        {cap['manifest_hash']} (recomputed from descriptors)",
        f"archive         {cap['archive_sha256'] or '<not computed>'}",
        f"records         {data['raw_record_verification']['records_verified']} verified",
        f"request         rate_value={request['greeks_rate_value']:g} "
        f"({request['binding']})",
        f"vendor rate     r={economics['vendor_effective_rate']:g} "
        f"| intended r={economics['intended_economic_rate']:g} "
        f"({economics['intended_rate_source']})",
        f"rate verdict    documentation conflict="
        f"{economics['rate_units_documentation_live_conflict']} | "
        f"economically correct="
        f"{economics['capture_effective_rate_matches_intended_rate']}",
    ]
    universe = data["universe"]
    lines.append(
        f"universe        list {universe['contract_list_count']:,} | quote "
        f"{universe['quote_count']:,} | greeks {universe['greeks_count']:,} "
        f"-> {universe['state']}"
    )
    oi = data["open_interest_coverage"]
    lines.append(
        f"open interest   present {oi['oi_present']:,} | explicit zero "
        f"{oi['oi_explicit_zero']:,} | missing {oi['oi_missing']:,} "
        f"({oi['coverage_ratio'] * 100:.3f}% covered)"
    )
    if oi["fully_missing_expirations"]:
        lines.append(
            "                expirations with no open interest at all: "
            + ", ".join(oi["fully_missing_expirations"])
        )
    for label, key in (
        ("rate semantics", "rate_semantics"),
        ("day count", "day_count_comparison"),
        ("expiration time", "expiration_time_comparison"),
    ):
        lines.append(f"{label}:")
        lines.extend(
            f"    {row['hypothesis']:<48} rows {row['rows']:>6,}  "
            f"RMSE {row['delta_rmse']:.6g}"
            for row in data[key]
        )
    lines.append("iv price basis:")
    lines.extend(
        f"    {row['basis']:<48} rows {row['rows']:>6,}  "
        f"median |IV err| {row['median_abs_iv_error']:.6g}"
        for row in data["iv_basis_comparison"]
    )
    clock = data["expiration_clock_evidence"]
    lines.append(
        f"resolved        day count {data['resolved_day_count']} | clock "
        f"{data['resolved_expiration_clock']} (estimate "
        f"{clock['implied_clock_et'] or 'n/a'})"
    )
    lines.append(
        f"clock scope     {clock['intraday_count']} expirations support it, "
        f"{clock['contradicting_count']} contradict a global rule; transition "
        f"{clock['boundary_status']}"
        + (
            f" between {clock['boundary_last_intraday']} and "
            f"{clock['boundary_first_whole_day']} "
            f"({clock['boundary_gap_days']}d unsampled)"
            if clock["boundary_status"] == "OPEN"
            else ""
        )
    )
    conflicts = ", ".join(data["documentation_live_conflicts"]) or "none"
    lines.append(f"conflicts:      {conflicts}")
    lines.append("resolved:       " + ", ".join(data["dimensions_resolved"]))
    lines.append(
        "unresolved:     " + (", ".join(data["dimensions_unresolved"]) or "none")
    )
    lines.append(f"readiness       {data['analytical_readiness']}")
    lines.append(f"trusted for gex {data['trusted_for_gex']}")
    lines.extend(f"    blocked by  {reason}" for reason in data["gex_blockers"])
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.certify_thetadata_capture",
        description="Certify a raw ThetaData capture offline.",
    )
    parser.add_argument("capture_root", help="the directory a capture run created")
    parser.add_argument(
        "--archive-sha256",
        default="",
        help=(
            "digest of the archive the capture was distributed as, recorded in "
            "the report so a reader can tie it to the artefact they hold"
        ),
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default="",
        help="write the full report here instead of stdout",
    )
    args = parser.parse_args(argv)

    try:
        report = certify_capture(
            pathlib.Path(args.capture_root), archive_sha256=args.archive_sha256
        )
    except CaptureCertificationError as error:
        print(f"capture cannot be certified: {error}", file=sys.stderr)
        return EXIT_UNREADABLE

    rendered = _render(report)
    if args.json_path:
        pathlib.Path(args.json_path).write_text(rendered, encoding="utf-8")
        for line in _summarise(report):
            print(line)
        print(f"report          {args.json_path}")
    else:
        print(rendered)
    print(f"report_hash     {report.report_hash()}", file=sys.stderr)

    return EXIT_CONFLICT if report.ledger.conflicts else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
