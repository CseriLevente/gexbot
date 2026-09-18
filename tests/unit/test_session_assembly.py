"""Merging one session's cycles by recorded availability, with lineage.

One scripted session is collected once per module on a fake clock -- reversion
``A -> B -> A``, an unchanging quote, a conflicting pair after a known state,
a stale re-observation of an older event, a failed Greeks request, an
overrunning cycle, a restart, a sparse open-interest refresh, an identity no
listing names -- and every rule is then checked on the assembled stream and on
what the chronological replay makes of it. Nothing here is market data.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
from datetime import UTC, date, datetime, timedelta

import pytest

from src.adapters.thetadata.capture_certification import (
    OPTION_CONTRACT_LIST,
    OPTION_GREEKS,
    OPTION_OPEN_INTEREST,
    OPTION_QUOTE,
)
from src.adapters.thetadata.research_events import NOT_ACQUIRED, NOT_SCHEDULED
from src.adapters.thetadata.session_assembly import (
    AMBIGUOUS_AFTER_KNOWN_STATE,
    ASSEMBLER,
    ASSEMBLY_SCHEMA,
    LATE_OLDER_EVENT,
    NEW_EVENT,
    NOT_IN_LATEST_INVENTORY,
    REOBSERVED_UNCHANGED,
    REVERTED_REVISION,
    REVISION,
    SessionAssemblyError,
    assemble_session,
    verify_session,
)
from src.gex.sessions import EASTERN
from src.ingest.clock import FakeClock
from src.ingest.schedule import CollectionPolicy
from src.ingest.session_collector import (
    INTENT_NAME,
    LOG_NAME,
    collect_session,
    plan_session,
    read_log,
)
from src.replay.event_store import (
    SESSION_EVENT_SCHEMA,
    EventStore,
    load_events,
    read_json,
)
from src.replay.session import replay_bundle
from src.replay.session_readiness import (
    SESSION_READINESS_SCHEMA,
    render_session_markdown,
    session_readiness,
)
from src.tools.assemble_intraday_session import main as assemble_main
from tests.native_capture import canonical, identity
from tests.synthetic_session import PATTERN, SyntheticFeed

pytestmark = pytest.mark.integration

REPO = pathlib.Path(__file__).resolve().parents[2]
CONFIG = str(REPO / "config" / "thetadata_capture.yaml")
SESSION = date(2026, 9, 8)
POLICY = CollectionPolicy(refresh_every_seconds=300)
PATTERN_KEY = canonical(date(2026, 9, 8), 6000, "CALL")
FROZEN_KEY = canonical(date(2026, 9, 9), 6050, "PUT")
UNLISTED_KEY = canonical(date(2026, 9, 9), 6100, "CALL")
STALE = identity(date(2026, 9, 9), 6000, "PUT")
STALE_KEY = canonical(date(2026, 9, 9), 6000, "PUT")
NO_OI = identity(date(2026, 9, 9), 6050, "CALL")
NO_OI_KEY = canonical(date(2026, 9, 9), 6050, "CALL")


def et(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 8, hour, minute, second, tzinfo=EASTERN)


def approval(root: pathlib.Path, clock: FakeClock) -> str:
    return plan_session(CONFIG, output=str(root), now=clock.peek(), policy=POLICY)[
        "session_approval"
    ]["approval_hash"]


@pytest.fixture(scope="module")
def session(tmp_path_factory) -> pathlib.Path:
    """The scripted session; see the module docstring."""
    root = tmp_path_factory.mktemp("assembly") / "session"
    clock = FakeClock(et(9, 29, 30), tick=timedelta(milliseconds=200))
    feed = SyntheticFeed(
        clock,
        root,
        frozen=True,
        conflict={"093100": PATTERN},
        fail={"093200": {OPTION_GREEKS}},
        slow={"093200": 125.0},
        pattern_from="093600",
        stale_in={"093700": STALE},
        unlisted_in=frozenset({"094100"}),
        missing_open_interest=(NO_OI,),
    )
    collect_session(
        CONFIG,
        output=str(root),
        approved=approval(root, clock),
        clock=clock,
        policy=POLICY,
        transport=feed,
        stop_after_label="093800",
    )
    clock.advance(timedelta(minutes=2, seconds=30))
    collect_session(
        CONFIG,
        output=str(root),
        approved=approval(root, clock),
        clock=clock,
        policy=POLICY,
        transport=feed,
        resume=True,
        stop_after_label="094200",
    )
    return root


@pytest.fixture(scope="module")
def assembled(session):
    return assemble_session(session)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory, session):
    out = tmp_path_factory.mktemp("assembly") / "out"
    assert assemble_main([str(session), "--out", str(out), "--label", "unit"]) == 0
    return out


def records(assembled, kind, key):
    return [r for r in assembled.records if r["kind"] == kind and r["key"] == key]


# -- the session itself ----------------------------------------------------------


def test_the_scripted_session_ran_as_scripted(session):
    statuses = [
        (e["label"], e["status"]) for e in read_log(session) if e.get("event") == "SLOT"
    ]
    assert statuses == [
        ("093000", "EXECUTED"),
        ("093100", "EXECUTED"),
        ("093200", "EXECUTED"),
        ("093300", "MISSED_OVERRUN"),
        ("093400", "MISSED_OVERRUN"),
        ("093500", "EXECUTED"),
        ("093600", "EXECUTED"),
        ("093700", "EXECUTED"),
        ("093800", "EXECUTED"),
        ("093900", "MISSED_RESTART_GAP"),
        ("094000", "MISSED_RESTART_GAP"),
        ("094100", "EXECUTED"),
        ("094200", "EXECUTED"),
    ]


def test_the_structure_is_verified_before_any_row_is_read(session):
    verification = verify_session(session)
    assert verification.verified is True
    assert verification.findings == []
    assert [c.label for c in verification.cycles] == [
        "093000",
        "093100",
        "093200",
        "093500",
        "093600",
        "093700",
        "093800",
        "094100",
        "094200",
    ]
    assert verification.orphan_cycle_directories == []
    payload = verification.as_dict()
    assert payload["structure_verified"] is True
    assert (
        payload["intent_sha256"]
        == hashlib.sha256((session / INTENT_NAME).read_bytes()).hexdigest()
    )


# -- merge rules -------------------------------------------------------------------


def test_a_to_b_to_a_yields_three_revisions_of_one_event(assembled):
    revisions = records(assembled, "option_quote", PATTERN_KEY)
    pattern = [
        r for r in revisions if r["lineage"]["cycle"] in {"093600", "093700", "093800"}
    ]
    assert [r["sequence"] for r in pattern] == [0, 1, 2]
    assert len({r["event_at"] for r in pattern}) == 1
    assert [r["data"]["bid"] for r in pattern] == ["12.30", "12.40", "12.30"]
    assert [r["lineage"]["cycle"] for r in pattern] == ["093600", "093700", "093800"]
    available = [datetime.fromisoformat(r["available_at"]) for r in pattern]
    assert available == sorted(available)
    assert available[0] < available[1] < available[2]
    outcomes = assembled.report["merge"]["outcomes_by_kind"]["option_quote"]
    assert outcomes[REVISION] >= 2
    assert outcomes[REVERTED_REVISION] == 1


def test_the_replay_selects_the_applicable_revision_before_validity(bundle, assembled):
    plan = read_json((bundle / "replay-plan.json").read_bytes())
    store, _ = load_events(bundle, plan["sources"])
    pattern = records(assembled, "option_quote", PATTERN_KEY)
    event_at = datetime.fromisoformat(
        next(r for r in pattern if r["lineage"]["cycle"] == "093600")["event_at"]
    )
    b_available = datetime.fromisoformat(
        next(r for r in pattern if r["sequence"] == 1)["available_at"]
    )
    a_again_available = datetime.fromisoformat(
        next(r for r in pattern if r["sequence"] == 2)["available_at"]
    )
    before_b = store.at("option_quote", PATTERN_KEY, b_available - timedelta(seconds=1))
    assert before_b is not None
    assert (before_b.event_at, before_b.sequence, before_b.data["bid"]) == (
        event_at,
        0,
        "12.30",
    )
    at_b = store.at("option_quote", PATTERN_KEY, b_available)
    assert (at_b.sequence, at_b.data["bid"]) == (1, "12.40")
    at_a = store.at("option_quote", PATTERN_KEY, a_again_available)
    assert (at_a.sequence, at_a.data["bid"]) == (2, "12.30")
    assert at_a.event_at == event_at


def test_a_repeated_unchanged_observation_keeps_its_original_availability(assembled):
    frozen = records(assembled, "option_quote", FROZEN_KEY)
    assert len(frozen) == 1
    assert frozen[0]["lineage"]["cycle"] == "093000"
    assert frozen[0]["sequence"] == 0
    first_receipt = datetime.fromisoformat(frozen[0]["available_at"])
    assert first_receipt < et(9, 30, 10).astimezone(UTC)
    outcomes = assembled.report["merge"]["outcomes_by_kind"]["option_quote"]
    # Re-observed in every later cycle that acquired quotes (8 of them).
    assert outcomes[REOBSERVED_UNCHANGED] >= 8


def test_a_re_observed_quote_is_never_made_younger(bundle):
    report = read_json((bundle / "replay-report.json").read_bytes())
    late = [
        d
        for d in report["decisions"]
        if d["decision_at"] >= et(9, 36).astimezone(UTC).isoformat()
    ]
    assert late, "decisions after the frozen quote is a minute old"
    assert all("STALE_OPTION_QUOTE" in d["blocker_counts"] for d in late[:5])


def test_a_late_revision_of_an_older_event_does_not_displace_the_newer_state(
    assembled, bundle
):
    stale = records(assembled, "option_quote", STALE_KEY)
    by_cycle = {r["lineage"]["cycle"]: r for r in stale}
    late = by_cycle["093700"]
    assert late["data"]["bid"] == "99.00"
    assert late["sequence"] == 1, "a revision of the event two cycles back"
    revised_event = late["event_at"]
    original = next(
        r for r in stale if r["event_at"] == revised_event and r["sequence"] == 0
    )
    assert original["lineage"]["cycle"] == "093500"
    newer = by_cycle["093600"]
    assert newer["event_at"] > revised_event
    assert (
        assembled.report["merge"]["outcomes_by_kind"]["option_quote"][LATE_OLDER_EVENT]
        == 1
    )
    plan = read_json((bundle / "replay-plan.json").read_bytes())
    store, _ = load_events(bundle, plan["sources"])
    after = store.at(
        "option_quote", STALE_KEY, datetime.fromisoformat(late["available_at"])
    )
    assert after.event_at == datetime.fromisoformat(newer["event_at"])
    assert after.data["bid"] != "99.00"
    # Yet the revision is available for the older event's own history.
    history = store.available(
        "option_quote", STALE_KEY, datetime.fromisoformat(late["available_at"])
    )
    assert any(e.sequence == 1 and e.data["bid"] == "99.00" for e in history)


def test_an_ambiguous_observation_leaves_the_known_state_standing(assembled, bundle):
    ambiguity = assembled.report["merge"]["ambiguity"]
    assert ambiguity["after_known_state"] == 1
    assert ambiguity["without_known_state"] == 0
    incident = ambiguity["incidents"][0]
    assert incident["cycle"] == "093100"
    assert incident["kind"] == "option_quote"
    assert incident["key"] == PATTERN_KEY
    assert incident["outcome"] == AMBIGUOUS_AFTER_KNOWN_STATE
    assert "nothing revised" in incident["effect"]
    # No record of the identity came out of 093100 and the 093000 record stands.
    pattern = records(assembled, "option_quote", PATTERN_KEY)
    assert "093100" not in {r["lineage"]["cycle"] for r in pattern}
    known = next(r for r in pattern if r["lineage"]["cycle"] == "093000")
    plan = read_json((bundle / "replay-plan.json").read_bytes())
    store, _ = load_events(bundle, plan["sources"])
    at_0932 = store.at("option_quote", PATTERN_KEY, et(9, 31, 30).astimezone(UTC))
    assert at_0932.event_at == datetime.fromisoformat(known["event_at"])
    assert at_0932.sequence == 0
    cycle = next(c for c in assembled.report["cycles"] if c["label"] == "093100")
    assert cycle["conflicting_groups"] == {OPTION_QUOTE: [PATTERN_KEY]}
    assert cycle["excluded"][OPTION_QUOTE]["CONFLICTING_DUPLICATE_OBSERVATIONS"] == 3


def test_open_interest_and_inventory_are_reused_only_through_original_receipts(
    assembled, bundle
):
    cadence = assembled.report["cadence"]
    assert cadence["open_interest_cycles"] == ["093000", "093500"]
    assert cadence["inventory_cycles"] == ["093000", "093500"]
    assert cadence["inventory_events"] == 2
    oi = [r for r in assembled.records if r["kind"] == "open_interest"]
    # Seven identities carry OI (one is missing); the 093500 refresh repeated
    # them unchanged, so there is exactly one record each, from 093000.
    assert len(oi) == 7
    assert {r["lineage"]["cycle"] for r in oi} == {"093000"}
    assert assembled.report["merge"]["outcomes_by_kind"]["open_interest"] == {
        NEW_EVENT: 7,
        REOBSERVED_UNCHANGED: 7,
    }
    assert NO_OI_KEY not in {r["key"] for r in oi}
    assert assembled.report["identities"]["without_open_interest"] == 1
    plan = read_json((bundle / "replay-plan.json").read_bytes())
    store, _ = load_events(bundle, plan["sources"])
    # Between refreshes the state is the 09:30 receipt, never a later one.
    state = store.at("open_interest", PATTERN_KEY, et(9, 34).astimezone(UTC))
    assert state.available_at == datetime.fromisoformat(oi[0]["available_at"])
    assert state.available_at < et(9, 30, 10).astimezone(UTC)
    assert store.at("open_interest", NO_OI_KEY, et(9, 45).astimezone(UTC)) is None
    # A listing is a new event per refresh, available at its own receipt.
    inventories = [r for r in assembled.records if r["kind"] == "contract_list"]
    assert [r["lineage"]["cycle"] for r in inventories] == ["093000", "093500"]
    assert inventories[0]["event_at"] == inventories[0]["available_at"]
    assert store.at(
        "contract_list", "SPXW", et(9, 34).astimezone(UTC)
    ).available_at == (datetime.fromisoformat(inventories[0]["available_at"]))


def test_a_failed_greeks_request_keeps_the_cycles_other_payloads(assembled):
    cycle = next(c for c in assembled.report["cycles"] if c["label"] == "093200")
    assert cycle["run_state"] == "FAILED_PARTIAL_ACQUISITION"
    assert cycle["acquired_endpoints"] == sorted(
        {OPTION_QUOTE, "/v3/index/snapshot/price"}
    )
    assert list(cycle["unacquired_endpoints"]) == [OPTION_GREEKS]
    detail = cycle["unacquired_endpoints"][OPTION_GREEKS]
    assert detail["attempts"] == 1
    assert detail["last_status_code"] == 404
    assert detail["succeeded"] is False
    assert cycle["model_evidence"] == {"emitted": 0, "excluded": {NOT_ACQUIRED: 1}}
    assert cycle["records_by_kind"] == {"option_quote": 8, "spx_price": 1}
    assert cycle["overran_next_boundary"] is True
    assert cycle["duration_seconds"] > 120
    quotes = [r for r in assembled.records if r["lineage"]["cycle"] == "093200"]
    assert {r["kind"] for r in quotes} == {"option_quote", "spx_price"}
    assert assembled.report["session"]["endpoint_failures"] == {OPTION_GREEKS: 1}
    assert assembled.report["session"]["cycles_with_every_scheduled_endpoint"] == 8
    market = next(c for c in assembled.report["cycles"] if c["label"] == "093100")
    assert market["scheduled_endpoints"] == sorted(
        {OPTION_QUOTE, OPTION_GREEKS, "/v3/index/snapshot/price"}
    )
    assert market["unacquired_endpoints"] == {}


def test_a_market_cycle_is_checked_against_the_latest_listing(assembled):
    assert UNLISTED_KEY not in {r["key"] for r in assembled.records}
    assert assembled.report["merge"]["membership"] == {NOT_IN_LATEST_INVENTORY: 1}
    cycle = next(c for c in assembled.report["cycles"] if c["label"] == "094100")
    assert cycle["merge"][NOT_IN_LATEST_INVENTORY] == 1
    assert cycle["inventory_membership_checked"] is False
    full = next(c for c in assembled.report["cycles"] if c["label"] == "093500")
    assert full["inventory_membership_checked"] is True


def test_missed_slots_overruns_and_restarts_are_reported_not_backfilled(assembled):
    session = assembled.report["session"]
    assert session["slots_by_status"] == {
        "EXECUTED": 9,
        "MISSED_OVERRUN": 2,
        "MISSED_RESTART_GAP": 2,
    }
    assert session["cycles_overrunning_a_boundary"] == 1
    assert session["restarts"] == 1
    assert session["cycles_assembled"] == 9
    assert session["cycles_skipped"] == []
    assert session["ended"] == {"status": "INTERRUPTED", "reason": "STOP_AFTER_LABEL"}
    cycles = {r["lineage"]["cycle"] for r in assembled.records}
    assert not cycles & {"093300", "093400", "093900", "094000"}


# -- lineage, schema and determinism ---------------------------------------------


def test_every_record_names_session_cycle_request_payload_row_and_rule(assembled):
    provenance = assembled.provenance
    assert provenance["assembler"] == ASSEMBLER
    assert set(provenance["cycles"]) == {c["label"] for c in assembled.report["cycles"]}
    for record in assembled.records:
        lineage = record["lineage"]
        assert set(lineage) == {
            "raw_sha256",
            "row_index",
            "rule",
            "availability_basis",
            "cycle",
            "request_id",
        }
        cycle = provenance["cycles"][lineage["cycle"]]
        payloads = {p["sha256"]: e for e, p in cycle["payloads"].items()}
        assert lineage["raw_sha256"] in payloads
        assert lineage["request_id"]
        assert lineage["rule"].startswith("thetadata-v3/")
        location = cycle["payloads"][payloads[lineage["raw_sha256"]]]["location"]
        assert location.startswith(f"cycles/{lineage['cycle']}/raw/")
    # The provenance points at bytes that exist and hash as stated.
    for cycle in provenance["cycles"].values():
        for payload in cycle["payloads"].values():
            raw = (assembled_root(assembled) / payload["location"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == payload["sha256"]


def assembled_root(assembled) -> pathlib.Path:
    return pathlib.Path(assembled.report["verification"]["session_root"])


def test_the_document_loads_as_research_events_2_1_36(bundle):
    document = read_json((bundle / "events.json").read_bytes())
    assert document["schema_version"] == SESSION_EVENT_SCHEMA
    assert document["origin"] == "SYNTHETIC"
    plan = read_json((bundle / "replay-plan.json").read_bytes())
    store, receipts = load_events(bundle, plan["sources"])
    assert isinstance(store, EventStore)
    assert receipts[0]["schema_version"] == SESSION_EVENT_SCHEMA
    assert receipts[0]["provenance"]["assembler"] == ASSEMBLER
    assert receipts[0]["records"] == len(document["records"])


def test_assembly_is_deterministic(session, tmp_path):
    first = assemble_session(session)
    second = assemble_session(session)
    assert first.encode() == second.encode()
    copy = tmp_path / "copy"
    shutil.copytree(session, copy)
    third = assemble_session(copy)
    assert third.encode() == first.encode()
    assert third.report["records"] == first.report["records"]


def test_wire_parameters_are_not_promoted_to_economic_inputs(assembled):
    greeks = [r for r in assembled.records if r["kind"] == "greeks"]
    assert greeks
    for record in greeks:
        assert "rate" not in record["data"] or record["data"]["rate"] == 0.042
        assert "resolved_rate" not in record["data"]
        assert "dividend_yield_resolved" not in record["data"]
    models = [r for r in assembled.records if r["kind"] == "model_evidence"]
    assert all("rate_value=0.042" in m["data"]["model_ids"][0] for m in models)
    assert len(models) == 8, "one per cycle that acquired Greeks"


# -- refusals ---------------------------------------------------------------------


def _copy(session: pathlib.Path, tmp_path: pathlib.Path) -> pathlib.Path:
    copy = tmp_path / "session"
    shutil.copytree(session, copy)
    return copy


def test_a_tampered_raw_payload_refuses_the_whole_session(session, tmp_path):
    copy = _copy(session, tmp_path)
    raw = sorted(
        p for p in (copy / "cycles" / "093600" / "raw").rglob("*") if p.is_file()
    )[0]
    raw.write_bytes(raw.read_bytes() + b"x")
    verification = verify_session(copy)
    assert any(
        f.startswith("CYCLE_NOT_VERIFIABLE:093600") for f in verification.findings
    )
    with pytest.raises(SessionAssemblyError, match="not verified"):
        assemble_session(copy)


def test_a_log_that_disagrees_with_a_cycle_refuses(session, tmp_path):
    copy = _copy(session, tmp_path)
    log = copy / LOG_NAME
    lines = log.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        entry = json.loads(line)
        if entry.get("label") == "093700":
            entry["manifest_hash"] = "0" * 64
            lines[index] = json.dumps(entry, sort_keys=True)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    verification = verify_session(copy)
    assert "CYCLE_MANIFEST_DIFFERS_FROM_LOG:093700" in verification.findings
    with pytest.raises(SessionAssemblyError, match="CYCLE_MANIFEST_DIFFERS_FROM_LOG"):
        assemble_session(copy)


def test_a_session_approval_that_does_not_hash_refuses(session, tmp_path):
    copy = _copy(session, tmp_path)
    intent = json.loads((copy / INTENT_NAME).read_text())
    intent["session_approval"]["request_budget"]["requests"] += 1
    (copy / INTENT_NAME).write_text(json.dumps(intent))
    findings = verify_session(copy).findings
    assert "SESSION_APPROVAL_HASH_MISMATCH" in findings
    with pytest.raises(SessionAssemblyError):
        assemble_session(copy)


def test_a_cycle_directory_the_log_does_not_vouch_for_is_reported(session, tmp_path):
    copy = _copy(session, tmp_path)
    shutil.copytree(copy / "cycles" / "093600", copy / "cycles" / "095900")
    verification = verify_session(copy)
    assert verification.orphan_cycle_directories == ["095900"]
    assert verification.verified is True
    assembled = assemble_session(copy)
    assert "095900" not in assembled.provenance["cycles"]
    assert assembled.report["verification"]["orphan_cycle_directories"] == ["095900"]


def test_a_cycle_outside_its_slot_scope_refuses(session, tmp_path):
    copy = _copy(session, tmp_path)
    intent = json.loads((copy / INTENT_NAME).read_text())
    for slot in intent["schedule"]["slots"]:
        if slot["label"] == "093100":
            slot["scope"] = sorted(set(slot["scope"]) | {OPTION_OPEN_INTEREST})
    (copy / INTENT_NAME).write_text(json.dumps(intent))
    findings = verify_session(copy).findings
    assert "SCHEDULE_FINGERPRINT_MISMATCH" in findings
    assert "SLOT_SCOPE_DIFFERS_FROM_SCHEDULE:093100" in findings
    assert "CYCLE_SCOPE_DIFFERS_FROM_SCHEDULE:093100" in findings


# -- request accounting (review of v2.1.36 r2, finding 2) -----------------------


def test_request_activity_is_recounted_from_each_cycle_and_matches_the_log(
    session, assembled
):
    """The session block reports what happened on the wire, summed from slot
    entries that the structure check recounted from each cycle's own report
    and attempt log; it is not the scheduled scope, and it agrees with the
    collector's summary."""
    report = assembled.report
    assert report["schema_version"] == ASSEMBLY_SCHEMA
    assert "requests_issued" not in report["session"]
    requests = report["session"]["requests"]
    collector = json.loads((session / "session-summary.json").read_text())["requests"]
    for key in (
        "scheduled",
        "attempted",
        "http_attempts",
        "http_attempts_failed",
        "with_receipt",
        "acquired",
        "not_attempted",
        "without_receipt",
        "cycles_with_unverified_attempt_evidence",
    ):
        assert requests[key] == collector[key], key
    executed = [e for e in read_log(session) if e.get("status") == "EXECUTED"]
    assert requests["scheduled"] == sum(len(e["scope"]) for e in executed) == 31
    # One scripted 404 (Greeks at 09:32): attempted and answered, not acquired.
    assert requests["attempted"] == requests["with_receipt"] == 31
    assert requests["http_attempts"] == 31
    assert requests["http_attempts_failed"] == 1
    assert requests["acquired"] == 30
    assert requests["without_receipt"] == requests["not_attempted"] == 0
    assert requests["cycles_recounted_from_evidence"] == 9
    assert requests["operator_cancelled_cycles"] == []
    # Every executed cycle carries the same accounting the collector logged
    # for it, recounted here from disk.
    logged = {e["label"]: e["requests"] for e in executed}
    for cycle in report["cycles"]:
        recounted = cycle["requests"]
        assert recounted["basis"] == "CYCLE_REPORT_AND_ATTEMPT_LOG"
        for key, value in recounted.items():
            assert logged[cycle["label"]][key] == value, (cycle["label"], key)
        assert cycle["operator_cancelled"] is False
    greeks_cycle = next(c for c in report["cycles"] if c["label"] == "093200")
    assert greeks_cycle["requests"]["acquired"] == 2
    assert greeks_cycle["requests"]["http_attempts_failed"] == 1
    for stop in report["session"]["stops"]:
        assert "interruption" in stop


