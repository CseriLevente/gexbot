"""Censored time observations must not manufacture an expiration clock."""

from __future__ import annotations

import json
import math
import zipfile
from dataclasses import replace
from datetime import date, datetime, timedelta
from statistics import NormalDist

import pytest

from src.adapters.thetadata import floor_inference
from src.adapters.thetadata.capture_certification import EASTERN, ContractKey
from src.adapters.thetadata.floor_inference import (
    clock_uncensored_rows,
    infer_capture,
    infer_rows,
    screen_candidates,
    validate_captures,
)
from src.adapters.thetadata.pricing_diagnostics import (
    PricingDiagnosticsError,
    PricingRow,
)
from src.domain.canonical import canonical_payload
from src.domain.digests import digest_of
from src.tools.infer_thetadata_pricing import main
from tests.synthetic_capture import SyntheticVendor, write_capture


def independent_rows(
    *, dtes=(0, 1, 7, 8, 30), hour=15, minute=57, floor=60, rate=0.042
):
    """Independent delta formula; does not use the inference's time constructor."""
    stamp = datetime(2026, 9, 2, hour, minute, tzinfo=EASTERN)
    rows = []
    for days in dtes:
        expiry = stamp.date() + timedelta(days=days)
        remaining_minutes = 1440 * days + 16 * 60 - hour * 60 - minute
        years = max(remaining_minutes, floor) / (1440 * 365)
        for strike in (99.9, 99.95, 100.0, 100.05, 100.1):
            for sigma in (0.1, 0.2, 0.4):
                for right in ("CALL", "PUT"):
                    # Unique strike/expiry/right identity for each volatility.
                    key = ContractKey(
                        "SPXW", expiry, str(strike + 0.000001 * sigma), right
                    )
                    exact_strike = float(key.strike)
                    d1 = (
                        math.log(100 / exact_strike) + (rate + sigma**2 / 2) * years
                    ) / (sigma * math.sqrt(years))
                    observed = NormalDist().cdf(d1) - (1 if right == "PUT" else 0)
                    rows.append(PricingRow(key, 100, sigma, observed, stamp, stamp))
    return tuple(rows)


def test_all_floored_rows_support_the_floor_but_cannot_identify_a_clock():
    result = infer_rows(independent_rows(dtes=(0,)), 0.042)
    assert result["dimensions"]["floor_minutes"]["selected_value"] == 60
    assert result["censoring"]["common_uncensored_rows"] == 0
    assert result["dimensions"]["clock_hour_et"]["status"] == "FLOOR_CENSORED"
    assert result["dimensions"]["clock_hour_et"]["selected_value"] is None
    assert result["clock_fit_on_common_uncensored_rows"] is None
    assert result["exact_expiration_clock_identified"] is False
    assert result["selected_vendor_model"] is None


def test_floor_observations_are_removed_from_every_clock_candidate_equally():
    rows = independent_rows()
    result = infer_rows(rows, 0.042)
    expected = len([r for r in rows if r.key.expiration > r.option_at.date()])
    assert result["censoring"]["common_uncensored_rows"] == expected
    assert {
        s["rows"] for s in result["clock_fit_on_common_uncensored_rows"]["ranking"]
    } == {expected}
    assert {s["rows"] for s in result["global_fit"]["ranking"]} == {len(rows)}
    assert result["global_fit"]["best_delta_rmse"] < 1e-14
    assert result["dimensions"]["rate_unit"]["selected_value"] == "DECIMAL"


@pytest.mark.parametrize(
    ("hour", "minute", "expected"), [(14, 59, 30), (15, 0, 0), (15, 1, 0)]
)
def test_censoring_includes_the_exact_floor_boundary(hour, minute, expected):
    assert (
        len(
            clock_uncensored_rows(independent_rows(dtes=(0,), hour=hour, minute=minute))
        )
        == expected
    )


def test_uncertain_timestamp_source_uses_the_more_conservative_censoring():
    rows = independent_rows(dtes=(0,), hour=14)
    rows = tuple(
        replace(r, option_at=r.option_at.replace(hour=15, minute=57)) for r in rows
    )
    assert not clock_uncensored_rows(rows)


