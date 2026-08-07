"""Exactly what will be sent, written down before anything is sent.

A dry run that reports "four endpoints, standard tier, READY" tells an operator
almost nothing about the requests it is authorising. The v2.1.15 dry run said
that, and the request it was authorising asked ``/v3/index/snapshot/price`` for
``symbol=SPXW`` -- a price for an instrument that does not exist, which would
have become the spot under every gamma in the chain. Nothing in the report
showed it, because the report described the *plan* in the abstract and the
symbol lived inside a method that had not run yet.

So the plan becomes an artifact. Every request is derived up front, canonicalised
and hashed; the dry run prints it; the live run compares each outgoing request
against it and refuses a mismatch before it reaches the transport.

Nothing here holds a credential. Query parameters are the filters -- symbols,
dates, DTE windows, rate parameters -- and the URL is the path, never the base
with its host. A plan is a document an operator pastes into a ticket.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "REQUEST_PLAN_SCHEMA_VERSION",
    "PlannedEndpointRequest",
    "RawRequestPlan",
    "RequestPlanViolation",
    "canonical_parameters",
]

#: Bumped when the shape of a request plan changes.
REQUEST_PLAN_SCHEMA_VERSION = "raw-request-plan/2.1.20"


def _default_stop_policy() -> str:
    """The executor's policy, read from the executor.

    Imported inside the function rather than at module scope: this module is
    pulled in by the plan-building path, and the acquisition module imports
    rather more.
    """
    from src.adapters.thetadata.raw_acquisition import RequestFailurePolicy

    return RequestFailurePolicy.CONTINUE_UNLESS_SYSTEMIC.value


def _systemic_stop_reasons() -> tuple[str, ...]:
    from src.adapters.thetadata.raw_acquisition import systemic_stop_reasons

    return systemic_stop_reasons()


class RequestPlanViolation(RuntimeError):
    """An outgoing request is not the one the plan authorised.

    Raised before the transport is touched. The plan is what the operator read
    and approved; a request that differs from it is one nobody approved, and
    the difference is exactly the class of defect this release exists to catch.
    """


def canonical_parameters(params: Any) -> tuple[tuple[str, str], ...]:
    """Query parameters as sorted ``(name, value)`` pairs of strings.

    Sorted so two derivations of the same request hash the same, and stringified
    so ``max_dte=60`` and ``max_dte="60"`` -- which the URL builder cannot tell
    apart -- do not produce two different plan hashes for one request.
    """
    return tuple(sorted((str(k), str(v)) for k, v in dict(params or {}).items()))


@dataclass(frozen=True, slots=True)
class PlannedEndpointRequest:
    """One request, fully resolved, before it is made."""

    endpoint: str
    #: The path only. The base URL is a deployment fact and is reported once,
    #: beside the plan, rather than repeated into every entry.
    safe_path: str
    canonical_query_parameters: tuple[tuple[str, str], ...]
    required_tier: str
    request_spec_hash: str
    #: What the sweep does if this request fails. Carried per request because
    #: "continue" and "stop" are the difference between a partial capture and a
    #: cancelled one, and an operator should see which is which before paying.
    #:
    #: Defaulted from the executor's own enum since v2.1.20. It said
    #: ``CONTINUE_ON_FAILURE`` while the sweep stopped on five systemic
    #: conditions -- two hand-written descriptions of one behaviour, and the
    #: plan's was the one being read before money changed hands.
    stop_policy: str = _default_stop_policy()
    #: The conditions that *do* stop the sweep, named rather than implied.
    systemic_stop_reasons: tuple[str, ...] = field(
        default_factory=lambda: _systemic_stop_reasons()
    )

    @property
    def parameters(self) -> dict[str, str]:
        return dict(self.canonical_query_parameters)

    def matches(self, endpoint: str, params: Any) -> bool:
        """Whether an actual request is the one this entry authorised."""
        return (
            endpoint == self.endpoint
            and canonical_parameters(params) == self.canonical_query_parameters
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "safe_path": self.safe_path,
            "canonical_query_parameters": [
                list(pair) for pair in self.canonical_query_parameters
            ],
            "required_tier": self.required_tier,
            "request_spec_hash": self.request_spec_hash,
            "stop_policy": self.stop_policy,
            "systemic_stop_reasons": list(self.systemic_stop_reasons),
        }


@dataclass(frozen=True, slots=True)
class RawRequestPlan:
    """Every request one session will make, and a digest over all of them."""

    requests: tuple[PlannedEndpointRequest, ...] = ()
    schema_version: str = REQUEST_PLAN_SCHEMA_VERSION
    #: The symbols, printed beside the plan because they are the thing an
    #: operator is checking when they read it.
    option_symbol: str = ""
    underlying_index_symbol: str = ""
    #: How this repository reaches the vendor. Recorded rather than assumed:
    #: v2.1.16 targets the v3 REST API through a local Theta Terminal, and a
    #: later release choosing differently should be a visible change.
    access_mode: str = "THETA_TERMINAL_REST_V3"
    base_url: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def request_plan_hash(self) -> str:
        """Digest over every request, in plan order.

        Covers the endpoints and their parameters and nothing environmental:
        the same plan run against two Terminals is the same plan.
        """
        payload = json.dumps(
            {
                "schema_version": self.schema_version,
                "option_symbol": self.option_symbol,
                "underlying_index_symbol": self.underlying_index_symbol,
                "requests": [entry.as_dict() for entry in self.requests],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def for_endpoint(self, endpoint: str) -> PlannedEndpointRequest | None:
        return next((e for e in self.requests if e.endpoint == endpoint), None)

    def authorize(self, endpoint: str, params: Any) -> PlannedEndpointRequest:
        """The plan entry for this request, or a refusal naming the difference.

        Called on the way to the transport. A request that is not in the plan,
        or that carries different parameters from the one that was printed, does
        not go out: the operator approved a document, and this is the only place
        that can tell whether what is happening is what they read.
        """
        planned = self.for_endpoint(endpoint)
        if planned is None:
            raise RequestPlanViolation(
                f"{endpoint} is not in the request plan "
                f"{self.request_plan_hash[:12]}..., which authorises "
                f"{sorted(e.endpoint for e in self.requests)}"
            )
        actual = canonical_parameters(params)
        if actual != planned.canonical_query_parameters:
            expected = dict(planned.canonical_query_parameters)
            got = dict(actual)
            differing = sorted(
                name
                for name in set(expected) | set(got)
                if expected.get(name) != got.get(name)
            )
            raise RequestPlanViolation(
                f"{endpoint} would be sent with parameters the plan did not "
                f"authorise. Differing: "
                + ", ".join(
                    f"{name}: planned {expected.get(name)!r}, actual {got.get(name)!r}"
                    for name in differing
                )
            )
        return planned

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_plan_hash": self.request_plan_hash,
            "access_mode": self.access_mode,
            "base_url": self.base_url,
            "option_symbol": self.option_symbol,
            "underlying_index_symbol": self.underlying_index_symbol,
            "request_count": len(self.requests),
            "requests": [entry.as_dict() for entry in self.requests],
            "notes": list(self.notes),
        }