def test_the_readiness_report_carries_the_same_request_accounting(bundle, assembled):
    readiness = read_json((bundle / "session-readiness.json").read_bytes())
    assert readiness["schema_version"] == SESSION_READINESS_SCHEMA
    assert "requests_issued" not in readiness["session"]
    assert readiness["session"]["requests"] == assembled.report["session"]["requests"]
    assert readiness["session"]["interruptions"] == []
    markdown = (bundle / "session-readiness.md").read_text(encoding="utf-8")
    assert "Requests: scheduled 31 of the approved budget" in markdown
    assert "HTTP attempts 31 (retries included, 1 failed)" in markdown
    assert "begun without a receipt 0" in markdown
    assert "Interruptions: none" in markdown


def test_a_log_whose_request_accounting_disagrees_with_the_cycle_refuses(
    session, tmp_path
):
    copy = _copy(session, tmp_path)
    log = copy / LOG_NAME
    lines = log.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        entry = json.loads(line)
        if entry.get("label") == "093500":
            entry["requests"]["http_attempts"] += 1
            entry["requests"]["acquired"] -= 1
            lines[index] = json.dumps(entry, sort_keys=True)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    findings = verify_session(copy).findings
    assert "CYCLE_REQUESTS_DIFFER_FROM_LOG:093500:http_attempts,acquired" in findings
    with pytest.raises(SessionAssemblyError, match="CYCLE_REQUESTS_DIFFER_FROM_LOG"):
        assemble_session(copy)


