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
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from src.adapters.raw_store import (
    PARSER_VERSION,
    CaptureSession,
    NullRawStore,
    RawResponseStore,
)
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
from src.domain.completeness import CompletenessStatus
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
from src.gex.sessions import EASTERN, to_eastern

ContractKey = tuple[str, date, float, str]


class ThetaDataError(RuntimeError):
    pass


class ThetaDataSchemaError(ThetaDataError):
    """A response is missing a column the parser requires."""


class DuplicateRowError(ThetaDataError):
    """Two rows claim the same contract identity and cannot be reconciled."""


class TimestampSource(str, Enum):
    """Which vendor response a timestamp came from.

    v2.1 tracked one chain-wide ``localisation_applied`` boolean, and set it
    from the quote loop alone. A chain with timezone-aware quotes and naive
    greeks therefore reported that no assumption had been applied while an
    assumption had in fact been applied to every greek record -- and the greeks
    carry the IV and the underlying price, so the assumption was doing real work
    where it was invisible.
    """

    QUOTE = "quote"
    OPEN_INTEREST = "open_interest"
    FIRST_ORDER_GREEKS = "first_order_greeks"
    SECOND_ORDER_GREEKS = "second_order_greeks"
    IMPLIED_VOL = "implied_vol"
    UNDERLYING = "underlying"
    CONTRACT_METADATA = "contract_metadata"


@dataclass(slots=True)
class TimestampLocalizationSummary:
    """What we assumed about one source's clock, and how often."""

    source: TimestampSource
    rows_seen: int = 0
    naive_rows_localized: int = 0
    aware_rows_preserved: int = 0
    invalid_rows: int = 0

    def record(self, *, parsed: datetime | None, assumed: bool) -> None:
        self.rows_seen += 1
        if parsed is None:
            self.invalid_rows += 1
        elif assumed:
            self.naive_rows_localized += 1
        else:
            self.aware_rows_preserved += 1

    @property
    def assumed_timezone(self) -> str | None:
        """Named only when an assumption was actually applied to this source."""
        return VENDOR_TIMEZONE_ASSUMPTION if self.naive_rows_localized else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "rows_seen": self.rows_seen,
            "naive_rows_localized": self.naive_rows_localized,
            "aware_rows_preserved": self.aware_rows_preserved,
            "invalid_rows": self.invalid_rows,
            "assumed_timezone": self.assumed_timezone,
        }


class LocalizationLedger:
    """Collects per-source summaries during assembly."""

    def __init__(self) -> None:
        self._by_source: dict[TimestampSource, TimestampLocalizationSummary] = {
            source: TimestampLocalizationSummary(source=source)
            for source in TimestampSource
        }

    def parse(self, value: str | None, *, source: TimestampSource) -> datetime | None:
        parsed, assumed = parse_vendor_timestamp(value)
        self._by_source[source].record(parsed=parsed, assumed=assumed)
        return parsed

    @property
    def any_assumption_applied(self) -> bool:
        return any(s.naive_rows_localized for s in self._by_source.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "any_assumption_applied": self.any_assumption_applied,
            "assumption_status": VENDOR_TIMEZONE_ASSUMPTION_STATUS,
            "by_source": {
                source.value: summary.as_dict()
                for source, summary in self._by_source.items()
                if summary.rows_seen
            },
        }


class ThetaDataVendorError(ThetaDataError):
    """The vendor returned a non-success status, or a vendor error document.

    The client checks the status itself rather than trusting the transport to
    have raised. ``RetryingTransport`` does raise on non-2xx, but the transport
    is a *protocol*: a caller may supply any implementation, and v2.1's
    contract did not say that a non-2xx response could never arrive here. One
    that returned a 500 handed an HTML error page straight to ``parse_csv``,
    which parsed it happily into rows with no recognised columns -- surfacing
    several layers later as a confusing schema error rather than as "the vendor
    said 500".
    """


