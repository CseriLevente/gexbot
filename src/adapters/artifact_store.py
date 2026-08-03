"""Content-addressed storage for the artifacts a fingerprint names.

A fingerprint is a good way to say "this capture was opened under *that*
settlement rule". It is not a way to recover the rule.

v2.1.8 stamped ``open_interest_date_rule_fingerprint`` and
``expected_universe_fingerprint`` onto every record and stored neither object.
Replay therefore worked only while the caller still held the originals in
memory: a year later the digests would name artifacts nobody could produce, and
the only thing left to do with them would be to compare them against a
reconstruction — which is the reconstruction the digest was supposed to
authenticate.

So the artifacts are written next to the payloads, keyed by their own hash. A
content-addressed store has the property this needs: writing the same artifact
twice is idempotent, two different artifacts cannot collide onto one key, and
the key *is* the verification — a document that does not hash to its filename
did not come from here.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
from typing import Any

from src.adapters.errors import ThetaDataProvenanceError
from src.domain.digests import digest_of

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ArtifactKind",
    "ArtifactStore",
    "InMemoryArtifactStore",
    "artifact_key",
    "canonical_bytes",
]

#: Bumped when the *envelope* changes -- not when an artifact type does; those
#: carry their own schema versions inside.
ARTIFACT_SCHEMA_VERSION = "capture-artifact/2.1.10"


class ArtifactKind(str):
    """What an artifact is. A string subclass so it serialises transparently."""

    SETTLEMENT_RULE = "settlement_rule"
    EXPECTED_UNIVERSE = "expected_universe"
    DOCUMENTATION_EVIDENCE = "documentation_evidence"
    SCHEDULE_DERIVATION = "schedule_derivation"


#: Every kind this store will accept. An artifact of an unknown kind is a
#: document nobody has agreed the meaning of.
ARTIFACT_KINDS = frozenset(
    {
        ArtifactKind.SETTLEMENT_RULE,
        ArtifactKind.EXPECTED_UNIVERSE,
        ArtifactKind.DOCUMENTATION_EVIDENCE,
        ArtifactKind.SCHEDULE_DERIVATION,
    }
)


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """The one serialisation an artifact hash is taken over."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _envelope(
    kind: str, payload: dict[str, Any], *, sources: tuple[str, ...]
) -> dict[str, Any]:
    if kind not in ARTIFACT_KINDS:
        raise ThetaDataProvenanceError(
            f"{kind!r} is not an artifact kind; valid kinds are "
            f"{sorted(ARTIFACT_KINDS)}"
        )
    return {
        "envelope_version": ARTIFACT_SCHEMA_VERSION,
        "kind": kind,
        "source_references": sorted(sources),
        "payload": payload,
    }


def artifact_key(payload: dict[str, Any]) -> str:
    """The address an artifact is stored at: the digest of its own payload.

    Deliberately *not* the digest of the envelope. The records stamp an
    artifact's own hash -- ``SettlementDateRuleArtifact.artifact_hash``,
    ``ExpectedContractUniverse.universe_hash`` -- and a store keyed on anything
    else would mean the stamped digest could not be used to look the object up,
    which is the entire job. The envelope's kind and sources are metadata about
    the storage, not about the artifact.
    """
    return digest_of(payload)


class InMemoryArtifactStore:
    """A working artifact store that forgets everything when the process exits.

    Fully supported for tests and offline fixtures, and deliberately *not*
    durable: the same distinction ``InMemoryRawStore`` draws. An artifact that
    outlives nothing cannot support the replay it exists for.
    """

    durability = "TEST_ONLY_VOLATILE"

    def __init__(self) -> None:
        self._documents: dict[str, dict[str, Any]] = {}

    def put(
        self, kind: str, payload: dict[str, Any], *, sources: tuple[str, ...] = ()
    ) -> str:
        document = _envelope(kind, payload, sources=sources)
        key = artifact_key(payload)
        existing = self._documents.get(key)
        if existing is not None and existing != document:
            # Cannot happen with SHA-256 and is checked anyway: the whole point
            # of content addressing is that the key implies the content.
            raise ThetaDataProvenanceError(
                f"artifact {key} already holds different content"
            )
        self._documents[key] = document
        return key

    def get(self, key: str) -> dict[str, Any] | None:
        return self._documents.get(key)

    def payload_of(self, key: str) -> dict[str, Any] | None:
        document = self.get(key)
        return document["payload"] if document else None

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._documents))

    def __len__(self) -> int:
        return len(self._documents)

    def __contains__(self, key: object) -> bool:
        return key in self._documents


class ArtifactStore:
    """Artifacts on disk, one JSON document per hash.

    Sits beside the raw payloads rather than inside them, because an expected
    universe of 5,000 identities repeated on every record would be several
    megabytes of the same list. The records carry the digest; this holds the
    thing.
    """

    durability = "DURABLE_APPEND_ONLY"

    def __init__(self, root: pathlib.Path | str) -> None:
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> pathlib.Path:
        # Two-character shard, so a directory listing stays usable after a few
        # thousand artifacts. Same shape as the raw payload store.
        return self.root / key[:2] / f"{key}.json"

    def put(
        self, kind: str, payload: dict[str, Any], *, sources: tuple[str, ...] = ()
    ) -> str:
        document = _envelope(kind, payload, sources=sources)
        key = artifact_key(payload)
        target = self._path(key)
        if target.exists():
            return key
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: a half-written artifact that hashes to its own name would be
        # a lie with a checksum on it.
        handle, temporary = tempfile.mkstemp(dir=str(target.parent), suffix=".part")
        try:
            with open(handle, "wb") as stream:
                stream.write(canonical_bytes(document))
            pathlib.Path(temporary).replace(target)
        except BaseException:
            pathlib.Path(temporary).unlink(missing_ok=True)
            raise
        return key

    def get(self, key: str) -> dict[str, Any] | None:
        target = self._path(key)
        if not target.is_file():
            return None
        document: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
        actual = artifact_key(document["payload"])
        if actual != key:
            raise ThetaDataProvenanceError(
                f"artifact {key} hashes to {actual}; the stored document has "
                "changed since it was written, so it is not the artifact the "
                "capture was bound to"
            )
        return document

    def payload_of(self, key: str) -> dict[str, Any] | None:
        document = self.get(key)
        return document["payload"] if document else None

    def keys(self) -> tuple[str, ...]:
        return tuple(
            sorted(path.stem for path in self.root.rglob("*.json") if path.is_file())
        )

    def __len__(self) -> int:
        return len(self.keys())

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self._path(key).is_file()
