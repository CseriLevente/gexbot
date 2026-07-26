"""GEX engine output types.

The plan mandates five separate GEX views written into one ``gex_snapshot``
object, precisely so that no downstream consumer can mistake a single number for
the truth. These dataclasses enforce that shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ExpiryBucket(str, Enum):
    """Fixed DTE buckets. The names are part of the contract with the feature
    store and the audit log -- do not renumber or rename them.
    """

    DTE_0 = "0DTE"
    DTE_1_2 = "1_2_DTE"
    DTE_3_5 = "3_5_DTE"
    DTE_6_30 = "6_30_DTE"
    DTE_GT_30 = "GT_30_DTE"


class SignConvention(str, Enum):
    """How the naive signed GEX assigns dealer direction.

    This is a *proxy*, never dealer inventory truth. Making it an explicit enum
    forces every stored snapshot to record which assumption produced its sign.

    ``DEALER_LONG_CALLS_SHORT_PUTS`` is the classic public convention: customers
    are assumed net buyers of puts and net sellers of calls, so dealers end up
    long call gamma and short put gamma.
    """

    DEALER_LONG_CALLS_SHORT_PUTS = "dealer_long_calls_short_puts"
    DEALER_SHORT_CALLS_LONG_PUTS = "dealer_short_calls_long_puts"
    FLOW_ADJUSTED = "flow_adjusted"


class IVConvention(str, Enum):
    """Volatility convention used when repricing on the zero-gamma spot grid.

    ``FROZEN_IV`` keeps each contract's raw snapshot IV. ``STICKY_STRIKE`` keeps
    IV pinned to the strike but reads it off a smoothed per-expiry smile, so it
    is the denoised sibling of ``FROZEN_IV`` rather than a duplicate of it.
    ``STICKY_DELTA`` translates the smile with spot (IV follows log-moneyness).
    ``SURFACE_REFIT`` is a later-phase hook.
    """

    FROZEN_IV = "frozen_iv"
    STICKY_STRIKE = "sticky_strike"
    STICKY_DELTA = "sticky_delta"
    SURFACE_REFIT = "surface_refit"


@dataclass(frozen=True, slots=True)
class StrikeGex:
    """Per-strike aggregation across every expiry and both rights."""

    strike: float
    call_gex: float
    put_gex: float
    unsigned_gex: float
    signed_gex: float
    call_open_interest: int
    put_open_interest: int

    @property
    def total_open_interest(self) -> int:
        return self.call_open_interest + self.put_open_interest


@dataclass(frozen=True, slots=True)
class BucketGex:
    bucket: ExpiryBucket
    unsigned_gex: float
    signed_gex: float
    contract_count: int
    open_interest: int


@dataclass(frozen=True, slots=True)
class GammaVoid:
    """A contiguous strike range with unusually little gamma.

    Price tends to travel quickly through these because there is little dealer
    hedging flow to absorb it -- they are targets, not support.
    """

    low_strike: float
    high_strike: float
    max_unsigned_gex_in_range: float

    @property
    def width(self) -> float:
        return self.high_strike - self.low_strike


@dataclass(frozen=True, slots=True)
class WallSet:
    """Structural levels derived from strike-aggregated gamma -- never from raw
    open interest, which is what the plan explicitly rules out.
    """

    call_wall: float | None
    put_wall: float | None
    largest_abs_gamma_strike: float | None
    positive_gamma_nodes: tuple[float, ...] = ()
    negative_gamma_nodes: tuple[float, ...] = ()
    gamma_voids: tuple[GammaVoid, ...] = ()


@dataclass(frozen=True, slots=True)
class ZeroGammaResult:
    """Outcome of one zero-gamma grid search under one IV convention."""

    convention: IVConvention
    zero_gamma_spot: float | None
    grid_low: float
    grid_high: float
    grid_points: int
    # Signed total GEX at each grid point, for plotting and for proving the
    # root-finder picked the crossing a human would pick.
    curve: tuple[tuple[float, float], ...] = ()
    sign_changes: int = 0
    # Set when the curve never crosses zero inside the grid.
    no_crossing: bool = False

    @property
    def resolved(self) -> bool:
        return self.zero_gamma_spot is not None


@dataclass(frozen=True, slots=True)
class ConfidenceComponent:
    name: str
    score: float  # 0.0 - 1.0
    weight: float
    detail: str = ""
    # True when this component's threshold is still UNSPECIFIED_CALIBRATE, i.e.
    # the score was produced with a placeholder and must not gate real money.
    uncalibrated: bool = False


@dataclass(frozen=True, slots=True)
class ConfidenceScore:
    value: float  # 0 - 100
    components: tuple[ConfidenceComponent, ...]

    @property
    def uncalibrated_components(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.components if c.uncalibrated)

    @property
    def calibrated(self) -> bool:
        """False while any component still relies on a placeholder threshold.

        The risk engine must refuse live orders when this is False. A confidence
        number built on invented thresholds is worse than no number at all.
        """
        return not self.uncalibrated_components


@dataclass(frozen=True, slots=True)
class GexSnapshot:
    """The single object every downstream service consumes.

    Deliberately fat: carrying all five views together means a strategy cannot
    accidentally read the aggregate while ignoring that the 0DTE bucket points
    the other way.
    """

    as_of: datetime
    spot: float
    source: str
    sign_convention: SignConvention

    # View 1 -- unsigned gamma concentration.
    total_unsigned_gex: float
    # View 2 -- naive signed GEX.
    total_signed_gex: float
    # View 3 -- expiry buckets.
    buckets: tuple[BucketGex, ...]
    # View 4 -- strike level.
    strikes: tuple[StrikeGex, ...]
    walls: WallSet
    # View 5 -- zero-gamma grid, one entry per IV convention that was run.
    zero_gamma: tuple[ZeroGammaResult, ...]

    confidence: ConfidenceScore
    contract_count: int = 0
    total_open_interest: int = 0
    warnings: tuple[str, ...] = ()
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def primary_zero_gamma(self) -> ZeroGammaResult | None:
        """The first convention that resolved a crossing, in run order."""
        for result in self.zero_gamma:
            if result.resolved:
                return result
        return None

    def bucket(self, bucket: ExpiryBucket) -> BucketGex | None:
        for entry in self.buckets:
            if entry.bucket is bucket:
                return entry
        return None

    def zero_gamma_for(self, convention: IVConvention) -> ZeroGammaResult | None:
        for result in self.zero_gamma:
            if result.convention is convention:
                return result
        return None

    @property
    def zero_gamma_spread_pct(self) -> float | None:
        """Max disagreement between IV conventions, as a % of spot.

        This is the honest measure of how model-dependent the zero-gamma level
        is on this snapshot, and it feeds ``zero_gamma_stability`` in the
        confidence score.
        """
        levels = [r.zero_gamma_spot for r in self.zero_gamma if r.resolved]
        if len(levels) < 2:
            return None
        return (max(levels) - min(levels)) / self.spot * 100.0
