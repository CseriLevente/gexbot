"""One construction path for a ThetaData research session.

Three v2.1.1 defects converge here, and they are the same defect: **two things
configured separately could disagree without anybody noticing.**

``ThetaDataRuntime`` carried an ``iv_source``. ``ModelSpec`` carried an
``iv_price_source``. Nothing compared them, so a session could fetch NBBO-mid IV
and price with the vendor default while every object involved looked correctly
configured in isolation.

ThetaData computes IV under *its* assumptions -- its rate, its dividend
treatment, its expiration instant, its day count. Possessing the number is not
evidence that it was produced the way we would produce it. v2.1.1 fed vendor IV
straight into local gamma and described the result as internally consistent.

``IVSource`` declared ``TRADE_IV`` and ``LOCALLY_SOLVED_MID_IV``, neither of
which is implemented. Selecting one loaded cleanly and then fell through to the
vendor default during resolution, so the operator silently got an IV they had
not chosen.

Nothing in this module trades, prices, or fetches. It decides whether a
configuration is coherent enough to be allowed to.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from src.config.thetadata import (
    ThetaDataConfig,
    ThetaDataConfigError,
    ThetaDataRuntime,
)
from src.domain.iv import IVSource
from src.domain.model_spec import (
    DividendSource,
    ModelSpec,
    UnderlyingPriceSource,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.adapters.thetadata.client import ChainRequest

__all__ = [
    "LOCAL_IV_SOURCES",
    "MODE_CAPABILITIES",
    "SUPPORTED_IV_SOURCES",
    "UNSUPPORTED_IV_SOURCES",
    "VENDOR_COMPUTED_IV_SOURCES",
    "DividendAssumption",
    "DividendConvention",
    "PipelineConsistencyError",
    "PricingCompatibilityReport",
    "PricingMode",
    "RateAssumption",
    "RateUnit",
    "ThetaDataResearchPipeline",
    "check_dividend_compatibility",
    "check_rate_compatibility",
    "derive_pricing_mode",
    "require_coherent_pricing_mode",
]


class PipelineConsistencyError(ThetaDataConfigError):
    """Two halves of one configuration disagree, so nothing may proceed.

    Raised before any request is made and before any number is computed. A
    session whose fetch assumptions and pricing assumptions differ produces
    numbers that are wrong in a way no downstream check can detect, because
    each half is individually valid.
    """


# =============================================================================
# What is actually implemented
# =============================================================================

#: IV sources with a real implementation behind them.
SUPPORTED_IV_SOURCES = frozenset(
    {
        IVSource.NBBO_BID_IV,
        IVSource.NBBO_MID_IV,
        IVSource.NBBO_ASK_IV,
        IVSource.VENDOR_DEFAULT_IV,
    }
)

#: **Every supported IV source is vendor-computed.**
#:
#: This is the correction at the centre of v2.1.3. ThetaData solves the implied
#: volatility; that the option *price* it solved against was an NBBO bid,
#: midpoint or ask says nothing about who did the solving, or under what rate,
#: dividend, expiration instant and day count. An NBBO price basis is a fact
#: about the input to the vendor's solver, not about the solver.
#:
#: v2.1.2 read "NBBO_MID_IV" as though the NBBO part made it ours, which let the
#: default configuration pair vendor IV with LOCAL_IV_LOCAL_GAMMA -- the one
#: mode that requires no vendor/local agreement. Every compatibility check was
#: skipped, and certification reported ready.
VENDOR_COMPUTED_IV_SOURCES = frozenset(
    {
        IVSource.VENDOR_DEFAULT_IV,
        IVSource.NBBO_BID_IV,
        IVSource.NBBO_MID_IV,
        IVSource.NBBO_ASK_IV,
        IVSource.TRADE_IV,
    }
)

#: The only IV this repository could compute itself. Not implemented, so
#: LOCAL_IV_LOCAL_GAMMA is currently unreachable rather than merely unused.
LOCAL_IV_SOURCES = frozenset({IVSource.LOCALLY_SOLVED_MID_IV})


def derive_pricing_mode(*, iv_source: IVSource) -> PricingMode:
    """The mode the IV provenance actually implies.

    Deterministic and total over the supported sources. A caller may still
    select ``VENDOR_GAMMA_VALIDATION`` on top of this -- comparing vendor gamma
    is orthogonal to where the IV came from -- but they cannot select a mode
    that contradicts the provenance.
    """
    if iv_source in LOCAL_IV_SOURCES:
        return PricingMode.LOCAL_IV_LOCAL_GAMMA
    return PricingMode.VENDOR_IV_LOCAL_GAMMA


def require_coherent_pricing_mode(
    *, iv_source: IVSource, pricing_mode: PricingMode, where: str = "pricing_mode"
) -> None:
    """Refuse a mode that misdescribes where the IV came from."""
    if pricing_mode is PricingMode.LOCAL_IV_LOCAL_GAMMA:
        if iv_source in VENDOR_COMPUTED_IV_SOURCES:
            raise ThetaDataConfigError(
                f"{where}: LOCAL_IV_LOCAL_GAMMA claims no vendor-computed "
                f"quantity enters the calculation, but iv_source="
                f"{iv_source.value} is solved by ThetaData. An NBBO price basis "
                "does not make the implied volatility local -- the vendor still "
                "chose the rate, dividend, expiration instant and day count. "
                "Use VENDOR_IV_LOCAL_GAMMA, which requires the compatibility "
                "checks this mode skips."
            )
        raise ThetaDataConfigError(
            f"{where}: LOCAL_IV_LOCAL_GAMMA needs a local implied-volatility "
            f"solver ({sorted(s.value for s in LOCAL_IV_SOURCES)}), which is not "
            "implemented. The mode is unreachable until it is."
        )


#: Declared in the enum so a config naming one fails loudly rather than being
#: ignored -- but not implemented, and therefore refused at load time.
#:
#: ``TRADE_IV`` needs a trade-price feed this repository does not consume.
#: ``LOCALLY_SOLVED_MID_IV`` needs an implied-vol solver with documented
#: convergence limits and a failure state, which is real work and should be
#: done deliberately rather than approximated.
UNSUPPORTED_IV_SOURCES = frozenset(IVSource) - SUPPORTED_IV_SOURCES


def require_supported_iv_source(source: IVSource, *, where: str = "iv_source") -> None:
    """Refuse an IV source that has no implementation.

    v2.1.1 accepted these and then resolved them through the vendor-default
    fallback, so the operator got a different IV than the one they selected and
    nothing said so.
    """
    if source in UNSUPPORTED_IV_SOURCES:
        raise ThetaDataConfigError(
            f"{where}: {source.value} is declared but not implemented. "
            f"Supported sources are {sorted(s.value for s in SUPPORTED_IV_SOURCES)}. "
            "Selecting an unimplemented source previously fell through to "
            "VENDOR_DEFAULT_IV silently, which is a different number."
        )


# =============================================================================
# Vendor-side conventions we cannot infer
# =============================================================================


class RateUnit(str, Enum):
    """How to read a rate number.

    ``4.2`` and ``0.042`` are the same rate or a hundred times apart depending
    on this, and nothing in the number itself says which.
    """

    DECIMAL_ANNUAL_RATE = "DECIMAL_ANNUAL_RATE"
    PERCENT_ANNUAL_RATE = "PERCENT_ANNUAL_RATE"
    UNKNOWN = "UNKNOWN"


#: Two normalised rates within this are the same rate. Tight enough that a
#: percent/decimal confusion can never pass, loose enough for float round-trips.
RATE_TOLERANCE = 1e-9
DIVIDEND_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class RateAssumption:
    """One side's risk-free rate, with enough metadata to compare it."""

    source: str
    raw_value: float | None
    unit: RateUnit
    effective_date: str | None = None
    #: True when the vendor was left to apply its own default. The *number* is
    #: then unknown, which is different from there being no number.
    vendor_default: bool = False

    @property
    def normalized(self) -> float | None:
        """The rate as a decimal, or ``None`` when that is not determinable."""
        if self.raw_value is None or self.unit is RateUnit.UNKNOWN:
            return None
        if self.unit is RateUnit.PERCENT_ANNUAL_RATE:
            return self.raw_value / 100.0
        return self.raw_value

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "raw_value": self.raw_value,
            "unit": self.unit.value,
            "normalized": self.normalized,
            "effective_date": self.effective_date,
            "vendor_default": self.vendor_default,
        }


