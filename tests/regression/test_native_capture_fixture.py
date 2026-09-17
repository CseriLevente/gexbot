"""The native-schema synthetic capture and its normalized outputs are frozen.

``tests/fixtures/native/synthetic_2026-09-08/`` pins, by digest, every file of
the synthetic native-schema capture that ``tests.native_capture`` writes (the
capture itself is regenerated rather than tracked: captured-payload files are
never committed), together with the exact bytes the v2.1.35 normalizer and
readiness command produce from it. A change to any of these is a deliberate
rule revision that must say why, not a regeneration. Revision history: the
first v2.1.35 cut kept the first row of a repeated identity; after the
independent review the row rules moved to revision 2 (repeated identities are
grouped before emission: identical rows coalesce, disagreeing rows are excluded
whole), the scenario gained a conflicting pair and a verbatim repeat, and the
fixture was regenerated once under those rules. v2.1.36 regenerated it again
for two reasons that leave every row rule at revision 2: the synthetic
capture's preflight approval now names the scenario's own market session date
(the normalizer cross-checks it against the listing date, which a fixed
2026-08-10 stamp failed), and the normalizer identifier moved to
``thetadata-research-events/2.1.36`` with the scope-aware coverage report
(``scheduled_endpoints``, ``acquired_endpoints``, ``request_id`` and
``detail`` on receipts). The records themselves are unchanged except for that
identifier in the provenance.

Everything here is invented and labelled ``SYNTHETIC``; nothing is market data
or trading evidence.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from src.replay.event_store import read_json
from src.replay.session import replay_bundle
from src.tools.normalize_thetadata_capture import main
from tests.native_capture import write_native_capture

pytestmark = pytest.mark.regression

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "native"
FROZEN = FIXTURE / "synthetic_2026-09-08"
FROZEN_EVENTS_SHA256 = (
    "6bffebc571040172a9023b99d65a0b013131b557de21c1c3c9a6d4ec88f08aa3"
)
FROZEN_PLAN_SHA256 = "47c198afa8051b45e9558ea474938c732d9fc02738bbbef1600dce6227b47fb4"
FROZEN_REPLAY_REPORT_HASH = (
    "7e289ef457e62c7fde86e8eb956b000dbd048c819daa0e63588664cb27ee16bf"
)
FROZEN_READINESS_HASH = (
    "2a67899f375bea6c8c5a8ed5e2008f5f1fe2bfe62b917ef76870a9faa148031b"
)
OUTPUTS = (
    "events.json",
    "replay-plan.json",
    "pilot-readiness.json",
    "pilot-readiness.md",
)


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_builder_reproduces_every_frozen_capture_file(tmp_path):
    capture = write_native_capture(tmp_path / "capture")
    rebuilt = {
        str(p.relative_to(capture)).replace("\\", "/"): sha256(p)
        for p in sorted(capture.rglob("*"))
        if p.is_file()
    }
    frozen = json.loads((FROZEN / "capture-sha256.json").read_text(encoding="utf-8"))
    assert rebuilt == frozen
    assert sorted(p.name for p in FROZEN.iterdir()) == [
        "capture-sha256.json",
        *sorted(OUTPUTS),
    ]
    # No captured payload is tracked: the capture lives only in the digests.
    assert not list(FROZEN.rglob("*.raw"))


def test_the_command_reproduces_the_frozen_outputs_byte_for_byte(tmp_path):
    capture = write_native_capture(tmp_path / "capture")
    out = tmp_path / "out"
    assert main([str(capture), "--out", str(out)]) == 0
    for name in OUTPUTS:
        assert (out / name).read_bytes() == (FROZEN / name).read_bytes(), name
    assert sha256(FROZEN / "events.json") == FROZEN_EVENTS_SHA256
    assert sha256(FROZEN / "replay-plan.json") == FROZEN_PLAN_SHA256
    report = read_json((out / "replay-report.json").read_bytes())
    assert report["report_hash"] == FROZEN_REPLAY_REPORT_HASH
    assert replay_bundle(FROZEN) == report


def test_the_frozen_readiness_is_bound_to_the_frozen_bundle():
    readiness = read_json((FROZEN / "pilot-readiness.json").read_bytes())
    assert readiness["report_hash"] == FROZEN_READINESS_HASH
    generated = readiness["generated_from"]
    assert generated["events_sha256"] == FROZEN_EVENTS_SHA256
    assert generated["plan_sha256"] == FROZEN_PLAN_SHA256
    assert generated["replay_report_hash"] == FROZEN_REPLAY_REPORT_HASH
    events = read_json((FROZEN / "events.json").read_bytes())
    assert events["schema_version"] == "research-events/2.1.35"
    assert events["origin"] == "SYNTHETIC"
    assert (
        generated["capture"]["manifest_sha256"]
        == (events["provenance"]["manifest_sha256"])
    )
    plan = read_json((FROZEN / "replay-plan.json").read_bytes())
    assert plan["sources"] == [{"path": "events.json", "sha256": FROZEN_EVENTS_SHA256}]
    assert plan["probes"] == []


def test_the_frozen_example_is_labelled_synthetic_and_claims_nothing():
    readiness = read_json((FROZEN / "pilot-readiness.json").read_bytes())
    assert readiness["synthetic_only"] is True
    assert readiness["observed_source_origins"] == ["SYNTHETIC"]
    assert readiness["usable_for_intraday_pilot"] is False
    assert readiness["usable_decisions"] == 1
    assert readiness["blocked_decisions"] == 370
    assert readiness["blocking_reasons"] == [
        "futures_bid_ask_size",
        "futures_instrument_metadata",
        "futures_costs",
        "multi_session_coverage",
    ]
    for flag in (
        "authenticity_verified",
        "normalization_verified",
        "ready_for_backtest",
        "trusted_for_gex",
        "gex_computed",
        "strategy_tested",
        "pnl_computed",
    ):
        assert readiness[flag] is False, flag
    assert readiness["orders_placed"] == 0
    markdown = (FROZEN / "pilot-readiness.md").read_text(encoding="utf-8")
    assert "synthetic only: True" in markdown
    assert "\r" not in markdown


def test_the_frozen_fixture_exercises_the_repeated_identity_rules():
    readiness = read_json((FROZEN / "pilot-readiness.json").read_bytes())
    endpoints = readiness["coverage"]["endpoints"]
    quote = endpoints["/v3/option/snapshot/quote"]
    assert quote["rule"] == "thetadata-v3/option_quote/2"
    assert quote["excluded"]["CONFLICTING_DUPLICATE_OBSERVATIONS"] == 2
    assert quote["excluded"]["IDENTICAL_DUPLICATE_COALESCED"] == 1
    assert quote["duplicate_groups"]["conflicting_keys"] == [
        "SPXW|2026-09-18|6200|CALL"
    ]
    greeks = endpoints["/v3/option/snapshot/greeks/first_order"]
    assert greeks["excluded"]["CONFLICTING_DUPLICATE_OBSERVATIONS"] == 2
    oi = endpoints["/v3/option/snapshot/open_interest"]
    assert oi["excluded"]["IDENTICAL_DUPLICATE_COALESCED"] == 1
    events = read_json((FROZEN / "events.json").read_bytes())
    keys = {(r["kind"], r["key"]) for r in events["records"]}
    assert ("option_quote", "SPXW|2026-09-18|6200|CALL") not in keys
    assert ("greeks", "SPXW|2026-09-18|6200|CALL") not in keys
    inventory = next(r for r in events["records"] if r["kind"] == "contract_list")
    assert "SPXW|2026-09-18|6200|CALL" in inventory["data"]["contracts"]
    assert events["provenance"]["normalizer"] == "thetadata-research-events/2.1.36"
