"""Getting the bytes, as a separate thing from understanding them.

The first paid session exists to find out what the vendor actually sends. Until
v2.1.15 the operator reached the wire through ``pipeline.fetch_chain()``, which
requests one endpoint, *parses it*, and uses the result to build the next
request. So an index snapshot that came back as an HTML error page -- a
maintenance window, a proxy, a schema nobody had seen -- raised before the quote
request was ever issued, and the operator paid for a session that captured one
response out of four.

That is exactly backwards. A response nobody can parse is the most interesting
thing a discovery session can find, and it is worth less than nothing if finding
it prevents the other three responses from being collected.

So: acquisition asks for every endpoint the plan requires, stores whatever comes
back before looking at it, and records what happened. Parsing runs afterwards,
against the stored bytes, and writes its own report. A parser finding cannot
shorten a capture, because by the time the parser runs the capture is over.

Stopping early is still permitted -- but only for failures where continuing
would produce nothing. Those are named, and the reason is recorded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "PARSER_REPORT_SCHEMA_VERSION",
    "RAW_ACQUISITION_SCHEMA_VERSION",
    "SYSTEMIC_STOP_REASONS",
    "ParserStatus",
    "RawAcquisitionOutcome",
    "RawAcquisitionStopReason",
    "RawEndpointAcquisitionStatus",
    "RawEndpointCaptureResult",
    "classify_failure",
    "nothing_is_answering",
    "stop_reason_for",
]

#: Bumped when the shape of an acquisition report changes.
RAW_ACQUISITION_SCHEMA_VERSION = "raw-acquisition/2.1.16"

#: Bumped when what a parser report *claims* changes. Separate from the
#: acquisition schema on purpose: the two documents answer different questions
#: and a reader must be able to accept one and refuse the other.
PARSER_REPORT_SCHEMA_VERSION = "parser-report/2.1.16"


class RawEndpointAcquisitionStatus(str, Enum):
    """What happened to one endpoint's *bytes*. Never about their content.

    A body that is valid CSV and a body that is an HTML error page are both
    ``ACQUIRED``: the vendor answered and the answer is on disk. Whether it can
    be parsed is a finding about the bytes, recorded separately, and it is the
    finding the session was paid to produce.
    """

    #: The vendor answered and the response body is in the raw store.
    ACQUIRED = "ACQUIRED"
    #: The vendor answered with a non-success status that the transport refuses
    #: to hand on as data -- a 400, a 404. The body is preserved byte-exact in
    #: the **attempt log** rather than the raw store, because the raw store
    #: holds responses a chain could be built from and this is not one.
    #: ``attempt_record_ids`` says where to find it. The sweep continues: a 400
    #: on the greeks endpoint is not a reason to skip open interest.
    VENDOR_REFUSED = "VENDOR_REFUSED"
    #: Nothing arrived: connection refused, DNS, TLS, a timeout on every
    #: attempt. The attempts are in the attempt log.
    NO_RESPONSE = "NO_RESPONSE"
    #: A response arrived and could not be written down. The bytes are lost,
    #: which is the one outcome worth stopping the run for.
    STORAGE_FAILED = "STORAGE_FAILED"
    #: The configured tier cannot serve this endpoint, so no request was made.
    #: Refused at configuration load, so this is a statement, not an expectation.
    TIER_NOT_ENTITLED = "TIER_NOT_ENTITLED"
    #: The run stopped under the systemic-failure policy before reaching it.
    NOT_ATTEMPTED = "NOT_ATTEMPTED"

    @property
    def has_bytes(self) -> bool:
        """Whether a raw record exists. A refusal's body lives elsewhere."""
        return self is RawEndpointAcquisitionStatus.ACQUIRED

    @property
    def answered(self) -> bool:
        """Whether the vendor said anything at all, usable or not."""
        return self in (
            RawEndpointAcquisitionStatus.ACQUIRED,
            RawEndpointAcquisitionStatus.VENDOR_REFUSED,
        )


