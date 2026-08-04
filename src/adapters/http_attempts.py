"""Every HTTP attempt, including the ones that were retried away.

v2.1.11's capture statement said it preserved every response. It preserved every
response the *client* saw, and the client saw only the last one:
:class:`RetryingTransport` consumes a 429 or a 503 body, logs a warning, sleeps
and tries again. So the responses that would explain a partial capture -- the
rate-limit body naming a quota, the 503 naming a maintenance window -- were the
ones thrown away, and the sentence describing the capture was wrong.

An attempt observer sits inside the retry loop. It is handed one
:class:`HttpAttemptRecord` per attempt, successful or not, and decides what to
do with it; :class:`HttpAttemptLog` writes the bodies content-addressed beside
the capture and keeps the metadata for the summary.

Nothing here is chain data. A preserved 500 body is evidence about a failure and
is never normalized into a chain: the raw store holds the responses a snapshot
was built from, and these live in their own directory with their own schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.adapters.errors import ThetaDataRawStoreError
from src.domain.digests import digest_of

__all__ = [
    "HTTP_ATTEMPT_SCHEMA_VERSION",
    "SAFE_RESPONSE_HEADERS",
    "HttpAttemptLog",
    "HttpAttemptRecord",
    "safe_headers",
]

#: Attempt bodies are stored with this suffix because they are bytes, not text.
#: v2.1.12 used ``.txt`` for a body that had already been through
#: ``errors="replace"``, which was consistent and wrong in the same direction.
ATTEMPT_BODY_SUFFIX = ".bin"

#: Bumped when the *meaning* of an attempt record changes.
HTTP_ATTEMPT_SCHEMA_VERSION = "http-attempt/2.1.13"

#: Response headers worth keeping. Everything else is dropped rather than
#: filtered: an allow-list cannot leak a header nobody thought about, and a
#: deny-list can.
SAFE_RESPONSE_HEADERS = (
    "content-length",
    "content-type",
    "date",
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "x-request-id",
)


def safe_headers(headers: Any) -> dict[str, str]:
    """The subset of a response's headers that may be written down."""
    if not headers:
        return {}
    lowered = {str(k).lower(): str(v) for k, v in dict(headers).items()}
    return {name: lowered[name] for name in SAFE_RESPONSE_HEADERS if name in lowered}


@dataclass(frozen=True, slots=True)
class HttpAttemptRecord:
    """One request attempt, whatever came back.

    ``response_body_location`` is where the body was written, when there was
    one -- **relative to the attempt-store root**, so a run directory that is
    copied to an archive host still points at its own bodies. Resolve it with
    :meth:`HttpAttemptLog.body_path`. A transport failure produces a record with
    no status and no body and a ``transport_error_code``, which is a different
    thing from a 500 and reads as one.
    """

    logical_request_id: str
    attempt_number: int
    endpoint: str
    #: The URL with credential-shaped query parameters replaced. The full URL is
    #: never stored: it reaches a log, a traceback and this file.
    safe_url: str
    request_parameters_hash: str
    started_at: datetime
    received_at: datetime | None = None
    status_code: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body_hash: str | None = None
    response_body_location: str | None = None
    response_byte_length: int | None = None
    transport_error_code: str | None = None
    retryable: bool = False
    #: Whatever this particular failure needs to be understood -- the configured
    #: cap and the bytes read, for a ``RESPONSE_TOO_LARGE``. Typed loosely on
    #: purpose: an error class nobody has met yet should be able to say what
    #: happened without a schema change.
    detail: dict[str, Any] = field(default_factory=dict)
    #: How the body was read, when there was one.
    decode: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        """A 2xx *and* nothing that stopped it being usable.

        A response too large to read carries a 200 and is not a success: the
        status describes what the vendor meant to send, and the error code
        describes what this process could do with it.
        """
        return (
            self.transport_error_code is None
            and self.status_code is not None
            and 200 <= self.status_code < 300
        )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": HTTP_ATTEMPT_SCHEMA_VERSION,
            "logical_request_id": self.logical_request_id,
            "attempt_number": self.attempt_number,
            "endpoint": self.endpoint,
            "safe_url": self.safe_url,
            "request_parameters_hash": self.request_parameters_hash,
            "started_at": self.started_at.isoformat(),
            "received_at": (self.received_at.isoformat() if self.received_at else None),
            "status_code": self.status_code,
            "response_headers": dict(sorted(self.response_headers.items())),
            "response_body_hash": self.response_body_hash,
            "response_body_location": self.response_body_location,
            "response_byte_length": self.response_byte_length,
            "transport_error_code": self.transport_error_code,
            "retryable": self.retryable,
            "detail": dict(sorted(self.detail.items())),
            "decode": dict(sorted(self.decode.items())),
            "succeeded": self.succeeded,
        }

    @property
    def fingerprint(self) -> str:
        return digest_of(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self.semantic_payload(), "fingerprint": self.fingerprint}


