"""GEX engine configuration and the UNSPECIFIED_CALIBRATE sentinel.

Every number in this file that has no researched value is represented by
:data:`UNSPECIFIED_CALIBRATE`, not by a plausible-looking default. A plausible
default is worse than a sentinel because it survives code review.

Two categories of parameter live here and they are treated differently:

* **Data-plumbing facts** -- staleness tolerances, grid resolution, finiteness
  bounds. These have defensible values without any backtest ("a 60-second-old
  option snapshot is stale" is true whatever the strategy turns out to be), so
  they carry real numbers.
* **Market claims** -- what counts as "too much convention disagreement", "too
  much 0DTE dominance". These are hypotheses about the market and stay
  sentinels.

Loading a config where a market threshold is still the sentinel is legal: the
engine runs, produces numbers, and marks the affected confidence components
``uncalibrated``. Nothing here can trade in any case -- there is no risk engine
and no broker in this repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Final, NoReturn

from src.domain.gex import ExpiryBucket, IVConvention, SignConvention
from src.domain.model_spec import ModelSpec
from src.domain.timestamps import DataQualityLimits


class _Unspecified:
    """Sentinel for a parameter that has no researched value yet.

    Truthiness is False and every ordering comparison raises, so an uncalibrated
    threshold cannot silently participate in a ``>=`` and pass. ``float()`` and
    ``int()`` raise for the same reason: the most likely accident is a caller
    coercing the sentinel to a number "just to make the types line up", which
    would convert a loud "not researched" into a quiet, arbitrary threshold.
    """

    __slots__ = ()
    _instance: _Unspecified | None = None

    def __new__(cls) -> _Unspecified:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSPECIFIED_CALIBRATE"

    def __str__(self) -> str:
        return "UNSPECIFIED_CALIBRATE"

    def __bool__(self) -> bool:
        return False

    def _refuse(self, operation: str) -> NoReturn:
        raise TypeError(
            f"UNSPECIFIED_CALIBRATE used in {operation} -- calibrate this "
            "threshold or branch on is_calibrated() first"
        )

    def __lt__(self, other: Any) -> NoReturn:
        self._refuse("a comparison")

    def __le__(self, other: Any) -> NoReturn:
        self._refuse("a comparison")

    def __gt__(self, other: Any) -> NoReturn:
        self._refuse("a comparison")

    def __ge__(self, other: Any) -> NoReturn:
        self._refuse("a comparison")

    def __float__(self) -> NoReturn:
        self._refuse("a float() conversion")

    def __int__(self) -> NoReturn:
        self._refuse("an int() conversion")

    def __add__(self, other: Any) -> NoReturn:
        self._refuse("arithmetic")

    __radd__ = __add__
    __sub__ = __add__
    __rsub__ = __add__
    __mul__ = __add__
    __rmul__ = __add__
    __truediv__ = __add__
    __rtruediv__ = __add__

    def __hash__(self) -> int:
        return hash("UNSPECIFIED_CALIBRATE")

    def __eq__(self, other: object) -> bool:
        # Equality is safe and useful (config round-trips compare values); only
        # *ordering* and coercion are dangerous.
        return isinstance(other, _Unspecified)


UNSPECIFIED_CALIBRATE: Final = _Unspecified()
YAML_SENTINEL: Final = "UNSPECIFIED_CALIBRATE"

Calibratable = float | _Unspecified


def is_calibrated(value: Calibratable) -> bool:
    return not isinstance(value, _Unspecified)


def calibrated_value(value: Calibratable) -> float | None:
    """Extract the number, or ``None`` when still a sentinel.

    The only sanctioned way to get a float out of a ``Calibratable``.
    """
    return None if isinstance(value, _Unspecified) else float(value)


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

    +/-4% around spot at 0.1% steps gives 81 points. Wide enough to bracket a
    realistic level, fine enough that the grid is denser than SPX's 25-point
    strike ladder.
    """

    grid_span_pct: float = 0.04
    grid_step_pct: float = 0.001
    conventions: tuple[IVConvention, ...] = (
        IVConvention.STICKY_STRIKE,  # default research convention
        IVConvention.FROZEN_IV,
        IVConvention.STICKY_MONEYNESS,
    )
    # Contracts beyond this DTE barely move the crossing but dominate runtime.
    # A documented approximation, quantified in ``GexSnapshot.zero_gamma_universe``.
    max_dte_for_grid: int = 60
    # Minimum smile points per expiry before a fitted convention is trusted.
    min_points_for_smile_fit: int = 5

    # --- Boundary handling ---
    # A root within this fraction of the grid edge may be an artefact of where we
    # stopped looking rather than a real crossing.
    boundary_tolerance_pct: float = 0.10
    # Bounded expansion when a root lands near the edge. Bounded because an
    # unbounded search on a one-sided book would run until the float range does.
    max_grid_expansions: int = 3
    grid_expansion_factor: float = 1.75

    def as_dict(self) -> dict[str, Any]:
        return {
            "grid_span_pct": self.grid_span_pct,
            "grid_step_pct": self.grid_step_pct,
            "conventions": [c.value for c in self.conventions],
            "max_dte_for_grid": self.max_dte_for_grid,
            "min_points_for_smile_fit": self.min_points_for_smile_fit,
            "boundary_tolerance_pct": self.boundary_tolerance_pct,
            "max_grid_expansions": self.max_grid_expansions,
            "grid_expansion_factor": self.grid_expansion_factor,
        }


