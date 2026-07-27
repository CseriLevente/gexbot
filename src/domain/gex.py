"""GEX engine output types.

Five separate GEX views are written into one ``GexSnapshot``, precisely so that
no downstream consumer can mistake a single number for the truth. These
dataclasses enforce that shape, and carry the metadata that makes each number
interpretable: which model produced it, which contracts it covers, and how
confident the diagnostics are.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any

from src.domain.model_spec import ModelSpec
from src.domain.validation import ValidationReport

# Significant figures retained when hashing a snapshot. Twelve is far tighter
# than any change of substance while staying immune to last-bit summation
# differences between platforms.
HASH_SIGNIFICANT_DIGITS = 12


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
    """

    DEALER_LONG_CALLS_SHORT_PUTS = "dealer_long_calls_short_puts"
    DEALER_SHORT_CALLS_LONG_PUTS = "dealer_short_calls_long_puts"
    FLOW_ADJUSTED = "flow_adjusted"


class IVConvention(str, Enum):
    """Volatility convention used when repricing on the zero-gamma spot grid.

    ``STICKY_MONEYNESS`` is named for what it actually does: it translates a
    smile fitted in *log-moneyness* as spot moves. It is deliberately NOT called
    ``STICKY_DELTA``. A true sticky-delta model parameterises the surface in
    delta coordinates, which requires an iterative solve because delta itself
    depends on the volatility being solved for. Naming an approximation after the
    method it approximates is how a known simplification quietly becomes an
    assumed capability.

    ``STICKY_DELTA`` is declared here so it can be requested and explicitly
    refused, rather than silently aliased onto the approximation.
    """

    FROZEN_IV = "frozen_iv"
    STICKY_STRIKE = "sticky_strike"
    STICKY_MONEYNESS = "sticky_moneyness"
    STICKY_DELTA = "sticky_delta"
    SURFACE_REFIT = "surface_refit"

    @property
    def is_implemented(self) -> bool:
        return self in (
            IVConvention.FROZEN_IV,
            IVConvention.STICKY_STRIKE,
            IVConvention.STICKY_MONEYNESS,
        )

    @property
    def unimplemented_reason(self) -> str | None:
        if self is IVConvention.STICKY_DELTA:
            return (
                "true sticky-delta requires a delta-coordinate surface solved "
                "iteratively (delta depends on the IV being solved for); the "
                "log-moneyness approximation is available as STICKY_MONEYNESS"
            )
        if self is IVConvention.SURFACE_REFIT:
            return "full per-grid-point surface re-estimation is not implemented"
        return None


class RootSelectionMethod(str, Enum):
    NEAREST_TO_SPOT = "nearest_to_spot"
    NONE_FOUND = "none_found"
    CURVE_IDENTICALLY_ZERO = "curve_identically_zero"
    CONVENTION_UNIMPLEMENTED = "convention_unimplemented"


class GammaVoidKind(str, Enum):
    """Why a strike range looks empty.

    The distinction that matters: a genuinely low-gamma region is traversable
    space, whereas a region where the vendor simply did not send strikes is an
    artefact. Trading the second as if it were the first is acting on absent data.
    """

    TRUE_LOW_GEX_VOID = "TRUE_LOW_GEX_VOID"
    MISSING_STRIKE_DATA = "MISSING_STRIKE_DATA"
    FILTERED_STRIKE_REGION = "FILTERED_STRIKE_REGION"
    IRREGULAR_STRIKE_SPACING = "IRREGULAR_STRIKE_SPACING"
    INSUFFICIENT_COVERAGE = "INSUFFICIENT_COVERAGE"

    @property
    def is_tradable_structure(self) -> bool:
        return self is GammaVoidKind.TRUE_LOW_GEX_VOID


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
    """A contiguous strike range with unusually little gamma, and why."""

    low_strike: float
    high_strike: float
    max_unsigned_gex_in_range: float
    kind: GammaVoidKind
    detail: str = ""
    # Strikes the expected ladder says should exist here but do not.
    missing_strike_count: int = 0
    observed_strike_count: int = 0

    @property
    def width(self) -> float:
        return self.high_strike - self.low_strike

    @property
    def is_tradable_structure(self) -> bool:
        return self.kind.is_tradable_structure

    def as_dict(self) -> dict[str, Any]:
        return {
            "low_strike": self.low_strike,
            "high_strike": self.high_strike,
            "width": self.width,
            "kind": self.kind.value,
            "detail": self.detail,
            "max_unsigned_gex_in_range": self.max_unsigned_gex_in_range,
            "missing_strike_count": self.missing_strike_count,
            "observed_strike_count": self.observed_strike_count,
        }


