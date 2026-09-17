"""Direct, floor-aware delta comparisons on verified SPXW captures.

This is an exploratory diagnostic, not a vendor-model selector. All candidates
use the same rows and the existing Black-Scholes implementation. It neither
changes historical certification nor computes GEX or authorizes a trade.
"""

from __future__ import annotations

import math
import pathlib
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time
from itertools import product
from statistics import median
from typing import Any

from src.adapters.thetadata.analytical_universe import analytical_universe_of
from src.adapters.thetadata.capture_certification import (
    EASTERN,
    OPTION_GREEKS,
    CaptureUniverse,
    ContractKey,
    LoadedCapture,
    _set_hash,
    capture_universe,
    certify_capture,
    load_capture,
)
from src.domain.canonical import CANONICAL_REPORT_SCHEMA_VERSION, canonical_payload
from src.domain.digests import digest_of
from src.gex.pricing import MAX_IMPLIED_VOL, MIN_IMPLIED_VOL, BlackScholesInputs, delta

SCHEMA_VERSION = "pricing-diagnostics/2.1.31"
ALGORITHM_VERSION = "direct-delta-grid/1"


class PricingDiagnosticsError(ValueError):
    """Inputs cannot support the declared comparison."""


@dataclass(frozen=True, slots=True)
class PricingHypothesis:
    rate_unit: str
    days_per_year: float
    clock_hour_et: int
    time_rule: str
    timestamp_source: str
    floor_minutes: int

    @property
    def identifier(self) -> str:
        return (
            f"{self.rate_unit}/{self.days_per_year:g}/{self.clock_hour_et:02}:00/"
            f"{self.time_rule}/{self.timestamp_source}/floor={self.floor_minutes}m"
        )


# Fixed before this command runs. No fitted expiration clocks or tuned rates.
# 17:00 is a diagnostic clock perturbation, NOT the SPXW market close.
HYPOTHESES = tuple(
    PricingHypothesis(*values)
    for values in product(
        ("DECIMAL", "PERCENT"),
        (365.0, 365.25, 360.0, 252.0),
        (16, 17),
        ("CONTINUOUS", "SHORT_LT7", "SHORT_LE7"),
        ("UNDERLYING", "OPTION"),
        (0, 60),
    )
)


@dataclass(frozen=True, slots=True)
class PricingRow:
    key: ContractKey
    spot: float
    implied_vol: float
    observed_delta: float
    underlying_at: datetime
    option_at: datetime

    @property
    def moneyness_bucket(self) -> str:
        ratio = float(self.key.strike) / self.spot
        if ratio < 0.95:
            return "STRIKE_BELOW_95_PERCENT_SPOT"
        if ratio > 1.05:
            return "STRIKE_ABOVE_105_PERCENT_SPOT"
        return "STRIKE_95_TO_105_PERCENT_SPOT"


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return (
        parsed.replace(tzinfo=EASTERN)
        if parsed.tzinfo is None
        else parsed.astimezone(EASTERN)
    )


def _pricing_rows(
    capture: LoadedCapture, universe: CaptureUniverse, session: date
) -> tuple[tuple[PricingRow, ...], dict[str, int]]:
    selected: list[PricingRow] = []
    exclusions: Counter[str] = Counter()
    seen: set[ContractKey] = set()
    for raw in capture.rows(OPTION_GREEKS):
        key = ContractKey.from_row(raw)
        if key in seen or key not in universe.expected:
            raise PricingDiagnosticsError("duplicate or unlisted Greeks identity")
        seen.add(key)
        if key.expiration < session:
            exclusions["EXPIRED_BEFORE_SESSION"] += 1
            continue
        try:
            spot = float(raw["underlying_price"])
            sigma = float(raw["implied_vol"])
            observed = float(raw["delta"])
            strike = float(key.strike)
            underlying_at = _timestamp(raw["underlying_timestamp"])
            option_at = _timestamp(raw["timestamp"])
        except (ValueError, KeyError, OverflowError):
            exclusions["MALFORMED_PRICING_INPUT"] += 1
            continue
        if not all(math.isfinite(v) for v in (spot, sigma, observed, strike)):
            exclusions["NONFINITE_PRICING_INPUT"] += 1
            continue
        if spot <= 0 or strike <= 0 or not MIN_IMPLIED_VOL <= sigma <= MAX_IMPLIED_VOL:
            exclusions["DEGENERATE_PRICING_INPUT"] += 1
            continue
        if not (
            (key.right == "CALL" and 0 < observed < 1)
            or (key.right == "PUT" and -1 < observed < 0)
        ):
            exclusions["SATURATED_OR_INVALID_DELTA"] += 1
            continue
        # Uniform admission for every candidate. Flooring must never revive an
        # expired series, or mask a stale quote as a current observation.
        if any(
            stamp.date() != session or stamp.time() >= time(16)
            for stamp in (underlying_at, option_at)
        ):
            exclusions["STALE_OR_AFTER_CLOSE_TIMESTAMP"] += 1
            continue
        selected.append(
            PricingRow(key, spot, sigma, observed, underlying_at, option_at)
        )
    if seen != universe.expected:
        raise PricingDiagnosticsError("Greeks and listed identity sets differ")
    if not selected:
        raise PricingDiagnosticsError("no comparable pricing rows")
    return tuple(sorted(selected, key=lambda row: row.key)), dict(
        sorted(exclusions.items())
    )


