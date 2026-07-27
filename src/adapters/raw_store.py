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
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

# Bumped when the parser's interpretation of a payload changes, so a stored
# record says which code read it.
PARSER_VERSION = "thetadata-v3-parser/2.0.0"


class RawStoreError(RuntimeError):
    pass


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


def _safe_component(value: str) -> str:
    """Filesystem-safe fragment. Rejects rather than silently mangling."""
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in value)
    if not cleaned or cleaned.strip("-") == "":
        raise RawStoreError(f"unsafe identifier component: {value!r}")
    return cleaned


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
    ) -> RawResponseRecord: ...

    def get_payload(self, record_id: str) -> str: ...

    def records(self) -> tuple[RawResponseRecord, ...]: ...


class InMemoryRawStore:
    """Append-only store for tests and short-lived research runs."""

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
        )
        self._records[record_id] = record
        self._payloads[record_id] = payload
        return record

    def get_payload(self, record_id: str) -> str:
        if record_id not in self._payloads:
            raise KeyError(record_id)
        return self._payloads[record_id]

    def records(self) -> tuple[RawResponseRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))


class FileRawStore:
    """Append-only store backed by a directory.

    Layout: ``<root>/<record_id>.raw`` for the payload and ``<root>/index.jsonl``
    for the metadata. Deliberately plain files -- the audit trail should be
    readable without this codebase.
    """

    def __init__(self, root: pathlib.Path) -> None:
        self._root = pathlib.Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._index = self._root / "index.jsonl"

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
        )

    def get_payload(self, record_id: str) -> str:
        raise KeyError(record_id)

    def records(self) -> tuple[RawResponseRecord, ...]:
        return ()


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
    _sequence: int = 0

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

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
        )
        self.captured.append(record)
        return record

    def manifest(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "parser_version": PARSER_VERSION,
            "records": [record.as_dict() for record in self.captured],
        }
