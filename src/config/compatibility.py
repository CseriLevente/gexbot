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
import pathlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
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
    "VendorObservation",
    "apply_attestations",
    "compare_observation",
    "derive_post_capture_compatibility",
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

    @property
    def vendor_owned(self) -> bool:
        """Whether the answer is the vendor's to give.

        The test is narrow and mechanical: **do we send this to the vendor?**

        We send ``rate_value`` and ``annual_dividend``. Those two numbers are
        ours, both sides of the comparison are ours, and there is no vendor
        statement to be right or wrong about.

        We do **not** send ``rate_units`` or ``dividend_convention``. v2.1.4
        treated them as locally owned on the reasoning that we configure them --
        but configuring a label for a number does not tell the vendor how to read
        it. ``rate_value: 4.2`` is 4.2% or 420% depending on a convention that
        lives entirely inside the vendor's API, and writing ``rate_units:
        DECIMAL_ANNUAL_RATE`` in our YAML expresses a hope. A local declaration
        cannot settle a remote semantic.

        The distinction decides which evidence is admissible:
        ``LOCAL_CONFIGURATION`` cannot settle a vendor-owned dimension, because
        there is nothing local to read.
        """
        return self not in (
            PricingDimension.RISK_FREE_RATE,
            PricingDimension.DIVIDEND_VALUE,
        )


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
    #: SHA-256 of what the reference pointed at when it was read. Empty for a
    #: reference this repository cannot open offline -- a URL, or a live
    #: comparison, which is bound to a manifest instead.
    #:
    #: A reference says where somebody looked. The hash says what was there. A
    #: vendor can rewrite a documentation page without changing its URL, and
    #: until v2.1.8 that rewrite moved nothing: the convention it established
    #: still resolved a load-bearing pricing dimension, and every fingerprint
    #: downstream was identical.
    document_content_hash: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "reference": self.reference,
            "observed_at": self.observed_at,
            "document_content_hash": self.document_content_hash,
        }

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


#: How far ahead of today an observation may be dated before it is a typo
#: rather than a timezone. Generous by a day either way; a year is not.
OBSERVATION_FUTURE_TOLERANCE_DAYS = 2


def _parse_observed_on(raw: object, dimension: PricingDimension) -> date:
    """Read ``observed_at`` as a real calendar date, or refuse the observation.

    Four different refusals because they are four different mistakes: nothing
    was written, what was written is not a date, it is a date that does not
    exist, or it is a date that has not happened.
    """
    text = str(raw or "").strip()
    if not text:
        raise AttestationError(
            f"{dimension.value}: observed_at is empty. Vendor conventions "
            "change; an answer with no date cannot be known to still hold."
        )
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise AttestationError(
            f"{dimension.value}: observed_at {text!r} is not an ISO date "
            f"(YYYY-MM-DD): {error}"
        ) from error
    ahead = (parsed - datetime.now(UTC).date()).days
    if ahead > OBSERVATION_FUTURE_TOLERANCE_DAYS:
        raise AttestationError(
            f"{dimension.value}: observed_at {text} is {ahead} days in the "
            "future. An observation that has not happened cannot be evidence "
            "of what the vendor does."
        )
    return parsed


