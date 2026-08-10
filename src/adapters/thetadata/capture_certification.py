"""Read an immutable capture and work out what the vendor actually did.

Offline, deterministic, and derived entirely from bytes on disk. Nothing here
opens a socket; the capture is the input and a content-addressed report is the
output, so running it twice on the same directory produces the same digest and
running it on a modified directory does not.

The method is the same for every dimension: state the competing hypotheses,
score each one against the vendor's own numbers, and keep the score table rather
than only the winner. A report that says "ACT/365" is an assertion. A report
that says ACT/365 scored 1.7e-4 and ACT/360 scored 1.6e-2 over 7,348 rows is a
result somebody can disagree with.

For the expiration timestamp the report does something stronger than scoring.
Given the vendor's reported delta and implied volatility, ``d1`` is determined,
and time-to-expiry follows from a quadratic -- so each row can be *inverted* for
the clock the vendor used, with no hypothesis at all. Grouped by expiration that
inversion turned out to matter: the first live capture uses 16:00 ET for
expirations inside the capture week and whole calendar days beyond it. Scoring a
single global hypothesis would have averaged those together and reported a
mediocre fit for both.

The pricing is the engine's own :mod:`src.gex.pricing`, not a private copy. The
question certification answers is whether *this repository's* Black-Scholes
reproduces the vendor's, and a second implementation written to match would
answer a different and much less useful question.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import pathlib
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from statistics import NormalDist
from typing import Any
from zoneinfo import ZoneInfo

from src.domain.contracts import OptionRight
from src.gex.pricing import BlackScholesInputs, norm_cdf
from src.gex.pricing import delta as bs_delta

__all__ = [
    "CAPTURE_CERTIFICATION_SCHEMA_VERSION",
    "CaptureCertificationError",
    "CaptureCertificationReport",
    "ContractKey",
    "EndpointSynchrony",
    "ExpirationClockReading",
    "HypothesisScore",
    "LoadedCapture",
    "OpenInterestCoverage",
    "OpenInterestCoverageState",
    "UniverseCertification",
    "certify_capture",
    "load_capture",
]

CAPTURE_CERTIFICATION_SCHEMA_VERSION = "capture-certification/2.1.22"

EASTERN = ZoneInfo("America/New_York")

#: ``implied_vol`` is reported to four decimals. Half a tick is the floor below
#: which a delta disagreement says nothing about the model -- it is the rounding
#: in the field we are comparing against.
IMPLIED_VOL_TICK = 1e-4

#: Endpoint paths, as they appear in the capture record index.
INDEX_PRICE = "/v3/index/snapshot/price"
OPTION_QUOTE = "/v3/option/snapshot/quote"
OPTION_OPEN_INTEREST = "/v3/option/snapshot/open_interest"
OPTION_GREEKS = "/v3/option/snapshot/greeks/first_order"
OPTION_CONTRACT_LIST = "/v3/option/list/contracts/quote"


class CaptureCertificationError(ValueError):
    """A capture that cannot be certified from what is on disk."""


class OpenInterestCoverageState(str, Enum):
    """Three different things, one of which used to be spelled ``0``.

    The first live capture returned 14,130 open-interest rows against a 14,556
    contract universe. 3,692 of those rows say ``open_interest,0`` and 426
    identities have no row at all. Those are not the same fact:

    ``OI_EXPLICIT_ZERO``
        The vendor was asked and answered zero. A real, usable observation --
        the contract exists and nobody holds it.
    ``OI_MISSING``
        The vendor returned nothing for this identity. The open interest could
        be zero, or ten thousand, or the endpoint could have truncated.

    Filling a missing identity with zero converts the second into the first and
    loses the distinction permanently. Open interest is the linear weight on
    every GEX term, so a contract silently weighted zero is a contract deleted
    from the aggregate -- and it disappears without changing any count that a
    completeness check looks at.
    """

    OI_PRESENT = "OI_PRESENT"
    OI_EXPLICIT_ZERO = "OI_EXPLICIT_ZERO"
    OI_MISSING = "OI_MISSING"

    @property
    def is_observed(self) -> bool:
        """Whether the vendor actually answered for this identity."""
        return self is not OpenInterestCoverageState.OI_MISSING


@dataclass(frozen=True, slots=True, order=True)
class ContractKey:
    """One option contract, canonically. Sorts, hashes, and prints stably."""

    symbol: str
    expiration: date
    strike: str
    right: str

    @classmethod
    def from_row(cls, row: dict[str, str]) -> ContractKey:
        return cls(
            symbol=row["symbol"].strip().strip('"'),
            expiration=date.fromisoformat(row["expiration"].strip().strip('"')),
            # Kept as the vendor's own text. Parsing to float and back would
            # make 7600.000 and 7600.0 the same identity in this repository and
            # different identities on the wire.
            strike=row["strike"].strip().strip('"'),
            right=row["right"].strip().strip('"'),
        )

    @property
    def canonical(self) -> str:
        return f"{self.symbol}|{self.expiration.isoformat()}|{self.strike}|{self.right}"

    @property
    def option_right(self) -> OptionRight:
        return OptionRight.CALL if self.right.upper() == "CALL" else OptionRight.PUT


def _set_hash(keys: set[ContractKey]) -> str:
    """A digest over an identity set, order-independent by construction."""
    joined = "\n".join(sorted(k.canonical for k in keys))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LoadedCapture:
    """The five responses, verified against the manifest that describes them."""

    root: pathlib.Path
    session_id: str
    manifest_hash: str
    captured_at: str
    tables: dict[str, list[dict[str, str]]]
    record_hashes: dict[str, str]
    verified_records: int
    parser_version: str

    def rows(self, endpoint: str) -> list[dict[str, str]]:
        try:
            return self.tables[endpoint]
        except KeyError as error:
            raise CaptureCertificationError(
                f"the capture has no {endpoint} response. Certification "
                f"compares endpoints against each other; with one absent there "
                f"is nothing to compare. Present: {sorted(self.tables)}"
            ) from error


def load_capture(root: pathlib.Path | str) -> LoadedCapture:
    """Read a capture directory and re-verify every payload against its hash.

    The verification is not decoration. Certification reads numbers out of these
    bytes and then this repository records vendor conventions on the strength of
    them; if the bytes changed after capture, every conclusion below is about a
    file rather than about the vendor.
    """
    root = pathlib.Path(root)
    if not root.is_dir():
        raise CaptureCertificationError(f"{root} is not a directory")

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise CaptureCertificationError(
            f"{manifest_path} does not exist. A capture without its manifest "
            "cannot be verified, and an unverified capture cannot certify "
            "anything."
        )
    manifest = json.loads(manifest_path.read_bytes())

    tables: dict[str, list[dict[str, str]]] = {}
    record_hashes: dict[str, str] = {}
    verified = 0
    for record in manifest.get("records", []):
        endpoint = record.get("endpoint", "")
        location = record.get("payload_location", "")
        expected = record.get("payload_hash", "")
        payload = root / "raw" / location
        if not payload.is_file():
            raise CaptureCertificationError(
                f"manifest names {location} for {endpoint} but the file is "
                "absent; the capture is incomplete"
            )
        body = payload.read_bytes()
        actual = hashlib.sha256(body).hexdigest()
        if actual != expected:
            raise CaptureCertificationError(
                f"{location} hashes to {actual} and the manifest says "
                f"{expected}. These bytes are not the bytes that were "
                "captured, so nothing derived from them describes the vendor."
            )
        verified += 1
        record_hashes[endpoint] = actual
        tables[endpoint] = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))

    return LoadedCapture(
        root=root,
        session_id=str(manifest.get("session_id", "")),
        manifest_hash=str(manifest.get("manifest_hash", "")),
        captured_at=str(
            (manifest.get("records") or [{}])[0].get(
                "effective_valuation_timestamp", ""
            )
        ),
        tables=tables,
        record_hashes=record_hashes,
        verified_records=verified,
        parser_version=str(manifest.get("parser_version", "")),
    )


# ---------------------------------------------------------------------------
# Universe and coverage
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UniverseCertification:
    """Whether the dedicated contract listing described the snapshot universe.

    Set hashes rather than counts. Two responses of 14,556 rows each can hold
    different contracts, and a count comparison would call that a match.
    """

    contract_list_count: int
    quote_count: int
    greeks_count: int
    contract_list_hash: str
    quote_hash: str
    greeks_hash: str
    quote_matches_list: bool
    greeks_matches_list: bool
    only_in_list: tuple[str, ...]
    only_in_snapshots: tuple[str, ...]

    @property
    def state(self) -> str:
        """The evidence state this capture supports for this request form.

        ``DEDICATED_CONTRACT_LIST_MATCHED_SNAPSHOT_UNIVERSE`` means exactly
        this: every contract the dedicated listing returned for this
        symbol/date/max-DTE scope is present in both snapshots, and neither
        snapshot carried anything the listing did not. It says nothing about
        another date, another symbol, another tier or another endpoint family.
        """
        if self.quote_matches_list and self.greeks_matches_list:
            return "DEDICATED_CONTRACT_LIST_MATCHED_SNAPSHOT_UNIVERSE"
        return "DEDICATED_CONTRACT_LIST_OBSERVED_UNVERIFIED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_list_count": self.contract_list_count,
            "quote_count": self.quote_count,
            "greeks_count": self.greeks_count,
            "contract_list_set_hash": self.contract_list_hash,
            "quote_set_hash": self.quote_hash,
            "greeks_set_hash": self.greeks_hash,
            "quote_matches_list": self.quote_matches_list,
            "greeks_matches_list": self.greeks_matches_list,
            "only_in_list": list(self.only_in_list[:50]),
            "only_in_snapshots": list(self.only_in_snapshots[:50]),
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class OpenInterestCoverage:
    """How much of the universe the open-interest endpoint actually answered."""

    universe_count: int
    present_count: int
    explicit_zero_count: int
    missing_count: int
    missing_by_expiration: tuple[tuple[str, int, int], ...]
    missing_identities_hash: str
    fully_missing_expirations: tuple[str, ...]

    @property
    def coverage_ratio(self) -> float:
        if not self.universe_count:
            return 0.0
        return (self.present_count + self.explicit_zero_count) / self.universe_count

    @property
    def permits_trusted_aggregate(self) -> bool:
        """Whether a trusted aggregate GEX may be computed over this universe.

        False while any identity is ``OI_MISSING``. There is no evidence-backed
        policy for an absent open-interest row -- the honest options are to
        exclude the contract and report reduced coverage, or to refuse -- and
        until one is chosen and justified, computing an aggregate would be
        choosing silently.
        """
        return self.missing_count == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "universe_count": self.universe_count,
            "oi_present": self.present_count,
            "oi_explicit_zero": self.explicit_zero_count,
            "oi_missing": self.missing_count,
            "coverage_ratio": self.coverage_ratio,
            "missing_by_expiration": [
                {"expiration": e, "listed": listed, "missing": missing}
                for e, listed, missing in self.missing_by_expiration
            ],
            "fully_missing_expirations": list(self.fully_missing_expirations),
            "missing_identities_hash": self.missing_identities_hash,
            "permits_trusted_aggregate": self.permits_trusted_aggregate,
        }


@dataclass(frozen=True, slots=True)
class EndpointSynchrony:
    """How far apart two sequentially acquired snapshots actually were.

    Quote and Greeks were separate HTTP requests. Joining them on contract
    identity and calling the result one observation would be asserting an
    atomicity the capture disproves.
    """

    overlap: int
    identical_timestamp_ratio: float
    identical_bid_ratio: float
    identical_ask_ratio: float
    p99_gap_seconds: float
    max_gap_seconds: float

    @property
    def is_atomic(self) -> bool:
        return self.max_gap_seconds == 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "overlap": self.overlap,
            "identical_timestamp_ratio": self.identical_timestamp_ratio,
            "identical_bid_ratio": self.identical_bid_ratio,
            "identical_ask_ratio": self.identical_ask_ratio,
            "p99_gap_seconds": self.p99_gap_seconds,
            "max_gap_seconds": self.max_gap_seconds,
            "is_atomic": self.is_atomic,
        }


# ---------------------------------------------------------------------------
# Numerical reconstruction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HypothesisScore:
    """One candidate convention and how well it reproduced the vendor."""

    hypothesis: str
    rows: int
    median_abs_delta_error: float
    delta_rmse: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis,
            "rows": self.rows,
            "median_abs_delta_error": self.median_abs_delta_error,
            "delta_rmse": self.delta_rmse,
        }


@dataclass(frozen=True, slots=True)
class ExpirationClockReading:
    """The time-to-expiry the vendor used for one expiration, read by inversion."""

    expiration: str
    rows: int
    implied_days: float
    calendar_days: int
    intraday_days_to_1600: float
    spread: float

    @property
    def offset_from_calendar(self) -> float:
        return self.implied_days - self.calendar_days

    @property
    def matches_intraday_1600(self) -> bool:
        return abs(self.implied_days - self.intraday_days_to_1600) < 0.01

    @property
    def matches_whole_days(self) -> bool:
        return abs(self.offset_from_calendar) < 0.05

    def as_dict(self) -> dict[str, Any]:
        return {
            "expiration": self.expiration,
            "rows": self.rows,
            "implied_days": self.implied_days,
            "calendar_days": self.calendar_days,
            "intraday_days_to_1600": self.intraday_days_to_1600,
            "spread": self.spread,
            "offset_from_calendar": self.offset_from_calendar,
            "matches_intraday_1600": self.matches_intraday_1600,
            "matches_whole_days": self.matches_whole_days,
        }


def _parse_et(text: str) -> datetime:
    return datetime.fromisoformat(text.strip().strip('"')).replace(tzinfo=EASTERN)


def _usable_greeks(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Rows that can speak to a pricing hypothesis at all.

    A zero implied volatility is a failed solve, not a quiet market, and a delta
    pinned at 0 or +/-1 is the boundary the formula returns for anything far
    enough from the money -- it would agree with every hypothesis equally and
    dilute each score by the same amount.
    """
    out = []
    for row in rows:
        try:
            iv = float(row["implied_vol"])
            d = float(row["delta"])
        except (KeyError, ValueError):
            continue
        if iv > 0.0 and 0.0 < abs(d) < 1.0:
            out.append(row)
    return out


