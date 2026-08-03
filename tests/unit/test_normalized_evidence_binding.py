"""A trusted number is bound to the chain its raw records normalize to.

v2.1.6 verified the *bytes* thoroughly and the ``ChainSnapshot`` not at all.
Those are different objects -- the chain is the result of parsing and joining
the records -- and nothing connected them except a caller passing both to the
same method. So this worked:

    chain = pipeline.fetch_chain(...)          # honest
    tampered = dataclasses.replace(chain, quotes=(edited, *chain.quotes[1:]))
    pipeline.compute_trusted_gex(tampered, context=real_context)   # trusted=True

Adding 999,999 to one strike's open interest moved the unsigned total by about
two orders of magnitude, and the result carried a verified manifest and
``trusted=True``.

Every test here fails against v2.1.6.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime

import pytest

from src.adapters.certification import AdapterValidator
from src.adapters.errors import ThetaDataProvenanceError
from src.adapters.open_interest import (
    EvidenceKind,
    OpenInterestAsOfEvidence,
    OpenInterestValueObservation,
)
from src.config.pipeline import PipelineConsistencyError
from src.domain.normalization import canonical_chain_hash
from tests.certification_fixtures import (
    AS_OF,
    build_capture,
    captured_chain,
    context_for,
    resolved_pipeline,
    trusted_evidence,
)


def with_first_quote(chain, **changes):
    """The same chain with one contract edited. The whole attack surface."""
    first, *rest = chain.quotes
    return dataclasses.replace(
        chain, quotes=(dataclasses.replace(first, **changes), *rest)
    )


def refuses(taken, chain, match=r"(?i)re-derived|rederiv"):
    with pytest.raises(PipelineConsistencyError, match=match):
        taken.pipeline.compute_trusted_gex(chain, **trusted_evidence(taken))


# =============================================================================
# §1 -- the normalized chain is re-derived from the raw payloads
# =============================================================================


def test_the_chain_is_rebuilt_from_the_stored_bytes_and_matches():
    """The gate must leave a path through it, and the path is re-derivation.

    The recipe carries the settlement date the *capture* derived. Since v2.1.9
    that date is stamped on every contract, so a rebuild that omitted it would
    produce a chain differing from the original in exactly that field -- which
    is the check working, not a fixture detail.
    """
    taken = captured_chain()
    recipe = taken.pipeline.normalization_recipe(
        as_of=taken.chain.as_of,
        open_interest_as_of=taken.settlement_artifact.resolved_settlement_date,
    )
    rebuilt = taken.pipeline.rebuild_chain_from_capture(
        manifest=taken.manifest, store=taken.store, recipe=recipe
    )
    assert canonical_chain_hash(rebuilt) == canonical_chain_hash(taken.chain)
    assert len(rebuilt.quotes) == len(taken.chain.quotes)


def test_the_rebuilt_chain_carries_the_captures_settlement_date():
    """§4. The resolved date reaches every contract, original and replay alike."""
    taken = captured_chain()
    expected = taken.settlement_artifact.resolved_settlement_date
    assert {q.timestamps.open_interest_as_of for q in taken.chain.quotes} == {expected}

    recipe = taken.pipeline.normalization_recipe(
        as_of=taken.chain.as_of, open_interest_as_of=expected
    )
    rebuilt = taken.pipeline.rebuild_chain_from_capture(
        manifest=taken.manifest, store=taken.store, recipe=recipe
    )
    assert {q.timestamps.open_interest_as_of for q in rebuilt.quotes} == {expected}


def test_the_reproduced_regression_adding_999999_open_interest():
    """The v2.1.6 defect, reproduced exactly.

    Open interest is the linear weight on every GEX term, so this moved the
    unsigned total by roughly two orders of magnitude -- and v2.1.6 returned it
    with ``trusted=True`` and a verified manifest.
    """
    taken = captured_chain()
    original = taken.pipeline.compute_trusted_gex(
        taken.chain, **trusted_evidence(taken)
    )
    first = taken.chain.quotes[0]
    tampered = with_first_quote(
        taken.chain, open_interest=(first.open_interest or 0) + 999_999
    )

    # The number really does move by the amount that made this worth fixing.
    diagnostic = taken.pipeline.compute_diagnostic_gex(tampered)
    assert diagnostic.total_unsigned_gex > original.total_unsigned_gex * 50

    refuses(taken, tampered)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("open_interest", 4_242),
        ("gamma", 0.0099),
        ("bid", 1.23),
        ("ask", 99.9),
        ("underlying_price", 4_321.0),
        ("delta", 0.11),
        ("theta", -9.9),
        ("vega", 1.5),
    ],
)
def test_editing_any_calculation_relevant_field_invalidates_trust(field, value):
    taken = captured_chain()
    refuses(taken, with_first_quote(taken.chain, **{field: value}))


def test_editing_the_implied_volatility_invalidates_trust():
    taken = captured_chain()
    first = taken.chain.quotes[0]
    louder = dataclasses.replace(first.iv, value=(first.iv.value or 0.1) * 2)
    refuses(taken, with_first_quote(taken.chain, iv=louder))


def test_editing_the_iv_source_invalidates_trust():
    """The same number from a different source is a different input."""
    from src.domain.iv import IVSource

    taken = captured_chain()
    first = taken.chain.quotes[0]
    relabelled = dataclasses.replace(first.iv, source=IVSource.TRADE_IV)
    refuses(taken, with_first_quote(taken.chain, iv=relabelled))


def test_editing_a_contract_identity_invalidates_trust():
    from decimal import Decimal

    taken = captured_chain()
    first = taken.chain.quotes[0]
    moved = dataclasses.replace(
        first.contract, strike=9999.0, strike_decimal=Decimal("9999")
    )
    refuses(taken, with_first_quote(taken.chain, contract=moved))


def test_editing_an_expiry_invalidates_trust():
    taken = captured_chain()
    first = taken.chain.quotes[0]
    moved = dataclasses.replace(first.contract, expiry=date(2026, 6, 19))
    refuses(taken, with_first_quote(taken.chain, contract=moved))


def test_editing_a_quote_timestamp_invalidates_trust():
    taken = captured_chain()
    first = taken.chain.quotes[0]
    shifted = dataclasses.replace(
        first.timestamps, quote_timestamp=datetime(2026, 3, 17, 9, 31, tzinfo=UTC)
    )
    refuses(taken, with_first_quote(taken.chain, timestamps=shifted))


def test_dropping_a_contract_invalidates_trust():
    taken = captured_chain()
    refuses(taken, dataclasses.replace(taken.chain, quotes=taken.chain.quotes[1:]))


@pytest.mark.parametrize(
    ("field", "value"),
    [("risk_free_rate", 0.09), ("dividend_yield", 0.05), ("spot", 4_321.0)],
)
def test_editing_a_chain_level_pricing_input_invalidates_trust(field, value):
    taken = captured_chain()
    refuses(
        taken,
        dataclasses.replace(taken.chain, **{field: value}),
        match=r"(?i)re-derived|rederiv|spot",
    )


def test_editing_the_snapshot_instant_invalidates_trust():
    taken = captured_chain()
    refuses(
        taken,
        dataclasses.replace(taken.chain, as_of=AS_OF.replace(hour=10)),
        match=r"(?i)re-derived|rederiv|spot|session",
    )


def test_a_refusal_names_the_field_that_moved():
    """A digest mismatch alone tells an operator nothing actionable."""
    taken = captured_chain()
    with pytest.raises(PipelineConsistencyError, match=r"(?i)open_interest"):
        taken.pipeline.compute_trusted_gex(
            with_first_quote(taken.chain, open_interest=1),
            **trusted_evidence(taken),
        )


def test_the_trusted_result_carries_its_normalization_receipt():
    taken = captured_chain()
    snapshot = taken.pipeline.compute_trusted_gex(
        taken.chain, **trusted_evidence(taken)
    )
    receipt = snapshot.meta["normalized_chain_receipt"]
    assert receipt["manifest_hash"] == taken.manifest.manifest_hash
    assert len(receipt["normalized_chain_hash"]) == 64
    assert receipt["contract_count"] == len(taken.chain.quotes)


# =============================================================================
# §3 -- pricing observations are part of validator equivalence
# =============================================================================


def test_pricing_observations_are_in_the_semantic_payload():
    store, manifest = build_capture()
    report = AdapterValidator.validate(
        manifest=manifest, store=store, pipeline=resolved_pipeline()
    )
    payload = report.semantic_payload()
    assert payload["pricing_observations"]
    for entry in payload["pricing_observations"]:
        for key in (
            "dimension",
            "observed_value",
            "observed_value_hash",
            "source",
            "reference",
            "observed_on",
            "manifest_hash",
            "record_ids",
        ):
            assert key in entry, key
        assert "note" not in entry


def test_a_tampered_observed_value_changes_the_semantic_payload():
    """The regression: MIXED_ACROSS_CHAIN relabelled as agreement."""
    store, manifest = build_capture(one_row_mismatched=True)
    report = AdapterValidator.validate(
        manifest=manifest, store=store, pipeline=resolved_pipeline()
    )
    observations = tuple(
        dataclasses.replace(o, observed_value="vendor_index_snapshot")
        if o.observed_value == "MIXED_ACROSS_CHAIN"
        else o
        for o in report.pricing_observations
    )
    assert any(o.observed_value == "vendor_index_snapshot" for o in observations)
    forged = dataclasses.replace(report, pricing_observations=observations)
    assert forged.semantic_payload() != report.semantic_payload()


def test_a_tampered_observation_fails_rederivation():
    store, manifest = build_capture(one_row_mismatched=True)
    pipeline = resolved_pipeline(one_row_mismatched=True)
    report = AdapterValidator.validate(
        manifest=manifest, store=store, pipeline=pipeline
    )
    forged = dataclasses.replace(
        report,
        pricing_observations=tuple(
            dataclasses.replace(o, observed_value="vendor_index_snapshot")
            for o in report.pricing_observations
        ),
    )
    rederived = AdapterValidator.validate(
        manifest=manifest, store=store, pipeline=pipeline
    )
    assert rederived.semantic_payload() != forged.semantic_payload()


# =============================================================================
# §4 -- a failed check cannot revise compatibility
# =============================================================================


def test_a_failed_check_cannot_revise_a_dimension():
    from src.config.compatibility import (
        CompatibilityStatus,
        PricingDimension,
        derive_post_capture_compatibility,
    )
    from tests.certification_fixtures import unresolved_pipeline

    # Unresolved, so UNDERLYING_SOURCE starts UNKNOWN and nothing but the
    # capture could move it. A failed check must leave it exactly there.
    pipeline = unresolved_pipeline(one_row_mismatched=True)
    store, manifest = build_capture(pipeline=pipeline, one_row_mismatched=True)
    report = AdapterValidator.validate(
        manifest=manifest, store=store, pipeline=pipeline
    )
    check = next(
        c for c in report.checks if c.dimension is PricingDimension.UNDERLYING_SOURCE
    )
    assert not check.passed
    assert check.coverage is not None
    assert check.coverage.mixed

    effective = derive_post_capture_compatibility(
        base_report=pipeline.pricing_compatibility,
        validation_report=report,
        model_spec=pipeline.model_spec,
        manifest=manifest,
    )
    result = next(
        d
        for d in effective.dimensions
        if d.dimension is PricingDimension.UNDERLYING_SOURCE
    )
    assert result.status is CompatibilityStatus.UNKNOWN
    assert any("did not pass" in r for r in effective.rejected_observations)


# =============================================================================
# §5 -- an open-interest value is not an open-interest settlement date
# =============================================================================


def test_an_oi_value_observation_carries_no_date():
    """The types are separate so the confusion cannot be expressed."""
    observation = OpenInterestValueObservation(record_id="r1", observed_value=4200)
    assert not hasattr(observation, "as_of")
    assert observation.observed_value == 4200


def test_an_oi_value_cannot_be_a_fraction_or_negative():
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)integer"):
        OpenInterestValueObservation(record_id="r1", observed_value=4200.5)
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)negative"):
        OpenInterestValueObservation(record_id="r1", observed_value=-1)


def test_a_caller_assumed_settlement_date_blocks_a_trusted_calculation():
    """The regression. v2.1.6 graded this OBSERVED by confirming the *value*.

    Expressed differently since v2.1.9 and blocking for the same reason: a
    caller assumption is not something a capture can be opened under, so a
    capture resting on one has no settlement artifact at all.
    """
    assumed = OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="caller",
        evidence_kind=EvidenceKind.CALLER_ASSUMPTION,
        chain_date=AS_OF.date(),
    )
    assert not assumed.permits_trusted_calculation

    taken = captured_chain(settlement_rule=None)
    with pytest.raises(PipelineConsistencyError, match=r"(?i)settlement rule"):
        taken.pipeline.compute_trusted_gex(taken.chain, **trusted_evidence(taken))


def test_no_settlement_date_evidence_is_treated_as_an_assumption():
    """Silence is not a vendor statement."""
    taken = captured_chain(settlement_rule=None)
    context = context_for(taken, settlement_artifact=None)
    assert context.settlement_artifact is None
    assert any("settlement rule" in f for f in context.failures)


def test_a_caller_assumption_still_permits_a_diagnostic():
    """Blocking a diagnostic would block the work that resolves the question."""
    taken = captured_chain()
    snapshot = taken.pipeline.compute_diagnostic_gex(taken.chain)
    assert snapshot.meta["trusted"] is False
    assert snapshot.total_unsigned_gex > 0


def test_a_vendor_field_date_must_name_the_records_it_was_read_from():
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)record"):
        OpenInterestAsOfEvidence(
            as_of=date(2026, 3, 16),
            source="vendor_field",
            evidence_kind=EvidenceKind.VENDOR_FIELD,
        )


def test_documented_evidence_must_carry_a_reference():
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)reference"):
        OpenInterestAsOfEvidence(
            as_of=date(2026, 3, 16),
            source="documentation",
            evidence_kind=EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION,
        )


def test_a_settlement_date_after_the_chain_is_refused():
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)has not happened"):
        OpenInterestAsOfEvidence(
            as_of=date(2026, 3, 18),
            source="caller",
            chain_date=date(2026, 3, 17),
        )


# =============================================================================
# §6 -- records are bound to the pipeline and the request that produced them
# =============================================================================


def test_every_record_is_stamped_with_its_capture_time_identity():
    taken = captured_chain()
    for record in taken.store.records():
        assert record.capture_identity.complete, record.record_id
        assert record.pipeline_fingerprint == taken.pipeline.fingerprint()


def test_a_manifest_relabelled_to_another_pipeline_fails():
    """The regression: the records now contradict the document.

    v2.1.6 compared the manifest's own ``pipeline_fingerprint`` against the
    expected one, so relabelling meant editing one field.
    """
    from src.adapters.certification import verify_capture

    other = resolved_pipeline(rate_value=3.1)
    taken = captured_chain()
    relabelled = dataclasses.replace(
        taken.manifest, pipeline_fingerprint=other.fingerprint()
    )
    result = verify_capture(
        relabelled,
        taken.store,
        plan=other.capture_plan,
        expected_pipeline_fingerprint=other.fingerprint(),
        expected_request_spec=other.request_spec(),
    )
    assert not result.verified
    assert any("RECORD_PIPELINE_MISMATCH" in f for f in result.failures)


def test_a_capture_taken_under_a_different_rate_does_not_verify():
    """``rate_value`` reaches the vendor and changes the greeks it returns."""
    from src.adapters.certification import verify_capture

    at_42 = resolved_pipeline(rate_value=4.2)
    at_31 = resolved_pipeline(rate_value=3.1)
    taken = captured_chain(at_42)

    result = verify_capture(
        taken.manifest,
        taken.store,
        plan=at_31.capture_plan,
        expected_pipeline_fingerprint=at_42.fingerprint(),
        expected_request_spec=at_31.request_spec(),
    )
    assert not result.verified
    assert any("REQUEST_PARAMETERS_MISMATCH" in f for f in result.failures)


def test_the_request_spec_states_every_endpoint_the_plan_uses():
    pipeline = resolved_pipeline()
    spec = pipeline.request_spec()
    for endpoint in pipeline.capture_plan.required_endpoints:
        assert spec.parameters_for(endpoint.value) is not None, endpoint
    assert spec.parameters_for("/v3/not/an/endpoint") is None


def test_the_request_spec_moves_with_the_rate():
    assert (
        resolved_pipeline(rate_value=4.2).request_spec().fingerprint
        != resolved_pipeline(rate_value=3.1).request_spec().fingerprint
    )


# =============================================================================
# §9 -- audit identities are complete digests
# =============================================================================


def test_audit_hashes_are_full_sha256():
    from src.adapters.raw_store import canonical_parameter_hash

    taken = captured_chain()
    recipe = taken.pipeline.normalization_recipe(as_of=AS_OF)
    for digest in (
        canonical_parameter_hash({"symbol": "SPXW"}),
        taken.pipeline.request_spec().fingerprint,
        recipe.recipe_hash,
        recipe.rules_fingerprint,
        taken.manifest.manifest_hash,
        canonical_chain_hash(taken.chain),
    ):
        assert len(digest) == 64, digest
        assert set(digest) <= set("0123456789abcdef")


def test_the_manifest_records_carry_their_byte_length_and_completeness():
    taken = captured_chain()
    for entry in taken.manifest.records:
        assert entry.byte_length > 0
        assert entry.capture_complete is True


@pytest.mark.parametrize(
    "change",
    [
        {"byte_length": 1},
        {"capture_complete": False},
        {"pipeline_fingerprint": "0" * 16},
        {"capture_plan_fingerprint": "0" * 16},
        {"request_spec_fingerprint": "0" * 64},
        {"normalization_recipe_fingerprint": "0" * 64},
        {"capture_session_id": "somebody-elses-session"},
    ],
)
def test_mutating_a_capture_time_field_moves_the_manifest_hash(change):
    _, manifest = build_capture()
    records = list(manifest.records)
    records[0] = dataclasses.replace(records[0], **change)
    mutated = dataclasses.replace(manifest, records=tuple(records))
    assert mutated.manifest_hash != manifest.manifest_hash


# =============================================================================
# §7 -- capture origin is derived, and a wrapper is not an origin
# =============================================================================


def test_the_retry_wrapper_does_not_erase_the_origin():
    """The defect this found. ``build_thetadata_client`` always wraps.

    ``capture_origin_of`` reads ``capture_origin`` off whatever transport it is
    handed, and the production client hands it a ``RetryingTransport``. Without
    delegation every real capture would have been stamped ``UNKNOWN_ORIGIN``.
    """
    from src.adapters.raw_store import CaptureOrigin
    from src.adapters.thetadata.client import capture_origin_of
    from src.adapters.transport import FakeTransport, RetryingTransport

    wrapped = RetryingTransport(FakeTransport())
    assert capture_origin_of(wrapped) is CaptureOrigin.OFFLINE_FIXTURE


def test_a_real_fetch_stamps_a_real_origin():
    from src.adapters.raw_store import CaptureOrigin

    taken = captured_chain()
    assert taken.manifest.capture_origin is CaptureOrigin.OFFLINE_FIXTURE
    assert all(
        r.capture_origin is CaptureOrigin.OFFLINE_FIXTURE for r in taken.store.records()
    )


# =============================================================================
# §10 -- analytical readiness is a separate question
# =============================================================================


def test_analytical_readiness_is_a_separate_axis():
    from src.adapters.certification import (
        ANALYTICAL_DATASET_REQUIREMENTS,
        AnalyticalReadiness,
        CertificationState,
    )

    assert AnalyticalReadiness.READY_FOR_ANALYTICAL_DATASET.value not in {
        state.value for state in CertificationState
    }
    assert len(ANALYTICAL_DATASET_REQUIREMENTS) >= 5
    assert any(
        "OD-26" in requirement for requirement in ANALYTICAL_DATASET_REQUIREMENTS
    )
    assert any(
        "OD-11" in requirement for requirement in ANALYTICAL_DATASET_REQUIREMENTS
    )
