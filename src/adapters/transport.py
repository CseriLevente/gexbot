"""HTTP transport abstraction for vendor adapters.

Split from the ThetaData client so that:

* unit tests run against a deterministic fake with **no network access at all**;
* the real transport's retry, timeout and rate-limit behaviour is testable in
  isolation, without a vendor account;
* swapping ``httpx`` for something else is one file.

Retry policy notes, since these are the decisions that matter operationally:

* **Bounded.** ``max_retries`` is a hard cap. An unbounded retry loop against a
  rate-limited vendor is how one slow morning turns into a ban.
* **Only idempotent failures are retried.** 5xx, 429 and transport errors, yes.
  4xx other than 429, no -- a malformed request will stay malformed.
* **Jittered backoff.** Deterministic backoff synchronises every client in a
  fleet onto the same retry instants. The jitter source is injectable so tests
  stay deterministic.
* **Response size is capped.** A full SPX chain is large but bounded; an
  unbounded read is a memory-exhaustion path driven by a remote party.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from src.adapters.errors import ThetaDataResponseTooLargeError

logger = logging.getLogger(__name__)

# 64 MiB. A full SPX+SPXW chain CSV is a few MiB; this leaves headroom without
# allowing an unbounded read.
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024

RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class TransportError(RuntimeError):
    """Network-level failure: connection refused, DNS, timeout, TLS."""


class VendorHTTPError(RuntimeError):
    """Non-2xx response.

    Carries the safe response metadata a caller needs to decide what to do --
    including ``retry_after``, which v2 documented but never retained.
    """

    def __init__(
        self,
        status_code: int,
        url: str,
        body_excerpt: str,
        *,
        headers: Mapping[str, str] | None = None,
        request_id: str = "",
        vendor_error_code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        # The URL is redacted before it reaches the message: an exception string
        # ends up in logs and tracebacks, and a credential in a query parameter
        # would travel with it.
        super().__init__(f"HTTP {status_code} from {_redact(url)}: {body_excerpt}")
        self.status_code = status_code
        self.url = url
        self.body_excerpt = body_excerpt
        self.headers = dict(headers or {})
        self.request_id = request_id
        self.vendor_error_code = vendor_error_code
        self.retry_after = retry_after


#: Aliased onto the adapter hierarchy. The cap is the caller's own setting, so
#: the failure it produces has to be catchable with the adapter's base class.
ResponseTooLargeError = ThetaDataResponseTooLargeError


class RetryBudgetExhaustedError(RuntimeError):
    def __init__(self, attempts: int, last_error: Exception) -> None:
        super().__init__(f"giving up after {attempts} attempt(s): {last_error}")
        self.attempts = attempts
        self.last_error = last_error


_SENSITIVE_PARAM_NAMES = ("password", "token", "key", "secret", "auth")


def _endpoint_of(url: str) -> str:
    """The path an attempt was made against, which is what the plan names."""
    without_query = url.partition("?")[0]
    for marker in ("://",):
        if marker in without_query:
            without_query = "/" + without_query.split(marker, 1)[1].partition("/")[2]
    return without_query


def _parameters_hash(params: Mapping[str, Any]) -> str:
    """Which request an attempt was, without recording what was in it.

    The parameters can carry a rate value and a symbol, neither secret; they can
    also carry whatever a future endpoint adds. A digest names the request
    without deciding in advance that everything in it is safe to write down.
    """
    import hashlib
    import json

    canonical = json.dumps(
        {str(k): str(v) for k, v in sorted(dict(params).items())},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _redact(url: str) -> str:
    """Strip credential-looking query parameters before anything is logged.

    Credentials in a URL are a common accident; a log line is forever.
    """
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    parts = []
    for pair in query.split("&"):
        name, _, _ = pair.partition("=")
        if any(marker in name.lower() for marker in _SENSITIVE_PARAM_NAMES):
            parts.append(f"{name}=***")
        else:
            parts.append(pair)
    return f"{base}?{'&'.join(parts)}"


def local_or_live_origin(url: str) -> str:
    """``LOCAL_TERMINAL_CAPTURE`` for a loopback host, ``LIVE_HTTP_CAPTURE`` else.

    Only the host is inspected. A path or a query parameter is remote-supplied
    text, and letting it decide where a capture came from would let the answer be
    written by the thing being described.

    An unparseable URL is ``LIVE_HTTP_CAPTURE``: the two are both live, and
    guessing "local" would understate what a request did.
    """
    import ipaddress
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url).hostname or "").strip().lower()
    except ValueError:
        return "LIVE_HTTP_CAPTURE"
    if not host:
        return "LIVE_HTTP_CAPTURE"
    if host == "localhost":
        return "LOCAL_TERMINAL_CAPTURE"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "LIVE_HTTP_CAPTURE"
    return "LOCAL_TERMINAL_CAPTURE" if address.is_loopback else "LIVE_HTTP_CAPTURE"


#: What ``HttpResponse.body`` holds, stated so nobody has to guess later.
#:
#: **The HTTP entity body after transfer- and content-decoding**: what ``httpx``
#: yields from ``iter_bytes()``, so a gzip-compressed response is stored
#: decompressed. Not the compressed wire bytes. That is the layer a parser reads
#: and the layer a re-derivation has to reproduce; preserving the compressed
#: form would mean re-running a decompressor to check a hash, and two
#: implementations of that decompressor is a second thing to be wrong.
BODY_REPRESENTATION = "http-entity-body-after-content-decoding"


class DecodeStatus(str, Enum):
    """How cleanly a byte body became text."""

    #: Decoded with the selected charset, no substitutions.
    EXACT = "EXACT"
    #: Undecodable sequences were replaced. The bytes are still exact; the
    #: *text* is not, and anything derived from the text says so.
    REPLACED = "REPLACED"
    #: The body was supplied as text, so there is nothing to decode.
    SUPPLIED_AS_TEXT = "SUPPLIED_AS_TEXT"


@dataclass(frozen=True, slots=True)
class DecodedBody:
    """A byte body and the reading of it, with both digests.

    v2.1.12 decoded with ``errors="replace"`` inside the transport and threw the
    bytes away, and the store then re-encoded the *text* as UTF-8. A response
    containing one invalid byte was stored as a U+FFFD, hashed as a U+FFFD, and
    the hash was described as the hash of the vendor's response. It was the hash
    of our reading of it.
    """

    text: str
    body_hash: str
    byte_length: int
    decoded_text_hash: str
    content_type: str = ""
    declared_charset: str = ""
    selected_charset: str = "utf-8"
    decode_status: DecodeStatus = DecodeStatus.EXACT

    def as_dict(self) -> dict[str, Any]:
        return {
            "body_representation": BODY_REPRESENTATION,
            "body_hash": self.body_hash,
            "byte_length": self.byte_length,
            "decoded_text_hash": self.decoded_text_hash,
            "content_type": self.content_type,
            "declared_charset": self.declared_charset,
            "selected_charset": self.selected_charset,
            "decode_status": self.decode_status.value,
        }


def _charset_of(headers: Mapping[str, str]) -> tuple[str, str]:
    """``(content_type, declared_charset)`` from the headers, lowercased."""
    for key, value in dict(headers or {}).items():
        if str(key).lower() != "content-type":
            continue
        text = str(value)
        charset = ""
        for part in text.split(";")[1:]:
            name, _, held = part.partition("=")
            if name.strip().lower() == "charset":
                charset = held.strip().strip('"').lower()
        return text.split(";")[0].strip().lower(), charset
    return "", ""


def decode_body(body: bytes, headers: Mapping[str, str] | None = None) -> DecodedBody:
    """Read a byte body as text, recording exactly how.

    The charset comes from the response's own ``Content-Type`` where it states
    one, and UTF-8 otherwise -- which is what ThetaData sends. A charset the
    interpreter does not know falls back to UTF-8 and says so through the
    selected charset, rather than raising in the middle of a paid capture.
    """
    content_type, declared = _charset_of(headers or {})
    selected = declared or "utf-8"
    try:
        text = body.decode(selected)
        status = DecodeStatus.EXACT
    except (UnicodeDecodeError, LookupError):
        text = body.decode("utf-8", errors="replace")
        selected = "utf-8"
        status = DecodeStatus.REPLACED
    return DecodedBody(
        text=text,
        body_hash=hashlib.sha256(body).hexdigest(),
        byte_length=len(body),
        decoded_text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        content_type=content_type,
        declared_charset=declared,
        selected_charset=selected,
        decode_status=status,
    )


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One vendor response, bytes first.

    ``body`` is authoritative and ``text`` is a reading of it. Both are fields so
    that a fixture can still be written as ``HttpResponse(status_code=200,
    text="a,b\\n1,2\\n")`` -- ``__post_init__`` fills in whichever was not
    supplied, and a caller who supplies text is recorded as having done so.
    """

    status_code: int
    text: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    url: str = ""
    request_id: str = ""
    elapsed_seconds: float = 0.0
    attempts: int = 1
    #: Vendor error code parsed from the body, when the vendor supplies one.
    vendor_error_code: str | None = None
    #: The entity body after content decoding. See :data:`BODY_REPRESENTATION`.
    body: bytes = b""
    _supplied_as_text: bool = field(default=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.body:
            if not self.text:
                object.__setattr__(
                    self, "text", decode_body(self.body, self.headers).text
                )
            return
        object.__setattr__(self, "body", self.text.encode("utf-8"))
        object.__setattr__(self, "_supplied_as_text", True)

    def decode_text(self) -> DecodedBody:
        """The typed reading of this response's bytes."""
        decoded = decode_body(self.body, self.headers)
        if self._supplied_as_text:
            return replace(decoded, decode_status=DecodeStatus.SUPPLIED_AS_TEXT)
        return decoded

    @property
    def retry_after_seconds(self) -> float | None:
        """The vendor's own instruction, when it sent one.

        v2 documented Retry-After support without retaining headers, so the
        claim was unbacked. Headers are now carried on the response and the
        header is genuinely honoured.
        """
        for key, value in self.headers.items():
            if key.lower() == "retry-after":
                return parse_retry_after(value)
        return None

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def byte_length(self) -> int:
        """The size of the *body*. v2.1.12 measured a re-encoding of the text."""
        return len(self.body)


def parse_retry_after(
    value: str | None, *, now: datetime | None = None
) -> float | None:
    """Parse a ``Retry-After`` header. ``None`` when absent or unusable.

    Both documented forms are supported: delta-seconds (``"120"``) and an
    HTTP-date (``"Wed, 21 Oct 2026 07:28:00 GMT"``). An unparseable or negative
    value returns ``None`` so the caller falls back to its own backoff rather
    than trusting a malformed header.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return seconds if seconds >= 0.0 else None

    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    delta = (parsed - reference).total_seconds()
    return max(0.0, delta)


@runtime_checkable
class HttpTransport(Protocol):
    """The seam between an adapter and the network."""

    def get(
        self,
        url: str,
        params: Mapping[str, Any],
        timeout_seconds: float,
    ) -> HttpResponse: ...


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 3
    backoff_base_seconds: float = 0.25
    backoff_max_seconds: float = 8.0
    #: Hard ceiling on an honoured Retry-After. Without it, a remote party
    #: decides how long this process blocks.
    max_retry_after_seconds: float = 120.0
    # Full jitter: sleep is drawn from [0, computed_backoff]. Prevents a fleet of
    # clients from retrying in lockstep after a shared outage.
    jitter: bool = True

    def delay_for(self, attempt: int, *, random_unit: float) -> float:
        """Backoff before ``attempt`` (1-based retry index).

        ``random_unit`` in [0, 1) is injected rather than drawn internally so
        tests are deterministic and the policy stays a pure function.
        """
        exponential: float = self.backoff_base_seconds * (2 ** (attempt - 1))
        capped: float = min(exponential, self.backoff_max_seconds)
        return capped * random_unit if self.jitter else capped


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """What the fake transport saw. Used to assert on request shape."""

    url: str
    params: dict[str, Any]
    timeout_seconds: float


class FakeTransport:
    """Deterministic in-memory transport. Never touches the network.

    Declares its own origin, so a capture taken through it is labelled an
    offline fixture in the manifest and in the manifest hash. v2.1.5 carried
    ``live_capture = False`` as a constant on the validation report, which was
    the right answer and not an *answer*: it would have stayed False through the
    first real session.

    Responses are keyed by a substring of the URL path so a test can register
    "whatever hits /v3/option/snapshot/quote" without reproducing the exact query
    string.
    """

    #: Nothing this transport returns is evidence about the vendor.
    capture_origin = "OFFLINE_FIXTURE"

    def __init__(
        self,
        routes: Mapping[str, HttpResponse | Exception] | None = None,
        *,
        default: HttpResponse | Exception | None = None,
    ) -> None:
        self._routes: dict[str, HttpResponse | Exception] = dict(routes or {})
        self._default = default
        self.calls: list[RecordedCall] = []
        # Per-route queues, so a test can make the first attempt fail and the
        # second succeed.
        self._sequences: dict[str, list[HttpResponse | Exception]] = {}

    def register(self, path_fragment: str, response: HttpResponse | Exception) -> None:
        self._routes[path_fragment] = response

    def register_sequence(
        self, path_fragment: str, responses: list[HttpResponse | Exception]
    ) -> None:
        self._sequences[path_fragment] = list(responses)

    def register_text(
        self, path_fragment: str, text: str, *, status_code: int = 200
    ) -> None:
        self.register(path_fragment, HttpResponse(status_code=status_code, text=text))

    def register_bytes(
        self, path_fragment: str, body: bytes, *, status_code: int = 200, **headers: str
    ) -> None:
        """Answer with exactly these bytes, whatever they decode to."""
        self.register(
            path_fragment,
            HttpResponse(status_code=status_code, body=body, headers=dict(headers)),
        )

    def registered_text(self, path_fragment: str) -> str | None:
        """The body registered for a route, or ``None``.

        So a fixture can capture the same bytes this transport would answer
        with, rather than keeping a second copy that can drift out of step.
        """
        response = self._routes.get(path_fragment)
        return response.text if isinstance(response, HttpResponse) else None

    def get(
        self, url: str, params: Mapping[str, Any], timeout_seconds: float
    ) -> HttpResponse:
        self.calls.append(
            RecordedCall(url=url, params=dict(params), timeout_seconds=timeout_seconds)
        )
        for fragment, queue in self._sequences.items():
            if fragment in url and queue:
                return _unwrap(queue.pop(0), url)
        for fragment, response in self._routes.items():
            if fragment in url:
                return _unwrap(response, url)
        if self._default is not None:
            return _unwrap(self._default, url)
        raise AssertionError(
            f"FakeTransport has no route for {url!r}. Register one with "
            "register()/register_text() -- unit tests must never reach the network."
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def urls(self) -> list[str]:
        return [call.url for call in self.calls]

    def timeouts(self) -> list[float]:
        """Per-call read timeouts, so a test can prove the configured value
        reached the wire rather than merely reaching a dataclass."""
        return [call.timeout_seconds for call in self.calls]


@dataclass(frozen=True, slots=True)
class ConsumedRecord:
    """One stored record, as normalization actually consumed it."""

    record_id: str
    endpoint: str
    serve_order: int
    parameters: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "endpoint": self.endpoint,
            "serve_order": self.serve_order,
            "parameters": dict(self.parameters),
        }


class StoredPayloadTransport:
    """Answers from a verified capture instead of from the vendor.

    This is what makes a normalized chain *re-derivable*. Verification proves
    things about raw records; the chain a caller hands to a trusted calculation
    is the result of parsing and joining them, and until v2.1.7 nothing
    connected the two. Rebuilding the chain by replaying the stored bytes
    through the ordinary fetch path is the connection.

    Replaying through the *same* code matters more than it looks. A second
    implementation of normalization -- a "rebuilder" that re-read the CSVs
    independently -- would be a second source of truth, and the two would drift.
    Here the client, the parser, the join and the model are the production ones;
    only the bytes come from a different place.

    Records for one endpoint are replayed in capture order, so a session that
    pulled an endpoint twice replays both, in the order it received them.
    """

    #: Not a live capture, and cannot be mistaken for one. Rebuilding never
    #: writes to a store, but if a caller wires this to one, its records will
    #: say what they are.
    capture_origin = "OFFLINE_FIXTURE"

    def __init__(
        self,
        responses: Mapping[str, list[str]],
        *,
        record_ids: Mapping[str, list[str]] | None = None,
    ) -> None:
        self._queues = {
            endpoint: list(bodies) for endpoint, bodies in responses.items()
        }
        self._record_ids = {
            endpoint: list(ids) for endpoint, ids in (record_ids or {}).items()
        }
        self._served: dict[str, int] = {}
        self.calls: list[RecordedCall] = []
        #: Which stored records normalization actually consumed, in order, with
        #: the parameters each was served against. v2.1.7 replayed a capture and
        #: never asked whether the replay used *all* of it -- so a capture with
        #: one extra quote response replayed from the first, matched the
        #: original, and verified. Bytes nobody consumed are bytes nobody
        #: checked, sitting inside a manifest that claims they produced the
        #: number.
        self.consumed: list[ConsumedRecord] = []

    @classmethod
    def from_capture(cls, *, manifest: Any, store: Any) -> StoredPayloadTransport:
        """Every payload the manifest names, keyed by the endpoint that served it.

        Ordered by the capture sequence rather than by record id, because the
        order responses arrived in is part of what the session did.
        """
        records = {record.record_id: record for record in store.records()}
        by_endpoint: dict[str, list[tuple[int, str, str]]] = {}
        for entry in manifest.records:
            record = records.get(entry.record_id)
            if record is None:
                continue
            by_endpoint.setdefault(record.endpoint, []).append(
                (
                    record.request_sequence,
                    store.get_payload(entry.record_id),
                    entry.record_id,
                )
            )
        ordered = {endpoint: sorted(items) for endpoint, items in by_endpoint.items()}
        return cls(
            {
                endpoint: [payload for _, payload, _ in items]
                for endpoint, items in ordered.items()
            },
            record_ids={
                endpoint: [record_id for _, _, record_id in items]
                for endpoint, items in ordered.items()
            },
        )

    def get(
        self, url: str, params: Mapping[str, Any], timeout_seconds: float
    ) -> HttpResponse:
        self.calls.append(
            RecordedCall(url=url, params=dict(params), timeout_seconds=timeout_seconds)
        )
        for endpoint, bodies in self._queues.items():
            if endpoint not in url:
                continue
            index = self._served.get(endpoint, 0)
            if index >= len(bodies):
                # The capture holds fewer responses for this endpoint than the
                # rebuild is asking for. Reusing the last one would silently
                # fabricate a response the session never received.
                raise TransportError(
                    f"the capture holds {len(bodies)} response(s) for {endpoint} "
                    f"and the rebuild asked for {index + 1}. A chain rebuilt "
                    "from responses that were not captured is not the chain the "
                    "capture produced."
                )
            self._served[endpoint] = index + 1
            served_ids = self._record_ids.get(endpoint, [])
            self.consumed.append(
                ConsumedRecord(
                    record_id=(served_ids[index] if index < len(served_ids) else ""),
                    endpoint=endpoint,
                    serve_order=len(self.consumed),
                    parameters=tuple(
                        sorted((str(k), str(v)) for k, v in params.items())
                    ),
                )
            )
            return HttpResponse(
                status_code=200, text=bodies[index], url=url, request_id="replay"
            )
        raise TransportError(
            f"the capture holds no response for {url!r}. Rebuilding a chain "
            "requires every endpoint the session used."
        )

    @property
    def endpoints(self) -> tuple[str, ...]:
        return tuple(sorted(self._queues))


def _unwrap(item: HttpResponse | Exception, url: str) -> HttpResponse:
    if isinstance(item, Exception):
        raise item
    return HttpResponse(
        status_code=item.status_code,
        text=item.text,
        headers=item.headers,
        url=url or item.url,
        request_id=item.request_id or "fake",
        elapsed_seconds=item.elapsed_seconds,
        attempts=item.attempts,
        # The registered bytes, not a re-encoding of the registered text: a
        # fixture that registers non-UTF-8 bytes must deliver them unchanged.
        body=item.body,
    )


class RetryingTransport:
    """Wraps any transport with bounded retries, backoff and structured logging.

    Separated from the concrete HTTP client so the retry semantics can be tested
    against :class:`FakeTransport` with no network and no real sleeping.
    """

    def __init__(
        self,
        inner: HttpTransport,
        *,
        policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_unit: Callable[[], float] = lambda: 0.5,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        attempt_observer: Any = None,
    ) -> None:
        self._inner = inner
        self._policy = policy or RetryPolicy()
        self._sleep = sleep
        self._random_unit = random_unit
        self._max_response_bytes = max_response_bytes
        #: Told about every attempt, successful or not. Until v2.1.12 a retryable
        #: 429 or 503 body was logged and dropped inside this loop, so the
        #: responses that would explain a partial capture were exactly the ones
        #: the capture layer never saw -- while the operator documentation said
        #: every response was preserved.
        self._attempt_observer = attempt_observer
        self.sleeps: list[float] = []

    def origin_for(self, url: str) -> Any:
        """Delegate. A wrapper adds no provenance and hides none either."""
        inner = getattr(self._inner, "origin_for", None)
        if callable(inner):
            return inner(url)
        return getattr(self._inner, "capture_origin", None)

    def close(self) -> None:
        """Close the wrapped transport, if it has anything to close."""
        closer = getattr(self._inner, "close", None)
        if callable(closer):
            closer()

    def _observe(
        self,
        *,
        url: str,
        params: Mapping[str, Any],
        request_id: str,
        attempt: int,
        started_at: datetime,
        status_code: int | None = None,
        headers: Any = None,
        body: bytes | None = None,
        transport_error_code: str | None = None,
        retryable: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self._attempt_observer is None:
            return
        from src.adapters.http_attempts import HttpAttemptRecord, safe_headers

        self._attempt_observer.observe(
            HttpAttemptRecord(
                logical_request_id=request_id,
                attempt_number=attempt,
                endpoint=_endpoint_of(url),
                safe_url=_redact(url),
                request_parameters_hash=_parameters_hash(params),
                started_at=started_at,
                received_at=datetime.now(UTC),
                status_code=status_code,
                response_headers=safe_headers(headers),
                transport_error_code=transport_error_code,
                retryable=retryable,
                detail=dict(extra or {}),
            ),
            body,
        )

    @property
    def inner(self) -> HttpTransport:
        """The transport that actually fetches. Retrying is not an origin.

        ``build_thetadata_client`` always wraps, so every production capture
        looks at *this* object when it asks where its bytes came from. Without
        this, ``capture_origin_of`` found no ``capture_origin`` on the wrapper
        and stamped ``UNKNOWN_ORIGIN`` on every record of every real session --
        which reads as "not live" and is not the same statement as "offline
        fixture".
        """
        return self._inner

    @property
    def capture_origin(self) -> Any:
        """Whatever the wrapped transport says. A wrapper adds no provenance."""
        return getattr(self._inner, "capture_origin", None)

    def get(
        self, url: str, params: Mapping[str, Any], timeout_seconds: float
    ) -> HttpResponse:
        request_id = uuid.uuid4().hex[:12]
        last_error: Exception | None = None
        retry_after: float | None = None

        for attempt in range(1, self._policy.max_retries + 2):
            started = time.monotonic()
            started_at = datetime.now(UTC)
            try:
                response = self._inner.get(url, params, timeout_seconds)
            except ResponseTooLargeError as exc:
                # The streaming reader aborted mid-body. There is no response to
                # describe, and the cap and the bytes read are the finding.
                self._observe(
                    url=url,
                    params=params,
                    request_id=request_id,
                    attempt=attempt,
                    started_at=started_at,
                    transport_error_code="RESPONSE_TOO_LARGE",
                    extra={
                        "configured_max_response_bytes": self._max_response_bytes,
                        "bytes_read_before_abort": getattr(exc, "bytes_read", None),
                    },
                )
                raise
            except TransportError as exc:
                last_error = exc
                logger.warning(
                    "vendor request failed",
                    extra={
                        "request_id": request_id,
                        "url": _redact(url),
                        "attempt": attempt,
                        "error": type(exc).__name__,
                    },
                )
                # No status and no body: a connection that never answered is a
                # different fact from a 500, and the record says which.
                self._observe(
                    url=url,
                    params=params,
                    request_id=request_id,
                    attempt=attempt,
                    started_at=started_at,
                    transport_error_code=type(exc).__name__,
                    retryable=True,
                )
            else:
                elapsed = time.monotonic() - started
                # Belt and braces: streaming transports abort mid-read, but a
                # non-streaming one (the fake, or a future implementation) is
                # still checked here so an oversized payload can never be parsed.
                if response.byte_length > self._max_response_bytes:
                    # Recorded before it is raised. v2.1.12 raised from here and
                    # from ``ByteLimitedReader``, so the attempt log reported
                    # zero attempts for a request that had definitely been made
                    # -- the one failure mode where the size of the thing is the
                    # whole finding.
                    self._observe(
                        url=url,
                        params=params,
                        request_id=request_id,
                        attempt=attempt,
                        started_at=started_at,
                        status_code=response.status_code,
                        headers=response.headers,
                        transport_error_code="RESPONSE_TOO_LARGE",
                        extra={
                            "configured_max_response_bytes": (self._max_response_bytes),
                            "bytes_read_before_abort": response.byte_length,
                        },
                    )
                    raise ResponseTooLargeError(
                        f"response of {response.byte_length} bytes exceeds the "
                        f"{self._max_response_bytes}-byte cap for {_redact(url)}"
                    )
                self._observe(
                    url=url,
                    params=params,
                    request_id=request_id,
                    attempt=attempt,
                    started_at=started_at,
                    status_code=response.status_code,
                    headers=response.headers,
                    body=response.body,
                    retryable=response.status_code in RETRYABLE_STATUS_CODES,
                )
                if response.ok:
                    logger.info(
                        "vendor request ok",
                        extra={
                            "request_id": request_id,
                            "url": _redact(url),
                            "attempt": attempt,
                            "status": response.status_code,
                            "bytes": response.byte_length,
                            "elapsed_seconds": round(elapsed, 4),
                        },
                    )
                    return HttpResponse(
                        status_code=response.status_code,
                        text=response.text,
                        headers=response.headers,
                        url=url,
                        request_id=request_id,
                        elapsed_seconds=elapsed,
                        attempts=attempt,
                        body=response.body,
                    )
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    # A malformed request stays malformed; retrying only wastes
                    # the vendor's rate limit and delays the error.
                    raise VendorHTTPError(
                        response.status_code,
                        url,
                        response.text[:500],
                        headers=response.headers,
                        request_id=request_id,
                        vendor_error_code=response.vendor_error_code,
                    )
                last_error = VendorHTTPError(
                    response.status_code,
                    url,
                    response.text[:500],
                    headers=response.headers,
                    request_id=request_id,
                    vendor_error_code=response.vendor_error_code,
                    retry_after=response.retry_after_seconds,
                )
                retry_after = response.retry_after_seconds
                logger.warning(
                    "vendor request retryable failure",
                    extra={
                        "request_id": request_id,
                        "url": _redact(url),
                        "attempt": attempt,
                        "status": response.status_code,
                    },
                )

            if attempt > self._policy.max_retries:
                break
            delay = self._retry_delay(attempt, last_error, retry_after)
            self.sleeps.append(delay)
            self._sleep(delay)

        assert last_error is not None  # loop always sets it before breaking
        raise RetryBudgetExhaustedError(self._policy.max_retries + 1, last_error)

    def _retry_delay(
        self, attempt: int, last_error: Exception | None, retry_after: float | None
    ) -> float:
        """Honour the vendor's Retry-After when it gives one; back off otherwise.

        Capped: an unbounded wait taken from a remote header is a remote party
        deciding how long this process blocks. Never below our own computed
        backoff either -- the vendor's hint is a floor, not a licence to hammer.
        """
        computed = self._policy.delay_for(attempt, random_unit=self._random_unit())
        if retry_after is not None:
            # A floor, never a reduction. v2.1.12 returned the header value
            # bounded below by ``backoff_base_seconds`` -- which is the *first*
            # delay, so on attempt four a ``Retry-After: 1`` shortened a computed
            # eight-second backoff to one second. The vendor asking us to wait
            # longer is information; the vendor asking us to hammer sooner is not.
            return min(
                max(retry_after, computed),
                self._policy.max_retry_after_seconds,
            )
        if isinstance(last_error, VendorHTTPError) and last_error.status_code == 429:
            return max(computed, self._policy.backoff_base_seconds)
        return computed


class ByteLimitedReader:
    """Reads a chunked body and stops the moment the cap is crossed.

    v2.1 enforced ``max_response_bytes`` in :class:`RetryingTransport`, which
    receives an ``HttpResponse`` -- an object whose body has *already* been read
    into memory. The cap therefore protected the parser from a large payload but
    did nothing about the payload itself: by the time it fired, the bytes were
    resident. On a vendor response large enough to matter, that is precisely the
    failure the limit exists to prevent.

    This reader is the authoritative limit. The retry layer keeps its own check
    as defence in depth for custom transports that buffer, but it is no longer
    the first line.

    On abort the partial body is discarded rather than returned: a truncated CSV
    that parses cleanly is worse than no CSV at all.
    """

    def __init__(self, *, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError(f"max_bytes must be positive, got {max_bytes}")
        self.max_bytes = max_bytes
        self.bytes_read = 0
        self.aborted = False

    def read(self, stream: Any) -> bytes:
        """The body, as bytes.

        v2.1.12 decoded here with ``errors="replace"`` and returned a string, so
        the only representation that ever left this method was already lossy --
        and the store then re-encoded that string as UTF-8 and called the digest
        the hash of the vendor's response. Decoding is now a separate, recorded
        step; this returns what arrived.
        """
        chunks: list[bytes] = []
        for chunk in stream:
            self.bytes_read += len(chunk)
            if self.bytes_read > self.max_bytes:
                self.aborted = True
                chunks.clear()
                self._close(stream)
                error = ResponseTooLargeError(
                    f"response exceeded the {self.max_bytes}-byte cap while "
                    f"streaming; aborted after {self.bytes_read} bytes and "
                    "discarded the partial body"
                )
                error.bytes_read = self.bytes_read  # type: ignore[attr-defined]
                error.max_bytes = self.max_bytes  # type: ignore[attr-defined]
                raise error
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _close(stream: Any) -> None:
        closer = getattr(stream, "close", None)
        if callable(closer):
            closer()


class HttpxTransport:  # pragma: no cover - exercised only against a live vendor
    """Real HTTP transport. Imports ``httpx`` lazily.

    Not covered by unit tests on purpose: covering it would mean either mocking
    ``httpx`` internals (testing the mock, not the code) or making a network call
    (which unit tests must never do). Its retry, redaction and size-cap behaviour
    lives in :class:`RetryingTransport`, which *is* covered.

    Response bodies are read **as a stream** and aborted the moment the running
    byte count exceeds the cap, so an oversized payload is never fully held in
    memory and never reaches a parser.
    """

    #: A real round trip, which is what makes a capture live. Whether it reaches
    #: the vendor directly or through a local Theta Terminal is decided per
    #: request from the URL: both are live, and they fail differently.
    capture_origin = "LIVE_HTTP_CAPTURE"

    @staticmethod
    def origin_for(url: str) -> str:
        """Local Theta Terminal or remote vendor, decided by the parsed host.

        v2.1.13 looked for ``"127.0.0.1"`` or ``"localhost"`` anywhere in the
        URL. ``https://notlocalhost.com/v3/...`` was therefore a local terminal,
        and so was any vendor URL with ``?next=localhost`` in the query -- which
        is a claim about who produced the bytes, made by a string nobody
        controls. ``127.0.0.10`` matched too, and it is a different host.
        """
        return local_or_live_origin(url)

    def __init__(
        self,
        *,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 30.0,
        headers: Mapping[str, str] | None = None,
        basic_auth: tuple[str, str] | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "HttpxTransport needs the 'http' extra: pip install -e '.[http]'"
            ) from exc
        self._httpx = httpx
        self._max_response_bytes = max_response_bytes
        self._connect_timeout_seconds = connect_timeout_seconds
        self._read_timeout_seconds = read_timeout_seconds
        # The one authoritative timeout object. Kept so a per-request read
        # timeout can be expressed *without* discarding the connect timeout --
        # which is what v2.1.13 did: ``get()`` passed ``timeout=<float>``, and a
        # scalar replaces every dimension, so the configured five-second connect
        # timeout became thirty at the wire while the dry run reported five.
        self._timeout = self._timeout_for(read_timeout_seconds)
        # Credentials go to httpx's auth handling, never into the URL: a URL ends
        # up in logs, tracebacks and the raw-response index.
        self._client = httpx.Client(
            timeout=self._timeout,
            headers=dict(headers or {}),
            auth=httpx.BasicAuth(*basic_auth) if basic_auth else None,
        )

    def _timeout_for(self, read_timeout_seconds: float) -> Any:
        """A full timeout object. Every dimension named, none inherited."""
        return self._httpx.Timeout(
            connect=self._connect_timeout_seconds,
            read=read_timeout_seconds,
            write=read_timeout_seconds,
            pool=self._connect_timeout_seconds,
        )

    @property
    def effective_timeout(self) -> Any:
        """What this transport will actually apply. Read by the tests."""
        return self._timeout

    def get(
        self, url: str, params: Mapping[str, Any], timeout_seconds: float
    ) -> HttpResponse:
        try:
            with self._client.stream(
                "GET",
                url,
                params=dict(params),
                # A full ``Timeout``, never a scalar. The per-call value is the
                # *read* budget; the connect and pool budgets stay configured.
                timeout=self._timeout_for(timeout_seconds),
            ) as response:
                # One authoritative implementation of the cap, shared with
                # the tests. Aborts mid-stream, closes the connection, and
                # discards the partial body.
                body = ByteLimitedReader(max_bytes=self._max_response_bytes).read(
                    response.iter_bytes()
                )
                return HttpResponse(
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    url=str(response.url),
                    # Bytes, decoded once by whoever needs text. See
                    # ``BODY_REPRESENTATION`` for exactly which bytes these are.
                    body=body,
                )
        except self._httpx.HTTPError as exc:
            raise TransportError(f"{type(exc).__name__} for {_redact(url)}") from exc

    def close(self) -> None:
        self._client.close()
