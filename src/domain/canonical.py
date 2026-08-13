"""One report, one digest, on any machine that runs it.

Two certifications of the same immutable capture produced the same findings on
Windows and on Linux and different ``report_hash`` values, for two reasons that
have nothing to do with the vendor.

**Absolute paths.** The report recorded where the pinned document was found:
``C:\\...\\vendor_documentation`` here, ``/.../vendor_documentation`` there.
Useful to an operator, and a statement about a filesystem rather than about a
capture. Content identity is now separated from local presentation: the report
still shows the path, the hash never sees it.

**Floating point.** Reconstruction runs ``exp``, ``log``, ``erf`` and ``sqrt``
thousands of times and sums the results. Different libm builds and different
summation orders land on values that differ in the last few bits::

    Windows   0.0001584804637398003
    Linux     0.00015848046373980108

That is a relative difference of about 5e-15 in a statistic whose inputs are
quoted to 1e-4. It is not a finding. Feeding ``repr`` of those doubles into
SHA-256 made it one.

So numbers enter the digest as decimal strings rounded to
:data:`SIGNIFICANT_DIGITS` significant digits. Nine, chosen against the source
data rather than for tidiness: the vendor reports ``delta`` and ``implied_vol``
to 1e-4, so a statistic over them carries four or five meaningful digits and
nine is four orders finer than anything the inputs can support. It is also six
orders coarser than the platform noise, which is what makes the rounding stable
-- a value only lands differently on two platforms if it sits within 5e-15
(relative) of a rounding boundary, which is about one value in two million. A
report carries a few hundred numbers, so the chance any of them straddles a
boundary is on the order of 1e-4. Not zero, and the remedy if it ever bites is
fewer digits rather than more.

Full-precision values stay in the human-readable report. Only the canonical
form is hashed.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_EVEN, Context, Decimal
from typing import Any, Final

__all__ = [
    "CANONICAL_REPORT_SCHEMA_VERSION",
    "SIGNIFICANT_DIGITS",
    "canonical_number",
    "canonical_payload",
    "without_fields",
]

#: Bumped when the canonical rendering changes. A digest taken under one
#: rendering and compared under another would disagree about two identical
#: reports, so the rendering is part of what a hash means.
CANONICAL_REPORT_SCHEMA_VERSION = "certification-report-canonical/1"

#: Significant digits kept in the canonical rendering of a real number.
SIGNIFICANT_DIGITS: Final = 9

#: ``ROUND_HALF_EVEN`` because it is the IEEE default and, unlike half-up, does
#: not drift a set of values in one direction. The context rounds to
#: significant digits rather than decimal places, so the resolution follows the
#: magnitude: a residual of 6e-8 and a spot price of 7759.27 are both kept to
#: nine meaningful digits instead of one being flattened to zero.
_CONTEXT: Final = Context(prec=SIGNIFICANT_DIGITS, rounding=ROUND_HALF_EVEN)


def canonical_number(value: float) -> str:
    """One real number, as the digest sees it.

    A string rather than a float, deliberately: re-encoding the rounded value
    as a double would reintroduce a binary representation, and the point is to
    leave binary floating point behind before hashing.

    ``Decimal(value)`` is exact -- it takes the double's true binary value, not
    a decimal approximation of it -- so the only rounding is the one this
    function performs.
    """
    if math.isnan(value):
        # Distinguishable from every real value and stable as text. NaN appears
        # where a capture has no index snapshot to compare against, which is a
        # state worth hashing rather than an error.
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0.0:
        # ``-0.0`` and ``0.0`` are the same number and must not be two digests.
        return "0E+0"
    # ``normalize`` after rounding, because ``Decimal`` carries significance and
    # would otherwise render one number two ways. ``100.0`` rounds to ``100``
    # and the float above it rounds to ``100.000000``; both are the same value
    # to nine significant digits, and without this they are ``1.00E+2`` and
    # ``1.00000000E+2`` -- two digests for one rounded number, which is the
    # whole defect reappearing one layer down.
    rounded = _CONTEXT.create_decimal(Decimal(value))
    return format(rounded.normalize(_CONTEXT), "E")


def canonical_payload(value: Any) -> Any:
    """A report payload rewritten into the form that gets hashed.

    Recursive, and structure-preserving: keys, ordering of sequences and every
    non-real value pass through untouched. Integers stay integers because they
    are exact -- a contract count is not an approximation of anything -- and
    ``bool`` is checked before ``int`` because it is a subclass of it and
    ``True`` must not be rendered as ``1``.
    """
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return canonical_number(value)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {key: canonical_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical_payload(item) for item in value]
    # Enums, dates, paths and anything else a report carries. ``str`` is what
    # the digest already applied to them; doing it here keeps the canonical
    # payload free of objects whose repr could move.
    return str(value)


def without_fields(payload: dict[str, Any], paths: tuple[str, ...]) -> dict[str, Any]:
    """A copy of ``payload`` with dotted ``paths`` removed.

    For fields that describe *this machine* rather than the artifact: where a
    file was found, which checkout answered. They belong in the report an
    operator reads and not in the identity two operators compare.

    A path that is not present is not an error -- the caller declares what must
    never be hashed, and a report that never carried it is already compliant.
    """
    pruned = {
        key: dict(item) if isinstance(item, dict) else item
        for key, item in payload.items()
    }
    for path in paths:
        head, _, tail = path.partition(".")
        if head not in pruned:
            continue
        if not tail:
            pruned.pop(head)
        elif isinstance(pruned[head], dict):
            pruned[head] = without_fields(pruned[head], (tail,))
    return pruned
