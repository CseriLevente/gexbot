"""GEX views 1-4: unsigned concentration, naive signed, expiry buckets, strikes.

The single per-contract quantity everything else is built from:

    GEX_i = gamma_i * OI_i * M * S * dS,    dS = spot_move_pct * S

so with the 1% convention ``GEX_i = gamma_i * OI_i * M * S^2 * 0.01``. Read it
as: dollars of dealer delta that must be re-hedged for a 1% move in spot.

Sign is applied on top of that magnitude and is a *proxy* -- see
:class:`src.domain.gex.SignConvention`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Any

from src.domain.contracts import (
    ChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionRight,
)
from src.domain.effective_model import (
    EffectiveModelInputs,
    ResolutionIssue,
    resolve_effective_inputs,
)
from src.domain.gex import (
    BucketGex,
    ExpiryBucket,
    OptionUniverse,
    SignConvention,
    StrikeGex,
)
from src.domain.iv import GammaComparison, IVSource
from src.domain.model_spec import ModelSpec
from src.domain.normalize import validate_chain
from src.domain.validation import ValidationCode, ValidationReport
from src.gex.config import BUCKET_BOUNDS, GexEngineConfig
from src.gex.sessions import calendar_dte


def bucket_for_dte(dte: int) -> ExpiryBucket:
    """Map calendar DTE to its bucket. Negative DTE is a caller error."""
    if dte < 0:
        raise ValueError(f"expired contract reached bucketing: dte={dte}")
    for bucket, upper in BUCKET_BOUNDS:
        if dte <= upper:
            return bucket
    return ExpiryBucket.DTE_GT_30


def sign_for(right: OptionRight, convention: SignConvention) -> float:
    """Dealer gamma sign under a stated convention.

    ``FLOW_ADJUSTED`` has no static answer -- the sign comes from classified
    flow (Cboe Open-Close), so asking for it here is a programming error.
    """
    if convention is SignConvention.DEALER_LONG_CALLS_SHORT_PUTS:
        return 1.0 if right is OptionRight.CALL else -1.0
    if convention is SignConvention.DEALER_SHORT_CALLS_LONG_PUTS:
        return -1.0 if right is OptionRight.CALL else 1.0
    raise ValueError(
        f"{convention} has no static sign; it requires per-contract classified "
        "flow and must be resolved by the flow-adjusted model"
    )


def notional_gex(
    *,
    gamma: float,
    open_interest: int,
    multiplier: float,
    spot: float,
    spot_move_pct: float,
) -> float:
    """Dollar gamma notional per ``spot_move_pct`` move. Always non-negative."""
    return abs(gamma) * open_interest * multiplier * spot * (spot_move_pct * spot)


class GammaSource(str, Enum):
    VENDOR = "vendor"
    SHADOW_PRICER = "shadow_pricer"


class MixedModelError(ValueError):
    """A chain contains more than one effective model and strict mode is on.

    Per-contract IV fallback means one chain can end up priced under several
    models. In research mode that is allowed and reported; in strict mode it is
    refused, because a single aggregate over several models is a number without
    a stated meaning.
    """


@dataclass(frozen=True, slots=True)
class ModelDistribution:
    """What models actually priced this chain, and in what proportion.

    v2.1.1 reported ``model_fingerprint`` from the *configured* spec, so a chain
    where half the contracts fell back to a different IV source still claimed a
    single model -- and any per-contract read took whichever contract came
    first. Iteration order decided what the snapshot said about itself.

    Every count here is sorted, so two runs over the same data serialise
    identically.
    """

    iv_source_counts: dict[str, int]
    gamma_source_counts: dict[str, int]
    effective_model_fingerprint_counts: dict[str, int]
    fallback_reason_counts: dict[str, int]

    @property
    def mixed_iv_sources(self) -> bool:
        return len(self.iv_source_counts) > 1

    @property
    def mixed_gamma_sources(self) -> bool:
        return len(self.gamma_source_counts) > 1

    @property
    def mixed_effective_models(self) -> bool:
        return len(self.effective_model_fingerprint_counts) > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "iv_source_counts": dict(sorted(self.iv_source_counts.items())),
            "gamma_source_counts": dict(sorted(self.gamma_source_counts.items())),
            "effective_model_fingerprint_counts": dict(
                sorted(self.effective_model_fingerprint_counts.items())
            ),
            "fallback_reason_counts": dict(sorted(self.fallback_reason_counts.items())),
            "mixed_iv_sources": self.mixed_iv_sources,
            "mixed_gamma_sources": self.mixed_gamma_sources,
            "mixed_effective_models": self.mixed_effective_models,
            # Named per the spec; same numbers, stated as "contracts by X".
            "contracts_by_iv_source": dict(sorted(self.iv_source_counts.items())),
            "contracts_by_effective_model": dict(
                sorted(self.effective_model_fingerprint_counts.items())
            ),
        }


def build_model_distribution(
    contracts: tuple[ContractGex, ...],
) -> ModelDistribution:
    """Count the models that actually priced the included contracts."""
    iv_counts: Counter[str] = Counter()
    gamma_counts: Counter[str] = Counter()
    model_counts: Counter[str] = Counter()
    fallbacks: Counter[str] = Counter()

    for contract in contracts:
        effective = contract.effective
        iv_counts[effective.implied_volatility_source.value] += 1
        gamma_counts[contract.gamma_source.value] += 1
        model_counts[effective.fingerprint()] += 1
        for issue in effective.issues:
            fallbacks[issue.value] += 1

    return ModelDistribution(
        iv_source_counts=dict(iv_counts),
        gamma_source_counts=dict(gamma_counts),
        effective_model_fingerprint_counts=dict(model_counts),
        fallback_reason_counts=dict(fallbacks),
    )


@dataclass(frozen=True, slots=True)
class ModelCompletenessReport:
    """Whether the model *could* resolve, separately from whether it *did*.

    Two layers, because v2.1.1 collapsed them into one and lost the first.
    ``model_parameter_completeness`` read the surviving contracts, so an empty
    result set had no missing inputs and a chain where every contract was
    excluded reported a fully specified model. The question "why did nothing
    survive?" went quiet exactly when it was being asked.

    Static completeness is a property of the configuration alone. It holds for
    an empty chain, a full chain, and a chain that has not been fetched yet.
    """

    static_model_complete: bool
    static_missing_inputs: tuple[str, ...]
    resolved_contract_count: int
    unresolved_contract_count: int
    per_input_failure_counts: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "static_model_complete": self.static_model_complete,
            "static_missing_inputs": list(self.static_missing_inputs),
            "resolved_contract_count": self.resolved_contract_count,
            "unresolved_contract_count": self.unresolved_contract_count,
            "per_input_failure_counts": dict(
                sorted(self.per_input_failure_counts.items())
            ),
        }


def static_model_missing_inputs(spec: ModelSpec) -> tuple[str, ...]:
    """Inputs the configuration cannot supply, whatever data arrives.

    Looks only at the spec. No chain, no contracts, no vendor response.
    """
    from src.domain.model_spec import (
        DividendSource,
        RateSource,
        UnderlyingPriceSource,
    )

    missing: list[str] = []
    if (
        spec.risk_free_rate_source is RateSource.CONFIGURED_CONSTANT
        and spec.risk_free_rate is None
    ):
        missing.append(
            "risk_free_rate: source is configured_constant but no constant is set"
        )
    if (
        spec.dividend_yield_source is DividendSource.CONFIGURED_CONSTANT
        and spec.dividend_yield is None
    ):
        missing.append(
            "dividend_yield: source is configured_constant but no constant is set"
        )
    if (
        spec.underlying_price_source is UnderlyingPriceSource.CONFIGURED_CONSTANT
        and spec.configured_underlying_price is None
    ):
        missing.append(
            "underlying_price: source is configured_constant but no constant is set"
        )
    if not spec.expiration_timestamp_rule.is_supported:
        missing.append(
            f"expiration_rule: {spec.expiration_timestamp_rule.value} is not "
            f"supported ({spec.expiration_timestamp_rule.unsupported_reason})"
        )
    if spec.minimum_time_to_expiry_minutes < 0.0:
        missing.append("minimum_time_to_expiry_minutes: negative")
    return tuple(missing)


def build_model_completeness(
    spec: ModelSpec, result: ContractGexResult
) -> ModelCompletenessReport:
    """Static configuration completeness plus per-contract resolution."""
    failures: Counter[str] = Counter()
    resolved = 0
    for contract in result.contracts:
        if contract.effective.is_fully_specified:
            resolved += 1
        for entry in contract.effective.missing_inputs:
            failures[entry] += 1

    static_missing = static_model_missing_inputs(spec)
    return ModelCompletenessReport(
        static_model_complete=not static_missing,
        static_missing_inputs=static_missing,
        resolved_contract_count=resolved,
        unresolved_contract_count=len(result.contracts) - resolved,
        per_input_failure_counts=dict(failures),
    )


class ExclusionReason(str, Enum):
    """Why a supplied quote did not reach the aggregates.

    Counted rather than logged: ``chain_completeness`` needs to state *why* the
    chain shrank, and an operator debugging a thin snapshot needs the breakdown.
    """

    VALIDATION_REJECTED = "validation_rejected"
    EXPIRED = "expired"
    BEYOND_MAX_DTE = "beyond_max_dte"
    CROSSED_QUOTE = "crossed_quote"
    NO_OPEN_INTEREST = "no_open_interest"
    NO_GAMMA_SOURCE = "no_gamma_source"
    NON_FINITE_GAMMA = "non_finite_gamma"
    #: The configured underlying-price source produced nothing usable for
    #: this contract. GEX scales by spot squared, so there is no number to
    #: report -- not a number computed against a different spot.
    NO_UNDERLYING_PRICE = "no_underlying_price"


@dataclass(frozen=True, slots=True)
class ContractGex:
    """One contract's contribution to every aggregate view."""

    contract: OptionContract
    dte: int
    bucket: ExpiryBucket
    time_to_expiry: float
    gamma: float
    implied_vol: float | None
    iv_source: IVSource | None
    open_interest: int
    unsigned_gex: float
    signed_gex: float
    # Dealer sign under the snapshot's convention, stored rather than re-derived.
    # Deriving it from ``signed_gex`` would break for contracts whose gamma
    # rounds to zero at the current spot -- exactly the far-wing strikes that
    # come alive once the zero-gamma grid moves spot toward them.
    sign: float
    gamma_source: GammaSource
    #: The one resolved model this contract was priced with. Carried so the
    #: zero-gamma grid and the gamma comparison reprice the SAME model rather
    #: than rebuilding a similar one.
    effective: EffectiveModelInputs
    vendor_gamma: float | None = None

    @property
    def moneyness(self) -> float | None:
        """Log-moneyness, or None when there is no spot to measure against.

        A contract without a resolved spot has no moneyness -- not a moneyness
        of zero, which would place it exactly at the money.
        """
        import math

        spot = self.effective.spot
        if spot is None or spot <= 0.0 or self.contract.strike <= 0.0:
            return None
        return math.log(self.contract.strike / spot)


