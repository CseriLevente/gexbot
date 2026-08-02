"""A trusted GEX cannot be computed under assumptions nobody has established.

**§1.** ``pipeline.compute_gex(chain)`` called the engine. That was all it did.
It ran with six load-bearing pricing dimensions ``UNKNOWN``, it ran on a chain
that had never been through this pipeline, and it ran on a chain with no capture
manifest -- and the number that came out was indistinguishable from one computed
under settled assumptions. The compatibility report existed and nothing consulted
it.

**§9.** ``fetch_chain(spot=...)`` took whatever number the caller passed, even
when ``underlying_price_source: vendor_index_snapshot`` says the underlying is
the vendor's to give. Every gamma is computed against that number.

**§15.** ``dataclasses.replace(pipeline, pricing_compatibility=<something
permissive>)`` produced a pipeline whose derived reports no longer followed from
its inputs, and nothing recomputed them.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.adapters.errors import ThetaDataCertificationError
from src.config.compatibility import PricingCompatibilityReport
from src.config.pipeline import CalculationMode, PipelineConsistencyError
from tests.certification_fixtures import (
    AS_OF,
    FIXTURE_INDEX_PRICE,
    captured_chain,
    context_for,
    resolved_pipeline,
    unresolved_pipeline,
)


def chain_for(pipeline):
    """A chain fetched through the pipeline, so it carries the provenance."""
    return pipeline.fetch_chain(as_of=AS_OF)


def evidence(pipeline):
    """A chain and the verified context that authorizes it, from one fetch.

    v2.1.6 requires the context, and it has to be derived from the same capture
    the chain came from -- so the tests below fail for the reason they name
    rather than because two unrelated captures were compared.
    """
    taken = captured_chain(pipeline)
    return taken.chain, context_for(taken)


# =============================================================================
# §1 -- the two calculations are different calculations
# =============================================================================


def test_the_pipeline_offers_both_calculations():
    from src.config.pipeline import ThetaDataResearchPipeline

    for name in ("compute_diagnostic_gex", "compute_trusted_gex"):
        assert callable(getattr(ThetaDataResearchPipeline, name)), name


def test_capture_and_compute_no_longer_exists_as_an_ambiguous_verb():
    """It computed *something*, and the name did not say which."""
    from src.config.pipeline import ThetaDataResearchPipeline

    assert not hasattr(ThetaDataResearchPipeline, "capture_and_compute")
    assert not hasattr(ThetaDataResearchPipeline, "compute_gex")


def test_an_incompatible_pipeline_cannot_run_a_trusted_calculation():
    """The regression."""
    pipeline = unresolved_pipeline()
    chain, context = evidence(pipeline)
    with pytest.raises(PipelineConsistencyError, match=r"(?i)unknown|unresolved"):
        pipeline.compute_trusted_gex(chain, context=context)


def test_an_incompatible_pipeline_can_still_run_a_diagnostic():
    pipeline = unresolved_pipeline()
    snapshot = pipeline.compute_diagnostic_gex(chain_for(pipeline))
    assert snapshot.meta["trusted"] is False
    assert (
        snapshot.meta["calculation_mode"] == CalculationMode.DIAGNOSTIC_UNTRUSTED.value
    )


def test_a_diagnostic_names_every_blocker_it_ran_despite():
    pipeline = unresolved_pipeline()
    snapshot = pipeline.compute_diagnostic_gex(chain_for(pipeline))
    assert snapshot.meta["calculation_blockers"]
    assert any(
        "load-bearing" in blocker for blocker in snapshot.meta["calculation_blockers"]
    )


def test_a_diagnostic_carries_both_fingerprints():
    pipeline = unresolved_pipeline()
    snapshot = pipeline.compute_diagnostic_gex(chain_for(pipeline))
    assert snapshot.meta["pipeline_fingerprint"] == pipeline.fingerprint()
    assert snapshot.meta["chain_fingerprint"]


def test_a_diagnostic_emits_a_deterministic_warning_code():
    pipeline = unresolved_pipeline()
    snapshot = pipeline.compute_diagnostic_gex(chain_for(pipeline))
    assert "DIAGNOSTIC_UNTRUSTED_CALCULATION" in snapshot.warnings


def test_a_diagnostic_result_cannot_be_passed_back_as_trusted_input():
    """Untrusted output is untrusted for good; nothing downstream re-blesses it."""
    pipeline = unresolved_pipeline()
    chain, context = evidence(pipeline)
    snapshot = pipeline.compute_diagnostic_gex(chain)
    with pytest.raises(PipelineConsistencyError, match=r"(?i)diagnostic"):
        pipeline.compute_trusted_gex(snapshot, context=context)  # type: ignore[arg-type]


def test_a_resolved_pipeline_can_run_a_trusted_calculation():
    """The gate must leave a path through it, or it is only a refusal."""
    pipeline = resolved_pipeline()
    chain, context = evidence(pipeline)
    snapshot = pipeline.compute_trusted_gex(chain, context=context)
    assert snapshot.meta["trusted"] is True
    assert snapshot.meta["calculation_mode"] == CalculationMode.TRUSTED.value


def test_a_chain_without_a_pipeline_fingerprint_cannot_be_trusted():
    from src.synthetic.chains import build_synthetic_chain

    pipeline = resolved_pipeline()
    _, context = evidence(pipeline)
    with pytest.raises(PipelineConsistencyError, match=r"(?i)fingerprint"):
        pipeline.compute_trusted_gex(build_synthetic_chain(), context=context)


def test_a_chain_from_another_pipeline_cannot_be_trusted():
    chain, _ = evidence(resolved_pipeline(rate_value=3.1))
    mine = resolved_pipeline()
    _, context = evidence(mine)
    with pytest.raises(PipelineConsistencyError, match=r"(?i)fingerprint"):
        mine.compute_trusted_gex(chain, context=context)


def test_a_chain_without_a_capture_manifest_cannot_be_trusted():
    from src.adapters.certification import build_verified_calculation_context
    from src.adapters.raw_store import InMemoryRawStore, RawCaptureManifest

    pipeline = resolved_pipeline(raw_capture_enabled=False, raw_capture_path=None)
    chain = chain_for(pipeline)
    context = build_verified_calculation_context(
        pipeline=pipeline,
        manifest=RawCaptureManifest.disabled(),
        store=InMemoryRawStore(),
    )
    with pytest.raises(PipelineConsistencyError, match=r"(?i)capture"):
        pipeline.compute_trusted_gex(chain, context=context)


def test_a_diagnostic_still_runs_without_a_capture():
    """Diagnostics are for looking at numbers, not for standing behind them."""
    pipeline = resolved_pipeline(raw_capture_enabled=False, raw_capture_path=None)
    snapshot = pipeline.compute_diagnostic_gex(chain_for(pipeline))
    assert snapshot.meta["trusted"] is False


def test_the_capture_profile_cannot_silently_compute_a_trusted_gex(tmp_path):
    """The shipped profile has no observations, so it must refuse.

    The profile's ``raw_capture_path`` is redirected at ``tmp_path``. It names
    ``artifacts/raw``, which is where a *real* session writes; a test capturing
    there puts fixture bytes into the production audit trail, and 573 of them
    reached a v2.1.5 release archive that way.
    """
    import dataclasses
    import pathlib

    from src.config.pipeline import ThetaDataResearchPipeline
    from src.config.schema import load_config
    from tests.certification_fixtures import vendor_transport

    loaded = load_config(pathlib.Path("config/thetadata_capture.yaml"))
    redirected = dataclasses.replace(
        loaded,
        thetadata=dataclasses.replace(
            loaded.thetadata, raw_capture_path=tmp_path / "raw"
        ),
    )
    pipeline = ThetaDataResearchPipeline.from_loaded_config(
        redirected, transport=vendor_transport()
    )
    chain, context = evidence(pipeline)
    with pytest.raises(PipelineConsistencyError):
        pipeline.compute_trusted_gex(chain, context=context)


# =============================================================================
# §9 -- the vendor's underlying is the vendor's to give
# =============================================================================


def test_the_canonical_fetch_takes_no_spot_at_all():
    """The regression, closed at the strongest point: the parameter is gone.

    Every gamma is computed against this number, and under
    ``vendor_index_snapshot`` it is the vendor's to give.
    """
    import inspect

    from src.config.pipeline import ThetaDataResearchPipeline

    parameters = set(
        inspect.signature(ThetaDataResearchPipeline.fetch_chain).parameters
    )
    assert "spot" not in parameters
    with pytest.raises(TypeError):
        resolved_pipeline().fetch_chain(as_of=AS_OF, spot=1234.5)


def test_a_non_vendor_underlying_cannot_use_the_canonical_fetch():
    """It has no index print to read, so it must say where its spot came from."""
    pipeline = resolved_pipeline(underlying_price_source="configured_constant")
    with pytest.raises(PipelineConsistencyError, match=r"(?i)external"):
        pipeline.fetch_chain(as_of=AS_OF)


def test_the_index_snapshot_is_fetched_in_the_same_capture_session():
    pipeline = resolved_pipeline()
    chain = chain_for(pipeline)
    manifest = chain.meta["raw_capture_manifest"]
    from src.adapters.thetadata.endpoints import Endpoint

    assert Endpoint.INDEX_PRICE_SNAPSHOT.value in manifest["endpoint_records"]
    assert manifest["session_id"]


def test_the_spot_and_its_clock_come_from_the_index_payload():
    pipeline = resolved_pipeline()
    chain = chain_for(pipeline)
    assert chain.spot == pytest.approx(FIXTURE_INDEX_PRICE)
    assert chain.meta["spot_provenance"]["source"] == "vendor_index_snapshot"
    assert chain.meta["spot_provenance"]["observation"] is not None


def test_the_index_record_is_in_the_snapshot_manifest():
    pipeline = resolved_pipeline()
    chain = chain_for(pipeline)
    manifest = chain.meta["raw_capture_manifest"]
    from src.adapters.thetadata.endpoints import Endpoint

    index_records = manifest["endpoint_records"][Endpoint.INDEX_PRICE_SNAPSHOT.value]
    assert index_records
    assert set(index_records) <= set(manifest["record_ids"])


def test_an_externally_supplied_spot_has_its_own_named_path():
    """Kept, because a research run on a historical spot is a real use.

    It is a different method with a different name, so nobody reaches it by
    passing an extra keyword to the canonical one.
    """
    pipeline = resolved_pipeline(underlying_price_source="configured_constant")
    chain = pipeline.fetch_chain_with_external_spot(as_of=AS_OF, spot=4999.5)
    assert chain.spot == pytest.approx(4999.5)
    assert chain.meta["spot_provenance"]["source"] == "caller_supplied"


def test_an_externally_supplied_spot_cannot_be_trusted():
    from src.adapters.certification import build_verified_calculation_context
    from src.adapters.raw_store import (
        CaptureSession,
        InMemoryRawStore,
        RawCaptureManifest,
    )
    from tests.certification_fixtures import verified_oi, verified_spot

    pipeline = resolved_pipeline(underlying_price_source="configured_constant")
    store = InMemoryRawStore()
    session = CaptureSession(store=store, session_id="external")
    chain = pipeline.fetch_chain_with_external_spot(
        as_of=AS_OF, spot=4999.5, capture=session
    )
    manifest = RawCaptureManifest.from_session(
        session,
        capture_plan_fingerprint=pipeline.capture_plan.fingerprint,
        pipeline_fingerprint=pipeline.fingerprint(),
    )
    context = build_verified_calculation_context(
        pipeline=pipeline,
        manifest=manifest,
        store=store,
        spot=verified_spot(store, manifest),
        open_interest=verified_oi(store, manifest),
    )
    with pytest.raises(PipelineConsistencyError, match=r"(?i)spot|caller"):
        pipeline.compute_trusted_gex(chain, context=context)


# =============================================================================
# §15 -- derived reports are recomputed, not trusted
# =============================================================================


def test_an_intact_pipeline_validates():
    resolved_pipeline().validate_integrity()


def test_a_replaced_compatibility_report_fails_integrity():
    """The regression."""
    tampered = dataclasses.replace(
        resolved_pipeline(), pricing_compatibility=PricingCompatibilityReport()
    )
    with pytest.raises(PipelineConsistencyError, match=r"(?i)compatib"):
        tampered.validate_integrity()


def test_a_replaced_capability_report_fails_integrity():
    tampered = dataclasses.replace(resolved_pipeline(), subscription_capability=None)
    with pytest.raises(PipelineConsistencyError, match=r"(?i)capab"):
        tampered.validate_integrity()


def test_a_replaced_engine_config_fails_integrity():
    from src.gex.config import GexEngineConfig

    tampered = dataclasses.replace(
        resolved_pipeline(), engine_config=GexEngineConfig(spot_move_pct=0.05)
    )
    with pytest.raises(PipelineConsistencyError, match=r"(?i)engine|model"):
        tampered.validate_integrity()


def test_a_tampered_pipeline_cannot_fetch():
    tampered = dataclasses.replace(
        resolved_pipeline(), pricing_compatibility=PricingCompatibilityReport()
    )
    with pytest.raises(PipelineConsistencyError):
        tampered.fetch_chain(as_of=AS_OF)


def test_a_tampered_pipeline_cannot_run_a_diagnostic_either():
    """Diagnostics tolerate unknowns, not incoherence."""
    pipeline = resolved_pipeline()
    chain = chain_for(pipeline)
    tampered = dataclasses.replace(
        pipeline, pricing_compatibility=PricingCompatibilityReport()
    )
    with pytest.raises(PipelineConsistencyError):
        tampered.compute_diagnostic_gex(chain)


def test_a_tampered_pipeline_cannot_be_assessed_for_readiness():
    from src.adapters.certification import assess_readiness
    from src.adapters.raw_store import InMemoryRawStore

    tampered = dataclasses.replace(
        resolved_pipeline(), pricing_compatibility=PricingCompatibilityReport()
    )
    with pytest.raises((PipelineConsistencyError, ThetaDataCertificationError)):
        assess_readiness(pipeline=tampered, as_of=AS_OF, raw_store=InMemoryRawStore())
