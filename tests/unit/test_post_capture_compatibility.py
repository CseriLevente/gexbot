"""What the capture showed has to reach the report that gates the calculation.

**§5.** ``AdapterValidator`` produced ``VendorObservation`` objects from the
captured payloads, and the pipeline's ``pricing_compatibility`` never changed.
Live evidence was computed and then dropped on the floor: the report that
``compute_trusted_gex`` consults was the static one derived from configuration,
so a capture could observe the vendor's day count and the gate would still say
``UNKNOWN``. It also meant a *disagreement* found in the bytes could not block
anything.

**§6.** Chain-level conventions were characterised from row zero. One matching
contract stood for the whole chain -- so a vendor that priced one strike against
the index print and the rest against something else read as agreement.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.adapters.certification import AdapterValidator
from src.config.compatibility import (
    CompatibilityStatus,
    PricingDimension,
    derive_post_capture_compatibility,
)
from tests.certification_fixtures import (
    build_capture,
    resolved_pipeline,
    unresolved_pipeline,
)


def validated(pipeline=None, **capture_kwargs):
    built = pipeline if pipeline is not None else unresolved_pipeline()
    store, manifest = build_capture(pipeline=built, **capture_kwargs)
    report = AdapterValidator.validate(manifest=manifest, store=store, pipeline=built)
    return built, store, manifest, report


def post_capture(pipeline, manifest, report):
    return derive_post_capture_compatibility(
        base_report=pipeline.pricing_compatibility,
        validation_report=report,
        model_spec=pipeline.model_spec,
        manifest=manifest,
    )


# =============================================================================
# §5 -- validated observations change the effective report
# =============================================================================


def test_a_validated_observation_settles_its_dimension():
    """The regression: the observation existed and the report never moved."""
    pipeline, _, manifest, report = validated()
    base = {d.dimension: d for d in pipeline.pricing_compatibility.dimensions}
    assert base[PricingDimension.UNDERLYING_SOURCE].status is (
        CompatibilityStatus.UNKNOWN
    )

    effective = post_capture(pipeline, manifest, report)
    observed = {d.dimension: d for d in effective.dimensions}
    assert observed[PricingDimension.UNDERLYING_SOURCE].status is not (
        CompatibilityStatus.UNKNOWN
    )


def test_the_effective_report_keeps_the_observed_and_local_values():
    pipeline, _, manifest, report = validated()
    effective = post_capture(pipeline, manifest, report)
    result = next(
        d
        for d in effective.dimensions
        if d.dimension is PricingDimension.UNDERLYING_SOURCE
    )
    assert result.vendor_value is not None
    assert result.local_value is not None
    assert result.evidence is not None


def test_the_effective_report_records_where_the_observation_came_from():
    pipeline, _, manifest, report = validated()
    effective = post_capture(pipeline, manifest, report)
    payload = effective.as_dict()
    assert payload["manifest_hash"] == manifest.manifest_hash
    assert payload["validator_version"] == report.validator_version
    assert payload["observed_record_ids"]


def test_an_observation_for_another_manifest_is_ignored():
    pipeline, _, _, report = validated()
    _, other = build_capture(pipeline=pipeline, session_id="another")
    effective = derive_post_capture_compatibility(
        base_report=pipeline.pricing_compatibility,
        validation_report=report,
        model_spec=pipeline.model_spec,
        manifest=other,
    )
    result = next(
        d
        for d in effective.dimensions
        if d.dimension is PricingDimension.UNDERLYING_SOURCE
    )
    assert result.status is CompatibilityStatus.UNKNOWN


def test_an_observation_whose_records_were_not_verified_is_ignored():
    pipeline, _, manifest, report = validated()
    checks = tuple(
        dataclasses.replace(check, record_ids=("never-captured",))
        if check.dimension is PricingDimension.UNDERLYING_SOURCE
        else check
        for check in report.checks
    )
    effective = post_capture(
        pipeline, manifest, dataclasses.replace(report, checks=checks)
    )
    result = next(
        d
        for d in effective.dimensions
        if d.dimension is PricingDimension.UNDERLYING_SOURCE
    )
    assert result.status is CompatibilityStatus.UNKNOWN


def test_a_documented_dimension_survives_into_the_post_capture_report():
    """Static evidence is not discarded because a capture happened."""
    pipeline, _, manifest, report = validated(pipeline=resolved_pipeline())
    base = {d.dimension: d.status for d in pipeline.pricing_compatibility.dimensions}
    effective = {
        d.dimension: d.status
        for d in post_capture(pipeline, manifest, report).dimensions
    }
    assert base[PricingDimension.DAY_COUNT] is CompatibilityStatus.MATCHED
    assert effective[PricingDimension.DAY_COUNT] is CompatibilityStatus.MATCHED


def test_a_live_mismatch_overrides_a_documented_match():
    """What the vendor did beats what the documentation said it would do."""
    pipeline, _, manifest, report = validated(
        pipeline=resolved_pipeline(), mismatched_underlying=True
    )
    effective = post_capture(pipeline, manifest, report)
    result = next(
        d
        for d in effective.dimensions
        if d.dimension is PricingDimension.UNDERLYING_SOURCE
    )
    assert result.status is CompatibilityStatus.MISMATCHED
    assert not effective.compatible


def test_act_360_against_act_365f_stays_mismatched_after_a_capture():
    """A capture that cannot observe the day count does not launder it."""
    from tests.certification_fixtures import CAPTURE_SETTINGS, pipeline_from
    from tests.pricing_evidence import attestations, resolved_settings

    disagreeing = pipeline_from(
        **resolved_settings(
            pricing_attestations=attestations(
                overrides={PricingDimension.DAY_COUNT: "ACT/360"}
            ),
            **CAPTURE_SETTINGS,
        )
    )
    _, _, manifest, report = validated(pipeline=disagreeing)
    effective = post_capture(disagreeing, manifest, report)
    result = next(
        d for d in effective.dimensions if d.dimension is PricingDimension.DAY_COUNT
    )
    assert result.status is CompatibilityStatus.MISMATCHED
    assert not effective.compatible


def test_the_trusted_calculation_reads_the_post_capture_report():
    """A disagreement found in the bytes has to reach the gate."""
    from src.adapters.certification import AdapterValidator
    from src.config.pipeline import PipelineConsistencyError
    from tests.certification_fixtures import (
        captured_chain,
        context_for,
        trusted_evidence,
    )

    # One fetch, through a transport whose greeks rows priced against something
    # other than the index print. The chain and the capture are the same event.
    pipeline = resolved_pipeline(mismatched_underlying=True)
    taken = captured_chain(pipeline)
    report = AdapterValidator.validate(
        manifest=taken.manifest, store=taken.store, pipeline=pipeline
    )
    effective = context_for(taken, validation=report).effective_pricing_compatibility
    assert PricingDimension.UNDERLYING_SOURCE in effective.load_bearing_mismatches

    with pytest.raises(PipelineConsistencyError, match=r"(?i)mismatch|underlying"):
        pipeline.compute_trusted_gex(
            taken.chain, **trusted_evidence(taken, validation_report=report)
        )


# =============================================================================
# §6 -- a chain-level convention is measured across the chain
# =============================================================================


def test_a_chain_level_check_reports_its_coverage():
    _, _, _, report = validated()
    check = next(
        c for c in report.checks if c.dimension is PricingDimension.UNDERLYING_SOURCE
    )
    coverage = check.coverage
    assert coverage is not None
    for key in (
        "rows_inspected",
        "records_inspected",
        "matching_rows",
        "mismatching_rows",
        "missing_rows",
        "non_finite_rows",
        "coverage_ratio",
        "distinct_observed_values",
        "maximum_deviation",
    ):
        assert key in coverage.as_dict(), key


def test_every_row_is_inspected_not_only_the_first():
    """The regression. v2.1.5 read row zero and stopped."""
    _, _, _, report = validated()
    check = next(
        c for c in report.checks if c.dimension is PricingDimension.UNDERLYING_SOURCE
    )
    assert check.coverage is not None
    assert check.coverage.rows_inspected > 1


def test_one_mismatching_row_blocks_chain_level_agreement():
    """One contract cannot characterise the chain."""
    _, _, _, report = validated(one_row_mismatched=True)
    check = next(
        c for c in report.checks if c.dimension is PricingDimension.UNDERLYING_SOURCE
    )
    assert not check.passed
    assert check.coverage is not None
    assert check.coverage.mismatching_rows == 1
    assert check.coverage.matching_rows > 0


def test_a_mixed_result_does_not_settle_the_dimension():
    pipeline, _, manifest, report = validated(one_row_mismatched=True)
    effective = post_capture(pipeline, manifest, report)
    result = next(
        d
        for d in effective.dimensions
        if d.dimension is PricingDimension.UNDERLYING_SOURCE
    )
    assert result.status is not CompatibilityStatus.MATCHED
    assert not effective.compatible


def test_records_inspected_counts_every_relevant_record():
    _, _, _, report = validated()
    check = next(
        c for c in report.checks if c.dimension is PricingDimension.UNDERLYING_TIMESTAMP
    )
    assert check.coverage is not None
    assert check.coverage.records_inspected >= 1
    assert check.record_ids