class HttpAttemptLog:
    """Collects attempt records and writes their bodies beside the capture.

    Installed on :class:`RetryingTransport` as ``attempt_observer``. Holding it
    outside the transport is deliberate: the transport decides what to retry, and
    this decides what is worth keeping, and neither should be able to change the
    other by accident.

    ``root`` is optional so a caller can collect metadata without writing
    anything -- which is what the dry run would do if it made requests, and it
    does not.
    """

    def __init__(self, root: pathlib.Path | str | None = None) -> None:
        self.root = pathlib.Path(root) if root is not None else None
        self.records: list[HttpAttemptRecord] = []

    def observe(self, record: HttpAttemptRecord, body: bytes | None = None) -> None:
        """Record one attempt, storing its body bytes when there are any.

        The bytes are written and the metadata appended **now**, not at
        finalization. v2.1.12 wrote bodies as they arrived and held the records
        in memory until the summary, so an interpreter that died mid-run left
        content-addressed bodies nobody could attribute to a request.
        """
        from dataclasses import replace

        from src.adapters.transport import decode_body

        stored = record
        if body is not None:
            digest = hashlib.sha256(body).hexdigest()
            location = self._write_body(body, digest) if self.root is not None else None
            stored = replace(
                record,
                response_body_hash=digest,
                response_body_location=location,
                response_byte_length=len(body),
                decode=decode_body(body, record.response_headers).as_dict(),
            )
        self.records.append(stored)
        self._append_index(stored)

    def _append_index(self, record: HttpAttemptRecord) -> None:
        """One line per attempt, flushed and fsynced as it happens.

        Append-only JSONL rather than one rewritten document: an append that is
        interrupted loses at most the line it was writing, and every earlier line
        is still parseable. A rewritten document that is interrupted loses all of
        them.
        """
        if self.root is None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.as_dict(), sort_keys=True, default=str) + "\n"
        with open(self.index_path, "a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())

    @property
    def index_path(self) -> pathlib.Path:
        assert self.root is not None
        return self.root / "index.jsonl"

    @classmethod
    def recovered_from(cls, root: pathlib.Path | str) -> tuple[dict[str, Any], ...]:
        """Every attempt recorded under ``root``, read back off disk.

        What makes the incremental index worth having: after an interpreter that
        died or a finalization that failed, this is the attempt evidence, and it
        does not depend on the process that wrote it still existing.
        """
        index = pathlib.Path(root) / "index.jsonl"
        if not index.is_file():
            return ()
        entries: list[dict[str, Any]] = []
        for line in index.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                entries.append(json.loads(text))
            except json.JSONDecodeError:
                # A torn final line. Everything before it still counts, and
                # discarding the whole file because of it would be the opposite
                # of what an append-only index is for.
                continue
        return tuple(entries)

    def body_path(self, location: str) -> pathlib.Path:
        """Where a recorded location actually is, for this log, right now.

        Locations are stored relative to the root, so this is the only place
        that knows the absolute path -- and it derives it from where the log is
        *now* rather than from where it was when the body was written.
        """
        candidate = pathlib.Path(location)
        if candidate.is_absolute() or self.root is None:
            # An absolute location comes from an index written before v2.1.14.
            # Honoured as written: reinterpreting it relative to this root would
            # silently point at a different file.
            return candidate
        return self.root / candidate

    def verify_bodies(self) -> tuple[str, ...]:
        """Which stored attempt bodies no longer hash to their own filenames."""
        failures: list[str] = []
        for record in self.records:
            location = record.response_body_location
            if not location or not record.response_body_hash:
                continue
            path = self.body_path(location)
            if not path.is_file():
                failures.append(f"{location} is missing")
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != record.response_body_hash:
                failures.append(f"{location} hashes to {actual[:12]}...")
        return tuple(failures)

    def _write_body(self, payload: bytes, digest: str) -> str:
        """Content-addressed, atomically. Two identical bodies are one file.

        An existing file is *verified* before it is reused: content addressing
        means the name implies the content, and a file that no longer hashes to
        its name has stopped being the response it claims to be.
        """
        assert self.root is not None
        target = self.root / digest[:2] / f"{digest}.bin"
        location = target.relative_to(self.root).as_posix()
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                return location
            raise ThetaDataRawStoreError(
                f"{target} no longer hashes to its own name; an attempt body "
                "that changed after it was written is not the response it names"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=str(target.parent), suffix=".part")
        try:
            with open(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                # The same durability the raw records get: a paid capture's
                # failure evidence is not less important than its data.
                os.fsync(stream.fileno())
            pathlib.Path(temporary).replace(target)
        except BaseException:
            pathlib.Path(temporary).unlink(missing_ok=True)
            raise
        return location

    # -- reporting -----------------------------------------------------------

    @property
    def failed(self) -> tuple[HttpAttemptRecord, ...]:
        return tuple(record for record in self.records if not record.succeeded)

    def per_endpoint(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.endpoint] = counts.get(record.endpoint, 0) + 1
        return dict(sorted(counts.items()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": HTTP_ATTEMPT_SCHEMA_VERSION,
            "attempt_count": len(self.records),
            "failed_attempt_count": len(self.failed),
            "attempts_per_endpoint": self.per_endpoint(),
            "bodies_preserved": sum(
                1 for record in self.records if record.response_body_location
            ),
            # Named relative to the attempt-store root, like every location in
            # this report. The root is a directory the operator already has --
            # they are reading a file inside it -- and an absolute path here
            # would be a claim about a machine rather than about the run.
            "attempt_store_root": self.root.name if self.root is not None else "",
            "index_path": self.index_path.name if self.root is not None else "",
            "attempts": [record.as_dict() for record in self.records],
        }