@dataclass(frozen=True, slots=True)
class DividendAssumption:
    """One side's dividend input: what kind, and how much."""

    convention: DividendConvention
    value: float | None

    def as_dict(self) -> dict[str, Any]:
        return {"convention": self.convention.value, "value": self.value}


class DividendConvention(str, Enum):
    """What a vendor dividend input means.

    ``annual_dividend`` could be a cash amount per year or a continuous yield.
    These are not the same quantity and are not interchangeable: Black-Scholes
    with a continuous yield ``q`` discounts the spot by ``exp(-qT)``, which a
    cash figure cannot substitute for without knowing the spot and the schedule.

    v2.1.1 passed ``annual_dividend`` through and let ``DividendSource`` treat
    the result as a yield.
    """

    ANNUAL_CASH_DIVIDEND = "ANNUAL_CASH_DIVIDEND"
    CONTINUOUS_DIVIDEND_YIELD = "CONTINUOUS_DIVIDEND_YIELD"
    ZERO_DIVIDEND = "ZERO_DIVIDEND"
    UNKNOWN_VENDOR_CONVENTION = "UNKNOWN_VENDOR_CONVENTION"


class PricingMode(str, Enum):
    """Which numbers come from where.

    Only ``VENDOR_IV_LOCAL_GAMMA`` requires vendor/local agreement, because it
    is the only mode that mixes them inside one calculation.
    """

    #: Vendor IV, our Black-Scholes gamma. Requires documented compatibility.
    VENDOR_IV_LOCAL_GAMMA = "VENDOR_IV_LOCAL_GAMMA"
    #: Our IV from NBBO legs, our gamma. Nothing of the vendor's model is used.
    LOCAL_IV_LOCAL_GAMMA = "LOCAL_IV_LOCAL_GAMMA"
    #: Vendor gamma alongside ours, for comparison rather than for aggregation.
    VENDOR_GAMMA_VALIDATION = "VENDOR_GAMMA_VALIDATION"

    @property
    def mixes_vendor_and_local_inside_one_calculation(self) -> bool:
        return self is PricingMode.VENDOR_IV_LOCAL_GAMMA


