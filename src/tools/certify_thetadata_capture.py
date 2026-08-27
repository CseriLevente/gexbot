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

from src.adapters.thetadata.analytical_universe import (
    AnalyticalUniverseReport,
    analytical_universe_of,
)
from src.adapters.thetadata.capture_certification import (
    CaptureCertificationError,
    CaptureCertificationReport,
    capture_universe,
    certify_capture,
    load_capture,
)

EXIT_OK = 0
EXIT_UNREADABLE = 2
EXIT_CONFLICT = 3


def _render(
    report: CaptureCertificationReport,
    universe: AnalyticalUniverseReport | None = None,
) -> str:
    """The certification report, plus what a later analytical layer may use.

    The two are separate objects with separate digests on purpose. Certification
    answers what these bytes are and what they say about the vendor's
    conventions; the analytical-universe partition answers which of the
    contracts in them belong to the session the capture is of. Nesting the
    second under its own key leaves ``report_hash`` covering exactly what it
    covered before -- a capture certified under an earlier release still
    reproduces its digest.
    """
    payload: dict[str, Any] = report.as_dict()
    payload["report_hash"] = report.report_hash()
    if universe is not None:
        payload["analytical_universe"] = {
            **universe.as_dict(),
            "analytical_universe_report_hash": universe.report_hash(),
        }
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

    def _tri(value: object) -> str:
        """``None`` prints as an explicit non-answer, never as ``False``."""
        return "not-established" if value is None else str(value)

    effective = economics["vendor_effective_rate"]
    lines = [
        f"capture         {cap['session_id']}",
        f"manifest        {cap['manifest_hash']} (recomputed from descriptors)",
        f"archive         {cap['archive_sha256'] or '<not computed>'}"
        f" (identity={cap['archive_identity_known']}, "
        f"matches capture={cap['archive_matches_capture']})",
        f"documentation   {data['documentary_evidence']['documentary_authority']}"
        + (
            f" [{', '.join(data['documentary_evidence']['rules_rederived'])}]"
            if data["documentary_evidence"]["rules_rederived"]
            else ""
        ),
        f"records         {data['raw_record_verification']['records_verified']} verified",
        f"request         rate_value={request['greeks_rate_value']:g} "
        f"({request['binding']})",
        f"vendor rate     r={'not-established' if effective is None else f'{effective:g}'} "
        f"| intended r={economics['intended_economic_rate']:g} "
        f"({economics['intended_rate_source']})",
        f"rate verdict    documentation conflict="
        f"{_tri(economics['rate_units_documentation_live_conflict'])} | "
        f"economically correct="
        f"{_tri(economics['capture_effective_rate_matches_intended_rate'])}",
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
        f"{oi['oi_explicit_zero']:,} | missing {oi['oi_missing']:,} | "
        f"unexpected {oi['unexpected_oi_count']:,} "
        f"({oi['coverage_ratio'] * 100:.3f}% covered) -> {oi['coverage_state']}"
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
    lines.append(
        "inference:      "
        + " | ".join(
            f"{name} {verdict}"
            for name, verdict in sorted(data["inference_decisions"].items())
        )
    )
    models = data["inference_models"]
    selected = models["resolved_selected"]
    lines.append(
        f"model           best fit "
        f"{models['numerical_best']['rate_interpretation']}/"
        f"{models['numerical_best']['day_count']} | selected "
        + (
            f"{selected['rate_interpretation']}/{selected['day_count']}"
            if selected
            else f"none ({models['admissible_model_count']} admissible)"
        )
    )
    roots = data["root_resolution"]
    lines.append(
        f"root choice     {roots['single_root_rows']} single, "
        f"{roots['disambiguated_multi_root_rows']} disambiguated, "
        f"{roots['ambiguous_root_rows']} ambiguous, "
        f"{roots['inconsistent_root_rows']} inconsistent"
    )
    binding = data["rate_intent_binding"]
    lines.append(f"rate intent     bound={binding['bound']} ({binding['source']})")
    conflicts = ", ".join(data["documentation_live_conflicts"]) or "none"
    lines.append(f"conflicts:      {conflicts}")
    lines.append("behaviour:      " + ", ".join(data["behavior_dimensions_resolved"]))
    lines.append(
        "  unresolved:   "
        + (", ".join(data["behavior_dimensions_unresolved"]) or "none")
    )
    lines.append(
        "pricing:        "
        + (", ".join(data["pricing_dimensions_supported_by_evidence"]) or "none")
    )
    lines.append(
        "  unresolved:   "
        + (", ".join(data["pricing_dimensions_still_unresolved"]) or "none")
    )
    lines.append(f"readiness       {data['analytical_readiness']}")
    lines.append(f"trusted for gex {data['trusted_for_gex']}")
    lines.extend(f"    blocked by  {reason}" for reason in data["gex_blockers"])
    return lines


def _summarise_universe(universe: AnalyticalUniverseReport) -> list[str]:
    """The temporal and open-interest partition, as an operator reads it."""
    data = universe.as_dict()
    counts = data["class_counts"]
    lines = [
        f"analysable      session {data['market_session_date']} "
        f"({data['market_session_source']}, capture clock agrees="
        f"{data['sessions_agree']})",
        f"                {data['eligible_count']:,} of {data['listed_count']:,} "
        f"listed identities are eligible "
        f"({counts['ELIGIBLE_CURRENT']:,} current, "
        f"{counts['ELIGIBLE_EXPIRING_THIS_SESSION']:,} expiring this session)",
        f"                excluded: "
        f"{counts['EXCLUDED_EXPIRED_BEFORE_SESSION']:,} expired before the "
        f"session, {counts['EXCLUDED_OPEN_INTEREST_NOT_REPORTED']:,} with no "
        "open-interest record (not zero -- unavailable)",
        f"                trusted analytical universe="
        f"{data['permits_trusted_analytical_universe']}",
    ]
    lines.extend(
        f"    not usable  {reason}" for reason in data["analytical_universe_blockers"]
    )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.certify_thetadata_capture",
        description="Certify a raw ThetaData capture offline.",
    )
    parser.add_argument("capture_root", help="the directory a capture run created")
    parser.add_argument(
        "--archive-path",
        default="",
        help=(
            "the archive the capture was distributed as. Hashed here, opened, "
            "and checked to hold this capture's manifest, its run intent and "
            "every raw payload the manifest names. This is the only way an "
            "archive digest becomes the capture's archive identity"
        ),
    )
    parser.add_argument(
        "--archive-sha256",
        default="",
        help=(
            "a digest computed somewhere else. On its own it is recorded as an "
            "unverified external claim and never as identity; alongside "
            "--archive-path it is checked against the bytes"
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
            pathlib.Path(args.capture_root),
            archive_path=(
                pathlib.Path(args.archive_path) if args.archive_path else None
            ),
            archive_sha256=args.archive_sha256,
        )
    except CaptureCertificationError as error:
        print(f"capture cannot be certified: {error}", file=sys.stderr)
        return EXIT_UNREADABLE

    # The capture certified, so its bytes are what they claim to be. Which of
    # the contracts in them this session's analysis may use is a second
    # question, answered by a second report over the same verified universe.
    root = pathlib.Path(args.capture_root)
    universe = analytical_universe_of(capture_universe(load_capture(root)))

    rendered = _render(report, universe)
    if args.json_path:
        pathlib.Path(args.json_path).write_text(rendered, encoding="utf-8")
        for line in _summarise(report):
            print(line)
        for line in _summarise_universe(universe):
            print(line)
        print(f"report          {args.json_path}")
    else:
        print(rendered)
    print(f"report_hash     {report.report_hash()}", file=sys.stderr)
    print(
        f"universe_hash   {universe.report_hash()}",
        file=sys.stderr,
    )

    return EXIT_CONFLICT if report.ledger.conflicts else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
