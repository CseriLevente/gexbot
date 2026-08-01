"""Runtime and ModelSpec come from one place, or the pipeline refuses to build.

Three v2.1.1 defects that only matter together:

* ``ThetaDataRuntime`` carried an ``iv_source``; ``ModelSpec`` carried an
  ``iv_price_source``. Nothing compared them. A config could fetch NBBO-mid IV
  and price with the vendor default, and every individual object would look
  correctly configured.
* ThetaData computes IV under *its* assumptions. Having the number does not mean
  it was produced with our rate, our dividend convention, our expiration
  instant, or our day count. v2.1.1 fed it straight into local gamma and called
  the result internally consistent.
* ``IVSource`` declared ``TRADE_IV`` and ``LOCALLY_SOLVED_MID_IV``. Neither is
  implemented. Selecting one loaded fine and then fell through to the vendor
  default at resolution time, so the operator got a different IV than the one
  they chose, silently.

The unifying question: *can two things that were configured separately ever
disagree without anybody noticing?* If yes, they should not have been separate.
"""

from __future__ import annotations

import pytest

from src.config.compatibility import (
    CompatibilityStatus,
    PricingDimension,
    PricingDimensionResult,
)
from src.config.pipeline import (
    DividendConvention,
    PricingCompatibilityReport,
    PricingMode,
    ThetaDataResearchPipeline,
)
from src.config.thetadata import ThetaDataConfigError, parse_thetadata_config
from src.domain.iv import IVSource
from src.domain.model_spec import ModelSpec


def config(**overrides):
    return parse_thetadata_config(overrides)


def pipeline(**overrides) -> ThetaDataResearchPipeline:
    from src.adapters.transport import FakeTransport

    return ThetaDataResearchPipeline.from_config(
        config(**overrides), transport=FakeTransport()
    )


# =============================================================================
# §2 -- one construction path
# =============================================================================


def test_the_pipeline_produces_every_coordinated_object():
    built = pipeline()
    assert built.runtime is not None
    assert isinstance(built.model_spec, ModelSpec)
    assert built.chain_request is not None
    assert isinstance(built.pricing_compatibility, PricingCompatibilityReport)


def test_runtime_and_model_spec_share_one_iv_source():
    """The regression: v2.1.1 let these diverge silently."""
    built = pipeline(iv_source="NBBO_MID_IV")
    assert built.runtime.iv_source is IVSource.NBBO_MID_IV
    assert built.model_spec.iv_price_source is IVSource.NBBO_MID_IV


def test_a_divergent_iv_source_cannot_be_constructed():
    from src.adapters.transport import FakeTransport
    from src.config.pipeline import PipelineConsistencyError

    with pytest.raises(PipelineConsistencyError, match=r"(?i)iv"):
        ThetaDataResearchPipeline.from_config(
            config(iv_source="NBBO_MID_IV"),
            transport=FakeTransport(),
            model_spec=ModelSpec(iv_price_source=IVSource.NBBO_BID_IV),
        )


def test_a_time_floor_mismatch_fails():
    from src.adapters.transport import FakeTransport
    from src.config.pipeline import PipelineConsistencyError

    with pytest.raises(PipelineConsistencyError, match=r"(?i)floor|min_time"):
        ThetaDataResearchPipeline.from_config(
            config(min_time_to_expiry_minutes=60.0),
            transport=FakeTransport(),
            model_spec=ModelSpec(minimum_time_to_expiry_minutes=5.0),
        )


def test_an_underlying_source_mismatch_fails():
    from src.adapters.transport import FakeTransport
    from src.config.pipeline import PipelineConsistencyError
    from src.domain.model_spec import UnderlyingPriceSource

    with pytest.raises(PipelineConsistencyError, match=r"(?i)underlying"):
        ThetaDataResearchPipeline.from_config(
            config(underlying_price_source="vendor_index_snapshot"),
            transport=FakeTransport(),
            model_spec=ModelSpec(
                underlying_price_source=UnderlyingPriceSource.VENDOR_PER_CONTRACT
            ),
        )