def test_a_shortened_attempt_log_is_noticed_by_the_recount(session, tmp_path):
    copy = _copy(session, tmp_path)
    index = copy / "cycles" / "093600" / "attempts" / "index.jsonl"
    lines = index.read_text(encoding="utf-8").splitlines()
    index.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    findings = verify_session(copy).findings
    assert any(
        f.startswith("CYCLE_REQUESTS_DIFFER_FROM_LOG:093600:") and "http_attempts" in f
        for f in findings
    )
    with pytest.raises(SessionAssemblyError):
        assemble_session(copy)


def test_a_log_without_per_cycle_request_accounting_is_not_assembled(session, tmp_path):
    """A 2.1.36 (r2) log summed scheduled scopes as ``requests_issued``; r3
    refuses to build a stream over a count it cannot recount."""
    copy = _copy(session, tmp_path)
    log = copy / LOG_NAME
    lines = log.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        entry = json.loads(line)
        entry["schema_version"] = "intraday-session-log/2.1.36"
        entry.pop("requests", None)
        lines[index] = json.dumps(entry, sort_keys=True)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    findings = verify_session(copy).findings
    assert "LOG_SCHEMA_UNSUPPORTED:intraday-session-log/2.1.36" in findings
    assert "SLOT_WITHOUT_REQUEST_ACCOUNTING:093000" in findings
    with pytest.raises(SessionAssemblyError, match="LOG_SCHEMA_UNSUPPORTED"):
        assemble_session(copy)


