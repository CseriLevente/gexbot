"""Immutable raw-response store.

The audit trail is the *raw payload*, not the parsed object. A parser bug found
three months later can only be diagnosed against what the vendor actually sent,
and a parser fix can only be validated by re-running it over the original bytes.

Two properties are enforced:

* **Append-only.** A record with an existing id is never overwritten. Silently
  replacing a stored response would destroy the only copy of the evidence.
* **Content-addressed.** Every record carries a SHA-256 of the payload, so
  tampering or truncation is detectable and the replay test can assert that the
  same bytes went in.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from src.adapters.errors import ThetaDataRawStoreError

# Bumped when the parser's interpretation of a payload changes, so a stored
# record says which code read it.
#: The single definition. Bump this whenever parsing behaviour changes in a way
#: that could alter a parsed value -- duplicate resolution, integer parsing,
#: timestamp localisation, float classification. It participates in the replay
#: hash, so a parser change that quietly alters numbers cannot masquerade as the
#: same output.
#:
#: 2.1.1 changed: exact Decimal integer parsing (was float round-trip),
#: structured float parse issues, per-source timestamp localisation, and
#: assembly from deduplicated rows.
#:
#: 2.1.3 changed: structured parsing on every vendor float, CSV body
#: validation before concluding "no rows", Decimal strike identity, and
#: per-contract selected-source timestamp provenance. v2.1.2 made those
#: changes and left the version at 2.1.1, so a replay could not tell that
#: the parser had changed underneath it.
#:
#: 2.1.4 changed: the canonical contract identity is now spelled by
#: ``canonical_strike`` on both sides -- ``SPXW:2026-03-17:5000:C`` rather than
#: ``SPXW:2026-03-17:5000.0000:C``. The contract is the same contract and the
#: strike is the same number, so no value the parser reads has changed meaning;
#: but the identity string is the *join key* between an expected universe and a
#: received chain, and two versions spelling it differently would not match each
#: other's output. A replay across the boundary has to be able to see that.
#: 2.1.6 changed: one shared vendor-timestamp interpretation. The validator
#: previously read a naive vendor string as UTC while this parser localised it
#: to US Eastern, so the same bytes produced instants four hours apart depending
#: on which module read them. A replay across that boundary has to be able to
#: see that the reading changed.
PARSER_VERSION = "thetadata-v3-parser/2.1.6"

#: The manifest's own schema. Bumped when the *shape* of the evidence changes,
#: independently of how a payload is read: v2.1.6 replaced parallel arrays of
#: ids, hashes and request ids with per-record descriptors, so an older manifest
#: cannot be verified by this code and is refused rather than reinterpreted.
MANIFEST_SCHEMA_VERSION = "raw-capture-manifest/2.1.6"


#: Aliased onto the adapter hierarchy so that a caller catching
#: ThetaDataError catches store failures too. Defined in adapters.errors rather
#: than here because the store is used by more than one adapter, so it must not
#: depend on the ThetaData client.
RawStoreError = ThetaDataRawStoreError


class CaptureOrigin(str, Enum):
    """Where a stored response actually came from.

    v2.1.5 carried ``AdapterValidationReport.live_capture = False`` as a
    hard-coded constant. It was the right answer, and it was not an *answer*: it
    would have stayed False through the first real session, and nothing but the
    constant stood between an offline fixture and a certification claim about
    live vendor behaviour.

    The origin is stamped on each record by the transport that produced it, and
    it enters the manifest hash, so a fixture capture cannot be relabelled after
    the fact without the manifest saying so.
    """

    #: A deterministic in-process transport. Never evidence about the vendor.
    OFFLINE_FIXTURE = "OFFLINE_FIXTURE"
    #: A real HTTP round trip to the vendor.
    LIVE_HTTP_CAPTURE = "LIVE_HTTP_CAPTURE"
    #: A real round trip to a local Theta Terminal, which proxies the vendor.
    LOCAL_TERMINAL_CAPTURE = "LOCAL_TERMINAL_CAPTURE"
    #: A transport that does not say. Treated as not-live.
    UNKNOWN_ORIGIN = "UNKNOWN_ORIGIN"

    @property
    def is_live(self) -> bool:
        return self in (
            CaptureOrigin.LIVE_HTTP_CAPTURE,
            CaptureOrigin.LOCAL_TERMINAL_CAPTURE,
        )


class StoreDurability(str, Enum):
    """Whether a store survives the process that wrote to it.

    A paid session's only copy of the evidence cannot live in a dictionary.
    v2.1.5's readiness check probed for protocol compliance, integrity and a
    successful write -- all of which ``InMemoryRawStore`` passes, because it
    really is a working store. It just forgets everything when Python exits.
    """

    TEST_ONLY_VOLATILE = "TEST_ONLY_VOLATILE"
    DURABLE_APPEND_ONLY = "DURABLE_APPEND_ONLY"

    @property
    def survives_the_process(self) -> bool:
        return self is StoreDurability.DURABLE_APPEND_ONLY


#: How much room a capture needs before it is worth starting. A full SPX chain
#: with greeks is tens of megabytes; the floor is deliberately generous, because
#: running out of disk halfway through a paid session is the one failure that
#: cannot be retried for free.
DEFAULT_MINIMUM_FREE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RawResponseRecord:
    """One vendor response, with everything needed to reproduce and audit it."""

    record_id: str
    endpoint: str
    query_params: dict[str, Any]
    request_started_at: datetime
    response_received_at: datetime
    http_status: int
    payload_hash: str
    payload_location: str
    parser_version: str = PARSER_VERSION
    vendor_schema_version: str | None = None
    byte_length: int = 0
    request_id: str = ""
    request_sequence: int = 0
    #: False when a write was interrupted before the atomic rename.
    capture_complete: bool = True
    #: Which transport produced this. Stamped at capture, never asserted later.
    capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN

    @property
    def round_trip_seconds(self) -> float:
        return (self.response_received_at - self.request_started_at).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "endpoint": self.endpoint,
            "query_params": dict(sorted(self.query_params.items())),
            "request_started_at": self.request_started_at.isoformat(),
            "response_received_at": self.response_received_at.isoformat(),
            "round_trip_seconds": self.round_trip_seconds,
            "http_status": self.http_status,
            "payload_hash": self.payload_hash,
            "payload_location": self.payload_location,
            "parser_version": self.parser_version,
            "vendor_schema_version": self.vendor_schema_version,
            "byte_length": self.byte_length,
            "request_id": self.request_id,
            "request_sequence": self.request_sequence,
            "capture_complete": self.capture_complete,
            "capture_origin": self.capture_origin.value,
        }


def payload_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_parameter_hash(params: Mapping[str, Any]) -> str:
    """Hash of the request parameters, order-independent.

    Sorted keys, so the same effective request always produces the same digest
    however the query happened to be assembled.
    """
    payload = json.dumps(
        {str(k): params[k] for k in sorted(params)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_record_id(
    *,
    session_id: str,
    sequence: int,
    endpoint: str,
    query_params: Mapping[str, Any],
    payload: str,
) -> str:
    """Collision-safe, deterministic, filesystem-safe record id.

    Includes the sequence *and* the parameter hash: the sequence distinguishes
    two identical requests in one session, the parameter hash distinguishes two
    different requests to the same endpoint, and the payload hash lets a reader
    spot when the same bytes came back twice.
    """
    safe_session = _safe_component(session_id)
    safe_endpoint = _safe_component(endpoint.strip("/").replace("/", "-"))
    return (
        f"{safe_session}-{sequence:04d}-{safe_endpoint}"
        f"-{canonical_parameter_hash(query_params)}-{payload_hash(payload)[:12]}"
    )


def record_id_belongs_to(record_id: str, session_id: str) -> bool:
    """Whether a record id was minted by this capture session.

    The session id is the first component of every id ``build_record_id``
    produces, which makes the association checkable rather than asserted: a
    manifest that claims a session cannot then list records another session
    wrote.
    """
    return record_id.startswith(f"{_safe_component(session_id)}-")


#: Every field an index entry must carry to be interpretable at all.
REQUIRED_METADATA_FIELDS = (
    "record_id",
    "endpoint",
    "payload_hash",
    "byte_length",
    "payload_location",
    "parser_version",
    "request_started_at",
    "response_received_at",
    "capture_complete",
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: Parser versions this code can interpret. A record written by a different
#: parser is not evidence this scanner can read, and pretending otherwise is how
#: a changed interpretation becomes invisible to a replay.
SUPPORTED_PARSER_VERSIONS = frozenset({PARSER_VERSION})


def new_capture_session_id(*, as_of: datetime | None = None) -> str:
    """A session id that cannot collide, with market time as audit metadata.

    v2.1.1 built this from the market ``as_of`` alone::

        f"{as_of.date().isoformat()}-{as_of.strftime('%H%M%S')}"

    Two captures at the same market timestamp -- a retry, a second symbol, a
    re-run of the same historical instant -- produced the same id. The store is
    append-only, so the second one raised, and the failure looked like a storage
    bug rather than an identity bug.

    Market time stays in the id because it is genuinely useful when reading a
    directory listing. It is not what makes the id unique; the nonce is. A
    restarted process cannot reuse an id because the nonce is drawn fresh.
    """
    stamp = (as_of or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}__{uuid.uuid4().hex[:12]}"


def validate_metadata(payload: Any) -> tuple[IntegrityStatus | None, str]:
    """Check an index entry BEFORE anything is derived from it.

    v2.1.1 read ``data["record_id"]`` and immediately resolved a filesystem path
    from it. Malformed metadata therefore crashed the scanner whose entire
    purpose is to report malformed metadata -- and a hostile or corrupt
    ``record_id`` got as far as path resolution.

    Returns ``(None, "")`` when the entry is well-formed.
    """
    if not isinstance(payload, dict):
        return IntegrityStatus.INVALID_METADATA, (
            f"expected a JSON object, got {type(payload).__name__}"
        )

    missing = [f for f in REQUIRED_METADATA_FIELDS if f not in payload]
    if missing:
        return IntegrityStatus.INVALID_METADATA, f"missing field(s) {sorted(missing)}"

    record_id = payload["record_id"]
    if not isinstance(record_id, str) or not record_id.strip():
        return IntegrityStatus.UNSAFE_RECORD_ID, (
            f"record_id must be a non-empty string, got {type(record_id).__name__}"
        )
    if not is_safe_record_id(record_id):
        return IntegrityStatus.UNSAFE_RECORD_ID, (
            "record_id contains path separators or traversal segments"
        )

    if not isinstance(payload["endpoint"], str):
        return IntegrityStatus.INVALID_METADATA, "endpoint must be a string"
    if not isinstance(payload["parser_version"], str):
        return IntegrityStatus.INVALID_METADATA, "parser_version must be a string"
    if not isinstance(payload["capture_complete"], bool):
        return IntegrityStatus.INVALID_METADATA, "capture_complete must be a boolean"

    byte_length = payload["byte_length"]
    if isinstance(byte_length, bool) or not isinstance(byte_length, int):
        return IntegrityStatus.INVALID_BYTE_LENGTH, (
            f"byte_length must be an integer, got {type(byte_length).__name__}"
        )
    if byte_length < 0:
        return (
            IntegrityStatus.INVALID_BYTE_LENGTH,
            f"negative byte_length {byte_length}",
        )

    digest = payload["payload_hash"]
    if not isinstance(digest, str) or not _HEX64.match(digest):
        return IntegrityStatus.INVALID_HASH, "payload_hash is not a sha256 hex digest"

    if payload["parser_version"] not in SUPPORTED_PARSER_VERSIONS:
        return IntegrityStatus.INVALID_METADATA, (
            f"parser_version {payload['parser_version']!r} is not supported by "
            f"this code ({sorted(SUPPORTED_PARSER_VERSIONS)}); a record read by "
            "a different parser is not one this scanner can interpret"
        )

    location = payload["payload_location"]
    if not isinstance(location, str) or not location.strip():
        return IntegrityStatus.INVALID_METADATA, (
            "payload_location must name where the bytes are; an entry that does "
            "not is an index of nothing"
        )

    params = payload.get("query_params", {})
    if not isinstance(params, dict):
        return IntegrityStatus.INVALID_METADATA, (
            f"query_params must be a mapping, got {type(params).__name__}. The "
            "parameters are part of what a response *is*."
        )

    status = payload.get("http_status")
    if isinstance(status, bool) or not isinstance(status, int):
        return IntegrityStatus.INVALID_METADATA, (
            f"http_status must be an integer, got {type(status).__name__}"
        )

    request_id = payload.get("request_id", "")
    if not isinstance(request_id, str):
        return IntegrityStatus.INVALID_METADATA, (
            f"request_id must be a string, got {type(request_id).__name__}"
        )

    sequence = payload.get("request_sequence", 0)
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        return IntegrityStatus.INVALID_METADATA, (
            f"request_sequence must be an integer, got {type(sequence).__name__}"
        )
    if sequence < 0:
        return IntegrityStatus.INVALID_METADATA, (
            f"negative request_sequence {sequence}"
        )

    schema = payload.get("vendor_schema_version")
    if schema is not None and not isinstance(schema, str):
        return IntegrityStatus.INVALID_METADATA, (
            f"vendor_schema_version must be a string or absent, got "
            f"{type(schema).__name__}"
        )

    origin = payload.get("capture_origin", CaptureOrigin.UNKNOWN_ORIGIN.value)
    if origin not in {member.value for member in CaptureOrigin}:
        return IntegrityStatus.INVALID_METADATA, (
            f"capture_origin {origin!r} is not a recognised origin"
        )

    instants: dict[str, datetime] = {}
    for field_name in ("request_started_at", "response_received_at"):
        value = payload[field_name]
        if not isinstance(value, str):
            return IntegrityStatus.INVALID_TIMESTAMP, f"{field_name} must be a string"
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return IntegrityStatus.INVALID_TIMESTAMP, (
                f"{field_name} is not an ISO-8601 timestamp: {value!r}"
            )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return IntegrityStatus.INVALID_TIMESTAMP, (
                f"{field_name} is naive; a stored clock without an offset means "
                "whatever zone the reader happens to be in"
            )
        instants[field_name] = parsed

    if instants["response_received_at"] < instants["request_started_at"]:
        return IntegrityStatus.INVALID_TIMESTAMP, (
            "the response was received before the request was sent: "
            f"{instants['response_received_at'].isoformat()} < "
            f"{instants['request_started_at'].isoformat()}"
        )

    return None, ""


def is_safe_record_id(record_id: str) -> bool:
    """True when a record id cannot escape the store root.

    Checked as a *string property*, so no path is ever constructed from an
    untrusted id in order to find out whether constructing it was safe.
    """
    if not record_id or record_id != record_id.strip():
        return False
    if any(sep in record_id for sep in ("/", "\\", "\x00")):
        return False
    return ".." not in pathlib.PurePosixPath(record_id).parts and ".." not in record_id


def _safe_component(value: str) -> str:
    """Filesystem-safe fragment. Rejects rather than silently mangling."""
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in value)
    if not cleaned or cleaned.strip("-") == "":
        raise RawStoreError(f"unsafe identifier component: {value!r}")
    return cleaned


class IntegrityStatus(str, Enum):
    """What a scan found for one artefact.

    Payload and index writes are each atomic, but not atomic *together*: a
    crash between them leaves a consistent-looking store with one half of a
    pair. Nothing in v2.1 could tell you afterwards which pairs had come apart,
    which meant an audit trail whose own completeness was unverifiable.
    """

    VALID = "VALID"
    #: A payload file with no index entry -- crash after rename, before append.
    ORPHAN_PAYLOAD = "ORPHAN_PAYLOAD"
    #: An index entry with no payload -- crash after append, before rename.
    MISSING_PAYLOAD = "MISSING_PAYLOAD"
    HASH_MISMATCH = "HASH_MISMATCH"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    #: A leftover temp file. Evidence, not garbage: reported, never removed.
    INCOMPLETE_WRITE = "INCOMPLETE_WRITE"
    DUPLICATE_ID = "DUPLICATE_ID"
    INVALID_METADATA = "INVALID_METADATA"
    #: The record id would escape the store root, or is not a string.
    #: Reported *before* any path is resolved from it.
    UNSAFE_RECORD_ID = "UNSAFE_RECORD_ID"
    INVALID_BYTE_LENGTH = "INVALID_BYTE_LENGTH"
    INVALID_HASH = "INVALID_HASH"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"


@dataclass(frozen=True, slots=True)
class IntegrityFinding:
    status: IntegrityStatus
    artifact: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "artifact": self.artifact,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """The result of a scan. Describes; never repairs."""

    findings: tuple[IntegrityFinding, ...]

    @property
    def ok(self) -> bool:
        return all(f.status is IntegrityStatus.VALID for f in self.findings)

    def counts(self) -> dict[str, int]:
        counter: dict[str, int] = {}
        for finding in self.findings:
            counter[finding.status.value] = counter.get(finding.status.value, 0) + 1
        return dict(sorted(counter.items()))

    def recovery_plan(self) -> tuple[str, ...]:
        """What a human *could* do, phrased so nobody mistakes it for done.

        Deliberately returns strings rather than callables. Silently deleting an
        artefact destroys the only evidence of how the store came apart, and the
        artefact may be the more trustworthy half of the pair.
        """
        actions: list[str] = []
        for finding in self.findings:
            if finding.status is IntegrityStatus.VALID:
                continue
            actions.append(
                f"proposed: inspect {finding.artifact} "
                f"({finding.status.value}) -- {finding.detail or 'no further detail'}"
            )
        return tuple(actions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "counts": self.counts(),
            "findings": [f.as_dict() for f in self.findings],
            "recovery_plan": list(self.recovery_plan()),
        }


@runtime_checkable
class RawResponseStore(Protocol):
    def put(
        self,
        *,
        record_id: str,
        endpoint: str,
        query_params: dict[str, Any],
        payload: str,
        request_started_at: datetime,
        response_received_at: datetime,
        http_status: int,
        vendor_schema_version: str | None = None,
        request_id: str = "",
        request_sequence: int = 0,
        capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN,
    ) -> RawResponseRecord: ...

    def get_payload(self, record_id: str) -> str: ...

    def records(self) -> tuple[RawResponseRecord, ...]: ...


class InMemoryRawStore:
    """Append-only store for tests and short-lived research runs.

    **Volatile.** Everything it holds disappears with the process, which makes
    it right for unit tests and offline fixtures and wrong for the only copy of
    a paid session's evidence. ``durability`` says so, and capture readiness
    refuses it -- v2.1.5 probed for protocol compliance, integrity and a
    successful write, all of which this passes.
    """

    durability = StoreDurability.TEST_ONLY_VOLATILE

    def __init__(self) -> None:
        self._records: dict[str, RawResponseRecord] = {}
        self._payloads: dict[str, str] = {}

    def put(
        self,
        *,
        record_id: str,
        endpoint: str,
        query_params: dict[str, Any],
        payload: str,
        request_started_at: datetime,
        response_received_at: datetime,
        http_status: int,
        vendor_schema_version: str | None = None,
        request_id: str = "",
        request_sequence: int = 0,
        capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN,
    ) -> RawResponseRecord:
        if record_id in self._records:
            raise RawStoreError(
                f"raw response {record_id!r} already exists; the store is "
                "append-only so evidence cannot be overwritten"
            )
        record = RawResponseRecord(
            record_id=record_id,
            endpoint=endpoint,
            query_params=dict(query_params),
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            http_status=http_status,
            payload_hash=payload_hash(payload),
            payload_location=f"memory://{record_id}",
            vendor_schema_version=vendor_schema_version,
            byte_length=len(payload.encode("utf-8")),
            request_id=request_id,
            request_sequence=request_sequence,
            capture_origin=capture_origin,
        )
        self._records[record_id] = record
        self._payloads[record_id] = payload
        return record

    def get_payload(self, record_id: str) -> str:
        if record_id not in self._payloads:
            raise KeyError(record_id)
        return self._payloads[record_id]

    @property
    def durable(self) -> bool:
        return self.durability.survives_the_process

    def next_request_sequence(self) -> int:
        recorded = [r.request_sequence for r in self.records()]
        return max(recorded, default=0) + 1

    def records(self) -> tuple[RawResponseRecord, ...]:
        return tuple(
            self._records[key]
            for key in sorted(self._records)
            if not is_probe_record(key)
        )


class FileRawStore:
    """Append-only store backed by a directory. Durable.

    Layout: ``<root>/<record_id>.raw`` for the payload and ``<root>/index.jsonl``
    for the metadata. Deliberately plain files -- the audit trail should be
    readable without this codebase.
    """

    durability = StoreDurability.DURABLE_APPEND_ONLY

    def __init__(self, root: pathlib.Path) -> None:
        self._root = pathlib.Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._index = self._root / "index.jsonl"
        # Probe records live in a sibling directory, never in the capture
        # namespace. v2.1.5 wrote them into the store and then filtered them out
        # of every scan -- which worked, and meant the health check of an
        # append-only store permanently added to it.
        self._probe_root = self._root.parent / f"{self._root.name}.health"

    def _payload_path(self, record_id: str) -> pathlib.Path:
        # Path-traversal guard: reject rather than sanitise, so a caller cannot
        # believe it wrote one file while another was written.
        safe = "".join(ch for ch in record_id if ch.isalnum() or ch in "-_.")
        if safe != record_id or not safe or ".." in record_id:
            raise RawStoreError(f"unsafe record id: {record_id!r}")
        resolved = (self._root / f"{safe}.raw").resolve()
        if not str(resolved).startswith(str(self._root.resolve())):
            raise RawStoreError(f"record id escapes the store root: {record_id!r}")
        return resolved

    def put(
        self,
        *,
        record_id: str,
        endpoint: str,
        query_params: dict[str, Any],
        payload: str,
        request_started_at: datetime,
        response_received_at: datetime,
        http_status: int,
        vendor_schema_version: str | None = None,
        request_id: str = "",
        request_sequence: int = 0,
        capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN,
    ) -> RawResponseRecord:
        path = self._payload_path(record_id)
        if path.exists():
            raise RawStoreError(
                f"raw response {record_id!r} already exists at {path}; the store "
                "is append-only so evidence cannot be overwritten"
            )
        record = RawResponseRecord(
            record_id=record_id,
            endpoint=endpoint,
            query_params=dict(query_params),
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            http_status=http_status,
            payload_hash=payload_hash(payload),
            payload_location=str(path),
            vendor_schema_version=vendor_schema_version,
            byte_length=len(payload.encode("utf-8")),
            request_id=request_id,
            request_sequence=request_sequence,
            capture_origin=capture_origin,
        )
        # Atomic write: temp file -> flush -> fsync -> rename. A crash midway
        # leaves either nothing or a complete file, never a truncated payload
        # that a later reader would treat as the vendor's full response.
        self._atomic_write(path, payload)
        self._append_index(record)
        return record

    @staticmethod
    def _atomic_write(path: pathlib.Path, payload: str) -> None:
        handle, temp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".partial-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except BaseException:
            pathlib.Path(temp_name).unlink(missing_ok=True)
            raise

    def _append_index(self, record: RawResponseRecord) -> None:
        with self._index.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record.as_dict(), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @property
    def root(self) -> pathlib.Path:
        """Where the artefacts live. Exposed so an integrity scan is auditable."""
        return self._root

    @property
    def durable(self) -> bool:
        return self.durability.survives_the_process

    def next_request_sequence(self) -> int:
        """The sequence a real capture would use next.

        Read by the health probe's test: a probe that consumed a sequence would
        leave a gap in the request numbering of the session it was checking.
        """
        recorded = [r.request_sequence for r in self.records()]
        return max(recorded, default=0) + 1

    def free_bytes(self) -> int | None:
        """Room left where the capture will be written."""
        import shutil

        try:
            return int(shutil.disk_usage(self._root).free)
        except OSError:
            return None

    def probe_write(self, payload: str) -> str:
        """Write and read back, without entering the capture index.

        Two writes, because they answer two questions. The sibling health
        directory proves the store's machinery works; a scratch file *inside the
        capture root* proves the destination a real session would write to is
        actually writable. Probing only the sibling would pass on a store whose
        own root had gone read-only, which is precisely the failure a paid
        session cannot survive.

        Neither write becomes evidence: ``records()`` and ``verify_integrity``
        read ``*.raw`` and the index, and neither of these is either.

        Returns what came back, so the caller compares bytes rather than
        trusting that no exception means success.
        """
        scratch = self._root / f".probe-{uuid.uuid4().hex[:16]}.tmp"
        self._atomic_write(scratch, payload)
        scratch.unlink(missing_ok=True)

        self._probe_root.mkdir(parents=True, exist_ok=True)
        target = self._probe_root / f"probe-{uuid.uuid4().hex[:16]}.tmp"
        self._atomic_write(target, payload)
        try:
            return target.read_text(encoding="utf-8")
        finally:
            target.unlink(missing_ok=True)

    def verify_integrity(self) -> IntegrityReport:
        """Scan the store and classify every artefact. Never modifies anything.

        Answers the question an auditor actually has -- "is this audit trail
        itself trustworthy?" -- which v2.1 had no way to answer.
        """
        findings: list[IntegrityFinding] = []
        seen_ids: set[str] = set()
        indexed_paths: set[pathlib.Path] = set()

        if self._index.exists():
            for number, line in enumerate(
                self._index.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    findings.append(
                        IntegrityFinding(
                            status=IntegrityStatus.INVALID_METADATA,
                            artifact=f"{self._index.name}:{number}",
                            detail=str(exc),
                        )
                    )
                    continue

                # Validate the schema BEFORE deriving anything -- above all
                # before resolving a filesystem path from record_id.
                status, detail = validate_metadata(data)
                if status is not None:
                    findings.append(
                        IntegrityFinding(
                            status=status,
                            artifact=f"{self._index.name}:{number}",
                            detail=detail,
                        )
                    )
                    continue

                record_id = str(data["record_id"])
                expected_hash = str(data["payload_hash"])

                # Health-probe records are the store proving it works, not
                # evidence. They are never removed -- an append-only store has
                # no delete -- so they are skipped here instead, or a probe
                # would make every later scan look like it had grown.
                if is_probe_record(record_id):
                    indexed_paths.add(self._payload_path(record_id))
                    continue

                if record_id in seen_ids:
                    findings.append(
                        IntegrityFinding(
                            status=IntegrityStatus.DUPLICATE_ID,
                            artifact=record_id,
                            detail=f"repeated at {self._index.name}:{number}",
                        )
                    )
                    continue
                seen_ids.add(record_id)

                path = self._payload_path(record_id)
                indexed_paths.add(path)
                if not path.exists():
                    findings.append(
                        IntegrityFinding(
                            status=IntegrityStatus.MISSING_PAYLOAD,
                            artifact=record_id,
                            detail="index entry has no payload file",
                        )
                    )
                    continue

                payload = path.read_text(encoding="utf-8")
                actual_hash = payload_hash(payload)
                expected_length = data.get("byte_length")
                if actual_hash != expected_hash:
                    findings.append(
                        IntegrityFinding(
                            status=IntegrityStatus.HASH_MISMATCH,
                            artifact=record_id,
                            detail=f"expected {expected_hash}, found {actual_hash}",
                        )
                    )
                elif expected_length is not None and len(
                    payload.encode("utf-8")
                ) != int(expected_length):
                    findings.append(
                        IntegrityFinding(
                            status=IntegrityStatus.SIZE_MISMATCH,
                            artifact=record_id,
                            detail=(
                                f"expected {expected_length} bytes, found "
                                f"{len(payload.encode('utf-8'))}"
                            ),
                        )
                    )
                else:
                    findings.append(
                        IntegrityFinding(
                            status=IntegrityStatus.VALID, artifact=record_id
                        )
                    )

        for path in sorted(self._root.iterdir()):
            if path == self._index or not path.is_file():
                continue
            if path.name.startswith(".") and path.suffix == ".tmp":
                findings.append(
                    IntegrityFinding(
                        status=IntegrityStatus.INCOMPLETE_WRITE,
                        artifact=path.name,
                        detail="temp file from an interrupted write",
                    )
                )
                continue
            if path not in indexed_paths:
                findings.append(
                    IntegrityFinding(
                        status=IntegrityStatus.ORPHAN_PAYLOAD,
                        artifact=path.name,
                        detail="payload file with no index entry",
                    )
                )

        return IntegrityReport(findings=tuple(findings))

    def incomplete_captures(self) -> tuple[str, ...]:
        """Temp files left behind by an interrupted write.

        Reported rather than cleaned up silently: an interrupted capture is
        evidence that something went wrong, and deleting it hides that.
        """
        return tuple(sorted(p.name for p in self._root.glob(".partial-*.tmp")))

    def get_payload(self, record_id: str) -> str:
        path = self._payload_path(record_id)
        if not path.exists():
            raise KeyError(record_id)
        return path.read_text(encoding="utf-8")

    def records(self) -> tuple[RawResponseRecord, ...]:
        if not self._index.exists():
            return ()
        out: list[RawResponseRecord] = []
        for line in self._index.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            if is_probe_record(str(data.get("record_id", ""))):
                continue
            out.append(
                RawResponseRecord(
                    record_id=data["record_id"],
                    endpoint=data["endpoint"],
                    query_params=data["query_params"],
                    request_started_at=datetime.fromisoformat(
                        data["request_started_at"]
                    ),
                    response_received_at=datetime.fromisoformat(
                        data["response_received_at"]
                    ),
                    http_status=data["http_status"],
                    payload_hash=data["payload_hash"],
                    payload_location=data["payload_location"],
                    parser_version=data["parser_version"],
                    vendor_schema_version=data.get("vendor_schema_version"),
                    byte_length=data.get("byte_length", 0),
                    request_id=data.get("request_id", ""),
                    request_sequence=data.get("request_sequence", 0),
                    capture_complete=data.get("capture_complete", True),
                    capture_origin=CaptureOrigin(
                        data.get("capture_origin", CaptureOrigin.UNKNOWN_ORIGIN.value)
                    ),
                )
            )
        return tuple(out)


class NullRawStore:
    """Discards everything. The default, so capture is opt-in."""

    def put(
        self,
        *,
        record_id: str,
        endpoint: str,
        query_params: dict[str, Any],
        payload: str,
        request_started_at: datetime,
        response_received_at: datetime,
        http_status: int,
        vendor_schema_version: str | None = None,
        request_id: str = "",
        request_sequence: int = 0,
        capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN,
    ) -> RawResponseRecord:
        return RawResponseRecord(
            record_id=record_id,
            endpoint=endpoint,
            query_params=dict(query_params),
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            http_status=http_status,
            payload_hash=payload_hash(payload),
            payload_location="null://discarded",
            vendor_schema_version=vendor_schema_version,
            byte_length=len(payload.encode("utf-8")),
            request_id=request_id,
            request_sequence=request_sequence,
            capture_origin=capture_origin,
        )

    def get_payload(self, record_id: str) -> str:
        raise KeyError(record_id)

    def records(self) -> tuple[RawResponseRecord, ...]:
        return ()


@dataclass(frozen=True, slots=True)
class RawStoreHealth:
    """Whether a store is a place to put a paid session's only copy.

    v2.1.4 accepted anything with the right attribute names. ``raw_store=
    object()`` has no ``verify_integrity``, so the integrity check was skipped
    rather than failed, and readiness passed on a store that could not have
    stored anything.
    """

    usable: bool
    failures: tuple[str, ...] = ()
    store_description: str = ""
    durability: StoreDurability = StoreDurability.TEST_ONLY_VOLATILE

    @property
    def durable(self) -> bool:
        return self.durability.survives_the_process

    def as_dict(self) -> dict[str, Any]:
        return {
            "usable": self.usable,
            "failures": list(self.failures),
            "store_description": self.store_description,
            "durability": self.durability.value,
        }


#: Prefix for health-probe records. Kept out of the capture namespace: a probe
#: that leaves a record behind has corrupted the audit trail it was checking.
PROBE_PREFIX = "healthprobe"


def probe_raw_store(
    store: Any,
    *,
    require_durable: bool = True,
    minimum_free_bytes: int = DEFAULT_MINIMUM_FREE_BYTES,
    source_root: pathlib.Path | None = None,
) -> RawStoreHealth:
    """Check that a store is somewhere a paid session's evidence can live.

    Six questions, because they fail for six different reasons: does it
    implement the protocol, does it survive the process, is it clean, is there
    room, can it take a write, and does that write read back byte-for-byte.

    v2.1.5 asked four of them. ``InMemoryRawStore`` passes all four -- it is a
    working store that forgets everything when Python exits -- so a readiness
    report could say the destination was fine for a session whose only copy of
    the evidence would not survive it.

    The probe writes into a sibling directory, never into the capture namespace,
    and removes what it wrote. v2.1.5 wrote probe records into the store and
    filtered them out of every scan afterwards, which meant the health check of
    an append-only store permanently added to it.
    """
    failures: list[str] = []
    description = type(store).__name__

    if not isinstance(store, RawResponseStore):
        return RawStoreHealth(
            usable=False,
            failures=(
                f"PROTOCOL: {description} does not implement RawResponseStore; a "
                "store that cannot put, get or list is not somewhere to keep "
                "evidence",
            ),
            store_description=description,
        )

    # A store that does not classify itself is volatile: durability is a claim,
    # and the default for an unmade claim is the one that blocks a paid session.
    try:
        durability = StoreDurability(
            getattr(store, "durability", StoreDurability.TEST_ONLY_VOLATILE)
        )
    except ValueError:
        durability = StoreDurability.TEST_ONLY_VOLATILE
    if require_durable and not durability.survives_the_process:
        failures.append(
            f"DURABILITY: {description} is not durable, it is "
            f"{durability.value}. A paid session's only copy of the evidence "
            "cannot live in a process that is about to exit. It stays supported "
            "for unit tests and offline fixtures."
        )

    integrity = getattr(store, "verify_integrity", None)
    if callable(integrity):
        report = integrity()
        if not report.ok:
            failures.append(f"INTEGRITY: store is not clean: {report.counts()}")

    root = getattr(store, "root", None)
    if root is not None:
        resolved = pathlib.Path(root).resolve()
        tree = (source_root or pathlib.Path(__file__).resolve().parents[2]).resolve()
        if resolved == tree or tree in resolved.parents:
            failures.append(
                f"LOCATION: {resolved} is inside the source tree. Captured "
                "vendor bytes are not source, and a capture written there ends "
                "up in a commit or a release archive."
            )

    free = getattr(store, "free_bytes", None)
    if callable(free):
        available = free()
        if available is not None and available < minimum_free_bytes:
            failures.append(
                f"SPACE: {available} bytes free, below the {minimum_free_bytes} "
                "minimum. Running out of disk halfway through a paid session is "
                "the one failure that cannot be retried for free."
            )

    payload = f"{PROBE_PREFIX}-{uuid.uuid4().hex}"
    probe = getattr(store, "probe_write", None)
    try:
        if callable(probe):
            read_back = probe(payload)
        else:
            read_back = _probe_through_the_store(store, payload)
    except Exception as exc:
        failures.append(f"WRITE: the store refused a probe record: {exc}")
        return RawStoreHealth(
            usable=False,
            failures=tuple(failures),
            store_description=description,
            durability=durability,
        )

    if read_back != payload:
        failures.append("READ: the probe did not read back byte-identical")

    return RawStoreHealth(
        usable=not failures,
        failures=tuple(failures),
        store_description=description,
        durability=durability,
    )


def _probe_through_the_store(store: Any, payload: str) -> str:
    """Fallback for a store with no native probe.

    Uses the reserved probe namespace, which ``records()`` and
    ``verify_integrity`` both ignore, so a volatile store can still be checked
    without the probe becoming evidence.
    """
    record_id = f"{PROBE_PREFIX}-{uuid.uuid4().hex[:16]}"
    now = datetime.now(UTC)
    store.put(
        record_id=record_id,
        endpoint="/internal/health-probe",
        query_params={},
        payload=payload,
        request_started_at=now,
        response_received_at=now,
        http_status=200,
    )
    return str(store.get_payload(record_id))


def is_probe_record(record_id: str) -> bool:
    """Probe records are health checks, not evidence, and never count as either."""
    return record_id.startswith(PROBE_PREFIX)


@dataclass(slots=True)
class CaptureSession:
    """Groups the records belonging to one chain pull.

    Record ids combine the session, a monotonic request sequence, the endpoint
    and a canonical parameter hash. v2 used ``session-endpoint`` alone, so two
    requests to the same endpoint in one session collided -- and because the
    store is append-only, the second one raised rather than being captured.
    """

    store: RawResponseStore
    session_id: str
    captured: list[RawResponseRecord] = field(default_factory=list)
    #: Where this session's responses come from. Set by the client from the
    #: transport it is actually using, not by whoever opened the session.
    capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN
    _sequence: int = 0

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def mark(self) -> int:
        """Where this session's record list currently ends.

        Pass the result to ``RawCaptureManifest.from_session(session, since=...)``
        to build a manifest of only what was captured afterwards.

        v2.1.4 always took the whole list, so a session reused for a second chain
        pull gave the second snapshot a manifest naming the first snapshot's
        responses. A provenance record listing bytes that produced a different
        number is worse than no provenance record, because it looks like one.
        """
        return len(self.captured)

    def capture(
        self,
        *,
        endpoint: str,
        query_params: dict[str, Any],
        payload: str,
        request_started_at: datetime,
        response_received_at: datetime,
        http_status: int,
        request_id: str = "",
        capture_origin: CaptureOrigin | None = None,
    ) -> RawResponseRecord:
        sequence = self.next_sequence()
        record = self.store.put(
            record_id=build_record_id(
                session_id=self.session_id,
                sequence=sequence,
                endpoint=endpoint,
                query_params=query_params,
                payload=payload,
            ),
            endpoint=endpoint,
            query_params=query_params,
            payload=payload,
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            http_status=http_status,
            request_id=request_id,
            request_sequence=sequence,
            capture_origin=capture_origin or self.capture_origin,
        )
        self.captured.append(record)
        return record

    def manifest(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "parser_version": PARSER_VERSION,
            "records": [record.as_dict() for record in self.captured],
        }


@dataclass(frozen=True, slots=True)
class ManifestRecord:
    """One captured response, described completely.

    v2.1.5 kept the same information in four parallel structures: a tuple of
    record ids, a tuple of payload hashes, a tuple of request ids and a dict of
    parameter hashes. Nothing tied the entries together, so verification could
    only compare *sets* -- and two records that swapped payload hashes still
    matched, because the multiset was unchanged.

    Here every field belongs to a named record, and the manifest hash covers the
    descriptor rather than four independently sorted lists.
    """

    record_id: str
    endpoint: str
    payload_hash: str
    parameter_hash: str
    request_id: str = ""
    request_sequence: int = 0
    http_status: int = 200
    request_started_at: datetime | None = None
    response_received_at: datetime | None = None
    parser_version: str = PARSER_VERSION
    vendor_schema_version: str | None = None
    capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN

    @classmethod
    def of(cls, record: RawResponseRecord) -> ManifestRecord:
        return cls(
            record_id=record.record_id,
            endpoint=record.endpoint,
            payload_hash=record.payload_hash,
            parameter_hash=canonical_parameter_hash(record.query_params),
            request_id=record.request_id,
            request_sequence=record.request_sequence,
            http_status=record.http_status,
            request_started_at=record.request_started_at,
            response_received_at=record.response_received_at,
            parser_version=record.parser_version,
            vendor_schema_version=record.vendor_schema_version,
            capture_origin=record.capture_origin,
        )

    def semantic_payload(self) -> dict[str, Any]:
        """Everything a change to which changes what this record *is*.

        All of it enters the manifest hash. A different request id, sequence,
        status or clock describes a different response, whatever the bytes say.
        """
        return {
            "record_id": self.record_id,
            "endpoint": self.endpoint,
            "payload_hash": self.payload_hash,
            "parameter_hash": self.parameter_hash,
            "request_id": self.request_id,
            "request_sequence": self.request_sequence,
            "http_status": self.http_status,
            "request_started_at": (
                self.request_started_at.isoformat() if self.request_started_at else None
            ),
            "response_received_at": (
                self.response_received_at.isoformat()
                if self.response_received_at
                else None
            ),
            "parser_version": self.parser_version,
            "vendor_schema_version": self.vendor_schema_version,
            "capture_origin": self.capture_origin.value,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.semantic_payload()


@dataclass(frozen=True, slots=True)
class RawCaptureManifest:
    """Which raw records a normalized snapshot was actually built from.

    Without this, a stored chain and a directory of captured payloads are two
    unrelated artefacts that happen to share a timestamp. Reconstructing which
    bytes produced which number -- the entire reason for capturing raw payloads
    -- meant guessing from filenames.

    ``manifest_hash`` covers the full per-record descriptors, so a snapshot
    whose sources changed in *any* audit-relevant way cannot present the same
    manifest.
    """

    session_id: str
    records: tuple[ManifestRecord, ...] = ()
    #: False when capture was disabled. Recorded explicitly rather than left to
    #: an absent key, which reads the same as "we forgot".
    capture_enabled: bool = True
    #: The plan this capture was taken against, so a later reader can tell what
    #: the capture was *meant* to contain rather than only what it does.
    capture_plan_fingerprint: str = ""
    #: Which code read these bytes, and which configuration asked for them.
    parser_version: str = PARSER_VERSION
    pipeline_fingerprint: str = ""
    #: The shape of this evidence. An older manifest is refused rather than
    #: reinterpreted -- v2.1.5's parallel arrays cannot express the per-record
    #: binding this one verifies.
    schema_version: str = MANIFEST_SCHEMA_VERSION
    #: Where the responses came from. Derived from the transport, never asserted.
    capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN

    @property
    def record_ids(self) -> tuple[str, ...]:
        return tuple(sorted(r.record_id for r in self.records))

    @property
    def payload_hashes(self) -> tuple[str, ...]:
        return tuple(sorted(r.payload_hash for r in self.records))

    @property
    def request_ids(self) -> tuple[str, ...]:
        return tuple(sorted(r.request_id for r in self.records if r.request_id))

    @property
    def endpoint_records(self) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, list[str]] = {}
        for record in self.records:
            grouped.setdefault(record.endpoint, []).append(record.record_id)
        return {
            endpoint: tuple(sorted(ids)) for endpoint, ids in sorted(grouped.items())
        }

    @property
    def request_parameter_hashes(self) -> dict[str, str]:
        return {
            r.record_id: r.parameter_hash
            for r in sorted(self.records, key=lambda r: r.record_id)
        }

    def record(self, record_id: str) -> ManifestRecord | None:
        for candidate in self.records:
            if candidate.record_id == record_id:
                return candidate
        return None

    @property
    def manifest_hash(self) -> str:
        """Full SHA-256 over sorted per-record descriptors.

        v2.1.4 used the first sixteen hex characters; v2.1.5 widened it but
        still hashed four independently sorted lists, so swapping two records'
        payload hashes left the digest unchanged. The descriptor is the unit.
        """
        payload = json.dumps(
            {
                "schema_version": self.schema_version,
                "session_id": self.session_id,
                "capture_enabled": self.capture_enabled,
                "capture_plan_fingerprint": self.capture_plan_fingerprint,
                "parser_version": self.parser_version,
                "pipeline_fingerprint": self.pipeline_fingerprint,
                "capture_origin": self.capture_origin.value,
                "records": sorted(
                    (record.semantic_payload() for record in self.records),
                    key=lambda entry: entry["record_id"],
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def records_for(self, endpoint: str) -> tuple[str, ...]:
        return tuple(
            sorted(r.record_id for r in self.records if r.endpoint == endpoint)
        )

    @property
    def endpoints(self) -> frozenset[str]:
        return frozenset(r.endpoint for r in self.records)

    @classmethod
    def disabled(cls) -> RawCaptureManifest:
        """Raw capture was off. Stated, not implied."""
        return cls(session_id="", capture_enabled=False)

    @classmethod
    def from_session(
        cls,
        session: CaptureSession,
        *,
        since: int = 0,
        capture_plan_fingerprint: str = "",
        pipeline_fingerprint: str = "",
    ) -> RawCaptureManifest:
        """The records this snapshot used, and only those.

        ``since`` is a mark taken from ``CaptureSession.mark()`` before the
        fetch. v2.1.4 always took the whole session, so a second chain pull
        inherited the first pull's responses -- a provenance record naming bytes
        that produced a different number.
        """
        captured = tuple(session.captured[since:])
        origins = {r.capture_origin for r in captured}
        return cls(
            session_id=session.session_id,
            records=tuple(
                sorted(
                    (ManifestRecord.of(record) for record in captured),
                    key=lambda entry: entry.record_id,
                )
            ),
            capture_plan_fingerprint=capture_plan_fingerprint,
            pipeline_fingerprint=pipeline_fingerprint,
            # One origin, or none: a capture assembled from two transports is
            # not one session, and calling it either would be a claim.
            capture_origin=(
                origins.pop() if len(origins) == 1 else CaptureOrigin.UNKNOWN_ORIGIN
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "capture_enabled": self.capture_enabled,
            "capture_origin": self.capture_origin.value,
            "record_count": len(self.records),
            "records": [record.as_dict() for record in self.records],
            "record_ids": list(self.record_ids),
            "payload_hashes": list(self.payload_hashes),
            "request_ids": list(self.request_ids),
            "endpoint_records": {
                endpoint: list(ids) for endpoint, ids in self.endpoint_records.items()
            },
            "request_parameter_hashes": dict(self.request_parameter_hashes),
            "capture_plan_fingerprint": self.capture_plan_fingerprint,
            "parser_version": self.parser_version,
            "pipeline_fingerprint": self.pipeline_fingerprint,
            "manifest_hash": self.manifest_hash,
        }
