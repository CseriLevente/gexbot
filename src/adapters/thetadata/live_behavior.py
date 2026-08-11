"""What the vendor documents, what the vendor does, and the gap between them.

Until v2.1.22 every vendor convention in this repository had one slot: either a
document settled it or it was ``UNKNOWN``. The first live capture broke that
model in the most direct way available.

The pinned OpenAPI document describes ``rate_value`` as *"The interest rate, as
a percent"*. v2.1.18 read that out of the bytes, correctly, and v2.1.21's
capture profile therefore sent ``rate_value=4.2`` to mean 4.2%. The returned
Greeks prove the v3 implementation put ``4.2`` into Black-Scholes unchanged --
420%. Reconstructing 7,348 usable live rows:

===============  ====================  =========================
``r``            median |delta error|  median mid-price error
===============  ====================  =========================
``4.2``          4.45e-05              $0.02
``0.042``        0.221                 $423
===============  ====================  =========================

That is not a close call, and it is not a documentation reading error either.
Both statements are true: the document says percent, the implementation reads a
decimal. A repository that can only hold one of them has to delete evidence to
record evidence.

So a dimension here carries **both** readings and a status that names their
relationship. When they conflict:

* the *observed* value governs request construction, because the request goes to
  the implementation, not to the document;
* the *documented* value stays exactly as extracted, because overwriting it
  would destroy the only record that the vendor's published description is
  wrong -- which is a fact about the vendor worth more than the parameter value;
* the status stays ``DOCUMENTATION_LIVE_CONFLICT`` permanently. There is no
  transition that resolves it silently. It resolves when the vendor changes one
  or the other, and then the pinned document hash changes and the drift check
  fires.

Nothing in this module writes to the documentation artifact. The extraction in
:mod:`src.adapters.thetadata.openapi_evidence` remains the sole authority on
what the document says, and this module is the sole authority on what the
capture showed. They are compared, never merged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

from src.adapters.errors import ThetaDataProvenanceError

__all__ = [
    "PRICING_EVIDENCE_SCHEMA_VERSION",
    "BehaviorDimension",
    "CaptureIdentity",
    "CaptureRateIntent",
    "EvidenceStatus",
    "InferenceDecision",
    "LiveBehaviorObservation",
    "ObservationBasis",
    "ReconstructionMetric",
    "VendorBehaviorLedger",
    "VendorRateSemantics",
]

#: Bumped when what a pricing-evidence record must carry changes. v2.1.22 adds
#: the observed-implementation side, so a v2.1.21 reader that saw only the
#: documented value would report agreement where there is a conflict.
PRICING_EVIDENCE_SCHEMA_VERSION = "pricing-evidence/2.1.24"


#: Rate units this repository can express. A closed set: a unit nobody named
#: cannot be checked, and "UNKNOWN" is a state rather than a spelling mistake.
RATE_UNITS = ("PERCENT_ANNUAL_RATE", "DECIMAL_ANNUAL_RATE", "UNKNOWN")


class InferenceDecision(str, Enum):
    """Whether a numerical comparison actually settled anything.

    v2.1.23 took the lowest score and called it the answer. That is not the same
    question. With ``rate_value=0`` the decimal and percent hypotheses are the
    same computation -- both give ``r = 0`` -- and score identically to the last
    bit; v2.1.23 nonetheless reported ``DECIMAL_ANNUAL_RATE`` and a
    documentation conflict, because ``min()`` returns the first of equals and
    decimal happened to be listed first. The verdict came from the ordering of a
    tuple.

    So the winner and the *decision* are separate. A score table always has a
    smallest entry; only some of them mean something.
    """

    #: One hypothesis fits adequately and is separated from the next.
    RESOLVED = "RESOLVED"
    #: A best exists but the runner-up is not distinguishable from it at the
    #: precision the source fields are reported to.
    AMBIGUOUS = "AMBIGUOUS"
    #: The hypotheses are the same computation. No amount of data separates
    #: them, because there is nothing to separate.
    NOT_IDENTIFIABLE = "NOT_IDENTIFIABLE"
    #: Every hypothesis reproduces the vendor badly. The best of a bad set is
    #: not evidence for anything.
    NO_ADEQUATE_FIT = "NO_ADEQUATE_FIT"
    #: Too few usable rows to distinguish anything.
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

    @property
    def is_resolved(self) -> bool:
        return self is InferenceDecision.RESOLVED


class BehaviorDimension(str, Enum):
    """The vendor conventions a live capture can speak to.

    A closed set, for the same reason ``DocumentedRule`` is closed: a caller
    that can invent a dimension identifier can record a resolution nobody
    reviewed against a name nothing reads.
    """

    #: Whether ``rate_value`` is consumed as a percent or a decimal fraction.
    RATE_UNITS = "RATE_UNITS"
    #: The year fraction the vendor divides day counts by.
    DAY_COUNT = "DAY_COUNT"
    #: The instant a series stops accruing time value.
    EXPIRATION_TIMESTAMP = "EXPIRATION_TIMESTAMP"
    #: Which price the reported implied volatility was solved against.
    IV_PRICE_BASIS = "IV_PRICE_BASIS"
    #: Which underlying print the vendor's own Greeks were computed from.
    UNDERLYING_SOURCE = "UNDERLYING_SOURCE"
    #: What ``annual_dividend`` means to the vendor.
    DIVIDEND_CONVENTION = "DIVIDEND_CONVENTION"
    #: Which instant the vendor's own underlying print carries.
    #:
    #: Separate from ``UNDERLYING_SOURCE`` because the analytical compatibility
    #: layer treats them separately, and a live response establishes both. With
    #: only the source recorded, ``dimensions_unresolved == []`` read as "every
    #: pricing dimension is settled" while one of them was not in the ledger at
    #: all.
    UNDERLYING_TIMESTAMP = "UNDERLYING_TIMESTAMP"
    #: Whether the dedicated contract listing matches the snapshot universe.
    CONTRACT_LIST_UNIVERSE = "CONTRACT_LIST_UNIVERSE"

    @property
    def pricing_dimension(self) -> str:
        """The analytical ``PricingDimension`` this evidence speaks to.

        Empty when it speaks to none. A typed mapping rather than a name match:
        the two enums were written for different purposes and only mostly
        agree, and guessing by string would make a rename look like a
        resolution.
        """
        return PRICING_DIMENSION_FOR.get(self, "")


#: Certification evidence -> the analytical pricing dimension it supports, by
#: value rather than by importing the enum (the compatibility layer lives above
#: this one). ``CONTRACT_LIST_UNIVERSE`` is deliberately absent: universe
#: coverage is not a pricing convention, and mapping it to one would let a
#: universe finding satisfy a pricing requirement.
PRICING_DIMENSION_FOR: dict[BehaviorDimension, str] = {
    BehaviorDimension.RATE_UNITS: "RATE_UNITS",
    BehaviorDimension.DAY_COUNT: "DAY_COUNT",
    BehaviorDimension.EXPIRATION_TIMESTAMP: "EXPIRATION_TIMESTAMP",
    BehaviorDimension.IV_PRICE_BASIS: "IV_PRICE_BASIS",
    BehaviorDimension.UNDERLYING_SOURCE: "UNDERLYING_SOURCE",
    BehaviorDimension.UNDERLYING_TIMESTAMP: "UNDERLYING_TIMESTAMP",
    BehaviorDimension.DIVIDEND_CONVENTION: "DIVIDEND_CONVENTION",
}


class ObservationBasis(str, Enum):
    """What kind of thing established a value.

    Ordered by what it can settle. A numerical reconstruction over thousands of
    live rows and a sentence in a document are both evidence; they are not the
    same evidence, and a reader deciding whether to trust a GEX number needs to
    know which one is underneath.
    """

    #: Read out of the pinned OpenAPI bytes by a named extractor.
    PINNED_DOCUMENTATION = "PINNED_DOCUMENTATION"
    #: Recovered by reproducing the vendor's own numbers under competing
    #: hypotheses and keeping the one that fits.
    LIVE_NUMERICAL_RECONSTRUCTION = "LIVE_NUMERICAL_RECONSTRUCTION"
    #: Established by comparing exact identity sets between two live responses.
    LIVE_SET_COMPARISON = "LIVE_SET_COMPARISON"
    #: Read directly out of a field the live response carried.
    LIVE_FIELD_READ = "LIVE_FIELD_READ"
    #: Follows from what the request asked for, not from what came back.
    REQUEST_BOUND = "REQUEST_BOUND"

    @property
    def is_live(self) -> bool:
        return self in (
            ObservationBasis.LIVE_NUMERICAL_RECONSTRUCTION,
            ObservationBasis.LIVE_SET_COMPARISON,
            ObservationBasis.LIVE_FIELD_READ,
        )


class EvidenceStatus(str, Enum):
    """How the documented reading and the observed reading stand to each other."""

    #: Both sides measured, and they say the same thing.
    DOCUMENTATION_LIVE_AGREE = "DOCUMENTATION_LIVE_AGREE"
    #: Both sides measured, and they do not. Terminal until the vendor moves.
    DOCUMENTATION_LIVE_CONFLICT = "DOCUMENTATION_LIVE_CONFLICT"
    #: The document settles it; no live capture has spoken to it yet.
    DOCUMENTATION_ONLY = "DOCUMENTATION_ONLY"
    #: A live capture settles it; the document is silent.
    LIVE_ONLY = "LIVE_ONLY"
    #: Neither settles it, or the evidence is not strong enough to.
    UNRESOLVED = "UNRESOLVED"

    @property
    def is_conflict(self) -> bool:
        return self is EvidenceStatus.DOCUMENTATION_LIVE_CONFLICT

    @property
    def is_resolved(self) -> bool:
        """Whether *some* value governs. A conflict is resolved and still a conflict.

        Deliberately true for a conflict: the live reading governs, so requests
        can be built. What a conflict blocks is calling the dimension
        documentation-matched, which is a different question and a different
        property.
        """
        return self is not EvidenceStatus.UNRESOLVED


@dataclass(frozen=True, slots=True)
class CaptureIdentity:
    """Which immutable capture an observation was read from.

    The session id names the run and the manifest hash names the set of
    records; both are mandatory, because an observation that cannot name the
    capture it came from is not reproducible, and an unreproducible measurement
    of vendor behaviour is indistinguishable from an assertion.

    ``archive_sha256`` is **optional and separate**, because it identifies a
    different byte artefact: the distributable archive, which a capture
    directory on disk does not have until somebody makes one. v2.1.22 wrote

        archive_sha256 = supplied or capture.manifest_hash

    which filled a field named after one artefact with the digest of another.
    Any reader checking a download against it would have got a mismatch and no
    way to tell a re-wrap from a substitution.

    Two different artefacts cannot share a SHA-256, so an ``archive_sha256``
    equal to the ``manifest_hash`` is that substitution and nothing else. It is
    refused here rather than documented, so the bug cannot come back by way of
    a convenient default somewhere else.
    """

    session_id: str
    manifest_hash: str
    #: Empty means *not computed*, which is honest and common. It never means
    #: "same as the manifest".
    archive_sha256: str = ""

    def __post_init__(self) -> None:
        for name in ("session_id", "manifest_hash"):
            if not str(getattr(self, name) or "").strip():
                raise ThetaDataProvenanceError(
                    f"CaptureIdentity.{name} is empty; a live observation that "
                    "cannot name the capture it came from is not reproducible, "
                    "and an unreproducible measurement of vendor behaviour is "
                    "indistinguishable from an assertion"
                )
        for name in ("manifest_hash", "archive_sha256"):
            value = str(getattr(self, name) or "")
            if name == "archive_sha256" and not value:
                continue
            if len(value) != 64 or not all(c in "0123456789abcdef" for c in value):
                raise ThetaDataProvenanceError(
                    f"CaptureIdentity.{name} is {value!r}; a full lowercase "
                    "SHA-256 is required. A truncated digest cannot be checked "
                    "against the artefact it claims to identify."
                )
        if self.archive_sha256 and self.archive_sha256 == self.manifest_hash:
            raise ThetaDataProvenanceError(
                "archive_sha256 equals manifest_hash. These identify different "
                "byte artefacts -- a manifest document and a distributable "
                "archive -- so equal digests mean one was substituted for the "
                "other. Leave archive_sha256 empty when no archive has been "
                "hashed; 'unknown' is a true statement and 'the manifest' is "
                "not."
            )

    @property
    def archive_identity_known(self) -> bool:
        """Whether an archive digest was actually computed for this capture."""
        return bool(self.archive_sha256)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "manifest_hash": self.manifest_hash,
            "archive_sha256": self.archive_sha256,
            "archive_identity_known": self.archive_identity_known,
        }


@dataclass(frozen=True, slots=True)
class ReconstructionMetric:
    """One number a reconstruction produced, and what it was measured over.

    Carries the competing hypothesis it belongs to, so a report shows *why* a
    resolution was chosen rather than only what was chosen. "ACT/365 won" is an
    assertion; "ACT/365 scored 1.75e-4 and ACT/360 scored 1.59e-2 over 7,348
    rows" is a result.
    """

    hypothesis: str
    statistic: str
    value: float
    rows: int
    selected: bool = False

    def __post_init__(self) -> None:
        if not str(self.hypothesis).strip() or not str(self.statistic).strip():
            raise ThetaDataProvenanceError(
                "a reconstruction metric must name both its hypothesis and its "
                "statistic; an unlabelled number cannot be compared to anything"
            )
        if self.rows <= 0:
            raise ThetaDataProvenanceError(
                f"a reconstruction over {self.rows} rows measures nothing"
            )
        if not math.isfinite(self.value):
            raise ThetaDataProvenanceError(
                f"reconstruction statistic {self.statistic!r} is {self.value!r}; "
                "a non-finite error statistic means the reconstruction diverged "
                "and its verdict must not be recorded as evidence"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis,
            "statistic": self.statistic,
            "value": self.value,
            "rows": self.rows,
            "selected": self.selected,
        }


@dataclass(frozen=True, slots=True)
class LiveBehaviorObservation:
    """One vendor convention, as documented and as observed, side by side.

    The invariant this type exists to hold: ``documented_value`` is never
    written from live evidence and ``observed_value`` is never written from a
    document. Two sources, two fields, one status describing the relationship.
    """

    dimension: BehaviorDimension
    status: EvidenceStatus
    basis: ObservationBasis
    #: Whether the numerical comparison behind this observation discriminated.
    #:
    #: Separate from ``status``, which describes how two *readings* stand to
    #: each other. A comparison can produce a smallest score and still have
    #: settled nothing -- identical hypotheses, a tie inside the reporting
    #: precision, or nothing fitting at all -- and v2.1.23 had no way to say so.
    decision: InferenceDecision = InferenceDecision.RESOLVED
    #: Exactly as extracted from the pinned document. Empty when it is silent.
    documented_value: str = ""
    #: What the capture showed the implementation actually does.
    observed_value: str = ""
    #: Where a reader checks the documented side.
    documentation_reference: str = ""
    #: Which capture the observed side came from.
    capture: CaptureIdentity | None = None
    #: How many live rows the observation rests on.
    rows_used: int = 0
    #: What this observation is *not* claimed to cover. Load-bearing: the SPXW
    #: expiration finding holds for the front week and demonstrably fails
    #: outside it, and a scope-free record of it would be a false generalisation
    #: with a capture hash attached.
    scope: str = ""
    metrics: tuple[ReconstructionMetric, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimension", BehaviorDimension(self.dimension))
        object.__setattr__(self, "status", EvidenceStatus(self.status))
        object.__setattr__(self, "basis", ObservationBasis(self.basis))
        object.__setattr__(self, "decision", InferenceDecision(self.decision))

        if self.status.is_resolved and not self.decision.is_resolved:
            raise ThetaDataProvenanceError(
                f"{self.dimension.value} carries status {self.status.value} on "
                f"a comparison that came out {self.decision.value}. A dimension "
                "cannot be settled by a comparison that settled nothing; that "
                "is the ordering-of-a-tuple verdict this field exists to stop."
            )

        if self.status.is_conflict:
            if not (self.documented_value and self.observed_value):
                raise ThetaDataProvenanceError(
                    f"{self.dimension.value} is marked "
                    "DOCUMENTATION_LIVE_CONFLICT but does not carry both "
                    "readings. A conflict is a statement about two values; with "
                    "one of them missing there is nothing to disagree."
                )
            if self.documented_value == self.observed_value:
                raise ThetaDataProvenanceError(
                    f"{self.dimension.value} is marked "
                    f"DOCUMENTATION_LIVE_CONFLICT and both readings are "
                    f"{self.observed_value!r}. Agreement recorded as conflict "
                    "would make every reader treat a settled dimension as open."
                )
        if (
            self.status is EvidenceStatus.DOCUMENTATION_LIVE_AGREE
            and self.documented_value != self.observed_value
        ):
            raise ThetaDataProvenanceError(
                f"{self.dimension.value} is marked "
                f"DOCUMENTATION_LIVE_AGREE with documented "
                f"{self.documented_value!r} and observed "
                f"{self.observed_value!r}. These do not agree."
            )
        if self.documented_value and not self.documentation_reference.strip():
            raise ThetaDataProvenanceError(
                f"{self.dimension.value} carries a documented value "
                f"{self.documented_value!r} with no reference. An unreferenced "
                "claim about what the vendor publishes cannot be rechecked when "
                "the vendor republishes."
            )
        if self.basis.is_live:
            if self.capture is None:
                raise ThetaDataProvenanceError(
                    f"{self.dimension.value} rests on {self.basis.value} but "
                    "names no capture. Live evidence that cannot be traced to "
                    "immutable bytes is an assertion with a confident label."
                )
            # Only a *resolved* observation has to rest on rows. An unresolved
            # one may have none precisely because there were too few to
            # discriminate, and refusing to record that would lose the finding.
            if self.rows_used <= 0 and self.decision.is_resolved:
                raise ThetaDataProvenanceError(
                    f"{self.dimension.value} rests on {self.basis.value} over "
                    f"{self.rows_used} rows"
                )
        if self.observed_value and self.basis is ObservationBasis.PINNED_DOCUMENTATION:
            raise ThetaDataProvenanceError(
                f"{self.dimension.value} carries an observed implementation "
                "value on PINNED_DOCUMENTATION basis. A document cannot witness "
                "what an implementation does; that is the whole reason this "
                "type has two sides."
            )

    @property
    def governing_value(self) -> str:
        """The value request construction must use.

        Live behaviour wins whenever it exists. The request is answered by the
        implementation; a parameter built to match the documentation and not the
        implementation is wrong by exactly the amount the two differ, which for
        the rate was a factor of one hundred.
        """
        return self.observed_value or self.documented_value

    @property
    def documentation_matched(self) -> bool:
        """Whether the vendor's published description survived contact with it.

        Separate from ``status.is_resolved``: a conflicting dimension is
        resolved *and* unmatched, and compatibility reporting needs to be able
        to say so without implying the value is unknown.
        """
        return self.status is EvidenceStatus.DOCUMENTATION_LIVE_AGREE or (
            self.status is EvidenceStatus.DOCUMENTATION_ONLY
            and bool(self.documented_value)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "pricing_dimension": self.dimension.pricing_dimension,
            "status": self.status.value,
            "decision": self.decision.value,
            "basis": self.basis.value,
            "documented_value": self.documented_value,
            "observed_value": self.observed_value,
            "governing_value": self.governing_value,
            "documentation_matched": self.documentation_matched,
            "documentation_reference": self.documentation_reference,
            "capture": self.capture.as_dict() if self.capture else None,
            "rows_used": self.rows_used,
            "scope": self.scope,
            "metrics": [m.as_dict() for m in self.metrics],
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class VendorBehaviorLedger:
    """Every dimension a capture spoke to, in one hashable object.

    Immutable and deduplicated by dimension: two observations of the same
    dimension would let a reader pick whichever answer suited, which is how a
    conflict becomes invisible without anybody deleting anything.
    """

    observations: tuple[LiveBehaviorObservation, ...] = ()
    schema_version: str = PRICING_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        seen: set[BehaviorDimension] = set()
        for observation in self.observations:
            if observation.dimension in seen:
                raise ThetaDataProvenanceError(
                    f"{observation.dimension.value} appears twice in the ledger. "
                    "Two records of one dimension let a reader choose the "
                    "convenient one; the conflict this ledger exists to hold "
                    "would be satisfiable by ignoring a row."
                )
            seen.add(observation.dimension)

    def for_dimension(
        self, dimension: BehaviorDimension
    ) -> LiveBehaviorObservation | None:
        for observation in self.observations:
            if observation.dimension is dimension:
                return observation
        return None

    @property
    def conflicts(self) -> tuple[LiveBehaviorObservation, ...]:
        return tuple(o for o in self.observations if o.status.is_conflict)

    @property
    def unresolved(self) -> tuple[LiveBehaviorObservation, ...]:
        return tuple(o for o in self.observations if not o.status.is_resolved)

    def fingerprint(self) -> str:
        from src.domain.digests import digest_of

        return digest_of(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "observations": [o.as_dict() for o in self.observations],
            "conflicts": [o.dimension.value for o in self.conflicts],
            "unresolved": [o.dimension.value for o in self.unresolved],
        }


@dataclass(frozen=True, slots=True)
class CaptureRateIntent:
    """What a capture means by its rate, in a form that cannot be edited later.

    v2.1.23 bound the ``rate_value`` on the wire correctly -- recomputed against
    the manifest's stamped request digest -- and then read the capture's
    *intended* economic rate out of a plain dictionary in ``run-intent.json``
    and believed it. Changing one number there:

        rate_semantics.economic_rate_decimal: 0.042 -> 4.2

    left every raw response, the request plan and the manifest untouched, and
    flipped ``capture_effective_rate_matches_intended_rate`` from true to false.
    The field that decides whether a capture is economically valid was the one
    field nothing checked.

    So the intent is a typed object with a fingerprint, and the fingerprint goes
    into the preflight approval *before the first request*. The approval hash is
    inside the operation fingerprint, and both are stamped on every manifest
    record, whose digest certification already recomputes. Editing any field
    here changes the fingerprint, which no longer matches the approval, whose
    hash no longer matches the records -- three independent refusals from one
    edit.

    The derived quantities are checked rather than trusted. Supplying
    ``economic_rate_percent=4.2`` beside ``economic_rate_decimal=0.5`` is not a
    capture with an unusual convention, it is a capture whose own statement of
    intent is incoherent, and it is refused at construction.
    """

    economic_rate_percent: float
    economic_rate_decimal: float
    local_model_rate: float
    vendor_request_rate_value: float
    vendor_observed_rate_unit: str
    documented_rate_unit: str
    schema_version: str = PRICING_EVIDENCE_SCHEMA_VERSION

    #: Float round-trips through JSON and through a percent/hundred conversion.
    #: Far tighter than the factor of a hundred this exists to catch.
    TOLERANCE: ClassVar[float] = 1e-12

    def __post_init__(self) -> None:
        for name in (
            "economic_rate_percent",
            "economic_rate_decimal",
            "local_model_rate",
            "vendor_request_rate_value",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ThetaDataProvenanceError(
                    f"CaptureRateIntent.{name} is {value!r}; a rate that is not "
                    "a finite number states no intent"
                )
        for name in ("vendor_observed_rate_unit", "documented_rate_unit"):
            if str(getattr(self, name)) not in RATE_UNITS:
                raise ThetaDataProvenanceError(
                    f"CaptureRateIntent.{name} is {getattr(self, name)!r}; "
                    f"expected one of {RATE_UNITS}"
                )
        if (
            abs(self.economic_rate_decimal - self.economic_rate_percent / 100.0)
            > self.TOLERANCE
        ):
            raise ThetaDataProvenanceError(
                f"CaptureRateIntent states {self.economic_rate_percent}% and "
                f"{self.economic_rate_decimal} as a decimal, which are not the "
                "same rate. A statement of intent that disagrees with itself "
                "cannot establish what a capture meant to buy."
            )
        if abs(self.local_model_rate - self.economic_rate_decimal) > self.TOLERANCE:
            raise ThetaDataProvenanceError(
                f"CaptureRateIntent prices the local model at "
                f"{self.local_model_rate} and states an economic rate of "
                f"{self.economic_rate_decimal}. The comparison certification "
                "makes is between the vendor and the local model; if those two "
                "already differ there is nothing to compare against."
            )
        expected = (
            self.economic_rate_percent
            if self.vendor_observed_rate_unit == "PERCENT_ANNUAL_RATE"
            else self.economic_rate_decimal
        )
        if (
            self.vendor_observed_rate_unit != "UNKNOWN"
            and abs(self.vendor_request_rate_value - expected) > self.TOLERANCE
        ):
            raise ThetaDataProvenanceError(
                f"CaptureRateIntent sends {self.vendor_request_rate_value} for an "
                f"economic {self.economic_rate_percent}% under "
                f"{self.vendor_observed_rate_unit}, where {expected} is the "
                "value that expresses it. This is the arithmetic the first "
                "capture got wrong, and an intent object that could carry it "
                "would be recording the defect rather than preventing it."
            )

    @property
    def predicted_vendor_effective_rate(self) -> float:
        """The ``r`` the vendor's Black-Scholes will receive."""
        if self.vendor_observed_rate_unit == "PERCENT_ANNUAL_RATE":
            return self.vendor_request_rate_value / 100.0
        return self.vendor_request_rate_value

    @property
    def documentation_live_conflict(self) -> bool:
        return (
            self.vendor_observed_rate_unit != self.documented_rate_unit
            and "UNKNOWN"
            not in (
                self.vendor_observed_rate_unit,
                self.documented_rate_unit,
            )
        )

    def semantic_payload(self) -> dict[str, Any]:
        """Everything the fingerprint covers. Derived values included.

        The derived numbers are *in* the digest even though they follow from the
        others, because they are what a later reader consumes. A fingerprint
        that covered only the inputs would let somebody edit the output.
        """
        return {
            "schema_version": self.schema_version,
            "economic_rate_percent": self.economic_rate_percent,
            "economic_rate_decimal": self.economic_rate_decimal,
            "local_model_rate": self.local_model_rate,
            "vendor_request_rate_value": self.vendor_request_rate_value,
            "vendor_observed_rate_unit": self.vendor_observed_rate_unit,
            "documented_rate_unit": self.documented_rate_unit,
        }

    @property
    def fingerprint(self) -> str:
        from src.domain.digests import digest_of

        return digest_of(self.semantic_payload())

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CaptureRateIntent:
        """Rebuild from a stored ``rate_semantics`` block.

        Every field is required. A missing one cannot be defaulted, because a
        default would be this code deciding what a capture meant.
        """
        try:
            return cls(
                economic_rate_percent=float(payload["economic_rate_percent"]),
                economic_rate_decimal=float(payload["economic_rate_decimal"]),
                local_model_rate=float(payload["local_model_rate"]),
                vendor_request_rate_value=float(payload["vendor_request_rate_value"]),
                vendor_observed_rate_unit=str(payload["vendor_observed_rate_unit"]),
                documented_rate_unit=str(payload["documented_rate_unit"]),
                schema_version=str(
                    payload.get("schema_version", PRICING_EVIDENCE_SCHEMA_VERSION)
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ThetaDataProvenanceError(
                f"the recorded rate intent is not a complete CaptureRateIntent: {error}"
            ) from error

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "predicted_vendor_effective_rate": self.predicted_vendor_effective_rate,
            "documentation_live_conflict": self.documentation_live_conflict,
            "rate_intent_fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class VendorRateSemantics:
    """The five distinct numbers the word "rate" was covering.

    Before v2.1.22 a capture profile held ``rate_value: 4.2`` next to
    ``rate_units: PERCENT_ANNUAL_RATE`` and ``risk_free_rate: 0.042``, and a
    comment explaining that 4.2 percent equals 0.042 decimal. Every one of those
    statements was true and the request was still wrong, because none of them
    was *the number the vendor's Black-Scholes would receive*. That quantity had
    no name, so nothing could check it.

    It has a name now, and the five concepts are separately stated:

    ``economic_rate_percent``
        The rate a human means. 4.2.
    ``economic_rate_decimal``
        The same rate as a fraction. 0.042. Derived, and checked, never stored
        independently -- two hand-maintained numbers drift.
    ``vendor_request_rate_value``
        The literal value that goes on the wire as ``rate_value``.
    ``vendor_observed_rate_unit``
        How the *implementation* reads that value. Measured, not assumed.
    ``documented_rate_unit``
        How the *documentation* says it reads it. Preserved even when wrong.
    """

    economic_rate_percent: float
    vendor_observed_rate_unit: str
    documented_rate_unit: str
    #: The value the configuration will *actually* put on the wire. Normally
    #: identical to ``vendor_request_rate_value``; supplied separately so a
    #: profile whose ``rate_value`` disagrees with its own declared units shows
    #: up as a mismatch instead of being assumed away by re-derivation.
    configured_wire_value: float | None = None
    #: What the local Black-Scholes prices with, read from the model spec
    #: rather than from the vendor block, so the two can be compared at all.
    configured_local_model_rate: float | None = None
    #: Set only when the two units disagree; carried so a report can show the
    #: conflict at the point the number is used rather than in a distant ledger.
    conflict: bool = field(default=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.economic_rate_percent):
            raise ThetaDataProvenanceError(
                f"economic_rate_percent is {self.economic_rate_percent!r}"
            )
        for name in ("vendor_observed_rate_unit", "documented_rate_unit"):
            value = str(getattr(self, name))
            if value not in ("PERCENT_ANNUAL_RATE", "DECIMAL_ANNUAL_RATE", "UNKNOWN"):
                raise ThetaDataProvenanceError(
                    f"VendorRateSemantics.{name} is {value!r}; a rate unit must "
                    "be one of PERCENT_ANNUAL_RATE, DECIMAL_ANNUAL_RATE, UNKNOWN"
                )
        object.__setattr__(
            self,
            "conflict",
            self.vendor_observed_rate_unit != self.documented_rate_unit
            and "UNKNOWN"
            not in (self.vendor_observed_rate_unit, self.documented_rate_unit),
        )

    @property
    def economic_rate_decimal(self) -> float:
        """The economic rate as a fraction. Always derived."""
        return self.economic_rate_percent / 100.0

    @property
    def vendor_request_rate_value(self) -> float:
        """The number to put on the wire so the vendor prices the intended rate.

        Under ``DECIMAL_ANNUAL_RATE`` the vendor uses the value unchanged, so
        the wire value *is* the decimal rate: 0.042. Under
        ``PERCENT_ANNUAL_RATE`` it would multiply by a hundredth itself, so the
        wire value would be 4.2.

        This is the one line that would have prevented the first capture from
        being priced at 420%, and it is derived from a measured unit rather than
        a documented one for exactly that reason.
        """
        if self.vendor_observed_rate_unit == "PERCENT_ANNUAL_RATE":
            return self.economic_rate_percent
        if self.vendor_observed_rate_unit == "DECIMAL_ANNUAL_RATE":
            return self.economic_rate_decimal
        raise ThetaDataProvenanceError(
            "the vendor's rate unit is UNKNOWN, so there is no value that can "
            "be sent with a known meaning. Send nothing and let the vendor "
            "apply its own default, or establish the unit first."
        )

    @property
    def wire_value(self) -> float:
        """The number that will really be sent, not the one that should be."""
        if self.configured_wire_value is not None:
            return self.configured_wire_value
        return self.vendor_request_rate_value

    @property
    def predicted_vendor_effective_rate(self) -> float:
        """The ``r`` the vendor's Black-Scholes will receive.

        The wire value read under the unit the *implementation* was measured
        using. This is the quantity that had no name before v2.1.22 and no
        prediction before v2.1.23: an operator reading a dry run could see the
        wire value and the economic rate and still not be told what the vendor
        would actually price, which is exactly what went wrong the first time.
        """
        if self.vendor_observed_rate_unit == "PERCENT_ANNUAL_RATE":
            return self.wire_value / 100.0
        return self.wire_value

    @property
    def predicted_effective_rate_matches_intended(self) -> bool:
        """Will the vendor price the rate the local model prices?

        Distinct from :attr:`conflict`. The documentation conflict is a
        standing fact about the vendor; this is a question about *this
        request*, and a correct request answers it yes while the conflict is
        still true.
        """
        return abs(self.predicted_vendor_effective_rate - self.local_model_rate) <= 1e-9

    @property
    def local_model_rate(self) -> float:
        """What the local Black-Scholes prices with. A decimal, always."""
        if self.configured_local_model_rate is not None:
            return self.configured_local_model_rate
        return self.economic_rate_decimal

    def to_intent(self) -> CaptureRateIntent:
        """The bindable form of the same statement.

        Built from the wire value the configuration will really send, not the
        value that *should* be sent, so a profile whose ``rate_value``
        disagrees with its own declared unit is refused here rather than
        quietly corrected into a coherent-looking intent.
        """
        return CaptureRateIntent(
            economic_rate_percent=self.economic_rate_percent,
            economic_rate_decimal=self.economic_rate_decimal,
            local_model_rate=self.local_model_rate,
            vendor_request_rate_value=self.wire_value,
            vendor_observed_rate_unit=self.vendor_observed_rate_unit,
            documented_rate_unit=self.documented_rate_unit,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.to_intent().semantic_payload(),
            "wire_value": self.wire_value,
            "documentation_live_conflict": self.conflict,
            "predicted_vendor_effective_rate": (self.predicted_vendor_effective_rate),
            "predicted_effective_rate_matches_intended_rate": (
                self.predicted_effective_rate_matches_intended
            ),
            "rate_intent_fingerprint": self.to_intent().fingerprint,
        }


def rate_semantics_for(pipeline: Any, config: Any) -> VendorRateSemantics:
    """The one derivation of a session's rate semantics.

    Called by the dry run, by the live run's intent record, and by
    :func:`~src.adapters.thetadata.preflight_approval.approval_for`. One
    function because the approval binds a fingerprint over exactly what the dry
    run printed, and two derivations that could drift would put an operator
    back to approving one thing and sending another.
    """
    from src.adapters.thetadata.vendor_documentation import DocumentedRule
    from src.config.pipeline import OBSERVED_RATE_UNITS, RateUnit

    unit = config.rate_units
    raw = config.rate_value
    factor = 0.01 if unit is RateUnit.PERCENT_ANNUAL_RATE else 1.0
    economic_percent = 0.0 if raw is None else float(raw) * factor * 100.0

    documented: Any = None
    bundle = getattr(pipeline, "documentation_bundle", None)
    if bundle is not None:
        documented = bundle.value_for(DocumentedRule.RATE_UNITS)

    spec = getattr(pipeline, "model_spec", None)
    local = getattr(spec, "risk_free_rate", None)

    return VendorRateSemantics(
        economic_rate_percent=economic_percent,
        # Measured, never read off the configuration being reported on. A
        # config that could supply its own "observed" unit would be marking its
        # own homework.
        vendor_observed_rate_unit=OBSERVED_RATE_UNITS.value,
        documented_rate_unit=(
            documented.value if isinstance(documented, RateUnit) else "UNKNOWN"
        ),
        configured_wire_value=(None if raw is None else float(raw)),
        configured_local_model_rate=(
            float(local) if isinstance(local, (int, float)) else None
        ),
    )
