"""Does the reconstruction recover a convention it was not told?

Everything in ``test_live_capture_certification.py`` asserts what the real
capture showed. Nothing there checks that the *method* works -- if the inversion
were wrong in a way that happened to favour ACT/365, those tests would pass and
be worthless.

So here the vendor is us. A capture is generated with the engine's own
Black-Scholes at a chosen rate, day count and expiration clock, written out in
the vendor's CSV shapes, and put through ``certify_capture``. The question is
whether the certification names the parameters the generator used, having been
told none of them.

**Nothing here is evidence about ThetaData.** These rows were computed, not
captured. A fabricated chain can be made to prove whatever its author already
believed, which is exactly why the real conclusions live in the other file and
rest on bytes nobody in this repository produced.
"""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
from datetime import date, datetime, time, timedelta

import pytest

from src.adapters.thetadata.capture_certification import (
    EASTERN,
    certify_capture,
)
from src.domain.contracts import OptionRight
from src.gex.pricing import BlackScholesInputs, norm_cdf
from src.gex.pricing import delta as bs_delta
from src.tools.certify_thetadata_capture import (
    EXIT_CONFLICT,
    EXIT_UNREADABLE,
    main,
)

# What the generator uses. The certification is told none of it.
SPOT = 6000.0
VALUATION = datetime(2026, 8, 10, 10, 1, 34, tzinfo=EASTERN)
RATE = 4.2
DAYS_PER_YEAR = 365.0
EXPIRY_TIME = time(16, 0)
#: Expirations at or before this price on an intraday clock to 16:00; later ones
#: price on whole calendar days. Mirrors what the live capture turned out to do.
FRONT_WEEK_END = date(2026, 8, 14)

EXPIRATIONS = (date(2026, 8, 12), date(2026, 8, 24), date(2026, 9, 16))


def _years(expiration: date) -> float:
    if expiration <= FRONT_WEEK_END:
        expiry = datetime.combine(expiration, EXPIRY_TIME, tzinfo=EASTERN)
        days = (expiry - VALUATION).total_seconds() / 86400.0
    else:
        days = float((expiration - VALUATION.date()).days)
    return days / DAYS_PER_YEAR


def _price(strike: float, years: float, sigma: float, right: OptionRight) -> float:
    sq = sigma * math.sqrt(years)
    d1 = (math.log(SPOT / strike) + (RATE + 0.5 * sigma * sigma) * years) / sq
    d2 = d1 - sq
    if right is OptionRight.CALL:
        return SPOT * norm_cdf(d1) - strike * math.exp(-RATE * years) * norm_cdf(d2)
    return strike * math.exp(-RATE * years) * norm_cdf(-d2) - SPOT * norm_cdf(-d1)


def _rows() -> tuple[list[dict[str, str]], ...]:
    """Generate a chain the certification can actually reconstruct."""
    quote, oi, greeks, listing = [], [], [], []
    for expiration in EXPIRATIONS:
        years = _years(expiration)
        for index, strike in enumerate(range(3000, 12000, 50)):
            for right in (OptionRight.CALL, OptionRight.PUT):
                sigma = 0.9
                inputs = BlackScholesInputs(
                    spot=SPOT,
                    strike=float(strike),
                    time_to_expiry=years,
                    implied_vol=sigma,
                    rate=RATE,
                )
                if inputs.is_degenerate():
                    continue
                delta = bs_delta(inputs, right)
                # The rows that carry information are the ones off the
                # boundary; the reconstruction ignores the rest anyway.
                if not (0.02 < abs(delta) < 0.98):
                    continue
                price = _price(float(strike), years, sigma, right)
                if price <= 0.10:
                    continue
                identity = {
                    "symbol": "SPXW",
                    "expiration": expiration.isoformat(),
                    "strike": f"{strike}.000",
                    "right": right.value.upper(),
                }
                stamp = (VALUATION - timedelta(seconds=index % 7)).strftime(
                    "%Y-%m-%dT%H:%M:%S.000"
                )
                bid, ask = round(price - 0.05, 4), round(price + 0.05, 4)
                quote.append(
                    {**identity, "timestamp": stamp, "bid": f"{bid}", "ask": f"{ask}"}
                )
                greeks.append(
                    {
                        **identity,
                        "timestamp": stamp,
                        "bid": f"{bid}",
                        "ask": f"{ask}",
                        # Rounded to four decimals, exactly as the vendor
                        # reports it, so the noise floor is realistic.
                        "implied_vol": f"{sigma:.4f}",
                        "delta": f"{delta:.4f}",
                        "iv_error": "0.0000",
                        "underlying_timestamp": VALUATION.strftime(
                            "%Y-%m-%dT%H:%M:%S.000"
                        ),
                        "underlying_price": f"{SPOT:.4f}",
                    }
                )
                listing.append(dict(identity))
                # Two contracts per expiration get no open-interest row, and
                # some get an explicit zero, so both paths are exercised.
                if index % 37 == 0:
                    continue
                oi.append(
                    {**identity, "open_interest": "0" if index % 5 == 0 else "125"}
                )
    return quote, oi, greeks, listing