@dataclass(frozen=True, slots=True)
class VendorObservation:
    """What the vendor's convention was observed to be. Not what follows from it.

    The v2.1.4 defect this replaces: ``PricingAssumptionAttestation`` set a
    dimension to ``MATCHED``. It carried a ``vendor_value`` field and nothing
    ever read it, so recording that the vendor uses ACT/360 while the local
    model uses ACT/365F produced ``MATCHED`` -- the object's *presence* was the
    answer. Observing a disagreement is the thing evidence most needs to be able
    to express, and it was the one thing this could not say.

    An observation states a value and where it came from. What follows is
    ``compare_observation``'s to decide.
    """

    dimension: PricingDimension
    #: What the vendor does. Required: an observation of nothing is not one.
    observed_value: object
    source: EvidenceSource
    reference: str
    observed_at: str
    #: Set by ``AdapterValidator`` for a live comparison, so the observation can
    #: be traced to the exact bytes it was read from. Empty for documentation.
    record_ids: tuple[str, ...] = ()
    manifest_hash: str = ""
    note: str = ""
    #: Derived, never supplied. See ``document_content_hash`` below.
    document_content_hash: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, PricingDimension):
            raise AttestationError(
                f"dimension must be a PricingDimension, got {self.dimension!r}"
            )
        if not isinstance(self.source, EvidenceSource):
            raise AttestationError(
                f"source must be an EvidenceSource, got {self.source!r}; a string "
                "is an assertion, not a provenance"
            )
        if self.observed_value is None or (
            isinstance(self.observed_value, str) and not self.observed_value.strip()
        ):
            raise AttestationError(
                f"{self.dimension.value}: observed_value is empty. An observation "
                "that records no value has not observed anything, and there is "
                "nothing for a comparator to compare."
            )
        if not self.reference.strip():
            raise AttestationError(
                f"{self.dimension.value}: reference is empty. Point at the "
                "documentation section, the comparison run, or the config key -- "
                "an unreferenced observation cannot be checked by anyone reading "
                "the certification report."
            )
        _parse_observed_on(self.observed_at, self.dimension)
        if self.source is EvidenceSource.LIVE_COMPARISON and not self.manifest_hash:
            raise AttestationError(
                f"{self.dimension.value}: a LIVE_COMPARISON observation must name "
                "the capture it was read from. Evidence of what the vendor did is "
                "evidence about specific bytes."
            )
        # Derived rather than accepted -- ``init=False`` above. A
        # caller-supplied content hash is a claim about a document, which is
        # precisely what the hash exists to stop being a claim.
        object.__setattr__(
            self, "document_content_hash", _referenced_content_hash(self)
        )

    @property
    def observed_on(self) -> date:
        """When this was observed, as a date rather than as characters.

        v2.1.5 stored ``observed_at`` as whatever string the file carried and
        never read it, so ``"not-a-date"`` and ``"2026-02-30"`` loaded happily
        and a reader deciding whether a convention was still current had
        nothing to decide with. Validated at construction; parsed here.
        """
        return _parse_observed_on(self.observed_at, self.dimension)

    @property
    def evidence(self) -> CompatibilityEvidence:
        return CompatibilityEvidence(
            source=self.source,
            reference=self.reference,
            # Canonical ISO, whatever spelling the file used.
            observed_at=self.observed_on.isoformat(),
            document_content_hash=self.document_content_hash,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "observed_value": _normalise(self.observed_value),
            "observed_on": self.observed_on.isoformat(),
            "evidence": self.evidence.as_dict(),
            "record_ids": list(self.record_ids),
            "manifest_hash": self.manifest_hash,
            "note": self.note,
        }


#: References this repository can open without a network. Anything else -- a URL
#: above all -- is a pointer to something only the vendor controls, and the
#: report says so rather than pretending to a hash it never computed.
_UNHASHABLE_PREFIXES = ("http://", "https://")


def _referenced_content_hash(observation: VendorObservation) -> str:
    """SHA-256 of the document a documentation observation rests on.

    Empty where there is nothing to hash: a live comparison is bound to a
    manifest, a local configuration has no vendor document behind it, and a URL
    cannot be read offline. Empty is the honest answer in each case, and the
    certification report distinguishes it from a hash.
    """
    if observation.source is not EvidenceSource.VENDOR_DOCUMENTATION:
        return ""
    text = observation.reference.strip()
    if not text or text.startswith(_UNHASHABLE_PREFIXES):
        return ""
    # A fragment names a section of a document, not a different document.
    target = pathlib.Path(text.split("#", 1)[0])
    if target.is_absolute() or ".." in target.parts:
        return ""
    root = pathlib.Path(__file__).resolve().parents[2]
    resolved = root / target
    if not resolved.is_file():
        return ""
    return hashlib.sha256(resolved.read_bytes()).hexdigest()


