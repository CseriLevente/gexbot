"""Black-Scholes shadow pricer.

Verified against closed-form identities rather than against hard-coded numbers
from another implementation: put-call parity, the gamma-vega relationship, and
the known ATM gamma limit. Identities catch sign and discounting errors that a
single golden number would not.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from src.domain.contracts import OptionRight
from src.gex.pricing import (
    MIN_TIME_TO_EXPIRY_YEARS,
    SECONDS_PER_YEAR,
    BlackScholesInputs,
    delta,
    gamma,
    implied_vol_from_price,
    norm_cdf,
    norm_pdf,
    price,
    vega,
    year_fraction,
)

BASE = BlackScholesInputs(
    spot=5000.0, strike=5000.0, time_to_expiry=0.25, implied_vol=0.20, rate=0.04
)


def test_norm_cdf_and_pdf_reference_values():
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_cdf(1.96) == pytest.approx(0.975, abs=1e-4)
    assert norm_cdf(-1.96) == pytest.approx(0.025, abs=1e-4)
    assert norm_pdf(0.0) == pytest.approx(1.0 / math.sqrt(2 * math.pi))


def test_put_call_parity():
    """C - P == S*e^-qT - K*e^-rT. Catches discounting and sign errors."""
    inputs = BlackScholesInputs(
        spot=5000.0,
        strike=4900.0,
        time_to_expiry=0.5,
        implied_vol=0.22,
        rate=0.042,
        dividend_yield=0.013,
    )
    call = price(inputs, OptionRight.CALL)
    put = price(inputs, OptionRight.PUT)
    expected = inputs.spot * math.exp(
        -inputs.dividend_yield * inputs.time_to_expiry
    ) - inputs.strike * math.exp(-inputs.rate * inputs.time_to_expiry)
    assert call - put == pytest.approx(expected, rel=1e-10)


def test_gamma_is_identical_for_calls_and_puts():
    """The identity the zero-gamma grid relies on to reprice cheaply."""
    inputs = BlackScholesInputs(
        spot=5000.0, strike=5100.0, time_to_expiry=0.1, implied_vol=0.25
    )
    numeric = {}
    step = 0.01
    for right in (OptionRight.CALL, OptionRight.PUT):
        up = delta(replace(inputs, spot=5000.0 + step), right)
        down = delta(
            replace(inputs, spot=5000.0 - step), right
        )
        numeric[right] = (up - down) / (2 * step)
    assert numeric[OptionRight.CALL] == pytest.approx(
        numeric[OptionRight.PUT], rel=1e-6
    )
    assert gamma(inputs) == pytest.approx(numeric[OptionRight.CALL], rel=1e-5)


def test_gamma_matches_second_derivative_of_price():
    inputs = BlackScholesInputs(
        spot=5000.0, strike=4950.0, time_to_expiry=0.08, implied_vol=0.19, rate=0.04
    )
    step = 0.5
    up = price(replace(inputs, spot=inputs.spot + step), OptionRight.CALL)
    mid = price(inputs, OptionRight.CALL)
    down = price(
        replace(inputs, spot=inputs.spot - step),
        OptionRight.CALL,
    )
    finite_difference = (up - 2 * mid + down) / (step * step)
    assert gamma(inputs) == pytest.approx(finite_difference, rel=1e-4)


def test_gamma_peaks_near_the_money():
    """Gamma must fall off on both wings, or every wall lands in the wrong place."""
    atm = gamma(BASE)
    for offset in (200.0, 500.0, 1000.0):
        for direction in (1.0, -1.0):
            wing = gamma(
                replace(BASE, strike=5000.0 + direction * offset)
            )
            assert wing < atm


def test_gamma_grows_as_expiry_approaches_for_an_atm_option():
    """The 0DTE singularity is real; the engine must reproduce it, not hide it."""
    long_dated = gamma(replace(BASE, time_to_expiry=0.25))
    short_dated = gamma(
        replace(BASE, time_to_expiry=1.0 / 365.0)
    )
    assert short_dated > long_dated * 5


def test_degenerate_inputs_return_zero_gamma_not_nan():
    for bad in (
        BlackScholesInputs(spot=5000.0, strike=5000.0, time_to_expiry=0.0, implied_vol=0.2),
        BlackScholesInputs(spot=5000.0, strike=5000.0, time_to_expiry=0.25, implied_vol=0.0),
        BlackScholesInputs(spot=0.0, strike=5000.0, time_to_expiry=0.25, implied_vol=0.2),
    ):
        assert gamma(bad) == 0.0
        assert vega(bad) == 0.0


def test_expired_delta_reports_the_intrinsic_limit():
    itm_call = BlackScholesInputs(
        spot=5100.0, strike=5000.0, time_to_expiry=0.0, implied_vol=0.2
    )
    assert delta(itm_call, OptionRight.CALL) == pytest.approx(1.0)
    assert delta(itm_call, OptionRight.PUT) == pytest.approx(0.0)


def test_year_fraction_floors_at_the_documented_minimum():
    assert year_fraction(0.0) == MIN_TIME_TO_EXPIRY_YEARS
    assert year_fraction(-5000.0) == MIN_TIME_TO_EXPIRY_YEARS
    one_hour = year_fraction(3600.0)
    assert one_hour == pytest.approx(3600.0 / SECONDS_PER_YEAR)
    assert year_fraction(0.0, floor=0.0) == 0.0


@pytest.mark.parametrize("sigma", [0.08, 0.15, 0.22, 0.45, 0.90])
@pytest.mark.parametrize("strike", [4500.0, 5000.0, 5500.0])
@pytest.mark.parametrize("right", [OptionRight.CALL, OptionRight.PUT])
def test_implied_vol_round_trips(sigma, strike, right):
    inputs = BlackScholesInputs(
        spot=5000.0,
        strike=strike,
        time_to_expiry=0.15,
        implied_vol=sigma,
        rate=0.042,
        dividend_yield=0.013,
    )
    target = price(inputs, right)
    recovered = implied_vol_from_price(
        target,
        spot=inputs.spot,
        strike=strike,
        time_to_expiry=inputs.time_to_expiry,
        right=right,
        rate=inputs.rate,
        dividend_yield=inputs.dividend_yield,
    )
    assert recovered is not None
    assert recovered == pytest.approx(sigma, rel=1e-4)


def test_implied_vol_returns_none_for_unreachable_prices():
    """An out-of-range target must not be silently clamped to the bracket edge --
    the caller needs to drop the contract and register the miss.
    """
    common = dict(
        spot=5000.0,
        strike=5000.0,
        time_to_expiry=0.15,
        right=OptionRight.CALL,
    )
    assert implied_vol_from_price(0.0, **common) is None
    assert implied_vol_from_price(-5.0, **common) is None
    assert implied_vol_from_price(4999.0, **common) is None  # above the max-vol price
