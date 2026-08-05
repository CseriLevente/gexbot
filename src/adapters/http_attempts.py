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
    "ATTEMPT_EVIDENCE_SCHEMA_VERSION",
    "HTTP_ATTEMPT_SCHEMA_VERSION",
    "INTERPRETIVE_RESPONSE_HEADERS",
    "SAFE_RESPONSE_HEADERS",
    "AttemptEvidenceReport",
    "HttpAttemptLog",
    "HttpAttemptRecord",
    "safe_headers",
]

#: Attempt bodies are stored with this suffix because they are bytes, not text.
#: v2.1.12 used ``.txt`` for a body that had already been through
#: ``errors="replace"``, which was consistent and wrong in the same direction.
ATTEMPT_BODY_SUFFIX = ".bin"

#: Bumped when the *meaning* of an attempt record changes.
HTTP_ATTEMPT_SCHEMA_VERSION = "http-attempt/2.1.16"

#: Response headers worth keeping. Everything else is dropped rather than
#: filtered: an allow-list cannot leak a header nobody thought about, and a
#: deny-list can.
SAFE_RESPONSE_HEADERS = (
    # Content decoding. These decide what the bytes *mean*, so a record without
    # them cannot be replayed under the reading it was captured with.
    "content-encoding",
    "content-length",
    "content-type",
    # Who answered, and when they say they did.
    "date",
    "x-request-id",
    "x-thetadata-request-id",
    # Whether this response is the whole answer. A paginated body that reads as
    # complete is the one failure a coverage claim cannot detect from the rows.
    "link",
    "x-next-page",
    "x-page",
    "x-total-count",
    "x-has-more",
    # Rate-limit diagnostics, which is what an operator needs after a 429.
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
)

#: The subset whose value changes how a stored body is *interpreted*. These go
#: into the raw record's fingerprint: a capture replayed under a different
#: content type is a different reading of the same bytes, and evidence should
#: not be able to change its meaning without changing its identity.
INTERPRETIVE_RESPONSE_HEADERS = (
    "content-encoding",
    "content-type",
)


def safe_headers(headers: Any) -> dict[str, str]:
    """The subset of a response's headers that may be written down."""
    if not headers:
        return {}
    lowered = {str(k).lower(): str(v) for k, v in dict(headers).items()}
    return {name: lowered[name] for name in SAFE_RESPONSE_HEADERS if name in lowered}


#: Bumped when what an attempt-evidence report checks changes.
ATTEMPT_EVIDENCE_SCHEMA_VERSION = "attempt-evidence/2.1.16"


@dataclass(frozen=True, slots=True)
class AttemptEvidenceReport:
    """What a persisted attempt log says about itself, read back off disk.

    Derived, never asserted. Every number here was recomputed from the files
    that are there now, which is the only way a report about evidence can be
    evidence about anything.
    """

    root: pathlib.Path
    findings: tuple[str, ...]
    attempt_count: int
    body_count: int
    #: Digest of the index file exactly as it is on disk. Recorded at
    #: finalization so a later reader can tell whether the log has been
    #: appended to, truncated or rewritten since the capture ended.
    index_hash: str
    schema_version: str = ATTEMPT_EVIDENCE_SCHEMA_VERSION

    @property
    def ok(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "attempt_index_hash": self.index_hash,
            "attempt_count": self.attempt_count,
            "attempt_body_count": self.body_count,
            "ok": self.ok,
            "findings": list(self.findings),
        }


def _schema_findings(entry: dict[str, Any]) -> list[str]:
    """Whether one persisted attempt record has the shape this code reads."""
    where = f"{entry.get('logical_request_id', '?')}#{entry.get('attempt_number', '?')}"
    found: list[str] = []
    version = entry.get("schema_version")
    if version != HTTP_ATTEMPT_SCHEMA_VERSION:
        found.append(
            f"{where}: schema {version!r} is not {HTTP_ATTEMPT_SCHEMA_VERSION!r}"
        )
    for name, kind in (
        ("logical_request_id", str),
        ("attempt_number", int),
        ("endpoint", str),
        ("safe_url", str),
        ("request_parameters_hash", str),
        ("started_at", str),
    ):
        value = entry.get(name)
        if not isinstance(value, kind) or (kind is str and not value):
            found.append(f"{where}: {name} is {value!r}")
    return found


