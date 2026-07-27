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


MINIMUM_TIER: Final[dict[Endpoint, Tier]] = {
    Endpoint.OPTION_QUOTE_SNAPSHOT: Tier.VALUE,
    Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT: Tier.VALUE,
    # delta/theta/vega/rho/implied_vol -- but NOT gamma.
    Endpoint.OPTION_GREEKS_FIRST_ORDER: Tier.STANDARD,
    # gamma/vanna/charm/vomma/veta live here.
    Endpoint.OPTION_GREEKS_SECOND_ORDER: Tier.PRO,
    Endpoint.OPTION_GREEKS_ALL: Tier.PRO,
    Endpoint.INDEX_PRICE_SNAPSHOT: Tier.VALUE,
    Endpoint.OPTION_QUOTE_HISTORY: Tier.VALUE,
    Endpoint.OPTION_OPEN_INTEREST_HISTORY: Tier.VALUE,
    Endpoint.INDEX_PRICE_HISTORY: Tier.VALUE,
}

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
