"""Four v2.1.4 boundaries: exceptions, construction, identity, hashing.

Each is a case where a value could be *nearly* right and nothing said so.

**§9.** ``except ThetaDataError`` caught the runtime failures and missed the
configuration ones, because ``ThetaDataConfigError`` was a bare ``ValueError``.
A caller handling "the adapter failed" caught the half that happens after the
config was accepted.

**§10.** ``ThetaDataConfig()`` constructed happily with four fields left at
``None`` under a lying type annotation, then raised ``AttributeError: 'NoneType'
object has no attribute 'value'`` from ``as_dict`` -- inside the audit trail,
which is the last place a config should first be found invalid.

**§11.** The canonical contract identity was spelled by two different
formatters. They agreed on every strike either was tested with, which is not the
same as agreeing.

**§12.** The replay hash covered prose. Rewording a message moved the digest of
an unchanged calculation; changing a finding while keeping the phrasing did not
move it at all.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.adapters.errors import ThetaDataError
from src.config.pipeline import PipelineConsistencyError, ThetaDataResearchPipeline
from src.config.thetadata import (
    MissingCredentialsError,
    ThetaDataConfig,
    ThetaDataConfigError,
    parse_thetadata_config,
)
from src.domain.completeness import contract_identity
from src.domain.contracts import OptionContract, OptionRight, OptionRoot
from src.domain.strikes import canonical_strike, canonical_strike_of, parse_strike

# =============================================================================
# §9 -- one exception hierarchy for the whole adapter
# =============================================================================


@pytest.mark.parametrize(
    "error",
    [ThetaDataConfigError, MissingCredentialsError, PipelineConsistencyError],
)
def test_configuration_failures_are_thetadata_failures(error):
    assert issubclass(error, ThetaDataError)


@pytest.mark.parametrize(
    "error",
    [ThetaDataConfigError, MissingCredentialsError, PipelineConsistencyError],
)
def test_configuration_failures_are_still_value_errors(error):
    """Kept so existing handlers, and the loader's own translation, still work."""
    assert issubclass(error, ValueError)


def test_one_except_clause_catches_a_configuration_failure():
    with pytest.raises(ThetaDataError):
        parse_thetadata_config({"tier": "not-a-tier"})


def test_the_base_redacts_credentials_from_a_configuration_message():
    """Inherited, not reimplemented: the redaction lives on ``ThetaDataError``."""
    raised = ThetaDataConfigError("rejected url with token=hunter2 attached")
    assert "hunter2" not in str(raised)
    assert "<redacted>" in str(raised)


# =============================================================================
# §10 -- a config is valid by construction
# =============================================================================


def test_the_default_config_constructs():
    assert ThetaDataConfig() is not None


def test_the_default_config_serialises():
    """The regression: this raised AttributeError from inside the audit trail."""
    payload = ThetaDataConfig().as_dict()
    assert payload["pricing_mode"] == "VENDOR_IV_LOCAL_GAMMA"
    assert payload["vendor_gamma_policy"] == "DISABLED"
    assert payload["rate_units"] == "UNKNOWN"
    assert payload["dividend_convention"] == "UNKNOWN_VENDOR_CONVENTION"


def test_no_derived_field_is_left_as_none():
    config = ThetaDataConfig()
    for name in (
        "pricing_mode",
        "vendor_gamma_policy",
        "rate_units",
        "dividend_convention",
    ):
        assert getattr(config, name) is not None, name


def test_the_pricing_mode_is_derived_from_the_iv_source_at_construction():
    """Not defaulted -- derived, so the two cannot be built in disagreement."""
    from src.config.pipeline import IvGammaPricingMode

    built = ThetaDataConfig(iv_source="NBBO_ASK_IV")
    assert built.pricing_mode is IvGammaPricingMode.VENDOR_IV_LOCAL_GAMMA


def test_an_incoherent_config_cannot_be_constructed_at_all():
    with pytest.raises(ThetaDataConfigError):
        ThetaDataConfig(pricing_mode="LOCAL_IV_LOCAL_GAMMA")


def test_a_string_valued_enum_field_is_coerced_rather_than_stored_raw():
    """A config assembled from raw values is valid, not merely conventional."""
    from src.config.pipeline import RateUnit

    assert ThetaDataConfig(rate_units="PERCENT_ANNUAL_RATE").rate_units is (
        RateUnit.PERCENT_ANNUAL_RATE
    )


def test_an_unimplemented_iv_source_cannot_be_constructed():
    """The YAML loader refused it; direct construction did not.

    An unimplemented source resolves through the vendor-default fallback, so the
    operator prices against a different number than the one they selected.
    """
    from src.domain.iv import IVSource

    with pytest.raises(ThetaDataConfigError, match=r"not implemented"):
        ThetaDataConfig(iv_source=IVSource.TRADE_IV)


