"""What the operator approved, and what the live command is allowed to send.

**Every test here fails against v2.1.18, and none of them makes a network
request.** Each drives the real operator against the deterministic fake
transport, or the real pipeline against the pinned document on disk.

Two gaps, one file.

The first is the join in the middle of the v2.1.18 workflow: dry run, read the
plan, rerun with ``--execute-live``, and the live command derived a *new* plan
and authorized itself. Every step was checked. Nothing tied the plan a human
looked at to the plan that went out.

The second is that ``VendorDocumentationBundle`` is a public dataclass, so a
bundle carrying the real document's digest, invented YAML paths and hand-picked
values constructed cleanly and ``validate_integrity`` then recomputed pricing
compatibility from it -- arithmetic consistent all the way down to a reading
nobody had taken.
"""

from __future__ import annotations

import json
import pathlib
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from src.adapters.thetadata.preflight_approval import (
    APPROVAL_TRANSPORT_FIELDS,
    CAPTURE_PREFLIGHT_APPROVAL_SCHEMA_VERSION,
    CapturePreflightApproval,
    PreflightApprovalError,
    approval_for,
)
from src.tools.capture_thetadata_once import (
    CaptureRunError,
    plan_capture,
    run_capture,
    run_path,
)
from tests.certification_fixtures import AS_OF, approval_hash_for, vendor_transport

CAPTURE_CONFIG = "config/thetadata_capture.yaml"


def approval_at(moment: datetime) -> CapturePreflightApproval:
    """The approval a dry run at this moment would print."""
    from src.adapters.transport import FakeTransport
    from src.config.pipeline import ThetaDataResearchPipeline
    from src.config.schema import load_config

    loaded = load_config(CAPTURE_CONFIG)
    pipeline = ThetaDataResearchPipeline.from_loaded_config(
        loaded, transport=FakeTransport()
    )
    return approval_for(pipeline=pipeline, config=loaded.thetadata, moment=moment)


def capture(tmp_path, **overrides):
    """A real acquiring run against the fake transport."""
    moment = overrides.pop("as_of", AS_OF)
    return run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / overrides.pop("name", "capture")),
        transport=overrides.pop("transport", None) or vendor_transport(),
        as_of=moment,
        allow_unsettled_raw_only=True,
        approved=overrides.pop(
            "approved", approval_hash_for(CAPTURE_CONFIG, as_of=moment)
        ),
        **overrides,
    )


# =============================================================================
# §1 -- the live run sends only what was approved
# =============================================================================


def test_live_execution_without_an_approval_is_refused(tmp_path):
    """**The central regression.** v2.1.18 authorized itself."""
    with pytest.raises(CaptureRunError, match=r"(?i)requires --approve"):
        capture(tmp_path, approved="")


def test_the_refusal_prints_the_approval_the_operator_would_need(tmp_path):
    """A refusal that does not say what to do next teaches nothing."""
    with pytest.raises(CaptureRunError) as raised:
        capture(tmp_path, approved="")
    expected = approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF)
    assert expected in str(raised.value)
    assert "--approve" in str(raised.value)


def test_a_valid_same_session_approval_permits_execution(tmp_path):
    from src.tools.capture_thetadata_once import RawCaptureRunState

    report = capture(tmp_path)
    assert report["run_state"] == RawCaptureRunState.COMPLETED_RAW_VERIFIED.value


def test_a_friday_approval_refuses_a_monday_execution(tmp_path):
    """The contract-list request carries the session date, so Monday's requests
    are different requests. Rerunning the dry run costs seconds."""
    friday = datetime(2026, 3, 13, 15, 0, tzinfo=UTC)
    monday = datetime(2026, 3, 16, 15, 0, tzinfo=UTC)
    stale = approval_hash_for(CAPTURE_CONFIG, as_of=friday)
    assert stale != approval_hash_for(CAPTURE_CONFIG, as_of=monday)

    with pytest.raises(CaptureRunError, match=r"(?i)does not authorise"):
        capture(tmp_path, as_of=monday, approved=stale)