@dataclass(frozen=True, slots=True)
class ContractGexResult:
    contracts: tuple[ContractGex, ...]
    validation: ValidationReport
    total_quotes: int
    exclusions: dict[ExclusionReason, int]
    excluded_expiries: tuple[str, ...]
    vendor_gamma_count: int
    shadow_gamma_count: int
    # Unsigned GEX that *would* have been contributed by contracts excluded for a
    # reason other than being unusable (i.e. DTE filtering). Contracts rejected by
    # validation have no trustworthy gamma, so they contribute nothing measurable.
    excluded_unsigned_gex: float = 0.0

    @property
    def usable_ratio(self) -> float:
        return len(self.contracts) / self.total_quotes if self.total_quotes else 0.0

    @property
    def crossed_ratio(self) -> float:
        """Share of supplied quotes with a torn book.

        Counts both the validation-stage detections and any the engine dropped
        directly, so the ratio does not change meaning when
        ``drop_crossed_quotes`` flips a crossed quote between warning and error.
        """
        if not self.total_quotes:
            return 0.0
        crossed = self.exclusions.get(ExclusionReason.CROSSED_QUOTE, 0)
        crossed += self.validation.count(ValidationCode.CROSSED_MARKET)
        return min(crossed / self.total_quotes, 1.0)

    def exclusion_counts(self) -> dict[str, int]:
        return {
            reason.value: count for reason, count in sorted(self.exclusions.items())
        }


