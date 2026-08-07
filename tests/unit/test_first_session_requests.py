"""The request plan the first paid ThetaData session will actually run.

Every test here fails against v2.1.15, and none of them makes a network
request: each drives the real derivation, the real dry run or the deterministic
fake transport.

The headline is the first one. The shipped profile trades SPXW options, and
v2.1.15 sent ``symbol=SPXW`` to ``/v3/index/snapshot/price`` -- a request for
the price of an instrument that does not exist. Whatever came back would have
become the spot under every gamma in the chain, and nothing in the dry run
showed the symbol at all.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from src.tools.capture_thetadata_once import (
    RawCaptureRunState,
    plan_capture,
    run_capture,
    run_path,
)
from tests.certification_fixtures import approval_hash_for

CAPTURE_CONFIG = "config/thetadata_capture.yaml"

INDEX = "/v3/index/snapshot/price"
QUOTE = "/v3/option/snapshot/quote"
OPEN_INTEREST = "/v3/option/snapshot/open_interest"
FIRST_ORDER = "/v3/option/snapshot/greeks/first_order"
CONTRACT_LIST = "/v3/option/list/contracts/quote"


# =============================================================================
# §1 -- the option root and the underlying index are two different things
# =============================================================================


def test_an_spxw_chain_asks_the_index_endpoint_for_spx():
    """The principal reproduced defect.

    ``SPXW`` is the PM-settled weekly option root. ``SPX`` is the index those
    options are written on. v2.1.15 held one symbol and gave it to both.
    """
    plan = _plan()
    by_endpoint = {entry.endpoint: entry.parameters for entry in plan.requests}

    assert by_endpoint[INDEX]["symbol"] == "SPX"
    assert plan.underlying_index_symbol == "SPX"


def test_every_option_market_request_asks_for_spxw():
    plan = _plan()
    by_endpoint = {entry.endpoint: entry.parameters for entry in plan.requests}

    for endpoint in (QUOTE, OPEN_INTEREST, FIRST_ORDER, CONTRACT_LIST):
        assert by_endpoint[endpoint]["symbol"] == "SPXW", endpoint
    assert plan.option_symbol == "SPXW"


def test_an_spx_chain_asks_the_index_endpoint_for_spx():
    """The identity case. ``SPX`` maps to itself -- declared, not defaulted."""
    from src.adapters.thetadata.instruments import mapping_for

    mapping = mapping_for("SPX")
    assert mapping.option_symbol == "SPX"
    assert mapping.underlying_index_symbol == "SPX"
    assert mapping.symbol_for(INDEX) == "SPX"
    assert mapping.symbol_for(QUOTE) == "SPX"


def test_an_undeclared_root_is_refused_rather_than_guessed():
    """No string rule. ``SPXW`` -> ``SPX`` is a fact about the market."""
    from src.adapters.thetadata.instruments import (
        UnknownInstrumentRootError,
        mapping_for,
    )

    with pytest.raises(UnknownInstrumentRootError, match=r"(?i)deliberately"):
        mapping_for("NDXP")


def test_changing_the_index_mapping_changes_the_request_plan_hash():
    """A corrected mapping is a different plan, so it cannot reuse a capture."""
    import dataclasses

    from src.adapters.thetadata.instruments import InstrumentMapping

    plan = _plan()
    original = plan.request_plan_hash

    wrong = dataclasses.replace(
        plan,
        underlying_index_symbol="SPXW",
        requests=tuple(
            dataclasses.replace(
                entry,
                canonical_query_parameters=(("symbol", "SPXW"),),
            )
            if entry.endpoint == INDEX
            else entry
            for entry in plan.requests
        ),
    )
    assert wrong.request_plan_hash != original

    # And the capture plan itself distinguishes them, which is what stops a
    # capture taken under the wrong mapping verifying under the right one.
    from src.adapters.thetadata.capture_plan import capture_plan_for
    from src.adapters.thetadata.endpoints import Tier
    from tests.certification_fixtures import resolved_pipeline

    pipeline = resolved_pipeline()
    correct = capture_plan_for(
        pricing_mode=pipeline.pricing_mode,
        vendor_gamma_policy=pipeline.vendor_gamma_policy,
        underlying_price_source=pipeline.config.underlying_price_source,
        tier=Tier(pipeline.config.tier),
        instruments=InstrumentMapping(
            option_symbol="SPXW", underlying_index_symbol="SPX"
        ),
    )
    mistaken = capture_plan_for(
        pricing_mode=pipeline.pricing_mode,
        vendor_gamma_policy=pipeline.vendor_gamma_policy,
        underlying_price_source=pipeline.config.underlying_price_source,
        tier=Tier(pipeline.config.tier),
        instruments=InstrumentMapping(
            option_symbol="SPXW", underlying_index_symbol="SPXW"
        ),
    )
    assert correct.fingerprint != mistaken.fingerprint


def test_a_capture_taken_under_the_wrong_mapping_does_not_verify(tmp_path):
    """Correcting the mapping must not silently bless the old bytes.

    The capture records carry the plan fingerprint they were taken under, and
    verification recomputes it. A run whose index request said ``SPXW`` is not
    a run this pipeline would have made.
    """
    import dataclasses

    from src.adapters.certification import verify_capture
    from tests.certification_fixtures import build_capture, plan_for, resolved_pipeline

    pipeline = resolved_pipeline()
    store, manifest = build_capture(pipeline=pipeline)
    assert verify_capture(
        manifest,
        store,
        plan=plan_for(pipeline),
        expected_pipeline_fingerprint=pipeline.fingerprint(),
    ).verified

    # The same bytes, presented as though the index had been asked for SPXW.
    mislabelled = dataclasses.replace(
        manifest,
        records=tuple(
            dataclasses.replace(entry, capture_plan_fingerprint="0" * 64)
            if entry.endpoint == INDEX
            else entry
            for entry in manifest.records
        ),
    )
    result = verify_capture(
        mislabelled,
        store,
        plan=plan_for(pipeline),
        expected_pipeline_fingerprint=pipeline.fingerprint(),
    )
    assert not result.verified
    # The record that was relabelled is named, and the endpoint it belongs to
    # now reads as missing -- a record claiming another plan is not one this
    # capture can count.
    assert any(INDEX in failure for failure in result.failures), result.failures


# =============================================================================
# §2 -- the contract listing is captured, and authorises nothing
# =============================================================================


def test_the_contract_list_is_in_the_first_session_plan():
    plan = _plan()
    assert CONTRACT_LIST in {entry.endpoint for entry in plan.requests}

    from tests.certification_fixtures import resolved_pipeline

    capture_plan = resolved_pipeline().capture_plan
    # An *evidence* endpoint: requested, but its absence is not a verification
    # failure, because a chain does not need it.
    assert CONTRACT_LIST in {e.value for e in capture_plan.evidence_endpoints}
    assert CONTRACT_LIST not in {e.value for e in capture_plan.required_endpoints}
    assert CONTRACT_LIST in {e.value for e in capture_plan.acquisition_endpoints}
    assert capture_plan.is_evidence_only(CONTRACT_LIST)


def test_the_contract_list_request_carries_the_session_date_and_scope():
    """Its date is the New York market session, not a UTC calendar day.

    ``as_of.date()`` answers a different question, and the two disagree for six
    hours out of every twenty-four.
    """
    from datetime import UTC, datetime

    from src.gex.sessions import market_session_date
    from tests.certification_fixtures import resolved_pipeline

    # 01:00Z on the 18th is still the 17th in New York, where the market was
    # open. A listing dated the 18th describes a different session.
    moment = datetime(2026, 3, 18, 1, 0, tzinfo=UTC)
    plan = resolved_pipeline().raw_request_plan(as_of=moment)
    listing = {e.endpoint: e.parameters for e in plan.requests}[CONTRACT_LIST]

    assert listing["date"] == market_session_date(moment).isoformat()
    assert listing["date"] == "2026-03-17"
    assert listing["symbol"] == "SPXW"


def test_the_shipped_standard_tier_can_serve_the_contract_list():
    from src.adapters.thetadata.endpoints import (
        MINIMUM_TIER,
        Endpoint,
        Tier,
        tier_satisfies,
    )

    assert tier_satisfies(
        Tier.STANDARD, MINIMUM_TIER[Endpoint.OPTION_CONTRACT_LIST_QUOTE]
    )
    plan = _plan()
    listing = next(e for e in plan.requests if e.endpoint == CONTRACT_LIST)
    assert listing.required_tier == "value"


def test_a_malformed_contract_list_does_not_stop_the_other_endpoints(tmp_path):
    """It is the last request and the least authoritative. It cannot cost the rest."""
    from src.adapters.transport import FakeTransport
    from tests.certification_fixtures import AS_OF, payloads

    transport = FakeTransport()
    for endpoint, text in payloads().items():
        if endpoint.value == CONTRACT_LIST:
            transport.register_bytes(
                endpoint.value,
                b"<html>no listing today</html>",
                **{"content-type": "text/html"},
            )
        else:
            transport.register_text(endpoint.value, text)

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=transport,
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
        approved=approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF),
    )
    acquisition = report["raw_acquisition"]
    assert set(acquisition["acquired_endpoints"]) == set(
        acquisition["planned_endpoints"]
    )
    assert acquisition["stopped_early"] is False
    # The chain endpoints all parsed; only the listing did not.
    parser = json.loads(
        run_path(report, "parser_report_path").read_text(encoding="utf-8")
    )
    failed = [
        e["endpoint"]
        for e in parser["endpoints"]
        if e["parser_status"] != "PARSER_VALID"
    ]
    assert failed == [CONTRACT_LIST]


def test_a_good_contract_list_response_grants_no_coverage(tmp_path):
    """The safety rule. Being a list is not being *our* list.

    A listing of everything quoted on a session is a different set from the
    contracts a request bounded by ``max_dte`` and ``strike_range`` was owed,
    and nobody has compared the two against real bytes.
    """
    from src.adapters.thetadata.endpoints import (
        DEDICATED_CONTRACT_LIST_ENDPOINTS,
        capabilities_of,
    )
    from tests.certification_fixtures import AS_OF, vendor_transport

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=vendor_transport(),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
        approved=approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF),
    )
    assert CONTRACT_LIST in report["raw_acquisition"]["acquired_endpoints"]
    # Derived since v2.1.17: bytes arrived and they parsed, which is the
    # strongest state available -- and it is still UNVERIFIED, because being a
    # list is not being *our* list.
    assert report["contract_list_evidence_state"] == "ACQUIRED_PARSED_UNVERIFIED"
    # A capture that answered every request is still raw-only. Coverage is an
    # open question and the report does not pretend otherwise.
    assert report["run_state"] == RawCaptureRunState.COMPLETED_RAW_VERIFIED.value
    assert report["trusted_gex_computed"] is False

    # A complete raw capture, including the listing, and readiness has not
    # moved: the dataset blockers are the same ones, and coverage is still an
    # open question. Capturing a list is not proving a universe.
    planned = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "planned"))
    assert planned["capture_readiness"] == "READY_FOR_RAW_CAPTURE_ONLY"
    assert planned["actual_analytical_blockers"], "coverage is still an open question"
    assert any(
        "universe" in blocker.lower()
        for blocker in planned["actual_analytical_blockers"]
    )

    # And the capability table says the same thing structurally.
    capability = capabilities_of(CONTRACT_LIST)
    assert capability.is_dedicated_contract_list
    assert not capability.enumerates_request_universe
    assert CONTRACT_LIST in DEDICATED_CONTRACT_LIST_ENDPOINTS


# =============================================================================
# §3 -- the exact request plan is visible, and binding
# =============================================================================


def test_the_dry_run_reveals_every_request(tmp_path):
    """The named regression: v2.1.15 printed a count of endpoints and a tier."""
    report = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "capture"))
    planned = report["planned_requests"]

    by_endpoint = {
        entry["endpoint"]: dict(
            (name, value) for name, value in entry["canonical_query_parameters"]
        )
        for entry in planned["requests"]
    }
    assert set(by_endpoint) == {INDEX, QUOTE, OPEN_INTEREST, FIRST_ORDER, CONTRACT_LIST}

    # The two symbols, visible before --execute-live.
    assert by_endpoint[INDEX]["symbol"] == "SPX"
    assert by_endpoint[QUOTE]["symbol"] == "SPXW"
    assert planned["access_mode"] == "THETA_TERMINAL_REST_V3"
    assert report["access_mode"] == "THETA_TERMINAL_REST_V3"

    # And the scope an operator is checking: the shipped profile's DTE window
    # reaches the option requests *and* the listing, so the two describe the
    # same set of expirations.
    assert by_endpoint[QUOTE]["max_dte"] == "60"
    assert by_endpoint[CONTRACT_LIST]["max_dte"] == "60"
    assert by_endpoint[CONTRACT_LIST]["date"]
    greeks = by_endpoint[FIRST_ORDER]
    assert {"rate_type", "rate_value", "annual_dividend"} <= set(greeks)


def test_planned_parameters_are_deterministically_sorted(tmp_path):
    report = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "capture"))
    for entry in report["planned_requests"]["requests"]:
        names = [name for name, _ in entry["canonical_query_parameters"]]
        assert names == sorted(names), entry["endpoint"]
        assert all(
            isinstance(value, str) for _, value in entry["canonical_query_parameters"]
        )

    # Two derivations of one configuration agree, which is what makes the hash
    # comparable between the dry run and the live run.
    again = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "other"))
    assert (
        again["planned_requests"]["request_plan_hash"]
        == report["planned_requests"]["request_plan_hash"]
    )


def test_no_secret_reaches_the_request_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("THETADATA_USERNAME", "an-operator")
    monkeypatch.setenv("THETADATA_PASSWORD", "not-in-any-report")
    report = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "capture"))
    rendered = json.dumps(report["planned_requests"])

    assert "not-in-any-report" not in rendered
    assert "an-operator" not in rendered
    for shape in ("password", "token", "secret", "api_key"):
        assert shape not in rendered.lower(), shape


def test_a_request_that_differs_from_the_plan_is_refused():
    """Refused before the transport. The operator approved a document."""
    from src.adapters.thetadata.request_plan import RequestPlanViolation

    plan = _plan()
    with pytest.raises(RequestPlanViolation, match=r"(?i)did not authorise"):
        plan.authorize(INDEX, {"symbol": "SPXW"})
    with pytest.raises(RequestPlanViolation, match=r"(?i)not in the request plan"):
        plan.authorize("/v3/option/history/quote", {"symbol": "SPXW"})

    # The planned request itself passes, which is what makes the refusal mean
    # something.
    assert plan.authorize(INDEX, {"symbol": "SPX"}).endpoint == INDEX


def test_the_live_run_records_the_plan_it_was_authorised_against(tmp_path):
    from tests.certification_fixtures import AS_OF, vendor_transport

    dry = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "capture"))
    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=vendor_transport(),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
        approved=approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF),
    )
    live = report["request_plan"]
    assert live["request_plan_hash"] == report["raw_acquisition"]["request_plan_hash"]

    # The dry run and the live run derive the same requests. Only the listing's
    # session date can differ, because the dry run was not run at ``as_of``.
    def comparable(plan):
        return {
            entry["endpoint"]: [
                pair
                for pair in entry["canonical_query_parameters"]
                if pair[0] != "date"
            ]
            for entry in plan["requests"]
        }

    assert comparable(live) == comparable(dry["planned_requests"])

    # And it is on disk before the first request, in the intent document.
    intent = json.loads(run_path(report, "intent_path").read_text(encoding="utf-8"))
    assert intent["request_plan"]["request_plan_hash"] == live["request_plan_hash"]


# =============================================================================
# §4/§5 -- attempt evidence is part of what "verified" means
# =============================================================================


def test_a_failed_attempt_receipt_prevents_a_verified_run(tmp_path, monkeypatch):
    """The named regression: verified beside ``attempt_evidence.ok = false``.

    The captured responses are not discarded and not downgraded -- they are on
    disk and they verified. What changes is the claim the run makes about
    itself.
    """
    import src.tools.capture_thetadata_once as tool
    from tests.certification_fixtures import AS_OF, vendor_transport

    original = tool._attempt_receipt

    def damaged(run):
        receipt = dict(original(run))
        receipt["ok"] = False
        receipt["findings"] = ["ab/cd.bin: body hashes to deadbeef..."]
        return receipt

    monkeypatch.setattr(tool, "_attempt_receipt", damaged)
    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=vendor_transport(),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
        approved=approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF),
    )

    assert report["attempt_evidence"]["ok"] is False
    assert report["run_state"] == RawCaptureRunState.COMPLETED_RAW_UNVERIFIED.value
    assert report["verification_layer"] == "HTTP_ATTEMPT_EVIDENCE"
    assert report["verification_findings"]
    assert report["verification_layers"]["RAW_STORE_INTEGRITY"] is True
    assert report["verification_layers"]["CAPTURE_MANIFEST"] is True
    # The bytes are still there. Only the claim changed.
    assert report["integrity_ok"] is True
    assert len(report["record_ids"]) == 5


def test_a_fresh_log_cannot_verify_a_directory_it_never_read(tmp_path):
    """The named regression: ``HttpAttemptLog(root).verify_bodies() == ()``.

    An empty answer now means "nothing is wrong", never "I looked at nothing".
    """
    from src.adapters.errors import ThetaDataRawStoreError
    from src.adapters.http_attempts import HttpAttemptLog
    from tests.certification_fixtures import AS_OF, vendor_transport

    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=vendor_transport(),
        as_of=AS_OF,
        allow_unsettled_raw_only=True,
        approved=approval_hash_for(CAPTURE_CONFIG, as_of=AS_OF),
    )
    root = run_path(report, "attempt_store_path")

    with pytest.raises(ThetaDataRawStoreError, match=r"(?i)has not loaded it"):
        HttpAttemptLog(root).verify_bodies()

    # And the two explicit doors behave as their names say.
    with pytest.raises(ThetaDataRawStoreError, match=r"(?i)already exists"):
        HttpAttemptLog.create_new(root)
    reopened = HttpAttemptLog.open_existing(root)
    assert reopened.ok, reopened.findings
    assert reopened.attempt_count >= 5


def test_create_new_accepts_a_directory_with_no_index(tmp_path):
    from src.adapters.http_attempts import HttpAttemptLog

    log = HttpAttemptLog.create_new(tmp_path / "attempts")
    assert log.records == []
    # Nothing was read, and nothing claims to have been.
    assert log.verify_bodies() == ()


# =============================================================================
# Shared helper
# =============================================================================


def _plan():
    from tests.certification_fixtures import AS_OF, resolved_pipeline

    return resolved_pipeline().raw_request_plan(as_of=AS_OF)


def test_this_file_makes_no_vendor_request():
    """A rule, checked, rather than a sentence in a docstring."""
    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    for forbidden in ("httpx" + ".Client(", "socket" + ".", "url" + "open"):
        assert forbidden not in source, forbidden
