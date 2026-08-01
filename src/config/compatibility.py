"""Typed vendor/local pricing compatibility.

v2.1.3 stored compatibility as sentences and decided which unknowns mattered by
searching those sentences for substrings::

    if any(name in field for name in LOAD_BEARING_COMPATIBILITY_FIELDS)

So whether a session was allowed to compute depended on how a message had been
worded. Rewording "risk_free_rate: units undocumented" to "the interest rate
convention is not published" silently turned a blocker into a warning -- the
sentence no longer contained ``risk_free_rate`` -- and nothing failed.

Prose also entered the replay hash, which meant a documentation edit changed a
snapshot digest while a genuine change in what was checked might not.

Here, whether a dimension is load-bearing is a **field on the result**, set where
the dimension is defined. The wording is carried alongside for humans and is
excluded from every decision and every hash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = [
    "AttestationError",
    "CompatibilityEvidence",
    "CompatibilityStatus",
    "EvidenceSource",
    "PricingAssumptionAttestation",
    "PricingCompatibilityReport",
    "PricingDimension",
    "PricingDimensionResult",
    "apply_attestations",
]


class AttestationError(ValueError):
    """A claim about a vendor convention that is not usable as evidence."""


class PricingDimension(str, Enum):
    """One thing the vendor and the local model must agree about.

    ``load_bearing`` is a property of the dimension, not of a sentence about it:
    these are the inputs that change gamma, and a disagreement or an unknown in
    any of them means the resulting number has no stated meaning.
    """

    IV_PRICE_BASIS = "IV_PRICE_BASIS"
    UNDERLYING_SOURCE = "UNDERLYING_SOURCE"
    UNDERLYING_TIMESTAMP = "UNDERLYING_TIMESTAMP"
    RISK_FREE_RATE = "RISK_FREE_RATE"
    RATE_UNITS = "RATE_UNITS"
    DIVIDEND_CONVENTION = "DIVIDEND_CONVENTION"
    DIVIDEND_VALUE = "DIVIDEND_VALUE"
    EXPIRATION_TIMESTAMP = "EXPIRATION_TIMESTAMP"
    DAY_COUNT = "DAY_COUNT"
    MINIMUM_TIME_FLOOR = "MINIMUM_TIME_FLOOR"
    SOLVER_VERSION = "SOLVER_VERSION"

    @property
    def load_bearing(self) -> bool:
        """Whether a disagreement here changes the gamma.

        ``SOLVER_VERSION`` is the only one that does not on its own: two solver
        versions agreeing on every input should agree on the answer, and if they
        do not, one of the *other* dimensions is what actually differs. It stays
        a warning so the version is still recorded.
        """
        return self is not PricingDimension.SOLVER_VERSION


class CompatibilityStatus(str, Enum):
    """What is known about one dimension."""

    MATCHED = "MATCHED"
    MISMATCHED = "MISMATCHED"
    #: Checked, and the answer is not available. Distinct from MISMATCHED --
    #: different remedy -- but equally blocking on a load-bearing dimension.
    UNKNOWN = "UNKNOWN"
    #: The dimension does not apply in this configuration.
    NOT_APPLICABLE = "NOT_APPLICABLE"

    @property
    def is_resolved(self) -> bool:
        return self in (CompatibilityStatus.MATCHED, CompatibilityStatus.NOT_APPLICABLE)


class EvidenceSource(str, Enum):
    """Where an answer about a vendor convention came from.

    The three are not interchangeable, and certification treats them
    differently: documentation says what the vendor claims, a live comparison
    says what the vendor did. Only the second is an observation.
    """

    #: A statement in vendor documentation. Records a claim, not a measurement.
    VENDOR_DOCUMENTATION = "VENDOR_DOCUMENTATION"
    #: A recorded comparison against real vendor output. An observation.
    LIVE_COMPARISON = "LIVE_COMPARISON"
    #: Settled by our own configuration, with no vendor side to disagree.
    LOCAL_CONFIGURATION = "LOCAL_CONFIGURATION"

    @property
    def is_observation(self) -> bool:
        """Whether this evidence records what the vendor actually did."""
        return self is EvidenceSource.LIVE_COMPARISON

    @property
    def rests_on_a_vendor_claim(self) -> bool:
        """Whether the answer is only as good as the vendor's own description.

        ``LOCAL_CONFIGURATION`` does not: both sides of the comparison are ours,
        so there is no vendor statement to be wrong about.
        """
        return self is EvidenceSource.VENDOR_DOCUMENTATION


@dataclass(frozen=True, slots=True)
class CompatibilityEvidence:
    """Why a dimension was resolved the way it was.

    Required to move a load-bearing dimension to ``MATCHED``: the production
    validator will not accept a bare assertion. ``source`` names where the answer
    came from and ``reference`` points at it.
    """

    source: EvidenceSource
    reference: str = ""
    observed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "reference": self.reference,
            "observed_at": self.observed_at,
        }

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class PricingDimensionResult:
    """One dimension, decided.

    ``detail`` is for humans. It takes no part in ``blocks_calculation``, in the
    report's aggregate state, or in any hash.
    """

    dimension: PricingDimension
    status: CompatibilityStatus
    code: str
    vendor_value: object | None = None
    local_value: object | None = None
    evidence: CompatibilityEvidence | None = None
    detail: str = ""
    #: Defaults to the dimension's own answer. Overridable only to make a
    #: dimension *less* blocking in a configuration where it genuinely does not
    #: apply, never to smuggle a load-bearing unknown past a check.
    load_bearing_override: bool | None = None

    @property
    def load_bearing(self) -> bool:
        if self.load_bearing_override is not None:
            return self.load_bearing_override
        return self.dimension.load_bearing

    @property
    def blocks_calculation(self) -> bool:
        """A load-bearing dimension that is not resolved."""
        return self.load_bearing and not self.status.is_resolved

    @property
    def is_hard_failure(self) -> bool:
        """A load-bearing dimension we checked and found to disagree.

        Worse than unknown: we know the two models differ, so mixing them
        produces a number that is wrong rather than merely unexplained.
        """
        return self.load_bearing and self.status is CompatibilityStatus.MISMATCHED

    def semantic_payload(self) -> dict[str, Any]:
        """Everything that decides behaviour. No prose.

        This is what enters the replay hash: dimension, status, code, the
        normalised values and the evidence fingerprint. ``detail`` is absent by
        construction, so rewording it cannot move a digest.
        """
        return {
            "dimension": self.dimension.value,
            "status": self.status.value,
            "code": self.code,
            "load_bearing": self.load_bearing,
            "vendor_value": _normalise(self.vendor_value),
            "local_value": _normalise(self.local_value),
            "evidence": self.evidence.fingerprint if self.evidence else None,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.semantic_payload(), "detail": self.detail}


def _normalise(value: object | None) -> Any:
    """A JSON-stable form, so equal values hash equally."""
    # Enums first: every enum here subclasses ``str``, so the isinstance check
    # below would return the member object and json would serialise it by its
    # ``str`` value on one path and its ``repr`` on another.
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        # 12 significant figures: far tighter than any change of substance, and
        # stable across platforms. Matches the snapshot hash convention.
        return float(f"{value:.12g}")
    return str(value)


@dataclass(frozen=True, slots=True)
class PricingAssumptionAttestation:
    """A recorded answer to one dimension the vendor does not publish inline.

    This is the *only* production route from ``UNKNOWN`` to ``MATCHED``, and it
    is deliberately not a boolean. Constructing one requires naming where the
    answer came from, pointing at it, and saying when it was established; a
    caller who cannot supply those has not resolved anything.

    It cannot overturn a ``MISMATCHED`` dimension. An attestation says "the
    question has been answered", not "the disagreement does not matter" -- and a
    measured disagreement is not a question.
    """

    dimension: PricingDimension
    evidence: CompatibilityEvidence
    #: What the vendor's convention turned out to be. Recorded so a later
    #: attestation that contradicts this one is visible rather than silent.
    vendor_value: object | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, PricingDimension):
            raise AttestationError(
                f"dimension must be a PricingDimension, got {self.dimension!r}"
            )
        if not isinstance(self.evidence, CompatibilityEvidence):
            raise AttestationError(
                "evidence must be a CompatibilityEvidence; a string or a boolean "
                "is an assertion, not evidence"
            )
        if not isinstance(self.evidence.source, EvidenceSource):
            raise AttestationError(
                f"evidence.source must be an EvidenceSource, got "
                f"{self.evidence.source!r}"
            )
        if not self.evidence.reference.strip():
            raise AttestationError(
                f"{self.dimension.value}: evidence.reference is empty. Point at "
                "the documentation section, the comparison run, or the config "
                "key that settles this -- an unreferenced attestation cannot be "
                "checked by anyone reading the certification report."
            )
        if not self.evidence.observed_at.strip():
            raise AttestationError(
                f"{self.dimension.value}: evidence.observed_at is empty. Vendor "
                "conventions change; an answer with no date cannot be known to "
                "still hold."
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "evidence": self.evidence.as_dict(),
            "vendor_value": _normalise(self.vendor_value),
            "note": self.note,
        }


def apply_attestations(
    report: PricingCompatibilityReport,
    attestations: tuple[PricingAssumptionAttestation, ...],
) -> PricingCompatibilityReport:
    """Fold recorded answers into an assessment.

    Three outcomes, all of them explicit:

    * an ``UNKNOWN`` load-bearing dimension becomes ``MATCHED``, carrying the
      evidence, so the certification report can say *how* it was settled;
    * an attestation aimed at a ``MISMATCHED`` dimension becomes a hard failure,
      because overriding a measured disagreement is the one thing this must
      never allow;
    * an attestation for a dimension the assessment did not raise is a warning,
      since a claim nobody asked for usually means the config drifted.
    """
    by_dimension = {d.dimension: d for d in report.dimensions}
    resolved: list[PricingDimensionResult] = []
    hard_failures: list[str] = []
    warnings: list[str] = []

    for attestation in attestations:
        current = by_dimension.get(attestation.dimension)
        if current is None:
            warnings.append(
                f"UNUSED_ATTESTATION: {attestation.dimension.value} was not "
                "raised by this assessment"
            )
            continue
        if current.status is CompatibilityStatus.MISMATCHED:
            hard_failures.append(
                f"ATTESTATION_CANNOT_OVERRIDE_MISMATCH:{attestation.dimension.value}"
            )
            continue
        if current.status is not CompatibilityStatus.UNKNOWN:
            warnings.append(
                f"REDUNDANT_ATTESTATION: {attestation.dimension.value} was "
                f"already {current.status.value}"
            )
            continue
        resolved.append(
            PricingDimensionResult(
                dimension=attestation.dimension,
                status=CompatibilityStatus.MATCHED,
                code=f"ATTESTED_{attestation.evidence.source.value}",
                vendor_value=attestation.vendor_value,
                local_value=current.local_value,
                evidence=attestation.evidence,
                detail=attestation.note,
            )
        )

    return report.merged_with(
        PricingCompatibilityReport(
            dimensions=tuple(resolved),
            hard_failures=tuple(hard_failures),
            warnings=tuple(warnings),
        )
    )


@dataclass(frozen=True, slots=True)
class PricingCompatibilityReport:
    """Whether vendor-derived numbers may enter a local calculation.

    Every aggregate below is *derived* from ``dimensions``. v2.1.3 carried a
    ``compatible`` boolean that a caller could set independently of the findings,
    so a report could claim compatibility while listing unresolved fields.
    """

    dimensions: tuple[PricingDimensionResult, ...] = ()
    #: Failures that are not about a single dimension -- an unsupported mode, a
    #: tier that cannot serve the request. Always honoured.
    hard_failures: tuple[str, ...] = ()
    #: Human notes. Never consulted by any decision.
    warnings: tuple[str, ...] = ()

    @property
    def compatible(self) -> bool:
        """Derived, never assigned."""
        return not self.hard_failures and not self.blocking_dimensions

    @property
    def blocking_dimensions(self) -> tuple[PricingDimensionResult, ...]:
        return tuple(
            sorted(
                (d for d in self.dimensions if d.blocks_calculation),
                key=lambda d: d.dimension.value,
            )
        )

    @property
    def mismatched(self) -> tuple[PricingDimensionResult, ...]:
        return tuple(
            sorted(
                (
                    d
                    for d in self.dimensions
                    if d.status is CompatibilityStatus.MISMATCHED
                ),
                key=lambda d: d.dimension.value,
            )
        )

    @property
    def unknown(self) -> tuple[PricingDimensionResult, ...]:
        return tuple(
            sorted(
                (d for d in self.dimensions if d.status is CompatibilityStatus.UNKNOWN),
                key=lambda d: d.dimension.value,
            )
        )

    @property
    def load_bearing_unknowns(self) -> tuple[PricingDimension, ...]:
        return tuple(
            d.dimension
            for d in self.blocking_dimensions
            if d.status is CompatibilityStatus.UNKNOWN
        )

    @property
    def load_bearing_mismatches(self) -> tuple[PricingDimension, ...]:
        return tuple(d.dimension for d in self.dimensions if d.is_hard_failure)

    def merged_with(
        self, other: PricingCompatibilityReport
    ) -> PricingCompatibilityReport:
        """Combine two partial assessments. Later results win per dimension."""
        by_dimension = {d.dimension: d for d in self.dimensions}
        by_dimension.update({d.dimension: d for d in other.dimensions})
        return PricingCompatibilityReport(
            dimensions=tuple(
                sorted(by_dimension.values(), key=lambda d: d.dimension.value)
            ),
            hard_failures=tuple(sorted({*self.hard_failures, *other.hard_failures})),
            warnings=tuple(sorted({*self.warnings, *other.warnings})),
        )

    def semantic_payload(self) -> dict[str, Any]:
        """What enters the replay hash. Sorted, typed, prose-free."""
        return {
            "compatible": self.compatible,
            "dimensions": [
                d.semantic_payload()
                for d in sorted(self.dimensions, key=lambda d: d.dimension.value)
            ],
            # Codes only: the hard-failure strings are identifiers, and are
            # deduplicated and sorted so ordering cannot move a digest.
            "hard_failures": sorted(set(self.hard_failures)),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "load_bearing_unknowns": [d.value for d in self.load_bearing_unknowns],
            "load_bearing_mismatches": [d.value for d in self.load_bearing_mismatches],
            "warnings": list(self.warnings),
            "dimension_detail": {
                d.dimension.value: d.detail for d in self.dimensions if d.detail
            },
        }
