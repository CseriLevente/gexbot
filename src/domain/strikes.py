"""Exact strike parsing, because strikes define contract identity.

Lives in the domain rather than the ThetaData adapter. Identity is not a
vendor's idea -- an expected universe from a contract-list endpoint and a
received chain from a quote endpoint have to agree on what "5000" means, and
they cannot if each layer formats it its own way.

The engine core imports this, so it stays stdlib-only.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

__all__ = ["canonical_strike", "canonical_strike_of", "parse_strike"]


def parse_strike(value: str | float | None) -> tuple[Decimal | None, str | None]:
    """Parse a strike exactly, for use in the canonical contract identity.

    v2.1.1 built the identity from ``float(row["strike"])``. Two consequences:

    * ``"NaN"`` produced an identity containing a NaN, which compares unequal to
      itself, so a NaN-struck contract could never be deduplicated or matched;
    * equivalence between ``"5000"``, ``"5000.0"`` and ``"5000.00"`` depended on
      float formatting rather than on the numbers being equal.

    ``Decimal`` gives exactness and a canonical form without either.
    """
    if value is None:
        return None, "missing_value"
    text = str(value).strip()
    if not text:
        return None, "missing_value"
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None, "malformed_value"
    if not parsed.is_finite():
        return None, "non_finite_input"
    if parsed <= 0:
        return None, "out_of_range"
    return parsed, None


def canonical_strike(value: Decimal) -> str:
    """One spelling per strike, so equivalent inputs share an identity."""
    normalised = value.normalize()
    if normalised == normalised.to_integral_value():
        normalised = normalised.quantize(Decimal(1))
    return f"{normalised:f}"


def canonical_strike_of(value: float) -> str:
    """The same spelling, starting from a float.

    ``str()`` first, deliberately. Python's float repr is the shortest string
    that round-trips, so ``str(4900.5)`` is ``'4900.5'`` -- the number a human
    would read -- whereas ``Decimal(4900.5)`` is the exact binary value, which
    for most strikes is a forty-digit tail nobody wrote down.

    v2.1.3 formatted the two sides of the identity differently:
    ``OptionContract.canonical_id`` used ``f"{strike:.4f}"`` while
    ``contract_identity`` went ``Decimal -> float -> f"{:.4f}"``. They agreed for
    every strike either side happened to be tested with, which is not the same as
    agreeing. A strike needing more than four decimals, or one whose float
    round-trip lands a bit low, produced a "missing" contract and an
    "unexpected" one for the same instrument -- and the completeness measure
    reported a shortfall that did not exist.
    """
    return canonical_strike(Decimal(str(value)))
