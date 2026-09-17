"""Offline comparisons must explain the floor without granting model authority."""

from __future__ import annotations

import json
import math
import zipfile
from dataclasses import replace
from datetime import date, datetime
from statistics import NormalDist

import pytest

from src.adapters.thetadata.capture_certification import (
    EASTERN,
    OPTION_GREEKS,
    CaptureCertificationError,
    CaptureUniverse,
    ContractKey,
    capture_universe,
    load_capture,
)
from src.adapters.thetadata.pricing_diagnostics import (
    HYPOTHESES,
    PricingDiagnosticsError,
    PricingHypothesis,
    PricingRow,
    _grid_report,
    _pricing_rows,
    coverage_breakdown,
    diagnose_capture,
    model_inputs,
)
from src.domain.canonical import canonical_payload
from src.domain.digests import digest_of
from src.tools.diagnose_thetadata_pricing import main
from tests.synthetic_capture import SyntheticVendor, write_capture


def _row(*, days=0, hour=15, minute=57, strike="100", sigma=0.2):
    stamp = datetime(2026, 9, 2, hour, minute, tzinfo=EASTERN)
    return PricingRow(
        ContractKey("SPXW", date(2026, 9, 2 + days), strike, "CALL"),
        100.0,
        sigma,
        0.5,
        stamp,
        stamp,
    )


BASE = PricingHypothesis("DECIMAL", 365.0, 16, "CONTINUOUS", "UNDERLYING", 60)


def test_the_floor_is_time_to_expiry_not_a_one_hour_shift_in_expiration():
    same_day = model_inputs(_row(), BASE, 0.042)
    tomorrow = model_inputs(_row(days=1), BASE, 0.042)
    assert same_day.time_to_expiry == pytest.approx(60 / (1440 * 365))
    assert tomorrow.time_to_expiry == pytest.approx(1443 / (1440 * 365))
    assert model_inputs(
        _row(), replace(BASE, floor_minutes=0), 0.042
    ).time_to_expiry == pytest.approx(3 / (1440 * 365))


@pytest.mark.parametrize(
    ("rule", "days", "expected"),
    [
        ("SHORT_LT7", 7, 7),
        ("SHORT_LE7", 7, 7 + 3 / 1440),
        ("SHORT_LE7", 8, 8),
        ("CONTINUOUS", 8, 8 + 3 / 1440),
    ],
)
def test_seven_day_boundary_is_explicit(rule, days, expected):
    result = model_inputs(_row(days=days), replace(BASE, time_rule=rule), 0.042)
    assert result.time_to_expiry == pytest.approx(expected / 365)


def test_timestamp_source_and_rate_units_are_independent_choices():
    row = replace(_row(days=1), option_at=datetime(2026, 9, 2, 15, tzinfo=EASTERN))
    result = model_inputs(
        row, replace(BASE, timestamp_source="OPTION", rate_unit="PERCENT"), 0.042
    )
    assert result.rate == pytest.approx(0.00042)
    assert result.time_to_expiry == pytest.approx(25 / (24 * 365))


def test_continuous_time_counts_the_repeated_dst_hour():
    stamp = datetime(2026, 10, 30, 15, tzinfo=EASTERN)
    row = replace(
        _row(),
        key=ContractKey("SPXW", date(2026, 11, 2), "100", "CALL"),
        underlying_at=stamp,
    )
    assert model_inputs(row, BASE, 0).time_to_expiry == pytest.approx(74 / (24 * 365))


def test_independent_atm_formula_distinguishes_floor_from_unfloored_model():
    rows = []
    for sigma in (0.1, 0.2, 0.4):
        row = _row(sigma=sigma)
        # ATM, r=q=0 implies delta = Phi(sigma * sqrt(T) / 2).
        observed = NormalDist().cdf(sigma * math.sqrt(1 / (24 * 365)) / 2)
        rows.append(replace(row, observed_delta=observed))
    report = _grid_report(tuple(rows), 0.0)
    assert report["candidate_count"] == 192
    assert len({h.identifier for h in HYPOTHESES}) == 192
    assert report["ranking"][0]["parameters"]["floor_minutes"] == 60
    assert report["ranking"][0]["delta_rmse"] < 1e-14
    assert report["floor_contrast"]["same_parameters_other_floor"]["delta_rmse"] > 1e-4
    assert report["vendor_model_identified"] is False
    assert report["selected_vendor_model"] is None
    assert {score["rows"] for score in report["ranking"]} == {3}


