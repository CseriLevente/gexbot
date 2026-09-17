"""Floor-aware evidence within a declared model grid, never trading authority."""

from __future__ import annotations

import math
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from typing import Any

from src.adapters.thetadata.capture_certification import (
    ADEQUATE_FIT_NOISE_FLOORS,
    DELTA_NOISE_FLOOR,
    MINIMUM_ROWS_FOR_INFERENCE,
    _set_hash,
    capture_universe,
    load_capture,
)
from src.adapters.thetadata.pricing_diagnostics import (
    HYPOTHESES,
    PricingDiagnosticsError,
    PricingRow,
    _errors,
    _metrics,
    _pricing_rows,
    diagnose_capture,
    model_inputs,
)
from src.domain.canonical import canonical_payload
from src.domain.digests import digest_of

SCHEMA_VERSION = "floor-aware-pricing-inference/2.1.32"
ALGORITHM_VERSION = "common-population-censoring/1"
VALIDATION_SCHEMA_VERSION = "pricing-inference-validation/2.1.32"
DIMENSIONS = tuple(asdict(HYPOTHESES[0]))


def _digest(report: dict[str, Any]) -> dict[str, Any]:
    return {**report, "report_hash": digest_of(canonical_payload(report))}


def screen_candidates(scores: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep every adequate near-best candidate, including exact ties.

    These are existing reconstruction heuristics, not confidence intervals.
    No threshold is estimated from the captures used to validate this release.
    """
    if not scores or len({s["rows"] for s in scores}) != 1:
        raise PricingDiagnosticsError(
            "candidate comparison needs one common population"
        )
    if len({s["id"] for s in scores}) != len(scores):
        raise PricingDiagnosticsError("duplicate candidate identifier")
    if any(not math.isfinite(s["delta_rmse"]) or s["delta_rmse"] < 0 for s in scores):
        raise PricingDiagnosticsError("invalid candidate score")
    ranked = sorted(scores, key=lambda s: (s["delta_rmse"], s["id"]))
    best = ranked[0]["delta_rmse"]
    adequate = ADEQUATE_FIT_NOISE_FLOORS * DELTA_NOISE_FLOOR
    status = (
        "INSUFFICIENT_DATA"
        if ranked[0]["rows"] < MINIMUM_ROWS_FOR_INFERENCE
        else "NO_ADEQUATE_FIT"
        if best > adequate
        else "ADEQUATE_CANDIDATE_SET"
    )
    supported = (
        [
            s["id"]
            for s in ranked
            if s["delta_rmse"] <= min(adequate, best + DELTA_NOISE_FLOOR)
        ]
        if status == "ADEQUATE_CANDIDATE_SET"
        else []
    )
    return {
        "status": status,
        "rows": ranked[0]["rows"],
        "best_delta_rmse": best,
        "adequacy_delta_rmse_limit": adequate,
        "near_best_delta_rmse_margin": DELTA_NOISE_FLOOR,
        "minimum_rows": MINIMUM_ROWS_FOR_INFERENCE,
        "supported_candidates": sorted(supported),
        "ranking": ranked,
    }


def clock_uncensored_rows(rows: tuple[PricingRow, ...]) -> tuple[PricingRow, ...]:
    """A common population nonbinding under *every* declared floored model.

    Rate and year denominator cannot change whether a minute floor binds.
    Remove duplicates in those dimensions to avoid repeating identical work.
    A floor-compatible observation can constrain a clock from above, but must
    not be inverted into an exact clock or labelled a whole-calendar-day rule.
    """
    time_models = {
        replace(h, rate_unit="DECIMAL", days_per_year=365.0)
        for h in HYPOTHESES
        if h.floor_minutes > 0
    }
    return tuple(
        row
        for row in rows
        if all(
            model_inputs(row, replace(h, floor_minutes=0), 0.0).time_to_expiry
            > h.floor_minutes / (1440.0 * h.days_per_year)
            for h in time_models
        )
    )


def _scores(rows: tuple[PricingRow, ...], wire_rate: float) -> list[dict[str, Any]]:
    return [
        {
            "id": h.identifier,
            "parameters": asdict(h),
            **_metrics(_errors(rows, h, wire_rate)),
        }
        for h in HYPOTHESES
    ]


def infer_rows(
    rows: tuple[PricingRow, ...],
    wire_rate: float,
) -> dict[str, Any]:
    """Numerical kernel. Its output alone carries no capture provenance."""
    if not rows or not math.isfinite(wire_rate):
        raise PricingDiagnosticsError("nonempty population and finite rate required")
    if len({r.key for r in rows}) != len(rows):
        raise PricingDiagnosticsError("duplicate pricing identity")
    rows = tuple(sorted(rows, key=lambda r: r.key))
    global_fit = screen_candidates(_scores(rows, wire_rate))
    uncensored = clock_uncensored_rows(rows)
    clock_fit = (
        screen_candidates(_scores(uncensored, wire_rate)) if uncensored else None
    )
    lookup = {h.identifier: asdict(h) for h in HYPOTHESES}
    dimensions: dict[str, Any] = {}
    for name in DIMENSIONS:
        values = sorted({lookup[i][name] for i in global_fit["supported_candidates"]})
        status = global_fit["status"]
        if status == "ADEQUATE_CANDIDATE_SET":
            status = "SUPPORTED_WITHIN_GRID" if len(values) == 1 else "AMBIGUOUS"
        if name == "floor_minutes" and len(uncensored) == len(rows) and values:
            status = "FLOOR_NOT_EXERCISED"
        if name == "clock_hour_et" and values:
            if clock_fit is None:
                status = "FLOOR_CENSORED"
                values = []
            elif clock_fit["status"] != "ADEQUATE_CANDIDATE_SET":
                status = clock_fit["status"]
                values = []
            else:
                # Both comparisons must agree. Union rather than intersection
                # prevents floor-bound rows from deleting an uncensored alternative.
                clock_values = {
                    lookup[i][name] for i in clock_fit["supported_candidates"]
                }
                values = sorted(set(values) | clock_values)
                status = "SUPPORTED_WITHIN_GRID" if len(values) == 1 else "AMBIGUOUS"
        dimensions[name] = {
            "status": status,
            "supported_values": values,
            "selected_value": values[0] if status == "SUPPORTED_WITHIN_GRID" else None,
        }
    return {
        "dimensions": dimensions,
        "global_fit": global_fit,
        "clock_fit_on_common_uncensored_rows": clock_fit,
        "censoring": {
            "potentially_floor_binding_rows": len(rows) - len(uncensored),
            "common_uncensored_rows": len(uncensored),
            "uncensored_identity_hash": _set_hash({r.key for r in uncensored}),
            "policy": "Exclude from clock comparison any row floored by any grid candidate.",
            "interpretation": "T_effective=max(T_raw,floor); a binding observation gives a bound, not an exact clock.",
        },
        "exact_expiration_clock_identified": False,
        "full_vendor_model_identified": False,
        "selected_vendor_model": None,
    }


def infer_capture(
    root: Path | str, *, archive_path: Path | str | None = None
) -> dict[str, Any]:
    diagnostic = diagnose_capture(root, archive_path=archive_path)
    capture = load_capture(root)
    if (capture.session_id, capture.manifest_hash, capture.run_intent_sha256) != (
        diagnostic["capture"]["session_id"],
        diagnostic["capture"]["manifest_hash"],
        diagnostic["capture"]["run_intent_sha256"],
    ):
        raise PricingDiagnosticsError("capture changed after diagnostic verification")
    universe = capture_universe(capture)
    rows, _ = _pricing_rows(
        capture, universe, date.fromisoformat(universe.session_date)
    )
    if (
        _set_hash({r.key for r in rows})
        != diagnostic["pricing"]["common_identity_hash"]
    ):
        raise PricingDiagnosticsError("pricing population changed after verification")
    return _digest(
        {
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "source_diagnostic_report_hash": diagnostic["diagnostic_report_hash"],
            "capture": diagnostic["capture"],
            "request": diagnostic["request"],
            "pricing_admission": diagnostic["pricing_admission"],
            "coverage": diagnostic["coverage"],
            "inference": infer_rows(rows, capture.request.greeks_rate_value),
            "trusted_for_gex": False,
            "gex_computed": False,
            "orders_placed": 0,
            "historical_certification_blockers": diagnostic[
                "historical_certification_blockers"
            ],
            "limitations": [
                "Support is conditional on the fixed 192-candidate grid and zero-dividend assumption.",
                "Fit thresholds are inherited heuristics, not statistical confidence intervals.",
                "Clock support concerns tested hours only, not an exact clock or market schedule.",
                "IV solver/price basis, independent exposure coverage and profitability remain outside this inference.",
            ],
        }
    )


def validate_captures(inputs: tuple[tuple[Path, Path], ...]) -> dict[str, Any]:
    """Recompute each report from verified bytes; never accept caller-built reports."""
    if len(inputs) < 2:
        raise PricingDiagnosticsError(
            "at least two independently dated captures required"
        )
    reports = [infer_capture(root, archive_path=archive) for root, archive in inputs]
    dates = [r["capture"]["market_session_date"] for r in reports]
    identities = [r["capture"]["manifest_hash"] for r in reports]
    if len(set(dates)) != len(dates) or len(set(identities)) != len(identities):
        raise PricingDiagnosticsError("duplicate session date or capture identity")
    reports.sort(key=lambda r: r["capture"]["market_session_date"])
    replicated = {}
    for name in DIMENSIONS:
        decisions = [r["inference"]["dimensions"][name] for r in reports]
        resolved = all(d["status"] == "SUPPORTED_WITHIN_GRID" for d in decisions)
        values = {d["selected_value"] for d in decisions}
        replicated[name] = {
            "status": "REPLICATED_WITHIN_GRID"
            if resolved and len(values) == 1
            else "UNRESOLVED_ACROSS_CAPTURES",
            "selected_value": next(iter(values))
            if resolved and len(values) == 1
            else None,
        }
    supports = [
        set(r["inference"]["global_fit"]["supported_candidates"]) for r in reports
    ]
    common = sorted(set.intersection(*supports))
    return _digest(
        {
            "schema_version": VALIDATION_SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "capture_count": len(reports),
            "capture_reports": reports,
            "replicated_dimensions": replicated,
            "common_global_candidates": common,
            "common_candidate_status": "COMMON_CANDIDATES_EXIST"
            if common
            else "NO_COMMON_CANDIDATE",
            "full_vendor_model_identified": False,
            "trusted_for_gex": False,
            "gex_computed": False,
            "orders_placed": 0,
            "interpretation": "Retrospective cross-capture replication; not prospective validation or a trading backtest.",
        }
    )
