"""The bounded session collector, run offline on a fake clock and a fake vendor.

Every cycle here is the real one-shot command with its preflight, per-request
authorisation, manifest, attempt log and verification; only the transport and
the clock are fakes, so nothing is sent and every receipt is reproducible.
Nothing in these sessions is market data.
"""

from __future__ import annotations

import json
import pathlib
from datetime import date, datetime, timedelta

import pytest

from src.adapters.thetadata.capture_certification import (
    INDEX_PRICE,
    OPTION_CONTRACT_LIST,
    OPTION_GREEKS,
    OPTION_OPEN_INTEREST,
    OPTION_QUOTE,
    load_capture,
)
from src.gex.sessions import EASTERN
from src.ingest.clock import FakeClock
from src.ingest.schedule import FULL_SCOPE, MARKET_SCOPE, CollectionPolicy
from src.ingest.session_collector import (
    INTENT_NAME,
    LOCK_NAME,
    SESSION_INTENT_SCHEMA,
    SESSION_LOG_SCHEMA,
    SESSION_SUMMARY_SCHEMA,
    SUMMARY_NAME,
    OperatorInterrupt,
    SessionApproval,
    SessionCollectionError,
    collect_session,
    plan_session,
    read_log,
)
from src.tools import capture_thetadata_once
from tests.synthetic_session import SyntheticFeed

pytestmark = pytest.mark.integration

REPO = pathlib.Path(__file__).resolve().parents[2]
CONFIG = str(REPO / "config" / "thetadata_capture.yaml")
SESSION = date(2026, 9, 8)
TICK = timedelta(milliseconds=200)


def et(hour: int, minute: int, second: int = 0, day: date = SESSION) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=EASTERN)


def clock_at(hour: int, minute: int, second: int = 0, day: date = SESSION) -> FakeClock:
    return FakeClock(et(hour, minute, second, day), tick=TICK)


def approval_for(root: pathlib.Path, clock: FakeClock, policy=None) -> str:
    plan = plan_session(
        CONFIG, output=str(root), now=clock.peek(), policy=policy or CollectionPolicy()
    )
    return plan["session_approval"]["approval_hash"]


def slots(root: pathlib.Path) -> list[dict]:
    return [e for e in read_log(root) if e.get("event") == "SLOT"]


def run(root, clock, feed=None, *, stop_after="093200", policy=None, **kw):
    policy = policy or CollectionPolicy()
    feed = feed or SyntheticFeed(clock, root)
    summary = collect_session(
        CONFIG,
        output=str(root),
        approved=approval_for(root, clock, policy),
        clock=clock,
        policy=policy,
        transport=feed,
        stop_after_label=stop_after,
        **kw,
    )
    return summary, feed


# -- planning ------------------------------------------------------------------


def test_the_dry_run_plans_writes_nothing_and_binds_the_session(tmp_path):
    root = tmp_path / "session"
    clock = clock_at(9, 29)
    plan = plan_session(
        CONFIG, output=str(root), now=clock.peek(), policy=CollectionPolicy()
    )
    assert plan["mode"] == "DRY_RUN"
    assert plan["wrote_files"] is False
    assert plan["would_place_orders"] is False
    assert plan["phase_now"] == "PREPARATION"
    assert plan["session_date"] == "2026-09-08"
    assert plan["capture_ready"] is True
    # The configured expectation; an injected offline transport is recorded
    # per cycle as OFFLINE_FIXTURE and the intent's mode says OFFLINE_TRANSPORT.
    assert plan["expected_capture_origin"] == "LOCAL_TERMINAL_CAPTURE"
    assert plan["schedule"]["slots"][0]["label"] == "093000"
    assert plan["request_budget"]["cycles"] == 390
    assert not root.exists()
    approval = plan["session_approval"]
    recomputed = SessionApproval(
        session_date=SESSION,
        cycle_approval_hash=plan["cycle_approval"]["approval_hash"],
        schedule_fingerprint=plan["schedule"]["fingerprint"],
        destination=str(root.resolve()),
        request_budget=plan["request_budget"],
    )
    assert recomputed.approval_hash == approval["approval_hash"]
    assert recomputed.matches(approval["approval_hash"].upper())
    # Another destination, policy or budget is another approval.
    other = plan_session(
        CONFIG,
        output=str(tmp_path / "elsewhere"),
        now=clock.peek(),
        policy=CollectionPolicy(),
    )
    assert other["session_approval"]["approval_hash"] != approval["approval_hash"]
    other = plan_session(
        CONFIG,
        output=str(root),
        now=clock.peek(),
        policy=CollectionPolicy(refresh_every_seconds=900),
    )
    assert other["session_approval"]["approval_hash"] != approval["approval_hash"]


