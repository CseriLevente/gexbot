"""Where a number came from decides what it may be called.

The central v2.1.2 misconception: **NBBO bid/mid/ask IV is vendor-computed IV.**

ThetaData solves the implied volatility. That the *option price* it solved
against was an NBBO bid, midpoint or ask says nothing about who did the solving,
or under what rate, dividend, expiration instant and day count. All four
currently supported sources -- ``VENDOR_DEFAULT_IV``, ``NBBO_BID_IV``,
``NBBO_MID_IV``, ``NBBO_ASK_IV`` -- are vendor output.

So the default configuration was internally contradictory::

    iv_source    = VENDOR_DEFAULT_IV      # vendor-computed
    pricing_mode = LOCAL_IV_LOCAL_GAMMA   # claims nothing vendor-computed is used

and `LOCAL_IV_LOCAL_GAMMA` is the one mode that requires *no* vendor/local
agreement. Labelling a vendor-IV session that way skipped every compatibility
check, and adapter certification then reported ready.

The only genuinely local IV would come from a local solver
(``LOCALLY_SOLVED_MID_IV``), which is not implemented. Until it is,
`LOCAL_IV_LOCAL_GAMMA` is unreachable and must fail rather than mislabel.
"""

from __future__ import annotations

import pytest

from src.config.pipeline import (
    DividendConvention,
    PipelineConsistencyError,
    PricingMode,
    RateUnit,
    ThetaDataResearchPipeline,
    derive_pricing_mode,
)
from src.config.thetadata import ThetaDataConfigError, parse_thetadata_config
from src.domain.iv import IVSource

VENDOR_IV_SOURCES = [
    IVSource.VENDOR_DEFAULT_IV,
    IVSource.NBBO_BID_IV,
    IVSource.NBBO_MID_IV,
    IVSource.NBBO_ASK_IV,
]


def config(**overrides):
    return parse_thetadata_config(overrides)


def pipeline(**overrides) -> ThetaDataResearchPipeline:
    from src.adapters.transport import FakeTransport

    return ThetaDataResearchPipeline.from_config(
        config(**overrides), transport=FakeTransport()
    )


# =============================================================================
# §1 -- pricing mode follows provenance
# =============================================================================


@pytest.mark.parametrize("source", VENDOR_IV_SOURCES)
def test_a_vendor_iv_source_cannot_claim_local_iv(source):
    """The regression. Every one of these is solved by ThetaData."""
    with pytest.raises(ThetaDataConfigError, match=r"(?i)vendor|local_iv"):
        config(iv_source=source.value, pricing_mode="LOCAL_IV_LOCAL_GAMMA")


@pytest.mark.parametrize("source", VENDOR_IV_SOURCES)
def test_nbbo_derived_iv_is_still_vendor_iv(source):
    """An NBBO *price* basis does not make the IV local."""
    assert derive_pricing_mode(iv_source=source) is PricingMode.VENDOR_IV_LOCAL_GAMMA


def test_the_only_local_iv_source_is_the_unimplemented_solver():
    from src.config.pipeline import LOCAL_IV_SOURCES

    assert frozenset({IVSource.LOCALLY_SOLVED_MID_IV}) == LOCAL_IV_SOURCES
    assert not (LOCAL_IV_SOURCES & frozenset(VENDOR_IV_SOURCES))


def test_local_iv_local_gamma_is_unreachable_today():
    """It requires a solver that does not exist, so it cannot be selected."""
    with pytest.raises(ThetaDataConfigError):
        config(pricing_mode="LOCAL_IV_LOCAL_GAMMA")


def test_the_unimplemented_solver_still_cannot_reach_the_runtime():
    with pytest.raises(ThetaDataConfigError, match=r"(?i)not implemented"):
        config(iv_source="LOCALLY_SOLVED_MID_IV")


def test_the_default_configuration_is_now_coherent():
    """v2.1.2 shipped VENDOR_DEFAULT_IV with LOCAL_IV_LOCAL_GAMMA."""
    built = config()
    assert built.pricing_mode is PricingMode.VENDOR_IV_LOCAL_GAMMA


def test_pricing_mode_is_derived_deterministically():
    assert derive_pricing_mode(iv_source=IVSource.NBBO_MID_IV) is derive_pricing_mode(
        iv_source=IVSource.NBBO_MID_IV
    )


def test_vendor_gamma_validation_is_selectable_with_vendor_iv():
    """Comparing vendor gamma is orthogonal to where the IV came from."""
    assert config(pricing_mode="VENDOR_GAMMA_VALIDATION", tier="pro").pricing_mode is (
        PricingMode.VENDOR_GAMMA_VALIDATION
    )


