"""Typed configuration loading and validation.

The behaviours that matter most:

* an unknown key is an error, because a silently-ignored typo is a parameter
  that never took effect while looking like it did;
* sentinels survive the round trip as sentinels, not as strings or numbers;
* nothing can configure this repository into placing an order.
"""

from __future__ import annotations

import pathlib

import pytest

from src.config.schema import (
    EXECUTION_CAPABLE_STAGES,
    ConfigError,
    find_sentinels,
    load_config,
    load_yaml,
    parse_config,
)
from src.domain.gex import IVConvention, SignConvention
from src.domain.iv import IVSource
from src.domain.model_spec import DayCountConvention
from src.gex.config import UNSPECIFIED_CALIBRATE, is_calibrated
from src.gex.engine import compute_gex_snapshot
from src.synthetic.chains import build_synthetic_chain

CONFIG_DIR = pathlib.Path(__file__).resolve().parents[2] / "config"

MINIMAL = {
    "stage": "DEVELOPMENT",
    "enabled": True,
    "data": {"options_source": "synthetic"},
    "execution": {"broker": "none", "trading_enabled": False},
}


def build(**overrides):
    payload = {**MINIMAL, **overrides}
    return parse_config(payload)


# --- Shipped profiles -------------------------------------------------------


def test_research_profile_loads_and_is_enabled():
    loaded = load_config(CONFIG_DIR / "research.yaml")
    assert loaded.profile.stage == "DEVELOPMENT"
    assert loaded.profile.enabled
    assert loaded.profile.options_source == "synthetic"


@pytest.mark.parametrize("name", ["paper", "live"])
def test_paper_and_live_profiles_are_explicitly_disabled(name):
    """Both are templates, not switches. Neither can run."""
    loaded = load_config(CONFIG_DIR / f"{name}.yaml")
    assert not loaded.profile.enabled
    assert loaded.profile.disabled_reason
    assert not loaded.profile.trading_enabled
    assert loaded.profile.broker == "none"


@pytest.mark.parametrize("name", ["research", "paper", "live"])
def test_no_profile_enables_trading(name):
    assert not load_config(CONFIG_DIR / f"{name}.yaml").profile.trading_enabled


@pytest.mark.parametrize("name", ["research", "paper", "live"])
def test_every_profile_keeps_market_thresholds_as_sentinels(name):
    loaded = load_config(CONFIG_DIR / f"{name}.yaml")
    assert set(loaded.uncalibrated_paths) == {
        "confidence.max_zero_gamma_shift_pct",
        "confidence.max_sign_model_disagreement",
        "confidence.max_0dte_dominance_ratio",
    }


def test_profiles_have_distinct_fingerprints():
    fingerprints = {
        name: load_config(CONFIG_DIR / f"{name}.yaml").fingerprint
        for name in ("research", "paper", "live")
    }
    assert len(set(fingerprints.values())) == 3


def test_shipped_profiles_contain_no_credential_values():
    """Only the *names* of environment variables belong in a config file."""
    for name in ("research", "paper", "live"):
        text = (CONFIG_DIR / f"{name}.yaml").read_text(encoding="utf-8")
        assert "password:" not in text
        assert "api_key:" not in text
        assert "password_env:" in text


# --- Type and range validation ----------------------------------------------


def test_unknown_top_level_key_is_rejected():
    """A silently-ignored typo is the most expensive class of config bug."""
    with pytest.raises(ConfigError, match="unknown key"):
        build(unexpected_section={})


def test_unknown_nested_key_is_rejected():
    with pytest.raises(ConfigError, match="unknown key"):
        build(gex={"spot_move_pctt": 0.01})


def test_error_message_names_the_offending_path():
    with pytest.raises(ConfigError, match=r"gex\.spot_move_pct"):
        build(gex={"spot_move_pct": "not a number"})


def test_missing_required_key_is_rejected():
    with pytest.raises(ConfigError, match="required key is missing"):
        parse_config({"enabled": True})


def test_wrong_type_is_rejected():
    with pytest.raises(ConfigError, match="expected a number"):
        build(gex={"spot_move_pct": "0.01"})
    with pytest.raises(ConfigError, match="expected a boolean"):
        build(gex={"prefer_vendor_gamma": "yes"})
    with pytest.raises(ConfigError, match="expected an integer"):
        build(zero_gamma={"max_dte_for_grid": 60.5})


