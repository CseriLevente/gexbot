"""Turning a universe declaration into a statement about how much was covered.

v2.1.9 made a universe *resolvable*: the resolver reopened the records it named
and re-derived the identities. v2.1.10 made coverage *derived*: each source kind
got its own check, and no check could reach a coverage its evidence could not
support.

Both left the same shape of hole, one level up. v2.1.10's resolver proved things
about records; it did not ask where the records came from. Concretely:

* it read from any object with ``records()`` and ``get_payload()``, so an HTTP
  500 body, a half-written capture, or a response captured under a different
  configuration was verified evidence as long as it hashed to its own
  descriptor. Hashing to your own descriptor is a statement about storage, not
  about the response;
* the *scope* came from ``ExpectedContractUniverse.scope`` -- the caller's
  description of the request -- so a listing fetched with ``min_time=15:30:00``
  could be declared unbounded and pass the compatibility check;
* the pipeline comparison was handed the current pipeline's fingerprint as both
  the source and the target value, so it compared a string with itself.

So v2.1.11 requires a *verified source capture*: a manifest, its store, and a
``verify_capture`` result covering every record the universe names. The scope and
the pipeline fingerprint are reconstructed from the stored request facts and
compared against what the declaration claims.

What each kind can reach today:

* **dedicated contract list** -- ``FULL_REQUEST_ENUMERATED``. No ThetaData
  endpoint qualifies, so this resolves to a refusal (OD-11);
* **captured pagination metadata** -- ``FULL_REQUEST_ENUMERATED`` with every
  page present, exactly one terminal page, and an identity count matching the
  vendor's ``total_results``. No ThetaData snapshot returns page metadata, so
  this also refuses;
* **authoritative documentation** -- ``FULL_REQUEST_ENUMERATED`` from identities
  a versioned extractor read out of verified document bytes, inside the rule's
  effective period. The registry is empty;
* **observed snapshot rows** -- ``OBSERVED_SUBSET``, verifiably, today;
* **caller declared** -- ``UNKNOWN_COVERAGE``.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any

from src.adapters.errors import ThetaDataProvenanceError
from src.adapters.thetadata.endpoints import capabilities_of
from src.adapters.universe_evidence import (
    UniverseDocumentationEvidenceArtifact,
    UniverseExtractionArtifact,
    build_documentation_evidence,
    read_pagination_metadata,
)
from src.domain.completeness import ContractIdentity, contract_identity
from src.domain.digests import digest_of
from src.domain.expected_universe import (
    ExpectedContractUniverse,
    ExpectedUniverseSourceKind,
    UniverseCoverageStatus,
)
from src.domain.universe_artifact import (
    UNIVERSE_RESOLVER_SCHEMA_VERSION,
    VerifiedExpectedUniverseArtifact,
)
from src.domain.universe_scope import UniverseRequestScope

__all__ = [
    "CONTRACT_SET_PARAMETERS",
    "DEFAULT_MAX_UNIVERSE_AGE",
    "IDENTITY_COLUMNS",
    "ParameterDifference",
    "PipelineCompatibilityPolicy",
    "ResolvedExpectedUniverse",
    "UniverseOnlyCompatibilityRule",
    "VerifiedUniverseSource",
    "check_source_compatibility",
    "derive_parameter_diff",
    "derive_source_scope",
    "diff_fingerprint",
    "resolve_expected_universe",
    "verification_receipt",
]

#: Columns a contract identity is built from. All must be present, or the
#: payload is not an enumeration of contracts whatever endpoint served it.
IDENTITY_COLUMNS = ("symbol", "expiration", "strike", "right")

#: Query parameters that change **which contracts** a response contains. Every
#: one of these is reconstructed into the source scope, and no pipeline
#: difference in any of them can be waived.
#:
#: ``min_time`` is the one v2.1.10 dropped. A sweep taken with
#: ``min_time=15:30:00`` returns only contracts that traded after 15:30, which is
#: a smaller contract set than the same request without it -- and a smaller set
#: that re-derives perfectly.
CONTRACT_SET_PARAMETERS = (
    "expiration",
    "max_dte",
    "min_time",
    "right",
    "strike",
    "strike_range",
    "symbol",
)

#: How stale a universe source may be before it stops describing the chain it is
#: measured against. Two sessions: a listing from yesterday is a reasonable
#: thing to reuse, one from last month is a different market's contract set.
DEFAULT_MAX_UNIVERSE_AGE = timedelta(days=2)


class PipelineCompatibilityPolicy(str, Enum):
    """How a source pipeline difference is treated."""

    #: The source and the chain must have been captured under the same pipeline
    #: configuration. The default and the shipped policy.
    IDENTICAL_PIPELINE = "IDENTICAL_PIPELINE"
    #: A registered :class:`UniverseOnlyCompatibilityRule` states that the two
    #: configurations differ only in ways that cannot change the contract set.
    UNIVERSE_ONLY_DOCUMENTED = "UNIVERSE_ONLY_DOCUMENTED"


@dataclass(frozen=True, slots=True)
class ParameterDifference:
    """One configuration key on which two pipelines disagree."""

    key: str
    source_value: str
    target_value: str

    @property
    def affects_contract_set(self) -> bool:
        """Whether this key decides *which contracts* a request returns."""
        leaf = self.key.rsplit(".", 1)[-1]
        return leaf in CONTRACT_SET_PARAMETERS or leaf in ("root",)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source_value": self.source_value,
            "target_value": self.target_value,
            "affects_contract_set": self.affects_contract_set,
        }


def _flatten(payload: Any, prefix: str = "") -> dict[str, str]:
    """A configuration as ``dotted.key -> rendered value`` pairs.

    Rendered as text on both sides for the same reason the request spec is:
    ``4.2`` and ``"4.2"`` are the same setting, and a diff that reported an
    int/float distinction would bury the differences that matter.
    """
    import json

    if isinstance(payload, dict):
        flat: dict[str, str] = {}
        for key, value in payload.items():
            flat.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    if isinstance(payload, list | tuple):
        return {
            prefix: json.dumps(
                list(payload), sort_keys=True, separators=(",", ":"), default=str
            )
        }
    return {prefix: "" if payload is None else str(payload)}


def derive_parameter_diff(
    source_configuration: dict[str, Any], target_configuration: dict[str, Any]
) -> tuple[ParameterDifference, ...]:
    """Every key on which two pipeline configurations actually differ.

    Computed, not stated. v2.1.11 took ``differing_parameters`` from the caller
    and checked only the names it was given, so a waiver claiming "these two
    differ only in ``timeout_seconds``" was accepted on the strength of the
    claim -- including when the real difference was ``min_time``, which decides
    which contracts come back.
    """
    left = _flatten(source_configuration)
    right = _flatten(target_configuration)
    return tuple(
        ParameterDifference(
            key=key, source_value=left.get(key, ""), target_value=right.get(key, "")
        )
        for key in sorted(set(left) | set(right))
        if left.get(key) != right.get(key)
    )


def diff_fingerprint(diff: tuple[ParameterDifference, ...]) -> str:
    """The digest an operator approves. Over the derived diff, never a claim."""
    return digest_of([entry.as_dict() for entry in diff])


@dataclass(frozen=True, slots=True)
class UniverseOnlyCompatibilityRule:
    """An operator approving a **derived** difference between two pipelines.

    Enumerating a universe under one configuration and capturing a chain under
    another is legitimate -- a listing sweep may use a longer timeout without
    changing which contracts come back. Saying so has to be deliberate, and in
    v2.1.11 it was also *unchecked*: the rule took ``differing_parameters`` from
    the caller, and the caller was the one asking for the waiver. Two pipelines
    whose real difference was ``min_time`` were waived by a rule naming
    ``timeout_seconds``.

    So the difference is computed from the two configurations and this carries
    only its digest. A caller may approve what the comparison found; a caller
    cannot state what it found.
    """

    rule_id: str
    source_pipeline_fingerprint: str
    target_pipeline_fingerprint: str
    approved_diff_hash: str
    rationale: str

    def __post_init__(self) -> None:
        for name in ("rule_id", "rationale"):
            if not str(getattr(self, name)).strip():
                raise ThetaDataProvenanceError(
                    f"UniverseOnlyCompatibilityRule.{name} is empty; a waiver "
                    "nobody has to justify is not a policy"
                )
        for fingerprint in (
            self.source_pipeline_fingerprint,
            self.target_pipeline_fingerprint,
            self.approved_diff_hash,
        ):
            if len(fingerprint) != 64:
                raise ThetaDataProvenanceError(
                    "a universe-only compatibility rule must name both full "
                    "pipeline fingerprints and the full digest of the derived "
                    "difference it approves; a waiver that does not say which "
                    "two configurations it covers covers all of them"
                )

    def permits(self, *, source: str, target: str) -> bool:
        return (
            source == self.source_pipeline_fingerprint
            and target == self.target_pipeline_fingerprint
        )


@dataclass(frozen=True, slots=True)
class VerifiedUniverseSource:
    """A source capture that passed verification, and what it was captured by.

    Built by :func:`verified_universe_source`. There is no path that reads
    records out of a bare store: v2.1.10 took ``store: Any``, so anything with
    a ``records()`` method was a capture.
    """

    payloads: dict[str, str]
    descriptors: dict[str, Any]
    pipeline_fingerprint: str
    verification_fingerprint: str
    scope: UniverseRequestScope

    @property
    def observed_at(self) -> datetime:
        latest: datetime = max(
            d.response_received_at for d in self.descriptors.values()
        )
        return latest


@dataclass(frozen=True, slots=True)
class ResolvedExpectedUniverse:
    """The outcome of asking a resolver to establish a universe's coverage."""

    artifact: VerifiedExpectedUniverseArtifact | None = None
    failure: str = ""
    derived_identities: tuple[ContractIdentity, ...] = ()
    #: The extraction this rests on, for a documentation-backed universe. Held
    #: so the capture can persist it: recovery must be able to re-check the
    #: reading without the registry that was populated in another process.
    extraction: UniverseExtractionArtifact | None = None
    #: The portable form of the whole documentation resolution -- the rule with
    #: no host path, the digest of the verified bytes, and the extraction. What
    #: v2.1.11 lacked, which is why a resolution made with a caller's registry
    #: could not be re-run by ``capture_session``.
    documentation_evidence: UniverseDocumentationEvidenceArtifact | None = None
    #: The exact bytes the extractor read. Carried in memory for the capture's
    #: revalidation and persisted content-addressed for recovery.
    document_text: str | None = None

    @property
    def established(self) -> bool:
        return self.artifact is not None and not self.failure

    @property
    def coverage_status(self) -> UniverseCoverageStatus:
        if self.artifact is None:
            return UniverseCoverageStatus.UNKNOWN_COVERAGE
        return self.artifact.coverage_status

    @property
    def establishes_completeness(self) -> bool:
        return self.established and bool(
            self.artifact and self.artifact.establishes_completeness
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "established": self.established,
            "coverage_status": self.coverage_status.value,
            "establishes_completeness": self.establishes_completeness,
            "failure": self.failure,
            "derived_identity_count": len(self.derived_identities),
            "artifact": self.artifact.as_dict() if self.artifact else None,
            "extraction": self.extraction.as_dict() if self.extraction else None,
            "documentation_evidence": (
                self.documentation_evidence.as_dict()
                if self.documentation_evidence
                else None
            ),
        }