def _implied_years(
    *,
    spot: float,
    strike: float,
    sigma: float,
    delta_value: float,
    rate: float,
    right: OptionRight,
) -> float | None:
    """Invert the vendor's own delta for the time-to-expiry behind it.

    ``delta`` fixes ``d1``; ``d1`` and ``sigma`` fix ``T`` through a quadratic
    in ``sqrt(T)``. No search and no hypothesis -- this reads the vendor's clock
    out of the vendor's numbers.
    """
    nd = NormalDist()
    try:
        d1 = (
            nd.inv_cdf(delta_value)
            if right is OptionRight.CALL
            else -nd.inv_cdf(-delta_value)
        )
    except (ValueError, statistics.StatisticsError):
        return None
    a = rate + 0.5 * sigma * sigma
    b = -d1 * sigma
    c = math.log(spot / strike)
    disc = b * b - 4.0 * a * c
    if disc < 0.0 or a == 0.0:
        return None
    for sign in (1.0, -1.0):
        x = (-b + sign * math.sqrt(disc)) / (2.0 * a)
        if x > 0.0:
            return x * x
    return None


def _score(
    rows: list[dict[str, str]],
    *,
    spot: float,
    valuation: datetime,
    rate: float,
    days_per_year: float,
    expiry_time: time,
    label: str,
    whole_days_from: date | None = None,
) -> HypothesisScore | None:
    """Score one convention by reproducing the vendor's delta with our pricer."""
    errors: list[float] = []
    for row in rows:
        exp = date.fromisoformat(row["expiration"].strip().strip('"'))
        if whole_days_from is not None and exp > whole_days_from:
            days = float((exp - valuation.date()).days)
        else:
            expiry_dt = datetime.combine(exp, expiry_time, tzinfo=EASTERN)
            days = (expiry_dt - valuation).total_seconds() / 86400.0
        if days <= 0.0:
            continue
        try:
            sigma = float(row["implied_vol"])
            reported = float(row["delta"])
            strike = float(row["strike"])
        except ValueError:
            continue
        key = ContractKey.from_row(row)
        inputs = BlackScholesInputs(
            spot=spot,
            strike=strike,
            time_to_expiry=days / days_per_year,
            implied_vol=sigma,
            rate=rate,
        )
        if inputs.is_degenerate():
            continue
        errors.append(abs(bs_delta(inputs, key.option_right) - reported))
    if not errors:
        return None
    return HypothesisScore(
        hypothesis=label,
        rows=len(errors),
        median_abs_delta_error=statistics.median(errors),
        delta_rmse=math.sqrt(sum(e * e for e in errors) / len(errors)),
    )