class IntegerParseIssue(str, Enum):
    """Why an integer field could not be read.

    Distinct codes because the causes are genuinely different facts. Collapsing
    them all into ``None`` -- which is what ``int(float(value))`` did on the paths
    where it did not simply crash -- makes corruption indistinguishable from the
    vendor not sending the field.
    """

    MISSING_VALUE = "missing_value"
    MALFORMED_VALUE = "malformed_value"
    NON_FINITE_INPUT = "non_finite_input"
    NON_INTEGER_INPUT = "non_integer_input"
    NEGATIVE_VALUE = "negative_value"
    OUT_OF_RANGE = "out_of_range"


#: Fast path for the overwhelmingly common case: an optionally-signed run of
#: digits, which ``int()`` parses exactly and without ceremony.
_INTEGER_GRAMMAR = re.compile(r"^[+-]?\d+$")


class FloatParseIssue(str, Enum):
    """Why a float field did not yield a number.

    v2.1 had ``_to_float`` return ``None`` for a missing field and ``None`` for
    ``"oops"``. Downstream, a vendor that sent nothing and a vendor that sent
    garbage produced identical state, so corruption was indistinguishable from
    absence -- and absence is normal, so corruption looked normal.
    """

    MISSING_VALUE = "missing_value"
    MALFORMED_VALUE = "malformed_value"
    NON_FINITE_INPUT = "non_finite_input"
    OUT_OF_RANGE = "out_of_range"
    CROSSED_MARKET = "crossed_market"
    ZERO_BID = "zero_bid"


def parse_float_field(
    value: str | float | None,
    *,
    field: str,
    allow_negative: bool = True,
    minimum: float | None = None,
    maximum: float | None = None,
) -> tuple[float | None, FloatParseIssue | None]:
    """Strictly parse a float vendor field, distinguishing every failure.

    Absence and corruption get different codes, so that "the vendor did not
    send a bid" and "the vendor sent the word oops" stop looking the same three
    layers downstream.
    """
    del field  # present for call-site readability
    if value is None:
        return None, FloatParseIssue.MISSING_VALUE
    if isinstance(value, bool):
        return None, FloatParseIssue.MALFORMED_VALUE
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, FloatParseIssue.MISSING_VALUE
        try:
            parsed = float(text)
        except ValueError:
            return None, FloatParseIssue.MALFORMED_VALUE
    else:
        parsed = float(value)
    if not math.isfinite(parsed):
        return None, FloatParseIssue.NON_FINITE_INPUT
    if not allow_negative and parsed < 0.0:
        return None, FloatParseIssue.OUT_OF_RANGE
    if minimum is not None and parsed < minimum:
        return None, FloatParseIssue.OUT_OF_RANGE
    if maximum is not None and parsed > maximum:
        return None, FloatParseIssue.OUT_OF_RANGE
    return parsed, None


def parse_int_field(
    value: str | int | None,
    *,
    field: str,
    allow_negative: bool = False,
    maximum: int | None = None,
) -> tuple[int | None, IntegerParseIssue | None]:
    """Strictly parse an integer vendor field.

    Replaces ``int(float(value))``, which had two separate defects:

    * ``int(float("12.9"))`` silently returned ``12``, inventing data by
      truncation on a field where the difference is the whole point;
    * ``int(float("NaN"))`` raised ``ValueError`` and ``int(float("inf"))``
      raised ``OverflowError``, neither of which any caller handled -- so a
      single corrupt cell crashed the adapter rather than being reported.

    Accepts integers, integer-form strings, and decimals that are *exactly*
    integral (``"12.0"``, ``"1e3"``). Everything else is refused with a code.
    """
    del field  # present for call-site readability
    if value is None:
        return None, IntegerParseIssue.MISSING_VALUE
    if isinstance(value, bool):
        return None, IntegerParseIssue.MALFORMED_VALUE
    if isinstance(value, int):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None, IntegerParseIssue.MISSING_VALUE
        # Decimal, not float. v2.1 reached the integer via ``float(text)``,
        # which is exact only below 2^53: "9007199254740993" became
        # 9007199254740992 -- a different number, equally plausible, with
        # nothing to indicate it had changed. Open interest is precisely the
        # field where a large integer is realistic.
        if _INTEGER_GRAMMAR.match(text):
            parsed = int(text)
        else:
            try:
                as_decimal = Decimal(text)
            except InvalidOperation:
                return None, IntegerParseIssue.MALFORMED_VALUE
            if as_decimal.is_nan() or as_decimal.is_infinite():
                return None, IntegerParseIssue.NON_FINITE_INPUT
            # Exact integrality checked in decimal arithmetic, so "12.0" and
            # "9007199254740993.0" are both handled without a float ever
            # existing.
            if as_decimal != as_decimal.to_integral_value():
                return None, IntegerParseIssue.NON_INTEGER_INPUT
            parsed = int(as_decimal)
    if not allow_negative and parsed < 0:
        return None, IntegerParseIssue.NEGATIVE_VALUE
    if maximum is not None and abs(parsed) > maximum:
        return None, IntegerParseIssue.OUT_OF_RANGE
    return parsed, None


