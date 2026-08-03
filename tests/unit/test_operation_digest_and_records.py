"""A stored digest is not evidence about the fields beside it.

Two v2.1.8 checks that looked stronger than they were.

**The operation fingerprint.** ``verify_capture`` compared the stored
``operation_fingerprint`` across a manifest's records and required them equal.
Equal to each other, and to nothing else -- the digest was never recomputed from
the fields it claims to cover. So editing ``requested_as_of`` on every record
while leaving the digest alone passed verification, and the valuation instant is
the input v2.1.8 exists to bind.

**Field evidence.** ``AdapterValidator.observe_field`` always opened
``records_for(endpoint)[0]``, so ``confirm_field`` compared a claim about the
second page of a sweep against the first page's bytes. With one record per
endpoint the two agree by accident; pagination, partitions and retained retries
are exactly where they would not, and all three are things this repository
intends to certify.

Both tests fail against v2.1.8.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.adapters.certification import verify_capture
from src.adapters.errors import ThetaDataProvenanceError
from src.adapters.thetadata.endpoints import Endpoint
from src.adapters.validation import AdapterValidator
from tests.certification_fixtures import (
    AS_OF,
    CAPTURED_AT,
    PAYLOADS,
    captured_chain,
)


def verified(taken, **overrides):
    from src.adapters.certification import _expected_identity

    payload = {
        "plan": taken.pipeline.capture_plan,
        "expected_pipeline_fingerprint": taken.pipeline.fingerprint(),
        "expected_identity": _expected_identity(
            taken.pipeline, manifest=taken.manifest
        ),
    }
    payload.update(overrides)
    return verify_capture(taken.manifest, taken.store, **payload)


# =============================================================================
# §10 -- the operation digest is recomputed from the fields it covers
# =============================================================================


def test_an_honest_capture_verifies_with_a_recomputed_digest():
    """The gate has to leave a path through it."""
    taken = captured_chain()
    result = verified(taken)
    assert result.verified, result.failures


def test_editing_the_requested_instant_fails_with_a_named_code():
    """The regression, exactly as the spec states it.

    Modify ``requested_as_of``, leave ``operation_fingerprint`` unchanged, and
    verification must fail with ``OPERATION_FINGERPRINT_MISMATCH``.
    """
    from datetime import timedelta

    taken = captured_chain()
    edited = dataclasses.replace(
        taken.manifest,
        records=tuple(
            dataclasses.replace(r, requested_as_of=r.requested_as_of + timedelta(1))
            for r in taken.manifest.records
        ),
    )
    result = verify_capture(
        edited,
        taken.store,
        plan=taken.pipeline.capture_plan,
        expected_pipeline_fingerprint=taken.pipeline.fingerprint(),
    )
    assert not result.verified
    assert any("OPERATION_FINGERPRINT_MISMATCH" in f for f in result.failures)


@pytest.mark.parametrize(
    "field",
    [
        "operation_id",
        "normalization_recipe_hash",
        "request_spec_fingerprint",
        "expected_universe_fingerprint",
        "open_interest_date_rule_fingerprint",
        "spot_synchronization_policy_fingerprint",
    ],
)
def test_editing_any_digest_covered_field_fails(field):
    """Every field the digest covers is stored, so every one is recomputable."""
    taken = captured_chain()
    edited = dataclasses.replace(
        taken.manifest,
        records=tuple(
            dataclasses.replace(r, **{field: "tampered"})
            for r in taken.manifest.records
        ),
    )
    result = verify_capture(
        edited,
        taken.store,
        plan=taken.pipeline.capture_plan,
        expected_pipeline_fingerprint=taken.pipeline.fingerprint(),
    )
    assert not result.verified
    assert any("OPERATION_FINGERPRINT_MISMATCH" in f for f in result.failures), (
        f"editing {field} did not move the recomputed digest, so the digest "
        "does not actually cover it"
    )


def test_the_spot_policy_fingerprint_is_stored_on_every_record():
    """It is covered by the digest, so it has to be recoverable from the record."""
    taken = captured_chain()
    stamps = {r.spot_synchronization_policy_fingerprint for r in taken.manifest.records}
    assert len(stamps) == 1
    assert stamps.pop() == taken.pipeline.spot_synchronization_policy_fingerprint


def test_a_record_naming_an_operation_with_no_instants_is_refused():
    taken = captured_chain()
    edited = dataclasses.replace(
        taken.manifest,
        records=tuple(
            dataclasses.replace(r, effective_valuation_timestamp=None)
            for r in taken.manifest.records
        ),
    )
    result = verify_capture(
        edited,
        taken.store,
        plan=taken.pipeline.capture_plan,
        expected_pipeline_fingerprint=taken.pipeline.fingerprint(),
    )
    assert not result.verified
    assert any("OPERATION_FIELDS_INCOMPLETE" in f for f in result.failures)


# =============================================================================
# §11 -- field evidence rereads the exact record it names
# =============================================================================


def two_quote_responses():
    """A capture holding two quote responses with *different* bytes."""
    from src.adapters.artifact_store import InMemoryArtifactStore
    from src.adapters.raw_store import RawCaptureManifest
    from tests.certification_fixtures import (
        documented_settlement_rule,
        durable_store,
        resolved_pipeline,
    )

    pipeline = resolved_pipeline()
    store = durable_store()
    session = pipeline.capture_session(
        store=store,
        session_id="two-pages",
        as_of=AS_OF,
        settlement_rule=documented_settlement_rule(),
        artifact_store=InMemoryArtifactStore(),
    )
    mark = session.mark()
    first = PAYLOADS[Endpoint.OPTION_QUOTE_SNAPSHOT]
    # Page two: the same shape, a different strike, so the two records disagree
    # about what they contain -- which is the only way to tell which one a
    # reread actually opened.
    header, _, rows = first.partition("\n")
    second = header + "\n" + rows.replace("4990", "4970", 1)
    for index, payload in enumerate((first, second), start=1):
        session.capture(
            endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT.value,
            query_params={"root": "SPXW", "page": str(index)},
            payload=payload,
            request_started_at=CAPTURED_AT,
            response_received_at=CAPTURED_AT,
            http_status=200,
        )
    manifest = RawCaptureManifest.from_session(
        session,
        since=mark,
        capture_plan_fingerprint=pipeline.capture_plan.fingerprint,
        pipeline_fingerprint=pipeline.fingerprint(),
    )
    return store, manifest


def test_observe_field_opens_the_record_it_is_given():
    store, manifest = two_quote_responses()
    ids = manifest.records_for(Endpoint.OPTION_QUOTE_SNAPSHOT.value)
    assert len(ids) == 2

    for record_id in ids:
        observation = AdapterValidator.observe_field(
            manifest=manifest,
            store=store,
            endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT,
            field_path="strike",
            record_id=record_id,
        )
        assert observation.record_id == record_id

    # And the two records really do disagree, so the check above is not passing
    # by reading the same bytes twice.
    strikes = {
        AdapterValidator.observe_field(
            manifest=manifest,
            store=store,
            endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT,
            field_path="strike",
            record_id=record_id,
        ).observed_value
        for record_id in ids
    }
    assert len(strikes) == 2, strikes


def test_observe_field_refuses_a_record_from_another_endpoint():
    """Reading one endpoint's bytes and reporting them under another's id."""
    taken = captured_chain()
    other = taken.manifest.records_for(Endpoint.INDEX_PRICE_SNAPSHOT.value)[0]
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)is not one of"):
        AdapterValidator.observe_field(
            manifest=taken.manifest,
            store=taken.store,
            endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT,
            field_path="strike",
            record_id=other,
        )


def test_confirm_field_checks_the_claim_against_the_record_it_names():
    """The regression. v2.1.8 confirmed page two against page one's bytes."""
    store, manifest = two_quote_responses()
    first, second = manifest.records_for(Endpoint.OPTION_QUOTE_SNAPSHOT.value)

    second_observation = AdapterValidator.observe_field(
        manifest=manifest,
        store=store,
        endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT,
        field_path="strike",
        record_id=second,
    )
    confirmed = AdapterValidator.confirm_field(
        manifest=manifest, store=store, observation=second_observation
    )
    assert confirmed.record_id == second

    # A claim about page two carrying page one's value must not confirm.
    first_observation = AdapterValidator.observe_field(
        manifest=manifest,
        store=store,
        endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT,
        field_path="strike",
        record_id=first,
    )
    transplanted = dataclasses.replace(
        first_observation,
        record_id=second,
        payload_hash=second_observation.payload_hash,
    )
    with pytest.raises(ThetaDataProvenanceError, match=r"(?i)is 4970|claimed value"):
        AdapterValidator.confirm_field(
            manifest=manifest, store=store, observation=transplanted
        )
