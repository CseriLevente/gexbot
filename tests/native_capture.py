"""A synthetic capture in the vendor's *native* v3 schema, for normalizer tests.

The column sets, quoting, strike text (``6000.000``), timestamp format and
endpoint order reproduce what the 2026-09-02 capture carried under
``thetadata-v3-parser/2.1.17``. The numbers are invented and the manifest is
stamped ``OFFLINE_FIXTURE``, so everything normalized from it is labelled
``SYNTHETIC``. Nothing here is market data.

Scenario (session Tuesday 2026-09-08, prior session Friday 2026-09-04):

- Eight clean contracts (6000/6050 CALL/PUT for 09-08 and 09-09) with quotes,
  Greeks and prior-session open interest, all observed at 09:40:10 ET and
  received between 09:40:10.5 and 09:40:13 ET -- so the 09:41 grid decision can
  pass and 09:42 is already stale under the 60-second budget.
- Out-of-universe contracts (expiry 09-18, ten calendar days out) carry the
  problem rows: zero IV, a vendor IV error, an out-of-range delta, a NaN, an
  open-interest row stamped on a Saturday, one stamped a day earlier, one that
  is not an integer, and one identity with no open-interest row at all.
- The quote payload additionally carries a row whose vendor time postdates the
  receipt, a zero-ask row, a zero-bid row, an unparseable timestamp, an exact
  duplicate of a clean row (coalesced), an identity absent from the inventory
  and a non-SPXW symbol.
- Repeated identities that disagree (v2.1.35 review finding): the far 6200 CALL
  appears twice in the quote payload with different prices and twice in the
  Greeks payload with different deltas; both groups are excluded whole. One
  clean contract's open-interest row is repeated verbatim and is coalesced.
- The expired 09-04 expiry is retained with 09-04 timestamps, as the vendor does.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from src.adapters.raw_store import CaptureOrigin
from src.adapters.thetadata.capture_certification import (
    INDEX_PRICE,
    OPTION_CONTRACT_LIST,
    OPTION_GREEKS,
    OPTION_OPEN_INTEREST,
    OPTION_QUOTE,
)
from src.gex.sessions import EASTERN
from tests.synthetic_capture import SyntheticVendor, write_capture

SESSION = date(2026, 9, 8)
PRIOR_SESSION = date(2026, 9, 4)
OBSERVED = datetime(2026, 9, 8, 9, 40, 10, tzinfo=EASTERN)
CLEAN_EXPIRIES = (date(2026, 9, 8), date(2026, 9, 9))
FAR_EXPIRY = date(2026, 9, 18)
EXPIRED = date(2026, 9, 4)
STRIKES = (6000, 6050)
QUOTE_COLUMNS = (
    "timestamp",
    "symbol",
    "expiration",
    "strike",
    "right",
    "bid_size",
    "bid_exchange",
    "bid",
    "bid_condition",
    "ask_size",
    "ask_exchange",
    "ask",
    "ask_condition",
)
OI_COLUMNS = ("timestamp", "symbol", "expiration", "strike", "right", "open_interest")
GREEKS_COLUMNS = (
    "symbol",
    "expiration",
    "strike",
    "right",
    "timestamp",
    "bid",
    "ask",
    "delta",
    "theta",
    "vega",
    "rho",
    "epsilon",
    "lambda",
    "implied_vol",
    "iv_error",
    "underlying_timestamp",
    "underlying_price",
)
LIST_COLUMNS = ("symbol", "expiration", "strike", "right")
#: Receipts, in the order the real capture issued its requests.
RECEIPTS = {
    INDEX_PRICE: OBSERVED + timedelta(milliseconds=500),
    OPTION_QUOTE: OBSERVED + timedelta(seconds=1),
    OPTION_OPEN_INTEREST: OBSERVED + timedelta(seconds=1, milliseconds=300),
    OPTION_GREEKS: OBSERVED + timedelta(seconds=2),
    OPTION_CONTRACT_LIST: OBSERVED + timedelta(seconds=3),
}


def vendor_stamp(moment: datetime) -> str:
    """The vendor's naive Eastern wall clock with millisecond precision."""
    local = moment.astimezone(EASTERN)
    return local.strftime("%Y-%m-%dT%H:%M:%S.") + f"{local.microsecond // 1000:03d}"


def identity(expiry: date, strike: int, right: str) -> dict[str, str]:
    return {
        "symbol": "SPXW",
        "expiration": expiry.isoformat(),
        "strike": f"{strike}.000",
        "right": right,
    }


def canonical(expiry: date, strike: int, right: str) -> str:
    return f"SPXW|{expiry.isoformat()}|{strike}|{right}"


