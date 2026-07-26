"""The complete set of pricing assumptions, carried with every result.

Motivation: a gamma number is only interpretable together with the conventions
that produced it. Two engines can implement Black-Scholes perfectly and still
disagree by 20% on a 0DTE gamma because one floors time-to-expiry at 30 minutes
and the other at 60, or because one uses ACT/365 and the other ACT/252. Without
the conventions travelling alongside the number, that disagreement is
uninvestigable.

So :class:`ModelSpec` is embedded in every ``GexSnapshot`` and contributes to the
snapshot fingerprint. Change an assumption and the fingerprint changes, which is
what makes the replay test meaningful.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from src.domain.iv import IVSource

# Bumped whenever a change alters numeric output for identical inputs. The replay
# test pins snapshot hashes, so this is the deliberate way to invalidate them.
MODEL_VERSION = "gex-engine/2.0.0"

SECONDS_PER_DAY = 86_400.0


class PricingModel(str, Enum):
    BLACK_SCHOLES_MERTON = "black_scholes_merton"


class DayCountConvention(str, Enum):
    """How seconds become year fractions.

    SPX index options are quoted against calendar time, so ACT/365F is the
    default. ACT/252 is offered because some vendor and academic pipelines use
    trading-day counting, and a comparison against one of those is meaningless
    unless the convention can be matched.
    """

    ACT_365_FIXED = "ACT/365F"
    ACT_360 = "ACT/360"
    ACT_252 = "ACT/252"

    @property
    def days_per_year(self) -> float:
        return {
            DayCountConvention.ACT_365_FIXED: 365.0,
            DayCountConvention.ACT_360: 360.0,
            DayCountConvention.ACT_252: 252.0,
        }[self]

    @property
    def seconds_per_year(self) -> float:
        return self.days_per_year * SECONDS_PER_DAY


class RateSource(str, Enum):
    CONFIGURED_CONSTANT = "configured_constant"
    VENDOR_SOFR = "vendor_sofr"
    VENDOR_TREASURY = "vendor_treasury"
    ZERO = "zero"


class DividendSource(str, Enum):
    CONFIGURED_CONSTANT = "configured_constant"
    VENDOR_ANNUAL_DIVIDEND = "vendor_annual_dividend"
    ZERO = "zero"


class ExpirationTimestampRule(str, Enum):
    """When a series stops accruing gamma.

    ``ROOT_SPECIFIC_SETTLEMENT`` is the correct rule and the default: SPXW is
    PM-settled at 16:00 ET, SPX standard is AM-settled at the 09:30 ET open. The
    alternatives exist to quantify how much that distinction is worth, not
    because they are defensible.
    """

    ROOT_SPECIFIC_SETTLEMENT = "root_specific_settlement"
    ROOT_SPECIFIC_SETTLEMENT_WITH_EARLY_CLOSE = (
        "root_specific_settlement_with_early_close"
    )
    FIXED_1600_ET = "fixed_1600_et"
    CALENDAR_MIDNIGHT = "calendar_midnight"


class UnderlyingPriceSource(str, Enum):
    VENDOR_INDEX_SNAPSHOT = "vendor_index_snapshot"
    VENDOR_PER_CONTRACT = "vendor_per_contract"
    CONFIGURED_CONSTANT = "configured_constant"
    SYNTHETIC = "synthetic"


# Minimum-time-to-expiry floors under evaluation. Gamma diverges as T -> 0 for an
# at-the-money option; the floor decides how much of that divergence reaches the
# aggregate. There is no obviously correct value, so the engine reports
# sensitivity across all three rather than asserting one.
#
# NOTE: ThetaData's documented short-dated handling has NOT been verified against
# a live response. Do not claim compatibility. See docs/OPEN_DECISIONS.md.
FLOOR_EPSILON_MINUTES = 1.0 / 60.0  # one second: numerical guard only
FLOOR_30_MINUTES = 30.0
FLOOR_60_MINUTES = 60.0
SENSITIVITY_FLOORS_MINUTES: tuple[float, ...] = (
    FLOOR_EPSILON_MINUTES,
    FLOOR_30_MINUTES,
    FLOOR_60_MINUTES,
)


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Every assumption that changes a number, in one hashable object."""

    pricing_model: PricingModel = PricingModel.BLACK_SCHOLES_MERTON
    day_count_convention: DayCountConvention = DayCountConvention.ACT_365_FIXED
    risk_free_rate_source: RateSource = RateSource.CONFIGURED_CONSTANT
    dividend_yield_source: DividendSource = DividendSource.CONFIGURED_CONSTANT
    expiration_timestamp_rule: ExpirationTimestampRule = (
        ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT
    )
    minimum_time_to_expiry_minutes: float = FLOOR_60_MINUTES
    underlying_price_source: UnderlyingPriceSource = (
        UnderlyingPriceSource.VENDOR_INDEX_SNAPSHOT
    )
    iv_price_source: IVSource = IVSource.VENDOR_DEFAULT_IV
    # Effective numeric values actually used, not just their provenance.
    risk_free_rate: float = 0.0
    dividend_yield: float = 0.0
    model_version: str = MODEL_VERSION

    @property
    def minimum_time_to_expiry_years(self) -> float:
        return (
            self.minimum_time_to_expiry_minutes
            * 60.0
            / self.day_count_convention.seconds_per_year
        )

    def year_fraction(self, seconds_to_expiry: float) -> float:
        """Seconds to a floored year fraction under this spec's conventions."""
        raw = seconds_to_expiry / self.day_count_convention.seconds_per_year
        return max(raw, self.minimum_time_to_expiry_years)

    def with_floor_minutes(self, minutes: float) -> ModelSpec:
        return replace(self, minimum_time_to_expiry_minutes=minutes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pricing_model": self.pricing_model.value,
            "day_count_convention": self.day_count_convention.value,
            "risk_free_rate_source": self.risk_free_rate_source.value,
            "dividend_yield_source": self.dividend_yield_source.value,
            "expiration_timestamp_rule": self.expiration_timestamp_rule.value,
            "minimum_time_to_expiry_minutes": self.minimum_time_to_expiry_minutes,
            "underlying_price_source": self.underlying_price_source.value,
            "iv_price_source": self.iv_price_source.value,
            "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "model_version": self.model_version,
        }

    def fingerprint(self) -> str:
        """Stable 16-hex-char digest of the assumption set.

        Sorted-key JSON so the digest depends on values, not on field order or
        dict iteration. Truncated for readability -- collision resistance is not
        a security property here, it is a "did the assumptions change" check.
        """
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def describe(self) -> str:
        return (
            f"{self.pricing_model.value} / {self.day_count_convention.value} / "
            f"floor={self.minimum_time_to_expiry_minutes:g}min / "
            f"iv={self.iv_price_source.value} / {self.fingerprint()}"
        )


