"""Two different facts about open interest, kept apart.

An open-interest response carries a *number*. It does not carry the settlement
date that number belongs to, and ThetaData does not publish one -- see
``docs/OPEN_DECISIONS.md`` OD-26.

v2.1.6 had one object for both. ``OpenInterestProvenance`` held an ``as_of``
date, a source, and a ``VerifiedFieldObservation``; ``claims_observation``
returned True when all three were present, and ``grade_claim`` confirmed the
observation by re-reading ``open_interest`` from the named record. That
confirmation is real and it is about the wrong field: it proves the record
contains the open-interest *value* claimed. The date came from the caller and
was graded ``OBSERVED`` on the strength of a value nobody disputed.

So a caller's assumption about which settlement session a figure belongs to was
being promoted to vendor-observed evidence by a check that never looked at a
date. Open interest is the linear weight on every GEX term; using Friday's
figures believing them to be Monday's is not a stale number, it is a different
market.

Here they are separate types with separate rules:

* ``OpenInterestValueObservation`` -- the vendor sent this number, in this
  record, for this contract. Confirmable by re-reading the payload;
* ``OpenInterestAsOfEvidence`` -- this is the settlement date, and here is what
  kind of thing makes us say so. Only some kinds are strong enough to permit a
  trusted calculation, and the honest kind today is ``CALLER_ASSUMPTION``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from src.adapters.errors import ThetaDataProvenanceError

__all__ = [
    "EvidenceKind",
    "OpenInterestAsOfEvidence",
    "OpenInterestValueObservation",
]


class EvidenceKind(str, Enum):
    """What kind of thing is making us believe a settlement date.

    Ordered by what it permits, not by how confident anybody feels. The
    distinction that matters is whether the vendor said it or we assumed it.
    """

    #: The response itself carried the date, in a field, which we read back.
    VENDOR_FIELD = "VENDOR_FIELD"
    #: Vendor documentation states the settlement convention, and the reference
    #: resolves to something a reader can open.
    AUTHORITATIVE_VENDOR_DOCUMENTATION = "AUTHORITATIVE_VENDOR_DOCUMENTATION"
    #: Computed from a settlement schedule that has itself been verified.
    DERIVED_FROM_VERIFIED_VENDOR_SCHEDULE = "DERIVED_FROM_VERIFIED_VENDOR_SCHEDULE"
    #: We picked a date. Legitimate, common, and not evidence about the vendor.
    CALLER_ASSUMPTION = "CALLER_ASSUMPTION"

    @property
    def is_vendor_established(self) -> bool:
        """Whether the vendor, rather than this repository, settled the date."""
        return self is not EvidenceKind.CALLER_ASSUMPTION

    @property
    def permits_trusted_calculation(self) -> bool:
        """Whether a GEX number resting on this date may be called trusted.

        A caller assumption permits a raw capture and a diagnostic calculation
        -- both are useful and neither claims the date is right. It does not
        permit a trusted calculation, calculation validation, adapter
        certification or feature generation, because all four are statements
        about what the vendor's data means.
        """
        return self.is_vendor_established


@dataclass(frozen=True, slots=True)
class ContractIdentity:
    """Which contract a value was read for. Optional, and worth having.

    A chain-level open-interest figure attributed to no contract cannot be
    checked against the chain that used it.
    """

    canonical_id: str

    def __post_init__(self) -> None:
        if not str(self.canonical_id).strip():
            raise ThetaDataProvenanceError(
                "ContractIdentity.canonical_id is empty; an observation that "
                "does not say which contract it read cannot be checked"
            )


@dataclass(frozen=True, slots=True)
class OpenInterestValueObservation:
    """The vendor sent this open-interest number, in this record.

    A statement about a *value*. Deliberately carries no date: this type
    existing separately is what stops a confirmed number from being read as a
    confirmed settlement session.
    """

    record_id: str
    observed_value: int
    contract_identity: ContractIdentity | None = None

    def __post_init__(self) -> None:
        if not str(self.record_id).strip():
            raise ThetaDataProvenanceError(
                "OpenInterestValueObservation.record_id is empty; an observation "
                "that does not name the bytes it read cannot be confirmed"
            )
        if isinstance(self.observed_value, bool) or not isinstance(
            self.observed_value, int
        ):
            raise ThetaDataProvenanceError(
                f"open interest must be an integer, got "
                f"{type(self.observed_value).__name__} {self.observed_value!r}. "
                "A fractional contract count is not a thing the vendor can send."
            )
        if self.observed_value < 0:
            raise ThetaDataProvenanceError(
                f"open interest is {self.observed_value}; a negative contract "
                "count is a parse failure, not a small position"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "observed_value": self.observed_value,
            "contract_identity": (
                self.contract_identity.canonical_id if self.contract_identity else None
            ),
        }


@dataclass(frozen=True, slots=True)
class OpenInterestAsOfEvidence:
    """The settlement session an open-interest figure belongs to, and why.

    ``evidence_kind`` is the whole point. It is not a confidence score; it names
    the sort of thing being relied on, and the trusted path reads it rather than
    inferring strength from the presence of a related observation.
    """

    as_of: date
    source: str
    record_ids: tuple[str, ...] = ()
    evidence_kind: EvidenceKind = EvidenceKind.CALLER_ASSUMPTION
    #: Where a reader can check a documentation-based claim.
    reference: str = ""
    #: The session the chain belongs to, so an impossible ordering is refused.
    chain_date: date | None = None

    def __post_init__(self) -> None:
        for name in ("as_of", "chain_date"):
            value = getattr(self, name)
            if value is None and name == "chain_date":
                continue
            if isinstance(value, datetime) or type(value) is not date:
                raise ThetaDataProvenanceError(
                    f"OpenInterestAsOfEvidence.{name} must be a date, got "
                    f"{type(value).__name__} {value!r}. Open interest settles "
                    "per session, not per instant."
                )
        try:
            kind = EvidenceKind(self.evidence_kind)
        except ValueError as error:
            raise ThetaDataProvenanceError(
                f"{self.evidence_kind!r} is not a recognised evidence kind; "
                f"valid values are {[k.value for k in EvidenceKind]}"
            ) from error
        object.__setattr__(self, "evidence_kind", kind)

        if self.chain_date is not None and self.as_of > self.chain_date:
            raise ThetaDataProvenanceError(
                f"open interest as_of {self.as_of.isoformat()} is after the "
                f"chain date {self.chain_date.isoformat()}. Open interest "
                "settles before the session it weights; a later date describes "
                "a session that has not happened."
            )
        if kind is EvidenceKind.VENDOR_FIELD and not self.record_ids:
            raise ThetaDataProvenanceError(
                "VENDOR_FIELD evidence must name the records the date was read "
                "from. A vendor field nobody can point at is an assumption."
            )
        if (
            kind is EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION
            and not self.reference.strip()
        ):
            raise ThetaDataProvenanceError(
                "documentation evidence must carry a reference a reader can "
                "open; an unreferenced claim about vendor behaviour cannot be "
                "checked by anyone reading the certification report"
            )

    @property
    def permits_trusted_calculation(self) -> bool:
        return self.evidence_kind.permits_trusted_calculation

    @property
    def blocker(self) -> str:
        """Why this evidence does not permit a trusted calculation, if it does not."""
        if self.permits_trusted_calculation:
            return ""
        return (
            f"the open-interest settlement date {self.as_of.isoformat()} rests "
            f"on {self.evidence_kind.value}: this repository chose it, and no "
            "vendor field, documented convention or verified schedule "
            "establishes it. Open interest is the linear weight on every GEX "
            "term, so a trusted number cannot rest on our own guess about which "
            "session it settled in. A raw capture and a diagnostic calculation "
            "are unaffected. See docs/OPEN_DECISIONS.md OD-26."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "source": self.source,
            "record_ids": list(self.record_ids),
            "evidence_kind": self.evidence_kind.value,
            "reference": self.reference,
            "chain_date": self.chain_date.isoformat() if self.chain_date else None,
            "permits_trusted_calculation": self.permits_trusted_calculation,
        }