def _failed(reason: str, derived: tuple[str, ...] = ()) -> ResolvedExpectedUniverse:
    return ResolvedExpectedUniverse(failure=reason, derived_identities=derived)


def resolve_expected_universe(
    universe: ExpectedContractUniverse,
    *,
    manifest: Any = None,
    store: Any = None,
    verification: Any = None,
    operation: Any = None,
    registry: Any = None,
    session_date: date | None = None,
    extraction: UniverseExtractionArtifact | None = None,
    documentation_evidence: UniverseDocumentationEvidenceArtifact | None = None,
    document_text: str | None = None,
) -> ResolvedExpectedUniverse:
    """Establish what a declaration actually covers, or say why nothing follows.

    ``manifest``, ``store`` and ``verification`` describe the *source* capture --
    the responses the universe was read out of, and the ``verify_capture`` result
    that says the capture holds. ``operation`` is that capture's operation
    identity.

    ``session_date`` is the market session the universe will be applied to,
    needed to check a documentation rule's effective period. ``extraction`` is a
    previously stored reading of that document, supplied on the recovery path so
    the instant the extraction happened is not silently replaced by now.
    """
    kind = ExpectedUniverseSourceKind(universe.source_kind)

    if kind is ExpectedUniverseSourceKind.CALLER_DECLARED:
        # A caller really did state a list, and stating one is not observing
        # one. It resolves, so diagnostics can use it, and it carries
        # UNKNOWN_COVERAGE, so nothing measures against it.
        return _resolve_caller_declared(universe)

    if kind is ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION:
        return _resolve_documented(
            universe,
            registry=registry,
            session_date=session_date,
            extraction=extraction,
            documentation_evidence=documentation_evidence,
            document_text=document_text,
        )

    source = verified_universe_source(
        universe, manifest=manifest, store=store, verification=verification
    )
    if isinstance(source, ResolvedExpectedUniverse):
        return source

    if kind is ExpectedUniverseSourceKind.VENDOR_CONTRACT_LIST:
        return _resolve_contract_list(universe, source=source, operation=operation)
    if kind is ExpectedUniverseSourceKind.CAPTURED_PAGINATION_METADATA:
        return _resolve_paginated(universe, source=source, operation=operation)
    return _resolve_snapshot_rows(universe, source=source, operation=operation)