def _resolve_gamma(
    quote: OptionQuote,
    *,
    effective: EffectiveModelInputs,
    config: GexEngineConfig,
) -> tuple[float, GammaSource] | None:
    """Vendor gamma if allowed and present, else the effective model's own.

    All pricing goes through ``effective.gamma()``. There is no second
    Black-Scholes call site and no local fallback for rates, dividends or spot --
    that fragmentation is what let ``spec.risk_free_rate or snapshot.risk_free_rate``
    silently discard an explicitly configured zero.
    """
    if config.prefer_vendor_gamma and quote.gamma is not None:
        return quote.gamma, GammaSource.VENDOR
    if effective.is_usable:
        return effective.gamma(), GammaSource.SHADOW_PRICER
    # Last resort: vendor gamma even though we would have preferred our own.
    if quote.gamma is not None:
        return quote.gamma, GammaSource.VENDOR
    return None


def compute_contract_gex(
    snapshot: ChainSnapshot, config: GexEngineConfig | None = None
) -> ContractGexResult:
    """Validate, then explode a chain snapshot into per-contract contributions.

    Validation runs first and unconditionally. A contract that fails it never
    reaches the arithmetic, so a NaN gamma or a negative open interest cannot
    reach a sum -- the failure shows up as a counted exclusion instead of as a
    NaN total that looks like a rendering bug three layers downstream.
    """
    import math

    cfg = config or GexEngineConfig()
    spec = cfg.model_spec

    normalized = validate_chain(
        snapshot,
        limits=cfg.data_quality,
        require_open_interest=cfg.require_open_interest,
        treat_crossed_as_error=cfg.drop_crossed_quotes,
    )

    exclusions: Counter[ExclusionReason] = Counter()
    exclusions[ExclusionReason.VALIDATION_REJECTED] = len(normalized.rejected)

    contracts: list[ContractGex] = []
    vendor_count = 0
    shadow_count = 0
    excluded_expiries: set[str] = set()

    for quote in normalized.snapshot.quotes:
        contract = quote.contract
        # One resolution per contract, reused by every downstream calculation.
        effective = resolve_effective_inputs(quote=quote, snapshot=snapshot, spec=spec)
        if ResolutionIssue.EXPIRED in effective.issues:
            exclusions[ExclusionReason.EXPIRED] += 1
            excluded_expiries.add(contract.expiry.isoformat())
            continue

        if not effective.has_valid_spot:
            # A vendor gamma does not rescue this: gamma is one factor of the
            # product and the spot is another. v2.1 recorded the missing spot
            # and priced the contract anyway against the chain-level spot the
            # operator had explicitly not selected.
            #
            # Checked on the spot specifically rather than on full resolution,
            # so that a vendor-gamma contract with no IV -- which needs neither
            # an IV nor a rate -- is not swept up and mislabelled.
            exclusions[ExclusionReason.NO_UNDERLYING_PRICE] += 1
            continue

        dte = calendar_dte(snapshot.as_of, contract.expiry)
        if cfg.max_dte is not None and dte > cfg.max_dte:
            exclusions[ExclusionReason.BEYOND_MAX_DTE] += 1
            excluded_expiries.add(contract.expiry.isoformat())
            continue

        open_interest = quote.open_interest or 0
        if cfg.require_open_interest and open_interest <= 0:
            exclusions[ExclusionReason.NO_OPEN_INTEREST] += 1
            continue

        resolved = _resolve_gamma(quote, effective=effective, config=cfg)
        if resolved is None:
            exclusions[ExclusionReason.NO_GAMMA_SOURCE] += 1
            continue
        gamma_value, source = resolved
        if not math.isfinite(gamma_value):
            exclusions[ExclusionReason.NON_FINITE_GAMMA] += 1
            continue

        if source is GammaSource.VENDOR:
            vendor_count += 1
        else:
            shadow_count += 1

        # The notional scales by the EFFECTIVE spot, which under
        # VENDOR_PER_CONTRACT differs per contract.
        # ``has_valid_spot`` was checked above, which is what makes this
        # narrowing sound: an unresolved spot never reaches the arithmetic.
        assert effective.spot is not None
        magnitude = notional_gex(
            gamma=gamma_value,
            open_interest=open_interest,
            multiplier=contract.multiplier,
            spot=effective.spot,
            spot_move_pct=cfg.spot_move_pct,
        )
        sign = sign_for(contract.right, cfg.sign_convention)
        contracts.append(
            ContractGex(
                contract=contract,
                dte=dte,
                bucket=bucket_for_dte(dte),
                time_to_expiry=effective.time_to_expiry_years,
                gamma=gamma_value,
                implied_vol=quote.effective_iv,
                iv_source=quote.iv.source if quote.effective_iv is not None else None,
                open_interest=open_interest,
                unsigned_gex=magnitude,
                signed_gex=magnitude * sign,
                sign=sign,
                gamma_source=source,
                vendor_gamma=quote.gamma,
                effective=effective,
            )
        )

    # Canonical ordering before anything sums these.
    #
    # Floating-point addition is not associative, so summing the same contracts
    # in a different order changes the last bits of every total -- and vendors do
    # not guarantee row order. Sorting by contract identity makes the engine's
    # output a function of the *data* rather than of the order it arrived in,
    # which is what the replay guarantee actually needs.
    contracts.sort(key=lambda c: c.contract.key)

    return ContractGexResult(
        contracts=tuple(contracts),
        validation=normalized.report,
        total_quotes=len(snapshot.quotes),
        exclusions=dict(exclusions),
        excluded_expiries=tuple(sorted(excluded_expiries)),
        vendor_gamma_count=vendor_count,
        shadow_gamma_count=shadow_count,
    )


