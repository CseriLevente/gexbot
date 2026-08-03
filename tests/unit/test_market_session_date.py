"""A financial session date is not the calendar day of an arbitrary timezone.

``as_of.date()`` answers a different question from "which trading session is
this", and the two disagree for six hours out of every twenty-four. Concretely:
2026-03-18T01:00Z is the 18th in UTC and the **17th** in New York, where the
options market was open.

That mattered in v2.1.9 because ``capture_session`` compared a settlement
artifact's session against ``as_of.date()``. A caller holding the UTC
representation of the same instant got a different answer from one holding the
Eastern representation -- and a settlement rule applied to the wrong session
derives a different prior session, which reweights every GEX term.

These tests fail against v2.1.9.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from src.gex.sessions import EASTERN, eastern, market_session_date

# 01:00 UTC on the 18th is 21:00 Eastern on the 17th.
LATE_EVENING_ET = eastern(2026, 3, 17, 21, 0)
SAME_INSTANT_UTC = LATE_EVENING_ET.astimezone(UTC)


def test_the_same_instant_gives_the_same_session_in_either_representation():
    """The regression, stated as the property that replaces it."""
    assert SAME_INSTANT_UTC.date() == date(2026, 3, 18)
    assert LATE_EVENING_ET.date() == date(2026, 3, 17)
    # And the session helper agrees with the market, not with the encoding.
    assert market_session_date(SAME_INSTANT_UTC) == date(2026, 3, 17)
    assert market_session_date(LATE_EVENING_ET) == date(2026, 3, 17)


@pytest.mark.parametrize(
    ("instant", "session"),
    [
        (datetime(2026, 3, 18, 1, 0, tzinfo=UTC), date(2026, 3, 17)),
        (datetime(2026, 3, 17, 14, 30, tzinfo=UTC), date(2026, 3, 17)),
        (datetime(2026, 3, 18, 3, 59, tzinfo=UTC), date(2026, 3, 17)),
        (datetime(2026, 3, 18, 4, 0, tzinfo=UTC), date(2026, 3, 18)),
    ],
)
def test_the_boundary_is_midnight_eastern(instant, session):
    assert market_session_date(instant) == session


def test_a_naive_instant_is_refused():
    """The same rule ``to_eastern`` enforces, for the same reason."""
    from src.gex.sessions import NaiveTimestampError

    with pytest.raises(NaiveTimestampError):
        market_session_date(datetime(2026, 3, 17, 11, 0))


def test_it_survives_the_daylight_saving_transitions():
    """One hour either side of both transitions, in UTC."""
    # Spring forward: 2026-03-08, 07:00 UTC.
    assert market_session_date(datetime(2026, 3, 8, 6, 0, tzinfo=UTC)) == date(
        2026, 3, 8
    )
    assert market_session_date(datetime(2026, 3, 8, 8, 0, tzinfo=UTC)) == date(
        2026, 3, 8
    )
    # Fall back: 2026-11-01, 06:00 UTC.
    assert market_session_date(datetime(2026, 11, 1, 5, 0, tzinfo=UTC)) == date(
        2026, 11, 1
    )
    assert market_session_date(datetime(2026, 11, 1, 7, 0, tzinfo=UTC)) == date(
        2026, 11, 1
    )


def test_a_capture_uses_the_market_session_for_its_settlement_rule():
    """End to end: the same capture instant, expressed two ways."""
    from src.adapters.artifact_store import InMemoryArtifactStore
    from tests.certification_fixtures import (
        documented_settlement_rule,
        durable_store,
        resolved_pipeline,
    )

    pipeline = resolved_pipeline()
    # An artifact derived for the 17th, and a capture instant that is the 18th
    # in UTC and the 17th in New York. v2.1.9 compared against ``.date()`` and
    # would have refused this as a session mismatch.
    rule = documented_settlement_rule(date(2026, 3, 17))
    session = pipeline.capture_session(
        store=durable_store(),
        session_id="late-evening",
        as_of=SAME_INSTANT_UTC,
        settlement_rule=rule,
        artifact_store=InMemoryArtifactStore(),
    )
    assert session.settlement_date == date(2026, 3, 16)


def test_a_genuinely_different_session_is_still_refused():
    """The control: the helper narrows the question, it does not soften it."""
    from src.adapters.artifact_store import InMemoryArtifactStore
    from src.config.pipeline import PipelineConsistencyError
    from tests.certification_fixtures import (
        documented_settlement_rule,
        durable_store,
        resolved_pipeline,
    )

    pipeline = resolved_pipeline()
    rule = documented_settlement_rule(date(2026, 3, 16))
    with pytest.raises(PipelineConsistencyError, match=r"(?i)different session"):
        pipeline.capture_session(
            store=durable_store(),
            session_id="wrong-session",
            as_of=SAME_INSTANT_UTC + timedelta(days=0),
            settlement_rule=rule,
            artifact_store=InMemoryArtifactStore(),
        )


def test_the_helper_is_the_one_place_a_session_date_is_produced():
    """A second implementation is how the two answers diverged in the first place."""
    import ast
    import pathlib

    #: Names that hold a market instant. ``.date()`` on one of them takes a
    #: session date from whichever zone the value happens to carry. AST-based
    #: rather than textual so that a docstring quoting the old code -- which
    #: several of these modules do, deliberately -- is not read as a call.
    instant_names = {"as_of", "requested_at", "observed_at"}

    root = pathlib.Path(__file__).resolve().parents[2] / "src"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "date":
                continue
            target = func.value
            name = (
                target.id
                if isinstance(target, ast.Name)
                else target.attr
                if isinstance(target, ast.Attribute)
                else ""
            )
            if name in instant_names:
                offenders.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    assert offenders == [], (
        f"session dates derived outside market_session_date(): {offenders}. "
        "``.date()`` on an instant names a calendar day in whichever zone the "
        "value carries, and the options market has one answer."
    )


def test_eastern_is_the_options_market_zone():
    assert str(EASTERN) == "America/New_York"