# =============================================================================
# The source-specific checks
# =============================================================================


def _resolve_caller_declared(
    universe: ExpectedContractUniverse,
) -> ResolvedExpectedUniverse:
    identities = tuple(sorted(universe.identity_set))
    scope = universe.scope or UniverseRequestScope(root="UNSPECIFIED")
    if universe.declared_at is None:
        return _failed(
            "a caller-declared universe must say when it was declared; an "
            "undated list cannot even be reported honestly"
        )
    return ResolvedExpectedUniverse(
        artifact=VerifiedExpectedUniverseArtifact(
            identities=universe.identity_set,
            source_kind=ExpectedUniverseSourceKind.CALLER_DECLARED,
            coverage_status=UniverseCoverageStatus.UNKNOWN_COVERAGE,
            source_operation_fingerprint="",
            source_record_ids=(),
            source_request_spec_fingerprint="",
            source_pipeline_fingerprint="",
            source_scope=scope,
            observed_at=universe.declared_at,
            evidence_fingerprint=digest_of(
                {"kind": "CALLER_DECLARED", "identities": sorted(identities)}
            ),
            declaration_hash=universe.declaration_hash,
        ),
        derived_identities=identities,
    )


def _resolve_documented(
    universe: ExpectedContractUniverse,
    *,
    registry: Any,
    session_date: date | None,
    extraction: UniverseExtractionArtifact | None,
    documentation_evidence: UniverseDocumentationEvidenceArtifact | None = None,
    document_text: str | None = None,
) -> ResolvedExpectedUniverse:
    """A universe a versioned extractor read out of a verified document.

    Three things v2.1.10 did not do. It looked the rule up in the universe
    registry (which was the v2.1.10 fix), took ``rule.identities`` -- a
    caller-supplied list sitting beside a genuine document hash -- never checked
    the rule's effective period, and dated the result from the declaration.

    v2.1.12 adds the *re-run* path. When ``documentation_evidence`` is supplied
    the rule is rebuilt from it and the bytes come from ``document_text``, so no
    registry is consulted at all -- which is what lets ``capture_session``
    re-derive a resolution a caller made with its own registry, and lets
    recovery work in a process where the global registry is empty.
    """
    from src.adapters.universe_evidence import UNIVERSE_DOCUMENTATION_RULES

    evidence_id = (universe.documentation_evidence_id or "").strip()
    if documentation_evidence is not None:
        if documentation_evidence.evidence_id != evidence_id:
            return _failed(
                f"the supplied documentation evidence is for "
                f"{documentation_evidence.evidence_id!r} and this universe names "
                f"{evidence_id!r}"
            )
        rule: Any = documentation_evidence.rule
    else:
        rules = registry if registry is not None else UNIVERSE_DOCUMENTATION_RULES
        if not hasattr(rules, "get"):
            return _failed(f"{type(rules).__name__} is not a universe rule registry")
        rule = rules.get(evidence_id)
        if rule is None:
            return _failed(
                f"{evidence_id!r} is not a registered *universe* documentation "
                "rule. A settlement-convention document is content-verified and "
                "says nothing about which contracts exist; looking one up here is "
                "how v2.1.9 let it define a universe. "
                f"Registered universe ids: {list(rules.registered_ids())}"
            )
    if not rule.established:
        return _failed(
            f"universe rule {evidence_id!r} has not been content verified; a "
            "hash nobody computed is not a hash"
        )
    if session_date is None:
        return _failed(
            f"universe rule {evidence_id!r} states an effective period and this "
            "resolution names no market session to check it against, so whether "
            "the document describes the market being captured is unknown"
        )
    period = rule.period_refusals(session_date)
    if period:
        return _failed("; ".join(period))
    if universe.scope is not None and not rule.scope.covers(universe.scope).compatible:
        reasons = rule.scope.covers(universe.scope).reasons
        return _failed(
            f"universe rule {evidence_id!r} documents a different scope than the "
            f"declaration asks about: {list(reasons)}"
        )

    try:
        text = document_text if document_text is not None else rule.document_text()
        if extraction is None:
            from datetime import UTC

            read = rule.extract(executed_at=datetime.now(UTC), document_text=text)
        else:
            mismatches = rule.confirm(extraction, document_text=text)
            if mismatches:
                return _failed("; ".join(mismatches))
            read = extraction
    except ThetaDataProvenanceError as error:
        return _failed(str(error))

    claimed = universe.identity_set
    if read.identities != claimed:
        return _failed(
            f"universe rule {evidence_id!r} extracts {len(read.identities)} "
            f"identities from {rule.document_reference} and the declaration "
            f"claims {len(claimed)}; a document that does not contain the "
            "claimed contracts has not established them",
            tuple(sorted(read.identities)),
        )

    evidence = (
        documentation_evidence
        if documentation_evidence is not None
        else build_documentation_evidence(rule, read, document_text=text)
    )
    return ResolvedExpectedUniverse(
        artifact=VerifiedExpectedUniverseArtifact(
            identities=read.identities,
            source_kind=ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION,
            coverage_status=UniverseCoverageStatus.FULL_REQUEST_ENUMERATED,
            source_operation_fingerprint="",
            source_record_ids=(),
            source_request_spec_fingerprint="",
            source_pipeline_fingerprint="",
            source_scope=rule.scope,
            # When the extractor ran over the document, not when a caller said
            # it had. v2.1.10 used ``universe.declared_at``, so the staleness of
            # a document reading was whatever the declaration claimed.
            observed_at=read.extraction_executed_at,
            evidence_fingerprint=read.fingerprint,
            declaration_hash=universe.declaration_hash,
            documentation_evidence_id=evidence_id,
            documentation_evidence_hash=evidence.artifact_hash,
        ),
        derived_identities=tuple(sorted(read.identities)),
        extraction=read,
        documentation_evidence=evidence,
        document_text=text,
    )