def test_a_contradictory_manual_mode_fails():
    with pytest.raises(ThetaDataConfigError):
        config(iv_source="NBBO_MID_IV", pricing_mode="LOCAL_IV_LOCAL_GAMMA")


def test_no_supported_mode_aggregates_vendor_gamma():
    from src.config.pipeline import MODE_CAPABILITIES

    for mode, capability in MODE_CAPABILITIES.items():
        assert capability.local_gamma_used_for_gex, mode
        assert not capability.vendor_gamma_used_for_gex, mode


# =============================================================================
# §6 -- gamma policy must match the pricing mode
# =============================================================================


def test_no_supported_mode_may_prefer_vendor_gamma():
    from src.gex.config import GexEngineConfig

    with pytest.raises(PipelineConsistencyError, match=r"(?i)vendor gamma"):
        ThetaDataResearchPipeline.from_config(
            config(),
            transport=__import__(
                "src.adapters.transport", fromlist=["FakeTransport"]
            ).FakeTransport(),
            engine_config=GexEngineConfig(prefer_vendor_gamma=True),
        )


def test_validation_mode_still_cannot_aggregate_vendor_gamma():
    from src.adapters.transport import FakeTransport
    from src.gex.config import GexEngineConfig

    with pytest.raises(PipelineConsistencyError):
        ThetaDataResearchPipeline.from_config(
            config(pricing_mode="VENDOR_GAMMA_VALIDATION", tier="pro"),
            transport=FakeTransport(),
            engine_config=GexEngineConfig(prefer_vendor_gamma=True),
        )


def test_the_gamma_policy_is_part_of_the_fingerprint():
    from src.adapters.transport import FakeTransport
    from src.gex.config import GexEngineConfig

    base = pipeline().fingerprint()
    other = ThetaDataResearchPipeline.from_config(
        config(),
        transport=FakeTransport(),
        engine_config=GexEngineConfig(spot_move_pct=0.02),
    ).fingerprint()
    assert base != other


# =============================================================================
# §4 -- rate compatibility compares source, value and units
# =============================================================================


def test_a_vendor_percent_rate_normalises_to_a_local_decimal():
    from src.config.pipeline import RateAssumption, check_rate_compatibility

    report = check_rate_compatibility(
        vendor=RateAssumption(
            source="sofr", raw_value=4.2, unit=RateUnit.PERCENT_ANNUAL_RATE
        ),
        local=RateAssumption(
            source="configured_constant",
            raw_value=0.042,
            unit=RateUnit.DECIMAL_ANNUAL_RATE,
        ),
    )
    assert report.compatible


def test_unknown_vendor_rate_units_are_not_compatible():
    from src.config.pipeline import RateAssumption, check_rate_compatibility

    report = check_rate_compatibility(
        vendor=RateAssumption(source="sofr", raw_value=4.2, unit=RateUnit.UNKNOWN),
        local=RateAssumption(
            source="configured_constant",
            raw_value=0.042,
            unit=RateUnit.DECIMAL_ANNUAL_RATE,
        ),
    )
    assert not report.compatible
    assert report.unknown_fields


def test_a_null_vendor_rate_is_unknown_not_matched():
    """The regression: sending no value was treated as nothing to disagree about."""
    from src.config.pipeline import RateAssumption, check_rate_compatibility

    report = check_rate_compatibility(
        vendor=RateAssumption(
            source="sofr", raw_value=None, unit=RateUnit.UNKNOWN, vendor_default=True
        ),
        local=RateAssumption(
            source="configured_constant",
            raw_value=0.042,
            unit=RateUnit.DECIMAL_ANNUAL_RATE,
        ),
    )
    assert not report.compatible
    assert any("UNKNOWN_VENDOR_DEFAULT" in f for f in report.unknown_fields)


def test_normalised_values_must_actually_match():
    from src.config.pipeline import RateAssumption, check_rate_compatibility

    report = check_rate_compatibility(
        vendor=RateAssumption(
            source="sofr", raw_value=5.0, unit=RateUnit.PERCENT_ANNUAL_RATE
        ),
        local=RateAssumption(
            source="configured_constant",
            raw_value=0.042,
            unit=RateUnit.DECIMAL_ANNUAL_RATE,
        ),
    )
    assert not report.compatible
    assert report.incompatible_fields


