"""Structural level extraction from the strike-level GEX view.

Two things this module is careful about:

**Gamma, not open interest.** A strike can carry enormous OI in far-dated series
that contribute almost no gamma; calling that a "wall" points the strategy at a
level with no hedging pressure behind it.

**Observation, not interpretation.** The strike with the most call gamma is a
fact. "Resistance above" is a claim, and it is only true if that strike is
actually above spot. The two are reported separately so the second can be
``None`` when nothing qualifies, instead of silently degrading into the first.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from statistics import median

from src.domain.gex import GammaVoid, GammaVoidKind, StrikeGex, WallSet
from src.gex.config import WallConfig


@dataclass(frozen=True, slots=True)
class StrikeLadder:
    """The strikes a complete chain would be expected to contain.

    Inferred from the observed spacing rather than assumed, because SPX mixes
    5-point strikes near the money with 25-point strikes further out, and a
    hard-coded ladder would report the wide region as permanently missing.
    """

    observed: tuple[float, ...]
    modal_spacing: float | None

    @classmethod
    def from_strikes(cls, strikes: tuple[float, ...]) -> StrikeLadder:
        ordered = tuple(sorted(set(strikes)))
        if len(ordered) < 3:
            return cls(observed=ordered, modal_spacing=None)
        gaps = [round(b - a, 6) for a, b in itertools.pairwise(ordered) if b > a]
        if not gaps:
            return cls(observed=ordered, modal_spacing=None)
        # Median rather than mode: robust to a handful of missing strikes, which
        # is exactly the situation this ladder exists to detect.
        return cls(observed=ordered, modal_spacing=median(gaps))

    def expected_count_between(self, low: float, high: float) -> int:
        if self.modal_spacing is None or self.modal_spacing <= 0.0:
            return 0
        return round((high - low) / self.modal_spacing) + 1

    def observed_count_between(self, low: float, high: float) -> int:
        return sum(1 for strike in self.observed if low <= strike <= high)

    def coverage_between(self, low: float, high: float) -> float | None:
        expected = self.expected_count_between(low, high)
        if expected <= 0:
            return None
        return min(self.observed_count_between(low, high) / expected, 1.0)


def _in_band(
    strikes: tuple[StrikeGex, ...], spot: float, band_pct: float
) -> tuple[StrikeGex, ...]:
    """Restrict to strikes within +/- ``band_pct`` of spot.

    Without this, a single deep-wing strike with a large gamma print can win the
    selection and drag every distance feature with it.
    """
    low, high = spot * (1.0 - band_pct), spot * (1.0 + band_pct)
    return tuple(s for s in strikes if low <= s.strike <= high)


def classify_void(
    *,
    run: tuple[StrikeGex, ...],
    ladder: StrikeLadder,
    config: WallConfig,
    filtered_region: bool = False,
) -> tuple[GammaVoidKind, str, int]:
    """Decide whether a quiet strike range is structure or absent data.

    Returns ``(kind, detail, missing_strike_count)``. The ordering of the checks
    is the point: a region can be both sparse *and* low-gamma, and in that case
    the sparse explanation must win, because acting on it as tradable structure
    would be acting on data that was never received.
    """
    low, high = run[0].strike, run[-1].strike
    expected = ladder.expected_count_between(low, high)
    observed = len(run)
    missing = max(0, expected - observed)

    if filtered_region:
        return (
            GammaVoidKind.FILTERED_STRIKE_REGION,
            "region lies outside the configured strike band",
            missing,
        )

    if ladder.modal_spacing is None:
        return (
            GammaVoidKind.INSUFFICIENT_COVERAGE,
            "not enough strikes to infer an expected ladder",
            missing,
        )

    coverage = observed / expected if expected > 0 else 0.0
    if coverage < config.min_ladder_coverage_for_true_void:
        # Sparse. Two very different causes look identical at first glance, so
        # they are separated by whether the wide spacing REPEATS:
        #
        #   * A single wide gap in an otherwise 25-point region is an omission --
        #     the vendor did not send those strikes.
        #   * Several consecutive gaps of the same wider size are a genuinely
        #     coarser increment, which SPX really does use in the far wings.
        #
        # One gap is never enough evidence for "the ladder changed here", so it
        # is reported as missing data. That is the safe direction: a
        # missing-data region is not tradable structure, whereas an irregular
        # one carries no such warning.
        internal_gaps = [
            round(b.strike - a.strike, 6) for a, b in itertools.pairwise(run)
        ]
        wide = ladder.modal_spacing * config.irregular_spacing_factor
        uniformly_wider = (
            len(internal_gaps) >= 2
            and min(internal_gaps) > wide
            and max(internal_gaps) - min(internal_gaps) < 1e-6
        )
        if uniformly_wider:
            return (
                GammaVoidKind.IRREGULAR_STRIKE_SPACING,
                (
                    f"{len(internal_gaps)} consecutive gaps of "
                    f"{internal_gaps[0]:g} against a modal "
                    f"{ladder.modal_spacing:g} -- a coarser increment, not omissions"
                ),
                missing,
            )
        return (
            GammaVoidKind.MISSING_STRIKE_DATA,
            (
                f"only {observed} of an expected {expected} strikes present "
                f"({coverage:.0%} coverage) -- absence of data, not absence of gamma"
            ),
            missing,
        )

    if observed < config.min_observed_strikes_for_true_void:
        return (
            GammaVoidKind.INSUFFICIENT_COVERAGE,
            f"only {observed} strike(s) observed in the range",
            missing,
        )

    return (
        GammaVoidKind.TRUE_LOW_GEX_VOID,
        f"{observed} strikes present, all below the void threshold",
        missing,
    )


def find_gamma_voids(
    strikes: tuple[StrikeGex, ...],
    *,
    spot: float,
    config: WallConfig,
    ladder: StrikeLadder | None = None,
) -> tuple[GammaVoid, ...]:
    """Contiguous strike runs carrying almost no gamma, each classified.

    A ``TRUE_LOW_GEX_VOID`` reads as travel space: little dealer re-hedging to
    absorb a move through it. Every other classification is a data artefact and
    ``is_tradable_structure`` is False for it.
    """
    if not strikes:
        return ()
    active_ladder = ladder or StrikeLadder.from_strikes(
        tuple(s.strike for s in strikes)
    )
    max_unsigned = max(s.unsigned_gex for s in strikes)
    if max_unsigned <= 0.0:
        return ()

    threshold = max_unsigned * config.void_max_share_of_max
    min_width = spot * config.void_min_width_pct

    voids: list[GammaVoid] = []
    run: list[StrikeGex] = []

    def flush() -> None:
        if len(run) < 2:
            return
        low, high = run[0].strike, run[-1].strike
        if high - low < min_width:
            return
        kind, detail, missing = classify_void(
            run=tuple(run), ladder=active_ladder, config=config
        )
        voids.append(
            GammaVoid(
                low_strike=low,
                high_strike=high,
                max_unsigned_gex_in_range=max(s.unsigned_gex for s in run),
                kind=kind,
                detail=detail,
                missing_strike_count=missing,
                observed_strike_count=len(run),
            )
        )

    for entry in strikes:
        if entry.unsigned_gex <= threshold:
            run.append(entry)
        else:
            flush()
            run = []
    flush()
    return tuple(voids)


def extract_walls(
    strikes: tuple[StrikeGex, ...],
    *,
    spot: float,
    config: WallConfig | None = None,
    full_ladder: StrikeLadder | None = None,
) -> WallSet:
    """Derive neutral maxima, directional walls, nodes and classified voids."""
    cfg = config or WallConfig()
    banded = _in_band(strikes, spot, cfg.band_pct)
    if not banded:
        return WallSet(
            largest_call_gamma_strike=None,
            largest_put_gamma_strike=None,
            largest_unsigned_gamma_strike=None,
        )

    # --- Neutral observations ---
    call_candidates = [s for s in banded if s.call_gex > 0.0]
    put_candidates = [s for s in banded if s.put_gex > 0.0]
    largest_call = _argmax(call_candidates, key=lambda s: s.call_gex)
    largest_put = _argmax(put_candidates, key=lambda s: s.put_gex)
    largest_unsigned = _argmax(
        [s for s in banded if s.unsigned_gex > 0.0], key=lambda s: s.unsigned_gex
    )

    # --- Directional interpretations ---
    upside = _directional_wall(
        banded, spot=spot, above=True, key=lambda s: s.call_gex, config=cfg
    )
    downside = _directional_wall(
        banded, spot=spot, above=False, key=lambda s: s.put_gex, config=cfg
    )

    max_unsigned = max(s.unsigned_gex for s in banded)
    node_floor = max_unsigned * cfg.node_min_share_of_max

    def top_nodes(positive: bool) -> tuple[float, ...]:
        selected = [
            s
            for s in banded
            if s.unsigned_gex >= node_floor
            and (s.signed_gex > 0.0 if positive else s.signed_gex < 0.0)
        ]
        # Sort by magnitude, then by strike, so equal maxima are ordered
        # deterministically rather than by input order.
        selected.sort(key=lambda s: (-abs(s.signed_gex), s.strike))
        return tuple(s.strike for s in selected[: cfg.max_nodes_per_side])

    ladder = full_ladder or StrikeLadder.from_strikes(tuple(s.strike for s in strikes))

    return WallSet(
        largest_call_gamma_strike=largest_call,
        largest_put_gamma_strike=largest_put,
        largest_unsigned_gamma_strike=largest_unsigned,
        upside_call_wall=upside,
        downside_put_wall=downside,
        positive_gamma_nodes=top_nodes(positive=True),
        negative_gamma_nodes=top_nodes(positive=False),
        gamma_voids=find_gamma_voids(banded, spot=spot, config=cfg, ladder=ladder),
    )


def _argmax(
    candidates: list[StrikeGex], *, key: Callable[[StrikeGex], float]
) -> float | None:
    """Strike of the maximum, ties broken by the lower strike.

    Deterministic tie-breaking matters for replay: ``max()`` returns whichever
    equal element it met first, which depends on aggregation order.
    """
    if not candidates:
        return None
    best = max(key(s) for s in candidates)
    if best <= 0.0:
        return None
    return min(s.strike for s in candidates if key(s) == best)


def _directional_wall(
    banded: tuple[StrikeGex, ...],
    *,
    spot: float,
    above: bool,
    key: Callable[[StrikeGex], float],
    config: WallConfig,
) -> float | None:
    """Largest gamma strike strictly on the requested side of spot.

    Returns ``None`` rather than falling back to the other side. A "resistance"
    level below the market is not resistance, and silently supplying one is worse
    than supplying nothing.
    """
    buffer = spot * config.directional_wall_min_distance_pct
    band_low = spot * (1.0 - config.directional_wall_band_pct)
    band_high = spot * (1.0 + config.directional_wall_band_pct)

    if above:
        candidates = [
            s
            for s in banded
            if s.strike > spot + buffer and s.strike <= band_high and key(s) > 0.0
        ]
    else:
        candidates = [
            s
            for s in banded
            if s.strike < spot - buffer and s.strike >= band_low and key(s) > 0.0
        ]
    return _argmax(candidates, key=key)


def distance_pct(spot: float, level: float | None) -> float | None:
    """Signed distance from spot to a level, as a percentage of spot.

    Positive means the level sits above spot. Feature-store fields
    ``spot_to_*_distance_pct`` are produced by this helper so the sign convention
    cannot drift between call side and put side.
    """
    if level is None or spot <= 0.0:
        return None
    return (level - spot) / spot * 100.0
