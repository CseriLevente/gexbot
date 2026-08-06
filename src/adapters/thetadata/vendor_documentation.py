"""What a vendor document is allowed to settle, and the storage behind it.

Several conventions this repository calls ``UNKNOWN`` are things the vendor
*documents*. Leaving them unknown because no live response has been captured
confuses two different reasons for not knowing: "nobody has looked" and "the
source does not say". The first is fixable by reading.

But a convention bound to a document nobody can re-check is not evidence, it is
a sentence somebody typed. So a binding requires the document's exact bytes,
hashed, stored content-addressed, with the URL and the moment it was retrieved.
A later reader can fetch the same URL, hash what they get, and see whether the
ground has moved.

**The official document is pinned.** It is served publicly at
``https://docs.thetadata.us/openapiv3.yaml``; the bytes are in this repository
under ``vendor_documentation/`` and the readings taken from them live in
:mod:`src.adapters.thetadata.openapi_evidence`. v2.1.17 concluded the source
bytes were unobtainable and left its registry empty on that basis, which was
wrong about the fact and therefore wrong about the state.

This module holds only what a *reading* needs to exist: the closed set of rules
a document may settle, the error raised when one cannot be trusted, and
content-addressed storage. It deliberately no longer holds a registry. The
registry it used to hold was a module-level ``dict`` that any caller could write
to, which is authority without a gate -- and nothing in the pipeline ever read
it, so an entry would have changed no behaviour anyway. What replaced it is
:class:`~src.adapters.thetadata.openapi_evidence.VendorDocumentationBundle`,
which is immutable, rederived from the bytes on every load, and consumed by the
capture path.

Three things stay separate here and everywhere: fetching public documentation,
requesting paid market data, and calling the local Theta Terminal. Only the
first has ever happened in this repository.
"""

from __future__ import annotations

import hashlib
import pathlib
from enum import Enum

__all__ = [
    "VENDOR_DOCUMENTATION_EXTRACTOR_VERSION",
    "VENDOR_DOCUMENTATION_SCHEMA_VERSION",
    "DocumentedRule",
    "VendorDocumentationError",
    "store_document",
]

#: Bumped when what a documentation artifact must carry changes.
VENDOR_DOCUMENTATION_SCHEMA_VERSION = "vendor-documentation/2.1.18"

#: Bumped when *how a statement is read out of a document* changes. Separate
#: from the schema: the same bytes read under different rules produce different
#: claims, and a reader must be able to tell which reading it is looking at.
VENDOR_DOCUMENTATION_EXTRACTOR_VERSION = "vendor-documentation-extractor/2.1.18"


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


def store_document(
    body: bytes, *, root: pathlib.Path, suffix: str = ".bin"
) -> tuple[str, str]:
    """Write a document content-addressed and return ``(digest, location)``.

    Content-addressed so the location *is* the digest: a file that no longer
    hashes to its own name has stopped being the document it claims to be, and
    there is nowhere for a second version to hide.

    The digest is over the bytes handed in. Callers must hand in the **exact
    response body** -- not a markdown rendering, not a reserialization of parsed
    YAML, not a summary. Hashing any of those pins our own paraphrase and files
    it under the vendor's name.
    """
    digest = hashlib.sha256(body).hexdigest()
    location = f"{digest[:2]}/{digest}{suffix}"
    target = pathlib.Path(root) / location
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(body)
    return digest, location