class RawAcquisitionStopReason(str, Enum):
    """Why acquisition stopped before attempting every planned endpoint.

    The policy, stated as a closed set rather than as whatever exception
    happened to escape. Everything not on this list is a finding about one
    endpoint and the loop continues.
    """

    #: Every planned endpoint was attempted.
    NONE = "NONE"
    #: The vendor rejected the credentials. Every later request would too.
    AUTHENTICATION_REJECTED = "AUTHENTICATION_REJECTED"
    #: Nothing is answering at the configured address.
    CONNECTION_UNAVAILABLE = "CONNECTION_UNAVAILABLE"
    #: The retry budget was spent on 429s. Continuing spends the quota that a
    #: later attempt at this session would need.
    RATE_LIMIT_EXHAUSTED = "RATE_LIMIT_EXHAUSTED"
    #: The operator interrupted the run.
    OPERATOR_CANCELLED = "OPERATOR_CANCELLED"
    #: A response arrived and could not be stored. Continuing would spend money
    #: on bytes that go nowhere.
    STORAGE_FAILURE = "STORAGE_FAILURE"


#: The systemic reasons, as a set, so a caller can ask "was this systemic?"
#: without enumerating the enum and drifting from it.
SYSTEMIC_STOP_REASONS = frozenset(
    reason
    for reason in RawAcquisitionStopReason
    if reason is not RawAcquisitionStopReason.NONE
)


class ParserStatus(str, Enum):
    """What a parser made of bytes that are already safely stored."""

    #: Parsing was not attempted -- no bytes, or the caller did not ask.
    PARSER_NOT_RUN = "PARSER_NOT_RUN"
    #: The stored bytes parse into rows with the columns this reader expects.
    PARSER_VALID = "PARSER_VALID"
    #: The stored bytes did not parse, or did not carry the expected schema.
    PARSER_FAILED = "PARSER_FAILED"
    #: Every endpoint parsed and the rows would not join into a chain.
    CHAIN_ASSEMBLY_FAILED = "CHAIN_ASSEMBLY_FAILED"


@dataclass(frozen=True, slots=True)
class RawEndpointCaptureResult:
    """One planned endpoint, and what became of its bytes.

    ``request_parameters`` is a sorted tuple of pairs rather than a mapping so
    the result is hashable and orders deterministically -- two runs of the same
    plan produce comparable reports.
    """

    endpoint: str
    request_parameters: tuple[tuple[str, str], ...]
    record_id: str | None
    attempt_record_ids: tuple[str, ...]
    http_status: int | None
    acquisition_status: RawEndpointAcquisitionStatus
    transport_error_code: str | None
    parser_status: str | None = None
    #: The redacted message, when something went wrong. Never a credential.
    detail: str = ""
    #: The typed operator error code for this endpoint's failure, so the run
    #: report can say ``VENDOR_HTTP_ERROR`` rather than "one endpoint failed".
    error_code: str = ""
    byte_length: int | None = None

    @property
    def acquired(self) -> bool:
        return self.acquisition_status.has_bytes

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "request_parameters": [list(pair) for pair in self.request_parameters],
            "record_id": self.record_id,
            "attempt_record_ids": list(self.attempt_record_ids),
            "http_status": self.http_status,
            "acquisition_status": self.acquisition_status.value,
            "transport_error_code": self.transport_error_code,
            "parser_status": self.parser_status,
            "detail": self.detail,
            "error_code": self.error_code,
            "byte_length": self.byte_length,
        }