@dataclass(frozen=True, slots=True)
class ModeCapability:
    """What a pricing mode permits the gamma policy to do.

    ``GexEngineConfig.prefer_vendor_gamma`` was independent of the pricing mode
    in v2.1.2, so a session could claim VENDOR_GAMMA_VALIDATION -- comparison
    only -- while aggregating the vendor's gamma into the totals.

    No supported mode aggregates vendor gamma. That would be a separate mode
    with its own compatibility requirements, and it has not been built.
    """

    local_gamma_used_for_gex: bool
    vendor_gamma_compared: bool
    vendor_gamma_used_for_gex: bool


MODE_CAPABILITIES: dict[PricingMode, ModeCapability] = {}


# =============================================================================
# The compatibility report
# =============================================================================


@dataclass(frozen=True, slots=True)
class PricingCompatibilityReport:
    """Whether vendor and local pricing assumptions may be mixed.

    ``unknown_fields`` is deliberately not merged into ``incompatible_fields``.
    "We checked and they differ" and "we cannot tell" are different findings
    with different remedies -- the first needs a config change, the second needs
    vendor documentation or a live comparison.

    Both block compatibility. Neither is allowed to be silence.
    """

    compatible: bool
    compatible_fields: tuple[str, ...] = ()
    incompatible_fields: tuple[str, ...] = ()
    unknown_fields: tuple[str, ...] = ()
    hard_failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def merged_with(
        self, other: PricingCompatibilityReport
    ) -> PricingCompatibilityReport:
        return PricingCompatibilityReport(
            compatible=self.compatible and other.compatible,
            compatible_fields=tuple(
                sorted({*self.compatible_fields, *other.compatible_fields})
            ),
            incompatible_fields=tuple(
                sorted({*self.incompatible_fields, *other.incompatible_fields})
            ),
            unknown_fields=tuple(sorted({*self.unknown_fields, *other.unknown_fields})),
            hard_failures=tuple(sorted({*self.hard_failures, *other.hard_failures})),
            warnings=tuple(sorted({*self.warnings, *other.warnings})),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "compatible": self.compatible,
            "compatible_fields": list(self.compatible_fields),
            "incompatible_fields": list(self.incompatible_fields),
            "unknown_fields": list(self.unknown_fields),
            "hard_failures": list(self.hard_failures),
            "warnings": list(self.warnings),
        }