# --- View 1 & 2: totals ------------------------------------------------------


def total_unsigned_gex(contracts: tuple[ContractGex, ...]) -> float:
    """View 1. Where gamma is concentrated, with no directional claim.

    The least model-sensitive view in the engine: it needs no dealer positioning
    assumption at all.
    """
    return sum(c.unsigned_gex for c in contracts)


def total_signed_gex(contracts: tuple[ContractGex, ...]) -> float:
    """View 2. The classic public proxy. Not dealer inventory truth."""
    return sum(c.signed_gex for c in contracts)


# --- View 3: expiry buckets --------------------------------------------------


def aggregate_by_bucket(contracts: tuple[ContractGex, ...]) -> tuple[BucketGex, ...]:
    """View 3. Every bucket is always present, zero-filled when empty.

    Zero-filling matters: a consumer reading ``bucket(DTE_0)`` must be able to
    distinguish "no 0DTE gamma" from "bucket missing from this snapshot".
    """
    unsigned: dict[ExpiryBucket, float] = defaultdict(float)
    signed: dict[ExpiryBucket, float] = defaultdict(float)
    counts: dict[ExpiryBucket, int] = defaultdict(int)
    open_interest: dict[ExpiryBucket, int] = defaultdict(int)

    for c in contracts:
        unsigned[c.bucket] += c.unsigned_gex
        signed[c.bucket] += c.signed_gex
        counts[c.bucket] += 1
        open_interest[c.bucket] += c.open_interest

    return tuple(
        BucketGex(
            bucket=bucket,
            unsigned_gex=unsigned[bucket],
            signed_gex=signed[bucket],
            contract_count=counts[bucket],
            open_interest=open_interest[bucket],
        )
        for bucket in ExpiryBucket
    )