def _quoted(value: str) -> str:
    return f'"{value}"'


def _csv(columns: tuple[str, ...], rows: list[dict[str, str]]) -> str:
    quoted = {"symbol", "expiration", "right"}
    lines = [",".join(columns)]
    for row in rows:
        lines.append(
            ",".join(_quoted(row[c]) if c in quoted else row[c] for c in columns)
        )
    return "\n".join(lines) + "\n"


@dataclass
class NativeScenario:
    """Rows per endpoint, mutable so a test can add one native anomaly."""

    quotes: list[dict[str, str]] = field(default_factory=list)
    open_interest: list[dict[str, str]] = field(default_factory=list)
    greeks: list[dict[str, str]] = field(default_factory=list)
    listing: list[dict[str, str]] = field(default_factory=list)
    index: list[dict[str, str]] = field(default_factory=list)

    def bodies(self) -> dict[str, str]:
        return {
            INDEX_PRICE: _csv(("timestamp", "symbol", "price"), self.index),
            OPTION_QUOTE: _csv(QUOTE_COLUMNS, self.quotes),
            OPTION_OPEN_INTEREST: _csv(OI_COLUMNS, self.open_interest),
            OPTION_GREEKS: _csv(GREEKS_COLUMNS, self.greeks),
            OPTION_CONTRACT_LIST: _csv(LIST_COLUMNS, self.listing),
        }


def quote_row(
    ident: dict[str, str],
    *,
    at: datetime = OBSERVED,
    bid: str = "12.30",
    ask: str = "12.60",
    bid_size: str = "25",
    ask_size: str = "18",
) -> dict[str, str]:
    return {
        **ident,
        "timestamp": vendor_stamp(at),
        "bid_size": bid_size,
        "bid_exchange": "5",
        "bid": bid,
        "bid_condition": "50",
        "ask_size": ask_size,
        "ask_exchange": "5",
        "ask": ask,
        "ask_condition": "50",
    }


def greeks_row(
    ident: dict[str, str],
    *,
    at: datetime = OBSERVED + timedelta(milliseconds=400),
    implied_vol: str = "0.1450",
    delta: str = "0.4800",
    iv_error: str = "0.0000",
) -> dict[str, str]:
    return {
        **ident,
        "timestamp": vendor_stamp(at),
        "bid": "12.3000",
        "ask": "12.6000",
        "delta": delta,
        "theta": "-1.2000",
        "vega": "300.0000",
        "rho": "-10.0000",
        "epsilon": "10.0000",
        "lambda": "-50.0000",
        "implied_vol": implied_vol,
        "iv_error": iv_error,
        "underlying_timestamp": vendor_stamp(OBSERVED),
        "underlying_price": "6001.2500",
    }


def oi_row(
    ident: dict[str, str],
    *,
    at: datetime = datetime(2026, 9, 8, 6, 30, 3, tzinfo=EASTERN),
    quantity: str = "125",
) -> dict[str, str]:
    return {**ident, "timestamp": vendor_stamp(at), "open_interest": quantity}


