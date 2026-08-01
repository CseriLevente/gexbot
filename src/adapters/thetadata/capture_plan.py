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
from typing import TYPE_CHECKING, Any

from src.adapters.thetadata.endpoints import Endpoint, Tier, tier_satisfies

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.config.pipeline import IvGammaPricingMode, VendorGammaPolicy

__all__ = ["CapturePlan", "capture_plan_for"]

#: Underlying sources that mean "read the vendor's index print". Anything else
#: is either synthetic or supplied from outside, and needs no index request.
VENDOR_INDEX_SOURCES = frozenset({"vendor_index_snapshot"})


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
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def reason_for(self, endpoint: Endpoint) -> str:
        return dict(self.rationale).get(endpoint.value, "")

    def as_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "required_endpoints": sorted(e.value for e in self.required_endpoints),
            "rationale": dict(self.rationale),
            "pricing_mode": self.pricing_mode,
            "vendor_gamma_policy": self.vendor_gamma_policy,
            "underlying_price_source": self.underlying_price_source,
            "tier": self.tier,
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
