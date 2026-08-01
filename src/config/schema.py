"""Typed configuration loading with fail-fast schema validation.

Design rules:

* **Unknown keys are errors, not warnings.** A silently-ignored typo in a config
  file is a parameter that never took effect while looking like it did. That is
  the single most expensive class of config bug.
* **Sentinels survive the round trip.** ``UNSPECIFIED_CALIBRATE`` in YAML becomes
  the sentinel object, not the string and not a number.
* **Execution-capable modes must be fully calibrated.** A profile that could act
  on the numbers is refused while any market threshold is still a sentinel. In
  this repository *no* profile is execution-capable -- there is no broker -- but
  the gate exists so it cannot be bypassed later by accident.
* **The loader is the only place YAML is parsed.** The engine core never imports
  PyYAML; ``tests/unit/test_architecture.py`` enforces that.

Every loaded config carries a fingerprint that ends up in the snapshot, so a
result can be traced back to the exact file contents that produced it.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, NoReturn

from src.config.thetadata import (
    ThetaDataConfig,
    ThetaDataConfigError,
    parse_thetadata_config,
)
from src.domain.gex import IVConvention, SignConvention
from src.domain.iv import IVSource
from src.domain.model_spec import (
    DayCountConvention,
    DividendSource,
    ExpirationTimestampRule,
    ModelSpec,
    PricingModel,
    RateSource,
    UnderlyingPriceSource,
)
from src.domain.timestamps import DataQualityLimits
from src.gex.config import (
    UNSPECIFIED_CALIBRATE,
    YAML_SENTINEL,
    Calibratable,
    ConfidenceConfig,
    GexEngineConfig,
    WallConfig,
    ZeroGammaConfig,
)


class ConfigError(ValueError):
    """Raised for any invalid configuration. Always names the offending path."""


VALID_STAGES = (
    "DEVELOPMENT",
    "VALIDATION",
    "OOS",
    "PAPER",
    "LIVE_STAGE_1",
    "LIVE_STAGE_2",
)

# Profiles that would be able to act on the numbers. Kept as a named set so the
# calibration gate cannot be sidestepped by adding a stage.
EXECUTION_CAPABLE_STAGES = frozenset({"PAPER", "LIVE_STAGE_1", "LIVE_STAGE_2"})

_ENV_PATTERN = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)(?::-(.*))?\}$")


@dataclass(frozen=True, slots=True)
class ProfileMetadata:
    """Non-engine settings, kept separate so the engine config stays pure."""

    stage: str
    enabled: bool
    disabled_reason: str | None
    options_source: str
    futures_source: str
    broker: str
    trading_enabled: bool
    # Environment overrides that were applied, recorded so a snapshot can show
    # that the file alone does not explain its inputs.
    env_overrides: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "options_source": self.options_source,
            "futures_source": self.futures_source,
            "broker": self.broker,
            "trading_enabled": self.trading_enabled,
            "env_overrides": list(self.env_overrides),
        }


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    engine: GexEngineConfig
    profile: ProfileMetadata
    fingerprint: str
    source_path: str
    #: Typed vendor settings. Present on every load, so no caller has to
    #: reach into ``raw`` and hand-assemble a client.
    thetadata: ThetaDataConfig = field(default_factory=ThetaDataConfig)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def uncalibrated_paths(self) -> tuple[str, ...]:
        return tuple(find_sentinels(self.raw))


# --- Primitive coercion -----------------------------------------------------


def _fail(path: str, message: str) -> NoReturn:
    raise ConfigError(f"{path}: {message}")


def _require(mapping: dict[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        _fail(f"{path}.{key}", "required key is missing")
    return mapping[key]


def _check_unknown(mapping: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        _fail(
            path,
            f"unknown key(s) {sorted(unknown)}; allowed keys are {sorted(allowed)}. "
            "A silently-ignored typo is a parameter that never took effect.",
        )


def _as_float(
    value: Any, path: str, *, low: float | None = None, high: float | None = None
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        _fail(path, f"expected a number, got {type(value).__name__}")
    number = float(value)
    import math

    if not math.isfinite(number):
        _fail(path, "value must be finite")
    if low is not None and number < low:
        _fail(path, f"value {number} is below the minimum {low}")
    if high is not None and number > high:
        _fail(path, f"value {number} is above the maximum {high}")
    return number


def _as_int(
    value: Any, path: str, *, low: int | None = None, high: int | None = None
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, f"expected an integer, got {type(value).__name__}")
    number = int(value)
    if low is not None and number < low:
        _fail(path, f"value {number} is below the minimum {low}")
    if high is not None and number > high:
        _fail(path, f"value {number} is above the maximum {high}")
    return number


def _as_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, f"expected a boolean, got {type(value).__name__}")
    return value


def _as_str(value: Any, path: str, *, allowed: tuple[str, ...] | None = None) -> str:
    if not isinstance(value, str):
        _fail(path, f"expected a string, got {type(value).__name__}")
    if allowed and value not in allowed:
        _fail(path, f"{value!r} is not one of {list(allowed)}")
    return value


def _as_calibratable(
    value: Any, path: str, *, low: float | None = None, high: float | None = None
) -> Calibratable:
    """A number, or the sentinel when the file says ``UNSPECIFIED_CALIBRATE``."""
    if value is None or value == YAML_SENTINEL:
        return UNSPECIFIED_CALIBRATE
    return _as_float(value, path, low=low, high=high)


def _as_enum[EnumT: Enum](value: Any, enum_cls: type[EnumT], path: str) -> EnumT:
    text = _as_str(value, path)
    for member in enum_cls:
        if member.value == text or member.name == text:
            return member
    _fail(
        path,
        f"{text!r} is not a valid {enum_cls.__name__}; "
        f"valid values are {[m.value for m in enum_cls]}",
    )


# --- Environment overrides --------------------------------------------------


def resolve_env(value: Any, path: str, applied: list[str]) -> Any:
    """Expand ``${VAR}`` / ``${VAR:-default}`` and record that it happened.

    Recording matters: an environment variable that changes a model assumption
    must not be invisible in the audit trail, or two runs of "the same config"
    become silently incomparable.
    """
    if not isinstance(value, str):
        return value
    match = _ENV_PATTERN.match(value.strip())
    if not match:
        return value
    name, default = match.group(1), match.group(2)
    if name in os.environ:
        applied.append(f"{path}<-${name}")
        return _coerce_scalar(os.environ[name])
    if default is not None:
        return _coerce_scalar(default)
    _fail(path, f"environment variable {name} is not set and has no default")


def _coerce_scalar(text: str) -> Any:
    lowered = text.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none", ""):
        return None
    if text.strip() == YAML_SENTINEL:
        return YAML_SENTINEL
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _expand(node: Any, path: str, applied: list[str]) -> Any:
    if isinstance(node, dict):
        return {
            key: _expand(value, f"{path}.{key}" if path else key, applied)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_expand(item, f"{path}[{i}]", applied) for i, item in enumerate(node)]
    return resolve_env(node, path, applied)


def find_sentinels(node: Any, path: str = "") -> list[str]:
    """Dotted paths of every value still set to the sentinel."""
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            out.extend(find_sentinels(value, f"{path}.{key}" if path else str(key)))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            out.extend(find_sentinels(item, f"{path}[{index}]"))
    elif node == YAML_SENTINEL:
        out.append(path)
    return out


# --- Section parsers --------------------------------------------------------


def _parse_model(raw: dict[str, Any], path: str) -> ModelSpec:
    allowed = {
        "pricing_model",
        "day_count_convention",
        "risk_free_rate_source",
        "dividend_yield_source",
        "expiration_timestamp_rule",
        "min_time_to_expiry_minutes",
        "underlying_price_source",
        "iv_price_source",
        "risk_free_rate",
        "dividend_yield",
        "configured_underlying_price",
    }
    _check_unknown(raw, allowed, path)
    rule = _as_enum(
        raw.get(
            "expiration_timestamp_rule",
            ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT.value,
        ),
        ExpirationTimestampRule,
        f"{path}.expiration_timestamp_rule",
    )
    if not rule.is_supported:
        # Refused rather than accepted-and-ignored. An accepted rule that never
        # reaches the maths lets the fingerprint claim a distinction the
        # calculation never made.
        _fail(
            f"{path}.expiration_timestamp_rule",
            f"{rule.value} is not supported: {rule.unsupported_reason}",
        )

    rate_source = _as_enum(
        raw.get("risk_free_rate_source", RateSource.CONFIGURED_CONSTANT.value),
        RateSource,
        f"{path}.risk_free_rate_source",
    )
    if not rate_source.is_available:
        _fail(
            f"{path}.risk_free_rate_source",
            f"{rate_source.value} has no wired-up data source in this repository; "
            "use configured_constant, snapshot or zero",
        )

    dividend_source = _as_enum(
        raw.get("dividend_yield_source", DividendSource.CONFIGURED_CONSTANT.value),
        DividendSource,
        f"{path}.dividend_yield_source",
    )
    if not dividend_source.is_available:
        _fail(
            f"{path}.dividend_yield_source",
            f"{dividend_source.value} has no wired-up data source in this "
            "repository; use configured_constant, snapshot or zero",
        )

    return ModelSpec(
        pricing_model=_as_enum(
            raw.get("pricing_model", PricingModel.BLACK_SCHOLES_MERTON.value),
            PricingModel,
            f"{path}.pricing_model",
        ),
        day_count_convention=_as_enum(
            raw.get("day_count_convention", DayCountConvention.ACT_365_FIXED.value),
            DayCountConvention,
            f"{path}.day_count_convention",
        ),
        risk_free_rate_source=rate_source,
        dividend_yield_source=_as_enum(
            raw.get("dividend_yield_source", DividendSource.CONFIGURED_CONSTANT.value),
            DividendSource,
            f"{path}.dividend_yield_source",
        ),
        expiration_timestamp_rule=rule,
        minimum_time_to_expiry_minutes=_as_float(
            raw.get("min_time_to_expiry_minutes", 60.0),
            f"{path}.min_time_to_expiry_minutes",
            low=0.0,
            high=1440.0,
        ),
        underlying_price_source=_as_enum(
            raw.get(
                "underlying_price_source",
                UnderlyingPriceSource.VENDOR_INDEX_SNAPSHOT.value,
            ),
            UnderlyingPriceSource,
            f"{path}.underlying_price_source",
        ),
        iv_price_source=_as_enum(
            raw.get("iv_price_source", IVSource.VENDOR_DEFAULT_IV.value),
            IVSource,
            f"{path}.iv_price_source",
        ),
        risk_free_rate=_as_float(
            raw.get("risk_free_rate", 0.0), f"{path}.risk_free_rate", low=-1.0, high=1.0
        ),
        dividend_yield=_as_float(
            raw.get("dividend_yield", 0.0), f"{path}.dividend_yield", low=-1.0, high=1.0
        ),
    )


def _parse_data_quality(raw: dict[str, Any], path: str) -> DataQualityLimits:
    allowed = set(DataQualityLimits().as_dict())
    _check_unknown(raw, allowed, path)
    defaults = DataQualityLimits()
    return DataQualityLimits(
        max_quote_greeks_skew_seconds=_as_float(
            raw.get(
                "max_quote_greeks_skew_seconds", defaults.max_quote_greeks_skew_seconds
            ),
            f"{path}.max_quote_greeks_skew_seconds",
            low=0.0,
        ),
        max_quote_iv_skew_seconds=_as_float(
            raw.get("max_quote_iv_skew_seconds", defaults.max_quote_iv_skew_seconds),
            f"{path}.max_quote_iv_skew_seconds",
            low=0.0,
        ),
        max_quote_underlying_skew_seconds=_as_float(
            raw.get(
                "max_quote_underlying_skew_seconds",
                defaults.max_quote_underlying_skew_seconds,
            ),
            f"{path}.max_quote_underlying_skew_seconds",
            low=0.0,
        ),
        max_future_timestamp_seconds=_as_float(
            raw.get(
                "max_future_timestamp_seconds", defaults.max_future_timestamp_seconds
            ),
            f"{path}.max_future_timestamp_seconds",
            low=0.0,
            high=3600.0,
        ),
        max_snapshot_age_seconds=_as_float(
            raw.get("max_snapshot_age_seconds", defaults.max_snapshot_age_seconds),
            f"{path}.max_snapshot_age_seconds",
            low=0.0,
        ),
        max_open_interest_age_sessions=_as_int(
            raw.get(
                "max_open_interest_age_sessions",
                defaults.max_open_interest_age_sessions,
            ),
            f"{path}.max_open_interest_age_sessions",
            low=1,
        ),
    )


def _parse_zero_gamma(raw: dict[str, Any], path: str) -> ZeroGammaConfig:
    allowed = set(ZeroGammaConfig().as_dict())
    _check_unknown(raw, allowed, path)
    defaults = ZeroGammaConfig()
    conventions_raw = raw.get("conventions", [c.value for c in defaults.conventions])
    if not isinstance(conventions_raw, list) or not conventions_raw:
        _fail(f"{path}.conventions", "expected a non-empty list")
    conventions = tuple(
        _as_enum(item, IVConvention, f"{path}.conventions[{i}]")
        for i, item in enumerate(conventions_raw)
    )
    unimplemented = [c.value for c in conventions if not c.is_implemented]
    if unimplemented:
        _fail(
            f"{path}.conventions",
            f"{unimplemented} are not implemented. STICKY_DELTA in particular is "
            "deliberately unavailable: the log-moneyness approximation is exposed "
            "as sticky_moneyness instead.",
        )
    return ZeroGammaConfig(
        grid_span_pct=_as_float(
            raw.get("grid_span_pct", defaults.grid_span_pct),
            f"{path}.grid_span_pct",
            low=0.0005,
            high=0.5,
        ),
        grid_step_pct=_as_float(
            raw.get("grid_step_pct", defaults.grid_step_pct),
            f"{path}.grid_step_pct",
            low=1e-5,
            high=0.05,
        ),
        conventions=conventions,
        max_dte_for_grid=_as_int(
            raw.get("max_dte_for_grid", defaults.max_dte_for_grid),
            f"{path}.max_dte_for_grid",
            low=0,
            high=3650,
        ),
        min_points_for_smile_fit=_as_int(
            raw.get("min_points_for_smile_fit", defaults.min_points_for_smile_fit),
            f"{path}.min_points_for_smile_fit",
            low=3,
        ),
        boundary_tolerance_pct=_as_float(
            raw.get("boundary_tolerance_pct", defaults.boundary_tolerance_pct),
            f"{path}.boundary_tolerance_pct",
            low=0.0,
            high=0.5,
        ),
        max_grid_expansions=_as_int(
            raw.get("max_grid_expansions", defaults.max_grid_expansions),
            f"{path}.max_grid_expansions",
            low=0,
            high=10,
        ),
        grid_expansion_factor=_as_float(
            raw.get("grid_expansion_factor", defaults.grid_expansion_factor),
            f"{path}.grid_expansion_factor",
            low=1.01,
            high=10.0,
        ),
    )


def _parse_walls(raw: dict[str, Any], path: str) -> WallConfig:
    allowed = set(WallConfig().as_dict())
    _check_unknown(raw, allowed, path)
    defaults = WallConfig()
    numeric = {
        "node_min_share_of_max": (0.0, 1.0),
        "void_max_share_of_max": (0.0, 1.0),
        "void_min_width_pct": (0.0, 1.0),
        "band_pct": (0.0, 1.0),
        "directional_wall_min_distance_pct": (0.0, 1.0),
        "directional_wall_band_pct": (0.0, 1.0),
        "irregular_spacing_factor": (1.0, 100.0),
        "min_ladder_coverage_for_true_void": (0.0, 1.0),
    }
    values: dict[str, Any] = {}
    for name, (low, high) in numeric.items():
        values[name] = _as_float(
            raw.get(name, getattr(defaults, name)), f"{path}.{name}", low=low, high=high
        )
    values["max_nodes_per_side"] = _as_int(
        raw.get("max_nodes_per_side", defaults.max_nodes_per_side),
        f"{path}.max_nodes_per_side",
        low=1,
        high=100,
    )
    values["min_observed_strikes_for_true_void"] = _as_int(
        raw.get(
            "min_observed_strikes_for_true_void",
            defaults.min_observed_strikes_for_true_void,
        ),
        f"{path}.min_observed_strikes_for_true_void",
        low=1,
    )
    return WallConfig(**values)


def _parse_confidence(raw: dict[str, Any], path: str) -> ConfidenceConfig:
    allowed = {
        "min_chain_completeness_ratio",
        "crossed_quote_zero_score_ratio",
        "min_universe_coverage_ratio",
        "min_good_iv_ratio",
        "ambiguous_root_spacing_pct",
        "steep_slope_threshold",
        "max_zero_gamma_shift_pct",
        "max_sign_model_disagreement",
        "max_0dte_dominance_ratio",
    }
    _check_unknown(raw, allowed, path)
    defaults = ConfidenceConfig()
    return ConfidenceConfig(
        min_chain_completeness_ratio=_as_float(
            raw.get(
                "min_chain_completeness_ratio", defaults.min_chain_completeness_ratio
            ),
            f"{path}.min_chain_completeness_ratio",
            low=0.0,
            high=1.0,
        ),
        crossed_quote_zero_score_ratio=_as_float(
            raw.get(
                "crossed_quote_zero_score_ratio",
                defaults.crossed_quote_zero_score_ratio,
            ),
            f"{path}.crossed_quote_zero_score_ratio",
            low=0.0,
            high=1.0,
        ),
        min_universe_coverage_ratio=_as_float(
            raw.get(
                "min_universe_coverage_ratio", defaults.min_universe_coverage_ratio
            ),
            f"{path}.min_universe_coverage_ratio",
            low=0.0,
            high=1.0,
        ),
        min_good_iv_ratio=_as_float(
            raw.get("min_good_iv_ratio", defaults.min_good_iv_ratio),
            f"{path}.min_good_iv_ratio",
            low=0.0,
            high=1.0,
        ),
        ambiguous_root_spacing_pct=_as_float(
            raw.get("ambiguous_root_spacing_pct", defaults.ambiguous_root_spacing_pct),
            f"{path}.ambiguous_root_spacing_pct",
            low=0.0,
        ),
        steep_slope_threshold=_as_float(
            raw.get("steep_slope_threshold", defaults.steep_slope_threshold),
            f"{path}.steep_slope_threshold",
            low=0.0,
        ),
        max_zero_gamma_shift_pct=_as_calibratable(
            raw.get("max_zero_gamma_shift_pct", YAML_SENTINEL),
            f"{path}.max_zero_gamma_shift_pct",
            low=0.0,
        ),
        max_sign_model_disagreement=_as_calibratable(
            raw.get("max_sign_model_disagreement", YAML_SENTINEL),
            f"{path}.max_sign_model_disagreement",
            low=0.0,
        ),
        max_0dte_dominance_ratio=_as_calibratable(
            raw.get("max_0dte_dominance_ratio", YAML_SENTINEL),
            f"{path}.max_0dte_dominance_ratio",
            low=0.0,
            high=1.0,
        ),
    )


# --- Top level --------------------------------------------------------------

TOP_LEVEL_KEYS = {
    "stage",
    "enabled",
    "disabled_reason",
    "data",
    "model",
    "data_quality",
    "gex",
    "zero_gamma",
    "walls",
    "confidence",
    "execution",
    "thetadata",
}


def fingerprint_of(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def parse_config(raw: dict[str, Any], *, source_path: str = "<memory>") -> LoadedConfig:
    """Validate a raw mapping into a typed config. Raises on any problem."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{source_path}: top level must be a mapping")

    applied: list[str] = []
    expanded = _expand(raw, "", applied)
    _check_unknown(expanded, TOP_LEVEL_KEYS, source_path)

    stage = _as_str(
        _require(expanded, "stage", source_path), "stage", allowed=VALID_STAGES
    )
    enabled = _as_bool(expanded.get("enabled", True), "enabled")
    disabled_reason = expanded.get("disabled_reason")
    if disabled_reason is not None and not isinstance(disabled_reason, str):
        _fail("disabled_reason", "expected a string or null")

    data = expanded.get("data", {})
    if not isinstance(data, dict):
        _fail("data", "expected a mapping")
    _check_unknown(
        data,
        {
            "options_source",
            "futures_source",
            "roots",
            "max_dte",
            "expected_contract_count",
        },
        "data",
    )

    gex = expanded.get("gex", {})
    if not isinstance(gex, dict):
        _fail("gex", "expected a mapping")
    _check_unknown(
        gex,
        {
            "spot_move_pct",
            "sign_convention",
            "prefer_vendor_gamma",
            "drop_crossed_quotes",
            "require_open_interest",
        },
        "gex",
    )

    execution = expanded.get("execution", {})
    if not isinstance(execution, dict):
        _fail("execution", "expected a mapping")
    _check_unknown(execution, {"broker", "trading_enabled"}, "execution")
    trading_enabled = _as_bool(
        execution.get("trading_enabled", False), "execution.trading_enabled"
    )
    broker = _as_str(execution.get("broker", "none"), "execution.broker")

    # Hard invariant: this repository has no broker implementation, so a config
    # claiming otherwise is a mistake that must not load.
    if trading_enabled:
        _fail(
            "execution.trading_enabled",
            "must be false. This repository contains no broker adapter and no "
            "order-placement code; a config that enables trading is describing a "
            "capability that does not exist.",
        )
    if broker != "none":
        _fail(
            "execution.broker",
            f"{broker!r} is not available. No broker adapter is implemented.",
        )

    engine = GexEngineConfig(
        spot_move_pct=_as_float(
            gex.get("spot_move_pct", 0.01), "gex.spot_move_pct", low=1e-6, high=1.0
        ),
        sign_convention=_as_enum(
            gex.get(
                "sign_convention", SignConvention.DEALER_LONG_CALLS_SHORT_PUTS.value
            ),
            SignConvention,
            "gex.sign_convention",
        ),
        prefer_vendor_gamma=_as_bool(
            gex.get("prefer_vendor_gamma", False), "gex.prefer_vendor_gamma"
        ),
        drop_crossed_quotes=_as_bool(
            gex.get("drop_crossed_quotes", True), "gex.drop_crossed_quotes"
        ),
        require_open_interest=_as_bool(
            gex.get("require_open_interest", True), "gex.require_open_interest"
        ),
        max_dte=(
            None
            if data.get("max_dte") is None
            else _as_int(data["max_dte"], "data.max_dte", low=0, high=3650)
        ),
        model_spec=_parse_model(expanded.get("model", {}) or {}, "model"),
        data_quality=_parse_data_quality(
            expanded.get("data_quality", {}) or {}, "data_quality"
        ),
        zero_gamma=_parse_zero_gamma(
            expanded.get("zero_gamma", {}) or {}, "zero_gamma"
        ),
        walls=_parse_walls(expanded.get("walls", {}) or {}, "walls"),
        confidence=_parse_confidence(
            expanded.get("confidence", {}) or {}, "confidence"
        ),
    )

    sentinels = find_sentinels(expanded)
    if stage in EXECUTION_CAPABLE_STAGES and enabled and sentinels:
        _fail(
            "stage",
            f"stage {stage} is execution-capable but {len(sentinels)} threshold(s) "
            f"are still UNSPECIFIED_CALIBRATE: {sentinels}. Calibrate them or mark "
            "the profile enabled: false.",
        )

    try:
        thetadata = parse_thetadata_config(expanded.get("thetadata", {}) or {})
    except ThetaDataConfigError as exc:
        raise ConfigError(str(exc)) from exc

    options_source = _as_str(
        data.get("options_source", "synthetic"), "data.options_source"
    )
    _require_coherent_capture_profile(
        options_source=options_source, engine=engine, thetadata=thetadata
    )

    fingerprint = fingerprint_of(expanded)
    return LoadedConfig(
        engine=engine.with_(config_fingerprint=fingerprint),
        profile=ProfileMetadata(
            stage=stage,
            enabled=enabled,
            disabled_reason=disabled_reason,
            options_source=options_source,
            futures_source=_as_str(
                data.get("futures_source", "none"), "data.futures_source"
            ),
            broker=broker,
            trading_enabled=trading_enabled,
            env_overrides=tuple(applied),
        ),
        fingerprint=fingerprint,
        source_path=str(source_path),
        thetadata=thetadata,
        raw=expanded,
    )