def check_rate_compatibility(
    *, vendor: RateAssumption, local: RateAssumption
) -> PricingCompatibilityReport:
    """Do the two sides mean the same rate?

    v2.1.2 compared a raw vendor number against a local decimal and treated a
    null vendor value as "nothing to disagree about". Both are wrong: a vendor
    4.2 matching a local 4.2 on the raw numbers is the *bug* if the vendor's is
    a percentage, and a vendor default is an unknown value rather than an absent
    one -- the vendor priced with *something*.
    """
    if vendor.vendor_default or (vendor.raw_value is None and vendor.source):
        return PricingCompatibilityReport(
            compatible=False,
            unknown_fields=(
                f"risk_free_rate: UNKNOWN_VENDOR_DEFAULT -- no rate_value was "
                f"sent, so the vendor applied its own {vendor.source} value. "
                "The number it used is not recoverable from the response.",
            ),
        )
    if vendor.unit is RateUnit.UNKNOWN:
        return PricingCompatibilityReport(
            compatible=False,
            unknown_fields=(
                f"risk_free_rate: vendor units are undocumented, so "
                f"{vendor.raw_value} cannot be compared with a local decimal",
            ),
        )
    if local.normalized is None:
        return PricingCompatibilityReport(
            compatible=False,
            unknown_fields=("risk_free_rate: no comparable local rate",),
        )
    vendor_decimal = vendor.normalized
    if vendor_decimal is None:
        return PricingCompatibilityReport(
            compatible=False,
            unknown_fields=("risk_free_rate: vendor rate is not determinable",),
        )

    warnings: list[str] = []
    if vendor.source and local.source and vendor.source != local.source:
        warnings.append(
            f"risk_free_rate source differs: vendor {vendor.source!r} vs local "
            f"{local.source!r}; the values agree but the curves may diverge later"
        )
    if abs(vendor_decimal - local.normalized) > RATE_TOLERANCE:
        return PricingCompatibilityReport(
            compatible=False,
            incompatible_fields=(
                f"risk_free_rate: vendor {vendor.raw_value} "
                f"{vendor.unit.value} is {vendor_decimal} as a decimal, local "
                f"model uses {local.normalized}",
            ),
            warnings=tuple(warnings),
        )
    return PricingCompatibilityReport(
        compatible=True,
        compatible_fields=(
            f"risk_free_rate ({vendor.raw_value} {vendor.unit.value} == "
            f"{local.normalized} decimal)",
        ),
        warnings=tuple(warnings),
    )


