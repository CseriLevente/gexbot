"""Deterministic research events from one verified ThetaData v3 capture.

Every emitted record is bound to the SHA-256 of the raw payload it came from,
the native row inside that payload and a versioned normalization rule, and the
document names the verified capture the payloads belong to. Availability comes
only from *recorded* receive times: the manifest's ``response_received_at``
(inside the verified manifest hash) corroborated, when present, by the attempt
log's ``received_at`` for the same request and body. A payload without a
recorded receipt yields no events at all; nothing here substitutes an event
time, a request time or a historical download time for an unknown receipt.

Event times are the vendor's own row timestamps, read as America/New_York wall
clock (the only place a zone is assumed, and it is recorded). A row whose
vendor timestamp postdates the local receipt is a clock inconsistency between
two machines; by default it is excluded and counted. The caller may allow a
bounded tolerance, in which case the record's availability is deferred to the
vendor event time and the record says so -- availability is never moved earlier.

One snapshot payload is expected to carry one row per identity. When it
carries more, the rows are grouped *before* anything is emitted: a group whose
rows agree on every column the rule reads is coalesced to one record with
explicit accounting; a group whose rows disagree is excluded whole, because a
snapshot offers no authoritative order or revision among them. Row order never
decides which observation survives.

What this module does not do: merge several captures of one session (a second
snapshot re-observing an unchanged row would collide with the first under the
event store's revision identity; that merge rule is specified for the pilot
collector, not implemented here), fabricate open interest for identities the
vendor did not answer, or decide anything about GEX, strategy or trading.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import pathlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from src.adapters.http_attempts import HttpAttemptLog
from src.adapters.raw_store import CaptureOrigin, RawCaptureManifest
from src.adapters.thetadata.capture_certification import (
    INDEX_PRICE,
    OPTION_CONTRACT_LIST,
    OPTION_GREEKS,
    OPTION_OPEN_INTEREST,
    OPTION_QUOTE,
    LoadedCapture,
    load_capture,
)
from src.domain.iv import VENDOR_IV_ERROR_LIMIT, IVSource
from src.domain.settlement import (
    SettlementRule,
    SettlementRuleError,
    SettlementRuleKind,
)
from src.domain.vendor_time import NONEXISTENT, parse_vendor_timestamp
from src.gex.sessions import EASTERN
from src.replay.event_store import (
    LINEAGE_EVENT_SCHEMA,
    MAX_INTEGER,
    decimal_value,
    option_parts,
)

NORMALIZER = "thetadata-research-events/2.1.36"
#: The events' origin follows the capture origin stamped inside the verified
#: manifest hash: only a capture whose every record came from a real round trip
#: is ``RECORDED_NORMALIZED``; a fixture or unknown transport stays ``SYNTHETIC``.
RECORDED, SYNTHETIC = "RECORDED_NORMALIZED", "SYNTHETIC"
VENDOR_TIMEZONE = "America/New_York"
#: The two independent receipt records of one response must agree this closely
#: or the endpoint is refused: a larger gap means they describe different
#: deliveries, and neither can then be taken as this payload's receipt.
RECEIPT_EVIDENCE_TOLERANCE = timedelta(seconds=5)
#: Upper bound on the caller-supplied vendor-clock tolerance. Anything beyond a
#: few seconds is not clock skew between two machines; it is a different event.
MAX_RECEIPT_CLOCK_TOLERANCE_MS = 5_000
IV_PRICE_BASIS = "UNDOCUMENTED_BY_VENDOR"

KINDS = {
    INDEX_PRICE: "spx_price",
    OPTION_QUOTE: "option_quote",
    OPTION_OPEN_INTEREST: "open_interest",
    OPTION_GREEKS: "greeks",
    OPTION_CONTRACT_LIST: "contract_list",
}
#: Rule names move when a rule's meaning moves. Revision 2 of the four row
#: rules groups repeated identities before emission (independent review of the
#: first v2.1.35 cut: revision 1 kept the first row of a conflicting pair).
RULES = {
    "contract_list": "thetadata-v3/contract_list/1",
    "option_quote": "thetadata-v3/option_quote/2",
    "greeks": "thetadata-v3/greeks_first_order/2",
    "open_interest": "thetadata-v3/open_interest_prior_session/2",
    "spx_price": "thetadata-v3/index_price/2",
    "model_evidence": "thetadata-v3/model_evidence/1",
}
CONFLICTING = "CONFLICTING_DUPLICATE_OBSERVATIONS"
COALESCED = "IDENTICAL_DUPLICATE_COALESCED"
NOT_SCHEDULED = "NOT_SCHEDULED"
NOT_ACQUIRED = "NOT_ACQUIRED"
#: The native columns each rule reads. Observed on the 2026-09-02 capture under
#: ``thetadata-v3-parser/2.1.17``; a payload missing any of them is not the
#: schema these rules were written for and the whole normalization refuses.
#: Extra columns (exchanges, conditions, sizes, higher-order Greeks) are left in
#: the raw bytes and reported as unused, never silently interpreted.
REQUIRED_COLUMNS = {
    INDEX_PRICE: ("timestamp", "symbol", "price"),
    OPTION_QUOTE: (
        "timestamp",
        "symbol",
        "expiration",
        "strike",
        "right",
        "bid",
        "ask",
    ),
    OPTION_OPEN_INTEREST: (
        "timestamp",
        "symbol",
        "expiration",
        "strike",
        "right",
        "open_interest",
    ),
    OPTION_GREEKS: (
        "symbol",
        "expiration",
        "strike",
        "right",
        "timestamp",
        "delta",
        "implied_vol",
        "iv_error",
    ),
    OPTION_CONTRACT_LIST: ("symbol", "expiration", "strike", "right"),
}
MODEL_PARAMETERS = ("rate_type", "rate_value", "annual_dividend", "version")
BASIS_RECEIPT = "RECEIPT"
BASIS_DEFERRED = "RECEIPT_DEFERRED_TO_VENDOR_EVENT_TIME"
BASIS_GREEKS_RECEIPT = "GREEKS_RECEIPT"


class NormalizationError(ValueError):
    """The capture cannot be normalized under these rules; nothing is guessed."""


@dataclass(frozen=True, slots=True)
class Receipt:
    """When one payload was actually in hand, from the records that say so."""

    endpoint: str
    payload_sha256: str
    location: str
    request_started_at: datetime | None
    manifest_received_at: datetime | None
    attempt_received_at: datetime | None
    available_at: datetime | None
    evidence: tuple[str, ...]
    refusal: str | None
    #: The logical request id the manifest record carries (v2.1.36), so a
    #: merged session record can name the request that produced its row.
    request_id: str = ""
    #: What the attempt log says about an endpoint that produced no verified
    #: payload (v2.1.36): attempts made, the last status, the last receipt.
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.astimezone(UTC).isoformat() if value else None

        return {
            "endpoint": self.endpoint,
            "payload_sha256": self.payload_sha256,
            "location": self.location,
            "request_id": self.request_id,
            "request_started_at": iso(self.request_started_at),
            "manifest_received_at": iso(self.manifest_received_at),
            "attempt_received_at": iso(self.attempt_received_at),
            "available_at": iso(self.available_at),
            "evidence": list(self.evidence),
            "refusal": self.refusal,
            "detail": self.detail,
        }


@dataclass(slots=True)
class _EndpointCoverage:
    kind: str
    rows: int = 0
    emitted: int = 0
    excluded: Counter[str] = field(default_factory=Counter)
    columns: tuple[str, ...] = ()
    vendor_event_min: datetime | None = None
    vendor_event_max: datetime | None = None
    clock_lead_rows: int = 0
    clock_lead_max_ms: int = 0
    identical_groups: int = 0
    conflicting_groups: int = 0
    conflicting_keys: list[str] = field(default_factory=list)

    def observe(self, event_at: datetime) -> None:
        if self.vendor_event_min is None or event_at < self.vendor_event_min:
            self.vendor_event_min = event_at
        if self.vendor_event_max is None or event_at > self.vendor_event_max:
            self.vendor_event_max = event_at

    def as_dict(self, receipt: Receipt, unused: tuple[str, ...]) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "rule": RULES[self.kind],
            "rows": self.rows,
            "emitted": self.emitted,
            "excluded": dict(sorted(self.excluded.items())),
            "columns": list(self.columns),
            "unused_columns": list(unused),
            "receipt": receipt.as_dict(),
            "vendor_event_time": {
                "min": (
                    self.vendor_event_min.astimezone(UTC).isoformat()
                    if self.vendor_event_min
                    else None
                ),
                "max": (
                    self.vendor_event_max.astimezone(UTC).isoformat()
                    if self.vendor_event_max
                    else None
                ),
            },
            "vendor_clock_lead": {
                "rows_after_receipt": self.clock_lead_rows,
                "max_ms": self.clock_lead_max_ms,
            },
            "duplicate_groups": {
                "identical_coalesced": self.identical_groups,
                "conflicting_excluded": self.conflicting_groups,
                "conflicting_keys": sorted(self.conflicting_keys),
            },
        }


@dataclass(frozen=True, slots=True)
class NormalizedCapture:
    """Everything one capture yields: hash-bound records plus their accounting."""

    session_date: date
    origin: str
    provenance: dict[str, Any]
    records: list[dict[str, Any]]
    coverage: dict[str, Any]

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": LINEAGE_EVENT_SCHEMA,
            "origin": self.origin,
            "provenance": self.provenance,
            "records": self.records,
        }

    def encode(self) -> bytes:
        """Compact, sorted, LF-terminated UTF-8; identical bytes for identical input."""
        return (
            json.dumps(
                self.document(),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
                ensure_ascii=True,
            )
            + "\n"
        ).encode("utf-8")


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _receipts(
    root: pathlib.Path, capture: LoadedCapture
) -> tuple[dict[str, Receipt], dict[str, Any]]:
    """Per-endpoint receipt evidence, re-verified rather than trusted."""
    manifest = json.loads((root / "manifest.json").read_bytes())
    rebuilt = RawCaptureManifest.rebuilt_from(manifest)
    if rebuilt.manifest_hash != capture.manifest_hash:
        raise NormalizationError("manifest changed between verification and read")
    origins = {
        str(CaptureOrigin(record.capture_origin).value) for record in rebuilt.records
    }
    live = all(CaptureOrigin(origin).is_live for origin in origins)
    attempt_log: dict[str, Any] = {"origins": sorted(origins), "live": live}
    attempts_root = root / "attempts"
    attempt_report = HttpAttemptLog.open_existing(attempts_root)
    attempts_present = (attempts_root / "index.jsonl").is_file()
    entries = (
        HttpAttemptLog.recovered_from(attempts_root)
        if attempts_present and attempt_report.ok
        else ()
    )
    attempt_log.update(
        {
            "present": attempts_present,
            "usable": attempts_present and attempt_report.ok,
            "findings": list(attempt_report.findings) if attempts_present else [],
            "index_sha256": attempt_report.index_hash or None,
        }
    )
    receipts: dict[str, Receipt] = {}
    for record in manifest.get("records", []):
        endpoint = str(record.get("endpoint", ""))
        if endpoint not in KINDS:
            continue
        digest = capture.record_hashes[endpoint]
        if record.get("payload_hash") != digest:
            raise NormalizationError(f"payload digest disagreement for {endpoint}")
        started = _aware(record.get("request_started_at"))
        manifest_received = _aware(record.get("response_received_at"))
        attempt_received = None
        for entry in entries:
            if (
                entry.get("logical_request_id") == record.get("request_id")
                and entry.get("response_body_hash") == digest
                and entry.get("succeeded") is True
            ):
                attempt_received = _aware(entry.get("received_at"))
                break
        evidence = []
        refusal = None
        candidates = []
        if manifest_received is not None:
            evidence.append("manifest.response_received_at")
            candidates.append(manifest_received)
        if attempt_received is not None:
            evidence.append("attempts.received_at")
            candidates.append(attempt_received)
        if not candidates:
            refusal = "AVAILABILITY_UNKNOWN"
        elif len(candidates) == 2 and abs(candidates[0] - candidates[1]) > (
            RECEIPT_EVIDENCE_TOLERANCE
        ):
            refusal = "RECEIVE_TIME_CONFLICT"
        elif started is not None and min(candidates) < started:
            refusal = "RECEIVED_BEFORE_REQUEST"
        receipts[endpoint] = Receipt(
            endpoint=endpoint,
            payload_sha256=digest,
            location=f"raw/{record.get('payload_location', '')}",
            request_started_at=started,
            manifest_received_at=manifest_received,
            attempt_received_at=attempt_received,
            # The later of the two records: a payload cannot have been in hand
            # before either clock says it was.
            available_at=max(candidates) if refusal is None else None,
            evidence=tuple(evidence),
            refusal=refusal,
            request_id=str(record.get("request_id", "")),
        )
    attempt_log["entries"] = tuple(entries)
    return receipts, attempt_log


def _attempt_detail(endpoint: str, entries: tuple[Any, ...]) -> dict[str, Any] | None:
    """What the verified attempt log recorded for an endpoint with no payload."""
    made = [e for e in entries if e.get("endpoint") == endpoint]
    if not made:
        return None
    last = made[-1]
    return {
        "attempts": len(made),
        "last_status_code": last.get("status_code"),
        "last_started_at": last.get("started_at"),
        "last_received_at": last.get("received_at"),
        "succeeded": any(e.get("succeeded") is True for e in made),
    }


def _table(
    root: pathlib.Path, receipt: Receipt
) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    raw = (root / receipt.location).read_bytes()
    if hashlib.sha256(raw).hexdigest() != receipt.payload_sha256:
        raise NormalizationError(f"{receipt.location} changed after verification")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    rows = list(reader)
    columns = tuple(reader.fieldnames or ())
    missing = [c for c in REQUIRED_COLUMNS[receipt.endpoint] if c not in columns]
    if missing or len(set(columns)) != len(columns):
        raise NormalizationError(
            f"{receipt.endpoint} native schema mismatch: columns {list(columns)} "
            f"lack {missing}; rules {RULES[KINDS[receipt.endpoint]]} do not apply"
        )
    if any(None in row or None in row.values() for row in rows):
        raise NormalizationError(f"{receipt.endpoint} has ragged CSV rows")
    return rows, columns


def _identity(row: dict[str, str]) -> tuple[str, str | None]:
    """Canonical option identity, or the exclusion reason."""
    symbol = row["symbol"].strip()
    right = row["right"].strip().upper()
    try:
        expiration = date.fromisoformat(row["expiration"].strip())
        strike = Decimal(row["strike"].strip())
    except (ValueError, InvalidOperation):
        return "", "INVALID_IDENTITY"
    if not strike.is_finite() or strike <= 0:
        return "", "INVALID_IDENTITY"
    key = f"{symbol}|{expiration.isoformat()}|{format(strike.normalize(), 'f')}|{right}"
    try:
        option_parts(key)
    except ValueError:
        return "", "UNEXPECTED_SYMBOL" if symbol != "SPXW" else "INVALID_IDENTITY"
    return key, None


def _event_time(value: str) -> tuple[datetime | None, str | None]:
    parsed = parse_vendor_timestamp(value, assumed_timezone=VENDOR_TIMEZONE)
    if parsed is None:
        return None, "UNPARSEABLE_TIMESTAMP"
    if parsed.ambiguity_resolution == NONEXISTENT:
        return None, "NONEXISTENT_WALL_CLOCK"
    return parsed.normalized_utc, None


def _price(value: str, *, zero: bool) -> tuple[str | None, str | None]:
    text_value = value.strip()
    try:
        decimal_value(text_value, zero=zero)
    except ValueError:
        return None, "ZERO_OR_INVALID_ASK" if not zero else "INVALID_PRICE"
    return text_value, None


def _finite(value: str) -> float | None:
    try:
        number = float(value.strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _settlement_rule(capture: LoadedCapture) -> SettlementRule:
    reading = (
        capture.documentation.value_for("OPEN_INTEREST_SETTLEMENT")
        if capture.documentation is not None
        else ""
    )
    try:
        kind = SettlementRuleKind(reading)
    except ValueError as error:
        raise NormalizationError(
            "the capture's documentation does not settle which session its "
            "open interest belongs to; the normalizer will not assume one"
        ) from error
    return SettlementRule(kind=kind)


def _model(capture: LoadedCapture) -> tuple[str, float, float, dict[str, str]] | None:
    """The vendor model the verified Greeks request named; ``None`` only for a
    cycle that acquired no Greeks payload (there is then no verified request)."""
    if OPTION_GREEKS not in capture.request.parameters:
        return None
    parameters = capture.request.parameters[OPTION_GREEKS]
    chosen = {name: parameters[name] for name in MODEL_PARAMETERS if name in parameters}
    if "rate_value" not in chosen:
        raise NormalizationError("the verified Greeks request carries no rate_value")
    rate = _finite(chosen["rate_value"])
    dividend = _finite(chosen.get("annual_dividend", "0"))
    if rate is None or dividend is None:
        raise NormalizationError("nonfinite Greeks model parameters in the request")
    model_id = "thetadata-v3/option/snapshot/greeks/first_order|" + "|".join(
        f"{name}={chosen[name]}" for name in MODEL_PARAMETERS if name in chosen
    )
    return model_id, rate, dividend, chosen


def _session_date(capture: LoadedCapture) -> date:
    """The session this capture belongs to, from verified evidence only.

    The listing request's ``date`` parameter (proved by the planned-request
    binding) and the preflight approval's ``market_session_date`` (proved by
    the approval hash and the manifest stamps) must agree when both exist; a
    partial-scope cycle without a listing has only the approval; a capture with
    neither is refused rather than dated from a clock.
    """
    listed = capture.request.value_for(OPTION_CONTRACT_LIST, "date")
    approved = capture.market_session_date
    if listed and approved is not None and date.fromisoformat(listed) != approved:
        raise NormalizationError(
            f"listing date {listed} disagrees with the approved session {approved}"
        )
    if listed:
        session = date.fromisoformat(listed)
    elif approved is not None:
        session = approved
    else:
        raise NormalizationError(
            "neither the verified contract-list request nor a verified approval "
            "names this capture's session"
        )
    captured = _aware(capture.captured_at)
    if captured is None or captured.astimezone(EASTERN).date() != session:
        raise NormalizationError(
            f"listing date {session} disagrees with the capture's valuation "
            f"instant {capture.captured_at!r}"
        )
    return session


def _absent(
    endpoint: str, refusal: str, detail: dict[str, Any] | None = None
) -> Receipt:
    """The receipt of an endpoint that yielded no verified payload."""
    return Receipt(
        endpoint=endpoint,
        payload_sha256="",
        location="",
        request_started_at=None,
        manifest_received_at=None,
        attempt_received_at=None,
        available_at=None,
        evidence=(),
        refusal=refusal,
        detail=detail,
    )


class _Builder:
    def __init__(
        self,
        root: pathlib.Path,
        capture: LoadedCapture,
        receipts: dict[str, Receipt],
        tolerance: timedelta,
        scope: frozenset[str],
    ) -> None:
        self.root = root
        self.capture = capture
        self.receipts = receipts
        self.tolerance = tolerance
        self.scope = scope
        #: Membership can only be checked against a listing in this payload
        #: set; a market cycle carries none and says so instead of guessing.
        self.membership_checked = OPTION_CONTRACT_LIST in scope
        self.records: list[dict[str, Any]] = []
        self.coverage: dict[str, _EndpointCoverage] = {}
        self.unused: dict[str, tuple[str, ...]] = {}
        self.inventory: list[str] = []
        self.inventory_set: set[str] = set()
        self.identities: dict[str, set[str]] = {}
        self.oi_as_of: Counter[str] = Counter()
        self.expirations: Counter[str] = Counter()

    def _load(self, endpoint: str) -> tuple[list[dict[str, str]], _EndpointCoverage]:
        receipt = self.receipts[endpoint]
        rows, columns = _table(self.root, receipt)
        coverage = _EndpointCoverage(
            kind=KINDS[endpoint], rows=len(rows), columns=columns
        )
        self.coverage[endpoint] = coverage
        self.unused[endpoint] = tuple(
            c for c in columns if c not in REQUIRED_COLUMNS[endpoint]
        )
        return rows, coverage

    def _availability(
        self, receipt: Receipt, event_at: datetime, coverage: _EndpointCoverage
    ) -> tuple[datetime | None, str | None]:
        available = receipt.available_at
        assert available is not None
        if event_at <= available:
            return available, BASIS_RECEIPT
        lead = event_at - available
        coverage.clock_lead_rows += 1
        coverage.clock_lead_max_ms = max(
            coverage.clock_lead_max_ms, math.ceil(lead / timedelta(milliseconds=1))
        )
        if lead <= self.tolerance:
            return event_at, BASIS_DEFERRED
        return None, None

    def _emit(
        self,
        kind: str,
        key: str,
        event_at: datetime,
        available_at: datetime,
        data: dict[str, Any],
        receipt: Receipt,
        row_index: int | None,
        basis: str,
        coverage: _EndpointCoverage | None,
    ) -> None:
        self.records.append(
            {
                "kind": kind,
                "key": key,
                "event_at": _iso(event_at),
                "available_at": _iso(available_at),
                "sequence": 0,
                "data": data,
                "lineage": {
                    "raw_sha256": receipt.payload_sha256,
                    "row_index": row_index,
                    "rule": RULES[kind],
                    "availability_basis": basis,
                },
            }
        )
        if coverage is not None:
            coverage.emitted += 1

    def contract_list(self) -> None:
        if self._skipped(OPTION_CONTRACT_LIST):
            return
        receipt = self.receipts[OPTION_CONTRACT_LIST]
        rows, coverage = self._load(OPTION_CONTRACT_LIST)
        excluded_rows = []
        for index, row in enumerate(rows, start=1):
            key, reason = _identity(row)
            if reason is None and key in self.inventory_set:
                reason = "DUPLICATE_IDENTITY"
            if reason is not None:
                coverage.excluded[reason] += 1
                excluded_rows.append(index)
                continue
            self.inventory.append(key)
            self.inventory_set.add(key)
            self.expirations[key.split("|")[1]] += 1
        if receipt.refusal is not None:
            coverage.excluded[receipt.refusal] += len(self.inventory)
            return
        available = receipt.available_at
        assert available is not None
        coverage.observe(available)
        self._emit(
            "contract_list",
            "SPXW",
            available,
            available,
            {"contracts": list(self.inventory)},
            receipt,
            None,
            BASIS_RECEIPT,
            None,
        )
        coverage.emitted = len(self.inventory)

    def _groups(
        self,
        endpoint: str,
        rows: list[dict[str, str]],
        coverage: _EndpointCoverage,
        key_of: Any,
    ) -> list[tuple[str, int, dict[str, str]]]:
        """One surviving row per identity, decided before anything is emitted.

        Rows whose identity cannot be read (or is not listed) are excluded one
        by one. The rest are grouped by identity: a group of one survives; a
        group whose rows agree on every column this rule reads is coalesced to
        its lowest row index and the rest are counted; a group whose rows
        disagree is excluded whole. Nothing here depends on row order.
        """
        groups: dict[str, list[tuple[int, dict[str, str]]]] = {}
        for index, row in enumerate(rows, start=1):
            key, reason = key_of(row)
            if reason is not None:
                coverage.excluded[reason] += 1
                continue
            groups.setdefault(key, []).append((index, row))
        columns = REQUIRED_COLUMNS[endpoint]
        survivors = []
        for key, members in groups.items():
            if len(members) > 1:
                observations = {
                    tuple(row[column].strip() for column in columns)
                    for _, row in members
                }
                if len(observations) > 1:
                    coverage.excluded[CONFLICTING] += len(members)
                    coverage.conflicting_groups += 1
                    coverage.conflicting_keys.append(key)
                    continue
                coverage.excluded[COALESCED] += len(members) - 1
                coverage.identical_groups += 1
            index, row = members[0]
            survivors.append((key, index, row))
        return survivors

    def _listed_identity(self, row: dict[str, str]) -> tuple[str, str | None]:
        key, reason = _identity(row)
        if reason is None and self.membership_checked and key not in self.inventory_set:
            return key, "NOT_IN_INVENTORY"
        return key, reason

    def _skipped(self, endpoint: str) -> bool:
        """Record an out-of-scope endpoint as not scheduled; emit nothing."""
        if endpoint in self.scope:
            return False
        self.coverage[endpoint] = _EndpointCoverage(kind=KINDS[endpoint])
        self.unused[endpoint] = ()
        return True

    def _timed(
        self, row: dict[str, str], receipt: Receipt, coverage: _EndpointCoverage
    ) -> tuple[datetime, datetime, str] | None:
        event_at, reason = _event_time(row["timestamp"])
        if event_at is None:
            coverage.excluded[str(reason)] += 1
            return None
        coverage.observe(event_at)
        available_at, basis = self._availability(receipt, event_at, coverage)
        if available_at is None or basis is None:
            coverage.excluded["VENDOR_TIMESTAMP_AFTER_RECEIPT"] += 1
            return None
        return event_at, available_at, basis

    def option_quote(self) -> None:
        if self._skipped(OPTION_QUOTE):
            return
        receipt = self.receipts[OPTION_QUOTE]
        rows, coverage = self._load(OPTION_QUOTE)
        if receipt.refusal is not None:
            coverage.excluded[receipt.refusal] += len(rows)
            return
        for key, index, row in self._groups(
            OPTION_QUOTE, rows, coverage, self._listed_identity
        ):
            bid, reason = _price(row["bid"], zero=True)
            ask, ask_reason = _price(row["ask"], zero=False)
            if reason is not None or ask_reason is not None:
                coverage.excluded[str(reason or ask_reason)] += 1
                continue
            timed = self._timed(row, receipt, coverage)
            if timed is None:
                continue
            event_at, available_at, basis = timed
            self.identities.setdefault("quoted", set()).add(key)
            self._emit(
                "option_quote",
                key,
                event_at,
                available_at,
                {"bid": bid, "ask": ask},
                receipt,
                index,
                basis,
                coverage,
            )

    def greeks(self, model_id: str | None, rate: float, dividend: float) -> None:
        if self._skipped(OPTION_GREEKS) or model_id is None:
            return
        receipt = self.receipts[OPTION_GREEKS]
        rows, coverage = self._load(OPTION_GREEKS)
        if receipt.refusal is not None:
            coverage.excluded[receipt.refusal] += len(rows)
            return
        for key, index, row in self._groups(
            OPTION_GREEKS, rows, coverage, self._listed_identity
        ):
            iv, delta, error = (
                _finite(row["implied_vol"]),
                _finite(row["delta"]),
                _finite(row["iv_error"]),
            )
            if iv is None or delta is None or error is None:
                coverage.excluded["NON_FINITE_INPUT"] += 1
                continue
            if not 0.0001 <= iv <= 5:
                coverage.excluded["IV_OUT_OF_RANGE"] += 1
                continue
            if abs(error) > VENDOR_IV_ERROR_LIMIT:
                coverage.excluded["VENDOR_IV_ERROR"] += 1
                continue
            if not -1 <= delta <= 1:
                coverage.excluded["DELTA_OUT_OF_RANGE"] += 1
                continue
            timed = self._timed(row, receipt, coverage)
            if timed is None:
                continue
            event_at, available_at, basis = timed
            self.identities.setdefault("greeked", set()).add(key)
            self._emit(
                "greeks",
                key,
                event_at,
                available_at,
                {
                    "implied_vol": iv,
                    "delta": delta,
                    "rate": rate,
                    "dividend_yield": dividend,
                    "model_id": model_id,
                },
                receipt,
                index,
                basis,
                coverage,
            )

    def open_interest(self, rule: SettlementRule) -> None:
        if self._skipped(OPTION_OPEN_INTEREST):
            return
        receipt = self.receipts[OPTION_OPEN_INTEREST]
        rows, coverage = self._load(OPTION_OPEN_INTEREST)
        if receipt.refusal is not None:
            coverage.excluded[receipt.refusal] += len(rows)
            return
        for key, index, row in self._groups(
            OPTION_OPEN_INTEREST, rows, coverage, self._listed_identity
        ):
            quantity_text = row["open_interest"].strip()
            if not quantity_text.isdigit() or int(quantity_text) > MAX_INTEGER:
                coverage.excluded["INVALID_OPEN_INTEREST"] += 1
                continue
            timed = self._timed(row, receipt, coverage)
            if timed is None:
                continue
            event_at, available_at, basis = timed
            try:
                as_of = rule.resolve(event_at.astimezone(EASTERN).date())
            except SettlementRuleError:
                coverage.excluded["OI_TIMESTAMP_NOT_TRADING_SESSION"] += 1
                continue
            self.oi_as_of[as_of.isoformat()] += 1
            self.identities.setdefault("with_open_interest", set()).add(key)
            self._emit(
                "open_interest",
                key,
                event_at,
                available_at,
                {"quantity": int(quantity_text), "as_of": as_of.isoformat()},
                receipt,
                index,
                basis,
                coverage,
            )

    def spx_price(self) -> None:
        if self._skipped(INDEX_PRICE):
            return
        receipt = self.receipts[INDEX_PRICE]
        rows, coverage = self._load(INDEX_PRICE)
        if receipt.refusal is not None:
            coverage.excluded[receipt.refusal] += len(rows)
            return

        def index_symbol(row: dict[str, str]) -> tuple[str, str | None]:
            symbol = row["symbol"].strip()
            return symbol, None if symbol == "SPX" else "UNEXPECTED_SYMBOL"

        for _key, index, row in self._groups(INDEX_PRICE, rows, coverage, index_symbol):
            price, _reason = _price(row["price"], zero=False)
            if price is None:
                coverage.excluded["INVALID_PRICE"] += 1
                continue
            timed = self._timed(row, receipt, coverage)
            if timed is None:
                continue
            event_at, available_at, basis = timed
            self._emit(
                "spx_price",
                "SPX",
                event_at,
                available_at,
                {"price": price},
                receipt,
                index,
                basis,
                coverage,
            )

    def model_evidence(self, model_id: str | None) -> dict[str, Any]:
        receipt = self.receipts[OPTION_GREEKS]
        if OPTION_GREEKS not in self.scope or model_id is None:
            return {
                "rule": RULES["model_evidence"],
                "model_fixed_at": None,
                "emitted": 0,
                "excluded": {receipt.refusal or NOT_ACQUIRED: 1},
            }
        fixed_at = _aware(self.capture.captured_at)
        outcome: dict[str, Any] = {
            "rule": RULES["model_evidence"],
            "model_fixed_at": _iso(fixed_at) if fixed_at else None,
            "emitted": 0,
            "excluded": {},
        }
        if receipt.refusal is not None or receipt.available_at is None:
            outcome["excluded"] = {receipt.refusal or "AVAILABILITY_UNKNOWN": 1}
            return outcome
        if fixed_at is None or fixed_at > receipt.available_at:
            outcome["excluded"] = {"MODEL_FIXED_AFTER_GREEKS_RECEIPT": 1}
            return outcome
        self._emit(
            "model_evidence",
            "MODEL",
            fixed_at,
            receipt.available_at,
            {
                "model_ids": [model_id],
                "iv_source": IVSource.VENDOR_DEFAULT_IV.value,
                "iv_price_basis": IV_PRICE_BASIS,
            },
            receipt,
            None,
            BASIS_GREEKS_RECEIPT,
            None,
        )
        outcome["emitted"] = 1
        return outcome


def _scope(
    capture: LoadedCapture, expected: frozenset[str] | None, allow_unacquired: bool
) -> tuple[frozenset[str], frozenset[str]]:
    """``(scheduled, acquired)``: what the cycle was to issue, what it holds.

    ``expected`` must equal the scope the capture's run intent declares. A
    capture that declares no scope is a whole-plan capture and can only be read
    as one. Payloads outside the schedule refuse the capture. A scheduled
    endpoint without a verified payload refuses it too, unless the caller
    accepts partial acquisition -- the session assembler does, and reports it.
    """
    kinds = frozenset(KINDS)
    declared = (
        frozenset(capture.scheduled_endpoints)
        if capture.scheduled_endpoints is not None
        else None
    )
    if expected is None:
        if declared is not None and declared != kinds:
            raise NormalizationError(
                f"the capture declares a partial scope {sorted(declared)}; pass "
                "expected_endpoints to normalize it as the cycle it is"
            )
        scheduled = kinds
    else:
        scheduled = frozenset(expected)
        if not scheduled or not scheduled <= kinds:
            raise NormalizationError(
                f"expected endpoints {sorted(scheduled)} must be a nonempty "
                f"subset of {sorted(kinds)}"
            )
        if declared is None and scheduled != kinds:
            raise NormalizationError(
                "the capture declares no partial scope, so it cannot be read as one"
            )
        if declared is not None and declared != scheduled:
            raise NormalizationError(
                f"the capture declares scope {sorted(declared)}, not the expected "
                f"{sorted(scheduled)}"
            )
    present = frozenset(capture.record_hashes) & kinds
    unexpected = sorted(present - scheduled)
    if unexpected:
        raise NormalizationError(
            f"capture holds responses outside its declared scope: {unexpected}"
        )
    missing = sorted(scheduled - present)
    if missing and not allow_unacquired:
        raise NormalizationError(f"capture lacks responses for {missing}")
    acquired = scheduled & present
    if not acquired:
        raise NormalizationError("no scheduled endpoint produced a verified payload")
    return scheduled, acquired


def normalize_capture(
    root: pathlib.Path | str,
    *,
    receipt_clock_tolerance_ms: int = 0,
    expected_endpoints: frozenset[str] | None = None,
    allow_unacquired: bool = False,
) -> NormalizedCapture:
    """Normalize one verified capture directory. Deterministic; refuses, never guesses.

    ``expected_endpoints`` (v2.1.36) is the scope a partial-scope cycle was
    scheduled to issue. It must equal the scope the capture's own run intent
    declares; endpoints outside it are reported ``NOT_SCHEDULED`` and yield
    nothing, and a response outside it refuses the capture. ``None`` keeps the
    single-capture rule: every endpoint, and a capture that declares a partial
    scope is refused rather than read as complete.

    ``allow_unacquired`` (v2.1.36) accepts a cycle in which a scheduled request
    produced no verified payload: that endpoint is reported ``NOT_ACQUIRED``
    with what the attempt log recorded, and the payloads that were acquired are
    normalized. The default refuses, as every earlier release did. A cycle
    without Greeks then carries no model evidence, because the model is read
    from the verified Greeks request and nothing else.
    """
    if (
        type(receipt_clock_tolerance_ms) is not int
        or not 0 <= receipt_clock_tolerance_ms <= MAX_RECEIPT_CLOCK_TOLERANCE_MS
    ):
        raise NormalizationError(
            f"receipt clock tolerance must be an integer in "
            f"[0, {MAX_RECEIPT_CLOCK_TOLERANCE_MS}] milliseconds"
        )
    root = pathlib.Path(root).resolve()
    capture = load_capture(root, require_greeks_request=not allow_unacquired)
    scheduled, scope = _scope(capture, expected_endpoints, allow_unacquired)
    session = _session_date(capture)
    receipts, attempt_log = _receipts(root, capture)
    entries = attempt_log.pop("entries")
    for endpoint in KINDS:
        if endpoint not in scheduled:
            receipts[endpoint] = _absent(endpoint, NOT_SCHEDULED)
        elif endpoint not in scope:
            receipts[endpoint] = _absent(
                endpoint, NOT_ACQUIRED, _attempt_detail(endpoint, entries)
            )
    rule = _settlement_rule(capture)
    model = _model(capture)
    if model is None and OPTION_GREEKS in scope:
        raise NormalizationError(
            "the Greeks payload is present but its request is not verified"
        )
    builder = _Builder(
        root,
        capture,
        receipts,
        timedelta(milliseconds=receipt_clock_tolerance_ms),
        scope,
    )
    builder.contract_list()
    builder.option_quote()
    if model is None:
        builder.greeks(None, 0.0, 0.0)
    else:
        builder.greeks(model[0], model[1], model[2])
    builder.open_interest(rule)
    builder.spx_price()
    model_outcome = builder.model_evidence(model[0] if model else None)
    builder.records.sort(
        key=lambda r: (r["kind"], r["key"], r["event_at"], r["sequence"])
    )
    listed = set(builder.inventory)
    with_oi = builder.identities.get("with_open_interest", set())
    provenance = {
        "capture_session_id": capture.session_id,
        "manifest_sha256": capture.manifest_hash,
        "run_intent_sha256": capture.run_intent_sha256,
        "payloads": {
            endpoint: {
                "sha256": receipts[endpoint].payload_sha256,
                "location": receipts[endpoint].location,
            }
            for endpoint in sorted(scope)
        },
        "normalizer": NORMALIZER,
        "session_date": session.isoformat(),
    }
    origin = RECORDED if attempt_log.pop("live") else SYNTHETIC
    capture_origins = attempt_log.pop("origins")
    coverage = {
        "normalizer": NORMALIZER,
        "origin": origin,
        "capture_origins": capture_origins,
        "capture": {
            "session_id": capture.session_id,
            "parser_version": capture.parser_version,
            "intent_schema_version": capture.intent_schema_version,
            "manifest_sha256": capture.manifest_hash,
            "run_intent_sha256": capture.run_intent_sha256,
            "verified_records": capture.verified_records,
            "verified_endpoints": list(capture.request.verified_endpoints),
            "captured_at": capture.captured_at,
            "documentation_sha256": (
                capture.documentation.document_sha256 if capture.documentation else None
            ),
        },
        "session_date": session.isoformat(),
        "scheduled_endpoints": sorted(scheduled),
        "acquired_endpoints": sorted(scope),
        "unacquired_endpoints": sorted(scheduled - scope),
        "inventory_membership_checked": builder.membership_checked,
        "receipt_clock_tolerance_ms": receipt_clock_tolerance_ms,
        "vendor_timestamp_policy": {
            "assumed_timezone": VENDOR_TIMEZONE,
            "ambiguous_wall_clock": "EARLIER",
            "nonexistent_wall_clock": "EXCLUDED",
            "vendor_timestamp_after_receipt": (
                "EXCLUDED"
                if receipt_clock_tolerance_ms == 0
                else f"AVAILABILITY_DEFERRED_WITHIN_{receipt_clock_tolerance_ms}_MS"
            ),
        },
        "attempt_log": attempt_log,
        "endpoints": {
            endpoint: builder.coverage[endpoint].as_dict(
                receipts[endpoint], builder.unused[endpoint]
            )
            for endpoint in sorted(KINDS)
        },
        "model_evidence": {
            **model_outcome,
            "model_id": model[0] if model else None,
            "rate": model[1] if model else None,
            "dividend_yield": model[2] if model else None,
            "request_parameters": model[3] if model else {},
            "iv_source": IVSource.VENDOR_DEFAULT_IV.value,
            "iv_price_basis": IV_PRICE_BASIS,
        },
        "open_interest": {
            "settlement_rule": rule.as_dict(),
            "as_of_basis": "settlement rule applied to the vendor row timestamp's Eastern date",
            "rows_by_as_of": dict(sorted(builder.oi_as_of.items())),
            "identities_with_open_interest": len(with_oi & listed),
            "identities_without_open_interest": len(listed - with_oi),
        },
        "identities": {
            "listed": len(listed),
            "quoted": len(builder.identities.get("quoted", set()) & listed),
            "greeked": len(builder.identities.get("greeked", set()) & listed),
            "with_open_interest": len(with_oi & listed),
            "without_open_interest": len(listed - with_oi),
            "listed_by_expiration": dict(sorted(builder.expirations.items())),
        },
        "records": len(builder.records),
        "records_by_kind": dict(
            sorted(Counter(r["kind"] for r in builder.records).items())
        ),
    }
    return NormalizedCapture(session, origin, provenance, builder.records, coverage)
