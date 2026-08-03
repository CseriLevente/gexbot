"""A documented rule derives the settlement date. It does not approve one.

v2.1.7 let an enum authorize a date. v2.1.8 replaced the enum with resolvers,
which was right and stopped one step short:

    rule = rules.get(evidence.reference)
    if not rule.covers(evidence.as_of):
        return failure
    return ResolvedSettlementDate(as_of=evidence.as_of, ...)

The date still came from the caller. The rule was consulted only to confirm it
was *in force* on the day already chosen, so one registered rule saying "prior
trading session" would authorize 2026-03-16, 2026-03-15 and 2026-03-01 alike
for a 2026-03-17 chain. ``normalized_value: str`` is why: free text cannot be
applied to a session date, so the date had to come from somewhere, and the only
somewhere available was the argument list.

And a rule could carry any 64-character string as a content hash, because
nothing opened the file.

Every test here fails against v2.1.8.
"""

from __future__ import annotations

import pathlib
from datetime import date

import pytest

from src.adapters.errors import ThetaDataProvenanceError
from src.adapters.evidence_resolvers import (
    DocumentationRule,
    DocumentationRuleRegistry,
    ScheduleDerivation,
    SettlementDateRuleArtifact,
    content_hash_of,
    resolve_settlement_date,
    settlement_artifact_from,
)
from src.adapters.open_interest import EvidenceKind
from src.config.pipeline import PipelineConsistencyError
from src.domain.settlement import (
    SettlementRule,
    SettlementRuleError,
    SettlementRuleKind,
)
from tests.certification_fixtures import (
    AS_OF,
    FIXTURE_DOCUMENT,
    FIXTURE_OI_EVIDENCE_ID,
    captured_chain,
    documented_settlement_rule,
    register_fixture_documentation_rule,
    trusted_evidence,
)

CHAIN_SESSION = AS_OF.date()  # Tuesday 2026-03-17
PRIOR_SESSION = date(2026, 3, 16)  # Monday


def prior_session_rule(**changes):
    payload = {
        "evidence_id": "rule-1",
        "document_reference": FIXTURE_DOCUMENT,
        "document_content_hash": content_hash_of(pathlib.Path(FIXTURE_DOCUMENT)),
        "rule_identifier": "oi_settles_prior_session",
        "effective_from": date(2020, 1, 1),
        "derivation_version": "test/1",
        "rule": SettlementRule(kind=SettlementRuleKind.PRIOR_TRADING_SESSION),
    }
    payload.update(changes)
    return DocumentationRule(**payload)


def registry_with(rule):
    registry = DocumentationRuleRegistry()
    registry.register(rule)
    return registry


# =============================================================================
# §2 -- the rule computes the date, through the trading calendar
# =============================================================================


def test_a_prior_session_rule_derives_exactly_the_prior_session():
    """The regression, stated as the spec states it.

    One rule, one chain date, one answer. 2026-03-15 is a Sunday and 2026-03-01
    is two weeks earlier; under v2.1.8 the same rule would have authorized both,
    because the rule was never applied to anything.
    """
    resolved = resolve_settlement_date(
        chain_session_date=CHAIN_SESSION,
        evidence_kind=EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION,
        evidence_id="rule-1",
        registry=registry_with(prior_session_rule()),
    )
    assert resolved.established
    assert resolved.as_of == PRIOR_SESSION
    assert resolved.as_of != date(2026, 3, 15)
    assert resolved.as_of != date(2026, 3, 1)


@pytest.mark.parametrize(
    ("chain_date", "expected", "why"),
    [
        (date(2026, 3, 17), date(2026, 3, 16), "Tuesday to Monday"),
        (date(2026, 3, 16), date(2026, 3, 13), "Monday back over the weekend"),
        (date(2026, 4, 6), date(2026, 4, 2), "Monday over Good Friday 2026-04-03"),
        (date(2026, 1, 2), date(2025, 12, 31), "over New Year's Day"),
        (date(2026, 7, 6), date(2026, 7, 2), "over the observed Independence Day"),
        (date(2026, 11, 27), date(2026, 11, 25), "over Thanksgiving"),
    ],
)
def test_the_derivation_walks_the_real_calendar(chain_date, expected, why):
    """Weekends, fixed holidays and the one moveable one."""
    rule = SettlementRule(kind=SettlementRuleKind.PRIOR_TRADING_SESSION)
    assert rule.resolve(chain_date) == expected, why


