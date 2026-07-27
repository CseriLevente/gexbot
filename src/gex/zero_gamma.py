"""View 5: the zero-gamma spot grid.

Algorithm:

1. Build a symmetric spot grid around the current spot.
2. Reprice every contract's gamma at each grid point under a stated IV
   convention.
3. Aggregate signed GEX at each point.
4. Find *every* sign change, interpolate each, and report the full set.

Step 4 is where this differs from the usual treatment. Returning a single
"the zero gamma level" hides three things that decide whether it means anything:

* **How many other crossings exist.** A book with open-interest bumps on both
  sides genuinely crosses more than once, and the nearest one is a reporting
  convention, not a discovery.
* **How steeply the curve passes through.** A shallow crossing moves a long way
  for a small data change; a steep isolated one does not.
* **Whether it sits at the grid edge**, where it may be an artefact of where we
  stopped looking rather than a real level.

The four IV conventions:

``FROZEN_IV``
    Each contract keeps its raw snapshot IV. Baseline, no fitting.
``STICKY_STRIKE``
    IV pinned to the strike, read off a fitted per-expiry smile rather than the
    raw quote. Same spot-invariance as ``FROZEN_IV``, less sensitive to one torn
    quote. The default research convention.
``STICKY_MONEYNESS``
    The fitted smile translates with spot: a contract's IV follows its new
    standardised log-moneyness as the grid moves. **Named for what it does.**
    This is an approximation to sticky-delta behaviour, not sticky-delta itself.
``STICKY_DELTA`` / ``SURFACE_REFIT``
    Not implemented. Requesting one returns an unresolved result carrying the
    reason, rather than quietly aliasing onto the approximation.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

from src.domain.gex import IVConvention, RootSelectionMethod, ZeroGammaResult
from src.gex.config import ZeroGammaConfig
from src.gex.formulas import ContractGex
from src.gex.pricing import (
    MAX_IMPLIED_VOL,
    MIN_IMPLIED_VOL,
)

_QUADRATIC_TERMS = 3


def _solve_symmetric_3x3(
    matrix: list[list[float]], rhs: list[float]
) -> tuple[float, float, float] | None:
    """Gaussian elimination with partial pivoting. ``None`` when singular."""
    a = [[*row[:], rhs[i]] for i, row in enumerate(matrix)]
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
        moneyness = standardised_moneyness(c.contract.strike, spot, c.time_to_expiry)
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
    """Symmetric grid around spot, guaranteed to include spot itself.

    The realised span is quantised to a whole number of steps, so it can differ
    slightly from ``span_pct`` (e.g. a 12.25% span at 0.1% steps becomes 12.3%).
    Keeping the step exact rather than the span is the right trade: the step is
    what interpolation accuracy depends on, and it must stay finer than the
    strike ladder.
    """
    if span_pct <= 0.0 or step_pct <= 0.0:
        raise ValueError("span_pct and step_pct must be positive")
    steps = round(span_pct / step_pct)
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
    else:  # STICKY_MONEYNESS -- the fitted smile travels with spot
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

    # Per-contract invariants, hoisted out of the grid loop. Only contracts that
    # resolved a usable effective model can be repriced -- see
    # ``zero_gamma_eligible`` in formulas.py. A contract carried on vendor gamma
    # with no IV has nothing to reprice, and silently pricing it at some default
    # would be inventing data.
    prepared: list[tuple[ContractGex, float]] = [
        (c, c.open_interest * c.contract.multiplier * c.sign)
        for c in contracts
        if c.effective.is_usable
    ]

    curve: list[tuple[float, float]] = []
    for grid_spot in grid:
        notional_scale = grid_spot * (spot_move_pct * grid_spot)
        total = 0.0
        for contract, scale in prepared:
            iv = _iv_at_grid_point(
                contract,
                convention=convention,
                grid_spot=grid_spot,
                base_spot=base_spot,
                fits=fits,
            )
            if iv is None or iv <= 0.0:
                continue
            # Reprice the contract's OWN effective model at the grid spot, so the
            # rate, dividend, day count and time floor are exactly those the
            # snapshot reports. Rebuilding BlackScholesInputs here is how the grid
            # used to drift away from the chain totals.
            gamma = contract.effective.reprice_at(
                grid_spot, implied_volatility=iv
            ).gamma()
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
    for (s1, y1), (s2, y2) in itertools.pairwise(curve):
        if y1 == 0.0:
            roots.append(s1)
            continue
        if y1 * y2 < 0.0:
            roots.append(s1 + (s2 - s1) * (-y1) / (y2 - y1))
    if curve and curve[-1][1] == 0.0:
        roots.append(curve[-1][0])
    return roots


def local_slope_at(curve: list[tuple[float, float]], root: float) -> float | None:
    """dGEX/dS across the grid interval containing ``root``.

    A finite difference on the bracketing points rather than an analytic
    derivative: the curve is only known on the grid, and pretending to more
    resolution than the grid has would overstate the precision of the slope.
    """
    if len(curve) < 2:
        return None
    for (s1, y1), (s2, y2) in itertools.pairwise(curve):
        if s1 <= root <= s2 and s2 > s1:
            return (y2 - y1) / (s2 - s1)
    # Root sits outside the grid interior (an endpoint zero); use the nearest
    # interval instead of reporting nothing.
    nearest = min(
        range(len(curve) - 1),
        key=lambda i: abs((curve[i][0] + curve[i + 1][0]) / 2 - root),
    )
    (s1, y1), (s2, y2) = curve[nearest], curve[nearest + 1]
    return (y2 - y1) / (s2 - s1) if s2 > s1 else None


def _nearest_spacing_pct(
    roots: list[float], selected: float, spot: float
) -> float | None:
    """Gap to the next-closest root, as a % of spot."""
    others = [r for r in roots if r != selected]
    if not others or spot <= 0.0:
        return None
    return min(abs(r - selected) for r in others) / spot * 100.0


def _is_near_boundary(root: float, *, grid: list[float], tolerance_pct: float) -> bool:
    low, high = grid[0], grid[-1]
    span = high - low
    if span <= 0.0:
        return True
    margin = span * tolerance_pct
    return root <= low + margin or root >= high - margin


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
    """Locate the zero-gamma spot level(s) under one IV convention.

    Every root is retained. ``selected_root`` is the nearest to spot *as a
    reporting convention* -- ``selection_method`` says so explicitly, so a
    consumer cannot mistake it for a claim that the others are irrelevant.

    When the selected root lands near the grid boundary the grid is widened and
    the search repeated, up to ``max_grid_expansions`` times. Bounded because an
    unbounded search on a one-sided book would widen until the float range gave
    out, and because a root only found 30% away from spot is not a level anyone
    should trade around anyway.
    """
    cfg = config or ZeroGammaConfig()

    if not convention.is_implemented:
        grid = build_spot_grid(
            spot, span_pct=cfg.grid_span_pct, step_pct=cfg.grid_step_pct
        )
        return ZeroGammaResult(
            convention=convention,
            selected_root=None,
            all_roots=(),
            selection_method=RootSelectionMethod.CONVENTION_UNIMPLEMENTED,
            grid_lower_bound=grid[0],
            grid_upper_bound=grid[-1],
            grid_points=len(grid),
            spot=spot,
            no_root_found=True,
            unimplemented_reason=convention.unimplemented_reason,
        )

    eligible = tuple(c for c in contracts if c.dte <= cfg.max_dte_for_grid)

    span_pct = cfg.grid_span_pct
    expansions = 0
    result: ZeroGammaResult | None = None

    while True:
        grid = build_spot_grid(spot, span_pct=span_pct, step_pct=cfg.grid_step_pct)
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
        result = _build_result(
            convention=convention,
            curve=curve,
            grid=grid,
            spot=spot,
            config=cfg,
            expansions=expansions,
        )
        should_expand = (
            expansions < cfg.max_grid_expansions
            and (result.root_near_boundary or result.no_root_found)
            and not result.identically_zero_curve
        )
        if not should_expand:
            return result
        span_pct *= cfg.grid_expansion_factor
        expansions += 1


def _build_result(
    *,
    convention: IVConvention,
    curve: list[tuple[float, float]],
    grid: list[float],
    spot: float,
    config: ZeroGammaConfig,
    expansions: int,
) -> ZeroGammaResult:
    values = [value for _, value in curve]
    identically_zero = bool(values) and not any(value != 0.0 for value in values)
    max_abs = max((abs(value) for value in values), default=0.0)
    roots = find_sign_change_roots(curve)

    if identically_zero:
        method = RootSelectionMethod.CURVE_IDENTICALLY_ZERO
    elif roots:
        method = RootSelectionMethod.NEAREST_TO_SPOT
    else:
        method = RootSelectionMethod.NONE_FOUND

    selected = min(roots, key=lambda r: abs(r - spot)) if roots else None

    return ZeroGammaResult(
        convention=convention,
        selected_root=selected,
        all_roots=tuple(roots),
        selection_method=method,
        grid_lower_bound=grid[0],
        grid_upper_bound=grid[-1],
        grid_points=len(grid),
        spot=spot,
        curve=tuple(curve),
        local_slope_at_selected_root=(
            local_slope_at(curve, selected) if selected is not None else None
        ),
        nearest_root_spacing_pct=(
            _nearest_spacing_pct(roots, selected, spot)
            if selected is not None
            else None
        ),
        root_near_boundary=(
            _is_near_boundary(
                selected, grid=grid, tolerance_pct=config.boundary_tolerance_pct
            )
            if selected is not None
            else False
        ),
        identically_zero_curve=identically_zero,
        no_root_found=not roots,
        max_abs_gex_on_grid=max_abs,
        grid_expansions=expansions,
    )
