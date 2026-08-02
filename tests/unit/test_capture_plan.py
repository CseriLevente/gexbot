"""A capture is complete when it holds every response the session needs.

**§8.** ``verify_capture`` asked whether the records a manifest *claims* are in
the store. It never asked whether the manifest claims enough. A one-record
manifest -- a single quote snapshot, no open interest, no IV, no index price --
verified, and the certification ladder advanced on it. Open interest is the
weight on every GEX term; a capture without it cannot produce a GEX at all, let
alone a certified one.

**§10.** ``raw_store`` was anything with the right attribute names.
``raw_store=object()`` produced no ``verify_integrity``, so the integrity check
was skipped and readiness passed. A store that cannot be written to is not a
place to put a paid session's only copy of the evidence.

**§14.** ``RawCaptureManifest.from_session`` took every record the session had
ever captured. Two chain pulls in one session gave the second snapshot a
manifest naming the first snapshot's responses -- a provenance record listing
bytes that produced a different number.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

from src.adapters.raw_store import (
    CaptureSession,
    FileRawStore,
    InMemoryRawStore,
    RawCaptureManifest,
)
from src.adapters.thetadata.endpoints import Endpoint

NOW = datetime(2026, 3, 17, 15, 0, tzinfo=UTC)


#: What ``store_with`` claims produced its capture. Any non-empty value will
#: do; v2.1.6 refuses an *empty* fingerprint, because "no claim" is not the same
#: as "nothing to check".
PIPELINE = "test-pipeline-fingerprint"


#: The stamp this fixture's records carry. It builds a store by hand rather
#: than through a pipeline, so these are stand-in values -- but they have to be
#: *present*, because since v2.1.7 verification refuses an unstamped record
#: rather than inventing a claim for it.
TEST_REQUEST_SPEC = "test-request-spec"
TEST_RECIPE = "test-normalization-recipe"
TEST_OPERATION_ID = "op-test-0001"
TEST_OPERATION_FINGERPRINT = "f" * 64
TEST_RECIPE_HASH = "e" * 64


def stamped_identity():
    from src.adapters.raw_store import CaptureIdentity

    return CaptureIdentity(
        session_id="s",
        pipeline_fingerprint=PIPELINE,
        capture_plan_fingerprint=plan_for().fingerprint,
        request_spec_fingerprint=TEST_REQUEST_SPEC,
        normalization_recipe_fingerprint=TEST_RECIPE,
        # Since v2.1.8 a record also says which *operation* issued it and what
        # instant that operation priced against. Stand-in values again -- this
        # fixture builds a store by hand -- but present, because an unstamped
        # record is refused rather than given a claim this code invented.
        operation_id=TEST_OPERATION_ID,
        operation_fingerprint=TEST_OPERATION_FINGERPRINT,
        normalization_recipe_hash=TEST_RECIPE_HASH,
        requested_as_of=NOW,
        effective_valuation_timestamp=NOW,
        valuation_timestamp_rule="INDEX_PRINT_TIMESTAMP",
    )


def store_with(*endpoints: Endpoint) -> tuple[InMemoryRawStore, RawCaptureManifest]:
    from src.adapters.raw_store import ManifestRecord, build_record_id

    identity = stamped_identity()
    store = InMemoryRawStore()
    records = []
    for sequence, endpoint in enumerate(endpoints, start=1):
        payload = f"col\n{sequence}\n"
        records.append(
            store.put(
                record_id=build_record_id(
                    session_id="s",
                    sequence=sequence,
                    endpoint=endpoint.value,
                    query_params={"root": "SPXW"},
                    payload=payload,
                ),
                endpoint=endpoint.value,
                query_params={"root": "SPXW"},
                payload=payload,
                request_started_at=NOW,
                response_received_at=NOW,
                http_status=200,
                request_sequence=sequence,
                identity=identity,
            )
        )
    manifest = RawCaptureManifest(
        session_id="s",
        # Derived from the records, per record. The endpoint map is a property
        # of the descriptors now, so it cannot disagree with them.
        records=tuple(ManifestRecord.of(record) for record in records),
        capture_plan_fingerprint=plan_for().fingerprint,
        pipeline_fingerprint=PIPELINE,
    )
    return store, manifest


def verified(manifest, store, **overrides):
    """Verify against a named plan and a named pipeline. Both are required."""
    from src.adapters.certification import verify_capture

    payload = {
        "plan": plan_for(),
        "expected_pipeline_fingerprint": PIPELINE,
        "expected_identity": stamped_identity(),
    }
    payload.update(overrides)
    return verify_capture(manifest, store, **payload)


# =============================================================================
# §8 -- the capture plan
# =============================================================================

STANDARD_VENDOR_IV = (
    Endpoint.OPTION_QUOTE_SNAPSHOT,
    Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT,
    Endpoint.OPTION_GREEKS_FIRST_ORDER,
    Endpoint.INDEX_PRICE_SNAPSHOT,
)


def plan_for(**overrides):
    from src.adapters.thetadata.capture_plan import capture_plan_for
    from src.adapters.thetadata.endpoints import Tier
    from src.config.pipeline import IvGammaPricingMode, VendorGammaPolicy

    settings = {
        "pricing_mode": IvGammaPricingMode.VENDOR_IV_LOCAL_GAMMA,
        "vendor_gamma_policy": VendorGammaPolicy.DISABLED,
        "underlying_price_source": "vendor_index_snapshot",
        "tier": Tier.STANDARD,
    }
    settings.update(overrides)
    return capture_plan_for(**settings)


def test_a_standard_vendor_iv_session_needs_four_endpoints():
    assert set(plan_for().required_endpoints) == set(STANDARD_VENDOR_IV)


def test_compare_only_additionally_needs_second_order_gamma():
    from src.adapters.thetadata.endpoints import Tier
    from src.config.pipeline import VendorGammaPolicy

    plan = plan_for(vendor_gamma_policy=VendorGammaPolicy.COMPARE_ONLY, tier=Tier.PRO)
    assert Endpoint.OPTION_GREEKS_SECOND_ORDER in plan.required_endpoints
    assert set(STANDARD_VENDOR_IV) < set(plan.required_endpoints)


def test_a_non_vendor_underlying_does_not_require_the_index_endpoint():
    plan = plan_for(underlying_price_source="synthetic")
    assert Endpoint.INDEX_PRICE_SNAPSHOT not in plan.required_endpoints


def test_the_plan_has_a_fingerprint_that_moves_with_its_contents():
    from src.adapters.thetadata.endpoints import Tier
    from src.config.pipeline import VendorGammaPolicy

    other = plan_for(vendor_gamma_policy=VendorGammaPolicy.COMPARE_ONLY, tier=Tier.PRO)
    assert plan_for().fingerprint != other.fingerprint


def test_one_record_is_not_a_complete_capture():
    """The regression. A single quote snapshot verified in v2.1.4."""

    store, manifest = store_with(Endpoint.OPTION_QUOTE_SNAPSHOT)
    verification = verified(manifest, store)
    assert not verification.verified
    assert any("MISSING_ENDPOINT" in f for f in verification.failures)


def test_a_capture_naming_every_required_endpoint_verifies():
    store, manifest = store_with(*STANDARD_VENDOR_IV)
    verification = verified(manifest, store)
    assert verification.verified, verification.failures


def test_a_missing_index_snapshot_is_named_specifically():
    store, manifest = store_with(*STANDARD_VENDOR_IV[:-1])
    verification = verified(manifest, store)
    assert any(Endpoint.INDEX_PRICE_SNAPSHOT.value in f for f in verification.failures)


def test_an_endpoint_record_the_store_does_not_hold_is_refused():
    import dataclasses

    from src.adapters.raw_store import ManifestRecord

    store, manifest = store_with(*STANDARD_VENDOR_IV)
    forged = dataclasses.replace(
        manifest,
        records=(
            *manifest.records,
            ManifestRecord(
                record_id="never-written",
                endpoint=Endpoint.OPTION_GREEKS_SECOND_ORDER.value,
                payload_hash="0" * 64,
                parameter_hash="0" * 16,
            ),
        ),
    )
    assert not verified(forged, store).verified


def test_the_manifest_hash_is_a_full_sha256():
    _, manifest = store_with(*STANDARD_VENDOR_IV)
    assert len(manifest.manifest_hash) == 64


def test_the_manifest_records_the_plan_parser_and_pipeline():
    _, manifest = store_with(*STANDARD_VENDOR_IV)
    payload = manifest.as_dict()
    for key in (
        "capture_plan_fingerprint",
        "endpoint_records",
        "parser_version",
        "pipeline_fingerprint",
        "request_parameter_hashes",
    ):
        assert key in payload, key


# =============================================================================
# §10 -- a real, healthy, writable store
# =============================================================================


def test_a_bare_object_is_not_a_raw_store():
    from src.adapters.raw_store import RawStoreHealth, probe_raw_store

    health = probe_raw_store(object())
    assert isinstance(health, RawStoreHealth)
    assert not health.usable
    assert any("PROTOCOL" in f for f in health.failures)


def test_a_healthy_store_probes_clean(tmp_path: pathlib.Path):
    from src.adapters.raw_store import probe_raw_store

    health = probe_raw_store(FileRawStore(tmp_path / "raw"))
    assert health.usable, health.failures


def test_the_probe_does_not_contaminate_the_capture_namespace(
    tmp_path: pathlib.Path,
):
    """A health check that leaves evidence behind is a corrupted audit trail."""
    from src.adapters.raw_store import probe_raw_store

    store = FileRawStore(tmp_path / "raw")
    probe_raw_store(store)
    assert store.records() == ()
    assert store.verify_integrity().ok


def test_an_unwritable_store_is_not_usable(tmp_path: pathlib.Path):
    from src.adapters.raw_store import probe_raw_store

    class Refuses(FileRawStore):
        """A store whose filesystem refuses every write.

        Overrides the write primitive rather than ``put``: since v2.1.6 the
        probe writes a scratch file directly, so that it can check the capture
        destination without adding a record to the index it is checking.
        """

        def _atomic_write(self, *args, **kwargs):  # type: ignore[override]
            raise OSError("read-only filesystem")

    health = probe_raw_store(Refuses(tmp_path / "raw"))
    assert not health.usable
    assert any("WRITE" in f for f in health.failures)


# =============================================================================
# §14 -- one snapshot, one manifest
# =============================================================================


def test_a_reused_session_produces_snapshot_specific_manifests():
    """The regression: the second manifest named the first snapshot's records."""
    store = InMemoryRawStore()
    session = CaptureSession(store=store, session_id="s")

    first_mark = session.mark()
    session.capture(
        endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT.value,
        query_params={"root": "SPXW"},
        payload="a\n1\n",
        request_started_at=NOW,
        response_received_at=NOW,
        http_status=200,
    )
    first = RawCaptureManifest.from_session(session, since=first_mark)

    second_mark = session.mark()
    session.capture(
        endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT.value,
        query_params={"root": "SPX"},
        payload="a\n2\n",
        request_started_at=NOW,
        response_received_at=NOW,
        http_status=200,
    )
    second = RawCaptureManifest.from_session(session, since=second_mark)

    assert len(first.record_ids) == 1
    assert len(second.record_ids) == 1
    assert set(first.record_ids).isdisjoint(second.record_ids)
    assert first.manifest_hash != second.manifest_hash


def test_a_manifest_without_a_mark_still_covers_the_whole_session():
    """The old behaviour stays available, and is now something you ask for."""
    store = InMemoryRawStore()
    session = CaptureSession(store=store, session_id="s")
    for index in range(2):
        session.capture(
            endpoint=Endpoint.OPTION_QUOTE_SNAPSHOT.value,
            query_params={"n": index},
            payload=f"a\n{index}\n",
            request_started_at=NOW,
            response_received_at=NOW,
            http_status=200,
        )
    assert len(RawCaptureManifest.from_session(session).record_ids) == 2
