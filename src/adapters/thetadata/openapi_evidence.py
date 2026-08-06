"""The vendor's own OpenAPI document, pinned, and read by path rather than by hand.

v2.1.17 bound a convention to a document through two free-text fields:
``extracted_statement`` and ``resolved_value``. Both were supplied by whoever
built the artifact. So the digest proved *which bytes were held* and proved
nothing about *what was read out of them*: a genuine hash of the real document
would have carried the sentence "open interest settles same-session" just as
happily, because nothing ever opened the file to check.

Here an extraction is a **path into the parsed document** plus the fragment that
path is required to contain, and the value is derived from the text found there
by a named normalizer. Nobody passes a sentence in. A fabricated statement
attached to the correct document hash fails, because the statement is not an
input -- there is no argument through which one could be supplied.

The document is ``https://docs.thetadata.us/openapiv3.yaml``, fetched once over
HTTPS from the public documentation host and stored content-addressed under
``vendor_documentation/``. The digest is over the **exact response body bytes**:
not a markdown rendering, not a reserialization of the parsed YAML, not a
summary, not copied page text. A reserialization in particular would pin
PyYAML's output formatting and call it the vendor's document.

**No paid market data was requested to build this, and the Theta Terminal was
not contacted.** The public documentation host is a static file server; an
OpenAPI description is not market data. Three separate things stay separate
throughout this repository: fetching public documentation, requesting paid
market data, and calling the local Terminal. Only the first has ever happened.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Final

from src.adapters.thetadata.vendor_documentation import (
    DocumentedRule,
    VendorDocumentationError,
)

__all__ = [
    "OPENAPI_EXTRACTOR_VERSION",
    "PRODUCTION_EXTRACTION_SPECS",
    "PRODUCTION_PINNED_DOCUMENT",
    "VENDOR_DOCUMENTATION_BUNDLE_SCHEMA_VERSION",
    "VENDOR_DOCUMENTATION_ROOT",
    "ExtractionSpec",
    "OpenApiEvidenceExtraction",
    "OpenApiExtractionError",
    "PinnedDocument",
    "VendorDocumentationBundle",
    "load_vendor_documentation_bundle",
    "production_bundle",
]

#: Bumped when what a bundle must carry changes.
VENDOR_DOCUMENTATION_BUNDLE_SCHEMA_VERSION = "vendor-documentation-bundle/2.1.18"

#: Bumped when *how a value is read out of the document* changes. Separate from
#: the schema on purpose: the same bytes read under different normalizers yield
#: different claims, and a reader must be able to tell which reading produced
#: the artifact in front of them.
OPENAPI_EXTRACTOR_VERSION = "openapi-evidence-extractor/2.1.18"

#: Where pinned vendor documents live, relative to the repository root.
VENDOR_DOCUMENTATION_ROOT: Final = "vendor_documentation"


class OpenApiExtractionError(VendorDocumentationError):
    """A document that cannot support the reading being asked of it."""


# -- the pinned document ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PinnedDocument:
    """One fetch of one document, recorded so a later reader can repeat it.

    Every field is something the fetch itself produced. ``byte_length`` and
    ``document_sha256`` are both here although the digest implies the length:
    a truncated body is the failure mode most likely to still parse, and a
    length that disagrees with the stored file is a cheaper signal than a hash
    mismatch is to explain.
    """

    source_url: str
    retrieved_at: datetime
    http_status: int
    content_type: str
    byte_length: int
    document_sha256: str
    #: Relative to :data:`VENDOR_DOCUMENTATION_ROOT`. Content-addressed, so the
    #: location is derivable from the digest and cannot drift away from it.
    content_location: str
    #: The ``openapi:`` version the document declares about itself.
    document_schema_version: str

    def __post_init__(self) -> None:
        digest = str(self.document_sha256).strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise OpenApiExtractionError(
                f"document_sha256 {self.document_sha256!r} is not a full "
                "SHA-256. A pinned document whose digest cannot be checked is a "
                "citation."
            )
        if self.http_status != 200:
            raise OpenApiExtractionError(
                f"{self.source_url} answered {self.http_status}. Only a 200 "
                "carries a document; a redirect or an error page hashes just as "
                "well and says nothing."
            )
        if self.byte_length <= 0:
            raise OpenApiExtractionError(
                f"{self.source_url} returned {self.byte_length} bytes. An empty "
                "body is not a document, and its digest is the digest of "
                "nothing."
            )
        if not str(self.source_url).startswith("https://"):
            raise OpenApiExtractionError(
                f"{self.source_url!r} is not an https URL. A document fetched "
                "over a channel nobody authenticated is a document somebody "
                "else could have written."
            )

    def path_under(self, root: pathlib.Path) -> pathlib.Path:
        return pathlib.Path(root) / self.content_location

    def read_bytes(self, root: pathlib.Path) -> bytes:
        """The stored bytes, after checking they are still the bytes named.

        Rereads and rehashes on every load rather than trusting the filename.
        Content addressing makes a swapped file *detectable*; it does not make
        it impossible, and a bundle that trusted its own directory listing would
        be pinning a path instead of a document.
        """
        target = self.path_under(root)
        if not target.is_file():
            raise OpenApiExtractionError(
                f"{self.content_location} is missing under {root}. The bundle "
                "names a document this checkout does not hold."
            )
        body = target.read_bytes()
        if len(body) != self.byte_length:
            raise OpenApiExtractionError(
                f"{self.content_location} is {len(body)} bytes; the pin names "
                f"{self.byte_length}"
            )
        actual = hashlib.sha256(body).hexdigest()
        if actual != self.document_sha256:
            raise OpenApiExtractionError(
                f"{self.content_location} hashes to {actual}; the pin names "
                f"{self.document_sha256}. The document has been replaced, and "
                "every value read out of it is about a different document."
            )
        return body

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at.isoformat(),
            "http_status": self.http_status,
            "content_type": self.content_type,
            "byte_length": self.byte_length,
            "document_sha256": self.document_sha256,
            "content_location": self.content_location,
            "document_schema_version": self.document_schema_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return dict(self.semantic_payload())


#: **The official ThetaData v3 OpenAPI description, as fetched.**
#:
#: v2.1.17 recorded that these bytes could not be obtained and left the registry
#: empty on that basis. That was wrong: the document is served publicly at the
#: URL below, and this is the fetch that produced it. Everything here came off
#: the wire -- the status, the content type, the length and the digest are what
#: the response carried, not a description of it.
#:
#: Fetched twice, minutes apart, byte-identical both times. Two fetches do not
#: make the vendor's document stable and they do rule out the failure that would
#: be silent: a partial read hashing cleanly as a shorter document.
PRODUCTION_PINNED_DOCUMENT: Final = PinnedDocument(
    source_url="https://docs.thetadata.us/openapiv3.yaml",
    retrieved_at=datetime.fromisoformat("2026-08-06T14:36:13+00:00"),
    http_status=200,
    content_type="application/octet-stream",
    byte_length=812792,
    document_sha256=(
        "1b65f93c879a5ca4477a0ff9177235138e0c81840e0c7dddfbd9e34164b40b50"
    ),
    content_location=(
        "thetadata/1b/"
        "1b65f93c879a5ca4477a0ff9177235138e0c81840e0c7dddfbd9e34164b40b50.yaml"
    ),
    document_schema_version="3.1.0",
)


# -- normalizers --------------------------------------------------------------
#
# Each one reads the text it is given and returns a typed value, or refuses.
# None of them returns a constant: a normalizer that ignored its argument would
# make the whole mechanism decorative, because a document saying the opposite
# would normalize to the same answer.


def _normalize_settlement(text: str) -> Any:
    """Which session the vendor says an open-interest figure belongs to."""
    from src.domain.settlement import SettlementRuleKind

    lowered = " ".join(text.lower().split())
    prior = any(
        phrase in lowered
        for phrase in ("previous trading day", "prior trading day", "previous session")
    )
    same = any(
        phrase in lowered
        for phrase in ("current trading day", "same trading day", "intraday updates")
    )
    if prior and same:
        raise OpenApiExtractionError(
            "the description names both a previous and a current trading "
            f"session: {text!r}. An ambiguous source settles nothing, and "
            "picking the reading we expected is how a document comes to confirm "
            "whatever it was consulted to confirm."
        )
    if prior:
        return SettlementRuleKind.PRIOR_TRADING_SESSION
    if same:
        return SettlementRuleKind.SAME_SESSION
    raise OpenApiExtractionError(
        f"no settlement session is stated in {text!r}. The convention stays "
        "unresolved rather than defaulting: open interest is a linear weight on "
        "every GEX term, and attributing it to the wrong session misstates "
        "every strike."
    )


def _normalize_rate_units(text: str) -> Any:
    """Whether the vendor wants ``rate_value`` as a percent or as a fraction."""
    from src.config.pipeline import RateUnit

    lowered = " ".join(text.lower().split())
    percent = "as a percent" in lowered or "in percent" in lowered
    decimal = "as a decimal" in lowered or "as a fraction" in lowered
    if percent and decimal:
        raise OpenApiExtractionError(
            f"the description names both percent and decimal units: {text!r}"
        )
    if percent:
        return RateUnit.PERCENT_ANNUAL_RATE
    if decimal:
        return RateUnit.DECIMAL_ANNUAL_RATE
    raise OpenApiExtractionError(
        f"no rate unit is stated in {text!r}. Guessing here is a factor of one "
        "hundred in every gamma."
    )


_FLOOR_PATTERN: Final = re.compile(
    r"minimum of\s+(?:(?P<value>\d+(?:\.\d+)?)\s*)?(?P<unit>hours?|minutes?)",
    re.IGNORECASE,
)


def _normalize_time_floor(text: str) -> Any:
    """The floor the vendor applies to time-to-expiry, in minutes."""
    match = _FLOOR_PATTERN.search(" ".join(text.split()))
    if match is None:
        raise OpenApiExtractionError(f"no minimum time to expiry is stated in {text!r}")
    quantity = float(match.group("value") or 1)
    minutes = quantity * (60.0 if match.group("unit").lower().startswith("hour") else 1)
    if minutes <= 0:
        raise OpenApiExtractionError(f"a floor of {minutes} minutes is not a floor")
    # Whole minutes, because that is the unit the model spec carries. A
    # fractional floor would be a different quantity wearing the same name.
    if minutes != int(minutes):
        raise OpenApiExtractionError(
            f"the documented floor is {minutes} minutes, which is not a whole "
            "number of minutes; the model spec carries integer minutes"
        )
    return int(minutes)


#: Normalizer name -> reader. Named rather than passed as a callable so a spec
#: stays *data*: a spec carrying a function could not be compared, hashed or
#: read out of a manifest, and "which reader ran" is part of what the extraction
#: hash has to cover.
NORMALIZERS: Final[dict[str, Callable[[str], Any]]] = {
    "settlement_session/1": _normalize_settlement,
    "rate_units/1": _normalize_rate_units,
    "minimum_time_floor_minutes/1": _normalize_time_floor,
}


# -- extraction ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractionSpec:
    """Where to look, what must be there, and which reader interprets it.

    Deliberately carries no value. A spec that named its own answer would be the
    v2.1.17 ``resolved_value`` field with more steps.
    """

    rule: DocumentedRule
    #: Keys to walk, in order, through the parsed document.
    yaml_path: tuple[str, ...]
    #: A fragment the text at that path must contain. This is the drift guard:
    #: the vendor can reorganise the document without renaming the path, and a
    #: normalizer reading a *different* description would answer confidently.
    expected_source_fragment: str
    normalizer: str

    def __post_init__(self) -> None:
        if not self.yaml_path:
            raise OpenApiExtractionError(
                f"{self.rule.value} names no yaml_path; an extraction with no "
                "path is a sentence with a hash stapled to it"
            )
        if not str(self.expected_source_fragment).strip():
            raise OpenApiExtractionError(
                f"{self.rule.value} names no expected_source_fragment. Without "
                "one the normalizer would read whatever now lives at that path "
                "and report it as the vendor's documented rule."
            )
        if self.normalizer not in NORMALIZERS:
            raise OpenApiExtractionError(
                f"{self.rule.value} names normalizer {self.normalizer!r}, which "
                f"is not defined. Known: {sorted(NORMALIZERS)}"
            )

    def resolve_text(self, document: Any) -> str:
        """The text at :attr:`yaml_path`, or a refusal naming where it stopped."""
        cursor: Any = document
        walked: list[str] = []
        for key in self.yaml_path:
            walked.append(key)
            if not isinstance(cursor, dict) or key not in cursor:
                raise OpenApiExtractionError(
                    f"{self.rule.value}: the document has nothing at "
                    f"{' / '.join(walked)}. The path is part of the evidence, "
                    "and a path that no longer resolves means the document has "
                    "been reorganised -- not that the old reading still holds."
                )
            cursor = cursor[key]
        if not isinstance(cursor, str) or not cursor.strip():
            raise OpenApiExtractionError(
                f"{self.rule.value}: {' / '.join(self.yaml_path)} holds "
                f"{type(cursor).__name__}, not readable text"
            )
        return cursor

    def extract(
        self, document: Any, *, document_sha256: str
    ) -> OpenApiEvidenceExtraction:
        """Read this rule out of a parsed document, or refuse to.

        The order matters. The fragment is checked *before* the normalizer runs,
        so a document that has drifted is reported as drift rather than being
        interpreted and reported as a different rule.
        """
        text = self.resolve_text(document)
        if self.expected_source_fragment not in " ".join(text.split()):
            raise OpenApiExtractionError(
                f"{self.rule.value}: {' / '.join(self.yaml_path)} does not "
                f"contain {self.expected_source_fragment!r}. What it says now "
                f"is {text!r}. The pinned reading is about text that is no "
                "longer there."
            )
        return OpenApiEvidenceExtraction(
            rule=self.rule,
            document_sha256=document_sha256,
            yaml_path=self.yaml_path,
            expected_source_fragment=self.expected_source_fragment,
            normalized_value=NORMALIZERS[self.normalizer](text),
            normalizer=self.normalizer,
        )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "rule": self.rule.value,
            "yaml_path": list(self.yaml_path),
            "expected_source_fragment": self.expected_source_fragment,
            "normalizer": self.normalizer,
        }


def _canonical(value: Any) -> Any:
    """A hashable rendering of a normalized value.

    Enums become their ``value``; everything else must already be a JSON
    primitive. Anything else is refused rather than stringified, because
    ``str()`` of an object is a rendering that changes when its ``__repr__``
    does, and the extraction hash would move without the evidence moving.
    """
    from enum import Enum

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise OpenApiExtractionError(
        f"a normalized value of type {type(value).__name__} cannot be hashed "
        "canonically"
    )


@dataclass(frozen=True)
class OpenApiEvidenceExtraction:
    """One typed value, read out of one pinned document, at one path.

    ``extraction_hash`` is computed here rather than accepted. It is a field
    because the task's shape calls for one and because it has to survive being
    written to a manifest and read back; it is recomputed in
    :meth:`__post_init__` and a supplied value that disagrees is refused. So a
    reader can treat it as derived even though it round-trips as data.
    """

    rule: DocumentedRule
    document_sha256: str
    yaml_path: tuple[str, ...]
    expected_source_fragment: str
    normalized_value: object
    normalizer: str = ""
    extractor_version: str = OPENAPI_EXTRACTOR_VERSION
    schema_version: str = VENDOR_DOCUMENTATION_BUNDLE_SCHEMA_VERSION
    extraction_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "yaml_path", tuple(self.yaml_path))
        computed = self._compute_hash()
        if self.extraction_hash and self.extraction_hash != computed:
            raise OpenApiExtractionError(
                f"{self.rule.value} carries extraction_hash "
                f"{self.extraction_hash} and its own contents hash to "
                f"{computed}. A hash that does not follow from the fields it "
                "covers certifies nothing."
            )
        object.__setattr__(self, "extraction_hash", computed)

    def _compute_hash(self) -> str:
        from src.domain.digests import digest_of

        return digest_of(
            {
                "schema_version": self.schema_version,
                "extractor_version": self.extractor_version,
                "normalizer": self.normalizer,
                "rule": self.rule.value,
                "document_sha256": self.document_sha256,
                "yaml_path": list(self.yaml_path),
                "expected_source_fragment": self.expected_source_fragment,
                "normalized_value": _canonical(self.normalized_value),
            }
        )

    @property
    def yaml_pointer(self) -> str:
        """The path as one readable string, for reports and refusals."""
        return " / ".join(self.yaml_path)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "extractor_version": self.extractor_version,
            "normalizer": self.normalizer,
            "rule": self.rule.value,
            "document_sha256": self.document_sha256,
            "yaml_path": list(self.yaml_path),
            "expected_source_fragment": self.expected_source_fragment,
            "normalized_value": _canonical(self.normalized_value),
            "extraction_hash": self.extraction_hash,
        }

    def as_dict(self) -> dict[str, Any]:
        return dict(self.semantic_payload())


#: **The three readings this repository takes from the official document.**
#:
#: Each path was confirmed against the pinned bytes. The fragments are quoted
#: from the vendor's text as it stands, typos included -- the open-interest
#: description reads "at the of the previous trading day", and matching a
#: corrected version of that sentence would be matching our own edit.
PRODUCTION_EXTRACTION_SPECS: Final[tuple[ExtractionSpec, ...]] = (
    ExtractionSpec(
        rule=DocumentedRule.OPEN_INTEREST_SETTLEMENT,
        yaml_path=(
            "paths",
            "/option/snapshot/open_interest",
            "get",
            "description",
        ),
        expected_source_fragment="the open interest at the of the previous trading day",
        normalizer="settlement_session/1",
    ),
    ExtractionSpec(
        rule=DocumentedRule.RATE_UNITS,
        yaml_path=("components", "parameters", "rate_value", "description"),
        expected_source_fragment="The interest rate, as a percent",
        normalizer="rate_units/1",
    ),
    ExtractionSpec(
        rule=DocumentedRule.MINIMUM_TIME_FLOOR,
        yaml_path=("components", "parameters", "greeks_version", "description"),
        expected_source_fragment="down to a minimum of 1 hour",
        normalizer="minimum_time_floor_minutes/1",
    ),
)


# -- the bundle ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VendorDocumentationBundle:
    """Everything one document establishes, verified together or not at all.

    Replaces ``PRODUCTION_VENDOR_DOCUMENTATION``, which was a module-level
    ``dict`` any caller could write to. A mutable global is authority without a
    gate: an import side effect could add a rule, and a capture would open under
    it without anything having verified the bytes behind it.

    A bundle is produced only by :func:`load_vendor_documentation_bundle`, which
    rereads the document, rehashes it, rewalks every path and reruns every
    normalizer. There is no constructor argument for an extraction, so there is
    nothing to insert.
    """

    document: PinnedDocument
    extractions: tuple[OpenApiEvidenceExtraction, ...]
    extractor_version: str = OPENAPI_EXTRACTOR_VERSION
    schema_version: str = VENDOR_DOCUMENTATION_BUNDLE_SCHEMA_VERSION
    #: Set by the loader. A bundle assembled any other way carries no root and
    #: cannot be re-verified, which is what ``verify_against`` reports.
    verified_root: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extractions", tuple(self.extractions))
        seen: set[DocumentedRule] = set()
        for extraction in self.extractions:
            if extraction.rule in seen:
                raise OpenApiExtractionError(
                    f"{extraction.rule.value} is extracted twice. A rule with "
                    "two readings resolves to whichever one a lookup finds "
                    "first."
                )
            seen.add(extraction.rule)
            if extraction.document_sha256 != self.document.document_sha256:
                raise OpenApiExtractionError(
                    f"{extraction.rule.value} was read out of "
                    f"{extraction.document_sha256[:12]}... and the bundle pins "
                    f"{self.document.document_sha256[:12]}.... A bundle is one "
                    "document; mixing two would let a reading survive the "
                    "document it came from."
                )

    def extraction_for(self, rule: DocumentedRule) -> OpenApiEvidenceExtraction | None:
        """The reading for a rule, or ``None`` when the document settles none.

        ``None`` means *unresolved* and every caller must treat it that way. It
        is not a default, and there is no default.
        """
        for extraction in self.extractions:
            if extraction.rule is rule:
                return extraction
        return None

    def value_for(self, rule: DocumentedRule) -> Any:
        extraction = self.extraction_for(rule)
        return None if extraction is None else extraction.normalized_value

    @property
    def rules(self) -> tuple[DocumentedRule, ...]:
        return tuple(e.rule for e in self.extractions)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "extractor_version": self.extractor_version,
            "document": self.document.semantic_payload(),
            "extractions": [e.semantic_payload() for e in self.extractions],
        }

    @property
    def bundle_hash(self) -> str:
        """One value covering the document, every path and every reading.

        This is what binds elsewhere. A capture stamped with it cannot verify
        under a bundle built from different bytes, a moved path or a changed
        normalizer, because all three are inside the hash.
        """
        from src.domain.digests import digest_of

        return digest_of(self.semantic_payload())

    def verify_against(self, root: pathlib.Path) -> tuple[str, ...]:
        """Whether this bundle still follows from the bytes on disk.

        Re-extracts rather than comparing recorded fields: comparing what the
        bundle says against what the bundle says would pass on any bundle.
        """
        try:
            rebuilt = load_vendor_documentation_bundle(
                root=root,
                document=self.document,
                specs=tuple(
                    ExtractionSpec(
                        rule=e.rule,
                        yaml_path=e.yaml_path,
                        expected_source_fragment=e.expected_source_fragment,
                        normalizer=e.normalizer,
                    )
                    for e in self.extractions
                ),
            )
        except VendorDocumentationError as error:
            return (str(error),)
        if rebuilt.bundle_hash != self.bundle_hash:
            return (
                f"the document under {root} re-extracts to bundle "
                f"{rebuilt.bundle_hash[:12]}...; this bundle is "
                f"{self.bundle_hash[:12]}...",
            )
        return ()

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "bundle_hash": self.bundle_hash,
            "verified_root": self.verified_root,
        }


def load_vendor_documentation_bundle(
    *,
    root: pathlib.Path | str,
    document: PinnedDocument = PRODUCTION_PINNED_DOCUMENT,
    specs: tuple[ExtractionSpec, ...] = PRODUCTION_EXTRACTION_SPECS,
) -> VendorDocumentationBundle:
    """Reread the document and rederive every reading. The only way to a bundle.

    Everything is redone on every call: the bytes are reread, the digest is
    recomputed, the YAML is reparsed, each path is rewalked, each expected
    fragment is rechecked and each normalizer is rerun. Caching the result would
    save a few milliseconds once per process and would mean a bundle could
    outlive the file it describes.
    """
    import yaml

    holder = pathlib.Path(root)
    body = document.read_bytes(holder)
    try:
        parsed = yaml.safe_load(body.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as error:
        raise OpenApiExtractionError(
            f"{document.content_location} is not parseable YAML: {error}. The "
            "bytes are held and nothing can be read out of them."
        ) from error
    if not isinstance(parsed, dict) or not isinstance(parsed.get("paths"), dict):
        raise OpenApiExtractionError(
            f"{document.content_location} parsed, and it has no ``paths`` "
            "mapping. A document with no operations is not the API description "
            "this repository pinned."
        )
    declared = str(parsed.get("openapi", "")).strip()
    if declared != document.document_schema_version:
        raise OpenApiExtractionError(
            f"{document.content_location} declares OpenAPI {declared!r}; the "
            f"pin names {document.document_schema_version!r}"
        )
    return VendorDocumentationBundle(
        document=document,
        extractions=tuple(
            spec.extract(parsed, document_sha256=document.document_sha256)
            for spec in specs
        ),
        verified_root=str(holder),
    )


def production_bundle(
    *, root: pathlib.Path | str | None = None
) -> VendorDocumentationBundle:
    """The pinned production bundle, loaded from the repository checkout.

    Not a module-level constant: a constant would be built at import time, and
    an import that reads a file and can raise turns a missing document into an
    ``ImportError`` somewhere unrelated. A capture asks for the bundle and gets
    either a verified one or a refusal it can report.
    """
    if root is None:
        root = repository_documentation_root()
    return load_vendor_documentation_bundle(root=root)


def repository_documentation_root() -> pathlib.Path:
    """``vendor_documentation/`` in this checkout."""
    return pathlib.Path(__file__).resolve().parents[3] / VENDOR_DOCUMENTATION_ROOT


def bundle_document_reference(
    bundle: VendorDocumentationBundle,
) -> str:
    """The pinned document as a repository-relative path.

    ``DocumentationRule`` verifies its reference by opening it relative to the
    repository root, so the reference has to include the documentation root
    rather than being relative to it.
    """
    return f"{VENDOR_DOCUMENTATION_ROOT}/{bundle.document.content_location}"


# -- settlement -------------------------------------------------------------


#: How long the pinned document is taken to have been in force.
#:
#: ``effective_from`` is the retrieval moment, not an earlier date, and the
#: distinction is the whole point. The document describes what the vendor does
#: *now*; it says nothing about when the convention started. Backdating it to
#: make historical sessions resolve would be inventing coverage the source does
#: not provide, and open interest is a linear weight on every GEX term.
def settlement_documentation_rule(bundle: VendorDocumentationBundle) -> Any:
    """A registrable documentation rule, derived from the verified extraction.

    The settlement semantics come from
    :data:`DocumentedRule.OPEN_INTEREST_SETTLEMENT` as normalized out of the
    document. Nothing here chooses a rule kind; if the extraction is absent this
    refuses, because a settlement rule with no reading behind it is the
    caller-assumption case wearing an artifact's clothes.
    """
    from src.adapters.evidence_resolvers import DocumentationRule
    from src.domain.settlement import SettlementRule, SettlementRuleKind

    extraction = bundle.extraction_for(DocumentedRule.OPEN_INTEREST_SETTLEMENT)
    if extraction is None:
        raise OpenApiExtractionError(
            "the bundle settles no open-interest convention, so no settlement "
            "rule follows from it"
        )
    kind = extraction.normalized_value
    if not isinstance(kind, SettlementRuleKind):
        raise OpenApiExtractionError(
            f"the open-interest extraction normalized to {kind!r}, which is not "
            "a SettlementRuleKind"
        )
    return DocumentationRule(
        evidence_id="thetadata-openapiv3-open-interest-settlement",
        document_reference=bundle_document_reference(bundle),
        document_content_hash=bundle.document.document_sha256,
        rule_identifier=extraction.yaml_pointer,
        effective_from=bundle.document.retrieved_at.date(),
        rule=SettlementRule(kind=kind),
        derivation_version=extraction.extractor_version,
        observed_on=bundle.document.retrieved_at.date(),
    )


def verified_settlement_artifact(
    bundle: VendorDocumentationBundle, *, chain_session_date: Any
) -> Any:
    """The settlement authority a capture on this session opens under.

    Goes the long way round on purpose: the rule is registered (which reopens
    and rehashes the document), then *resolved* against the session date, then
    turned into an artifact that re-derives its own date in ``__post_init__``.
    Three independent chances to notice that the answer does not follow from the
    document, where v2.1.17's operator had none because it passed ``None``.
    """
    from src.adapters.evidence_resolvers import (
        DocumentationRuleRegistry,
        resolve_settlement_date,
        settlement_artifact_from,
    )
    from src.adapters.open_interest import EvidenceKind

    rule = settlement_documentation_rule(bundle)
    registry = DocumentationRuleRegistry()
    registry.register(rule)
    resolved = resolve_settlement_date(
        chain_session_date=chain_session_date,
        registry=registry,
        evidence_kind=EvidenceKind.AUTHORITATIVE_VENDOR_DOCUMENTATION,
        evidence_id=rule.evidence_id,
    )
    if not resolved.established:
        raise OpenApiExtractionError(
            f"the pinned document establishes no settlement date for "
            f"{chain_session_date}: {resolved.failure}"
        )
    return settlement_artifact_from(resolved, chain_session_date=chain_session_date)


# -- endpoint drift ---------------------------------------------------------


class DriftKind(str, Enum):
    """Why a documented endpoint and the local model disagree.

    ``NOT_DOCUMENTED`` is separate from ``CONFLICT`` deliberately. A document
    that is silent has not confirmed us and has not contradicted us, and
    collapsing the two would let silence read as agreement -- which is the shape
    of every defect this repository has spent four releases removing.
    """

    ENDPOINT_ABSENT = "ENDPOINT_ABSENT"
    TIER_CONFLICT = "TIER_CONFLICT"
    TIER_NOT_DOCUMENTED = "TIER_NOT_DOCUMENTED"
    FIELDS_CONFLICT = "FIELDS_CONFLICT"
    FIELDS_NOT_DOCUMENTED = "FIELDS_NOT_DOCUMENTED"


@dataclass(frozen=True, slots=True)
class EndpointDriftFinding:
    """One disagreement between the pinned document and this repository."""

    endpoint: str
    document_path: str
    kind: DriftKind
    detail: str

    @property
    def blocks_capture(self) -> bool:
        """Whether this finding must stop a session.

        A conflict blocks: the request we would send is described differently by
        the vendor's own document, and finding out during a paid session is the
        expensive way. Silence does not block -- it is reported and the
        corresponding dimension stays unresolved.
        """
        return self.kind in (
            DriftKind.ENDPOINT_ABSENT,
            DriftKind.TIER_CONFLICT,
            DriftKind.FIELDS_CONFLICT,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "document_path": self.document_path,
            "kind": self.kind.value,
            "detail": self.detail,
            "blocks_capture": self.blocks_capture,
        }


#: Vendor subscription names -> our tier names. The document says
#: ``professional``; the price list this repository was built from says ``pro``.
#: Mapped explicitly rather than by prefix so a new vendor tier is an error
#: rather than a near-match.
_DOCUMENTED_TIERS: Final[dict[str, str]] = {
    "free": "free",
    "value": "value",
    "standard": "standard",
    "professional": "pro",
}

#: The five requests the first raw-only session makes, as endpoint paths.
#:
#: Spelled out rather than derived from the capture plan: the plan chooses what
#: to request from a tier and a profile, and this is the set whose *description*
#: has to agree with the vendor's before any of it is requested. Deriving one
#: from the other would mean a profile change silently changed what got checked.
FIRST_SESSION_ENDPOINTS: Final[tuple[str, ...]] = (
    "/v3/index/snapshot/price",
    "/v3/option/snapshot/quote",
    "/v3/option/snapshot/open_interest",
    "/v3/option/snapshot/greeks/first_order",
    "/v3/option/list/contracts/quote",
)

#: Local endpoint -> the path template that serves it, where the two differ.
#: ``/v3/option/list/contracts/quote`` is one instantiation of a templated
#: operation; comparing it literally would report the endpoint as absent from a
#: document that describes it perfectly well.
_DOCUMENT_PATH_OVERRIDES: Final[dict[str, str]] = {
    "/v3/option/list/contracts/quote": "/option/list/contracts/{request_type}",
}


def _server_base_path(parsed: dict[str, Any]) -> str:
    """The path prefix the document's own server URL carries.

    Read out of the document rather than hardcoded as ``/v3``: the local
    ``Endpoint`` values include the prefix and the document's paths do not, and
    which of the two is right about the prefix is a question the document
    answers about itself.
    """
    from urllib.parse import urlparse

    servers = parsed.get("servers") or []
    if not servers or not isinstance(servers[0], dict):
        return ""
    return urlparse(str(servers[0].get("url", ""))).path.rstrip("/")


def endpoint_drift(
    *,
    root: pathlib.Path | str,
    document: PinnedDocument = PRODUCTION_PINNED_DOCUMENT,
    endpoints: tuple[Any, ...] | None = None,
) -> tuple[EndpointDriftFinding, ...]:
    """Check the first-session endpoints against the pinned document.

    Two questions per endpoint: does the vendor document the same minimum
    subscription tier we model, and does it document the same CSV response
    fields. Both are things a paid session would otherwise discover -- a tier
    that is too low is refused at the vendor, and a field list that has moved is
    the v2.1.16 index defect returning under a different column name.
    """
    import yaml

    from src.adapters.thetadata.endpoints import (
        MINIMUM_TIER,
        RESPONSE_FIELDS,
        Endpoint,
    )

    if endpoints is None:
        endpoints = FIRST_SESSION_ENDPOINTS
    parsed = yaml.safe_load(document.read_bytes(pathlib.Path(root)).decode("utf-8"))
    prefix = _server_base_path(parsed)
    paths = parsed.get("paths") or {}
    findings: list[EndpointDriftFinding] = []

    for endpoint in endpoints:
        local = Endpoint(endpoint)
        document_path = _DOCUMENT_PATH_OVERRIDES.get(local.value)
        if document_path is None:
            document_path = local.value
            if prefix and document_path.startswith(prefix):
                document_path = document_path[len(prefix) :]
        item = paths.get(document_path)
        if not isinstance(item, dict):
            findings.append(
                EndpointDriftFinding(
                    endpoint=local.value,
                    document_path=document_path,
                    kind=DriftKind.ENDPOINT_ABSENT,
                    detail=(
                        f"the pinned document describes no operation at "
                        f"{document_path}. The first session would request an "
                        "endpoint the vendor's own description does not carry."
                    ),
                )
            )
            continue

        findings.extend(
            _tier_findings(
                local=local,
                item=item,
                document_path=document_path,
                expected=MINIMUM_TIER.get(local),
            )
        )
        findings.extend(
            _field_findings(
                local=local,
                item=item,
                document_path=document_path,
                expected=RESPONSE_FIELDS.get(local),
            )
        )
    return tuple(findings)


def _tier_findings(
    *, local: Any, item: dict[str, Any], document_path: str, expected: Any
) -> list[EndpointDriftFinding]:
    documented = item.get("x-min-subscription")
    if documented is None:
        return [
            EndpointDriftFinding(
                endpoint=local.value,
                document_path=document_path,
                kind=DriftKind.TIER_NOT_DOCUMENTED,
                detail=(
                    f"{document_path} states no minimum subscription, so the "
                    f"modelled tier {getattr(expected, 'value', expected)!r} is "
                    "unconfirmed rather than agreed"
                ),
            )
        ]
    mapped = _DOCUMENTED_TIERS.get(str(documented).strip().lower())
    if mapped is None:
        return [
            EndpointDriftFinding(
                endpoint=local.value,
                document_path=document_path,
                kind=DriftKind.TIER_CONFLICT,
                detail=(
                    f"{document_path} requires subscription {documented!r}, "
                    f"which is not a tier this repository models "
                    f"({sorted(_DOCUMENTED_TIERS)})"
                ),
            )
        ]
    if expected is None or mapped != expected.value:
        return [
            EndpointDriftFinding(
                endpoint=local.value,
                document_path=document_path,
                kind=DriftKind.TIER_CONFLICT,
                detail=(
                    f"{document_path} documents minimum subscription "
                    f"{documented!r} ({mapped}); this repository models "
                    f"{getattr(expected, 'value', expected)!r}"
                ),
            )
        ]
    return []


def _documented_csv_fields(item: dict[str, Any]) -> tuple[str, ...] | None:
    """The CSV column order the document declares for a 200 response."""
    schema = (
        item.get("get", {})
        .get("responses", {})
        .get("200", {})
        .get("content", {})
        .get("text/csv", {})
        .get("schema", {})
    )
    properties = (schema.get("items") or {}).get("properties")
    if not isinstance(properties, dict) or not properties:
        return None
    return tuple(properties)


def _field_findings(
    *, local: Any, item: dict[str, Any], document_path: str, expected: Any
) -> list[EndpointDriftFinding]:
    documented = _documented_csv_fields(item)
    if documented is None:
        return [
            EndpointDriftFinding(
                endpoint=local.value,
                document_path=document_path,
                kind=DriftKind.FIELDS_NOT_DOCUMENTED,
                detail=(
                    f"{document_path} declares no text/csv response schema, so "
                    "the modelled column list is unconfirmed"
                ),
            )
        ]
    if expected is None:
        return [
            EndpointDriftFinding(
                endpoint=local.value,
                document_path=document_path,
                kind=DriftKind.FIELDS_CONFLICT,
                detail=(
                    f"{document_path} documents columns {list(documented)} and "
                    "this repository models none for that endpoint"
                ),
            )
        ]
    if tuple(expected) != documented:
        missing = [f for f in documented if f not in expected]
        extra = [f for f in expected if f not in documented]
        difference = (
            f"documented but not modelled: {missing}; modelled but not "
            f"documented: {extra}"
            if (missing or extra)
            else f"same columns in a different order: documented {list(documented)}"
        )
        return [
            EndpointDriftFinding(
                endpoint=local.value,
                document_path=document_path,
                kind=DriftKind.FIELDS_CONFLICT,
                detail=f"{document_path} -- {difference}",
            )
        ]
    return []