def test_the_dry_run_on_a_non_trading_day_refuses_a_schedule(tmp_path):
    clock = clock_at(9, 29, day=date(2026, 9, 12))
    plan = plan_session(
        CONFIG, output=str(tmp_path / "s"), now=clock.peek(), policy=CollectionPolicy()
    )
    assert "not a trading session" in plan["schedule_refusal"]
    assert "session_approval" not in plan
    with pytest.raises(SessionCollectionError, match="not a trading session"):
        collect_session(
            CONFIG,
            output=str(tmp_path / "s"),
            approved="anything",
            clock=clock,
            policy=CollectionPolicy(),
            transport=SyntheticFeed(clock, tmp_path / "s"),
        )


# -- refusals before the first request -----------------------------------------


def test_a_session_needs_the_matching_session_approval(tmp_path):
    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    feed = SyntheticFeed(clock, root)
    with pytest.raises(SessionCollectionError, match="requires --approve"):
        collect_session(
            CONFIG,
            output=str(root),
            approved="",
            clock=clock,
            policy=CollectionPolicy(),
            transport=feed,
        )
    wrong = approval_for(tmp_path / "elsewhere", clock)
    with pytest.raises(SessionCollectionError, match="does not authorise this session"):
        collect_session(
            CONFIG,
            output=str(root),
            approved=wrong,
            clock=clock,
            policy=CollectionPolicy(),
            transport=feed,
        )
    # A one-shot (per-cycle) approval is not a session approval either.
    cycle = plan_session(
        CONFIG, output=str(root), now=clock.peek(), policy=CollectionPolicy()
    )["cycle_approval"]["approval_hash"]
    with pytest.raises(SessionCollectionError, match="does not authorise"):
        collect_session(
            CONFIG,
            output=str(root),
            approved=cycle,
            clock=clock,
            policy=CollectionPolicy(),
            transport=feed,
        )
    assert not root.exists()
    assert feed.calls == []


def test_an_approval_from_another_day_or_policy_does_not_carry_over(tmp_path):
    root = tmp_path / "session"
    yesterday = clock_at(9, 29, day=date(2026, 9, 4))
    stale = approval_for(root, yesterday)
    clock = clock_at(9, 29, 30)
    with pytest.raises(SessionCollectionError, match="does not authorise"):
        collect_session(
            CONFIG,
            output=str(root),
            approved=stale,
            clock=clock,
            policy=CollectionPolicy(),
            transport=SyntheticFeed(clock, root),
        )
    approved = approval_for(root, clock)
    with pytest.raises(SessionCollectionError, match="does not authorise"):
        collect_session(
            CONFIG,
            output=str(root),
            approved=approved,
            clock=clock,
            policy=CollectionPolicy(refresh_every_seconds=900),
            transport=SyntheticFeed(clock, root),
        )
    assert not root.exists()


def test_an_existing_destination_is_refused_and_left_alone(tmp_path):
    root = tmp_path / "session"
    root.mkdir()
    (root / "keep.txt").write_text("mine")
    clock = clock_at(9, 29, 30)
    with pytest.raises(SessionCollectionError, match="exists"):
        collect_session(
            CONFIG,
            output=str(root),
            approved=approval_for(root, clock),
            clock=clock,
            policy=CollectionPolicy(),
            transport=SyntheticFeed(clock, root),
        )
    assert sorted(p.name for p in root.iterdir()) == ["keep.txt"]


@pytest.mark.parametrize(
    "clock",
    [clock_at(16, 30), clock_at(15, 59, 6)],
    ids=["after-close", "past-last-slot"],
)
def test_after_the_last_slot_nothing_starts(tmp_path, clock):
    """A session approved for today cannot begin once its last slot has passed.

    (Another day's clock never reaches this check: both approvals bind the
    market session date, so the approval itself stops matching first, as
    ``test_an_approval_from_another_day_or_policy_does_not_carry_over`` shows.)
    """
    root = tmp_path / "session"
    plan = plan_session(
        CONFIG, output=str(root), now=clock.peek(), policy=CollectionPolicy()
    )
    assert plan["phase_now"] == "AFTER_COLLECTION"
    with pytest.raises(SessionCollectionError, match="AFTER_COLLECTION"):
        collect_session(
            CONFIG,
            output=str(root),
            approved=plan["session_approval"]["approval_hash"],
            clock=clock,
            policy=CollectionPolicy(),
            transport=SyntheticFeed(clock, root),
        )
    assert not root.exists()


# -- the happy path --------------------------------------------------------------


