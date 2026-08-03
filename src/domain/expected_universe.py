"""Which contracts should have arrived, and how much of the request was listed.

v2.1.9 consolidated this into one type and made a universe *resolvable*: the
resolver reopens the records it names and re-derives the identities. That closed
"a caller typed a list and labelled it a vendor listing".

It left the harder half open. Proving that a set of identities occurs in stored
records is not proving that those records enumerate the **complete universe the
request should have returned** — and a truncated response enumerates its own
rows perfectly. Two things followed:

* any endpoint with one row per contract was accepted as ``VENDOR_CONTRACT_LIST``,
  so a quote snapshot established ``MEASURED_COMPLETE`` for the whole chain;
* ``complete_for_request: bool`` was a constructor argument. A caller passing
  ``True`` was the entire evidence for full coverage, and the Boolean went into
  the universe hash, which made an assertion look like a verification.

So coverage is now a *resolver output*. A caller may still say what it believes
— that is worth recording — but the belief is labelled ``declared_`` and nothing
downstream reads it. Only
``src.adapters.universe_resolvers`` produces a
:class:`~src.adapters.universe_artifact.VerifiedExpectedUniverseArtifact`, and
only that artifact can make completeness independent.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from src.domain.completeness import ContractIdentity
from src.domain.digests import digest_of, short_id
from src.domain.universe_scope import UniverseRequestScope

__all__ = [
    "EXPECTED_UNIVERSE_SCHEMA_VERSION",
    "ExpectedContractUniverse",
    "ExpectedUniverseSourceKind",
    "UniverseCoverageStatus",
]

#: Bumped when the *meaning* of a universe hash changes.
EXPECTED_UNIVERSE_SCHEMA_VERSION = "expected-universe/2.1.10"


class UniverseCoverageStatus(str, Enum):
    """How much of the requested universe a source actually enumerated.

    The distinction v2.1.9 lacked. It had ``complete_for_request: bool``, which
    conflates two very different failures -- "this is one page of several" and
    "this is whatever the vendor happened to send" -- and, being an argument,
    was answered by whoever was asking.
    """

    #: The source is contractually the complete set for the request: a dedicated
    #: listing endpoint, or every page of a paginated sweep with verified
    #: metadata. The only status that can support ``MEASURED_COMPLETE``.
    FULL_REQUEST_ENUMERATED = "FULL_REQUEST_ENUMERATED"

    #: A verified page of a larger listing. It can prove that something it
    #: listed is missing; it cannot prove nothing is.
    PARTIAL_PAGE = "PARTIAL_PAGE"

    #: Rows read out of an ordinary market-data response. Every identity in it
    #: really arrived, and the response never claimed to be exhaustive. Useful
    #: for diagnostics and for nothing else.
    OBSERVED_SUBSET = "OBSERVED_SUBSET"

    #: Nothing established how much was covered -- an unresolved declaration, or
    #: a caller's list.
    UNKNOWN_COVERAGE = "UNKNOWN_COVERAGE"

    @property
    def establishes_completeness(self) -> bool:
        """Whether a ``MEASURED_COMPLETE`` verdict may rest on this."""
        return self is UniverseCoverageStatus.FULL_REQUEST_ENUMERATED

    @property
    def detects_missing_identities(self) -> bool:
        """Whether an absent listed contract is a finding rather than a shrug."""
        return self in (
            UniverseCoverageStatus.FULL_REQUEST_ENUMERATED,
            UniverseCoverageStatus.PARTIAL_PAGE,
        )

    @property
    def is_verified(self) -> bool:
        """Whether a resolver established this against stored evidence."""
        return self is not UniverseCoverageStatus.UNKNOWN_COVERAGE


class ExpectedUniverseSourceKind(str, Enum):
    """Where the statement of which contracts to expect came from.

    A kind rather than a free string, for the same reason ``EvidenceKind``
    became resolvers: a label is something a caller writes, and the difference
    between "the vendor listed these contracts" and "somebody typed these
    contracts" is the whole of whether completeness was measured.
    """

    #: A dedicated vendor listing endpoint enumerated the contracts.
    #: **Unsupported in production**: no ThetaData endpoint this repository has
    #: verified is a contract list, and a market-data snapshot is not one
    #: because it has rows. See OPEN_DECISIONS OD-11.
    VENDOR_CONTRACT_LIST = "VENDOR_CONTRACT_LIST"

    #: The captured responses carried page and total metadata a resolver can
    #: read back. Also unsupported today, for the same reason: no verified
    #: ThetaData response exposes it.
    CAPTURED_PAGINATION_METADATA = "CAPTURED_PAGINATION_METADATA"

    #: A registered, content-verified document states the universe, through
    #: universe-specific typed semantics.
    AUTHORITATIVE_DOCUMENTATION = "AUTHORITATIVE_DOCUMENTATION"

    #: Rows read out of ordinary market-data responses. Resolvable, honest, and
    #: never more than ``OBSERVED_SUBSET``.
    OBSERVED_SNAPSHOT_ROWS = "OBSERVED_SNAPSHOT_ROWS"

    #: Somebody supplied a list. Legitimate, common, and not evidence about the
    #: vendor: it permits raw capture and diagnostics, and it cannot establish
    #: measured completeness or analytical readiness.
    CALLER_DECLARED = "CALLER_DECLARED"

    @property
    def is_independent_evidence(self) -> bool:
        """Whether something outside this process stated the universe."""
        return self is not ExpectedUniverseSourceKind.CALLER_DECLARED

    @property
    def needs_records(self) -> bool:
        """Whether verification means reopening captured responses."""
        return self in (
            ExpectedUniverseSourceKind.VENDOR_CONTRACT_LIST,
            ExpectedUniverseSourceKind.CAPTURED_PAGINATION_METADATA,
            ExpectedUniverseSourceKind.OBSERVED_SNAPSHOT_ROWS,
        )

    @property
    def best_possible_coverage(self) -> UniverseCoverageStatus:
        """The most a source of this kind could ever establish.

        A ceiling, not a grant: the resolver still has to do the work. What it
        expresses is that no amount of checking turns a market-data snapshot
        into a complete listing, because the response never claimed to be one.
        """
        if self is ExpectedUniverseSourceKind.OBSERVED_SNAPSHOT_ROWS:
            return UniverseCoverageStatus.OBSERVED_SUBSET
        if self is ExpectedUniverseSourceKind.CALLER_DECLARED:
            return UniverseCoverageStatus.UNKNOWN_COVERAGE
        return UniverseCoverageStatus.FULL_REQUEST_ENUMERATED


@dataclass(frozen=True, slots=True)
class ExpectedContractUniverse:
    """A **declaration** of which contracts a chain should hold.

    Deliberately not evidence. It is what a caller believes and where it says
    that belief came from, and it becomes evidence only by passing through
    ``resolve_expected_universe``, which returns a
    ``VerifiedExpectedUniverseArtifact``.

    ``declared_coverage`` records what the caller *expected* the source to
    cover, for the audit trail. Nothing reads it to decide anything: v2.1.9's
    ``complete_for_request`` was the same value under a name that did not say it
    was a claim, and it went into the hash, which made an assertion look
    verified.
    """

    identities: Iterable[ContractIdentity]
    source_kind: ExpectedUniverseSourceKind
    #: Where the source records live. Required for any kind that is read back.
    source_record_ids: tuple[str, ...] = ()
    #: What the request asked for. Required to compare one capture's listing
    #: against another capture's chain.
    scope: UniverseRequestScope | None = None
    documentation_evidence_id: str | None = None
    #: The caller's expectation, for the record. Never consulted by the engine,
    #: the confidence model or the completeness measure.
    declared_coverage: UniverseCoverageStatus = UniverseCoverageStatus.UNKNOWN_COVERAGE
    #: When the *caller* built this. The verified artifact derives its own
    #: ``observed_at`` from the source records instead.
    declared_at: datetime | None = None
    schema_version: str = EXPECTED_UNIVERSE_SCHEMA_VERSION
    _hash: str = field(default="", compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "identities", frozenset(self.identities))
        kind = ExpectedUniverseSourceKind(self.source_kind)
        object.__setattr__(self, "source_kind", kind)
        object.__setattr__(
            self, "declared_coverage", UniverseCoverageStatus(self.declared_coverage)
        )
        if self.declared_at is not None and (
            self.declared_at.tzinfo is None or self.declared_at.utcoffset() is None
        ):
            raise ValueError(
                "ExpectedContractUniverse.declared_at must be timezone-aware; a "
                "naive instant silently means whatever the reading machine's "
                "zone is"
            )
        if kind.needs_records and not self.source_record_ids:
            raise ValueError(
                f"{kind.value} names no records. A universe said to come from "
                "captured responses, that cannot say which responses, is a list "
                "somebody typed -- and v2 inferred the expected universe from "
                "the response being judged, so a truncated response was complete."
            )
        if (
            kind is ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION
            and not (self.documentation_evidence_id or "").strip()
        ):
            raise ValueError(
                "AUTHORITATIVE_DOCUMENTATION needs a registered evidence id; a "
                "documented universe nobody can look up is an undocumented one"
            )
        object.__setattr__(self, "_hash", digest_of(self.semantic_payload()))

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identities": sorted(self.identity_set),
            "source_kind": self.source_kind.value,
            "source_record_ids": sorted(self.source_record_ids),
            "scope": self.scope.semantic_payload() if self.scope else None,
            "documentation_evidence_id": self.documentation_evidence_id,
            "declared_coverage": self.declared_coverage.value,
            "declared_at": (self.declared_at.isoformat() if self.declared_at else None),
        }

    @property
    def declaration_hash(self) -> str:
        """Full SHA-256 over the declaration. Names a claim, not a finding."""
        return self._hash

    @property
    def display_id(self) -> str:
        return f"{self.source_kind.value}@{short_id(self.declaration_hash)}"

    @property
    def identity_set(self) -> frozenset[ContractIdentity]:
        """The identities, as ``__post_init__`` normalised them."""
        return frozenset(self.identities)

    @property
    def source(self) -> str:
        """The kind, spelled for a report. Never used to decide anything."""
        return self.source_kind.value

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "identity_count": len(self.identity_set),
            "declaration_hash": self.declaration_hash,
            # Stated plainly so no reader of a serialised declaration mistakes
            # it for a verified artifact.
            "verified": False,
        }
