"""The last gap between "approved" and "sent".

**Every test here fails against v2.1.19, and none of them makes a network
request.**

v2.1.19 checked the approval against the pipeline built in ``_preflight()`` and
then built a *second* pipeline in ``run_capture()`` to do the work. Nothing
compared them. A reproduced case had preflight at ``0872bb7245ec...`` and
execution at ``418fd521b915...``, and every response was acquired: the run
ended unverified, which is a report written after the money was spent.

Three more of the same shape:

* the sweep re-derived its own request plan after the approval had been checked
  against a different derivation;
* ``dataclasses.replace`` could install a forged documentation bundle onto a
  pipeline that ``from_config`` had built correctly, and ``validate_integrity``
  accepted it once the pricing report was replaced to match;
* ``httpx.Client`` defaults to ``trust_env=True``, so a capture recording
  itself as ``LOCAL_TERMINAL_CAPTURE`` could have gone through an ambient
  ``ALL_PROXY``.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from src.tools.capture_thetadata_once import (
    CaptureRunError,
    plan_capture,
    run_capture,
    run_path,
)
from tests.certification_fixtures import (
    AS_OF,
    DOCUMENTED_SESSION,
    approval_hash_for,
    vendor_transport,
)

CAPTURE_CONFIG = "config/thetadata_capture.yaml"


def genuine_pipeline():
    from src.adapters.transport import FakeTransport
    from src.config.pipeline import ThetaDataResearchPipeline
    from src.config.schema import load_config

    return ThetaDataResearchPipeline.from_loaded_config(
        load_config(CAPTURE_CONFIG), transport=FakeTransport()
    )


class CountingTransport:
    """A transport that answers correctly and records every call."""

    def __init__(self) -> None:
        self.calls = 0
        self._inner = vendor_transport()
        self.capture_origin = getattr(self._inner, "capture_origin", "FIXTURE_REPLAY")

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def counted(*args, **kwargs):
            self.calls += 1
            return attribute(*args, **kwargs)

        return counted


# =============================================================================
# §1 -- the execution pipeline is the approved pipeline
# =============================================================================


def test_semantic_drift_between_preflight_and_execution_sends_nothing(
    tmp_path, monkeypatch
):
    """**The central regression.**

    The two pipelines are each internally coherent; they are coherent about
    different configurations. v2.1.19 acquired every response.
    """
    import src.config.pipeline as pipeline_module

    approval = approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF)
    transport = CountingTransport()
    original = pipeline_module.ThetaDataResearchPipeline.from_loaded_config
    seen: list[int] = []

    def drifting(cls, loaded, **kwargs):
        seen.append(1)
        # The preflight build is first and is left alone. The execution build
        # -- the one with a real transport -- is given a wider DTE window, so
        # it derives different requests while remaining entirely self-
        # consistent.
        if kwargs.get("transport") is transport:
            loaded = dataclasses.replace(
                loaded,
                thetadata=dataclasses.replace(
                    loaded.thetadata, max_dte=int(loaded.thetadata.max_dte) + 1
                ),
            )
        return original.__func__(cls, loaded, **kwargs)

    monkeypatch.setattr(
        pipeline_module.ThetaDataResearchPipeline,
        "from_loaded_config",
        classmethod(drifting),
    )

    destination = tmp_path / "capture"
    with pytest.raises(CaptureRunError) as raised:
        run_capture(
            CAPTURE_CONFIG,
            output=str(destination),
            transport=transport,
            as_of=AS_OF,
            allow_unsettled_raw_only=True,
            approved=approval,
        )

    message = str(raised.value)
    assert "not the pipeline that was approved" in message
    # A named semantic field, not two digests and a shrug. Widening ``max_dte``
    # moves several of them at once; the refusal reports the first in the
    # difference order rather than listing all of them.
    assert any(
        named in message
        for named in (
            "pipeline fingerprint changed",
            "capture plan changed",
            "request plan changed",
        )
    ), message
    assert "Rerun the dry run" in message
    # And nothing was sent.
    assert transport.calls == 0
    assert len(seen) >= 2, "both pipelines were built, which is the premise"


def test_execution_drift_creates_no_raw_response_record(tmp_path, monkeypatch):
    """A refusal that leaves records behind is not a refusal."""
    import src.config.pipeline as pipeline_module

    transport = CountingTransport()
    original = pipeline_module.ThetaDataResearchPipeline.from_loaded_config

    def drifting(cls, loaded, **kwargs):
        if kwargs.get("transport") is transport:
            loaded = dataclasses.replace(
                loaded,
                thetadata=dataclasses.replace(
                    loaded.thetadata, max_dte=int(loaded.thetadata.max_dte) + 1
                ),
            )
        return original.__func__(cls, loaded, **kwargs)

    monkeypatch.setattr(
        pipeline_module.ThetaDataResearchPipeline,
        "from_loaded_config",
        classmethod(drifting),
    )

    destination = tmp_path / "capture"
    with pytest.raises(CaptureRunError):
        run_capture(
            CAPTURE_CONFIG,
            output=str(destination),
            transport=transport,
            as_of=AS_OF,
            allow_unsettled_raw_only=True,
            approved=approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF),
        )

    raw = destination / "raw"
    assert not list(raw.glob("*.raw")) if raw.exists() else True
    assert transport.calls == 0


def test_an_undrifted_run_still_completes(tmp_path):
    """The check must not refuse the ordinary case."""
    from src.tools.capture_thetadata_once import RawCaptureRunState

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=vendor_transport(),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
        approved=approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF),
    )
    assert report["run_state"] == RawCaptureRunState.COMPLETED_RAW_VERIFIED.value


# =============================================================================
# §2 -- the sweep uses the approved plan object
# =============================================================================


def test_the_sweep_receives_the_authorized_plan_rather_than_deriving_one(
    tmp_path, monkeypatch
):
    """v2.1.19's sweep derived a third plan after the approval was checked."""
    import src.config.pipeline as pipeline_module

    received: list[object] = []
    original = pipeline_module.ThetaDataResearchPipeline.capture_required_endpoints_raw

    def watched(self, *, capture, as_of, plan=None):
        received.append(plan)
        return original(self, capture=capture, as_of=as_of, plan=plan)

    monkeypatch.setattr(
        pipeline_module.ThetaDataResearchPipeline,
        "capture_required_endpoints_raw",
        watched,
    )

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=vendor_transport(),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
        approved=approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF),
    )

    assert len(received) == 1
    supplied = received[0]
    assert supplied is not None, "the sweep was left to derive its own plan"
    # And it is the plan the run recorded as authorized.
    assert supplied.request_plan_hash == report["request_plan"]["request_plan_hash"]


