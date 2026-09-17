"""Hash-bound normalized events; byte integrity does not prove vendor origin."""

from __future__ import annotations

import hashlib
import json
import math
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

EVENT_SCHEMA = "research-events/2.1.34"
#: v2.1.35: every record additionally names the raw payload digest, the native
#: row and the versioned rule it was normalized under, and the document names
#: the verified capture the payloads came from. The replay semantics of a
#: record are unchanged; only its traceability is.
LINEAGE_EVENT_SCHEMA = "research-events/2.1.35"
#: v2.1.36: a session assembled from several collection cycles. Each record
#: additionally names the cycle and the logical request that produced its row,
#: and the document names every cycle's verified capture. Replay semantics are
#: again unchanged: the same availability-ordered, revision-aware selection.
SESSION_EVENT_SCHEMA = "research-events/2.1.36"
EVENT_SCHEMAS = frozenset({EVENT_SCHEMA, LINEAGE_EVENT_SCHEMA, SESSION_EVENT_SCHEMA})
ORIGINS = {"SYNTHETIC", "RECORDED_NORMALIZED"}
LINEAGE_FIELDS = frozenset({"raw_sha256", "row_index", "rule", "availability_basis"})
SESSION_LINEAGE_FIELDS = LINEAGE_FIELDS | {"cycle", "request_id"}
PROVENANCE_FIELDS = frozenset(
    {
        "capture_session_id",
        "manifest_sha256",
        "run_intent_sha256",
        "payloads",
        "normalizer",
        "session_date",
    }
)
CYCLE_PROVENANCE_FIELDS = frozenset(
    {"capture_session_id", "manifest_sha256", "run_intent_sha256", "payloads"}
)
SESSION_PROVENANCE_FIELDS = frozenset(
    {
        "session_date",
        "session_approval_hash",
        "schedule_fingerprint",
        "session_intent_sha256",
        "session_log_sha256",
        "collector",
        "normalizer",
        "assembler",
        "cycles",
    }
)
_CYCLE_LABEL = re.compile(r"[0-9]{6}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
#: Research bound on every integer quantity (sizes, OI, sequences, slippage
#: ticks, probe quantities). Keeps exact decimal cost arithmetic inside the
#: fill-probe precision budget; it is not a market limit.
MAX_INTEGER = 10**9
MAX_SOURCE_BYTES = 50 * 1024 * 1024


def fields(value: Any, expected: set[str] | frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(
            f"unexpected fields in normalized research input; expected exactly "
            f"{sorted(expected)}"
        )
    return value


def text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("nonempty string required")
    return value


def stamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an offset-aware string")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp requires UTC offset")
    return result.astimezone(UTC)


def integer(value: Any, *, minimum: int = 0, maximum: int = MAX_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid integer quantity")
    return value


def decimal_value(value: Any, *, zero: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("prices and costs must be decimal strings")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("invalid decimal") from error
    if not result.is_finite() or result < 0 or (not zero and result == 0):
        raise ValueError("invalid nonnegative decimal")
    exponent = result.as_tuple().exponent
    if result > Decimal("1e12") or not isinstance(exponent, int) or exponent < -12:
        raise ValueError("decimal outside research precision bounds")
    return result


def option_parts(key: Any) -> tuple[str, date, str, str]:
    parts = text(key).split("|")
    if len(parts) != 4:
        raise ValueError("noncanonical option identity")
    root, expiry, strike, right = parts
    normalized = format(decimal_value(strike).normalize(), "f")
    day = date.fromisoformat(expiry)
    if (
        root != "SPXW"
        or right not in {"CALL", "PUT"}
        or key != f"{root}|{day}|{normalized}|{right}"
    ):
        raise ValueError("noncanonical option identity")
    return root, day, strike, right


def instrument_expiry(key: Any) -> date:
    if not re.fullmatch(r"[A-Z][A-Z0-9]{0,11}\|\d{4}-\d{2}-\d{2}", text(key)):
        raise ValueError("specific futures expiry identity required")
    return date.fromisoformat(key.split("|")[1])


def relative_path(value: Any) -> str:
    """A bounded relative POSIX path: no drive, no absolute root, no ``..``."""
    name = value
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or PurePosixPath(name).is_absolute()
        or ".." in PurePosixPath(name).parts
    ):
        raise ValueError("source path must stay inside bundle")
    return name


def hex_digest(value: Any) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError("SHA-256 hex digest required")
    return value


def _validate_provenance(value: Any) -> dict[str, Any]:
    fields(value, PROVENANCE_FIELDS)
    text(value["capture_session_id"])
    text(value["normalizer"])
    hex_digest(value["manifest_sha256"])
    hex_digest(value["run_intent_sha256"])
    date.fromisoformat(text(value["session_date"]))
    _validate_payloads(value["payloads"])
    return dict(value)


def _validate_payloads(payloads: Any) -> set[str]:
    if not isinstance(payloads, dict) or not payloads:
        raise ValueError("provenance must name the verified raw payloads")
    digests = []
    for endpoint, payload in payloads.items():
        text(endpoint)
        fields(payload, {"sha256", "location"})
        digests.append(hex_digest(payload["sha256"]))
        relative_path(payload["location"])
    if len(set(digests)) != len(digests):
        raise ValueError("duplicate raw payload digest in provenance")
    return set(digests)


def _validate_session_provenance(value: Any) -> dict[str, Any]:
    """A session document names every cycle's verified capture; each record
    then names its cycle, and its raw digest must belong to that cycle."""
    fields(value, SESSION_PROVENANCE_FIELDS)
    date.fromisoformat(text(value["session_date"]))
    hex_digest(value["session_approval_hash"])
    hex_digest(value["schedule_fingerprint"])
    hex_digest(value["session_intent_sha256"])
    hex_digest(value["session_log_sha256"])
    for name in ("collector", "normalizer", "assembler"):
        text(value[name])
    cycles = value["cycles"]
    if not isinstance(cycles, dict) or not cycles:
        raise ValueError("session provenance must name at least one cycle")
    seen_manifests = set()
    for label, cycle in cycles.items():
        if not isinstance(label, str) or not _CYCLE_LABEL.fullmatch(label):
            raise ValueError("cycle labels are HHMMSS strings")
        fields(cycle, CYCLE_PROVENANCE_FIELDS)
        text(cycle["capture_session_id"])
        manifest = hex_digest(cycle["manifest_sha256"])
        hex_digest(cycle["run_intent_sha256"])
        if manifest in seen_manifests:
            raise ValueError("two cycles name the same manifest")
        seen_manifests.add(manifest)
        _validate_payloads(cycle["payloads"])
    return dict(value)


def payload_digests(provenance: dict[str, Any]) -> set[str]:
    return {payload["sha256"] for payload in provenance["payloads"].values()}


def cycle_payload_digests(provenance: dict[str, Any]) -> dict[str, set[str]]:
    return {
        label: payload_digests(cycle) for label, cycle in provenance["cycles"].items()
    }


def _validate_lineage(value: Any, payloads: set[str]) -> dict[str, Any]:
    fields(value, LINEAGE_FIELDS)
    if hex_digest(value["raw_sha256"]) not in payloads:
        raise ValueError("lineage names a raw payload outside the provenance")
    if value["row_index"] is not None:
        integer(value["row_index"], minimum=1)
    text(value["rule"])
    text(value["availability_basis"])
    return dict(value)


def _validate_session_lineage(
    value: Any, cycles: dict[str, set[str]]
) -> dict[str, Any]:
    fields(value, SESSION_LINEAGE_FIELDS)
    cycle = text(value["cycle"])
    if cycle not in cycles:
        raise ValueError("lineage names a cycle outside the provenance")
    if hex_digest(value["raw_sha256"]) not in cycles[cycle]:
        raise ValueError("lineage names a raw payload outside its cycle")
    if value["row_index"] is not None:
        integer(value["row_index"], minimum=1)
    text(value["rule"])
    text(value["availability_basis"])
    text(value["request_id"])
    return dict(value)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON constant: {value}")


def read_json(raw: bytes) -> Any:
    """Strict UTF-8 JSON: duplicate keys, NaN/Infinity and BOMs are refused."""
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_pairs,
        parse_constant=_invalid_constant,
    )


def _validate_data(kind: str, key: str, data: Any) -> dict[str, Any]:
    if kind == "contract_list":
        fields(data, {"contracts"})
        if key != "SPXW" or not isinstance(data["contracts"], list):
            raise ValueError("invalid inventory")
        for identity in data["contracts"]:
            option_parts(identity)
        if len(set(data["contracts"])) != len(data["contracts"]):
            raise ValueError("duplicate inventory identity")
    elif kind in {"option_quote", "greeks", "open_interest"}:
        option_parts(key)
        if kind == "option_quote":
            fields(data, {"bid", "ask"})
            decimal_value(data["bid"], zero=True)
            decimal_value(data["ask"])
        elif kind == "open_interest":
            fields(data, {"quantity", "as_of"})
            if data["quantity"] is not None:
                integer(data["quantity"])
            date.fromisoformat(text(data["as_of"]))
        else:
            fields(data, {"implied_vol", "delta", "rate", "dividend_yield", "model_id"})
            if any(
                type(data[k]) not in (float, int) or not math.isfinite(data[k])
                for k in ("implied_vol", "delta", "rate", "dividend_yield")
            ):
                raise ValueError("invalid Greek inputs")
            if (
                not 0.0001 <= data["implied_vol"] <= 5
                or not -1 <= data["delta"] <= 1
                or not isinstance(data["model_id"], str)
                or not data["model_id"]
            ):
                raise ValueError("invalid Greek range or model")
    elif kind == "spx_price":
        fields(data, {"price"})
        if key != "SPX":
            raise ValueError("unexpected index")
        decimal_value(data["price"])
    elif kind == "model_evidence":
        fields(data, {"model_ids", "iv_source", "iv_price_basis"})
        ids = data["model_ids"]
        if (
            key != "MODEL"
            or not isinstance(ids, list)
            or not ids
            or any(not isinstance(x, str) or not x for x in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError("invalid model evidence")
        if any(
            not isinstance(data[k], str) or not data[k]
            for k in ("iv_source", "iv_price_basis")
        ):
            raise ValueError("IV source and price basis required")
    elif kind in {"futures_quote", "instrument", "costs"}:
        instrument_expiry(key)
        if kind == "futures_quote":
            fields(data, {"bid", "ask", "bid_size", "ask_size"})
            for k in ("bid", "ask"):
                decimal_value(data[k])
            for k in ("bid_size", "ask_size"):
                integer(data[k])
        else:
            expected = {"valid_from", "valid_to"} | (
                {"tick_size", "point_value", "currency"}
                if kind == "instrument"
                else {"fee_per_contract_side", "extra_slippage_ticks"}
            )
            fields(data, expected)
            if stamp(data["valid_from"]) >= stamp(data["valid_to"]):
                raise ValueError("empty effective interval")
            if kind == "instrument":
                decimal_value(data["tick_size"])
                decimal_value(data["point_value"])
                if data["currency"] != "USD":
                    raise ValueError("unsupported research currency")
            else:
                decimal_value(data["fee_per_contract_side"], zero=True)
                integer(data["extra_slippage_ticks"])
    else:
        raise ValueError("unsupported normalized event kind")
    return dict(data)


@dataclass(frozen=True)
class Event:
    kind: str
    key: str
    event_at: datetime
    available_at: datetime
    sequence: int
    data: dict[str, Any]
    source_sha256: str
    record_index: int
    #: Raw-payload binding carried by ``research-events/2.1.35`` records; absent
    #: for the 2.1.34 schema. Not part of :meth:`reference`, whose shape is
    #: frozen by the shipped v2.1.34 report: the reference locates the record
    #: in its hash-bound source file, and the record there carries the lineage.
    lineage: dict[str, Any] | None = None

    def reference(self) -> dict[str, Any]:
        return {"source_sha256": self.source_sha256, "record_index": self.record_index}


def load_events(root: Path, sources: Any) -> tuple[EventStore, list[dict[str, Any]]]:
    if not isinstance(sources, list) or not sources:
        raise ValueError("nonempty source descriptor list required")
    root = root.resolve()
    events = []
    receipts = []
    seen_records = set()
    seen_hashes = set()
    for source in sources:
        fields(source, {"path", "sha256"})
        name, expected = relative_path(source["path"]), source["sha256"]
        path = (root / name).resolve()
        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or path.stat().st_size > MAX_SOURCE_BYTES
        ):
            raise ValueError("invalid source path or size")
        raw = path.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            raise ValueError("source SHA-256 mismatch")
        if actual in seen_hashes:
            raise ValueError("duplicate source bytes")
        seen_hashes.add(actual)
        doc = read_json(raw)
        schema = doc.get("schema_version") if isinstance(doc, dict) else None
        if schema not in EVENT_SCHEMAS:
            raise ValueError("unsupported source schema or origin")
        session_bound = schema == SESSION_EVENT_SCHEMA
        lineage_bound = schema == LINEAGE_EVENT_SCHEMA or session_bound
        expected_fields = {"schema_version", "origin", "records"}
        if lineage_bound:
            expected_fields.add("provenance")
        fields(doc, expected_fields)
        if doc["origin"] not in ORIGINS or not isinstance(doc["records"], list):
            raise ValueError("unsupported source schema or origin")
        provenance: dict[str, Any] | None = None
        payloads: set[str] = set()
        cycles: dict[str, set[str]] = {}
        if session_bound:
            provenance = _validate_session_provenance(doc["provenance"])
            cycles = cycle_payload_digests(provenance)
        elif lineage_bound:
            provenance = _validate_provenance(doc["provenance"])
            payloads = payload_digests(provenance)
        record_fields = {"kind", "key", "event_at", "available_at", "sequence", "data"}
        if lineage_bound:
            record_fields.add("lineage")
        for index, item in enumerate(doc["records"]):
            fields(item, record_fields)
            kind, key = text(item["kind"]), text(item["key"])
            event_at, available_at = (
                stamp(item["event_at"]),
                stamp(item["available_at"]),
            )
            sequence = integer(item["sequence"])
            if event_at > available_at:
                raise ValueError("event occurs after declared availability")
            data = _validate_data(kind, key, item["data"])
            lineage = None
            if session_bound:
                lineage = _validate_session_lineage(item["lineage"], cycles)
            elif lineage_bound:
                lineage = _validate_lineage(item["lineage"], payloads)
            identity = (kind, key, event_at, sequence)
            if identity in seen_records:
                raise ValueError("duplicate event revision identity")
            seen_records.add(identity)
            events.append(
                Event(
                    kind,
                    key,
                    event_at,
                    available_at,
                    sequence,
                    data,
                    actual,
                    index,
                    lineage,
                )
            )
        receipt = {
            "source_sha256": actual,
            "bytes": len(raw),
            "origin": doc["origin"],
            "records": len(doc["records"]),
        }
        if provenance is not None:
            receipt["schema_version"] = schema
            receipt["provenance"] = provenance
        receipts.append(receipt)
    events.sort(key=lambda e: (e.available_at, e.event_at, e.sequence, e.kind, e.key))
    receipts.sort(key=lambda r: r["source_sha256"])
    return EventStore(tuple(events)), receipts


class EventStore:
    """Availability-indexed state, with monotone event/revision selection.

    The state of ``(kind, key)`` at instant ``t`` is the record with the greatest
    ``(event_at, sequence)`` among the records whose ``available_at <= t``. A
    record that is delivered late but describes an older market event therefore
    never replaces a newer known state, and a revision (same ``event_at``, higher
    ``sequence``) takes effect only once it is available.
    """

    def __init__(self, events: tuple[Event, ...]) -> None:
        histories: dict[tuple[str, str], list[Event]] = {}
        for event in sorted(
            events, key=lambda e: (e.available_at, e.event_at, e.sequence)
        ):
            histories.setdefault((event.kind, event.key), []).append(event)
        self._times: dict[tuple[str, str], tuple[datetime, ...]] = {}
        self._history: dict[tuple[str, str], tuple[Event, ...]] = {}
        self._states: dict[tuple[str, str], tuple[Event, ...]] = {}
        for key, history in histories.items():
            winner = history[0]
            states = []
            for event in history:
                if (event.event_at, event.sequence) > (
                    winner.event_at,
                    winner.sequence,
                ):
                    winner = event
                states.append(winner)
            self._times[key] = tuple(e.available_at for e in history)
            self._history[key] = tuple(history)
            self._states[key] = tuple(states)

    def at(self, kind: str, key: str, at: datetime) -> Event | None:
        """Newest known state whose availability is at or before ``at``."""
        identity = (kind, key)
        times = self._times.get(identity, ())
        index = bisect_right(times, at) - 1
        return self._states[identity][index] if index >= 0 else None

    def available(self, kind: str, key: str, at: datetime) -> tuple[Event, ...]:
        """Every record available at or before ``at``, in availability order.

        For effective-dated declarations (instrument metadata, cost profiles)
        the newest record is not necessarily the one in force: a preannounced
        future profile must not hide an older profile that still applies.
        """
        identity = (kind, key)
        times = self._times.get(identity, ())
        return self._history.get(identity, ())[: bisect_right(times, at)]

    def next_update(
        self, kind: str, key: str, start: datetime, end: datetime
    ) -> datetime | None:
        """First availability instant in ``[start, end]``, whatever it carries."""
        times = self._times.get((kind, key), ())
        index = bisect_left(times, start)
        return times[index] if index < len(times) and times[index] <= end else None
