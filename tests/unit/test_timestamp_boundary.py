"""Naive timestamps may be localised at exactly one boundary, and nowhere else.

The v2 defect: ``to_eastern()`` silently reinterpreted any naive datetime as
Eastern. It is a general-purpose utility called from the engine, the confidence
model and the calendar, so a naive timestamp that slipped past the adapter would
quietly acquire a timezone deep inside the maths -- and be wrong by up to five
hours with no record that a guess had been made.

The rule now:

* ``vendor_timestamp_parsing`` (adapter only) may localise, and records that it
  did.
* ``domain_timestamp_validation`` rejects naive timestamps outright.
* ``timezone_conversion`` converts aware timestamps and refuses naive ones.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from src.adapters.thetadata.client import (
    VENDOR_TIMEZONE_ASSUMPTION,
    ChainAssemblyInputs,
    assemble_chain,
    parse_csv,
    parse_vendor_timestamp,
)
from src.domain.validation import ValidationCode
from src.gex.formulas import compute_contract_gex
from src.gex.sessions import (
    NaiveTimestampError,
    eastern,
    require_aware,
    to_eastern,
)
from src.synthetic.chains import build_synthetic_chain, with_quote

AS_OF = eastern(2026, 3, 17, 11, 0)
NAIVE = datetime(2026, 3, 17, 11, 0)


# --- Layer 3: conversion refuses naive input --------------------------------


def test_to_eastern_refuses_a_naive_datetime():
    """v2 bug: this silently assumed Eastern.

    A guess made here is invisible -- there is no record that any assumption was
    applied, and the caller cannot tell a converted timestamp from an assumed one.
    """
    with pytest.raises(NaiveTimestampError, match="naive"):
        to_eastern(NAIVE)


def test_to_eastern_converts_aware_utc_correctly():
    utc = datetime(2026, 3, 17, 15, 0, tzinfo=UTC)
    assert to_eastern(utc).hour == 11  # EDT in March


def test_to_eastern_preserves_an_aware_eastern_instant():
    assert to_eastern(AS_OF) == AS_OF


def test_require_aware_is_the_explicit_guard():
    assert require_aware(AS_OF, field="x") is AS_OF
    with pytest.raises(NaiveTimestampError, match="x"):
        require_aware(NAIVE, field="x")


def test_require_aware_allows_none_when_optional():
    assert require_aware(None, field="x") is None


# --- Layer 2: the domain rejects naive timestamps ---------------------------


@pytest.mark.parametrize(
    "field",
    [
        "quote_timestamp",
        "greeks_timestamp",
        "iv_timestamp",
        "underlying_timestamp",
    ],
)
def test_naive_contract_timestamps_are_rejected(field):
    chain = build_synthetic_chain()
    poisoned = with_quote(
        chain, 0, timestamps=replace(chain.quotes[0].timestamps, **{field: NAIVE})
    )
    result = compute_contract_gex(poisoned)
    assert result.validation.count(ValidationCode.NAIVE_TIMESTAMP) >= 1


def test_naive_snapshot_as_of_is_refused_at_construction():
    chain = build_synthetic_chain()
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(chain, as_of=NAIVE)


def test_naive_spot_timestamp_is_refused_at_construction():
    """The spot clock is a domain field like any other."""
    chain = build_synthetic_chain()
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(chain, spot_timestamp=NAIVE)


@pytest.mark.parametrize(
    "field", ["request_started_at", "response_received_at", "normalized_at"]
)
def test_naive_snapshot_clocks_are_refused(field):
    from src.domain.contracts import SnapshotClocks

    with pytest.raises(ValueError, match="timezone-aware"):
        SnapshotClocks(**{field: NAIVE})


def test_aware_timestamps_pass_every_boundary():
    chain = build_synthetic_chain()
    assert (
        compute_contract_gex(chain).validation.count(ValidationCode.NAIVE_TIMESTAMP)
        == 0
    )


# --- Layer 1: only the adapter may localise ---------------------------------


def test_the_adapter_localises_a_naive_vendor_timestamp():
    """ThetaData emits wall-clock strings with no offset, so someone must decide.

    The adapter is that someone, and it is the only such place.
    """
    parsed, assumed = parse_vendor_timestamp("2026-03-17T11:00:00.000")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.hour == 11
    assert assumed is True


def test_an_offset_bearing_vendor_timestamp_is_not_assumed():
    parsed, assumed = parse_vendor_timestamp("2026-03-17T15:00:00+00:00")
    assert parsed is not None
    assert to_eastern(parsed).hour == 11
    assert assumed is False


def test_the_localisation_assumption_is_recorded_in_metadata():
    """An assumption nobody can see is an assumption nobody can check."""
    quotes = parse_csv(
        "timestamp,symbol,expiration,strike,right,bid,ask\n"
        "2026-03-17T11:00:00.000,SPXW,2026-03-20,5000.00,call,12.30,12.60\n"
    )
    chain = assemble_chain(
        ChainAssemblyInputs(
            as_of=AS_OF,
            spot=5000.0,
            quote_rows=quotes,
            open_interest_rows=[],
            first_order_rows=[],
        )
    )
    assumption = chain.meta["vendor_timezone_assumption"]
    assert assumption["applied"] is True
    assert assumption["assumed_timezone"] == VENDOR_TIMEZONE_ASSUMPTION
    assert "NOT_YET_VALIDATED_WITH_LIVE_VENDOR_DATA" in assumption["status"]


def test_no_assumption_is_recorded_when_the_vendor_sends_offsets():
    quotes = parse_csv(
        "timestamp,symbol,expiration,strike,right,bid,ask\n"
        "2026-03-17T15:00:00+00:00,SPXW,2026-03-20,5000.00,call,12.30,12.60\n"
    )
    chain = assemble_chain(
        ChainAssemblyInputs(
            as_of=AS_OF,
            spot=5000.0,
            quote_rows=quotes,
            open_interest_rows=[],
            first_order_rows=[],
        )
    )
    assert chain.meta["vendor_timezone_assumption"]["applied"] is False


def test_an_unparseable_vendor_timestamp_yields_none_not_a_guess():
    assert parse_vendor_timestamp("not a timestamp") == (None, False)
    assert parse_vendor_timestamp("") == (None, False)


# --- DST edge cases ---------------------------------------------------------


def test_a_nonexistent_dst_wall_clock_is_refused():
    """02:30 on the spring-forward Sunday never happens in Eastern.

    Accepting it would mean inventing an instant.
    """
    with pytest.raises(ValueError, match="does not exist"):
        parse_vendor_timestamp("2026-03-08T02:30:00.000", strict_dst=True)


def test_an_ambiguous_dst_wall_clock_requires_explicit_fold():
    """01:30 on the fall-back Sunday happens twice."""
    with pytest.raises(ValueError, match="ambiguous"):
        parse_vendor_timestamp("2026-11-01T01:30:00.000", strict_dst=True)


def test_ambiguous_dst_is_resolvable_with_an_explicit_fold():
    first, _ = parse_vendor_timestamp(
        "2026-11-01T01:30:00.000", strict_dst=True, fold=0
    )
    second, _ = parse_vendor_timestamp(
        "2026-11-01T01:30:00.000", strict_dst=True, fold=1
    )
    assert first is not None
    assert second is not None
    assert first.fold == 0
    assert second.fold == 1


def test_ordinary_timestamps_are_unaffected_by_strict_dst():
    parsed, _ = parse_vendor_timestamp("2026-03-17T11:00:00.000", strict_dst=True)
    assert parsed is not None
    assert parsed.hour == 11


# --- Future-timestamp rules still apply after localisation ------------------


def test_future_rules_apply_to_a_localised_vendor_timestamp():
    """Localisation must not be a way around the future-drift check."""
    quotes = parse_csv(
        "timestamp,symbol,expiration,strike,right,bid,ask\n"
        "2026-03-17T11:30:00.000,SPXW,2026-03-20,5000.00,call,12.30,12.60\n"
    )
    oi = parse_csv(
        "timestamp,symbol,expiration,strike,right,open_interest\n"
        "2026-03-17T11:30:00.000,SPXW,2026-03-20,5000.00,call,4200\n"
    )
    chain = assemble_chain(
        ChainAssemblyInputs(
            as_of=AS_OF,
            spot=5000.0,
            quote_rows=quotes,
            open_interest_rows=oi,
            first_order_rows=[],
        )
    )
    result = compute_contract_gex(chain)
    assert result.validation.count(ValidationCode.FUTURE_TIMESTAMP) >= 1


def test_small_clock_skew_survives_localisation():
    quotes = parse_csv(
        "timestamp,symbol,expiration,strike,right,bid,ask\n"
        "2026-03-17T11:00:01.000,SPXW,2026-03-20,5000.00,call,12.30,12.60\n"
    )
    chain = assemble_chain(
        ChainAssemblyInputs(
            as_of=AS_OF + timedelta(seconds=1),
            spot=5000.0,
            quote_rows=quotes,
            open_interest_rows=[],
            first_order_rows=[],
        )
    )
    result = compute_contract_gex(chain)
    assert result.validation.count(ValidationCode.FUTURE_TIMESTAMP) == 0


# --- Layer separation -------------------------------------------------------


def test_the_domain_never_calls_the_localising_parser():
    """Architectural check: only the adapter may localise.

    If a domain module imported the vendor parser, the single-boundary guarantee
    would be gone regardless of what the docstrings claim.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "src"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if parts[0] not in ("domain", "gex", "synthetic"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and ("thetadata" in node.module)
            ):
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], (
        "domain/engine code imports the vendor parser; localisation must stay "
        f"inside the adapter: {offenders}"
    )
