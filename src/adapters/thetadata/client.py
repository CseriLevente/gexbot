"""ThetaData v3 chain assembly.

The interesting work is the join and the timestamps, not the HTTP. A GEX
snapshot needs several responses stitched together on the canonical contract
identity ``(root, expiry, strike, right)``:

* quotes -- bid/ask, plus the quote clock
* open interest -- the GEX weight, plus its settlement date
* first-order greeks -- ``implied_vol`` and ``underlying_price``, plus their own
  clock
* second-order greeks (optional, Pro tier) -- vendor ``gamma``

Each source carries its own timestamp and **every one is preserved**. Nothing is
back-stamped to the request instant: a five-minute-old greeks record must still
read as five minutes old after the join, or freshness becomes a number we
assigned to ourselves.

Calculation parameters (rate, dividend, greeks version, stock price source) are
sent explicitly and stored in the snapshot metadata rather than left to vendor
defaults. Relying on a default means the vendor can change our numbers without
us changing anything, and the change would be invisible.
"""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from src.adapters.raw_store import CaptureSession, NullRawStore, RawResponseStore
from src.adapters.thetadata.endpoints import (
    DEFAULT_BASE_URL,
    MINIMUM_TIER,
    Endpoint,
    Tier,
    build_url,
    requires_shadow_gamma,
    tier_satisfies,
)
from src.adapters.transport import HttpResponse, HttpTransport
from src.domain.contracts import (
    ChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionRight,
    OptionRoot,
    SnapshotClocks,
)
from src.domain.iv import IVSource, build_iv_quote
from src.domain.timestamps import ContractTimestamps
from src.gex.sessions import to_eastern

ContractKey = tuple[str, date, float, str]


class ThetaDataError(RuntimeError):
    pass


class ThetaDataSchemaError(ThetaDataError):
    """A response is missing a column the parser requires."""


# --- Calculation parameters -------------------------------------------------


@dataclass(frozen=True, slots=True)
class GreeksParameters:
    """Model inputs sent to the vendor's greeks endpoints.

    Every one of these changes the returned IV and greeks. Sending them
    explicitly -- and storing them -- is what makes a later disagreement between
    our gamma and the vendor's investigable rather than mysterious.
    """

    # "latest" or a pinned version. Pinning is safer for research reproducibility;
    # "latest" means the vendor can change historical answers under you.
    greeks_version: str = "latest"
    rate_type: str = "sofr"
    rate_value: float | None = None
    annual_dividend: float | None = None
    # Which underlying print the vendor should use.
    stock_price_source: str = "vendor_default"
    use_market_value: bool | None = None

    def as_query(self) -> dict[str, str | float]:
        """Only the parameters the vendor actually accepts, omitting unset ones.

        Sending an unset parameter as an empty string is not the same as omitting
        it, and some vendors treat the two differently.
        """
        query: dict[str, str | float] = {
            "version": self.greeks_version,
            "rate_type": self.rate_type,
        }
        if self.rate_value is not None:
            query["rate_value"] = self.rate_value
        if self.annual_dividend is not None:
            query["annual_dividend"] = self.annual_dividend
        if self.use_market_value is not None:
            query["use_market_value"] = str(self.use_market_value).lower()
        return query

    def as_dict(self) -> dict[str, Any]:
        return {
            "greeks_version": self.greeks_version,
            "rate_type": self.rate_type,
            "rate_value": self.rate_value,
            "annual_dividend": self.annual_dividend,
            "stock_price_source": self.stock_price_source,
            "use_market_value": self.use_market_value,
        }


@dataclass(frozen=True, slots=True)
class ChainRequest:
    """Server-side filters. Sent only to endpoints that accept them."""

    symbol: str
    expiration: str = "*"
    strike: str | None = None
    right: str | None = None
    max_dte: int | None = None
    strike_range: int | None = None
    min_time: str | None = None

    def as_query(self, *, supports_filters: bool = True) -> dict[str, str | int]:
        query: dict[str, str | int] = {
            "symbol": self.symbol,
            "expiration": self.expiration,
        }
        if not supports_filters:
            return query
        if self.strike is not None:
            query["strike"] = self.strike
        if self.right is not None:
            query["right"] = self.right
        if self.max_dte is not None:
            query["max_dte"] = self.max_dte
        if self.strike_range is not None:
            query["strike_range"] = self.strike_range
        if self.min_time is not None:
            query["min_time"] = self.min_time
        return query


