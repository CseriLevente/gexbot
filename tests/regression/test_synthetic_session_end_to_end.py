"""Collection -> assembly -> replay -> readiness -> summary, offline, end to end.

Two clean synthetic sessions (consecutive trading days) are collected on a
fake clock through the fake vendor, assembled with the real command, replayed
with the v2.1.34 replay and summarised. Every artefact must say it is
synthetic: the session intent (``OFFLINE_TRANSPORT``), every cycle
(``OFFLINE_FIXTURE``), the events (``SYNTHETIC``), the readiness and the
summary (``synthetic_only``). The point is that the pipeline can produce
usable option-side decisions from a session -- and that a synthetic session
can never be counted as trading evidence.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, date, datetime, timedelta

import pytest

from src.gex.sessions import EASTERN
from src.ingest.clock import FakeClock
from src.ingest.schedule import CollectionPolicy
from src.ingest.session_collector import collect_session, plan_session, read_log
from src.replay.event_store import read_json
from src.replay.pilot_summary import SUMMARY_SCHEMA, PilotSummaryError, summarize_pilot
from src.tools import collect_intraday_session
from src.tools.assemble_intraday_session import main as assemble_main
from src.tools.summarize_intraday_pilot import main as summarize_main
from tests.synthetic_session import SyntheticFeed

pytestmark = [pytest.mark.regression, pytest.mark.integration]

REPO = pathlib.Path(__file__).resolve().parents[2]
CONFIG = str(REPO / "config" / "thetadata_capture.yaml")
REAL_READINESS = REPO / "docs" / "evidence" / "PILOT_READINESS_2026-09-02.json"
DAYS = (date(2026, 9, 8), date(2026, 9, 9))
STOP_AFTER = "094000"


def collect(root: pathlib.Path, day: date) -> tuple[dict, SyntheticFeed]:
    clock = FakeClock(
        datetime(day.year, day.month, day.day, 9, 29, 30, tzinfo=EASTERN),
        tick=timedelta(milliseconds=200),
    )
    feed = SyntheticFeed(clock, root)
    plan = plan_session(
        CONFIG, output=str(root), now=clock.peek(), policy=CollectionPolicy()
    )
    summary = collect_session(
        CONFIG,
        output=str(root),
        approved=plan["session_approval"]["approval_hash"],
        clock=clock,
        policy=CollectionPolicy(),
        transport=feed,
        stop_after_label=STOP_AFTER,
    )
    return summary, feed


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    base = tmp_path_factory.mktemp("e2e")
    outputs = []
    for day in DAYS:
        root = base / f"session-{day.isoformat()}"
        summary, feed = collect(root, day)
        out = base / f"assembled-{day.isoformat()}"
        code = assemble_main(
            [str(root), "--out", str(out), "--label", f"synthetic {day}"]
        )
        assert code == 0
        outputs.append((day, root, out, summary, feed))
    summary_out = base / "summary"
    code = summarize_main(
        [
            str(outputs[0][2] / "session-readiness.json"),
            str(outputs[1][2] / "session-readiness.json"),
            "--out",
            str(summary_out),
            "--minimum-sessions",
            "2",
            "--label",
            "synthetic pipeline demonstration",
        ]
    )
    assert code == 0
    return outputs, summary_out


def test_collection_issued_only_scheduled_snapshot_requests(pipeline):
    outputs, _ = pipeline
    for _day, root, _out, summary, feed in outputs:
        assert summary["cycles_executed"] == 11
        assert summary["slots_by_status"] == {"EXECUTED": 11}
        assert summary["endpoint_failures"] == {}
        assert summary["requests_issued"] == 5 + 10 * 3
        assert len(feed.calls) == summary["requests_issued"]
        for call in feed.calls:
            assert call.url.startswith("http")
            assert "/v3/" in call.url
            assert "history" not in call.url
            assert "at_time" not in call.url
            assert "at_date" not in call.url
        intent = json.loads((root / "session-intent.json").read_text())
        assert intent["mode"] == "OFFLINE_TRANSPORT"
        assert intent["overrides"] == {
            "allow_out_of_session": False,
            "allow_unsettled": False,
        }
        for entry in read_log(root):
            if entry.get("event") == "SLOT":
                manifest = json.loads(
                    (root / entry["cycle_dir"] / "manifest.json").read_text()
                )
                assert manifest["capture_origin"] == "OFFLINE_FIXTURE"


def test_a_clean_session_yields_usable_option_side_decisions(pipeline):
    outputs, _ = pipeline
    for day, _root, out, _summary, _feed in outputs:
        readiness = read_json((out / "session-readiness.json").read_bytes())
        assert readiness["session_date"] == day.isoformat()
        assert readiness["synthetic_only"] is True
        assert readiness["observed_source_origins"] == ["SYNTHETIC"]
        # Cycles ran 09:30-09:40; decisions 09:35-09:40 have fresh inputs.
        assert readiness["usable_decisions"] >= 5
        assert readiness["option_side_usable"] is True
        assert readiness["option_side_blocking_reasons"] == []
        assert readiness["usable_for_intraday_pilot"] is False
        assert set(readiness["blocking_reasons"]) == {
            "futures_bid_ask_size",
            "futures_instrument_metadata",
            "futures_costs",
            "multi_session_coverage",
        }
        status = {r["requirement"]: r["status"] for r in readiness["requirements"]}
        assert status["prior_session_open_interest"] == "PRESENT"
        assert status["iv_model_inputs"] == "PRESENT"
        assert status["receive_times"] == "PRESENT"
        replay = readiness["replay"]
        # Everything after the last cycle is stale: reported, never backfilled.
        assert replay["decisions_with_stale_inputs"] == (
            replay["expected_decisions"] - readiness["usable_decisions"]
        )
        assert replay["blocker_decision_counts"].get("INVENTORY_NOT_AVAILABLE", 0) == 0
        for flag in ("trusted_for_gex", "gex_computed", "ready_for_backtest"):
            assert readiness[flag] is False
        events = read_json((out / "events.json").read_bytes())
        assert events["origin"] == "SYNTHETIC"
        assert events["schema_version"] == "research-events/2.1.36"
        assert len(events["provenance"]["cycles"]) == 11
        assembly = read_json((out / "session-assembly.json").read_bytes())
        assert assembly["merge"]["outcomes_by_kind"]["open_interest"] == {
            "NEW_EVENT": 8
        }
        assert assembly["merge"]["membership"] == {}
        assert assembly["merge"]["ambiguity"]["incidents"] == []


def test_the_summary_counts_sessions_but_never_calls_synthetic_data_evidence(pipeline):
    _outputs, summary_out = pipeline
    summary = read_json((summary_out / "pilot-summary.json").read_bytes())
    assert summary["schema_version"] == SUMMARY_SCHEMA
    assert summary["totals"]["sessions"] == 2
    assert summary["totals"]["distinct_session_dates"] == 2
    assert summary["totals"]["option_side_usable_sessions"] == 2
    assert summary["totals"]["usable_decisions"] >= 10
    assert summary["minimum_sessions"] == 2
    assert summary["synthetic_only"] is True
    assert summary["option_side_pilot_ready"] is False
    assert summary["option_side_blocking_reasons"] == ["SYNTHETIC_SESSIONS_ONLY"]
    assert summary["usable_for_intraday_pilot"] is False
    assert "SESSIONS_NOT_USABLE_FOR_THE_WHOLE_PILOT" in summary["blocking_reasons"]
    for row in summary["sessions"]:
        assert row["kind"] == "COLLECTION_SESSION"
        assert row["counts_basis"] == "EXACT"
        assert row["coverage"]["cycles_executed"] == 11
        assert row["coverage"]["restarts"] == 0
    markdown = (summary_out / "pilot-summary.md").read_text(encoding="utf-8")
    assert "synthetic only: True" in markdown
    assert "SYNTHETIC_SESSIONS_ONLY" in markdown
    for flag in ("trusted_for_gex", "gex_computed", "pnl_computed", "strategy_tested"):
        assert summary[flag] is False


def test_synthetic_and_recorded_sessions_are_never_summarised_together(pipeline):
    outputs, _ = pipeline
    synthetic = outputs[0][2] / "session-readiness.json"
    with pytest.raises(PilotSummaryError, match="cannot be summarised together"):
        summarize_pilot(
            [
                (synthetic.name, synthetic.read_bytes()),
                (REAL_READINESS.name, REAL_READINESS.read_bytes()),
            ]
        )
    with pytest.raises(PilotSummaryError, match="same session date"):
        summarize_pilot(
            [
                ("a.json", synthetic.read_bytes()),
                ("b.json", synthetic.read_bytes()),
            ]
        )


def test_the_recorded_single_capture_summarises_alone_as_recorded(tmp_path):
    summary = summarize_pilot([(REAL_READINESS.name, REAL_READINESS.read_bytes())])
    assert summary["synthetic_only"] is False
    assert summary["observed_source_origins"] == ["RECORDED_NORMALIZED"]
    row = summary["sessions"][0]
    assert row["kind"] == "SINGLE_CAPTURE"
    assert row["counts_basis"] == "LOWER_BOUND"
    assert row["session_date"] == "2026-09-02"
    assert summary["option_side_pilot_ready"] is False
    assert (
        "FEWER_OPTION_SIDE_USABLE_SESSIONS_THAN_MINIMUM"
        in summary["option_side_blocking_reasons"]
    )
    assert summary["usable_for_intraday_pilot"] is False


def test_the_collector_command_dry_run_prints_both_approvals_and_writes_nothing(
    tmp_path, monkeypatch, capsys
):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 8, 13, 15, tzinfo=UTC).astimezone(tz or UTC)

    monkeypatch.setattr(collect_intraday_session, "datetime", Frozen)
    output = tmp_path / "session"
    code = collect_intraday_session.main(["--config", CONFIG, "--output", str(output)])
    out = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN for session 2026-09-08" in out
    assert "phase now: PREPARATION" in out
    assert "slots 390: first 093000 ET, last 155900 ET" in out
    assert "SESSION APPROVAL: " in out
    assert "per-cycle approval (one-shot): " in out
    assert "--execute-live --approve" in out
    assert "Nothing was sent and nothing was written" in out
    assert not output.exists()


def test_the_collector_command_refuses_live_without_a_matching_approval(
    tmp_path, monkeypatch, capsys
):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 8, 13, 29, 30, tzinfo=UTC).astimezone(tz or UTC)

    monkeypatch.setattr(collect_intraday_session, "datetime", Frozen)
    calls: list[str] = []

    def fake_collect(*args, **kwargs):
        calls.append("collect")
        raise AssertionError("must not be reached")

    # Live mode goes through collect_session, whose approval check runs before
    # any transport is built; here the clock is the wall clock, so the refusal
    # is the date mismatch rather than a request.
    output = tmp_path / "session"
    code = collect_intraday_session.main(
        [
            "--config",
            CONFIG,
            "--output",
            str(output),
            "--execute-live",
            "--approve",
            "0" * 64,
        ]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert "REFUSED" in err
    assert "does not authorise" in err or "not a trading session" in err
    assert not output.exists()
    assert calls == []


def test_show_schedule_needs_no_approval_and_writes_nothing(capsys):
    assert (
        collect_intraday_session.main(
            ["--config", CONFIG, "--show-schedule", "2026-11-27"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "early close: True" in out
    assert "slots 210: first 093000 ET, last 125900 ET" in out
    assert (
        collect_intraday_session.main(
            ["--config", CONFIG, "--show-schedule", "2026-09-13"]
        )
        == 2
    )
    assert "not a trading session" in capsys.readouterr().err
    assert collect_intraday_session.main(["--config", CONFIG]) == 2
    assert "--output is required" in capsys.readouterr().err
