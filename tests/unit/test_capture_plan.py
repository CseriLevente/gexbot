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


def store_with(*endpoints: Endpoint) -> tuple[InMemoryRawStore, RawCaptureManifest]:
    from src.adapters.raw_store import build_record_id

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
            )
        )
    manifest = RawCaptureManifest(
        session_id="s",
        record_ids=tuple(sorted(r.record_id for r in records)),
        payload_hashes=tuple(sorted(r.payload_hash for r in records)),
        endpoint_records={r.endpoint: (r.record_id,) for r in records},
    )
    return store, manifest


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
    from src.adapters.certification import verify_capture

    store, manifest = store_with(Endpoint.OPTION_QUOTE_SNAPSHOT)
    verification = verify_capture(manifest, store, plan=plan_for())
    assert not verification.verified
    assert any("MISSING_ENDPOINT" in f for f in verification.failures)


def test_a_capture_naming_every_required_endpoint_verifies():
    from src.adapters.certification import verify_capture

    store, manifest = store_with(*STANDARD_VENDOR_IV)
    verification = verify_capture(manifest, store, plan=plan_for())
    assert verification.verified, verification.failures


def test_a_missing_index_snapshot_is_named_specifically():
    from src.adapters.certification import verify_capture

    store, manifest = store_with(*STANDARD_VENDOR_IV[:-1])
    verification = verify_capture(manifest, store, plan=plan_for())
    assert any(Endpoint.INDEX_PRICE_SNAPSHOT.value in f for f in verification.failures)


def test_an_endpoint_record_the_store_does_not_hold_is_refused():
    from src.adapters.certification import verify_capture

    store, manifest = store_with(*STANDARD_VENDOR_IV)
    forged = RawCaptureManifest(
        session_id=manifest.session_id,
        record_ids=manifest.record_ids,
        payload_hashes=manifest.payload_hashes,
        endpoint_records={
            **manifest.endpoint_records,
            Endpoint.OPTION_GREEKS_SECOND_ORDER.value: ("never-written",),
        },
    )
    verification = verify_capture(forged, store, plan=plan_for())
    assert not verification.verified


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
        def put(self, **kwargs):  # type: ignore[override]
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
