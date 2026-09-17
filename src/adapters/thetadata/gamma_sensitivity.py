"""Retrospective per-contract gamma ranges, not GEX or a trading signal."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any

from src.adapters.thetadata.capture_certification import (
    _set_hash,
    capture_universe,
    load_capture,
)
from src.adapters.thetadata.floor_inference import validate_captures
from src.adapters.thetadata.pricing_diagnostics import (
    HYPOTHESES,
    PricingDiagnosticsError,
    PricingHypothesis,
    PricingRow,
    _pricing_rows,
    model_inputs,
)
from src.domain.canonical import canonical_payload
from src.domain.digests import digest_of
from src.gex.pricing import gamma

SCHEMA_VERSION = "gamma-model-sensitivity/2.1.33"


def gamma_ranges(
    rows: tuple[PricingRow, ...],
    models: tuple[PricingHypothesis, ...],
    wire_rate: float,
) -> dict[str, Any]:
    """Same identities for every model; unweighted ranges are not exposure risk."""
    if (
        not math.isfinite(wire_rate)
        or not models
        or len(set(models)) != len(models)
        or any(h not in HYPOTHESES for h in models)
    ):
        raise PricingDiagnosticsError("finite rate and distinct models required")
    if len({r.key for r in rows}) != len(rows):
        raise PricingDiagnosticsError("duplicate sensitivity identity")
    ranges: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: r.key):
        inputs = [
            model_inputs(row, h, wire_rate)
            for h in sorted(models, key=lambda h: h.identifier)
        ]
        if any(
            not all(
                math.isfinite(v)
                for v in (
                    x.spot,
                    x.strike,
                    x.implied_vol,
                    x.time_to_expiry,
                    x.rate,
                    x.dividend_yield,
                )
            )
            or x.is_degenerate()
            for x in inputs
        ):
            raise PricingDiagnosticsError("invalid gamma inputs")
        values = [gamma(x) for x in inputs]
        if any(not math.isfinite(v) or v < 0 for v in values):
            raise PricingDiagnosticsError("invalid gamma result")
        low, high = min(values), max(values)
        ranges.append(
            {
                "symbol": row.key.symbol,
                "expiration": row.key.expiration.isoformat(),
                "strike": row.key.strike,
                "right": row.key.right,
                "minimum_gamma": low,
                "maximum_gamma": high,
                "absolute_range": high - low,
                "range_percent_of_maximum": 100 * (high - low) / high if high else None,
            }
        )
    relative = sorted(
        r["range_percent_of_maximum"]
        for r in ranges
        if r["range_percent_of_maximum"] is not None
    )
    largest = sorted(
        ranges,
        key=lambda r: (
            -(r["range_percent_of_maximum"] or 0),
            r["expiration"],
            r["strike"],
            r["right"],
        ),
    )[:10]
    return {
        "rows": len(rows),
        "identity_hash": _set_hash({r.key for r in rows}),
        "model_count": len(models),
        "status": "NO_ROWS"
        if not rows
        else "SINGLE_MODEL"
        if len(models) == 1
        else "COMPARISON",
        "all_zero_gamma_rows": len(rows) - len(relative),
        "median_range_percent": median(relative) if relative else None,
        "p95_range_percent": relative[math.ceil(0.95 * len(relative)) - 1]
        if relative
        else None,
        "maximum_range_percent": max(relative) if relative else None,
        "maximum_absolute_gamma_range": max(
            (r["absolute_range"] for r in ranges), default=None
        ),
        "largest_relative_ranges": largest,
        "units": "delta change per one index point; no OI or contract multiplier applied",
    }


def analyze_captures(inputs: tuple[tuple[Path, Path], ...]) -> dict[str, Any]:
    validation = validate_captures(inputs)
    model_ids = validation["common_global_candidates"]
    models = tuple(h for h in HYPOTHESES if h.identifier in model_ids)
    captures = []
    reports = {r["capture"]["manifest_hash"]: r for r in validation["capture_reports"]}
    for root, _archive in inputs:
        capture = load_capture(root)
        prior = reports.get(capture.manifest_hash)
        if prior is None or any(
            getattr(capture, key) != prior["capture"][key]
            for key in ("session_id", "run_intent_sha256")
        ):
            raise PricingDiagnosticsError("capture changed after model verification")
        universe = capture_universe(capture)
        session = date.fromisoformat(universe.session_date)
        rows, _ = _pricing_rows(capture, universe, session)
        available = tuple(r for r in rows if r.key in universe.answered)
        if (
            _set_hash({r.key for r in available})
            != prior["pricing_admission"]["oi_available_pricing_subset_hash"]
        ):
            raise PricingDiagnosticsError("OI-available population changed")
        groups: dict[str, list[PricingRow]] = defaultdict(list)
        for label in ("0DTE", "0_TO_7DTE", "0_TO_60DTE"):
            groups[label] = []
        for row in available:
            dte = (row.key.expiration - session).days
            for label, maximum in (("0DTE", 0), ("0_TO_7DTE", 7), ("0_TO_60DTE", 60)):
                if 0 <= dte <= maximum:
                    groups[label].append(row)
            groups["EXPIRATION_" + row.key.expiration.isoformat()].append(row)
        captures.append(
            {
                "capture": prior["capture"],
                "source_inference_report_hash": prior["report_hash"],
                "pricing_rows": len(rows),
                "oi_available_pricing_rows": len(available),
                "excluded_missing_oi_pricing_rows": len(rows) - len(available),
                "coverage": prior["coverage"],
                "ranges": {
                    label: gamma_ranges(
                        tuple(group), models, capture.request.greeks_rate_value
                    )
                    for label, group in sorted(groups.items())
                }
                if models
                else {},
            }
        )
    captures.sort(key=lambda c: c["capture"]["market_session_date"])
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_validation_report_hash": validation["report_hash"],
        "interpretation": "RETROSPECTIVE_MODEL_SENSITIVITY_ONLY",
        "model_ids": model_ids,
        "status": "NO_COMMON_MODELS"
        if not models
        else "SINGLE_MODEL"
        if len(models) == 1
        else "COMPARISON",
        "capture_reports": captures,
        "gamma_computed": any(
            any(s["rows"] for s in c["ranges"].values()) for c in captures
        ),
        "gex_computed": False,
        "trusted_for_gex": False,
        "orders_placed": 0,
        "limitations": [
            "The finite retained grid does not bound uncertainty from untested models.",
            "Models are selected using all supplied dates; these are not point-in-time trading features.",
            "Observed IV is held fixed across candidates; IV solver and price-basis uncertainty remain outside the range.",
            "Relative ranges are unweighted; tiny gamma can have a large percentage change.",
            "Missing OI and inadmissible Greeks are excluded, not zero-filled. Exposure completeness is unknown.",
            "No materiality cutoff, dealer inventory, signed GEX, walls, PnL or profitability is inferred.",
        ],
    }
    return {**payload, "report_hash": digest_of(canonical_payload(payload))}
