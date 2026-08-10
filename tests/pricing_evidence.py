"""Observed vendor values for tests that need a settled configuration.

Some tests are about something else entirely -- open-interest provenance, spot
synchronisation, capture readiness -- and would otherwise be blocked by
unresolved *pricing* assumptions for reasons unrelated to what they check.

v2.1.3 handled that with ``dataclasses.replace(built, pricing_compatibility=
PricingCompatibilityReport(compatible=True, ...))``: the fixture asserted the
conclusion directly. v2.1.4 replaced that with attestations, which was better
and still wrong -- an attestation *was* the answer, so the fixture could record
a vendor convention that disagreed with the local model and still get MATCHED.

Here the fixture supplies **observed values**, and the production comparators
decide what follows. The values are derived from the ``ModelSpec`` they will be
compared against, so the fixture states agreement explicitly rather than
obtaining it by accident. A test that wants a mismatch says so by passing a
different value.

The evidence itself is fabricated and says so -- see
``tests/fixtures/vendor_conventions.md``. It is recorded as
``VENDOR_DOCUMENTATION``, which certification treats as a claim rather than an
observation, so nothing here can produce ``ADAPTER_CERTIFIED``.
"""

from __future__ import annotations

from typing import Any

from src.config.compatibility import EvidenceSource, PricingDimension
from src.domain.model_spec import ModelSpec

#: Where the invented answers are written down. A real path in the repository,
#: so a reader following the reference finds the disclaimer rather than nothing.
#: The loader rejects a reference that does not resolve.
FIXTURE_REFERENCE = "tests/fixtures/vendor_conventions.md"

#: Every dimension a static configuration cannot settle for itself.
#:
#: Since v2.1.5 this includes ``RATE_UNITS`` and ``DIVIDEND_CONVENTION``: the
#: adapter sends ``rate_value`` and ``annual_dividend`` but sends neither of
#: those two labels, so how the vendor *reads* the numbers is its own
#: convention. ``SOLVER_VERSION`` is included because it is raised as UNKNOWN,
#: though no comparator can settle it.
VENDOR_OWNED_DIMENSIONS = tuple(d for d in PricingDimension if d.vendor_owned)


def agreeing_value(dimension: PricingDimension, spec: ModelSpec) -> Any:
    """What the vendor would have to do for this dimension to agree.

    Read off the spec rather than written out, so the fixture cannot drift from
    the model the comparators check it against.
    """
    if dimension is PricingDimension.DAY_COUNT:
        return spec.day_count_convention.value
    if dimension is PricingDimension.MINIMUM_TIME_FLOOR:
        return spec.minimum_time_to_expiry_minutes
    if dimension is PricingDimension.IV_PRICE_BASIS:
        return spec.iv_price_source.value
    if dimension is PricingDimension.UNDERLYING_SOURCE:
        return spec.underlying_price_source.value
    if dimension is PricingDimension.UNDERLYING_TIMESTAMP:
        return "OPTION_QUOTE_INSTANT"
    if dimension is PricingDimension.EXPIRATION_TIMESTAMP:
        return spec.expiration_timestamp_rule.value
    if dimension is PricingDimension.RATE_UNITS:
        return "DECIMAL_ANNUAL_RATE"
    if dimension is PricingDimension.RISK_FREE_RATE:
        return spec.risk_free_rate
    if dimension is PricingDimension.DIVIDEND_CONVENTION:
        return (
            "ZERO_DIVIDEND"
            if not spec.dividend_yield
            else ("CONTINUOUS_DIVIDEND_YIELD")
        )
    if dimension is PricingDimension.DIVIDEND_VALUE:
        return spec.dividend_yield
    # SOLVER_VERSION and anything without a comparator: recording the vendor's
    # value settles nothing, and the report says so.
    return "not exposed"


def attestations(
    dimensions: tuple[PricingDimension, ...] = VENDOR_OWNED_DIMENSIONS,
    *,
    source: EvidenceSource = EvidenceSource.VENDOR_DOCUMENTATION,
    observed_at: str = "2026-08-01",
    spec: ModelSpec | None = None,
    overrides: dict[PricingDimension, Any] | None = None,
) -> list[dict[str, Any]]:
    """Observation entries in the shape ``parse_thetadata_config`` accepts."""
    model = spec if spec is not None else ModelSpec()
    stated = overrides or {}
    return [
        {
            "dimension": dimension.value,
            "source": source.value,
            "reference": FIXTURE_REFERENCE,
            "observed_at": observed_at,
            "vendor_value": stated.get(dimension, agreeing_value(dimension, model)),
            "note": "fabricated for tests; no vendor comparison has been run",
        }
        for dimension in dimensions
    ]


#: The rate and dividend *values* are settled by configuration: we choose the
#: numbers and we send them, so both sides of that comparison are ours.
#:
#: The unit is not ours, and until v2.1.22 this said ``4.2`` /
#: ``PERCENT_ANNUAL_RATE`` on the authority of the pinned OpenAPI description.
#: The first live capture was taken on that basis and priced at 420%: the v3
#: implementation consumes ``rate_value`` as a decimal. These fixtures now state
#: the same economic rate the way the vendor actually reads it, because a helper
#: that builds "a resolved pricing configuration" out of a combination the
#: vendor mis-prices is not building a resolved configuration.
CONFIGURED_PRICING_SETTINGS: dict[str, Any] = {
    "rate_value": 0.042,
    "rate_units": "DECIMAL_ANNUAL_RATE",
    "annual_dividend": 0.0,
    "dividend_convention": "ZERO_DIVIDEND",
}


def spec_for(**overrides: Any) -> ModelSpec:
    """The ModelSpec a ``resolved_settings`` config derives."""
    from src.config.thetadata import parse_thetadata_config

    settings = {**CONFIGURED_PRICING_SETTINGS}
    settings.update(overrides)
    settings.pop("pricing_attestations", None)
    return parse_thetadata_config(settings).to_model_spec()


def resolved_settings(**overrides: Any) -> dict[str, Any]:
    """A ``thetadata:`` mapping whose pricing dimensions all resolve."""
    settings: dict[str, Any] = {**CONFIGURED_PRICING_SETTINGS}
    settings.update(overrides)
    settings.setdefault(
        "pricing_attestations", attestations(spec=spec_for(**overrides))
    )
    return settings