#: v2.1.4 name. The type is different -- it no longer carries a verdict -- but
#: the role is the same, and the old name reads correctly at call sites.
PricingAssumptionAttestation = VendorObservation


def _as_float(value: object) -> float | None:
    """A number, or nothing. Never a guess."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_token(value: object) -> str:
    """Compare conventions as case-insensitive tokens, not as prose."""
    text = value.value if isinstance(value, Enum) else str(value)
    return text.strip().upper().replace(" ", "_")


#: How close two rates have to be to be the same rate.
RATE_TOLERANCE = 1e-9
#: And two dividends.
DIVIDEND_TOLERANCE = 1e-9
#: And two time floors, in minutes.
FLOOR_TOLERANCE = 1e-9


def compare_observation(
    observation: VendorObservation, spec: Any
) -> PricingDimensionResult:
    """Compare what the vendor does against what the local model does.

    The comparator is per dimension because the comparison is: a day count is a
    token, a time floor is a number of minutes, and a solver version has no
    local counterpart at all. Where there is nothing meaningful to compare
    against, the answer stays ``UNKNOWN`` -- an observation we cannot interpret
    is not agreement.
    """
    dimension = observation.dimension
    observed = observation.observed_value
    evidence = observation.evidence

    def result(
        status: CompatibilityStatus, code: str, local: object | None, detail: str = ""
    ) -> PricingDimensionResult:
        return PricingDimensionResult(
            dimension=dimension,
            status=status,
            code=code,
            vendor_value=observed,
            local_value=local,
            evidence=evidence if status is not CompatibilityStatus.UNKNOWN else None,
            detail=detail,
        )

    def tokens(local: object, matched: str, differs: str) -> PricingDimensionResult:
        if _as_token(observed) == _as_token(local):
            return result(CompatibilityStatus.MATCHED, matched, local)
        return result(
            CompatibilityStatus.MISMATCHED,
            differs,
            local,
            detail=(
                f"the vendor uses {observed!r} where the local model uses "
                f"{local!r}; these are different conventions, not different "
                "spellings"
            ),
        )

    def numbers(
        local: float | None, tolerance: float, matched: str, differs: str
    ) -> PricingDimensionResult:
        vendor_number = _as_float(observed)
        if vendor_number is None:
            return result(
                CompatibilityStatus.UNKNOWN,
                "OBSERVED_VALUE_NOT_A_NUMBER",
                local,
                detail=f"{observed!r} could not be read as a number",
            )
        if local is None:
            return result(
                CompatibilityStatus.UNKNOWN, "NO_COMPARABLE_LOCAL_VALUE", None
            )
        if abs(vendor_number - local) > tolerance:
            return result(
                CompatibilityStatus.MISMATCHED,
                differs,
                local,
                detail=f"vendor {vendor_number} vs local {local}",
            )
        return result(CompatibilityStatus.MATCHED, matched, local)

    if dimension is PricingDimension.DAY_COUNT:
        return tokens(
            spec.day_count_convention, "DAY_COUNT_AGREES", "DAY_COUNT_DIFFERS"
        )
    if dimension is PricingDimension.MINIMUM_TIME_FLOOR:
        return numbers(
            float(spec.minimum_time_to_expiry_minutes),
            FLOOR_TOLERANCE,
            "TIME_FLOOR_AGREES",
            "TIME_FLOOR_DIFFERS",
        )
    if dimension is PricingDimension.IV_PRICE_BASIS:
        return tokens(
            spec.iv_price_source, "IV_PRICE_BASIS_AGREES", "IV_PRICE_BASIS_DIFFERS"
        )
    if dimension is PricingDimension.UNDERLYING_SOURCE:
        return tokens(
            spec.underlying_price_source,
            "UNDERLYING_SOURCE_AGREES",
            "UNDERLYING_SOURCE_DIFFERS",
        )
    if dimension is PricingDimension.UNDERLYING_TIMESTAMP:
        # The local model reads the underlying at the option quote instant. A
        # vendor that reads it anywhere else is pricing against a different
        # spot, whatever the two prints happen to be worth today.
        return tokens(
            "OPTION_QUOTE_INSTANT",
            "UNDERLYING_TIMESTAMP_AGREES",
            "UNDERLYING_TIMESTAMP_DIFFERS",
        )
    if dimension is PricingDimension.EXPIRATION_TIMESTAMP:
        return tokens(
            spec.expiration_timestamp_rule,
            "EXPIRATION_RULE_AGREES",
            "EXPIRATION_RULE_DIFFERS",
        )
    if dimension is PricingDimension.RISK_FREE_RATE:
        return numbers(
            _as_float(spec.risk_free_rate),
            RATE_TOLERANCE,
            "RATE_VALUE_AGREES",
            "RATE_VALUE_DIFFERS",
        )
    if dimension is PricingDimension.RATE_UNITS:
        # The local model always states its rate as a decimal. The question is
        # how the vendor reads the number we send it.
        return tokens("DECIMAL_ANNUAL_RATE", "RATE_UNITS_AGREE", "RATE_UNITS_DIFFER")
    if dimension is PricingDimension.DIVIDEND_CONVENTION:
        local_convention = (
            "ZERO_DIVIDEND"
            if _as_float(spec.dividend_yield) in (0.0, None)
            else "CONTINUOUS_DIVIDEND_YIELD"
        )
        return tokens(
            local_convention,
            "DIVIDEND_CONVENTION_AGREES",
            "DIVIDEND_CONVENTION_DIFFERS",
        )
    if dimension is PricingDimension.DIVIDEND_VALUE:
        return numbers(
            _as_float(spec.dividend_yield),
            DIVIDEND_TOLERANCE,
            "DIVIDEND_VALUE_AGREES",
            "DIVIDEND_VALUE_DIFFERS",
        )

    # SOLVER_VERSION and anything added later without a comparator. There is no
    # local solver version to compare against, so an observation of the vendor's
    # is recorded and settles nothing.
    return result(
        CompatibilityStatus.UNKNOWN,
        "NO_LOCAL_COUNTERPART",
        None,
        detail=(
            f"{dimension.value} has no local counterpart to compare against, so "
            "observing the vendor's value records it without settling anything"
        ),
    )


def apply_attestations(
    report: PricingCompatibilityReport,
    attestations: tuple[VendorObservation, ...],
    spec: Any,
) -> PricingCompatibilityReport:
    """Fold observed vendor values into an assessment, and compare them.

    Three outcomes, all of them explicit:

    * an ``UNKNOWN`` load-bearing dimension gets a *comparison*: the observed
      vendor value against the local model's, which may agree, disagree, or be
      uninterpretable. v2.1.4 wrote ``MATCHED`` unconditionally here;
    * an attestation aimed at a ``MISMATCHED`` dimension becomes a hard failure,
      because overriding a measured disagreement is the one thing this must
      never allow;
    * an attestation for a dimension the assessment did not raise is a warning,
      since a claim nobody asked for usually means the config drifted.

    ``LOCAL_CONFIGURATION`` evidence is refused on a vendor-owned dimension. It
    means "settled by our own configuration, with no vendor side to disagree",
    which is true of the rate and the dividend and false of everything the
    vendor did inside its solver. Without this, an operator could write
    ``source: LOCAL_CONFIGURATION`` against ``DAY_COUNT`` and the certification
    ladder would treat a YAML edit as an observation of vendor behaviour.
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
        if (
            attestation.dimension.vendor_owned
            and attestation.source is EvidenceSource.LOCAL_CONFIGURATION
        ):
            hard_failures.append(
                f"LOCAL_EVIDENCE_CANNOT_SETTLE_A_VENDOR_CONVENTION:"
                f"{attestation.dimension.value}"
            )
            continue
        # The comparator decides, not the observation. v2.1.4 wrote MATCHED
        # here, so an observation recording that the vendor uses ACT/360 against
        # a local ACT/365F settled the dimension as agreement.
        resolved.append(compare_observation(attestation, spec))

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