def test_the_factory_applies_every_value_once():
    built = pipeline(max_dte=45, strike_range=20, iv_source="NBBO_MID_IV")
    assert built.chain_request.max_dte == 45
    assert built.chain_request.strike_range == 20
    assert built.runtime.default_chain_request.max_dte == 45


def test_a_caller_cannot_silently_override_one_configured_value():
    """Any override must be checked against the config, not merged into it."""
    import inspect

    parameters = inspect.signature(ThetaDataResearchPipeline.from_config).parameters
    # model_spec exists only so a mismatch can be *detected*; there is no
    # per-field override that would let one value drift.
    assert "iv_source" not in parameters
    assert "risk_free_rate" not in parameters


def test_the_pipeline_fingerprint_changes_with_effective_behaviour():
    assert (
        pipeline(rate_value=4.2).fingerprint() != pipeline(rate_value=3.1).fingerprint()
    )


def test_the_pipeline_fingerprint_is_stable_for_identical_config():
    assert (
        pipeline(rate_value=4.2).fingerprint() == pipeline(rate_value=4.2).fingerprint()
    )


def test_the_fingerprint_covers_both_halves():
    """A change on either side must move it, or the fingerprint is not of the
    pipeline but of one of its parts."""
    base = pipeline(iv_source="VENDOR_DEFAULT_IV").fingerprint()
    assert pipeline(iv_source="NBBO_MID_IV").fingerprint() != base


# =============================================================================
# §3 -- vendor/local pricing compatibility
# =============================================================================


def test_a_fully_aligned_rate_and_dividend_are_compatible():
    """The remaining unknowns are vendor conventions, not our numbers.

    v2.1.2 wrote LOCAL_IV_LOCAL_GAMMA here, which short-circuited the whole
    assessment. That mode is unreachable now -- every supported IV source is
    vendor-computed -- so this asserts on the two dimensions we *can* settle.
    """
    built = pipeline(
        rate_value=4.2,
        rate_units="PERCENT_ANNUAL_RATE",
        dividend_convention="ZERO_DIVIDEND",
    )
    report = built.pricing_compatibility
    settled = {
        d.dimension: d
        for d in report.dimensions
        if d.status is CompatibilityStatus.MATCHED
    }
    # Only the two numbers we actually send are settleable from configuration.
    # ``rate_units`` and ``dividend_convention`` are not parameters this adapter
    # sends, so since v2.1.5 they are the vendor's conventions and stay unknown.
    assert PricingDimension.RISK_FREE_RATE in settled
    assert PricingDimension.DIVIDEND_VALUE in settled
    assert set(report.load_bearing_unknowns) == {
        PricingDimension.DAY_COUNT,
        PricingDimension.DIVIDEND_CONVENTION,
        PricingDimension.EXPIRATION_TIMESTAMP,
        PricingDimension.IV_PRICE_BASIS,
        PricingDimension.MINIMUM_TIME_FLOOR,
        PricingDimension.RATE_UNITS,
        PricingDimension.UNDERLYING_SOURCE,
        PricingDimension.UNDERLYING_TIMESTAMP,
    }


def test_vendor_iv_with_unknown_dividend_convention_is_not_compatible():
    """The vendor's `annual_dividend` may be cash or yield. We do not know."""
    built = pipeline(
        annual_dividend=1.3,
        dividend_convention="UNKNOWN_VENDOR_CONVENTION",
    )
    report = built.pricing_compatibility
    assert not report.compatible
    assert PricingDimension.DIVIDEND_CONVENTION in report.load_bearing_unknowns


def test_cash_dividend_is_not_interchangeable_with_a_yield():
    from src.config.pipeline import DividendAssumption, check_dividend_compatibility

    cash = check_dividend_compatibility(
        vendor=DividendAssumption(
            convention=DividendConvention.ANNUAL_CASH_DIVIDEND, value=65.0
        ),
        local=DividendAssumption(
            convention=DividendConvention.CONTINUOUS_DIVIDEND_YIELD, value=0.013
        ),
    )
    assert not cash.compatible
    assert PricingDimension.DIVIDEND_CONVENTION in cash.load_bearing_mismatches