def model_inputs(
    row: PricingRow, hypothesis: PricingHypothesis, wire_rate: float
) -> BlackScholesInputs:
    """No inversion, no inferred boundary; the candidate supplies every choice."""
    stamp = (
        row.underlying_at
        if hypothesis.timestamp_source == "UNDERLYING"
        else row.option_at
    )
    calendar_days = (row.key.expiration - stamp.date()).days
    end = datetime.combine(row.key.expiration, time(hypothesis.clock_hour_et), EASTERN)
    # UTC subtraction counts elapsed time across a DST change.
    days = (end.timestamp() - stamp.timestamp()) / 86400.0
    if (hypothesis.time_rule == "SHORT_LT7" and calendar_days >= 7) or (
        hypothesis.time_rule == "SHORT_LE7" and calendar_days > 7
    ):
        days = float(calendar_days)
    days = max(days, hypothesis.floor_minutes / 1440.0)
    rate = wire_rate if hypothesis.rate_unit == "DECIMAL" else wire_rate / 100.0
    return BlackScholesInputs(
        row.spot,
        float(row.key.strike),
        days / hypothesis.days_per_year,
        row.implied_vol,
        rate,
        dividend_yield=0.0,
    )


def _errors(
    rows: tuple[PricingRow, ...], hypothesis: PricingHypothesis, wire_rate: float
) -> list[float]:
    return [
        delta(model_inputs(row, hypothesis, wire_rate), row.key.option_right)
        - row.observed_delta
        for row in rows
    ]


def _metrics(errors: list[float]) -> dict[str, Any]:
    absolute = sorted(abs(value) for value in errors)
    count = len(errors)
    return {
        "rows": count,
        "delta_rmse": math.sqrt(math.fsum(v * v for v in errors) / count),
        "mean_signed_delta_error": math.fsum(errors) / count,
        "median_absolute_delta_error": median(absolute),
        "p95_absolute_delta_error": absolute[math.ceil(0.95 * count) - 1],
        "max_absolute_delta_error": absolute[-1],
    }


def coverage_breakdown(universe: CaptureUniverse, session: date) -> dict[str, Any]:
    """Contract-count coverage only. Never describe it as gamma coverage."""
    per_expiration: list[dict[str, Any]] = []
    current_listed = current_answered = 0
    for expiry in sorted({key.expiration for key in universe.expected}):
        keys = {key for key in universe.expected if key.expiration == expiry}
        counts = Counter(universe.state_of(key) for key in keys)
        answered = counts["OI_ZERO"] + counts["OI_POSITIVE"]
        current = expiry >= session
        if current:
            current_listed += len(keys)
            current_answered += answered
        per_expiration.append(
            {
                "expiration": expiry.isoformat(),
                "current_session_eligible_date": current,
                "listed": len(keys),
                "positive": counts["OI_POSITIVE"],
                "explicit_zero": counts["OI_ZERO"],
                "missing": counts["OI_MISSING"],
                "answered_fraction": answered / len(keys),
            }
        )
    return {
        "listed": len(universe.expected),
        "expired_before_session": len(universe.expected) - current_listed,
        "current_listed": current_listed,
        "current_oi_answered": current_answered,
        "current_oi_missing": current_listed - current_answered,
        "current_contract_count_coverage": (
            current_answered / current_listed if current_listed else None
        ),
        "unexpected_oi_identities": len(universe.unexpected),
        "gamma_weighted_coverage": None,
        "missing_oi_policy": "UNAVAILABLE; EXCLUDE; NEVER IMPUTE ZERO",
        "per_expiration": per_expiration,
    }


