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

from src.config.compatibility import (
    CompatibilityEvidence,
    CompatibilityStatus,
    EvidenceSource,
    PricingAssumptionAttestation,
    PricingCompatibilityReport,
    PricingDimension,
    PricingDimensionResult,
    apply_attestations,
)
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
    from datetime import date, datetime

    from src.adapters.thetadata.client import ChainRequest
    from src.domain.contracts import ChainSnapshot

__all__ = [
    "LEGACY_PRICING_MODES",
    "LOCAL_IV_SOURCES",
    "SUPPORTED_IV_SOURCES",
    "UNSUPPORTED_IV_SOURCES",
    "VENDOR_COMPUTED_IV_SOURCES",
    "CompatibilityEvidence",
    "CompatibilityStatus",
    "DividendAssumption",
    "DividendConvention",
    "EvidenceSource",
    "IvGammaPricingMode",
    "PipelineConsistencyError",
    "PricingAssumptionAttestation",
    "PricingCompatibilityReport",
    "PricingDimension",
    "PricingDimensionResult",
    "PricingMode",
    "RateAssumption",
    "RateUnit",
    "ThetaDataResearchPipeline",
    "VendorGammaPolicy",
    "check_dividend_compatibility",
    "check_rate_compatibility",
    "derive_pricing_mode",
    "load_bearing_unknowns",
    "reject_legacy_pricing_mode",
    "require_coherent_pricing_mode",
    "required_capabilities",
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
    switch on the vendor-gamma comparison on top of this -- see
    ``VendorGammaPolicy``, which is orthogonal to where the IV came from -- but
    they cannot select a mode that contradicts the provenance.
    """
    if iv_source in LOCAL_IV_SOURCES:
        return PricingMode.LOCAL_IV_LOCAL_GAMMA
    return PricingMode.VENDOR_IV_LOCAL_GAMMA


def require_coherent_pricing_mode(
    *, iv_source: IVSource, pricing_mode: PricingMode, where: str = "pricing_mode"
) -> None:
    """Refuse a mode that misdescribes where the IV came from.

    Both directions. v2.1.4's first cut only rejected ``LOCAL_IV_LOCAL_GAMMA``
    on a vendor source, so ``dataclasses.replace(config, iv_source=
    LOCALLY_SOLVED_MID_IV)`` produced a locally-solved IV still labelled
    ``VENDOR_IV_LOCAL_GAMMA`` -- the derivation in ``__post_init__`` does not
    re-run when the field is already set. Harmless today because the local
    solver does not exist; silent on the day it lands.
    """
    if (
        pricing_mode is PricingMode.VENDOR_IV_LOCAL_GAMMA
        and iv_source in LOCAL_IV_SOURCES
    ):
        raise ThetaDataConfigError(
            f"{where}: VENDOR_IV_LOCAL_GAMMA says a vendor-computed IV feeds "
            f"the local gamma, but iv_source={iv_source.value} is solved "
            "locally. The mode has to follow the provenance; derive it with "
            "derive_pricing_mode rather than carrying one over."
        )
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


class IvGammaPricingMode(str, Enum):
    """Where the IV comes from, and therefore whether agreement is required.

    v2.1.3 modelled ``VENDOR_GAMMA_VALIDATION`` as a third value of this enum,
    which made it an *alternative* to ``VENDOR_IV_LOCAL_GAMMA``. That is
    structurally wrong: fetching the vendor's gamma for comparison does not stop
    the vendor's IV from feeding our gamma. Selecting it therefore moved a
    session out of the mode whose compatibility checks it still needed, and
    ``assess_pricing_compatibility`` skipped them.

    Vendor-gamma comparison is an overlay now -- see ``VendorGammaPolicy`` --
    and this enum answers only the question it can answer.
    """

    #: Vendor-computed IV feeds a local Black-Scholes gamma. Requires agreement.
    VENDOR_IV_LOCAL_GAMMA = "VENDOR_IV_LOCAL_GAMMA"
    #: A local solver produces the IV. Nothing vendor-computed enters the maths.
    #: Unreachable until such a solver exists.
    LOCAL_IV_LOCAL_GAMMA = "LOCAL_IV_LOCAL_GAMMA"

    @property
    def mixes_vendor_and_local_inside_one_calculation(self) -> bool:
        return self is IvGammaPricingMode.VENDOR_IV_LOCAL_GAMMA


class VendorGammaPolicy(str, Enum):
    """What to do with the vendor's gamma, independently of the IV question."""

    DISABLED = "DISABLED"
    #: Fetch it, compare it against ours, never aggregate it. Needs Pro.
    COMPARE_ONLY = "COMPARE_ONLY"

    @property
    def requires_vendor_gamma(self) -> bool:
        return self is VendorGammaPolicy.COMPARE_ONLY

    @property
    def aggregates_vendor_gamma(self) -> bool:
        """Always False.

        Aggregating vendor gamma would be a third policy with its own
        compatibility requirements. It has not been built, and no value of this
        enum turns it on -- which is what ``ModeCapability`` used to say in a
        table that could drift from the enum it described.
        """
        return False


#: v2.1.3 name for the IV dimension. Retained so existing imports keep working.
PricingMode = IvGammaPricingMode

#: Values that ``pricing_mode`` accepted in v2.1.3 and no longer names, mapped to
#: what the operator meant in the two-axis model.
LEGACY_PRICING_MODES = {
    "VENDOR_GAMMA_VALIDATION": (
        IvGammaPricingMode.VENDOR_IV_LOCAL_GAMMA,
        VendorGammaPolicy.COMPARE_ONLY,
    )
}


def reject_legacy_pricing_mode(raw: object, *, where: str = "pricing_mode") -> None:
    """Refuse a v2.1.3 mode name rather than translating it silently.

    Translating would be easy and wrong. ``VENDOR_GAMMA_VALIDATION`` used to
    *skip* the vendor-IV compatibility checks; the same file re-read under
    v2.1.4 runs them and can now refuse to compute. That is a change in what the
    configuration does, so the operator has to write the new form themselves.
    """
    if not isinstance(raw, str):
        return
    replacement = LEGACY_PRICING_MODES.get(raw)
    if replacement is None:
        return
    mode, policy = replacement
    raise ThetaDataConfigError(
        f"{where}: {raw} was a single enum in v2.1.3 that answered two "
        "independent questions -- where the IV came from, and what to do with "
        "the vendor's gamma. Selecting it moved the session out of "
        "VENDOR_IV_LOCAL_GAMMA, so the vendor-IV compatibility checks were "
        "skipped even though vendor IV still fed the local gamma. Write "
        f"pricing_mode: {mode.value} and vendor_gamma_policy: {policy.value}. "
        "Note that the compatibility checks now run, and may refuse to compute."
    )


def _result(
    dimension: PricingDimension,
    status: CompatibilityStatus,
    code: str,
    *,
    vendor: object | None = None,
    local: object | None = None,
    evidence: CompatibilityEvidence | None = None,
    detail: str = "",
) -> PricingDimensionResult:
    return PricingDimensionResult(
        dimension=dimension,
        status=status,
        code=code,
        vendor_value=vendor,
        local_value=local,
        evidence=evidence,
        detail=detail,
    )


def check_rate_compatibility(
    *, vendor: RateAssumption, local: RateAssumption
) -> PricingCompatibilityReport:
    """Do the two sides mean the same rate?

    Two dimensions decided together: the units have to be known before the
    values can be compared at all.
    """
    if vendor.vendor_default or (vendor.raw_value is None and vendor.source):
        return PricingCompatibilityReport(
            dimensions=(
                _result(
                    PricingDimension.RATE_UNITS,
                    CompatibilityStatus.UNKNOWN,
                    "UNKNOWN_VENDOR_DEFAULT",
                    vendor=vendor.source,
                    detail=(
                        "no rate_value was sent, so the vendor applied its own "
                        f"{vendor.source} value; the number it used is not "
                        "recoverable from the response"
                    ),
                ),
                _result(
                    PricingDimension.RISK_FREE_RATE,
                    CompatibilityStatus.UNKNOWN,
                    "UNKNOWN_VENDOR_DEFAULT",
                    local=local.normalized,
                ),
            )
        )
    if vendor.unit is RateUnit.UNKNOWN:
        return PricingCompatibilityReport(
            dimensions=(
                _result(
                    PricingDimension.RATE_UNITS,
                    CompatibilityStatus.UNKNOWN,
                    "VENDOR_RATE_UNITS_UNDOCUMENTED",
                    vendor=vendor.raw_value,
                    detail="4.2 is either 4.2% or 420%; the vendor does not say",
                ),
                _result(
                    PricingDimension.RISK_FREE_RATE,
                    CompatibilityStatus.UNKNOWN,
                    "VENDOR_RATE_NOT_NORMALISABLE",
                    vendor=vendor.raw_value,
                    local=local.normalized,
                ),
            )
        )

    units = _result(
        PricingDimension.RATE_UNITS,
        CompatibilityStatus.MATCHED,
        "VENDOR_RATE_UNITS_STATED",
        vendor=vendor.unit.value,
        local=local.unit.value,
        evidence=CompatibilityEvidence(
            source=EvidenceSource.LOCAL_CONFIGURATION, reference="rate_units"
        ),
    )

    if local.normalized is None or vendor.normalized is None:
        return PricingCompatibilityReport(
            dimensions=(
                units,
                _result(
                    PricingDimension.RISK_FREE_RATE,
                    CompatibilityStatus.UNKNOWN,
                    "NO_COMPARABLE_LOCAL_RATE",
                    vendor=vendor.normalized,
                    local=local.normalized,
                ),
            )
        )

    warnings: tuple[str, ...] = ()
    if vendor.source and local.source and vendor.source != local.source:
        warnings = (
            f"RATE_SOURCE_DIFFERS: vendor {vendor.source!r} vs local "
            f"{local.source!r}; the values agree today and may not tomorrow",
        )

    if abs(vendor.normalized - local.normalized) > RATE_TOLERANCE:
        return PricingCompatibilityReport(
            dimensions=(
                units,
                _result(
                    PricingDimension.RISK_FREE_RATE,
                    CompatibilityStatus.MISMATCHED,
                    "RATE_VALUE_DIFFERS",
                    vendor=vendor.normalized,
                    local=local.normalized,
                    detail=(
                        f"vendor {vendor.raw_value} {vendor.unit.value} is "
                        f"{vendor.normalized} as a decimal; local model uses "
                        f"{local.normalized}"
                    ),
                ),
            ),
            warnings=warnings,
        )
    return PricingCompatibilityReport(
        dimensions=(
            units,
            _result(
                PricingDimension.RISK_FREE_RATE,
                CompatibilityStatus.MATCHED,
                "RATE_VALUE_AGREES",
                vendor=vendor.normalized,
                local=local.normalized,
                evidence=CompatibilityEvidence(
                    source=EvidenceSource.LOCAL_CONFIGURATION,
                    reference="rate_value+rate_units",
                ),
            ),
        ),
        warnings=warnings,
    )


def check_dividend_compatibility(
    *, vendor: DividendAssumption, local: DividendAssumption
) -> PricingCompatibilityReport:
    """Do the two sides mean the same dividend?

    Convention and value are separate dimensions. Matching conventions with
    different magnitudes is still a mismatch.
    """
    if vendor.convention is DividendConvention.UNKNOWN_VENDOR_CONVENTION:
        return PricingCompatibilityReport(
            dimensions=(
                _result(
                    PricingDimension.DIVIDEND_CONVENTION,
                    CompatibilityStatus.UNKNOWN,
                    "VENDOR_DIVIDEND_CONVENTION_UNDOCUMENTED",
                    detail=(
                        "annual_dividend may be a cash amount or a continuous "
                        "yield; the two are not interchangeable"
                    ),
                ),
                _result(
                    PricingDimension.DIVIDEND_VALUE,
                    CompatibilityStatus.UNKNOWN,
                    "VENDOR_DIVIDEND_NOT_COMPARABLE",
                    vendor=vendor.value,
                    local=local.value,
                ),
            )
        )
    if vendor.convention is not local.convention:
        return PricingCompatibilityReport(
            dimensions=(
                _result(
                    PricingDimension.DIVIDEND_CONVENTION,
                    CompatibilityStatus.MISMATCHED,
                    "DIVIDEND_CONVENTION_DIFFERS",
                    vendor=vendor.convention.value,
                    local=local.convention.value,
                    detail=(
                        "converting a cash amount to a continuous yield needs "
                        "the spot and the payment schedule"
                    ),
                ),
                _result(
                    PricingDimension.DIVIDEND_VALUE,
                    CompatibilityStatus.NOT_APPLICABLE,
                    "CONVENTIONS_DIFFER",
                ),
            )
        )

    convention = _result(
        PricingDimension.DIVIDEND_CONVENTION,
        CompatibilityStatus.MATCHED,
        "DIVIDEND_CONVENTION_AGREES",
        vendor=vendor.convention.value,
        local=local.convention.value,
        evidence=CompatibilityEvidence(
            source=EvidenceSource.LOCAL_CONFIGURATION,
            reference="dividend_convention",
        ),
    )
    if vendor.convention is DividendConvention.ZERO_DIVIDEND:
        # Both sides *say* zero. Check that they are, rather than writing 0.0
        # into the audit trail because the convention name contains the word.
        #
        # ``annual_dividend`` is forwarded to the vendor as a query parameter,
        # so a config declaring ZERO_DIVIDEND while sending 3.5 has the vendor
        # solving its IV under q=3.5 and the local model pricing under q=0.0.
        # The first cut of v2.1.4 returned BOTH_ZERO here without looking, and
        # recorded the vendor's dividend as 0.0 -- the compatibility report,
        # whose entire job is to catch exactly this, agreeing that it had not
        # happened.
        non_zero = tuple(
            sorted(
                name
                for name, value in (("vendor", vendor.value), ("local", local.value))
                if value is not None and abs(value) > DIVIDEND_TOLERANCE
            )
        )
        if non_zero:
            return PricingCompatibilityReport(
                dimensions=(
                    convention,
                    _result(
                        PricingDimension.DIVIDEND_VALUE,
                        CompatibilityStatus.MISMATCHED,
                        "ZERO_DIVIDEND_WITH_A_NON_ZERO_VALUE",
                        vendor=vendor.value,
                        local=local.value,
                        detail=(
                            f"the convention says zero but {list(non_zero)} "
                            "carries a non-zero dividend, which is sent to the "
                            "vendor and changes its IV"
                        ),
                    ),
                )
            )
        return PricingCompatibilityReport(
            dimensions=(
                convention,
                _result(
                    PricingDimension.DIVIDEND_VALUE,
                    CompatibilityStatus.MATCHED,
                    "BOTH_ZERO",
                    vendor=vendor.value if vendor.value is not None else 0.0,
                    local=local.value if local.value is not None else 0.0,
                    evidence=CompatibilityEvidence(
                        source=EvidenceSource.LOCAL_CONFIGURATION,
                        reference="annual_dividend",
                    ),
                ),
            )
        )
    if vendor.value is None or local.value is None:
        return PricingCompatibilityReport(
            dimensions=(
                convention,
                _result(
                    PricingDimension.DIVIDEND_VALUE,
                    CompatibilityStatus.UNKNOWN,
                    "DIVIDEND_VALUE_MISSING",
                    vendor=vendor.value,
                    local=local.value,
                ),
            )
        )
    if abs(vendor.value - local.value) > DIVIDEND_TOLERANCE:
        return PricingCompatibilityReport(
            dimensions=(
                convention,
                _result(
                    PricingDimension.DIVIDEND_VALUE,
                    CompatibilityStatus.MISMATCHED,
                    "DIVIDEND_VALUE_DIFFERS",
                    vendor=vendor.value,
                    local=local.value,
                    detail="same kind of quantity, different magnitude",
                ),
            )
        )
    return PricingCompatibilityReport(
        dimensions=(
            convention,
            _result(
                PricingDimension.DIVIDEND_VALUE,
                CompatibilityStatus.MATCHED,
                "DIVIDEND_VALUE_AGREES",
                vendor=vendor.value,
                local=local.value,
                evidence=CompatibilityEvidence(
                    source=EvidenceSource.LOCAL_CONFIGURATION,
                    reference="annual_dividend",
                ),
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class ThetaDataResearchPipeline:
    """Everything one research session needs, built once from one config.

    A caller cannot construct the runtime and the model spec independently,
    because that is precisely how they came to disagree.
    """

    runtime: ThetaDataRuntime
    model_spec: ModelSpec
    chain_request: ChainRequest
    pricing_mode: IvGammaPricingMode
    #: Orthogonal to ``pricing_mode``. Comparing the vendor's gamma is a thing
    #: this session additionally does; it is not a different way of pricing.
    vendor_gamma_policy: VendorGammaPolicy
    pricing_compatibility: PricingCompatibilityReport
    config: ThetaDataConfig
    #: The engine settings this session will compute with. Owned here so the
    #: gamma policy cannot contradict the pricing mode.
    engine_config: Any = None
    #: Whether the subscription exposes what the mode and the policy need.
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
        _require_consistent_gamma_policy(config.vendor_gamma_policy, engine)

        capability = assess_tier(
            Tier(config.tier),
            required_capabilities(
                config.pricing_mode, policy=config.vendor_gamma_policy
            ),
        )
        if not capability.satisfied:
            raise PipelineConsistencyError(
                f"the {config.tier} tier does not expose what "
                f"{config.pricing_mode.value} with vendor_gamma_policy="
                f"{config.vendor_gamma_policy.value} needs: "
                f"missing={list(capability.missing)} "
                f"uncertain={list(capability.uncertain)}. An uncertain "
                "capability counts against, because the alternative is finding "
                "out at the first paid request."
            )

        runtime = ThetaDataRuntime.from_config(
            config, symbol=symbol, transport=transport, clock=clock
        )
        # Runs on the IV question alone. In v2.1.3 selecting the vendor-gamma
        # comparison moved the session into a different ``PricingMode`` and this
        # returned "compatible" without checking anything.
        report = assess_pricing_compatibility(config, spec)

        if config.fail_on_incompatible_pricing and not report.compatible:
            raise PipelineConsistencyError(
                "pricing assumptions are not compatible, so vendor and local "
                "numbers must not be mixed: "
                f"mismatched={[d.value for d in report.load_bearing_mismatches]} "
                f"unknown={[d.value for d in report.load_bearing_unknowns]} "
                f"hard_failures={list(report.hard_failures)}"
            )

        warnings: list[str] = []
        if not report.compatible:
            warnings.append(
                "PRICING_ASSUMPTIONS_NOT_VERIFIED: "
                f"{len(report.load_bearing_mismatches)} mismatched, "
                f"{len(report.load_bearing_unknowns)} unknown, "
                f"{len(report.hard_failures)} hard failures"
            )

        return cls(
            runtime=runtime,
            model_spec=spec,
            chain_request=runtime.default_chain_request,
            pricing_mode=config.pricing_mode,
            vendor_gamma_policy=config.vendor_gamma_policy,
            pricing_compatibility=report,
            config=config,
            engine_config=engine,
            subscription_capability=capability,
            warnings=tuple(warnings),
        )

    # -- the canonical way to get a number out of a session ------------------
    #
    # v2.1.3 left this to callers: fetch through the runtime, remember to pass
    # ``pipeline=pipeline`` so the compatibility decision reached the metadata,
    # then call the engine with an engine config obtained from somewhere else.
    # Every step was optional, and each one omitted produced a plausible-looking
    # snapshot with a piece of its provenance missing.

    def fetch_chain(
        self,
        *,
        as_of: datetime,
        spot: float,
        spot_timestamp: datetime | None = None,
        open_interest_as_of: date | None = None,
        expected_contract_ids: tuple[str, ...] | None = None,
        expected_source: str = "none",
        capture: Any = None,
    ) -> ChainSnapshot:
        """Fetch the chain this session is configured for.

        Takes no ``request``: the request is the session's, derived once from
        the configuration. A caller who could substitute one could fetch a
        different symbol, a different DTE window or a different strike range
        from the one the compatibility assessment was made about, and the
        resulting snapshot would carry a provenance record describing a session
        that did not happen.

        Takes no ``risk_free_rate`` or ``dividend_yield`` either. Those are the
        model's, and they are exactly the numbers the compatibility check
        compared against the vendor's.
        """
        # ``or 0.0`` never fires in practice: ``ModelSpec`` resolves an absent
        # rate or dividend to a stated ZERO source. It is here so the types say
        # so rather than relying on the resolver staying that way.
        return self.runtime.fetch_chain(
            as_of=as_of,
            spot=spot,
            spot_timestamp=spot_timestamp,
            open_interest_as_of=open_interest_as_of,
            risk_free_rate=self.model_spec.risk_free_rate or 0.0,
            dividend_yield=self.model_spec.dividend_yield or 0.0,
            capture=capture,
            expected_contract_ids=expected_contract_ids,
            expected_source=expected_source,
            pipeline=self,
        )

    def compute_gex(self, chain: ChainSnapshot) -> Any:
        """Compute with this session's engine settings.

        The engine config is the one ``from_config`` validated against the
        pricing mode, so the gamma policy that reaches the calculation is the
        one the consistency check saw.
        """
        from src.gex.engine import compute_gex_snapshot

        return compute_gex_snapshot(chain, self.engine_config)

    def capture_and_compute(
        self,
        *,
        as_of: datetime,
        spot: float,
        spot_timestamp: datetime | None = None,
        open_interest_as_of: date | None = None,
        expected_contract_ids: tuple[str, ...] | None = None,
        expected_source: str = "none",
        capture: Any = None,
    ) -> Any:
        """Fetch and compute in one call, with the provenance attached.

        The whole point of the class in one method: there is no way to run this
        and end up with a snapshot that does not say which configuration, which
        model and which compatibility decision produced it.
        """
        return self.compute_gex(
            self.fetch_chain(
                as_of=as_of,
                spot=spot,
                spot_timestamp=spot_timestamp,
                open_interest_as_of=open_interest_as_of,
                expected_contract_ids=expected_contract_ids,
                expected_source=expected_source,
                capture=capture,
            )
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
                "vendor_gamma_policy": self.vendor_gamma_policy.value,
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
            "vendor_gamma_policy": self.vendor_gamma_policy.value,
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


def required_capabilities(
    mode: IvGammaPricingMode,
    *,
    policy: VendorGammaPolicy = VendorGammaPolicy.DISABLED,
) -> tuple[str, ...]:
    """What the subscription must expose for a session to be honest.

    Two independent additions. v2.1.3 read a single enum, so the second-order
    requirement could only be expressed by *replacing* the vendor-IV mode --
    which is how asking for a gamma comparison came to relax the IV checks.
    """
    from src.adapters.thetadata.capabilities import (
        SECOND_ORDER_GAMMA,
        CapabilityRequirement,
    )

    required: tuple[str, ...] = (
        CapabilityRequirement.BASELINE
        if mode is IvGammaPricingMode.LOCAL_IV_LOCAL_GAMMA
        else CapabilityRequirement.VENDOR_IV
    )
    if policy.requires_vendor_gamma:
        required = (*required, SECOND_ORDER_GAMMA)
    return tuple(dict.fromkeys(required))


def _require_consistent_gamma_policy(policy: VendorGammaPolicy, engine: Any) -> None:
    """No supported policy aggregates vendor gamma into the totals."""
    if (
        getattr(engine, "prefer_vendor_gamma", False)
        and not policy.aggregates_vendor_gamma
    ):
        raise PipelineConsistencyError(
            f"vendor_gamma_policy={policy.value} does not aggregate vendor "
            "gamma, but the engine is configured with prefer_vendor_gamma=True. "
            "Aggregating the vendor's gamma is a different policy with its own "
            "compatibility requirements, and it has not been built. Set "
            "prefer_vendor_gamma=False; vendor gamma is still available for "
            "comparison under COMPARE_ONLY."
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

    Runs whenever the IV is vendor-computed, **regardless of the vendor-gamma
    policy**. v2.1.3 short-circuited on a mode enum that
    ``VENDOR_GAMMA_VALIDATION`` replaced, so asking for a gamma comparison
    switched off the IV checks that comparison had nothing to do with.
    """
    if not config.pricing_mode.mixes_vendor_and_local_inside_one_calculation:
        return PricingCompatibilityReport(
            dimensions=tuple(
                _result(
                    dimension,
                    CompatibilityStatus.NOT_APPLICABLE,
                    "NO_VENDOR_QUANTITY_IN_CALCULATION",
                    detail=(
                        f"{config.pricing_mode.value} mixes no vendor-computed "
                        "quantity into a local calculation"
                    ),
                )
                for dimension in PricingDimension
            )
        )

    report = check_rate_compatibility(
        vendor=vendor_rate_assumption(config), local=local_rate_assumption(spec)
    ).merged_with(
        check_dividend_compatibility(
            vendor=vendor_dividend_assumption(config),
            local=local_dividend_assumption(spec),
        )
    )

    # Dimensions ThetaData does not document. UNKNOWN rather than MISMATCHED --
    # we have not found a disagreement, we have found that the question is
    # unanswerable from here -- and load-bearing, so each one blocks a trusted
    # calculation until a live comparison or vendor documentation settles it.
    undocumented = tuple(
        _result(
            dimension,
            CompatibilityStatus.UNKNOWN,
            "VENDOR_CONVENTION_UNDOCUMENTED",
            detail=note,
        )
        for dimension, note in (
            (
                PricingDimension.IV_PRICE_BASIS,
                "which price the vendor solved against, and how, is undocumented",
            ),
            (
                PricingDimension.UNDERLYING_SOURCE,
                "which underlying print the vendor used is undocumented",
            ),
            (
                PricingDimension.UNDERLYING_TIMESTAMP,
                "when the vendor read the underlying is undocumented",
            ),
            (
                PricingDimension.EXPIRATION_TIMESTAMP,
                "the vendor's settlement instant for its own solve is undocumented",
            ),
            (
                PricingDimension.DAY_COUNT,
                "the vendor's year fraction is undocumented",
            ),
            (
                PricingDimension.MINIMUM_TIME_FLOOR,
                "the vendor's short-dated floor is undocumented",
            ),
            (
                PricingDimension.SOLVER_VERSION,
                "the vendor's IV solver version is not exposed",
            ),
        )
    )
    report = report.merged_with(
        PricingCompatibilityReport(
            dimensions=undocumented,
            warnings=(
                "VENDOR_IV_LOCAL_GAMMA mixes a vendor-computed IV into a local "
                "gamma; model consistency cannot be claimed until a live "
                "comparison is run. See docs/ADAPTER_CERTIFICATION.md.",
            ),
        )
    )
    # The one production route from UNKNOWN to MATCHED. Each attestation names
    # its source and reference; none of them can overturn a MISMATCHED
    # dimension, which stays a hard failure.
    return apply_attestations(report, config.pricing_attestations)


def load_bearing_unknowns(report: PricingCompatibilityReport) -> tuple[str, ...]:
    """Dimensions that block a trusted calculation.

    Reads the typed results. v2.1.3 searched prose for substrings, so rewording
    a message could turn a blocker into a warning.
    """
    return tuple(d.value for d in report.load_bearing_unknowns)
