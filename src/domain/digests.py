"""One place that decides how long a digest is, and why.

Every fingerprint in this repository is a SHA-256. Until v2.1.8 most of them
were then truncated to sixteen hex characters, with a comment saying collision
resistance was not the point -- the digest answered "did the assumptions
change?", and sixty-four bits is plenty for that.

That reasoning was right when the digests were *descriptions*. It stopped being
right once they became *bindings*. A capture is now refused when its stamped
pipeline fingerprint differs from the current one; a chain is refused when its
canonical hash differs from the re-derived one. Those are equality checks that
decide whether a number may be called trusted, and an equality check on a
truncated digest is a weaker statement than the same check on the whole thing --
for no saving worth having, because nobody reads these values.

So: **full digests for anything compared, short forms only for display.**
``short_id`` exists to make the second case explicit at the call site, rather
than leaving a bare ``[:16]`` for a later reader to classify.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = [
    "DISPLAY_DIGEST_CHARS",
    "digest_of",
    "short_id",
]

#: How much of a digest a human-facing string carries. Enough to tell two
#: values apart in a log line or a filename; never enough to compare.
DISPLAY_DIGEST_CHARS = 16


def digest_of(payload: Any) -> str:
    """Full SHA-256 over a canonically serialised payload.

    Sorted keys and tight separators, so the digest depends on values rather
    than on dict iteration order or whitespace.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def short_id(full_digest: str) -> str:
    """The display prefix of a full digest.

    For log lines, filenames and error messages. **Never** for equality,
    capture binding or verification -- if a comparison needs a digest, it needs
    the whole one.
    """
    return full_digest[:DISPLAY_DIGEST_CHARS]