@dataclass(frozen=True, slots=True)
class FloorSensitivityEntry:
    floor_minutes: float
    total_unsigned_gex: float
    total_signed_gex: float
    zero_gamma_spot: float | None
    dte0_unsigned_gex: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "floor_minutes": self.floor_minutes,
            "total_unsigned_gex": self.total_unsigned_gex,
            "total_signed_gex": self.total_signed_gex,
            "zero_gamma_spot": self.zero_gamma_spot,
            "dte0_unsigned_gex": self.dte0_unsigned_gex,
        }


@dataclass(frozen=True, slots=True)
class FloorSensitivityReport:
    """How much of the answer is the time-floor rather than the market.

    If the 0DTE bucket weight swings wildly across these floors, the floor is
    doing the modelling and the aggregate should not be leaned on. Reported, not
    resolved -- picking a floor is a research decision, recorded in
    docs/OPEN_DECISIONS.md.
    """

    baseline_floor_minutes: float
    entries: tuple[FloorSensitivityEntry, ...]

    @property
    def unsigned_gex_range_pct(self) -> float | None:
        values = [entry.total_unsigned_gex for entry in self.entries]
        if not values or max(values) <= 0.0:
            return None
        return (max(values) - min(values)) / max(values) * 100.0

    @property
    def dte0_range_pct(self) -> float | None:
        values = [entry.dte0_unsigned_gex for entry in self.entries]
        if not values or max(values) <= 0.0:
            return None
        return (max(values) - min(values)) / max(values) * 100.0

    @property
    def zero_gamma_range_pct(self) -> float | None:
        levels = [
            entry.zero_gamma_spot
            for entry in self.entries
            if entry.zero_gamma_spot is not None
        ]
        if len(levels) < 2:
            return None
        return (max(levels) - min(levels)) / max(levels) * 100.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline_floor_minutes": self.baseline_floor_minutes,
            "entries": [entry.as_dict() for entry in self.entries],
            "unsigned_gex_range_pct": self.unsigned_gex_range_pct,
            "dte0_range_pct": self.dte0_range_pct,
            "zero_gamma_range_pct": self.zero_gamma_range_pct,
        }