def test_out_of_range_values_are_rejected():
    with pytest.raises(ConfigError, match="below the minimum"):
        build(gex={"spot_move_pct": -0.01})
    with pytest.raises(ConfigError, match="above the maximum"):
        build(zero_gamma={"grid_span_pct": 5.0})
    with pytest.raises(ConfigError, match="below the minimum"):
        build(walls={"max_nodes_per_side": 0})


def test_non_finite_values_are_rejected():
    with pytest.raises(ConfigError, match="finite"):
        build(gex={"spot_move_pct": float("nan")})


def test_invalid_enum_value_lists_the_valid_ones():
    with pytest.raises(ConfigError, match="not a valid"):
        build(gex={"sign_convention": "dealer_is_always_right"})
    with pytest.raises(ConfigError, match="ACT/365F"):
        build(model={"day_count_convention": "ACT/999"})


def test_valid_enums_are_coerced_to_their_types():
    loaded = build(
        gex={"sign_convention": "dealer_short_calls_long_puts"},
        model={"day_count_convention": "ACT/252", "iv_price_source": "NBBO_BID_IV"},
    )
    assert loaded.engine.sign_convention is SignConvention.DEALER_SHORT_CALLS_LONG_PUTS
    assert loaded.engine.model_spec.day_count_convention is DayCountConvention.ACT_252
    assert loaded.engine.model_spec.iv_price_source is IVSource.NBBO_BID_IV


# --- Sentinels --------------------------------------------------------------


def test_sentinel_round_trips_as_the_sentinel_object():
    loaded = build(confidence={"max_zero_gamma_shift_pct": "UNSPECIFIED_CALIBRATE"})
    threshold = loaded.engine.confidence.max_zero_gamma_shift_pct
    assert threshold is UNSPECIFIED_CALIBRATE
    assert not is_calibrated(threshold)


def test_a_calibrated_value_is_parsed_as_a_number():
    loaded = build(confidence={"max_zero_gamma_shift_pct": 0.25})
    assert is_calibrated(loaded.engine.confidence.max_zero_gamma_shift_pct)


def test_an_omitted_market_threshold_defaults_to_the_sentinel():
    """Omission must never silently become a number."""
    assert not is_calibrated(build().engine.confidence.max_0dte_dominance_ratio)


def test_sentinel_paths_are_discoverable():
    payload = {"a": {"b": "UNSPECIFIED_CALIBRATE"}, "c": [1, "UNSPECIFIED_CALIBRATE"]}
    assert find_sentinels(payload) == ["a.b", "c[1]"]


# --- Execution safety -------------------------------------------------------


def test_trading_enabled_true_is_rejected_outright():
    """No broker adapter exists, so a config claiming otherwise is describing a
    capability that is not there.
    """
    with pytest.raises(ConfigError, match="no broker adapter"):
        build(execution={"broker": "none", "trading_enabled": True})


def test_a_named_broker_is_rejected():
    with pytest.raises(ConfigError, match="No broker adapter is implemented"):
        build(execution={"broker": "ibkr_paper", "trading_enabled": False})


def test_execution_capable_stage_with_sentinels_is_refused():
    """The gate exists so it cannot be bypassed later by accident, even though
    nothing here is execution-capable today.
    """
    with pytest.raises(ConfigError, match="execution-capable"):
        build(
            stage="PAPER",
            enabled=True,
            confidence={"max_zero_gamma_shift_pct": "UNSPECIFIED_CALIBRATE"},
        )


def test_execution_capable_stage_is_allowed_when_fully_calibrated_and_disabled():
    loaded = build(stage="PAPER", enabled=False)
    assert loaded.profile.stage == "PAPER"
    assert not loaded.profile.enabled


def test_execution_capable_stage_passes_when_every_threshold_is_calibrated():
    loaded = build(
        stage="PAPER",
        enabled=True,
        confidence={
            "max_zero_gamma_shift_pct": 0.25,
            "max_sign_model_disagreement": 0.3,
            "max_0dte_dominance_ratio": 0.6,
        },
    )
    assert loaded.uncalibrated_paths == ()


def test_the_execution_capable_stage_set_is_explicit():
    assert {"PAPER", "LIVE_STAGE_1", "LIVE_STAGE_2"} == EXECUTION_CAPABLE_STAGES


def test_invalid_stage_is_rejected():
    with pytest.raises(ConfigError, match="not one of"):
        build(stage="PRODUCTION")


# --- Unimplemented conventions ----------------------------------------------


def test_configuring_sticky_delta_is_refused_with_an_explanation():
    with pytest.raises(ConfigError, match="sticky_moneyness"):
        build(zero_gamma={"conventions": ["sticky_strike", "sticky_delta"]})