@dataclass(frozen=True, slots=True)
class WallSet:
    """Structural levels from strike-aggregated gamma, never from raw OI.

    Neutral facts and directional interpretations are kept separate on purpose.
    ``largest_call_gamma_strike`` is an observation. ``upside_call_wall`` is a
    claim that price would meet resistance there, and that claim is only
    meaningful if the strike is actually above spot. Collapsing the two -- calling
    the largest call-gamma strike "the call wall" regardless of where spot is --
    produces a resistance level below the market, which is not resistance.
    """

    # --- Neutral observations ---
    largest_call_gamma_strike: float | None
    largest_put_gamma_strike: float | None
    largest_unsigned_gamma_strike: float | None

    # --- Directional interpretations (None when no qualifying strike exists) ---
    upside_call_wall: float | None = None
    downside_put_wall: float | None = None

    positive_gamma_nodes: tuple[float, ...] = ()
    negative_gamma_nodes: tuple[float, ...] = ()
    gamma_voids: tuple[GammaVoid, ...] = ()

    @property
    def tradable_voids(self) -> tuple[GammaVoid, ...]:
        return tuple(void for void in self.gamma_voids if void.is_tradable_structure)

    def as_dict(self) -> dict[str, Any]:
        return {
            "largest_call_gamma_strike": self.largest_call_gamma_strike,
            "largest_put_gamma_strike": self.largest_put_gamma_strike,
            "largest_unsigned_gamma_strike": self.largest_unsigned_gamma_strike,
            "upside_call_wall": self.upside_call_wall,
            "downside_put_wall": self.downside_put_wall,
            "positive_gamma_nodes": list(self.positive_gamma_nodes),
            "negative_gamma_nodes": list(self.negative_gamma_nodes),
            "gamma_voids": [void.as_dict() for void in self.gamma_voids],
        }


@dataclass(frozen=True, slots=True)
class ZeroGammaResult:
    """Outcome of one zero-gamma grid search under one IV convention.

    Reporting only ``selected_root`` would hide the three things that decide
    whether the level means anything: how many other crossings exist, how steeply
    the curve passes through this one, and whether it sits at the edge of the
    search grid where it may be an artefact of where we stopped looking.
    """

    convention: IVConvention
    selected_root: float | None
    all_roots: tuple[float, ...]
    selection_method: RootSelectionMethod
    grid_lower_bound: float
    grid_upper_bound: float
    grid_points: int
    spot: float

    curve: tuple[tuple[float, float], ...] = ()
    # dGEX/dS at the selected root, in GEX units per index point. A shallow
    # crossing means a small data change moves the level a long way.
    local_slope_at_selected_root: float | None = None
    # Gap to the next-nearest root, as a % of spot. Small means the level is
    # ambiguous even within one convention.
    nearest_root_spacing_pct: float | None = None
    root_near_boundary: bool = False
    identically_zero_curve: bool = False
    no_root_found: bool = False
    max_abs_gex_on_grid: float = 0.0
    grid_expansions: int = 0
    unimplemented_reason: str | None = None

    @property
    def root_count(self) -> int:
        return len(self.all_roots)

    @property
    def resolved(self) -> bool:
        return self.selected_root is not None

    @property
    def selected_root_distance_from_spot_pct(self) -> float | None:
        if self.selected_root is None or self.spot <= 0.0:
            return None
        return (self.selected_root - self.spot) / self.spot * 100.0

    @property
    def normalised_slope(self) -> float | None:
        """Slope scaled by the curve's own magnitude, so it is comparable across
        chains of different size. Units: fraction of max |GEX| per 1% of spot.
        """
        if self.local_slope_at_selected_root is None or self.max_abs_gex_on_grid <= 0.0:
            return None
        per_one_pct = self.local_slope_at_selected_root * (self.spot / 100.0)
        return per_one_pct / self.max_abs_gex_on_grid

    def as_dict(self) -> dict[str, Any]:
        return {
            "convention": self.convention.value,
            "selected_root": self.selected_root,
            "all_roots": list(self.all_roots),
            "root_count": self.root_count,
            "selection_method": self.selection_method.value,
            "selected_root_distance_from_spot_pct": (
                self.selected_root_distance_from_spot_pct
            ),
            "local_slope_at_selected_root": self.local_slope_at_selected_root,
            "normalised_slope": self.normalised_slope,
            "nearest_root_spacing_pct": self.nearest_root_spacing_pct,
            "grid_lower_bound": self.grid_lower_bound,
            "grid_upper_bound": self.grid_upper_bound,
            "grid_points": self.grid_points,
            "grid_expansions": self.grid_expansions,
            "root_near_boundary": self.root_near_boundary,
            "identically_zero_curve": self.identically_zero_curve,
            "no_root_found": self.no_root_found,
            "max_abs_gex_on_grid": self.max_abs_gex_on_grid,
            "unimplemented_reason": self.unimplemented_reason,
        }