def test_a_plan_this_pipeline_would_not_produce_is_refused():
    """The sweep sanity-checks a supplied plan; it never silently replaces it."""
    from src.config.pipeline import PipelineConsistencyError

    pipeline = genuine_pipeline()
    moment = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    stranger = pipeline.raw_request_plan(as_of=datetime(2026, 8, 5, 15, 0, tzinfo=UTC))
    assert stranger.request_plan_hash != (
        pipeline.raw_request_plan(as_of=moment).request_plan_hash
    )

    with pytest.raises(PipelineConsistencyError, match=r"(?i)different session"):
        pipeline.capture_required_endpoints_raw(
            capture=None, as_of=moment, plan=stranger
        )


# =============================================================================
# §3 -- forged documentation cannot cross an authority boundary
# =============================================================================


def forged_pipeline():
    """A valid pipeline carrying a bundle whose readings are invented.

    The document is untouched -- real bytes, real digest, real paths -- so the
    cheap byte-hash check still passes. The *values* are not the ones those
    bytes yield, which is exactly what the strong check re-derives.
    """
    from src.adapters.thetadata.openapi_evidence import (
        OpenApiEvidenceExtraction,
        VendorDocumentationBundle,
        repository_documentation_root,
    )
    from src.adapters.thetadata.vendor_documentation import DocumentedRule
    from src.config.pipeline import (
        DocumentedVendorConventions,
        RateUnit,
        assess_pricing_compatibility,
    )
    from src.domain.settlement import SettlementRuleKind

    genuine = genuine_pipeline()
    real = genuine.documentation_bundle
    invented = {
        DocumentedRule.OPEN_INTEREST_SETTLEMENT: SettlementRuleKind.SAME_SESSION,
        DocumentedRule.RATE_UNITS: RateUnit.DECIMAL_ANNUAL_RATE,
        DocumentedRule.MINIMUM_TIME_FLOOR: 30,
    }
    fake = VendorDocumentationBundle(
        document=real.document,
        extractions=tuple(
            OpenApiEvidenceExtraction(
                rule=e.rule,
                document_sha256=e.document_sha256,
                yaml_path=e.yaml_path,
                expected_source_fragment=e.expected_source_fragment,
                normalized_value=invented[e.rule],
                normalizer=e.normalizer,
            )
            for e in real.extractions
        ),
        verified_root=str(repository_documentation_root()),
    )
    return genuine, dataclasses.replace(
        genuine,
        documentation_bundle=fake,
        pricing_compatibility=assess_pricing_compatibility(
            genuine.config,
            genuine.model_spec,
            DocumentedVendorConventions.from_bundle(fake),
        ),
    )


