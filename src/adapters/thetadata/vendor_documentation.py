"""Vendor documentation as content-addressed evidence, or not at all.

Several conventions this repository calls `UNKNOWN` are things the vendor
*documents*. Leaving them unknown because no live response has been captured
confuses two different reasons for not knowing: "nobody has looked" and "the
source does not say". The first is fixable by reading.

But a convention bound to a document nobody can re-check is not evidence, it is
a sentence somebody typed. So a binding here requires the document's exact
bytes, hashed, stored content-addressed, with the URL and the moment it was
retrieved -- and the specific sentence that was read out of it. A later reader
can fetch the same URL, hash what they get, and see whether the ground has
moved.

**The production registry is empty, and that is the honest state.**

v2.1.17 attempted to pin the official v3 documentation. What is reachable at
``http-docs.thetadata.us`` today is the **v2** operation set: the v3 operation
URLs return 404, and the pages that do resolve say nothing about open-interest
settlement, ``rate_value`` units, or a minimum time to expiration. Nor can a
fetch through a markdown-converting reader produce a hash of the *source* bytes
-- it returns a rendering, and hashing a rendering would be pinning our own
paraphrase and calling it the vendor's.

So the mechanism exists and nothing is registered. Registering an entry requires
bytes somebody actually holds. Until then the dimensions stay `UNKNOWN`, which
is what they are.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Final

__all__ = [
    "PRODUCTION_VENDOR_DOCUMENTATION",
    "VENDOR_DOCUMENTATION_EXTRACTOR_VERSION",
    "VENDOR_DOCUMENTATION_SCHEMA_VERSION",
    "DocumentedRule",
    "VendorDocumentationArtifact",
    "VendorDocumentationError",
    "register_vendor_documentation",
    "resolve_documented_rule",
    "store_document",
]

#: Bumped when what a documentation artifact must carry changes.
VENDOR_DOCUMENTATION_SCHEMA_VERSION = "vendor-documentation/2.1.17"

#: Bumped when *how a statement is read out of a document* changes. Separate
#: from the schema: the same bytes read under different rules produce different
#: claims, and a reader must be able to tell which reading it is looking at.
VENDOR_DOCUMENTATION_EXTRACTOR_VERSION = "vendor-documentation-extractor/2.1.17"


class VendorDocumentationError(ValueError):
    """A documentation artifact that cannot be trusted to say what it claims."""


class DocumentedRule(str, Enum):
    """The conventions a vendor document could settle.

    Named as a closed set so a caller cannot invent a rule identifier and bind
    something nobody reviewed. Each one is a question a GEX depends on.
    """

    #: Which trading session the open-interest figure belongs to.
    OPEN_INTEREST_SETTLEMENT = "OPEN_INTEREST_SETTLEMENT"
    #: Whether ``rate_value`` is a percent or a decimal fraction.
    RATE_UNITS = "RATE_UNITS"
    #: The floor the vendor applies to time-to-expiry under ``latest``.
    MINIMUM_TIME_FLOOR = "MINIMUM_TIME_FLOOR"


@dataclass(frozen=True, slots=True)
class VendorDocumentationArtifact:
    """One vendor document, pinned, and one statement read out of it.

    ``document_sha256`` is over the **source bytes**, not over a rendering of
    them. A markdown conversion, a summary or a screenshot is a reading; hashing
    one and calling it the document would pin our own paraphrase.
    """

    rule: DocumentedRule
    source_url: str
    retrieved_at: datetime
    document_sha256: str
    #: Where the bytes are, relative to the artifact root. Content-addressed, so
    #: the location is derivable from the digest and cannot drift from it.
    content_location: str
    #: The sentence, verbatim. Not a summary: a paraphrase is a second document
    #: nobody reviewed.
    extracted_statement: str
    #: What the statement is taken to establish, in this repository's terms.
    resolved_value: str
    extractor_version: str = VENDOR_DOCUMENTATION_EXTRACTOR_VERSION
    schema_version: str = VENDOR_DOCUMENTATION_SCHEMA_VERSION
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        digest = str(self.document_sha256).strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise VendorDocumentationError(
                f"document_sha256 {self.document_sha256!r} is not a full SHA-256. "
                "A binding whose digest cannot be checked is a sentence, not "
                "evidence."
            )
        if not str(self.extracted_statement).strip():
            raise VendorDocumentationError(
                f"{self.rule.value} names no extracted statement. Binding a "
                "convention to a document without quoting what it says is how a "
                "claim outlives the sentence it came from."
            )
        if not str(self.source_url).strip():
            raise VendorDocumentationError(f"{self.rule.value} names no source URL")

    @property
    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "extractor_version": self.extractor_version,
            "rule": self.rule.value,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at.isoformat(),
            "document_sha256": self.document_sha256,
            "content_location": self.content_location,
            "extracted_statement": self.extracted_statement,
            "resolved_value": self.resolved_value,
        }

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.semantic_payload, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def verify_against(self, root: pathlib.Path) -> tuple[str, ...]:
        """Whether the stored bytes are still the bytes this artifact names."""
        held = pathlib.Path(root) / self.content_location
        if not held.is_file():
            return (f"{self.content_location}: the pinned document is missing",)
        actual = hashlib.sha256(held.read_bytes()).hexdigest()
        if actual != self.document_sha256:
            return (
                f"{self.content_location}: hashes to {actual[:12]}..., the "
                f"artifact names {self.document_sha256[:12]}...",
            )
        return ()

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload,
            "fingerprint": self.fingerprint,
            "notes": list(self.notes),
        }


#: **Empty, deliberately.** See the module docstring: the reachable ThetaData
#: documentation is the v2 operation set, it says nothing about these three
#: conventions, and no path available here can produce a hash of the official
#: source bytes rather than of a rendering of them.
#:
#: An entry belongs here when somebody holds the bytes. Until then every
#: dimension below stays ``UNKNOWN``, and the first paid session is how several
#: of them get answered from responses rather than from prose.
PRODUCTION_VENDOR_DOCUMENTATION: Final[
    dict[DocumentedRule, VendorDocumentationArtifact]
] = {}


def register_vendor_documentation(
    artifact: VendorDocumentationArtifact,
    *,
    root: pathlib.Path,
    registry: dict[DocumentedRule, VendorDocumentationArtifact] | None = None,
) -> VendorDocumentationArtifact:
    """Pin a document, after checking that the bytes are the bytes it names.

    Verified at registration rather than at use: a registry that accepts an
    artifact and discovers at capture time that the file is missing has already
    let a session start on a claim it could not check.
    """
    problems = artifact.verify_against(root)
    if problems:
        raise VendorDocumentationError(
            f"{artifact.rule.value} cannot be registered: {list(problems)}"
        )
    target = PRODUCTION_VENDOR_DOCUMENTATION if registry is None else registry
    target[artifact.rule] = artifact
    return artifact


def resolve_documented_rule(
    rule: DocumentedRule,
    *,
    registry: dict[DocumentedRule, VendorDocumentationArtifact] | None = None,
) -> VendorDocumentationArtifact | None:
    """The pinned artifact for a rule, or ``None`` when nothing is pinned.

    ``None`` means *unresolved*, and every caller must treat it that way. It
    does not mean "the default", and there is no default: a convention nobody
    has a document for is a convention this repository does not know.
    """
    held = PRODUCTION_VENDOR_DOCUMENTATION if registry is None else registry
    return held.get(rule)


def store_document(body: bytes, *, root: pathlib.Path) -> tuple[str, str]:
    """Write a document content-addressed and return ``(digest, location)``.

    Content-addressed so the location *is* the digest: a file that no longer
    hashes to its own name has stopped being the document it claims to be, and
    there is nowhere for a second version to hide.
    """
    digest = hashlib.sha256(body).hexdigest()
    location = f"{digest[:2]}/{digest}.bin"
    target = pathlib.Path(root) / location
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(body)
    return digest, location