@dataclass(frozen=True, slots=True)
class ThetaDataSettings:
    """Connection and credential configuration.

    ThetaData's current access model routes requests through a local Theta
    Terminal, so there is normally no remote credential. The username/password
    env-var names are still configurable because the access mode may change, and
    because the point of this structure is that a credential is *never* a
    literal in the repository.
    """

    base_url: str = DEFAULT_BASE_URL
    tier: Tier = Tier.STANDARD
    timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 5.0
    max_retries: int = 3
    backoff_base_seconds: float = 0.25
    username_env: str = "THETADATA_USERNAME"
    # This is the NAME of an environment variable to read, not a secret; the
    # value never appears in the repository. `tests/unit/test_architecture.py`
    # scans for real credential literals and distinguishes the two cases.
    password_env: str = "THETADATA_PASSWORD"  # noqa: S105
    # "local_terminal" (no credential) or "basic" (env-provided credential).
    auth_mode: str = "local_terminal"

    def credentials(self) -> tuple[str | None, str | None]:
        """Read credentials from the environment. Never from a file in the repo."""
        if self.auth_mode == "local_terminal":
            return None, None
        return os.environ.get(self.username_env), os.environ.get(self.password_env)

    def has_credentials(self) -> bool:
        username, password = self.credentials()
        return bool(username and password)

    def as_dict(self) -> dict[str, Any]:
        """Serialisable settings. Deliberately contains no secret value -- only
        the *names* of the environment variables to read.
        """
        return {
            "base_url": self.base_url,
            "tier": self.tier.value,
            "timeout_seconds": self.timeout_seconds,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "max_retries": self.max_retries,
            "backoff_base_seconds": self.backoff_base_seconds,
            "auth_mode": self.auth_mode,
            "username_env": self.username_env,
            "password_env": self.password_env,
        }


# --- Parsing ----------------------------------------------------------------

REQUIRED_COLUMNS: dict[Endpoint, frozenset[str]] = {
    Endpoint.OPTION_QUOTE_SNAPSHOT: frozenset(
        {"symbol", "expiration", "strike", "right", "bid", "ask"}
    ),
    Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT: frozenset(
        {"symbol", "expiration", "strike", "right", "open_interest"}
    ),
    Endpoint.OPTION_GREEKS_FIRST_ORDER: frozenset(
        {"symbol", "expiration", "strike", "right", "implied_vol"}
    ),
    Endpoint.OPTION_GREEKS_SECOND_ORDER: frozenset(
        {"symbol", "expiration", "strike", "right", "gamma"}
    ),
}


def parse_csv(text: str) -> list[dict[str, str]]:
    """Parse a ThetaData CSV response into row dicts.

    The header row drives the column mapping rather than the documented order:
    a vendor adding a column must not shift every field by one. Unknown extra
    columns are carried through untouched.
    """
    stripped = text.strip()
    if not stripped:
        return []
    reader = csv.DictReader(io.StringIO(stripped))
    rows: list[dict[str, str]] = []
    for row in reader:
        cleaned = {
            (key or "").strip(): (value or "").strip()
            for key, value in row.items()
            if key is not None
        }
        if any(cleaned.values()):
            rows.append(cleaned)
    return rows


def check_schema(rows: list[dict[str, str]], endpoint: Endpoint) -> None:
    """Fail loudly on a missing required column.

    Missing required fields must fail clearly rather than silently producing
    ``None`` for every contract, which would look like an empty market.
    """
    required = REQUIRED_COLUMNS.get(endpoint)
    if not required or not rows:
        return
    present = set(rows[0])
    missing = required - present
    if missing:
        raise ThetaDataSchemaError(
            f"{endpoint.value} response is missing required column(s) "
            f"{sorted(missing)}; present columns are {sorted(present)}"
        )