def _resolve_contract_list(
    universe: ExpectedContractUniverse,
    *,
    source: VerifiedUniverseSource,
    operation: Any,
) -> ResolvedExpectedUniverse:
    """A dedicated vendor listing endpoint. None exists, so this refuses.

    The named regression. v2.1.9 accepted every option snapshot here, which is
    how a quote response came to establish a complete universe.
    """
    not_listings = sorted(
        {
            descriptor.endpoint
            for descriptor in source.descriptors.values()
            if not capabilities_of(descriptor.endpoint).is_dedicated_contract_list
        }
    )
    if not_listings:
        return _failed(
            f"{not_listings} are not dedicated contract-list endpoints. Having "
            "one row per returned contract is not the same as listing every "
            "contract the request owed: a truncated response enumerates its own "
            "rows perfectly. No verified ThetaData contract-list endpoint "
            "exists (OPEN_DECISIONS OD-11), so VENDOR_CONTRACT_LIST is "
            "unsupported; use OBSERVED_SNAPSHOT_ROWS, which resolves to "
            "OBSERVED_SUBSET and says what it is."
        )

    return _artifact_from_records(  # pragma: no cover - unreachable until OD-11
        universe,
        source=source,
        operation=operation,
        coverage=UniverseCoverageStatus.FULL_REQUEST_ENUMERATED,
        extra_evidence={"listing": "dedicated_contract_list"},
    )