def test_nonbinding_floor_is_not_declared_identified():
    result = infer_rows(independent_rows(dtes=(1, 7)), 0.042)
    assert result["dimensions"]["floor_minutes"]["status"] == "FLOOR_NOT_EXERCISED"
    assert result["dimensions"]["floor_minutes"]["selected_value"] is None


def test_zero_rate_keeps_both_wire_unit_readings():
    result = infer_rows(independent_rows(rate=0), 0)
    assert result["dimensions"]["rate_unit"]["supported_values"] == [
        "DECIMAL",
        "PERCENT",
    ]
    assert result["dimensions"]["rate_unit"]["status"] == "AMBIGUOUS"


def test_too_few_uncensored_rows_do_not_borrow_sample_size_from_floored_rows():
    floored = independent_rows(dtes=(0,))
    future = independent_rows(dtes=(1,))[:5]
    result = infer_rows(floored + future, 0.042)
    assert result["global_fit"]["status"] == "ADEQUATE_CANDIDATE_SET"
    assert result["dimensions"]["clock_hour_et"]["status"] == "INSUFFICIENT_DATA"


def test_floor_bound_rows_cannot_remove_an_uncensored_clock_alternative():
    result = infer_rows(independent_rows(dtes=(0, 30)), 0.042)
    retained = set(result["global_fit"]["supported_candidates"])
    assert {
        s["parameters"]["clock_hour_et"]
        for s in result["global_fit"]["ranking"]
        if s["id"] in retained
    } == {16}
    assert result["dimensions"]["clock_hour_et"] == {
        "status": "AMBIGUOUS",
        "supported_values": [16, 17],
        "selected_value": None,
    }


def test_candidate_order_and_row_order_cannot_choose_a_tied_winner():
    scores = [{"id": name, "rows": 30, "delta_rmse": 0.0} for name in ("z", "a")]
    assert screen_candidates(scores) == screen_candidates(list(reversed(scores)))
    assert screen_candidates(scores)["supported_candidates"] == ["a", "z"]
    rows = independent_rows(dtes=(0, 1))
    assert infer_rows(rows, 0.042) == infer_rows(tuple(reversed(rows)), 0.042)


@pytest.mark.parametrize(
    ("count", "error", "status"),
    [(29, 0, "INSUFFICIENT_DATA"), (30, 0.01, "NO_ADEQUATE_FIT")],
)
def test_no_best_candidate_is_promoted_without_adequacy_and_sample_size(
    count, error, status
):
    result = screen_candidates(
        [{"id": "candidate", "rows": count, "delta_rmse": error}]
    )
    assert result["status"] == status
    assert not result["supported_candidates"]


@pytest.mark.parametrize(
    "scores",
    [
        [],
        [{"id": "a", "rows": 30, "delta_rmse": float("nan")}],
        [
            {"id": "a", "rows": 30, "delta_rmse": 0},
            {"id": "a", "rows": 30, "delta_rmse": 0},
        ],
        [
            {"id": "a", "rows": 30, "delta_rmse": 0},
            {"id": "b", "rows": 31, "delta_rmse": 0},
        ],
    ],
)
def test_invalid_comparisons_are_refused(scores):
    with pytest.raises(PricingDiagnosticsError):
        screen_candidates(scores)


def test_kernel_refuses_empty_nonfinite_or_duplicate_inputs():
    rows = independent_rows(dtes=(0,))
    for sample, rate in (((), 0.0), (rows, float("nan")), (rows + rows, 0.042)):
        with pytest.raises(PricingDiagnosticsError):
            infer_rows(sample, rate)


def _capture(
    tmp_path, name, session=date(2026, 8, 10), rate_unit="DECIMAL_ANNUAL_RATE"
):
    vendor = SyntheticVendor(
        wire_rate_value=0.042,
        rate_unit=rate_unit,
        declared_economic_rate=(
            0.00042 if rate_unit == "PERCENT_ANNUAL_RATE" else 0.042
        ),
        strikes=tuple(range(5950, 6100, 25)),
        valuation=datetime.combine(
            session, datetime.min.time().replace(hour=10), EASTERN
        ),
    )
    root = write_capture(tmp_path / name, vendor)
    archive = tmp_path / (name + ".zip")
    with zipfile.ZipFile(archive, "w") as z:
        for p in root.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(root))
    return root, archive


