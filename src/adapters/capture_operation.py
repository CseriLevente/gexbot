"""One capture operation: what was asked for, and against what instant.

The gap this closes. v2.1.7 stamped every raw record with the *configuration*
in force at capture time -- pipeline, capture plan, request specification,
normalization rules -- and re-derived the chain from the stored bytes to check
it. What it did not bind was the **per-operation inputs**, and the sharpest of
those is the valuation instant. The rebuild did this:

    recipe = self.normalization_recipe(as_of=chain.as_of)
    rederived = self.rebuild_chain_from_capture(..., recipe=recipe)

Read it twice. The chain being checked chose the timestamp it was checked
against, so shifting ``chain.as_of`` shifted the rebuild with it and the two
agreed. A one-second shift on a 0DTE afternoon is a real change in
time-to-expiry and therefore in every gamma; an hour is a different market. The
re-derivation was exact about everything except the one input it took from the
thing under test.

A capture *operation* is the unit that fixes this. A session may run several --
a chain pull, a later re-pull, a paginated sweep -- and each has its own
requested instant, its own effective valuation timestamp and its own expected
universe. Every record says which operation issued it, and the operation's
identity is what verification and replay use.

The identity is immutable and hashed whole. Nothing here is a claim a caller
makes at calculation time; it is a record of what the capture did.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from src.domain.digests import digest_of, short_id

__all__ = [
    "CAPTURE_OPERATION_SCHEMA_VERSION",
    "CaptureOperationIdentity",
    "ValuationTimestampRule",
    "new_operation_id",
]

#: Bumped when the *meaning* of an operation identity changes, so a stamp taken
#: under older rules is refused rather than compared field by field against
#: newer ones.
CAPTURE_OPERATION_SCHEMA_VERSION = "capture-operation/2.1.10"


class ValuationTimestampRule(str, Enum):
    """How the instant every gamma is priced against was chosen.

    Named rather than implied, because the three answers differ by minutes and
    minutes are the whole quantity on a 0DTE contract. Recording *which* rule
    ran is what lets a later reader recompute the same number, and what stops a
    caller substituting a different instant under the same name.
    """

    #: The clock the vendor's index snapshot carried. Preferred under
    #: ``vendor_index_snapshot``: it comes from verified raw data, it is the
    #: instant the underlying was actually observed, and every gamma in the
    #: chain is computed against that underlying.
    INDEX_PRINT_TIMESTAMP = "INDEX_PRINT_TIMESTAMP"

    #: A single instant asserted to represent several responses, admitted only
    #: when their clocks agree within the configured tolerance. Not implemented
    #: as a *selection* rule today -- the synchronisation check exists, and
    #: choosing a representative instant from several disagreeing ones is a
    #: modelling decision nobody has made.
    SYNCHRONIZED_MARKET_TIMESTAMP = "SYNCHRONIZED_MARKET_TIMESTAMP"

    #: The instant the capture was requested. Honest, and weaker: it is a fact
    #: about this process rather than about the market. Used where no vendor
    #: clock is available -- an externally supplied spot, for instance -- and
    #: it does not support a trusted calculation on its own.
    CAPTURE_REQUEST_INSTANT = "CAPTURE_REQUEST_INSTANT"

    @property
    def derives_from_verified_data(self) -> bool:
        """Whether the instant came from bytes the vendor sent.

        ``CAPTURE_REQUEST_INSTANT`` does not: it is this machine's clock, which
        no capture can confirm.
        """
        return self is not ValuationTimestampRule.CAPTURE_REQUEST_INSTANT


def new_operation_id(*, as_of: datetime) -> str:
    """A collision-free operation id with the market instant as audit metadata.

    Same shape as ``new_capture_session_id`` and for the same reason: the
    timestamp makes an id readable, the nonce makes it unique. Two operations
    issued at the same market instant are still two operations.
    """
    stamp = as_of.astimezone().strftime("%Y%m%dT%H%M%S")
    return f"op-{stamp}-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True, slots=True)
class CaptureOperationIdentity:
    """Everything one capture operation fixed, before any response arrived.

    Immutable by construction and hashed whole. ``operation_fingerprint`` is
    what a record carries and what verification compares -- so a record from one
    operation cannot verify under another, even within the same session and the
    same configuration.
    """

    operation_id: str
    session_id: str
    pipeline_fingerprint: str
    capture_plan_fingerprint: str
    request_spec_fingerprint: str
    normalization_recipe_hash: str
    #: What the caller asked for.
    requested_as_of: datetime
    #: What Black-Scholes will actually be priced against. Usually not the same
    #: value, and the difference is the point of recording both.
    effective_valuation_timestamp: datetime
    valuation_timestamp_rule: ValuationTimestampRule
    #: Digest of the spot synchronisation policy -- tolerance and source -- so a
    #: wider tolerance cannot be supplied for one calculation.
    spot_synchronization_policy_fingerprint: str
    #: Digest of the rule that establishes the open-interest settlement date,
    #: where one has been established. ``None`` where none has.
    open_interest_date_rule_fingerprint: str | None = None
    #: Digest of the independently observed contract universe, where one exists.
    expected_universe_fingerprint: str | None = None
    parser_version: str = ""
    schema_version: str = CAPTURE_OPERATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("requested_as_of", "effective_valuation_timestamp"):
            moment = getattr(self, name)
            if not isinstance(moment, datetime):
                raise ValueError(
                    f"CaptureOperationIdentity.{name} must be a datetime, got "
                    f"{type(moment).__name__}"
                )
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError(
                    f"CaptureOperationIdentity.{name} must be timezone-aware; a "
                    "naive instant silently means whatever the reading machine's "
                    "zone is, and this value decides every time-to-expiry"
                )
        if not self.operation_id.strip():
            raise ValueError(
                "CaptureOperationIdentity.operation_id is empty; an operation "
                "nobody can name cannot be the one a record belongs to"
            )

    def semantic_payload(self) -> dict[str, Any]:
        """Everything the fingerprint covers.

        Both timestamps are here. Stamping only the normalization *rules* --
        which is what v2.1.7 did -- left the per-operation inputs unbound, and
        the valuation instant is the one that moves every gamma.
        """
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "session_id": self.session_id,
            "pipeline_fingerprint": self.pipeline_fingerprint,
            "capture_plan_fingerprint": self.capture_plan_fingerprint,
            "request_spec_fingerprint": self.request_spec_fingerprint,
            "normalization_recipe_hash": self.normalization_recipe_hash,
            "requested_as_of": self.requested_as_of.isoformat(),
            "effective_valuation_timestamp": (
                self.effective_valuation_timestamp.isoformat()
            ),
            "valuation_timestamp_rule": self.valuation_timestamp_rule.value,
            "spot_synchronization_policy_fingerprint": (
                self.spot_synchronization_policy_fingerprint
            ),
            "open_interest_date_rule_fingerprint": (
                self.open_interest_date_rule_fingerprint
            ),
            "expected_universe_fingerprint": self.expected_universe_fingerprint,
            "parser_version": self.parser_version,
        }

    @property
    def operation_fingerprint(self) -> str:
        """Full SHA-256 over the whole identity. Compared, never displayed."""
        return digest_of(self.semantic_payload())

    @property
    def display_id(self) -> str:
        """For a log line. Never for a comparison."""
        return f"{self.operation_id}@{short_id(self.operation_fingerprint)}"

    def matches(self, other: CaptureOperationIdentity) -> bool:
        return self.operation_fingerprint == other.operation_fingerprint

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "operation_fingerprint": self.operation_fingerprint,
        }