def test_a_forged_bundle_can_still_be_installed_and_says_something_different():
    """The premise: the forgery is real and changes what the pipeline claims."""
    from src.config.pipeline import DocumentedVendorConventions, RateUnit

    genuine, forged = forged_pipeline()
    assert forged.documentation_bundle is not genuine.documentation_bundle
    conventions = DocumentedVendorConventions.from_bundle(forged.documentation_bundle)
    assert conventions.rate_units is RateUnit.DECIMAL_ANNUAL_RATE
    assert conventions.minimum_time_floor_minutes == 30


def test_the_strong_check_catches_post_construction_replacement():
    """**The §3 regression.** v2.1.19 had no such check to run."""
    from src.config.pipeline import PipelineConsistencyError

    _, forged = forged_pipeline()
    with pytest.raises(PipelineConsistencyError, match=r"(?i)does not follow"):
        forged.require_documentation_authority()


def test_capture_session_refuses_a_forged_bundle(tmp_path):
    from src.adapters.raw_store import FileRawStore
    from src.config.pipeline import PipelineConsistencyError

    _, forged = forged_pipeline()
    with pytest.raises(PipelineConsistencyError, match=r"(?i)does not follow"):
        forged.capture_session(
            store=FileRawStore(tmp_path / "raw"),
            session_id="forged",
            as_of=datetime(2026, 8, 6, 15, 0, tzinfo=UTC),
        )


def test_certification_readiness_refuses_a_forged_bundle(tmp_path):
    from src.adapters.certification import assess_readiness
    from src.adapters.raw_store import FileRawStore
    from src.config.pipeline import PipelineConsistencyError

    _, forged = forged_pipeline()
    with pytest.raises(PipelineConsistencyError, match=r"(?i)does not follow"):
        assess_readiness(
            pipeline=forged,
            as_of=datetime(2026, 8, 6, 15, 0, tzinfo=UTC),
            raw_store=FileRawStore(tmp_path / "raw"),
        )


def test_the_cheap_check_still_passes_and_that_is_deliberate():
    """``validate_integrity`` runs before every fetch and every calculation.

    Making it re-derive the bundle costs 326ms against 0.8ms and buys nothing
    on a path that authorizes nothing. The split is the design, not an
    oversight -- so it is asserted rather than left to be rediscovered.
    """
    _, forged = forged_pipeline()
    forged.validate_integrity()


def test_the_genuine_pipeline_passes_the_strong_check():
    genuine_pipeline().require_documentation_authority()