def test_capture_report_is_bound_and_cli_cannot_overwrite_or_pollute_evidence(tmp_path):
    root, archive = _capture(tmp_path, "one")
    before = {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    report = infer_capture(root, archive_path=archive)
    digest = report.pop("report_hash")
    assert digest == digest_of(canonical_payload(report))
    assert report["capture"]["archive_identity_known"] is True
    assert len(report["source_diagnostic_report_hash"]) == 64
    assert report["trusted_for_gex"] is False
    assert report["orders_placed"] == 0
    output = tmp_path / "inference.json"
    args = ["--capture", str(root), str(archive), "--json", str(output)]
    assert main(args) == 0
    assert json.loads(output.read_bytes())["report_hash"] == digest
    assert b"\r" not in output.read_bytes()
    assert str(root) not in output.read_text()
    assert main(args) == 2
    assert (
        main(["--capture", str(root), str(archive), "--json", str(root / "new.json")])
        == 2
    )
    assert before == {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_duplicate_sessions_do_not_count_as_independent_validation(tmp_path):
    capture = _capture(tmp_path, "one")
    with pytest.raises(PricingDiagnosticsError, match="at least two"):
        validate_captures((capture,))
    with pytest.raises(PricingDiagnosticsError, match="duplicate session"):
        validate_captures((capture, capture))


def test_different_sessions_are_recomputed_in_stable_order(tmp_path):
    first = _capture(tmp_path, "first")
    second = _capture(tmp_path, "second", date(2026, 8, 11))
    result = validate_captures((second, first))
    assert result["capture_count"] == 2
    assert result == validate_captures((first, second))
    assert (
        result["capture_reports"][0]["capture"]["market_session_date"] == "2026-08-10"
    )
    assert result["full_vendor_model_identified"] is result["trusted_for_gex"] is False


def test_wrong_archive_refuses_before_emitting_inference(tmp_path):
    root, archive = _capture(tmp_path, "one")
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("unrelated", "bytes")
    out = tmp_path / "must-not-exist.json"
    assert main(["--capture", str(root), str(archive), "--json", str(out)]) == 2
    assert not out.exists()


def test_conflicting_sessions_cannot_establish_replicated_rate_units(tmp_path):
    first = _capture(tmp_path, "decimal")
    second = _capture(tmp_path, "percent", date(2026, 8, 11), "PERCENT_ANNUAL_RATE")
    out = tmp_path / "cross-capture.json"
    args = ["--capture", *map(str, first), "--capture", *map(str, second)]
    assert main([*args, "--json", str(out)]) == 0
    result = json.loads(out.read_bytes())
    individual = [
        r["inference"]["dimensions"]["rate_unit"]["selected_value"]
        for r in result["capture_reports"]
    ]
    assert individual == ["DECIMAL", "PERCENT"]
    assert result["replicated_dimensions"]["rate_unit"] == {
        "status": "UNRESOLVED_ACROSS_CAPTURES",
        "selected_value": None,
    }
    assert result["common_candidate_status"] == "NO_COMMON_CANDIDATE"
    assert result["trusted_for_gex"] is False


def test_bad_fit_does_not_resolve_any_dimension():
    rows = tuple(replace(r, observed_delta=0.8) for r in independent_rows())
    result = infer_rows(rows, 0.042)
    assert result["global_fit"]["status"] == "NO_ADEQUATE_FIT"
    assert all(d["selected_value"] is None for d in result["dimensions"].values())


@pytest.mark.parametrize("field", ["manifest_hash", "run_intent_sha256", "population"])
def test_inference_refuses_mismatched_provenance_from_prior_diagnostic(
    tmp_path, monkeypatch, field
):
    root, archive = _capture(tmp_path, "capture")
    diagnostic = floor_inference.diagnose_capture(root, archive_path=archive)
    if field == "population":
        diagnostic["pricing"]["common_identity_hash"] = "0" * 64
    else:
        diagnostic["capture"][field] = "0" * 64
    monkeypatch.setattr(
        floor_inference, "diagnose_capture", lambda *a, **kw: diagnostic
    )
    with pytest.raises(PricingDiagnosticsError, match="changed after"):
        infer_capture(root, archive_path=archive)
