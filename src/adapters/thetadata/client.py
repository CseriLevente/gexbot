"""ThetaData v3 chain assembly.

The interesting work is the join, not the HTTP. A GEX snapshot needs three
responses stitched together on ``(symbol, expiration, strike, right)``:

* quotes -- bid/ask, for spread and crossed-market diagnostics
* open interest -- the GEX weight
* first-order greeks -- ``implied_vol`` and ``underlying_price``

Second-order greeks (gamma) are optional and Pro-only; when absent the engine's
shadow pricer fills in. That is the recommended configuration, not a degraded
one -- see ``src/adapters/thetadata/endpoints.py``.

HTTP is injected as a ``transport`` callable so the join is unit-testable with no
Theta Terminal running and no subscription. The default transport is left
unimplemented on purpose: wiring it up is a deliberate step taken once a
subscription exists, not something that happens by accident during development.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from datetime import date, datetime

from src.adapters.thetadata.endpoints import (
    DEFAULT_BASE_URL,
    Endpoint,
    Tier,
    build_url,
    requires_shadow_gamma,
    tier_satisfies,
    MINIMUM_TIER,
)
from src.domain.contracts import (
    ChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionRight,
    OptionRoot,
)
from src.gex.sessions import to_eastern

Transport = Callable[[str], str]
ContractKey = tuple[str, date, float, str]


class ThetaDataError(RuntimeError):
    pass


def parse_csv(text: str) -> list[dict[str, str]]:
    """Parse a ThetaData CSV response into row dicts.

    ThetaData emits a header row, so the column order is read from the payload
    rather than assumed from :data:`RESPONSE_FIELDS`. The constants there are for
    documentation and validation; trusting them positionally would break silently
    the first time a vendor adds a column.
    """
    stripped = text.strip()
    if not stripped:
        return []
    reader = csv.DictReader(io.StringIO(stripped))
    return [
        {(key or "").strip(): (value or "").strip() for key, value in row.items()}
        for row in reader
    ]


def _to_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed


def _to_int(value: str | None) -> int | None:
    parsed = _to_float(value)
    return None if parsed is None else int(parsed)


def parse_expiration(value: str) -> date:
    """Accept both ``YYYY-MM-DD`` and ``YYYYMMDD``, the two forms v3 emits."""
    cleaned = value.strip()
    if "-" in cleaned:
        return date.fromisoformat(cleaned)
    if len(cleaned) == 8 and cleaned.isdigit():
        return date(int(cleaned[:4]), int(cleaned[4:6]), int(cleaned[6:]))
    raise ThetaDataError(f"unrecognised expiration format: {value!r}")


def parse_right(value: str) -> OptionRight:
    normalised = value.strip().lower()
    if normalised in ("c", "call"):
        return OptionRight.CALL
    if normalised in ("p", "put"):
        return OptionRight.PUT
    raise ThetaDataError(f"unrecognised right: {value!r}")


def parse_root(value: str) -> OptionRoot:
    normalised = value.strip().upper()
    try:
        return OptionRoot(normalised)
    except ValueError as exc:
        raise ThetaDataError(
            f"unsupported root {value!r}; only SPX and SPXW are modelled"
        ) from exc


def row_key(row: dict[str, str]) -> ContractKey:
    return (
        row["symbol"].strip().upper(),
        parse_expiration(row["expiration"]),
        float(row["strike"]),
        parse_right(row["right"]).value,
    )


def index_rows(rows: list[dict[str, str]]) -> dict[ContractKey, dict[str, str]]:
    return {row_key(row): row for row in rows}


def assemble_chain(
    *,
    as_of: datetime,
    spot: float,
    quote_rows: list[dict[str, str]],
    open_interest_rows: list[dict[str, str]],
    first_order_rows: list[dict[str, str]],
    second_order_rows: list[dict[str, str]] | None = None,
    open_interest_asof: date | None = None,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
    options_feed_timestamp: datetime | None = None,
    spot_feed_timestamp: datetime | None = None,
) -> ChainSnapshot:
    """Join the responses into one ``ChainSnapshot``.

    Quotes drive the iteration: a contract with no quote has no book and cannot
    contribute a meaningful spread or crossed-market signal. Missing OI or IV
    leaves those fields ``None``, and the engine then drops the contract and
    counts it against ``chain_completeness`` -- which is the behaviour we want,
    because a partial join must be visible in the confidence score rather than
    quietly shrinking the chain.
    """
    oi_by_key = index_rows(open_interest_rows)
    first_by_key = index_rows(first_order_rows)
    second_by_key = index_rows(second_order_rows or [])

    quotes: list[OptionQuote] = []
    for row in quote_rows:
        key = row_key(row)
        first = first_by_key.get(key, {})
        second = second_by_key.get(key, {})
        contract = OptionContract(
            root=parse_root(row["symbol"]),
            expiry=key[1],
            strike=key[2],
            right=OptionRight(key[3]),
        )
        quotes.append(
            OptionQuote(
                contract=contract,
                timestamp=to_eastern(as_of),
                bid=_to_float(row.get("bid")),
                ask=_to_float(row.get("ask")),
                bid_size=_to_int(row.get("bid_size")),
                ask_size=_to_int(row.get("ask_size")),
                open_interest=_to_int(oi_by_key.get(key, {}).get("open_interest")),
                open_interest_asof=open_interest_asof,
                implied_vol=_to_float(
                    first.get("implied_vol") or second.get("implied_vol")
                ),
                delta=_to_float(first.get("delta")),
                gamma=_to_float(second.get("gamma")),
                vega=_to_float(first.get("vega")),
                theta=_to_float(first.get("theta")),
            )
        )

    return ChainSnapshot(
        as_of=to_eastern(as_of),
        spot=spot,
        quotes=tuple(quotes),
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
        options_feed_timestamp=options_feed_timestamp,
        spot_feed_timestamp=spot_feed_timestamp,
        source="thetadata",
    )


def _not_wired(url: str) -> str:
    raise NotImplementedError(
        "No ThetaData transport configured. Pass transport=<callable> once a "
        "subscription and a running Theta Terminal exist. See "
        "docs/handoff/data-requirements.md before buying a tier."
    )


class ThetaDataClient:
    """Thin request builder around the verified v3 endpoints."""

    name = "thetadata"

    def __init__(
        self,
        *,
        tier: Tier = Tier.STANDARD,
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport | None = None,
    ) -> None:
        self.tier = tier
        self.base_url = base_url
        self._transport = transport or _not_wired

    @property
    def needs_shadow_gamma(self) -> bool:
        return requires_shadow_gamma(self.tier)

    def _get(self, endpoint: Endpoint, params: dict[str, str | int | float]) -> str:
        required = MINIMUM_TIER[endpoint]
        if not tier_satisfies(self.tier, required):
            raise ThetaDataError(
                f"{endpoint.value} requires the {required.value} tier but the "
                f"client is configured for {self.tier.value}"
            )
        return self._transport(
            build_url(endpoint, params, base_url=self.base_url)
        )

    def option_quotes(self, *, symbol: str, expiration: str = "*") -> list[dict[str, str]]:
        return parse_csv(
            self._get(
                Endpoint.OPTION_QUOTE_SNAPSHOT,
                {"symbol": symbol, "expiration": expiration},
            )
        )

    def option_open_interest(
        self, *, symbol: str, expiration: str = "*"
    ) -> list[dict[str, str]]:
        return parse_csv(
            self._get(
                Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT,
                {"symbol": symbol, "expiration": expiration},
            )
        )

    def option_first_order_greeks(
        self, *, symbol: str, expiration: str = "*"
    ) -> list[dict[str, str]]:
        """delta/theta/vega/rho plus ``implied_vol`` -- Standard tier."""
        return parse_csv(
            self._get(
                Endpoint.OPTION_GREEKS_FIRST_ORDER,
                {"symbol": symbol, "expiration": expiration},
            )
        )

    def option_second_order_greeks(
        self, *, symbol: str, expiration: str = "*"
    ) -> list[dict[str, str]]:
        """Vendor gamma -- Pro tier. Optional; the shadow pricer covers it."""
        return parse_csv(
            self._get(
                Endpoint.OPTION_GREEKS_SECOND_ORDER,
                {"symbol": symbol, "expiration": expiration},
            )
        )

    def index_price(self, *, symbol: str) -> list[dict[str, str]]:
        return parse_csv(
            self._get(Endpoint.INDEX_PRICE_SNAPSHOT, {"symbol": symbol})
        )