@dataclass(frozen=True, slots=True)
class WallConfig:
    """Wall, node and void extraction.

    Walls come from strike-aggregated gamma, never from raw open interest: a
    strike can carry enormous OI in far-dated series that contribute almost no
    gamma, and an OI-ranked "wall" points at a level with no hedging pressure
    behind it.
    """

    node_min_share_of_max: float = 0.25
    max_nodes_per_side: int = 5
    void_max_share_of_max: float = 0.05
    void_min_width_pct: float = 0.002
    band_pct: float = 0.10

    # --- Directional wall rules ---
    # A qualifying upside wall must sit at least this far above spot; without a
    # buffer, a strike one tick above spot would be reported as resistance.
    directional_wall_min_distance_pct: float = 0.0
    # Restrict directional walls to the same band as the neutral maxima.
    directional_wall_band_pct: float = 0.10

    # --- Void classification against an expected strike ladder ---
    # Fraction of the modal strike spacing above which a gap counts as irregular.
    irregular_spacing_factor: float = 1.5
    # A void needs at least this many observed strikes inside it before it can be
    # called a true low-gamma region rather than a coverage hole.
    min_observed_strikes_for_true_void: int = 2
    # Below this share of expected strikes present, the region is coverage-limited.
    min_ladder_coverage_for_true_void: float = 0.80

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_min_share_of_max": self.node_min_share_of_max,
            "max_nodes_per_side": self.max_nodes_per_side,
            "void_max_share_of_max": self.void_max_share_of_max,
            "void_min_width_pct": self.void_min_width_pct,
            "band_pct": self.band_pct,
            "directional_wall_min_distance_pct": (
                self.directional_wall_min_distance_pct
            ),
            "directional_wall_band_pct": self.directional_wall_band_pct,
            "irregular_spacing_factor": self.irregular_spacing_factor,
            "min_observed_strikes_for_true_void": (
                self.min_observed_strikes_for_true_void
            ),
            "min_ladder_coverage_for_true_void": (
                self.min_ladder_coverage_for_true_void
            ),
        }


@dataclass(frozen=True, slots=True)
class ConfidenceWeights:
    """Relative weights of the confidence components.

    Weights encode which failure modes we consider most dangerous; they are a
    design choice, not a calibration target. The *thresholds* inside each
    component are what needs research.
    """

    chain_completeness: float = 0.12
    quote_freshness: float = 0.12
    oi_freshness: float = 0.04
    crossed_market_penalty: float = 0.06
    zero_gamma_stability: float = 0.10
    sign_model_agreement: float = 0.06
    dte0_dominance_alert: float = 0.06
    vendor_lag_alert: float = 0.06
    # Added in v2.
    multiple_root_penalty: float = 0.06
    root_slope_score: float = 0.06
    root_boundary_penalty: float = 0.06
    root_identity_stability: float = 0.04
    timestamp_alignment_score: float = 0.06
    future_timestamp_penalty: float = 0.04
    option_universe_coverage_score: float = 0.06
    iv_spread_quality: float = 0.04
    model_parameter_completeness: float = 0.02

    def as_dict(self) -> dict[str, float]:
        return {
            "chain_completeness": self.chain_completeness,
            "quote_freshness": self.quote_freshness,
            "oi_freshness": self.oi_freshness,
            "crossed_market_penalty": self.crossed_market_penalty,
            "zero_gamma_stability": self.zero_gamma_stability,
            "sign_model_agreement": self.sign_model_agreement,
            "dte0_dominance_alert": self.dte0_dominance_alert,
            "vendor_lag_alert": self.vendor_lag_alert,
            "multiple_root_penalty": self.multiple_root_penalty,
            "root_slope_score": self.root_slope_score,
            "root_boundary_penalty": self.root_boundary_penalty,
            "root_identity_stability": self.root_identity_stability,
            "timestamp_alignment_score": self.timestamp_alignment_score,
            "future_timestamp_penalty": self.future_timestamp_penalty,
            "option_universe_coverage_score": self.option_universe_coverage_score,
            "iv_spread_quality": self.iv_spread_quality,
            "model_parameter_completeness": self.model_parameter_completeness,
        }