def _csv(rows: list[dict[str, str]], columns: tuple[str, ...]) -> str:
    lines = [",".join(columns)]
    lines.extend(",".join(row[c] for c in columns) for row in rows)
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def generated_capture(tmp_path_factory) -> pathlib.Path:
    root = tmp_path_factory.mktemp("generated") / "capture"
    (root / "raw").mkdir(parents=True)
    quote, oi, greeks, listing = _rows()
    bodies = {
        "/v3/index/snapshot/price": (
            "timestamp,symbol,price\n"
            f'{VALUATION.strftime("%Y-%m-%dT%H:%M:%S.000")},"SPX",{SPOT + 0.27:.2f}\n'
        ),
        "/v3/option/snapshot/quote": _csv(
            quote,
            ("timestamp", "symbol", "expiration", "strike", "right", "bid", "ask"),
        ),
        "/v3/option/snapshot/open_interest": _csv(
            oi, ("symbol", "expiration", "strike", "right", "open_interest")
        ),
        "/v3/option/snapshot/greeks/first_order": _csv(
            greeks,
            (
                "symbol",
                "expiration",
                "strike",
                "right",
                "timestamp",
                "bid",
                "ask",
                "delta",
                "implied_vol",
                "iv_error",
                "underlying_timestamp",
                "underlying_price",
            ),
        ),
        "/v3/option/list/contracts/quote": _csv(
            listing, ("symbol", "expiration", "strike", "right")
        ),
    }
    records = []
    for endpoint, body in bodies.items():
        name = endpoint.strip("/").replace("/", "-") + ".raw"
        raw = body.encode("utf-8")
        (root / "raw" / name).write_bytes(raw)
        records.append(
            {
                "endpoint": endpoint,
                "payload_location": name,
                "payload_hash": hashlib.sha256(raw).hexdigest(),
                "effective_valuation_timestamp": "2026-08-10T14:01:34+00:00",
            }
        )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "session_id": "capture-generated",
                "manifest_hash": "b" * 64,
                "parser_version": "thetadata-v3-parser/2.1.17",
                "records": records,
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture(scope="module")
def report(generated_capture):
    return certify_capture(generated_capture, archive_sha256="c" * 64)


def test_the_reconstruction_recovers_the_rate_it_was_not_told(report):
    best = min(report.rate_scores, key=lambda s: s.delta_rmse)
    assert "DECIMAL_ANNUAL_RATE" in best.hypothesis
    worst = max(report.rate_scores, key=lambda s: s.delta_rmse)
    assert worst.delta_rmse > 100 * best.delta_rmse


def test_the_reconstruction_recovers_the_day_count_it_was_not_told(report):
    best = min(report.day_count_scores, key=lambda s: s.delta_rmse)
    assert best.hypothesis == "ACT/365"


def test_the_reconstruction_recovers_the_expiration_clock_it_was_not_told(report):
    best = min(report.expiration_time_scores, key=lambda s: s.delta_rmse)
    assert best.hypothesis == "16:00 America/New_York"


def test_the_inversion_finds_the_front_week_boundary_it_was_not_told(report):
    intraday = {r.expiration for r in report.clock_readings if r.matches_intraday_1600}
    whole = {r.expiration for r in report.clock_readings if r.matches_whole_days}
    assert intraday == {"2026-08-12"}
    assert whole == {"2026-08-24", "2026-09-16"}