@dataclass(frozen=True, slots=True)
class OptionUniverse:
    """Which contracts a number actually covers.

    Zero-gamma runs on a DTE-capped subset for tractability, while the chain
    totals use everything. Comparing the two without knowing that is comparing
    different populations, so the difference is reported rather than left for the
    reader to infer.
    """

    total_contract_count: int
    included_contract_count: int
    included_unsigned_gex: float
    excluded_unsigned_gex: float
    included_expirations: tuple[str, ...] = ()
    excluded_expirations: tuple[str, ...] = ()
    max_dte_used: int | None = None
    filter_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def excluded_contract_count(self) -> int:
        return self.total_contract_count - self.included_contract_count

    @property
    def total_unsigned_gex(self) -> float:
        return self.included_unsigned_gex + self.excluded_unsigned_gex

    @property
    def included_unsigned_gex_share(self) -> float | None:
        total = self.total_unsigned_gex
        return self.included_unsigned_gex / total if total > 0.0 else None

    @property
    def excluded_unsigned_gex_share(self) -> float | None:
        total = self.total_unsigned_gex
        return self.excluded_unsigned_gex / total if total > 0.0 else None

    @property
    def coverage_ratio(self) -> float:
        if self.total_contract_count <= 0:
            return 0.0
        return self.included_contract_count / self.total_contract_count

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_contract_count": self.total_contract_count,
            "included_contract_count": self.included_contract_count,
            "excluded_contract_count": self.excluded_contract_count,
            "included_expirations": list(self.included_expirations),
            "excluded_expirations": list(self.excluded_expirations),
            "max_dte_used": self.max_dte_used,
            "included_unsigned_gex": self.included_unsigned_gex,
            "excluded_unsigned_gex": self.excluded_unsigned_gex,
            "included_unsigned_gex_share": self.included_unsigned_gex_share,
            "excluded_unsigned_gex_share": self.excluded_unsigned_gex_share,
            "coverage_ratio": self.coverage_ratio,
            "filter_reasons": dict(sorted(self.filter_reasons.items())),
        }


@dataclass(frozen=True, slots=True)
class ConfidenceComponent:
    name: str
    #: ``None`` means "this component could not be evaluated" -- distinct from
    #: 0.0, which means "evaluated, and bad". A ``None`` component is excluded
    #: from the weighted mean rather than contributing an invented number.
    score: float | None  # 0.0 - 1.0, or None when unevaluable
    weight: float
    detail: str = ""
    #: Stable machine-readable reason, when there is one. Deterministic for a
    #: given cause so that a log scraper can match on it; empty when the
    #: component evaluated normally.
    warning_code: str = ""
    # True when this component's threshold is still UNSPECIFIED_CALIBRATE, i.e.
    # the score was produced with a placeholder and must not gate real money.
    uncalibrated: bool = False
    # True when the component detected a condition that invalidates the snapshot
    # outright, rather than merely lowering its score.
    hard_failure: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": self.score,
            "weight": self.weight,
            "detail": self.detail,
            "warning_code": self.warning_code,
            "uncalibrated": self.uncalibrated,
            "hard_failure": self.hard_failure,
        }