@dataclass(frozen=True, slots=True)
class RawAcquisitionOutcome:
    """Every planned endpoint, and whether the sweep ran to the end."""

    results: tuple[RawEndpointCaptureResult, ...] = ()
    stop_reason: RawAcquisitionStopReason = RawAcquisitionStopReason.NONE
    stop_detail: str = ""
    #: The endpoints the plan required, in plan order, so a report can be read
    #: without the plan next to it.
    planned_endpoints: tuple[str, ...] = ()
    schema_version: str = RAW_ACQUISITION_SCHEMA_VERSION
    #: The plan every request in this sweep was authorised against, so a
    #: capture can be compared with the document its operator approved.
    request_plan_hash: str = ""
    #: Named so a reader can tell "we chose not to continue" from "there was
    #: nothing left to do".
    stop_policy: tuple[str, ...] = field(
        default_factory=lambda: tuple(sorted(r.value for r in SYSTEMIC_STOP_REASONS))
    )

    @property
    def stopped_early(self) -> bool:
        return self.stop_reason is not RawAcquisitionStopReason.NONE

    @property
    def acquired_endpoints(self) -> tuple[str, ...]:
        return tuple(sorted({r.endpoint for r in self.results if r.acquired}))

    @property
    def first_failure(self) -> RawEndpointCaptureResult | None:
        """The first endpoint that did not yield a usable raw record."""
        return next((r for r in self.results if not r.acquired), None)

    @property
    def attempted_endpoints(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    r.endpoint
                    for r in self.results
                    if r.acquisition_status
                    is not RawEndpointAcquisitionStatus.NOT_ATTEMPTED
                }
            )
        )

    @property
    def missing_endpoints(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.planned_endpoints) - set(self.acquired_endpoints)))

    @property
    def any_response(self) -> bool:
        """Whether the vendor answered at all. Drives the run state."""
        return any(r.acquisition_status.answered for r in self.results)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "planned_endpoints": list(self.planned_endpoints),
            "attempted_endpoints": list(self.attempted_endpoints),
            "acquired_endpoints": list(self.acquired_endpoints),
            "missing_endpoints": list(self.missing_endpoints),
            "stopped_early": self.stopped_early,
            "stop_reason": self.stop_reason.value,
            "stop_detail": self.stop_detail,
            "stop_policy": list(self.stop_policy),
            "request_plan_hash": self.request_plan_hash,
            "results": [result.as_dict() for result in self.results],
        }

    def fingerprint(self) -> str:
        import hashlib

        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: Adapter failure -> the operator's typed error code. Ordered most specific
#: first. Lives here rather than in the operator because the *adapter* owns the
#: exception hierarchy, and one table means the code an endpoint result carries
#: and the code the run reports cannot disagree.
def classify_failure(error: BaseException) -> str:
    """The typed error code for an adapter failure. Never a message parse."""
    from src.adapters.errors import (
        ThetaDataAuthenticationError,
        ThetaDataConfigurationError,
        ThetaDataHTTPError,
        ThetaDataProvenanceError,
        ThetaDataRateLimitError,
        ThetaDataRawStoreError,
        ThetaDataResponseTooLargeError,
        ThetaDataRetryExhaustedError,
        ThetaDataSchemaError,
        ThetaDataValidationError,
        ThetaDataVendorError,
    )
    from src.adapters.transport import (
        ResponseTooLargeError,
        RetryBudgetExhaustedError,
        TransportError,
        VendorHTTPError,
    )
    from src.config.schema import ConfigError

    # What the retry budget was actually spent on. ``RetryBudgetExhaustedError``
    # says only that we gave up; the last failure says why, and "we retried a
    # 429 four times" and "we retried a 503 four times" send an operator to
    # different places.
    status = _status_of(error)
    if status in (401, 403):
        return "AUTHENTICATION_REJECTED"
    if status == 429:
        return "RATE_LIMITED"

    for kind, code in (
        (ThetaDataAuthenticationError, "AUTHENTICATION_REJECTED"),
        (ThetaDataRateLimitError, "RATE_LIMITED"),
        (ThetaDataResponseTooLargeError, "RESPONSE_TOO_LARGE"),
        (ThetaDataVendorError, "VENDOR_HTTP_ERROR"),
        (ThetaDataSchemaError, "SCHEMA_ERROR"),
        (ThetaDataProvenanceError, "PROVENANCE_ERROR"),
        (ThetaDataValidationError, "VALIDATION_ERROR"),
        (ThetaDataRawStoreError, "STORAGE_ERROR"),
        (ThetaDataConfigurationError, "CONFIGURATION_ERROR"),
        (ConfigError, "CONFIGURATION_ERROR"),
        (ThetaDataRetryExhaustedError, "RETRY_EXHAUSTED"),
        (ThetaDataHTTPError, "VENDOR_HTTP_ERROR"),
        (RetryBudgetExhaustedError, "RETRY_EXHAUSTED"),
        (VendorHTTPError, "VENDOR_HTTP_ERROR"),
        (ResponseTooLargeError, "RESPONSE_TOO_LARGE"),
        (TransportError, "TRANSPORT_FAILURE"),
    ):
        if isinstance(error, kind):
            return code
    return f"INTERNAL_ERROR:{type(error).__name__}"


