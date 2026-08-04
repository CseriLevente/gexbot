"""The only object that may make a chain's completeness independently measured.

A declaration says what somebody believes. This says what a resolver
established, against which bytes, under which request, and how much of that
request the source actually enumerated.

The separation matters because v2.1.9 had one type doing both jobs. A
``ExpectedContractUniverse`` carrying ``complete_for_request=True`` was
indistinguishable, downstream, from one a resolver had verified -- the field was
a constructor argument, it entered the universe hash, and a hash over an
assertion looks exactly like a hash over a finding.

So verification produces a *different type*. The coverage status is set by the
source-specific resolver and refused here when the source kind could not support
it.

That is a constraint on what an artifact may *say*, and v2.1.10 mistook it for a
constraint on who may *make* one. This is a public frozen dataclass: a caller can
construct one directly, name a documentation evidence id that was never
registered, invent an evidence fingerprint, and hand it to ``capture_session``,
which checked only ``isinstance``. So v2.1.11 removes the parameter that took
one. A capture is opened against a :class:`UniverseResolution` -- a receipt that
carries the declaration and the verified source capture it was established from
-- and the pipeline re-runs the resolution before the chain operation opens. An
artifact remains a serialisable *report*; constructing one authorizes nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.domain.completeness import ContractIdentity
from src.domain.digests import digest_of, short_id
from src.domain.expected_universe import (
    ExpectedUniverseSourceKind,
    UniverseCoverageStatus,
)
from src.domain.universe_scope import UniverseRequestScope

__all__ = [
    "UNIVERSE_RESOLVER_SCHEMA_VERSION",
    "UniverseArtifactError",
    "VerifiedExpectedUniverseArtifact",
    "first_semantic_difference",
]

#: Bumped when the *meaning* of a verified universe changes -- a new coverage
#: state, a different derivation. An artifact resolved under older rules is
#: refused rather than compared field by field against newer ones.
UNIVERSE_RESOLVER_SCHEMA_VERSION = "universe-resolver/2.1.11"


class UniverseArtifactError(ValueError):
    """An artifact claiming more than its evidence could establish."""


@dataclass(frozen=True, slots=True)
class VerifiedExpectedUniverseArtifact:
    """What a resolver established about a contract universe.

    Every field is something the resolver read or computed. There is no
    parameter through which a caller states a conclusion: ``coverage_status`` in
    particular is produced by the source-specific resolver, and
    :meth:`__post_init__` refuses a status the source kind could not support --
    so an artifact claiming ``FULL_REQUEST_ENUMERATED`` from snapshot rows
    cannot be constructed at all, let alone believed.
    """

    identities: Iterable[ContractIdentity]
    source_kind: ExpectedUniverseSourceKind
    coverage_status: UniverseCoverageStatus
    #: Which operation captured the *source*. Not the chain's operation: a
    #: listing is captured before the chain it describes.
    source_operation_fingerprint: str
    source_record_ids: tuple[str, ...]
    source_request_spec_fingerprint: str
    #: The pipeline configuration the *source* was captured under, read off the
    #: verified source records. v2.1.10 had no such field, so the compatibility
    #: check was handed the current pipeline's fingerprint as both the source and
    #: the target value and compared it with itself.
    source_pipeline_fingerprint: str
    #: Reconstructed from the stored endpoint and query parameters of the source
    #: records, not copied from the declaration. A caller could otherwise state a
    #: wider scope than the request that produced the bytes, and a scope check
    #: against a caller's description of the request is a check against the
    #: caller.
    source_scope: UniverseRequestScope
    #: Derived from the source records, never supplied. v2.1.9 took
    #: ``observed_at`` from the caller, so a listing captured three weeks ago
    #: could present itself as observed this morning.
    observed_at: datetime
    #: Digest of the derivation: which bytes, which pages, which document.
    evidence_fingerprint: str
    resolver_version: str = UNIVERSE_RESOLVER_SCHEMA_VERSION
    #: The declaration this was resolved from, for the audit trail. Deliberately
    #: **outside** :meth:`semantic_payload`, so it is not part of the artifact's
    #: identity: two callers who declare the same universe from the same records
    #: at different instants established the same thing, and ``declared_at`` is
    #: a caller statement nothing reads. Hashing one into the evidence would be
    #: the pattern this release exists to remove, and it would make recovery
    #: impossible without persisting the caller's wording as well.
    declaration_hash: str = ""
    documentation_evidence_id: str | None = None
    #: Digest of the ``CaptureVerification`` receipt that established the source
    #: records. Empty for a source with no records. Since v2.1.11 a record-backed
    #: artifact cannot be built without one: existing in a store and hashing to
    #: its own descriptor is not the same as having come from a capture that
    #: passed verification.
    source_verification_fingerprint: str = ""
    _hash: str = field(default="", compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "identities", frozenset(self.identities))
        kind = ExpectedUniverseSourceKind(self.source_kind)
        object.__setattr__(self, "source_kind", kind)
        coverage = UniverseCoverageStatus(self.coverage_status)
        object.__setattr__(self, "coverage_status", coverage)

        if coverage.establishes_completeness and not (
            kind.best_possible_coverage.establishes_completeness
        ):
            raise UniverseArtifactError(
                f"a {kind.value} source cannot establish "
                f"{coverage.value}: the most it could ever support is "
                f"{kind.best_possible_coverage.value}. A market-data response "
                "enumerates the contracts the vendor sent, and a truncated "
                "response enumerates its own rows perfectly."
            )
        if not str(self.evidence_fingerprint).strip():
            raise UniverseArtifactError(
                "VerifiedExpectedUniverseArtifact.evidence_fingerprint is "
                "empty; an artifact that cannot say what established it is a "
                "declaration wearing a verified type's name"
            )
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise UniverseArtifactError(
                "VerifiedExpectedUniverseArtifact.observed_at must be "
                "timezone-aware; staleness measured against a naive instant is "
                "measured against whichever zone happens to be reading it"
            )
        if kind.needs_records and not self.source_record_ids:
            raise UniverseArtifactError(
                f"a verified {kind.value} artifact names no source records, so "
                "nothing can be re-read to confirm it"
            )
        if kind.needs_records and not self.source_verification_fingerprint:
            raise UniverseArtifactError(
                f"a verified {kind.value} artifact names no capture verification. "
                "Records that exist and hash to their own descriptors can still "
                "be an HTTP 500 body, a half-written capture or a response from "
                "a pipeline that asked a different question; only "
                "verify_capture() rules those out."
            )
        if kind.needs_records and not self.source_pipeline_fingerprint:
            raise UniverseArtifactError(
                f"a verified {kind.value} artifact does not say which pipeline "
                "captured its source, so it cannot be compared against the "
                "pipeline capturing the chain"
            )
        object.__setattr__(self, "_hash", digest_of(self.semantic_payload()))

    # -- what it permits -----------------------------------------------------

    @property
    def establishes_completeness(self) -> bool:
        """Whether ``MEASURED_COMPLETE`` may rest on this."""
        return self.coverage_status.establishes_completeness

    @property
    def independently_observed(self) -> bool:
        """Whether something outside the chain being judged stated this.

        Both halves: the source has to be independent evidence *and* the
        coverage has to have been established. A caller-declared list is never
        independent however many records it names, and an unresolved
        declaration never reaches this type at all.
        """
        return (
            self.source_kind.is_independent_evidence
            and self.coverage_status.is_verified
        )

    @property
    def identity_set(self) -> frozenset[ContractIdentity]:
        return frozenset(self.identities)

    @property
    def source(self) -> str:
        """For a report. Never used to decide anything."""
        return self.source_kind.value

    # -- identity ------------------------------------------------------------

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "resolver_version": self.resolver_version,
            "identities": sorted(self.identity_set),
            "source_kind": self.source_kind.value,
            "coverage_status": self.coverage_status.value,
            "source_operation_fingerprint": self.source_operation_fingerprint,
            "source_record_ids": sorted(self.source_record_ids),
            "source_request_spec_fingerprint": self.source_request_spec_fingerprint,
            "source_pipeline_fingerprint": self.source_pipeline_fingerprint,
            "source_scope": self.source_scope.semantic_payload(),
            "observed_at": self.observed_at.isoformat(),
            "evidence_fingerprint": self.evidence_fingerprint,
            "documentation_evidence_id": self.documentation_evidence_id,
            "source_verification_fingerprint": self.source_verification_fingerprint,
        }

    @property
    def artifact_hash(self) -> str:
        """Full SHA-256. What a chain operation is stamped with."""
        return self._hash

    @property
    def display_id(self) -> str:
        return f"{self.source_kind.value}@{short_id(self.artifact_hash)}"

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "declaration_hash": self.declaration_hash,
            "identity_count": len(self.identity_set),
            "artifact_hash": self.artifact_hash,
            "independently_observed": self.independently_observed,
            "establishes_completeness": self.establishes_completeness,
            "verified": True,
        }


def first_semantic_difference(
    stored: VerifiedExpectedUniverseArtifact,
    rederived: VerifiedExpectedUniverseArtifact,
) -> str:
    """The first field of the artifact that moved, or ``""`` if none did.

    Recovery compares ``artifact_hash``, which is the complete check: every
    semantic field is under it, so nothing can be edited without the digest
    moving. A digest mismatch on its own tells an operator that *something*
    changed and nothing about what, which is not a usable refusal -- so this
    walks the same canonical payload the hash is taken over and names the first
    disagreement.

    v2.1.10 compared only the identity set and the coverage status, which left
    ``observed_at``, the source scope, the source fingerprints and the
    documentation evidence id free to be replaced. A stale listing edited to look
    current recovered cleanly.
    """
    left, right = stored.semantic_payload(), rederived.semantic_payload()
    for key in sorted(set(left) | set(right)):
        mine, theirs = left.get(key), right.get(key)
        if mine == theirs:
            continue
        if key == "identities":
            missing = sorted(set(mine or ()) - set(theirs or ()))
            extra = sorted(set(theirs or ()) - set(mine or ()))
            return (
                f"identities differ: {len(missing)} stored but not re-derived "
                f"({missing[:3]}), {len(extra)} re-derived but not stored "
                f"({extra[:3]})"
            )
        return f"{key} is {mine!r} in the stored artifact and {theirs!r} re-derived"
    return ""
