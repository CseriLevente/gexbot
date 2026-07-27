"""The confidence model.

Design rule: a component whose threshold is still ``UNSPECIFIED_CALIBRATE`` is
*computed* with a documented placeholder and *flagged* -- never silently given a
made-up number, and never dropped. Flagged components make
``ConfidenceScore.calibrated`` False.

An important honesty note about what that flag is and is not: it is a **research
signal**, not an enforcement mechanism. This repository has no risk engine and no
broker, so nothing consumes ``calibrated`` to block an order -- there are no
orders. Earlier documentation overstated this; see ``docs/OPEN_DECISIONS.md``.

Components fall into two families:

* **Data-quality**, where a defensible threshold exists without any backtest --
  a 60-second-old option snapshot is stale whatever the strategy turns out to be.
* **Market claims** (``zero_gamma_stability``, ``sign_model_agreement``,
  ``0dte_dominance_alert``), whose thresholds are hypotheses and stay sentinels.

Separately, some conditions are *hard failures* rather than score reductions: a
vendor timestamp materially in the future means the snapshot cannot be trusted at
all, so it zeroes its component and raises a flag that a downstream regime
service would map to ``DATA_HALT``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from src.domain.contracts import OptionQuote
from src.domain.gex import (
    ConfidenceComponent,
    ConfidenceScore,
    ExpiryBucket,
    OptionUniverse,
    ZeroGammaResult,
)
from src.domain.iv import IVQualityFlag
from src.domain.model_spec import ModelSpec
from src.domain.timestamps import DataQualityLimits
from src.domain.validation import ValidationCode, ValidationReport
from src.gex.calendar import sessions_since
from src.gex.config import Calibratable, ConfidenceConfig, calibrated_value
from src.gex.formulas import ContractGexResult
from src.gex.sessions import to_eastern

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
    """Return ``(value, uncalibrated)``.

    Goes through ``calibrated_value`` rather than ``float(threshold)`` because
    the sentinel refuses coercion by design.
    """
    resolved = calibrated_value(threshold)
    if resolved is None:
        return placeholder, True
    return resolved, False


@dataclass(frozen=True, slots=True)
class ConfidenceInputs:
    """Everything the score needs, gathered explicitly.

    A flat input struct rather than the whole pipeline keeps the score
    unit-testable and makes it obvious which measurements the system must be able
    to produce before a component means anything.
    """

    as_of: datetime
    result: ContractGexResult
    zero_gamma_results: tuple[ZeroGammaResult, ...]
    spot: float
    dte0_dominance_ratio: float | None
    model_spec: ModelSpec
    limits: DataQualityLimits
    chain_universe: OptionUniverse
    zero_gamma_universe: OptionUniverse
    quotes: tuple[OptionQuote, ...] = ()
    expected_contract_count: int | None = None
    options_feed_timestamp: datetime | None = None
    spot_feed_timestamp: datetime | None = None
    open_interest_as_of: date | None = None
    #: Newest OI date in the chain. The future check uses this; the staleness
    #: measure uses the oldest. Defaults to ``open_interest_as_of`` when the
    #: caller supplies only one.
    latest_open_interest_as_of: date | None = None
    # Signed GEX from the flow-adjusted model, when Cboe Open-Close data is
    # available. ``None`` disables the agreement check.
    flow_adjusted_signed_gex: float | None = None
    naive_signed_gex: float | None = None

    @property
    def validation(self) -> ValidationReport:
        return self.result.validation


# --- Data-quality components ------------------------------------------------


def score_chain_completeness(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    result = inputs.result
    ratio = (
        len(result.contracts) / inputs.expected_contract_count
        if inputs.expected_contract_count
        else result.usable_ratio
    )
    ratio = min(ratio, 1.0)
    floor = config.min_chain_completeness_ratio
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
            f"(ratio {ratio:.3f}, floor {floor:.3f}); exclusions="
            f"{result.exclusion_counts()}"
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
    if age < 0.0:
        # A quote dated after the request is not "extra fresh". Scoring the
        # absolute drift, not the clamped age, is what stops a future timestamp
        # from earning full marks here.
        return ConfidenceComponent(
            name="quote_freshness",
            score=0.0,
            weight=config.weights.quote_freshness,
            detail=(
                f"options feed timestamp is {-age:.1f}s AHEAD of the request "
                "instant; freshness is not measurable"
            ),
        )
    return ConfidenceComponent(
        name="quote_freshness",
        score=_linear_decay(age, zero_at=inputs.limits.max_snapshot_age_seconds),
        weight=config.weights.quote_freshness,
        detail=(
            f"snapshot age {age:.1f}s "
            f"(zero at {inputs.limits.max_snapshot_age_seconds:.0f}s)"
        ),
    )


def score_oi_freshness(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """Open interest is built from the prior session's settlement.

    T-1 is the best achievable state and scores full marks; this component exists
    to catch a stalled OI job, not to penalise a fact of market structure. Ageing
    is in *trading sessions*, so Friday's settlement read on Monday is one
    session old, and a holiday weekend does not make it look worse.
    """
    if inputs.open_interest_as_of is None:
        return ConfidenceComponent(
            name="oi_freshness",
            score=0.0,
            weight=config.weights.oi_freshness,
            detail="no open_interest_as_of date supplied",
        )
    # Ordering matters: the future check runs BEFORE session ageing.
    #
    # `sessions_between` returns 0 when end <= start, so a future-dated OI aged to
    # "0 sessions old" and scored a perfect 1.0 -- the freshest possible reading
    # for impossible data. Settlement dates get no clock-skew tolerance either:
    # unlike a quote timestamp, tomorrow's settlement date is not drift between
    # two machines, it is data that cannot exist.
    reference_date = to_eastern(inputs.as_of).date()
    newest = inputs.latest_open_interest_as_of or inputs.open_interest_as_of
    if newest > reference_date:
        ahead = (newest - reference_date).days
        return ConfidenceComponent(
            name="oi_freshness",
            score=0.0,
            weight=config.weights.oi_freshness,
            detail=(
                f"open interest is dated {newest}, "
                f"{ahead} day(s) in the future relative to the snapshot date "
                f"{reference_date}; settlement data cannot come from the future "
                "-- DATA_HALT eligible"
            ),
            hard_failure=True,
        )

    sessions = sessions_since(inputs.as_of, inputs.open_interest_as_of)
    limit = inputs.limits.max_open_interest_age_sessions
    score = (
        1.0 if sessions <= 1 else _linear_decay(sessions - 1, zero_at=max(1, limit - 1))
    )
    return ConfidenceComponent(
        name="oi_freshness",
        score=score,
        weight=config.weights.oi_freshness,
        detail=(
            f"OI as of {inputs.open_interest_as_of} ({sessions} trading "
            f"session(s) old, limit {limit})"
        ),
    )


def score_crossed_market_penalty(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    ratio = inputs.result.crossed_ratio
    return ConfidenceComponent(
        name="crossed_market_penalty",
        score=_linear_decay(ratio, zero_at=config.crossed_quote_zero_score_ratio),
        weight=config.weights.crossed_market_penalty,
        detail=(
            f"crossed/locked share {ratio:.4f} of {inputs.result.total_quotes} quotes"
        ),
    )


def score_vendor_lag(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """Drift between the options feed and the spot feed."""
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
        score=_linear_decay(
            drift, zero_at=inputs.limits.max_quote_underlying_skew_seconds
        ),
        weight=config.weights.vendor_lag_alert,
        detail=(
            f"options/spot timestamp drift {drift:.2f}s (zero at "
            f"{inputs.limits.max_quote_underlying_skew_seconds:.1f}s)"
        ),
    )


def score_timestamp_alignment(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """Share of contracts whose source clocks agree within tolerance.

    Measured from the validation pass rather than re-derived, so the score and
    the rejection counters can never tell different stories.
    """
    report = inputs.validation
    total = report.total
    if total == 0:
        return ConfidenceComponent(
            name="timestamp_alignment_score",
            score=0.0,
            weight=config.weights.timestamp_alignment_score,
            detail="no records validated",
        )
    skewed = report.count(ValidationCode.TIMESTAMP_SKEW)
    ratio = min(skewed / total, 1.0)
    return ConfidenceComponent(
        name="timestamp_alignment_score",
        score=1.0 - ratio,
        weight=config.weights.timestamp_alignment_score,
        detail=(
            f"{skewed} of {total} records exceeded a join skew tolerance "
            f"(greeks<={inputs.limits.max_quote_greeks_skew_seconds}s, "
            f"iv<={inputs.limits.max_quote_iv_skew_seconds}s, "
            f"underlying<={inputs.limits.max_quote_underlying_skew_seconds}s)"
        ),
    )


def score_future_timestamp(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """Hard failure on a vendor timestamp materially in the future.

    Small clock skew between two machines is ordinary and is absorbed by
    ``max_future_timestamp_seconds``. Beyond that, either the feed, the parser or
    our own clock is wrong, and none of those should be able to produce a
    healthy-looking snapshot. This is the component a downstream regime service
    would read to raise ``DATA_HALT``.
    """
    report = inputs.validation
    offenders = report.count(ValidationCode.FUTURE_TIMESTAMP)
    tolerance = inputs.limits.max_future_timestamp_seconds

    # The chain-level clock is checked directly: it does not pass through the
    # per-contract validation pass.
    feed_ahead_by = 0.0
    for label, stamp in (
        ("options feed", inputs.options_feed_timestamp),
        ("spot feed", inputs.spot_feed_timestamp),
    ):
        if stamp is None:
            continue
        drift = (to_eastern(stamp) - to_eastern(inputs.as_of)).total_seconds()
        if drift > tolerance:
            offenders += 1
            feed_ahead_by = max(feed_ahead_by, drift)
            del label

    if offenders:
        return ConfidenceComponent(
            name="future_timestamp_penalty",
            score=0.0,
            weight=config.weights.future_timestamp_penalty,
            detail=(
                f"{offenders} record(s)/feed(s) dated after the request instant "
                f"beyond the {tolerance:.1f}s clock-skew allowance"
                + (f"; worst {feed_ahead_by:.1f}s" if feed_ahead_by else "")
                + " -- DATA_HALT eligible"
            ),
            hard_failure=True,
        )
    return ConfidenceComponent(
        name="future_timestamp_penalty",
        score=1.0,
        weight=config.weights.future_timestamp_penalty,
        detail=f"no timestamp beyond the {tolerance:.1f}s future allowance",
    )


def score_iv_spread_quality(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """Share of contracts whose IV came from a clean two-sided book."""
    if not inputs.quotes:
        return ConfidenceComponent(
            name="iv_spread_quality",
            score=0.0,
            weight=config.weights.iv_spread_quality,
            detail="no quotes supplied -- IV quality unmeasurable",
        )
    counts: dict[str, int] = {}
    good = 0
    for quote in inputs.quotes:
        flag = quote.iv.quality
        counts[flag.value] = counts.get(flag.value, 0) + 1
        if flag is IVQualityFlag.OK:
            good += 1
    ratio = good / len(inputs.quotes)
    floor = config.min_good_iv_ratio
    score = 1.0 if ratio >= floor else max(0.0, ratio / floor)
    return ConfidenceComponent(
        name="iv_spread_quality",
        score=score,
        weight=config.weights.iv_spread_quality,
        detail=f"{good}/{len(inputs.quotes)} clean two-sided IV; breakdown={counts}",
    )


def score_option_universe_coverage(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """How much of the chain's gamma the zero-gamma grid actually covers.

    The grid runs on a DTE-capped subset. If that subset carries only half the
    chain's gamma, the level it produces is not a statement about the whole book,
    and the score says so instead of leaving the reader to compare two universes
    unknowingly.
    """
    universe = inputs.zero_gamma_universe
    share = universe.included_unsigned_gex_share
    if share is None:
        return ConfidenceComponent(
            name="option_universe_coverage_score",
            score=0.0,
            weight=config.weights.option_universe_coverage_score,
            detail="no gamma in the chain -- coverage undefined",
        )
    floor = config.min_universe_coverage_ratio
    # Monotone in coverage, not a step at the floor.
    #
    # A flat "1.0 above the floor" meant 71% coverage scored identically to 100%
    # -- reporting full confidence for a grid that skipped 29% of the chain's
    # gamma. Full marks now require full coverage; below the floor the component
    # collapses, because a grid missing a third of the book is not describing the
    # book.
    score = 0.0 if share < floor else share
    return ConfidenceComponent(
        name="option_universe_coverage_score",
        score=score,
        weight=config.weights.option_universe_coverage_score,
        detail=(
            f"zero-gamma grid covers {share:.1%} of chain unsigned GEX "
            f"({universe.included_contract_count}/{universe.total_contract_count} "
            f"contracts, max_dte={universe.max_dte_used}; floor {floor:.0%}); "
            f"exclusions={universe.filter_reasons}"
        ),
    )


def score_model_parameter_completeness(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """Whether the pricing assumptions are fully specified.

    A zero rate and a zero dividend yield on an SPX chain are almost certainly
    "nobody configured this" rather than a deliberate choice, and both bias gamma.
    """
    spec = inputs.model_spec
    missing: list[str] = []
    if spec.risk_free_rate == 0.0:
        missing.append("risk_free_rate=0")
    if spec.dividend_yield == 0.0:
        missing.append("dividend_yield=0 (SPX carries a material yield)")
    from src.domain.iv import IVSource

    if spec.iv_price_source is IVSource.VENDOR_DEFAULT_IV:
        missing.append("iv_price_source is the undocumented vendor default")
    score = 1.0 - (len(missing) / 3.0)
    return ConfidenceComponent(
        name="model_parameter_completeness",
        score=max(0.0, score),
        weight=config.weights.model_parameter_completeness,
        detail=(
            f"model {spec.fingerprint()}; unspecified: {missing}"
            if missing
            else f"model {spec.fingerprint()} fully specified"
        ),
    )


# --- Zero-gamma root diagnostics -------------------------------------------


def _resolved_results(inputs: ConfidenceInputs) -> list[ZeroGammaResult]:
    return [r for r in inputs.zero_gamma_results if r.resolved]


def score_multiple_root_penalty(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """Penalise ambiguity in the number and proximity of crossings.

    Two crossings 5% apart is a legitimate, readable market state. Two crossings
    0.2% apart means the level is a coin flip, so proximity is penalised
    separately from count.
    """
    resolved = _resolved_results(inputs)
    if not resolved:
        return ConfidenceComponent(
            name="multiple_root_penalty",
            score=0.0,
            weight=config.weights.multiple_root_penalty,
            detail="no convention resolved a crossing",
        )
    worst_count = max(r.root_count for r in resolved)
    count_score = 1.0 if worst_count <= 1 else max(0.0, 1.0 - 0.35 * (worst_count - 1))

    spacings = [
        r.nearest_root_spacing_pct
        for r in resolved
        if r.nearest_root_spacing_pct is not None
    ]
    if spacings:
        tightest = min(spacings)
        proximity_score = min(1.0, tightest / config.ambiguous_root_spacing_pct)
    else:
        tightest = None
        proximity_score = 1.0

    return ConfidenceComponent(
        name="multiple_root_penalty",
        score=min(count_score, proximity_score),
        weight=config.weights.multiple_root_penalty,
        detail=(
            f"worst root count {worst_count}"
            + (
                f", tightest spacing {tightest:.3f}% of spot "
                f"(ambiguous below {config.ambiguous_root_spacing_pct:.2f}%)"
                if tightest is not None
                else ", single root per convention"
            )
        ),
    )


def score_root_slope(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """Steep crossings are stable; shallow ones move on noise."""
    resolved = _resolved_results(inputs)
    slopes = [r.normalised_slope for r in resolved if r.normalised_slope is not None]
    if not slopes:
        return ConfidenceComponent(
            name="root_slope_score",
            score=0.0,
            weight=config.weights.root_slope_score,
            detail="no measurable slope at any selected root",
        )
    shallowest = min(abs(slope) for slope in slopes)
    score = min(1.0, shallowest / config.steep_slope_threshold)
    return ConfidenceComponent(
        name="root_slope_score",
        score=score,
        weight=config.weights.root_slope_score,
        detail=(
            f"shallowest normalised slope {shallowest:.4f} "
            f"(full marks at {config.steep_slope_threshold:.2f}); "
            "units are fraction of max |GEX| per 1% of spot"
        ),
    )


def score_root_boundary(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """A root at the edge of the search window may be where we stopped looking."""
    resolved = _resolved_results(inputs)
    if not resolved:
        return ConfidenceComponent(
            name="root_boundary_penalty",
            score=0.0,
            weight=config.weights.root_boundary_penalty,
            detail="no convention resolved a crossing",
        )
    at_boundary = [r for r in resolved if r.root_near_boundary]
    expansions = max(r.grid_expansions for r in resolved)
    if not at_boundary:
        return ConfidenceComponent(
            name="root_boundary_penalty",
            score=1.0,
            weight=config.weights.root_boundary_penalty,
            detail=(
                "all selected roots sit inside the grid interior"
                + (f" after {expansions} expansion(s)" if expansions else "")
            ),
        )
    return ConfidenceComponent(
        name="root_boundary_penalty",
        score=max(0.0, 1.0 - len(at_boundary) / len(resolved)),
        weight=config.weights.root_boundary_penalty,
        detail=(
            f"{len(at_boundary)}/{len(resolved)} selected roots sit near the grid "
            f"boundary after {expansions} expansion(s) -- level may be an artefact "
            "of the search window"
        ),
    )


# --- Cross-convention root topology -----------------------------------------


@dataclass(frozen=True, slots=True)
class RootMatching:
    """Result of pairing two conventions' root sets."""

    matched_root_count: int
    unmatched_root_count: int
    maximum_matched_root_shift_pct: float | None
    matched_pairs: tuple[tuple[float, float], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched_root_count": self.matched_root_count,
            "unmatched_root_count": self.unmatched_root_count,
            "maximum_matched_root_shift_pct": self.maximum_matched_root_shift_pct,
        }


