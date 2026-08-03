"""Turning a claim about a settlement date into a derived one.

v2.1.7 let an enum authorize a date: ``VENDOR_FIELD`` permitted a trusted
calculation, so an object carrying that kind did too, and the record it named
was never opened.

v2.1.8 replaced the enum with resolvers, which was the right move and stopped
one step short. The documentation resolver did this:

    rule = rules.get(evidence.reference)
    if not rule.covers(evidence.as_of):
        return failure
    return ResolvedSettlementDate(as_of=evidence.as_of, ...)

The date still came from the caller. The rule was consulted only to confirm it
was *in force* on the day the caller had already picked, so a single registered
rule saying "prior trading session" would authorize 2026-03-16, 2026-03-15 and
2026-03-01 alike for a 2026-03-17 chain. A permission slip, not a calculation.

``normalized_value: str`` is why. A rule whose content is free text cannot be
applied to anything, so the date had to come from somewhere else -- and the only
somewhere else available was the caller.

Two changes close it:

* a rule carries **typed semantics** -- a :class:`SettlementRule` with a kind, an
  offset and a calendar -- and the resolver *applies* it to the chain's session
  date. Nothing here takes a settlement date as an input, so there is nothing
  for a caller to assert;
* a rule is registered only after its document has been **read and hashed**. A
  64-character string in a dataclass field is a claim that somebody hashed
  something; reading the bytes is the hashing.
"""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from src.adapters.errors import ThetaDataProvenanceError
from src.adapters.open_interest import EvidenceKind
from src.domain.digests import digest_of
from src.domain.settlement import (
    SETTLEMENT_EVIDENCE_SCHEMA_VERSION,
    SettlementRule,
    SettlementRuleError,
    SettlementRuleKind,
)

__all__ = [
    "DOCUMENTATION_RULES",
    "SETTLEMENT_DATE_FIELDS",
    "DocumentationRule",
    "DocumentationRuleRegistry",
    "ResolvedSettlementDate",
    "ScheduleDerivation",
    "SettlementDateRuleArtifact",
    "content_hash_of",
    "resolve_settlement_date",
    "settlement_artifact_from",
    "verify_document",
]

#: Where a document reference is resolved from. Repository-relative, so a
#: reference is checkable by anyone with the source tree and cannot silently
#: mean a file on the author's machine.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: How many sessions back a vendor-stated settlement date may sit before this
#: repository stops trying to express it as a rule. Ten sessions is already two
#: weeks; anything further is a stale feed rather than a convention.
MAX_EXPRESSIBLE_SESSION_OFFSET = 10


