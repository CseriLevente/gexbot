"""One configuration file, one coherent session.

The v2.1.2 defect. ``ThetaDataResearchPipeline.from_config`` took only the
``thetadata:`` subsection and derived its own ``ModelSpec``, so the top-level
``model:`` block was never consulted. The repository's own ``research.yaml``
then shipped::

    model:
      iv_price_source: NBBO_MID_IV      # what the engine priced with
    thetadata:
      # iv_source absent -> VENDOR_DEFAULT_IV   # what the adapter fetched

Two different implied volatilities in one file, and nothing said so, because
nothing ever compared the halves.
"""

from __future__ import annotations

import pathlib

import pytest

from src.adapters.transport import FakeTransport
from src.config.pipeline import PipelineConsistencyError, ThetaDataResearchPipeline
from src.config.schema import load_config, parse_config

CONFIG_DIR = pathlib.Path(__file__).resolve().parents[2] / "config"


def build(loaded):
    return ThetaDataResearchPipeline.from_loaded_config(
        loaded, transport=FakeTransport()
    )


def loaded_with(**thetadata_overrides):
    """A minimal but complete top-level configuration."""
    model = {
        "iv_price_source": "NBBO_MID_IV",
        "underlying_price_source": "vendor_index_snapshot",
        "min_time_to_expiry_minutes": 60.0,
        "expiration_timestamp_rule": "root_specific_settlement",
    }
    thetadata = {
        "tier": "standard",
        "iv_source": "NBBO_MID_IV",
        "underlying_price_source": "vendor_index_snapshot",
        "min_time_to_expiry_minutes": 60.0,
        "expiration_rule": "root_specific_settlement",
        # options_source is `thetadata` below, and since v2.1.4 such a profile
        # has to keep what it fetches -- a paid session whose responses are
        # discarded produces numbers nobody can re-derive.
        "raw_capture_enabled": True,
        "raw_capture_path": "artifacts/raw",
    }
    thetadata.update(thetadata_overrides)
    return parse_config(
        {
            "stage": "DEVELOPMENT",
            "enabled": True,
            "data": {"options_source": "thetadata"},
            "execution": {"broker": "none", "trading_enabled": False},
            "model": model,
            "thetadata": thetadata,
        }
    )


# =============================================================================
# The shipped configuration
# =============================================================================


def test_the_repository_research_config_builds_one_pipeline():
    """The regression: v2.1.2's own research.yaml was internally inconsistent."""
    pipeline = build(load_config(CONFIG_DIR / "research.yaml"))
    assert pipeline.model_spec.iv_price_source is pipeline.config.iv_source


def test_the_research_config_states_a_coherent_pricing_mode():
    from src.config.pipeline import PricingMode

    pipeline = build(load_config(CONFIG_DIR / "research.yaml"))
    assert pipeline.pricing_mode is PricingMode.VENDOR_IV_LOCAL_GAMMA


def test_the_research_config_tier_serves_its_mode():
    assert build(
        load_config(CONFIG_DIR / "research.yaml")
    ).subscription_capability.satisfied


@pytest.mark.parametrize("name", ["paper.yaml", "live.yaml"])
def test_the_disabled_profiles_still_load(name):
    """They must remain loadable, disabled, and explicitly incomplete."""
    loaded = load_config(CONFIG_DIR / name)
    assert loaded.profile.enabled is False
    assert loaded.profile.trading_enabled is False
    assert loaded.profile.broker == "none"


# =============================================================================
# One factory, and it checks both halves
# =============================================================================


def test_from_loaded_config_produces_every_coordinated_object():
    pipeline = build(loaded_with())
    for attribute in (
        "runtime",
        "model_spec",
        "chain_request",
        "pricing_mode",
        "pricing_compatibility",
        "engine_config",
        "subscription_capability",
    ):
        assert getattr(pipeline, attribute) is not None, attribute


def test_a_top_level_iv_mismatch_fails():
    """The exact shape of the defect."""
    with pytest.raises(PipelineConsistencyError, match=r"(?i)iv_source"):
        build(loaded_with(iv_source="NBBO_BID_IV"))


def test_a_top_level_underlying_mismatch_fails():
    with pytest.raises(PipelineConsistencyError, match=r"(?i)underlying"):
        build(loaded_with(underlying_price_source="vendor_per_contract"))


def test_a_top_level_time_floor_mismatch_fails():
    with pytest.raises(PipelineConsistencyError, match=r"(?i)floor"):
        build(loaded_with(min_time_to_expiry_minutes=5.0))


def test_a_top_level_expiration_rule_mismatch_fails():
    with pytest.raises(PipelineConsistencyError, match=r"(?i)expiration"):
        build(loaded_with(expiration_rule="fixed_1600_et"))


def test_the_pipeline_uses_the_top_level_model_not_a_derived_one():
    """v2.1.2 built its own ModelSpec and discarded the file's."""
    loaded = loaded_with()
    assert build(loaded).model_spec is loaded.engine.model_spec


def test_the_engine_config_comes_from_the_file():
    loaded = loaded_with()
    assert build(loaded).engine_config is loaded.engine


def test_a_caller_cannot_repair_the_file_by_passing_a_second_model():
    """The signature takes the whole config; there is no per-field override."""
    import inspect

    parameters = set(
        inspect.signature(ThetaDataResearchPipeline.from_loaded_config).parameters
    )
    assert "model_spec" not in parameters
    assert "iv_source" not in parameters


# =============================================================================
# The fingerprint covers what changes a number
# =============================================================================


def test_the_fingerprint_reflects_the_thetadata_half():
    assert (
        build(loaded_with(max_dte=30)).fingerprint()
        != build(loaded_with(max_dte=60)).fingerprint()
    )


def test_the_fingerprint_reflects_the_tier():
    assert (
        build(loaded_with(tier="pro")).fingerprint()
        != build(loaded_with(tier="standard")).fingerprint()
    )


def test_the_fingerprint_is_stable_for_an_identical_file():
    assert build(loaded_with()).fingerprint() == build(loaded_with()).fingerprint()


def test_the_serialised_pipeline_names_its_unresolved_fields():
    payload = build(loaded_with()).as_dict()
    assert "load_bearing_unknowns" in payload
    assert "subscription_capability" in payload


def test_an_incoherent_file_fails_before_any_request():
    transport = FakeTransport()
    with pytest.raises(PipelineConsistencyError):
        ThetaDataResearchPipeline.from_loaded_config(
            loaded_with(iv_source="NBBO_ASK_IV"), transport=transport
        )
    assert transport.call_count == 0


def test_trading_stays_disabled_in_every_profile():
    for name in ("research.yaml", "paper.yaml", "live.yaml"):
        assert load_config(CONFIG_DIR / name).profile.trading_enabled is False
