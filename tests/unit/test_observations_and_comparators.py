"""Evidence is an observed value, not a verdict.

**§5.** ``PricingAssumptionAttestation`` set a dimension to ``MATCHED``. It
carried a ``vendor_value`` field, and nothing ever read it: recording that the
vendor uses ACT/360 while the local model uses ACT/365F produced ``MATCHED``,
because the object's presence *was* the answer. Observing a disagreement is the
one thing evidence most needs to be able to express.

**§4.** A YAML file could state ``source: LIVE_COMPARISON`` -- that a comparison
against real vendor output had been run. Nothing had been run. A static file
cannot witness an event.

**§11.** ``rate_units`` and ``dividend_convention`` were treated as locally
owned, on the reasoning that we configure them. We do not send them. The adapter
sends ``rate_value`` and ``annual_dividend``; how the vendor *reads* those is
the vendor's convention, and a local YAML entry cannot settle it.
"""

from __future__ import annotations

import pytest

from src.config.compatibility import (
    CompatibilityStatus,
    EvidenceSource,
    PricingDimension,
    VendorObservation,
    compare_observation,
)
from src.config.thetadata import ThetaDataConfigError, parse_thetadata_config
from src.domain.model_spec import ModelSpec

SPEC = ModelSpec()


def observe(dimension: PricingDimension, value: object) -> VendorObservation:
    return VendorObservation(
        dimension=dimension,
        observed_value=value,
        source=EvidenceSource.VENDOR_DOCUMENTATION,
        reference="tests/fixtures/vendor_conventions.md",
        observed_at="2026-08-01",
    )


# =============================================================================
# §5 -- a comparator derives the status; evidence cannot assert it
# =============================================================================


def test_a_matching_day_count_matches():
    result = compare_observation(observe(PricingDimension.DAY_COUNT, "ACT/365F"), SPEC)
    assert result.status is CompatibilityStatus.MATCHED


def test_act_360_against_act_365f_is_a_mismatch():
    """The regression. v2.1.4 reported MATCHED for this."""
    result = compare_observation(observe(PricingDimension.DAY_COUNT, "ACT/360"), SPEC)
    assert result.status is CompatibilityStatus.MISMATCHED
    assert result.vendor_value == "ACT/360"
    assert result.local_value == SPEC.day_count_convention.value


def test_a_thirty_minute_vendor_floor_against_sixty_is_a_mismatch():
    result = compare_observation(
        observe(PricingDimension.MINIMUM_TIME_FLOOR, 30.0), SPEC
    )
    assert result.status is CompatibilityStatus.MISMATCHED


def test_a_matching_time_floor_matches():
    result = compare_observation(
        observe(PricingDimension.MINIMUM_TIME_FLOOR, 60.0), SPEC
    )
    assert result.status is CompatibilityStatus.MATCHED


#: What the *default* ``ModelSpec`` says, per dimension, and something else.
#: Read off the spec rather than written out, so the table cannot drift from the
#: model it is meant to be comparing against.
COMPARABLE = [
    (PricingDimension.IV_PRICE_BASIS, SPEC.iv_price_source.value, "TRADE_IV"),
    (
        PricingDimension.UNDERLYING_SOURCE,
        SPEC.underlying_price_source.value,
        "synthetic_forward",
    ),
    (
        PricingDimension.UNDERLYING_TIMESTAMP,
        "OPTION_QUOTE_INSTANT",
        "PREVIOUS_CLOSE",
    ),
    (
        PricingDimension.EXPIRATION_TIMESTAMP,
        SPEC.expiration_timestamp_rule.value,
        "uniform_1600_et",
    ),
    (PricingDimension.DAY_COUNT, SPEC.day_count_convention.value, "ACT/360"),
    (
        PricingDimension.MINIMUM_TIME_FLOOR,
        SPEC.minimum_time_to_expiry_minutes,
        SPEC.minimum_time_to_expiry_minutes / 2,
    ),
    (
        PricingDimension.RISK_FREE_RATE,
        SPEC.risk_free_rate,
        (SPEC.risk_free_rate or 0.0) + 0.01,
    ),
    (PricingDimension.RATE_UNITS, "DECIMAL_ANNUAL_RATE", "PERCENT_ANNUAL_RATE"),
    (PricingDimension.DIVIDEND_CONVENTION, "ZERO_DIVIDEND", "ANNUAL_CASH_DIVIDEND"),
    (
        PricingDimension.DIVIDEND_VALUE,
        SPEC.dividend_yield,
        (SPEC.dividend_yield or 0.0) + 0.01,
    ),
]


