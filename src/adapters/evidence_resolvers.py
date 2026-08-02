"""Turning a claim about a settlement date into a checked one.

v2.1.7 separated the open-interest *value* from the open-interest *date*, which
was the right split, and then let the date authorize itself:

    OpenInterestAsOfEvidence(
        as_of=date(2026, 3, 16),
        source="vendor_field",
        evidence_kind=EvidenceKind.VENDOR_FIELD,
        record_ids=("fake-record",),
    )

``VENDOR_FIELD`` permits a trusted calculation, so that object did too. The
record id was never opened. ``AUTHORITATIVE_VENDOR_DOCUMENTATION`` needed a
non-empty ``reference`` and ``"lol"`` is non-empty. The enum was doing the work
of the check.

A *resolver* does the check. Each kind has one, each one either produces a
resolved date or says why it could not, and none of them takes the caller's word
for the thing it is supposed to establish:

* **vendor field** -- reread the named field, out of the named record, and
  compare the hashes on the way;
* **documentation** -- look the rule up in a registry by id, and hash the
  document it points at, so changing the content changes the fingerprint;
* **schedule** -- run a versioned derivation over explicit inputs and keep the
  artefact it produced;
* **caller assumption** -- resolve to nothing that authorizes anything, which is
  the honest state of this repository today.
"""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.adapters.errors import ThetaDataProvenanceError
from src.adapters.open_interest import EvidenceKind
from src.domain.digests import digest_of

__all__ = [
    "DocumentationRule",
    "DocumentationRuleRegistry",
    "ResolvedSettlementDate",
    "ScheduleDerivation",
    "resolve_settlement_date",
]


@dataclass(frozen=True, slots=True)
class DocumentationRule:
    """A rule this repository read out of a document, bound to that document.

    ``document_content_hash`` is what makes it evidence rather than a citation.
    A reference alone says where somebody looked; the hash says what was there
    when they looked, so a vendor quietly rewriting a page changes the
    fingerprint and the pipeline that relied on it.
    """

    evidence_id: str
    document_reference: str
    document_content_hash: str
    rule_identifier: str
    effective_from: date
    #: ``None`` where the rule is still current.
    effective_to: date | None = None
    #: Which version of *our* reading produced the normalised value. A document
    #: can stay the same while our interpretation of it improves.
    derivation_version: str = ""
    #: What the rule says, normalised. For a settlement convention this is the
    #: offset in sessions from the chain date.
    normalized_value: str = ""
    observed_on: date | None = None

    def __post_init__(self) -> None:
        for name in ("evidence_id", "document_reference", "rule_identifier"):
            if not str(getattr(self, name)).strip():
                raise ThetaDataProvenanceError(
                    f"DocumentationRule.{name} is empty; a rule that does not "
                    "say what it is, or where it came from, cannot be checked "
                    "by anyone reading the certification report"
                )
        if len(self.document_content_hash) != 64:
            raise ThetaDataProvenanceError(
                f"DocumentationRule.document_content_hash is "
                f"{len(self.document_content_hash)} characters; a full SHA-256 "
                "of the referenced content is what makes this evidence rather "
                "than a citation. A vendor can rewrite a page."
            )
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ThetaDataProvenanceError(
                f"DocumentationRule {self.evidence_id!r} stops being effective "
                f"({self.effective_to.isoformat()}) before it starts "
                f"({self.effective_from.isoformat()})"
            )

    def covers(self, moment: date) -> bool:
        """Whether this rule was in force on a given session."""
        if moment < self.effective_from:
            return False
        return self.effective_to is None or moment <= self.effective_to

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "document_reference": self.document_reference,
            "document_content_hash": self.document_content_hash,
            "rule_identifier": self.rule_identifier,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": (
                self.effective_to.isoformat() if self.effective_to else None
            ),
            "derivation_version": self.derivation_version,
            "normalized_value": self.normalized_value,
            "observed_on": (self.observed_on.isoformat() if self.observed_on else None),
        }

    @property
    def evidence_fingerprint(self) -> str:
        """Full SHA-256. Moves when the referenced content moves."""
        return digest_of(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "evidence_fingerprint": self.evidence_fingerprint,
        }


class DocumentationRuleRegistry:
    """The rules this repository has actually read and recorded.

    A registry rather than free text on the config, because the point is that a
    pipeline may *reference* an evidence id and may not *supply* evidence. An
    arbitrary string in a YAML file is a claim about vendor behaviour that
    nobody checked, and ``reference="lol"`` satisfied v2.1.7.
    """

    def __init__(self, rules: dict[str, DocumentationRule] | None = None) -> None:
        self._rules = dict(rules or {})

    def register(self, rule: DocumentationRule) -> None:
        existing = self._rules.get(rule.evidence_id)
        if existing is not None and existing != rule:
            raise ThetaDataProvenanceError(
                f"evidence id {rule.evidence_id!r} is already registered with "
                "different content. An id that means two things is worse than "
                "no id: a pipeline referencing it would be relying on whichever "
                "one happened to load first."
            )
        self._rules[rule.evidence_id] = rule

    def get(self, evidence_id: str) -> DocumentationRule | None:
        return self._rules.get(evidence_id)

    def fingerprint_for(self, evidence_id: str) -> str | None:
        rule = self.get(evidence_id)
        return rule.evidence_fingerprint if rule is not None else None

    def __contains__(self, evidence_id: object) -> bool:
        return evidence_id in self._rules

    def __len__(self) -> int:
        return len(self._rules)