def test_good_friday_is_not_a_trading_session():
    from src.gex.calendar import good_friday, is_trading_session

    assert good_friday(2026) == date(2026, 4, 3)
    assert not is_trading_session(date(2026, 4, 3))


def test_a_rule_cannot_be_applied_to_a_non_session():
    rule = SettlementRule(kind=SettlementRuleKind.PRIOR_TRADING_SESSION)
    with pytest.raises(SettlementRuleError, match=r"(?i)not a trading session"):
        rule.resolve(date(2026, 3, 15))  # Sunday


def test_an_offset_rule_needs_its_offset_and_refuses_a_negative_one():
    with pytest.raises(SettlementRuleError, match=r"(?i)how many sessions"):
        SettlementRule(kind=SettlementRuleKind.TRADING_SESSION_OFFSET)
    with pytest.raises(SettlementRuleError, match=r"(?i)has not happened"):
        SettlementRule(
            kind=SettlementRuleKind.TRADING_SESSION_OFFSET, trading_session_offset=-1
        )


def test_a_rule_naming_an_unimplemented_calendar_is_refused():
    """'The prior session' is a different day on a different exchange."""
    with pytest.raises(SettlementRuleError, match=r"(?i)not implemented"):
        SettlementRule(
            kind=SettlementRuleKind.PRIOR_TRADING_SESSION, calendar_id="XETRA"
        )


def test_the_same_rule_cannot_authorize_two_different_dates():
    """The v2.1.8 defect, stated as the property that replaces it."""
    registry = registry_with(prior_session_rule())
    answers = {
        resolve_settlement_date(
            chain_session_date=CHAIN_SESSION,
            evidence_kind=EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION,
            evidence_id="rule-1",
            registry=registry,
        ).as_of
        for _ in range(5)
    }
    assert answers == {PRIOR_SESSION}
    # And there is no parameter through which a caller could offer another.
    import inspect

    assert "as_of" not in inspect.signature(resolve_settlement_date).parameters


def test_a_rule_with_no_typed_semantics_establishes_nothing():
    """``normalized_value: str`` documented something. Not this."""
    registry = registry_with(prior_session_rule(rule=None))
    resolved = resolve_settlement_date(
        chain_session_date=CHAIN_SESSION,
        evidence_kind=EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION,
        evidence_id="rule-1",
        registry=registry,
    )
    assert not resolved.established
    assert "no typed settlement semantics" in resolved.failure


def test_free_text_is_not_accepted_as_a_rule():
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)SettlementRule"):
        prior_session_rule(rule="prior_session")


# =============================================================================
# §3 -- the document is opened, read and hashed
# =============================================================================


def test_a_missing_document_fails_registration():
    """The named regression: ``/definitely/missing`` with ``'0'*64``."""
    registry = DocumentationRuleRegistry()
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)does not exist"):
        registry.register(
            prior_session_rule(
                document_reference="definitely/missing.md",
                document_content_hash="0" * 64,
            )
        )


def test_an_absolute_path_is_refused():
    registry = DocumentationRuleRegistry()
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)repository-relative"):
        registry.register(
            prior_session_rule(
                document_reference="/definitely/missing",
                document_content_hash="0" * 64,
            )
        )


def test_a_content_hash_mismatch_fails_registration():
    """A real file, a hash of something else."""
    registry = DocumentationRuleRegistry()
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)hashes to"):
        registry.register(prior_session_rule(document_content_hash="a" * 64))


def test_a_url_cannot_be_content_verified():
    """No network request is made, so no page is read, so no page is evidence."""
    registry = DocumentationRuleRegistry()
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)is a URL"):
        registry.register(
            prior_session_rule(document_reference="https://http-docs.thetadata.us/")
        )