def check_dividend_compatibility(
    *, vendor: DividendAssumption, local: DividendAssumption
) -> PricingCompatibilityReport:
    """Do the two sides mean the same dividend?

    Two questions, and v2.1.2 asked only the first: *what kind of quantity* and
    *how much*. Two continuous yields of 0.02 and 0.01 are the same kind and
    different numbers, which is a mismatch that a convention comparison cannot
    see.
    """
    if vendor.convention is DividendConvention.UNKNOWN_VENDOR_CONVENTION:
        return PricingCompatibilityReport(
            compatible=False,
            unknown_fields=(
                "dividend_yield: the vendor's annual_dividend may be a cash "
                "amount or a continuous yield; ThetaData does not document "
                "which, and the two are not interchangeable",
            ),
        )
    if vendor.convention is not local.convention:
        return PricingCompatibilityReport(
            compatible=False,
            incompatible_fields=(
                f"dividend_yield: vendor uses {vendor.convention.value}, local "
                f"model uses {local.convention.value}. Converting a cash amount "
                "to a continuous yield needs the spot and the payment schedule, "
                "neither of which is available here.",
            ),
        )
    if vendor.convention is DividendConvention.ZERO_DIVIDEND:
        return PricingCompatibilityReport(
            compatible=True, compatible_fields=("dividend_yield (both zero)",)
        )
    if vendor.value is None or local.value is None:
        return PricingCompatibilityReport(
            compatible=False,
            unknown_fields=(
                "dividend_yield: one side has no value to compare, so matching "
                "conventions establish nothing",
            ),
        )
    if abs(vendor.value - local.value) > DIVIDEND_TOLERANCE:
        return PricingCompatibilityReport(
            compatible=False,
            incompatible_fields=(
                f"dividend_yield: both sides use {vendor.convention.value} but "
                f"vendor {vendor.value} != local {local.value}. The same kind of "
                "quantity with a different magnitude is still a different "
                "assumption.",
            ),
        )
    return PricingCompatibilityReport(
        compatible=True,
        compatible_fields=(
            f"dividend_yield ({vendor.convention.value} == {vendor.value})",
        ),
    )


# =============================================================================
# The pipeline
# =============================================================================