def match_roots(
    left: tuple[float, ...],
    right: tuple[float, ...],
    *,
    spot: float,
    tolerance_pct: float,
) -> RootMatching:
    """Pair roots between two conventions, deterministically.

    Comparing root *counts* alone -- the v2 behaviour -- calls two conventions
    stable when each finds two roots in completely different places. What matters
    is whether the same levels survived, so the roots are paired and the
    survivors, the casualties and the displacement are reported separately.

    Algorithm: enumerate every candidate pair within tolerance, then greedily
    accept in order of (distance, left value, right value). Sorting candidates by
    *value* rather than input order is what makes the pairing independent of how
    the roots arrived -- including when two roots sit a hair apart, where an
    order-dependent rule would flip between runs.
    """
    if spot <= 0.0:
        return RootMatching(0, len(left) + len(right), None)

    tolerance = spot * tolerance_pct / 100.0
    candidates = sorted(
        (abs(a - b), a, b, i, j)
        for i, a in enumerate(sorted(left))
        for j, b in enumerate(sorted(right))
        if abs(a - b) <= tolerance
    )

    used_left: set[int] = set()
    used_right: set[int] = set()
    pairs: list[tuple[float, float]] = []
    worst = 0.0
    for distance, a, b, i, j in candidates:
        if i in used_left or j in used_right:
            continue
        used_left.add(i)
        used_right.add(j)
        pairs.append((a, b))
        worst = max(worst, distance / spot * 100.0)

    unmatched = (len(left) - len(used_left)) + (len(right) - len(used_right))
    return RootMatching(
        matched_root_count=len(pairs),
        unmatched_root_count=unmatched,
        maximum_matched_root_shift_pct=worst if pairs else None,
        matched_pairs=tuple(pairs),
    )


