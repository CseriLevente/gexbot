"""Local gamma uncertainty remains separate from GEX and tradable evidence."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import date
from pathlib import Path
from statistics import NormalDist

import pytest

from src.adapters.thetadata import gamma_sensitivity
from src.adapters.thetadata.gamma_sensitivity import analyze_captures, gamma_ranges
from src.adapters.thetadata.pricing_diagnostics import (
    HYPOTHESES,
    PricingDiagnosticsError,
)
from src.domain.canonical import canonical_payload
from src.domain.digests import digest_of
from src.tools.audit_research_inputs import main
from tests.unit.test_floor_inference import _capture, independent_rows


def models():
    return tuple(
        h
        for h in HYPOTHESES
        if h.rate_unit == "DECIMAL"
        and h.days_per_year == 365
        and h.clock_hour_et == 16
        and h.floor_minutes == 60
    )


def test_floor_bound_gamma_matches_independent_normal_density():
    row = independent_rows(dtes=(0,))[0]
    years = 60 / (1440 * 365)
    d1 = (
        math.log(row.spot / float(row.key.strike))
        + (0.042 + row.implied_vol**2 / 2) * years
    ) / (row.implied_vol * math.sqrt(years))
    expected = NormalDist().pdf(d1) / (row.spot * row.implied_vol * math.sqrt(years))
    result = gamma_ranges((row,), models(), 0.042)
    item = result["largest_relative_ranges"][0]
    assert item["minimum_gamma"] == pytest.approx(expected, rel=1e-12)
    assert item["maximum_gamma"] == pytest.approx(expected, rel=1e-12)
    assert result["maximum_range_percent"] == 0


def test_seven_day_time_rules_retain_real_uncertainty_and_permutation_invariance():
    rows = independent_rows(dtes=(7,))
    result = gamma_ranges(rows, models(), 0.042)
    assert result == gamma_ranges(
        tuple(reversed(rows)), tuple(reversed(models())), 0.042
    )
    assert result["model_count"] == 6
    assert result["maximum_absolute_gamma_range"] > 0
    assert 0 < result["median_range_percent"] <= result["p95_range_percent"] <= 100


def test_empty_single_model_and_zero_gamma_do_not_invent_uncertainty():
    row = independent_rows(dtes=(0,))[0]
    assert gamma_ranges((), models(), 0.042)["status"] == "NO_ROWS"
    single = gamma_ranges((row,), models()[:1], 0.042)
    assert single["status"] == "SINGLE_MODEL"
    assert single["maximum_range_percent"] == 0
    extreme = replace(row, key=replace(row.key, strike="1e50"))
    result = gamma_ranges((extreme,), models(), 0.042)
    assert result["all_zero_gamma_rows"] == 1
    assert result["maximum_range_percent"] is None
    assert result["maximum_absolute_gamma_range"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"spot": 0},
        {"spot": float("nan")},
        {"implied_vol": 0},
        {"implied_vol": float("inf")},
    ],
)
def test_invalid_inputs_do_not_turn_into_zero_gamma(change):
    row = replace(independent_rows(dtes=(0,))[0], **change)
    with pytest.raises(PricingDiagnosticsError, match="invalid gamma inputs"):
        gamma_ranges((row,), models(), 0.042)


def test_duplicate_keys_invalid_rates_and_unregistered_models_are_refused():
    row = independent_rows(dtes=(0,))[0]
    for rows, candidates, rate in (
        ((row, row), models(), 0.042),
        ((row,), (), 0.042),
        ((row,), models(), float("nan")),
        ((row,), models() + models(), 0.042),
        ((row,), (replace(models()[0], clock_hour_et=18),), 0.042),
    ):
        with pytest.raises(PricingDiagnosticsError):
            gamma_ranges(rows, candidates, rate)


def test_nonfinite_calculation_is_refused(monkeypatch):
    monkeypatch.setattr(gamma_sensitivity, "gamma", lambda _: float("nan"))
    with pytest.raises(PricingDiagnosticsError, match="invalid gamma result"):
        gamma_ranges(independent_rows(dtes=(0,)), models(), 0.042)


def test_two_verified_captures_cli_is_bound_reproducible_and_cannot_modify_evidence(
    tmp_path,
):
    inputs = (
        _capture(tmp_path, "first"),
        _capture(tmp_path, "second", date(2026, 8, 11)),
    )
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    report = analyze_captures(inputs)
    assert report["source_validation_report_hash"]
    assert report["model_ids"]
    assert report["gamma_computed"] is True
    assert report["gex_computed"] is False
    assert report["trusted_for_gex"] is False
    assert report["orders_placed"] == 0
    assert report["interpretation"] == "RETROSPECTIVE_MODEL_SENSITIVITY_ONLY"
    digest = report.pop("report_hash")
    assert digest == digest_of(canonical_payload(report))
    for r in report["capture_reports"]:
        assert r["pricing_rows"] > r["oi_available_pricing_rows"]
        assert r["excluded_missing_oi_pricing_rows"] > 0
        assert r["ranges"]["0DTE"]["status"] == "NO_ROWS"
        assert r["ranges"]["0_TO_60DTE"]["rows"] == r["oi_available_pricing_rows"]
    args = ["gamma"]
    for root, archive in inputs:
        args.extend(["--capture", str(root), str(archive)])
    output = tmp_path / "gamma.json"
    assert main([*args, "--json", str(output)]) == 0
    assert json.loads(output.read_bytes())["report_hash"] == digest
    assert main([*args, "--json", str(output)]) == 2
    assert main([*args, "--json", str(inputs[0][0] / "pollution.json")]) == 2
    assert {p: Path(p).read_bytes() for p in before} == before
    assert (
        main(
            [
                "gamma",
                "--capture",
                str(inputs[0][0]),
                str(inputs[0][1]),
                "--json",
                str(tmp_path / "one.json"),
            ]
        )
        == 2
    )


def test_model_selection_must_be_recomputed_and_changed_capture_link_is_refused(
    tmp_path, monkeypatch
):
    inputs = (
        _capture(tmp_path, "first"),
        _capture(tmp_path, "second", date(2026, 8, 11)),
    )
    original = gamma_sensitivity.validate_captures

    def swapped(inputs):
        report = original(inputs)
        report["capture_reports"][0]["capture"]["session_id"] = "another-capture"
        return report

    monkeypatch.setattr(gamma_sensitivity, "validate_captures", swapped)
    with pytest.raises(
        PricingDiagnosticsError, match="changed after model verification"
    ):
        analyze_captures(inputs)


def test_no_common_models_computes_no_gamma_and_changed_population_is_refused(
    tmp_path, monkeypatch
):
    inputs = (
        _capture(tmp_path, "first"),
        _capture(tmp_path, "second", date(2026, 8, 11)),
    )
    validation = gamma_sensitivity.validate_captures(inputs)
    validation["common_global_candidates"] = []
    monkeypatch.setattr(gamma_sensitivity, "validate_captures", lambda _: validation)
    result = analyze_captures(inputs)
    assert result["status"] == "NO_COMMON_MODELS"
    assert result["gamma_computed"] is False
    assert all(not r["ranges"] for r in result["capture_reports"])
    validation["capture_reports"][0]["pricing_admission"][
        "oi_available_pricing_subset_hash"
    ] = "b" * 64
    with pytest.raises(PricingDiagnosticsError, match="population changed"):
        analyze_captures(inputs)