@dataclass(frozen=True, slots=True)
class ThetaDataResearchPipeline:
    """Everything one research session needs, built once from one config.

    A caller cannot construct the runtime and the model spec independently,
    because that is precisely how they came to disagree.
    """

    runtime: ThetaDataRuntime
    model_spec: ModelSpec
    chain_request: ChainRequest
    pricing_mode: PricingMode
    pricing_compatibility: PricingCompatibilityReport
    config: ThetaDataConfig
    #: The engine settings this session will compute with. Owned here so the
    #: gamma policy cannot contradict the pricing mode.
    engine_config: Any = None
    #: Whether the subscription exposes what the mode needs.
    subscription_capability: Any = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_loaded_config(
        cls,
        loaded: Any,
        *,
        symbol: str = "SPXW",
        transport: Any = None,
        clock: Any = None,
    ) -> ThetaDataResearchPipeline:
        """Build one session from the whole configuration file.

        The canonical entry point. v2.1.2 took only the ``thetadata:`` section
        and derived its own ``ModelSpec``, so the top-level ``model:`` block was
        silently ignored -- and the repository's own ``research.yaml`` set
        ``model.iv_price_source: NBBO_MID_IV`` alongside
        ``thetadata.iv_source: VENDOR_DEFAULT_IV``. Two different IVs, one file,
        no complaint.
        """
        return cls.from_config(
            loaded.thetadata,
            symbol=symbol,
            transport=transport,
            clock=clock,
            model_spec=loaded.engine.model_spec,
            engine_config=loaded.engine,
        )

    @classmethod
    def from_config(
        cls,
        config: ThetaDataConfig,
        *,
        symbol: str = "SPXW",
        transport: Any = None,
        clock: Any = None,
        model_spec: ModelSpec | None = None,
        engine_config: Any = None,
    ) -> ThetaDataResearchPipeline:
        """Build a coherent session, or refuse.

        ``model_spec`` exists so an existing spec can be *checked* against the
        config -- not so individual fields can be overridden. Every consistency
        rule below was a way two objects could disagree in v2.1.1.
        """
        from src.adapters.thetadata.capabilities import assess_tier
        from src.adapters.thetadata.endpoints import Tier
        from src.gex.config import GexEngineConfig

        require_supported_iv_source(config.iv_source)
        require_coherent_pricing_mode(
            iv_source=config.iv_source, pricing_mode=config.pricing_mode
        )

        spec = model_spec if model_spec is not None else config.to_model_spec()
        _require_consistent(config, spec)

        engine = (
            engine_config
            if engine_config is not None
            else GexEngineConfig(model_spec=spec)
        )
        _require_consistent_gamma_policy(config.pricing_mode, engine)

        capability = assess_tier(
            Tier(config.tier), required_capabilities(config.pricing_mode)
        )
        if not capability.satisfied:
            raise PipelineConsistencyError(
                f"the {config.tier} tier does not expose what "
                f"{config.pricing_mode.value} needs: "
                f"missing={list(capability.missing)} "
                f"uncertain={list(capability.uncertain)}. An uncertain "
                "capability counts against, because the alternative is finding "
                "out at the first paid request."
            )

        runtime = ThetaDataRuntime.from_config(
            config, symbol=symbol, transport=transport, clock=clock
        )
        report = assess_pricing_compatibility(config, spec)

        if config.fail_on_incompatible_pricing and not report.compatible:
            raise PipelineConsistencyError(
                "pricing assumptions are not compatible, so vendor and local "
                "numbers must not be mixed: "
                f"incompatible={list(report.incompatible_fields)} "
                f"unknown={list(report.unknown_fields)}"
            )

        warnings: list[str] = []
        if not report.compatible:
            warnings.append(
                "PRICING_ASSUMPTIONS_NOT_VERIFIED: "
                f"{len(report.incompatible_fields)} incompatible, "
                f"{len(report.unknown_fields)} unknown"
            )

        return cls(
            runtime=runtime,
            model_spec=spec,
            chain_request=runtime.default_chain_request,
            pricing_mode=config.pricing_mode,
            pricing_compatibility=report,
            config=config,
            engine_config=engine,
            subscription_capability=capability,
            warnings=tuple(warnings),
        )

    def fingerprint(self) -> str:
        """One digest over everything that changes a number.

        Covers both halves deliberately: a fingerprint of only the config, or
        only the model spec, would be stable across exactly the divergence this
        class exists to prevent.
        """
        payload = json.dumps(
            {
                "config": self.config.as_dict(),
                "model": self.model_spec.as_dict()
                if hasattr(self.model_spec, "as_dict")
                else self.model_spec.fingerprint(),
                "pricing_mode": self.pricing_mode.value,
                # The gamma policy changes which number is reported, so it
                # belongs in the fingerprint.
                "engine": self.engine_config.fingerprint()
                if hasattr(self.engine_config, "fingerprint")
                else str(self.engine_config),
                "tier_capability": self.subscription_capability.as_dict()
                if self.subscription_capability is not None
                else None,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict[str, Any]:
        return {
            "pipeline_fingerprint": self.fingerprint(),
            "pricing_mode": self.pricing_mode.value,
            "pricing_compatibility": self.pricing_compatibility.as_dict(),
            "model_fingerprint": self.model_spec.fingerprint(),
            "iv_source": self.model_spec.iv_price_source.value,
            "thetadata": self.config.as_dict(),
            "subscription_capability": (
                self.subscription_capability.as_dict()
                if self.subscription_capability is not None
                else None
            ),
            "load_bearing_unknowns": list(
                load_bearing_unknowns(self.pricing_compatibility)
            ),
            "warnings": list(self.warnings),
        }


def required_capabilities(mode: PricingMode) -> tuple[str, ...]:
    """What the subscription must expose for a mode to be honest."""
    from src.adapters.thetadata.capabilities import CapabilityRequirement

    if mode is PricingMode.VENDOR_GAMMA_VALIDATION:
        return CapabilityRequirement.VENDOR_GAMMA
    if mode is PricingMode.LOCAL_IV_LOCAL_GAMMA:
        return CapabilityRequirement.BASELINE
    return CapabilityRequirement.VENDOR_IV


def _require_consistent_gamma_policy(mode: PricingMode, engine: Any) -> None:
    """No supported mode aggregates vendor gamma into the totals."""
    capability = MODE_CAPABILITIES[mode]
    if getattr(engine, "prefer_vendor_gamma", False) and not (
        capability.vendor_gamma_used_for_gex
    ):
        raise PipelineConsistencyError(
            f"{mode.value} does not aggregate vendor gamma, but the engine is "
            "configured with prefer_vendor_gamma=True. Aggregating the vendor's "
            "gamma is a different mode with its own compatibility requirements, "
            "and it has not been built. Set prefer_vendor_gamma=False; vendor "
            "gamma is still available for comparison."
        )


def _require_consistent(config: ThetaDataConfig, spec: ModelSpec) -> None:
    """Every way the two halves could silently disagree."""
    mismatches: list[str] = []

    if config.iv_source is not spec.iv_price_source:
        mismatches.append(
            f"iv_source: runtime fetches {config.iv_source.value} but the model "
            f"prices with {spec.iv_price_source.value}"
        )
    if abs(config.min_time_to_expiry_minutes - spec.minimum_time_to_expiry_minutes) > 0:
        mismatches.append(
            f"time floor: runtime {config.min_time_to_expiry_minutes}min vs "
            f"model {spec.minimum_time_to_expiry_minutes}min"
        )
    configured_underlying = UnderlyingPriceSource(config.underlying_price_source)
    if configured_underlying is not spec.underlying_price_source:
        mismatches.append(
            f"underlying source: runtime {configured_underlying.value} vs model "
            f"{spec.underlying_price_source.value}"
        )
    if config.expiration_rule != spec.expiration_timestamp_rule.value:
        mismatches.append(
            f"expiration rule: runtime {config.expiration_rule} vs model "
            f"{spec.expiration_timestamp_rule.value}"
        )

    if mismatches:
        raise PipelineConsistencyError(
            "ThetaData runtime and ModelSpec disagree; a session configured "
            "this way fetches under one set of assumptions and prices under "
            "another: " + "; ".join(mismatches)
        )


#: Fields whose meaning changes the gamma. An UNKNOWN here is a blocker, not a
#: warning: pricing under an assumption we cannot name is not research, it is a
#: number with no stated meaning.
LOAD_BEARING_COMPATIBILITY_FIELDS = (
    "risk_free_rate",
    "dividend_yield",
    "expiration_timestamp",
    "minimum_time_to_expiry",
    "underlying_price_source",
    "iv_calculation_convention",
)


def vendor_rate_assumption(config: ThetaDataConfig) -> RateAssumption:
    """What the vendor will price with, as far as we can tell."""
    return RateAssumption(
        source=config.rate_type or "vendor_default",
        raw_value=config.rate_value,
        unit=config.rate_units,
        vendor_default=config.rate_value is None,
    )


def local_rate_assumption(spec: ModelSpec) -> RateAssumption:
    """What our own pricer uses. Always a decimal by construction."""
    return RateAssumption(
        source=spec.risk_free_rate_source.value,
        raw_value=spec.risk_free_rate,
        unit=RateUnit.DECIMAL_ANNUAL_RATE,
    )


def vendor_dividend_assumption(config: ThetaDataConfig) -> DividendAssumption:
    return DividendAssumption(
        convention=config.dividend_convention, value=config.annual_dividend
    )


def local_dividend_assumption(spec: ModelSpec) -> DividendAssumption:
    convention = (
        DividendConvention.ZERO_DIVIDEND
        if spec.dividend_yield_source is DividendSource.ZERO
        else DividendConvention.CONTINUOUS_DIVIDEND_YIELD
    )
    return DividendAssumption(convention=convention, value=spec.dividend_yield)


def assess_pricing_compatibility(
    config: ThetaDataConfig, spec: ModelSpec
) -> PricingCompatibilityReport:
    """Whether vendor-derived numbers may enter a local calculation.

    Inspects the *effective* assumptions on both sides. v2.1.2 short-circuited
    on the pricing-mode enum, so a session that mislabelled itself
    LOCAL_IV_LOCAL_GAMMA skipped every check below and was reported compatible.
    The mode is now derived from provenance, so this cannot be reached by
    assertion.
    """
    if not config.pricing_mode.mixes_vendor_and_local_inside_one_calculation:
        return PricingCompatibilityReport(
            compatible=True,
            compatible_fields=(
                f"{config.pricing_mode.value}: no vendor-computed quantity "
                "enters a local calculation, so no agreement is required",
            ),
        )

    report = check_rate_compatibility(
        vendor=vendor_rate_assumption(config), local=local_rate_assumption(spec)
    ).merged_with(
        check_dividend_compatibility(
            vendor=vendor_dividend_assumption(config),
            local=local_dividend_assumption(spec),
        )
    )

    # Dimensions ThetaData does not document. They are unknown rather than
    # incompatible -- we have not found a disagreement, we have found that the
    # question is unanswerable from here -- and they stay unknown until a live
    # comparison settles them. All are load-bearing: each one changes gamma.
    return report.merged_with(
        PricingCompatibilityReport(
            compatible=False,
            unknown_fields=(
                "expiration_timestamp: the vendor's settlement instant for its "
                "own IV solve is undocumented",
                "day_count_convention: the vendor's year fraction is undocumented",
                "minimum_time_to_expiry: the vendor's short-dated floor is "
                "undocumented",
                "iv_calculation_convention: which price the vendor solved "
                "against, and how, is undocumented",
                "underlying_price_source: which underlying print the vendor used "
                "for its solve, and when, is undocumented",
                "solver_version: the vendor's IV solver version is not exposed",
            ),
            warnings=(
                "VENDOR_IV_LOCAL_GAMMA mixes a vendor-computed IV into a local "
                "gamma. Six vendor-side conventions are undocumented, so model "
                "consistency cannot be claimed until a live comparison is run. "
                "See docs/ADAPTER_CERTIFICATION.md.",
            ),
        )
    )


def load_bearing_unknowns(report: PricingCompatibilityReport) -> tuple[str, ...]:
    """Unknowns that change a number, as opposed to ones that merely annoy.

    Certification blocks on these. A non-load-bearing unknown may remain a
    warning, but it has to be named here to be treated that way -- the default
    for anything unrecognised is to block.
    """
    return tuple(
        sorted(
            field
            for field in report.unknown_fields
            if any(name in field for name in LOAD_BEARING_COMPATIBILITY_FIELDS)
        )
    )


#: Filled after ``PricingMode`` is defined. Every supported mode uses local
#: gamma for the aggregate; none uses the vendor's.
MODE_CAPABILITIES.update(
    {
        PricingMode.VENDOR_IV_LOCAL_GAMMA: ModeCapability(
            local_gamma_used_for_gex=True,
            vendor_gamma_compared=False,
            vendor_gamma_used_for_gex=False,
        ),
        PricingMode.LOCAL_IV_LOCAL_GAMMA: ModeCapability(
            local_gamma_used_for_gex=True,
            vendor_gamma_compared=False,
            vendor_gamma_used_for_gex=False,
        ),
        PricingMode.VENDOR_GAMMA_VALIDATION: ModeCapability(
            local_gamma_used_for_gex=True,
            vendor_gamma_compared=True,
            vendor_gamma_used_for_gex=False,
        ),
    }
)


#: v2.1.2 called this ``VendorRateUnits`` with members ``PERCENT``/``DECIMAL``.
#: Retained as an alias so an existing import does not break; new code should
#: use ``RateUnit``, whose member names say what unit they mean.
VendorRateUnits = RateUnit
