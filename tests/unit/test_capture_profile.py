"""One way to run a session, and a profile that describes the session it runs.

Two v2.1.4 defects, both about the distance between what a configuration says
and what actually happens.

**§7.** Getting a GEX number out of a configured session took four steps, each
of them optional: build the pipeline, fetch through ``runtime.fetch_chain``,
remember ``pipeline=pipeline`` so the compatibility decision reached the
metadata, then call the engine with an engine config obtained separately. Omit
any one and you still got a plausible snapshot -- with a piece of its provenance
missing. ``runtime.fetch_chain(request=...)`` made it worse: it could replace the
symbol, the DTE window and the strike range *after* the compatibility assessment
had been made about a different request.

**§8.** ``config/research.yaml`` pairs ``options_source: synthetic`` with a
populated ``thetadata:`` block, which is right -- the block is there so the
settings are reviewable before anybody spends money. Nothing stopped a file from
setting ``options_source: thetadata`` while leaving ``underlying_price_source:
synthetic``, which would compute real vendor gammas against an underlying
labelled invented.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from src.adapters.transport import FakeTransport
from src.config.pipeline import ThetaDataResearchPipeline
from src.config.schema import ConfigError, load_config
from src.config.thetadata import ThetaDataRuntime

CAPTURE_PROFILE = pathlib.Path("config/thetadata_capture.yaml")


def loaded():
    return load_config(CAPTURE_PROFILE)


def pipeline():
    return ThetaDataResearchPipeline.from_loaded_config(
        loaded(), transport=FakeTransport()
    )


def _write(tmp_path: pathlib.Path, mutate) -> pathlib.Path:
    """The capture profile with one thing changed."""
    import yaml

    raw = yaml.safe_load(CAPTURE_PROFILE.read_text(encoding="utf-8"))
    mutate(raw)
    target = tmp_path / "profile.yaml"
    target.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return target


# =============================================================================
# §7 -- one canonical way to run a session
# =============================================================================


def test_the_pipeline_exposes_the_whole_run():
    """Two calculations since v2.1.5, and the fetch that feeds them."""
    for name in (
        "fetch_chain",
        "fetch_chain_with_external_spot",
        "compute_diagnostic_gex",
        "compute_trusted_gex",
        "validate_integrity",
    ):
        assert callable(getattr(ThetaDataResearchPipeline, name)), name


def test_the_pipeline_api_takes_no_request_override():
    """The seam that let a session fetch something other than what it assessed."""
    parameters = set(
        inspect.signature(ThetaDataResearchPipeline.fetch_chain).parameters
    )
    assert "request" not in parameters
    assert "request" not in set(
        inspect.signature(ThetaDataRuntime.fetch_chain).parameters
    )


def test_the_pipeline_api_takes_no_model_parameter_overrides():
    """The rate and dividend are the ones the compatibility check compared.

    A caller who could pass a different rate here would compute under numbers
    the assessment never saw, while the snapshot still carried the assessment.
    """
    parameters = set(
        inspect.signature(ThetaDataResearchPipeline.fetch_chain).parameters
    )
    for owned in ("risk_free_rate", "dividend_yield", "iv_source", "duplicate_policy"):
        assert owned not in parameters, owned


def test_no_caller_has_to_thread_the_pipeline_through_by_hand():
    """``pipeline=pipeline`` was a keyword argument you had to remember."""
    source = pathlib.Path("src/config/pipeline.py").read_text(encoding="utf-8")
    assert "pipeline=self" in source


def test_the_model_spec_supplies_the_rate_and_dividend():
    built = pipeline()
    assert built.model_spec.risk_free_rate == pytest.approx(0.042)
    assert built.model_spec.dividend_yield == pytest.approx(0.0)


# =============================================================================
# §8 -- a capture profile describes the capture it performs
# =============================================================================


def test_the_capture_profile_loads():
    assert loaded().profile.options_source == "thetadata"


def test_the_capture_profile_captures():
    assert loaded().thetadata.raw_capture_enabled
    assert loaded().thetadata.raw_capture_path is not None


def test_the_capture_profile_names_a_real_underlying():
    config = loaded()
    assert config.engine.model_spec.underlying_price_source.value != "synthetic"
    assert config.thetadata.underlying_price_source != "synthetic"


def test_a_thetadata_profile_with_a_synthetic_underlying_is_refused(tmp_path):
    """The regression."""

    def mutate(raw):
        raw["model"]["underlying_price_source"] = "synthetic"
        raw["thetadata"]["underlying_price_source"] = "synthetic"

    with pytest.raises(ConfigError, match=r"(?i)synthetic|vendor print"):
        load_config(_write(tmp_path, mutate))


def test_a_thetadata_profile_that_discards_its_responses_is_refused(tmp_path):
    def mutate(raw):
        raw["thetadata"]["raw_capture_enabled"] = False
        raw["thetadata"]["raw_capture_path"] = None

    with pytest.raises(ConfigError, match=r"(?i)discard|raw_capture"):
        load_config(_write(tmp_path, mutate))


def test_a_synthetic_profile_may_still_carry_a_thetadata_block():
    """research.yaml is reviewable precisely because it does."""
    research = load_config(pathlib.Path("config/research.yaml"))
    assert research.profile.options_source == "synthetic"
    assert research.thetadata.base_url


def test_the_capture_profile_attests_to_nothing_yet():
    """No comparison has been run, so there is nothing to record.

    If this ever fails, somebody has written down an answer this repository has
    not established -- which is the whole class of defect the attestation type
    exists to make visible.
    """
    assert loaded().thetadata.pricing_attestations == ()


def test_the_capture_profile_cannot_be_trusted_to_calculate():
    """It is a capture profile. Computing comes after the comparison."""
    report = pipeline().pricing_compatibility
    assert not report.compatible
    assert report.load_bearing_unknowns


def test_the_capture_profile_settles_what_it_can():
    """The rate and dividend are ours on both sides, so they are answerable."""
    from src.config.compatibility import CompatibilityStatus, PricingDimension

    report = pipeline().pricing_compatibility
    matched = {
        d.dimension
        for d in report.dimensions
        if d.status is CompatibilityStatus.MATCHED
    }
    # The two numbers this adapter sends. ``rate_units`` and
    # ``dividend_convention`` are not sent, so they are the vendor's conventions
    # and no configuration entry settles them (v2.1.5 §11).
    assert PricingDimension.RISK_FREE_RATE in matched
    assert PricingDimension.DIVIDEND_VALUE in matched
    assert PricingDimension.RATE_UNITS not in matched
    assert PricingDimension.DIVIDEND_CONVENTION not in matched


def test_the_capture_profile_cannot_trade():
    config = loaded()
    assert config.profile.trading_enabled is False
    assert config.profile.broker == "none"