def test_a_matching_zero_dividend_settles_its_value():
    """The convention stays the vendor's; zero makes the value independent of it."""
    from src.config.pipeline import DividendAssumption, check_dividend_compatibility

    report = check_dividend_compatibility(
        vendor=DividendAssumption(
            convention=DividendConvention.ZERO_DIVIDEND, value=0.0
        ),
        local=DividendAssumption(
            convention=DividendConvention.ZERO_DIVIDEND, value=0.0
        ),
    )
    matched = {
        d.dimension
        for d in report.dimensions
        if d.status is CompatibilityStatus.MATCHED
    }
    assert PricingDimension.DIVIDEND_VALUE in matched


def test_incompatible_assumptions_block_the_research_calculation():
    from src.config.pipeline import PipelineConsistencyError

    with pytest.raises(PipelineConsistencyError, match=r"(?i)compat"):
        pipeline(
            pricing_mode="VENDOR_IV_LOCAL_GAMMA",
            dividend_convention="UNKNOWN_VENDOR_CONVENTION",
            annual_dividend=1.3,
            fail_on_incompatible_pricing=True,
        )


def test_the_report_lands_in_audit_metadata():
    built = pipeline()
    payload = built.as_dict()
    assert "pricing_compatibility" in payload
    assert "compatible" in payload["pricing_compatibility"]


def test_local_iv_local_gamma_is_unreachable_until_a_solver_exists():
    """v2.1.2 asserted this mode "does not require vendor agreement", which was
    true of the mode and false of the configuration: the IV was still the
    vendor's."""
    with pytest.raises(ThetaDataConfigError):
        pipeline(pricing_mode="LOCAL_IV_LOCAL_GAMMA", iv_source="NBBO_MID_IV")


def test_the_reachable_modes_are_selectable():
    """LOCAL_IV_LOCAL_GAMMA is documented but unreachable; see §1."""
    assert pipeline().pricing_mode is PricingMode.VENDOR_IV_LOCAL_GAMMA
    # The gamma comparison is a second field, so it leaves the mode alone.
    assert (
        pipeline(vendor_gamma_policy="COMPARE_ONLY", tier="pro").pricing_mode
        is PricingMode.VENDOR_IV_LOCAL_GAMMA
    )


def test_an_unknown_pricing_mode_is_refused():
    with pytest.raises(ThetaDataConfigError, match=r"(?i)pricing_mode"):
        config(pricing_mode="MAGIC")


def test_the_report_lists_what_it_checked():
    """Every dimension is accounted for, with no silent omissions."""
    report = pipeline().pricing_compatibility
    assert {d.dimension for d in report.dimensions} == set(PricingDimension)


def test_the_report_separates_unknown_from_incompatible():
    """Different findings with different remedies: one needs a config change,
    the other needs vendor documentation."""
    report = pipeline().pricing_compatibility
    assert set(report.load_bearing_unknowns).isdisjoint(report.load_bearing_mismatches)


def test_a_dimension_carries_one_status_and_cannot_be_two_things():
    report = pipeline().pricing_compatibility
    seen = [d.dimension for d in report.dimensions]
    assert len(seen) == len(set(seen))


def test_rewording_a_detail_cannot_move_a_decision():
    """The v2.1.4 regression for §2.

    v2.1.3 decided which unknowns were load-bearing by searching the message for
    a field name, so an editorial change to the wording flipped a blocker into a
    warning. Detail is now carried outside every decision and every hash.
    """
    from dataclasses import replace

    report = pipeline().pricing_compatibility
    reworded = PricingCompatibilityReport(
        dimensions=tuple(
            replace(d, detail="the interest rate convention is not published")
            for d in report.dimensions
        ),
        hard_failures=report.hard_failures,
    )
    assert reworded.compatible is report.compatible
    assert reworded.load_bearing_unknowns == report.load_bearing_unknowns
    assert reworded.semantic_payload() == report.semantic_payload()


