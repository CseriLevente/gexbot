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
from dataclasses import replace as _replace
from enum import Enum
from typing import TYPE_CHECKING, Any

from src.adapters.raw_store import PARSER_VERSION
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
from src.domain.digests import short_id
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
    "CalculationMode",
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

    # The units stay UNKNOWN however confidently the configuration states them.
    #
    # We send ``rate_value``. We do not send ``rate_units`` -- there is no such
    # query parameter -- so how ThetaData reads the number is a fact about its
    # API, not about our YAML. v2.1.4 marked this MATCHED from the local setting
    # and thereby let a local declaration settle a remote semantic. Everything
    # below is computed *under* the stated units, and says so.
    units = _result(
        PricingDimension.RATE_UNITS,
        CompatibilityStatus.UNKNOWN,
        "VENDOR_RATE_UNITS_UNVERIFIED",
        vendor=None,
        local=local.unit.value,
        detail=(
            f"the configuration states the vendor reads rate_value as "
            f"{vendor.unit.value}, but rate_units is not a parameter this "
            "adapter sends; nothing has confirmed how the API reads it. 4.2 is "
            "either 4.2% or 420%, and the difference is a factor of a hundred "
            "in every gamma"
        ),
    )
    conditional = (
        " -- conditional on the unverified rate units, which are the vendor's to define"
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
                detail=(
                    f"we send {vendor.raw_value} and the model uses "
                    f"{local.normalized}, which agree{conditional}"
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

    # The convention stays UNKNOWN however confidently the configuration states
    # it, for the same reason as the rate units: we send ``annual_dividend`` and
    # we do not send ``dividend_convention``. Whether ThetaData reads 1.3 as a
    # cash amount or a continuous yield is a fact about its API. v2.1.4 marked
    # this MATCHED from the local setting.
    convention = _result(
        PricingDimension.DIVIDEND_CONVENTION,
        CompatibilityStatus.UNKNOWN,
        "VENDOR_DIVIDEND_CONVENTION_UNVERIFIED",
        vendor=None,
        local=local.convention.value,
        detail=(
            f"the configuration states the vendor reads annual_dividend as "
            f"{vendor.convention.value}, but dividend_convention is not a "
            "parameter this adapter sends; nothing has confirmed how the API "
            "reads it"
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
                    detail=(
                        "zero is the one dividend whose *value* does not depend "
                        "on the convention: exp(-0*T) is 1 whether the vendor "
                        "read it as cash or as a yield. The convention itself "
                        "stays unverified"
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
    # The two numbers agree. Whether they *mean* the same thing depends on the
    # convention, which is the vendor's and is unverified -- so a non-zero
    # dividend is not settled by its magnitude alone. Zero is the exception,
    # handled above.
    return PricingCompatibilityReport(
        dimensions=(
            convention,
            _result(
                PricingDimension.DIVIDEND_VALUE,
                CompatibilityStatus.UNKNOWN,
                "DIVIDEND_VALUE_UNVERIFIED_UNDER_AN_UNKNOWN_CONVENTION",
                vendor=vendor.value,
                local=local.value,
                detail=(
                    f"both sides carry {vendor.value}, but a cash amount and a "
                    "continuous yield of the same magnitude are different "
                    "quantities and the vendor's reading is unverified"
                ),
            ),
        )
    )


class CalculationMode(str, Enum):
    """Which of the two calculations produced a snapshot.

    Stamped into the metadata, so a number carries the answer with it. v2.1.4
    had one calculation and no way to tell from the result whether the
    assumptions behind it had been established.
    """

    #: Computed under whatever is currently known. Never an input to anything
    #: that stands behind a number.
    DIAGNOSTIC_UNTRUSTED = "DIAGNOSTIC_UNTRUSTED"
    #: Every dependency established: pricing resolved, provenance observed,
    #: capture complete, fingerprints matching.
    TRUSTED = "TRUSTED"


#: Emitted by every diagnostic calculation, so a scan of the warnings finds them.
DIAGNOSTIC_WARNING_CODE = "DIAGNOSTIC_UNTRUSTED_CALCULATION"


def _mapping(value: object) -> dict[str, Any]:
    """A nested metadata mapping, or an empty one.

    ``ChainSnapshot.meta`` is ``dict[str, object]`` by design -- adapters put
    arbitrary provenance in it -- so reading two levels down needs a narrowing
    step rather than an annotation asserting what came out.
    """
    return dict(value) if isinstance(value, dict) else {}


def _chain_fingerprint(chain: Any) -> str:
    """A digest of the chain a calculation ran on."""
    payload = json.dumps(
        {
            "as_of": chain.as_of.isoformat(),
            "spot": chain.spot,
            "contracts": sorted(q.contract.canonical_id for q in chain.quotes),
            "manifest": (chain.meta.get("raw_capture_manifest") or {}).get(
                "manifest_hash"
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _open_interest_as_of(chain: Any) -> Any:
    """The settlement date the chain's contracts carry, when they agree.

    Read from the quotes rather than from ``meta``: the recipe has to describe
    what normalization actually produced, and a metadata key is a description.
    ``None`` when the chain is empty or its contracts disagree, which is a state
    the trusted path refuses elsewhere rather than papering over here.
    """
    dates = {
        quote.timestamps.open_interest_as_of for quote in getattr(chain, "quotes", ())
    }
    return dates.pop() if len(dates) == 1 else None


def _first_field_difference(supplied: Any, rederived: Any) -> str:
    """Name the first field that differs, so a refusal is actionable.

    A digest mismatch on its own tells an operator that something moved and
    nothing about what. This walks the same canonical payload the hash covers
    and reports the first disagreement it finds.
    """
    from src.domain.normalization import canonical_chain_payload

    left, right = canonical_chain_payload(supplied), canonical_chain_payload(rederived)
    for key in sorted(set(left) | set(right)):
        if key == "quotes":
            continue
        if left.get(key) != right.get(key):
            return f"{key} is {left.get(key)!r} here and {right.get(key)!r} there"

    by_id = {entry["contract_id"]: entry for entry in right.get("quotes", ())}
    for entry in left.get("quotes", ()):
        other = by_id.pop(entry["contract_id"], None)
        if other is None:
            return f"contract {entry['contract_id']} is not in the re-derived chain"
        for field_name in sorted(entry):
            if entry[field_name] != other.get(field_name):
                return (
                    f"contract {entry['contract_id']} has {field_name} "
                    f"{entry[field_name]!r} here and "
                    f"{other.get(field_name)!r} there"
                )
    if by_id:
        return f"the re-derived chain has contracts this one does not: {sorted(by_id)}"
    return "the difference is not in a field either payload names"


def _same_price(left: object, right: object, tolerance: float = 1e-9) -> bool:
    """Two readings of the same underlying, to the precision it is quoted at."""
    try:
        return abs(float(left) - float(right)) <= tolerance  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _require_a_chain(chain: Any, *, mode: str) -> None:
    """Refuse a computed snapshot where a chain belongs.

    A ``GexSnapshot`` has ``meta`` and looks close enough to a chain to get some
    way in. Passing a diagnostic result back as trusted input is the specific
    mistake worth naming.
    """
    from src.domain.contracts import ChainSnapshot

    if isinstance(chain, ChainSnapshot):
        return
    calculation_mode = getattr(chain, "meta", {}).get("calculation_mode")
    if calculation_mode == CalculationMode.DIAGNOSTIC_UNTRUSTED.value:
        raise PipelineConsistencyError(
            "this is the output of a diagnostic calculation, not a chain. A "
            "diagnostic result is untrusted permanently; recomputing from it "
            "would launder that away."
        )
    raise PipelineConsistencyError(
        f"the {mode} calculation needs a ChainSnapshot, got {type(chain).__name__}"
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
        open_interest_as_of: date | None = None,
        expected_contract_ids: tuple[str, ...] | None = None,
        expected_source: str = "none",
        capture: Any = None,
        store: Any = None,
    ) -> ChainSnapshot:
        """Fetch the chain this session is configured for, spot included.

        Takes no ``spot``. Under ``underlying_price_source:
        vendor_index_snapshot`` the underlying is the vendor's to give, and
        every gamma in the snapshot is computed against it -- so this fetches
        the index snapshot itself, inside the same capture session, and uses
        what came back. v2.1.4 accepted whatever number the caller passed and
        recorded it as the session's spot.

        Takes no ``request`` either: the request is the session's, derived once
        from the configuration. A caller who could substitute one could fetch a
        different symbol, DTE window or strike range from the one the
        compatibility assessment was made about.

        For a genuinely external spot -- a historical research run, a spot from
        somewhere this adapter does not reach -- see
        ``fetch_chain_with_external_spot``, which is a different method because
        it produces a differently-provenanced snapshot.
        """
        self.validate_integrity()
        if not self._uses_vendor_index_spot:
            raise PipelineConsistencyError(
                f"underlying_price_source is {self.config.underlying_price_source!r}, "
                "which this method cannot fetch. Use "
                "fetch_chain_with_external_spot, which records the spot as "
                "caller-supplied rather than observed."
            )
        session = (
            capture
            if capture is not None
            else self._new_capture_session(as_of, store=store)
        )
        mark = session.mark() if session is not None else 0
        spot, spot_timestamp, observation = self._capture_index_spot(
            as_of=as_of, capture=session
        )
        return self._assemble(
            as_of=as_of,
            spot=spot,
            spot_timestamp=spot_timestamp,
            spot_source="vendor_index_snapshot",
            spot_observation=observation,
            open_interest_as_of=open_interest_as_of,
            expected_contract_ids=expected_contract_ids,
            expected_source=expected_source,
            capture=session,
            mark=mark,
        )

    def fetch_chain_with_external_spot(
        self,
        *,
        as_of: datetime,
        spot: float,
        spot_timestamp: datetime | None = None,
        open_interest_as_of: date | None = None,
        expected_contract_ids: tuple[str, ...] | None = None,
        expected_source: str = "none",
        capture: Any = None,
        store: Any = None,
    ) -> ChainSnapshot:
        """Fetch with a spot from outside this adapter.

        A real use -- a research run against a historical print, a spot from a
        source this repository does not read -- and a different claim. The
        snapshot records the spot as ``caller_supplied``, which keeps it out of
        a trusted calculation.
        """
        self.validate_integrity()
        session = (
            capture
            if capture is not None
            else self._new_capture_session(as_of, store=store)
        )
        mark = session.mark() if session is not None else 0
        return self._assemble(
            as_of=as_of,
            spot=spot,
            spot_timestamp=spot_timestamp,
            spot_source="caller_supplied",
            spot_observation=None,
            open_interest_as_of=open_interest_as_of,
            expected_contract_ids=expected_contract_ids,
            expected_source=expected_source,
            capture=session,
            mark=mark,
        )

    @property
    def _uses_vendor_index_spot(self) -> bool:
        return self.config.underlying_price_source == "vendor_index_snapshot"

    def _new_capture_session(self, as_of: datetime, *, store: Any = None) -> Any:
        """Open a capture session for one fetch.

        ``store`` overrides where the evidence lands. The default is the store
        this session was configured with; naming one explicitly is how a caller
        keeps a fetch and the manifest it will be verified against in the same
        place.

        The session is stamped with what the repository looks like *now*, and
        every record it writes carries the stamp. v2.1.6 put the pipeline
        fingerprint on the manifest alone, which made relabelling a capture a
        one-field edit that the evidence could not contradict.
        """
        from src.adapters.raw_store import new_capture_session_id
        from src.adapters.thetadata.client import capture_origin_of

        if not self.config.raw_capture_enabled:
            return None
        return self.capture_session(
            store=store if store is not None else self.runtime.client.raw_store,
            session_id=new_capture_session_id(as_of=as_of),
            as_of=as_of,
            capture_origin=capture_origin_of(self.runtime.client.transport),
        )

    def capture_session(
        self,
        *,
        store: Any,
        session_id: str,
        as_of: datetime,
        capture_origin: Any = None,
        open_interest_as_of: date | None = None,
        expected_universe: Any = None,
        open_interest_date_rule_fingerprint: str | None = None,
    ) -> Any:
        """A capture session stamped with this pipeline's identity.

        The one way to open a session that produces verifiable records. The
        five capture-time claims are computed here, from the pipeline, and are
        immutable for the life of the session -- so every record it writes can
        be asked which pipeline, plan, request specification and normalization
        recipe were in force when the bytes arrived.
        """
        from src.adapters.raw_store import CaptureOrigin, CaptureSession
        from src.adapters.thetadata.client import capture_origin_of

        recipe = self.normalization_recipe(
            as_of=as_of,
            open_interest_as_of=open_interest_as_of,
            expected_universe_fingerprint=(
                expected_universe.universe_hash if expected_universe else None
            ),
        )
        operation = self.begin_operation(
            requested_as_of=as_of,
            session_id=session_id,
            open_interest_as_of=open_interest_as_of,
            expected_universe=expected_universe,
            open_interest_date_rule_fingerprint=open_interest_date_rule_fingerprint,
        )
        return CaptureSession(
            store=store,
            session_id=session_id,
            capture_origin=(
                capture_origin
                if capture_origin is not None
                else capture_origin_of(self.runtime.client.transport)
            )
            or CaptureOrigin.UNKNOWN_ORIGIN,
            pipeline_fingerprint=self.fingerprint(),
            capture_plan_fingerprint=self.capture_plan.fingerprint,
            request_spec_fingerprint=recipe.request_spec_fingerprint,
            # The *rules*, not this fetch's parameters. Stamping the
            # full recipe hash would bind the record to one market
            # instant, and verification would have to guess it back.
            normalization_recipe_fingerprint=recipe.rules_fingerprint,
            # The operation. This is what carries the per-fetch inputs -- above
            # all the instant every gamma is priced against, which v2.1.7 left
            # unbound so the chain under test could choose it.
            operation_id=operation.operation_id,
            operation_fingerprint=operation.operation_fingerprint,
            normalization_recipe_hash=recipe.recipe_hash,
            requested_as_of=operation.requested_as_of,
            effective_valuation_timestamp=operation.effective_valuation_timestamp,
            valuation_timestamp_rule=operation.valuation_timestamp_rule.value,
            # The universe and the settlement rule this operation was opened
            # under. Stamped rather than passed at calculation time, so a replay
            # is measured against the universe the capture expected instead of
            # whichever one the caller happens to hold.
            expected_universe_fingerprint=(
                expected_universe.universe_hash if expected_universe else ""
            ),
            open_interest_date_rule_fingerprint=(
                open_interest_date_rule_fingerprint or ""
            ),
        )

    def _capture_index_spot(
        self, *, as_of: datetime, capture: Any
    ) -> tuple[float, datetime | None, Any]:
        """Fetch the index snapshot into this session and read it back."""
        record = self.runtime.capture_index_snapshot(as_of=as_of, capture=capture)
        if record is None:
            raise PipelineConsistencyError(
                "the index snapshot could not be captured, so this session has "
                "no underlying it can attribute. Every gamma is computed "
                "against that number."
            )
        return record

    def _assemble(
        self,
        *,
        as_of: datetime,
        spot: float,
        spot_timestamp: datetime | None,
        spot_source: str,
        spot_observation: Any,
        open_interest_as_of: date | None,
        expected_contract_ids: tuple[str, ...] | None,
        expected_source: str,
        capture: Any,
        mark: int,
    ) -> ChainSnapshot:
        from dataclasses import replace as _replace

        # ``or 0.0`` never fires in practice: ``ModelSpec`` resolves an absent
        # rate or dividend to a stated ZERO source. It is here so the types say
        # so rather than relying on the resolver staying that way.
        chain = self.runtime.fetch_chain(
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
            manifest_since=mark,
            capture_plan_fingerprint=self.capture_plan.fingerprint,
        )
        return _replace(
            chain,
            meta={
                **chain.meta,
                "spot_provenance": {
                    "source": spot_source,
                    "timestamp": (
                        spot_timestamp.isoformat() if spot_timestamp else None
                    ),
                    "observation": (
                        spot_observation.as_dict() if spot_observation else None
                    ),
                },
            },
        )

    @property
    def capture_plan(self) -> Any:
        """What this session must capture. Derived from what it is configured to do."""
        from src.adapters.thetadata.capture_plan import capture_plan_for
        from src.adapters.thetadata.endpoints import Tier

        return capture_plan_for(
            pricing_mode=self.pricing_mode,
            vendor_gamma_policy=self.vendor_gamma_policy,
            underlying_price_source=self.config.underlying_price_source,
            tier=Tier(self.config.tier),
        )

    # -- binding a normalized chain to the bytes behind it --------------------

    # -- the capture operation ------------------------------------------------

    @property
    def valuation_timestamp_rule(self) -> Any:
        """Which instant this session prices against, and where it comes from.

        Under ``vendor_index_snapshot`` the answer is the clock the index print
        carried: it is in the verified bytes, and it is the instant the
        underlying every gamma rests on was actually observed. Anything else
        falls back to the capture request instant, which is a fact about this
        process rather than about the market and does not support a trusted
        calculation on its own.
        """
        from src.adapters.capture_operation import ValuationTimestampRule

        if self.config.underlying_price_source == "vendor_index_snapshot":
            return ValuationTimestampRule.INDEX_PRINT_TIMESTAMP
        return ValuationTimestampRule.CAPTURE_REQUEST_INSTANT

    @property
    def spot_synchronization_policy(self) -> dict[str, Any]:
        """The tolerance a spot print must meet, and where it came from.

        Configuration, not an argument. v2.1.7 let a caller construct a
        ``SpotProvenance`` with any ``tolerance_seconds`` it liked and hand it to
        the trusted path, so one calculation could be granted a wider window
        than the pipeline was configured for -- and the skew check is the only
        thing standing between a chain and an underlying it never saw.
        """
        return {
            "max_spot_skew_seconds": self.config.max_spot_skew_seconds,
            "underlying_price_source": self.config.underlying_price_source,
            "valuation_timestamp_rule": self.valuation_timestamp_rule.value,
        }

    @property
    def spot_synchronization_policy_fingerprint(self) -> str:
        from src.domain.digests import digest_of

        return digest_of(self.spot_synchronization_policy)

    @property
    def documentation_evidence_fingerprints(self) -> dict[str, str]:
        """Every document this pipeline's conventions rest on, by content.

        A pricing dimension resolved by vendor documentation is a load-bearing
        answer sourced from a page the vendor controls. Until v2.1.8 the
        reference travelled into the fingerprint and the *content* did not, so a
        rewritten page changed nothing anywhere: the same fingerprint described
        two different claims about how gamma should be priced.

        Keyed by reference and sorted, so the mapping is stable and a reader of
        the audit trail can see which documents were relied on and check them.
        """
        return {
            observation.reference: observation.document_content_hash
            for observation in sorted(
                self.config.pricing_attestations, key=lambda o: o.reference
            )
            if observation.document_content_hash
        }

    def begin_operation(
        self,
        *,
        requested_as_of: datetime,
        session_id: str,
        open_interest_as_of: date | None = None,
        expected_universe: Any = None,
        open_interest_date_rule_fingerprint: str | None = None,
    ) -> Any:
        """Fix everything one capture operation decides, before it runs.

        The *provisional* identity: ``effective_valuation_timestamp`` equals the
        requested instant, because no response has arrived yet and the rule that
        will select it is recorded rather than guessed at. This is what every
        record of the operation carries.

        ``resolve_operation`` produces the settled identity afterwards, reading
        the effective instant out of the verified bytes. The split is deliberate
        -- a value stamped before the evidence exists would be an assertion, and
        the whole point is that the instant is *derived*.
        """
        from src.adapters.capture_operation import (
            CaptureOperationIdentity,
            new_operation_id,
        )

        recipe = self.normalization_recipe(
            as_of=requested_as_of,
            open_interest_as_of=open_interest_as_of,
            expected_universe_fingerprint=(
                expected_universe.universe_hash if expected_universe else None
            ),
        )
        return CaptureOperationIdentity(
            operation_id=new_operation_id(as_of=requested_as_of),
            session_id=session_id,
            pipeline_fingerprint=self.fingerprint(),
            capture_plan_fingerprint=self.capture_plan.fingerprint,
            request_spec_fingerprint=recipe.request_spec_fingerprint,
            normalization_recipe_hash=recipe.recipe_hash,
            requested_as_of=requested_as_of,
            effective_valuation_timestamp=requested_as_of,
            valuation_timestamp_rule=self.valuation_timestamp_rule,
            spot_synchronization_policy_fingerprint=(
                self.spot_synchronization_policy_fingerprint
            ),
            open_interest_date_rule_fingerprint=open_interest_date_rule_fingerprint,
            expected_universe_fingerprint=(
                expected_universe.universe_hash if expected_universe else None
            ),
            parser_version=PARSER_VERSION,
        )

    def resolve_operation(
        self, *, manifest: Any, store: Any, expected_universe: Any = None
    ) -> Any:
        """The settled operation identity, with the instant read from evidence.

        Reconstructed from what the records were stamped with, then completed by
        reading the index print out of the verified bytes. **Nothing here comes
        from the chain being checked** -- that is the defect this closes. v2.1.7
        rebuilt with ``as_of=chain.as_of``, so the chain under test chose the
        timestamp it was tested against and a shifted one shifted the rebuild
        with it.

        ``expected_universe`` is checked against the stamp rather than adopted
        from it. A universe supplied at calculation time decides completeness,
        the confidence score and whether a dataset is fit to build on, and in
        v2.1.7 it was an argument -- so the same capture could be scored
        ``MEASURED_COMPLETE`` against one universe and replayed
        ``PARTIALLY_OBSERVED`` against another. Two answers from the same bytes.
        """
        from src.adapters.capture_operation import (
            CaptureOperationIdentity,
            ValuationTimestampRule,
        )

        stamped = self._stamped_operation(manifest)
        self._require_captured_universe(stamped, expected_universe)
        rule = ValuationTimestampRule(stamped["valuation_timestamp_rule"])
        effective = stamped["effective_valuation_timestamp"]
        if rule is ValuationTimestampRule.INDEX_PRINT_TIMESTAMP:
            effective = self._index_print_instant(manifest=manifest, store=store)

        return CaptureOperationIdentity(
            operation_id=stamped["operation_id"],
            session_id=manifest.session_id,
            pipeline_fingerprint=stamped["pipeline_fingerprint"],
            capture_plan_fingerprint=stamped["capture_plan_fingerprint"],
            request_spec_fingerprint=stamped["request_spec_fingerprint"],
            normalization_recipe_hash=stamped["normalization_recipe_hash"],
            requested_as_of=stamped["requested_as_of"],
            effective_valuation_timestamp=effective,
            valuation_timestamp_rule=rule,
            spot_synchronization_policy_fingerprint=stamped[
                "spot_synchronization_policy_fingerprint"
            ],
            open_interest_date_rule_fingerprint=stamped[
                "open_interest_date_rule_fingerprint"
            ],
            expected_universe_fingerprint=stamped["expected_universe_fingerprint"],
            parser_version=stamped["parser_version"],
        )

    @staticmethod
    def _require_captured_universe(
        stamped: dict[str, Any], expected_universe: Any
    ) -> None:
        """The universe a replay measures against must be the captured one."""
        captured = stamped["expected_universe_fingerprint"]
        supplied = (
            expected_universe.universe_hash if expected_universe is not None else None
        )
        if captured == supplied:
            return
        if captured is None:
            raise PipelineConsistencyError(
                "an expected contract universe was supplied at calculation time, "
                "and this capture operation declared none. Whether a chain is "
                "complete is decided against the universe the capture expected; "
                "one produced afterwards can be shaped to whatever arrived, "
                f"which is what {expected_universe.display_id!r} would be doing "
                "here. Declare it on capture_session()."
            )
        if supplied is None:
            raise PipelineConsistencyError(
                f"this capture operation was opened against expected universe "
                f"{short_id(captured)}... and none was supplied for the "
                "calculation. Dropping it would report a chain as complete by "
                "having nothing to be incomplete against."
            )
        raise PipelineConsistencyError(
            f"this capture operation expected universe {short_id(captured)}... "
            f"and the calculation supplied {short_id(supplied)}.... The same "
            "bytes would be MEASURED_COMPLETE against one and "
            "PARTIALLY_OBSERVED against the other."
        )

    def _stamped_operation(self, manifest: Any) -> dict[str, Any]:
        """What every record of this manifest agrees it was captured under.

        Refuses a manifest whose records disagree. Two operations in one
        manifest is not one operation, and picking a majority would be inventing
        a session that did not happen.
        """
        if not manifest.records:
            raise PipelineConsistencyError(
                "the manifest names no records, so there is no operation to resolve"
            )
        stamps = {r.operation_fingerprint for r in manifest.records}
        if len(stamps) != 1 or not stamps.pop():
            raise PipelineConsistencyError(
                "the records of this manifest were captured under "
                f"{len({r.operation_fingerprint for r in manifest.records})} "
                "different operations, or under none. A chain re-derived from a "
                "mixture of operations was not produced by any of them."
            )
        first = manifest.records[0]
        if first.requested_as_of is None:
            raise PipelineConsistencyError(
                f"record {first.record_id!r} carries no requested capture "
                "instant, so the valuation timestamp it was captured for cannot "
                "be recovered. Captures written before v2.1.8 are refused rather "
                "than given a timestamp this process invented."
            )
        return {
            "operation_id": first.operation_id,
            "pipeline_fingerprint": first.pipeline_fingerprint,
            "capture_plan_fingerprint": first.capture_plan_fingerprint,
            "request_spec_fingerprint": first.request_spec_fingerprint,
            "normalization_recipe_hash": first.normalization_recipe_hash,
            "requested_as_of": first.requested_as_of,
            "effective_valuation_timestamp": (
                first.effective_valuation_timestamp or first.requested_as_of
            ),
            "valuation_timestamp_rule": (
                first.valuation_timestamp_rule or self.valuation_timestamp_rule.value
            ),
            "spot_synchronization_policy_fingerprint": (
                self.spot_synchronization_policy_fingerprint
            ),
            "open_interest_date_rule_fingerprint": (
                first.open_interest_date_rule_fingerprint or None
            ),
            "expected_universe_fingerprint": (
                first.expected_universe_fingerprint or None
            ),
            "parser_version": first.parser_version,
        }

    def _index_print_instant(self, *, manifest: Any, store: Any) -> datetime:
        """The clock the captured index snapshot carried.

        Read out of the stored bytes through the ordinary parser, so the instant
        every gamma is priced against is one the vendor sent rather than one a
        caller supplied.
        """
        from src.adapters.transport import StoredPayloadTransport
        from src.config.thetadata import ThetaDataRuntime

        replay = ThetaDataRuntime.from_config(
            _replace(self.config, raw_capture_enabled=False, raw_capture_path=None),
            symbol=self.runtime.default_chain_request.symbol,
            transport=StoredPayloadTransport.from_capture(
                manifest=manifest, store=store
            ),
        )
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        record = replay.client.fetch_index_snapshot(
            symbol=replay.default_chain_request.symbol,
            # Only a freshness reference for the fetch. The instant this method
            # returns is the one the *payload* carried.
            as_of=_datetime.now(_UTC),
        )
        if record is None or record.timestamp is None:
            raise PipelineConsistencyError(
                "the captured index snapshot carries no clock, so the instant "
                "this chain was priced against cannot be derived from the "
                "evidence. A trusted calculation cannot rest on a timestamp "
                "nobody can point at."
            )
        return record.timestamp

    def request_spec(self) -> Any:
        """What this session would send to each endpoint of its plan.

        Canonical and computed from configuration alone, so it can be
        recomputed later without the capture and compared against it. Stamped
        onto every record so a capture taken at ``rate_value=4.2`` cannot be
        presented as one from a pipeline configured with 3.1 -- the vendor
        computed those greeks under a rate, and it is not the one being claimed.
        """
        from src.adapters.thetadata.request_spec import build_request_spec

        return build_request_spec(
            request=self.runtime.default_chain_request,
            greeks=self.runtime.client.greeks,
            settings=self.runtime.client.settings,
            endpoints=self.capture_plan.required_endpoints,
        )

    def normalization_recipe(
        self,
        *,
        as_of: datetime,
        open_interest_as_of: date | None = None,
        expected_universe_fingerprint: str | None = None,
    ) -> Any:
        """Every input besides the raw bytes that decides what normalization gives.

        Two captures of identical payloads normalize differently under a
        different IV source, duplicate policy or rate. Rebuilding therefore
        needs this, and its digest is stamped at capture time so a chain cannot
        be re-derived under rules the capture never saw.
        """
        from src.domain.normalization import NormalizationRecipe

        return NormalizationRecipe(
            parser_version=PARSER_VERSION,
            pipeline_fingerprint=self.fingerprint(),
            model_fingerprint=self.model_spec.fingerprint(),
            capture_plan_fingerprint=self.capture_plan.fingerprint,
            request_spec_fingerprint=self.request_spec().fingerprint,
            as_of=as_of,
            iv_source=self.runtime.iv_source.value,
            duplicate_policy=self.runtime.duplicate_policy,
            risk_free_rate=self.model_spec.risk_free_rate,
            dividend_yield=self.model_spec.dividend_yield,
            spot_source=self.config.underlying_price_source,
            open_interest_as_of=open_interest_as_of,
            expected_universe_fingerprint=expected_universe_fingerprint,
        )

    def rebuild_from_capture(
        self,
        *,
        manifest: Any,
        store: Any,
        recipe: Any,
        operation: Any = None,
        expected_universe: Any = None,
    ) -> tuple[ChainSnapshot, Any]:
        """Normalize the stored payloads again, and say which ones were used.

        Returns the chain *and* the consumption report, because the two answer
        different questions and only one of them is a chain: v2.1.7 replayed a
        capture without ever asking whether the replay used all of it, so a
        capture carrying an extra unread response verified.

        Replays the capture through the ordinary fetch path with a transport
        that answers from the store. That is deliberate: a separate rebuilder
        that re-read the CSVs itself would be a second implementation of
        normalization, and two implementations of the same thing drift. Here the
        client, the parser, the join and the model are the production ones and
        only the bytes come from somewhere else.

        The rebuilt chain carries no capture manifest of its own -- it is not a
        session, it is a re-derivation -- and nothing is written to any store.
        """
        from src.adapters.transport import StoredPayloadTransport
        from src.config.thetadata import ThetaDataRuntime

        self.validate_integrity()
        if recipe.pipeline_fingerprint != self.fingerprint():
            raise PipelineConsistencyError(
                f"the recipe was written for pipeline "
                f"{recipe.pipeline_fingerprint!r}; this one is "
                f"{self.fingerprint()!r}. Rebuilding under different rules "
                "produces a different chain, which would be compared against "
                "the original and read as tampering."
            )

        transport = StoredPayloadTransport.from_capture(manifest=manifest, store=store)
        replay = ThetaDataRuntime.from_config(
            # Capture is off for the rebuild. The evidence already exists; a
            # rebuild that wrote more of it would be appending to the audit
            # trail it is checking.
            _replace(self.config, raw_capture_enabled=False, raw_capture_path=None),
            symbol=self.runtime.default_chain_request.symbol,
            transport=transport,
        )
        spot, spot_timestamp = self._replayed_spot(
            runtime=replay, recipe=recipe, manifest=manifest, store=store
        )
        # The valuation instant comes from the *operation*, which resolved it
        # from the verified index print. v2.1.7 used ``recipe.as_of``, and the
        # recipe was built with ``as_of=chain.as_of`` -- so the chain being
        # checked chose the timestamp it was checked against, and shifting it
        # shifted the rebuild too.
        as_of = (
            operation.effective_valuation_timestamp
            if operation is not None
            else recipe.as_of
        )
        chain = replay.fetch_chain(
            as_of=as_of,
            spot=spot,
            spot_timestamp=spot_timestamp,
            open_interest_as_of=recipe.open_interest_as_of,
            risk_free_rate=recipe.risk_free_rate or 0.0,
            dividend_yield=recipe.dividend_yield or 0.0,
            expected_contract_ids=(
                tuple(sorted(expected_universe.identities))
                if expected_universe is not None
                else None
            ),
            expected_source=(
                expected_universe.source if expected_universe is not None else "none"
            ),
        )
        return chain, self._consumption_report(manifest=manifest, transport=transport)

    def rebuild_chain_from_capture(self, **kwargs: Any) -> ChainSnapshot:
        """Just the chain, for callers that do not need the accounting."""
        chain, _ = self.rebuild_from_capture(**kwargs)
        return chain

    def _consumption_report(self, *, manifest: Any, transport: Any) -> Any:
        """Which records normalization consumed, against which it was given.

        Returned alongside the chain rather than stashed on the pipeline: the
        pipeline is frozen on purpose -- ``validate_integrity`` exists because
        v2.1.4 let a caller replace its derived reports -- so a rebuild cannot
        leave state behind for a later call to pick up.
        """
        from src.domain.normalization import RecordConsumptionReport

        consumed = list(getattr(transport, "consumed", ()))
        plan = self.capture_plan
        return RecordConsumptionReport(
            assigned_record_ids=tuple(sorted(r.record_id for r in manifest.records)),
            consumed_record_ids=tuple(entry.record_id for entry in consumed),
            serve_order=tuple(
                (entry.record_id, entry.endpoint, entry.serve_order)
                for entry in consumed
            ),
            declared_multiples=tuple(sorted(plan.declared_multiple_records)),
            assigned_endpoints=tuple(
                sorted((r.record_id, r.endpoint) for r in manifest.records)
            ),
        )

    def _replayed_spot(
        self, *, runtime: Any, recipe: Any, manifest: Any, store: Any
    ) -> tuple[float, datetime | None]:
        """The underlying, read back out of the captured index snapshot.

        Read from the stored bytes rather than taken from the chain being
        checked. Every gamma is computed against this number, so accepting the
        supplied chain's own spot here would make the comparison circular.
        """

        if recipe.spot_source != "vendor_index_snapshot":
            raise PipelineConsistencyError(
                f"a chain whose underlying is {recipe.spot_source!r} cannot be "
                "rebuilt from a capture: the number did not come from the "
                "captured bytes, so re-deriving it would prove nothing"
            )
        record = runtime.client.fetch_index_snapshot(
            symbol=runtime.default_chain_request.symbol, as_of=recipe.as_of
        )
        if record is None:
            raise PipelineConsistencyError(
                "the capture holds no index snapshot, so the underlying this "
                "chain was priced against cannot be re-derived"
            )
        return record.spot, record.timestamp

    def normalized_chain_receipt(
        self,
        chain: ChainSnapshot,
        *,
        manifest: Any,
        recipe: Any,
        operation: Any = None,
        consumption: Any = None,
        expected_universe: Any = None,
    ) -> Any:
        """Name a chain by the evidence, the rules and the operation."""
        from src.domain.normalization import (
            NormalizedChainReceipt,
            canonical_chain_hash,
        )

        return NormalizedChainReceipt(
            manifest_hash=manifest.manifest_hash,
            recipe_hash=recipe.recipe_hash,
            normalized_chain_hash=canonical_chain_hash(chain),
            contract_count=len(chain.quotes),
            parser_version=PARSER_VERSION,
            operation_fingerprint=(
                operation.operation_fingerprint if operation is not None else ""
            ),
            consumption_hash=(
                consumption.consumption_hash if consumption is not None else ""
            ),
            expected_universe_hash=(
                expected_universe.universe_hash
                if expected_universe is not None
                else None
            ),
        )

    # -- the two calculations ------------------------------------------------
    #
    # v2.1.4 had one, ``compute_gex``, and it called the engine. It ran with six
    # load-bearing pricing dimensions UNKNOWN, on a chain that had never been
    # through this pipeline, with no capture behind it -- and the number it
    # returned was indistinguishable from one computed under settled
    # assumptions. The compatibility report existed and nothing read it.

    def compute_diagnostic_gex(self, chain: ChainSnapshot) -> Any:
        """Compute under whatever is currently known, and mark it untrusted.

        Legitimate and useful: looking at the shape of a number is how you find
        out whether the assumptions are worth establishing. The result is
        stamped so that no later reader, and no downstream layer, can mistake it
        for one that was.
        """
        self.validate_integrity()
        _require_a_chain(chain, mode="diagnostic")
        snapshot = self._compute(chain)
        blockers = self.calculation_blockers(chain)
        return snapshot.with_meta(
            trusted=False,
            calculation_mode=CalculationMode.DIAGNOSTIC_UNTRUSTED.value,
            calculation_blockers=list(blockers),
            pipeline_fingerprint=self.fingerprint(),
            chain_fingerprint=_chain_fingerprint(chain),
        ).with_warnings(DIAGNOSTIC_WARNING_CODE)

    def compute_trusted_gex(
        self,
        chain: ChainSnapshot,
        *,
        manifest: Any,
        store: Any,
        validation_report: Any = None,
        open_interest_provenance: Any = None,
        open_interest_as_of_evidence: Any = None,
        expected_universe: Any = None,
    ) -> Any:
        """Compute only when this call has itself derived the authority to.

        Refuses rather than warns. A warning beside a number is read by whoever
        happens to look; a refusal is read by everybody.

        **Takes primitive evidence, not a verdict.** v2.1.6 took a
        ``VerifiedCalculationContext``, which was the right shape of object and
        the wrong kind of argument: it is a public frozen dataclass whose
        ``context_hash`` any caller can recompute, so

            forged = dataclasses.replace(
                context, effective_pricing_compatibility=PricingCompatibilityReport()
            )
            forged = dataclasses.replace(forged, context_hash=forged.recomputed_hash())

        produced an internally consistent context asserting that every pricing
        dimension was fine. A hash is an integrity checksum. It says the fields
        agree with the digest; it says nothing about who computed them.

        There is no cryptography here and none is wanted -- arbitrary Python can
        reach into anything. The requirement is narrower and achievable: **no
        public API accepts a derived verdict where it could derive one.** So
        this method takes the manifest, the store and the provenance claims, and
        runs the verification, the validation re-derivation, the compatibility
        derivation and the chain rebuild itself, in that order, before computing.

        **And no spot provenance.** v2.1.7 accepted a caller-built
        ``SpotProvenance`` carrying a timestamp and a tolerance, and checked the
        chain against *those* -- so a caller could claim 12:00 for a print the
        vendor stamped 11:00, set ``chain.as_of`` to match, and be trusted. Both
        are now derived: the timestamp from the verified index record, the
        tolerance from the pipeline configuration.

        The returned snapshot carries the ``VerifiedCalculationContext`` this
        call produced. It remains a serialisable *report* of what was checked.
        """
        from src.adapters.certification import build_verified_calculation_context

        self.validate_integrity()
        _require_a_chain(chain, mode="trusted")

        # Derived here, from the bytes, before anything else looks at it.
        spot_provenance = self.derive_spot_provenance(manifest=manifest, store=store)
        context = build_verified_calculation_context(
            pipeline=self,
            manifest=manifest,
            store=store,
            validation=validation_report,
            spot=spot_provenance,
            open_interest=open_interest_provenance,
            open_interest_as_of_evidence=open_interest_as_of_evidence,
        )
        receipt = self._rederivation_refusals(
            chain,
            manifest=manifest,
            store=store,
            context=context,
            expected_universe=expected_universe,
        )
        refusals = [*self._context_refusals(chain, context), *receipt]
        if refusals:
            raise PipelineConsistencyError(
                "the evidence does not authorize a trusted calculation for this "
                "chain: " + "; ".join(refusals)
            )
        # After the refusals, which report a mismatched universe or an
        # unresolvable operation as a reason rather than as a raw exception.
        operation = self.resolve_operation(
            manifest=manifest, store=store, expected_universe=expected_universe
        )
        blockers = self.calculation_blockers(
            chain, report=context.effective_pricing_compatibility
        )
        if blockers:
            raise PipelineConsistencyError(
                "a trusted GEX cannot be computed under unresolved assumptions: "
                + "; ".join(blockers)
                + ". compute_diagnostic_gex() will produce the number, marked "
                "untrusted."
            )
        snapshot = self._compute(chain)
        return snapshot.with_meta(
            trusted=True,
            calculation_mode=CalculationMode.TRUSTED.value,
            calculation_blockers=[],
            pipeline_fingerprint=self.fingerprint(),
            chain_fingerprint=_chain_fingerprint(chain),
            evidence_context=context.as_dict(),
            evidence_context_hash=context.context_hash,
            normalized_chain_receipt=self.normalized_chain_receipt(
                chain,
                manifest=manifest,
                recipe=self.normalization_recipe(
                    # The operation's instant, not the chain's. They are equal
                    # by the time this runs -- the refusal above checked -- and
                    # taking it from the operation says which one is the source.
                    as_of=operation.effective_valuation_timestamp,
                    open_interest_as_of=_open_interest_as_of(chain),
                    expected_universe_fingerprint=(
                        expected_universe.universe_hash
                        if expected_universe is not None
                        else None
                    ),
                ),
                operation=operation,
                consumption=self.rebuild_from_capture(
                    manifest=manifest,
                    store=store,
                    recipe=self.normalization_recipe(
                        as_of=operation.effective_valuation_timestamp,
                        open_interest_as_of=_open_interest_as_of(chain),
                    ),
                    operation=operation,
                    expected_universe=expected_universe,
                )[1],
                expected_universe=expected_universe,
            ).as_dict(),
            capture_operation=operation.as_dict(),
        )

    def derive_spot_provenance(self, *, manifest: Any, store: Any) -> Any:
        """The underlying, its clock and its tolerance -- all from evidence.

        Nothing here is a parameter. v2.1.7 took a ``SpotProvenance`` from the
        caller and compared the chain against it, which made the two load-bearing
        numbers -- the print's timestamp and the allowed skew -- assertions.
        Claiming 12:00 for a print the vendor stamped 11:00, and setting
        ``chain.as_of`` to match, produced a trusted result.
        """
        from src.adapters.certification import SpotProvenance, SpotSource
        from src.adapters.thetadata.endpoints import Endpoint
        from src.adapters.validation import AdapterValidator

        observation = AdapterValidator.observe_field(
            manifest=manifest,
            store=store,
            endpoint=Endpoint.INDEX_PRICE_SNAPSHOT,
            field_path="index_price",
        )
        return SpotProvenance(
            source=SpotSource.VENDOR_INDEX_SNAPSHOT,
            timestamp=observation.source_timestamp,
            # Configuration, so it is in the pipeline fingerprint and every
            # record stamped under it.
            tolerance_seconds=self.config.max_spot_skew_seconds,
            observation=observation,
        )

    def _rederivation_refusals(
        self,
        chain: ChainSnapshot,
        *,
        manifest: Any,
        store: Any,
        context: Any,
        expected_universe: Any = None,
    ) -> tuple[str, ...]:
        """Whether this chain is the one those raw records normalize to.

        The gap v2.1.6 left. Verification proved a great deal about the *bytes*
        and nothing about the ``ChainSnapshot`` -- so a chain fetched honestly,
        then edited, kept a verified manifest and a trusted verdict. Adding
        999,999 to one strike's open interest moved the unsigned total by about
        two orders of magnitude and the result still said ``trusted=True``.

        The answer is not a stricter check on the chain's metadata. It is to
        normalize the stored payloads again and compare the two chains field by
        field.
        """
        from src.domain.normalization import canonical_chain_hash

        if not context.capture_verification.verified:
            # Rebuilding from a capture that did not verify would compare a
            # chain against bytes nobody has vouched for. The capture failure is
            # reported by ``_context_refusals``; this is not a second one.
            return ()

        # The operation, resolved from the records and the verified index print.
        # **Not from the chain.** v2.1.7 built the recipe with
        # ``as_of=chain.as_of``, so the chain under test chose the instant it was
        # tested against and any shift shifted the rebuild with it.
        try:
            operation = self.resolve_operation(
                manifest=manifest, store=store, expected_universe=expected_universe
            )
        except PipelineConsistencyError as error:
            return (f"the capture operation could not be resolved: {error}",)

        valuation = operation.effective_valuation_timestamp
        if chain.as_of != valuation:
            return (
                f"the chain is stamped {chain.as_of.isoformat()} but the capture "
                f"operation priced against {valuation.isoformat()}, selected by "
                f"{operation.valuation_timestamp_rule.value}. Time to expiry is "
                "measured from that instant and drives every gamma, so the two "
                "cannot differ.",
            )

        recipe = self.normalization_recipe(
            as_of=valuation,
            open_interest_as_of=_open_interest_as_of(chain),
            expected_universe_fingerprint=(
                expected_universe.universe_hash if expected_universe else None
            ),
        )
        try:
            rederived, consumption = self.rebuild_from_capture(
                manifest=manifest,
                store=store,
                recipe=recipe,
                operation=operation,
                expected_universe=expected_universe,
            )
        except PipelineConsistencyError as error:
            return (f"the chain could not be re-derived from its capture: {error}",)
        except Exception as error:
            return (
                f"re-deriving the chain from its capture raised "
                f"{type(error).__name__}: {error}",
            )

        # Every record assigned to this operation, consumed exactly once. A
        # capture carrying a response nobody parsed is a capture claiming bytes
        # that produced nothing -- v2.1.7 replayed the first quote response and
        # never noticed the second.
        if not consumption.exact:
            return (
                f"the re-derivation did not consume the capture exactly: "
                f"{list(consumption.unconsumed)} were never parsed, "
                f"{list(consumption.repeated)} were parsed more than once, "
                f"{list(consumption.unassigned)} are not in the manifest, and "
                f"{list(consumption.undeclared_multiples)} answered more than "
                "once under a capture plan that declares no pagination, batched "
                "expirations, retained retries or partitions for them",
            )

        supplied, rebuilt = canonical_chain_hash(chain), canonical_chain_hash(rederived)
        if supplied == rebuilt:
            return ()
        return (
            f"the supplied chain hashes to {short_id(supplied)}... and the chain "
            f"re-derived from its raw records hashes to {short_id(rebuilt)}...; "
            f"{_first_field_difference(chain, rederived)}",
        )

    def _context_refusals(self, chain: ChainSnapshot, context: Any) -> tuple[str, ...]:
        """Every reason this context cannot authorize this chain.

        All of them reported, not just the first: a context can be for the wrong
        pipeline *and* for the wrong capture, and a caller fixing one at a time
        learns more from being told both.
        """
        refusals: list[str] = []

        # -- it describes this pipeline --------------------------------------
        if context.pipeline_fingerprint != self.fingerprint():
            refusals.append(
                f"the context was built for pipeline "
                f"{context.pipeline_fingerprint!r}; this one is "
                f"{self.fingerprint()!r}"
            )
        if context.capture_plan_fingerprint != self.capture_plan.fingerprint:
            refusals.append("the context was verified against a different capture plan")
        if context.parser_version != PARSER_VERSION:
            refusals.append(
                f"the context was built by parser {context.parser_version!r}; "
                f"this repository reads {PARSER_VERSION!r}"
            )

        # -- and this chain ---------------------------------------------------
        provenance_meta = _mapping(chain.meta.get("pipeline"))
        carried = provenance_meta.get("pipeline_fingerprint")
        if carried != self.fingerprint():
            refusals.append(
                f"the chain carries pipeline fingerprint {carried!r}; this "
                f"pipeline is {self.fingerprint()!r}"
            )
        chain_manifest = _mapping(chain.meta.get("raw_capture_manifest"))
        chain_hash = chain_manifest.get("manifest_hash")
        if chain_hash != context.manifest.manifest_hash:
            refusals.append(
                f"the context verified capture "
                f"{context.manifest.manifest_hash[:16]}..., but this chain was "
                f"built from {str(chain_hash)[:16]}...; a manifest mismatch "
                "means the verified bytes are not the bytes behind this number"
            )

        # -- the evidence itself holds ---------------------------------------
        if not context.capture_verification.verified:
            refusals.append(
                "the capture did not verify against its store: "
                f"{list(context.capture_verification.failures)}"
            )
        if context.failures:
            refusals.append(f"the context records {list(context.failures)}")

        # -- provenance applies to this chain --------------------------------
        refusals.extend(self._provenance_refusals(chain, context))
        return tuple(refusals)

    def _provenance_refusals(
        self, chain: ChainSnapshot, context: Any
    ) -> tuple[str, ...]:
        """Whether the spot and open-interest evidence is about *this* chain."""
        refusals: list[str] = []
        spot = context.spot_provenance
        if spot is None:
            refusals.append("the context carries no spot provenance")
        else:
            carried = _mapping(chain.meta.get("spot_provenance"))
            source = getattr(spot.source, "value", spot.source)
            if carried.get("source") != source:
                refusals.append(
                    f"the chain's spot is {carried.get('source')!r}; the context "
                    f"verified a {source!r} print"
                )
            skew = spot.skew_seconds(chain.as_of)
            if skew is None:
                refusals.append("the verified spot has no timestamp to compare")
            elif skew > spot.tolerance_seconds:
                refusals.append(
                    f"the verified spot is {skew:.3f}s from the chain instant, "
                    f"outside the {spot.tolerance_seconds}s tolerance"
                )
            # The number itself, not only its provenance. Every gamma in the
            # snapshot is computed against ``chain.spot``, and the verified
            # evidence says what the vendor's index print actually was -- so a
            # chain carrying a different underlying was not built from these
            # bytes, whatever its metadata claims.
            observed = getattr(spot.observation, "observed_value", None)
            if observed is None:
                refusals.append(
                    "the verified spot names no stored value, so nothing "
                    "connects it to the underlying this chain used"
                )
            elif not _same_price(chain.spot, observed):
                refusals.append(
                    f"the chain was computed against spot {chain.spot!r}, but "
                    f"the verified index print is {observed!r}"
                )

        open_interest = context.open_interest_provenance
        if open_interest is None:
            refusals.append("the context carries no open-interest provenance")
        elif open_interest.chain_date is not None and (
            open_interest.chain_date != chain.as_of.date()
        ):
            refusals.append(
                f"the open-interest evidence is for session "
                f"{open_interest.chain_date.isoformat()}, not "
                f"{chain.as_of.date().isoformat()}"
            )
        return tuple(refusals)

    def calculation_blockers(
        self, chain: ChainSnapshot, *, report: Any = None
    ) -> tuple[str, ...]:
        """Everything standing between this chain and a trusted number.

        ``report`` is the *effective* pricing compatibility -- the static
        assessment revised by what a verified capture observed. It defaults to
        the static one so a diagnostic calculation, which has no capture behind
        it, still gets an answer.
        """
        report = report if report is not None else self.pricing_compatibility
        blockers: list[str] = []

        if report.hard_failures:
            blockers.append(
                f"the pricing assessment reports hard failures "
                f"{list(report.hard_failures)}"
            )
        if report.load_bearing_mismatches:
            blockers.append(
                "load-bearing pricing dimensions disagree: "
                f"{[d.value for d in report.load_bearing_mismatches]}"
            )
        if report.load_bearing_unknowns:
            blockers.append(
                "load-bearing pricing dimensions are UNKNOWN: "
                f"{[d.value for d in report.load_bearing_unknowns]}"
            )

        provenance_meta = _mapping(chain.meta.get("pipeline"))
        carried = provenance_meta.get("pipeline_fingerprint")
        if carried != self.fingerprint():
            blockers.append(
                f"the chain carries pipeline fingerprint {carried!r}; this "
                f"pipeline is {self.fingerprint()!r}"
            )

        manifest = _mapping(chain.meta.get("raw_capture_manifest"))
        if not manifest.get("capture_enabled") or not manifest.get("record_ids"):
            blockers.append(
                "the chain carries no verified raw-capture manifest, so the "
                "bytes behind the number cannot be produced"
            )

        engine_fingerprint = getattr(self.engine_config, "fingerprint", None)
        if callable(engine_fingerprint):
            carried_engine = provenance_meta.get("engine_fingerprint")
            if carried_engine is not None and carried_engine != engine_fingerprint():
                blockers.append("the chain was fetched under different engine settings")

        provenance = _mapping(chain.meta.get("spot_provenance"))
        if provenance.get("source") != "vendor_index_snapshot":
            blockers.append(
                f"the spot is {provenance.get('source') or 'unattributed'!r}, "
                "not a vendor index observation"
            )
        elif provenance.get("observation") is None:
            blockers.append("the spot was not read back out of a stored payload")

        if chain.meta.get("open_interest_as_of") is None and not any(
            q.open_interest for q in chain.quotes
        ):
            blockers.append("the chain carries no open interest")

        plan = self.capture_plan
        captured = set(_mapping(manifest.get("endpoint_records")))
        missing = sorted(
            endpoint.value
            for endpoint in plan.required_endpoints
            if endpoint.value not in captured
        )
        if missing:
            blockers.append(f"the capture is missing required endpoints {missing}")

        return tuple(blockers)

    def validate_integrity(self) -> None:
        """Recompute every derived report and refuse if one has been replaced.

        ``dataclasses.replace(pipeline, pricing_compatibility=<permissive>)``
        produced a pipeline whose reports no longer followed from its inputs,
        and v2.1.4 read those reports without ever recomputing them. Called
        before every fetch, every calculation and every readiness assessment.
        """
        from src.adapters.thetadata.capabilities import assess_tier
        from src.adapters.thetadata.endpoints import Tier

        expected = assess_pricing_compatibility(self.config, self.model_spec)
        if expected.semantic_payload() != self.pricing_compatibility.semantic_payload():
            raise PipelineConsistencyError(
                "the pricing compatibility report does not follow from this "
                "pipeline's configuration and model. It has been replaced, and "
                "a report that was not derived from the inputs it describes "
                "says nothing about them."
            )

        capability = assess_tier(
            Tier(self.config.tier),
            required_capabilities(self.pricing_mode, policy=self.vendor_gamma_policy),
        )
        carried = self.subscription_capability
        if carried is None or carried.as_dict() != capability.as_dict():
            raise PipelineConsistencyError(
                "the subscription capability report does not follow from this "
                "pipeline's tier and mode; it has been replaced"
            )

        if getattr(self.engine_config, "model_spec", None) is not None and (
            self.engine_config.model_spec.fingerprint() != self.model_spec.fingerprint()
        ):
            raise PipelineConsistencyError(
                "the engine config carries a different model spec than the "
                "pipeline: engine "
                f"{self.engine_config.model_spec.fingerprint()!r} vs pipeline "
                f"{self.model_spec.fingerprint()!r}"
            )

    def _compute(self, chain: ChainSnapshot) -> Any:
        from src.gex.engine import compute_gex_snapshot

        return compute_gex_snapshot(chain, self.engine_config)

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
                # Content, not citations. A vendor documentation page that
                # resolves a load-bearing pricing dimension is an input to every
                # number this pipeline produces, and rewriting it has to move
                # this digest. Redundant with ``config.as_dict()`` since the
                # hashes travel on the observations -- named separately because
                # a requirement nobody can find is one that gets refactored away.
                "documentation_evidence": self.documentation_evidence_fingerprints,
                "spot_synchronization_policy": self.spot_synchronization_policy,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "pipeline_fingerprint": self.fingerprint(),
            "engine_fingerprint": (
                self.engine_config.fingerprint()
                if hasattr(self.engine_config, "fingerprint")
                else None
            ),
            "capture_plan_fingerprint": self.capture_plan.fingerprint,
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
    # Observed vendor values, *compared* against the local model's. v2.1.4
    # granted MATCHED for the observation existing, so recording that the vendor
    # uses ACT/360 against a local ACT/365F settled the dimension as agreement.
    # None of them can overturn a MISMATCHED dimension, which stays a hard
    # failure.
    return apply_attestations(report, config.pricing_attestations, spec)


def load_bearing_unknowns(report: PricingCompatibilityReport) -> tuple[str, ...]:
    """Dimensions that block a trusted calculation.

    Reads the typed results. v2.1.3 searched prose for substrings, so rewording
    a message could turn a blocker into a warning.
    """
    return tuple(d.value for d in report.load_bearing_unknowns)