def compare_root_topology(
    results: tuple[ZeroGammaResult, ...], *, spot: float, tolerance_pct: float
) -> dict[str, Any]:
    """Topology metrics across every resolved convention, each compared against
    the first. Deterministic, and serialised into the snapshot and the hash.
    """
    resolved = [r for r in results if r.selected_root is not None]
    if len(resolved) < 2:
        return {
            "comparable": False,
            "matched_root_count": 0,
            "unmatched_root_count": 0,
            "maximum_matched_root_shift_pct": None,
            "selected_root_identity_stable": None,
            "root_topology_stable": None,
        }

    reference = resolved[0]
    matched = 0
    unmatched = 0
    worst_shift = 0.0
    selected_stable = True

    for other in resolved[1:]:
        matching = match_roots(
            reference.all_roots,
            other.all_roots,
            spot=spot,
            tolerance_pct=tolerance_pct,
        )
        matched += matching.matched_root_count
        unmatched += matching.unmatched_root_count
        if matching.maximum_matched_root_shift_pct is not None:
            worst_shift = max(worst_shift, matching.maximum_matched_root_shift_pct)
        assert reference.selected_root is not None
        assert other.selected_root is not None
        drift = abs(other.selected_root - reference.selected_root) / spot * 100.0
        if drift > tolerance_pct:
            selected_stable = False

    return {
        "comparable": True,
        "matched_root_count": matched,
        "unmatched_root_count": unmatched,
        "maximum_matched_root_shift_pct": worst_shift,
        "selected_root_identity_stable": selected_stable,
        "root_topology_stable": unmatched == 0 and selected_stable,
    }