def test_each_slot_is_one_verified_one_shot_capture_in_its_scope(tmp_path):
    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    summary, feed = run(root, clock)
    assert summary["schema_version"] == SESSION_SUMMARY_SCHEMA
    assert summary["status"] == "INTERRUPTED"
    assert summary["stop_reason"] == "STOP_AFTER_LABEL"
    assert summary["cycles_executed"] == 3
    assert summary["slots_by_status"] == {"EXECUTED": 3}
    # r3: activity is counted from each cycle's evidence, and on a clean
    # session every count agrees with the scheduled scope and the fake vendor.
    requests = summary["requests"]
    assert requests["scheduled"] == 5 + 3 + 3
    assert requests["attempted"] == requests["with_receipt"] == 11
    assert requests["acquired"] == 11
    assert requests["http_attempts"] == len(feed.calls) == 11
    assert requests["http_attempts_failed"] == 0
    assert requests["not_attempted"] == requests["without_receipt"] == 0
    assert requests["cycles_with_unverified_attempt_evidence"] == 0
    assert requests["cycles_without_a_report"] == 0
    assert summary["interruption"] is None
    assert summary["cycles_with_every_scheduled_endpoint"] == 3
    assert summary["restarts"] == 0
    assert sorted(p.name for p in (root / "cycles").iterdir()) == [
        "093000",
        "093100",
        "093200",
    ]
    assert not (root / LOCK_NAME).exists()
    assert json.loads((root / SUMMARY_NAME).read_text()) == summary
    intent = json.loads((root / INTENT_NAME).read_text())
    assert intent["schema_version"] == SESSION_INTENT_SCHEMA
    assert intent["mode"] == "OFFLINE_TRANSPORT"
    assert intent["overrides"] == {
        "allow_out_of_session": False,
        "allow_unsettled": False,
    }
    assert intent["session_approval"]["approval_hash"] == approval_for(
        root, clock_at(9, 29, 30)
    )
    # The fake vendor saw exactly the scoped requests, in the pipeline's order.
    urls = [call.url for call in feed.calls]
    assert len(urls) == 11
    assert all(
        "/v3/" in url and "history" not in url and "at_time" not in url for url in urls
    )
    assert [e["schema_version"] for e in read_log(root)] == [SESSION_LOG_SCHEMA] * 5
    executed = slots(root)
    assert [e["label"] for e in executed] == ["093000", "093100", "093200"]
    assert executed[0]["scope"] == sorted(FULL_SCOPE)
    assert executed[1]["scope"] == sorted(MARKET_SCOPE)
    for entry in executed:
        assert entry["status"] == "EXECUTED"
        assert entry["run_state"] == "COMPLETED_RAW_VERIFIED"
        assert entry["missing"] == []
        assert entry["overran_next_boundary"] is False
        assert 0 <= entry["start_delay_seconds"] < 5
        capture = load_capture(root / entry["cycle_dir"])
        assert capture.manifest_hash == entry["manifest_hash"]
        assert capture.session_id == entry["capture_session_id"]
        assert list(capture.scheduled_endpoints) == entry["scope"]
        assert capture.market_session_date == SESSION
        assert sorted(capture.record_hashes) == entry["scope"]
        run_intent = json.loads(
            (root / entry["cycle_dir"] / "run-intent.json").read_text()
        )
        assert run_intent["scheduled_endpoints"] == entry["scope"]
        assert run_intent["out_of_session_capture"] is False
        assert (
            run_intent["preflight_approval"]["approval_hash"]
            == intent["cycle_approval"]["approval_hash"]
        )
        # Receipts come from the injected clock, inside the cycle's interval.
        for line in (
            (root / entry["cycle_dir"] / "attempts" / "index.jsonl")
            .read_text()
            .splitlines()
        ):
            attempt = json.loads(line)
            received = datetime.fromisoformat(attempt["received_at"])
            assert (
                datetime.fromisoformat(entry["started_at"])
                <= received
                <= datetime.fromisoformat(entry["finished_at"])
            )
    market = load_capture(root / "cycles" / "093100")
    assert OPTION_OPEN_INTEREST not in market.record_hashes
    assert OPTION_CONTRACT_LIST not in market.record_hashes
    assert {INDEX_PRICE, OPTION_QUOTE, OPTION_GREEKS} == set(market.record_hashes)


def test_a_late_start_records_the_passed_slots_and_never_catches_up(tmp_path):
    root = tmp_path / "session"
    clock = clock_at(9, 33, 10)
    summary, feed = run(root, clock, stop_after="093500")
    entries = slots(root)
    assert [(e["label"], e["status"]) for e in entries] == [
        ("093000", "MISSED_LATE_START"),
        ("093100", "MISSED_LATE_START"),
        ("093200", "MISSED_LATE_START"),
        ("093300", "MISSED_LATE_START"),
        ("093400", "EXECUTED"),
        ("093500", "EXECUTED"),
    ]
    assert entries[3]["late_by_seconds"] > 5
    assert sorted(p.name for p in (root / "cycles").iterdir()) == ["093400", "093500"]
    assert summary["slots_by_status"] == {"EXECUTED": 2, "MISSED_LATE_START": 4}
    # The first executed cycle was a MARKET slot: no listing, no open interest
    # was requested to make up for the missed FULL slot.
    assert OPTION_CONTRACT_LIST not in {
        c.url.split("?")[0] for c in feed.calls if "093000" in c.url
    }
    assert entries[4]["scope"] == sorted(MARKET_SCOPE)