# =============================================================================
# Post-capture -- what the bytes showed, folded into what gates the calculation
# =============================================================================


@dataclass(frozen=True, slots=True)
class PostCaptureCompatibilityReport(PricingCompatibilityReport):
    """The static assessment, revised by what a verified capture observed.

    ``AdapterValidator`` built ``VendorObservation`` objects out of the captured
    payloads and nothing consumed them. The report ``compute_trusted_gex``
    consulted was the one derived from configuration alone, so a capture that
    observed the vendor's underlying source left the gate reading ``UNKNOWN``,
    and -- worse -- a *disagreement* found in the bytes could not block
    anything.

    The provenance travels with the verdict: which capture, which validator,
    and which records each revision rests on. A revised status whose evidence
    cannot be named is not an improvement on an unrevised one.
    """

    manifest_hash: str = ""
    validator_version: str = ""
    #: Per dimension, the records the live observation was read from.
    observed_record_ids: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: Per dimension, what the status was before the capture spoke.
    base_statuses: tuple[tuple[str, str], ...] = ()
    #: Dimensions a live observation was rejected for, and why.
    rejected_observations: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        # Named explicitly rather than through zero-argument ``super()``:
        # ``slots=True`` rebuilds the class object, which leaves the implicit
        # ``__class__`` cell pointing at the discarded one.
        return {
            **PricingCompatibilityReport.as_dict(self),
            "manifest_hash": self.manifest_hash,
            "validator_version": self.validator_version,
            "observed_record_ids": {
                dimension: list(ids) for dimension, ids in self.observed_record_ids
            },
            "base_status": dict(self.base_statuses),
            "rejected_observations": list(self.rejected_observations),
        }


