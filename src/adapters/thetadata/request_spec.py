"""What this session intends to ask the vendor, endpoint by endpoint.

A capture is evidence about a *request*. v2.1.6 verified that the stored bytes
matched their manifest and that the manifest named the expected pipeline, but
the request itself was never checked against anything: the query parameters were
whatever the capture happened to send, and the parameter hash proved only that
they had not changed since.

That is a gap with a concrete failure. ``rate_value`` and ``annual_dividend``
are sent to the vendor's greeks endpoints and change the IV and the greeks that
come back. A capture taken at ``rate_value=4.2`` and relabelled as a capture
from a pipeline configured with ``3.1`` describes numbers the vendor computed
under a different rate -- and every gamma in the chain rests on them.

So the pipeline states, in advance and canonically, what it would send to each
endpoint it plans to use. The digest of that statement is stamped onto every
record at capture time, and verification recomputes it from the *current*
pipeline and compares -- both against the stamp and against the parameters
actually stored on each record.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from src.adapters.thetadata.endpoints import Endpoint

__all__ = [
    "REQUEST_SPEC_SCHEMA_VERSION",
    "RequestSpec",
    "build_request_spec",
]

#: Bumped when the *meaning* of a request spec changes, so a stamp taken under
#: older rules cannot be silently compared against one taken under newer ones.
REQUEST_SPEC_SCHEMA_VERSION = "thetadata-request-spec/2.1.7"

#: Endpoints that accept the server-side filters. The index snapshot takes the
#: symbol and nothing else, and sending it a strike range would be a request the
#: vendor quietly ignores -- which is exactly the kind of difference that makes
#: two captures look alike when they are not.
FILTERED_ENDPOINTS = frozenset(
    {
        Endpoint.OPTION_QUOTE_SNAPSHOT,
        Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT,
        Endpoint.OPTION_GREEKS_FIRST_ORDER,
        Endpoint.OPTION_GREEKS_SECOND_ORDER,
    }
)

#: Endpoints that carry the vendor's calculation parameters.
GREEKS_ENDPOINTS = frozenset(
    {Endpoint.OPTION_GREEKS_FIRST_ORDER, Endpoint.OPTION_GREEKS_SECOND_ORDER}
)

#: The index snapshot takes the symbol and nothing else -- see
#: ``ThetaDataClient.index_price``. It is not an option endpoint, so it has no
#: expiration to filter on, and stating otherwise here would make every capture
#: fail verification against a request it never sent.
SYMBOL_ONLY_ENDPOINTS = frozenset({Endpoint.INDEX_PRICE_SNAPSHOT})


@dataclass(frozen=True, slots=True)
class RequestSpec:
    """The canonical query this session would send to each endpoint.

    ``expected`` maps an endpoint value to the exact parameter mapping. It is
    what the pipeline *intends*, computed from configuration alone, so it can be
    recomputed later without the capture and compared against it.
    """

    expected: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    base_url: str = ""
    vendor_api_version: str = ""
    schema_version: str = REQUEST_SPEC_SCHEMA_VERSION

    def parameters_for(self, endpoint: str) -> dict[str, str] | None:
        """What this session would send to one endpoint, or ``None``.

        ``None`` means the endpoint is not part of this session at all, which is
        different from "sends no parameters" and must not read the same.
        """
        for name, params in self.expected:
            if name == endpoint:
                return dict(params)
        return None

    @property
    def endpoints(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, _ in self.expected))

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "base_url": self.base_url,
            "vendor_api_version": self.vendor_api_version,
            "expected": {
                endpoint: dict(params) for endpoint, params in sorted(self.expected)
            },
        }

    @property
    def fingerprint(self) -> str:
        """Full SHA-256 of the canonical request statement."""
        payload = json.dumps(
            self.semantic_payload(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {**self.semantic_payload(), "fingerprint": self.fingerprint}


def _canonical(params: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Parameters as sorted ``(name, text)`` pairs.

    Rendered as text because that is what reaches the wire and what the store
    reads back: ``4.2`` and ``"4.2"`` are the same request, and comparing a
    float against a parsed string would fail on formatting rather than on
    substance.
    """
    return tuple(sorted((str(k), _render(v)) for k, v in params.items()))


def _render(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value.is_integer():
        # ``4.0`` and ``4`` are the same rate. Rendering them differently would
        # make a spec mismatch out of an int/float distinction the vendor does
        # not see.
        return str(int(value))
    return str(value)


def build_request_spec(
    *,
    request: Any,
    greeks: Any,
    settings: Any,
    endpoints: tuple[Endpoint, ...],
) -> RequestSpec:
    """State, canonically, what this session would send to each endpoint.

    Built from the same objects the client sends -- ``ChainRequest.as_query``
    and ``GreeksParameters.as_query`` -- rather than from a parallel description
    of them. A separate description would be a second source of truth, and the
    defect this closes is exactly a claim that nothing checked.
    """
    expected: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for endpoint in sorted(set(endpoints), key=lambda e: e.value):
        params: dict[str, Any]
        if endpoint in SYMBOL_ONLY_ENDPOINTS:
            params = {"symbol": request.symbol}
        else:
            params = dict(
                request.as_query(supports_filters=endpoint in FILTERED_ENDPOINTS)
            )
            if endpoint in GREEKS_ENDPOINTS:
                params.update(greeks.as_query())
        expected.append((endpoint.value, _canonical(params)))

    return RequestSpec(
        expected=tuple(expected),
        base_url=str(getattr(settings, "base_url", "")),
        vendor_api_version=str(getattr(settings, "api_version", "v3")),
    )