def stop_reason_for(error: BaseException) -> RawAcquisitionStopReason:
    """Whether this failure stops the sweep, and under which named reason.

    Consults the whole class hierarchy, then the HTTP status, then the cause
    chain -- because a 401 arrives wrapped differently depending on which layer
    noticed it, and "the credentials are wrong" is the same operational fact
    every time.
    """
    from src.adapters.errors import (
        ThetaDataAuthenticationError,
        ThetaDataRateLimitError,
        ThetaDataRawStoreError,
    )

    if isinstance(error, KeyboardInterrupt | SystemExit):
        return RawAcquisitionStopReason.OPERATOR_CANCELLED
    if isinstance(error, ThetaDataRawStoreError):
        return RawAcquisitionStopReason.STORAGE_FAILURE

    # The status decides, not the wrapper class. A budget spent on 429s is a
    # rate limit and the *next* request would be refused too; a budget spent on
    # 503s is one endpoint having a bad minute, and open interest may well
    # answer. v2.1.15's first draft treated every ``RetryBudgetExhaustedError``
    # as systemic, which reintroduced the defect this release exists to remove:
    # one endpoint's failure silently cancelling the rest of the capture.
    status = _status_of(error)
    if status in (401, 403) or isinstance(error, ThetaDataAuthenticationError):
        return RawAcquisitionStopReason.AUTHENTICATION_REJECTED
    if status == 429 or isinstance(error, ThetaDataRateLimitError):
        return RawAcquisitionStopReason.RATE_LIMIT_EXHAUSTED

    # Everything else -- a 503 retried away, a timeout, a refused connection --
    # is one endpoint's finding. Whether *nothing* is answering is a property of
    # the sweep as a whole, and :func:`nothing_is_answering` decides it.
    return RawAcquisitionStopReason.NONE


#: How many consecutive endpoints must answer with nothing at all before the
#: sweep concludes the vendor is unreachable. Two rather than one: a single
#: endpoint timing out is ordinary, and the cost of trying the second is a few
#: seconds against a capture that costs money.
UNREACHABLE_AFTER_CONSECUTIVE_SILENCES = 2


def nothing_is_answering(results: list[RawEndpointCaptureResult]) -> bool:
    """Whether every attempt so far produced no response at all.

    The "is the Theta Terminal even running?" check. Distinguished from a
    per-endpoint timeout because the remedy is different and because continuing
    to request from an address nothing is listening on produces attempts, not
    evidence.
    """
    if len(results) < UNREACHABLE_AFTER_CONSECUTIVE_SILENCES:
        return False
    return all(
        result.acquisition_status is RawEndpointAcquisitionStatus.NO_RESPONSE
        for result in results
    )


def _status_of(error: BaseException) -> int | None:
    """The HTTP status behind a wrapped failure, following the cause chain."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status_code", None)
        if isinstance(status, int):
            return status
        current = getattr(current, "last_error", None) or current.__cause__
    return None