@dataclass(frozen=True, slots=True)
class ConfidenceScore:
    value: float  # 0 - 100
    components: tuple[ConfidenceComponent, ...]
    warnings: tuple[str, ...] = ()

    @property
    def score(self) -> float:
        """Alias for ``value``; the required field name in the output contract."""
        return self.value

    @property
    def hard_failures(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.components if c.hard_failure)

    @property
    def uncalibrated_components(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.components if c.uncalibrated)

    @property
    def calibrated(self) -> bool:
        """False while any component still relies on a placeholder threshold.

        NOTE: this is a *flag*, not an enforcement mechanism. No risk engine
        exists in this repository, and nothing consumes this value to block an
        order -- because nothing here can place an order. It is a research
        signal that thresholds are unresearched.
        """
        return not self.uncalibrated_components and not self.hard_failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.value,
            "calibrated": self.calibrated,
            "components": [c.as_dict() for c in self.components],
            "warnings": list(self.warnings),
            "hard_failures": list(self.hard_failures),
        }


@dataclass(frozen=True, slots=True)
class GexSnapshot:
    """The single object every downstream consumer reads.

    Deliberately fat: carrying all five views plus their provenance together
    means a consumer cannot read the aggregate while remaining unaware that the
    0DTE bucket points the other way, that 40% of the chain was excluded, or that
    the zero-gamma level sits on the grid boundary.
    """

    as_of: datetime
    spot: float
    source: str
    sign_convention: SignConvention
    model_spec: ModelSpec

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
    # Universe covered by the chain totals.
    chain_universe: OptionUniverse
    # Universe covered by the zero-gamma grid, which is usually narrower.
    zero_gamma_universe: OptionUniverse
    validation: ValidationReport

    contract_count: int = 0
    total_open_interest: int = 0
    warnings: tuple[str, ...] = ()
    config_fingerprint: str | None = None
    #: Canonical serialisation of the one effective model every contract was
    #: priced with, plus its fingerprint. Present whenever at least one
    #: contract resolved; ``None`` on an empty chain.
    effective_model: dict[str, Any] | None = None
    #: Cross-convention root topology: which roots survived, how far they
    #: moved, and whether the selected root kept its identity.
    root_topology: dict[str, Any] | None = None
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

        The honest measure of how model-dependent the level is on this snapshot.
        """
        levels = [
            r.selected_root for r in self.zero_gamma if r.selected_root is not None
        ]
        if len(levels) < 2:
            return None
        return (max(levels) - min(levels)) / self.spot * 100.0

    @property
    def zero_gamma_root_identity_stable(self) -> bool | None:
        """Whether every convention found the same *number* of roots.

        A convention change that alters the root count is a stronger warning than
        one that merely shifts the level: it means the conventions disagree about
        the shape of the curve, not just its position.
        """
        counts = {r.root_count for r in self.zero_gamma if not r.no_root_found}
        if not counts:
            return None
        return len(counts) == 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "spot": self.spot,
            "source": self.source,
            "sign_convention": self.sign_convention.value,
            "model_spec": self.model_spec.as_dict(),
            "model_fingerprint": self.model_spec.fingerprint(),
            "config_fingerprint": self.config_fingerprint,
            "effective_model": self.effective_model,
            "total_unsigned_gex": self.total_unsigned_gex,
            "total_signed_gex": self.total_signed_gex,
            "contract_count": self.contract_count,
            "total_open_interest": self.total_open_interest,
            "buckets": [
                {
                    "bucket": b.bucket.value,
                    "unsigned_gex": b.unsigned_gex,
                    "signed_gex": b.signed_gex,
                    "contract_count": b.contract_count,
                    "open_interest": b.open_interest,
                }
                for b in self.buckets
            ],
            "strikes": [
                {
                    "strike": s.strike,
                    "call_gex": s.call_gex,
                    "put_gex": s.put_gex,
                    "unsigned_gex": s.unsigned_gex,
                    "signed_gex": s.signed_gex,
                }
                for s in self.strikes
            ],
            "walls": self.walls.as_dict(),
            "zero_gamma": [z.as_dict() for z in self.zero_gamma],
            "zero_gamma_spread_pct": self.zero_gamma_spread_pct,
            "zero_gamma_root_identity_stable": (self.zero_gamma_root_identity_stable),
            "root_topology": self.root_topology,
            "chain_universe": self.chain_universe.as_dict(),
            "zero_gamma_universe": self.zero_gamma_universe.as_dict(),
            "validation": self.validation.as_dict(),
            "confidence": self.confidence.as_dict(),
            "warnings": list(self.warnings),
            "meta": _jsonable(self.meta),
        }

    def hash_payload(self) -> dict[str, Any]:
        """The canonical, deterministic subset of the snapshot that is hashed.

        Exposed as a method so a test can inspect exactly what is covered rather
        than inferring it from hash collisions.

        **Included** -- every deterministic numeric and state field: totals,
        buckets, strikes, walls, zero-gamma diagnostics, both universes, the
        validation counters, the model and config fingerprints, root topology,
        and the full confidence component structure (name, score, weight,
        hard_failure, uncalibrated).

        **Excluded** -- free-form human prose only: component ``detail`` strings,
        snapshot ``warnings``, and the bounded ``validation.examples`` sample.
        Their wording may legitimately improve without a single number changing,
        and a hash that trips on a reworded message is a hash nobody trusts. Every
        machine-readable *code* those messages describe is present elsewhere in
        the payload.

        v2 excluded the confidence components entirely, so a component score,
        weight or flag could change while the replay hash stood still.
        """
        payload = self.as_dict()

        payload["validation"] = {
            key: value
            for key, value in payload["validation"].items()
            if key != "examples"
        }
        payload.pop("warnings", None)

        confidence = payload["confidence"]
        payload["confidence"] = {
            "score": confidence["score"],
            "calibrated": confidence["calibrated"],
            "hard_failures": sorted(confidence["hard_failures"]),
            # Sorted by name: two snapshots with the same components in a
            # different order are semantically identical and must hash the same.
            "components": sorted(
                (
                    {
                        "name": component["name"],
                        "score": component["score"],
                        "weight": component["weight"],
                        "hard_failure": component["hard_failure"],
                        "uncalibrated": component["uncalibrated"],
                    }
                    for component in confidence["components"]
                ),
                key=lambda entry: entry["name"],
            ),
        }

        for entry in payload["zero_gamma"]:
            entry.pop("unimplemented_reason", None)
            entry.pop("curve", None)
        for void in payload["walls"]["gamma_voids"]:
            void.pop("detail", None)
        return payload

    def with_meta(self, **changes: Any) -> GexSnapshot:
        """Copy with extra metadata. Used to prove metadata reaches the hash."""
        return replace(self, meta={**self.meta, **changes})

    def output_hash(self, *, significant_digits: int = HASH_SIGNIFICANT_DIGITS) -> str:
        """Digest of the deterministic output, for replay checks.

        Floats are quantised to ``significant_digits`` (12) before hashing. Full
        float repr would make the digest sensitive to last-bit summation
        differences between platforms and libm versions, so "same data, same
        hash" would hold on one machine and fail on another. Twelve significant
        figures is far tighter than any change of substance.
        """
        encoded = json.dumps(
            _quantise(self.hash_payload(), significant_digits),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _quantise(value: Any, digits: int) -> Any:
    """Round every float to ``digits`` significant figures, recursively.

    Significant figures rather than decimal places: GEX totals are ~1e10 while
    confidence scores are ~1e0, and a fixed decimal rounding would either destroy
    the small values or fail to stabilise the large ones.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return repr(value)
        if value == 0.0:
            return 0.0
        magnitude = math.floor(math.log10(abs(value)))
        return round(value, max(0, digits - 1 - magnitude))
    if isinstance(value, dict):
        return {key: _quantise(item, digits) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_quantise(item, digits) for item in value]
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): _jsonable(v)
            for k, v in sorted(value.items(), key=lambda i: str(i[0]))
        }
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float | int | str | bool) or value is None:
        return value
    return str(value)