def test_an_overrunning_cycle_misses_the_slots_it_spans(tmp_path):
    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    feed = SyntheticFeed(clock, root, slow={"093100": 125.0})
    summary, _ = run(root, clock, feed, stop_after="093500")
    entries = {e["label"]: e for e in slots(root)}
    assert entries["093100"]["status"] == "EXECUTED"
    assert entries["093100"]["overran_next_boundary"] is True
    assert entries["093100"]["duration_seconds"] > 120
    assert entries["093200"]["status"] == "MISSED_OVERRUN"
    assert entries["093300"]["status"] == "MISSED_OVERRUN"
    assert entries["093200"]["cause"] == "previous cycle finished later"
    assert entries["093400"]["status"] == "EXECUTED"
    assert entries["093400"]["start_delay_seconds"] < 5
    assert summary["cycles_overrunning_a_boundary"] == 1
    assert summary["slots_by_status"] == {"EXECUTED": 4, "MISSED_OVERRUN": 2}
    assert sorted(p.name for p in (root / "cycles").iterdir()) == [
        "093000",
        "093100",
        "093400",
        "093500",
    ]
    # One cycle in flight: the next cycle started only after the slow one ended.
    assert datetime.fromisoformat(
        entries["093400"]["started_at"]
    ) > datetime.fromisoformat(entries["093100"]["finished_at"])


def test_a_restart_resumes_at_the_next_future_slot(tmp_path):
    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    feed = SyntheticFeed(clock, root)
    run(root, clock, feed, stop_after="093100")
    assert (root / SUMMARY_NAME).exists()
    clock.advance(timedelta(minutes=2, seconds=30))
    summary = collect_session(
        CONFIG,
        output=str(root),
        approved=approval_for(root, clock),
        clock=clock,
        policy=CollectionPolicy(),
        transport=feed,
        resume=True,
        stop_after_label="093500",
    )
    statuses = [(e["label"], e["status"]) for e in slots(root)]
    assert statuses == [
        ("093000", "EXECUTED"),
        ("093100", "EXECUTED"),
        ("093200", "MISSED_RESTART_GAP"),
        ("093300", "MISSED_RESTART_GAP"),
        ("093400", "EXECUTED"),
        ("093500", "EXECUTED"),
    ]
    events = [e["event"] for e in read_log(root)]
    assert events.count("SESSION_START") == 1
    assert events.count("RESTART") == 1
    assert events.count("SESSION_END") == 2
    assert summary["restarts"] == 1
    assert summary["cycles_executed"] == 4
    assert summary["slots_by_status"] == {"EXECUTED": 4, "MISSED_RESTART_GAP": 2}
    assert not (root / LOCK_NAME).exists()
    # The earlier cycles were not re-run or touched.
    assert sorted(p.name for p in (root / "cycles").iterdir()) == [
        "093000",
        "093100",
        "093400",
        "093500",
    ]


def test_a_resume_needs_the_same_session_and_no_live_lock(tmp_path):
    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    feed = SyntheticFeed(clock, root)
    with pytest.raises(SessionCollectionError, match="no session to resume"):
        collect_session(
            CONFIG,
            output=str(root),
            approved=approval_for(root, clock),
            clock=clock,
            policy=CollectionPolicy(),
            transport=feed,
            resume=True,
        )
    run(root, clock, feed, stop_after="093000")
    other_policy = CollectionPolicy(refresh_every_seconds=900)
    with pytest.raises(SessionCollectionError, match="must be the same session"):
        collect_session(
            CONFIG,
            output=str(root),
            approved=approval_for(root, clock, other_policy),
            clock=clock,
            policy=other_policy,
            transport=feed,
            resume=True,
        )
    (root / LOCK_NAME).write_text("{}")
    with pytest.raises(SessionCollectionError, match="collector may still own"):
        collect_session(
            CONFIG,
            output=str(root),
            approved=approval_for(root, clock),
            clock=clock,
            policy=CollectionPolicy(),
            transport=feed,
            resume=True,
        )
    # Without --resume an existing session directory is never reused.
    (root / LOCK_NAME).unlink()
    with pytest.raises(SessionCollectionError):
        collect_session(
            CONFIG,
            output=str(root),
            approved=approval_for(root, clock),
            clock=clock,
            policy=CollectionPolicy(),
            transport=feed,
        )
    assert len(slots(root)) == 1


# -- failures ------------------------------------------------------------------