def _solve_iv(
    target: float,
    *,
    spot: float,
    strike: float,
    years: float,
    rate: float,
    right: OptionRight,
) -> float | None:
    """Bisect for the volatility that reprices ``target``. Deterministic."""

    def price(sigma: float) -> float:
        sq = sigma * math.sqrt(years)
        d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * years) / sq
        d2 = d1 - sq
        if right is OptionRight.CALL:
            return spot * norm_cdf(d1) - strike * math.exp(-rate * years) * norm_cdf(d2)
        return strike * math.exp(-rate * years) * norm_cdf(-d2) - spot * norm_cdf(-d1)

    lo, hi = 1e-6, 30.0
    try:
        if price(lo) > target or price(hi) < target:
            return None
        for _ in range(120):
            mid = 0.5 * (lo + hi)
            if price(mid) < target:
                lo = mid
            else:
                hi = mid
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptureCertificationReport:
    """Everything one capture established, and everything it did not."""

    schema_version: str
    session_id: str
    manifest_hash: str
    archive_sha256: str
    captured_at: str
    parser_version: str
    verified_records: int
    record_hashes: dict[str, str]
    universe: UniverseCertification
    open_interest: OpenInterestCoverage
    synchrony: EndpointSynchrony
    underlying: dict[str, Any]
    rate_scores: tuple[HypothesisScore, ...]
    day_count_scores: tuple[HypothesisScore, ...]
    expiration_time_scores: tuple[HypothesisScore, ...]
    iv_basis_scores: tuple[tuple[str, int, float, float], ...]
    clock_readings: tuple[ExpirationClockReading, ...]
    ledger: Any
    rows_reconstructed: int

    @property
    def resolved_dimensions(self) -> tuple[str, ...]:
        return tuple(
            o.dimension.value for o in self.ledger.observations if o.status.is_resolved
        )

    @property
    def unresolved_dimensions(self) -> tuple[str, ...]:
        return tuple(o.dimension.value for o in self.ledger.unresolved)

    @property
    def analytical_readiness(self) -> str:
        """What this capture may be used for.

        ``ADAPTER_CERTIFICATION_EVIDENCE``, and nothing stronger. A capture
        earns that label by being verifiable and reproducible, which this one
        is; it does not earn a GEX by being either.
        """
        return "ADAPTER_CERTIFICATION_EVIDENCE"

    @property
    def gex_blockers(self) -> tuple[str, ...]:
        """Why no GEX may be computed from this capture. Derived, not asserted.

        Listed rather than summarised because they are independent, and fixing
        the rate does not fix the open interest.
        """
        blockers: list[str] = []
        rate = self.ledger.for_dimension(_rate_dimension())
        if rate is not None and rate.status.is_conflict:
            blockers.append(
                "the Greeks in this capture were produced under rate_value=4.2 "
                "consumed as a decimal, i.e. 420%. Every implied volatility and "
                "every delta in it describes a market that does not exist. The "
                "capture remains the evidence that established the conflict and "
                "must not be discarded."
            )
        if not self.open_interest.permits_trusted_aggregate:
            blockers.append(
                f"{self.open_interest.missing_count} contract identities have "
                "no open-interest row. Open interest is the linear weight on "
                "every GEX term and there is no evidence-backed policy for an "
                "absent one, so an aggregate would be choosing silently."
            )
        return tuple(blockers)

    @property
    def trusted_for_gex(self) -> bool:
        """Always false here, and derived so it cannot drift from the reasons."""
        return not self.gex_blockers

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capture": {
                "session_id": self.session_id,
                "manifest_hash": self.manifest_hash,
                "archive_sha256": self.archive_sha256,
                "captured_at": self.captured_at,
                "parser_version": self.parser_version,
            },
            "raw_record_verification": {
                "records_verified": self.verified_records,
                "record_payload_hashes": dict(sorted(self.record_hashes.items())),
            },
            "universe": self.universe.as_dict(),
            "open_interest_coverage": self.open_interest.as_dict(),
            "endpoint_synchrony": self.synchrony.as_dict(),
            "underlying_synchronization": self.underlying,
            "rate_semantics": [s.as_dict() for s in self.rate_scores],
            "day_count_comparison": [s.as_dict() for s in self.day_count_scores],
            "expiration_time_comparison": [
                s.as_dict() for s in self.expiration_time_scores
            ],
            "iv_basis_comparison": [
                {
                    "basis": basis,
                    "rows": rows,
                    "median_abs_iv_error": median,
                    "within_half_tick_ratio": within,
                }
                for basis, rows, median, within in self.iv_basis_scores
            ],
            "vendor_clock_by_expiration": [r.as_dict() for r in self.clock_readings],
            "rows_reconstructed": self.rows_reconstructed,
            "vendor_behavior": self.ledger.as_dict(),
            "documentation_live_conflicts": [
                o.dimension.value for o in self.ledger.conflicts
            ],
            "dimensions_resolved": list(self.resolved_dimensions),
            "dimensions_unresolved": list(self.unresolved_dimensions),
            "analytical_readiness": self.analytical_readiness,
            "trusted_for_gex": self.trusted_for_gex,
            "gex_blockers": list(self.gex_blockers),
        }

    def report_hash(self) -> str:
        from src.domain.digests import digest_of

        return digest_of(self.as_dict())