def bucket_gex_ratio_0dte_vs_rest(buckets: tuple[BucketGex, ...]) -> float | None:
    """0DTE unsigned GEX as a share of the whole chain."""
    total = sum(b.unsigned_gex for b in buckets)
    if total <= 0.0:
        return None
    zero_dte = next(
        (b.unsigned_gex for b in buckets if b.bucket is ExpiryBucket.DTE_0), 0.0
    )
    return zero_dte / total


# --- View 4: strike level ----------------------------------------------------


def aggregate_by_strike(contracts: tuple[ContractGex, ...]) -> tuple[StrikeGex, ...]:
    """View 4. Sorted ascending by strike; call and put legs kept separate."""
    call_gex: dict[float, float] = defaultdict(float)
    put_gex: dict[float, float] = defaultdict(float)
    signed: dict[float, float] = defaultdict(float)
    call_oi: dict[float, int] = defaultdict(int)
    put_oi: dict[float, int] = defaultdict(int)

    for c in contracts:
        strike = c.contract.strike
        signed[strike] += c.signed_gex
        if c.contract.right is OptionRight.CALL:
            call_gex[strike] += c.unsigned_gex
            call_oi[strike] += c.open_interest
        else:
            put_gex[strike] += c.unsigned_gex
            put_oi[strike] += c.open_interest

    return tuple(
        StrikeGex(
            strike=strike,
            call_gex=call_gex[strike],
            put_gex=put_gex[strike],
            unsigned_gex=call_gex[strike] + put_gex[strike],
            signed_gex=signed[strike],
            call_open_interest=call_oi[strike],
            put_open_interest=put_oi[strike],
        )
        for strike in sorted(set(call_gex) | set(put_gex))
    )