def content_hash_of(path: pathlib.Path) -> str:
    """SHA-256 of a document this repository ships."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_document(reference: str, claimed_hash: str) -> tuple[str, str]:
    """Read the referenced bytes and check them against the claimed digest.

    Returns ``(resolved_location, actual_hash)``. Raises rather than returning a
    verdict: a rule whose document cannot be produced is not weak evidence, it
    is a citation of something nobody can open.
    """
    text = str(reference or "").strip()
    if not text:
        raise ThetaDataProvenanceError(
            "a documentation rule must reference a document; an unreferenced "
            "claim about vendor behaviour cannot be checked by anyone reading "
            "the certification report"
        )
    if text.startswith(("http://", "https://")):
        raise ThetaDataProvenanceError(
            f"{text!r} is a URL. This repository makes no network request to "
            "verify a document, and a URL whose content nobody read is a "
            "citation rather than evidence. Save the page into the repository "
            "and reference the file, so the bytes travel with the claim."
        )
    # A fragment names a section of a document, not a different document.
    reference_path = text.split("#", 1)[0]
    target = pathlib.Path(reference_path)
    # ``is_absolute`` is platform-dependent -- ``/etc/x`` is relative on Windows,
    # having no drive -- and a repository-relative reference never begins with a
    # separator on any platform. Checking the spelling as well keeps the same
    # reference refused the same way wherever the tests run.
    if (
        target.is_absolute()
        or reference_path.startswith(("/", "\\"))
        or ".." in target.parts
    ):
        raise ThetaDataProvenanceError(
            f"{text!r} must be a repository-relative path; an absolute path is "
            "a fact about one machine"
        )
    resolved = REPO_ROOT / target
    if not resolved.is_file():
        raise ThetaDataProvenanceError(
            f"{text!r} does not exist. A documentation rule is registered only "
            "after its bytes have been read: until v2.1.9 a rule could carry "
            "any 64-character string and the file was never opened, so "
            "``document_reference='/definitely/missing'`` with "
            "``document_content_hash='0'*64`` registered cleanly."
        )
    actual = content_hash_of(resolved)
    if actual != claimed_hash:
        raise ThetaDataProvenanceError(
            f"{text!r} hashes to {actual} and the rule claims {claimed_hash}. "
            "The document has changed since the rule was read, or the rule was "
            "never read from it. Either way what the rule says the vendor does "
            "is no longer what the cited document says."
        )
    return str(target.as_posix()), actual


@dataclass(frozen=True, slots=True)
class DocumentationRule:
    """A settlement convention this repository read out of a document.

    Two things make it evidence rather than an assertion, and v2.1.8 had
    neither in full:

    * ``rule`` is typed semantics that :meth:`SettlementRule.resolve` can apply.
      The v2.1.8 field was ``normalized_value: str``, which nothing could apply,
      so the date had to come from the caller;
    * ``document_content_hash`` is checked against the referenced bytes at
      registration. A vendor can rewrite a page without renaming it.
    """

    evidence_id: str
    document_reference: str
    document_content_hash: str
    rule_identifier: str
    effective_from: date
    #: What the document says, in a form that computes a date.
    rule: SettlementRule | None = None
    #: ``None`` where the rule is still current.
    effective_to: date | None = None
    #: Which version of *our* reading produced the semantics. A document can
    #: stay the same while our interpretation of it improves.
    derivation_version: str = ""
    observed_on: date | None = None
    schema_version: str = SETTLEMENT_EVIDENCE_SCHEMA_VERSION
    #: Filled by ``DocumentationRuleRegistry.register`` once the bytes have been
    #: read. An unverified rule cannot resolve anything.
    verified_location: str = field(default="", compare=False)

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
        if self.rule is not None and not isinstance(self.rule, SettlementRule):
            raise ThetaDataProvenanceError(
                f"DocumentationRule.rule must be a SettlementRule, got "
                f"{type(self.rule).__name__}. Free text cannot be applied to a "
                "session date, which is why v2.1.8 had to take the settlement "
                "date from the caller instead of deriving it."
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

    @property
    def establishes_a_date(self) -> bool:
        """Whether this rule can produce a settlement date at all.

        A rule with no typed semantics documents *something* -- and not the
        thing a trusted calculation needs.
        """
        return self.rule is not None and bool(self.verified_location)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "document_reference": self.document_reference,
            "document_content_hash": self.document_content_hash,
            "rule_identifier": self.rule_identifier,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": (
                self.effective_to.isoformat() if self.effective_to else None
            ),
            "derivation_version": self.derivation_version,
            "rule": self.rule.semantic_payload() if self.rule else None,
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
            "verified_location": self.verified_location,
            "establishes_a_date": self.establishes_a_date,
        }


class DocumentationRuleRegistry:
    """The rules this repository has read, hashed and recorded.

    A registry rather than free text on the config, because the point is that a
    pipeline may *reference* an evidence id and may not *supply* evidence. An
    arbitrary string in a YAML file is a claim about vendor behaviour that
    nobody checked, and ``reference="lol"`` satisfied v2.1.7.

    ``register`` opens the document. That is the difference between v2.1.8 and
    v2.1.9: there, a rule carried a hash nobody had computed.
    """

    def __init__(self, rules: dict[str, DocumentationRule] | None = None) -> None:
        self._rules: dict[str, DocumentationRule] = {}
        for rule in (rules or {}).values():
            self.register(rule)

    def register(self, rule: DocumentationRule) -> DocumentationRule:
        """Verify the document, then record the rule. Raises if it cannot."""
        location, _ = verify_document(
            rule.document_reference, rule.document_content_hash
        )
        verified = (
            rule
            if rule.verified_location == location
            else replace(rule, verified_location=location)
        )
        existing = self._rules.get(rule.evidence_id)
        if existing is not None and existing.semantic_payload() != (
            verified.semantic_payload()
        ):
            raise ThetaDataProvenanceError(
                f"evidence id {rule.evidence_id!r} is already registered with "
                "different content. An id that means two things is worse than "
                "no id: a pipeline referencing it would be relying on whichever "
                "one happened to load first."
            )
        self._rules[rule.evidence_id] = verified
        return verified

    def get(self, evidence_id: str) -> DocumentationRule | None:
        return self._rules.get(evidence_id)

    def fingerprint_for(self, evidence_id: str) -> str | None:
        rule = self.get(evidence_id)
        return rule.evidence_fingerprint if rule is not None else None

    def registered_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._rules))

    def __contains__(self, evidence_id: object) -> bool:
        return evidence_id in self._rules

    def __len__(self) -> int:
        return len(self._rules)


#: The registry the pipeline consults. Deliberately empty: this repository has
#: read no ThetaData document establishing an open-interest settlement
#: convention, and pre-populating it with a plausible-looking entry would be
#: exactly the defect being closed. See OPEN_DECISIONS OD-26 and OD-37.
DOCUMENTATION_RULES = DocumentationRuleRegistry()


@dataclass(frozen=True, slots=True)
class ScheduleDerivation:
    """What a versioned settlement-schedule derivation actually did.

    Required for ``DERIVED_FROM_VERIFIED_VENDOR_SCHEDULE``, because "derived
    from a schedule" without the derivation is the same shape of claim as a
    citation without the document.

    Since v2.1.9 it carries no ``derived_settlement_date``: the date is what the
    supporting rule *produces* from the input session, and a derivation stating
    its own answer would be the v2.1.8 defect one level down.
    """

    rule_version: str
    input_session_date: date
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
            "supporting_evidence_id": self.supporting_evidence_id,
        }

    @property
    def fingerprint(self) -> str:
        return digest_of(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self.semantic_payload(), "fingerprint": self.fingerprint}


@dataclass(frozen=True, slots=True)
class ResolvedSettlementDate:
    """The outcome of asking a resolver to *derive* a settlement date."""

    as_of: date | None
    evidence_kind: EvidenceKind
    #: Empty when the date was established. Otherwise why it was not.
    failure: str = ""
    #: Full digest of whatever established it, for the operation identity.
    rule_fingerprint: str | None = None
    record_ids: tuple[str, ...] = ()
    #: The typed semantics that produced ``as_of``, where semantics were used.
    rule: SettlementRule | None = None
    evidence_id: str = ""
    documentation_content_hash: str | None = None
    derivation_version: str = ""

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
            "rule": self.rule.as_dict() if self.rule else None,
            "evidence_id": self.evidence_id,
            "documentation_content_hash": self.documentation_content_hash,
            "derivation_version": self.derivation_version,
        }


@dataclass(frozen=True, slots=True)
class SettlementDateRuleArtifact:
    """The settlement authority one capture operation was opened under.

    Immutable, hashed whole, and fixed *before* any response arrives. A capture
    that carries none is usable for raw storage, diagnostic calculation and
    vendor-schema research, and can never become eligible for a trusted GEX
    afterwards -- because there is no argument through which a later caller can
    supply one.

    Both dates are here on purpose. ``chain_session_date`` is the input the rule
    was applied to; ``resolved_settlement_date`` is what it produced. Recording
    only the second would make the artifact re-checkable in principle and not in
    practice.
    """

    evidence_kind: EvidenceKind
    rule_fingerprint: str
    evidence_id: str
    normalized_rule: SettlementRule
    chain_session_date: date
    resolved_settlement_date: date
    documentation_content_hash: str | None = None
    derivation_version: str = ""
    rule_schema_version: str = SETTLEMENT_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        kind = EvidenceKind(self.evidence_kind)
        object.__setattr__(self, "evidence_kind", kind)
        if not kind.permits_trusted_calculation:
            raise ThetaDataProvenanceError(
                f"a settlement artifact cannot rest on {kind.value}: this "
                "repository chose the date, and an artifact exists to record "
                "that the vendor established it. A capture with no artifact is "
                "the honest way to say nobody did."
            )
        if not str(self.rule_fingerprint).strip():
            raise ThetaDataProvenanceError(
                "SettlementDateRuleArtifact.rule_fingerprint is empty; an "
                "artifact nobody can identify cannot bind a capture to anything"
            )
        if not isinstance(self.normalized_rule, SettlementRule):
            raise ThetaDataProvenanceError(
                "SettlementDateRuleArtifact.normalized_rule must be a "
                "SettlementRule; without typed semantics the settlement date "
                "cannot be re-derived and the artifact is only a record of what "
                "somebody once believed"
            )
        # The artifact re-checks itself. A stored date that its own rule does
        # not produce is the v2.1.8 defect preserved in amber.
        try:
            produced = self.normalized_rule.resolve(self.chain_session_date)
        except SettlementRuleError as error:
            raise ThetaDataProvenanceError(
                f"the artifact's rule cannot be applied to "
                f"{self.chain_session_date.isoformat()}: {error}"
            ) from error
        if produced != self.resolved_settlement_date:
            raise ThetaDataProvenanceError(
                f"the rule produces {produced.isoformat()} for chain session "
                f"{self.chain_session_date.isoformat()} and the artifact records "
                f"{self.resolved_settlement_date.isoformat()}. A settlement date "
                "its own rule does not derive is an assertion."
            )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "rule_schema_version": self.rule_schema_version,
            "evidence_kind": self.evidence_kind.value,
            "rule_fingerprint": self.rule_fingerprint,
            "evidence_id": self.evidence_id,
            "documentation_content_hash": self.documentation_content_hash,
            "derivation_version": self.derivation_version,
            "normalized_rule": self.normalized_rule.semantic_payload(),
            "chain_session_date": self.chain_session_date.isoformat(),
            "resolved_settlement_date": self.resolved_settlement_date.isoformat(),
        }

    @property
    def artifact_hash(self) -> str:
        return digest_of(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self.semantic_payload(), "artifact_hash": self.artifact_hash}


def settlement_artifact_from(
    resolved: ResolvedSettlementDate, *, chain_session_date: date
) -> SettlementDateRuleArtifact:
    """The artifact a resolved settlement date supports, or a refusal."""
    if not resolved.established:
        raise ThetaDataProvenanceError(
            f"no settlement date was established: {resolved.failure}"
        )
    if resolved.rule is None or resolved.rule_fingerprint is None:
        raise ThetaDataProvenanceError(
            "the settlement date was established without typed semantics, so "
            "it cannot be re-derived at replay. Only a rule that computes a "
            "date can bind a capture."
        )
    assert resolved.as_of is not None  # `established` guarantees it
    return SettlementDateRuleArtifact(
        evidence_kind=resolved.evidence_kind,
        rule_fingerprint=resolved.rule_fingerprint,
        evidence_id=resolved.evidence_id,
        normalized_rule=resolved.rule,
        chain_session_date=chain_session_date,
        resolved_settlement_date=resolved.as_of,
        documentation_content_hash=resolved.documentation_content_hash,
        derivation_version=resolved.derivation_version,
    )


#: Fields a vendor response would have to carry for ``VENDOR_FIELD`` to mean
#: anything. None of ThetaData's snapshot endpoints has one -- which is the
#: whole of OD-26 -- so this list exists to be checked against, not to be
#: satisfied today.
SETTLEMENT_DATE_FIELDS = ("open_interest_as_of", "settlement_date", "oi_date")


def resolve_settlement_date(
    evidence: Any = None,
    *,
    chain_session_date: date,
    manifest: Any = None,
    store: Any = None,
    registry: DocumentationRuleRegistry | None = None,
    derivation: ScheduleDerivation | None = None,
    evidence_kind: EvidenceKind | None = None,
    evidence_id: str = "",
    record_ids: tuple[str, ...] = (),
) -> ResolvedSettlementDate:
    """Derive a settlement date for a chain session, or say why none follows.

    ``chain_session_date`` is the *input*. There is deliberately no parameter
    for the answer: every v2.1.8 resolver except the vendor-field one took the
    caller's ``as_of`` and handed it back once a document had been found to be
    in force, which meant one rule could authorize any date.
    """
    kind = EvidenceKind(
        evidence_kind
        if evidence_kind is not None
        else getattr(evidence, "evidence_kind", EvidenceKind.CALLER_ASSUMPTION)
    )
    reference = str(evidence_id or getattr(evidence, "reference", "") or "").strip()
    records = tuple(record_ids or getattr(evidence, "record_ids", ()) or ())

    if kind is EvidenceKind.CALLER_ASSUMPTION:
        return ResolvedSettlementDate(
            as_of=None,
            evidence_kind=kind,
            failure=(
                "a caller assumption establishes no settlement date. It is a "
                "legitimate thing to hold and it is not evidence about the "
                "vendor, so there is nothing here for a resolver to derive."
            ),
        )

    if kind is EvidenceKind.VENDOR_FIELD:
        return _resolve_vendor_field(
            record_ids=records,
            manifest=manifest,
            store=store,
            chain_session_date=chain_session_date,
        )

    if kind is EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION:
        return _resolve_documentation(
            evidence_id=reference,
            registry=registry,
            chain_session_date=chain_session_date,
        )

    return _resolve_schedule(
        registry=registry,
        derivation=derivation,
        chain_session_date=chain_session_date,
    )


def _resolve_vendor_field(
    *,
    record_ids: tuple[str, ...],
    manifest: Any,
    store: Any,
    chain_session_date: date,
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
    if not record_ids:
        return ResolvedSettlementDate(
            as_of=None,
            evidence_kind=kind,
            failure=(
                "vendor-field evidence names no record. A vendor field nobody "
                "can point at is an assumption."
            ),
        )
    known = set(getattr(manifest, "record_ids", ()))
    unknown = sorted(set(record_ids) - known)
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

    for record_id in record_ids:
        for field_path in SETTLEMENT_DATE_FIELDS:
            try:
                observation = AdapterValidator.observe_field(
                    manifest=manifest,
                    store=store,
                    endpoint=Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT,
                    field_path=field_path,
                    # The exact record the evidence names. Reading the
                    # endpoint's first record instead would confirm a claim
                    # about one response against a different one.
                    record_id=record_id,
                )
            except ThetaDataProvenanceError:
                continue
            observed = _as_date(observation.observed_value)
            if observed is None:
                continue
            if observed > chain_session_date:
                return ResolvedSettlementDate(
                    as_of=None,
                    evidence_kind=kind,
                    failure=(
                        f"the vendor field says {observed.isoformat()}, which is "
                        f"after the chain session "
                        f"{chain_session_date.isoformat()}; open interest "
                        "settles before the session it weights"
                    ),
                    record_ids=(observation.record_id,),
                )
            expressed = _field_rule(observed, chain_session_date)
            if expressed is None:
                return ResolvedSettlementDate(
                    as_of=None,
                    evidence_kind=kind,
                    failure=(
                        f"the vendor field says {observed.isoformat()}, which is "
                        f"more than {MAX_EXPRESSIBLE_SESSION_OFFSET} sessions "
                        f"before {chain_session_date.isoformat()}. That is a "
                        "stale feed rather than a settlement convention, and "
                        "open interest is the weight on every GEX term."
                    ),
                    record_ids=(observation.record_id,),
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
                # The vendor stated the date outright; the "rule" is the session
                # offset that date actually is, so replay can re-derive it.
                rule=expressed,
                evidence_id=observation.record_id,
                derivation_version="vendor-field/1",
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


def _field_rule(observed: date, chain_session_date: date) -> SettlementRule | None:
    """The typed rule a vendor-stated date is consistent with, if any.

    A response that names its own settlement date does not need a convention --
    but the artifact needs semantics it can re-derive from, so the observed date
    is expressed as the session offset it actually is. ``None`` when the gap is
    larger than any rule this repository will express, which is itself worth
    refusing rather than papering over.
    """
    from src.gex.calendar import previous_session

    cursor = chain_session_date
    for steps in range(MAX_EXPRESSIBLE_SESSION_OFFSET + 1):
        if cursor == observed:
            if steps == 0:
                return SettlementRule(kind=SettlementRuleKind.SAME_SESSION)
            if steps == 1:
                return SettlementRule(kind=SettlementRuleKind.PRIOR_TRADING_SESSION)
            return SettlementRule(
                kind=SettlementRuleKind.TRADING_SESSION_OFFSET,
                trading_session_offset=steps,
            )
        cursor = previous_session(cursor)
    return None


def _resolve_documentation(
    *,
    evidence_id: str,
    registry: DocumentationRuleRegistry | None,
    chain_session_date: date,
) -> ResolvedSettlementDate:
    """Look the rule up by id, then **apply** it to the chain session."""
    kind = EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION
    rules = registry if registry is not None else DOCUMENTATION_RULES
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
                f"Registered ids: {list(rules.registered_ids())}"
            ),
        )
    if not rule.establishes_a_date:
        return ResolvedSettlementDate(
            as_of=None,
            evidence_kind=kind,
            failure=(
                f"documentation rule {rule.evidence_id!r} carries no typed "
                "settlement semantics, so it cannot produce a date for any "
                "session. It documents something; not this."
            ),
        )
    if not rule.covers(chain_session_date):
        return ResolvedSettlementDate(
            as_of=None,
            evidence_kind=kind,
            failure=(
                f"documentation rule {rule.evidence_id!r} was in force from "
                f"{rule.effective_from.isoformat()} to "
                f"{rule.effective_to.isoformat() if rule.effective_to else 'now'}, "
                f"which does not cover the chain session "
                f"{chain_session_date.isoformat()}"
            ),
        )
    assert rule.rule is not None  # `establishes_a_date` guarantees it
    try:
        derived = rule.rule.resolve(chain_session_date)
    except SettlementRuleError as error:
        return ResolvedSettlementDate(
            as_of=None,
            evidence_kind=kind,
            failure=(
                f"documentation rule {rule.evidence_id!r} could not be applied "
                f"to {chain_session_date.isoformat()}: {error}"
            ),
        )
    return ResolvedSettlementDate(
        as_of=derived,
        evidence_kind=kind,
        rule_fingerprint=rule.evidence_fingerprint,
        rule=rule.rule,
        evidence_id=rule.evidence_id,
        documentation_content_hash=rule.document_content_hash,
        derivation_version=rule.derivation_version,
    )


def _resolve_schedule(
    *,
    registry: DocumentationRuleRegistry | None,
    derivation: ScheduleDerivation | None,
    chain_session_date: date,
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
    if derivation.input_session_date != chain_session_date:
        return ResolvedSettlementDate(
            as_of=None,
            evidence_kind=kind,
            failure=(
                f"the derivation was run for session "
                f"{derivation.input_session_date.isoformat()} and this chain "
                f"was captured in {chain_session_date.isoformat()}"
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
    if not supporting.establishes_a_date:
        return ResolvedSettlementDate(
            as_of=None,
            evidence_kind=kind,
            failure=(
                f"the supporting rule {supporting.evidence_id!r} carries no "
                "typed settlement semantics, so the derivation has nothing to "
                "apply"
            ),
        )
    assert supporting.rule is not None
    try:
        derived = supporting.rule.resolve(chain_session_date)
    except SettlementRuleError as error:
        return ResolvedSettlementDate(
            as_of=None,
            evidence_kind=kind,
            failure=(
                f"the schedule rule could not be applied to "
                f"{chain_session_date.isoformat()}: {error}"
            ),
        )
    return ResolvedSettlementDate(
        as_of=derived,
        evidence_kind=kind,
        rule_fingerprint=digest_of(
            {
                "derivation": derivation.semantic_payload(),
                "supporting": supporting.semantic_payload(),
            }
        ),
        rule=supporting.rule,
        evidence_id=supporting.evidence_id,
        documentation_content_hash=supporting.document_content_hash,
        derivation_version=derivation.rule_version,
    )


def _as_date(value: object) -> date | None:
    """A settlement date read out of a payload, or nothing."""
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None
