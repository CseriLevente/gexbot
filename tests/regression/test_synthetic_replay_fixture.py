"""The shipped synthetic replay bundle and its report are frozen bytes.

``tests/fixtures/replay/synthetic_2026-09-08/`` is the reproducible v2.1.34
example: one hypothetical SPXW option, one hypothetical ``SIMFUT`` future,
``origin = "SYNTHETIC"`` throughout. The frozen report beside it is what the
offline CLI produced from those bytes. A change to either file is a deliberate
protocol revision that must say why, not a regeneration.

Nothing in this fixture is market data or trading evidence.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from src.replay.event_store import read_json
from src.replay.session import replay_bundle
from src.tools.replay_research_session import main
from tests.synthetic_replay import write_bundle

pytestmark = pytest.mark.regression

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "replay"
BUNDLE = FIXTURES / "synthetic_2026-09-08"
REPORT = FIXTURES / "synthetic_2026-09-08_report.json"

FROZEN_EVENTS_SHA256 = (
    "4f2bbf9984363d7485d639642e9ea7c14f07675d7cf6c40d48f7f9c57d9fcd8b"
)
FROZEN_PLAN_SHA256 = "7de930df6db9c8bb898f30e407d5de1b40921befaef986b316ed5d1b0d7bc9fe"
FROZEN_REPORT_HASH = "67e1fc90ba6b8ed8e1341cfc136c3e9c3e9d7316a6f8422c1c9ae659593010eb"


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_builder_reproduces_the_frozen_bundle_bytes(tmp_path):
    rebuilt = write_bundle(tmp_path / "rebuilt")
    for name in ("events.json", "replay-plan.json"):
        assert (rebuilt / name).read_bytes() == (BUNDLE / name).read_bytes(), name
    assert sha256(BUNDLE / "events.json") == FROZEN_EVENTS_SHA256
    assert sha256(BUNDLE / "replay-plan.json") == FROZEN_PLAN_SHA256
    assert sorted(p.name for p in BUNDLE.iterdir()) == [
        "events.json",
        "replay-plan.json",
    ]


def test_the_frozen_report_is_what_the_frozen_bundle_replays_to():
    frozen = json.loads(REPORT.read_text(encoding="utf-8"))
    assert frozen["report_hash"] == FROZEN_REPORT_HASH
    assert frozen["plan_sha256"] == FROZEN_PLAN_SHA256
    assert [s["source_sha256"] for s in frozen["sources"]] == [FROZEN_EVENTS_SHA256]
    assert replay_bundle(BUNDLE) == frozen


def test_the_frozen_report_bytes_are_deterministic_lf_utf8(tmp_path):
    out = tmp_path / "report.json"
    assert main([str(BUNDLE), "--json", str(out)]) == 0
    assert out.read_bytes() == REPORT.read_bytes()
    raw = REPORT.read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    raw.decode("utf-8")


def test_the_frozen_example_is_labelled_synthetic_and_claims_nothing():
    frozen = read_json(REPORT.read_bytes())
    events = read_json((BUNDLE / "events.json").read_bytes())
    assert events["origin"] == "SYNTHETIC"
    assert frozen["synthetic_only"] is True
    assert frozen["observed_source_origins"] == ["SYNTHETIC"]
    assert frozen["expected_decisions"] == 371
    assert frozen["passing_decisions"] == 2
    assert frozen["blocked_decisions"] == 369
    assert frozen["probe_counts"]["simulated_fills"] == 2
    assert frozen["probe_counts"]["refusal_reasons"] == {"RESEARCH_FRAME_BLOCKED": 1}
    prices = [(p["probe"]["side"], p.get("price")) for p in frozen["fill_probes"]]
    assert prices == [("BUY", "100.50"), ("SELL", "99.75"), ("BUY", None)]
    for flag in (
        "authenticity_verified",
        "normalization_verified",
        "ready_for_backtest",
        "trusted_for_gex",
        "gex_computed",
        "strategy_tested",
        "pnl_computed",
    ):
        assert frozen[flag] is False, flag
    assert frozen["orders_placed"] == 0
    assert all("SIMFUT" in p["probe"]["instrument"] for p in frozen["fill_probes"])
