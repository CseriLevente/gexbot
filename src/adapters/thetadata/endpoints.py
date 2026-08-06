"""ThetaData v3 endpoint map, with the subscription tier each one needs.

Verified against docs.thetadata.us in July 2026. The tier column is the point of
this file: it is what turns "buy ThetaData" into a decision with a price on it.

The single most consequential fact here: **gamma is a second-order greek**, and
``/v3/option/snapshot/greeks/second_order`` requires **Pro** ($160/mo).
``implied_vol`` is returned by ``/v3/option/snapshot/greeks/first_order``, which
needs only **Standard** ($80/mo).

ThetaData Standard therefore *appears* to expose the inputs a local gamma
calculation requires. Two things follow from that, and only two:

* it is $80/mo cheaper, which is a fact about a price list;
* the gamma at spot and the gamma on the zero-gamma grid would come from one
  model rather than two, which is a property of our own code.

What does **not** follow, and is claimed nowhere: that Standard is sufficient in
practice, or that a locally-derived gamma agrees numerically with ThetaData's
own. **Numerical consistency with ThetaData vendor gamma has not been validated
on live data** -- no request in this repository has ever reached the vendor.
See ``docs/OPEN_DECISIONS.md`` OD-3.

See ``docs/handoff/data-requirements.md`` for the full comparison.

All requests go to the local Theta Terminal, not to a cloud host: the Terminal
process must be running or every call fails. That is an operational dependency
the monitoring layer has to watch, alongside the feeds themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

DEFAULT_BASE_URL: Final = "http://127.0.0.1:25503"


class Tier(str, Enum):
    FREE = "free"
    VALUE = "value"
    STANDARD = "standard"
    PRO = "pro"


TIER_MONTHLY_USD: Final[dict[Tier, int]] = {
    Tier.FREE: 0,
    Tier.VALUE: 40,
    Tier.STANDARD: 80,
    Tier.PRO: 160,
}

_TIER_ORDER: Final = (Tier.FREE, Tier.VALUE, Tier.STANDARD, Tier.PRO)


class Endpoint(str, Enum):
    OPTION_QUOTE_SNAPSHOT = "/v3/option/snapshot/quote"
    OPTION_OPEN_INTEREST_SNAPSHOT = "/v3/option/snapshot/open_interest"
    OPTION_GREEKS_FIRST_ORDER = "/v3/option/snapshot/greeks/first_order"
    OPTION_GREEKS_SECOND_ORDER = "/v3/option/snapshot/greeks/second_order"
    OPTION_GREEKS_ALL = "/v3/option/snapshot/greeks/all"
    INDEX_PRICE_SNAPSHOT = "/v3/index/snapshot/price"
    OPTION_QUOTE_HISTORY = "/v3/option/history/quote"
    OPTION_OPEN_INTEREST_HISTORY = "/v3/option/history/open_interest"
    INDEX_PRICE_HISTORY = "/v3/index/history/price"
    #: The vendor's dedicated listing of contracts that have a quote for a
    #: session. Added in v2.1.16 and captured as *evidence*, not as authority:
    #: see :data:`RESPONSE_CAPABILITIES` for why documentation saying an
    #: endpoint lists contracts does not make it a proof of our universe.
    OPTION_CONTRACT_LIST_QUOTE = "/v3/option/list/contracts/quote"


#: Endpoints that take the **underlying index** symbol rather than the option
#: root. The list is short and explicit because getting it wrong is silent: an
#: index request carrying an option root returns whatever the vendor makes of a
#: symbol that is not an index, and the result becomes the spot every gamma in
#: the chain is computed against.
INDEX_ENDPOINTS: Final[frozenset[str]] = frozenset(
    {
        Endpoint.INDEX_PRICE_SNAPSHOT.value,
        Endpoint.INDEX_PRICE_HISTORY.value,
    }
)


MINIMUM_TIER: Final[dict[Endpoint, Tier]] = {
    Endpoint.OPTION_QUOTE_SNAPSHOT: Tier.VALUE,
    Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT: Tier.VALUE,
    # delta/theta/vega/rho/implied_vol -- but NOT gamma.
    Endpoint.OPTION_GREEKS_FIRST_ORDER: Tier.STANDARD,
    # gamma/vanna/charm/vomma/veta live here.
    Endpoint.OPTION_GREEKS_SECOND_ORDER: Tier.PRO,
    Endpoint.OPTION_GREEKS_ALL: Tier.PRO,
    # **Standard, per the current documented subscription table.** v2.1.16
    # modelled this at Value, so a Value-tier profile configured for
    # ``vendor_index_snapshot`` passed its tier check and would have been
    # refused by the vendor at the one endpoint the spot comes from.
    Endpoint.INDEX_PRICE_SNAPSHOT: Tier.STANDARD,
    Endpoint.OPTION_QUOTE_HISTORY: Tier.VALUE,
    Endpoint.OPTION_OPEN_INTEREST_HISTORY: Tier.VALUE,
    Endpoint.INDEX_PRICE_HISTORY: Tier.VALUE,
    # Listing contracts is a Value-tier capability at the documented tiers, so
    # the shipped Standard profile can request it. Modelled at the minimum the
    # vendor documents rather than at the tier we happen to hold: a tier check
    # that passes because our subscription is generous would stop catching the
    # case it exists for.
    Endpoint.OPTION_CONTRACT_LIST_QUOTE: Tier.VALUE,
}


@dataclass(frozen=True, slots=True)
class ResponseCapabilities:
    """What a response can and cannot establish about the contract universe.

    Four separate questions, because v2.1.9 collapsed them into one and got the
    wrong answer. Its universe resolver accepted any endpoint returning a row
    per contract as a ``VENDOR_CONTRACT_LIST``, so a quote snapshot -- which
    returns the contracts the vendor *chose to send* -- established
    ``MEASURED_COMPLETE`` for the whole request.

    That is the v2 defect in a new place. A response enumerating its own rows
    says which contracts arrived; it cannot say which were owed, because a
    truncated response enumerates its own rows perfectly.
    """

    #: One row per contract in the response. Necessary for extracting
    #: identities, and on its own it establishes nothing about coverage.
    enumerates_rows: bool = False

    #: The response is contractually the complete set for the request. No
    #: ThetaData endpoint this repository has verified makes that promise.
    enumerates_request_universe: bool = False

    #: The response carries page/total metadata a resolver can read back.
    carries_pagination_metadata: bool = False

    #: A dedicated listing endpoint whose purpose is to enumerate contracts,
    #: as opposed to a market-data snapshot that happens to have rows.
    is_dedicated_contract_list: bool = False

    @property
    def can_establish_full_coverage(self) -> bool:
        """Whether a response of this kind could ever prove a complete universe."""
        return self.enumerates_request_universe or self.is_dedicated_contract_list

    @property
    def can_supply_identities(self) -> bool:
        return self.enumerates_rows


#: What each endpoint's response can support. Every option snapshot enumerates
#: its own rows and nothing more: none is a listing endpoint, none promises the
#: complete requested universe, and none returns pagination metadata this
#: repository has verified. See OPEN_DECISIONS OD-11.
RESPONSE_CAPABILITIES: Final[dict[Endpoint, ResponseCapabilities]] = {
    Endpoint.OPTION_QUOTE_SNAPSHOT: ResponseCapabilities(enumerates_rows=True),
    Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT: ResponseCapabilities(enumerates_rows=True),
    Endpoint.OPTION_GREEKS_FIRST_ORDER: ResponseCapabilities(enumerates_rows=True),
    Endpoint.OPTION_GREEKS_SECOND_ORDER: ResponseCapabilities(enumerates_rows=True),
    Endpoint.OPTION_GREEKS_ALL: ResponseCapabilities(enumerates_rows=True),
    Endpoint.OPTION_QUOTE_HISTORY: ResponseCapabilities(enumerates_rows=True),
    Endpoint.OPTION_OPEN_INTEREST_HISTORY: ResponseCapabilities(enumerates_rows=True),
    # An index print names one instrument. It enumerates nothing.
    Endpoint.INDEX_PRICE_SNAPSHOT: ResponseCapabilities(),
    Endpoint.INDEX_PRICE_HISTORY: ResponseCapabilities(),
    # **A dedicated listing, and deliberately not yet an authority.**
    #
    # ``is_dedicated_contract_list`` says what the endpoint is *for*. It does
    # not say that its scope is the scope of our snapshot request, and that is
    # the whole question: a listing of every contract quoted on a session is a
    # different set from the contracts a request filtered by ``max_dte`` and
    # ``strike_range`` was owed. Until a real response has been compared
    # against a real snapshot, treating the two as the same set would be the
    # v2.1.9 defect with a better-named endpoint.
    #
    # So: it enumerates rows, it is a dedicated list, and
    # ``enumerates_request_universe`` stays False. See OPEN_DECISIONS OD-11.
    Endpoint.OPTION_CONTRACT_LIST_QUOTE: ResponseCapabilities(
        enumerates_rows=True,
        is_dedicated_contract_list=True,
    ),
}

#: Convenience for the resolver and the architecture test.
#:
#: Non-empty since v2.1.16: ``/v3/option/list/contracts/quote`` exists and the
#: first session captures it. What has *not* changed is what it authorizes.
#: Membership here means "this endpoint's purpose is to list contracts"; it does
#: not mean the list it returns is the set our filtered snapshot request was
#: owed. That comparison needs a real response and has never been made.
DEDICATED_CONTRACT_LIST_ENDPOINTS: Final[frozenset[str]] = frozenset(
    endpoint.value
    for endpoint, capability in RESPONSE_CAPABILITIES.items()
    if capability.is_dedicated_contract_list
)


def capabilities_of(endpoint: str) -> ResponseCapabilities:
    """What an endpoint's response can establish, by URL path.

    Unknown endpoints get the empty capability rather than a permissive
    default: a response nobody has characterised proves nothing, and defaulting
    the other way is how a new endpoint would silently acquire authority.
    """
    for candidate, capability in RESPONSE_CAPABILITIES.items():
        if candidate.value == endpoint:
            return capability
    return ResponseCapabilities()


# Response columns, in the order the CSV format returns them.
RESPONSE_FIELDS: Final[dict[Endpoint, tuple[str, ...]]] = {
    Endpoint.OPTION_QUOTE_SNAPSHOT: (
        "timestamp",
        "symbol",
        "expiration",
        "strike",
        "right",
        "bid_size",
        "bid_exchange",
        "bid",
        "bid_condition",
        "ask_size",
        "ask_exchange",
        "ask",
        "ask_condition",
    ),
    Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT: (
        "timestamp",
        "symbol",
        "expiration",
        "strike",
        "right",
        "open_interest",
    ),
    # **Modelled, not verified.** The four columns that identify a contract are
    # what a listing must carry to be a listing at all, and they are what the
    # first session will check the real response against. If the vendor sends
    # more, the extra columns are captured in the bytes and reported by the
    # parser; if it sends fewer, that is the finding.
    Endpoint.OPTION_CONTRACT_LIST_QUOTE: (
        "symbol",
        "expiration",
        "strike",
        "right",
    ),
    # The documented v3 index response. ``price``, not ``index_price``: the
    # v2.1.16 adapter read a column the vendor does not send, so a correct
    # response produced no snapshot at all.
    Endpoint.INDEX_PRICE_SNAPSHOT: (
        "timestamp",
        "symbol",
        "price",
    ),
    Endpoint.OPTION_GREEKS_FIRST_ORDER: (
        "symbol",
        "expiration",
        "strike",
        "right",
        "timestamp",
        "bid",
        "ask",
        "delta",
        "theta",
        "vega",
        "rho",
        "epsilon",
        "lambda",
        "implied_vol",
        "iv_error",
        "underlying_timestamp",
        "underlying_price",
    ),
    Endpoint.OPTION_GREEKS_SECOND_ORDER: (
        "symbol",
        "expiration",
        "strike",
        "right",
        "timestamp",
        "bid",
        "ask",
        "gamma",
        "vanna",
        "charm",
        "vomma",
        "veta",
        "implied_vol",
        "iv_error",
        "underlying_timestamp",
        "underlying_price",
    ),
}

# Concurrent-request limits per tier. The SPX chain is large enough that these
# matter: at Standard you get 4 concurrent requests, so a full-chain pull has to
# be batched by expiration rather than fired off in one burst.
CONCURRENT_REQUESTS: Final[dict[Tier, int]] = {
    Tier.FREE: 1,
    Tier.VALUE: 2,
    Tier.STANDARD: 4,
    Tier.PRO: 8,
}


def tier_satisfies(available: Tier, required: Tier) -> bool:
    return _TIER_ORDER.index(available) >= _TIER_ORDER.index(required)


def endpoints_for_tier(tier: Tier) -> tuple[Endpoint, ...]:
    return tuple(
        endpoint
        for endpoint, required in MINIMUM_TIER.items()
        if tier_satisfies(tier, required)
    )


def requires_shadow_gamma(tier: Tier) -> bool:
    """True when the tier cannot supply gamma and the pricer must derive it."""
    return not tier_satisfies(tier, MINIMUM_TIER[Endpoint.OPTION_GREEKS_SECOND_ORDER])


def build_url(
    endpoint: Endpoint,
    params: dict[str, str | int | float],
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    """Assemble a request URL. Parameters are emitted in sorted order.

    Sorted rather than insertion order so a URL is reproducible across runs --
    replay compares request logs, and dict ordering is not something to bet an
    audit trail on.
    """
    from urllib.parse import urlencode

    query = urlencode({key: params[key] for key in sorted(params)})
    return f"{base_url.rstrip('/')}{endpoint.value}?{query}"
