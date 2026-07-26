"""GEX engine configuration and the UNSPECIFIED_CALIBRATE sentinel.

Every number in this file that the plan marks as un-researched is represented by
:data:`UNSPECIFIED_CALIBRATE`, not by a plausible-looking default. A plausible
default is worse than a sentinel because it survives code review.

Loading a config where a threshold is still the sentinel is legal -- the engine
runs, produces numbers, and marks the affected confidence components
``uncalibrated``. What is *not* legal is trading on it: see
:meth:`src.domain.gex.ConfidenceScore.calibrated`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Final

from src.domain.gex import ExpiryBucket, IVConvention, SignConvention


class _Unspecified:
    """Sentinel for a parameter that has no researched value yet.

    Truthiness is False and arithmetic raises, so an uncalibrated threshold
    cannot silently participate in a comparison.
    """

    __slots__ = ()
    _instance: "_Unspecified | None" = None

    def __new__(cls) -> "_Unspecified":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSPECIFIED_CALIBRATE"

    def __bool__(self) -> bool:
        return False

    def __lt__(self, other: Any) -> bool:
        raise TypeError(
            "UNSPECIFIED_CALIBRATE used in a comparison -- calibrate this "
            "threshold or branch on is_calibrated() first"
        )

    __le__ = __gt__ = __ge__ = __lt__


UNSPECIFIED_CALIBRATE: Final = _Unspecified()
YAML_SENTINEL: Final = "UNSPECIFIED_CALIBRATE"

Calibratable = float | _Unspecified


def is_calibrated(value: Calibratable) -> bool:
    return not isinstance(value, _Unspecified)


# DTE upper bounds, inclusive, in evaluation order.
BUCKET_BOUNDS: Final[tuple[tuple[ExpiryBucket, int], ...]] = (
    (ExpiryBucket.DTE_0, 0),
    (ExpiryBucket.DTE_1_2, 2),
    (ExpiryBucket.DTE_3_5, 5),
    (ExpiryBucket.DTE_6_30, 30),
)


@dataclass(frozen=True, slots=True)
class ZeroGammaConfig:
    """Spot-grid search settings.

    ``grid_span_pct`` of +/-4% around spot with 0.1% steps gives 81 points. Wide
    enough to bracket a realistic zero-gamma level on an ordinary day, narrow
    enough that a crossing found at the very edge is suspicious -- which is why
    ``no_crossing`` is reported rather than extrapolated.
    """

    grid_span_pct: float = 0.04
    grid_step_pct: float = 0.001
    conventions: tuple[IVConvention, ...] = (
        IVConvention.STICKY_STRIKE,  # plan's default research convention
        IVConvention.FROZEN_IV,
        IVConvention.STICKY_DELTA,
    )
    # Contracts beyond this DTE barely move the crossing but dominate runtime.
    # Excluding them is a documented approximation, not a silent one.
    max_dte_for_grid: int = 60
    # Minimum smile points per expiry before a fitted convention is trusted.
    min_points_for_smile_fit: int = 5


@dataclass(frozen=True, slots=True)
class WallConfig:
    """Wall and node extraction.

    Walls come from strike-aggregated gamma, never from raw open interest -- the
    plan rules that out explicitly, and OI-only walls put levels where the
    contracts are numerous rather than where hedging pressure is.
    """

    # A strike must carry at least this share of the largest strike's unsigned
    # GEX to qualify as a node.
    node_min_share_of_max: float = 0.25
    max_nodes_per_side: int = 5
    # A strike is "void" when its unsigned GEX is below this share of the max.
    void_max_share_of_max: float = 0.05
    # Voids narrower than this (as a share of spot) are noise, not travel space.
    void_min_width_pct: float = 0.002
    # Only strikes within this band of spot are considered, so a far-wing LEAP
    # strike cannot become the call wall.
    band_pct: float = 0.10


@dataclass(frozen=True, slots=True)
class ConfidenceWeights:
    """Relative weights of the eight components from the plan.

    Weights are a design choice, not a calibration target -- they encode which
    failure modes we consider most dangerous. The *thresholds* inside each
    component are the calibration targets.
    """

    chain_completeness: float = 0.20
    quote_freshness: float = 0.20
    oi_freshness: float = 0.05
    crossed_market_penalty: float = 0.10
    zero_gamma_stability: float = 0.15
    sign_model_agreement: float = 0.10
    dte0_dominance_alert: float = 0.10
    vendor_lag_alert: float = 0.10


@dataclass(frozen=True, slots=True)
class ConfidenceConfig:
    weights: ConfidenceWeights = field(default_factory=ConfidenceWeights)

    # --- Calibrated in code: these are measurable data-quality facts, not
    # market hypotheses, so a defensible value exists without backtesting. ---

    # Expected strikes are counted against the chain we asked for; below this
    # ratio the aggregate is missing structure.
    min_chain_completeness_ratio: float = 0.90
    # An option snapshot older than this is not describing the current book.
    max_quote_staleness_sec: float = 30.0
    # Fraction of crossed/locked quotes at which the component scores zero.
    crossed_quote_zero_score_ratio: float = 0.10
    # Options-feed vs spot-feed timestamp drift that scores zero.
    max_vendor_lag_sec: float = 5.0
    # OI older than this many sessions is unusable. T-1 settlement is normal and
    # scores full marks; the component exists to catch a stalled OI job.
    max_oi_age_sessions: int = 2

    # --- Genuine calibration targets: each needs out-of-sample evidence. ---

    # Zero-gamma disagreement between IV conventions, as % of spot, at which the
    # level is too model-dependent to trade around.
    max_zero_gamma_shift_pct: Calibratable = UNSPECIFIED_CALIBRATE
    # Relative gap between naive-signed and flow-adjusted signed GEX at which
    # the sign model is considered unreliable.
    max_sign_model_disagreement: Calibratable = UNSPECIFIED_CALIBRATE
    # 0DTE share of total unsigned GEX above which the aggregate is dominated by
    # same-day flow and the longer-dated structure is being masked.
    max_0dte_dominance_ratio: Calibratable = UNSPECIFIED_CALIBRATE


@dataclass(frozen=True, slots=True)
class GexEngineConfig:
    """Top-level engine settings."""

    # The 1% convention from the plan: GEX_i = gamma_i * OI_i * M * S * (0.01*S).
    spot_move_pct: float = 0.01
    sign_convention: SignConvention = SignConvention.DEALER_LONG_CALLS_SHORT_PUTS
    # Prefer the vendor's gamma when present, otherwise derive it from IV.
    # Setting this False forces the shadow pricer everywhere, which is the
    # apples-to-apples mode for zero-gamma work. See docs/specs/gex-engine.md.
    prefer_vendor_gamma: bool = True
    # Drop quotes whose book is crossed; they usually carry a nonsense IV.
    drop_crossed_quotes: bool = True
    # Contracts with no usable gamma source are excluded and counted against
    # chain completeness.
    require_open_interest: bool = True

    zero_gamma: ZeroGammaConfig = field(default_factory=ZeroGammaConfig)
    walls: WallConfig = field(default_factory=WallConfig)
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)

    def with_(self, **changes: Any) -> "GexEngineConfig":
        return replace(self, **changes)