@pytest.mark.parametrize(
    ("dimension", "agreeing", "differing"),
    COMPARABLE,
    ids=[entry[0].value for entry in COMPARABLE],
)
def test_every_comparable_dimension_can_report_a_mismatch(
    dimension, agreeing, differing
):
    """A comparator that cannot disagree is not a comparator."""
    assert (
        compare_observation(observe(dimension, agreeing), SPEC).status
        is CompatibilityStatus.MATCHED
    ), dimension
    assert (
        compare_observation(observe(dimension, differing), SPEC).status
        is CompatibilityStatus.MISMATCHED
    ), dimension


def test_the_comparators_cover_every_dimension_but_the_solver_version():
    """A dimension with no comparator silently stays UNKNOWN, so name them."""
    covered = {dimension for dimension, _, _ in COMPARABLE}
    assert set(PricingDimension) - covered == {PricingDimension.SOLVER_VERSION}


def test_the_solver_version_has_no_local_counterpart_so_stays_unknown():
    """Where no meaningful comparison exists, UNKNOWN is the honest answer."""
    result = compare_observation(
        observe(PricingDimension.SOLVER_VERSION, "theta-iv-7"), SPEC
    )
    assert result.status is CompatibilityStatus.UNKNOWN


def test_an_unreadable_observed_value_is_unknown_not_matched():
    result = compare_observation(
        observe(PricingDimension.MINIMUM_TIME_FLOOR, "sometime soonish"), SPEC
    )
    assert result.status is CompatibilityStatus.UNKNOWN


def test_an_observation_carries_the_value_into_the_result():
    result = compare_observation(observe(PricingDimension.DAY_COUNT, "ACT/360"), SPEC)
    assert result.evidence is not None
    assert result.evidence.source is EvidenceSource.VENDOR_DOCUMENTATION


def test_an_observation_without_a_value_cannot_be_constructed():
    from src.config.compatibility import AttestationError

    with pytest.raises(AttestationError, match=r"observed_value"):
        VendorObservation(
            dimension=PricingDimension.DAY_COUNT,
            observed_value=None,
            source=EvidenceSource.VENDOR_DOCUMENTATION,
            reference="somewhere",
            observed_at="2026-08-01",
        )


# =============================================================================
# §4 -- a static file cannot witness a live comparison
# =============================================================================


def test_live_comparison_in_yaml_is_rejected():
    """The regression."""
    with pytest.raises(ThetaDataConfigError, match=r"(?i)live_comparison"):
        parse_thetadata_config(
            {
                "pricing_attestations": [
                    {
                        "dimension": "DAY_COUNT",
                        "source": "LIVE_COMPARISON",
                        "reference": "somewhere",
                        "observed_at": "2026-08-01",
                        "vendor_value": "ACT/365F",
                    }
                ]
            }
        )


def test_live_comparison_cannot_be_built_through_the_config_object():
    from src.config.thetadata import ThetaDataConfig

    # Constructible on its own -- the validator emits exactly this, bound to a
    # capture. What must fail is putting it in a *configuration*.
    live = VendorObservation(
        dimension=PricingDimension.DAY_COUNT,
        observed_value="ACT/365F",
        source=EvidenceSource.LIVE_COMPARISON,
        reference="artifacts/validation/day-count.md",
        observed_at="2026-08-01",
        manifest_hash="a" * 64,
    )
    with pytest.raises(ThetaDataConfigError, match=r"(?i)live_comparison"):
        ThetaDataConfig(pricing_attestations=(live,))


def test_a_live_observation_must_name_the_capture_it_came_from():
    from src.config.compatibility import AttestationError

    with pytest.raises(AttestationError, match=r"(?i)capture"):
        VendorObservation(
            dimension=PricingDimension.DAY_COUNT,
            observed_value="ACT/365F",
            source=EvidenceSource.LIVE_COMPARISON,
            reference="artifacts/validation/day-count.md",
            observed_at="2026-08-01",
        )


