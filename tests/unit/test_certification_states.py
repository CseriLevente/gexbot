"""Certification must not be able to bless what it has not checked.

The v2.1.2 defect. ``assess_readiness`` read ``pipeline.pricing_mode`` and, for
any mode that did not "mix vendor and local", returned compatible without
looking further.

The v2.1.4 defects were in the evidence rather than the check: typed objects
whose *presence* was the answer. ``CaptureVerification(confirmed_record_ids=
("fake",), failures=())`` verified, ``ValidationCheck(name="anything",
passed=True)`` validated, and a ``ProvenanceEvidence`` naming a record proved
that the record existed and nothing else.

v2.1.5 derives all three from the raw capture, so this file supplies inputs and
asserts on what the production code concludes from them. Open-interest and spot
provenance, which had their own file in v2.1.4, are checked here too: the
question "was this observed?" is now answered by the same machinery.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.adapters.certification import (
    AdapterValidator,
    CertificationState,
    OpenInterestProvenance,
    ProvenanceGrade,
    SpotProvenance,
    SpotSource,
    grade_claim,
    verify_capture,
)
from src.adapters.raw_store import InMemoryRawStore, RawCaptureManifest
from src.adapters.thetadata.endpoints import Endpoint
from src.config.compatibility import EvidenceSource, PricingDimension
from src.config.thetadata import ThetaDataConfigError, parse_thetadata_config
from tests.certification_fixtures import (
    AS_OF,
    CAPTURE_SETTINGS,
    FIXTURE_OPEN_INTEREST,
    build_capture,
    plan_for,
    readiness,
    resolved_pipeline,
    unresolved_pipeline,
    verified_oi,
)
from tests.pricing_evidence import attestations, resolved_settings


def pipeline(**overrides):
    from src.adapters.transport import FakeTransport
    from src.config.pipeline import ThetaDataResearchPipeline

    settings = {**CAPTURE_SETTINGS}
    settings.update(overrides)
    return ThetaDataResearchPipeline.from_config(
        parse_thetadata_config(settings), transport=FakeTransport()
    )


def observed_oi(store, manifest):
    """Provenance whose evidence is read back out of the capture."""
    return OpenInterestProvenance(
        as_of=date(2026, 3, 16),
        source="vendor_field",
        chain_date=AS_OF.date(),
        observation=AdapterValidator.observe_field(
            manifest=manifest,
            store=store,
            endpoint=Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT,
            field_path="open_interest",
        ),
    )


def observed_spot(store, manifest):
    return SpotProvenance(
        source=SpotSource.VENDOR_INDEX_SNAPSHOT,
        timestamp=AS_OF - timedelta(milliseconds=200),
        tolerance_seconds=1.0,
        observation=AdapterValidator.observe_field(
            manifest=manifest,
            store=store,
            endpoint=Endpoint.INDEX_PRICE_SNAPSHOT,
            field_path="index_price",
        ),
    )


def observed_readiness(**overrides):
    store, manifest = build_capture()
    payload = {
        "manifest": manifest,
        "raw_store": store,
        "open_interest": observed_oi(store, manifest),
        "spot": observed_spot(store, manifest),
    }
    payload.update(overrides)
    return readiness(**payload)


# =============================================================================
# unknowns that change gamma block the calculation, not the capture
# =============================================================================


def test_the_default_configuration_cannot_be_trusted_to_calculate():
    result = readiness(pipeline=unresolved_pipeline())
    assert not result.calculation_trusted
    assert result.state is CertificationState.READY_FOR_RAW_CAPTURE_ONLY


def test_unknown_pricing_does_not_block_a_raw_capture():
    """Bytes are worth having whatever we understand about the day count."""
    result = readiness(pipeline=unresolved_pipeline())
    assert result.ready, result.blockers
    assert result.blockers == ()


def test_the_default_blocks_specifically_on_load_bearing_unknowns():
    result = readiness(pipeline=unresolved_pipeline())
    assert any("load-bearing" in b for b in result.calculation_blockers)


def test_an_unknown_vendor_rate_blocks_the_calculation():
    result = readiness(
        pipeline=unresolved_pipeline(rate_value=4.2, rate_units="UNKNOWN")
    )
    assert not result.calculation_trusted


def test_a_vendor_default_rate_blocks_the_calculation():
    result = readiness(pipeline=unresolved_pipeline(rate_value=None))
    assert not result.calculation_trusted


def test_a_resolved_configuration_can_become_capture_ready():
    result = readiness()
    assert result.ready, result.blockers
    assert result.calculation_trusted, result.calculation_blockers
    assert result.state is CertificationState.READY_FOR_RAW_CAPTURE_ONLY


def test_a_disagreeing_observation_becomes_a_mismatch_not_a_match():
    """v2.1.4 wrote MATCHED for the observation existing."""
    from src.config.compatibility import CompatibilityStatus

    disagreeing = pipeline(
        **resolved_settings(
            pricing_attestations=attestations(
                overrides={PricingDimension.DAY_COUNT: "ACT/360"}
            ),
            **CAPTURE_SETTINGS,
        )
    )
    by_dimension = {
        d.dimension: d for d in disagreeing.pricing_compatibility.dimensions
    }
    assert by_dimension[PricingDimension.DAY_COUNT].status is (
        CompatibilityStatus.MISMATCHED
    )
    assert not disagreeing.pricing_compatibility.compatible


def test_configuration_can_no_longer_claim_a_live_comparison():
    """A configuration file cannot witness an event."""
    with pytest.raises(ThetaDataConfigError, match=r"(?i)live_comparison"):
        pipeline(
            **resolved_settings(
                pricing_attestations=attestations(
                    source=EvidenceSource.LIVE_COMPARISON
                ),
                **CAPTURE_SETTINGS,
            )
        )


# =============================================================================
# raw capture is mandatory, and the store has to be real
# =============================================================================


def test_capture_disabled_is_not_capture_ready():
    result = readiness(pipeline=resolved_pipeline(raw_capture_enabled=False))
    assert not result.ready
    assert any("raw_capture_enabled" in b for b in result.blockers)
    assert result.state is CertificationState.NOT_READY


def test_capture_enabled_with_no_path_never_reaches_readiness():
    with pytest.raises(ThetaDataConfigError, match=r"raw_capture_path"):
        resolved_pipeline(raw_capture_enabled=True, raw_capture_path=None)


def test_capture_configured_with_no_store_to_check_is_not_capture_ready():
    result = readiness(raw_store=None)
    assert not result.ready
    assert any("no store was supplied" in b for b in result.blockers)


def test_a_bare_object_store_is_not_capture_ready():
    """The v2.1.5 regression: v2.1.4 skipped the check rather than failing it."""
    result = readiness(raw_store=object())
    assert not result.ready
    assert any("not usable" in b for b in result.blockers)


# =============================================================================
# the state machine
# =============================================================================


def test_a_verified_capture_with_unknown_pricing_stops_at_raw_capture():
    store, manifest = build_capture(pipeline=unresolved_pipeline())
    result = readiness(
        pipeline=unresolved_pipeline(),
        manifest=manifest,
        raw_store=store,
        open_interest=observed_oi(store, manifest),
        spot=observed_spot(store, manifest),
    )
    assert result.state is CertificationState.RAW_CAPTURE_COMPLETED
    assert not result.calculation_trusted


def test_a_verified_capture_with_resolved_pricing_permits_an_unvalidated_calculation():
    result = observed_readiness()
    assert result.state is CertificationState.CALCULATION_NOT_VALIDATED
    assert result.calculation_trusted


def test_a_one_record_capture_is_not_a_capture():
    """A quote snapshot alone cannot produce a GEX, certified or otherwise."""
    store, manifest = build_capture(endpoints=(Endpoint.OPTION_QUOTE_SNAPSHOT,))
    result = readiness(manifest=manifest, raw_store=store)
    assert result.state is CertificationState.READY_FOR_RAW_CAPTURE_ONLY
    assert any("MISSING_ENDPOINT" in b for b in result.calculation_blockers)


def test_a_disabled_capture_manifest_is_not_a_capture():
    verification = verify_capture(
        RawCaptureManifest.disabled(), InMemoryRawStore(), plan=plan_for()
    )
    assert not verification.verified
    assert any("CAPTURE_DISABLED" in f for f in verification.failures)


def test_no_offline_run_can_reach_certified():
    """The ceiling. Every convention rests on documentation, or on nothing."""
    store, manifest = build_capture()
    report = AdapterValidator.validate(
        manifest=manifest, store=store, pipeline=resolved_pipeline()
    )
    for validation in (None, report):
        result = observed_readiness(validation=validation)
        assert result.state is not CertificationState.ADAPTER_CERTIFIED


def test_blockers_override_every_other_state():
    result = observed_readiness(spot=None)
    assert result.state is CertificationState.NOT_READY


def test_missing_credentials_prevent_capture_readiness(monkeypatch):
    monkeypatch.delenv("THETA_USER", raising=False)
    monkeypatch.delenv("THETA_PASS", raising=False)
    from dataclasses import replace

    needs_auth = replace(
        resolved_pipeline(),
        config=parse_thetadata_config(
            resolved_settings(
                authentication_mode="basic",
                username_env="THETA_USER",
                password_env="THETA_PASS",
                **CAPTURE_SETTINGS,
            )
        ),
    )
    result = readiness(pipeline=needs_auth)
    assert not result.ready
    assert any("credential" in b.lower() for b in result.blockers)


def test_the_state_is_serialised():
    payload = readiness().as_dict()
    assert payload["state"] == "READY_FOR_RAW_CAPTURE_ONLY"
    assert payload["schema_version"] == "adapter-certification/2.1.9"
    assert payload["provenance_grades"]["spot"] == "PLANNED"
    observed = observed_readiness().as_dict()
    assert observed["provenance_grades"]["spot"] == "OBSERVED"


@pytest.mark.parametrize("state", list(CertificationState))
def test_every_state_is_declared(state):
    assert state.value


def test_the_ladder_has_a_rung_for_each_distinct_claim():
    assert [state.value for state in CertificationState] == [
        "NOT_READY",
        "READY_FOR_RAW_CAPTURE_ONLY",
        "RAW_CAPTURE_COMPLETED",
        "CALCULATION_NOT_VALIDATED",
        "CALCULATION_VALIDATED",
        "ADAPTER_CERTIFIED",
    ]


def test_unknown_chain_completeness_is_still_only_a_warning():
    result = readiness()
    assert result.ready
    assert any("completeness" in w.lower() for w in result.warnings)


def test_documentation_backed_dimensions_are_called_out():
    result = readiness()
    assert any("vendor documentation" in w for w in result.warnings)


def test_a_tier_that_cannot_serve_the_mode_blocks():
    with pytest.raises(ThetaDataConfigError, match=r"(?i)tier"):
        pipeline(tier="value")


# =============================================================================
# open-interest and spot provenance
# =============================================================================


def test_missing_open_interest_provenance_blocks_certification():
    result = readiness(open_interest=None)
    assert not result.ready
    assert any("open_interest" in b for b in result.blockers)


def test_open_interest_without_a_date_blocks_certification():
    empty = OpenInterestProvenance(as_of=None, source="caller")
    assert not readiness(open_interest=empty).ready


def test_a_caller_supplied_date_does_not_block_the_capture():
    """It is a documented limitation, not a reason to refuse the session."""
    supplied = OpenInterestProvenance(as_of=date(2026, 3, 16), source="caller")
    assert readiness(open_interest=supplied).ready


def test_missing_spot_provenance_blocks_certification():
    assert not readiness(spot=None).ready


def test_a_missing_spot_timestamp_blocks_certification():
    result = readiness(
        spot=SpotProvenance(source=SpotSource.VENDOR_INDEX_SNAPSHOT, timestamp=None)
    )
    assert not result.ready
    assert any("spot" in b for b in result.blockers)


def test_spot_skew_beyond_tolerance_blocks_certification():
    result = readiness(
        spot=SpotProvenance(
            source=SpotSource.VENDOR_INDEX_SNAPSHOT,
            timestamp=AS_OF - timedelta(seconds=90),
            tolerance_seconds=1.0,
        )
    )
    assert not result.ready
    assert any("skew" in b or "tolerance" in b for b in result.blockers)


def test_spot_skew_within_tolerance_is_accepted():
    assert readiness(
        spot=SpotProvenance(
            source=SpotSource.VENDOR_INDEX_SNAPSHOT,
            timestamp=AS_OF - timedelta(milliseconds=500),
            tolerance_seconds=1.0,
        )
    ).ready


# =============================================================================
# provenance grades are derived from the bytes
# =============================================================================


def test_provenance_without_evidence_claims_nothing():
    store, manifest = build_capture()
    planned = OpenInterestProvenance(as_of=date(2026, 3, 16), source="caller")
    assert not planned.claims_observation
    assert grade_claim(planned, manifest=manifest, store=store, validated=False) == (
        ProvenanceGrade.PLANNED,
        "",
    )


def test_a_claim_is_observed_only_once_the_payload_confirms_it():
    store, manifest = build_capture()
    grade, complaint = grade_claim(
        observed_oi(store, manifest), manifest=manifest, store=store, validated=False
    )
    assert grade is ProvenanceGrade.OBSERVED
    assert complaint == ""


def test_the_observed_value_is_the_one_in_the_payload():
    store, manifest = build_capture()
    claim = observed_oi(store, manifest)
    assert claim.observation is not None
    assert claim.observation.observed_value == FIXTURE_OPEN_INTEREST


def test_evidence_naming_another_session_is_refused():
    store, manifest = build_capture()
    _, other = build_capture(session_id="other")
    grade, complaint = grade_claim(
        observed_oi(store, manifest), manifest=other, store=store, validated=True
    )
    assert grade is ProvenanceGrade.PLANNED
    assert "manifest" in complaint


def test_evidence_without_a_capture_cannot_be_observed():
    store, manifest = build_capture()
    grade, complaint = grade_claim(
        observed_oi(store, manifest), manifest=None, store=store, validated=True
    )
    assert grade is ProvenanceGrade.PLANNED
    assert "no capture" in complaint


def test_a_planned_claim_is_a_warning_not_a_blocker():
    result = observed_readiness(open_interest=verified_oi())
    assert result.ready
    assert dict(result.provenance_grades)["open_interest_as_of"] == "PLANNED"


def test_readiness_never_implies_trading_readiness():
    result = readiness()
    assert result.trading_enabled is False
    assert "not a trading" in result.scope.lower()


def test_the_report_cannot_enable_trading():
    """Inspect the project's own API, not everything the runtime attaches."""
    result = readiness()
    declared = {name for name in vars(type(result)) if not name.startswith("_")} | set(
        type(result).__dataclass_fields__
    )
    banned = ("place_order", "submit_order", "execute", "broker", "position_size")
    for name in declared:
        assert not any(word in name.lower() for word in banned), name


def test_readiness_is_recomputed_not_cached():
    assert readiness().ready
    assert not readiness(spot=None).ready
    assert readiness().ready
