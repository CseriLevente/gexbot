"""Evidence that a source enumerated the whole request, not merely some of it.

Two kinds, both absent from v2.1.9 in the sense that mattered.

**Pagination.** ``CAPTURED_PAGINATION_METADATA`` was a source kind whose
resolver re-derived *identities* and never read a page number, a total or a
continuation token -- because the resolver for it was the same code that handles
a contract listing. So the kind named a check nobody had written, and one
ordinary quote response satisfied it.

**Documentation.** The universe resolver looked its evidence id up in
``DOCUMENTATION_RULES``, which is the *settlement* registry. A rule saying "open
interest settles on the prior trading session" is a content-verified document
that says nothing whatsoever about which option contracts exist, and it
established a universe of whatever identities the caller had put beside it.

Both are fixed the same way: a separate, typed object that has to be read out of
something, and a resolver that refuses when it cannot be.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.adapters.errors import ThetaDataProvenanceError
from src.domain.completeness import ContractIdentity
from src.domain.digests import digest_of
from src.domain.universe_scope import UniverseRequestScope

__all__ = [
    "PAGINATION_METADATA_FIELDS",
    "UNIVERSE_DOCUMENTATION_RULES",
    "PaginationCoverageEvidence",
    "UniverseDerivation",
    "UniverseDocumentationRegistry",
    "UniverseDocumentationRule",
    "read_pagination_metadata",
]

#: Columns a response would have to carry for pagination coverage to be
#: verifiable. **No ThetaData v3 snapshot endpoint returns any of them**, which
#: is why ``CAPTURED_PAGINATION_METADATA`` resolves to a failure today rather
#: than to a coverage state. Listed so the check is against a named thing.
PAGINATION_METADATA_FIELDS = (
    "page",
    "total_pages",
    "total_results",
    "next_page_token",
)


@dataclass(frozen=True, slots=True)
class PaginationCoverageEvidence:
    """Which pages of a paginated listing were actually captured.

    Every field is read back out of stored responses by
    :func:`read_pagination_metadata`. Constructing one by hand is possible and
    proves nothing: the resolver builds its own from the bytes and compares, so
    a caller-supplied ``total_pages`` is a number the check disagrees with
    rather than a number the check believes.
    """

    total_pages: int
    captured_pages: frozenset[int]
    source_record_ids: tuple[str, ...]
    total_results: int | None = None
    continuation_complete: bool = False
    #: One digest per partition, where a sweep was split by strike range or
    #: expiration rather than paginated.
    partition_fingerprints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "captured_pages", frozenset(self.captured_pages))
        if self.total_pages < 1:
            raise ThetaDataProvenanceError(
                f"total_pages is {self.total_pages}; a listing with no pages "
                "enumerated nothing"
            )
        if not self.source_record_ids:
            raise ThetaDataProvenanceError(
                "pagination evidence names no records, so nothing can be "
                "re-read to confirm which pages were captured"
            )

    @property
    def missing_pages(self) -> tuple[int, ...]:
        return tuple(sorted(set(range(1, self.total_pages + 1)) - self.captured_pages))

    @property
    def complete(self) -> bool:
        """Every declared page present, and the vendor said there are no more."""
        return not self.missing_pages and self.continuation_complete

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "total_pages": self.total_pages,
            "captured_pages": sorted(self.captured_pages),
            "source_record_ids": sorted(self.source_record_ids),
            "total_results": self.total_results,
            "continuation_complete": self.continuation_complete,
            "partition_fingerprints": sorted(self.partition_fingerprints),
        }

    @property
    def fingerprint(self) -> str:
        return digest_of(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "fingerprint": self.fingerprint,
            "missing_pages": list(self.missing_pages),
            "complete": self.complete,
        }


def read_pagination_metadata(
    payloads: dict[str, str],
) -> PaginationCoverageEvidence | None:
    """Build pagination evidence from stored responses, or ``None``.

    ``None`` when the responses carry no pagination metadata at all, which is
    the state of every ThetaData v3 snapshot endpoint this repository has
    characterised. The resolver turns that into a refusal; it does not turn it
    into a coverage claim.
    """
    pages: dict[str, int] = {}
    totals: set[int] = set()
    results: set[int] = set()
    continuations: set[str] = set()

    for record_id, payload in payloads.items():
        rows = list(csv.DictReader(io.StringIO(payload)))
        if not rows:
            continue
        header = rows[0]
        if not any(field in header for field in PAGINATION_METADATA_FIELDS):
            continue
        page = _as_int(header.get("page"))
        total = _as_int(header.get("total_pages"))
        if page is None or total is None:
            raise ThetaDataProvenanceError(
                f"record {record_id!r} carries partial pagination metadata "
                f"({sorted(k for k in header if k in PAGINATION_METADATA_FIELDS)}); "
                "a page number without a total, or a total without a page, "
                "cannot establish how much of the request was captured"
            )
        pages[record_id] = page
        totals.add(total)
        found = _as_int(header.get("total_results"))
        if found is not None:
            results.add(found)
        continuations.add(str(header.get("next_page_token", "")).strip())

    if not pages:
        return None
    if len(totals) != 1:
        raise ThetaDataProvenanceError(
            f"the captured pages disagree about how many there are ({sorted(totals)}); "
            "responses from two different sweeps are not one sweep"
        )
    return PaginationCoverageEvidence(
        total_pages=totals.pop(),
        captured_pages=frozenset(pages.values()),
        source_record_ids=tuple(sorted(pages)),
        total_results=results.pop() if len(results) == 1 else None,
        # The last page is the one that says there is nothing after it.
        continuation_complete=any(token == "" for token in continuations),
    )


def _as_int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# =============================================================================
# Universe documentation -- separate from settlement documentation
# =============================================================================


@dataclass(frozen=True, slots=True)
class UniverseDerivation:
    """Typed semantics that deterministically produce a contract set.

    The alternative to listing identities outright. A document saying "SPXW
    lists strikes at 5-point intervals from 90% to 110% of spot, calls and
    puts, for every weekly expiration" is a rule; a resolver can apply it and
    get the same answer twice.

    Deliberately minimal and deliberately unimplemented: no ThetaData document
    stating such a rule has been read, so the only honest thing to ship is the
    shape it would take and a resolver that refuses without it.
    """

    rule_identifier: str
    extraction_version: str
    #: Explicit strike ladder, where the document lists one.
    strikes: tuple[str, ...] = ()
    expirations: tuple[date, ...] = ()
    rights: tuple[str, ...] = ("call", "put")
    root: str = ""

    def __post_init__(self) -> None:
        for name in ("rule_identifier", "extraction_version"):
            if not str(getattr(self, name)).strip():
                raise ThetaDataProvenanceError(
                    f"UniverseDerivation.{name} is empty; a derivation that "
                    "cannot say which reading of which rule produced it is an "
                    "assertion with extra steps"
                )

    def derive(self) -> frozenset[ContractIdentity]:
        """The identity set this rule produces, or a refusal.

        Refuses rather than returning an empty set: an empty universe and an
        underspecified one are different states, and only the second is a bug.
        """
        from src.domain.completeness import contract_identity

        if not (self.strikes and self.expirations and self.root):
            raise ThetaDataProvenanceError(
                f"universe derivation {self.rule_identifier!r} does not specify "
                "a root, a strike ladder and expirations, so it produces no "
                "contract set. A rule that cannot be applied documents "
                "something; not which options exist."
            )
        return frozenset(
            contract_identity(
                symbol=self.root, expiry=expiry.isoformat(), strike=strike, right=right
            )
            for expiry in self.expirations
            for strike in self.strikes
            for right in self.rights
        )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "rule_identifier": self.rule_identifier,
            "extraction_version": self.extraction_version,
            "root": self.root,
            "strikes": list(self.strikes),
            "expirations": [d.isoformat() for d in self.expirations],
            "rights": list(self.rights),
        }


@dataclass(frozen=True, slots=True)
class UniverseDocumentationRule:
    """A document that states which option contracts exist.

    A *different type* from the settlement ``DocumentationRule``, and the
    separation is the point. v2.1.9's universe resolver looked its evidence id
    up in the settlement registry, so a content-verified document about
    open-interest settlement established a universe of whatever identities the
    caller had put beside it. Both rules are content-verified; they are
    verified to say different things.

    Either ``identities`` or ``derivation`` must be present. A document that
    does neither is a document about something else.
    """

    evidence_id: str
    document_reference: str
    document_content_hash: str
    rule_identifier: str
    effective_from: date
    scope: UniverseRequestScope
    extraction_version: str
    effective_to: date | None = None
    identities: frozenset[ContractIdentity] | None = None
    derivation: UniverseDerivation | None = None
    verified_location: str = ""

    def __post_init__(self) -> None:
        for name in ("evidence_id", "document_reference", "rule_identifier"):
            if not str(getattr(self, name)).strip():
                raise ThetaDataProvenanceError(
                    f"UniverseDocumentationRule.{name} is empty; a rule that "
                    "does not say what it is, or where it came from, cannot be "
                    "checked by anyone reading the certification report"
                )
        if len(self.document_content_hash) != 64:
            raise ThetaDataProvenanceError(
                f"UniverseDocumentationRule.document_content_hash is "
                f"{len(self.document_content_hash)} characters; a full SHA-256 "
                "of the referenced content is what makes this evidence rather "
                "than a citation"
            )
        if self.identities is None and self.derivation is None:
            raise ThetaDataProvenanceError(
                f"universe rule {self.evidence_id!r} carries neither an identity "
                "set nor a derivation, so it establishes no contracts. A "
                "content-verified document that says nothing about which "
                "options exist must establish nothing -- which is how a "
                "settlement-convention document came to define a universe in "
                "v2.1.9."
            )
        if self.identities is not None:
            object.__setattr__(self, "identities", frozenset(self.identities))

    def covers(self, moment: date) -> bool:
        if moment < self.effective_from:
            return False
        return self.effective_to is None or moment <= self.effective_to

    @property
    def established(self) -> bool:
        """Whether this rule has been read out of its document."""
        return bool(self.verified_location)

    def derive_identities(self) -> frozenset[ContractIdentity]:
        """The contracts this document states, listed or derived."""
        if self.identities is not None:
            return frozenset(self.identities)
        assert self.derivation is not None  # __post_init__ guarantees one of them
        return self.derivation.derive()

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "universe-documentation/2.1.10",
            "evidence_id": self.evidence_id,
            "document_reference": self.document_reference,
            "document_content_hash": self.document_content_hash,
            "rule_identifier": self.rule_identifier,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": (
                self.effective_to.isoformat() if self.effective_to else None
            ),
            "extraction_version": self.extraction_version,
            "scope": self.scope.semantic_payload(),
            "identities": (
                sorted(self.identities) if self.identities is not None else None
            ),
            "derivation": (
                self.derivation.semantic_payload() if self.derivation else None
            ),
        }

    @property
    def evidence_fingerprint(self) -> str:
        return digest_of(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "evidence_fingerprint": self.evidence_fingerprint,
            "verified_location": self.verified_location,
            "established": self.established,
        }


class UniverseDocumentationRegistry:
    """Universe rules this repository has read, hashed and recorded.

    Separate from ``DOCUMENTATION_RULES`` so that a settlement document cannot
    be looked up as a universe document by an id that happens to match.
    """

    def __init__(self) -> None:
        self._rules: dict[str, UniverseDocumentationRule] = {}

    def register(self, rule: UniverseDocumentationRule) -> UniverseDocumentationRule:
        """Verify the document, then record the rule."""
        from dataclasses import replace

        from src.adapters.evidence_resolvers import verify_document

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
                f"universe evidence id {rule.evidence_id!r} is already "
                "registered with different content"
            )
        self._rules[rule.evidence_id] = verified
        return verified

    def get(self, evidence_id: str) -> UniverseDocumentationRule | None:
        return self._rules.get(evidence_id)

    def registered_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._rules))

    def __contains__(self, evidence_id: object) -> bool:
        return evidence_id in self._rules

    def __len__(self) -> int:
        return len(self._rules)


#: The registry the universe resolver consults. Deliberately empty: this
#: repository has read no document stating which SPX/SPXW contracts exist, and
#: pre-populating it would be exactly the defect being closed. OD-11.
UNIVERSE_DOCUMENTATION_RULES = UniverseDocumentationRegistry()