def _resolve_paginated(
    universe: ExpectedContractUniverse,
    *,
    source: VerifiedUniverseSource,
    operation: Any,
) -> ResolvedExpectedUniverse:
    """Every declared page, read out of the responses themselves."""
    without_metadata = sorted(
        {
            descriptor.endpoint
            for descriptor in source.descriptors.values()
            if not capabilities_of(descriptor.endpoint).carries_pagination_metadata
        }
    )
    if without_metadata:
        return _failed(
            f"{without_metadata} return no pagination metadata, so how much of "
            "the request they cover cannot be read back. v2.1.9 named this "
            "source kind and re-derived identities instead, so one ordinary "
            "quote response satisfied it. No verified ThetaData snapshot "
            "endpoint exposes page or total metadata (OPEN_DECISIONS OD-11)."
        )

    try:
        evidence = read_pagination_metadata(source.payloads)
    except ThetaDataProvenanceError as error:  # pragma: no cover - see above
        return _failed(str(error))
    if evidence is None:  # pragma: no cover - unreachable while none carry it
        return _failed(
            "the named records carry no pagination metadata, so nothing states "
            "how many pages this listing has"
        )
    if not evidence.complete:  # pragma: no cover - unreachable today
        return _failed(
            f"pages {list(evidence.missing_pages)} of {evidence.total_pages} were "
            "never captured"
            if evidence.missing_pages
            else "the listing has no terminating page, so a continuation may "
            "remain uncaptured",
        )
    count_refusals = evidence.identity_count_refusals(  # pragma: no cover
        len(universe.identity_set)
    )
    if count_refusals:  # pragma: no cover - unreachable today
        return _failed("; ".join(count_refusals))
    return _artifact_from_records(  # pragma: no cover - unreachable today
        universe,
        source=source,
        operation=operation,
        coverage=UniverseCoverageStatus.FULL_REQUEST_ENUMERATED,
        extra_evidence={"pagination": evidence.semantic_payload()},
    )


def _resolve_snapshot_rows(
    universe: ExpectedContractUniverse,
    *,
    source: VerifiedUniverseSource,
    operation: Any,
) -> ResolvedExpectedUniverse:
    """Rows out of ordinary market-data responses. Honest, and a subset.

    This is what an option snapshot can support and it is genuinely useful: the
    identities really did arrive, so a diagnostic can say what the chain held.
    It cannot say what the chain was owed, and the coverage status says so.
    """
    non_enumerating = sorted(
        {
            descriptor.endpoint
            for descriptor in source.descriptors.values()
            if not capabilities_of(descriptor.endpoint).can_supply_identities
        }
    )
    if non_enumerating:
        return _failed(
            f"{non_enumerating} do not enumerate contracts. One index print "
            "cannot say which options exist."
        )
    return _artifact_from_records(
        universe,
        source=source,
        operation=operation,
        coverage=UniverseCoverageStatus.OBSERVED_SUBSET,
        extra_evidence={},
    )


# =============================================================================
# The verified source capture
# =============================================================================


def verified_universe_source(
    universe: ExpectedContractUniverse,
    *,
    manifest: Any,
    store: Any,
    verification: Any,
) -> VerifiedUniverseSource | ResolvedExpectedUniverse:
    """Every named record, from a capture that passed verification.

    v2.1.10 opened the records and re-hashed their payloads. That proves the
    bytes have not changed since they were written, and it is silent on whether
    the response was a 200, whether the write finished, whether the parser that
    read it is one this code supports, and whether the manifest's account of the
    capture survives comparison with the store. ``verify_capture`` asks all of
    those; this requires it to have been asked and to have passed.
    """
    if store is None or manifest is None:
        return _failed(
            f"{universe.source_kind.value} was offered with no capture to read "
            "it from, so the records it names were never opened. v2.1.9 used "
            "``source_record_ids`` as a boolean and never opened one."
        )
    if verification is None:
        return _failed(
            f"{universe.source_kind.value} was offered with no capture "
            "verification. A record that exists in a store and hashes to its "
            "own descriptor can still be an HTTP 500 body or a half-written "
            "capture; resolve the universe through "
            "ThetaDataResearchPipeline.resolve_expected_universe(), which runs "
            "verify_capture() over the source before reading a byte of it."
        )
    if not getattr(verification, "verified", False):
        return _failed(
            "the source capture did not pass verification: "
            f"{list(getattr(verification, 'failures', ()))[:4]}"
        )

    named = tuple(universe.source_record_ids)
    if not named:
        return _failed(
            f"{universe.source_kind.value} names no source records, so nothing "
            "can be re-read to confirm it"
        )
    confirmed = set(getattr(verification, "confirmed_record_ids", ()))
    unconfirmed = sorted(set(named) - confirmed)
    if unconfirmed:
        return _failed(
            f"the universe names records {unconfirmed} which this capture "
            "verification did not confirm. A record outside the verified "
            "manifest was checked against nothing in particular."
        )

    known = {r.record_id: r for r in store.records()}
    unknown = sorted(set(named) - set(known))
    if unknown:
        return _failed(
            f"the universe names records {unknown} which this store does not "
            "hold. A universe read from responses nobody can produce is a list "
            "somebody typed."
        )

    payloads: dict[str, str] = {}
    descriptors: dict[str, Any] = {}
    for record_id in named:
        descriptor = known[record_id]
        refusal = _record_refusal(descriptor)
        if refusal:
            return _failed(refusal)
        try:
            payload = store.get_payload(record_id)
        except Exception as error:
            return _failed(f"record {record_id!r} could not be read: {error}")
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != (
            descriptor.payload_hash
        ):
            return _failed(
                f"record {record_id!r} does not hash to what the store "
                "recorded; the bytes have changed since capture"
            )
        payloads[record_id] = payload
        descriptors[record_id] = descriptor

    pipelines = {d.pipeline_fingerprint for d in descriptors.values()}
    if len(pipelines) != 1 or not next(iter(pipelines)):
        return _failed(
            f"the named records were captured under {sorted(pipelines)} "
            "pipeline configurations; a universe assembled under two "
            "configurations asked two questions"
        )

    scope = derive_source_scope(descriptors)
    if isinstance(scope, str):
        return _failed(scope)

    declared_scope = universe.scope
    if declared_scope is not None:
        widening = scope.covers(declared_scope)
        if not widening.compatible:
            return _failed(
                "the declaration describes a wider request than the records "
                f"were captured under: {list(widening.reasons)}. A scope a "
                "caller typed cannot enlarge the sweep that produced the bytes."
            )

    return VerifiedUniverseSource(
        payloads=payloads,
        descriptors=descriptors,
        pipeline_fingerprint=next(iter(pipelines)),
        verification_fingerprint=digest_of(
            verification_receipt(verification, manifest=manifest)
        ),
        scope=scope,
    )


