"""Evidence that a source enumerated the whole request, not merely some of it.

Two kinds, and both have now been wrong twice in different ways.

**Pagination.** ``CAPTURED_PAGINATION_METADATA`` was a v2.1.9 source kind whose
resolver re-derived *identities* and never read a page number, a total or a
continuation token. v2.1.10 read them out of the stored bytes, and read them
loosely: two responses claiming to be page 3 collapsed into one entry, a
``total_results`` disagreement was discarded rather than refused, and several
terminal pages counted as one. No endpoint reaches this path today, which is
exactly when the semantics are cheap to fix.

**Documentation.** v2.1.9 looked its evidence id up in the *settlement*
registry, so a document about open-interest settlement established a universe of
whatever identities the caller had put beside it. v2.1.10 gave universe
documents their own registry and still let the rule carry
``identities=frozenset(...)`` -- a caller-supplied list, alongside a hash of a
real file. The hash proves which bytes were read. It proves nothing about where
the identities came from, and they came from the caller.

So a documentation rule no longer states contracts. It names a *document* and an
*extractor version*, and the identities are whatever a registered, versioned
extractor reads out of the verified bytes, recorded with the byte ranges it read
them from.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from src.adapters.errors import ThetaDataProvenanceError
from src.domain.completeness import ContractIdentity, contract_identity
from src.domain.digests import digest_of
from src.domain.universe_scope import UniverseRequestScope

__all__ = [
    "PAGINATION_METADATA_FIELDS",
    "RECOVERED_DOCUMENT_LOCATION",
    "UNIVERSE_DOCUMENTATION_RULES",
    "UNIVERSE_DOCUMENTATION_SCHEMA_VERSION",
    "UNIVERSE_EXTRACTION_SCHEMA_VERSION",
    "UNIVERSE_EXTRACTORS",
    "PaginationCoverageEvidence",
    "UniverseDocumentationEvidenceArtifact",
    "UniverseDocumentationRegistry",
    "UniverseDocumentationRule",
    "UniverseExtractionArtifact",
    "build_documentation_evidence",
    "document_bytes_payload",
    "extractor_for",
    "read_pagination_metadata",
]

#: Bumped when what a documentation universe has to carry changes.
UNIVERSE_DOCUMENTATION_SCHEMA_VERSION = "universe-documentation/2.1.12"

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
    #: Exactly one captured page must say there is nothing after it. Several
    #: terminal pages means several sweeps, not one complete sweep.
    continuation_complete: bool = False
    #: One digest per partition, where a sweep was split by strike range or
    #: expiration rather than paginated.
    partition_fingerprints: tuple[str, ...] = ()
    #: Whether two partitions covering the same contracts is acceptable for this
    #: source. Off by default: overlapping partitions inflate the identity count
    #: against ``total_results`` and hide a gap somewhere else.
    overlapping_partitions_allowed: bool = False

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
        if len(set(self.source_record_ids)) != len(self.source_record_ids):
            raise ThetaDataProvenanceError(
                "pagination evidence names the same record twice; one response "
                "counted as two pages is one page short of what it claims"
            )
        if not self.overlapping_partitions_allowed and len(
            set(self.partition_fingerprints)
        ) != len(self.partition_fingerprints):
            raise ThetaDataProvenanceError(
                "two partitions of this sweep have the same fingerprint, so they "
                "asked the same question; overlapping partitions inflate the "
                "identity count and hide a gap elsewhere"
            )
        beyond = sorted(p for p in self.captured_pages if p > self.total_pages or p < 1)
        if beyond:
            raise ThetaDataProvenanceError(
                f"pages {beyond} are outside 1..{self.total_pages}; a page number "
                "the sweep does not contain is not evidence about the sweep"
            )

    @property
    def missing_pages(self) -> tuple[int, ...]:
        return tuple(sorted(set(range(1, self.total_pages + 1)) - self.captured_pages))

    @property
    def complete(self) -> bool:
        """Every declared page present, and the vendor said there are no more."""
        return not self.missing_pages and self.continuation_complete

    def identity_count_refusals(self, derived: int) -> tuple[str, ...]:
        """Why the identities derived from these pages contradict the metadata.

        ``total_results`` is the vendor stating how many contracts the request
        owed. If the pages parse into a different number, one of the two is
        wrong and neither can be preferred -- so full coverage is refused rather
        than resolved to whichever number is larger.
        """
        if self.total_results is None:
            return (
                "the captured pages carry no total_results, so nothing states "
                "how many contracts the request owed; the page numbers alone "
                "cannot distinguish a complete sweep from a truncated one",
            )
        if derived != self.total_results:
            return (
                f"the pages state total_results={self.total_results} and parse "
                f"into {derived} unique contract identities; a listing that does "
                "not contain the number of contracts it says it contains has "
                "not enumerated the request",
            )
        return ()

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "total_pages": self.total_pages,
            "captured_pages": sorted(self.captured_pages),
            "source_record_ids": sorted(self.source_record_ids),
            "total_results": self.total_results,
            "continuation_complete": self.continuation_complete,
            "partition_fingerprints": sorted(self.partition_fingerprints),
            "overlapping_partitions_allowed": self.overlapping_partitions_allowed,
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
    terminal: list[str] = []

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
        if not str(header.get("next_page_token", "")).strip():
            terminal.append(record_id)

    if not pages:
        return None
    if len(totals) != 1:
        raise ThetaDataProvenanceError(
            f"the captured pages disagree about how many there are ({sorted(totals)}); "
            "responses from two different sweeps are not one sweep"
        )
    if len(results) > 1:
        raise ThetaDataProvenanceError(
            f"the captured pages disagree about total_results ({sorted(results)}); "
            "one sweep states one total, and preferring either would be choosing "
            "which answer to get"
        )
    seen: dict[int, str] = {}
    for record_id, page in sorted(pages.items()):
        if page in seen:
            raise ThetaDataProvenanceError(
                f"records {seen[page]!r} and {record_id!r} both claim to be page "
                f"{page} of {sorted(totals)[0]}; two responses to the same page "
                "are one page captured twice, not two pages captured"
            )
        seen[page] = record_id
    if len(terminal) != 1:
        raise ThetaDataProvenanceError(
            f"{len(terminal)} of the captured pages carry no continuation token "
            f"({sorted(terminal)}); exactly one page ends a sweep, and none or "
            "several means this is not one complete sweep"
        )
    return PaginationCoverageEvidence(
        total_pages=next(iter(totals)),
        captured_pages=frozenset(pages.values()),
        source_record_ids=tuple(sorted(pages)),
        total_results=next(iter(results)) if results else None,
        continuation_complete=True,
    )


def _as_int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# =============================================================================
# Universe documentation -- extracted from bytes, not stated beside them
# =============================================================================

#: Bumped when the *meaning* of an extraction changes.
UNIVERSE_EXTRACTION_SCHEMA_VERSION = "universe-extraction/2.1.11"

#: The marker an extractable universe table is delimited by. A document that
#: does not contain one states no contracts as far as this code is concerned,
#: whatever prose it holds.
_RULE_BLOCK = re.compile(
    r"<!--\s*universe-rule:\s*(?P<rule>[^\s>]+)\s*-->\r?\n"
    r"(?P<body>.*?)"
    r"<!--\s*end-universe-rule\s*-->",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class UniverseExtractionArtifact:
    """Contract identities read out of a verified document, and from where.

    The object v2.1.10 lacked. Its rule carried ``identities`` and a document
    hash side by side, so the hash authenticated bytes nobody had read the
    identities out of -- a citation formatted as evidence.

    Every field here is a fact about the extraction: which document, verified
    when, effective when, read by which extractor version, at which byte
    offsets, executed at which instant. ``extraction_executed_at`` is what a
    universe's ``observed_at`` is derived from, because the declaration's
    ``declared_at`` is a caller statement and staleness measured against a
    caller statement is not measured.
    """

    rule_identifier: str
    extractor_version: str
    document_content_hash: str
    #: When registration opened the file and computed its hash.
    document_verified_at: datetime
    #: The session the document's rule is effective from.
    document_effective_date: date
    #: When the extractor ran over those bytes.
    extraction_executed_at: datetime
    identities: frozenset[ContractIdentity]
    #: ``(start, end)`` character offsets in the verified document that the
    #: identities were read from. A range nobody can point at is an assertion.
    source_ranges: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "identities", frozenset(self.identities))
        object.__setattr__(
            self,
            "source_ranges",
            tuple(sorted((int(a), int(b)) for a, b in self.source_ranges)),
        )
        if not self.identities:
            raise ThetaDataProvenanceError(
                f"extraction {self.rule_identifier!r} produced no identities; an "
                "empty universe and a document that does not state one are "
                "different things, and only the second happened here"
            )
        if not self.source_ranges:
            raise ThetaDataProvenanceError(
                f"extraction {self.rule_identifier!r} names no source ranges, so "
                "nothing says which part of the document the identities came "
                "from"
            )
        for name in ("document_verified_at", "extraction_executed_at"):
            moment = getattr(self, name)
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ThetaDataProvenanceError(
                    f"UniverseExtractionArtifact.{name} must be timezone-aware"
                )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": UNIVERSE_EXTRACTION_SCHEMA_VERSION,
            "rule_identifier": self.rule_identifier,
            "extractor_version": self.extractor_version,
            "document_content_hash": self.document_content_hash,
            "document_verified_at": self.document_verified_at.isoformat(),
            "document_effective_date": self.document_effective_date.isoformat(),
            "extraction_executed_at": self.extraction_executed_at.isoformat(),
            "identities": sorted(self.identities),
            "source_ranges": [list(pair) for pair in self.source_ranges],
        }

    @property
    def fingerprint(self) -> str:
        return digest_of(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "fingerprint": self.fingerprint,
            "identity_count": len(self.identities),
        }


#: An extractor: verified document text plus a rule name in, identities and the
#: character ranges they were read from out.
Extractor = Callable[
    [str, str], tuple[frozenset[ContractIdentity], tuple[tuple[int, int], ...]]
]


def _contract_table_extractor(
    text: str, rule_identifier: str
) -> tuple[frozenset[ContractIdentity], tuple[tuple[int, int], ...]]:
    """Read a machine-readable contract table out of a document.

    The only extractor this repository implements, and it is deliberately
    literal: it finds the block delimited by ``<!-- universe-rule: NAME -->``,
    parses the CSV inside it, and reports the character range it read. Nothing
    is inferred from prose, because a rule inferred from prose is a rule
    somebody decided.
    """
    for match in _RULE_BLOCK.finditer(text):
        if match.group("rule").strip() != rule_identifier:
            continue
        body = match.group("body")
        start = match.start("body")
        rows = list(csv.DictReader(io.StringIO(body)))
        if not rows:
            raise ThetaDataProvenanceError(
                f"the universe-rule block {rule_identifier!r} contains no rows, "
                "so the document states no contracts under that rule"
            )
        missing = [
            c for c in ("symbol", "expiration", "strike", "right") if c not in rows[0]
        ]
        if missing:
            raise ThetaDataProvenanceError(
                f"the universe-rule block {rule_identifier!r} is missing columns "
                f"{missing}; its columns are {sorted(rows[0])}"
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
                    f"row {index} of universe-rule block {rule_identifier!r} does "
                    f"not parse into a contract identity: {error}"
                ) from error
        return frozenset(identities), ((start, start + len(body)),)
    raise ThetaDataProvenanceError(
        f"the verified document contains no <!-- universe-rule: "
        f"{rule_identifier} --> block, so it does not state which contracts "
        "exist under that rule. A content hash proves which bytes were read; it "
        "does not make those bytes about contracts."
    )


def _canonical_right(value: str) -> str:
    from src.domain.contracts import OptionRight

    text = value.strip().lower()
    if text in ("c", "call"):
        return OptionRight.CALL.value
    if text in ("p", "put"):
        return OptionRight.PUT.value
    return text


#: Extractors by version. Code rather than caller state: registering a *rule*
#: is a deliberate act by an operator, registering an *extractor* is shipping an
#: implementation, and only the first is something a test or a caller does.
UNIVERSE_EXTRACTORS: dict[str, Extractor] = {
    "contract-table/2.1.11": _contract_table_extractor,
}


def extractor_for(version: str) -> Extractor:
    extractor = UNIVERSE_EXTRACTORS.get(version)
    if extractor is None:
        raise ThetaDataProvenanceError(
            f"no universe extractor is registered for {version!r}; known "
            f"versions are {sorted(UNIVERSE_EXTRACTORS)}. An unversioned reading "
            "of a document is not reproducible, so it is not evidence."
        )
    return extractor


@dataclass(frozen=True, slots=True)
class UniverseDocumentationRule:
    """A document that states which option contracts exist, and how to read it.

    A *different type* from the settlement ``DocumentationRule``, and the
    separation is v2.1.10's. What is new in v2.1.11 is that the rule no longer
    carries contracts. It carries a document, an effective period, a scope and
    an extractor version; the identities come from
    :meth:`extract`, which opens the verified bytes and runs that extractor.

    ``effective_from`` and ``effective_to`` are optional so that "this rule
    states no period" is *representable* and can be refused at resolution.
    Making them mandatory would have callers invent a date to satisfy the
    constructor, which is the failure mode being closed rather than avoided.
    """

    evidence_id: str
    document_reference: str
    document_content_hash: str
    rule_identifier: str
    scope: UniverseRequestScope
    extractor_version: str
    effective_from: date | None = None
    effective_to: date | None = None
    verified_location: str = ""
    #: When registration read and hashed the document. Set by the registry.
    document_verified_at: datetime | None = None

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
        if not str(self.extractor_version).strip():
            raise ThetaDataProvenanceError(
                f"universe rule {self.evidence_id!r} names no extractor version. "
                "Until v2.1.11 a rule could carry an identity list beside a "
                "document hash; the hash proved which bytes were read and the "
                "identities came from whoever wrote the rule."
            )
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ThetaDataProvenanceError(
                f"universe rule {self.evidence_id!r} expires "
                f"{self.effective_to.isoformat()}, before it takes effect "
                f"{self.effective_from.isoformat()}"
            )

    # -- effective period ----------------------------------------------------

    def period_refusals(self, session: date) -> tuple[str, ...]:
        """Why this rule may not be applied to a given market session.

        v2.1.10 had ``covers()`` and never called it, so a rule effective from
        2030 established a universe for a March 2026 capture.
        """
        if self.effective_from is None:
            return (
                f"universe rule {self.evidence_id!r} states no effective date, "
                "so nothing says which sessions it describes. A contract "
                "universe is a fact about a period, not a standing one.",
            )
        if session < self.effective_from:
            return (
                f"universe rule {self.evidence_id!r} takes effect "
                f"{self.effective_from.isoformat()} and this capture is for "
                f"session {session.isoformat()}; a document about a later "
                "market does not describe this one",
            )
        if self.effective_to is not None and session > self.effective_to:
            return (
                f"universe rule {self.evidence_id!r} expired "
                f"{self.effective_to.isoformat()} and this capture is for "
                f"session {session.isoformat()}",
            )
        return ()

    def covers(self, session: date) -> bool:
        return not self.period_refusals(session)

    @property
    def established(self) -> bool:
        """Whether this rule has been read out of its document."""
        return bool(self.verified_location) and self.document_verified_at is not None

    # -- extraction ----------------------------------------------------------

    def document_text(self) -> str:
        """The verified bytes, read from where registration found them."""
        import pathlib

        from src.adapters.evidence_resolvers import content_hash_of

        if not self.established:
            raise ThetaDataProvenanceError(
                f"universe rule {self.evidence_id!r} has not been content "
                "verified; a hash nobody computed is not a hash"
            )
        location = pathlib.Path(self.verified_location)
        actual = content_hash_of(location)
        if actual != self.document_content_hash:
            raise ThetaDataProvenanceError(
                f"the document behind universe rule {self.evidence_id!r} now "
                f"hashes to {actual[:12]}... and the rule records "
                f"{self.document_content_hash[:12]}...; the bytes changed after "
                "registration, so the extraction would be from a different "
                "document"
            )
        return location.read_text(encoding="utf-8")

    def extract(
        self, *, executed_at: datetime, document_text: str | None = None
    ) -> UniverseExtractionArtifact:
        """Read the contracts out of the verified document.

        ``document_text`` is the v2.1.12 addition: the *recovered* bytes, which
        arrive from the artifact store rather than from a path. Until then a
        documentation resolution could only be re-run on a machine that still
        had the file at the same location and a registry populated in the same
        process -- so ``capture_session``, which re-runs the resolution, refused
        every documentation universe a caller had resolved with its own
        registry.
        """
        text = document_text if document_text is not None else self.document_text()
        digest = _text_hash(text)
        if digest != self.document_content_hash:
            raise ThetaDataProvenanceError(
                f"the document behind universe rule {self.evidence_id!r} hashes "
                f"to {digest[:12]}... and the rule records "
                f"{self.document_content_hash[:12]}...; these are different "
                "documents, so the extraction would be from the wrong one"
            )
        assert self.document_verified_at is not None  # implied by established
        identities, ranges = extractor_for(self.extractor_version)(
            text, self.rule_identifier
        )
        assert self.effective_from is not None  # callers check period first
        return UniverseExtractionArtifact(
            rule_identifier=self.rule_identifier,
            extractor_version=self.extractor_version,
            document_content_hash=self.document_content_hash,
            document_verified_at=self.document_verified_at,
            document_effective_date=self.effective_from,
            extraction_executed_at=executed_at,
            identities=identities,
            source_ranges=ranges,
        )

    def confirm(
        self,
        artifact: UniverseExtractionArtifact,
        *,
        document_text: str | None = None,
    ) -> tuple[str, ...]:
        """Why a stored extraction is not what this rule produces today.

        The recovery path. Re-running :meth:`extract` would stamp a fresh
        ``extraction_executed_at`` and therefore a different universe hash, so
        recovery re-reads the bytes and compares *what was extracted* while
        keeping the instant the extraction actually happened.
        """
        try:
            fresh = self.extract(
                executed_at=artifact.extraction_executed_at,
                document_text=document_text,
            )
        except ThetaDataProvenanceError as error:
            return (str(error),)
        if fresh.identities != artifact.identities:
            missing = sorted(artifact.identities - fresh.identities)
            extra = sorted(fresh.identities - artifact.identities)
            return (
                f"re-reading the document produces a different contract set: "
                f"{len(missing)} stored but not extracted ({missing[:3]}), "
                f"{len(extra)} extracted but not stored ({extra[:3]})",
            )
        if fresh.source_ranges != artifact.source_ranges:
            return (
                f"the identities were extracted from {list(fresh.source_ranges)} "
                f"and the stored artifact records {list(artifact.source_ranges)}",
            )
        if fresh.fingerprint != artifact.fingerprint:
            return (
                "the stored extraction artifact does not match a fresh reading "
                "of the same document under the same extractor",
            )
        return ()

    # -- identity ------------------------------------------------------------

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": UNIVERSE_DOCUMENTATION_SCHEMA_VERSION,
            "evidence_id": self.evidence_id,
            "document_reference": self.document_reference,
            "document_content_hash": self.document_content_hash,
            "rule_identifier": self.rule_identifier,
            "effective_from": (
                self.effective_from.isoformat() if self.effective_from else None
            ),
            "effective_to": (
                self.effective_to.isoformat() if self.effective_to else None
            ),
            "extractor_version": self.extractor_version,
            "scope": self.scope.semantic_payload(),
        }

    @property
    def evidence_fingerprint(self) -> str:
        return digest_of(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "evidence_fingerprint": self.evidence_fingerprint,
            "verified_location": self.verified_location,
            "document_verified_at": (
                self.document_verified_at.isoformat()
                if self.document_verified_at
                else None
            ),
            "established": self.established,
        }

    # -- reconstruction ------------------------------------------------------

    def portable_payload(self) -> dict[str, Any]:
        """Everything :meth:`rebuilt_from` needs, and nothing about this host.

        Deliberately includes ``document_verified_at`` -- which is a fact about
        the registration, and enters the extraction artifact -- and deliberately
        excludes ``verified_location``, which is a path on one machine.
        """
        return {
            **self.semantic_payload(),
            "document_verified_at": (
                self.document_verified_at.isoformat()
                if self.document_verified_at
                else None
            ),
        }

    @classmethod
    def rebuilt_from(cls, payload: dict[str, Any]) -> UniverseDocumentationRule:
        """Reconstruct a rule from its portable payload.

        ``verified_location`` comes back as a marker rather than a path: the
        rebuilt rule is used with recovered *bytes*, and a rule that could still
        open a file would be a rule that could read a different document from
        the one the capture was resolved against.
        """
        return cls(
            evidence_id=payload["evidence_id"],
            document_reference=payload["document_reference"],
            document_content_hash=payload["document_content_hash"],
            rule_identifier=payload["rule_identifier"],
            scope=UniverseRequestScope(
                root=payload["scope"]["root"],
                expirations=(
                    tuple(
                        date.fromisoformat(v) for v in payload["scope"]["expirations"]
                    )
                    if payload["scope"].get("expirations")
                    else None
                ),
                max_dte=payload["scope"].get("max_dte"),
                strike_range=payload["scope"].get("strike_range"),
                rights=tuple(payload["scope"].get("rights") or ("call", "put")),
                request_filters=tuple(
                    (pair[0], pair[1])
                    for pair in payload["scope"].get("request_filters") or ()
                ),
                requested_at=(
                    datetime.fromisoformat(payload["scope"]["requested_at"])
                    if payload["scope"].get("requested_at")
                    else None
                ),
            ),
            extractor_version=payload["extractor_version"],
            effective_from=(
                date.fromisoformat(payload["effective_from"])
                if payload.get("effective_from")
                else None
            ),
            effective_to=(
                date.fromisoformat(payload["effective_to"])
                if payload.get("effective_to")
                else None
            ),
            verified_location=RECOVERED_DOCUMENT_LOCATION,
            document_verified_at=(
                datetime.fromisoformat(payload["document_verified_at"])
                if payload.get("document_verified_at")
                else None
            ),
        )


#: What a rebuilt rule records instead of a filesystem path. It is deliberately
#: not openable: recovery works from the content-addressed bytes, and a rule that
#: could reopen a file could read a document other than the one the capture was
#: resolved against.
RECOVERED_DOCUMENT_LOCATION = "<recovered-from-artifact-store>"


def _text_hash(text: str) -> str:
    """The document hash, over exactly the bytes an extractor will read."""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class UniverseDocumentationEvidenceArtifact:
    """A documentation universe, reconstructable without any live object.

    The v2.1.12 gap. A ``UniverseResolution`` produced with a caller-supplied
    :class:`UniverseDocumentationRegistry` resolved, and then ``capture_session``
    re-ran it *without* that registry and refused it -- so the only documentation
    resolution that could open a capture was one registered in the process-global
    registry, which production keeps empty.

    Everything needed to run the extractor again is here or content-addressed
    beside it: the rule (portably, with no host path), the digest of the exact
    verified bytes, the artifact key those bytes are stored under, the extractor
    version and the extraction it produced.
    """

    evidence_id: str
    rule_semantic_payload: dict[str, Any]
    document_content_hash: str
    #: SHA-256 of the exact text the extractor read. Equal to
    #: ``document_content_hash`` for a UTF-8 document, and carried separately
    #: because "what the rule claims" and "what was read" are two statements.
    verified_document_bytes_hash: str
    #: The artifact store key those bytes live under.
    document_bytes_artifact_hash: str
    extractor_version: str
    extraction_artifact_hash: str
    effective_from: date | None
    effective_to: date | None
    scope: UniverseRequestScope

    def __post_init__(self) -> None:
        if self.verified_document_bytes_hash != self.document_content_hash:
            raise ThetaDataProvenanceError(
                "the bytes stored for this evidence hash to "
                f"{self.verified_document_bytes_hash[:12]}... and the rule "
                f"records {self.document_content_hash[:12]}...; a document "
                "artifact that is not the document the rule names cannot rerun "
                "the extraction"
            )

    @property
    def rule(self) -> UniverseDocumentationRule:
        return UniverseDocumentationRule.rebuilt_from(self.rule_semantic_payload)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": UNIVERSE_DOCUMENTATION_SCHEMA_VERSION,
            "evidence_id": self.evidence_id,
            "rule_semantic_payload": dict(sorted(self.rule_semantic_payload.items())),
            "document_content_hash": self.document_content_hash,
            "verified_document_bytes_hash": self.verified_document_bytes_hash,
            "document_bytes_artifact_hash": self.document_bytes_artifact_hash,
            "extractor_version": self.extractor_version,
            "extraction_artifact_hash": self.extraction_artifact_hash,
            "effective_from": (
                self.effective_from.isoformat() if self.effective_from else None
            ),
            "effective_to": (
                self.effective_to.isoformat() if self.effective_to else None
            ),
            "scope": self.scope.semantic_payload(),
        }

    @property
    def artifact_hash(self) -> str:
        return digest_of(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self.semantic_payload(), "artifact_hash": self.artifact_hash}

    @classmethod
    def rebuilt_from(
        cls, payload: dict[str, Any]
    ) -> UniverseDocumentationEvidenceArtifact:
        return cls(
            evidence_id=payload["evidence_id"],
            rule_semantic_payload=dict(payload["rule_semantic_payload"]),
            document_content_hash=payload["document_content_hash"],
            verified_document_bytes_hash=payload["verified_document_bytes_hash"],
            document_bytes_artifact_hash=payload["document_bytes_artifact_hash"],
            extractor_version=payload["extractor_version"],
            extraction_artifact_hash=payload["extraction_artifact_hash"],
            effective_from=(
                date.fromisoformat(payload["effective_from"])
                if payload.get("effective_from")
                else None
            ),
            effective_to=(
                date.fromisoformat(payload["effective_to"])
                if payload.get("effective_to")
                else None
            ),
            scope=UniverseDocumentationRule.rebuilt_from(
                payload["rule_semantic_payload"]
            ).scope,
        )


def document_bytes_payload(text: str) -> dict[str, Any]:
    """The content-addressed envelope the verified document is stored in."""
    return {
        "schema_version": UNIVERSE_DOCUMENTATION_SCHEMA_VERSION,
        "content_hash": _text_hash(text),
        "text": text,
    }


def build_documentation_evidence(
    rule: UniverseDocumentationRule,
    extraction: UniverseExtractionArtifact,
    *,
    document_text: str,
) -> UniverseDocumentationEvidenceArtifact:
    """Assemble the portable evidence for one documentation resolution."""
    return UniverseDocumentationEvidenceArtifact(
        evidence_id=rule.evidence_id,
        rule_semantic_payload=rule.portable_payload(),
        document_content_hash=rule.document_content_hash,
        verified_document_bytes_hash=_text_hash(document_text),
        document_bytes_artifact_hash=digest_of(document_bytes_payload(document_text)),
        extractor_version=rule.extractor_version,
        extraction_artifact_hash=extraction.fingerprint,
        effective_from=rule.effective_from,
        effective_to=rule.effective_to,
        scope=rule.scope,
    )


class UniverseDocumentationRegistry:
    """Universe rules this repository has read, hashed and recorded.

    Separate from ``DOCUMENTATION_RULES`` so that a settlement document cannot
    be looked up as a universe document by an id that happens to match.
    """

    def __init__(self) -> None:
        self._rules: dict[str, UniverseDocumentationRule] = {}

    def register(
        self,
        rule: UniverseDocumentationRule,
        *,
        verified_at: datetime | None = None,
    ) -> UniverseDocumentationRule:
        """Verify the document, then record the rule and when it was read."""
        from dataclasses import replace
        from datetime import UTC

        from src.adapters.evidence_resolvers import verify_document

        # Named extractor first: a rule whose extractor does not exist could be
        # registered, look established, and fail only when something depended on
        # it.
        extractor_for(rule.extractor_version)
        location, _ = verify_document(
            rule.document_reference, rule.document_content_hash
        )
        verified = replace(
            rule,
            verified_location=location,
            document_verified_at=(
                verified_at if verified_at is not None else datetime.now(UTC)
            ),
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