def test_coverage_does_not_count_stale_or_missing_as_zero():
    today = date(2026, 9, 2)
    keys = [
        ContractKey("SPXW", expiry, str(strike), "CALL")
        for expiry, strike in [
            (date(2026, 9, 1), 90),
            (today, 100),
            (today, 110),
            (date(2026, 10, 23), 120),
        ]
    ]
    universe = CaptureUniverse(
        "session",
        "a" * 64,
        str(today),
        "SPXW",
        "60",
        "",
        frozenset(keys),
        {keys[0]: 100, keys[1]: 0, keys[2]: 20},
    )
    report = coverage_breakdown(universe, today)
    assert report["current_listed"] == 3
    assert report["current_oi_answered"] == 2
    assert report["current_oi_missing"] == 1
    assert report["current_contract_count_coverage"] == pytest.approx(2 / 3)
    assert report["per_expiration"][-1]["answered_fraction"] == 0
    assert report["per_expiration"][1]["explicit_zero"] == 1
    assert report["gamma_weighted_coverage"] is None
    assert (
        coverage_breakdown(replace(universe, expected=frozenset()), today)[
            "current_contract_count_coverage"
        ]
        is None
    )


@pytest.fixture
def captured(tmp_path):
    return write_capture(
        tmp_path / "capture",
        SyntheticVendor(
            wire_rate_value=0.042,
            declared_economic_rate=0.042,
            strikes=tuple(range(5700, 6350, 50)),
            stale_expirations=(date(2026, 8, 7),),
        ),
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("delta", "nan", "NONFINITE_PRICING_INPUT"),
        ("delta", "1.0", "SATURATED_OR_INVALID_DELTA"),
        ("implied_vol", "0", "DEGENERATE_PRICING_INPUT"),
        ("underlying_price", "bad", "MALFORMED_PRICING_INPUT"),
        ("timestamp", "2026-08-09T15:00:00", "STALE_OR_AFTER_CLOSE_TIMESTAMP"),
        ("timestamp", "2026-08-10T16:00:00", "STALE_OR_AFTER_CLOSE_TIMESTAMP"),
    ],
)
def test_invalid_rows_are_counted_and_every_candidate_uses_same_members(
    captured, field, value, reason
):
    capture = load_capture(captured)
    universe = capture_universe(capture)
    capture.tables[OPTION_GREEKS][0][field] = value
    rows, excluded = _pricing_rows(capture, universe, date(2026, 8, 10))
    assert excluded[reason] == 1
    assert excluded["EXPIRED_BEFORE_SESSION"] > 0
    assert len(rows) + sum(excluded.values()) == len(universe.expected)


def test_duplicate_or_incomplete_greeks_cannot_silently_change_population(captured):
    capture = load_capture(captured)
    universe = capture_universe(capture)
    capture.tables[OPTION_GREEKS].append(capture.tables[OPTION_GREEKS][0])
    with pytest.raises(PricingDiagnosticsError, match="duplicate"):
        _pricing_rows(capture, universe, date(2026, 8, 10))
    capture.tables[OPTION_GREEKS].pop()
    capture.tables[OPTION_GREEKS].pop()
    with pytest.raises(PricingDiagnosticsError, match="sets differ"):
        _pricing_rows(capture, universe, date(2026, 8, 10))


def test_cli_reproduces_verified_input_without_modifying_it(captured, tmp_path):
    archive = tmp_path / "capture.zip"
    before = {
        p.relative_to(captured): p.read_bytes()
        for p in captured.rglob("*")
        if p.is_file()
    }
    with zipfile.ZipFile(archive, "w") as target:
        for name in before:
            target.write(captured / name, str(name))
    output = tmp_path / "diagnostic.json"
    assert (
        main([str(captured), "--archive-path", str(archive), "--json", str(output)])
        == 0
    )
    data = json.loads(output.read_bytes())
    assert data["capture"]["archive_identity_known"] is True
    assert data["pricing_admission"]["accounting_is_exhaustive"] is True
    assert data["trusted_for_gex"] is data["gex_computed"] is False
    assert data["orders_placed"] == 0
    digest = data.pop("diagnostic_report_hash")
    assert digest == digest_of(canonical_payload(data))
    assert all(
        p.read_bytes() == before[p.relative_to(captured)]
        for p in captured.rglob("*")
        if p.is_file()
    )
    assert b"\r" not in output.read_bytes()
    assert str(captured) not in output.read_text()
    assert main([str(captured), "--json", str(output)]) == 2


def test_cli_refuses_to_write_into_evidence(captured):
    output = captured / "do-not-create.json"
    assert main([str(captured), "--json", str(output)]) == 2
    assert not output.exists()


def test_unrelated_archive_is_rejected(captured, tmp_path):
    archive = tmp_path / "other.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("unrelated.txt", "not a capture")
    with pytest.raises(PricingDiagnosticsError, match="archive"):
        diagnose_capture(captured, archive_path=archive)


def test_tampered_raw_bytes_cannot_receive_a_diagnostic(captured):
    raw = next((captured / "raw").glob("*.raw"))
    raw.write_bytes(raw.read_bytes() + b"\nTAMPERED\n")
    with pytest.raises(CaptureCertificationError):
        diagnose_capture(captured)


@pytest.mark.parametrize(
    ("strike", "bucket"), [("90", "BELOW"), ("100", "95_TO_105"), ("110", "ABOVE")]
)
def test_moneyness_is_strike_over_spot_and_independent_of_right(strike, bucket):
    assert bucket in _row(strike=strike).moneyness_bucket