class _InterruptingFeed(SyntheticFeed):
    interrupted = False

    def get(self, url, params, timeout_seconds):
        if self._label() == "093500" and OPTION_QUOTE in url and not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt("simulated operator interrupt in a request")
        return super().get(url, params, timeout_seconds)


def test_a_cancelled_and_resumed_session_assembles_and_reports_the_interruption(
    tmp_path,
):
    """Review finding 1, downstream: the partial capture of the cancelled cycle
    is preserved and assembled, the interruption is reported all the way to
    the readiness report, and the accounting names the request that was in
    flight without a receipt."""
    root = tmp_path / "session"
    clock = FakeClock(et(9, 29, 30), tick=timedelta(milliseconds=200))
    feed = _InterruptingFeed(clock, root)
    with pytest.raises(KeyboardInterrupt):
        collect_session(
            CONFIG,
            output=str(root),
            approved=approval(root, clock),
            clock=clock,
            policy=POLICY,
            transport=feed,
            stop_after_label="095900",
        )
    assert [e["label"] for e in read_log(root) if e.get("event") == "SLOT"] == [
        "093000",
        "093100",
        "093200",
        "093300",
        "093400",
        "093500",
    ]
    clock.advance(timedelta(minutes=1, seconds=20))
    collect_session(
        CONFIG,
        output=str(root),
        approved=approval(root, clock),
        clock=clock,
        policy=POLICY,
        transport=feed,
        resume=True,
        stop_after_label="093700",
    )
    verification = verify_session(root)
    assert verification.findings == []
    assembled = assemble_session(root)
    session = assembled.report["session"]
    assert session["requests"]["operator_cancelled_cycles"] == ["093500"]
    assert session["requests"]["without_receipt"] == 1
    # 09:35 was a FULL slot: after the interrupted quote request, open
    # interest, Greeks and the listing were never begun.
    assert session["requests"]["not_attempted"] == 3
    assert session["requests"]["http_attempts"] == len(feed.calls)
    assert session["slots_by_status"] == {"EXECUTED": 7, "MISSED_RESTART_GAP": 1}
    cancelled = next(c for c in assembled.report["cycles"] if c["label"] == "093500")
    assert cancelled["operator_cancelled"] is True
    assert cancelled["stop_reason"] == "OPERATOR_CANCELLED"
    assert cancelled["requests"]["without_receipt"] == [OPTION_QUOTE]
    assert cancelled["requests"]["not_attempted"] == sorted(
        [OPTION_CONTRACT_LIST, OPTION_GREEKS, OPTION_OPEN_INTEREST]
    )
    assert cancelled["acquired_endpoints"] == ["/v3/index/snapshot/price"]
    interruptions = [s["interruption"] for s in session["stops"] if s["interruption"]]
    assert len(interruptions) == 1
    assert interruptions[0]["slot"] == "093500"
    assert interruptions[0]["phase"] == "REQUEST"
    assert interruptions[0]["partial_capture_preserved"] is True
    out = tmp_path / "out"
    assert assemble_main([str(root), "--out", str(out), "--label", "cancelled"]) == 0
    readiness = read_json((out / "session-readiness.json").read_bytes())
    assert readiness["session"]["interruptions"] == interruptions
    assert readiness["session"]["requests"] == session["requests"]
    markdown = (out / "session-readiness.md").read_text(encoding="utf-8")
    assert (
        "Interruptions: REQUEST at slot 093500 (partial capture preserved)" in markdown
    )
    assert "operator-cancelled cycles ['093500']" in markdown


