"""The calendar decides a great deal here, so no test may read the wall clock.

**This file passes on every day of the week, and so must every other file.**

v2.1.20's suite called the dry run with no instant. Three tests then asserted
that the resulting New York date was a trading session, and on Saturday
2026-08-08 they failed:

    test_the_dry_run_reports_an_established_settlement_date
        expected ESTABLISHED, got UNESTABLISHED

    test_the_dry_run_does_not_call_established_settlement_a_blocker
        expected ESTABLISHED, got UNESTABLISHED

    test_a_failed_run_returns_a_documented_nonzero_exit_code
        expected RETRY_EXHAUSTED/7, got REFUSED/2

**Production was right in all three cases.** Saturday is not a session, no
settlement date follows from one, and a live run on a Saturday is refused. The
tests were asserting against whichever day CI happened to start.

So the behaviour that exposed the defect is covered here deliberately, at a
fixed Saturday, and the guard at the bottom of this file keeps the rest of the
suite from drifting back to the clock.
"""

from __future__ import annotations

import pathlib
import re
from datetime import UTC, date, datetime

import pytest

from src.tools.capture_thetadata_once import CaptureRunError, plan_capture, run_capture
from tests.certification_fixtures import (
    BOUNDARY_NEW_YORK_SESSION,
    DOCUMENTED_SESSION,
    NON_TRADING_SATURDAY,
    SESSION_AFTER_SATURDAY,
    UTC_NEW_YORK_BOUNDARY,
    approval_hash_for,
    vendor_transport,
)

CAPTURE_CONFIG = "config/thetadata_capture.yaml"
CONTRACT_LIST = "/v3/option/list/contracts/quote"


# =============================================================================
# §3 -- Saturday, deterministically
# =============================================================================


def test_the_fixture_saturday_really_is_a_saturday():
    """A fixture that silently stopped being a weekend would take the tests
    below with it, and they would still pass."""
    from src.gex.calendar import is_trading_session
    from src.gex.sessions import market_session_date

    session = market_session_date(NON_TRADING_SATURDAY)
    assert session == date(2026, 8, 8)
    assert session.strftime("%A") == "Saturday"
    assert not is_trading_session(session)
    assert date(2026, 8, 10) == SESSION_AFTER_SATURDAY
    assert SESSION_AFTER_SATURDAY.strftime("%A") == "Monday"


def test_a_saturday_dry_run_reports_a_non_trading_day(tmp_path):
    report = plan_capture(
        CAPTURE_CONFIG, output=str(tmp_path / "capture"), as_of=NON_TRADING_SATURDAY
    )
    session = report["market_session"]
    assert session["session_status"] == "NON_TRADING_DAY"
    assert session["inside_capture_window"] is False


def test_a_saturday_dry_run_establishes_no_settlement_date(tmp_path):
    """No session, no prior session to settle against, no evidence."""
    report = plan_capture(
        CAPTURE_CONFIG, output=str(tmp_path / "capture"), as_of=NON_TRADING_SATURDAY
    )
    assert report["vendor_documentation"]["settlement_evidence"] == "UNESTABLISHED"
    assert report["vendor_documentation"]["resolved_open_interest_settlement_date"] is (
        None
    )


def test_a_saturday_dry_run_names_settlement_among_the_actual_blockers(tmp_path):
    """The v2.1.20 terminology split has to work in both directions.

    On a session the settlement date is established and must not appear as a
    blocker. On a Saturday it is not established and must.
    """
    saturday = plan_capture(
        CAPTURE_CONFIG, output=str(tmp_path / "sat"), as_of=NON_TRADING_SATURDAY
    )
    weekday = plan_capture(
        CAPTURE_CONFIG, output=str(tmp_path / "weekday"), as_of=DOCUMENTED_SESSION
    )

    assert [
        blocker
        for blocker in saturday["actual_analytical_blockers"]
        if "settlement" in blocker.lower()
    ]
    assert not [
        blocker
        for blocker in weekday["actual_analytical_blockers"]
        if "settlement" in blocker.lower()
    ]


def test_a_saturday_dry_run_points_at_the_next_session(tmp_path):
    """An operator who ran this at the weekend should be told when to come
    back, not left to work it out."""
    report = plan_capture(
        CAPTURE_CONFIG, output=str(tmp_path / "capture"), as_of=NON_TRADING_SATURDAY
    )
    session = report["market_session"]
    assert session["next_valid_capture_window"].startswith(
        SESSION_AFTER_SATURDAY.isoformat()
    ), session


