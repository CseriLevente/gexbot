"""What a normalized chain *is*, as a value that can be recomputed.

The gap this closes. Through v2.1.6 the verifier proved a great deal about raw
records -- that they existed, that their bytes hashed as claimed, that every
audit field on the manifest matched the store, that the capture was taken under
this pipeline and this plan. It proved nothing about the ``ChainSnapshot`` the
caller then handed to ``compute_trusted_gex``.

Those are different objects. The chain is the *result* of parsing and joining
the records; nothing tied the two together except the caller passing them in the
same call. So this held:

    chain = pipeline.fetch_chain(...)          # honest
    quote = chain.quotes[0]
    tampered = dataclasses.replace(
        chain,
        quotes=(dataclasses.replace(quote, open_interest=999_999), *chain.quotes[1:]),
    )
    pipeline.compute_trusted_gex(tampered, context=real_context)   # trusted=True

Open interest is a linear weight on every GEX term. Adding 999,999 to one strike
moved the chain's unsigned total by about two orders of magnitude, and the result
still carried ``trusted=True`` and a verified manifest, because the verification
was about bytes nobody had compared the number against.

Two pieces close it:

* a **recipe** -- every input, other than the raw bytes, that determines what
  normalization produces. Rebuilding requires it, and it is stamped onto each
  record at capture time so a chain cannot be re-derived under different rules
  than it was captured for;
* a **canonical hash** over every calculation-relevant field of the chain, so
  "the same chain" is a computable claim rather than an identity comparison.

The hash covers what a GEX number depends on, which is a wider set than it looks:
identity, exact strike, expiry, right, bid, ask, last, open interest, IV *and its
source*, gamma *and its source*, delta, theta, vega, the per-contract underlying,
every selected timestamp, parse issues, exclusion state, and the chain-level
spot, clocks, rate and dividend. A field that changes a gamma and is not here
would be a field a tamper could move for free.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

__all__ = [
    "NORMALIZATION_SCHEMA_VERSION",
    "NormalizationRecipe",
    "NormalizedChainReceipt",
    "canonical_chain_hash",
    "canonical_chain_payload",
]

#: Bumped when the *meaning* of a normalized-chain hash changes -- a new field
#: covered, a different rendering. A receipt taken under older rules must not be
#: compared against one taken under newer ones and read as a mismatch of data.
NORMALIZATION_SCHEMA_VERSION = "normalized-chain/2.1.7"


@dataclass(frozen=True, slots=True)
class NormalizationRecipe:
    """Everything besides the raw bytes that decides what normalization yields.

    Two captures of identical payloads normalize to different chains under
    different rules -- a different IV source, a different duplicate policy, a
    different rate. So rebuilding a chain from records requires this, and the
    digest is stamped onto every record at capture time: a chain rebuilt under
    rules the capture never saw is not the chain the capture produced.
    """

    parser_version: str
    pipeline_fingerprint: str
    model_fingerprint: str
    capture_plan_fingerprint: str
    request_spec_fingerprint: str
    as_of: datetime
    iv_source: str
    duplicate_policy: str
    risk_free_rate: float | None
    dividend_yield: float | None
    spot_source: str
    open_interest_as_of: date | None = None
    #: Digest of the contract universe an independent source said to expect.
    #: ``None`` where no independent source exists, which is the honest state
    #: today -- see OPEN_DECISIONS OD-11.
    expected_universe_fingerprint: str | None = None
    schema_version: str = NORMALIZATION_SCHEMA_VERSION

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "parser_version": self.parser_version,
            "pipeline_fingerprint": self.pipeline_fingerprint,
            "model_fingerprint": self.model_fingerprint,
            "capture_plan_fingerprint": self.capture_plan_fingerprint,
            "request_spec_fingerprint": self.request_spec_fingerprint,
            "as_of": self.as_of.isoformat(),
            "iv_source": self.iv_source,
            "duplicate_policy": self.duplicate_policy,
            "risk_free_rate": _number(self.risk_free_rate),
            "dividend_yield": _number(self.dividend_yield),
            "spot_source": self.spot_source,
            "open_interest_as_of": (
                self.open_interest_as_of.isoformat()
                if self.open_interest_as_of
                else None
            ),
            "expected_universe_fingerprint": self.expected_universe_fingerprint,
        }

    @property
    def recipe_hash(self) -> str:
        """Full SHA-256 of the recipe, this fetch's parameters included.

        What the receipt names: two fetches at different instants are different
        normalizations even under identical rules.
        """
        payload = json.dumps(
            self.semantic_payload(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def rules_fingerprint(self) -> str:
        """SHA-256 of the *rules* alone, without this fetch's parameters.

        This is what a capture session stamps onto its records, and the
        distinction matters. ``as_of`` and ``open_interest_as_of`` are arguments
        to one fetch; the IV source, duplicate policy, rate, dividend, spot
        source, parser, model, plan and request specification are the standing
        configuration. Stamping the full hash would mean a record could only
        ever be verified against a recomputation that guessed the original
        market instant -- so every capture would fail verification as soon as
        the clock moved, which is a check that tests the wrong thing.

        The per-fetch parameters are not thereby unverified: ``as_of`` is on the
        chain and is hashed by ``canonical_chain_hash``, and the settlement date
        has its own evidence type.
        """
        payload = json.dumps(
            {
                key: value
                for key, value in self.semantic_payload().items()
                if key not in ("as_of", "open_interest_as_of")
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "recipe_hash": self.recipe_hash,
            "rules_fingerprint": self.rules_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class NormalizedChainReceipt:
    """A normalized chain, named by what produced it.

    Small on purpose: three digests and two facts. It travels with a trusted
    result so a later reader can ask which capture, under which rules, produced
    which chain -- and recompute all three.
    """

    manifest_hash: str
    recipe_hash: str
    normalized_chain_hash: str
    contract_count: int
    parser_version: str
    schema_version: str = NORMALIZATION_SCHEMA_VERSION

    def matches(self, other: NormalizedChainReceipt) -> bool:
        """Whether two receipts describe the same chain from the same evidence."""
        return (
            self.schema_version == other.schema_version
            and self.manifest_hash == other.manifest_hash
            and self.recipe_hash == other.recipe_hash
            and self.normalized_chain_hash == other.normalized_chain_hash
            and self.contract_count == other.contract_count
            and self.parser_version == other.parser_version
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "manifest_hash": self.manifest_hash,
            "recipe_hash": self.recipe_hash,
            "normalized_chain_hash": self.normalized_chain_hash,
            "contract_count": self.contract_count,
            "parser_version": self.parser_version,
        }


#: Significant figures a float is rounded to before hashing. The same reasoning
#: as ``GexSnapshot.output_hash``: full float repr makes a digest sensitive to
#: last-bit differences between platforms, and twelve figures is far tighter
#: than any change of substance. A tamper that survives this is a tamper that
#: changed nothing.
HASH_SIGNIFICANT_DIGITS = 12


def _number(value: float | None) -> float | None:
    if value is None:
        return None
    return float(f"{float(value):.{HASH_SIGNIFICANT_DIGITS}g}")


def _moment(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _quote_payload(quote: Any) -> dict[str, Any]:
    """Every field of one contract that a GEX number can depend on.

    Deliberately explicit rather than a generic walk of the dataclass. A walk
    would silently start covering a new field, or silently stop covering a
    renamed one, and this is the list a reviewer has to be able to check against
    the engine.
    """
    contract = quote.contract
    timestamps = quote.timestamps
    return {
        # -- identity ------------------------------------------------------
        "contract_id": contract.canonical_id,
        "root": contract.root.value,
        "expiry": contract.expiry.isoformat(),
        "strike": contract.canonical_strike,
        "right": contract.right.value,
        "multiplier": _number(contract.multiplier),
        # -- market state --------------------------------------------------
        "bid": _number(quote.bid),
        "ask": _number(quote.ask),
        "last": _number(quote.last),
        "bid_size": quote.bid_size,
        "ask_size": quote.ask_size,
        "volume": quote.volume,
        "open_interest": quote.open_interest,
        # -- the pricing inputs, each with its provenance -------------------
        "iv_value": _number(quote.iv.value),
        "iv_source": getattr(quote.iv.source, "value", str(quote.iv.source)),
        "iv_is_usable": bool(quote.iv.is_usable),
        "gamma": _number(quote.gamma),
        "gamma_source": "vendor" if quote.gamma is not None else "absent",
        "delta": _number(quote.delta),
        "theta": _number(quote.theta),
        "vega": _number(quote.vega),
        "underlying_price": _number(quote.underlying_price),
        # -- every clock this record carries --------------------------------
        "quote_timestamp": _moment(timestamps.quote_timestamp),
        "greeks_timestamp": _moment(timestamps.greeks_timestamp),
        "iv_timestamp": _moment(timestamps.iv_timestamp),
        "underlying_timestamp": _moment(timestamps.underlying_timestamp),
        "open_interest_as_of": _moment(timestamps.open_interest_as_of),
        "selected_timestamp_sources": _selected(timestamps),
        # -- what the adapter could not read, and what was dropped ----------
        "parse_issues": sorted(tuple(pair) for pair in quote.parse_issues),
        "excluded": not quote.iv.is_usable and quote.gamma is None,
    }


def _selected(timestamps: Any) -> dict[str, Any]:
    """Which source clock was chosen for each role, canonically."""
    selected = getattr(timestamps, "selected_timestamp_sources", None) or {}
    return {
        role: {str(k): str(v) for k, v in sorted(detail.items())}
        for role, detail in sorted(selected.items())
    }


def canonical_chain_payload(chain: Any) -> dict[str, Any]:
    """The whole chain, as the value its GEX depends on.

    Quotes are sorted by canonical identity, so two chains that assembled the
    same contracts in a different order hash the same -- ordering is an artefact
    of the join, not a property of the market.
    """
    return {
        "schema_version": NORMALIZATION_SCHEMA_VERSION,
        "as_of": chain.as_of.isoformat(),
        "spot": _number(chain.spot),
        "spot_timestamp": _moment(chain.spot_timestamp),
        "risk_free_rate": _number(chain.risk_free_rate),
        "dividend_yield": _number(chain.dividend_yield),
        "source": chain.source,
        # The chain-level clocks record *when this process made the request*:
        # ``request_started_at``, ``response_received_at`` and ``normalized_at``
        # all come from the client's own clock. A rebuild necessarily runs at a
        # different moment, so hashing them would make the comparison fail
        # always -- and a check that can never pass is not a check.
        #
        # They are not unverified. The per-record request and response clocks
        # are compared against the store, field by field, by ``verify_capture``:
        # that is where a timestamp is evidence about a response rather than
        # about the machine that asked for it. What the *chain* hash covers is
        # the clocks that describe the market data -- ``as_of``, the spot
        # timestamp, and every per-contract source clock below.
        "clock_ordering_holds": _clocks_are_ordered(chain.clocks),
        "expected_contract_count": chain.expected_contract_count,
        "completeness_status": getattr(
            chain.completeness_status, "value", str(chain.completeness_status)
        ),
        "effective_model_fingerprint": _effective_model_fingerprint(chain),
        "quotes": sorted(
            (_quote_payload(quote) for quote in chain.quotes),
            key=lambda entry: entry["contract_id"],
        ),
    }


def _clocks_are_ordered(clocks: Any) -> bool:
    """Whether the request/response/normalize clocks run forwards.

    The part of the snapshot clocks that survives a rebuild: their *values* are
    process wall-clocks and legitimately differ, but a response that arrived
    before its request was sent is a defect in either capture.
    """
    moments = [
        clocks.request_started_at,
        clocks.response_received_at,
        clocks.normalized_at,
    ]
    present = [moment for moment in moments if moment is not None]
    return all(a <= b for a, b in itertools.pairwise(present))


def _effective_model_fingerprint(chain: Any) -> str | None:
    """The resolved model the chain was normalized under, if it carries one.

    Read from ``meta`` because that is where the pipeline records it. Absent on
    a chain assembled outside the pipeline, which is a real state and not a
    failure -- such a chain simply cannot be the subject of a trusted
    calculation, for other reasons.
    """
    meta = getattr(chain, "meta", None)
    if not isinstance(meta, dict):
        return None
    model = meta.get("effective_model")
    if isinstance(model, dict):
        value = model.get("fingerprint")
        return str(value) if value is not None else None
    return None


def canonical_chain_hash(chain: Any) -> str:
    """Full SHA-256 over every calculation-relevant field of a chain."""
    payload = json.dumps(
        canonical_chain_payload(chain),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
