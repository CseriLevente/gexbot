"""What a configured session must actually fetch.

A capture is not "some vendor bytes". It is the specific set of responses the
configured session needs in order to produce the number it claims to produce,
and a capture missing one of them cannot produce that number at all.

v2.1.4 had no such notion. ``verify_capture`` asked whether the records a
manifest *claimed* were present in the store, and never whether the manifest
claimed enough. A one-record capture -- a single quote snapshot, no open
interest, no implied volatility, no underlying -- verified cleanly and advanced
the certification ladder. Open interest is the weight on every GEX term; without
it there is no GEX, certified or otherwise.

The plan is derived, not configured. It falls out of four settings that are
already stated elsewhere, so it cannot drift from them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from src.adapters.thetadata.endpoints import Endpoint, Tier, tier_satisfies

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.config.pipeline import IvGammaPricingMode, VendorGammaPolicy

__all__ = ["CapturePlan", "MultipleRecordReason", "capture_plan_for"]

#: Underlying sources that mean "read the vendor's index print". Anything else
#: is either synthetic or supplied from outside, and needs no index request.
VENDOR_INDEX_SOURCES = frozenset({"vendor_index_snapshot"})


class MultipleRecordReason(str, Enum):
    """Why one endpoint may legitimately appear more than once in a capture.

    Four reasons, named, because "an endpoint answered twice" is otherwise
    indistinguishable from a defect. v2.1.7 replayed the *first* record for an
    endpoint and left any others unread, so a capture containing two quote
    responses -- one stale, one current -- normalized from whichever arrived
    first and the second sat in the store looking like evidence.

    A plan that declares none of these is a plan under which a second response
    for the same endpoint is unaccounted for, and that is the honest default.
    """

    #: The vendor returned the result across pages.
    PAGINATION = "PAGINATION"
    #: One request per expiration, batched into one operation.
    BATCHED_EXPIRATIONS = "BATCHED_EXPIRATIONS"
    #: A request was reissued after a failure. Both attempts are in the store.
    RETRY = "RETRY"
    #: The universe was split across requests -- by strike range, say.
    PARTITIONED_UNIVERSE = "PARTITIONED_UNIVERSE"


@dataclass(frozen=True, slots=True)
class CapturePlan:
    """The endpoints one session must capture, and why.

    ``rationale`` is carried per endpoint so a capture that comes back short
    says what the missing response was *for*, rather than naming a URL.
    """

    required_endpoints: tuple[Endpoint, ...]
    rationale: tuple[tuple[str, str], ...] = ()
    pricing_mode: str = ""
    vendor_gamma_policy: str = ""
    underlying_price_source: str = ""
    tier: str = ""
    #: ``(endpoint, MultipleRecordReason)`` pairs. Empty in every shipped
    #: profile: a snapshot plan issues one request per endpoint, so a second
    #: response is a duplicate nobody accounted for rather than a page two.
    declared_multiple_records: tuple[tuple[str, str], ...] = ()

    @property
    def fingerprint(self) -> str:
        """Digest of the plan, so a manifest can say which plan it satisfied.

        Covers the inputs as well as the endpoints: two plans that happen to
        require the same responses for different reasons are different plans,
        and a capture taken under one does not certify the other.
        """
        payload = json.dumps(
            {
                "required_endpoints": sorted(e.value for e in self.required_endpoints),
                "pricing_mode": self.pricing_mode,
                "vendor_gamma_policy": self.vendor_gamma_policy,
                "underlying_price_source": self.underlying_price_source,
                "tier": self.tier,
                # Whether an endpoint may answer twice changes which bytes a
                # chain was built from, so it is part of the plan's identity.
                "declared_multiple_records": sorted(
                    list(entry) for entry in self.declared_multiple_records
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def reason_for(self, endpoint: Endpoint) -> str:
        return dict(self.rationale).get(endpoint.value, "")

    def permits_multiple_records(self, endpoint: str) -> bool:
        """Whether this plan accounted for the endpoint answering more than once."""
        return any(
            declared == endpoint for declared, _ in self.declared_multiple_records
        )

    def multiple_record_reason(self, endpoint: str) -> str:
        return dict(self.declared_multiple_records).get(endpoint, "")

    def as_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "required_endpoints": sorted(e.value for e in self.required_endpoints),
            "rationale": dict(self.rationale),
            "pricing_mode": self.pricing_mode,
            "vendor_gamma_policy": self.vendor_gamma_policy,
            "underlying_price_source": self.underlying_price_source,
            "tier": self.tier,
            "declared_multiple_records": dict(self.declared_multiple_records),
        }


def capture_plan_for(
    *,
    pricing_mode: IvGammaPricingMode,
    vendor_gamma_policy: VendorGammaPolicy,
    underlying_price_source: str,
    tier: Tier,
) -> CapturePlan:
    """Derive the plan from what the session is already configured to do."""
    required: list[tuple[Endpoint, str]] = [
        (
            Endpoint.OPTION_QUOTE_SNAPSHOT,
            "the chain itself; without it there are no contracts",
        ),
        (
            Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT,
            "open interest is the weight on every GEX term",
        ),
    ]

    if pricing_mode.mixes_vendor_and_local_inside_one_calculation:
        required.append(
            (
                Endpoint.OPTION_GREEKS_FIRST_ORDER,
                "vendor implied volatility, which feeds the local gamma",
            )
        )

    if underlying_price_source in VENDOR_INDEX_SOURCES:
        required.append(
            (
                Endpoint.INDEX_PRICE_SNAPSHOT,
                "the underlying every gamma is computed against, read from the "
                "vendor rather than supplied by a caller",
            )
        )

    if vendor_gamma_policy.requires_vendor_gamma:
        required.append(
            (
                Endpoint.OPTION_GREEKS_SECOND_ORDER,
                "the vendor gamma this session compares against its own",
            )
        )

    # A tier that cannot serve an endpoint is refused at configuration load, so
    # reaching here with one is a bug rather than a user error -- but the plan
    # should still say so rather than list a response that cannot arrive.
    from src.adapters.thetadata.endpoints import MINIMUM_TIER

    unreachable = [
        endpoint.value
        for endpoint, _ in required
        if not tier_satisfies(tier, MINIMUM_TIER[endpoint])
    ]
    if unreachable:
        raise ValueError(
            f"tier {tier.value} cannot serve {sorted(unreachable)}, so this plan "
            "describes a capture that cannot happen"
        )

    return CapturePlan(
        required_endpoints=tuple(endpoint for endpoint, _ in required),
        rationale=tuple((endpoint.value, reason) for endpoint, reason in required),
        pricing_mode=pricing_mode.value,
        vendor_gamma_policy=vendor_gamma_policy.value,
        underlying_price_source=underlying_price_source,
        tier=tier.value,
    )