def test_configuring_surface_refit_is_refused():
    with pytest.raises(ConfigError, match="not implemented"):
        build(zero_gamma={"conventions": ["surface_refit"]})


def test_implemented_conventions_are_accepted():
    loaded = build(zero_gamma={"conventions": ["frozen_iv", "sticky_moneyness"]})
    assert loaded.engine.zero_gamma.conventions == (
        IVConvention.FROZEN_IV,
        IVConvention.STICKY_MONEYNESS,
    )


def test_empty_convention_list_is_rejected():
    with pytest.raises(ConfigError, match="non-empty list"):
        build(zero_gamma={"conventions": []})


# --- Environment overrides --------------------------------------------------


def test_environment_variable_override_is_applied_and_recorded(monkeypatch):
    """An env var that changes a model assumption must not be invisible, or two
    runs of "the same config" become silently incomparable.
    """
    monkeypatch.setenv("GEX_RATE", "0.05")
    loaded = build(model={"risk_free_rate": "${GEX_RATE}"})
    assert loaded.engine.model_spec.risk_free_rate == pytest.approx(0.05)
    assert any("GEX_RATE" in entry for entry in loaded.profile.env_overrides)


def test_environment_default_is_used_when_the_variable_is_absent(monkeypatch):
    monkeypatch.delenv("GEX_RATE", raising=False)
    loaded = build(model={"risk_free_rate": "${GEX_RATE:-0.03}"})
    assert loaded.engine.model_spec.risk_free_rate == pytest.approx(0.03)
    assert loaded.profile.env_overrides == ()


def test_missing_environment_variable_without_a_default_fails(monkeypatch):
    monkeypatch.delenv("GEX_MISSING", raising=False)
    with pytest.raises(ConfigError, match="is not set and has no default"):
        build(model={"risk_free_rate": "${GEX_MISSING}"})


def test_environment_override_changes_the_fingerprint(monkeypatch):
    monkeypatch.setenv("GEX_RATE", "0.05")
    with_env = build(model={"risk_free_rate": "${GEX_RATE}"}).fingerprint
    monkeypatch.setenv("GEX_RATE", "0.06")
    other = build(model={"risk_free_rate": "${GEX_RATE}"}).fingerprint
    assert with_env != other


# --- YAML handling ----------------------------------------------------------


def test_duplicate_keys_are_rejected():
    """PyYAML keeps the last occurrence silently, so both values look applied in
    review while only one takes effect.
    """
    with pytest.raises(ConfigError, match="duplicate key"):
        load_yaml("stage: DEVELOPMENT\nenabled: false\nenabled: true\n")


def test_yaml_cannot_construct_arbitrary_python_objects():
    with pytest.raises(ConfigError, match=r"invalid YAML"):
        load_yaml("!!python/object/apply:os.system ['echo pwned']")


def test_invalid_yaml_is_reported_with_the_source_path():
    with pytest.raises(ConfigError, match=r"bad\.yaml"):
        load_yaml("key: [unclosed", source_path="bad.yaml")


def test_missing_file_is_reported():
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(CONFIG_DIR / "nope.yaml")


def test_empty_file_is_rejected(tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="is empty"):
        load_config(empty)


def test_non_mapping_top_level_is_rejected():
    with pytest.raises(ConfigError, match="must be a mapping"):
        parse_config(["a", "list"])  # type: ignore[arg-type]


# --- Fingerprint reaches the snapshot ---------------------------------------


def test_config_fingerprint_flows_into_the_snapshot():
    loaded = load_config(CONFIG_DIR / "research.yaml")
    snapshot = compute_gex_snapshot(build_synthetic_chain(), loaded.engine)
    assert snapshot.config_fingerprint == loaded.fingerprint


def test_changing_a_value_changes_the_fingerprint():
    assert build().fingerprint != build(gex={"spot_move_pct": 0.02}).fingerprint


def test_identical_configs_share_a_fingerprint():
    assert build().fingerprint == build().fingerprint


def test_loaded_config_drives_the_engine_end_to_end():
    loaded = load_config(CONFIG_DIR / "research.yaml")
    snapshot = compute_gex_snapshot(build_synthetic_chain(), loaded.engine)
    assert snapshot.contract_count > 0
    assert snapshot.model_spec.risk_free_rate == pytest.approx(0.042)
    assert snapshot.model_spec.dividend_yield == pytest.approx(0.013)
    assert snapshot.model_spec.minimum_time_to_expiry_minutes == 60.0
