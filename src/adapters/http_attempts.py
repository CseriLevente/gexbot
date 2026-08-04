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
import pathlib
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.domain.digests import digest_of

__all__ = [
    "HTTP_ATTEMPT_SCHEMA_VERSION",
    "SAFE_RESPONSE_HEADERS",
    "HttpAttemptLog",
    "HttpAttemptRecord",
    "safe_headers",
]

#: Bumped when the *meaning* of an attempt record changes.
HTTP_ATTEMPT_SCHEMA_VERSION = "http-attempt/2.1.12"

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
    one. A transport failure produces a record with no status and no body and a
    ``transport_error_code``, which is a different thing from a 500 and reads as
    one.
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

    @property
    def succeeded(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300

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

    def observe(self, record: HttpAttemptRecord, body: str | None = None) -> None:
        """Record one attempt, storing its body when there is one."""
        from dataclasses import replace

        if body is None:
            self.records.append(record)
            return
        payload = body.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        location = self._write_body(payload, digest) if self.root is not None else None
        self.records.append(
            replace(
                record,
                response_body_hash=digest,
                response_body_location=location,
                response_byte_length=len(payload),
            )
        )

    def _write_body(self, payload: bytes, digest: str) -> str:
        """Content-addressed, atomically. Two identical bodies are one file."""
        assert self.root is not None
        target = self.root / digest[:2] / f"{digest}.txt"
        if target.exists():
            return str(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=str(target.parent), suffix=".part")
        try:
            with open(handle, "wb") as stream:
                stream.write(payload)
            pathlib.Path(temporary).replace(target)
        except BaseException:
            pathlib.Path(temporary).unlink(missing_ok=True)
            raise
        return str(target)

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
            "attempts": [record.as_dict() for record in self.records],
        }
