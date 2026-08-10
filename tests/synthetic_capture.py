"""Build a capture directory the certification machinery has never seen.

Certification is an inference engine, and an inference engine tested only on
data generated under its own assumptions proves nothing. v2.1.22's synthetic
test generated at ``RATE = 4.2``, ``DAYS_PER_YEAR = 365`` and ``EXPIRY_TIME =
16:00`` -- the same three constants the implementation contained -- so it
demonstrated self-consistency and would have passed unchanged had every one of
those constants been wrong.

So the generator here takes them as parameters. A test picks a wire value, a
unit for the vendor to read it under, a day count and a close; the vendor's
numbers are computed from the engine's own Black-Scholes accordingly; and the
certification is asked to name all four having been told none of them.

The output is a *real* capture directory: full manifest descriptors whose
digest hashes to its own contents, a run intent whose planned requests
reproduce the stamps on the manifest records, and payloads that hash to the
digests the descriptors record. Anything less would exercise the reconstruction
while skipping the verification, and the verification is half of what
certification is.

**None of this is evidence about ThetaData.** These rows were computed, not
captured. They test the method; the conclusions live in the fixture derived
from the real capture.
"""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.adapters.raw_store import CaptureOrigin, ManifestRecord, RawCaptureManifest
from src.adapters.thetadata.request_plan import (
    canonical_parameters,
    planned_request_hash,
)
from src.domain.contracts import OptionRight
from src.gex.pricing import BlackScholesInputs, norm_cdf
from src.gex.pricing import delta as bs_delta

EASTERN = ZoneInfo("America/New_York")

INDEX_PRICE = "/v3/index/snapshot/price"
OPTION_QUOTE = "/v3/option/snapshot/quote"
OPTION_OPEN_INTEREST = "/v3/option/snapshot/open_interest"
OPTION_GREEKS = "/v3/option/snapshot/greeks/first_order"
OPTION_CONTRACT_LIST = "/v3/option/list/contracts/quote"

#: A fingerprint standing in for the session's request spec. Arbitrary, and
#: identical on both sides of the binding, which is what the binding checks.
SPEC_FINGERPRINT = "a" * 64


@dataclass(frozen=True, slots=True)
class SyntheticVendor:
    """The conventions the fake vendor prices under. Certification is told none.

    ``rate_unit`` is how this vendor *reads* ``wire_rate_value``: a decimal
    vendor uses the number unchanged, a percent vendor divides by a hundred.
    The two produce different Greeks from the same request, which is the whole
    ambiguity the first live capture exposed.
    """

    wire_rate_value: float = 4.2
    rate_unit: str = "DECIMAL_ANNUAL_RATE"
    days_per_year: float = 365.0
    expiry_clock: time = time(16, 0)
    #: Expirations at or before this price on an intraday clock; later ones on
    #: whole calendar days, as the real vendor turned out to do.
    front_week_end: date = date(2026, 8, 14)
    valuation: datetime = datetime(2026, 8, 10, 10, 1, 34, tzinfo=EASTERN)
    spot: float = 6000.0
    sigma: float = 0.9
    expirations: tuple[date, ...] = (
        date(2026, 8, 12),
        date(2026, 8, 24),
        date(2026, 9, 16),
    )
    #: Declared by the capturing session, when it declares one at all. ``None``
    #: reproduces a pre-v2.1.23 capture, which recorded no intent.
    declared_economic_rate: float | None = None
    strikes: tuple[int, ...] = field(
        default_factory=lambda: tuple(range(3000, 12000, 50))
    )

    @property
    def effective_rate(self) -> float:
        """The ``r`` this vendor puts into Black-Scholes."""
        if self.rate_unit == "PERCENT_ANNUAL_RATE":
            return self.wire_rate_value / 100.0
        return self.wire_rate_value

    def years(self, expiration: date) -> float:
        if expiration <= self.front_week_end:
            expiry = datetime.combine(expiration, self.expiry_clock, tzinfo=EASTERN)
            days = (expiry - self.valuation).total_seconds() / 86400.0
        else:
            days = float((expiration - self.valuation.date()).days)
        return days / self.days_per_year

    def price(self, strike: float, years: float, right: OptionRight) -> float:
        rate, sigma = self.effective_rate, self.sigma
        sq = sigma * math.sqrt(years)
        d1 = (math.log(self.spot / strike) + (rate + 0.5 * sigma * sigma) * years) / sq
        d2 = d1 - sq
        if right is OptionRight.CALL:
            return self.spot * norm_cdf(d1) - strike * math.exp(
                -rate * years
            ) * norm_cdf(d2)
        return strike * math.exp(-rate * years) * norm_cdf(-d2) - self.spot * norm_cdf(
            -d1
        )


def _rows(vendor: SyntheticVendor) -> tuple[list[dict[str, str]], ...]:
    quote: list[dict[str, str]] = []
    oi: list[dict[str, str]] = []
    greeks: list[dict[str, str]] = []
    listing: list[dict[str, str]] = []
    for expiration in vendor.expirations:
        years = vendor.years(expiration)
        for index, strike in enumerate(vendor.strikes):
            for right in (OptionRight.CALL, OptionRight.PUT):
                inputs = BlackScholesInputs(
                    spot=vendor.spot,
                    strike=float(strike),
                    time_to_expiry=years,
                    implied_vol=vendor.sigma,
                    rate=vendor.effective_rate,
                )
                if inputs.is_degenerate():
                    continue
                delta = bs_delta(inputs, right)
                if not (0.02 < abs(delta) < 0.98):
                    continue
                price = vendor.price(float(strike), years, right)
                if price <= 0.10:
                    continue
                identity = {
                    "symbol": "SPXW",
                    "expiration": expiration.isoformat(),
                    "strike": f"{strike}.000",
                    "right": right.value.upper(),
                }
                stamp = (vendor.valuation - timedelta(seconds=index % 7)).strftime(
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
                        # Four decimals, exactly as the vendor reports it, so
                        # the reconstruction meets a realistic noise floor.
                        "implied_vol": f"{vendor.sigma:.4f}",
                        "delta": f"{delta:.4f}",
                        "iv_error": "0.0000",
                        "underlying_timestamp": vendor.valuation.strftime(
                            "%Y-%m-%dT%H:%M:%S.000"
                        ),
                        "underlying_price": f"{vendor.spot:.4f}",
                    }
                )
                listing.append(dict(identity))
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