#: The registry the pipeline consults. Deliberately empty: this repository has
#: read no ThetaData document establishing an open-interest settlement
#: convention, and pre-populating it with a plausible-looking entry would be
#: exactly the defect being closed. See OPEN_DECISIONS OD-26 and OD-37.
DOCUMENTATION_RULES = DocumentationRuleRegistry()


def content_hash_of(path: pathlib.Path) -> str:
    """SHA-256 of a document this repository ships, for a registry entry."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ScheduleDerivation:
    """What a versioned settlement-schedule derivation actually did.

    Required for ``DERIVED_FROM_VERIFIED_VENDOR_SCHEDULE``, because "derived
    from a schedule" without the derivation is the same shape of claim as a
    citation without the document.
    """

    rule_version: str
    input_session_date: date
    derived_settlement_date: date
    supporting_evidence_id: str

    def __post_init__(self) -> None:
        for name in ("rule_version", "supporting_evidence_id"):
            if not str(getattr(self, name)).strip():
                raise ThetaDataProvenanceError(
                    f"ScheduleDerivation.{name} is empty; a derivation that "
                    "cannot say which rule produced it, or what backs that "
                    "rule, is an assertion with extra steps"
                )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "rule_version": self.rule_version,
            "input_session_date": self.input_session_date.isoformat(),
            "derived_settlement_date": self.derived_settlement_date.isoformat(),
            "supporting_evidence_id": self.supporting_evidence_id,
        }

    @property
    def fingerprint(self) -> str:
        return digest_of(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self.semantic_payload(), "fingerprint": self.fingerprint}


@dataclass(frozen=True, slots=True)
class ResolvedSettlementDate:
    """The outcome of asking a resolver to establish a settlement date."""

    as_of: date | None
    evidence_kind: EvidenceKind
    #: Empty when the date was established. Otherwise why it was not.
    failure: str = ""
    #: Full digest of whatever established it, for the operation identity.
    rule_fingerprint: str | None = None
    record_ids: tuple[str, ...] = ()

    @property
    def established(self) -> bool:
        return self.as_of is not None and not self.failure

    @property
    def permits_trusted_calculation(self) -> bool:
        return self.established and self.evidence_kind.permits_trusted_calculation

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "evidence_kind": self.evidence_kind.value,
            "established": self.established,
            "permits_trusted_calculation": self.permits_trusted_calculation,
            "failure": self.failure,
            "rule_fingerprint": self.rule_fingerprint,
            "record_ids": list(self.record_ids),
        }


#: Fields a vendor response would have to carry for ``VENDOR_FIELD`` to mean
#: anything. None of ThetaData's snapshot endpoints has one -- which is the
#: whole of OD-26 -- so this list exists to be checked against, not to be
#: satisfied today.
SETTLEMENT_DATE_FIELDS = ("open_interest_as_of", "settlement_date", "oi_date")


def resolve_settlement_date(
    evidence: Any,
    *,
    manifest: Any = None,
    store: Any = None,
    registry: DocumentationRuleRegistry | None = None,
    derivation: ScheduleDerivation | None = None,
) -> ResolvedSettlementDate:
    """Establish a settlement date, or say precisely why it was not established.

    Never trusts the enum. The kind selects *which check to run*; passing it
    does not pass the check.
    """
    kind = EvidenceKind(evidence.evidence_kind)

    if kind is EvidenceKind.CALLER_ASSUMPTION:
        return ResolvedSettlementDate(
            as_of=evidence.as_of,
            evidence_kind=kind,
            failure="",
            rule_fingerprint=None,
        )

    if kind is EvidenceKind.VENDOR_FIELD:
        return _resolve_vendor_field(evidence, manifest=manifest, store=store)

    if kind is EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION:
        return _resolve_documentation(evidence, registry=registry)

    return _resolve_schedule(evidence, registry=registry, derivation=derivation)


def _resolve_vendor_field(
    evidence: Any, *, manifest: Any, store: Any
) -> ResolvedSettlementDate:
    """Reread the settlement date out of the record that is said to carry it."""
    kind = EvidenceKind.VENDOR_FIELD
    if manifest is None or store is None:
        return ResolvedSettlementDate(
            as_of=None,
            evidence_kind=kind,
            failure=(
                "vendor-field evidence was offered with no capture to read it "
                "from, so the field it names was never opened"
            ),
        )
    known = set(getattr(manifest, "record_ids", ()))
    unknown = sorted(set(evidence.record_ids) - known)
    if unknown:
        return ResolvedSettlementDate(
            as_of=None,
            evidence_kind=kind,
            failure=(
                f"vendor-field evidence names records {unknown} which this "
                "capture does not contain. v2.1.7 accepted "
                "``record_ids=('fake-record',)`` because the enum permitted a "
                "trusted calculation and nobody opened the record."
            ),
        )

    from src.adapters.thetadata.endpoints import Endpoint
    from src.adapters.validation import AdapterValidator

    for record_id in evidence.record_ids:
        for field_path in SETTLEMENT_DATE_FIELDS:
            try:
                observation = AdapterValidator.observe_field(
                    manifest=manifest,
                    store=store,
                    endpoint=Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT,
                    field_path=field_path,
                )
            except ThetaDataProvenanceError:
                continue
            observed = _as_date(observation.observed_value)
            if observed is None:
                continue
            if observed != evidence.as_of:
                return ResolvedSettlementDate(
                    as_of=None,
                    evidence_kind=kind,
                    failure=(
                        f"the vendor field says {observed.isoformat()} and the "
                        f"evidence claims {evidence.as_of.isoformat()}"
                    ),
                    record_ids=(record_id,),
                )
            return ResolvedSettlementDate(
                as_of=observed,
                evidence_kind=kind,
                rule_fingerprint=digest_of(
                    {
                        "kind": kind.value,
                        "field_path": field_path,
                        "record_id": observation.record_id,
                        "payload_hash": observation.payload_hash,
                        "observed": observed.isoformat(),
                    }
                ),
                record_ids=(observation.record_id,),
            )

    return ResolvedSettlementDate(
        as_of=None,
        evidence_kind=kind,
        failure=(
            "no captured open-interest response carries a settlement-date "
            f"field. Looked for {list(SETTLEMENT_DATE_FIELDS)}; ThetaData's "
            "snapshot endpoints publish none, which is OPEN_DECISIONS OD-26. A "
            "response containing an open-interest *number* does not establish "
            "which session that number settled in."
        ),
    )


def _resolve_documentation(
    evidence: Any, *, registry: DocumentationRuleRegistry | None
) -> ResolvedSettlementDate:
    """Look the rule up by id, and use what the registry holds."""
    kind = EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION
    rules = registry if registry is not None else DOCUMENTATION_RULES
    evidence_id = str(getattr(evidence, "reference", "") or "").strip()
    rule = rules.get(evidence_id)
    if rule is None:
        return ResolvedSettlementDate(
            as_of=None,
            evidence_kind=kind,
            failure=(
                f"{evidence_id!r} is not a registered documentation rule. A "
                "pipeline may reference an evidence id; it may not supply "
                "evidence text per calculation, which is how "
                "``reference='lol'`` authorized a settlement date in v2.1.7. "
                f"Registered ids: {sorted(rules._rules)}"
            ),
        )
    if not rule.covers(evidence.as_of):
        return ResolvedSettlementDate(
            as_of=None,
            evidence_kind=kind,
            failure=(
                f"documentation rule {rule.evidence_id!r} was in force from "
                f"{rule.effective_from.isoformat()} to "
                f"{rule.effective_to.isoformat() if rule.effective_to else 'now'}, "
                f"which does not cover {evidence.as_of.isoformat()}"
            ),
        )
    return ResolvedSettlementDate(
        as_of=evidence.as_of,
        evidence_kind=kind,
        rule_fingerprint=rule.evidence_fingerprint,
    )


def _resolve_schedule(
    evidence: Any,
    *,
    registry: DocumentationRuleRegistry | None,
    derivation: ScheduleDerivation | None,
) -> ResolvedSettlementDate:
    """Require the derivation artefact, and the documentation behind it."""
    kind = EvidenceKind.DERIVED_FROM_VERIFIED_VENDOR_SCHEDULE
    if derivation is None:
        return ResolvedSettlementDate(
            as_of=None,
            evidence_kind=kind,
            failure=(
                "schedule-derived evidence was offered with no derivation "
                "artefact. 'Derived from a schedule' without the derivation is "
                "a citation without the document."
            ),
        )
    if derivation.derived_settlement_date != evidence.as_of:
        return ResolvedSettlementDate(
            as_of=None,
            evidence_kind=kind,
            failure=(
                f"the derivation produced "
                f"{derivation.derived_settlement_date.isoformat()} and the "
                f"evidence claims {evidence.as_of.isoformat()}"
            ),
        )
    rules = registry if registry is not None else DOCUMENTATION_RULES
    supporting = rules.get(derivation.supporting_evidence_id)
    if supporting is None:
        return ResolvedSettlementDate(
            as_of=None,
            evidence_kind=kind,
            failure=(
                f"the derivation rests on evidence "
                f"{derivation.supporting_evidence_id!r}, which is not "
                "registered. A schedule nobody documented is a schedule "
                "somebody assumed."
            ),
        )
    return ResolvedSettlementDate(
        as_of=evidence.as_of,
        evidence_kind=kind,
        rule_fingerprint=digest_of(
            {
                "derivation": derivation.semantic_payload(),
                "supporting": supporting.semantic_payload(),
            }
        ),
    )


def _as_date(value: object) -> date | None:
    """A settlement date read out of a payload, or nothing."""
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None