def test_a_slot_the_one_shot_refused_is_accounted_as_not_started(tmp_path):
    """A cycle directory that already exists makes the one-shot refuse before
    it claims anything: the slot is ``FAILED_TO_START`` with no report, its
    accounting is unknown rather than zero, and the session still assembles,
    judges and summarises with that slot counted as without a report."""
    root = tmp_path / "session"
    clock = FakeClock(et(9, 29, 30), tick=timedelta(milliseconds=200))
    feed = SyntheticFeed(clock, root)
    approved = approval(root, clock)
    plan_session(CONFIG, output=str(root), now=clock.peek(), policy=POLICY)
    stray = root / "cycles" / "093100"
    # Created after the session claims its root: the collector makes cycles/.
    original = feed.get

    def planting(url, params, timeout_seconds):
        if not stray.exists():
            stray.mkdir(parents=True)
            (stray / "stray.txt").write_text("left behind")
        return original(url, params, timeout_seconds)

    feed.get = planting  # type: ignore[method-assign]
    summary = collect_session(
        CONFIG,
        output=str(root),
        approved=approved,
        clock=clock,
        policy=POLICY,
        transport=feed,
        stop_after_label="093200",
    )
    entries = {e["label"]: e for e in read_log(root) if e.get("event") == "SLOT"}
    assert entries["093100"]["status"] == "FAILED_TO_START"
    assert "already exists" in entries["093100"]["error_message"]
    assert entries["093100"]["requests"]["basis"] == "CYCLE_DID_NOT_START"
    assert entries["093100"]["requests"]["attempted"] is None
    assert entries["093100"]["requests"]["scheduled"] == 3
    assert summary["requests"]["cycles_without_a_report"] == 1
    assert summary["requests"]["scheduled"] == 5 + 3 + 3
    assert summary["requests"]["attempted"] == summary["requests"]["http_attempts"] == 8
    assert summary["requests"]["http_attempts"] == len(feed.calls)
    verification = verify_session(root)
    assert verification.findings == []
    assert verification.orphan_cycle_directories == ["093100"]
    out = tmp_path / "out"
    assert assemble_main([str(root), "--out", str(out), "--label", "refused slot"]) == 0
    readiness = read_json((out / "session-readiness.json").read_bytes())
    assert readiness["session"]["requests"]["cycles_without_a_report"] == 1
    assert readiness["session"]["slots_by_status"] == {
        "EXECUTED": 2,
        "FAILED_TO_START": 1,
    }
    from src.replay.pilot_summary import summarize_pilot

    pilot = summarize_pilot(
        [("refused.json", (out / "session-readiness.json").read_bytes())],
        minimum_sessions=1,
    )
    row = pilot["sessions"][0]
    assert row["requests"]["scheduled"] == 11
    assert row["requests"]["attempted"] == row["requests"]["http_attempts"] == 8
    assert row["requests"]["not_attempted"] == 0
    assert row["report_hash_verified"] is True