def test_a_rate_source_mismatch_is_reported():
    from src.config.pipeline import RateAssumption, check_rate_compatibility

    report = check_rate_compatibility(
        vendor=RateAssumption(
            source="treasury_y10", raw_value=4.2, unit=RateUnit.PERCENT_ANNUAL_RATE
        ),
        local=RateAssumption(
            source="sofr", raw_value=0.042, unit=RateUnit.DECIMAL_ANNUAL_RATE
        ),
    )
    assert any("source" in f for f in report.warnings + report.incompatible_fields)


# =============================================================================
# §5 -- dividend compatibility compares convention AND value
# =============================================================================


def test_cash_and_yield_are_not_interchangeable():
    from src.config.pipeline import DividendAssumption, check_dividend_compatibility

    report = check_dividend_compatibility(
        vendor=DividendAssumption(
            convention=DividendConvention.ANNUAL_CASH_DIVIDEND, value=65.0
        ),
        local=DividendAssumption(
            convention=DividendConvention.CONTINUOUS_DIVIDEND_YIELD, value=0.013
        ),
    )
    assert not report.compatible


def test_the_same_convention_with_different_values_is_incompatible():
    """The regression: v2.1.2 compared conventions and stopped."""
    from src.config.pipeline import DividendAssumption, check_dividend_compatibility

    report = check_dividend_compatibility(
        vendor=DividendAssumption(
            convention=DividendConvention.CONTINUOUS_DIVIDEND_YIELD, value=0.02
        ),
        local=DividendAssumption(
            convention=DividendConvention.CONTINUOUS_DIVIDEND_YIELD, value=0.01
        ),
    )
    assert not report.compatible
    assert report.incompatible_fields


def test_the_same_convention_and_value_is_compatible():
    from src.config.pipeline import DividendAssumption, check_dividend_compatibility

    assert check_dividend_compatibility(
        vendor=DividendAssumption(
            convention=DividendConvention.CONTINUOUS_DIVIDEND_YIELD, value=0.013
        ),
        local=DividendAssumption(
            convention=DividendConvention.CONTINUOUS_DIVIDEND_YIELD, value=0.013
        ),
    ).compatible


def test_an_explicit_zero_dividend_stays_valid():
    from src.config.pipeline import DividendAssumption, check_dividend_compatibility

    assert check_dividend_compatibility(
        vendor=DividendAssumption(
            convention=DividendConvention.ZERO_DIVIDEND, value=0.0
        ),
        local=DividendAssumption(
            convention=DividendConvention.ZERO_DIVIDEND, value=0.0
        ),
    ).compatible


def test_an_unknown_vendor_convention_is_unknown_not_incompatible():
    from src.config.pipeline import DividendAssumption, check_dividend_compatibility

    report = check_dividend_compatibility(
        vendor=DividendAssumption(
            convention=DividendConvention.UNKNOWN_VENDOR_CONVENTION, value=1.3
        ),
        local=DividendAssumption(
            convention=DividendConvention.CONTINUOUS_DIVIDEND_YIELD, value=0.013
        ),
    )
    assert not report.compatible
    assert report.unknown_fields


# =============================================================================
# §7 -- the tier must expose what the mode needs
# =============================================================================


def test_standard_tier_cannot_request_vendor_gamma_validation():
    """Second-order greeks are Pro-only, so there is no vendor gamma to compare."""
    with pytest.raises(ThetaDataConfigError, match=r"(?i)tier"):
        config(tier="standard", pricing_mode="VENDOR_GAMMA_VALIDATION")


def test_value_tier_cannot_supply_vendor_iv():
    """First-order greeks carry implied_vol and need Standard."""
    with pytest.raises(ThetaDataConfigError, match=r"(?i)tier"):
        config(tier="value", iv_source="NBBO_MID_IV")


def test_pro_tier_supports_vendor_gamma_validation():
    assert config(tier="pro", pricing_mode="VENDOR_GAMMA_VALIDATION") is not None


def test_standard_tier_supports_vendor_iv_local_gamma():
    assert config(tier="standard").pricing_mode is PricingMode.VENDOR_IV_LOCAL_GAMMA


def test_the_capability_matrix_marks_uncertainty_explicitly():
    from src.adapters.thetadata.capabilities import TIER_CAPABILITIES, Capability

    for tier, capabilities in TIER_CAPABILITIES.items():
        for name, state in capabilities.items():
            assert isinstance(state, Capability), (tier, name)


def test_the_tier_report_reaches_the_pipeline():
    assert pipeline().subscription_capability.tier is not None