def scenario() -> NativeScenario:
    built = NativeScenario()
    built.index = [
        {"timestamp": vendor_stamp(OBSERVED), "symbol": "SPX", "price": "6001.20"}
    ]
    far = [
        identity(FAR_EXPIRY, strike, right)
        for strike in STRIKES
        for right in ("CALL", "PUT")
    ]
    for expiry in CLEAN_EXPIRIES:
        for strike in STRIKES:
            for right in ("CALL", "PUT"):
                ident = identity(expiry, strike, right)
                built.listing.append(ident)
                built.quotes.append(quote_row(ident))
                built.greeks.append(greeks_row(ident))
                built.open_interest.append(oi_row(ident))
    # Out-of-universe contracts carry every kind of problem row.
    zero_iv, iv_error, bad_delta, nan = far
    for ident in far:
        built.listing.append(ident)
        built.quotes.append(quote_row(ident, bid="0.00", bid_size="0"))
    built.greeks.extend(
        [
            greeks_row(zero_iv, implied_vol="0.0000", delta="0.0000"),
            greeks_row(iv_error, iv_error="0.6000"),
            greeks_row(bad_delta, delta="1.2000"),
            greeks_row(nan, implied_vol="nan"),
        ]
    )
    saturday = datetime(2026, 9, 5, 6, 30, 3, tzinfo=EASTERN)
    a_day_earlier = datetime(2026, 9, 4, 6, 30, 3, tzinfo=EASTERN)
    built.open_interest.extend(
        [
            oi_row(zero_iv, at=saturday),
            oi_row(iv_error, at=a_day_earlier),
            oi_row(bad_delta, quantity="12.0"),
            # ``nan`` deliberately has no open-interest row: unavailable, not zero.
        ]
    )
    # Retained expired contracts, stamped on their last live session.
    expired_close = datetime(2026, 9, 4, 15, 59, 31, 827000, tzinfo=EASTERN)
    for right in ("CALL", "PUT"):
        ident = identity(EXPIRED, 6000, right)
        built.listing.append(ident)
        built.quotes.append(quote_row(ident, at=expired_close, bid="0.05", ask="0.15"))
        built.greeks.append(
            greeks_row(ident, at=expired_close, implied_vol="0.0000", delta="0.0000")
        )
        built.open_interest.append(
            oi_row(ident, at=datetime(2026, 9, 4, 6, 30, 3, tzinfo=EASTERN))
        )
    # Native anomalies inside the quote payload.
    clean = identity(CLEAN_EXPIRIES[0], STRIKES[0], "CALL")
    built.quotes.extend(
        [
            # Vendor clock 200 ms ahead of the local receipt.
            quote_row(
                identity(FAR_EXPIRY, 6100, "CALL"),
                at=RECEIPTS[OPTION_QUOTE] + timedelta(milliseconds=200),
            ),
            quote_row(identity(FAR_EXPIRY, 6100, "PUT"), ask="0.00", ask_size="0"),
            {
                **quote_row(identity(FAR_EXPIRY, 6150, "CALL")),
                "timestamp": "not-a-time",
            },
            quote_row(clean),  # exact duplicate of a clean row: coalesced
            quote_row(identity(FAR_EXPIRY, 7000, "PUT")),  # not in the inventory
            {**quote_row(identity(FAR_EXPIRY, 6150, "PUT")), "symbol": "SPX"},
            # Two observations of one identity that disagree: excluded whole.
            quote_row(identity(FAR_EXPIRY, 6200, "CALL")),
            quote_row(identity(FAR_EXPIRY, 6200, "CALL"), bid="20.30", ask="20.60"),
        ]
    )
    built.greeks.extend(
        [
            greeks_row(identity(FAR_EXPIRY, 6200, "CALL")),
            greeks_row(identity(FAR_EXPIRY, 6200, "CALL"), delta="0.5100"),
        ]
    )
    built.open_interest.append(oi_row(clean))  # verbatim repeat: coalesced
    for extra in (
        identity(FAR_EXPIRY, 6100, "CALL"),
        identity(FAR_EXPIRY, 6100, "PUT"),
        identity(FAR_EXPIRY, 6150, "CALL"),
        identity(FAR_EXPIRY, 6150, "PUT"),
        identity(FAR_EXPIRY, 6200, "CALL"),
    ):
        built.listing.append(extra)
    return built


def write_native_capture(
    root: pathlib.Path,
    built: NativeScenario | None = None,
    *,
    receipts: dict[str, datetime] | None = None,
    attempts: dict[str, datetime | None] | None | bool = True,
    origin: CaptureOrigin = CaptureOrigin.OFFLINE_FIXTURE,
    valuation: datetime = OBSERVED,
    omit: tuple[str, ...] = (),
) -> pathlib.Path:
    """Write the scenario as a verifiable capture with recorded receipts.

    ``attempts=True`` writes an attempt log agreeing with the manifest receipts
    to the millisecond; a mapping overrides per-endpoint attempt receipts;
    ``False`` writes no attempt log at all. ``omit`` leaves those endpoints
    out entirely -- no body, no receipt, no attempt -- as a request that was
    never answered leaves a cycle (v2.1.36).
    """
    built = built or scenario()
    receipts = {
        endpoint: received
        for endpoint, received in (RECEIPTS if receipts is None else receipts).items()
        if endpoint not in omit
    }
    timing = {
        endpoint: (received - timedelta(milliseconds=600), received)
        for endpoint, received in receipts.items()
    }
    log: dict[str, datetime | None] | None
    if attempts is True:
        log = dict(receipts)
    elif attempts is False:
        log = None
    else:
        log = attempts
    vendor = SyntheticVendor(
        valuation=valuation,
        declared_economic_rate=0.042,
        wire_rate_value=0.042,
        expirations=(*CLEAN_EXPIRIES, FAR_EXPIRY),
    )
    return write_capture(
        root,
        vendor,
        bodies={e: b for e, b in built.bodies().items() if e not in omit},
        timing=timing,
        attempts=log,
        origin=origin,
    )


def parameters_of(root: pathlib.Path) -> dict[str, Any]:
    """The recorded Greeks request, for tests that assert the model identity."""
    from src.adapters.thetadata.capture_certification import load_capture

    return dict(load_capture(root).request.parameters[OPTION_GREEKS])