def test_the_v2_1_3_mode_name_is_refused_on_the_programmatic_path_too():
    """A config rebuilt from a stored ``as_dict`` went straight past the loader."""
    with pytest.raises(ThetaDataConfigError, match=r"vendor_gamma_policy"):
        ThetaDataConfig(pricing_mode="VENDOR_GAMMA_VALIDATION")


def test_replace_cannot_leave_the_mode_disagreeing_with_the_iv_source():
    """``__post_init__`` only *derives* when the field is None.

    ``dataclasses.replace`` carries the resolved mode over, so the derivation
    never re-runs. Today the unsupported-source guard catches this first, since
    the only local IV source is unimplemented -- but the coherence check has to
    catch it too, or the day a local solver lands the mismatch becomes silent.
    """
    import dataclasses

    from src.domain.iv import IVSource

    with pytest.raises(ThetaDataConfigError):
        dataclasses.replace(ThetaDataConfig(), iv_source=IVSource.LOCALLY_SOLVED_MID_IV)


def test_the_coherence_check_catches_a_local_iv_labelled_as_vendor():
    """The direction that has no other guard once a local solver exists."""
    from src.config.pipeline import IvGammaPricingMode, require_coherent_pricing_mode
    from src.domain.iv import IVSource

    with pytest.raises(ThetaDataConfigError, match=r"solved\s+locally"):
        require_coherent_pricing_mode(
            iv_source=IVSource.LOCALLY_SOLVED_MID_IV,
            pricing_mode=IvGammaPricingMode.VENDOR_IV_LOCAL_GAMMA,
        )


def test_a_default_config_reaches_a_pipeline():
    from src.adapters.transport import FakeTransport

    built = ThetaDataResearchPipeline.from_config(
        ThetaDataConfig(), transport=FakeTransport()
    )
    assert built.config.as_dict()["tier"] == "standard"


# =============================================================================
# §11 -- one formatter for the contract identity
# =============================================================================


def contract(strike: float) -> OptionContract:
    from datetime import date

    return OptionContract(
        root=OptionRoot.SPXW,
        expiry=date(2026, 3, 20),
        strike=strike,
        right=OptionRight.CALL,
    )


def test_no_float_round_trip_reaches_the_identity():
    """The regression: ``float(parsed)`` undid the exact Decimal parse.

    Checked by behaviour rather than by reading the source. This strike has more
    precision than a double carries, so ``float()`` collapses it onto 5000.0 and
    the two identities become one. ``Decimal`` keeps them apart, which is why
    ``parse_strike`` returns one.
    """
    beyond_a_double = contract_identity(
        symbol="SPXW", expiry="2026-03-20", strike="5000.0000000000000001", right="call"
    )
    plain = contract_identity(
        symbol="SPXW", expiry="2026-03-20", strike="5000", right="call"
    )
    assert float("5000.0000000000000001") == float("5000")
    assert beyond_a_double != plain


@pytest.mark.parametrize("spelling", ["5000", "5000.0", "5000.00", "5000.000000"])
def test_equivalent_spellings_share_one_identity(spelling):
    assert contract_identity(
        symbol="SPXW", expiry="2026-03-20", strike=spelling, right="call"
    ) == contract_identity(
        symbol="SPXW", expiry="2026-03-20", strike="5000", right="call"
    )


@pytest.mark.parametrize("strike", [5000.0, 4900.5, 4987.25, 1.125])
def test_both_sides_of_the_join_spell_a_strike_the_same_way(strike):
    """The expected universe and the received chain must agree by construction."""
    expected = contract_identity(
        symbol="SPXW", expiry="2026-03-20", strike=str(strike), right="call"
    )
    assert contract(strike).canonical_id == expected


def test_a_strike_needing_more_than_four_decimals_survives():
    """``.4f`` silently rounded these together; ``canonical_strike`` does not."""
    first = contract_identity(
        symbol="SPXW", expiry="2026-03-20", strike="4900.00001", right="call"
    )
    second = contract_identity(
        symbol="SPXW", expiry="2026-03-20", strike="4900.00002", right="call"
    )
    assert first != second


def test_the_float_entry_point_agrees_with_the_decimal_one():
    parsed, _ = parse_strike("4900.5")
    assert canonical_strike(parsed) == canonical_strike_of(4900.5)


def test_the_float_entry_point_reads_the_number_a_human_wrote():
    """``Decimal(4900.5)`` is the exact binary value; ``Decimal(str(...))`` is not."""
    assert canonical_strike_of(4900.5) == "4900.5"
    assert canonical_strike(Decimal(str(0.1))) == "0.1"


def test_an_unparseable_strike_still_refuses_to_produce_an_identity():
    with pytest.raises(ValueError, match=r"no identity"):
        contract_identity(
            symbol="SPXW", expiry="2026-03-20", strike="NaN", right="call"
        )