#: Values of ``model.underlying_price_source`` that name no vendor print. Fine
#: for the synthetic adapter; incoherent in a profile that fetches from
#: ThetaData, because there is then a real underlying and this says otherwise.
SYNTHETIC_UNDERLYING_SOURCES = frozenset({"synthetic"})


def _require_coherent_capture_profile(
    *, options_source: str, engine: Any, thetadata: Any
) -> None:
    """A ThetaData profile must not carry synthetic provenance.

    ``config/research.yaml`` pairs ``data.options_source: synthetic`` with a
    fully populated ``thetadata:`` block, which is correct -- the block is there
    so the settings are reviewable before anybody spends money. Flipping
    ``options_source`` to ``thetadata`` while leaving
    ``underlying_price_source: synthetic`` is not correct, and nothing caught it:
    the resulting snapshot would carry real vendor gammas against an underlying
    labelled as invented.

    Raw capture is required for the same reason it is required for capture
    readiness -- a paid session whose bytes are discarded cannot be re-derived.
    """
    if options_source != "thetadata":
        return

    problems: list[str] = []
    model_source = getattr(engine.model_spec.underlying_price_source, "value", "")
    if model_source in SYNTHETIC_UNDERLYING_SOURCES:
        problems.append(
            f"model.underlying_price_source is {model_source!r}, which names no "
            "vendor print"
        )
    if thetadata.underlying_price_source in SYNTHETIC_UNDERLYING_SOURCES:
        problems.append(
            f"thetadata.underlying_price_source is "
            f"{thetadata.underlying_price_source!r}, which names no vendor print"
        )
    if not thetadata.raw_capture_enabled:
        problems.append(
            "thetadata.raw_capture_enabled is false, so the vendor responses this "
            "profile pays for would be discarded"
        )

    if problems:
        _fail(
            "data.options_source",
            "this profile fetches from ThetaData, but "
            + "; ".join(problems)
            + ". A capture profile must describe the capture it performs.",
        )


