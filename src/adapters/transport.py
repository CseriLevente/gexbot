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

import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# 64 MiB. A full SPX+SPXW chain CSV is a few MiB; this leaves headroom without
# allowing an unbounded read.
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024

RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class TransportError(RuntimeError):
    """Network-level failure: connection refused, DNS, timeout, TLS."""


class VendorHTTPError(RuntimeError):
    """Non-2xx response that will not be retried."""

    def __init__(self, status_code: int, url: str, body_excerpt: str) -> None:
        super().__init__(f"HTTP {status_code} from {_redact(url)}: {body_excerpt}")
        self.status_code = status_code
        self.url = url
        self.body_excerpt = body_excerpt


class ResponseTooLargeError(RuntimeError):
    pass


class RetryBudgetExhaustedError(RuntimeError):
    def __init__(self, attempts: int, last_error: Exception) -> None:
        super().__init__(f"giving up after {attempts} attempt(s): {last_error}")
        self.attempts = attempts
        self.last_error = last_error


_SENSITIVE_PARAM_NAMES = ("password", "token", "key", "secret", "auth")


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


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    text: str
    headers: Mapping[str, str] = field(default_factory=dict)
    url: str = ""
    request_id: str = ""
    elapsed_seconds: float = 0.0
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def byte_length(self) -> int:
        return len(self.text.encode("utf-8"))


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

    Responses are keyed by a substring of the URL path so a test can register
    "whatever hits /v3/option/snapshot/quote" without reproducing the exact query
    string.
    """

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
    ) -> None:
        self._inner = inner
        self._policy = policy or RetryPolicy()
        self._sleep = sleep
        self._random_unit = random_unit
        self._max_response_bytes = max_response_bytes
        self.sleeps: list[float] = []

    def get(
        self, url: str, params: Mapping[str, Any], timeout_seconds: float
    ) -> HttpResponse:
        request_id = uuid.uuid4().hex[:12]
        last_error: Exception | None = None

        for attempt in range(1, self._policy.max_retries + 2):
            started = time.monotonic()
            try:
                response = self._inner.get(url, params, timeout_seconds)
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
            else:
                elapsed = time.monotonic() - started
                if response.byte_length > self._max_response_bytes:
                    raise ResponseTooLargeError(
                        f"response of {response.byte_length} bytes exceeds the "
                        f"{self._max_response_bytes}-byte cap for {_redact(url)}"
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
                    )
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    # A malformed request stays malformed; retrying only wastes
                    # the vendor's rate limit and delays the error.
                    raise VendorHTTPError(
                        response.status_code, url, response.text[:500]
                    )
                last_error = VendorHTTPError(
                    response.status_code, url, response.text[:500]
                )
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
            delay = self._retry_delay(attempt, last_error)
            self.sleeps.append(delay)
            self._sleep(delay)

        assert last_error is not None  # loop always sets it before breaking
        raise RetryBudgetExhaustedError(self._policy.max_retries + 1, last_error)

    def _retry_delay(self, attempt: int, last_error: Exception | None) -> float:
        """Honour a vendor's Retry-After when it gives one; back off otherwise."""
        if isinstance(last_error, VendorHTTPError) and last_error.status_code == 429:
            # Respect an explicit rate-limit instruction over our own guess, but
            # never below the computed backoff -- the vendor's hint is a floor,
            # not a licence to hammer.
            return max(
                self._policy.delay_for(attempt, random_unit=self._random_unit()),
                self._policy.backoff_base_seconds,
            )
        return self._policy.delay_for(attempt, random_unit=self._random_unit())


class HttpxTransport:  # pragma: no cover - exercised only against a live vendor
    """Real HTTP transport. Imports ``httpx`` lazily.

    Not covered by unit tests on purpose: covering it would mean either mocking
    ``httpx`` internals (testing the mock, not the code) or making a network
    call (which unit tests must never do). Its retry, redaction and size-cap
    behaviour lives in :class:`RetryingTransport`, which *is* covered.
    """

    def __init__(
        self,
        *,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 30.0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "HttpxTransport needs the 'http' extra: pip install -e '.[http]'"
            ) from exc
        self._httpx = httpx
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=connect_timeout_seconds,
                read=read_timeout_seconds,
                write=read_timeout_seconds,
                pool=connect_timeout_seconds,
            ),
            headers=dict(headers or {}),
        )

    def get(
        self, url: str, params: Mapping[str, Any], timeout_seconds: float
    ) -> HttpResponse:
        try:
            raw = self._client.get(url, params=dict(params), timeout=timeout_seconds)
        except self._httpx.HTTPError as exc:
            raise TransportError(f"{type(exc).__name__} for {_redact(url)}") from exc
        return HttpResponse(
            status_code=raw.status_code,
            text=raw.text,
            headers=dict(raw.headers),
            url=str(raw.url),
        )

    def close(self) -> None:
        self._client.close()