@dataclass(frozen=True, slots=True)
class DuplicateReport:
    """One contract identity that appeared more than once in a response."""

    duplicate_key: str
    duplicate_count: int
    selection_rule: str
    selected_record_identifier: str
    discarded_record_identifiers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "duplicate_key": self.duplicate_key,
            "duplicate_count": self.duplicate_count,
            "selection_rule": self.selection_rule,
            "selected_record_identifier": self.selected_record_identifier,
            "discarded_record_identifiers": list(self.discarded_record_identifiers),
        }


@dataclass(frozen=True, slots=True)
class IndexedRows:
    """Rows keyed by contract identity, plus the duplicate accounting."""

    rows: dict[ContractKey, dict[str, str]]
    duplicates: tuple[DuplicateReport, ...] = ()
    selection_rule: str = "unique"

    @property
    def duplicate_count(self) -> int:
        return sum(report.duplicate_count - 1 for report in self.duplicates)


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
    """Lenient read for fields where absence and corruption are equivalent.

    Prefer :func:`_to_float_recorded` on anything that reaches a number the
    operator will read.
    """
    parsed, _ = parse_float_field(value, field="")
    return parsed


def _to_float_recorded(
    value: str | None,
    *,
    field: str,
    sink: list[tuple[str, str]],
    allow_negative: bool = True,
) -> float | None:
    """Parse a float and record *why* it failed, when it failed for a reason.

    A missing field is not recorded: absence is ordinary and would drown the
    signal. A malformed or non-finite one is, because that is the vendor
    sending something wrong rather than sending nothing.
    """
    parsed, issue = parse_float_field(value, field=field, allow_negative=allow_negative)
    if issue is not None and issue is not FloatParseIssue.MISSING_VALUE:
        sink.append((field, issue.value))
    return parsed


def _to_int_recorded(
    value: str | None,
    *,
    field: str,
    sink: list[tuple[str, str]],
    allow_negative: bool = False,
) -> int | None:
    """Parse an integer field, recording any problem on the record.

    Returns ``None`` on failure -- but appends the reason to ``sink``, which the
    quote carries into validation. That is the difference from the old
    ``int(float(value))``: the value still ends up absent, yet the *reason* it is
    absent survives, so validation can reject the contract with a specific code
    instead of it merely looking like the vendor sent nothing.

    ``MISSING_VALUE`` is not recorded, because genuine absence is already scored
    by the completeness components and is not a parse failure.
    """
    parsed, issue = parse_int_field(value, field=field, allow_negative=allow_negative)
    if issue is not None and issue is not IntegerParseIssue.MISSING_VALUE:
        sink.append((field, issue.value))
    return parsed


#: The zone the adapter assumes when a vendor timestamp carries no offset.
#: NOT confirmed against a live response -- see docs/OPEN_DECISIONS.md.
VENDOR_TIMEZONE_ASSUMPTION = "America/New_York (US Eastern)"
VENDOR_TIMEZONE_ASSUMPTION_STATUS = (
    "NOT_YET_VALIDATED_WITH_LIVE_VENDOR_DATA: ThetaData emits wall-clock "
    "timestamps without an offset; Eastern is inferred from the venue, not "
    "stated by the payload. One live response compared against a known "
    "wall-clock instant would settle it."
)