def test_a_stale_session_approval_is_named_as_a_session_change(tmp_path):
    """ "It is Monday now" is a different message from "your hash is wrong"."""
    monday = datetime(2026, 3, 16, 15, 0, tzinfo=UTC)
    friday = monday - timedelta(days=3)
    with pytest.raises(CaptureRunError) as raised:
        capture(
            tmp_path,
            as_of=monday,
            approved=approval_hash_for(CAPTURE_CONFIG, as_of=friday),
        )
    message = str(raised.value)
    assert "market session date changed" in message
    assert "2026-03-13" in message
    assert "2026-03-16" in message


def test_there_is_no_flag_that_skips_the_approval():
    """A bypass would restore the gap with a name, and would be reached for
    exactly when somebody is in a hurry during a paid session."""
    from src.tools.capture_thetadata_once import build_parser

    flags = {action.dest for action in build_parser()._actions}
    assert "force" not in flags
    assert not [name for name in flags if "skip" in name or "bypass" in name]


# =============================================================================
# §1 -- what an approval covers, and what it must not
# =============================================================================


def test_changing_one_request_parameter_invalidates_the_approval():
    """**The §2 regression.** Approve a plan, then change a query parameter."""
    from src.adapters.transport import FakeTransport
    from src.config.pipeline import ThetaDataResearchPipeline
    from src.config.schema import load_config

    loaded = load_config(CAPTURE_CONFIG)
    approved = approval_at(AS_OF)

    widened = ThetaDataResearchPipeline.from_config(
        parse_with(loaded, max_dte=int(loaded.thetadata.max_dte) + 1),
        model_spec=loaded.engine.model_spec,
        engine_config=loaded.engine,
        transport=FakeTransport(),
    )
    after = approval_for(
        pipeline=widened,
        config=parse_with(loaded, max_dte=int(loaded.thetadata.max_dte) + 1),
        moment=AS_OF,
    )
    assert after.request_plan_hash != approved.request_plan_hash
    assert not approved.matches(after.approval_hash)
    assert "request plan changed" in after.differences_from(approved)


def parse_with(loaded, **overrides):
    """The shipped profile with one setting changed."""
    import dataclasses

    return dataclasses.replace(loaded.thetadata, **overrides)


def test_changing_the_instrument_mapping_invalidates_the_approval():
    """SPXW options are written on SPX. A capture of one is not the other."""
    from src.adapters.thetadata.instruments import InstrumentMapping
    from src.domain.digests import digest_of

    approved = approval_at(AS_OF)
    moved = digest_of(
        InstrumentMapping(
            option_symbol="SPX", underlying_index_symbol="SPX"
        ).semantic_payload()
    )
    assert moved != approved.instrument_mapping_fingerprint


def test_changing_the_documentation_bundle_invalidates_the_approval():
    from src.adapters.transport import FakeTransport
    from src.config.pipeline import ThetaDataResearchPipeline
    from src.config.schema import load_config

    loaded = load_config(CAPTURE_CONFIG)
    with_document = approval_at(AS_OF)
    without = ThetaDataResearchPipeline.from_config(
        loaded.thetadata,
        model_spec=loaded.engine.model_spec,
        engine_config=loaded.engine,
        transport=FakeTransport(),
        documentation_bundle=None,
    )
    after = approval_for(pipeline=without, config=loaded.thetadata, moment=AS_OF)
    assert after.documentation_bundle_fingerprint == ""
    assert with_document.documentation_bundle_fingerprint != ""
    assert not with_document.matches(after.approval_hash)
    assert "documentation bundle changed" in after.differences_from(with_document)