def detect_vendor_error(text: str) -> str | None:
    """Recognise a vendor error body returned with a 2xx status."""
    stripped = text.strip()
    if not stripped:
        return None
    lowered = stripped[:400].lower()
    for marker in ('{"error"', '"error":', "error_type", "no data for request"):
        if marker in lowered:
            return stripped[:400]
    return None


def _to_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_int(value: str | None) -> int | None:
    parsed = _to_float(value)
    return None if parsed is None else int(parsed)


def _to_datetime(value: str | None) -> datetime | None:
    """Parse a vendor timestamp and attach Eastern.

    ThetaData emits wall-clock timestamps without an offset. Attaching Eastern
    here is a *documented adapter assumption*, not a guess buried in the engine:
    the engine itself refuses naive datetimes precisely so this decision has to
    be made somewhere visible. See docs/OPEN_DECISIONS.md.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return to_eastern(parsed)


def parse_expiration(value: str) -> date:
    """Accept both ``YYYY-MM-DD`` and ``YYYYMMDD``, the two forms v3 emits."""
    cleaned = value.strip()
    if "-" in cleaned:
        try:
            return date.fromisoformat(cleaned)
        except ValueError as exc:
            raise ThetaDataError(f"unrecognised expiration format: {value!r}") from exc
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
    try:
        return (
            row["symbol"].strip().upper(),
            parse_expiration(row["expiration"]),
            float(row["strike"]),
            parse_right(row["right"]).value,
        )
    except KeyError as exc:
        raise ThetaDataSchemaError(f"row is missing join column {exc}") from exc


def index_rows(rows: list[dict[str, str]]) -> dict[ContractKey, dict[str, str]]:
    return {row_key(row): row for row in rows}


# --- Chain assembly ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChainAssemblyInputs:
    as_of: datetime
    spot: float
    quote_rows: list[dict[str, str]]
    open_interest_rows: list[dict[str, str]]
    first_order_rows: list[dict[str, str]]
    second_order_rows: list[dict[str, str]] = field(default_factory=list)
    open_interest_as_of: date | None = None
    risk_free_rate: float = 0.0
    dividend_yield: float = 0.0
    spot_timestamp: datetime | None = None
    clocks: SnapshotClocks = field(default_factory=SnapshotClocks)
    iv_source: IVSource = IVSource.VENDOR_DEFAULT_IV
    meta: dict[str, Any] = field(default_factory=dict)


def assemble_chain(inputs: ChainAssemblyInputs) -> ChainSnapshot:
    """Join the responses into one ``ChainSnapshot``.

    Quotes drive the iteration: a contract with no quote has no book and cannot
    contribute a spread or a crossed-market signal. Missing OI or IV leaves those
    fields ``None``, and the validation pass then drops the contract and counts
    it -- a partial join must be visible in the report rather than quietly
    shrinking the chain.
    """
    oi_by_key = index_rows(inputs.open_interest_rows)
    first_by_key = index_rows(inputs.first_order_rows)
    second_by_key = index_rows(inputs.second_order_rows)

    quotes: list[OptionQuote] = []
    for row in inputs.quote_rows:
        key = row_key(row)
        first = first_by_key.get(key, {})
        second = second_by_key.get(key, {})
        oi_row = oi_by_key.get(key, {})

        bid = _to_float(row.get("bid"))
        ask = _to_float(row.get("ask"))
        crossed = bid is not None and ask is not None and ask < bid

        contract = OptionContract(
            root=parse_root(row["symbol"]),
            expiry=key[1],
            strike=key[2],
            right=OptionRight(key[3]),
        )
        # Every source clock preserved separately. This is the whole point.
        timestamps = ContractTimestamps(
            quote_timestamp=_to_datetime(row.get("timestamp")),
            greeks_timestamp=_to_datetime(second.get("timestamp")),
            iv_timestamp=_to_datetime(first.get("timestamp")),
            underlying_timestamp=_to_datetime(
                first.get("underlying_timestamp") or second.get("underlying_timestamp")
            ),
            open_interest_as_of=inputs.open_interest_as_of,
            request_started_at=inputs.clocks.request_started_at,
            response_received_at=inputs.clocks.response_received_at,
            normalized_at=inputs.clocks.normalized_at,
        )
        quotes.append(
            OptionQuote(
                contract=contract,
                timestamps=timestamps,
                bid=bid,
                ask=ask,
                bid_size=_to_int(row.get("bid_size")),
                ask_size=_to_int(row.get("ask_size")),
                open_interest=_to_int(oi_row.get("open_interest")),
                iv=build_iv_quote(
                    bid_iv=_to_float(first.get("bid_iv")),
                    mid_iv=_to_float(first.get("mid_iv")),
                    ask_iv=_to_float(first.get("ask_iv")),
                    vendor_iv=_to_float(
                        first.get("implied_vol") or second.get("implied_vol")
                    ),
                    vendor_iv_error=_to_float(
                        first.get("iv_error") or second.get("iv_error")
                    ),
                    preferred_source=inputs.iv_source,
                    zero_bid=bid is not None and bid <= 0.0,
                    crossed=crossed,
                ),
                delta=_to_float(first.get("delta")),
                gamma=_to_float(second.get("gamma")),
                vega=_to_float(first.get("vega")),
                theta=_to_float(first.get("theta")),
                underlying_price=_to_float(
                    first.get("underlying_price") or second.get("underlying_price")
                ),
            )
        )

    return ChainSnapshot(
        as_of=to_eastern(inputs.as_of),
        spot=inputs.spot,
        quotes=tuple(quotes),
        risk_free_rate=inputs.risk_free_rate,
        dividend_yield=inputs.dividend_yield,
        clocks=inputs.clocks,
        spot_timestamp=inputs.spot_timestamp,
        source="thetadata",
        expected_contract_count=len(inputs.quote_rows),
        meta=dict(inputs.meta),
    )


# --- Client -----------------------------------------------------------------


class UnconfiguredTransport:
    """Explicit failure when no transport was supplied.

    Better a clear error than a silent fallback to synthetic data: a research
    run that quietly used made-up numbers is worse than one that stopped.
    """

    def get(
        self, url: str, params: Mapping[str, Any], timeout_seconds: float
    ) -> HttpResponse:
        raise NotImplementedError(
            "No ThetaData transport configured. Pass transport=HttpxTransport() "
            "once a subscription and a running Theta Terminal exist, or "
            "FakeTransport() in tests. See docs/THETADATA_INTEGRATION.md."
        )


class ThetaDataClient:
    """Request builder + parser over the verified v3 endpoints."""

    name = "thetadata"

    def __init__(
        self,
        *,
        settings: ThetaDataSettings | None = None,
        greeks: GreeksParameters | None = None,
        transport: HttpTransport | None = None,
        raw_store: RawResponseStore | None = None,
        clock: Any = None,
    ) -> None:
        self.settings = settings or ThetaDataSettings()
        self.greeks = greeks or GreeksParameters()
        self._transport: HttpTransport = transport or UnconfiguredTransport()
        self.raw_store: RawResponseStore = raw_store or NullRawStore()
        # Injected so tests are deterministic and the client stays clock-free.
        self._clock = clock or (lambda: datetime.now().astimezone())

    @property
    def tier(self) -> Tier:
        return self.settings.tier

    @property
    def needs_shadow_gamma(self) -> bool:
        return requires_shadow_gamma(self.tier)

    def effective_request_parameters(self) -> dict[str, Any]:
        """Everything that influenced the numbers, for snapshot metadata.

        Contains no credential -- only the names of the environment variables the
        settings would read.
        """
        return {
            "settings": self.settings.as_dict(),
            "greeks": self.greeks.as_dict(),
            "needs_shadow_gamma": self.needs_shadow_gamma,
        }

    def _get(
        self,
        endpoint: Endpoint,
        params: dict[str, Any],
        *,
        capture: CaptureSession | None = None,
    ) -> list[dict[str, str]]:
        required = MINIMUM_TIER[endpoint]
        if not tier_satisfies(self.tier, required):
            raise ThetaDataError(
                f"{endpoint.value} requires the {required.value} tier but the "
                f"client is configured for {self.tier.value}"
            )
        url = build_url(endpoint, params, base_url=self.settings.base_url)
        started = self._clock()
        response = self._transport.get(url, params, self.settings.timeout_seconds)
        received = self._clock()

        if capture is not None:
            capture.capture(
                endpoint=endpoint.value,
                query_params=dict(params),
                payload=response.text,
                request_started_at=started,
                response_received_at=received,
                http_status=response.status_code,
                request_id=response.request_id,
            )

        vendor_error = detect_vendor_error(response.text)
        if vendor_error:
            raise ThetaDataError(
                f"{endpoint.value} returned a vendor error body: {vendor_error}"
            )
        rows = parse_csv(response.text)
        check_schema(rows, endpoint)
        return rows

    def option_quotes(
        self, request: ChainRequest, *, capture: CaptureSession | None = None
    ) -> list[dict[str, str]]:
        return self._get(
            Endpoint.OPTION_QUOTE_SNAPSHOT, request.as_query(), capture=capture
        )

    def option_open_interest(
        self, request: ChainRequest, *, capture: CaptureSession | None = None
    ) -> list[dict[str, str]]:
        return self._get(
            Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT,
            request.as_query(),
            capture=capture,
        )

    def option_first_order_greeks(
        self, request: ChainRequest, *, capture: CaptureSession | None = None
    ) -> list[dict[str, str]]:
        """delta/theta/vega/rho plus ``implied_vol`` -- Standard tier.

        Calculation parameters are attached explicitly rather than left to the
        vendor default.
        """
        params = {**request.as_query(), **self.greeks.as_query()}
        return self._get(Endpoint.OPTION_GREEKS_FIRST_ORDER, params, capture=capture)

    def option_second_order_greeks(
        self, request: ChainRequest, *, capture: CaptureSession | None = None
    ) -> list[dict[str, str]]:
        """Vendor gamma -- Pro tier. Optional; the shadow pricer covers it."""
        params = {**request.as_query(), **self.greeks.as_query()}
        return self._get(Endpoint.OPTION_GREEKS_SECOND_ORDER, params, capture=capture)

    def index_price(
        self, symbol: str, *, capture: CaptureSession | None = None
    ) -> list[dict[str, str]]:
        return self._get(
            Endpoint.INDEX_PRICE_SNAPSHOT, {"symbol": symbol}, capture=capture
        )

    def fetch_chain(
        self,
        request: ChainRequest,
        *,
        as_of: datetime,
        spot: float,
        spot_timestamp: datetime | None = None,
        open_interest_as_of: date | None = None,
        risk_free_rate: float = 0.0,
        dividend_yield: float = 0.0,
        iv_source: IVSource = IVSource.VENDOR_DEFAULT_IV,
        capture: CaptureSession | None = None,
    ) -> ChainSnapshot:
        """Pull and join a full chain.

        Second-order greeks are requested only when the tier allows them, so a
        Standard-tier client produces a complete chain without a failed request.
        """
        started = self._clock()
        quotes = self.option_quotes(request, capture=capture)
        open_interest = self.option_open_interest(request, capture=capture)
        first_order = self.option_first_order_greeks(request, capture=capture)
        second_order: list[dict[str, str]] = []
        if not self.needs_shadow_gamma:
            second_order = self.option_second_order_greeks(request, capture=capture)
        received = self._clock()

        return assemble_chain(
            ChainAssemblyInputs(
                as_of=as_of,
                spot=spot,
                quote_rows=quotes,
                open_interest_rows=open_interest,
                first_order_rows=first_order,
                second_order_rows=second_order,
                open_interest_as_of=open_interest_as_of,
                risk_free_rate=risk_free_rate,
                dividend_yield=dividend_yield,
                spot_timestamp=spot_timestamp,
                clocks=SnapshotClocks(
                    request_started_at=started,
                    response_received_at=received,
                    normalized_at=received,
                ),
                iv_source=iv_source,
                meta={"thetadata_request": self.effective_request_parameters()},
            )
        )
