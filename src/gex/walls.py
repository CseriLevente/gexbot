"""Structural level extraction from the strike-level GEX view.

Everything here reads strike-aggregated *gamma*, never raw open interest. A
strike can carry enormous OI in far-dated series that contribute almost no
gamma; calling that a "wall" points the strategy at a level with no hedging
pressure behind it.
"""

from __future__ import annotations

from src.domain.gex import GammaVoid, StrikeGex, WallSet
from src.gex.config import WallConfig


def _in_band(
    strikes: tuple[StrikeGex, ...], spot: float, band_pct: float
) -> tuple[StrikeGex, ...]:
    """Restrict to strikes within +/- ``band_pct`` of spot.

    Without this, a single deep-wing strike with a large gamma print can win the
    wall selection and drag every distance feature with it.
    """
    low, high = spot * (1.0 - band_pct), spot * (1.0 + band_pct)
    return tuple(s for s in strikes if low <= s.strike <= high)


def find_gamma_voids(
    strikes: tuple[StrikeGex, ...],
    *,
    spot: float,
    config: WallConfig,
) -> tuple[GammaVoid, ...]:
    """Contiguous strike runs carrying almost no gamma.

    The range reported spans the void strikes themselves. Price tends to cross
    these quickly because there is little dealer re-hedging to absorb it, so they
    read as travel space rather than as support or resistance.
    """
    if not strikes:
        return ()
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
        if high - low >= min_width:
            voids.append(
                GammaVoid(
                    low_strike=low,
                    high_strike=high,
                    max_unsigned_gex_in_range=max(s.unsigned_gex for s in run),
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
) -> WallSet:
    """Derive walls, gamma nodes and voids from the strike-level view."""
    cfg = config or WallConfig()
    banded = _in_band(strikes, spot, cfg.band_pct)
    if not banded:
        return WallSet(call_wall=None, put_wall=None, largest_abs_gamma_strike=None)

    call_candidates = [s for s in banded if s.call_gex > 0.0]
    put_candidates = [s for s in banded if s.put_gex > 0.0]

    call_wall = (
        max(call_candidates, key=lambda s: s.call_gex).strike
        if call_candidates
        else None
    )
    put_wall = (
        max(put_candidates, key=lambda s: s.put_gex).strike if put_candidates else None
    )
    largest = max(banded, key=lambda s: s.unsigned_gex)
    largest_strike = largest.strike if largest.unsigned_gex > 0.0 else None

    max_unsigned = max(s.unsigned_gex for s in banded)
    node_floor = max_unsigned * cfg.node_min_share_of_max

    def top_nodes(positive: bool) -> tuple[float, ...]:
        selected = [
            s
            for s in banded
            if s.unsigned_gex >= node_floor
            and (s.signed_gex > 0.0 if positive else s.signed_gex < 0.0)
        ]
        selected.sort(key=lambda s: abs(s.signed_gex), reverse=True)
        return tuple(s.strike for s in selected[: cfg.max_nodes_per_side])

    return WallSet(
        call_wall=call_wall,
        put_wall=put_wall,
        largest_abs_gamma_strike=largest_strike,
        positive_gamma_nodes=top_nodes(positive=True),
        negative_gamma_nodes=top_nodes(positive=False),
        gamma_voids=find_gamma_voids(banded, spot=spot, config=cfg),
    )


def distance_pct(spot: float, level: float | None) -> float | None:
    """Signed distance from spot to a level, as a percentage of spot.

    Positive means the level sits above spot. Feature-store fields
    ``spot_to_*_distance_pct`` are produced by this helper so the sign
    convention cannot drift between call side and put side.
    """
    if level is None or spot <= 0.0:
        return None
    return (level - spot) / spot * 100.0