# --- Universe accounting -----------------------------------------------------


def build_universe(
    *,
    included: tuple[ContractGex, ...],
    all_contracts: tuple[ContractGex, ...],
    max_dte_used: int | None,
    extra_filter_reasons: dict[str, int] | None = None,
) -> OptionUniverse:
    """Account for exactly which contracts a number covers.

    Called once for the chain totals and once for the zero-gamma grid, because
    the two run on different universes and comparing them without saying so
    invites a false conclusion about how much gamma the level accounts for.
    """
    included_keys = {id(c) for c in included}
    excluded = tuple(c for c in all_contracts if id(c) not in included_keys)
    reasons: dict[str, int] = dict(extra_filter_reasons or {})
    if excluded and max_dte_used is not None:
        beyond = sum(1 for c in excluded if c.dte > max_dte_used)
        if beyond:
            reasons["beyond_max_dte"] = reasons.get("beyond_max_dte", 0) + beyond

    return OptionUniverse(
        total_contract_count=len(all_contracts),
        included_contract_count=len(included),
        included_unsigned_gex=total_unsigned_gex(included),
        excluded_unsigned_gex=total_unsigned_gex(excluded),
        included_expirations=tuple(
            sorted({c.contract.expiry.isoformat() for c in included})
        ),
        excluded_expirations=tuple(
            sorted(
                {c.contract.expiry.isoformat() for c in excluded}
                - {c.contract.expiry.isoformat() for c in included}
            )
        ),
        max_dte_used=max_dte_used,
        filter_reasons=reasons,
    )


def zero_gamma_eligible(
    contracts: tuple[ContractGex, ...], *, max_dte: int
) -> tuple[tuple[ContractGex, ...], dict[str, int]]:
    """Split contracts into repricable and not, with machine-readable reasons.

    A contract can contribute to *current* GEX on vendor gamma alone, yet be
    impossible to reprice on the zero-gamma grid because it has no IV. Counting
    it as covered reports 100% coverage for a grid that skipped it -- which is
    the v2 defect this exists to prevent.

    Two exclusion families, deliberately kept apart:

    * ``beyond_max_dte`` -- a deliberate tractability filter.
    * resolution issues (``iv_missing`` and friends) -- an input we do not have.

    They mean different things: the first is a choice, the second is a gap.
    """
    eligible: list[ContractGex] = []
    reasons: Counter[str] = Counter()
    for contract in contracts:
        if contract.dte > max_dte:
            reasons["beyond_max_dte"] += 1
            continue
        if not contract.effective.is_usable:
            for issue in contract.effective.issues:
                reasons[issue.value] += 1
            continue
        eligible.append(contract)
    return tuple(eligible), dict(sorted(reasons.items()))


def gamma_comparisons(
    contracts: tuple[ContractGex, ...],
    *,
    observed_at: str | None = None,
) -> tuple[GammaComparison, ...]:
    """Local-vs-vendor gamma comparisons, where vendor gamma is available.

    Uses each contract's own ``EffectiveModelInputs`` -- the same object the
    engine priced with -- rather than rebuilding a simplified model here. A
    comparison computed under different assumptions from the engine would report
    a disagreement that is an artefact of the comparison itself.

    Empty when the subscription tier supplies no gamma, which is the normal case.
    """
    out: list[GammaComparison] = []
    for c in contracts:
        if c.vendor_gamma is None:
            continue
        local = c.effective.gamma() if c.effective.is_usable else None
        out.append(
            GammaComparison(
                local_gamma=local,
                vendor_gamma=c.vendor_gamma,
                dte=c.dte,
                moneyness=c.moneyness,
                right=c.contract.right.value,
                implied_vol=c.implied_vol,
                observed_at=observed_at,
                effective_model=c.effective.as_dict(),
            )
        )
    return tuple(out)


def apply_model_spec(config: GexEngineConfig, spec: ModelSpec) -> GexEngineConfig:
    return config.with_(model_spec=spec)
