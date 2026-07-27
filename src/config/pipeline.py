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
    RateSource,
    UnderlyingPriceSource,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.adapters.thetadata.client import ChainRequest

__all__ = [
    "SUPPORTED_IV_SOURCES",
    "UNSUPPORTED_IV_SOURCES",
    "DividendConvention",
    "PipelineConsistencyError",
    "PricingCompatibilityReport",
    "PricingMode",
    "ThetaDataResearchPipeline",
    "VendorRateUnits",
    "check_dividend_compatibility",
    "check_rate_compatibility",
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


class VendorRateUnits(str, Enum):
    """How to read a vendor rate number.

    ``rate_value: 4.2`` is either 4.2% or 420%, and the difference is a factor
    of one hundred in every gamma. ThetaData's documentation does not state
    which, so ``UNKNOWN`` is the honest default and it blocks compatibility.
    """

    PERCENT = "percent"
    DECIMAL = "decimal"
    UNKNOWN = "unknown"


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
    UNKNOWN_VENDOR_DIVIDEND_CONVENTION = "UNKNOWN_VENDOR_DIVIDEND_CONVENTION"


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
    *,
    vendor_units: VendorRateUnits,
    vendor_value: float | None,
    local_rate: float | None,
    local_source: RateSource,
) -> PricingCompatibilityReport:
    """Does the vendor's rate mean the same number as ours?

    A vendor 4.2 and a local 4.2 agree only if the vendor's 4.2 is a decimal.
    If it is a percentage, the local value should be 0.042 and a match on the
    raw numbers is the *bug*, not the confirmation.
    """
    if vendor_value is None:
        return PricingCompatibilityReport(
            compatible=True,
            compatible_fields=("risk_free_rate (no vendor value sent)",),
        )
    if vendor_units is VendorRateUnits.UNKNOWN:
        return PricingCompatibilityReport(
            compatible=False,
            unknown_fields=(
                "risk_free_rate: vendor rate units are undocumented, so "
                f"{vendor_value} cannot be compared with the local rate",
            ),
        )
    if local_source is RateSource.ZERO:
        return PricingCompatibilityReport(
            compatible=False,
            incompatible_fields=(
                f"risk_free_rate: vendor priced with {vendor_value} but the "
                "local model uses an explicit zero rate",
            ),
        )
    if local_rate is None:
        return PricingCompatibilityReport(
            compatible=False,
            unknown_fields=("risk_free_rate: no local rate to compare against",),
        )
    as_decimal = (
        vendor_value / 100.0
        if vendor_units is VendorRateUnits.PERCENT
        else (vendor_value)
    )
    if abs(as_decimal - local_rate) <= 1e-9:
        return PricingCompatibilityReport(
            compatible=True,
            compatible_fields=(
                f"risk_free_rate ({vendor_value} {vendor_units.value} == "
                f"{local_rate} decimal)",
            ),
        )
    return PricingCompatibilityReport(
        compatible=False,
        incompatible_fields=(
            f"risk_free_rate: vendor {vendor_value} {vendor_units.value} is "
            f"{as_decimal} as a decimal, local model uses {local_rate}",
        ),
    )


def check_dividend_compatibility(
    *,
    vendor: DividendConvention,
    local_source: DividendSource,
) -> PricingCompatibilityReport:
    """Does the vendor's dividend input mean the same quantity as ours?"""
    if vendor is DividendConvention.UNKNOWN_VENDOR_DIVIDEND_CONVENTION:
        return PricingCompatibilityReport(
            compatible=False,
            unknown_fields=(
                "dividend_yield: the vendor's annual_dividend may be a cash "
                "amount or a continuous yield; ThetaData does not document "
                "which, and the two are not interchangeable",
            ),
        )
    if vendor is DividendConvention.ZERO_DIVIDEND:
        if local_source is DividendSource.ZERO:
            return PricingCompatibilityReport(
                compatible=True, compatible_fields=("dividend_yield (both zero)",)
            )
        return PricingCompatibilityReport(
            compatible=False,
            incompatible_fields=(
                "dividend_yield: vendor priced with no dividend, local model "
                f"uses {local_source.value}",
            ),
        )
    if vendor is DividendConvention.ANNUAL_CASH_DIVIDEND:
        return PricingCompatibilityReport(
            compatible=False,
            incompatible_fields=(
                "dividend_yield: vendor supplied an annual CASH dividend; the "
                "local model consumes a continuous yield. Converting needs the "
                "spot and the payment schedule, neither of which is available "
                "here",
            ),
        )
    # CONTINUOUS_DIVIDEND_YIELD
    if local_source in (DividendSource.CONFIGURED_CONSTANT, DividendSource.SNAPSHOT):
        return PricingCompatibilityReport(
            compatible=True,
            compatible_fields=("dividend_yield (both continuous yields)",),
        )
    return PricingCompatibilityReport(
        compatible=False,
        incompatible_fields=(
            "dividend_yield: vendor supplied a continuous yield, local model "
            f"uses {local_source.value}",
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
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_config(
        cls,
        config: ThetaDataConfig,
        *,
        symbol: str = "SPXW",
        transport: Any = None,
        clock: Any = None,
        model_spec: ModelSpec | None = None,
    ) -> ThetaDataResearchPipeline:
        """Build a coherent session, or refuse.

        ``model_spec`` exists so an existing spec can be *checked* against the
        config -- not so individual fields can be overridden. Every consistency
        rule below was a way two objects could disagree in v2.1.1.
        """
        require_supported_iv_source(config.iv_source)

        spec = model_spec if model_spec is not None else config.to_model_spec()
        _require_consistent(config, spec)

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
            "warnings": list(self.warnings),
        }


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


def assess_pricing_compatibility(
    config: ThetaDataConfig, spec: ModelSpec
) -> PricingCompatibilityReport:
    """Whether vendor-derived numbers may enter a local calculation."""
    if not config.pricing_mode.mixes_vendor_and_local_inside_one_calculation:
        return PricingCompatibilityReport(
            compatible=True,
            compatible_fields=(
                f"{config.pricing_mode.value}: no vendor-computed quantity "
                "enters a local calculation, so no agreement is required",
            ),
        )

    report = check_rate_compatibility(
        vendor_units=config.rate_units,
        vendor_value=config.rate_value,
        local_rate=spec.risk_free_rate,
        local_source=spec.risk_free_rate_source,
    ).merged_with(
        check_dividend_compatibility(
            vendor=config.dividend_convention,
            local_source=spec.dividend_yield_source,
        )
    )

    # Dimensions ThetaData does not document at all. They are unknown rather
    # than incompatible, and they stay unknown until a live comparison settles
    # them. See docs/OPEN_DECISIONS.md.
    return report.merged_with(
        PricingCompatibilityReport(
            compatible=False,
            unknown_fields=(
                "expiration_timestamp: the vendor's settlement instant for its "
                "own IV solve is undocumented",
                "day_count_convention: the vendor's year fraction is undocumented",
                "minimum_time_to_expiry: the vendor's short-dated floor is "
                "undocumented",
                "quote_price_source: which price the vendor solved against is "
                "undocumented",
                "solver_version: the vendor's IV solver version is not exposed",
            ),
            warnings=(
                "VENDOR_IV_LOCAL_GAMMA mixes a vendor-computed IV into a local "
                "gamma. Five vendor-side conventions are undocumented, so model "
                "consistency cannot be claimed until a live comparison is run.",
            ),
        )
    )