def _fingerprint_findings(entry: dict[str, Any]) -> list[str]:
    """Whether the record still hashes to the fingerprint it carries."""
    where = f"{entry.get('logical_request_id', '?')}#{entry.get('attempt_number', '?')}"
    stated = entry.get("fingerprint")
    if not isinstance(stated, str) or not stated:
        return [f"{where}: no fingerprint"]
    semantic = {k: v for k, v in entry.items() if k != "fingerprint"}
    recomputed = digest_of(semantic)
    if recomputed != stated:
        return [
            f"{where}: fingerprint {stated[:12]}... recomputes to {recomputed[:12]}..."
        ]
    return []


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
        #: Whether the records in memory came from a persisted index. False for
        #: a fresh log, and :meth:`verify_bodies` refuses rather than reporting
        #: no failures -- see the method for why that distinction is the whole
        #: point.
        self._loaded_from_disk = False

    @classmethod
    def create_new(cls, root: pathlib.Path | str) -> HttpAttemptLog:
        """A log for a run that is about to start. Refuses an existing index.

        Explicit since v2.1.16. ``HttpAttemptLog(root)`` was used for both
        "start a log here" and "there is a log here", and the two need opposite
        behaviour: the first must refuse a directory that already has evidence
        in it, the second must load it.
        """
        held = pathlib.Path(root)
        index = held / "index.jsonl"
        if index.exists():
            raise ThetaDataRawStoreError(
                f"{index} already exists, so this directory holds an earlier "
                "run's attempt evidence. An append-only log is not resumed: "
                "open it with open_existing() to read it, or give this run its "
                "own directory."
            )
        return cls(held)

    @classmethod
    def for_reporting(cls) -> HttpAttemptLog:
        """A log that collects records and writes nothing. No root, no files."""
        return cls(None)

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
                #
                # A malformed line that is *not* the last one is a different
                # fact and :meth:`open_existing` reports it as a finding. This
                # method returns what is readable; that one says what is wrong.
                continue
        return tuple(entries)

    @classmethod
    def open_existing(cls, root: pathlib.Path | str) -> AttemptEvidenceReport:
        """Reload a persisted attempt log and check it against itself.

        **The v2.1.15 correction.** ``HttpAttemptLog(root)`` constructs an
        object with an empty in-memory ``records`` list, so
        :meth:`verify_bodies` on a freshly opened log iterated over nothing and
        returned no failures. An archived capture could have every attempt body
        replaced and the check that exists to notice would say it was fine.

        Everything here is derived from disk: the index is parsed, every record
        validated against the schema, every fingerprint recomputed, every body
        located, hashed and measured, and orphaned body files -- present on
        disk, named by no record -- are reported too.
        """
        root_path = pathlib.Path(root)
        index = root_path / "index.jsonl"
        findings: list[str] = []
        entries: list[dict[str, Any]] = []

        if not index.is_file():
            return AttemptEvidenceReport(
                root=root_path,
                findings=(f"{index} does not exist",),
                attempt_count=0,
                body_count=0,
                index_hash="",
            )

        raw = index.read_text(encoding="utf-8")
        lines = raw.splitlines()
        for number, line in enumerate(lines, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                entries.append(json.loads(text))
            except json.JSONDecodeError as error:
                if number == len(lines) and not raw.endswith("\n"):
                    # The last line of a file that does not end in a newline:
                    # an append interrupted mid-write. Everything before it
                    # still counts, which is what append-only buys.
                    findings.append(f"line {number}: torn final line ({error})")
                else:
                    # A malformed line in the *middle* means a completed write
                    # was later damaged. Silently skipping it -- which is what
                    # v2.1.14 did -- turns tampering into a shorter list.
                    findings.append(f"line {number}: malformed, not the final line")
                continue

        expected_bodies: set[pathlib.Path] = set()
        for entry in entries:
            findings.extend(_schema_findings(entry))
            findings.extend(_fingerprint_findings(entry))
            location = entry.get("response_body_location")
            if not location:
                continue
            path = (
                pathlib.Path(location)
                if pathlib.Path(location).is_absolute()
                else root_path / location
            )
            expected_bodies.add(path.resolve())
            digest = str(entry.get("response_body_hash") or "")
            if not path.is_file():
                findings.append(f"{location}: body is missing")
                continue
            payload = path.read_bytes()
            actual = hashlib.sha256(payload).hexdigest()
            if digest and actual != digest:
                findings.append(f"{location}: body hashes to {actual[:12]}...")
            length = entry.get("response_byte_length")
            if isinstance(length, int) and length != len(payload):
                findings.append(
                    f"{location}: {len(payload)} bytes, record says {length}"
                )

        for path in sorted(root_path.rglob(f"*{ATTEMPT_BODY_SUFFIX}")):
            if path.resolve() not in expected_bodies:
                findings.append(
                    f"{path.relative_to(root_path).as_posix()}: orphaned body, "
                    "no attempt record names it"
                )

        return AttemptEvidenceReport(
            root=root_path,
            findings=tuple(findings),
            attempt_count=len(entries),
            body_count=len(expected_bodies),
            index_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )

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
        """Which stored attempt bodies no longer hash to their own filenames.

        **Refuses a log it did not write and did not load.** This iterated over
        ``self.records``, which is empty on a freshly constructed log -- so
        ``HttpAttemptLog(archived_root).verify_bodies() == ()`` reported no
        failures for a directory it had never read. Every attempt body in an
        archived capture could have been replaced and the check that exists to
        notice said it was fine.

        An empty answer now means "nothing is wrong", never "I looked at
        nothing". To check a persisted log, use :meth:`open_existing`.
        """
        if self.root is not None and not self.records and not self._loaded_from_disk:
            index = self.root / "index.jsonl"
            if index.exists():
                raise ThetaDataRawStoreError(
                    f"{index} exists but this log has not loaded it, so there is "
                    "nothing here to verify and an empty result would be false "
                    "reassurance. Use HttpAttemptLog.open_existing(root)."
                )
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