def test_a_session_without_intent_or_cycles_refuses(tmp_path):
    with pytest.raises(SessionAssemblyError, match="not a session"):
        assemble_session(tmp_path)
    (tmp_path / INTENT_NAME).write_text(json.dumps({"schema_version": "other"}))
    with pytest.raises(SessionAssemblyError, match="unsupported session intent"):
        verify_session(tmp_path)


def test_tampered_lineage_in_the_events_file_refuses_the_replay(bundle, tmp_path):
    copy = tmp_path / "bundle"
    shutil.copytree(bundle, copy)
    document = read_json((copy / "events.json").read_bytes())
    document["records"][0]["lineage"]["cycle"] = "235959"
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    (copy / "events.json").write_bytes(raw)
    plan = read_json((copy / "replay-plan.json").read_bytes())
    plan["sources"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
    (copy / "replay-plan.json").write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="cycle outside the provenance"):
        replay_bundle(copy)
    document["records"][0]["lineage"]["cycle"] = "093000"
    document["records"][0]["lineage"]["raw_sha256"] = "f" * 64
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    (copy / "events.json").write_bytes(raw)
    plan["sources"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
    (copy / "replay-plan.json").write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="outside its cycle"):
        replay_bundle(copy)


def test_receipt_ties_across_cycles_break_deterministically(session, monkeypatch):
    """Two cycles' records at one instant merge in cycle-label order."""
    from src.adapters.thetadata import session_assembly

    real = session_assembly.normalize_capture
    stamp = et(9, 30, 1).astimezone(UTC).isoformat()

    def tied(root, **kwargs):
        result = real(root, **kwargs)
        for record in result.records:
            if record["kind"] == "option_quote" and record["key"] == PATTERN_KEY:
                record["available_at"] = stamp
                record["event_at"] = stamp
        return result

    monkeypatch.setattr(session_assembly, "normalize_capture", tied)
    assembled = assemble_session(session)
    pattern = records(assembled, "option_quote", PATTERN_KEY)
    assert [r["available_at"] for r in pattern] == [stamp] * len(pattern)
    assert [r["sequence"] for r in pattern] == list(range(len(pattern)))
    assert [r["lineage"]["cycle"] for r in pattern] == sorted(
        r["lineage"]["cycle"] for r in pattern
    )