def derive_post_capture_compatibility(
    base_report: PricingCompatibilityReport,
    validation_report: Any,
    model_spec: Any,
    manifest: Any,
) -> PostCaptureCompatibilityReport:
    """Fold a capture's verified observations into the static assessment.

    Two rules, and the asymmetry between them is the point:

    * a live **mismatch** overrides anything, including a documented match. What
      the vendor did beats what its documentation said it would do;
    * a live **match** only fills an ``UNKNOWN``. Letting one agree with a
      dimension already measured as disagreeing would launder the disagreement,
      which is the one thing evidence must never do.

    An observation is admitted only behind a **passing** check that names it,
    reads the same manifest, names records this capture holds, measured a
    uniform chain where a chain-level answer was required, and is itself part of
    the report's semantic payload. Each of those is a way v2.1.6 could be
    talked round:

    * it iterated the checks and never looked at ``check.passed``, so a check
      that had *failed* still handed its observation to the comparator -- and a
      chain measured as ``MIXED_ACROSS_CHAIN`` could revise a dimension;
    * ``pricing_observations`` was outside ``semantic_payload``, so an
      observation could be edited without the re-derivation noticing.
    """
    if validation_report is None or manifest is None:
        return PostCaptureCompatibilityReport(
            dimensions=base_report.dimensions,
            hard_failures=base_report.hard_failures,
            warnings=base_report.warnings,
            base_statuses=_statuses(base_report),
        )

    by_dimension = {d.dimension: d for d in base_report.dimensions}
    observations = {
        observation.dimension: observation
        for observation in getattr(validation_report, "pricing_observations", ())
    }
    captured = set(getattr(manifest, "record_ids", ()))
    manifest_hash = getattr(manifest, "manifest_hash", "")

    revised: dict[PricingDimension, PricingDimensionResult] = {}
    sources: list[tuple[str, tuple[str, ...]]] = []
    rejected: list[str] = []

    for check in getattr(validation_report, "checks", ()):
        dimension = getattr(check, "dimension", None)
        if dimension is None:
            continue
        observation = observations.get(dimension)
        if observation is None:
            continue

        # -- the check must have passed, about this dimension ---------------
        if not getattr(check, "passed", False):
            rejected.append(
                f"{dimension.value}: the validation check did not pass "
                f"({getattr(check, 'name', '?')}), so its observation cannot "
                "revise anything. A failed check is a finding, not a source."
            )
            continue
        if observation.dimension is not dimension:
            rejected.append(
                f"{dimension.value}: the observation is about "
                f"{observation.dimension.value}"
            )
            continue

        # -- a chain-level answer requires a uniform chain -------------------
        coverage = getattr(check, "coverage", None)
        if coverage is not None and not coverage.settled:
            rejected.append(
                f"{dimension.value}: the chain was measured across "
                f"{coverage.rows_inspected} rows and did not settle on one "
                f"answer ({coverage.matching_rows} matching, "
                f"{coverage.mismatching_rows} mismatching, "
                f"{coverage.missing_rows} missing, "
                f"{coverage.non_finite_rows} unreadable). One contract cannot "
                "characterise a chain."
            )
            continue

        # -- the evidence is about this capture ------------------------------
        if observation.manifest_hash != manifest_hash:
            rejected.append(
                f"{dimension.value}: the observation was read from capture "
                f"{observation.manifest_hash[:16]!r}, not this one"
            )
            continue
        records = tuple(sorted(set(check.record_ids) | set(observation.record_ids)))
        if not records or not set(records) <= captured:
            # A named record this capture does not hold is not weak evidence,
            # it is evidence about something else.
            rejected.append(
                f"{dimension.value}: names records {sorted(set(records) - captured)} "
                "which this capture does not contain"
            )
            continue
        if not set(observation.record_ids) <= set(check.record_ids):
            rejected.append(
                f"{dimension.value}: the observation names records "
                f"{sorted(set(observation.record_ids) - set(check.record_ids))} "
                "that the check did not read"
            )
            continue

        # -- and it is something the report is committed to ------------------
        covered = getattr(
            validation_report, "observation_is_in_the_semantic_payload", None
        )
        if callable(covered) and not covered(observation):
            rejected.append(
                f"{dimension.value}: the observation is not part of the "
                "validation report's comparable payload, so re-deriving the "
                "report would not have caught a change to it"
            )
            continue

        outcome = compare_observation(observation, model_spec)
        current = by_dimension.get(dimension)
        if current is not None and not _supersedes(current, outcome):
            rejected.append(
                f"{dimension.value}: a live {outcome.status.value} does not "
                f"override an established {current.status.value}"
            )
            continue

        revised[dimension] = outcome
        sources.append((dimension.value, records))

    merged = base_report.merged_with(
        PricingCompatibilityReport(dimensions=tuple(revised.values()))
    )
    return PostCaptureCompatibilityReport(
        dimensions=merged.dimensions,
        hard_failures=merged.hard_failures,
        warnings=merged.warnings,
        manifest_hash=manifest_hash,
        validator_version=getattr(validation_report, "validator_version", ""),
        observed_record_ids=tuple(sorted(sources)),
        base_statuses=_statuses(base_report),
        rejected_observations=tuple(sorted(rejected)),
    )


def _supersedes(
    current: PricingDimensionResult, observed: PricingDimensionResult
) -> bool:
    """May a live finding replace what the static assessment concluded?"""
    if observed.status is CompatibilityStatus.MISMATCHED:
        # What the vendor did beats what its documentation said it would do.
        return True
    # A live agreement fills a gap; it never overturns a measured disagreement,
    # which would be laundering it.
    return current.status is CompatibilityStatus.UNKNOWN


def _statuses(report: PricingCompatibilityReport) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((d.dimension.value, d.status.value) for d in report.dimensions))
