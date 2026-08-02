"""One raw timestamp, one interpretation.

**§7.** ThetaData v3 emits wall-clock strings with no offset. The adapter
localised them to US Eastern -- a documented assumption, OD-2. The validator,
written later, read the same strings as UTC.

So the same four characters meant two different instants depending on which
module was looking, and the module that *checks* the other one was the one
disagreeing. On a 0DTE contract five hours is not a slightly wrong gamma.

Both now go through ``parse_vendor_timestamp``, which takes the configured
timezone assumption and a DST fold policy, and records what it did.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from src.domain.vendor_time import (
    AmbiguityPolicy,
    VendorTimestamp,
    parse_vendor_timestamp,
)
from src.gex.sessions import EASTERN

NAIVE = "2026-03-17T11:00:00.000"


def test_a_naive_vendor_timestamp_is_localised_to_the_configured_zone():
    parsed = parse_vendor_timestamp(NAIVE)
    assert parsed.normalized_utc == datetime(2026, 3, 17, 15, 0, tzinfo=UTC)
    assert parsed.localization_applied is True
    assert parsed.assumed_timezone == "America/New_York"


def test_an_offset_bearing_timestamp_is_taken_at_its_word():
    parsed = parse_vendor_timestamp("2026-03-17T11:00:00+00:00")
    assert parsed.normalized_utc == datetime(2026, 3, 17, 11, 0, tzinfo=UTC)
    assert parsed.localization_applied is False


def test_the_observation_records_what_it_did():
    payload = parse_vendor_timestamp(NAIVE).as_dict()
    for key in (
        "raw_value",
        "assumed_timezone",
        "normalized_utc",
        "localization_applied",
        "ambiguity_resolution",
    ):
        assert key in payload, key
    assert payload["raw_value"] == NAIVE


def test_the_validator_and_the_adapter_agree_on_the_same_string():
    """The regression, stated as directly as it can be.

    v2.1.5: the adapter said 15:00 UTC and the validator said 11:00 UTC.
    """
    from src.adapters.thetadata.client import _to_datetime
    from src.adapters.validation import parse_observation_timestamp

    adapter = _to_datetime(NAIVE)
    validator = parse_observation_timestamp(NAIVE)
    assert adapter is not None
    assert validator is not None
    assert adapter.astimezone(UTC) == validator.astimezone(UTC)


def test_the_spot_clock_uses_the_same_parser():
    from src.adapters.validation import parse_observation_timestamp

    assert parse_observation_timestamp(NAIVE) == parse_vendor_timestamp(
        NAIVE
    ).normalized_utc.astimezone(EASTERN)


@pytest.mark.parametrize("policy", list(AmbiguityPolicy))
def test_an_ambiguous_autumn_instant_is_resolved_explicitly(policy):
    """01:30 on the fall-back Sunday happens twice; the choice is recorded."""
    parsed = parse_vendor_timestamp("2026-11-01T01:30:00", ambiguity=policy)
    assert parsed.ambiguity_resolution == policy.value
    assert parsed.normalized_utc.tzinfo is UTC


def test_the_two_folds_are_genuinely_different_instants():
    earlier = parse_vendor_timestamp(
        "2026-11-01T01:30:00", ambiguity=AmbiguityPolicy.EARLIER
    )
    later = parse_vendor_timestamp(
        "2026-11-01T01:30:00", ambiguity=AmbiguityPolicy.LATER
    )
    assert earlier.normalized_utc != later.normalized_utc
    assert (later.normalized_utc - earlier.normalized_utc).total_seconds() == 3600


def test_a_nonexistent_spring_forward_instant_is_reported():
    """02:30 on the spring-forward Sunday never happens."""
    parsed = parse_vendor_timestamp("2026-03-08T02:30:00")
    assert parsed.ambiguity_resolution.startswith("NONEXISTENT")


@pytest.mark.parametrize("raw", ["", "not-a-time", "2026-13-01T00:00:00"])
def test_an_unparseable_timestamp_returns_nothing_rather_than_guessing(raw):
    assert parse_vendor_timestamp(raw) is None


def test_the_parsed_value_is_hashable_and_frozen():
    parsed = parse_vendor_timestamp(NAIVE)
    assert isinstance(parsed, VendorTimestamp)
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        parsed.raw_value = "changed"  # type: ignore[misc]
