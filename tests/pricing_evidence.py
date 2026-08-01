"""Typed pricing evidence for tests that need a settled configuration.

Some tests are about something else entirely -- open-interest provenance, spot
synchronisation, capture readiness -- and would otherwise be blocked by
unresolved *pricing* assumptions for reasons unrelated to what they check.

v2.1.3 handled that with ``dataclasses.replace(built, pricing_compatibility=
PricingCompatibilityReport(compatible=True, ...))``: the fixture asserted the
conclusion directly, so the tests passed whether or not any production code could
have reached it. The report carried a settable ``compatible`` flag, which is what
made that possible.

Here the fixture supplies **evidence** in the shape the configuration file
accepts, and the production ``assess_pricing_compatibility`` decides what follows
from it. If the resolution path regresses, these tests fail; if it silently stops
requiring evidence, the negative tests below fail instead.

The evidence itself is fabricated and says so -- see
``tests/fixtures/vendor_conventions.md``. It is recorded as
``VENDOR_DOCUMENTATION``, which certification treats as a claim rather than an
observation, so nothing here can produce ``ADAPTER_CERTIFIED``.
"""

from __future__ import annotations

from typing import Any

from src.config.compatibility import EvidenceSource, PricingDimension

#: Where the invented answers are written down. A real path in the repository, so
#: a reader following the reference finds the disclaimer rather than nothing.
FIXTURE_REFERENCE = "tests/fixtures/vendor_conventions.md"

#: The dimensions ThetaData does not publish inline, which
#: ``assess_pricing_compatibility`` therefore raises as UNKNOWN on every
#: vendor-IV session.
UNDOCUMENTED_DIMENSIONS = (
    PricingDimension.IV_PRICE_BASIS,
    PricingDimension.UNDERLYING_SOURCE,
    PricingDimension.UNDERLYING_TIMESTAMP,
    PricingDimension.EXPIRATION_TIMESTAMP,
    PricingDimension.DAY_COUNT,
    PricingDimension.MINIMUM_TIME_FLOOR,
    PricingDimension.SOLVER_VERSION,
)

#: What each invented answer is, so the attestation records a value rather than
#: only the fact that somebody looked.
FABRICATED_ANSWERS: dict[PricingDimension, str] = {
    PricingDimension.IV_PRICE_BASIS: "NBBO_MIDPOINT",
    PricingDimension.UNDERLYING_SOURCE: "INDEX_PRINT",
    PricingDimension.UNDERLYING_TIMESTAMP: "OPTION_QUOTE_INSTANT",
    PricingDimension.EXPIRATION_TIMESTAMP: "16:00 America/New_York",
    PricingDimension.DAY_COUNT: "ACT/365F",
    PricingDimension.MINIMUM_TIME_FLOOR: "60 minutes",
    PricingDimension.SOLVER_VERSION: "not exposed",
}


def attestations(
    dimensions: tuple[PricingDimension, ...] = UNDOCUMENTED_DIMENSIONS,
    *,
    source: EvidenceSource = EvidenceSource.VENDOR_DOCUMENTATION,
    observed_at: str = "2026-07-31",
) -> list[dict[str, Any]]:
    """Attestation entries in the shape ``parse_thetadata_config`` accepts."""
    return [
        {
            "dimension": dimension.value,
            "source": source.value,
            "reference": FIXTURE_REFERENCE,
            "observed_at": observed_at,
            "vendor_value": FABRICATED_ANSWERS[dimension],
            "note": "fabricated for tests; no vendor comparison has been run",
        }
        for dimension in dimensions
    ]


#: The rate and dividend halves are settled by configuration rather than by
#: attestation: both sides are ours to state, so there is a real answer.
CONFIGURED_PRICING_SETTINGS: dict[str, Any] = {
    "rate_value": 4.2,
    "rate_units": "PERCENT_ANNUAL_RATE",
    "annual_dividend": 0.0,
    "dividend_convention": "ZERO_DIVIDEND",
}


def resolved_settings(**overrides: Any) -> dict[str, Any]:
    """A ``thetadata:`` mapping whose pricing dimensions all resolve."""
    settings: dict[str, Any] = {
        **CONFIGURED_PRICING_SETTINGS,
        "pricing_attestations": attestations(),
    }
    settings.update(overrides)
    return settings