def test_changing_effective_http_settings_invalidates_the_approval():
    from src.config.schema import load_config

    loaded = load_config(CAPTURE_CONFIG)
    approved = approval_at(AS_OF)
    slower = parse_with(
        loaded, timeout_seconds=float(loaded.thetadata.timeout_seconds) + 5
    )

    from src.adapters.transport import FakeTransport
    from src.config.pipeline import ThetaDataResearchPipeline

    pipeline = ThetaDataResearchPipeline.from_config(
        slower,
        model_spec=loaded.engine.model_spec,
        engine_config=loaded.engine,
        transport=FakeTransport(),
    )
    after = approval_for(pipeline=pipeline, config=slower, moment=AS_OF)
    assert after.effective_transport_fingerprint != (
        approved.effective_transport_fingerprint
    )
    assert "effective transport settings changed" in after.differences_from(approved)


def test_credentials_are_not_in_the_approval_payload(tmp_path):
    """An approval is a value an operator pastes into a shell.

    The transport fields it covers are an allowlist, so a credential field that
    arrives in ``effective_transport_settings`` later cannot walk in by default.
    """
    report = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "capture"))
    blob = json.dumps(report["preflight_approval"]).lower()
    for forbidden in ("password", "username", "credential", "secret", "token"):
        assert forbidden not in blob, forbidden

    for name in APPROVAL_TRANSPORT_FIELDS:
        assert "user" not in name
        assert "pass" not in name
        assert "credential" not in name


def test_the_approval_ignores_the_destination_and_the_run_id(tmp_path):
    """An approval that moved when the output directory changed would train an
    operator to stop reading it."""
    first = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "one"))
    second = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "two"))
    assert (
        first["preflight_approval"]["approval_hash"]
        == second["preflight_approval"]["approval_hash"]
    )


def test_an_approval_hash_that_does_not_follow_from_its_fields_is_refused():
    approved = approval_at(AS_OF)
    with pytest.raises(PreflightApprovalError, match=r"(?i)hash"):
        CapturePreflightApproval(
            market_session_date=approved.market_session_date,
            request_plan_hash=approved.request_plan_hash,
            capture_plan_fingerprint=approved.capture_plan_fingerprint,
            pipeline_fingerprint=approved.pipeline_fingerprint,
            documentation_bundle_fingerprint=(
                approved.documentation_bundle_fingerprint
            ),
            effective_transport_fingerprint=(approved.effective_transport_fingerprint),
            instrument_mapping_fingerprint=approved.instrument_mapping_fingerprint,
            subscription_tier=approved.subscription_tier,
            approval_hash="0" * 64,
        )


def test_an_approval_pasted_with_stray_whitespace_still_matches():
    """Refusing a correct approval because of a trailing space teaches the
    operator that the check is noise."""
    approved = approval_at(AS_OF)
    assert approved.matches(f"  {approved.approval_hash.upper()}\n")


# =============================================================================
# §1 -- when the approval is checked, and what records it
# =============================================================================


def test_the_approval_is_checked_before_the_destination_is_created(tmp_path):
    destination = tmp_path / "capture"
    with pytest.raises(CaptureRunError, match=r"(?i)requires --approve"):
        capture(tmp_path, approved="")
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_the_approval_is_checked_before_any_transport_request(tmp_path):
    """Counted, not assumed: the transport records every call it is asked for."""

    class _Counting:
        def __init__(self) -> None:
            self.calls = 0
            self.inner = vendor_transport()

        def __getattr__(self, name):
            attribute = getattr(self.inner, name)
            if not callable(attribute):
                return attribute

            def counted(*args, **kwargs):
                self.calls += 1
                return attribute(*args, **kwargs)

            return counted

    transport = _Counting()
    with pytest.raises(CaptureRunError, match=r"(?i)requires --approve"):
        capture(tmp_path, approved="", transport=transport)
    assert transport.calls == 0


def test_the_approval_hash_is_persisted_into_the_run_evidence(tmp_path):
    report = capture(tmp_path)
    expected = approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF)

    assert report["preflight_approval"]["approval_hash"] == expected

    intent = json.loads(
        run_path(report, "output_root").joinpath("run-intent.json").read_text("utf-8")
    )
    assert intent["preflight_approval"]["approval_hash"] == expected

    summary = json.loads(run_path(report, "summary_path").read_text("utf-8"))
    assert summary["preflight_approval"]["approval_hash"] == expected