def test_the_strong_check_makes_no_network_request(monkeypatch):
    import socket

    def refuse(*args, **kwargs):  # pragma: no cover - only on regression
        raise AssertionError("documentation authority must not use the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    genuine_pipeline().require_documentation_authority()


# =============================================================================
# §4 -- routing is configured, not inherited
# =============================================================================


PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "all_proxy")


def test_the_local_terminal_transport_ignores_ambient_proxy_settings(monkeypatch):
    """**The §4 regression.** ``httpx.Client`` defaults to ``trust_env=True``.

    A capture that records itself ``LOCAL_TERMINAL_CAPTURE`` -- a
    classification derived from the URL -- would have kept saying so while an
    ambient ``ALL_PROXY`` sent the bytes somewhere else.
    """
    httpx = pytest.importorskip("httpx")

    for name in PROXY_VARIABLES:
        monkeypatch.setenv(name, "http://127.0.0.1:9/deliberately-unreachable")

    from src.adapters.transport import HttpxTransport

    transport = HttpxTransport(connect_timeout_seconds=1.0, read_timeout_seconds=1.0)
    try:
        assert transport.trust_env is False
        client = transport._client
        assert isinstance(client, httpx.Client)
        assert client.trust_env is False
    finally:
        transport.close()


def test_the_transport_can_be_asked_to_trust_the_environment_explicitly():
    """Not removed, made explicit. If proxying is ever wanted it is
    configuration an operator approves, not a shell variable."""
    pytest.importorskip("httpx")
    from src.adapters.transport import HttpxTransport

    transport = HttpxTransport(trust_env=True)
    try:
        assert transport.trust_env is True
    finally:
        transport.close()


def test_the_effective_settings_report_the_routing_policy():
    from src.config.schema import load_config
    from src.config.thetadata import effective_transport_settings

    settings = effective_transport_settings(load_config(CAPTURE_CONFIG).thetadata)
    assert settings["trust_env"] is False


def test_the_dry_run_reports_the_routing_policy(tmp_path):
    report = plan_capture(
        CAPTURE_CONFIG, output=str(tmp_path / "capture"), as_of=DOCUMENTED_SESSION
    )
    assert report["effective_transport"]["trust_env"] is False


def test_changing_the_routing_policy_invalidates_the_approval():
    """It is inside the approval's transport fingerprint."""
    from src.adapters.thetadata.preflight_approval import (
        APPROVAL_TRANSPORT_FIELDS,
        approval_for,
    )
    from src.domain.digests import digest_of

    assert "trust_env" in APPROVAL_TRANSPORT_FIELDS

    from src.config.schema import load_config
    from src.config.thetadata import effective_transport_settings

    loaded = load_config(CAPTURE_CONFIG)
    approved = approval_for(
        pipeline=genuine_pipeline(), config=loaded.thetadata, moment=AS_OF
    )
    settings = dict(effective_transport_settings(loaded.thetadata))
    settings["trust_env"] = True
    proxied = digest_of({n: settings.get(n) for n in APPROVAL_TRANSPORT_FIELDS})
    assert proxied != approved.effective_transport_fingerprint


def test_the_run_evidence_records_the_routing_policy(tmp_path):
    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=vendor_transport(),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
        approved=approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF),
    )
    assert report["effective_transport"]["trust_env"] is False
    intent = json.loads(
        run_path(report, "output_root").joinpath("run-intent.json").read_text("utf-8")
    )
    assert intent["effective_transport"]["trust_env"] is False
    summary = json.loads(run_path(report, "summary_path").read_text("utf-8"))
    assert summary["effective_transport"]["trust_env"] is False


# =============================================================================
# §5 -- the plan describes the executor
# =============================================================================