def test_registration_records_where_the_bytes_were_read_from():
    registry = registry_with(prior_session_rule())
    stored = registry.get("rule-1")
    assert stored.verified_location == FIXTURE_DOCUMENT
    assert stored.establishes_a_date


def test_an_unregistered_rule_cannot_establish_a_date():
    """A rule object nobody put through registration was never opened."""
    unregistered = prior_session_rule()
    assert not unregistered.establishes_a_date


def test_rewriting_the_document_changes_the_evidence_fingerprint():
    original = prior_session_rule()
    rewritten = prior_session_rule(document_content_hash="0" * 64)
    assert rewritten.evidence_fingerprint != original.evidence_fingerprint


def test_one_evidence_id_cannot_mean_two_things():
    registry = registry_with(prior_session_rule())
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)already registered"):
        registry.register(prior_session_rule(rule_identifier="something_else"))


def test_the_production_registry_holds_no_thetadata_settlement_rule():
    """The honest state, asserted so it cannot drift quietly.

    Pre-populating this with a plausible-looking entry would be exactly the
    defect v2.1.8 and v2.1.9 close. See OPEN_DECISIONS OD-26 and OD-37.
    """
    from src.adapters.evidence_resolvers import DOCUMENTATION_RULES

    register_fixture_documentation_rule()
    assert set(DOCUMENTATION_RULES.registered_ids()) <= {FIXTURE_OI_EVIDENCE_ID}


# =============================================================================
# §1 -- the rule is chosen before the capture, and never after
# =============================================================================


def test_a_capture_with_no_settlement_rule_cannot_later_become_trusted():
    """The central regression.

    v2.1.8 stamped ``open_interest_date_rule_fingerprint=""`` on an ordinary
    capture and then accepted documentation evidence at calculation time, so the
    same bytes went from "no rule established" to trusted on the strength of an
    argument.
    """
    taken = captured_chain(settlement_rule=None)
    assert all(
        not r.open_interest_date_rule_fingerprint for r in taken.manifest.records
    )
    with pytest.raises(PipelineConsistencyError, match=r"(?i)established no"):
        taken.pipeline.compute_trusted_gex(taken.chain, **trusted_evidence(taken))


def test_the_trusted_api_accepts_no_settlement_evidence():
    """Closed at the signature, which is where it stays closed."""
    import inspect

    from src.config.pipeline import ThetaDataResearchPipeline

    parameters = set(
        inspect.signature(ThetaDataResearchPipeline.compute_trusted_gex).parameters
    )
    assert "open_interest_as_of_evidence" not in parameters
    assert "settlement_rule" not in parameters
    assert "open_interest_as_of" not in parameters


def test_a_trusted_capture_stamps_a_nonempty_rule_fingerprint():
    taken = captured_chain()
    stamps = {r.open_interest_date_rule_fingerprint for r in taken.manifest.records}
    assert len(stamps) == 1
    assert len(stamps.pop()) == 64


def test_the_capture_session_takes_the_rule_and_derives_the_date():
    taken = captured_chain()
    assert taken.settlement_artifact.chain_session_date == CHAIN_SESSION
    assert taken.settlement_artifact.resolved_settlement_date == PRIOR_SESSION
    assert (
        taken.settlement_artifact.normalized_rule.kind
        is SettlementRuleKind.PRIOR_TRADING_SESSION
    )


def test_an_artifact_whose_rule_does_not_produce_its_date_is_refused():
    """The v2.1.8 defect, refused even when preserved in an artifact."""
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)the rule produces"):
        SettlementDateRuleArtifact(
            evidence_kind=EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION,
            rule_fingerprint="f" * 64,
            evidence_id="rule-1",
            normalized_rule=SettlementRule(
                kind=SettlementRuleKind.PRIOR_TRADING_SESSION
            ),
            chain_session_date=CHAIN_SESSION,
            resolved_settlement_date=date(2026, 3, 1),
        )