def verification_receipt(verification: Any, *, manifest: Any = None) -> dict[str, Any]:
    """That this manifest was checked against that store, and the manifest.

    The *source* manifest travels with the receipt because a universe is
    resolved against records belonging to an earlier operation, so the chain's
    own manifest does not name them. Without it, recovery would have to rebuild
    the claim out of the store it is meant to be checked against -- and a
    manifest derived from a store always matches that store.

    The digest of this payload is what the artifact carries as
    ``source_verification_fingerprint``, and it is the artifact store's key for
    the receipt: the stamped digest *is* the lookup, as everywhere else here.
    """
    return {
        "schema_version": "capture-verification/2.1.11",
        "manifest_hash": getattr(verification, "manifest_hash", ""),
        "source_manifest": (
            manifest.semantic_payload() if manifest is not None else None
        ),
        "confirmed_record_ids": sorted(
            getattr(verification, "confirmed_record_ids", ())
        ),
        "plan_fingerprint": getattr(verification, "plan_fingerprint", ""),
        "expected_pipeline_fingerprint": getattr(
            verification, "expected_pipeline_fingerprint", ""
        ),
        "store_description": getattr(verification, "store_description", ""),
        "verified": bool(getattr(verification, "verified", False)),
        "waived_failures": sorted(getattr(verification, "waived_failures", ())),
        "waiver_reason": getattr(verification, "waiver_reason", ""),
    }


def _record_refusal(descriptor: Any) -> str:
    """Why one stored record is not usable as universe evidence."""
    from src.adapters.raw_store import SUPPORTED_PARSER_VERSIONS

    status = int(getattr(descriptor, "http_status", 0))
    if not 200 <= status < 300:
        return (
            f"record {descriptor.record_id!r} was captured with HTTP {status}. "
            "An error body parses into whatever rows it happens to contain, "
            "including none, and a universe read out of one is a universe read "
            "out of a failure."
        )
    if not getattr(descriptor, "capture_complete", False):
        return (
            f"record {descriptor.record_id!r} is marked incomplete: the write "
            "was interrupted before the atomic rename, so the response is "
            "truncated by this repository rather than by the vendor"
        )
    parser = getattr(descriptor, "parser_version", "")
    if parser not in SUPPORTED_PARSER_VERSIONS:
        return (
            f"record {descriptor.record_id!r} was read by parser {parser!r} and "
            f"this code supports {sorted(SUPPORTED_PARSER_VERSIONS)}; a payload "
            "interpreted under different rules is a different payload"
        )
    for name in ("operation_fingerprint", "request_spec_fingerprint"):
        if not str(getattr(descriptor, name, "")).strip():
            return (
                f"record {descriptor.record_id!r} carries no {name}, so nothing "
                "says which request produced it"
            )
    return ""