def _grid_report(rows: tuple[PricingRow, ...], wire_rate: float) -> dict[str, Any]:
    scores = []
    for hypothesis in HYPOTHESES:
        errors = _errors(rows, hypothesis, wire_rate)
        scores.append(
            {
                "id": hypothesis.identifier,
                "parameters": asdict(hypothesis),
                **_metrics(errors),
            }
        )
    scores.sort(key=lambda score: (score["delta_rmse"], score["id"]))
    best = next(h for h in HYPOTHESES if h.identifier == scores[0]["id"])
    # A controlled contrast: exactly the same rows and all other parameters.
    counterfactual = replace(best, floor_minutes=60 if best.floor_minutes == 0 else 0)
    best_errors = _errors(rows, best, wire_rate)
    other_errors = _errors(rows, counterfactual, wire_rate)
    grouped: dict[tuple[str, str], tuple[list[float], list[float]]] = defaultdict(
        lambda: ([], [])
    )
    for row, error, other in zip(rows, best_errors, other_errors, strict=True):
        for dimension, value in (
            ("expiration", row.key.expiration.isoformat()),
            ("moneyness", row.moneyness_bucket),
        ):
            grouped[dimension, value][0].append(error)
            grouped[dimension, value][1].append(other)
    return {
        "candidate_count": len(HYPOTHESES),
        "common_row_count": len(rows),
        "common_identity_hash": _set_hash({row.key for row in rows}),
        "ranking": scores,
        "numerical_best_candidate": best.identifier,
        "vendor_model_identified": False,
        "selected_vendor_model": None,
        "interpretation": (
            "Exploratory in-sample ranking. No acceptance threshold is applied; "
            "ranking does not resolve the vendor model or grant analytical trust."
        ),
        "floor_contrast": {
            "best": {"id": best.identifier, **_metrics(best_errors)},
            "same_parameters_other_floor": {
                "id": counterfactual.identifier,
                **_metrics(other_errors),
            },
        },
        "breakdowns": [
            {
                "dimension": dimension,
                "value": value,
                "best": _metrics(errors),
                "same_parameters_other_floor": _metrics(other),
            }
            for (dimension, value), (errors, other) in sorted(grouped.items())
        ],
    }


def diagnose_capture(
    root: pathlib.Path | str, *, archive_path: pathlib.Path | str | None = None
) -> dict[str, Any]:
    """Verify evidence, then describe a fixed grid without mutating that evidence."""
    certification = certify_capture(root, archive_path=archive_path)
    if archive_path is not None and not certification.archive.known:
        raise PricingDiagnosticsError(
            "supplied archive does not verify against capture"
        )
    capture = load_capture(root)
    if (capture.session_id, capture.manifest_hash) != (
        certification.session_id,
        certification.manifest_hash,
    ):
        raise PricingDiagnosticsError("capture changed after certification")
    universe = capture_universe(capture)
    analytical = analytical_universe_of(universe)
    if not analytical.sessions_agree or not analytical.accounting_is_exhaustive:
        raise PricingDiagnosticsError("session or analytical accounting does not agree")
    if universe.symbol != "SPXW":
        raise PricingDiagnosticsError("this diagnostic grid is scoped to SPXW")
    raw_dividend = capture.request.value_for(OPTION_GREEKS, "annual_dividend")
    if raw_dividend is None or float(raw_dividend) != 0.0:
        raise PricingDiagnosticsError("explicit zero annual_dividend request required")
    wire_rate = capture.request.greeks_rate_value
    if not math.isfinite(wire_rate):
        raise PricingDiagnosticsError("nonfinite rate_value")
    if certification.open_interest.duplicate_count or universe.unexpected:
        raise PricingDiagnosticsError("duplicate or unlisted OI identity")
    rows, exclusions = _pricing_rows(capture, universe, analytical.market_session_date)
    pricing_keys = {row.key for row in rows}
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "canonical_schema_version": CANONICAL_REPORT_SCHEMA_VERSION,
        "capture": {
            "session_id": capture.session_id,
            "manifest_hash": capture.manifest_hash,
            "run_intent_sha256": capture.run_intent_sha256,
            "market_session_date": universe.session_date,
            "source_certification_report_hash": certification.report_hash(),
            "source_analytical_universe_hash": analytical.report_hash(),
            **certification.archive.as_dict(),
        },
        "request": {"wire_rate_value": wire_rate, "annual_dividend": 0.0},
        "pricing_admission": {
            "greeks_rows": len(capture.rows(OPTION_GREEKS)),
            "comparable_rows": len(rows),
            "exclusions": exclusions,
            "pricing_rows_with_oi": len(pricing_keys & set(universe.answered)),
            "pricing_rows_without_oi": len(pricing_keys - set(universe.answered)),
            "oi_available_pricing_subset_hash": _set_hash(
                pricing_keys & set(universe.answered)
            ),
            "accounting_is_exhaustive": len(rows) + sum(exclusions.values())
            == len(universe.expected),
        },
        "coverage": coverage_breakdown(universe, analytical.market_session_date),
        "pricing": _grid_report(rows, wire_rate),
        "trusted_for_gex": False,
        "orders_placed": 0,
        "gex_computed": False,
        "historical_certification_blockers": list(certification.gex_blockers),
        "limitations": [
            "One close snapshot; no independent validation or profitability evidence.",
            "Embedded underlying price is used in every candidate; dividend yield is assumed zero.",
            "16:00 and 17:00 are model clock hypotheses; 17:00 is not a market-hours claim.",
            "IV and delta rounding are present; the grid does not identify an IV solver or price basis.",
            "Contract-count coverage is not gamma coverage; missing OI exposure is unknown.",
            "Date eligibility and a nonzero delta are insufficient to authorize analytical use.",
        ],
    }
    report["diagnostic_report_hash"] = digest_of(canonical_payload(report))
    return report
