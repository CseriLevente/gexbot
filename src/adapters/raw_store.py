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
import pathlib
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
        }


def payload_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
        safe = "".join(ch for ch in record_id if ch.isalnum() or ch in "-_.")
        if safe != record_id or not safe:
            raise RawStoreError(f"unsafe record id: {record_id!r}")
        return self._root / f"{safe}.raw"

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
        )
        path.write_text(payload, encoding="utf-8", newline="")
        with self._index.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record.as_dict(), sort_keys=True) + "\n")
        return record

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
        )

    def get_payload(self, record_id: str) -> str:
        raise KeyError(record_id)

    def records(self) -> tuple[RawResponseRecord, ...]:
        return ()


@dataclass(slots=True)
class CaptureSession:
    """Groups the records belonging to one chain pull."""

    store: RawResponseStore
    session_id: str
    captured: list[RawResponseRecord] = field(default_factory=list)

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
        suffix = endpoint.strip("/").replace("/", "-")
        record = self.store.put(
            record_id=f"{self.session_id}-{suffix}",
            endpoint=endpoint,
            query_params=query_params,
            payload=payload,
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            http_status=http_status,
            request_id=request_id,
        )
        self.captured.append(record)
        return record

    def manifest(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "parser_version": PARSER_VERSION,
            "records": [record.as_dict() for record in self.captured],
        }