@pytest.mark.parametrize("strike", [1e28, 1e30, 1e40])
def test_an_absurdly_large_strike_still_produces_an_identity(strike):
    """``canonical_id`` must not become a property that throws.

    ``quantize`` raises ``InvalidOperation`` when an integral value needs more
    digits than the decimal context allows, which for the first cut of v2.1.4
    meant any strike at or above 1e28 crashed inside a set comprehension during
    chain parsing. The ``f"{strike:.4f}"`` it replaced formatted these without
    complaint, so a mis-scaled vendor value used to survive and must still.
    """
    assert contract(strike).canonical_id.count(":") == 3


@pytest.mark.parametrize("strike", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_strike_is_refused_rather_than_spelled(strike):
    """``OptionContract`` holds a raw float, so this is the last chance to stop it.

    An identity containing ``NaN`` compares unequal to itself, so the contract
    could never be deduplicated or matched -- the exact failure ``parse_strike``
    was written to eliminate, reachable through the other entry point.
    """
    from src.domain.strikes import StrikeError

    with pytest.raises(StrikeError, match=r"not finite"):
        assert contract(strike).canonical_id


# =============================================================================
# §12 -- the replay hash covers decisions, not sentences
# =============================================================================


def snapshot():
    from src.gex.engine import compute_gex_snapshot
    from src.synthetic.chains import build_synthetic_chain

    return compute_gex_snapshot(build_synthetic_chain())


def test_prose_in_metadata_does_not_reach_the_digest():
    """The regression, at the level the whole snapshot sees it."""
    base = snapshot()
    reworded = base.with_meta(
        pipeline={
            "pricing_compatibility": {
                "compatible": False,
                "dimension_detail": {"DAY_COUNT": "the vendor does not say"},
            },
            "warnings": ("PRICING_ASSUMPTIONS_NOT_VERIFIED: 6 unknown",),
        }
    )
    edited = base.with_meta(
        pipeline={
            "pricing_compatibility": {
                "compatible": False,
                "dimension_detail": {"DAY_COUNT": "the year fraction is undocumented"},
            },
            "warnings": ("PRICING_ASSUMPTIONS_NOT_VERIFIED: six unknown",),
        }
    )
    assert reworded.output_hash() == edited.output_hash()


def test_a_changed_decision_does_reach_the_digest():
    """The other half. Excluding prose must not mean excluding the finding."""
    base = snapshot()
    blocked = base.with_meta(pipeline={"pricing_compatibility": {"compatible": False}})
    allowed = base.with_meta(pipeline={"pricing_compatibility": {"compatible": True}})
    assert blocked.output_hash() != allowed.output_hash()


def test_a_changed_status_reaches_the_digest():
    from src.config.compatibility import (
        CompatibilityStatus,
        PricingCompatibilityReport,
        PricingDimension,
        PricingDimensionResult,
    )

    def report(status: CompatibilityStatus) -> PricingCompatibilityReport:
        return PricingCompatibilityReport(
            dimensions=(
                PricingDimensionResult(
                    dimension=PricingDimension.DAY_COUNT,
                    status=status,
                    code="VENDOR_CONVENTION_UNDOCUMENTED",
                    detail="identical wording on both sides",
                ),
            )
        )

    base = snapshot()
    unknown = base.with_meta(
        pipeline={
            "pricing_compatibility": report(
                CompatibilityStatus.UNKNOWN
            ).semantic_payload()
        }
    )
    matched = base.with_meta(
        pipeline={
            "pricing_compatibility": report(
                CompatibilityStatus.MATCHED
            ).semantic_payload()
        }
    )
    assert unknown.output_hash() != matched.output_hash()


def test_the_semantic_payload_carries_no_prose_key():
    from src.config.compatibility import (
        CompatibilityStatus,
        PricingCompatibilityReport,
        PricingDimension,
        PricingDimensionResult,
    )

    payload = PricingCompatibilityReport(
        dimensions=(
            PricingDimensionResult(
                dimension=PricingDimension.DAY_COUNT,
                status=CompatibilityStatus.UNKNOWN,
                code="VENDOR_CONVENTION_UNDOCUMENTED",
                detail="prose that must not be hashed",
            ),
        ),
        warnings=("a note for humans",),
    ).semantic_payload()
    assert "warnings" not in payload
    assert all("detail" not in dimension for dimension in payload["dimensions"])


def test_hard_failure_codes_are_hashed_but_deduplicated_and_sorted():
    from src.config.compatibility import PricingCompatibilityReport

    one = PricingCompatibilityReport(hard_failures=("B_CODE", "A_CODE", "A_CODE"))
    two = PricingCompatibilityReport(hard_failures=("A_CODE", "B_CODE"))
    assert one.semantic_payload() == two.semantic_payload()

    three = PricingCompatibilityReport(hard_failures=("A_CODE",))
    assert three.semantic_payload() != two.semantic_payload()