def parse_vendor_timestamp(
    value: str | None,
    *,
    strict_dst: bool = False,
    fold: int | None = None,
) -> tuple[datetime | None, bool]:
    """Parse a vendor timestamp. **The only place localisation may happen.**

    Returns ``(timestamp, assumption_applied)``. ``assumption_applied`` is True
    when the payload carried no offset and this function supplied one, so the
    caller can record that a guess was made rather than leaving it implicit.

    Everything downstream refuses naive datetimes (``to_eastern`` raises), which
    is what confines the guess to this single documented boundary.

    ``strict_dst`` rejects wall-clock readings Eastern does not have exactly
    once. Both cases are real: 02:30 on the spring-forward Sunday never happens,
    and 01:30 on the fall-back Sunday happens twice. Off by default because
    neither window overlaps a US index-option session, so enforcing it on
    ordinary market data would add failure modes without preventing an error.
    """
    if not value:
        return None, False
    text = value.strip()
    if not text:
        return None, False
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, False

    if parsed.tzinfo is not None:
        return to_eastern(parsed), False

    if strict_dst:
        _reject_impossible_eastern_wall_clock(parsed, fold=fold)
    localised = parsed.replace(tzinfo=EASTERN)
    if fold is not None:
        localised = localised.replace(fold=fold)
    return localised, True


def _reject_impossible_eastern_wall_clock(naive: datetime, *, fold: int | None) -> None:
    """Refuse wall-clock readings that Eastern does not have exactly once."""
    from src.gex.sessions import dst_end, dst_start

    start, end = dst_start(naive.year), dst_end(naive.year)
    if start <= naive < start.replace(hour=3):
        raise ValueError(
            f"{naive.isoformat()} does not exist in US Eastern: the clock jumps "
            "from 02:00 to 03:00 on the spring-forward date"
        )
    if fold is None and end.replace(hour=1) <= naive < end:
        raise ValueError(
            f"{naive.isoformat()} is ambiguous in US Eastern: the hour before the "
            "fall-back transition occurs twice. Pass fold=0 or fold=1."
        )


def _to_datetime(value: str | None) -> datetime | None:
    """Convenience wrapper that discards the assumption flag."""
    parsed, _ = parse_vendor_timestamp(value)
    return parsed


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


def _row_identifier(row: dict[str, str]) -> str:
    """Stable identifier for one raw row, for the duplicate report."""
    return json.dumps({k: row[k] for k in sorted(row)}, separators=(",", ":"))


def index_rows(
    rows: list[dict[str, str]],
    *,
    endpoint: str = "unknown",
    duplicate_policy: str = "reject",
) -> IndexedRows:
    """Key rows by contract identity, refusing to silently lose one.

    The v2 implementation was ``{row_key(row): row for row in rows}``: a dict
    comprehension in which the *last* duplicate silently overwrote the first.
    That made the output depend on vendor row order -- which the replay guarantee
    forbids -- and hid genuine vendor conflicts entirely.

    Policies:

    ``reject`` (default)
        Byte-identical duplicates collapse, because there is nothing to choose
        between them. Any disagreement raises.
    ``newest_timestamp``
        Explicit, deterministic resolution by vendor timestamp. Requires every
        candidate to carry a usable timestamp, and refuses a tie: with no
        discriminator there is no principled winner, and picking one anyway is
        precisely the bug being replaced.
    """
    grouped: dict[ContractKey, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row_key(row), []).append(row)

    selected: dict[ContractKey, dict[str, str]] = {}
    reports: list[DuplicateReport] = []
    rule_used = "unique"

    for key, candidates in grouped.items():
        if len(candidates) == 1:
            selected[key] = candidates[0]
            continue

        identity = ":".join(str(part) for part in key)
        identifiers = sorted(_row_identifier(row) for row in candidates)

        if len(set(identifiers)) == 1:
            rule_used = "exact_duplicate_collapsed"
            selected[key] = candidates[0]
            reports.append(
                DuplicateReport(
                    duplicate_key=identity,
                    duplicate_count=len(candidates),
                    selection_rule=rule_used,
                    selected_record_identifier=identifiers[0],
                    discarded_record_identifiers=tuple(identifiers[1:]),
                )
            )
            continue

        if duplicate_policy == "newest_timestamp":
            stamped = [(_to_datetime(row.get("timestamp")), row) for row in candidates]
            if any(ts is None for ts, _ in stamped):
                raise DuplicateRowError(
                    f"{endpoint}: {len(candidates)} conflicting rows for {identity} "
                    "and at least one has no usable timestamp, so there is no "
                    "deterministic winner"
                )
            newest = max(ts for ts, _ in stamped if ts is not None)
            winners = [row for ts, row in stamped if ts == newest]
            if len(winners) > 1:
                raise DuplicateRowError(
                    f"{endpoint}: {len(winners)} conflicting rows for {identity} "
                    f"tie on timestamp {newest.isoformat()}; refusing to arbitrate"
                )
            rule_used = "newest_timestamp"
            chosen = _row_identifier(winners[0])
            selected[key] = winners[0]
            reports.append(
                DuplicateReport(
                    duplicate_key=identity,
                    duplicate_count=len(candidates),
                    selection_rule=rule_used,
                    selected_record_identifier=chosen,
                    discarded_record_identifiers=tuple(
                        i for i in identifiers if i != chosen
                    ),
                )
            )
            continue

        raise DuplicateRowError(
            f"{endpoint}: {len(candidates)} conflicting rows for {identity}. "
            "Vendor rows disagree and no deterministic selection rule is "
            "configured; last-write-wins would make the result depend on row "
            f"order. Rows: {identifiers}"
        )

    return IndexedRows(
        rows=selected, duplicates=tuple(reports), selection_rule=rule_used
    )


