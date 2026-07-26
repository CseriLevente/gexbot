"""The eight-component confidence score.

Design rule: a component whose threshold is still ``UNSPECIFIED_CALIBRATE`` is
*computed* with a documented placeholder and *flagged* -- never silently given a
made-up number, and never dropped. Flagged components make
:attr:`ConfidenceScore.calibrated` False, which the risk engine treats as a hard
block on live orders. The engine therefore runs and produces research output on
day one, while remaining unable to trade until the calibration work is done.

Three components (``zero_gamma_stability``, ``sign_model_agreement``,
``0dte_dominance_alert``) are genuine calibration targets: their thresholds are
claims about the market. The other five measure data quality, where a defensible
value exists without any backtest -- a 60-second-old option snapshot is stale
whatever the strategy turns out to be.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

from src.domain.gex import (
    ConfidenceComponent,
    ConfidenceScore,
    ExpiryBucket,
    ZeroGammaResult,
)
from src.gex.config import Calibratable, ConfidenceConfig, is_calibrated
from src.gex.formulas import ContractGexResult
from src.gex.sessions import to_eastern, weekdays_between

# Placeholders used when a threshold is still uncalibrated. Deliberately
# conservative -- they make the score pessimistic, so an uncalibrated system
# looks worse than it is rather than better.
PLACEHOLDER_MAX_ZERO_GAMMA_SHIFT_PCT = 0.25
PLACEHOLDER_MAX_SIGN_MODEL_DISAGREEMENT = 0.30
PLACEHOLDER_MAX_0DTE_DOMINANCE_RATIO = 0.60


def _linear_decay(value: float, *, zero_at: float) -> float:
    """1.0 at ``value == 0``, falling linearly to 0.0 at ``value >= zero_at``."""
    if zero_at <= 0.0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - value / zero_at))


def _resolve(threshold: Calibratable, placeholder: float) -> tuple[float, bool]:
    """Return ``(value, uncalibrated)``."""
    if is_calibrated(threshold):
        return float(threshold), False  # type: ignore[arg-type]
    return placeholder, True


@dataclass(frozen=True, slots=True)
class ConfidenceInputs:
    """Everything the score needs, gathered explicitly.

    Passing a flat input struct rather than the whole pipeline keeps the score
    unit-testable and makes it obvious which measurements the system must be able
    to produce before a component means anything.
    """

    as_of: datetime
    result: ContractGexResult
    zero_gamma_results: tuple[ZeroGammaResult, ...]
    spot: float
    dte0_dominance_ratio: float | None
    # Expected strike/expiry count for the chain we requested. ``None`` when the
    # adapter cannot state it, in which case completeness falls back to the ratio
    # of usable quotes.
    expected_contract_count: int | None = None
    options_feed_timestamp: datetime | None = None
    spot_feed_timestamp: datetime | None = None
    open_interest_asof: date | None = None
    # Signed GEX from the flow-adjusted model, when Cboe Open-Close data is
    # available. ``None`` disables the agreement check.
    flow_adjusted_signed_gex: float | None = None
    naive_signed_gex: float | None = None


def score_chain_completeness(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    result = inputs.result
    if inputs.expected_contract_count:
        ratio = len(result.contracts) / inputs.expected_contract_count
    else:
        ratio = result.usable_ratio
    ratio = min(ratio, 1.0)
    floor = config.min_chain_completeness_ratio
    # Full marks at or above the floor, linear to zero at half the floor.
    if ratio >= floor:
        score = 1.0
    else:
        span = floor * 0.5
        score = max(0.0, (ratio - span) / (floor - span)) if floor > span else 0.0
    return ConfidenceComponent(
        name="chain_completeness",
        score=score,
        weight=config.weights.chain_completeness,
        detail=(
            f"{len(result.contracts)} usable of {result.total_quotes} quotes "
            f"(ratio {ratio:.3f}, floor {floor:.3f}); dropped: "
            f"expired={result.dropped_expired} "
            f"no_oi={result.dropped_no_open_interest} "
            f"no_gamma={result.dropped_no_gamma_source}"
        ),
    )


def score_quote_freshness(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    feed_ts = inputs.options_feed_timestamp
    if feed_ts is None:
        return ConfidenceComponent(
            name="quote_freshness",
            score=0.0,
            weight=config.weights.quote_freshness,
            detail="no options feed timestamp -- cannot prove freshness",
        )
    age = (to_eastern(inputs.as_of) - to_eastern(feed_ts)).total_seconds()
    score = _linear_decay(max(age, 0.0), zero_at=config.max_quote_staleness_sec)
    return ConfidenceComponent(
        name="quote_freshness",
        score=score,
        weight=config.weights.quote_freshness,
        detail=f"snapshot age {age:.1f}s (zero at {config.max_quote_staleness_sec:.0f}s)",
    )


def score_oi_freshness(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """Open interest is built from the prior day's settlement, so T-1 is the best
    achievable state and scores full marks. This component exists to catch an OI
    job that stalled days ago, not to penalise a fact of market structure.
    """
    if inputs.open_interest_asof is None:
        return ConfidenceComponent(
            name="oi_freshness",
            score=0.0,
            weight=config.weights.oi_freshness,
            detail="no OI as-of date supplied",
        )
    sessions = weekdays_between(
        inputs.open_interest_asof, to_eastern(inputs.as_of).date()
    )
    score = (
        1.0
        if sessions <= 1
        else _linear_decay(
            sessions - 1, zero_at=max(1, config.max_oi_age_sessions - 1)
        )
    )
    return ConfidenceComponent(
        name="oi_freshness",
        score=score,
        weight=config.weights.oi_freshness,
        detail=f"OI as of {inputs.open_interest_asof} ({sessions} session(s) old)",
    )


def score_crossed_market_penalty(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    ratio = inputs.result.crossed_ratio
    score = _linear_decay(ratio, zero_at=config.crossed_quote_zero_score_ratio)
    return ConfidenceComponent(
        name="crossed_market_penalty",
        score=score,
        weight=config.weights.crossed_market_penalty,
        detail=(
            f"{inputs.result.dropped_crossed} crossed/locked of "
            f"{inputs.result.total_quotes} ({ratio:.4f})"
        ),
    )


def score_zero_gamma_stability(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """How far apart the IV conventions place the level, as % of spot."""
    threshold, uncalibrated = _resolve(
        config.max_zero_gamma_shift_pct, PLACEHOLDER_MAX_ZERO_GAMMA_SHIFT_PCT
    )
    levels = [r.zero_gamma_spot for r in inputs.zero_gamma_results if r.resolved]
    if len(levels) < 2:
        return ConfidenceComponent(
            name="zero_gamma_stability",
            score=0.0,
            weight=config.weights.zero_gamma_stability,
            detail=(
                f"only {len(levels)} convention(s) resolved a crossing -- "
                "stability is unmeasurable"
            ),
            uncalibrated=uncalibrated,
        )
    spread_pct = (max(levels) - min(levels)) / inputs.spot * 100.0
    return ConfidenceComponent(
        name="zero_gamma_stability",
        score=_linear_decay(spread_pct, zero_at=threshold),
        weight=config.weights.zero_gamma_stability,
        detail=(
            f"convention spread {spread_pct:.4f}% of spot across "
            f"{len(levels)} conventions (zero at {threshold:.4f}%)"
        ),
        uncalibrated=uncalibrated,
    )


def score_sign_model_agreement(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """Naive-signed vs flow-adjusted signed GEX.

    Without Cboe Open-Close there is no second model to compare against, so the
    component scores zero and says why. That is the correct answer, not a
    penalty-free pass: a single unverified sign model is exactly the risk the
    plan warns about.
    """
    threshold, uncalibrated = _resolve(
        config.max_sign_model_disagreement, PLACEHOLDER_MAX_SIGN_MODEL_DISAGREEMENT
    )
    naive = inputs.naive_signed_gex
    flow = inputs.flow_adjusted_signed_gex
    if naive is None or flow is None:
        return ConfidenceComponent(
            name="sign_model_agreement",
            score=0.0,
            weight=config.weights.sign_model_agreement,
            detail="no flow-adjusted sign model available (needs Cboe Open-Close)",
            uncalibrated=uncalibrated,
        )
    scale = max(abs(naive), abs(flow))
    if scale <= 0.0:
        disagreement = 0.0
    else:
        disagreement = abs(naive - flow) / scale
    # A sign flip between the two models is a hard zero regardless of magnitude.
    if naive * flow < 0.0:
        return ConfidenceComponent(
            name="sign_model_agreement",
            score=0.0,
            weight=config.weights.sign_model_agreement,
            detail=f"models disagree on sign: naive={naive:.3e} flow={flow:.3e}",
            uncalibrated=uncalibrated,
        )
    return ConfidenceComponent(
        name="sign_model_agreement",
        score=_linear_decay(disagreement, zero_at=threshold),
        weight=config.weights.sign_model_agreement,
        detail=f"relative gap {disagreement:.4f} (zero at {threshold:.4f})",
        uncalibrated=uncalibrated,
    )


def score_0dte_dominance(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """Penalise snapshots where same-day gamma masks the longer-dated structure.

    High 0DTE share is not itself a data error -- it is now the normal state of
    the SPX complex. It *is* a reason to distrust the chain-wide aggregate, which
    is what this score expresses.
    """
    threshold, uncalibrated = _resolve(
        config.max_0dte_dominance_ratio, PLACEHOLDER_MAX_0DTE_DOMINANCE_RATIO
    )
    ratio = inputs.dte0_dominance_ratio
    if ratio is None:
        return ConfidenceComponent(
            name="0dte_dominance_alert",
            score=0.0,
            weight=config.weights.dte0_dominance_alert,
            detail="no gamma in chain -- dominance ratio undefined",
            uncalibrated=uncalibrated,
        )
    excess = max(0.0, ratio - threshold)
    score = _linear_decay(excess, zero_at=max(1e-9, 1.0 - threshold))
    return ConfidenceComponent(
        name="0dte_dominance_alert",
        score=score,
        weight=config.weights.dte0_dominance_alert,
        detail=(
            f"{ExpiryBucket.DTE_0.value} share {ratio:.4f} of unsigned GEX "
            f"(alert above {threshold:.4f})"
        ),
        uncalibrated=uncalibrated,
    )


def score_vendor_lag(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """Drift between the options feed and the spot feed.

    Gamma is computed from a spot and a chain that are supposed to describe the
    same instant. When they drift apart the gamma is a blend of two moments, and
    on 0DTE that blend is where the error lives.
    """
    options_ts = inputs.options_feed_timestamp
    spot_ts = inputs.spot_feed_timestamp
    if options_ts is None or spot_ts is None:
        return ConfidenceComponent(
            name="vendor_lag_alert",
            score=0.0,
            weight=config.weights.vendor_lag_alert,
            detail="missing feed timestamp on options or spot -- drift unmeasurable",
        )
    drift = abs((to_eastern(options_ts) - to_eastern(spot_ts)).total_seconds())
    return ConfidenceComponent(
        name="vendor_lag_alert",
        score=_linear_decay(drift, zero_at=config.max_vendor_lag_sec),
        weight=config.weights.vendor_lag_alert,
        detail=f"options/spot timestamp drift {drift:.2f}s "
        f"(zero at {config.max_vendor_lag_sec:.1f}s)",
    )


_SCORERS = (
    score_chain_completeness,
    score_quote_freshness,
    score_oi_freshness,
    score_crossed_market_penalty,
    score_zero_gamma_stability,
    score_sign_model_agreement,
    score_0dte_dominance,
    score_vendor_lag,
)


def compute_confidence(
    inputs: ConfidenceInputs, config: ConfidenceConfig | None = None
) -> ConfidenceScore:
    """Weighted 0-100 score over all eight components."""
    cfg = config or ConfidenceConfig()
    components = tuple(scorer(inputs, cfg) for scorer in _SCORERS)
    total_weight = sum(c.weight for c in components)
    if total_weight <= 0.0:
        raise ValueError("confidence weights sum to zero")
    weighted = sum(c.score * c.weight for c in components) / total_weight
    value = round(100.0 * min(max(weighted, 0.0), 1.0), 4)
    if math.isnan(value):  # pragma: no cover - defensive
        raise ValueError("confidence score is NaN")
    return ConfidenceScore(value=value, components=components)