def test_an_artifact_cannot_rest_on_a_caller_assumption():
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)CALLER_ASSUMPTION"):
        SettlementDateRuleArtifact(
            evidence_kind=EvidenceKind.CALLER_ASSUMPTION,
            rule_fingerprint="f" * 64,
            evidence_id="",
            normalized_rule=SettlementRule(
                kind=SettlementRuleKind.PRIOR_TRADING_SESSION
            ),
            chain_session_date=CHAIN_SESSION,
            resolved_settlement_date=PRIOR_SESSION,
        )


def test_a_caller_assumption_establishes_no_date_at_all():
    resolved = resolve_settlement_date(
        chain_session_date=CHAIN_SESSION,
        evidence_kind=EvidenceKind.CALLER_ASSUMPTION,
    )
    assert not resolved.established
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)no settlement date"):
        settlement_artifact_from(resolved, chain_session_date=CHAIN_SESSION)


def test_a_settlement_artifact_for_another_session_is_refused_at_capture():
    from tests.certification_fixtures import resolved_pipeline

    built = resolved_pipeline()
    other_session = documented_settlement_rule(date(2026, 3, 16))
    with pytest.raises(PipelineConsistencyError, match=r"(?i)different session"):
        built.capture_session(
            store=None,
            session_id="s",
            as_of=AS_OF,
            settlement_rule=other_session,
        )


# =============================================================================
# §4 -- the resolved date reaches the chain, the replay and the refusals
# =============================================================================


def test_the_trusted_chain_carries_the_resolved_date_on_every_contract():
    taken = captured_chain()
    assert {q.timestamps.open_interest_as_of for q in taken.chain.quotes} == {
        PRIOR_SESSION
    }
    taken.pipeline.compute_trusted_gex(taken.chain, **trusted_evidence(taken))


def test_a_chain_carrying_a_different_date_is_refused():
    import dataclasses

    taken = captured_chain()
    shifted = dataclasses.replace(
        taken.chain,
        quotes=tuple(
            dataclasses.replace(
                q,
                timestamps=dataclasses.replace(
                    q.timestamps, open_interest_as_of=date(2026, 3, 13)
                ),
            )
            for q in taken.chain.quotes
        ),
    )
    with pytest.raises(PipelineConsistencyError, match=r"(?i)carry 2026-03-13"):
        taken.pipeline.compute_trusted_gex(shifted, **trusted_evidence(taken))


def test_a_chain_carrying_no_date_cannot_claim_an_established_one():
    import dataclasses

    taken = captured_chain()
    undated = dataclasses.replace(
        taken.chain,
        quotes=tuple(
            dataclasses.replace(
                q,
                timestamps=dataclasses.replace(q.timestamps, open_interest_as_of=None),
            )
            for q in taken.chain.quotes
        ),
    )
    with pytest.raises(PipelineConsistencyError, match=r"(?i)carries none"):
        taken.pipeline.compute_trusted_gex(undated, **trusted_evidence(taken))


def test_the_receipt_records_the_settlement_date_the_capture_derived():
    taken = captured_chain()
    snapshot = taken.pipeline.compute_trusted_gex(
        taken.chain, **trusted_evidence(taken)
    )
    receipt = snapshot.meta["normalized_chain_receipt"]
    assert receipt["recipe_hash"]
    context = snapshot.meta["evidence_context"]
    assert (
        context["settlement_artifact"]["resolved_settlement_date"]
        == PRIOR_SESSION.isoformat()
    )


# =============================================================================
# §4 -- vendor-field evidence still rereads the record it names
# =============================================================================


def test_fake_vendor_field_evidence_does_not_resolve():
    taken = captured_chain()
    resolved = resolve_settlement_date(
        chain_session_date=CHAIN_SESSION,
        evidence_kind=EvidenceKind.VENDOR_FIELD,
        record_ids=("fake-record",),
        manifest=taken.manifest,
        store=taken.store,
    )
    assert not resolved.established
    assert "does not contain" in resolved.failure