# -- readiness --------------------------------------------------------------------


def test_the_session_readiness_separates_the_option_side_from_the_pilot(bundle):
    readiness = read_json((bundle / "session-readiness.json").read_bytes())
    assert readiness["schema_version"] == SESSION_READINESS_SCHEMA
    assert readiness["label"] == "unit"
    assert readiness["synthetic_only"] is True
    assert readiness["observed_source_origins"] == ["SYNTHETIC"]
    assert readiness["usable_for_intraday_pilot"] is False
    assert {
        "futures_bid_ask_size",
        "futures_instrument_metadata",
        "futures_costs",
    } <= set(readiness["blocking_reasons"])
    assert "multi_session_coverage" in readiness["blocking_reasons"]
    status = {r["requirement"]: r["status"] for r in readiness["requirements"]}
    assert status["option_inventory"] == "PRESENT"
    assert status["option_quotes"] == "PRESENT"
    assert status["iv_model_inputs"] == "PARTIAL"
    assert status["prior_session_open_interest"] == "PARTIAL"
    assert status["receive_times"] == "PARTIAL"
    assert status["futures_costs"] == "MISSING"
    session = readiness["session"]
    assert session["restarts"] == 1
    assert session["cycles_overrunning_a_boundary"] == 1
    assert session["endpoint_failures"] == {OPTION_GREEKS: 1}
    assert session["ambiguity_after_known_state"] == 1
    replay = readiness["replay"]
    assert replay["decisions_with_open_interest_gaps"] == replay["expected_decisions"]
    assert replay["decisions_with_stale_inputs"] > 0
    for flag in (
        "authenticity_verified",
        "normalization_verified",
        "ready_for_backtest",
        "trusted_for_gex",
        "gex_computed",
        "strategy_tested",
        "pnl_computed",
    ):
        assert readiness[flag] is False
    assert readiness["orders_placed"] == 0
    markdown = (bundle / "session-readiness.md").read_text(encoding="utf-8")
    assert markdown == render_session_markdown(readiness)
    assert "Option side: NOT usable" in markdown
    assert "synthetic only: True" in markdown
    assert readiness["generated_from"]["cycles"][0]["label"] == "093000"


