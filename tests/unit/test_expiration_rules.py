"""Expiration-timestamp rules must be real or refused.

The v2 defect: `FIXED_1600_ET` and `CALENDAR_MIDNIGHT` were accepted by config
and recorded in the model fingerprint, but the engine always used root-specific
settlement. Two snapshots could therefore carry different fingerprints while
every number in them was identical -- an audit trail asserting a distinction the
maths never made.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from src.config.schema import ConfigError, parse_config
from src.domain.contracts import OptionRoot
from src.domain.effective_model import ResolutionIssue, resolve_effective_inputs
from src.domain.model_spec import ExpirationTimestampRule, ModelSpec
from src.gex.sessions import eastern, expiration_timestamp, seconds_to_expiry_at
from src.synthetic.chains import build_single_contract_chain

AS_OF = eastern(2026, 3, 17, 11, 0)

# 2026-11-27 is the day after Thanksgiving: a 13:00 ET early close.
EARLY_CLOSE_DAY = date(2026, 11, 27)
# 2026-03-20 is a third Friday, so both an AM-settled SPX and a PM-settled SPXW
# series expire on it.
THIRD_FRIDAY = date(2026, 3, 20)


def ts(root: OptionRoot, expiry: date, rule: ExpirationTimestampRule):
    return expiration_timestamp(root=root, expiry=expiry, rule=rule)


# --- Each supported rule produces a different instant -----------------------


def test_am_and_pm_settlement_differ_under_the_root_specific_rule():
    rule = ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT
    am = ts(OptionRoot.SPX, THIRD_FRIDAY, rule)
    pm = ts(OptionRoot.SPXW, THIRD_FRIDAY, rule)
    assert (am.hour, am.minute) == (9, 30)
    assert (pm.hour, pm.minute) == (16, 0)
    assert am < pm


def test_fixed_1600_ignores_the_root():
    """That is the whole point of the rule: it measures what AM/PM is worth."""
    rule = ExpirationTimestampRule.FIXED_1600_ET
    am = ts(OptionRoot.SPX, THIRD_FRIDAY, rule)
    pm = ts(OptionRoot.SPXW, THIRD_FRIDAY, rule)
    assert am == pm
    assert (am.hour, am.minute) == (16, 0)


def test_fixed_1600_differs_from_root_specific_for_an_am_settled_series():
    root_specific = ts(
        OptionRoot.SPX, THIRD_FRIDAY, ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT
    )
    fixed = ts(OptionRoot.SPX, THIRD_FRIDAY, ExpirationTimestampRule.FIXED_1600_ET)
    assert root_specific != fixed
    assert (fixed - root_specific).total_seconds() == pytest.approx(6.5 * 3600)


def test_early_close_rule_shortens_a_pm_settlement():
    plain = ts(
        OptionRoot.SPXW,
        EARLY_CLOSE_DAY,
        ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT,
    )
    early = ts(
        OptionRoot.SPXW,
        EARLY_CLOSE_DAY,
        ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT_WITH_EARLY_CLOSE,
    )
    assert plain.hour == 16
    assert early.hour == 13
    assert early < plain


def test_early_close_rule_is_a_no_op_on_a_regular_session():
    """Only an early-close date should differ, or the rule is doing too much."""
    plain = ts(
        OptionRoot.SPXW, THIRD_FRIDAY, ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT
    )
    early = ts(
        OptionRoot.SPXW,
        THIRD_FRIDAY,
        ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT_WITH_EARLY_CLOSE,
    )
    assert plain == early


def test_early_close_rule_does_not_move_an_am_settlement():
    """An AM settlement is struck at the open, which an early close does not move."""
    plain = ts(
        OptionRoot.SPX,
        EARLY_CLOSE_DAY,
        ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT,
    )
    early = ts(
        OptionRoot.SPX,
        EARLY_CLOSE_DAY,
        ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT_WITH_EARLY_CLOSE,
    )
    assert plain == early == eastern(2026, 11, 27, 9, 30)


def test_every_supported_rule_is_timezone_aware():
    for rule in ExpirationTimestampRule:
        if not rule.is_supported:
            continue
        assert ts(OptionRoot.SPXW, THIRD_FRIDAY, rule).tzinfo is not None


# --- The unsupported rule is refused, not ignored ---------------------------


def test_calendar_midnight_raises_rather_than_returning_a_meaningless_instant():
    with pytest.raises(ValueError, match="not supported"):
        ts(OptionRoot.SPXW, THIRD_FRIDAY, ExpirationTimestampRule.CALENDAR_MIDNIGHT)


def test_calendar_midnight_states_why_it_is_unsupported():
    reason = ExpirationTimestampRule.CALENDAR_MIDNIGHT.unsupported_reason
    assert reason is not None
    assert "midnight" in reason
    assert "start or" in reason  # the ambiguity is named


def test_calendar_midnight_fails_configuration_loading():
    with pytest.raises(ConfigError, match="not supported"):
        parse_config(
            {
                "stage": "DEVELOPMENT",
                "enabled": True,
                "data": {"options_source": "synthetic"},
                "execution": {"broker": "none", "trading_enabled": False},
                "model": {"expiration_timestamp_rule": "calendar_midnight"},
            }
        )


def test_the_resolver_flags_an_unsupported_rule_rather_than_pricing_it():
    chain = build_single_contract_chain(as_of=AS_OF)
    resolved = resolve_effective_inputs(
        quote=chain.quotes[0],
        snapshot=chain,
        spec=ModelSpec(
            expiration_timestamp_rule=ExpirationTimestampRule.CALENDAR_MIDNIGHT
        ),
    )
    assert not resolved.is_usable
    assert ResolutionIssue.EXPIRATION_RULE_UNSUPPORTED in resolved.issues


def test_supported_rules_report_as_supported():
    assert ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT.is_supported
    assert ExpirationTimestampRule.FIXED_1600_ET.is_supported
    assert not ExpirationTimestampRule.CALENDAR_MIDNIGHT.is_supported


# --- The rule changes the numbers, not just the metadata --------------------


def resolve_with(rule: ExpirationTimestampRule, *, root: OptionRoot, expiry: date):
    chain = build_single_contract_chain(as_of=AS_OF, expiry=expiry)
    quote = replace(
        chain.quotes[0],
        contract=replace(chain.quotes[0].contract, root=root, expiry=expiry),
    )
    return resolve_effective_inputs(
        quote=quote,
        snapshot=replace(chain, quotes=(quote,)),
        spec=ModelSpec(
            expiration_timestamp_rule=rule, minimum_time_to_expiry_minutes=1.0
        ),
    )


def test_rule_change_moves_time_to_expiry_for_an_am_settled_series():
    root_specific = resolve_with(
        ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT,
        root=OptionRoot.SPX,
        expiry=THIRD_FRIDAY,
    )
    fixed = resolve_with(
        ExpirationTimestampRule.FIXED_1600_ET, root=OptionRoot.SPX, expiry=THIRD_FRIDAY
    )
    assert fixed.time_to_expiry_years > root_specific.time_to_expiry_years


def test_rule_change_moves_gamma_for_an_am_settled_series():
    """The assertion the v2 code could not have satisfied."""
    root_specific = resolve_with(
        ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT,
        root=OptionRoot.SPX,
        expiry=THIRD_FRIDAY,
    )
    fixed = resolve_with(
        ExpirationTimestampRule.FIXED_1600_ET, root=OptionRoot.SPX, expiry=THIRD_FRIDAY
    )
    assert root_specific.gamma() != fixed.gamma()


# Each non-default supported rule, with an input on which it MUST differ from
# the default. A rule with no such witness is inert -- which is precisely the v2
# defect -- so the witness is part of the rule's definition, not an incidental
# test detail.
#
# Note the early-close rule is deliberately identical to the default on ordinary
# dates. That is correct behaviour, not inertness: its witness is an early-close
# date. Asserting "every pair of rules differs on every input" would be wrong and
# would force a bogus fix.
RULE_WITNESSES = {
    ExpirationTimestampRule.FIXED_1600_ET: (OptionRoot.SPX, THIRD_FRIDAY),
    ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT_WITH_EARLY_CLOSE: (
        OptionRoot.SPXW,
        EARLY_CLOSE_DAY,
    ),
}


@pytest.mark.parametrize(
    "rule", sorted(RULE_WITNESSES, key=lambda r: r.value), ids=lambda r: r.value
)
def test_every_supported_rule_has_an_input_where_it_changes_the_number(rule):
    """The invariant the v2 code violated.

    A recorded assumption must be able to affect the calculation. If a rule can
    never change a number, the fingerprint that records it is asserting a
    distinction the maths never made.
    """
    root, expiry = RULE_WITNESSES[rule]
    default = resolve_with(
        ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT, root=root, expiry=expiry
    )
    variant = resolve_with(rule, root=root, expiry=expiry)

    assert variant.expiration_timestamp != default.expiration_timestamp
    assert variant.time_to_expiry_years != default.time_to_expiry_years
    assert variant.gamma() != default.gamma()
    assert variant.fingerprint() != default.fingerprint()


def test_a_differing_fingerprint_on_one_input_does_not_require_differing_on_all():
    """Guards against over-correcting the invariant above.

    The early-close rule legitimately agrees with the default on an ordinary
    session. Forcing it to differ everywhere would make it wrong.
    """
    ordinary = resolve_with(
        ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT_WITH_EARLY_CLOSE,
        root=OptionRoot.SPXW,
        expiry=THIRD_FRIDAY,
    )
    default = resolve_with(
        ExpirationTimestampRule.ROOT_SPECIFIC_SETTLEMENT,
        root=OptionRoot.SPXW,
        expiry=THIRD_FRIDAY,
    )
    assert ordinary.expiration_timestamp == default.expiration_timestamp
    assert ordinary.gamma() == default.gamma()


# --- 0DTE either side of the floor -----------------------------------------


def test_0dte_before_the_floor_is_floored_and_after_it_is_not():
    """At 15:45 with a 60-minute floor the remaining 15 minutes are floored; at
    11:00 the remaining five hours are not.
    """
    late = eastern(2026, 3, 17, 15, 45)
    mid = eastern(2026, 3, 17, 11, 0)
    spec = ModelSpec(minimum_time_to_expiry_minutes=60.0)

    for as_of, expect_floored in ((late, True), (mid, False)):
        chain = build_single_contract_chain(as_of=as_of, expiry=date(2026, 3, 17))
        resolved = resolve_effective_inputs(
            quote=chain.quotes[0], snapshot=chain, spec=spec
        )
        raw_seconds = seconds_to_expiry_at(as_of, resolved.expiration_timestamp)
        raw_years = raw_seconds / spec.day_count_convention.seconds_per_year
        floored = resolved.time_to_expiry_years > raw_years * 1.0000001
        assert floored is expect_floored, as_of


def test_an_am_settled_0dte_series_is_already_expired_at_midday():
    chain = build_single_contract_chain(as_of=AS_OF, expiry=date(2026, 3, 17))
    quote = replace(
        chain.quotes[0],
        contract=replace(chain.quotes[0].contract, root=OptionRoot.SPX),
    )
    resolved = resolve_effective_inputs(
        quote=quote,
        snapshot=replace(chain, quotes=(quote,)),
        spec=ModelSpec(),
    )
    assert ResolutionIssue.EXPIRED in resolved.issues


def test_a_pm_settled_0dte_series_is_still_alive_at_midday():
    chain = build_single_contract_chain(as_of=AS_OF, expiry=date(2026, 3, 17))
    resolved = resolve_effective_inputs(
        quote=chain.quotes[0], snapshot=chain, spec=ModelSpec()
    )
    assert ResolutionIssue.EXPIRED not in resolved.issues
