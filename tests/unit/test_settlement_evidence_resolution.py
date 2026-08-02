"""An enum does not establish a settlement date. A resolver does.

v2.1.7 separated the open-interest *value* from the open-interest *date*, which
was the right split, and then let the date authorize itself. ``EvidenceKind``
had a ``permits_trusted_calculation`` property, the trusted path read it, and
that was the whole check:

    OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="vendor_field",
        evidence_kind=EvidenceKind.VENDOR_FIELD,
        record_ids=("fake-record",),
    )

``VENDOR_FIELD`` permits a trusted calculation, so that object did. The record
was never opened. ``AUTHORITATIVE_VENDOR_DOCUMENTATION`` required a non-empty
``reference``, and ``"lol"`` is non-empty.

Every test here fails against v2.1.7.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.adapters.errors import ThetaDataProvenanceError
from src.adapters.evidence_resolvers import (
    DocumentationRule,
    DocumentationRuleRegistry,
    ScheduleDerivation,
    content_hash_of,
    resolve_settlement_date,
)
from src.adapters.open_interest import EvidenceKind, OpenInterestAsOfEvidence
from src.config.pipeline import PipelineConsistencyError
from tests.certification_fixtures import (
    AS_OF,
    FIXTURE_OI_EVIDENCE_ID,
    captured_chain,
    register_fixture_documentation_rule,
    trusted_evidence,
)

DOCUMENT = "tests/fixtures/vendor_conventions.md"


def a_rule(**changes):
    import pathlib

    payload = {
        "evidence_id": "rule-1",
        "document_reference": DOCUMENT,
        "document_content_hash": content_hash_of(pathlib.Path(DOCUMENT)),
        "rule_identifier": "oi_settles_prior_session",
        "effective_from": date(2020, 1, 1),
        "derivation_version": "test/1",
    }
    payload.update(changes)
    return DocumentationRule(**payload)


# =============================================================================
# §4 -- vendor-field evidence is reread, not believed
# =============================================================================


def test_fake_vendor_field_evidence_does_not_resolve():
    """The regression. ``record_ids=("fake-record",)`` was trusted."""
    taken = captured_chain()
    evidence = OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="vendor_field",
        evidence_kind=EvidenceKind.VENDOR_FIELD,
        record_ids=("fake-record",),
        chain_date=AS_OF.date(),
    )
    # The enum still says yes. That is precisely why it must not be the check.
    assert evidence.permits_trusted_calculation

    resolved = resolve_settlement_date(
        evidence, manifest=taken.manifest, store=taken.store
    )
    assert not resolved.established
    assert not resolved.permits_trusted_calculation
    assert "does not contain" in resolved.failure


def test_vendor_field_evidence_naming_a_real_record_still_needs_the_field():
    """ThetaData publishes no settlement-date column. OD-26 in one test."""
    taken = captured_chain()
    oi_record = next(
        r for r in taken.manifest.records if r.endpoint.endswith("open_interest")
    )
    evidence = OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="vendor_field",
        evidence_kind=EvidenceKind.VENDOR_FIELD,
        record_ids=(oi_record.record_id,),
        chain_date=AS_OF.date(),
    )
    resolved = resolve_settlement_date(
        evidence, manifest=taken.manifest, store=taken.store
    )
    assert not resolved.established
    assert "settlement-date field" in resolved.failure


def test_vendor_field_evidence_with_no_capture_cannot_resolve():
    evidence = OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="vendor_field",
        evidence_kind=EvidenceKind.VENDOR_FIELD,
        record_ids=("r1",),
    )
    resolved = resolve_settlement_date(evidence)
    assert not resolved.established
    assert "never opened" in resolved.failure


def test_a_fake_vendor_field_date_cannot_authorize_a_trusted_calculation():
    """End to end, through the public API."""
    taken = captured_chain()
    evidence = OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="vendor_field",
        evidence_kind=EvidenceKind.VENDOR_FIELD,
        record_ids=("fake-record",),
        chain_date=AS_OF.date(),
    )
    with pytest.raises(PipelineConsistencyError, match=r"(?i)does not contain"):
        taken.pipeline.compute_trusted_gex(
            taken.chain,
            **trusted_evidence(taken, open_interest_as_of_evidence=evidence),
        )


# =============================================================================
# §4/§10 -- documentation is looked up, and bound to its content
# =============================================================================


def test_an_arbitrary_reference_does_not_resolve():
    """The regression. ``reference="lol"`` satisfied v2.1.7."""
    evidence = OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="documentation",
        evidence_kind=EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION,
        reference="lol",
    )
    assert evidence.permits_trusted_calculation  # the enum still says yes

    resolved = resolve_settlement_date(evidence, registry=DocumentationRuleRegistry())
    assert not resolved.established
    assert "not a registered documentation rule" in resolved.failure


def test_an_arbitrary_reference_cannot_authorize_a_trusted_calculation():
    taken = captured_chain()
    evidence = OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="documentation",
        evidence_kind=EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION,
        reference="lol",
        chain_date=AS_OF.date(),
    )
    with pytest.raises(PipelineConsistencyError, match=r"(?i)not a registered"):
        taken.pipeline.compute_trusted_gex(
            taken.chain,
            **trusted_evidence(taken, open_interest_as_of_evidence=evidence),
        )


def test_a_registered_rule_resolves():
    registry = DocumentationRuleRegistry()
    registry.register(a_rule())
    evidence = OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="documentation",
        evidence_kind=EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION,
        reference="rule-1",
    )
    resolved = resolve_settlement_date(evidence, registry=registry)
    assert resolved.established
    assert resolved.permits_trusted_calculation
    assert len(resolved.rule_fingerprint) == 64


def test_changing_the_document_content_changes_the_evidence_fingerprint():
    """The point of a content hash. A vendor can rewrite a page."""
    original = a_rule()
    rewritten = a_rule(document_content_hash="0" * 64)
    assert rewritten.evidence_fingerprint != original.evidence_fingerprint


def test_a_rule_must_carry_a_full_content_hash():
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)SHA-256"):
        a_rule(document_content_hash="abc123")


def test_a_rule_outside_its_effective_period_does_not_resolve():
    registry = DocumentationRuleRegistry()
    registry.register(a_rule(effective_from=date(2027, 1, 1)))
    evidence = OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="documentation",
        evidence_kind=EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION,
        reference="rule-1",
    )
    resolved = resolve_settlement_date(evidence, registry=registry)
    assert not resolved.established
    assert "in force" in resolved.failure


def test_one_evidence_id_cannot_mean_two_things():
    registry = DocumentationRuleRegistry()
    registry.register(a_rule())
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)already registered"):
        registry.register(a_rule(rule_identifier="something_else"))


def test_the_production_registry_holds_no_thetadata_settlement_rule():
    """The honest state, asserted so it cannot drift quietly.

    Pre-populating this with a plausible-looking entry would be exactly the
    defect v2.1.8 closes. See OPEN_DECISIONS OD-26 and OD-37.
    """
    from src.adapters.evidence_resolvers import DOCUMENTATION_RULES

    register_fixture_documentation_rule()
    registered = set(DOCUMENTATION_RULES._rules)
    assert registered <= {FIXTURE_OI_EVIDENCE_ID}, (
        "a documentation rule about ThetaData appeared in the production "
        f"registry: {sorted(registered)}"
    )


# =============================================================================
# §4 -- schedule derivation needs its artefact
# =============================================================================


def test_schedule_evidence_without_a_derivation_does_not_resolve():
    evidence = OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="schedule",
        evidence_kind=EvidenceKind.DERIVED_FROM_VERIFIED_VENDOR_SCHEDULE,
    )
    resolved = resolve_settlement_date(evidence)
    assert not resolved.established
    assert "no derivation artefact" in resolved.failure


def test_a_derivation_that_disagrees_with_the_claim_does_not_resolve():
    registry = DocumentationRuleRegistry()
    registry.register(a_rule())
    evidence = OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="schedule",
        evidence_kind=EvidenceKind.DERIVED_FROM_VERIFIED_VENDOR_SCHEDULE,
    )
    resolved = resolve_settlement_date(
        evidence,
        registry=registry,
        derivation=ScheduleDerivation(
            rule_version="v1",
            input_session_date=date(2026, 3, 17),
            derived_settlement_date=date(2026, 3, 13),
            supporting_evidence_id="rule-1",
        ),
    )
    assert not resolved.established
    assert "the derivation produced" in resolved.failure


def test_a_derivation_resting_on_unregistered_evidence_does_not_resolve():
    evidence = OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="schedule",
        evidence_kind=EvidenceKind.DERIVED_FROM_VERIFIED_VENDOR_SCHEDULE,
    )
    resolved = resolve_settlement_date(
        evidence,
        registry=DocumentationRuleRegistry(),
        derivation=ScheduleDerivation(
            rule_version="v1",
            input_session_date=date(2026, 3, 17),
            derived_settlement_date=date(2026, 3, 16),
            supporting_evidence_id="nobody-registered-this",
        ),
    )
    assert not resolved.established
    assert "is not registered" in resolved.failure


def test_a_complete_derivation_resolves():
    registry = DocumentationRuleRegistry()
    registry.register(a_rule())
    evidence = OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="schedule",
        evidence_kind=EvidenceKind.DERIVED_FROM_VERIFIED_VENDOR_SCHEDULE,
    )
    resolved = resolve_settlement_date(
        evidence,
        registry=registry,
        derivation=ScheduleDerivation(
            rule_version="v1",
            input_session_date=date(2026, 3, 17),
            derived_settlement_date=date(2026, 3, 16),
            supporting_evidence_id="rule-1",
        ),
    )
    assert resolved.established
    assert len(resolved.rule_fingerprint) == 64


# =============================================================================
# §4 -- the value's provenance and the date's evidence must agree
# =============================================================================


def test_provenance_and_settlement_evidence_must_agree():
    from tests.certification_fixtures import verified_oi

    taken = captured_chain()
    disagreeing = OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 13),
        source="documentation",
        evidence_kind=EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION,
        reference=FIXTURE_OI_EVIDENCE_ID,
        chain_date=AS_OF.date(),
    )
    register_fixture_documentation_rule()
    with pytest.raises(PipelineConsistencyError, match=r"(?i)provenance says"):
        taken.pipeline.compute_trusted_gex(
            taken.chain,
            **trusted_evidence(
                taken,
                open_interest_provenance=verified_oi(taken.store, taken.manifest),
                open_interest_as_of_evidence=disagreeing,
            ),
        )


# =============================================================================
# §10 -- the pipeline's own documentation evidence is content-bound
# =============================================================================


def test_a_documentation_reference_carries_the_content_it_pointed_at():
    """A pricing convention read out of a document, bound to that document."""
    import pathlib

    from tests.certification_fixtures import resolved_pipeline

    observations = resolved_pipeline().config.pricing_attestations
    documented = [o for o in observations if o.document_content_hash]
    assert documented, "the fixture attestations cite a document"
    expected = content_hash_of(pathlib.Path(DOCUMENT))
    assert all(o.document_content_hash == expected for o in documented)


def test_rewriting_the_referenced_document_moves_the_pipeline_fingerprint(tmp_path):
    """The regression. A vendor can rewrite a page without renaming it.

    v2.1.7 carried the reference into the fingerprint and not the content, so
    the same digest described two different claims about how gamma is priced.
    """
    import pathlib

    from tests.certification_fixtures import resolved_pipeline

    root = pathlib.Path(__file__).resolve().parents[2]
    document = root / "tests" / "fixtures" / "_rewritable_convention.md"
    document.write_text("The vendor uses ACT/365F.\n", encoding="utf-8")
    try:
        reference = "tests/fixtures/_rewritable_convention.md"
        before = resolved_pipeline(pricing_attestations=_citing(reference))
        first = before.fingerprint()
        first_evidence = before.documentation_evidence_fingerprints

        document.write_text("The vendor uses ACT/360, actually.\n", encoding="utf-8")
        after = resolved_pipeline(pricing_attestations=_citing(reference))
    finally:
        document.unlink(missing_ok=True)

    assert after.documentation_evidence_fingerprints != first_evidence
    assert after.fingerprint() != first


def test_a_url_reference_records_no_content_hash():
    """Honest emptiness. This release makes no network request to check a page."""
    from src.config.compatibility import (
        EvidenceSource,
        PricingDimension,
        VendorObservation,
    )

    observation = VendorObservation(
        dimension=PricingDimension.DAY_COUNT,
        observed_value="ACT_365_FIXED",
        source=EvidenceSource.VENDOR_DOCUMENTATION,
        reference="https://http-docs.thetadata.us/",
        observed_at="2026-08-01",
    )
    assert observation.document_content_hash == ""


def test_a_caller_cannot_supply_a_content_hash():
    """Derived, not accepted -- the same rule as spot tolerance and valuation time."""
    from src.config.compatibility import (
        EvidenceSource,
        PricingDimension,
        VendorObservation,
    )

    with pytest.raises(TypeError, match="document_content_hash"):
        VendorObservation(
            dimension=PricingDimension.DAY_COUNT,
            observed_value="ACT_365_FIXED",
            source=EvidenceSource.VENDOR_DOCUMENTATION,
            reference=DOCUMENT,
            observed_at="2026-08-01",
            document_content_hash="0" * 64,
        )


def _citing(reference: str):
    """Fixture attestations rewritten to cite one particular document."""
    from tests.pricing_evidence import attestations

    return [{**entry, "reference": reference} for entry in attestations()]


def test_a_caller_assumption_still_blocks_a_trusted_calculation():
    taken = captured_chain()
    assumed = OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="caller",
        evidence_kind=EvidenceKind.CALLER_ASSUMPTION,
        chain_date=AS_OF.date(),
    )
    resolved = resolve_settlement_date(assumed)
    # It *resolves* -- the caller really did state a date -- and resolving is
    # not the same as authorizing.
    assert resolved.established
    assert not resolved.permits_trusted_calculation

    with pytest.raises(PipelineConsistencyError, match=r"(?i)settlement date"):
        taken.pipeline.compute_trusted_gex(
            taken.chain,
            **trusted_evidence(taken, open_interest_as_of_evidence=assumed),
        )
