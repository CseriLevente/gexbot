"""Exact strike parsing, because strikes define contract identity.

Lives in the domain rather than the ThetaData adapter. Identity is not a
vendor's idea -- an expected universe from a contract-list endpoint and a
received chain from a quote endpoint have to agree on what "5000" means, and
they cannot if each layer formats it its own way.

The engine core imports this, so it stays stdlib-only.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, getcontext, localcontext

__all__ = ["StrikeError", "canonical_strike", "canonical_strike_of", "parse_strike"]


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


class StrikeError(ValueError):
    """A strike that cannot be given a canonical spelling."""


def canonical_strike(value: Decimal) -> str:
    """One spelling per strike, so equivalent inputs share an identity.

    Refuses a non-finite input rather than spelling it. ``Decimal("NaN")`` would
    otherwise format as the identity fragment ``NaN``, and an identity
    containing a NaN is exactly what ``parse_strike`` exists to prevent -- it
    compares unequal to itself, so the contract can never be deduplicated or
    matched.
    """
    if not value.is_finite():
        raise StrikeError(f"strike {value} is not finite; it has no canonical form")
    normalised = value.normalize()
    if normalised == normalised.to_integral_value():
        # ``quantize`` raises InvalidOperation when the result would need more
        # digits than the context allows, which for an integral value means one
        # digit per order of magnitude. Anything that large is not a strike, and
        # the previous ``f"{strike:.4f}"`` formatted it without complaint --
        # so a mis-scaled vendor value used to sail through and now must not
        # turn ``canonical_id`` into a property that throws.
        with localcontext() as ctx:
            # ``adjusted()`` is the exponent of the most significant digit, so
            # +2 covers every digit plus a guard. Finite by the check above, so
            # it is an integer rather than one of the NaN/infinity markers.
            ctx.prec = max(getcontext().prec, normalised.adjusted() + 2)
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

    Raises ``StrikeError`` on a non-finite float. ``OptionContract`` holds a
    plain float that has not been through ``parse_strike``, so this is the last
    point at which a NaN can be stopped from becoming an identity.
    """
    if value != value or value in (float("inf"), float("-inf")):
        raise StrikeError(f"strike {value!r} is not finite; it has no canonical form")
    return canonical_strike(Decimal(str(value)))