def test_vendor_field_evidence_naming_a_real_record_still_needs_the_field():
    """ThetaData publishes no settlement-date column. OD-26 in one test."""
    taken = captured_chain()
    oi_record = next(
        r for r in taken.manifest.records if r.endpoint.endswith("open_interest")
    )
    resolved = resolve_settlement_date(
        chain_session_date=CHAIN_SESSION,
        evidence_kind=EvidenceKind.VENDOR_FIELD,
        record_ids=(oi_record.record_id,),
        manifest=taken.manifest,
        store=taken.store,
    )
    assert not resolved.established
    assert "settlement-date field" in resolved.failure


def test_vendor_field_evidence_with_no_capture_cannot_resolve():
    resolved = resolve_settlement_date(
        chain_session_date=CHAIN_SESSION,
        evidence_kind=EvidenceKind.VENDOR_FIELD,
        record_ids=("r1",),
    )
    assert not resolved.established
    assert "never opened" in resolved.failure


# =============================================================================
# §4 -- schedule derivation needs its artefact and its documentation
# =============================================================================


def test_schedule_evidence_without_a_derivation_does_not_resolve():
    resolved = resolve_settlement_date(
        chain_session_date=CHAIN_SESSION,
        evidence_kind=EvidenceKind.DERIVED_FROM_VERIFIED_VENDOR_SCHEDULE,
    )
    assert not resolved.established
    assert "no derivation artefact" in resolved.failure


def test_a_derivation_run_for_another_session_does_not_resolve():
    resolved = resolve_settlement_date(
        chain_session_date=CHAIN_SESSION,
        evidence_kind=EvidenceKind.DERIVED_FROM_VERIFIED_VENDOR_SCHEDULE,
        registry=registry_with(prior_session_rule()),
        derivation=ScheduleDerivation(
            rule_version="v1",
            input_session_date=date(2026, 3, 16),
            supporting_evidence_id="rule-1",
        ),
    )
    assert not resolved.established
    assert "was run for session" in resolved.failure


def test_a_derivation_resting_on_unregistered_evidence_does_not_resolve():
    resolved = resolve_settlement_date(
        chain_session_date=CHAIN_SESSION,
        evidence_kind=EvidenceKind.DERIVED_FROM_VERIFIED_VENDOR_SCHEDULE,
        registry=DocumentationRuleRegistry(),
        derivation=ScheduleDerivation(
            rule_version="v1",
            input_session_date=CHAIN_SESSION,
            supporting_evidence_id="nobody-registered-this",
        ),
    )
    assert not resolved.established
    assert "is not registered" in resolved.failure


def test_a_complete_derivation_resolves_by_applying_its_rule():
    resolved = resolve_settlement_date(
        chain_session_date=CHAIN_SESSION,
        evidence_kind=EvidenceKind.DERIVED_FROM_VERIFIED_VENDOR_SCHEDULE,
        registry=registry_with(prior_session_rule()),
        derivation=ScheduleDerivation(
            rule_version="v1",
            input_session_date=CHAIN_SESSION,
            supporting_evidence_id="rule-1",
        ),
    )
    assert resolved.established
    assert resolved.as_of == PRIOR_SESSION
    assert len(resolved.rule_fingerprint) == 64


def test_a_derivation_cannot_state_its_own_answer():
    """The v2.1.8 field is gone: a derivation applies a rule, it does not assert."""
    import inspect

    parameters = set(inspect.signature(ScheduleDerivation).parameters)
    assert "derived_settlement_date" not in parameters


# =============================================================================
# §1 -- the value's provenance and the capture's rule must agree
# =============================================================================


def test_provenance_and_the_capture_bound_rule_must_agree():
    from src.adapters.certification import OpenInterestProvenance

    taken = captured_chain()
    disagreeing = OpenInterestProvenance(
        as_of=date(2026, 3, 13),
        source="vendor_field",
        chain_date=CHAIN_SESSION,
    )
    with pytest.raises(PipelineConsistencyError, match=r"(?i)capture-bound rule"):
        taken.pipeline.compute_trusted_gex(
            taken.chain,
            **trusted_evidence(taken, open_interest_provenance=disagreeing),
        )
