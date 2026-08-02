"""Which contracts should have arrived, stated by something other than the chain.

An expected universe changes three things: completeness, the confidence score,
and whether a dataset is fit to build on. v2.1.7 had the type -- in
``src.domain.completeness`` -- and nothing bound it. It was an argument to
``compute_gex_snapshot``, so the same capture could be scored against one
universe and replayed against another, or against none, and the replay would
report ``PARTIALLY_OBSERVED`` where the original reported
``MEASURED_COMPLETE``. Two different answers from the same bytes.

The binding here is a full digest over the identities and their provenance,
carried into the capture operation, the normalization recipe and the receipt --
so replay receives the exact universe the original was measured against, and a
*different* universe is a different chain rather than a quieter one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.domain.digests import digest_of, short_id

__all__ = [
    "EXPECTED_UNIVERSE_SCHEMA_VERSION",
    "ExpectedContractUniverse",
]

#: Bumped when the *meaning* of a universe hash changes.
EXPECTED_UNIVERSE_SCHEMA_VERSION = "expected-universe/2.1.8"


@dataclass(frozen=True, slots=True)
class ExpectedContractUniverse:
    """An independent statement of which contracts a chain should contain.

    ``source_record_ids`` is what makes it evidence rather than an opinion: a
    universe read out of stored vendor bytes can be re-read, and one that names
    no records is a list somebody typed. Both are legitimate -- a documented
    universe is a real thing -- and they are not the same claim, so the digest
    covers which one this is.

    ``complete_for_request`` records whether the source enumerated the whole
    requested universe or one page of it. A partial list still detects missing
    identities; it cannot establish completeness.
    """

    #: Annotated as an iterable and normalised in ``__post_init__``: passing a
    #: set or a generator at a call site is natural, and the type this ends up
    #: holding is always a frozenset.
    identities: Iterable[str]
    source: str
    observed_at: datetime
    source_record_ids: tuple[str, ...] = ()
    complete_for_request: bool = True
    schema_version: str = EXPECTED_UNIVERSE_SCHEMA_VERSION
    #: Documentation evidence backing a universe that came from a document
    #: rather than from a response. Empty for an observed one.
    documentation_evidence_id: str = ""
    _hash: str = field(default="", compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "identities", frozenset(self.identities))
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError(
                "ExpectedContractUniverse.observed_at must be timezone-aware; a "
                "naive instant silently means whatever the reading machine's "
                "zone is, and this records when somebody looked"
            )
        if not self.source.strip():
            raise ValueError(
                "ExpectedContractUniverse.source is empty. A universe that does "
                "not say where it came from cannot be checked, and an "
                "expectation nobody can check is the defect this type exists to "
                "prevent -- v2 inferred the expected universe from the response "
                "being judged, so a truncated response was complete."
            )
        object.__setattr__(self, "_hash", digest_of(self.semantic_payload()))

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identities": sorted(self.identities),
            "source": self.source,
            "observed_at": self.observed_at.isoformat(),
            "source_record_ids": sorted(self.source_record_ids),
            "complete_for_request": self.complete_for_request,
            "documentation_evidence_id": self.documentation_evidence_id,
        }

    @property
    def universe_hash(self) -> str:
        """Full SHA-256 over the identities and their provenance."""
        return self._hash

    @property
    def display_id(self) -> str:
        return f"{self.source}@{short_id(self.universe_hash)}"

    @property
    def identity_set(self) -> frozenset[str]:
        """The identities, as ``__post_init__`` normalised them.

        ``identities`` is annotated as an iterable so a call site can pass a set
        or a generator; this accessor says what the field actually holds.
        """
        return frozenset(self.identities)

    @property
    def independently_observed(self) -> bool:
        """Whether something outside the chain being judged said this."""
        return bool(self.source_record_ids or self.documentation_evidence_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "identity_count": len(self.identity_set),
            "universe_hash": self.universe_hash,
            "independently_observed": self.independently_observed,
        }