def test_vendor_documentation_in_yaml_is_still_accepted():
    built = parse_thetadata_config(
        {
            "pricing_attestations": [
                {
                    "dimension": "DAY_COUNT",
                    "source": "VENDOR_DOCUMENTATION",
                    "reference": "tests/fixtures/vendor_conventions.md",
                    "observed_at": "2026-08-01",
                    "vendor_value": "ACT/365F",
                }
            ]
        }
    )
    assert len(built.pricing_attestations) == 1


def test_a_reference_that_points_nowhere_is_rejected():
    """An unresolvable reference cannot be checked by a reader."""
    with pytest.raises(ThetaDataConfigError, match=r"(?i)reference"):
        parse_thetadata_config(
            {
                "pricing_attestations": [
                    {
                        "dimension": "DAY_COUNT",
                        "source": "VENDOR_DOCUMENTATION",
                        "reference": "docs/this-file-does-not-exist.md",
                        "observed_at": "2026-08-01",
                        "vendor_value": "ACT/365F",
                    }
                ]
            }
        )


# =============================================================================
# §11 -- what the adapter does not send, it does not own
# =============================================================================


@pytest.mark.parametrize(
    "dimension",
    [PricingDimension.RATE_UNITS, PricingDimension.DIVIDEND_CONVENTION],
)
def test_the_conventions_we_never_send_are_vendor_owned(dimension):
    """The regression.

    The adapter sends ``rate_value`` and ``annual_dividend``. It does not send
    ``rate_units`` or ``dividend_convention`` -- how the vendor *reads* those
    numbers is the vendor's convention, and a local YAML entry cannot settle it.
    """
    assert dimension.vendor_owned


@pytest.mark.parametrize(
    "dimension",
    [PricingDimension.RISK_FREE_RATE, PricingDimension.DIVIDEND_VALUE],
)
def test_the_values_we_do_send_stay_locally_settleable(dimension):
    """We chose the number and sent it; there is no vendor claim to be wrong."""
    assert not dimension.vendor_owned


def test_local_evidence_cannot_settle_the_rate_units():
    from src.config.compatibility import (
        PricingCompatibilityReport,
        PricingDimensionResult,
        apply_attestations,
    )

    report = PricingCompatibilityReport(
        dimensions=(
            PricingDimensionResult(
                dimension=PricingDimension.RATE_UNITS,
                status=CompatibilityStatus.UNKNOWN,
                code="VENDOR_RATE_UNITS_UNDOCUMENTED",
            ),
        )
    )
    after = apply_attestations(
        report,
        (
            VendorObservation(
                dimension=PricingDimension.RATE_UNITS,
                observed_value="DECIMAL_ANNUAL_RATE",
                source=EvidenceSource.LOCAL_CONFIGURATION,
                reference="config/thetadata_capture.yaml",
                observed_at="2026-08-01",
            ),
        ),
        SPEC,
    )
    assert not after.compatible
    assert any("VENDOR_CONVENTION" in f for f in after.hard_failures)


def test_a_zero_dividend_is_numerically_convention_free_but_still_unverified():
    """exp(-0*T) is 1 whatever the convention means, and that is not knowledge.

    The value is safe to use. The convention is still unestablished, and the
    report has to keep saying so.
    """
    built = parse_thetadata_config(
        {"annual_dividend": 0.0, "dividend_convention": "ZERO_DIVIDEND"}
    )
    from src.adapters.transport import FakeTransport
    from src.config.pipeline import ThetaDataResearchPipeline

    pipeline = ThetaDataResearchPipeline.from_config(built, transport=FakeTransport())
    dimensions = {d.dimension: d for d in pipeline.pricing_compatibility.dimensions}
    assert dimensions[PricingDimension.DIVIDEND_VALUE].status is (
        CompatibilityStatus.MATCHED
    )
    assert dimensions[PricingDimension.DIVIDEND_CONVENTION].status is (
        CompatibilityStatus.UNKNOWN
    )
