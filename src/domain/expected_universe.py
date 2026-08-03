"""Which contracts should have arrived, stated by something other than the chain.

An expected universe changes three things: completeness, the confidence score,
and whether a dataset is fit to build on. v2.1.8 bound it to the capture
operation, which stopped a replay measuring the same bytes against a different
universe. Two problems survived that.

**There were two of these types.** ``src.domain.completeness`` had one, and this
module had another. The engine took the first, the capture path took the second,
and a value satisfying one was not the same object as a value satisfying the
other -- so the type that carried provenance was not the type the completeness
measure actually read.

**Neither was evidence.** ``source="vendor_contract_list"`` is a string. Nothing
opened a record, nothing checked the identities against anything, and a caller
typing a plausible source name got the same ``MEASURED_COMPLETE`` a re-read
vendor listing would have got. ``source_record_ids`` existed and was used only
as a boolean: non-empty meant "independently observed".

Here there is one type, its source is a *kind* rather than a label, and
``src.adapters.universe_resolvers`` is what turns one into a verified artifact
by re-deriving the identities from the records it names.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from src.domain.completeness import ContractIdentity
from src.domain.digests import digest_of, short_id

__all__ = [
    "EXPECTED_UNIVERSE_SCHEMA_VERSION",
    "ExpectedContractUniverse",
    "ExpectedUniverseSourceKind",
]

#: Bumped when the *meaning* of a universe hash changes.
EXPECTED_UNIVERSE_SCHEMA_VERSION = "expected-universe/2.1.9"


class ExpectedUniverseSourceKind(str, Enum):
    """Where the statement of which contracts to expect came from.

    A kind rather than a free string, for the same reason ``EvidenceKind``
    became resolvers: a label is something a caller writes, and the difference
    between "the vendor listed these contracts" and "somebody typed these
    contracts" is the whole of whether completeness was measured.
    """

    #: A vendor listing endpoint enumerated the contracts. Re-derivable: the
    #: resolver reopens the named records and parses the identities out again.
    VENDOR_CONTRACT_LIST = "VENDOR_CONTRACT_LIST"

    #: The captured responses carried pagination metadata stating the total.
    #: Re-derivable the same way, from the records that carried it.
    CAPTURED_PAGINATION_METADATA = "CAPTURED_PAGINATION_METADATA"

    #: A registered, content-verified document states the universe -- a listed
    #: strike ladder, say. Rests on the documentation registry.
    AUTHORITATIVE_DOCUMENTATION = "AUTHORITATIVE_DOCUMENTATION"

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
        )


@dataclass(frozen=True, slots=True)
class ExpectedContractUniverse:
    """The one authoritative statement of which contracts a chain should hold.

    ``source_record_ids`` is what makes a vendor-sourced universe checkable: a
    universe read out of stored bytes can be read again, and
    ``src.adapters.universe_resolvers.resolve_expected_universe`` does exactly
    that before anything is allowed to call a chain complete.

    ``complete_for_request`` records whether the source enumerated the whole
    requested universe or one page of it. A partial list still detects missing
    identities; it cannot establish completeness, and since v2.1.9 the status
    says which of the two happened rather than collapsing both into
    ``MEASURED_COMPLETE``.
    """

    #: Annotated as an iterable and normalised in ``__post_init__``: passing a
    #: set or a generator at a call site is natural, and the type this ends up
    #: holding is always a frozenset.
    identities: Iterable[ContractIdentity]
    source_kind: ExpectedUniverseSourceKind
    observed_at: datetime
    source_record_ids: tuple[str, ...] = ()
    complete_for_request: bool = True
    documentation_evidence_id: str | None = None
    #: Digest of whatever re-derivation established this universe. Empty until
    #: a resolver has verified it -- an unverified universe is a claim.
    evidence_fingerprint: str = ""
    schema_version: str = EXPECTED_UNIVERSE_SCHEMA_VERSION
    _hash: str = field(default="", compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "identities", frozenset(self.identities))
        kind = ExpectedUniverseSourceKind(self.source_kind)
        object.__setattr__(self, "source_kind", kind)
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError(
                "ExpectedContractUniverse.observed_at must be timezone-aware; a "
                "naive instant silently means whatever the reading machine's "
                "zone is, and this records when somebody looked"
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
            "observed_at": self.observed_at.isoformat(),
            "source_record_ids": sorted(self.source_record_ids),
            "complete_for_request": self.complete_for_request,
            "documentation_evidence_id": self.documentation_evidence_id,
            "evidence_fingerprint": self.evidence_fingerprint,
        }

    @property
    def universe_hash(self) -> str:
        """Full SHA-256 over the identities and their provenance."""
        return self._hash

    @property
    def display_id(self) -> str:
        return f"{self.source_kind.value}@{short_id(self.universe_hash)}"

    @property
    def identity_set(self) -> frozenset[ContractIdentity]:
        """The identities, as ``__post_init__`` normalised them.

        ``identities`` is annotated as an iterable so a call site can pass a set
        or a generator; this accessor says what the field actually holds.
        """
        return frozenset(self.identities)

    @property
    def source(self) -> str:
        """The kind, spelled for a report. Never used to decide anything."""
        return self.source_kind.value

    @property
    def verified(self) -> bool:
        """Whether a resolver re-derived this universe from its source."""
        return bool(self.evidence_fingerprint)

    @property
    def independently_observed(self) -> bool:
        """Whether something outside the chain stated this, *and* it was checked.

        Both halves are required. A ``CALLER_DECLARED`` universe is never
        independent however many records it lists, and a
        ``VENDOR_CONTRACT_LIST`` that no resolver has re-derived is a claim
        about records rather than a reading of them.
        """
        return self.source_kind.is_independent_evidence and self.verified

    @property
    def establishes_completeness(self) -> bool:
        """Whether this universe can support a ``MEASURED_COMPLETE`` verdict.

        A partial page can prove a contract is *missing*. It cannot prove none
        is, because it never claimed to list them all.
        """
        return self.independently_observed and self.complete_for_request

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "identity_count": len(self.identity_set),
            "universe_hash": self.universe_hash,
            "independently_observed": self.independently_observed,
            "establishes_completeness": self.establishes_completeness,
            "verified": self.verified,
        }