def derive_source_scope(descriptors: dict[str, Any]) -> UniverseRequestScope | str:
    """Reconstruct the request the source records answered, from the records.

    Not from ``ExpectedContractUniverse.scope``, which is what a caller says the
    request was. The two are compared afterwards, and the *derived* one is what
    the artifact carries.

    Returns the scope, or a string saying why one cannot be derived.
    """
    option_records = [
        d
        for d in descriptors.values()
        if capabilities_of(d.endpoint).can_supply_identities
    ]
    if not option_records:
        return (
            "none of the named records came from an endpoint that enumerates "
            "contracts, so no contract-set request can be reconstructed from "
            "them"
        )

    params: dict[str, set[str]] = {}
    for descriptor in option_records:
        stored = dict(getattr(descriptor, "query_params", {}) or {})
        for key in CONTRACT_SET_PARAMETERS:
            if key in stored:
                params.setdefault(key, set()).add(_render_parameter(stored[key]))
    inconsistent = sorted(key for key, values in params.items() if len(values) != 1)
    if inconsistent:
        return (
            f"the named records disagree about {inconsistent}, so they answered "
            "more than one contract-set request and no single scope describes "
            "them"
        )
    single = {key: next(iter(values)) for key, values in params.items()}

    symbol = single.get("symbol", "")
    if not symbol:
        return (
            "the named records record no symbol parameter, so which underlying "
            "they enumerated cannot be read back"
        )

    expiration = single.get("expiration", "*")
    expirations: tuple[date, ...] | None = None
    if expiration not in ("*", ""):
        try:
            expirations = (date.fromisoformat(expiration),)
        except ValueError:
            return (
                f"the records were captured with expiration={expiration!r}, "
                "which is neither a date nor the wildcard, so which expirations "
                "they cover cannot be established"
            )

    right = single.get("right")
    rights = ("call", "put") if right is None else (_canonical_right(right),)

    filters = tuple(
        (key, value)
        for key, value in sorted(single.items())
        if key not in ("symbol", "expiration", "max_dte", "strike_range", "right")
    )

    requested = [
        d.requested_as_of or d.request_started_at
        for d in option_records
        if (d.requested_as_of or d.request_started_at) is not None
    ]
    try:
        return UniverseRequestScope(
            root=symbol,
            expirations=expirations,
            max_dte=_as_int(single.get("max_dte")),
            strike_range=_as_int(single.get("strike_range")),
            rights=rights,
            request_filters=filters,
            requested_at=min(requested) if requested else None,
        )
    except ValueError as error:
        return f"the records do not reconstruct into a request scope: {error}"


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _render_parameter(value: Any) -> str:
    """Render a stored query value the way the request spec renders it."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# =============================================================================
# Shared machinery
# =============================================================================


def _artifact_from_records(
    universe: ExpectedContractUniverse,
    *,
    source: VerifiedUniverseSource,
    operation: Any,
    coverage: UniverseCoverageStatus,
    extra_evidence: dict[str, Any],
) -> ResolvedExpectedUniverse:
    """Re-derive identities, compare, and build the artifact."""
    derived: set[ContractIdentity] = set()
    for record_id, payload in source.payloads.items():
        try:
            derived |= _identities_in(payload, record_id=record_id)
        except ThetaDataProvenanceError as error:
            return _failed(str(error))

    claimed = universe.identity_set
    if derived != claimed:
        missing = sorted(claimed - derived)
        extra = sorted(derived - claimed)
        return _failed(
            f"the universe claims {len(claimed)} identities and the named "
            f"records yield {len(derived)}: "
            f"{len(missing)} claimed but not present in the records "
            f"({missing[:5]}), {len(extra)} present but not claimed "
            f"({extra[:5]}). A universe that its own source does not produce is "
            "an assertion about which contracts should exist.",
            tuple(sorted(derived)),
        )

    stamps = {d.operation_fingerprint for d in source.descriptors.values()}
    if len(stamps) != 1:
        return _failed(
            f"the named records come from {len(stamps)} different capture "
            "operations; a universe assembled from several sweeps is not one "
            "listing"
        )
    specs = {d.request_spec_fingerprint for d in source.descriptors.values()}
    if len(specs) != 1:
        return _failed(
            f"the named records were captured under {len(specs)} different "
            "request specifications, so they did not ask one question"
        )

    return ResolvedExpectedUniverse(
        artifact=VerifiedExpectedUniverseArtifact(
            identities=derived,
            source_kind=universe.source_kind,
            coverage_status=coverage,
            source_operation_fingerprint=next(iter(stamps)),
            source_record_ids=tuple(sorted(source.payloads)),
            source_request_spec_fingerprint=next(iter(specs)),
            source_pipeline_fingerprint=source.pipeline_fingerprint,
            # Derived from the records, not from the caller. v2.1.10 carried the
            # declaration's scope, so a sweep taken with ``min_time=15:30:00``
            # could present itself as unbounded.
            source_scope=source.scope,
            observed_at=source.observed_at,
            evidence_fingerprint=digest_of(
                {
                    "resolver_version": UNIVERSE_RESOLVER_SCHEMA_VERSION,
                    "kind": universe.source_kind.value,
                    "coverage": coverage.value,
                    "records": [
                        {
                            "record_id": source.descriptors[r].record_id,
                            "endpoint": source.descriptors[r].endpoint,
                            "payload_hash": source.descriptors[r].payload_hash,
                        }
                        for r in sorted(source.payloads)
                    ],
                    "identities": sorted(derived),
                    "scope": source.scope.semantic_payload(),
                    "verification": source.verification_fingerprint,
                    **extra_evidence,
                }
            ),
            declaration_hash=universe.declaration_hash,
            source_verification_fingerprint=source.verification_fingerprint,
        ),
        derived_identities=tuple(sorted(derived)),
    )


def check_source_compatibility(
    artifact: VerifiedExpectedUniverseArtifact,
    *,
    chain_scope: UniverseRequestScope,
    chain_requested_at: Any,
    chain_pipeline_fingerprint: str = "",
    max_age: timedelta = DEFAULT_MAX_UNIVERSE_AGE,
    policy: PipelineCompatibilityPolicy = (
        PipelineCompatibilityPolicy.IDENTICAL_PIPELINE
    ),
    waiver: UniverseOnlyCompatibilityRule | None = None,
    source_configuration: dict[str, Any] | None = None,
    target_configuration: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Every reason this universe cannot serve this chain.

    v2.1.9 accepted a universe because its identities re-derived. That check is
    necessary and says nothing about whether the listing was *about this chain*:
    a narrower or older sweep re-derives just as cleanly, and on a narrower
    scope a perfect re-derivation is exactly what a false ``MEASURED_COMPLETE``
    looks like.

    The pipeline comparison is against ``artifact.source_pipeline_fingerprint``,
    which is read off the source records. v2.1.10 was handed the current
    pipeline's fingerprint for both sides and compared it with itself.
    """
    reasons: list[str] = []

    compatibility = artifact.source_scope.covers(chain_scope)
    reasons.extend(compatibility.reasons)

    age = artifact.source_scope.age_against(chain_requested_at)
    if artifact.observed_at > chain_requested_at:
        reasons.append(
            f"the universe was observed at {artifact.observed_at.isoformat()}, "
            f"after the chain request at {chain_requested_at.isoformat()}; a "
            "listing cannot describe a chain captured before it"
        )
    elif age is None:
        reasons.append(
            "the universe records no request instant, so its staleness relative "
            "to this chain cannot be measured"
        )
    elif age > max_age:
        reasons.append(
            f"the universe was collected {age} before this chain, beyond the "
            f"{max_age} tolerance; contract sets change between sessions"
        )

    reasons.extend(
        _pipeline_reasons(
            artifact,
            chain_pipeline_fingerprint=chain_pipeline_fingerprint,
            policy=policy,
            waiver=waiver,
            source_configuration=source_configuration,
            target_configuration=target_configuration,
        )
    )

    if artifact.resolver_version != UNIVERSE_RESOLVER_SCHEMA_VERSION:
        reasons.append(
            f"the universe was resolved by {artifact.resolver_version!r} and "
            f"this repository reads {UNIVERSE_RESOLVER_SCHEMA_VERSION!r}; a "
            "coverage state established under other rules is not comparable"
        )

    return tuple(reasons)


