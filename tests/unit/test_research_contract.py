"""Time-of-availability controls must precede any intraday performance claim."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta

import pytest

from src.gex.sessions import EASTERN
from src.replay.research_contract import (
    REQUIRED_INPUTS,
    Observation,
    ResearchContract,
    ResearchFrame,
    audit_document,
    audit_frame,
)
from src.tools.audit_research_inputs import main


def frame():
    # Tuesday follows Labor Day. OI must refer to Friday, not Monday.
    decision = datetime(2026, 9, 8, 10, tzinfo=EASTERN)
    inputs = tuple(
        Observation(kind, decision - timedelta(seconds=1), decision, "a" * 64)
        for kind in sorted(REQUIRED_INPUTS)
    )
    return ResearchFrame(
        "SPXW|2026-09-08|6000|CALL",
        decision,
        "SPXW",
        decision.date(),
        0,
        date(2026, 9, 4),
        inputs,
    )


def document(f=None):
    f = f or frame()
    raw = asdict(f)
    raw["decision_at"] = f.decision_at.isoformat()
    raw["expiration"] = f.expiration.isoformat()
    raw["oi_as_of"] = f.oi_as_of.isoformat() if f.oi_as_of else None
    raw["observations"] = [
        {
            **asdict(o),
            "event_at": o.event_at.isoformat(),
            "available_at": o.available_at.isoformat(),
        }
        for o in f.observations
    ]
    return {"contract": ResearchContract().as_dict(), "frames": [raw]}


def audit(f):
    return audit_frame(f, ResearchContract())


def test_explicit_zero_after_holiday_passes_declarations_but_does_not_verify_bytes():
    result = audit_document(document())
    assert result["passing_declarations"] == 1
    assert not result["frames"][0]["blockers"]
    assert result["provenance_verified"] is False
    assert result["ready_for_backtest"] is False
    assert result["frames"][0]["open_interest"] == 0


@pytest.mark.parametrize("kind", ["greeks", "open_interest", "model_evidence"])
def test_old_event_does_not_hide_late_arrival_or_retrospective_model_selection(kind):
    f = frame()
    observations = tuple(
        replace(o, available_at=f.decision_at + timedelta(days=2))
        if o.kind == kind
        else o
        for o in f.observations
    )
    assert (
        f"NOT_YET_AVAILABLE_{kind.upper()}"
        in audit(replace(f, observations=observations))["blockers"]
    )


def test_future_event_is_refused_even_if_declared_available_now():
    f = frame()
    obs = replace(f.observations[0], event_at=f.decision_at + timedelta(seconds=1))
    blockers = audit(replace(f, observations=(obs, *f.observations[1:])))["blockers"]
    assert "EVENT_AFTER_AVAILABILITY_CONTRACT_LIST" in blockers
    assert "FUTURE_EVENT_CONTRACT_LIST" in blockers


def test_absent_oi_is_not_zero_and_same_day_or_calendar_yesterday_is_not_prior_session():
    f = frame()
    assert "OI_UNAVAILABLE" in audit(replace(f, open_interest=None))["blockers"]
    for wrong in (None, date(2026, 9, 7), date(2026, 9, 8)):
        assert (
            "OI_NOT_PRIOR_COMPLETED_SESSION"
            in audit(replace(f, oi_as_of=wrong))["blockers"]
        )


@pytest.mark.parametrize("seconds", [61, 3600])
def test_stale_market_inputs_cannot_be_refreshed_by_the_receive_timestamp(seconds):
    f = frame()
    observations = tuple(
        replace(o, event_at=f.decision_at - timedelta(seconds=seconds))
        if o.kind == "greeks"
        else o
        for o in f.observations
    )
    blockers = audit(replace(f, observations=observations))["blockers"]
    assert "STALE_GREEKS" in blockers
    assert "MARKET_INPUTS_MISALIGNED" in blockers


def test_exact_skew_and_age_boundaries_are_inclusive():
    f = frame()
    observations = tuple(
        replace(
            o,
            event_at=f.decision_at
            - timedelta(seconds=60 if o.kind == "greeks" else 58),
        )
        for o in f.observations
    )
    assert audit(replace(f, observations=observations))["declared_checks_passed"]


def test_early_close_changes_window_and_grid():
    f = frame()
    decision = datetime(2026, 11, 27, 12, 45, tzinfo=EASTERN)
    f = replace(
        f,
        decision_at=decision,
        expiration=decision.date(),
        oi_as_of=date(2026, 11, 25),
        observations=tuple(
            replace(o, event_at=decision, available_at=decision) for o in f.observations
        ),
    )
    assert audit(f)["declared_checks_passed"]
    assert (
        "OUTSIDE_RESEARCH_WINDOW"
        in audit(replace(f, decision_at=decision + timedelta(minutes=1)))["blockers"]
    )
    assert (
        "OFF_DECISION_GRID"
        in audit(replace(f, decision_at=decision - timedelta(seconds=1)))["blockers"]
    )


@pytest.mark.parametrize(
    "change,blocker",
    [
        ({"root": "SPX"}, "OUTSIDE_FIXED_UNIVERSE"),
        ({"expiration": date(2026, 9, 16)}, "OUTSIDE_FIXED_UNIVERSE"),
        ({"expiration": date(2026, 9, 7)}, "OUTSIDE_FIXED_UNIVERSE"),
        (
            {"decision_at": datetime(2026, 9, 6, 10, tzinfo=EASTERN)},
            "NOT_TRADING_SESSION",
        ),
        (
            {"decision_at": datetime(2026, 9, 8, 9, 34, tzinfo=EASTERN)},
            "OUTSIDE_RESEARCH_WINDOW",
        ),
    ],
)
def test_fixed_scope_and_calendar_are_enforced(change, blocker):
    assert blocker in audit(replace(frame(), **change))["blockers"]


def test_missing_duplicate_and_unknown_sources_are_not_silently_joined():
    f = frame()
    assert (
        "MISSING_CONTRACT_LIST"
        in audit(replace(f, observations=f.observations[1:]))["blockers"]
    )
    assert (
        "DUPLICATE_CONTRACT_LIST"
        in audit(replace(f, observations=(*f.observations, f.observations[0])))[
            "blockers"
        ]
    )
    with pytest.raises(ValueError, match="unknown"):
        replace(f.observations[0], kind="unexpected")


def test_instant_and_row_order_equivalence_give_identical_hashes():
    f = frame()
    shifted = replace(
        f,
        decision_at=f.decision_at.astimezone(UTC),
        observations=tuple(
            replace(
                o,
                event_at=o.event_at.astimezone(UTC),
                available_at=o.available_at.astimezone(UTC),
            )
            for o in reversed(f.observations)
        ),
    )
    assert audit(f) == audit(shifted)


@pytest.mark.parametrize("value", [True, -1, 0.5])
def test_invalid_oi_cannot_coerce_to_valid_quantity(value):
    with pytest.raises(ValueError):
        replace(frame(), open_interest=value)


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "unknown"},
        {"root": "OTHER"},
        {"max_dte": -1},
        {"cadence_seconds": 0},
        {"max_market_age_seconds": float("nan")},
        {"max_market_skew_seconds": -1},
    ],
)
def test_invalid_research_design_is_refused(change):
    with pytest.raises(ValueError):
        ResearchContract(**change)


def test_naive_time_empty_identity_and_malformed_digest_are_refused():
    f = frame()
    for change in (
        {"decision_at": f.decision_at.replace(tzinfo=None)},
        {"identity": ""},
    ):
        with pytest.raises(ValueError):
            replace(f, **change)
    with pytest.raises(ValueError, match="SHA-256"):
        replace(f.observations[0], source_sha256="looks plausible")


def test_strict_document_rejects_unknown_fields_and_duplicate_instants():
    d = document()
    missing_design = document()
    del missing_design["contract"]["max_dte"]
    with pytest.raises(ValueError, match="whole design"):
        audit_document(missing_design)
    for changed in ({**d, "other": 1}, {**d, "frames": {}}):
        with pytest.raises(ValueError):
            audit_document(changed)
    d["frames"][0]["trusted"] = True
    with pytest.raises(ValueError):
        audit_document(d)
    d = document()
    d["frames"].append(document()["frames"][0])
    with pytest.raises(ValueError, match="duplicate"):
        audit_document(d)


def test_repeated_dst_hour_compares_absolute_availability_not_wall_clock():
    f = frame()
    first = datetime(2026, 11, 1, 1, 30, tzinfo=EASTERN, fold=0)
    second = datetime(2026, 11, 1, 1, 30, tzinfo=EASTERN, fold=1)
    obs = tuple(replace(o, event_at=first, available_at=second) for o in f.observations)
    result = audit(replace(f, decision_at=first, observations=obs))
    assert "NOT_YET_AVAILABLE_MODEL_EVIDENCE" in result["blockers"]


def test_cli_is_offline_nonoverwriting_and_empty_template_is_not_backtest_ready(
    tmp_path,
):
    src, out = tmp_path / "frames.json", tmp_path / "audit.json"
    src.write_text(json.dumps(document()))
    args = ["frames", str(src), "--json", str(out)]
    assert main(args) == 0
    original = out.read_bytes()
    assert b"\r" not in original
    assert main(args) == 2
    assert out.read_bytes() == original
    assert main(["frames", str(out), "--json", str(tmp_path / "invalid.json")]) == 2
    empty = audit_document({"contract": ResearchContract().as_dict(), "frames": []})
    assert empty["frame_count"] == 0
    assert empty["ready_for_backtest"] is False