def _bodies(vendor: SyntheticVendor) -> dict[str, str]:
    quote, oi, greeks, listing = _rows(vendor)
    stamp = vendor.valuation.strftime("%Y-%m-%dT%H:%M:%S.000")
    return {
        INDEX_PRICE: (
            f'timestamp,symbol,price\n{stamp},"SPX",{vendor.spot + 0.27:.2f}\n'
        ),
        OPTION_QUOTE: _csv(
            quote,
            ("timestamp", "symbol", "expiration", "strike", "right", "bid", "ask"),
        ),
        OPTION_OPEN_INTEREST: _csv(
            oi, ("symbol", "expiration", "strike", "right", "open_interest")
        ),
        OPTION_GREEKS: _csv(
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
        OPTION_CONTRACT_LIST: _csv(
            listing, ("symbol", "expiration", "strike", "right")
        ),
    }


def _parameters(vendor: SyntheticVendor, endpoint: str) -> dict[str, Any]:
    """What the request for one endpoint carried, including the rate."""
    if endpoint == INDEX_PRICE:
        return {"symbol": "SPX"}
    params: dict[str, Any] = {"symbol": "SPXW", "max_dte": 60}
    if endpoint == OPTION_GREEKS:
        params.update(
            {
                "rate_type": "sofr",
                "rate_value": vendor.wire_rate_value,
                "annual_dividend": 0.0,
                "version": "latest",
            }
        )
    return params


def write_capture(root: pathlib.Path, vendor: SyntheticVendor) -> pathlib.Path:
    """Write a capture directory that passes every verification in load_capture."""
    (root / "raw").mkdir(parents=True, exist_ok=True)
    bodies = _bodies(vendor)
    session_id = "capture-synthetic"
    stamp = vendor.valuation.astimezone(ZoneInfo("UTC"))

    records: list[ManifestRecord] = []
    planned: list[dict[str, Any]] = []
    for sequence, (endpoint, body) in enumerate(bodies.items(), start=1):
        name = endpoint.strip("/").replace("/", "-") + ".raw"
        raw = body.encode("utf-8")
        (root / "raw" / name).write_bytes(raw)
        canonical = canonical_parameters(_parameters(vendor, endpoint))
        stamped = planned_request_hash(SPEC_FINGERPRINT, endpoint, canonical)
        records.append(
            ManifestRecord(
                record_id=f"{session_id}-{sequence:04d}",
                endpoint=endpoint,
                payload_hash=hashlib.sha256(raw).hexdigest(),
                parameter_hash=hashlib.sha256(
                    json.dumps(canonical, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                payload_location=name,
                request_id=f"req-{sequence:04d}",
                request_sequence=sequence,
                http_status=200,
                byte_length=len(raw),
                capture_origin=CaptureOrigin.LOCAL_TERMINAL_CAPTURE,
                capture_session_id=session_id,
                request_spec_fingerprint=SPEC_FINGERPRINT,
                planned_request_hash=stamped,
                effective_valuation_timestamp=stamp,
            )
        )
        planned.append(
            {
                "endpoint": endpoint,
                "safe_path": endpoint,
                "canonical_query_parameters": [list(p) for p in canonical],
                "required_tier": "standard",
                "request_spec_hash": stamped,
            }
        )

    manifest = RawCaptureManifest(
        session_id=session_id,
        records=tuple(records),
        declared_capture_origin=CaptureOrigin.LOCAL_TERMINAL_CAPTURE,
    )
    (root / "manifest.json").write_text(
        json.dumps(manifest.as_dict(), sort_keys=True, default=str), encoding="utf-8"
    )

    intent: dict[str, Any] = {
        "schema_version": "raw-capture-intent/2.1.23",
        "session_id": session_id,
        "request_plan": {"requests": planned, "request_count": len(planned)},
    }
    if vendor.declared_economic_rate is not None:
        intent["rate_semantics"] = {
            "economic_rate_decimal": vendor.declared_economic_rate,
            "economic_rate_percent": vendor.declared_economic_rate * 100.0,
        }
    (root / "run-intent.json").write_text(json.dumps(intent), encoding="utf-8")
    return root


def rewrite_manifest(root: pathlib.Path, mutate: Any) -> None:
    """Apply ``mutate`` to the stored manifest payload and write it back.

    Deliberately does *not* recompute ``manifest_hash``: the tests that use
    this are checking that certification notices when a descriptor and its
    digest stop agreeing.
    """
    payload = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    mutate(payload)
    (root / "manifest.json").write_text(
        json.dumps(payload, default=str), encoding="utf-8"
    )


def rewrite_intent(root: pathlib.Path, mutate: Any) -> None:
    """Apply ``mutate`` to the stored run intent and write it back."""
    payload = json.loads((root / "run-intent.json").read_text(encoding="utf-8"))
    mutate(payload)
    (root / "run-intent.json").write_text(json.dumps(payload), encoding="utf-8")