def test_every_raw_record_is_bound_to_the_approved_operation(tmp_path):
    """The transitive binding: the operation digest covers the approval, and
    every record is stamped with the operation."""
    from src.adapters.raw_store import FileRawStore

    report = capture(tmp_path)
    expected = approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF)

    store = FileRawStore(run_path(report, "raw_store_path"))
    records = store.records()
    assert records
    for record in records:
        assert record.preflight_approval_hash == expected


def test_the_dry_run_prints_the_five_values_the_operator_decides_on(tmp_path):
    approval = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "capture"))[
        "preflight_approval"
    ]
    for name in (
        "market_session_date",
        "request_plan_hash",
        "pipeline_fingerprint",
        "documentation_bundle_fingerprint",
        "approval_hash",
    ):
        assert approval[name], name
    assert approval["schema_version"] == CAPTURE_PREFLIGHT_APPROVAL_SCHEMA_VERSION


def test_the_dry_run_still_writes_nothing(tmp_path):
    """Producing an approval must not make the dry run mutating."""
    destination = tmp_path / "capture"
    report = plan_capture(CAPTURE_CONFIG, output=str(destination))
    assert report["wrote_files"] is False
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


# =============================================================================
# §3 -- a bundle is a report; the loader is the authority
# =============================================================================


def forged(**overrides):
    """A bundle carrying the real document and a reading nobody took."""
    from src.adapters.thetadata.openapi_evidence import (
        OpenApiEvidenceExtraction,
        VendorDocumentationBundle,
        production_bundle,
        repository_documentation_root,
    )

    genuine = production_bundle()
    fields = {
        "rule": genuine.extractions[0].rule,
        "document_sha256": genuine.document.document_sha256,
        "yaml_path": genuine.extractions[0].yaml_path,
        "expected_source_fragment": (genuine.extractions[0].expected_source_fragment),
        "normalized_value": genuine.extractions[0].normalized_value,
        "normalizer": genuine.extractions[0].normalizer,
    }
    return VendorDocumentationBundle(
        document=genuine.document,
        extractions=(OpenApiEvidenceExtraction(**{**fields, **overrides}),),
        verified_root=str(repository_documentation_root()),
    )


def pipeline_with(bundle):
    from src.adapters.transport import FakeTransport
    from src.config.pipeline import ThetaDataResearchPipeline
    from src.config.schema import load_config

    loaded = load_config(CAPTURE_CONFIG)
    return ThetaDataResearchPipeline.from_config(
        loaded.thetadata,
        model_spec=loaded.engine.model_spec,
        engine_config=loaded.engine,
        transport=FakeTransport(),
        documentation_bundle=bundle,
    )


def test_a_hand_built_bundle_with_a_fake_yaml_path_is_refused():
    """**The §3 regression.** Real digest, invented path."""
    from src.config.pipeline import PipelineConsistencyError

    with pytest.raises(PipelineConsistencyError, match=r"(?i)does not follow"):
        pipeline_with(
            forged(yaml_path=("components", "parameters", "invented", "description"))
        )


def test_a_hand_built_bundle_with_a_fabricated_value_is_refused():
    """Real digest, real path, a value those bytes do not yield."""
    from src.config.pipeline import PipelineConsistencyError
    from src.domain.settlement import SettlementRuleKind

    with pytest.raises(PipelineConsistencyError, match=r"(?i)not the values"):
        pipeline_with(forged(normalized_value=SettlementRuleKind.SAME_SESSION))


def test_a_hand_built_bundle_with_a_fabricated_fragment_is_refused():
    from src.config.pipeline import PipelineConsistencyError

    with pytest.raises(PipelineConsistencyError, match=r"(?i)does not follow"):
        pipeline_with(forged(expected_source_fragment="settles on the current session"))