def test_a_saturday_live_run_is_refused_without_the_override(tmp_path):
    """Production behaviour, unchanged and now covered on purpose."""
    with pytest.raises(CaptureRunError, match=r"(?i)allow-out-of-session"):
        run_capture(
            CAPTURE_CONFIG,
            output=str(tmp_path / "capture"),
            transport=None,
            as_of=NON_TRADING_SATURDAY,
            allow_unsettled_raw_only=True,
            approved=approval_hash_for(CAPTURE_CONFIG, as_of=NON_TRADING_SATURDAY),
        )
    assert not (tmp_path / "capture").exists()


def test_a_saturday_capture_is_permitted_with_the_override(tmp_path):
    """The override still works, and the capture still cannot become trusted."""
    report = run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=vendor_transport(),
        as_of=NON_TRADING_SATURDAY,
        allow_out_of_session=True,
        allow_unsettled_raw_only=True,
        approved=approval_hash_for(CAPTURE_CONFIG, as_of=NON_TRADING_SATURDAY),
    )
    assert report["out_of_session_capture"] is True
    assert report["trusted_gex_computed"] is False


# =============================================================================
# §4 -- the UTC date and the New York date disagree
# =============================================================================


def test_the_boundary_instant_really_straddles_midnight():
    """UTC says the 8th; New York says the 7th. The fixture is the premise."""
    from src.gex.sessions import market_session_date

    assert UTC_NEW_YORK_BOUNDARY.astimezone(UTC).date() == date(2026, 8, 8)
    assert market_session_date(UTC_NEW_YORK_BOUNDARY) == BOUNDARY_NEW_YORK_SESSION
    assert date(2026, 8, 7) == BOUNDARY_NEW_YORK_SESSION


def test_every_derived_date_uses_the_new_york_session(tmp_path):
    """**The §4 regression.** Four derivations, one date.

    Reading ``as_of.date()`` anywhere would answer Saturday the 8th, and the
    contract-list request would ask the vendor to list contracts for a day the
    market was shut.
    """
    from src.adapters.thetadata.preflight_approval import approval_for
    from src.adapters.transport import FakeTransport
    from src.config.pipeline import ThetaDataResearchPipeline
    from src.config.schema import load_config
    from src.gex.sessions import market_session_date

    loaded = load_config(CAPTURE_CONFIG)
    pipeline = ThetaDataResearchPipeline.from_loaded_config(
        loaded, transport=FakeTransport()
    )

    # 1. the market session date itself
    assert market_session_date(UTC_NEW_YORK_BOUNDARY) == BOUNDARY_NEW_YORK_SESSION

    # 2. the contract-list request parameter
    plan = pipeline.raw_request_plan(as_of=UTC_NEW_YORK_BOUNDARY)
    listing = next(e for e in plan.requests if e.endpoint == CONTRACT_LIST)
    assert listing.parameters["date"] == BOUNDARY_NEW_YORK_SESSION.isoformat()

    # 3. the settlement derivation -- the prior *trading* session, from the
    #    New York date. Thursday the 6th, not Friday.
    from src.adapters.thetadata.openapi_evidence import (
        production_bundle,
        verified_settlement_artifact,
    )

    artifact = verified_settlement_artifact(
        production_bundle(), chain_session_date=BOUNDARY_NEW_YORK_SESSION
    )
    assert artifact.chain_session_date == BOUNDARY_NEW_YORK_SESSION
    assert artifact.resolved_settlement_date == date(2026, 8, 6)

    # 4. the approval's session date
    approval = approval_for(
        pipeline=pipeline, config=loaded.thetadata, moment=UTC_NEW_YORK_BOUNDARY
    )
    assert approval.market_session_date == BOUNDARY_NEW_YORK_SESSION

    # And the dry run agrees with all of them.
    report = plan_capture(
        CAPTURE_CONFIG, output=str(tmp_path / "capture"), as_of=UTC_NEW_YORK_BOUNDARY
    )
    assert report["market_session"]["market_session_date"] == (
        BOUNDARY_NEW_YORK_SESSION.isoformat()
    )
    assert report["preflight_approval"]["market_session_date"] == (
        BOUNDARY_NEW_YORK_SESSION.isoformat()
    )


def test_the_boundary_result_does_not_depend_on_the_machine_timezone(monkeypatch):
    """``TZ`` must not be able to move a market date.

    Set to Tokyo, where the local calendar day is ahead of both UTC and New
    York. The answer is the same because every derivation converts explicitly.
    """
    import time

    from src.gex.sessions import market_session_date

    before = market_session_date(UTC_NEW_YORK_BOUNDARY)
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    if hasattr(time, "tzset"):  # pragma: no branch - absent on Windows
        time.tzset()
    assert market_session_date(UTC_NEW_YORK_BOUNDARY) == before
    assert before == BOUNDARY_NEW_YORK_SESSION