@dataclass(frozen=True, slots=True)
class ConfidenceConfig:
    weights: ConfidenceWeights = field(default_factory=ConfidenceWeights)

    # --- Data-quality thresholds: measurable facts, defensible without a
    # backtest, so they carry real values. ---
    min_chain_completeness_ratio: float = 0.90
    crossed_quote_zero_score_ratio: float = 0.10
    # Universe coverage below which the aggregate is not describing the chain.
    min_universe_coverage_ratio: float = 0.70
    # Share of contracts with a usable, tight IV needed for full marks.
    min_good_iv_ratio: float = 0.90
    # Roots closer together than this (% of spot) make the level ambiguous.
    ambiguous_root_spacing_pct: float = 0.50
    # Normalised slope at or above which a crossing counts as steep and stable.
    steep_slope_threshold: float = 0.20

    # --- Market claims: each needs out-of-sample evidence. ---
    max_zero_gamma_shift_pct: Calibratable = UNSPECIFIED_CALIBRATE
    max_sign_model_disagreement: Calibratable = UNSPECIFIED_CALIBRATE
    max_0dte_dominance_ratio: Calibratable = UNSPECIFIED_CALIBRATE

    def as_dict(self) -> dict[str, Any]:
        return {
            "weights": self.weights.as_dict(),
            "min_chain_completeness_ratio": self.min_chain_completeness_ratio,
            "crossed_quote_zero_score_ratio": self.crossed_quote_zero_score_ratio,
            "min_universe_coverage_ratio": self.min_universe_coverage_ratio,
            "min_good_iv_ratio": self.min_good_iv_ratio,
            "ambiguous_root_spacing_pct": self.ambiguous_root_spacing_pct,
            "steep_slope_threshold": self.steep_slope_threshold,
            "max_zero_gamma_shift_pct": str(self.max_zero_gamma_shift_pct),
            "max_sign_model_disagreement": str(self.max_sign_model_disagreement),
            "max_0dte_dominance_ratio": str(self.max_0dte_dominance_ratio),
        }


@dataclass(frozen=True, slots=True)
class GexEngineConfig:
    """Top-level engine settings."""

    # The 1% convention: GEX_i = gamma_i * OI_i * M * S * (0.01*S).
    spot_move_pct: float = 0.01
    sign_convention: SignConvention = SignConvention.DEALER_LONG_CALLS_SHORT_PUTS
    # Prefer the vendor's gamma when present, otherwise derive it from IV.
    # False forces the shadow pricer everywhere, which is the apples-to-apples
    # mode for zero-gamma work.
    prefer_vendor_gamma: bool = False
    drop_crossed_quotes: bool = True
    require_open_interest: bool = True
    # Cap on the chain-total universe. ``None`` means "everything supplied".
    max_dte: int | None = None

    model_spec: ModelSpec = field(default_factory=ModelSpec)
    data_quality: DataQualityLimits = field(default_factory=DataQualityLimits)
    zero_gamma: ZeroGammaConfig = field(default_factory=ZeroGammaConfig)
    walls: WallConfig = field(default_factory=WallConfig)
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)
    # Set by the config loader so a snapshot can be traced back to the exact file
    # that produced it. ``None`` when the engine is driven programmatically.
    config_fingerprint: str | None = None

    def with_(self, **changes: Any) -> GexEngineConfig:
        return replace(self, **changes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "spot_move_pct": self.spot_move_pct,
            "sign_convention": self.sign_convention.value,
            "prefer_vendor_gamma": self.prefer_vendor_gamma,
            "drop_crossed_quotes": self.drop_crossed_quotes,
            "require_open_interest": self.require_open_interest,
            "max_dte": self.max_dte,
            "model_spec": self.model_spec.as_dict(),
            "data_quality": self.data_quality.as_dict(),
            "zero_gamma": self.zero_gamma.as_dict(),
            "walls": self.walls.as_dict(),
            "confidence": self.confidence.as_dict(),
        }

    def fingerprint(self) -> str:
        import hashlib
        import json

        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