def _pipeline_reasons(
    artifact: VerifiedExpectedUniverseArtifact,
    *,
    chain_pipeline_fingerprint: str,
    policy: PipelineCompatibilityPolicy,
    waiver: UniverseOnlyCompatibilityRule | None,
    source_configuration: dict[str, Any] | None = None,
    target_configuration: dict[str, Any] | None = None,
) -> list[str]:
    """Whether the source's pipeline may stand in for the chain's."""
    source = artifact.source_pipeline_fingerprint
    if not artifact.source_kind.needs_records:
        # A document or a caller's list was not captured under any pipeline, so
        # there is nothing to compare. The universe is refused elsewhere if it
        # is unsupported for other reasons.
        return []
    if not chain_pipeline_fingerprint:
        return [
            "this comparison was not told which pipeline is capturing the "
            "chain, so the source configuration was checked against nothing"
        ]
    if not source:
        return [
            "the universe does not record which pipeline captured its source, "
            "so a configuration that narrowed the contract set cannot be ruled "
            "out"
        ]
    if source == chain_pipeline_fingerprint:
        return []
    if policy is PipelineCompatibilityPolicy.IDENTICAL_PIPELINE:
        return [
            f"the universe was captured under pipeline {source[:12]}... and the "
            f"chain under {chain_pipeline_fingerprint[:12]}...; under "
            "IDENTICAL_PIPELINE the two must match, because a configuration "
            "difference such as min_time changes which contracts come back. A "
            "difference that genuinely cannot is waived with a "
            "UniverseOnlyCompatibilityRule."
        ]
    if waiver is None:
        return [
            "UNIVERSE_ONLY_DOCUMENTED was selected and no "
            "UniverseOnlyCompatibilityRule was supplied, so nothing states why "
            "these two configurations ask the same contract question"
        ]
    if not waiver.permits(source=source, target=chain_pipeline_fingerprint):
        return [
            f"compatibility rule {waiver.rule_id!r} covers "
            f"{waiver.source_pipeline_fingerprint[:12]}... against "
            f"{waiver.target_pipeline_fingerprint[:12]}..., not this pair"
        ]
    if source_configuration is None or target_configuration is None:
        return [
            "a universe-only waiver was supplied and the two pipeline "
            "configurations were not, so what they actually differ in was never "
            "computed. v2.1.11 took the difference from the caller asking for "
            "the waiver."
        ]
    diff = derive_parameter_diff(source_configuration, target_configuration)
    blocking = [entry for entry in diff if entry.affects_contract_set]
    if blocking:
        return [
            "the two configurations differ in "
            f"{[entry.key for entry in blocking]}, which decide which contracts "
            "a request returns, so the difference is not universe-only whatever "
            f"rule {waiver.rule_id!r} says"
        ]
    derived = diff_fingerprint(diff)
    if derived != waiver.approved_diff_hash:
        return [
            f"compatibility rule {waiver.rule_id!r} approves difference "
            f"{waiver.approved_diff_hash[:12]}... and these two configurations "
            f"differ by {derived[:12]}... in {[entry.key for entry in diff]}"
        ]
    return []


def _identities_in(payload: str, *, record_id: str) -> set[ContractIdentity]:
    """Every canonical contract identity a stored payload enumerates."""
    rows = list(csv.DictReader(io.StringIO(payload)))
    if not rows:
        raise ThetaDataProvenanceError(
            f"record {record_id!r} has no rows, so it enumerates no contracts. "
            "An empty listing does not mean an empty universe; it means the "
            "listing failed."
        )
    missing = [column for column in IDENTITY_COLUMNS if column not in rows[0]]
    if missing:
        raise ThetaDataProvenanceError(
            f"record {record_id!r} is missing {missing}, so a contract identity "
            f"cannot be built from it; its columns are {sorted(rows[0])}"
        )
    identities: set[ContractIdentity] = set()
    for index, row in enumerate(rows):
        try:
            identities.add(
                contract_identity(
                    symbol=str(row["symbol"]),
                    expiry=str(row["expiration"]),
                    strike=row["strike"],
                    right=_canonical_right(str(row["right"])),
                )
            )
        except ValueError as error:
            raise ThetaDataProvenanceError(
                f"record {record_id!r} row {index} does not parse into a "
                f"contract identity: {error}"
            ) from error
    return identities


def _canonical_right(value: str) -> str:
    """The one spelling of a right, matching ``OptionContract.canonical_id``.

    ``C`` and ``call`` are the same contract, and a resolver that spelled them
    differently from the chain parser would report one missing identity and one
    unexpected identity for the same instrument -- a completeness shortfall that
    does not exist. Unrecognised values pass through so the identity builder
    raises with the offending text rather than this silently choosing.
    """
    from src.domain.contracts import OptionRight

    text = value.strip().lower()
    if text in ("c", "call"):
        return OptionRight.CALL.value
    if text in ("p", "put"):
        return OptionRight.PUT.value
    return text