def test_a_failed_endpoint_is_recorded_and_the_cycle_keeps_the_rest(tmp_path):
    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    feed = SyntheticFeed(
        clock,
        root,
        fail={"093000": {OPTION_OPEN_INTEREST}, "093100": {OPTION_GREEKS}},
    )
    summary, _ = run(root, clock, feed, stop_after="093200")
    entries = {e["label"]: e for e in slots(root)}
    assert entries["093000"]["status"] == "EXECUTED"
    assert entries["093000"]["run_state"] == "FAILED_PARTIAL_ACQUISITION"
    assert entries["093000"]["missing"] == [OPTION_OPEN_INTEREST]
    assert entries["093000"]["error_code"] == "VENDOR_HTTP_ERROR"
    assert entries["093000"]["stop_reason"] == "NONE"
    assert entries["093100"]["missing"] == [OPTION_GREEKS]
    assert entries["093200"]["missing"] == []
    assert summary["endpoint_failures"] == {OPTION_GREEKS: 1, OPTION_OPEN_INTEREST: 1}
    assert summary["cycles_with_every_scheduled_endpoint"] == 1
    assert summary["status"] == "INTERRUPTED"
    # The partial cycle still verified what it did acquire.
    capture = load_capture(root / "cycles" / "093000")
    assert sorted(capture.record_hashes) == sorted(FULL_SCOPE - {OPTION_OPEN_INTEREST})
    capture = load_capture(root / "cycles" / "093100", require_greeks_request=False)
    assert sorted(capture.record_hashes) == sorted(MARKET_SCOPE - {OPTION_GREEKS})


def test_consecutive_empty_cycles_stop_the_session(tmp_path):
    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    everything = set(FULL_SCOPE)
    feed = SyntheticFeed(
        clock,
        root,
        fail={f"09{m:02d}00": everything for m in range(31, 40)},
    )
    policy = CollectionPolicy(max_consecutive_failed_cycles=3)
    summary, _ = run(root, clock, feed, stop_after="095900", policy=policy)
    assert summary["status"] == "STOPPED"
    assert summary["stop_reason"] == "CONSECUTIVE_FAILED_CYCLES"
    assert [e["label"] for e in slots(root)] == ["093000", "093100", "093200", "093300"]
    stop = next(e for e in read_log(root) if e.get("event") == "STOP")
    assert stop["consecutive_failed_cycles"] == 3
    assert summary["cycles_executed"] == 4


def test_a_systemic_refusal_stops_the_session_at_once(tmp_path):
    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    feed = SyntheticFeed(clock, root, reject=frozenset({"093100"}))
    summary, _ = run(root, clock, feed, stop_after="095900")
    assert summary["status"] == "STOPPED"
    assert summary["stop_reason"] == "AUTHENTICATION_REJECTED"
    entries = slots(root)
    assert [e["label"] for e in entries] == ["093000", "093100"]
    assert entries[1]["stop_reason"] == "AUTHENTICATION_REJECTED"
    assert entries[1]["acquired"] == []
    # The rejected cycle issued at most its scoped requests before stopping.
    assert len(feed.calls) <= 5 + 3


def test_the_budget_bounds_the_session(tmp_path, monkeypatch):
    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    feed = SyntheticFeed(clock, root)
    # Pretend the approved budget allowed two cycles: the third slot stops.
    import src.ingest.session_collector as collector

    original = collector.CollectionSchedule.request_budget

    def two_cycles(self):
        budget = dict(original.fget(self))
        budget["cycles"] = 2
        return budget

    monkeypatch.setattr(
        collector.CollectionSchedule, "request_budget", property(two_cycles)
    )
    summary, _ = run(root, clock, feed, stop_after="095900")
    assert summary["status"] == "STOPPED"
    assert summary["stop_reason"] == "BUDGET_EXHAUSTED"
    assert [(e["label"], e["status"]) for e in slots(root)] == [
        ("093000", "EXECUTED"),
        ("093100", "EXECUTED"),
        ("093200", "STOPPED"),
    ]


# -- operator interruption (review of v2.1.36 r2, finding 1) --------------------
#
# Wherever the operator strikes -- between cycles, during a wait, in the middle
# of a request, or in the moments before a cycle's first request -- the session
# takes no later-cycle request, logs a STOP with where it was interrupted,
# writes its summary, releases its lock and raises one OperatorInterrupt.
# Continuing is an explicit --resume, which never retakes a logged slot.