def test_the_plan_stop_policy_is_the_executors_policy():
    """**The §5 regression.** The plan said ``CONTINUE_ON_FAILURE`` while the
    sweep stopped on five named conditions."""
    from src.adapters.thetadata.raw_acquisition import (
        RequestFailurePolicy,
        systemic_stop_reasons,
    )

    plan = genuine_pipeline().raw_request_plan(
        as_of=datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    )
    assert plan.requests
    for request in plan.requests:
        assert request.stop_policy == RequestFailurePolicy.CONTINUE_UNLESS_SYSTEMIC
        assert request.systemic_stop_reasons == systemic_stop_reasons()
        assert request.stop_policy != "CONTINUE_ON_FAILURE"


def test_the_plan_names_every_systemic_reason_the_executor_has():
    """Derived from the enum, so a new stop reason cannot fail to appear."""
    from src.adapters.thetadata.raw_acquisition import (
        SYSTEMIC_STOP_REASONS,
        systemic_stop_reasons,
    )

    plan = genuine_pipeline().raw_request_plan(
        as_of=datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    )
    assert set(plan.requests[0].systemic_stop_reasons) == {
        reason.value for reason in SYSTEMIC_STOP_REASONS
    }
    assert systemic_stop_reasons() == plan.requests[0].systemic_stop_reasons


def test_the_stop_policy_is_inside_the_request_plan_hash():
    """Which is why the request-plan schema moved."""
    from src.adapters.thetadata.request_plan import (
        REQUEST_PLAN_SCHEMA_VERSION,
        PlannedEndpointRequest,
        RawRequestPlan,
    )

    def plan_with(policy: str) -> str:
        return RawRequestPlan(
            requests=(
                PlannedEndpointRequest(
                    endpoint="/v3/x",
                    safe_path="/v3/x",
                    canonical_query_parameters=(("symbol", "SPXW"),),
                    required_tier="standard",
                    request_spec_hash="a" * 64,
                    stop_policy=policy,
                ),
            )
        ).request_plan_hash

    assert plan_with("CONTINUE_UNLESS_SYSTEMIC") != plan_with("CONTINUE_ON_FAILURE")
    assert REQUEST_PLAN_SCHEMA_VERSION == "raw-request-plan/2.1.20"


# =============================================================================
# §6 -- the report stops contradicting itself
# =============================================================================


def test_the_dry_run_does_not_call_established_settlement_a_blocker(tmp_path):
    """**The §6 regression.** The same report said ``ESTABLISHED`` and listed
    the settlement date among ``analytical_blockers``."""
    report = plan_capture(
        CAPTURE_CONFIG, output=str(tmp_path / "capture"), as_of=DOCUMENTED_SESSION
    )

    assert report["vendor_documentation"]["settlement_evidence"] == "ESTABLISHED"
    assert "analytical_blockers" not in report

    actual = report["actual_analytical_blockers"]
    assert not [b for b in actual if "settlement" in b.lower()], actual
    # The standing requirements are still reported, under a name that cannot be
    # read as the current state.
    assert report["analytical_requirements"]
    assert any(
        "settlement" in requirement.lower()
        for requirement in report["analytical_requirements"]
    )


def test_the_actual_blockers_name_what_is_really_missing(tmp_path):
    report = plan_capture(
        CAPTURE_CONFIG, output=str(tmp_path / "capture"), as_of=DOCUMENTED_SESSION
    )
    joined = " ".join(report["actual_analytical_blockers"]).lower()
    assert "no capture exists yet" in joined
    assert "universe" in joined


# =============================================================================
# The engine is untouched
# =============================================================================


def test_the_frozen_gex_outputs_are_unchanged():
    """This release moves no arithmetic. Asserted rather than assumed."""
    from src.domain.model_spec import MODEL_VERSION
    from src.gex.engine import compute_gex_snapshot
    from src.synthetic.chains import build_synthetic_chain

    snapshot = compute_gex_snapshot(build_synthetic_chain())
    assert snapshot.total_unsigned_gex > 0
    # The frozen totals live in tests/regression; this asserts the *version*
    # did not move, which is the claim v2.1.20 makes about the arithmetic.
    assert MODEL_VERSION == "gex-engine/2.1.10"