def test_a_bundle_naming_no_verified_root_is_refused():
    """A bundle carrying no path to its own bytes is a set of claims."""
    import dataclasses

    from src.adapters.thetadata.openapi_evidence import production_bundle
    from src.config.pipeline import PipelineConsistencyError

    rootless = dataclasses.replace(production_bundle(), verified_root="")
    with pytest.raises(PipelineConsistencyError, match=r"(?i)no verified root"):
        pipeline_with(rootless)


def test_the_genuine_production_bundle_still_loads_normally():
    """No behaviour change for the valid path, which is most of the point."""
    from src.adapters.thetadata.openapi_evidence import production_bundle

    built = pipeline_with(production_bundle())
    assert built.documentation_fingerprint == production_bundle().bundle_hash
    built.validate_integrity()


def test_the_rederived_bundle_replaces_the_callers_object():
    """Even when they agree. Keeping the caller's would leave the pipeline
    holding an object whose provenance is "somebody passed it in"."""
    from src.adapters.thetadata.openapi_evidence import production_bundle

    supplied = production_bundle()
    built = pipeline_with(supplied)
    assert built.documentation_bundle is not supplied
    assert built.documentation_bundle.bundle_hash == supplied.bundle_hash


# =============================================================================
# §4 -- integrity checking re-establishes the document
# =============================================================================


def test_validate_integrity_rechecks_the_document_bytes(tmp_path):
    """**The §4 regression.** Tampering with the pinned file invalidates the
    pipeline, without any network access."""
    import dataclasses

    from src.adapters.thetadata.openapi_evidence import (
        PRODUCTION_PINNED_DOCUMENT,
        production_bundle,
        repository_documentation_root,
    )
    from src.config.pipeline import PipelineConsistencyError

    location = PRODUCTION_PINNED_DOCUMENT.content_location
    copy = tmp_path / location
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(repository_documentation_root() / location, copy)

    relocated = dataclasses.replace(production_bundle(), verified_root=str(tmp_path))
    built = pipeline_with(relocated)
    built.validate_integrity()

    copy.write_bytes(b"openapi: 3.1.0\npaths: {}\n")
    with pytest.raises(PipelineConsistencyError, match=r"(?i)no longer follows"):
        built.validate_integrity()


def test_integrity_checking_makes_no_network_request(monkeypatch):
    """Only the pinned local bytes. An integrity check that reached the vendor
    would make every calculation depend on the vendor being up."""
    import socket

    from src.adapters.thetadata.openapi_evidence import production_bundle

    built = pipeline_with(production_bundle())

    def refuse(*args, **kwargs):  # pragma: no cover - only on regression
        raise AssertionError("validate_integrity must not use the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    built.validate_integrity()


def test_a_session_without_documentation_still_validates():
    """No documentary evidence is a legitimate state, not a failure."""
    built = pipeline_with(None)
    assert built.documentation_fingerprint == ""
    built.validate_integrity()


# =============================================================================
# §5 -- documentary settlement is not empirical confirmation
# =============================================================================


def test_the_settlement_rule_stays_documentary_after_a_capture(tmp_path):
    """A capture observes open-interest *values*, timestamps and identities.

    No ThetaData snapshot endpoint carries a settlement-date field, so the
    bytes look the same whether the documented convention holds or not.
    Upgrading the rule to ``LIVE_COMPARISON`` because a capture exists would
    record that we watched the vendor do something we did not watch.
    """
    from src.adapters.thetadata.endpoints import RESPONSE_FIELDS, Endpoint

    fields = RESPONSE_FIELDS[Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT]
    assert "open_interest" in fields
    for absent in ("settlement_date", "open_interest_as_of", "oi_date"):
        assert absent not in fields, absent

    capture(tmp_path)


def test_no_current_document_claims_a_capture_proves_the_settlement_rule():
    root = pathlib.Path(__file__).resolve().parents[2]
    text = (root / "docs" / "THETADATA_INTEGRATION.md").read_text(encoding="utf-8")
    assert "the capture cannot confirm it" in text
    assert "AUTHORITATIVE_VENDOR_DOCUMENTATION" in text
