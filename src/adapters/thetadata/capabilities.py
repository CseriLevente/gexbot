"""What each ThetaData subscription tier actually exposes.

A pricing mode is a claim about which inputs are available. Selecting
``VENDOR_GAMMA_VALIDATION`` on a Standard subscription is not a configuration
preference -- it is a request for a field the tier does not return, and it will
fail at the first request rather than at load time.

Every entry is one of three states. ``UNCERTAIN`` is used deliberately and is
not a soft ``SUPPORTED``: it means the repository's endpoint understanding does
not settle the question, and anything depending on it is blocked rather than
assumed. Nothing here has been checked against a live subscription.
"""

from __future__ import annotations

from enum import Enum

from src.adapters.thetadata.endpoints import Tier

__all__ = [
    "TIER_CAPABILITIES",
    "Capability",
    "CapabilityRequirement",
    "TierCapabilityReport",
    "assess_tier",
]


class Capability(str, Enum):
    """Whether a tier provides an input."""

    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    #: The repository's documented endpoint understanding does not settle it.
    #: Treated as unavailable wherever a decision depends on it.
    UNCERTAIN = "UNCERTAIN"

    @property
    def is_usable(self) -> bool:
        return self is Capability.SUPPORTED


#: Capability names, kept as constants so a typo in a requirement cannot silently
#: read as "not required".
OPTION_QUOTES = "option_quotes"
OPEN_INTEREST = "open_interest"
FIRST_ORDER_GREEKS = "first_order_greeks"
VENDOR_IV_FIELDS = "vendor_iv_fields"
SECOND_ORDER_GAMMA = "second_order_gamma"
CHAIN_SNAPSHOT_ENDPOINT = "chain_snapshot_endpoint"
CONTRACT_LIST_ENDPOINT = "contract_list_endpoint"

#: Derived from ``src/adapters/thetadata/endpoints.py``, which records the
#: minimum tier per endpoint from vendor documentation read in July 2026.
#:
#: ``contract_list_endpoint`` is ``UNCERTAIN`` at every tier on purpose: no
#: contract-list endpoint has been verified, which is why chain completeness is
#: PARTIALLY_OBSERVED. See docs/OPEN_DECISIONS.md OD-11.
TIER_CAPABILITIES: dict[Tier, dict[str, Capability]] = {
    Tier.FREE: {
        OPTION_QUOTES: Capability.UNSUPPORTED,
        OPEN_INTEREST: Capability.UNSUPPORTED,
        FIRST_ORDER_GREEKS: Capability.UNSUPPORTED,
        VENDOR_IV_FIELDS: Capability.UNSUPPORTED,
        SECOND_ORDER_GAMMA: Capability.UNSUPPORTED,
        CHAIN_SNAPSHOT_ENDPOINT: Capability.UNSUPPORTED,
        CONTRACT_LIST_ENDPOINT: Capability.UNCERTAIN,
    },
    Tier.VALUE: {
        OPTION_QUOTES: Capability.SUPPORTED,
        OPEN_INTEREST: Capability.SUPPORTED,
        # implied_vol arrives on the first-order greeks endpoint, which needs
        # Standard. A Value subscription therefore has no vendor IV at all.
        FIRST_ORDER_GREEKS: Capability.UNSUPPORTED,
        VENDOR_IV_FIELDS: Capability.UNSUPPORTED,
        SECOND_ORDER_GAMMA: Capability.UNSUPPORTED,
        CHAIN_SNAPSHOT_ENDPOINT: Capability.SUPPORTED,
        CONTRACT_LIST_ENDPOINT: Capability.UNCERTAIN,
    },
    Tier.STANDARD: {
        OPTION_QUOTES: Capability.SUPPORTED,
        OPEN_INTEREST: Capability.SUPPORTED,
        FIRST_ORDER_GREEKS: Capability.SUPPORTED,
        VENDOR_IV_FIELDS: Capability.SUPPORTED,
        # Gamma is a second-order greek and needs Pro.
        SECOND_ORDER_GAMMA: Capability.UNSUPPORTED,
        CHAIN_SNAPSHOT_ENDPOINT: Capability.SUPPORTED,
        CONTRACT_LIST_ENDPOINT: Capability.UNCERTAIN,
    },
    Tier.PRO: {
        OPTION_QUOTES: Capability.SUPPORTED,
        OPEN_INTEREST: Capability.SUPPORTED,
        FIRST_ORDER_GREEKS: Capability.SUPPORTED,
        VENDOR_IV_FIELDS: Capability.SUPPORTED,
        SECOND_ORDER_GAMMA: Capability.SUPPORTED,
        CHAIN_SNAPSHOT_ENDPOINT: Capability.SUPPORTED,
        CONTRACT_LIST_ENDPOINT: Capability.UNCERTAIN,
    },
}


class CapabilityRequirement:
    """What a pricing mode needs from the subscription."""

    #: Any run needs a chain and its weights.
    BASELINE = (OPTION_QUOTES, OPEN_INTEREST, CHAIN_SNAPSHOT_ENDPOINT)
    #: Vendor IV comes from the first-order greeks endpoint.
    VENDOR_IV = (*BASELINE, FIRST_ORDER_GREEKS, VENDOR_IV_FIELDS)
    #: Comparing vendor gamma needs the second-order endpoint.
    VENDOR_GAMMA = (*VENDOR_IV, SECOND_ORDER_GAMMA)


class TierCapabilityReport:
    """Whether a tier satisfies a set of requirements."""

    __slots__ = ("missing", "required", "tier", "uncertain")

    def __init__(
        self,
        *,
        tier: Tier,
        required: tuple[str, ...],
        missing: tuple[str, ...],
        uncertain: tuple[str, ...],
    ) -> None:
        self.tier = tier
        self.required = required
        self.missing = missing
        self.uncertain = uncertain

    @property
    def satisfied(self) -> bool:
        """Uncertain counts against, not for.

        A capability we cannot confirm is one we must not depend on -- the
        alternative is discovering it at the first paid request.
        """
        return not self.missing and not self.uncertain

    def as_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier.value,
            "required": list(self.required),
            "missing": list(self.missing),
            "uncertain": list(self.uncertain),
            "satisfied": self.satisfied,
        }


def assess_tier(tier: Tier, required: tuple[str, ...]) -> TierCapabilityReport:
    """Check one tier against one requirement set."""
    capabilities = TIER_CAPABILITIES[tier]
    missing = tuple(
        sorted(
            name
            for name in required
            if capabilities.get(name, Capability.UNCERTAIN) is Capability.UNSUPPORTED
        )
    )
    uncertain = tuple(
        sorted(
            name
            for name in required
            if capabilities.get(name, Capability.UNCERTAIN) is Capability.UNCERTAIN
        )
    )
    return TierCapabilityReport(
        tier=tier, required=required, missing=missing, uncertain=uncertain
    )