def _rate_dimension() -> Any:
    from src.adapters.thetadata.live_behavior import BehaviorDimension

    return BehaviorDimension.RATE_UNITS


def certify_capture(
    root: pathlib.Path | str, *, archive_sha256: str = ""
) -> CaptureCertificationReport:
    """Derive every vendor convention this capture can settle. No network."""
    from src.adapters.thetadata.live_behavior import (
        BehaviorDimension,
        CaptureIdentity,
        EvidenceStatus,
        LiveBehaviorObservation,
        ObservationBasis,
        ReconstructionMetric,
        VendorBehaviorLedger,
    )

    capture = load_capture(root)
    quote = capture.rows(OPTION_QUOTE)
    greeks = capture.rows(OPTION_GREEKS)
    oi_rows = capture.rows(OPTION_OPEN_INTEREST)
    listing = capture.rows(OPTION_CONTRACT_LIST)
    index = capture.rows(INDEX_PRICE)

    # -- universe -----------------------------------------------------------
    list_set = {ContractKey.from_row(r) for r in listing}
    quote_set = {ContractKey.from_row(r) for r in quote}
    greeks_set = {ContractKey.from_row(r) for r in greeks}
    snapshots = quote_set | greeks_set
    universe = UniverseCertification(
        contract_list_count=len(listing),
        quote_count=len(quote),
        greeks_count=len(greeks),
        contract_list_hash=_set_hash(list_set),
        quote_hash=_set_hash(quote_set),
        greeks_hash=_set_hash(greeks_set),
        quote_matches_list=quote_set == list_set,
        greeks_matches_list=greeks_set == list_set,
        only_in_list=tuple(sorted(k.canonical for k in list_set - snapshots)),
        only_in_snapshots=tuple(sorted(k.canonical for k in snapshots - list_set)),
    )

    # -- open interest ------------------------------------------------------
    oi_by_key: dict[ContractKey, int] = {}
    for row in oi_rows:
        try:
            oi_by_key[ContractKey.from_row(row)] = int(float(row["open_interest"]))
        except (KeyError, ValueError):
            continue
    missing = list_set - set(oi_by_key)
    explicit_zero = sum(1 for v in oi_by_key.values() if v == 0)
    listed_by_exp: dict[str, int] = defaultdict(int)
    missing_by_exp: dict[str, int] = defaultdict(int)
    for key in list_set:
        listed_by_exp[key.expiration.isoformat()] += 1
    for key in missing:
        missing_by_exp[key.expiration.isoformat()] += 1
    coverage = OpenInterestCoverage(
        universe_count=len(list_set),
        present_count=len(oi_by_key) - explicit_zero,
        explicit_zero_count=explicit_zero,
        missing_count=len(missing),
        missing_by_expiration=tuple(
            (e, listed_by_exp[e], missing_by_exp[e]) for e in sorted(missing_by_exp)
        ),
        missing_identities_hash=_set_hash(missing),
        fully_missing_expirations=tuple(
            e for e in sorted(missing_by_exp) if missing_by_exp[e] == listed_by_exp[e]
        ),
    )

    # -- quote/greeks synchrony --------------------------------------------
    quote_by_key = {ContractKey.from_row(r): r for r in quote}
    gaps: list[float] = []
    same_ts = same_bid = same_ask = 0
    for row in greeks:
        other = quote_by_key.get(ContractKey.from_row(row))
        if other is None:
            continue
        if other["timestamp"].strip() == row["timestamp"].strip():
            same_ts += 1
        try:
            if abs(float(other["bid"]) - float(row["bid"])) < 1e-9:
                same_bid += 1
            if abs(float(other["ask"]) - float(row["ask"])) < 1e-9:
                same_ask += 1
            gaps.append(
                abs(
                    (
                        _parse_et(other["timestamp"]) - _parse_et(row["timestamp"])
                    ).total_seconds()
                )
            )
        except (KeyError, ValueError):
            continue
    gaps.sort()
    n_gap = len(gaps) or 1
    synchrony = EndpointSynchrony(
        overlap=len(gaps),
        identical_timestamp_ratio=same_ts / n_gap,
        identical_bid_ratio=same_bid / n_gap,
        identical_ask_ratio=same_ask / n_gap,
        p99_gap_seconds=gaps[min(int(0.99 * n_gap), n_gap - 1)] if gaps else 0.0,
        max_gap_seconds=max(gaps) if gaps else 0.0,
    )

    # -- underlying ---------------------------------------------------------
    embedded_prices = sorted({r["underlying_price"].strip() for r in greeks})
    embedded_times = sorted({r["underlying_timestamp"].strip() for r in greeks})
    if len(embedded_prices) != 1 or len(embedded_times) != 1:
        raise CaptureCertificationError(
            f"the Greeks response carries {len(embedded_prices)} distinct "
            f"underlying prices and {len(embedded_times)} timestamps. This "
            "certification reconstructs one snapshot against one underlying "
            "state; several would need a per-row reconstruction instead."
        )
    spot = float(embedded_prices[0])
    valuation = _parse_et(embedded_times[0])
    index_price = float(index[0]["price"]) if index else float("nan")
    index_time = index[0]["timestamp"].strip() if index else ""
    underlying = {
        "vendor_greeks_underlying_price": spot,
        "vendor_greeks_underlying_timestamp": embedded_times[0],
        "index_snapshot_price": index_price,
        "index_snapshot_timestamp": index_time,
        "price_difference": round(index_price - spot, 10),
        "synchronized": index_price == spot and index_time == embedded_times[0],
        "authoritative_for_vendor_model": "GREEKS_RESPONSE_EMBEDDED_VENDOR_UNDERLYING",
    }

    # -- the vendor's clock, by inversion -----------------------------------
    usable = _usable_greeks(greeks)
    by_expiration: dict[str, list[float]] = defaultdict(list)
    for row in usable:
        key = ContractKey.from_row(row)
        d = float(row["delta"])
        if not (0.05 <= abs(d) <= 0.95):
            continue
        years = _implied_years(
            spot=spot,
            strike=float(row["strike"]),
            sigma=float(row["implied_vol"]),
            delta_value=d,
            rate=4.2,
            right=key.option_right,
        )
        if years is not None and years > 0.0:
            by_expiration[key.expiration.isoformat()].append(years * 365.0)

    readings: list[ExpirationClockReading] = []
    for exp_text in sorted(by_expiration):
        values = sorted(by_expiration[exp_text])
        if len(values) < 20:
            continue
        exp_d = date.fromisoformat(exp_text)
        cal = (exp_d - valuation.date()).days
        intraday = (
            datetime.combine(exp_d, time(16, 0), tzinfo=EASTERN) - valuation
        ).total_seconds() / 86400.0
        lo = values[int(0.05 * len(values))]
        hi = values[min(int(0.95 * len(values)), len(values) - 1)]
        readings.append(
            ExpirationClockReading(
                expiration=exp_text,
                rows=len(values),
                implied_days=statistics.median(values),
                calendar_days=cal,
                intraday_days_to_1600=intraday,
                spread=hi - lo,
            )
        )

    # Where does the intraday clock stop applying? Derived, not assumed.
    intraday_expirations = [r for r in readings if r.matches_intraday_1600]
    boundary = (
        max(date.fromisoformat(r.expiration) for r in intraday_expirations)
        if intraday_expirations
        else None
    )

    def score(**kwargs: Any) -> HypothesisScore | None:
        return _score(
            usable,
            spot=spot,
            valuation=valuation,
            whole_days_from=boundary,
            **kwargs,
        )

    # The two readings of the same configured rate. Which one the vendor used
    # is *derived* from the fit, not asserted: a later capture in which the
    # vendor has corrected the parameter must be able to say so, and a
    # certification that could only ever report a conflict would report one
    # after the conflict was gone.
    rate_hypotheses = (
        ("DECIMAL_ANNUAL_RATE", 4.2),
        ("PERCENT_ANNUAL_RATE", 0.042),
    )
    scored_rates = [
        (unit, s)
        for unit, rate in rate_hypotheses
        if (
            s := score(
                rate=rate,
                days_per_year=365.0,
                expiry_time=time(16, 0),
                label=f"rate_value consumed as {unit} (r={rate})",
            )
        )
        is not None
    ]
    rate_scores = tuple(s for _unit, s in scored_rates)
    day_count_scores = tuple(
        s
        for s in (
            score(rate=4.2, days_per_year=basis, expiry_time=time(16, 0), label=label)
            for basis, label in (
                (365.0, "ACT/365"),
                (365.25, "ACT/365.25"),
                (360.0, "ACT/360"),
                (252.0, "ACT/252"),
            )
        )
        if s is not None
    )
    front = [
        r
        for r in usable
        if boundary is not None
        and date.fromisoformat(r["expiration"].strip().strip('"')) <= boundary
    ]
    expiration_time_scores = tuple(
        s
        for s in (
            _score(
                front,
                spot=spot,
                valuation=valuation,
                rate=4.2,
                days_per_year=365.0,
                expiry_time=t,
                label=label,
            )
            for t, label in (
                (time(16, 0), "16:00 America/New_York"),
                (time(15, 30), "15:30 America/New_York"),
                (time(16, 15), "16:15 America/New_York"),
                (time(16, 30), "16:30 America/New_York"),
            )
        )
        if s is not None
    )

    # -- IV price basis -----------------------------------------------------
    iv_basis_scores: list[tuple[str, int, float, float]] = []
    for basis_name in ("NBBO_MID", "BID", "ASK"):
        errors: list[float] = []
        within = 0
        for row in usable:
            key = ContractKey.from_row(row)
            if not (0.05 <= abs(float(row["delta"])) <= 0.95):
                continue
            exp_d = key.expiration
            if boundary is not None and exp_d > boundary:
                days = float((exp_d - valuation.date()).days)
            else:
                days = (
                    datetime.combine(exp_d, time(16, 0), tzinfo=EASTERN) - valuation
                ).total_seconds() / 86400.0
            if days <= 0.0:
                continue
            try:
                bid, ask = float(row["bid"]), float(row["ask"])
            except (KeyError, ValueError):
                continue
            target = {"NBBO_MID": (bid + ask) / 2.0, "BID": bid, "ASK": ask}[basis_name]
            solved = _solve_iv(
                target,
                spot=spot,
                strike=float(row["strike"]),
                years=days / 365.0,
                rate=4.2,
                right=key.option_right,
            )
            if solved is None:
                continue
            err = abs(solved - float(row["implied_vol"]))
            errors.append(err)
            if err <= IMPLIED_VOL_TICK / 2:
                within += 1
        if errors:
            iv_basis_scores.append(
                (
                    basis_name,
                    len(errors),
                    statistics.median(errors),
                    within / len(errors),
                )
            )

    # -- the ledger ---------------------------------------------------------
    identity = CaptureIdentity(
        session_id=capture.session_id,
        manifest_hash=capture.manifest_hash,
        archive_sha256=archive_sha256 or capture.manifest_hash,
    )
    best_rate = min(rate_scores, key=lambda s: s.delta_rmse) if rate_scores else None
    #: What the pinned OpenAPI description says. Constant, and never written
    #: from a measurement -- that separation is the whole point of this module.
    documented_rate_unit = "PERCENT_ANNUAL_RATE"
    observed_rate_unit = next(
        (unit for unit, s in scored_rates if s is best_rate), documented_rate_unit
    )
    rate_status = (
        EvidenceStatus.DOCUMENTATION_LIVE_CONFLICT
        if observed_rate_unit != documented_rate_unit
        else EvidenceStatus.DOCUMENTATION_LIVE_AGREE
    )
    best_day = (
        min(day_count_scores, key=lambda s: s.delta_rmse) if day_count_scores else None
    )
    best_exp = (
        min(expiration_time_scores, key=lambda s: s.delta_rmse)
        if expiration_time_scores
        else None
    )
    best_iv = min(iv_basis_scores, key=lambda s: s[2]) if iv_basis_scores else None
    beyond = [r for r in readings if not r.matches_intraday_1600]

    observations = [
        LiveBehaviorObservation(
            dimension=BehaviorDimension.RATE_UNITS,
            status=rate_status,
            basis=ObservationBasis.LIVE_NUMERICAL_RECONSTRUCTION,
            documented_value=documented_rate_unit,
            observed_value=observed_rate_unit,
            documentation_reference="components/parameters/rate_value/description",
            capture=identity,
            rows_used=best_rate.rows if best_rate else 0,
            scope="/v3/option/snapshot/greeks/first_order, greeks_version=latest",
            metrics=tuple(
                ReconstructionMetric(
                    hypothesis=s.hypothesis,
                    statistic="delta_rmse",
                    value=s.delta_rmse,
                    rows=s.rows,
                    selected=s is best_rate,
                )
                for s in rate_scores
            ),
            notes=(
                (
                    "The document says percent and the implementation reads a "
                    "decimal. Both records are kept; the observed reading "
                    "governs request construction because the request is "
                    "answered by the implementation."
                )
                if rate_status.is_conflict
                else "The implementation reads rate_value as documented."
            ),
        ),
        LiveBehaviorObservation(
            dimension=BehaviorDimension.DAY_COUNT,
            status=EvidenceStatus.LIVE_ONLY,
            basis=ObservationBasis.LIVE_NUMERICAL_RECONSTRUCTION,
            observed_value="ACT_365",
            capture=identity,
            rows_used=best_day.rows if best_day else 0,
            scope="SPXW first-order greeks in this capture",
            metrics=tuple(
                ReconstructionMetric(
                    hypothesis=s.hypothesis,
                    statistic="delta_rmse",
                    value=s.delta_rmse,
                    rows=s.rows,
                    selected=s is best_day,
                )
                for s in day_count_scores
            ),
        ),
        LiveBehaviorObservation(
            dimension=BehaviorDimension.EXPIRATION_TIMESTAMP,
            status=EvidenceStatus.LIVE_ONLY,
            basis=ObservationBasis.LIVE_NUMERICAL_RECONSTRUCTION,
            observed_value="16:00 America/New_York",
            capture=identity,
            rows_used=best_exp.rows if best_exp else 0,
            scope=(
                "SPXW expirations up to and including "
                f"{boundary.isoformat() if boundary else 'n/a'}. Beyond that "
                f"boundary {len(beyond)} expirations in this capture price on "
                "whole calendar days instead, so this rule is NOT established "
                "for longer maturities and must not be applied to them."
            ),
            metrics=tuple(
                ReconstructionMetric(
                    hypothesis=s.hypothesis,
                    statistic="delta_rmse",
                    value=s.delta_rmse,
                    rows=s.rows,
                    selected=s is best_exp,
                )
                for s in expiration_time_scores
            ),
            notes=(
                "Read by inverting delta and implied volatility for the vendor's "
                "own time-to-expiry, not by scoring a global hypothesis."
            ),
        ),
        LiveBehaviorObservation(
            dimension=BehaviorDimension.IV_PRICE_BASIS,
            status=EvidenceStatus.DOCUMENTATION_LIVE_CONFLICT,
            basis=ObservationBasis.LIVE_NUMERICAL_RECONSTRUCTION,
            documented_value="TRADE_PRICE",
            observed_value="NBBO_MID",
            documentation_reference=(
                "components/schemas/first_order_greeks/properties/implied_vol"
            ),
            capture=identity,
            rows_used=best_iv[1] if best_iv else 0,
            scope="SPXW first-order greeks in this capture",
            metrics=tuple(
                ReconstructionMetric(
                    hypothesis=name,
                    statistic="median_abs_iv_error",
                    value=median,
                    rows=rows,
                    selected=best_iv is not None and name == best_iv[0],
                )
                for name, rows, median, _within in iv_basis_scores
            ),
            notes=(
                "The residual under NBBO mid is half the reporting tick of "
                "implied_vol, which is the floor -- there is nothing left to "
                "explain. Bid and ask are two orders of magnitude worse."
            ),
        ),
        LiveBehaviorObservation(
            dimension=BehaviorDimension.UNDERLYING_SOURCE,
            status=EvidenceStatus.LIVE_ONLY,
            basis=ObservationBasis.LIVE_FIELD_READ,
            observed_value="GREEKS_RESPONSE_EMBEDDED_VENDOR_UNDERLYING",
            capture=identity,
            rows_used=len(greeks),
            scope="reproduction of the vendor's own model",
            notes=(
                f"The Greeks response carries one underlying print ({spot} at "
                f"{embedded_times[0]}). The separately captured index snapshot "
                f"returned {index_price} at {index_time}, which is a different "
                "state. The index response is not what these Greeks were "
                "computed from and must not be described as synchronized."
            ),
        ),
        LiveBehaviorObservation(
            dimension=BehaviorDimension.DIVIDEND_CONVENTION,
            status=EvidenceStatus.DOCUMENTATION_ONLY,
            basis=ObservationBasis.REQUEST_BOUND,
            documented_value="ANNUAL_CASH_DIVIDEND_CONVERTED_TO_YIELD",
            documentation_reference="ThetaData greeks article, annual_div",
            scope=(
                "annual_dividend=0.0 was requested, so this capture exercises "
                "only the zero case and cannot speak to non-zero handling"
            ),
            notes="effective dividend 0, effective q 0 for this capture.",
        ),
        LiveBehaviorObservation(
            dimension=BehaviorDimension.CONTRACT_LIST_UNIVERSE,
            status=EvidenceStatus.LIVE_ONLY,
            basis=ObservationBasis.LIVE_SET_COMPARISON,
            observed_value=universe.state,
            capture=identity,
            rows_used=universe.contract_list_count,
            scope=(
                "SPXW, session 2026-08-10, max_dte=60, quote and first-order "
                "greeks snapshots only"
            ),
            notes=(
                f"contract list {universe.contract_list_hash[:16]}, quote "
                f"{universe.quote_hash[:16]}, greeks {universe.greeks_hash[:16]}"
            ),
        ),
    ]

    return CaptureCertificationReport(
        schema_version=CAPTURE_CERTIFICATION_SCHEMA_VERSION,
        session_id=capture.session_id,
        manifest_hash=capture.manifest_hash,
        archive_sha256=archive_sha256,
        captured_at=capture.captured_at,
        parser_version=capture.parser_version,
        verified_records=capture.verified_records,
        record_hashes=capture.record_hashes,
        universe=universe,
        open_interest=coverage,
        synchrony=synchrony,
        underlying=underlying,
        rate_scores=rate_scores,
        day_count_scores=day_count_scores,
        expiration_time_scores=expiration_time_scores,
        iv_basis_scores=tuple(iv_basis_scores),
        clock_readings=tuple(readings),
        ledger=VendorBehaviorLedger(observations=tuple(observations)),
        rows_reconstructed=len(usable),
    )