def _strict_loader() -> type:
    """A YAML loader that rejects duplicate keys.

    PyYAML silently keeps the *last* occurrence of a duplicated key. In a config
    file that is a live trap: a stray second ``enabled:`` further down the file
    overrides the first, and the file reads as if the first value applied. Both
    values look correct in review; only the last one takes effect.
    """
    import yaml

    class StrictLoader(yaml.SafeLoader):
        pass

    def _no_duplicates(
        loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
    ) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ConfigError(
                    f"duplicate key {key!r} at line {key_node.start_mark.line + 1}. "
                    "YAML would silently keep the last occurrence, so the earlier "
                    "value would look applied while having no effect."
                )
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    StrictLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates
    )
    return StrictLoader


def load_yaml(text: str, *, source_path: str = "<memory>") -> Any:
    """Parse YAML safely and strictly.

    ``SafeLoader`` because a config file must not be able to construct arbitrary
    Python objects; the duplicate-key check on top of it because silent key
    shadowing is the config bug that costs the most to find.
    """
    import yaml

    try:
        return yaml.load(text, Loader=_strict_loader())  # noqa: S506 - strict SafeLoader subclass
    except ConfigError as exc:
        raise ConfigError(f"{source_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source_path}: invalid YAML -- {exc}") from exc


def load_config(path: str | pathlib.Path) -> LoadedConfig:
    """Load and validate a YAML profile.

    PyYAML is imported here and nowhere in the engine core, so importing the
    maths never drags in a parser.
    """
    resolved = pathlib.Path(path)
    if not resolved.exists():
        raise ConfigError(f"{resolved}: config file does not exist")
    raw = load_yaml(resolved.read_text(encoding="utf-8"), source_path=str(resolved))
    if raw is None:
        raise ConfigError(f"{resolved}: file is empty")
    return parse_config(raw, source_path=str(resolved))
