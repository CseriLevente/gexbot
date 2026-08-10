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
from typing import Any

from src.adapters.errors import ThetaDataProvenanceError

__all__ = [
    "PRICING_EVIDENCE_SCHEMA_VERSION",
    "BehaviorDimension",
    "CaptureIdentity",
    "EvidenceStatus",
    "LiveBehaviorObservation",
    "ObservationBasis",
    "ReconstructionMetric",
    "VendorBehaviorLedger",
    "VendorRateSemantics",
]

#: Bumped when what a pricing-evidence record must carry changes. v2.1.22 adds
#: the observed-implementation side, so a v2.1.21 reader that saw only the
#: documented value would report agreement where there is a conflict.
PRICING_EVIDENCE_SCHEMA_VERSION = "pricing-evidence/2.1.22"


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
    #: Whether the dedicated contract listing matches the snapshot universe.
    CONTRACT_LIST_UNIVERSE = "CONTRACT_LIST_UNIVERSE"


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

    All three, always. The session id names the run, the manifest hash names the
    set of records, and the archive digest names the bytes a third party can
    obtain. An observation carrying only a session id points at a directory that
    could have been edited since.
    """

    session_id: str
    manifest_hash: str
    archive_sha256: str

    def __post_init__(self) -> None:
        for name in ("session_id", "manifest_hash", "archive_sha256"):
            if not str(getattr(self, name) or "").strip():
                raise ThetaDataProvenanceError(
                    f"CaptureIdentity.{name} is empty; a live observation that "
                    "cannot name the capture it came from is not reproducible, "
                    "and an unreproducible measurement of vendor behaviour is "
                    "indistinguishable from an assertion"
                )
        for name in ("manifest_hash", "archive_sha256"):
            value = str(getattr(self, name))
            if len(value) != 64 or not all(c in "0123456789abcdef" for c in value):
                raise ThetaDataProvenanceError(
                    f"CaptureIdentity.{name} is {value!r}; a full lowercase "
                    "SHA-256 is required. A truncated digest cannot be checked "
                    "against the artefact it claims to identify."
                )

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "manifest_hash": self.manifest_hash,
            "archive_sha256": self.archive_sha256,
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
            if self.rows_used <= 0:
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
            "status": self.status.value,
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
    def local_model_rate(self) -> float:
        """What the local Black-Scholes prices with. A decimal, always."""
        return self.economic_rate_decimal

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

    def as_dict(self) -> dict[str, Any]:
        return {
            "economic_rate_percent": self.economic_rate_percent,
            "economic_rate_decimal": self.economic_rate_decimal,
            "local_model_rate": self.local_model_rate,
            "vendor_request_rate_value": self.vendor_request_rate_value,
            "vendor_observed_rate_unit": self.vendor_observed_rate_unit,
            "documented_rate_unit": self.documented_rate_unit,
            "documentation_live_conflict": self.conflict,
        }
