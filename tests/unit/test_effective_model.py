"""The authoritative effective-model resolver.

Written before the implementation. Each test names the v2 defect it reproduces.

The single rule this module exists to enforce: **every pricing, gamma, GEX,
zero-gamma and gamma-comparison calculation consumes the same resolved inputs.**
Scattered fallback logic is how two code paths end up pricing the same contract
differently while both look correct in isolation.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from src.domain.effective_model import (
    ModelResolutionError,
    ResolutionIssue,
    resolve_effective_inputs,
)
from src.domain.iv import IVSource, build_iv_quote
from src.domain.model_spec import (
    DayCountConvention,
    DividendSource,
    ExpirationTimestampRule,
    ModelSpec,
    RateSource,
    UnderlyingPriceSource,
)
from src.gex.pricing import BlackScholesInputs
from src.gex.pricing import gamma as bs_gamma
from src.gex.sessions import eastern
from src.synthetic.chains import build_single_contract_chain

AS_OF = eastern(2026, 3, 17, 11, 0)


def chain(**overrides):
    base = build_single_contract_chain(as_of=AS_OF)
    return replace(base, **overrides)


def resolve(*, spec: ModelSpec | None = None, snapshot=None, quote=None):
    snap = snapshot if snapshot is not None else chain()
    return resolve_effective_inputs(
        quote=quote if quote is not None else snap.quotes[0],
        snapshot=snap,
        spec=spec or ModelSpec(),
    )


# --- Shape ------------------------------------------------------------------


def test_resolver_returns_every_input_the_pricer_needs():
    resolved = resolve()
    for field in (
        "spot",
        "risk_free_rate",
        "dividend_yield",
        "expiration_timestamp",
        "time_to_expiry_years",
        "implied_volatility",
        "implied_volatility_source",
        "underlying_price_source",
        "risk_free_rate_source",
        "dividend_yield_source",
        "expiration_rule",
        "minimum_time_to_expiry_minutes",
        "day_count_convention",
    ):
        assert hasattr(resolved, field), field


def test_resolved_inputs_are_frozen_and_serialisable():
    resolved = resolve()
    # Frozen slotted dataclasses raise FrozenInstanceError, which subclasses
    # AttributeError; naming both keeps the test honest about what it accepts.
    with pytest.raises((AttributeError, TypeError)):
        resolved.spot = 1.0  # type: ignore[misc]
    payload = resolved.as_dict()
    assert payload["spot"] == pytest.approx(resolved.spot)
    assert payload["risk_free_rate_source"] == resolved.risk_free_rate_source.value
    import json

    json.dumps(payload)


def test_resolution_is_deterministic():
    assert resolve().as_dict() == resolve().as_dict()
    assert resolve().fingerprint() == resolve().fingerprint()


def test_expiration_timestamp_is_timezone_aware():
    assert resolve().expiration_timestamp.tzinfo is not None


# --- DEFECT: falsy-number fallback -----------------------------------------


def test_explicit_zero_risk_free_rate_stays_zero():
    """v2 bug: ``spec.risk_free_rate or snapshot.risk_free_rate``.

    0.0 is falsy, so an explicitly configured zero rate silently fell back to the
    snapshot's rate. The engine then priced with a rate the operator did not ask
    for, and the fingerprint recorded the one they did.
    """
    snapshot = chain(risk_free_rate=0.05)
    spec = ModelSpec(
        risk_free_rate_source=RateSource.CONFIGURED_CONSTANT, risk_free_rate=0.0
    )
    assert resolve(spec=spec, snapshot=snapshot).risk_free_rate == 0.0


def test_explicit_zero_dividend_yield_stays_zero():
    snapshot = chain(dividend_yield=0.02)
    spec = ModelSpec(
        dividend_yield_source=DividendSource.CONFIGURED_CONSTANT, dividend_yield=0.0
    )
    assert resolve(spec=spec, snapshot=snapshot).dividend_yield == 0.0


def test_explicit_zero_rate_changes_gamma_versus_a_nonzero_rate():
    """The fallback bug was not cosmetic: the two rates price differently."""
    snapshot = chain(risk_free_rate=0.05)
    zero = resolve(
        spec=ModelSpec(risk_free_rate=0.0, dividend_yield=0.0), snapshot=snapshot
    )
    nonzero = resolve(
        spec=ModelSpec(risk_free_rate=0.05, dividend_yield=0.0), snapshot=snapshot
    )
    assert zero.gamma() != nonzero.gamma()


def test_zero_rate_source_yields_zero_regardless_of_configured_value():
    spec = ModelSpec(risk_free_rate_source=RateSource.ZERO, risk_free_rate=0.09)
    assert resolve(spec=spec).risk_free_rate == 0.0


def test_zero_dividend_source_yields_zero_regardless_of_configured_value():
    spec = ModelSpec(dividend_yield_source=DividendSource.ZERO, dividend_yield=0.09)
    assert resolve(spec=spec).dividend_yield == 0.0


def test_snapshot_fallback_happens_only_when_the_source_asks_for_it():
    """Resolution follows the configured source enum, not "first non-None".

    A configured-constant source must not quietly read the snapshot just because
    the configured value happens to be zero.
    """
    snapshot = chain(risk_free_rate=0.07)
    configured = resolve(
        spec=ModelSpec(
            risk_free_rate_source=RateSource.CONFIGURED_CONSTANT, risk_free_rate=0.01
        ),
        snapshot=snapshot,
    )
    assert configured.risk_free_rate == pytest.approx(0.01)

    from_snapshot = resolve(
        spec=ModelSpec(risk_free_rate_source=RateSource.SNAPSHOT, risk_free_rate=0.01),
        snapshot=snapshot,
    )
    assert from_snapshot.risk_free_rate == pytest.approx(0.07)


def test_a_zero_value_is_fully_specified_when_its_source_is_explicit():
    """Provenance, not magnitude, decides completeness."""
    resolved = resolve(
        spec=ModelSpec(
            risk_free_rate_source=RateSource.ZERO,
            dividend_yield_source=DividendSource.ZERO,
            iv_price_source=IVSource.NBBO_MID_IV,
        )
    )
    assert resolved.is_fully_specified
    assert resolved.missing_inputs == ()


def test_configured_constant_without_a_value_is_incomplete():
    spec = ModelSpec(
        risk_free_rate_source=RateSource.CONFIGURED_CONSTANT, risk_free_rate=None
    )
    resolved = resolve(spec=spec)
    assert not resolved.is_fully_specified
    assert any("risk_free_rate" in entry for entry in resolved.missing_inputs)


def test_snapshot_source_with_no_snapshot_value_is_incomplete():
    snapshot = chain(risk_free_rate=None)
    resolved = resolve(
        spec=ModelSpec(risk_free_rate_source=RateSource.SNAPSHOT), snapshot=snapshot
    )
    assert not resolved.is_fully_specified


def test_vendor_dividend_without_vendor_data_is_incomplete():
    resolved = resolve(
        spec=ModelSpec(dividend_yield_source=DividendSource.VENDOR_ANNUAL_DIVIDEND)
    )
    assert not resolved.is_fully_specified
    assert any("dividend_yield" in entry for entry in resolved.missing_inputs)


# --- DEFECT: underlying-price source was inert ------------------------------


def test_snapshot_spot_source_uses_the_chain_spot():
    snapshot = chain(spot=5000.0)
    resolved = resolve(
        spec=ModelSpec(
            underlying_price_source=UnderlyingPriceSource.VENDOR_INDEX_SNAPSHOT
        ),
        snapshot=snapshot,
    )
    assert resolved.spot == pytest.approx(5000.0)


def test_per_contract_source_uses_the_contract_underlying():
    """v2 bug: the engine always used the snapshot spot, so this enum changed
    metadata without changing a single number.
    """
    snapshot = chain(spot=5000.0)
    quote = replace(snapshot.quotes[0], underlying_price=5123.0)
    resolved = resolve_effective_inputs(
        quote=quote,
        snapshot=snapshot,
        spec=ModelSpec(
            underlying_price_source=UnderlyingPriceSource.VENDOR_PER_CONTRACT
        ),
    )
    assert resolved.spot == pytest.approx(5123.0)


def test_changing_the_underlying_source_changes_gamma():
    snapshot = chain(spot=5000.0)
    quote = replace(snapshot.quotes[0], underlying_price=5300.0)
    spot_based = resolve_effective_inputs(
        quote=quote,
        snapshot=snapshot,
        spec=ModelSpec(
            underlying_price_source=UnderlyingPriceSource.VENDOR_INDEX_SNAPSHOT
        ),
    )
    contract_based = resolve_effective_inputs(
        quote=quote,
        snapshot=snapshot,
        spec=ModelSpec(
            underlying_price_source=UnderlyingPriceSource.VENDOR_PER_CONTRACT
        ),
    )
    assert spot_based.gamma() != contract_based.gamma()


def test_missing_per_contract_underlying_is_not_silently_replaced():
    """Falling back to the snapshot spot would hide that the vendor sent nothing."""
    snapshot = chain(spot=5000.0)
    quote = replace(snapshot.quotes[0], underlying_price=None)
    resolved = resolve_effective_inputs(
        quote=quote,
        snapshot=snapshot,
        spec=ModelSpec(
            underlying_price_source=UnderlyingPriceSource.VENDOR_PER_CONTRACT
        ),
    )
    assert not resolved.is_usable
    assert ResolutionIssue.UNDERLYING_MISSING in resolved.issues


def test_non_finite_per_contract_underlying_is_rejected():
    snapshot = chain(spot=5000.0)
    quote = replace(snapshot.quotes[0], underlying_price=float("nan"))
    resolved = resolve_effective_inputs(
        quote=quote,
        snapshot=snapshot,
        spec=ModelSpec(
            underlying_price_source=UnderlyingPriceSource.VENDOR_PER_CONTRACT
        ),
    )
    assert not resolved.is_usable
    assert ResolutionIssue.UNDERLYING_NOT_FINITE in resolved.issues


def test_configured_constant_underlying_requires_a_value():
    resolved = resolve(
        spec=ModelSpec(
            underlying_price_source=UnderlyingPriceSource.CONFIGURED_CONSTANT
        )
    )
    assert not resolved.is_usable


# --- IV resolution ----------------------------------------------------------


def test_iv_comes_from_the_quote_and_records_its_source():
    quote = replace(
        chain().quotes[0],
        iv=build_iv_quote(
            bid_iv=0.18, mid_iv=0.20, ask_iv=0.22, preferred_source=IVSource.NBBO_MID_IV
        ),
    )
    resolved = resolve(quote=quote)
    assert resolved.implied_volatility == pytest.approx(0.20)
    assert resolved.implied_volatility_source is IVSource.NBBO_MID_IV


def test_unusable_iv_makes_the_resolution_unusable():
    quote = replace(
        chain().quotes[0],
        iv=build_iv_quote(bid_iv=None, mid_iv=None, ask_iv=None),
    )
    resolved = resolve(quote=quote)
    assert not resolved.is_usable
    assert ResolutionIssue.IV_MISSING in resolved.issues


# --- Time resolution --------------------------------------------------------


def test_time_to_expiry_honours_the_day_count_convention():
    act365 = resolve(
        spec=ModelSpec(day_count_convention=DayCountConvention.ACT_365_FIXED)
    )
    act252 = resolve(spec=ModelSpec(day_count_convention=DayCountConvention.ACT_252))
    assert act365.time_to_expiry_years != act252.time_to_expiry_years


def test_time_to_expiry_honours_the_minimum_floor():
    """Fifteen minutes to a PM settlement: a 60-minute floor must bind."""
    late = eastern(2026, 3, 17, 15, 45)
    snapshot = replace(
        build_single_contract_chain(as_of=late, expiry=date(2026, 3, 17)), as_of=late
    )
    unfloored = resolve(
        spec=ModelSpec(minimum_time_to_expiry_minutes=1.0 / 60.0), snapshot=snapshot
    )
    floored = resolve(
        spec=ModelSpec(minimum_time_to_expiry_minutes=60.0), snapshot=snapshot
    )
    assert floored.time_to_expiry_years > unfloored.time_to_expiry_years
    assert floored.gamma() != unfloored.gamma()


def test_expired_contract_is_flagged_not_priced():
    past = eastern(2026, 3, 18, 11, 0)
    snapshot = replace(
        build_single_contract_chain(as_of=past, expiry=date(2026, 3, 17)), as_of=past
    )
    resolved = resolve(snapshot=snapshot)
    assert not resolved.is_usable
    assert ResolutionIssue.EXPIRED in resolved.issues


# --- One model everywhere ---------------------------------------------------


def test_resolver_gamma_matches_a_direct_black_scholes_call():
    """The resolver must not be a second, subtly different pricing path."""
    resolved = resolve(spec=ModelSpec(risk_free_rate=0.042, dividend_yield=0.013))
    direct = bs_gamma(
        BlackScholesInputs(
            spot=resolved.spot,
            strike=resolved.strike,
            time_to_expiry=resolved.time_to_expiry_years,
            implied_vol=resolved.implied_volatility,
            rate=resolved.risk_free_rate,
            dividend_yield=resolved.dividend_yield,
        )
    )
    assert resolved.gamma() == pytest.approx(direct, rel=1e-15)


def test_reprice_at_moves_only_spot():
    """The zero-gamma grid reprices at hypothetical spots; everything else in the
    effective model must stay pinned.
    """
    resolved = resolve()
    moved = resolved.reprice_at(resolved.spot * 1.02)
    assert moved.spot == pytest.approx(resolved.spot * 1.02)
    assert moved.time_to_expiry_years == resolved.time_to_expiry_years
    assert moved.risk_free_rate == resolved.risk_free_rate
    assert moved.dividend_yield == resolved.dividend_yield
    assert moved.gamma() != resolved.gamma()


def test_reprice_with_iv_moves_only_volatility():
    resolved = resolve()
    shifted = resolved.reprice_at(resolved.spot, implied_volatility=0.40)
    assert shifted.implied_volatility == pytest.approx(0.40)
    assert shifted.spot == resolved.spot
    assert shifted.gamma() != resolved.gamma()


# --- Fingerprint ------------------------------------------------------------


def test_fingerprint_changes_with_every_resolved_value():
    baseline = resolve().fingerprint()
    for spec in (
        ModelSpec(risk_free_rate=0.09),
        ModelSpec(dividend_yield=0.09),
        ModelSpec(minimum_time_to_expiry_minutes=30.0),
        ModelSpec(day_count_convention=DayCountConvention.ACT_252),
        ModelSpec(expiration_timestamp_rule=ExpirationTimestampRule.FIXED_1600_ET),
    ):
        assert resolve(spec=spec).fingerprint() != baseline, spec


def test_fingerprint_covers_the_resolved_value_not_just_the_declared_source():
    """The audit fingerprint must describe the effective model.

    Two configurations that resolve to the same numbers should agree; a
    configuration whose declared source differs but whose resolved value is
    identical is still recorded, because provenance is part of the audit.
    """
    zero_source = resolve(spec=ModelSpec(risk_free_rate_source=RateSource.ZERO))
    explicit_zero = resolve(
        spec=ModelSpec(
            risk_free_rate_source=RateSource.CONFIGURED_CONSTANT, risk_free_rate=0.0
        )
    )
    assert zero_source.risk_free_rate == explicit_zero.risk_free_rate == 0.0
    assert zero_source.fingerprint() != explicit_zero.fingerprint()


def test_unresolvable_inputs_raise_when_priced():
    quote = replace(
        chain().quotes[0], iv=build_iv_quote(bid_iv=None, mid_iv=None, ask_iv=None)
    )
    resolved = resolve(quote=quote)
    with pytest.raises(ModelResolutionError):
        resolved.gamma()


# --- Snapshot integration ---------------------------------------------------


def test_snapshot_carries_the_canonical_effective_model():
    """§1: the resolved inputs must reach the audit output, not just the maths."""
    from src.gex.config import GexEngineConfig
    from src.gex.engine import compute_gex_snapshot
    from src.synthetic.chains import SyntheticChainSpec, build_synthetic_chain

    spec = SyntheticChainSpec()
    produced = compute_gex_snapshot(
        build_synthetic_chain(spec),
        GexEngineConfig(model_spec=spec.model_spec()),
    )
    effective = produced.effective_model
    assert effective is not None
    assert effective["risk_free_rate"] == pytest.approx(0.042)
    assert effective["risk_free_rate_source"] == "configured_constant"
    assert effective["effective_model_fingerprint"]
    assert produced.as_dict()["effective_model"] == effective


def test_snapshot_effective_model_is_absent_for_an_empty_chain():
    from dataclasses import replace as _replace

    from src.gex.engine import compute_gex_snapshot
    from src.synthetic.chains import build_synthetic_chain

    empty = _replace(build_synthetic_chain(), quotes=())
    assert compute_gex_snapshot(empty).effective_model is None


def test_engine_warns_when_the_effective_model_is_not_fully_specified():
    from src.gex.config import GexEngineConfig
    from src.gex.engine import compute_gex_snapshot
    from src.synthetic.chains import build_synthetic_chain

    produced = compute_gex_snapshot(
        build_synthetic_chain(),
        GexEngineConfig(
            model_spec=ModelSpec(
                risk_free_rate_source=RateSource.CONFIGURED_CONSTANT,
                risk_free_rate=None,
            )
        ),
    )
    assert any("not fully specified" in w for w in produced.warnings)


def test_every_contract_shares_one_model_fingerprint():
    """Model-level assumptions are per-snapshot; only spot, strike, IV and expiry
    vary per contract. If two contracts disagreed, two models would be in play.
    """
    from src.gex.config import GexEngineConfig
    from src.gex.formulas import compute_contract_gex
    from src.synthetic.chains import SyntheticChainSpec, build_synthetic_chain

    spec = SyntheticChainSpec()
    result = compute_contract_gex(
        build_synthetic_chain(spec), GexEngineConfig(model_spec=spec.model_spec())
    )
    assert len({c.effective.fingerprint() for c in result.contracts}) == 1


def test_zero_rate_config_changes_the_whole_snapshot():
    """End-to-end proof that the falsy-fallback fix reaches the aggregates."""
    from src.gex.config import GexEngineConfig
    from src.gex.engine import compute_gex_snapshot
    from src.synthetic.chains import SyntheticChainSpec, build_synthetic_chain

    spec = SyntheticChainSpec()
    built = build_synthetic_chain(spec)
    with_rate = compute_gex_snapshot(
        built, GexEngineConfig(model_spec=spec.model_spec())
    )
    zero_rate = compute_gex_snapshot(
        built,
        GexEngineConfig(
            model_spec=replace(spec.model_spec(), risk_free_rate_source=RateSource.ZERO)
        ),
    )
    assert zero_rate.total_unsigned_gex != with_rate.total_unsigned_gex
    assert zero_rate.effective_model["risk_free_rate"] == 0.0
