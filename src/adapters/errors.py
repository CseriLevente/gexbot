"""One exception hierarchy for every ThetaData adapter failure.

The v2.1.1 defect. Failures came out of four layers with four unrelated base
classes: ``TransportError`` and ``VendorHTTPError`` from the transport,
``RetryBudgetExhaustedError`` from the retry wrapper, ``RawStoreError`` from the
store, and ``ThetaDataError`` from the parser. ``except ThetaDataError`` caught
roughly half the ways an adapter can fail, and which half depended on which
layer happened to break.

A caller wanting "did the adapter fail?" had to enumerate the internals.

This module holds the base and its subclasses so that both the raw store and
the ThetaData client can import it without either importing the other -- the
store is used by more than one adapter, so a dependency on the ThetaData client
would run the wrong way.
"""

from __future__ import annotations

import re

__all__ = [
    "ThetaDataAuthenticationError",
    "ThetaDataCertificationError",
    "ThetaDataConfigurationError",
    "ThetaDataError",
    "ThetaDataHTTPError",
    "ThetaDataProvenanceError",
    "ThetaDataRateLimitError",
    "ThetaDataRawStoreError",
    "ThetaDataResponseTooLargeError",
    "ThetaDataRetryExhaustedError",
    "ThetaDataSchemaError",
    "ThetaDataValidationError",
    "ThetaDataVendorError",
    "redact_secrets",
]

#: Anything shaped like ``token=...`` or ``password: ...``. Vendor error bodies
#: are echoed into exception messages, and a vendor that helpfully includes the
#: failing query string would otherwise put a credential into a traceback, a
#: log, and every bug report that quotes it.
_SECRET_PATTERN = re.compile(
    r"(?i)\b(token|password|passwd|secret|api[_-]?key|auth|bearer)\s*[=:]\s*\S+"
)


def redact_secrets(text: str) -> str:
    """Replace credential-shaped values with a placeholder."""
    return _SECRET_PATTERN.sub(r"\1=<redacted>", text)


class ThetaDataError(RuntimeError):
    """Base class for every ThetaData adapter failure.

    Carries the vendor status and request id when they exist, so an operator
    debugging a failed capture has the two identifiers the vendor's support
    would ask for. Never carries a credential.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str = "",
    ) -> None:
        super().__init__(redact_secrets(message))
        self.status_code = status_code
        self.request_id = request_id


class ThetaDataConfigurationError(ThetaDataError):
    """The adapter was asked to do something its configuration forbids."""


class ThetaDataHTTPError(ThetaDataError):
    """A non-success HTTP status from the vendor."""


class ThetaDataAuthenticationError(ThetaDataHTTPError):
    """401 or 403. Separate because the remedy is a credential, not a retry."""


class ThetaDataRateLimitError(ThetaDataHTTPError):
    """429. Separate because the remedy is to slow down, not to reconfigure."""


class ThetaDataRetryExhaustedError(ThetaDataError):
    """The retry budget ran out. The last underlying error is chained."""


class ThetaDataSchemaError(ThetaDataError):
    """A response is missing a column the parser requires."""


class ThetaDataVendorError(ThetaDataHTTPError):
    """The vendor returned an error document, or a non-success status."""


class ThetaDataResponseTooLargeError(ThetaDataError):
    """A response exceeded the configured cap.

    Public because the cap is a configuration decision the caller made, and
    they need to catch it. v2.1.2 raised ``ResponseTooLargeError(RuntimeError)``
    from the transport, which escaped ``except ThetaDataError`` -- so the one
    failure most likely to be triggered by a deliberate setting was the one a
    caller could not catch with the adapter's own base class.
    """


class ThetaDataRawStoreError(ThetaDataError):
    """A raw-response store failure: append-only violation, unsafe id, IO."""


class ThetaDataCertificationError(ThetaDataError):
    """A certification input that cannot be accepted.

    v2.1.4 raised a bare ``TypeError`` from ``assess_readiness`` when handed an
    untyped capture, and ``ValueError`` subclasses from the provenance and
    validation objects. Public certification refusals are part of the adapter's
    contract, so they belong in the adapter's hierarchy -- a caller writing
    ``except ThetaDataError`` should not also have to enumerate builtins.
    """


class ThetaDataProvenanceError(ThetaDataCertificationError):
    """A provenance claim that does not hold against the raw evidence."""


class ThetaDataValidationError(ThetaDataCertificationError):
    """A validation report that does not describe what it claims to."""


def http_error_for(
    *, endpoint: str, status_code: int, request_id: str = "", body_length: int = 0
) -> ThetaDataHTTPError:
    """The right subclass for a status, so callers can act on the category."""
    message = (
        f"{endpoint} returned HTTP {status_code}; refusing to parse a "
        f"non-success body as CSV ({body_length} bytes)"
    )
    if status_code in (401, 403):
        return ThetaDataAuthenticationError(
            message, status_code=status_code, request_id=request_id
        )
    if status_code == 429:
        return ThetaDataRateLimitError(
            message, status_code=status_code, request_id=request_id
        )
    return ThetaDataHTTPError(message, status_code=status_code, request_id=request_id)
