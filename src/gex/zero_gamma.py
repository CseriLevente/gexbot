"""View 5: the zero-gamma spot grid.

Algorithm, per the plan:

1. Build a symmetric spot grid around the current spot.
2. Reprice every contract's gamma at each grid point under a stated IV
   convention.
3. Aggregate signed GEX at each point.
4. Interpolate where the aggregate changes sign.

Step 2 is where the answer is actually decided, which is why the convention is a
first-class parameter and why the engine runs several of them. The disagreement
between conventions is not noise to be averaged away -- it is the honest error
bar on the level, and it feeds ``zero_gamma_stability`` in the confidence score.

The four conventions:

``FROZEN_IV``
    Each contract keeps its raw snapshot IV. Baseline, and the only one that
    involves no fitting.
``STICKY_STRIKE``
    IV stays pinned to the strike, but is read off a fitted per-expiry smile
    rather than the raw quote. Same spot-invariance as ``FROZEN_IV``, less
    sensitive to one torn quote. The plan's default research convention.
``STICKY_DELTA``
    The smile translates with spot: a contract's IV follows its *new*
    standardised moneyness as the grid moves.
``SURFACE_REFIT``
    Explicitly out of scope for v1. Requested runs return an unresolved result
    carrying a reason instead of a fabricated number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.domain.gex import IVConvention, ZeroGammaResult
from src.gex.config import ZeroGammaConfig
from src.gex.formulas import ContractGex
from src.gex.pricing import (
    MAX_IMPLIED_VOL,
    MIN_IMPLIED_VOL,
    BlackScholesInputs,
    gamma as bs_gamma,
)

_QUADRATIC_TERMS = 3


def _solve_symmetric_3x3(
    matrix: list[list[float]], rhs: list[float]
) -> tuple[float, float, float] | None:
    """Gaussian elimination with partial pivoting. ``None`` when singular."""
    a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(_QUADRATIC_TERMS):
        pivot_row = max(range(col, _QUADRATIC_TERMS), key=lambda r: abs(a[r][col]))
        if abs(a[pivot_row][col]) < 1e-12:
            return None
        a[col], a[pivot_row] = a[pivot_row], a[col]
        pivot = a[col][col]
        for row in range(col + 1, _QUADRATIC_TERMS):
            factor = a[row][col] / pivot
            for k in range(col, _QUADRATIC_TERMS + 1):
                a[row][k] -= factor * a[col][k]
    out = [0.0] * _QUADRATIC_TERMS
    for row in reversed(range(_QUADRATIC_TERMS)):
        total = a[row][_QUADRATIC_TERMS] - sum(
            a[row][k] * out[k] for k in range(row + 1, _QUADRATIC_TERMS)
        )
        out[row] = total / a[row][row]
    return out[0], out[1], out[2]


@dataclass(frozen=True, slots=True)
class SmileFit:
    """Least-squares quadratic in standardised moneyness.

    ``sigma(m) = a + b*m + c*m^2``, with ``m = ln(K/S) / sqrt(T)``.

    A quadratic captures level, skew and curvature -- enough structure to be a
    real smile, few enough parameters that a handful of torn quotes cannot bend
    it. Output is clamped to the pricer's usable vol range because a quadratic
    extrapolates badly in the far wings.
    """

    a: float
    b: float
    c: float

    def at(self, moneyness: float) -> float:
        value = self.a + self.b * moneyness + self.c * moneyness * moneyness
        return min(max(value, MIN_IMPLIED_VOL), MAX_IMPLIED_VOL)


def standardised_moneyness(strike: float, spot: float, time_to_expiry: float) -> float:
    if spot <= 0.0 or strike <= 0.0 or time_to_expiry <= 0.0:
        return 0.0
    return math.log(strike / spot) / math.sqrt(time_to_expiry)


def fit_smile(points: list[tuple[float, float]]) -> SmileFit | None:
    """Fit ``sigma(m)`` from ``(moneyness, implied_vol)`` pairs."""
    if len(points) < _QUADRATIC_TERMS:
        return None
    n = float(len(points))
    s1 = s2 = s3 = s4 = 0.0
    t0 = t1 = t2 = 0.0
    for m, sigma in points:
        m2 = m * m
        s1 += m
        s2 += m2
        s3 += m2 * m
        s4 += m2 * m2
        t0 += sigma
        t1 += m * sigma
        t2 += m2 * sigma
    solution = _solve_symmetric_3x3(
        [[n, s1, s2], [s1, s2, s3], [s2, s3, s4]], [t0, t1, t2]
    )
    return None if solution is None else SmileFit(*solution)


def fit_smiles_by_expiry(
    contracts: tuple[ContractGex, ...],
    *,
    spot: float,
    min_points: int,
) -> dict[tuple[str, float], SmileFit]:
    """One smile per (expiry, time-to-expiry) group.

    Keyed by expiry ISO date plus ``time_to_expiry`` so SPX and SPXW series that
    nominally share an expiry date but settle at different times -- AM vs PM --
    never get pooled into one smile.
    """
    grouped: dict[tuple[str, float], list[tuple[float, float]]] = {}
    for c in contracts:
        if c.implied_vol is None or c.implied_vol <= 0.0:
            continue
        key = (c.contract.expiry.isoformat(), c.time_to_expiry)
        moneyness = standardised_moneyness(
            c.contract.strike, spot, c.time_to_expiry
        )
        grouped.setdefault(key, []).append((moneyness, c.implied_vol))

    fits: dict[tuple[str, float], SmileFit] = {}
    for key, points in grouped.items():
        if len(points) < min_points:
            continue
        fit = fit_smile(points)
        if fit is not None:
            fits[key] = fit
    return fits


def build_spot_grid(spot: float, *, span_pct: float, step_pct: float) -> list[float]:
    """Symmetric grid around spot, guaranteed to include spot itself."""
    if span_pct <= 0.0 or step_pct <= 0.0:
        raise ValueError("span_pct and step_pct must be positive")
    steps = int(round(span_pct / step_pct))
    return [spot * (1.0 + i * step_pct) for i in range(-steps, steps + 1)]


def _iv_at_grid_point(
    contract: ContractGex,
    *,
    convention: IVConvention,
    grid_spot: float,
    base_spot: float,
    fits: dict[tuple[str, float], SmileFit],
) -> float | None:
    if convention is IVConvention.FROZEN_IV:
        return contract.implied_vol

    key = (contract.contract.expiry.isoformat(), contract.time_to_expiry)
    fit = fits.get(key)
    if fit is None:
        # Not enough smile points for this expiry: fall back to the raw IV rather
        # than dropping the contract, and let chain_completeness stay honest.
        return contract.implied_vol

    if convention is IVConvention.STICKY_STRIKE:
        moneyness = standardised_moneyness(
            contract.contract.strike, base_spot, contract.time_to_expiry
        )
    else:  # STICKY_DELTA -- the smile travels with spot
        moneyness = standardised_moneyness(
            contract.contract.strike, grid_spot, contract.time_to_expiry
        )
    return fit.at(moneyness)


def signed_gex_curve(
    contracts: tuple[ContractGex, ...],
    *,
    grid: list[float],
    base_spot: float,
    convention: IVConvention,
    spot_move_pct: float,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
    min_points_for_smile_fit: int = 5,
) -> list[tuple[float, float]]:
    """Total signed GEX at each grid point.

    Note the two roles of spot in the notional: gamma is evaluated *at* the grid
    point, and the ``S * dS`` scaling also uses the grid point. Holding the
    scaling at the original spot would tilt the curve and move the crossing.
    """
    fits = (
        fit_smiles_by_expiry(
            contracts, spot=base_spot, min_points=min_points_for_smile_fit
        )
        if convention is not IVConvention.FROZEN_IV
        else {}
    )

    # Per-contract invariants, hoisted out of the grid loop.
    prepared: list[tuple[ContractGex, float, float]] = [
        (c, c.open_interest * c.contract.multiplier * c.sign, c.time_to_expiry)
        for c in contracts
    ]

    curve: list[tuple[float, float]] = []
    for grid_spot in grid:
        notional_scale = grid_spot * (spot_move_pct * grid_spot)
        total = 0.0
        for contract, scale, time_to_expiry in prepared:
            iv = _iv_at_grid_point(
                contract,
                convention=convention,
                grid_spot=grid_spot,
                base_spot=base_spot,
                fits=fits,
            )
            if iv is None or iv <= 0.0:
                continue
            gamma = bs_gamma(
                BlackScholesInputs(
                    spot=grid_spot,
                    strike=contract.contract.strike,
                    time_to_expiry=time_to_expiry,
                    implied_vol=iv,
                    rate=risk_free_rate,
                    dividend_yield=dividend_yield,
                )
            )
            total += gamma * scale * notional_scale
        curve.append((grid_spot, total))
    return curve


def find_sign_change_roots(curve: list[tuple[float, float]]) -> list[float]:
    """Linearly interpolated zero crossings, in grid order.

    An identically-zero curve has no crossing. Without this guard an empty or
    fully-filtered contract set would report a "root" at every single grid point
    and the nearest-to-spot pick would then hand back spot itself -- a zero-gamma
    level that looks perfectly plausible and is entirely fabricated.
    """
    if not any(value != 0.0 for _, value in curve):
        return []
    roots: list[float] = []
    for (s1, y1), (s2, y2) in zip(curve, curve[1:]):
        if y1 == 0.0:
            roots.append(s1)
            continue
        if y1 * y2 < 0.0:
            roots.append(s1 + (s2 - s1) * (-y1) / (y2 - y1))
    if curve and curve[-1][1] == 0.0:
        roots.append(curve[-1][0])
    return roots


def compute_zero_gamma(
    contracts: tuple[ContractGex, ...],
    *,
    spot: float,
    convention: IVConvention,
    spot_move_pct: float,
    config: ZeroGammaConfig | None = None,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
) -> ZeroGammaResult:
    """Locate the zero-gamma spot level under one IV convention.

    When the curve crosses zero more than once the crossing nearest spot is
    returned, and ``sign_changes`` records that the level was ambiguous. A
    multi-crossing snapshot is a legitimate market state, not an error, but a
    strategy should treat the level as soft -- hence surfacing the count rather
    than hiding it.
    """
    cfg = config or ZeroGammaConfig()
    grid = build_spot_grid(
        spot, span_pct=cfg.grid_span_pct, step_pct=cfg.grid_step_pct
    )

    if convention is IVConvention.SURFACE_REFIT:
        return ZeroGammaResult(
            convention=convention,
            zero_gamma_spot=None,
            grid_low=grid[0],
            grid_high=grid[-1],
            grid_points=len(grid),
            no_crossing=True,
        )

    eligible = tuple(c for c in contracts if c.dte <= cfg.max_dte_for_grid)
    curve = signed_gex_curve(
        eligible,
        grid=grid,
        base_spot=spot,
        convention=convention,
        spot_move_pct=spot_move_pct,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
        min_points_for_smile_fit=cfg.min_points_for_smile_fit,
    )
    roots = find_sign_change_roots(curve)
    nearest = min(roots, key=lambda r: abs(r - spot)) if roots else None

    return ZeroGammaResult(
        convention=convention,
        zero_gamma_spot=nearest,
        grid_low=grid[0],
        grid_high=grid[-1],
        grid_points=len(grid),
        curve=tuple(curve),
        sign_changes=len(roots),
        no_crossing=not roots,
    )