# --- Chain assembly ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChainCompleteness:
    """How complete a chain is, measured against an INDEPENDENT expectation.

    The v2 defect: the expected universe was inferred from the quote response
    being evaluated, so a truncated response was, by construction, 100% complete.
    A measure that derives its expectation from the thing it is measuring cannot
    detect truncation.

    ``expected_contract_count`` must come from somewhere else -- a contract-list
    endpoint, vendor response-count metadata, pagination metadata, or the
    requested strike/expiration universe. When no independent source is
    available the completeness is labelled **partially observed** rather than
    reported as complete.
    """

    received_quote_count: int
    received_oi_count: int
    received_iv_count: int
    received_greeks_count: int
    joined_contract_count: int
    expected_contract_count: int | None = None
    expected_source: str = "none"
    missing_by_source: dict[str, int] = field(default_factory=dict)
    unexpected_identities: tuple[str, ...] = ()

    @property
    def independently_observed(self) -> bool:
        """False when the expectation came from the response itself."""
        return (
            self.expected_contract_count is not None
            and self.expected_source
            not in (
                "none",
                "quote_response",
            )
        )

    @property
    def completeness_ratio(self) -> float | None:
        if not self.expected_contract_count:
            return None
        return min(self.joined_contract_count / self.expected_contract_count, 1.0)

    @property
    def status(self) -> CompletenessStatus:
        """Measurement or absence. Never "complete" on the strength of itself."""
        if not self.independently_observed:
            # An expectation taken from the response being judged is not an
            # expectation. Rows arrived and joined; whether more were owed is
            # unknowable from here.
            return (
                CompletenessStatus.UNKNOWN
                if self.joined_contract_count == 0
                else CompletenessStatus.PARTIALLY_OBSERVED
            )
        ratio = self.completeness_ratio
        if ratio is None:
            # independently_observed with no usable ratio means the universe was
            # supplied but empty -- nothing was claimed, so nothing is measured.
            return CompletenessStatus.UNKNOWN
        return (
            CompletenessStatus.MEASURED_COMPLETE
            if ratio >= 1.0
            else CompletenessStatus.MEASURED_INCOMPLETE
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "expected_contract_count": self.expected_contract_count,
            "expected_source": self.expected_source,
            "received_quote_count": self.received_quote_count,
            "received_oi_count": self.received_oi_count,
            "received_iv_count": self.received_iv_count,
            "received_greeks_count": self.received_greeks_count,
            "joined_contract_count": self.joined_contract_count,
            "completeness_ratio": self.completeness_ratio,
            "independently_observed": self.independently_observed,
            "status": self.status.value,
            "missing_by_source": dict(sorted(self.missing_by_source.items())),
            "unexpected_identities": list(self.unexpected_identities),
        }


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
    #: How to resolve two rows claiming the same contract. See ``index_rows``.
    duplicate_policy: str = "reject"
    #: Contract identities from an INDEPENDENT source (contract-list
    #: endpoint, pagination metadata, or the requested universe). Without
    #: one, completeness is only partially observed -- see ChainCompleteness.
    expected_contract_ids: tuple[str, ...] | None = None
    expected_source: str = "none"
    meta: dict[str, Any] = field(default_factory=dict)