def test_an_approval_for_the_boundary_instant_is_fridays_not_saturdays():
    """A Friday-evening approval authorises Friday's requests.

    Which is the same statement as "an approval is for one session", checked at
    the one instant where the two calendars disagree about which session that
    is.
    """
    friday_evening = approval_hash_for(CAPTURE_CONFIG, as_of=UTC_NEW_YORK_BOUNDARY)
    friday_midday = approval_hash_for(
        CAPTURE_CONFIG, as_of=datetime(2026, 8, 7, 15, 0, tzinfo=UTC)
    )
    saturday = approval_hash_for(CAPTURE_CONFIG, as_of=NON_TRADING_SATURDAY)

    assert friday_evening == friday_midday, "both are Friday's session"
    assert friday_evening != saturday


# =============================================================================
# The guard: no test may go back to reading the clock
# =============================================================================


_SUITE = pathlib.Path(__file__).resolve().parents[1]

#: Reading the clock is fine where the assertion is about the clock and cannot
#: change with the weekday. These are the two that are, and they are named so a
#: third has to be argued for rather than added quietly.
_CLOCK_ALLOWED = {
    # "two days from now is in the future" is true on every calendar day.
    "test_adapter_validator.py",
    "test_evidence_binding.py",
    # This file: proving the guard works needs the thing it forbids.
    "test_deterministic_clock.py",
}

#: The call names that read the machine's clock.
_CLOCK_CALLS = {"now", "today", "utcnow"}


def _clock_reads(source: str) -> list[int]:
    """Line numbers where this module actually *calls* the clock.

    Parsed rather than grepped. The first version of this guard matched text
    and immediately flagged a docstring in ``test_sessions.py`` that explains
    why a naive ``utcnow().date()`` would be wrong -- prose about the defect,
    not the defect. A guard that cries wolf on its own documentation gets an
    exclusion added for the wrong reason, and then it is not a guard.
    """
    import ast

    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _CLOCK_CALLS:
            # ``datetime.now(...)``, ``date.today()``, ``datetime.utcnow()``.
            # Anything else that happens to be called ``now`` is caught too,
            # which is the safe direction to be wrong in.
            found.append(node.lineno)
    return found


def test_no_test_decides_a_market_question_from_the_wall_clock():
    """**The regression that keeps the other regressions honest.**

    v2.1.20 had no such check, so three tests could depend on the day CI ran
    and nobody noticed until a Saturday.
    """
    offenders: list[str] = []
    for path in sorted(_SUITE.rglob("test_*.py")):
        if path.name in _CLOCK_ALLOWED:
            continue
        for line in _clock_reads(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name}:{line}")
    assert offenders == [], (
        "these read the wall clock; use a fixed instant from "
        "tests.certification_fixtures, or add the file to _CLOCK_ALLOWED with "
        f"a reason: {offenders}"
    )


def test_the_clock_guard_can_actually_fail():
    """A guard nobody has seen fire is a guard nobody should trust."""
    assert _clock_reads("import datetime\nx = datetime.datetime.now()\n") == [2]
    assert _clock_reads("from datetime import date\nd = date.today()\n") == [2]
    # And prose about the clock is not a clock read.
    assert _clock_reads('"""A naive ``utcnow().date()`` would be wrong."""\n') == []


def test_every_dry_run_in_the_suite_names_its_instant():
    """``plan_capture`` without ``as_of`` plans for today.

    Which is right for the CLI and wrong for a test: the report it returns is
    almost entirely a function of the market session.
    """
    unpinned: list[str] = []
    for path in sorted(_SUITE.rglob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"plan_capture\(", source):
            depth, index = 0, match.end() - 1
            while index < len(source):
                if source[index] == "(":
                    depth += 1
                elif source[index] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            call = source[match.end() - 1 : index]
            if "as_of" not in call:
                line = source[: match.start()].count("\n") + 1
                unpinned.append(f"{path.name}:{line}")
    assert unpinned == [], (
        f"these dry runs plan for whatever day the suite runs on: {unpinned}"
    )


def test_the_clock_matrix_agrees_with_itself(tmp_path):
    """One profile, three instants, three different and correct verdicts.

    Nothing here depends on when it runs.
    """
    matrix = {
        "weekday session": (DOCUMENTED_SESSION, "REGULAR_SESSION", "ESTABLISHED"),
        "saturday": (NON_TRADING_SATURDAY, "NON_TRADING_DAY", "UNESTABLISHED"),
        # Friday's session, after the close: a real session, so settlement
        # resolves; outside the window, so a live run would be refused.
        "utc/ny boundary": (UTC_NEW_YORK_BOUNDARY, "AFTER_CLOSE", "ESTABLISHED"),
    }
    for label, (moment, status, evidence) in matrix.items():
        report = plan_capture(
            CAPTURE_CONFIG, output=str(tmp_path / label.replace("/", "-")), as_of=moment
        )
        assert report["market_session"]["session_status"] == status, label
        assert report["vendor_documentation"]["settlement_evidence"] == evidence, label