def _assert_cleanly_interrupted(root, *, slot, phase, partial):
    log = read_log(root)
    stop = [e for e in log if e.get("event") == "STOP"]
    assert len(stop) == 1
    assert stop[0]["reason"] == "OPERATOR_INTERRUPT"
    assert stop[0]["interruption"]["slot"] == slot
    assert stop[0]["interruption"]["phase"] == phase
    assert stop[0]["interruption"]["partial_capture_preserved"] is partial
    assert "--resume" in stop[0]["interruption"]["continuing_requires"]
    assert log[-1]["event"] == "SESSION_END"
    assert log[-1]["status"] == "INTERRUPTED"
    assert log[-1]["reason"] == "OPERATOR_INTERRUPT"
    assert log.index(stop[0]) == len(log) - 2
    assert not (root / LOCK_NAME).exists()
    summary = json.loads((root / SUMMARY_NAME).read_text())
    assert summary["status"] == "INTERRUPTED"
    assert summary["stop_reason"] == "OPERATOR_INTERRUPT"
    assert summary["interruption"] == stop[0]["interruption"]
    return log, summary


def test_an_operator_interrupt_between_cycles_is_logged_and_the_lock_released(
    tmp_path,
):
    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    feed = SyntheticFeed(clock, root)
    seen: list[str] = []

    def interrupt(entry):
        seen.append(entry.get("label", entry["event"]))
        if entry.get("label") == "093100":
            raise KeyboardInterrupt

    with pytest.raises(OperatorInterrupt) as raised:
        collect_session(
            CONFIG,
            output=str(root),
            approved=approval_for(root, clock),
            clock=clock,
            policy=CollectionPolicy(),
            transport=feed,
            on_entry=interrupt,
        )
    assert raised.value.phase == "BETWEEN_CYCLES"
    assert raised.value.label == "093100"
    _assert_cleanly_interrupted(
        root, slot="093100", phase="BETWEEN_CYCLES", partial=False
    )
    assert [e["label"] for e in slots(root)] == ["093000", "093100"]
    # No request of a later cycle: exactly the two executed cycles' requests.
    assert len(feed.calls) == 5 + 3
    assert seen[:3] == ["SESSION_START", "093000", "093100"]


class InterruptingFeed(SyntheticFeed):
    """Raises KeyboardInterrupt on the quote request of one cycle, once."""

    interrupt_in: str = "093100"
    interrupted: bool = False

    def get(self, url, params, timeout_seconds):
        if (
            self._label() == self.interrupt_in
            and OPTION_QUOTE in url
            and not self.interrupted
        ):
            self.interrupted = True
            raise KeyboardInterrupt("simulated operator interrupt in a request")
        return super().get(url, params, timeout_seconds)


def test_an_interrupt_during_a_request_stops_the_session_and_keeps_the_partial(
    tmp_path,
):
    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    feed = InterruptingFeed(clock, root)
    with pytest.raises(OperatorInterrupt) as raised:
        run(root, clock, feed, stop_after="095900")
    assert feed.interrupted
    assert (raised.value.label, raised.value.phase) == ("093100", "REQUEST")
    assert raised.value.partial_capture is True
    _assert_cleanly_interrupted(root, slot="093100", phase="REQUEST", partial=True)
    entries = slots(root)
    # The interrupted cycle is logged as the one-shot reported it, and no
    # later cycle executed: the 09:32 slot was never taken.
    assert [(e["label"], e["status"]) for e in entries] == [
        ("093000", "EXECUTED"),
        ("093100", "EXECUTED"),
    ]
    cancelled = entries[1]
    assert cancelled["stop_reason"] == "OPERATOR_CANCELLED"
    assert cancelled["operator_cancelled"] is True
    assert cancelled["run_state"] == "FAILED_PARTIAL_ACQUISITION"
    assert cancelled["acquired"] == [INDEX_PRICE]
    assert entries[0]["operator_cancelled"] is False
    # The partial capture is preserved: manifest, attempt log, the one payload.
    cycle = root / "cycles" / "093100"
    assert (cycle / "manifest.json").is_file()
    assert (cycle / "attempts" / "index.jsonl").is_file()
    capture = load_capture(cycle, require_greeks_request=False)
    assert sorted(capture.record_hashes) == [INDEX_PRICE]
    # The accounting says what happened on the wire: the index print was
    # requested and received, the quote request was begun and has no receipt
    # (it was in flight), the Greeks request was never begun.
    requests = cancelled["requests"]
    assert requests["scheduled"] == 3
    assert requests["attempted"] == 2
    assert requests["http_attempts"] == requests["with_receipt"] == 1
    assert requests["acquired"] == 1
    assert requests["without_receipt"] == [OPTION_QUOTE]
    assert requests["not_attempted"] == [OPTION_GREEKS]
    assert requests["attempt_evidence_verified"] is True
    # The fake vendor saw the first cycle and the index print only.
    assert len(feed.calls) == 5 + 1
    summary = json.loads((root / SUMMARY_NAME).read_text())
    assert summary["requests"]["http_attempts"] == 6
    assert summary["requests"]["without_receipt"] == 1
    assert summary["requests"]["not_attempted"] == 1
    assert summary["interruption"]["cycle_dir"] == "cycles/093100"
    # Continuing is an explicit resume: the cancelled slot is not retaken, the
    # slots that passed meanwhile are missed, collection goes on from there.
    clock.advance(timedelta(minutes=2, seconds=20))
    resumed = collect_session(
        CONFIG,
        output=str(root),
        approved=approval_for(root, clock),
        clock=clock,
        policy=CollectionPolicy(),
        transport=feed,
        resume=True,
        stop_after_label="093400",
    )
    assert [(e["label"], e["status"]) for e in slots(root)] == [
        ("093000", "EXECUTED"),
        ("093100", "EXECUTED"),
        ("093200", "MISSED_RESTART_GAP"),
        ("093300", "MISSED_RESTART_GAP"),
        ("093400", "EXECUTED"),
    ]
    assert resumed["restarts"] == 1
    assert resumed["requests"]["http_attempts"] == len(feed.calls) == 9
    assert resumed["interruption"] is None
    assert not (root / LOCK_NAME).exists()