def test_compatibility_is_derived_and_cannot_be_assigned():
    """v2.1.3 carried a settable ``compatible`` flag next to the findings."""
    with pytest.raises(TypeError):
        PricingCompatibilityReport(compatible=True)  # type: ignore[call-arg]


def test_a_hard_failure_is_honoured_even_with_every_dimension_resolved():
    resolved = PricingCompatibilityReport(
        dimensions=tuple(
            PricingDimensionResult(
                dimension=dimension,
                status=CompatibilityStatus.NOT_APPLICABLE,
                code="NOT_APPLICABLE",
            )
            for dimension in PricingDimension
        ),
        hard_failures=("TIER_CANNOT_SERVE_REQUEST",),
    )
    assert not resolved.blocking_dimensions
    assert not resolved.compatible


# =============================================================================
# §10 -- IV modes that are declared but not implemented
# =============================================================================


@pytest.mark.parametrize("mode", ["TRADE_IV", "LOCALLY_SOLVED_MID_IV"])
def test_an_unimplemented_iv_source_fails_configuration(mode):
    with pytest.raises(ThetaDataConfigError, match=r"(?i)not implemented|unsupported"):
        config(iv_source=mode)


@pytest.mark.parametrize("mode", ["TRADE_IV", "LOCALLY_SOLVED_MID_IV"])
def test_an_unimplemented_iv_source_cannot_reach_the_runtime(mode):
    with pytest.raises(ThetaDataConfigError):
        pipeline(iv_source=mode)


def test_an_unimplemented_iv_source_never_becomes_the_vendor_default():
    """v2.1.1 accepted TRADE_IV and then resolved it as VENDOR_DEFAULT_IV."""
    with pytest.raises(ThetaDataConfigError):
        config(iv_source="TRADE_IV")
    # And the supported default is genuinely a different value, so the fallback
    # v2.1.1 performed was not a no-op.
    assert IVSource.TRADE_IV is not IVSource.VENDOR_DEFAULT_IV


@pytest.mark.parametrize(
    "mode", ["NBBO_BID_IV", "NBBO_MID_IV", "NBBO_ASK_IV", "VENDOR_DEFAULT_IV"]
)
def test_a_supported_iv_source_is_accepted_and_applied(mode):
    assert config(iv_source=mode).iv_source is IVSource(mode)
    assert pipeline(iv_source=mode).model_spec.iv_price_source is IVSource(mode)


def test_the_supported_set_is_stated_once():
    from src.config.pipeline import SUPPORTED_IV_SOURCES, UNSUPPORTED_IV_SOURCES

    assert frozenset() == SUPPORTED_IV_SOURCES & UNSUPPORTED_IV_SOURCES
    assert frozenset(IVSource) == SUPPORTED_IV_SOURCES | UNSUPPORTED_IV_SOURCES


def test_the_error_says_what_is_supported():
    with pytest.raises(ThetaDataConfigError) as excinfo:
        config(iv_source="TRADE_IV")
    assert "NBBO_MID_IV" in str(excinfo.value)


# =============================================================================
# Trading remains impossible
# =============================================================================


def test_the_pipeline_exposes_no_execution_surface():
    """Inspect the project's own API, not everything the runtime attaches.

    ``dir()`` includes interpreter-provided attributes -- Python 3.13 adds
    ``__replace__`` to frozen dataclasses, and "place" is a substring of it. A
    safety check that fires on an unrelated language feature is a check people
    learn to ignore.
    """
    built = pipeline()
    declared = {name for name in vars(type(built)) if not name.startswith("_")} | set(
        type(built).__dataclass_fields__
    )
    for banned in ("place_order", "submit_order", "execute", "broker", "position_size"):
        assert not any(banned in name for name in declared), banned