def test_the_reconstruction_recovers_the_iv_basis_it_was_not_told(report):
    best = min(report.iv_basis_scores, key=lambda row: row[2])
    assert best[0] == "NBBO_MID"


def test_the_generated_universe_is_certified_by_set_hash(report):
    assert report.universe.quote_matches_list
    assert report.universe.greeks_matches_list
    assert report.universe.state == "DEDICATED_CONTRACT_LIST_MATCHED_SNAPSHOT_UNIVERSE"


def test_missing_and_explicit_zero_open_interest_are_counted_apart(report):
    coverage = report.open_interest
    assert coverage.missing_count > 0
    assert coverage.explicit_zero_count > 0
    assert coverage.present_count > 0
    assert (
        coverage.present_count + coverage.explicit_zero_count + coverage.missing_count
        == coverage.universe_count
    )
    assert coverage.permits_trusted_aggregate is False
    assert 0.0 < coverage.coverage_ratio < 1.0


def test_the_embedded_underlying_beats_the_index_snapshot(report):
    assert report.underlying["synchronized"] is False
    assert report.underlying["vendor_greeks_underlying_price"] == pytest.approx(SPOT)


def test_no_capture_is_ever_trusted_for_gex(report):
    assert report.trusted_for_gex is False
    assert report.analytical_readiness == "ADAPTER_CERTIFICATION_EVIDENCE"
    assert report.gex_blockers


def test_the_report_is_content_addressed_and_stable(generated_capture, report):
    again = certify_capture(generated_capture, archive_sha256="c" * 64)
    assert again.report_hash() == report.report_hash()
    assert again.as_dict() == report.as_dict()


def test_a_different_archive_digest_is_a_different_report(generated_capture, report):
    other = certify_capture(generated_capture, archive_sha256="d" * 64)
    assert other.report_hash() != report.report_hash()


def test_greeks_carrying_two_underlying_states_are_refused(generated_capture, tmp_path):
    """One snapshot, one underlying. Several would need a per-row reconstruction."""
    import shutil

    from src.adapters.thetadata.capture_certification import CaptureCertificationError

    root = tmp_path / "twostate"
    shutil.copytree(generated_capture, root)
    target = root / "raw" / "v3-option-snapshot-greeks-first_order.raw"
    lines = target.read_text().splitlines()
    lines[1] = lines[1].replace("6000.0000", "6001.0000")
    body = "\n".join(lines) + "\n"
    target.write_bytes(body.encode("utf-8"))
    manifest = json.loads((root / "manifest.json").read_text())
    for record in manifest["records"]:
        if "greeks" in record["endpoint"]:
            record["payload_hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CaptureCertificationError, match=r"distinct\s+underlying"):
        certify_capture(root)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def test_the_command_reports_a_conflict_with_its_own_exit_code(
    generated_capture, tmp_path, capsys
):
    out = tmp_path / "report.json"
    code = main(
        [str(generated_capture), "--json", str(out), "--archive-sha256", "e" * 64]
    )

    assert code == EXIT_CONFLICT
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["report_hash"]
    assert "RATE_UNITS" in written["documentation_live_conflicts"]

    printed = capsys.readouterr().out
    assert "DEDICATED_CONTRACT_LIST_MATCHED_SNAPSHOT_UNIVERSE" in printed
    assert "trusted for gex False" in printed
    assert "blocked by" in printed


def test_the_command_writes_the_whole_report_to_stdout_without_a_json_path(
    generated_capture, capsys
):
    code = main([str(generated_capture)])
    assert code == EXIT_CONFLICT
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"].startswith("capture-certification/")
    assert payload["universe"]["contract_list_count"] > 0


def test_the_command_refuses_an_unreadable_capture(tmp_path, capsys):
    code = main([str(tmp_path / "nothing-here")])
    assert code == EXIT_UNREADABLE
    assert "cannot be certified" in capsys.readouterr().err


def test_the_command_makes_no_network_call(generated_capture, monkeypatch):
    """Certification reads a directory. It has no business opening a socket."""
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("certification attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert main([str(generated_capture)]) == EXIT_CONFLICT
