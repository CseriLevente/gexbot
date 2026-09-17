"""The pilot collection specification is grounded in inspected capabilities.

Every endpoint the specification relies on must exist in the pinned vendor
document; the research contract it names must be the repository default; and
the futures leg must be stated as unsupported, because the pinned document has
no futures path. A specification that drifted from either would be a plan
written against a vendor that does not exist.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
import yaml

from src.replay.research_contract import ResearchContract
from src.replay.session import decision_grid

pytestmark = pytest.mark.replay

REPO = pathlib.Path(__file__).resolve().parents[2]
CONFIG = REPO / "config" / "intraday_pilot.json"
RUNBOOK = REPO / "docs" / "INTRADAY_PILOT_COLLECTION.md"
PINNED = (
    REPO
    / "vendor_documentation"
    / "thetadata"
    / "1b"
    / "1b65f93c879a5ca4477a0ff9177235138e0c81840e0c7dddfbd9e34164b40b50.yaml"
)


@pytest.fixture(scope="module")
def spec():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def pinned_paths():
    document = yaml.safe_load(PINNED.read_bytes())
    return set(document["paths"])


def test_the_specification_is_labelled_unexecuted_and_names_the_default_contract(
    spec,
):
    assert spec["schema_version"] == "intraday-pilot-collection/2.1.36"
    assert spec["status"] == "IMPLEMENTED_OFFLINE_TESTED_NOT_EXECUTED_LIVE"
    assert spec["collection"] == {
        "cadence_seconds": 60,
        "refresh_every_seconds": 1800,
        "start_tolerance_seconds": 5,
        "max_consecutive_failed_cycles": 5,
    }
    assert spec["research_contract"] == ResearchContract().as_dict()
    assert (
        spec["cycle"]["cadence_seconds"] == spec["research_contract"]["cadence_seconds"]
    )
    assert spec["grounding"]["vendor_document_sha256"] == PINNED.stem


def test_every_endpoint_named_exists_in_the_pinned_vendor_document(spec, pinned_paths):
    named = {request["endpoint"] for request in spec["requests"]}
    named |= set(spec["cycle"]["request_order"])
    templates = [
        re.compile("^" + re.sub(r"\{[^/]+\}", "[^/]+", pinned) + "$")
        for pinned in pinned_paths
    ]
    for endpoint in sorted(named):
        assert endpoint.startswith("/v3/")
        path = endpoint.removeprefix("/v3")
        assert any(template.match(path) for template in templates), path
    assert "/calendar/on_date" in pinned_paths
    assert len(pinned_paths) == 66


def test_the_pinned_document_has_no_futures_path_and_the_spec_says_so(
    spec, pinned_paths
):
    assert not [p for p in pinned_paths if "future" in p.lower()]
    assert spec["futures"]["status"] == "UNSUPPORTED_BY_PINNED_SOURCE"
    for kind in ("futures_quote", "instrument", "costs"):
        fields = spec["futures"]["required_fields"][kind]
        assert any("receipt" in field for field in fields), kind
    assert "operational decision outside this repository" in spec["futures"]["action"]


def test_the_cycle_covers_the_whole_research_grid(spec):
    from datetime import date, time

    grid = decision_grid(date(2026, 9, 8), ResearchContract())
    first = time.fromisoformat(spec["cycle"]["first_cycle_et"])
    last = time.fromisoformat(spec["cycle"]["last_cycle_et"])
    from src.gex.sessions import EASTERN

    assert first <= grid[0].astimezone(EASTERN).time()
    assert last >= grid[-1].astimezone(EASTERN).time()
    assert first == time(9, 30), "the calendar open, inside the capture window"
    from src.ingest.schedule import CollectionPolicy, CollectionSchedule

    schedule = CollectionSchedule.build(
        date(2026, 9, 8),
        CollectionPolicy.from_specification(CONFIG),
        max_attempts_per_request=1,
    )
    assert schedule.slots[0].label == first.strftime("%H%M%S")
    assert schedule.slots[-1].label == last.strftime("%H%M%S")
    assert sorted(spec["cycle"]["scopes"]["FULL"]) == sorted(schedule.slots[0].scope)
    assert sorted(spec["cycle"]["scopes"]["MARKET"]) == sorted(schedule.slots[1].scope)
    assert spec["cycle"]["request_order"] == [
        "/v3/index/snapshot/price",
        "/v3/option/snapshot/quote",
        "/v3/option/snapshot/open_interest",
        "/v3/option/snapshot/greeks/first_order",
        "/v3/option/list/contracts/quote",
    ]


def test_receipts_and_recovery_are_specified_without_backfill(spec):
    required = " ".join(spec["receipt_recording"]["required_per_attempt"])
    assert "received_at" in required
    assert "request_started_at" in required
    assert "SHA-256" in required
    assert "never backfilled" in spec["recovery"]["interruption"]
    assert spec["normalization_merge_rule"]["status"] == "IMPLEMENTED"
    for module in spec["normalization_merge_rule"]["tests"].split(", "):
        assert (REPO / module).is_file(), module
    greeks = next(
        r for r in spec["requests"] if r["endpoint"].endswith("greeks/first_order")
    )
    assert greeks["parameters"]["rate_value"] == 0.042
    assert greeks["parameters"]["annual_dividend"] == 0.0


def test_the_runbook_exists_and_points_at_the_configuration():
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "config/intraday_pilot.json" in text
    assert "config/thetadata_intraday.yaml" in text
    assert "not executed live" in text
    assert "No path serves futures" in text
    assert "PILOT_READINESS_2026-09-02.md" in text
    for command in (
        "python -m src.tools.collect_intraday_session",
        "python -m src.tools.assemble_intraday_session",
        "python -m src.tools.summarize_intraday_pilot",
        "--execute-live --approve",
        "--resume",
        "--show-schedule",
    ):
        assert command in text, command


def test_the_intraday_profile_plans_the_specified_requests(spec):
    from datetime import UTC, datetime

    from src.tools.capture_thetadata_once import plan_capture

    plan = plan_capture(
        str(REPO / "config" / "thetadata_intraday.yaml"),
        output=str(REPO.parent / "never-created-intraday-plan"),
        as_of=datetime(2026, 9, 8, 13, 29, tzinfo=UTC),
    )
    assert plan["capture_ready"] is True
    assert plan["capture_readiness"] == "READY_FOR_RAW_CAPTURE_ONLY"
    planned = {
        r["endpoint"]: dict(r["canonical_query_parameters"])
        for r in plan["planned_requests"]["requests"]
    }
    assert list(planned) == spec["cycle"]["request_order"]
    for request in spec["requests"]:
        expected = {
            k: ("2026-09-08" if v == "<session>" else str(v))
            for k, v in request["parameters"].items()
        }
        assert planned[request["endpoint"]] == expected, request["endpoint"]
    assert planned["/v3/option/snapshot/greeks/first_order"]["rate_value"] == "0.042"