class InterruptibleClock(FakeClock):
    """A fake clock whose wait for one slot is interrupted by the operator."""

    def __init__(self, *args, interrupt_before, **kwargs):
        super().__init__(*args, **kwargs)
        self.interrupt_before = interrupt_before
        self.interrupted = False

    def sleep_until(self, moment):
        if moment == self.interrupt_before and not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt("simulated operator interrupt while waiting")
        super().sleep_until(moment)


def test_an_interrupt_during_a_wait_takes_no_request_of_that_slot(tmp_path):
    root = tmp_path / "session"
    clock = InterruptibleClock(et(9, 29, 30), tick=TICK, interrupt_before=et(9, 31))
    feed = SyntheticFeed(clock, root)
    with pytest.raises(OperatorInterrupt) as raised:
        run(root, clock, feed, stop_after="095900")
    assert clock.interrupted
    assert (raised.value.label, raised.value.phase) == ("093100", "WAIT")
    assert raised.value.partial_capture is False
    _assert_cleanly_interrupted(root, slot="093100", phase="WAIT", partial=False)
    # Only the 09:30 cycle ran; nothing of 09:31 was requested or claimed.
    assert [e["label"] for e in slots(root)] == ["093000"]
    assert len(feed.calls) == 5
    assert sorted(p.name for p in (root / "cycles").iterdir()) == ["093000"]
    summary = json.loads((root / SUMMARY_NAME).read_text())
    assert summary["requests"]["scheduled"] == 5
    assert summary["requests"]["http_attempts"] == 5
    assert summary["interruption"]["cycle_dir"] is None


def test_an_interrupt_before_a_cycles_first_request_still_stops_the_session(
    tmp_path, monkeypatch
):
    """The one-shot turns an interrupt between claiming the cycle directory and
    its first request into a bootstrap-failure report (no manifest, no
    request). The session must read that report as the operator's stop."""
    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    feed = SyntheticFeed(clock, root)
    original = capture_thetadata_once._write_intent

    def interrupting(run, *, config_path):
        if run.destination.name == "093100":
            raise KeyboardInterrupt("simulated operator interrupt before a request")
        return original(run, config_path=config_path)

    monkeypatch.setattr(capture_thetadata_once, "_write_intent", interrupting)
    with pytest.raises(OperatorInterrupt) as raised:
        run(root, clock, feed, stop_after="095900")
    assert (raised.value.label, raised.value.phase) == ("093100", "REQUEST")
    assert raised.value.partial_capture is False
    _assert_cleanly_interrupted(root, slot="093100", phase="REQUEST", partial=False)
    entries = slots(root)
    assert [(e["label"], e["status"]) for e in entries] == [
        ("093000", "EXECUTED"),
        ("093100", "FAILED_TO_START"),
    ]
    stopped = entries[1]
    assert stopped["operator_cancelled"] is True
    assert stopped["error_code"] == "INTERNAL_ERROR:KeyboardInterrupt"
    assert stopped["run_state"] == "FAILED_BEFORE_REQUEST"
    assert stopped["report_path"] == "capture-bootstrap-failure.json"
    assert (root / "cycles" / "093100" / "capture-bootstrap-failure.json").is_file()
    assert not (root / "cycles" / "093100" / "manifest.json").exists()
    assert stopped["requests"]["attempted"] == 0
    assert stopped["requests"]["http_attempts"] == 0
    assert stopped["requests"]["not_attempted"] == sorted(MARKET_SCOPE)
    assert stopped["requests"]["basis"] == "BOOTSTRAP_FAILURE_REPORT_BEFORE_ANY_REQUEST"
    assert len(feed.calls) == 5
    summary = json.loads((root / SUMMARY_NAME).read_text())
    assert summary["cycles_executed"] == 1
    assert summary["requests"]["scheduled"] == 8
    assert summary["requests"]["http_attempts"] == 5
    assert summary["requests"]["cycles_with_unverified_attempt_evidence"] == 0
    assert summary["requests"]["cycles_without_a_report"] == 0