def test_readiness_recomputes_from_the_same_inputs(bundle):
    readiness = read_json((bundle / "session-readiness.json").read_bytes())
    assembly = read_json((bundle / "session-assembly.json").read_bytes())
    assert assembly["schema_version"] == ASSEMBLY_SCHEMA
    replay = read_json((bundle / "replay-report.json").read_bytes())
    again = session_readiness(
        assembly,
        replay,
        events_sha256=readiness["generated_from"]["events_sha256"],
        plan_sha256=readiness["generated_from"]["plan_sha256"],
        assembly_sha256=readiness["generated_from"]["assembly_sha256"],
        receipt_clock_tolerance_ms=0,
        label="unit",
    )
    assert again == readiness
    assert (
        readiness["generated_from"]["assembly_sha256"]
        == hashlib.sha256((bundle / "session-assembly.json").read_bytes()).hexdigest()
    )


def test_the_command_refuses_a_used_or_nested_output(session, tmp_path, capsys):
    assert assemble_main([str(session), "--out", str(session / "out")]) == 2
    assert "outside the session" in capsys.readouterr().err
    used = tmp_path / "used"
    used.mkdir()
    assert assemble_main([str(session), "--out", str(used)]) == 2
    assert "unused" in capsys.readouterr().err
    assert assemble_main([str(tmp_path / "nowhere"), "--out", str(tmp_path / "o")]) == 2
    assert "refused" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_scheduled_but_unrequested_endpoints_are_named_not_pretended(assembled):
    """A MARKET cycle says NOT_SCHEDULED for listing and open interest; it
    is never reported as a five-endpoint capture."""
    market = next(c for c in assembled.report["cycles"] if c["label"] == "093100")
    assert OPTION_OPEN_INTEREST not in market["scheduled_endpoints"]
    assert OPTION_CONTRACT_LIST not in market["scheduled_endpoints"]
    from src.adapters.thetadata.research_events import normalize_capture
    from src.ingest.schedule import MARKET_SCOPE

    normalized = normalize_capture(
        assembled_root(assembled) / "cycles" / "093100", expected_endpoints=MARKET_SCOPE
    )
    receipts = {
        endpoint: entry["receipt"]["refusal"]
        for endpoint, entry in normalized.coverage["endpoints"].items()
    }
    assert receipts[OPTION_OPEN_INTEREST] == NOT_SCHEDULED
    assert receipts[OPTION_CONTRACT_LIST] == NOT_SCHEDULED
    assert receipts[OPTION_QUOTE] is None
    with pytest.raises(Exception, match="partial scope"):
        normalize_capture(assembled_root(assembled) / "cycles" / "093100")