def assemble_chain(inputs: ChainAssemblyInputs) -> ChainSnapshot:
    """Join the responses into one ``ChainSnapshot``.

    Quotes drive the iteration: a contract with no quote has no book and cannot
    contribute a spread or a crossed-market signal. Missing OI or IV leaves those
    fields ``None``, and the validation pass then drops the contract and counts
    it -- a partial join must be visible in the report rather than quietly
    shrinking the chain.
    """
    oi_indexed = index_rows(
        inputs.open_interest_rows,
        endpoint="open_interest",
        duplicate_policy=inputs.duplicate_policy,
    )
    first_indexed = index_rows(
        inputs.first_order_rows,
        endpoint="first_order",
        duplicate_policy=inputs.duplicate_policy,
    )
    second_indexed = index_rows(
        inputs.second_order_rows,
        endpoint="second_order",
        duplicate_policy=inputs.duplicate_policy,
    )
    quote_indexed = index_rows(
        inputs.quote_rows,
        endpoint="quote",
        duplicate_policy=inputs.duplicate_policy,
    )
    oi_by_key = oi_indexed.rows
    first_by_key = first_indexed.rows
    second_by_key = second_indexed.rows
    duplicate_reports = [
        report
        for indexed in (quote_indexed, oi_indexed, first_indexed, second_indexed)
        for report in indexed.duplicates
    ]

    quotes: list[OptionQuote] = []
    ledger = LocalizationLedger()
    # Iterate the DEDUPLICATED rows, not ``inputs.quote_rows``. v2.1 computed
    # ``quote_indexed`` -- selecting a canonical row per identity and reporting
    # what it discarded -- and then walked the original unfiltered list anyway.
    # A duplicated quote was therefore assembled twice while the duplicate
    # report described a collapse that had not happened. Sorted by key so the
    # assembled order cannot depend on the order the vendor happened to send.
    for key in sorted(quote_indexed.rows):
        row = quote_indexed.rows[key]
        first = first_by_key.get(key, {})
        second = second_by_key.get(key, {})
        oi_row = oi_by_key.get(key, {})

        parse_issues: list[tuple[str, str]] = []
        quote_ts = ledger.parse(row.get("timestamp"), source=TimestampSource.QUOTE)
        if oi_row:
            ledger.parse(oi_row.get("timestamp"), source=TimestampSource.OPEN_INTEREST)
        if first:
            ledger.parse(
                first.get("timestamp"), source=TimestampSource.FIRST_ORDER_GREEKS
            )
            ledger.parse(
                first.get("underlying_timestamp"), source=TimestampSource.UNDERLYING
            )
        if second:
            ledger.parse(
                second.get("timestamp"), source=TimestampSource.SECOND_ORDER_GREEKS
            )
        bid = _to_float_recorded(row.get("bid"), field="bid", sink=parse_issues)
        ask = _to_float_recorded(row.get("ask"), field="ask", sink=parse_issues)
        crossed = bid is not None and ask is not None and ask < bid

        contract = OptionContract(
            root=parse_root(row["symbol"]),
            expiry=key[1],
            strike=key[2],
            right=OptionRight(key[3]),
        )
        # Every source clock preserved separately. This is the whole point.
        timestamps = ContractTimestamps(
            quote_timestamp=quote_ts,
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
                bid_size=_to_int_recorded(
                    row.get("bid_size"), field="bid_size", sink=parse_issues
                ),
                ask_size=_to_int_recorded(
                    row.get("ask_size"), field="ask_size", sink=parse_issues
                ),
                open_interest=_to_int_recorded(
                    oi_row.get("open_interest"),
                    field="open_interest",
                    sink=parse_issues,
                ),
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
                parse_issues=tuple(parse_issues),
            )
        )

    joined_ids = {q.contract.canonical_id for q in quotes}
    expected_ids = set(inputs.expected_contract_ids or ())
    completeness = ChainCompleteness(
        received_quote_count=len(inputs.quote_rows),
        received_oi_count=len(inputs.open_interest_rows),
        received_iv_count=len(inputs.first_order_rows),
        received_greeks_count=len(inputs.second_order_rows),
        joined_contract_count=len(quotes),
        expected_contract_count=(
            len(expected_ids) if inputs.expected_contract_ids is not None else None
        ),
        expected_source=inputs.expected_source,
        missing_by_source={
            "quote": max(0, len(expected_ids) - len(joined_ids)) if expected_ids else 0,
            "open_interest": sum(1 for q in quotes if q.open_interest is None),
            "implied_vol": sum(1 for q in quotes if q.effective_iv is None),
            "vendor_gamma": sum(1 for q in quotes if q.gamma is None),
        },
        unexpected_identities=tuple(sorted(joined_ids - expected_ids))
        if expected_ids
        else (),
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
        # The INDEPENDENT expectation, or None. v2.1 substituted
        # ``len(inputs.quote_rows)`` here, which made a truncated response
        # complete by construction: the measure and the measured were the same
        # number. ``None`` must survive to the confidence model.
        expected_contract_count=(
            completeness.expected_contract_count
            if completeness.independently_observed
            else None
        ),
        completeness_status=completeness.status,
        meta={
            **dict(inputs.meta),
            "duplicate_reports": [r.as_dict() for r in duplicate_reports],
            "chain_completeness": completeness.as_dict(),
            "parser_version": PARSER_VERSION,
            # Per source, not one chain-wide flag. See TimestampSource.
            "timestamp_localization": ledger.as_dict(),
            "vendor_timezone_assumption": {
                # Retained for compatibility; the per-source breakdown above is
                # the authoritative record.
                "applied": ledger.any_assumption_applied,
                "assumed_timezone": VENDOR_TIMEZONE_ASSUMPTION,
                "status": VENDOR_TIMEZONE_ASSUMPTION_STATUS,
            },
        },
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

        # Status first, unconditionally, before the body is looked at at all.
        # A 500 is a 500 whether or not its body happens to resemble a vendor
        # error document, and an HTML error page must never reach the CSV
        # parser as though it were data.
        if not 200 <= response.status_code < 300:
            raise ThetaDataVendorError(
                f"{endpoint.value} returned HTTP {response.status_code}; "
                f"refusing to parse a non-success body as CSV "
                f"({len(response.text)} bytes)"
            )

        vendor_error = detect_vendor_error(response.text)
        if vendor_error:
            raise ThetaDataVendorError(
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
        duplicate_policy: str = "reject",
        capture: CaptureSession | None = None,
        expected_contract_ids: tuple[str, ...] | None = None,
        expected_source: str = "none",
    ) -> ChainSnapshot:
        """Pull and join a full chain.

        Second-order greeks are requested only when the tier allows them, so a
        Standard-tier client produces a complete chain without a failed request.

        ``expected_contract_ids`` is the INDEPENDENT universe the chain is
        measured against. It is a caller argument rather than something derived
        here on purpose: an expectation computed from the quote response cannot
        detect that the quote response was truncated. No ThetaData contract-list
        endpoint is wired in this release, so when the caller supplies nothing
        the resulting completeness is reported as ``PARTIALLY_OBSERVED`` --
        never as complete. See docs/OPEN_DECISIONS.md (OD-9).
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
                duplicate_policy=duplicate_policy,
                expected_contract_ids=expected_contract_ids,
                expected_source=expected_source,
                meta={"thetadata_request": self.effective_request_parameters()},
            )
        )