# -- request accounting (review of v2.1.36 r2, finding 2) -----------------------
#
# ``requests`` distinguishes the scheduled scope from what happened on the wire:
# logical requests begun, HTTP attempts (retries included), receipts, verified
# payloads, requests never begun and requests begun without a receipt. Every
# count is read from the cycle's own report and attempt log, never inferred
# from the schedule; the fake vendor's call count is the check.


def test_a_first_request_rejection_counts_one_attempt_not_the_whole_scope(
    tmp_path,
):
    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    feed = SyntheticFeed(clock, root, reject=frozenset({"093000"}))
    summary, _ = run(root, clock, feed, stop_after="095900")
    assert summary["stop_reason"] == "AUTHENTICATION_REJECTED"
    assert len(feed.calls) == 1
    requests = summary["requests"]
    assert requests["scheduled"] == 5
    assert requests["attempted"] == 1
    assert requests["http_attempts"] == 1
    assert requests["http_attempts_failed"] == 1
    assert requests["with_receipt"] == 1
    assert requests["acquired"] == 0
    assert requests["not_attempted"] == 4
    assert requests["without_receipt"] == 0
    entry = slots(root)[0]
    assert entry["requests"]["attempted"] == 1
    assert len(entry["requests"]["not_attempted"]) == 4
    assert entry["requests"]["without_receipt"] == []


def test_partial_acquisition_counts_attempts_receipts_and_payloads_apart(tmp_path):
    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    feed = SyntheticFeed(clock, root, fail={"093000": {OPTION_OPEN_INTEREST}})
    summary, _ = run(root, clock, feed, stop_after="093000")
    entry = slots(root)[0]
    requests = entry["requests"]
    # Every scheduled request was attempted once and answered (a 404 is a
    # receipt); one answer carried no usable payload.
    assert requests["scheduled"] == requests["attempted"] == 5
    assert requests["http_attempts"] == requests["with_receipt"] == 5
    assert requests["http_attempts_failed"] == 1
    assert requests["acquired"] == 4
    assert requests["not_attempted"] == requests["without_receipt"] == []
    assert len(feed.calls) == 5
    assert summary["requests"]["acquired"] == 4
    assert summary["endpoint_failures"] == {OPTION_OPEN_INTEREST: 1}


def test_retries_are_counted_as_http_attempts_not_as_requests(tmp_path):
    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    feed = SyntheticFeed(
        clock, root, flaky={"093000": {OPTION_QUOTE: 2, INDEX_PRICE: 1}}
    )
    summary, _ = run(root, clock, feed, stop_after="093100")
    entries = slots(root)
    first = entries[0]["requests"]
    # Two 503s on the quote, one on the index print, then success: five
    # logical requests, eight HTTP attempts, three of them failed.
    assert first["scheduled"] == first["attempted"] == first["acquired"] == 5
    assert first["with_receipt"] == 5
    assert first["http_attempts"] == 8
    assert first["http_attempts_failed"] == 3
    assert entries[0]["missing"] == []
    second = entries[1]["requests"]
    assert second["http_attempts"] == second["attempted"] == 3
    assert summary["requests"]["http_attempts"] == len(feed.calls) == 11
    assert summary["requests"]["attempted"] == 8
    assert summary["requests"]["http_attempts_failed"] == 3
    assert summary["request_budget"]["requests"] > summary["requests"]["scheduled"]
    assert (
        summary["request_budget"]["max_attempts"]
        >= summary["requests"]["http_attempts"]
    )


def test_a_transport_failure_without_a_response_is_still_an_attempt(tmp_path):
    from src.adapters.transport import TransportError

    class Dropping(SyntheticFeed):
        dropped = False

        def get(self, url, params, timeout_seconds):
            if self._label() == "093000" and OPTION_GREEKS in url and not self.dropped:
                self.dropped = True
                self.calls.append(None)
                raise TransportError("simulated timeout before any response")
            return super().get(url, params, timeout_seconds)

    root = tmp_path / "session"
    clock = clock_at(9, 29, 30)
    feed = Dropping(clock, root)
    summary, _ = run(root, clock, feed, stop_after="093000")
    requests = slots(root)[0]["requests"]
    assert requests["attempted"] == requests["acquired"] == 5
    assert requests["http_attempts"] == 6
    assert requests["http_attempts_failed"] == 1
    assert requests["with_receipt"] == 5
    assert requests["without_receipt"] == []
    assert summary["requests"]["http_attempts"] == len(feed.calls) == 6
