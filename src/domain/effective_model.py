"""The one authoritative resolution of model inputs.

Before this module existed, `formulas.py`, `engine.py`, `zero_gamma.py` and the
gamma comparison each resolved rates, dividends, spot and time-to-expiry for
themselves. Four code paths, four chances to disagree — and two of them used

    spec.risk_free_rate or snapshot.risk_free_rate

which is wrong for `0.0`, because zero is falsy. An explicitly configured zero
rate silently became the snapshot's rate, while the fingerprint recorded the
zero. The number and its audit record disagreed.

The rule now: **every pricing, gamma, GEX, zero-gamma and gamma-comparison
calculation consumes an `EffectiveModelInputs` produced here.** There is no
other fallback logic anywhere.

Resolution follows the configured *source enum*, never "first non-None value".
That distinction is the whole point: a `CONFIGURED_CONSTANT` rate of zero is a
decision, and it must not be overridden just because the number happens to be
falsy.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from src.domain.iv import IVSource
from src.domain.model_spec import (
    DayCountConvention,
    DividendSource,
    ExpirationTimestampRule,
    ModelSpec,
    PricingModel,
    RateSource,
    UnderlyingPriceSource,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.domain.contracts import ChainSnapshot, OptionQuote


class ModelResolutionError(RuntimeError):
    """Raised when an unusable resolution is asked to price something."""


class ResolutionIssue(str, Enum):
    """Why a contract cannot be priced under the configured model.

    Machine-readable and stable: these codes appear in zero-gamma exclusion
    accounting and in the validation report, so renaming one breaks stored audit
    trails.
    """

    UNDERLYING_MISSING = "underlying_missing"
    UNDERLYING_NOT_FINITE = "underlying_not_finite"
    UNDERLYING_NON_POSITIVE = "underlying_non_positive"
    UNDERLYING_SOURCE_UNSUPPORTED = "underlying_source_unsupported"
    RATE_MISSING = "rate_missing"
    RATE_NOT_FINITE = "rate_not_finite"
    RATE_SOURCE_UNSUPPORTED = "rate_source_unsupported"
    DIVIDEND_MISSING = "dividend_missing"
    DIVIDEND_NOT_FINITE = "dividend_not_finite"
    DIVIDEND_SOURCE_UNSUPPORTED = "dividend_source_unsupported"
    IV_MISSING = "iv_missing"
    IV_NOT_FINITE = "iv_not_finite"
    IV_NON_POSITIVE = "iv_non_positive"
    EXPIRED = "expired"
    EXPIRATION_RULE_UNSUPPORTED = "expiration_rule_unsupported"
    STRIKE_INVALID = "strike_invalid"


def _finite(value: float | None) -> bool:
    return (
        value is not None
        and isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


@dataclass(frozen=True, slots=True)
class EffectiveModelInputs:
    """Everything the pricer needs, plus where each value came from.

    Both halves matter. The values are what the maths uses; the sources are what
    makes a later disagreement investigable rather than mysterious.
    """

    # --- Resolved values ---
    spot: float
    strike: float
    right: str
    risk_free_rate: float
    dividend_yield: float
    expiration_timestamp: datetime
    time_to_expiry_years: float
    implied_volatility: float
    multiplier: float

    # --- Provenance ---
    implied_volatility_source: IVSource
    underlying_price_source: UnderlyingPriceSource
    risk_free_rate_source: RateSource
    dividend_yield_source: DividendSource
    expiration_rule: ExpirationTimestampRule
    minimum_time_to_expiry_minutes: float
    day_count_convention: DayCountConvention
    pricing_model: PricingModel
    model_version: str

    # --- Resolution outcome ---
    issues: tuple[ResolutionIssue, ...] = ()
    missing_inputs: tuple[str, ...] = ()

    @property
    def is_usable(self) -> bool:
        """False when the contract cannot be priced under this configuration."""
        return not self.issues

    @property
    def is_fully_specified(self) -> bool:
        """Provenance-based completeness.

        An explicit zero is complete: `ZERO` rate resolving to 0.0 is a fully
        specified decision. What is incomplete is a source that promised a value
        and did not supply one.
        """
        return not self.missing_inputs

    def _require_usable(self) -> None:
        if self.issues:
            raise ModelResolutionError(
                "cannot price with unresolved inputs: "
                f"{[issue.value for issue in self.issues]}"
            )

    def black_scholes_inputs(self) -> Any:
        from src.gex.pricing import BlackScholesInputs

        self._require_usable()
        return BlackScholesInputs(
            spot=self.spot,
            strike=self.strike,
            time_to_expiry=self.time_to_expiry_years,
            implied_vol=self.implied_volatility,
            rate=self.risk_free_rate,
            dividend_yield=self.dividend_yield,
        )

    def gamma(self) -> float:
        """The single gamma entry point. Nothing else calls the pricer directly."""
        from src.gex.pricing import gamma as bs_gamma

        return bs_gamma(self.black_scholes_inputs())

    def reprice_at(
        self, spot: float, *, implied_volatility: float | None = None
    ) -> EffectiveModelInputs:
        """A copy at a hypothetical spot, and optionally a different IV.

        Used by the zero-gamma grid. Only spot and IV may move: holding rate,
        dividend, time and conventions fixed is what makes the grid a repricing
        of *this* model rather than a different one.
        """
        changes: dict[str, Any] = {"spot": spot}
        if implied_volatility is not None:
            changes["implied_volatility"] = implied_volatility
        return replace(self, **changes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "spot": self.spot,
            "strike": self.strike,
            "right": self.right,
            "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "expiration_timestamp": self.expiration_timestamp.isoformat(),
            "time_to_expiry_years": self.time_to_expiry_years,
            "implied_volatility": self.implied_volatility,
            "multiplier": self.multiplier,
            "implied_volatility_source": self.implied_volatility_source.value,
            "underlying_price_source": self.underlying_price_source.value,
            "risk_free_rate_source": self.risk_free_rate_source.value,
            "dividend_yield_source": self.dividend_yield_source.value,
            "expiration_rule": self.expiration_rule.value,
            "minimum_time_to_expiry_minutes": self.minimum_time_to_expiry_minutes,
            "day_count_convention": self.day_count_convention.value,
            "pricing_model": self.pricing_model.value,
            "model_version": self.model_version,
            "issues": [issue.value for issue in self.issues],
            "missing_inputs": list(self.missing_inputs),
        }

    def fingerprint(self) -> str:
        """Digest of the effective model, values and provenance together.

        Provenance is included deliberately: a rate of 0.0 from `ZERO` and a rate
        of 0.0 from `CONFIGURED_CONSTANT` price identically but are different
        decisions, and an audit trail that cannot tell them apart is weaker than
        one that can.
        """
        payload = self.as_dict()
        # Contract identity varies per row; the fingerprint describes the MODEL.
        for key in (
            "spot",
            "strike",
            "right",
            "multiplier",
            "expiration_timestamp",
            "time_to_expiry_years",
            "implied_volatility",
        ):
            payload.pop(key, None)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def describe(self) -> str:
        return (
            f"{self.pricing_model.value} / {self.day_count_convention.value} / "
            f"rate={self.risk_free_rate:g}({self.risk_free_rate_source.value}) / "
            f"div={self.dividend_yield:g}({self.dividend_yield_source.value}) / "
            f"spot={self.underlying_price_source.value} / "
            f"expiry={self.expiration_rule.value} / "
            f"floor={self.minimum_time_to_expiry_minutes:g}min"
        )


# --- Component resolvers ----------------------------------------------------
#
# Each returns (value, issues, missing). Split out so every source enum has one
# obvious place where it is honoured, and a new enum member that nobody wired up
# fails loudly instead of being ignored.


def _resolve_underlying(
    *, quote: OptionQuote, snapshot: ChainSnapshot, spec: ModelSpec
) -> tuple[float, list[ResolutionIssue], list[str]]:
    source = spec.underlying_price_source
    issues: list[ResolutionIssue] = []
    missing: list[str] = []

    if source in (
        UnderlyingPriceSource.VENDOR_INDEX_SNAPSHOT,
        UnderlyingPriceSource.SYNTHETIC,
    ):
        return snapshot.spot, issues, missing

    if source is UnderlyingPriceSource.VENDOR_PER_CONTRACT:
        value = quote.underlying_price
        if value is None:
            # Deliberately NOT falling back to the snapshot spot. A silent
            # fallback would hide that the vendor sent nothing for this contract,
            # which is exactly what the source was selected to detect.
            issues.append(ResolutionIssue.UNDERLYING_MISSING)
            missing.append("underlying_price (vendor_per_contract)")
            return snapshot.spot, issues, missing
        if not _finite(value):
            issues.append(ResolutionIssue.UNDERLYING_NOT_FINITE)
            return snapshot.spot, issues, missing
        if value <= 0.0:
            issues.append(ResolutionIssue.UNDERLYING_NON_POSITIVE)
            return snapshot.spot, issues, missing
        return value, issues, missing

    if source is UnderlyingPriceSource.CONFIGURED_CONSTANT:
        value = spec.configured_underlying_price
        if value is None:
            issues.append(ResolutionIssue.UNDERLYING_MISSING)
            missing.append("configured_underlying_price")
            return snapshot.spot, issues, missing
        if not _finite(value) or value <= 0.0:
            issues.append(ResolutionIssue.UNDERLYING_NOT_FINITE)
            return snapshot.spot, issues, missing
        return value, issues, missing

    issues.append(ResolutionIssue.UNDERLYING_SOURCE_UNSUPPORTED)
    return snapshot.spot, issues, missing


def _resolve_rate(
    *, snapshot: ChainSnapshot, spec: ModelSpec
) -> tuple[float, list[ResolutionIssue], list[str]]:
    source = spec.risk_free_rate_source
    issues: list[ResolutionIssue] = []
    missing: list[str] = []

    if source is RateSource.ZERO:
        # An explicit decision, complete by construction.
        return 0.0, issues, missing

    if source is RateSource.CONFIGURED_CONSTANT:
        value = spec.risk_free_rate
        if value is None:
            issues.append(ResolutionIssue.RATE_MISSING)
            missing.append("risk_free_rate (configured_constant)")
            return 0.0, issues, missing
        if not _finite(value):
            issues.append(ResolutionIssue.RATE_NOT_FINITE)
            return 0.0, issues, missing
        return float(value), issues, missing

    if source is RateSource.SNAPSHOT:
        value = snapshot.risk_free_rate
        if value is None:
            issues.append(ResolutionIssue.RATE_MISSING)
            missing.append("snapshot.risk_free_rate")
            return 0.0, issues, missing
        if not _finite(value):
            issues.append(ResolutionIssue.RATE_NOT_FINITE)
            return 0.0, issues, missing
        return float(value), issues, missing

    # VENDOR_SOFR / VENDOR_TREASURY: no vendor rate feed is wired up, so these
    # are declared-but-unavailable rather than silently defaulted.
    issues.append(ResolutionIssue.RATE_SOURCE_UNSUPPORTED)
    missing.append(f"risk_free_rate ({source.value} has no data source)")
    return 0.0, issues, missing


def _resolve_dividend(
    *, snapshot: ChainSnapshot, spec: ModelSpec
) -> tuple[float, list[ResolutionIssue], list[str]]:
    source = spec.dividend_yield_source
    issues: list[ResolutionIssue] = []
    missing: list[str] = []

    if source is DividendSource.ZERO:
        return 0.0, issues, missing

    if source is DividendSource.CONFIGURED_CONSTANT:
        value = spec.dividend_yield
        if value is None:
            issues.append(ResolutionIssue.DIVIDEND_MISSING)
            missing.append("dividend_yield (configured_constant)")
            return 0.0, issues, missing
        if not _finite(value):
            issues.append(ResolutionIssue.DIVIDEND_NOT_FINITE)
            return 0.0, issues, missing
        return float(value), issues, missing

    if source is DividendSource.SNAPSHOT:
        value = snapshot.dividend_yield
        if value is None:
            issues.append(ResolutionIssue.DIVIDEND_MISSING)
            missing.append("snapshot.dividend_yield")
            return 0.0, issues, missing
        if not _finite(value):
            issues.append(ResolutionIssue.DIVIDEND_NOT_FINITE)
            return 0.0, issues, missing
        return float(value), issues, missing

    issues.append(ResolutionIssue.DIVIDEND_SOURCE_UNSUPPORTED)
    missing.append(f"dividend_yield ({source.value} has no data source)")
    return 0.0, issues, missing


def _resolve_iv(
    *, quote: OptionQuote
) -> tuple[float, IVSource, list[ResolutionIssue], list[str]]:
    issues: list[ResolutionIssue] = []
    missing: list[str] = []
    iv = quote.iv
    value = quote.effective_iv
    if value is None:
        issues.append(ResolutionIssue.IV_MISSING)
        missing.append("implied_volatility")
        return 0.0, iv.source, issues, missing
    if not _finite(value):
        issues.append(ResolutionIssue.IV_NOT_FINITE)
        return 0.0, iv.source, issues, missing
    if value <= 0.0:
        issues.append(ResolutionIssue.IV_NON_POSITIVE)
        return 0.0, iv.source, issues, missing
    return float(value), iv.source, issues, missing


def resolve_effective_inputs(
    *,
    quote: OptionQuote,
    snapshot: ChainSnapshot,
    spec: ModelSpec,
) -> EffectiveModelInputs:
    """Resolve one contract's complete effective model.

    Never raises for bad data: an unusable contract comes back with `issues`
    populated so the caller can count and classify the exclusion. Only *pricing*
    an unusable resolution raises.
    """
    from src.gex.sessions import expiration_timestamp, seconds_to_expiry_at

    issues: list[ResolutionIssue] = []
    missing: list[str] = []

    spot, spot_issues, spot_missing = _resolve_underlying(
        quote=quote, snapshot=snapshot, spec=spec
    )
    issues += spot_issues
    missing += spot_missing

    rate, rate_issues, rate_missing = _resolve_rate(snapshot=snapshot, spec=spec)
    issues += rate_issues
    missing += rate_missing

    dividend, div_issues, div_missing = _resolve_dividend(snapshot=snapshot, spec=spec)
    issues += div_issues
    missing += div_missing

    iv, iv_source, iv_issues, iv_missing = _resolve_iv(quote=quote)
    issues += iv_issues
    missing += iv_missing

    contract = quote.contract
    if not _finite(contract.strike) or contract.strike <= 0.0:
        issues.append(ResolutionIssue.STRIKE_INVALID)

    try:
        expiry_ts = expiration_timestamp(
            root=contract.root,
            expiry=contract.expiry,
            rule=spec.expiration_timestamp_rule,
        )
    except ValueError:
        issues.append(ResolutionIssue.EXPIRATION_RULE_UNSUPPORTED)
        expiry_ts = snapshot.as_of

    remaining = seconds_to_expiry_at(snapshot.as_of, expiry_ts)
    if remaining <= 0.0:
        issues.append(ResolutionIssue.EXPIRED)

    return EffectiveModelInputs(
        spot=spot,
        strike=contract.strike,
        right=contract.right.value,
        risk_free_rate=rate,
        dividend_yield=dividend,
        expiration_timestamp=expiry_ts,
        time_to_expiry_years=spec.year_fraction(max(remaining, 0.0)),
        implied_volatility=iv,
        multiplier=contract.multiplier,
        implied_volatility_source=iv_source,
        underlying_price_source=spec.underlying_price_source,
        risk_free_rate_source=spec.risk_free_rate_source,
        dividend_yield_source=spec.dividend_yield_source,
        expiration_rule=spec.expiration_timestamp_rule,
        minimum_time_to_expiry_minutes=spec.minimum_time_to_expiry_minutes,
        day_count_convention=spec.day_count_convention,
        pricing_model=spec.pricing_model,
        model_version=spec.model_version,
        issues=tuple(issues),
        missing_inputs=tuple(missing),
    )