def score_root_identity_stability(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """Do the conventions agree on the *shape* of the curve, not just the count?

    v2 compared root counts only, so two conventions each finding two roots in
    entirely different places scored a perfect 1.0. Full topology is compared
    now: which roots survived, how far the survivors moved, and whether the
    selected root kept its identity.

    Penalties, in increasing severity:

    * matched roots that drifted -- a proportionate deduction;
    * roots that appeared or disappeared -- the curve changed shape;
    * the selected root changing identity -- the conventions disagree about which
      level matters at all, which is a hard zero.
    """
    topology = compare_root_topology(
        inputs.zero_gamma_results,
        spot=inputs.spot,
        tolerance_pct=config.root_match_tolerance_pct,
    )
    if not topology["comparable"]:
        resolved = len(_resolved_results(inputs))
        return ConfidenceComponent(
            name="root_identity_stability",
            score=0.0,
            weight=config.weights.root_identity_stability,
            detail=(
                f"only {resolved} convention(s) resolved -- not comparable; "
                "matched_root_count=0 unmatched_root_count=0 "
                "maximum_matched_root_shift_pct=None "
                "selected_root_identity_stable=None root_topology_stable=None"
            ),
        )

    if not topology["selected_root_identity_stable"]:
        score = 0.0
    else:
        total = topology["matched_root_count"] + topology["unmatched_root_count"]
        matched_share = topology["matched_root_count"] / total if total else 0.0
        shift = topology["maximum_matched_root_shift_pct"] or 0.0
        drift_penalty = min(1.0, shift / max(config.root_match_tolerance_pct, 1e-9))
        score = max(0.0, matched_share * (1.0 - 0.5 * drift_penalty))

    return ConfidenceComponent(
        name="root_identity_stability",
        score=score,
        weight=config.weights.root_identity_stability,
        detail=(
            f"matched_root_count={topology['matched_root_count']} "
            f"unmatched_root_count={topology['unmatched_root_count']} "
            "maximum_matched_root_shift_pct="
            f"{topology['maximum_matched_root_shift_pct']} "
            "selected_root_identity_stable="
            f"{topology['selected_root_identity_stable']} "
            f"root_topology_stable={topology['root_topology_stable']}"
        ),
    )


# --- Market-claim components ------------------------------------------------


def score_zero_gamma_stability(
    inputs: ConfidenceInputs, config: ConfidenceConfig
) -> ConfidenceComponent:
    """How far apart the IV conventions place the level, as % of spot."""
    threshold, uncalibrated = _resolve(
        config.max_zero_gamma_shift_pct, PLACEHOLDER_MAX_ZERO_GAMMA_SHIFT_PCT
    )
    # `resolved` guarantees selected_root is not None, but narrowing has to be
    # explicit for the type checker -- and the explicit form is also what stops
    # a future refactor from quietly letting a None into the min/max.
    levels = [
        r.selected_root
        for r in _resolved_results(inputs)
        if r.selected_root is not None
    ]
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

    Without Cboe Open-Close there is no second model, so the component scores
    zero and says why. That is the correct answer, not a penalty-free pass: a
    single unverified sign model is precisely the risk worth surfacing.
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
    if naive * flow < 0.0:
        return ConfidenceComponent(
            name="sign_model_agreement",
            score=0.0,
            weight=config.weights.sign_model_agreement,
            detail=f"models disagree on sign: naive={naive:.3e} flow={flow:.3e}",
            uncalibrated=uncalibrated,
        )
    scale = max(abs(naive), abs(flow))
    disagreement = abs(naive - flow) / scale if scale > 0.0 else 0.0
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
    the SPX complex. It *is* a reason to distrust the chain-wide aggregate.
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
    return ConfidenceComponent(
        name="0dte_dominance_alert",
        score=_linear_decay(excess, zero_at=max(1e-9, 1.0 - threshold)),
        weight=config.weights.dte0_dominance_alert,
        detail=(
            f"{ExpiryBucket.DTE_0.value} share {ratio:.4f} of unsigned GEX "
            f"(alert above {threshold:.4f})"
        ),
        uncalibrated=uncalibrated,
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
    score_multiple_root_penalty,
    score_root_slope,
    score_root_boundary,
    score_root_identity_stability,
    score_timestamp_alignment,
    score_future_timestamp,
    score_option_universe_coverage,
    score_iv_spread_quality,
    score_model_parameter_completeness,
)

COMPONENT_NAMES: tuple[str, ...] = (
    "chain_completeness",
    "quote_freshness",
    "oi_freshness",
    "crossed_market_penalty",
    "zero_gamma_stability",
    "sign_model_agreement",
    "0dte_dominance_alert",
    "vendor_lag_alert",
    "multiple_root_penalty",
    "root_slope_score",
    "root_boundary_penalty",
    "root_identity_stability",
    "timestamp_alignment_score",
    "future_timestamp_penalty",
    "option_universe_coverage_score",
    "iv_spread_quality",
    "model_parameter_completeness",
)


def compute_confidence(
    inputs: ConfidenceInputs, config: ConfidenceConfig | None = None
) -> ConfidenceScore:
    """Weighted 0-100 score over all components, plus warnings and hard failures.

    A hard failure zeroes the whole score rather than merely reducing it. Letting
    a snapshot with a future-dated feed score 85/100 because everything else was
    fine would be exactly the kind of averaged-away warning this model exists to
    prevent.
    """
    cfg = config or ConfidenceConfig()
    components = tuple(scorer(inputs, cfg) for scorer in _SCORERS)
    total_weight = sum(c.weight for c in components)
    if total_weight <= 0.0:
        raise ValueError("confidence weights sum to zero")

    weighted = sum(c.score * c.weight for c in components) / total_weight
    value = round(100.0 * min(max(weighted, 0.0), 1.0), 4)
    if math.isnan(value):  # pragma: no cover - defensive
        raise ValueError("confidence score is NaN")

    warnings: list[str] = []
    hard = [c for c in components if c.hard_failure]
    if hard:
        value = 0.0
        warnings.extend(f"HARD FAILURE {c.name}: {c.detail}" for c in hard)
    warnings.extend(
        f"{c.name} is uncalibrated: {c.detail}" for c in components if c.uncalibrated
    )
    return ConfidenceScore(value=value, components=components, warnings=tuple(warnings))
