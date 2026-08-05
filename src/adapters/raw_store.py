"""Immutable raw-response store.

The audit trail is the *raw payload*, not the parsed object. A parser bug found
three months later can only be diagnosed against what the vendor actually sent,
and a parser fix can only be validated by re-running it over the original bytes.

Two properties are enforced:

* **Append-only.** A record with an existing id is never overwritten. Silently
  replacing a stored response would destroy the only copy of the evidence.
* **Content-addressed.** Every record carries a SHA-256 of the payload, so
  tampering or truncation is detectable and the replay test can assert that the
  same bytes went in.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from src.adapters.errors import ThetaDataRawStoreError

# Bumped when the parser's interpretation of a payload changes, so a stored
# record says which code read it.
#: The single definition. Bump this whenever parsing behaviour changes in a way
#: that could alter a parsed value -- duplicate resolution, integer parsing,
#: timestamp localisation, float classification. It participates in the replay
#: hash, so a parser change that quietly alters numbers cannot masquerade as the
#: same output.
#:
#: 2.1.1 changed: exact Decimal integer parsing (was float round-trip),
#: structured float parse issues, per-source timestamp localisation, and
#: assembly from deduplicated rows.
#:
#: 2.1.3 changed: structured parsing on every vendor float, CSV body
#: validation before concluding "no rows", Decimal strike identity, and
#: per-contract selected-source timestamp provenance. v2.1.2 made those
#: changes and left the version at 2.1.1, so a replay could not tell that
#: the parser had changed underneath it.
#:
#: 2.1.4 changed: the canonical contract identity is now spelled by
#: ``canonical_strike`` on both sides -- ``SPXW:2026-03-17:5000:C`` rather than
#: ``SPXW:2026-03-17:5000.0000:C``. The contract is the same contract and the
#: strike is the same number, so no value the parser reads has changed meaning;
#: but the identity string is the *join key* between an expected universe and a
#: received chain, and two versions spelling it differently would not match each
#: other's output. A replay across the boundary has to be able to see that.
#: 2.1.6 changed: one shared vendor-timestamp interpretation. The validator
#: previously read a naive vendor string as UTC while this parser localised it
#: to US Eastern, so the same bytes produced instants four hours apart depending
#: on which module read them. A replay across that boundary has to be able to
#: see that the reading changed.
# Moved in v2.1.15 because *how a stored payload becomes text* changed:
# replay consumes the exact bytes under the captured content type and charset
# rather than a UTF-8-with-replacement reading of them. Nothing about how rows
# become a gamma changed -- that is the engine version, and it has not moved.
PARSER_VERSION = "thetadata-v3-parser/2.1.15"

#: The manifest's own schema. Bumped when the *shape* of the evidence changes,
#: independently of how a payload is read: v2.1.6 replaced parallel arrays of
#: ids, hashes and request ids with per-record descriptors, so an older manifest
#: cannot be verified by this code and is refused rather than reinterpreted.
MANIFEST_SCHEMA_VERSION = "raw-capture-manifest/2.1.15"

#: What a stored raw payload *is*. Bumped when that changes, which it did in
#: v2.1.13 -- the store holds the response's entity bytes rather than a UTF-8
#: re-encoding of a lossily decoded string -- and again in v2.1.14, when the
#: statement stopped being an unused constant and started being written onto
#: every record, checked by the scanner, and covered by the manifest hash.
RAW_RESPONSE_SCHEMA_VERSION = "raw-response/2.1.15"

#: Which bytes those are: the HTTP entity body **after transfer- and
#: content-decoding**, so a gzip response is stored decompressed. That is the
#: layer a parser reads and the layer a re-derivation has to reproduce.
BODY_REPRESENTATION = "http-entity-body-after-content-decoding"

#: Raw-response schemas this code can interpret. A record written under another
#: one is refused rather than reinterpreted: v2.1.12 and earlier stored a UTF-8
#: re-encoding of decoded text, and reading those bytes as "the response" under
#: v2.1.14 rules would be exactly the silent reinterpretation this refuses.
SUPPORTED_RAW_RESPONSE_SCHEMAS = frozenset({RAW_RESPONSE_SCHEMA_VERSION})


#: Aliased onto the adapter hierarchy so that a caller catching
#: ThetaDataError catches store failures too. Defined in adapters.errors rather
#: than here because the store is used by more than one adapter, so it must not
#: depend on the ThetaData client.
RawStoreError = ThetaDataRawStoreError


class CaptureOrigin(str, Enum):
    """Where a stored response actually came from.

    v2.1.5 carried ``AdapterValidationReport.live_capture = False`` as a
    hard-coded constant. It was the right answer, and it was not an *answer*: it
    would have stayed False through the first real session, and nothing but the
    constant stood between an offline fixture and a certification claim about
    live vendor behaviour.

    The origin is stamped on each record by the transport that produced it, and
    it enters the manifest hash, so a fixture capture cannot be relabelled after
    the fact without the manifest saying so.
    """

    #: A deterministic in-process transport. Never evidence about the vendor.
    OFFLINE_FIXTURE = "OFFLINE_FIXTURE"
    #: A real HTTP round trip to the vendor.
    LIVE_HTTP_CAPTURE = "LIVE_HTTP_CAPTURE"
    #: A real round trip to a local Theta Terminal, which proxies the vendor.
    LOCAL_TERMINAL_CAPTURE = "LOCAL_TERMINAL_CAPTURE"
    #: A transport that does not say. Treated as not-live.
    UNKNOWN_ORIGIN = "UNKNOWN_ORIGIN"

    @property
    def is_live(self) -> bool:
        return self in (
            CaptureOrigin.LIVE_HTTP_CAPTURE,
            CaptureOrigin.LOCAL_TERMINAL_CAPTURE,
        )


class StoreDurability(str, Enum):
    """Whether a store survives the process that wrote to it.

    A paid session's only copy of the evidence cannot live in a dictionary.
    v2.1.5's readiness check probed for protocol compliance, integrity and a
    successful write -- all of which ``InMemoryRawStore`` passes, because it
    really is a working store. It just forgets everything when Python exits.
    """

    TEST_ONLY_VOLATILE = "TEST_ONLY_VOLATILE"
    DURABLE_APPEND_ONLY = "DURABLE_APPEND_ONLY"

    @property
    def survives_the_process(self) -> bool:
        return self is StoreDurability.DURABLE_APPEND_ONLY


#: How much room a capture needs before it is worth starting. A full SPX chain
#: with greeks is tens of megabytes; the floor is deliberately generous, because
#: running out of disk halfway through a paid session is the one failure that
#: cannot be retried for free.
DEFAULT_MINIMUM_FREE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CaptureIdentity:
    """Everything about *this repository* that was true when a response arrived.

    v2.1.6 put the pipeline fingerprint on the manifest and nowhere else. The
    manifest is a document assembled after the fact, so relabelling a capture as
    belonging to another pipeline was a one-field edit and the stored records --
    the actual evidence -- had nothing to say about it.

    These five values are decided when a capture session opens and stamped onto
    every record it writes. Verification then asks the *records* which pipeline
    they were captured under, which is a question a manifest cannot answer about
    itself.

    They travel together rather than as five parameters because they are one
    claim. A record stamped with four of them and not the fifth would be a
    record nobody could place.
    """

    session_id: str
    pipeline_fingerprint: str = ""
    capture_plan_fingerprint: str = ""
    #: Digest of the canonical query parameters this session intends to send,
    #: per endpoint. Binds a capture to the request that produced it, so a
    #: capture taken at ``rate_value=4.2`` cannot be relabelled as one taken at
    #: 3.1.
    request_spec_fingerprint: str = ""
    #: Digest of the recipe a normalized chain would be rebuilt under.
    normalization_recipe_fingerprint: str = ""
    #: Which transport produced the bytes. Never asserted by a caller.
    capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN
    #: Which *operation* issued this record, and the whole of what that
    #: operation fixed. v2.1.7 stamped the standing configuration and left the
    #: per-operation inputs -- above all the valuation instant -- unbound, so
    #: the chain under test could choose the timestamp it was checked against.
    operation_id: str = ""
    operation_fingerprint: str = ""
    #: The full recipe hash for *this* operation, parameters included. The
    #: ``normalization_recipe_fingerprint`` above is the rules alone.
    normalization_recipe_hash: str = ""
    requested_as_of: datetime | None = None
    effective_valuation_timestamp: datetime | None = None
    valuation_timestamp_rule: str = ""
    #: The universe this operation expected, and the rule establishing the
    #: open-interest settlement date. Both change what a chain *means* --
    #: completeness, confidence, and the weight on every GEX term -- so both are
    #: fixed by the operation rather than supplied at calculation time. v2.1.7
    #: took the expected universe as an argument to the calculation, so one
    #: capture could be scored MEASURED_COMPLETE and replayed PARTIALLY_OBSERVED.
    expected_universe_fingerprint: str = ""
    open_interest_date_rule_fingerprint: str = ""
    #: The remaining field the operation digest covers. Stored explicitly since
    #: v2.1.9 so ``verify_capture`` can recompute that digest from the record
    #: rather than comparing stored digests to each other and calling it checked.
    spot_synchronization_policy_fingerprint: str = ""

    @property
    def names_an_operation(self) -> bool:
        """Whether this identity says which operation issued the record."""
        return bool(self.operation_id and self.operation_fingerprint)

    @property
    def complete(self) -> bool:
        """Whether every claim was actually made. An empty one is not a claim."""
        return all(
            (
                self.session_id,
                self.pipeline_fingerprint,
                self.capture_plan_fingerprint,
                self.request_spec_fingerprint,
                self.normalization_recipe_fingerprint,
                self.operation_id,
                self.operation_fingerprint,
                self.normalization_recipe_hash,
                self.requested_as_of is not None,
                self.effective_valuation_timestamp is not None,
                self.valuation_timestamp_rule,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "capture_session_id": self.session_id,
            "pipeline_fingerprint": self.pipeline_fingerprint,
            "capture_plan_fingerprint": self.capture_plan_fingerprint,
            "request_spec_fingerprint": self.request_spec_fingerprint,
            "normalization_recipe_fingerprint": self.normalization_recipe_fingerprint,
            "operation_id": self.operation_id,
            "operation_fingerprint": self.operation_fingerprint,
            "normalization_recipe_hash": self.normalization_recipe_hash,
            "requested_as_of": (
                self.requested_as_of.isoformat() if self.requested_as_of else None
            ),
            "effective_valuation_timestamp": (
                self.effective_valuation_timestamp.isoformat()
                if self.effective_valuation_timestamp
                else None
            ),
            "valuation_timestamp_rule": self.valuation_timestamp_rule,
            "expected_universe_fingerprint": self.expected_universe_fingerprint,
            "open_interest_date_rule_fingerprint": (
                self.open_interest_date_rule_fingerprint
            ),
            "spot_synchronization_policy_fingerprint": (
                self.spot_synchronization_policy_fingerprint
            ),
        }


@dataclass(frozen=True, slots=True)
class RawResponseRecord:
    """One vendor response, with everything needed to reproduce and audit it."""

    record_id: str
    endpoint: str
    query_params: dict[str, Any]
    request_started_at: datetime
    response_received_at: datetime
    http_status: int
    payload_hash: str
    payload_location: str
    parser_version: str = PARSER_VERSION
    vendor_schema_version: str | None = None
    byte_length: int = 0
    request_id: str = ""
    request_sequence: int = 0
    #: False when a write was interrupted before the atomic rename.
    capture_complete: bool = True
    #: Which transport produced this. Stamped at capture, never asserted later.
    capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN
    #: Which session wrote it, and what the repository looked like at the time.
    #: Stamped here so a manifest cannot claim a pipeline the bytes never saw.
    capture_session_id: str = ""
    pipeline_fingerprint: str = ""
    capture_plan_fingerprint: str = ""
    request_spec_fingerprint: str = ""
    normalization_recipe_fingerprint: str = ""
    #: Which capture operation issued this record. A session may run several.
    operation_id: str = ""
    operation_fingerprint: str = ""
    normalization_recipe_hash: str = ""
    requested_as_of: datetime | None = None
    effective_valuation_timestamp: datetime | None = None
    valuation_timestamp_rule: str = ""
    #: The universe this operation expected, and the rule establishing the
    #: open-interest settlement date. Both change what a chain *means* --
    #: completeness, confidence, and the weight on every GEX term -- so both are
    #: fixed by the operation rather than supplied at calculation time. v2.1.7
    #: took the expected universe as an argument to the calculation, so one
    #: capture could be scored MEASURED_COMPLETE and replayed PARTIALLY_OBSERVED.
    expected_universe_fingerprint: str = ""
    open_interest_date_rule_fingerprint: str = ""
    #: The remaining field the operation digest covers. Stored explicitly since
    #: v2.1.9 so ``verify_capture`` can recompute that digest from the record
    #: rather than comparing stored digests to each other and calling it checked.
    spot_synchronization_policy_fingerprint: str = ""
    #: What the stored bytes *are*, and under which rules. Written since v2.1.14;
    #: before that the constant existed and nothing recorded it, so a record gave
    #: no way to tell whether its digest covered entity bytes or a re-encoding of
    #: decoded text.
    raw_response_schema_version: str = RAW_RESPONSE_SCHEMA_VERSION
    body_representation: str = BODY_REPRESENTATION
    #: The allow-listed response headers, kept because several of them decide
    #: what the bytes mean and the rest are what an audit asks for.
    response_headers: Mapping[str, str] = field(default_factory=dict)
    #: How the *parser* read those bytes. Descriptive only: the bytes and their
    #: digest stay authoritative, and this says what one reading of them was.
    content_type: str = ""
    declared_charset: str = ""
    selected_charset: str = ""
    decode_status: str = ""
    decoded_text_hash: str = ""

    @property
    def interpretive_headers(self) -> dict[str, str]:
        """The retained headers that change how these bytes read.

        Content type and content encoding decide the charset and whether the
        body was compressed on the wire. A replay under a different one is a
        different reading of the same bytes, so they belong to the record's
        identity rather than beside it.
        """
        from src.adapters.http_attempts import INTERPRETIVE_RESPONSE_HEADERS

        held = {k.lower(): v for k, v in dict(self.response_headers).items()}
        return {
            name: held[name] for name in INTERPRETIVE_RESPONSE_HEADERS if name in held
        }

    @property
    def raw_response_semantics(self) -> dict[str, Any]:
        """The v2.1.14 block, as one thing, so it travels as one thing."""
        return {
            "payload_location": self.payload_location,
            "raw_response_schema_version": self.raw_response_schema_version,
            "body_representation": self.body_representation,
            "content_type": self.content_type,
            "declared_charset": self.declared_charset,
            "selected_charset": self.selected_charset,
            "decode_status": self.decode_status,
            "decoded_text_hash": self.decoded_text_hash,
            "response_headers": dict(sorted(self.response_headers.items())),
        }

    @property
    def capture_identity(self) -> CaptureIdentity:
        return CaptureIdentity(
            session_id=self.capture_session_id,
            pipeline_fingerprint=self.pipeline_fingerprint,
            capture_plan_fingerprint=self.capture_plan_fingerprint,
            request_spec_fingerprint=self.request_spec_fingerprint,
            normalization_recipe_fingerprint=self.normalization_recipe_fingerprint,
            capture_origin=self.capture_origin,
            operation_id=self.operation_id,
            operation_fingerprint=self.operation_fingerprint,
            normalization_recipe_hash=self.normalization_recipe_hash,
            requested_as_of=self.requested_as_of,
            effective_valuation_timestamp=self.effective_valuation_timestamp,
            valuation_timestamp_rule=self.valuation_timestamp_rule,
            expected_universe_fingerprint=self.expected_universe_fingerprint,
            open_interest_date_rule_fingerprint=(
                self.open_interest_date_rule_fingerprint
            ),
            spot_synchronization_policy_fingerprint=(
                self.spot_synchronization_policy_fingerprint
            ),
        )

    @property
    def round_trip_seconds(self) -> float:
        return (self.response_received_at - self.request_started_at).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "endpoint": self.endpoint,
            "query_params": dict(sorted(self.query_params.items())),
            "request_started_at": self.request_started_at.isoformat(),
            "response_received_at": self.response_received_at.isoformat(),
            "round_trip_seconds": self.round_trip_seconds,
            "http_status": self.http_status,
            "payload_hash": self.payload_hash,
            "parser_version": self.parser_version,
            "vendor_schema_version": self.vendor_schema_version,
            "byte_length": self.byte_length,
            "request_id": self.request_id,
            "request_sequence": self.request_sequence,
            "capture_complete": self.capture_complete,
            "capture_origin": self.capture_origin.value,
            **self.raw_response_semantics,
            **self.capture_identity.as_dict(),
        }


def _moment_or_none(value: Any) -> datetime | None:
    """An ISO timestamp read back from the index, or ``None``."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


#: Decode statuses this code knows how to reason about. A record claiming
#: anything else is not one this reader can interpret, and guessing is how a
#: reading becomes a fact.
KNOWN_DECODE_STATUSES = frozenset({"EXACT", "REPLACED", "SUPPLIED_AS_TEXT"})

#: Origins where the bytes came off a socket. ``SUPPLIED_AS_TEXT`` means "no
#: decoding happened because a caller handed us a string", which cannot be true
#: of a response that arrived over HTTP.
LIVE_CAPTURE_ORIGINS = frozenset({"LIVE_HTTP_CAPTURE", "LOCAL_TERMINAL_CAPTURE"})


def _decode_metadata_problem(payload: Mapping[str, Any]) -> str:
    """Whether the recorded decode metadata is even the right shape.

    Checked before the bytes are read, because a malformed claim about a
    payload is a reason to refuse the record rather than to compare against it.
    """
    status = str(payload.get("decode_status", "") or "")
    if status and status not in KNOWN_DECODE_STATUSES:
        return (
            f"decode_status {status!r} is not one this reader implements "
            f"({sorted(KNOWN_DECODE_STATUSES)})"
        )
    origin = str(payload.get("capture_origin", "") or "")
    if status == "SUPPLIED_AS_TEXT" and origin in LIVE_CAPTURE_ORIGINS:
        return (
            f"a {origin} record claims SUPPLIED_AS_TEXT, which means no bytes "
            "were decoded -- but these arrived over HTTP. An offline fixture may "
            "say this; a live capture may not."
        )
    digest = str(payload.get("decoded_text_hash", "") or "")
    if digest and (len(digest) != 64 or not _is_hex(digest)):
        return f"decoded_text_hash {digest!r} is not a full SHA-256 digest"
    if status in ("EXACT", "REPLACED") and not str(
        payload.get("selected_charset", "") or ""
    ):
        return f"decode_status is {status} but no charset was selected"
    for name in ("content_type", "declared_charset", "selected_charset"):
        value = str(payload.get(name, "") or "")
        if value and value != value.strip().lower():
            return f"{name} {value!r} is not canonicalized (lowercase, trimmed)"
    return ""


def _is_hex(text: str) -> bool:
    return all(character in "0123456789abcdef" for character in text)


def is_portable_location(location: str) -> bool:
    """Whether a recorded location survives the directory being moved.

    A scheme (``memory://``, ``null://``) names a store rather than a path and
    travels fine. Anything else must be relative: an absolute path is a fact
    about one machine's filesystem, and evidence that only verifies on the
    machine that produced it is not evidence anyone else can check.
    """
    if "://" in location:
        return True
    return not (
        location.startswith(("/", "\\"))
        or pathlib.PurePosixPath(location).is_absolute()
        or pathlib.PureWindowsPath(location).is_absolute()
    )


def _safe_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Allow-listed, lowercased, sorted. Never a credential.

    Filtered again here rather than trusted from the caller: a record is
    evidence, and evidence that retained whatever it was handed would be one
    careless call away from carrying an ``Authorization`` header into an
    archive.
    """
    from src.adapters.http_attempts import safe_headers

    return dict(sorted(safe_headers(headers).items()))


def _decode_drift(body: bytes, data: Mapping[str, Any]) -> str:
    """Where a record's decode metadata stops describing its own payload.

    Compares independently derived values against the stored ones. A record
    that supplied text is exempt from the *reading* comparison -- there was
    nothing to decode -- but its shape is still checked above.
    """
    from src.adapters.transport import decode_body

    status = str(data.get("decode_status", "") or "")
    if not status or status == "SUPPLIED_AS_TEXT":
        return ""
    headers = dict(data.get("response_headers", {}) or {})
    derived = decode_body(body, headers)

    if status != derived.decode_status.value:
        return (
            f"decode_status says {status!r}; these bytes under these headers "
            f"decode {derived.decode_status.value!r}"
        )
    for name, derived_value in (
        ("selected_charset", derived.selected_charset),
        ("declared_charset", derived.declared_charset),
        ("content_type", derived.content_type),
        ("decoded_text_hash", derived.decoded_text_hash),
    ):
        stated = str(data.get(name, "") or "")
        if stated and stated != derived_value:
            return f"{name} says {stated!r}; re-derives to {derived_value!r}"
    return ""


def _decode_fields(decode: Mapping[str, Any] | None) -> dict[str, Any]:
    """The parser's reading, as record fields.

    Descriptive only. The bytes and ``payload_hash`` stay authoritative; this
    says how one reader interpreted them, so a later disagreement about a
    charset is a comparison rather than an argument.
    """
    held = dict(decode or {})
    return {
        "content_type": str(held.get("content_type", "")),
        "declared_charset": str(held.get("declared_charset", "")),
        "selected_charset": str(held.get("selected_charset", "")),
        "decode_status": str(held.get("decode_status", "")),
        "decoded_text_hash": str(held.get("decoded_text_hash", "")),
    }


def payload_hash(payload: str | bytes) -> str:
    """SHA-256 over the *stored bytes*.

    Text is encoded as UTF-8 for the same result it always gave, so every
    existing fixture hashes identically. What changed is that a caller with real
    bytes can hand them over unaltered: v2.1.12 could only accept a string, and
    the string had already been through ``errors="replace"``.
    """
    if isinstance(payload, bytes):
        return hashlib.sha256(payload).hexdigest()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _as_body(payload: str | bytes) -> bytes:
    return payload if isinstance(payload, bytes) else payload.encode("utf-8")


def canonical_parameter_hash(params: Mapping[str, Any]) -> str:
    """Full SHA-256 of the request parameters, order-independent.

    Sorted keys, so the same effective request always produces the same digest
    however the query happened to be assembled.

    v2.1.6 truncated this to sixteen hex characters. Sixty-four bits is a great
    deal for an accident and not much for an audit identity, and there was no
    reason to economise: the value is compared, never read. The short form
    survives only in filenames, where length is a real constraint.
    """
    payload = json.dumps(
        {str(k): params[k] for k in sorted(params)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: How much of a digest a filename carries. Enough to keep record ids distinct
#: on a filesystem; never enough to be an audit identity on its own.
FILENAME_DIGEST_CHARS = 16


def short_digest(digest: str) -> str:
    """The filename-length prefix of a full digest. Display use only."""
    return digest[:FILENAME_DIGEST_CHARS]


def build_record_id(
    *,
    session_id: str,
    sequence: int,
    endpoint: str,
    query_params: Mapping[str, Any],
    payload: str | bytes,
) -> str:
    """Collision-safe, deterministic, filesystem-safe record id.

    Includes the sequence *and* the parameter hash: the sequence distinguishes
    two identical requests in one session, the parameter hash distinguishes two
    different requests to the same endpoint, and the payload hash lets a reader
    spot when the same bytes came back twice.
    """
    safe_session = _safe_component(session_id)
    safe_endpoint = _safe_component(endpoint.strip("/").replace("/", "-"))
    return (
        f"{safe_session}-{sequence:04d}-{safe_endpoint}"
        f"-{short_digest(canonical_parameter_hash(query_params))}"
        f"-{payload_hash(payload)[:12]}"
    )


def record_id_belongs_to(record_id: str, session_id: str) -> bool:
    """Whether a record id was minted by this capture session.

    The session id is the first component of every id ``build_record_id``
    produces, which makes the association checkable rather than asserted: a
    manifest that claims a session cannot then list records another session
    wrote.
    """
    return record_id.startswith(f"{_safe_component(session_id)}-")


#: Every field an index entry must carry to be interpretable at all.
REQUIRED_METADATA_FIELDS = (
    "record_id",
    "endpoint",
    "payload_hash",
    "byte_length",
    "payload_location",
    "parser_version",
    "request_started_at",
    "response_received_at",
    "capture_complete",
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: Parser versions this code can interpret. A record written by a different
#: parser is not evidence this scanner can read, and pretending otherwise is how
#: a changed interpretation becomes invisible to a replay.
SUPPORTED_PARSER_VERSIONS = frozenset({PARSER_VERSION})


def new_capture_session_id(*, as_of: datetime | None = None) -> str:
    """A session id that cannot collide, with market time as audit metadata.

    v2.1.1 built this from the market ``as_of`` alone::

        f"{as_of.date().isoformat()}-{as_of.strftime('%H%M%S')}"

    Two captures at the same market timestamp -- a retry, a second symbol, a
    re-run of the same historical instant -- produced the same id. The store is
    append-only, so the second one raised, and the failure looked like a storage
    bug rather than an identity bug.

    Market time stays in the id because it is genuinely useful when reading a
    directory listing. It is not what makes the id unique; the nonce is. A
    restarted process cannot reuse an id because the nonce is drawn fresh.
    """
    stamp = (as_of or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}__{uuid.uuid4().hex[:12]}"


def validate_metadata(payload: Any) -> tuple[IntegrityStatus | None, str]:
    """Check an index entry BEFORE anything is derived from it.

    v2.1.1 read ``data["record_id"]`` and immediately resolved a filesystem path
    from it. Malformed metadata therefore crashed the scanner whose entire
    purpose is to report malformed metadata -- and a hostile or corrupt
    ``record_id`` got as far as path resolution.

    Returns ``(None, "")`` when the entry is well-formed.
    """
    if not isinstance(payload, dict):
        return IntegrityStatus.INVALID_METADATA, (
            f"expected a JSON object, got {type(payload).__name__}"
        )

    missing = [f for f in REQUIRED_METADATA_FIELDS if f not in payload]
    if missing:
        return IntegrityStatus.INVALID_METADATA, f"missing field(s) {sorted(missing)}"

    record_id = payload["record_id"]
    if not isinstance(record_id, str) or not record_id.strip():
        return IntegrityStatus.UNSAFE_RECORD_ID, (
            f"record_id must be a non-empty string, got {type(record_id).__name__}"
        )
    if not is_safe_record_id(record_id):
        return IntegrityStatus.UNSAFE_RECORD_ID, (
            "record_id contains path separators or traversal segments"
        )

    if not isinstance(payload["endpoint"], str):
        return IntegrityStatus.INVALID_METADATA, "endpoint must be a string"
    if not isinstance(payload["parser_version"], str):
        return IntegrityStatus.INVALID_METADATA, "parser_version must be a string"
    if not isinstance(payload["capture_complete"], bool):
        return IntegrityStatus.INVALID_METADATA, "capture_complete must be a boolean"

    byte_length = payload["byte_length"]
    if isinstance(byte_length, bool) or not isinstance(byte_length, int):
        return IntegrityStatus.INVALID_BYTE_LENGTH, (
            f"byte_length must be an integer, got {type(byte_length).__name__}"
        )
    if byte_length < 0:
        return (
            IntegrityStatus.INVALID_BYTE_LENGTH,
            f"negative byte_length {byte_length}",
        )

    digest = payload["payload_hash"]
    if not isinstance(digest, str) or not _HEX64.match(digest):
        return IntegrityStatus.INVALID_HASH, "payload_hash is not a sha256 hex digest"

    if payload["parser_version"] not in SUPPORTED_PARSER_VERSIONS:
        return IntegrityStatus.INVALID_METADATA, (
            f"parser_version {payload['parser_version']!r} is not supported by "
            f"this code ({sorted(SUPPORTED_PARSER_VERSIONS)}); a record read by "
            "a different parser is not one this scanner can interpret"
        )

    # What the stored bytes are. Absent means a record written before v2.1.14,
    # when the payload was a UTF-8 re-encoding of decoded text -- refused rather
    # than reinterpreted, because the digest on such a record does not cover the
    # response and this scanner would say it did.
    raw_schema = payload.get("raw_response_schema_version")
    if raw_schema is None:
        return IntegrityStatus.INVALID_METADATA, (
            "the record states no raw_response_schema_version, so it predates "
            f"{RAW_RESPONSE_SCHEMA_VERSION} and its payload_hash may cover a "
            "re-encoding of decoded text rather than the response bytes. Migrate "
            "it deliberately or read it with the version that wrote it."
        )
    if raw_schema not in SUPPORTED_RAW_RESPONSE_SCHEMAS:
        return IntegrityStatus.INVALID_METADATA, (
            f"raw_response_schema_version {raw_schema!r} is not supported by "
            f"this code ({sorted(SUPPORTED_RAW_RESPONSE_SCHEMAS)})"
        )
    representation = payload.get("body_representation")
    if representation != BODY_REPRESENTATION:
        return IntegrityStatus.INVALID_METADATA, (
            f"body_representation {representation!r} is not "
            f"{BODY_REPRESENTATION!r}; the digest would be over a different "
            "thing from the one this code compares"
        )

    decode_problem = _decode_metadata_problem(payload)
    if decode_problem:
        return IntegrityStatus.INVALID_METADATA, decode_problem

    location = payload["payload_location"]
    if not isinstance(location, str) or not location.strip():
        return IntegrityStatus.INVALID_METADATA, (
            "payload_location must name where the bytes are; an entry that does "
            "not is an index of nothing"
        )
    if not is_portable_location(location):
        return IntegrityStatus.INVALID_METADATA, (
            f"payload_location {location!r} is absolute, so it describes the "
            "machine that captured rather than the capture; a relocated run "
            "directory would point at a path that is not there"
        )

    params = payload.get("query_params", {})
    if not isinstance(params, dict):
        return IntegrityStatus.INVALID_METADATA, (
            f"query_params must be a mapping, got {type(params).__name__}. The "
            "parameters are part of what a response *is*."
        )

    status = payload.get("http_status")
    if isinstance(status, bool) or not isinstance(status, int):
        return IntegrityStatus.INVALID_METADATA, (
            f"http_status must be an integer, got {type(status).__name__}"
        )

    request_id = payload.get("request_id", "")
    if not isinstance(request_id, str):
        return IntegrityStatus.INVALID_METADATA, (
            f"request_id must be a string, got {type(request_id).__name__}"
        )

    sequence = payload.get("request_sequence", 0)
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        return IntegrityStatus.INVALID_METADATA, (
            f"request_sequence must be an integer, got {type(sequence).__name__}"
        )
    if sequence < 0:
        return IntegrityStatus.INVALID_METADATA, (
            f"negative request_sequence {sequence}"
        )

    schema = payload.get("vendor_schema_version")
    if schema is not None and not isinstance(schema, str):
        return IntegrityStatus.INVALID_METADATA, (
            f"vendor_schema_version must be a string or absent, got "
            f"{type(schema).__name__}"
        )

    origin = payload.get("capture_origin", CaptureOrigin.UNKNOWN_ORIGIN.value)
    if origin not in {member.value for member in CaptureOrigin}:
        return IntegrityStatus.INVALID_METADATA, (
            f"capture_origin {origin!r} is not a recognised origin"
        )

    instants: dict[str, datetime] = {}
    for field_name in ("request_started_at", "response_received_at"):
        value = payload[field_name]
        if not isinstance(value, str):
            return IntegrityStatus.INVALID_TIMESTAMP, f"{field_name} must be a string"
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return IntegrityStatus.INVALID_TIMESTAMP, (
                f"{field_name} is not an ISO-8601 timestamp: {value!r}"
            )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return IntegrityStatus.INVALID_TIMESTAMP, (
                f"{field_name} is naive; a stored clock without an offset means "
                "whatever zone the reader happens to be in"
            )
        instants[field_name] = parsed

    if instants["response_received_at"] < instants["request_started_at"]:
        return IntegrityStatus.INVALID_TIMESTAMP, (
            "the response was received before the request was sent: "
            f"{instants['response_received_at'].isoformat()} < "
            f"{instants['request_started_at'].isoformat()}"
        )

    return None, ""


def is_safe_record_id(record_id: str) -> bool:
    """True when a record id cannot escape the store root.

    Checked as a *string property*, so no path is ever constructed from an
    untrusted id in order to find out whether constructing it was safe.
    """
    if not record_id or record_id != record_id.strip():
        return False
    if any(sep in record_id for sep in ("/", "\\", "\x00")):
        return False
    return ".." not in pathlib.PurePosixPath(record_id).parts and ".." not in record_id


def _safe_component(value: str) -> str:
    """Filesystem-safe fragment. Rejects rather than silently mangling."""
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in value)
    if not cleaned or cleaned.strip("-") == "":
        raise RawStoreError(f"unsafe identifier component: {value!r}")
    return cleaned


class IntegrityStatus(str, Enum):
    """What a scan found for one artefact.

    Payload and index writes are each atomic, but not atomic *together*: a
    crash between them leaves a consistent-looking store with one half of a
    pair. Nothing in v2.1 could tell you afterwards which pairs had come apart,
    which meant an audit trail whose own completeness was unverifiable.
    """

    VALID = "VALID"
    #: A payload file with no index entry -- crash after rename, before append.
    ORPHAN_PAYLOAD = "ORPHAN_PAYLOAD"
    #: An index entry with no payload -- crash after append, before rename.
    MISSING_PAYLOAD = "MISSING_PAYLOAD"
    HASH_MISMATCH = "HASH_MISMATCH"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    #: A leftover temp file. Evidence, not garbage: reported, never removed.
    INCOMPLETE_WRITE = "INCOMPLETE_WRITE"
    DUPLICATE_ID = "DUPLICATE_ID"
    INVALID_METADATA = "INVALID_METADATA"
    #: The record id would escape the store root, or is not a string.
    #: Reported *before* any path is resolved from it.
    UNSAFE_RECORD_ID = "UNSAFE_RECORD_ID"
    INVALID_BYTE_LENGTH = "INVALID_BYTE_LENGTH"
    INVALID_HASH = "INVALID_HASH"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"


@dataclass(frozen=True, slots=True)
class IntegrityFinding:
    status: IntegrityStatus
    artifact: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "artifact": self.artifact,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """The result of a scan. Describes; never repairs."""

    findings: tuple[IntegrityFinding, ...]

    @property
    def ok(self) -> bool:
        return all(f.status is IntegrityStatus.VALID for f in self.findings)

    def counts(self) -> dict[str, int]:
        counter: dict[str, int] = {}
        for finding in self.findings:
            counter[finding.status.value] = counter.get(finding.status.value, 0) + 1
        return dict(sorted(counter.items()))

    def recovery_plan(self) -> tuple[str, ...]:
        """What a human *could* do, phrased so nobody mistakes it for done.

        Deliberately returns strings rather than callables. Silently deleting an
        artefact destroys the only evidence of how the store came apart, and the
        artefact may be the more trustworthy half of the pair.
        """
        actions: list[str] = []
        for finding in self.findings:
            if finding.status is IntegrityStatus.VALID:
                continue
            actions.append(
                f"proposed: inspect {finding.artifact} "
                f"({finding.status.value}) -- {finding.detail or 'no further detail'}"
            )
        return tuple(actions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "counts": self.counts(),
            "findings": [f.as_dict() for f in self.findings],
            "recovery_plan": list(self.recovery_plan()),
        }


@runtime_checkable
class RawResponseStore(Protocol):
    def put(
        self,
        *,
        record_id: str,
        endpoint: str,
        query_params: dict[str, Any],
        payload: str | bytes,
        request_started_at: datetime,
        response_received_at: datetime,
        http_status: int,
        vendor_schema_version: str | None = None,
        request_id: str = "",
        request_sequence: int = 0,
        capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN,
        identity: CaptureIdentity | None = None,
        decode: Mapping[str, Any] | None = None,
        response_headers: Mapping[str, str] | None = None,
    ) -> RawResponseRecord: ...

    def get_payload(self, record_id: str) -> str: ...

    def get_body(self, record_id: str) -> bytes: ...

    def records(self) -> tuple[RawResponseRecord, ...]: ...


class InMemoryRawStore:
    """Append-only store for tests and short-lived research runs.

    **Volatile.** Everything it holds disappears with the process, which makes
    it right for unit tests and offline fixtures and wrong for the only copy of
    a paid session's evidence. ``durability`` says so, and capture readiness
    refuses it -- v2.1.5 probed for protocol compliance, integrity and a
    successful write, all of which this passes.
    """

    durability = StoreDurability.TEST_ONLY_VOLATILE

    def __init__(self) -> None:
        self._records: dict[str, RawResponseRecord] = {}
        self._payloads: dict[str, bytes] = {}

    def put(
        self,
        *,
        record_id: str,
        endpoint: str,
        query_params: dict[str, Any],
        payload: str | bytes,
        request_started_at: datetime,
        response_received_at: datetime,
        http_status: int,
        vendor_schema_version: str | None = None,
        request_id: str = "",
        request_sequence: int = 0,
        capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN,
        identity: CaptureIdentity | None = None,
        decode: Mapping[str, Any] | None = None,
        response_headers: Mapping[str, str] | None = None,
    ) -> RawResponseRecord:
        if record_id in self._records:
            raise RawStoreError(
                f"raw response {record_id!r} already exists; the store is "
                "append-only so evidence cannot be overwritten"
            )
        record = RawResponseRecord(
            record_id=record_id,
            endpoint=endpoint,
            query_params=dict(query_params),
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            http_status=http_status,
            payload_hash=payload_hash(payload),
            payload_location=f"memory://{record_id}",
            vendor_schema_version=vendor_schema_version,
            byte_length=len(_as_body(payload)),
            request_id=request_id,
            request_sequence=request_sequence,
            capture_origin=(
                identity.capture_origin if identity is not None else capture_origin
            ),
            capture_session_id=identity.session_id if identity else "",
            pipeline_fingerprint=identity.pipeline_fingerprint if identity else "",
            capture_plan_fingerprint=(
                identity.capture_plan_fingerprint if identity else ""
            ),
            request_spec_fingerprint=(
                identity.request_spec_fingerprint if identity else ""
            ),
            normalization_recipe_fingerprint=(
                identity.normalization_recipe_fingerprint if identity else ""
            ),
            operation_id=identity.operation_id if identity else "",
            operation_fingerprint=identity.operation_fingerprint if identity else "",
            normalization_recipe_hash=(
                identity.normalization_recipe_hash if identity else ""
            ),
            requested_as_of=identity.requested_as_of if identity else None,
            effective_valuation_timestamp=(
                identity.effective_valuation_timestamp if identity else None
            ),
            valuation_timestamp_rule=(
                identity.valuation_timestamp_rule if identity else ""
            ),
            expected_universe_fingerprint=(
                identity.expected_universe_fingerprint if identity else ""
            ),
            open_interest_date_rule_fingerprint=(
                identity.open_interest_date_rule_fingerprint if identity else ""
            ),
            spot_synchronization_policy_fingerprint=(
                identity.spot_synchronization_policy_fingerprint if identity else ""
            ),
            **_decode_fields(decode),
            response_headers=_safe_headers(response_headers),
        )
        self._records[record_id] = record
        self._payloads[record_id] = _as_body(payload)
        return record

    def get_payload(self, record_id: str) -> str:
        """The stored bytes, read as text. See :meth:`get_body` for the bytes."""
        return self.get_body(record_id).decode("utf-8", errors="replace")

    def get_body(self, record_id: str) -> bytes:
        """Exactly what was stored.

        The authoritative accessor since v2.1.13: ``payload_hash`` is taken over
        these bytes, so anything checking a digest has to compare against them
        rather than against a decoding of them.
        """
        if record_id not in self._payloads:
            raise KeyError(record_id)
        return self._payloads[record_id]

    @property
    def durable(self) -> bool:
        return self.durability.survives_the_process

    def next_request_sequence(self) -> int:
        recorded = [r.request_sequence for r in self.records()]
        return max(recorded, default=0) + 1

    def records(self) -> tuple[RawResponseRecord, ...]:
        return tuple(
            self._records[key]
            for key in sorted(self._records)
            if not is_probe_record(key)
        )


class FileRawStore:
    """Append-only store backed by a directory. Durable.

    Layout: ``<root>/<record_id>.raw`` for the payload and ``<root>/index.jsonl``
    for the metadata. Deliberately plain files -- the audit trail should be
    readable without this codebase.
    """

    durability = StoreDurability.DURABLE_APPEND_ONLY

    def __init__(self, root: pathlib.Path) -> None:
        self._root = pathlib.Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._index = self._root / "index.jsonl"
        # Probe records live in a sibling directory, never in the capture
        # namespace. v2.1.5 wrote them into the store and then filtered them out
        # of every scan -- which worked, and meant the health check of an
        # append-only store permanently added to it.
        self._probe_root = self._root.parent / f"{self._root.name}.health"

    def canonical_location(self, record_id: str) -> str:
        """Where this store keeps a record, relative to its own root.

        The single authority. A manifest or an index entry that states anything
        else is describing a different store, and the honest response is to
        refuse rather than to quietly read the file the id points at.
        """
        return self.payload_path(record_id).relative_to(self._root.resolve()).as_posix()

    def payload_path(self, record_id: str) -> pathlib.Path:
        """Where this record's bytes are, for this store, right now.

        Derived from the record id and the store's *current* root, never from
        the recorded location -- so relocating the directory relocates the
        evidence with it.
        """
        # Path-traversal guard: reject rather than sanitise, so a caller cannot
        # believe it wrote one file while another was written.
        safe = "".join(ch for ch in record_id if ch.isalnum() or ch in "-_.")
        if safe != record_id or not safe or ".." in record_id:
            raise RawStoreError(f"unsafe record id: {record_id!r}")
        resolved = (self._root / f"{safe}.raw").resolve()
        if not str(resolved).startswith(str(self._root.resolve())):
            raise RawStoreError(f"record id escapes the store root: {record_id!r}")
        return resolved

    def put(
        self,
        *,
        record_id: str,
        endpoint: str,
        query_params: dict[str, Any],
        payload: str | bytes,
        request_started_at: datetime,
        response_received_at: datetime,
        http_status: int,
        vendor_schema_version: str | None = None,
        request_id: str = "",
        request_sequence: int = 0,
        capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN,
        identity: CaptureIdentity | None = None,
        decode: Mapping[str, Any] | None = None,
        response_headers: Mapping[str, str] | None = None,
    ) -> RawResponseRecord:
        path = self.payload_path(record_id)
        if path.exists():
            raise RawStoreError(
                f"raw response {record_id!r} already exists at {path}; the store "
                "is append-only so evidence cannot be overwritten"
            )
        record = RawResponseRecord(
            record_id=record_id,
            endpoint=endpoint,
            query_params=dict(query_params),
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            http_status=http_status,
            payload_hash=payload_hash(payload),
            # Relative to the store root. An absolute path is a fact about the
            # machine that captured, not about the evidence: copy the run
            # directory to an archive host and every absolute location becomes a
            # claim about a directory that is not there.
            payload_location=path.relative_to(self._root.resolve()).as_posix(),
            vendor_schema_version=vendor_schema_version,
            byte_length=len(_as_body(payload)),
            request_id=request_id,
            request_sequence=request_sequence,
            capture_origin=(
                identity.capture_origin if identity is not None else capture_origin
            ),
            capture_session_id=identity.session_id if identity else "",
            pipeline_fingerprint=identity.pipeline_fingerprint if identity else "",
            capture_plan_fingerprint=(
                identity.capture_plan_fingerprint if identity else ""
            ),
            request_spec_fingerprint=(
                identity.request_spec_fingerprint if identity else ""
            ),
            normalization_recipe_fingerprint=(
                identity.normalization_recipe_fingerprint if identity else ""
            ),
            operation_id=identity.operation_id if identity else "",
            operation_fingerprint=identity.operation_fingerprint if identity else "",
            normalization_recipe_hash=(
                identity.normalization_recipe_hash if identity else ""
            ),
            requested_as_of=identity.requested_as_of if identity else None,
            effective_valuation_timestamp=(
                identity.effective_valuation_timestamp if identity else None
            ),
            valuation_timestamp_rule=(
                identity.valuation_timestamp_rule if identity else ""
            ),
            expected_universe_fingerprint=(
                identity.expected_universe_fingerprint if identity else ""
            ),
            open_interest_date_rule_fingerprint=(
                identity.open_interest_date_rule_fingerprint if identity else ""
            ),
            spot_synchronization_policy_fingerprint=(
                identity.spot_synchronization_policy_fingerprint if identity else ""
            ),
            **_decode_fields(decode),
            response_headers=_safe_headers(response_headers),
        )
        # Atomic write: temp file -> flush -> fsync -> rename. A crash midway
        # leaves either nothing or a complete file, never a truncated payload
        # that a later reader would treat as the vendor's full response.
        self._atomic_write(path, payload)
        self._append_index(record)
        return record

    @staticmethod
    def _atomic_write(path: pathlib.Path, payload: str | bytes) -> None:
        """Binary, always.

        v2.1.12 opened the file in text mode with ``newline=""``, so the bytes
        on disk were a UTF-8 re-encoding of a string that had already been
        decoded with ``errors="replace"``. Two lossy conversions between the
        socket and the file, and the digest was described as the hash of the
        vendor's response.
        """
        handle, temp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".partial-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(_as_body(payload))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except BaseException:
            pathlib.Path(temp_name).unlink(missing_ok=True)
            raise

    def _append_index(self, record: RawResponseRecord) -> None:
        with self._index.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record.as_dict(), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @property
    def root(self) -> pathlib.Path:
        """Where the artefacts live. Exposed so an integrity scan is auditable."""
        return self._root

    @property
    def durable(self) -> bool:
        return self.durability.survives_the_process

    def next_request_sequence(self) -> int:
        """The sequence a real capture would use next.

        Read by the health probe's test: a probe that consumed a sequence would
        leave a gap in the request numbering of the session it was checking.
        """
        recorded = [r.request_sequence for r in self.records()]
        return max(recorded, default=0) + 1

    def free_bytes(self) -> int | None:
        """Room left where the capture will be written."""
        import shutil

        try:
            return int(shutil.disk_usage(self._root).free)
        except OSError:
            return None

    def probe_write(self, payload: str) -> str:
        """Write and read back, without entering the capture index.

        Two writes, because they answer two questions. The sibling health
        directory proves the store's machinery works; a scratch file *inside the
        capture root* proves the destination a real session would write to is
        actually writable. Probing only the sibling would pass on a store whose
        own root had gone read-only, which is precisely the failure a paid
        session cannot survive.

        Neither write becomes evidence: ``records()`` and ``verify_integrity``
        read ``*.raw`` and the index, and neither of these is either.

        Returns what came back, so the caller compares bytes rather than
        trusting that no exception means success.
        """
        scratch = self._root / f".probe-{uuid.uuid4().hex[:16]}.tmp"
        self._atomic_write(scratch, payload)
        scratch.unlink(missing_ok=True)

        self._probe_root.mkdir(parents=True, exist_ok=True)
        target = self._probe_root / f"probe-{uuid.uuid4().hex[:16]}.tmp"
        self._atomic_write(target, payload)
        try:
            # Read back as bytes and decode here, so a probe answers "did these
            # bytes survive?" rather than "did text mode give them back to me?".
            # Text mode would translate CRLF and hide exactly the round-trip
            # failure this exists to catch.
            return target.read_bytes().decode("utf-8")
        finally:
            target.unlink(missing_ok=True)

    def verify_integrity(self) -> IntegrityReport:
        """Scan the store and classify every artefact. Never modifies anything.

        Answers the question an auditor actually has -- "is this audit trail
        itself trustworthy?" -- which v2.1 had no way to answer.
        """
        findings: list[IntegrityFinding] = []
        seen_ids: set[str] = set()
        indexed_paths: set[pathlib.Path] = set()

        if self._index.exists():
            for number, line in enumerate(
                self._index.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    findings.append(
                        IntegrityFinding(
                            status=IntegrityStatus.INVALID_METADATA,
                            artifact=f"{self._index.name}:{number}",
                            detail=str(exc),
                        )
                    )
                    continue

                # Validate the schema BEFORE deriving anything -- above all
                # before resolving a filesystem path from record_id.
                status, detail = validate_metadata(data)
                if status is not None:
                    findings.append(
                        IntegrityFinding(
                            status=status,
                            artifact=f"{self._index.name}:{number}",
                            detail=detail,
                        )
                    )
                    continue

                record_id = str(data["record_id"])
                expected_hash = str(data["payload_hash"])

                # Health-probe records are the store proving it works, not
                # evidence. They are never removed -- an append-only store has
                # no delete -- so they are skipped here instead, or a probe
                # would make every later scan look like it had grown.
                if is_probe_record(record_id):
                    indexed_paths.add(self.payload_path(record_id))
                    continue

                if record_id in seen_ids:
                    findings.append(
                        IntegrityFinding(
                            status=IntegrityStatus.DUPLICATE_ID,
                            artifact=record_id,
                            detail=f"repeated at {self._index.name}:{number}",
                        )
                    )
                    continue
                seen_ids.add(record_id)

                path = self.payload_path(record_id)
                indexed_paths.add(path)

                # **The location is checked, not merely validated.** v2.1.14
                # confirmed that ``payload_location`` was relative and then
                # ignored it, deriving the path from ``record_id`` instead -- so
                # an index could name ``missing/other.raw`` for a record whose
                # bytes were somewhere else entirely and the scan reported
                # VALID. A location nobody consults is a location that can lie.
                canonical = path.relative_to(self._root.resolve()).as_posix()
                stated = str(data["payload_location"])
                if stated != canonical:
                    findings.append(
                        IntegrityFinding(
                            status=IntegrityStatus.INVALID_METADATA,
                            artifact=record_id,
                            detail=(
                                f"payload_location {stated!r} is not where this "
                                f"store keeps {record_id!r}, which is "
                                f"{canonical!r}"
                            ),
                        )
                    )
                    continue

                if not path.exists():
                    findings.append(
                        IntegrityFinding(
                            status=IntegrityStatus.MISSING_PAYLOAD,
                            artifact=record_id,
                            detail="index entry has no payload file",
                        )
                    )
                    continue

                # **Bytes, never text.** ``read_text`` opens in text mode, which
                # translates CRLF to LF -- so a vendor sending ``\r\n`` line
                # endings had every record report HASH_MISMATCH, and a body that
                # is not valid UTF-8 raised ``UnicodeDecodeError`` and took the
                # whole scan down. Decoding is the parser's job; this layer
                # answers "are these the bytes we wrote?" and nothing else.
                payload = path.read_bytes()
                actual_hash = hashlib.sha256(payload).hexdigest()
                actual_length = len(payload)
                expected_length = data.get("byte_length")

                # **The reading, re-derived.** The digest says the bytes have
                # not changed. It says nothing about whether the *description*
                # of them is still true, and a capture is only as good as the
                # description a later reader will parse it under. Derived from
                # the bytes and the retained headers, never read back off the
                # claim it is supposed to be checking.
                drift = _decode_drift(payload, data)
                if drift:
                    findings.append(
                        IntegrityFinding(
                            status=IntegrityStatus.INVALID_METADATA,
                            artifact=record_id,
                            detail=drift,
                        )
                    )
                    continue

                if actual_hash != expected_hash:
                    findings.append(
                        IntegrityFinding(
                            status=IntegrityStatus.HASH_MISMATCH,
                            artifact=record_id,
                            detail=f"expected {expected_hash}, found {actual_hash}",
                        )
                    )
                elif expected_length is not None and actual_length != int(
                    expected_length
                ):
                    findings.append(
                        IntegrityFinding(
                            status=IntegrityStatus.SIZE_MISMATCH,
                            artifact=record_id,
                            detail=(
                                f"expected {expected_length} bytes, found "
                                f"{actual_length}"
                            ),
                        )
                    )
                else:
                    findings.append(
                        IntegrityFinding(
                            status=IntegrityStatus.VALID, artifact=record_id
                        )
                    )

        for path in sorted(self._root.iterdir()):
            if path == self._index or not path.is_file():
                continue
            if path.name.startswith(".") and path.suffix == ".tmp":
                findings.append(
                    IntegrityFinding(
                        status=IntegrityStatus.INCOMPLETE_WRITE,
                        artifact=path.name,
                        detail="temp file from an interrupted write",
                    )
                )
                continue
            if path not in indexed_paths:
                findings.append(
                    IntegrityFinding(
                        status=IntegrityStatus.ORPHAN_PAYLOAD,
                        artifact=path.name,
                        detail="payload file with no index entry",
                    )
                )

        return IntegrityReport(findings=tuple(findings))

    def incomplete_captures(self) -> tuple[str, ...]:
        """Temp files left behind by an interrupted write.

        Reported rather than cleaned up silently: an interrupted capture is
        evidence that something went wrong, and deleting it hides that.
        """
        return tuple(sorted(p.name for p in self._root.glob(".partial-*.tmp")))

    def get_payload(self, record_id: str) -> str:
        """The stored bytes, read as text. See :meth:`get_body` for the bytes."""
        return self.get_body(record_id).decode("utf-8", errors="replace")

    def get_body(self, record_id: str) -> bytes:
        """Exactly the bytes that were written, byte for byte."""
        path = self.payload_path(record_id)
        if not path.exists():
            raise KeyError(record_id)
        return path.read_bytes()

    def records(self) -> tuple[RawResponseRecord, ...]:
        if not self._index.exists():
            return ()
        out: list[RawResponseRecord] = []
        for line in self._index.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            if is_probe_record(str(data.get("record_id", ""))):
                continue
            out.append(
                RawResponseRecord(
                    record_id=data["record_id"],
                    endpoint=data["endpoint"],
                    query_params=data["query_params"],
                    request_started_at=datetime.fromisoformat(
                        data["request_started_at"]
                    ),
                    response_received_at=datetime.fromisoformat(
                        data["response_received_at"]
                    ),
                    http_status=data["http_status"],
                    payload_hash=data["payload_hash"],
                    payload_location=data["payload_location"],
                    response_headers=dict(data.get("response_headers", {})),
                    parser_version=data["parser_version"],
                    vendor_schema_version=data.get("vendor_schema_version"),
                    byte_length=data.get("byte_length", 0),
                    request_id=data.get("request_id", ""),
                    request_sequence=data.get("request_sequence", 0),
                    capture_complete=data.get("capture_complete", True),
                    capture_origin=CaptureOrigin(
                        data.get("capture_origin", CaptureOrigin.UNKNOWN_ORIGIN.value)
                    ),
                    # The capture-time stamps. Absent from an index written by
                    # an older release, which reads as "unstamped" -- and
                    # verification refuses an unstamped record rather than
                    # inventing a claim for it.
                    capture_session_id=data.get("capture_session_id", ""),
                    pipeline_fingerprint=data.get("pipeline_fingerprint", ""),
                    capture_plan_fingerprint=data.get("capture_plan_fingerprint", ""),
                    request_spec_fingerprint=data.get("request_spec_fingerprint", ""),
                    normalization_recipe_fingerprint=data.get(
                        "normalization_recipe_fingerprint", ""
                    ),
                    operation_id=data.get("operation_id", ""),
                    operation_fingerprint=data.get("operation_fingerprint", ""),
                    normalization_recipe_hash=data.get("normalization_recipe_hash", ""),
                    requested_as_of=_moment_or_none(data.get("requested_as_of")),
                    effective_valuation_timestamp=_moment_or_none(
                        data.get("effective_valuation_timestamp")
                    ),
                    valuation_timestamp_rule=data.get("valuation_timestamp_rule", ""),
                    expected_universe_fingerprint=data.get(
                        "expected_universe_fingerprint", ""
                    ),
                    open_interest_date_rule_fingerprint=data.get(
                        "open_interest_date_rule_fingerprint", ""
                    ),
                    spot_synchronization_policy_fingerprint=data.get(
                        "spot_synchronization_policy_fingerprint", ""
                    ),
                )
            )
        return tuple(out)


class NullRawStore:
    """Discards everything. The default, so capture is opt-in."""

    def put(
        self,
        *,
        record_id: str,
        endpoint: str,
        query_params: dict[str, Any],
        payload: str | bytes,
        request_started_at: datetime,
        response_received_at: datetime,
        http_status: int,
        vendor_schema_version: str | None = None,
        request_id: str = "",
        request_sequence: int = 0,
        capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN,
        identity: CaptureIdentity | None = None,
        decode: Mapping[str, Any] | None = None,
        response_headers: Mapping[str, str] | None = None,
    ) -> RawResponseRecord:
        return RawResponseRecord(
            record_id=record_id,
            endpoint=endpoint,
            query_params=dict(query_params),
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            http_status=http_status,
            payload_hash=payload_hash(payload),
            payload_location="null://discarded",
            vendor_schema_version=vendor_schema_version,
            byte_length=len(_as_body(payload)),
            request_id=request_id,
            request_sequence=request_sequence,
            capture_origin=(
                identity.capture_origin if identity is not None else capture_origin
            ),
            capture_session_id=identity.session_id if identity else "",
            pipeline_fingerprint=identity.pipeline_fingerprint if identity else "",
            capture_plan_fingerprint=(
                identity.capture_plan_fingerprint if identity else ""
            ),
            request_spec_fingerprint=(
                identity.request_spec_fingerprint if identity else ""
            ),
            normalization_recipe_fingerprint=(
                identity.normalization_recipe_fingerprint if identity else ""
            ),
            operation_id=identity.operation_id if identity else "",
            operation_fingerprint=identity.operation_fingerprint if identity else "",
            normalization_recipe_hash=(
                identity.normalization_recipe_hash if identity else ""
            ),
            requested_as_of=identity.requested_as_of if identity else None,
            effective_valuation_timestamp=(
                identity.effective_valuation_timestamp if identity else None
            ),
            valuation_timestamp_rule=(
                identity.valuation_timestamp_rule if identity else ""
            ),
            expected_universe_fingerprint=(
                identity.expected_universe_fingerprint if identity else ""
            ),
            open_interest_date_rule_fingerprint=(
                identity.open_interest_date_rule_fingerprint if identity else ""
            ),
            spot_synchronization_policy_fingerprint=(
                identity.spot_synchronization_policy_fingerprint if identity else ""
            ),
            **_decode_fields(decode),
            response_headers=_safe_headers(response_headers),
        )

    def get_payload(self, record_id: str) -> str:
        raise KeyError(record_id)

    def get_body(self, record_id: str) -> bytes:
        raise KeyError(record_id)

    def records(self) -> tuple[RawResponseRecord, ...]:
        return ()


@dataclass(frozen=True, slots=True)
class RawStoreHealth:
    """Whether a store is a place to put a paid session's only copy.

    v2.1.4 accepted anything with the right attribute names. ``raw_store=
    object()`` has no ``verify_integrity``, so the integrity check was skipped
    rather than failed, and readiness passed on a store that could not have
    stored anything.
    """

    usable: bool
    failures: tuple[str, ...] = ()
    store_description: str = ""
    durability: StoreDurability = StoreDurability.TEST_ONLY_VOLATILE

    @property
    def durable(self) -> bool:
        return self.durability.survives_the_process

    def as_dict(self) -> dict[str, Any]:
        return {
            "usable": self.usable,
            "failures": list(self.failures),
            "store_description": self.store_description,
            "durability": self.durability.value,
        }


#: Prefix for health-probe records. Kept out of the capture namespace: a probe
#: that leaves a record behind has corrupted the audit trail it was checking.
PROBE_PREFIX = "healthprobe"


def probe_raw_store(
    store: Any,
    *,
    require_durable: bool = True,
    minimum_free_bytes: int = DEFAULT_MINIMUM_FREE_BYTES,
    source_root: pathlib.Path | None = None,
) -> RawStoreHealth:
    """Check that a store is somewhere a paid session's evidence can live.

    Six questions, because they fail for six different reasons: does it
    implement the protocol, does it survive the process, is it clean, is there
    room, can it take a write, and does that write read back byte-for-byte.

    v2.1.5 asked four of them. ``InMemoryRawStore`` passes all four -- it is a
    working store that forgets everything when Python exits -- so a readiness
    report could say the destination was fine for a session whose only copy of
    the evidence would not survive it.

    The probe writes into a sibling directory, never into the capture namespace,
    and removes what it wrote. v2.1.5 wrote probe records into the store and
    filtered them out of every scan afterwards, which meant the health check of
    an append-only store permanently added to it.
    """
    failures: list[str] = []
    description = type(store).__name__

    if not isinstance(store, RawResponseStore):
        return RawStoreHealth(
            usable=False,
            failures=(
                f"PROTOCOL: {description} does not implement RawResponseStore; a "
                "store that cannot put, get or list is not somewhere to keep "
                "evidence",
            ),
            store_description=description,
        )

    # A store that does not classify itself is volatile: durability is a claim,
    # and the default for an unmade claim is the one that blocks a paid session.
    try:
        durability = StoreDurability(
            getattr(store, "durability", StoreDurability.TEST_ONLY_VOLATILE)
        )
    except ValueError:
        durability = StoreDurability.TEST_ONLY_VOLATILE
    if require_durable and not durability.survives_the_process:
        failures.append(
            f"DURABILITY: {description} is not durable, it is "
            f"{durability.value}. A paid session's only copy of the evidence "
            "cannot live in a process that is about to exit. It stays supported "
            "for unit tests and offline fixtures."
        )

    integrity = getattr(store, "verify_integrity", None)
    if callable(integrity):
        report = integrity()
        if not report.ok:
            failures.append(f"INTEGRITY: store is not clean: {report.counts()}")

    root = getattr(store, "root", None)
    if root is not None:
        resolved = pathlib.Path(root).resolve()
        tree = (source_root or pathlib.Path(__file__).resolve().parents[2]).resolve()
        if resolved == tree or tree in resolved.parents:
            failures.append(
                f"LOCATION: {resolved} is inside the source tree. Captured "
                "vendor bytes are not source, and a capture written there ends "
                "up in a commit or a release archive."
            )

    free = getattr(store, "free_bytes", None)
    if callable(free):
        available = free()
        if available is not None and available < minimum_free_bytes:
            failures.append(
                f"SPACE: {available} bytes free, below the {minimum_free_bytes} "
                "minimum. Running out of disk halfway through a paid session is "
                "the one failure that cannot be retried for free."
            )

    payload = f"{PROBE_PREFIX}-{uuid.uuid4().hex}"
    probe = getattr(store, "probe_write", None)
    try:
        if callable(probe):
            read_back = probe(payload)
        else:
            read_back = _probe_through_the_store(store, payload)
    except Exception as exc:
        failures.append(f"WRITE: the store refused a probe record: {exc}")
        return RawStoreHealth(
            usable=False,
            failures=tuple(failures),
            store_description=description,
            durability=durability,
        )

    if read_back != payload:
        failures.append("READ: the probe did not read back byte-identical")

    return RawStoreHealth(
        usable=not failures,
        failures=tuple(failures),
        store_description=description,
        durability=durability,
    )


def _probe_through_the_store(store: Any, payload: str) -> str:
    """Fallback for a store with no native probe.

    Uses the reserved probe namespace, which ``records()`` and
    ``verify_integrity`` both ignore, so a volatile store can still be checked
    without the probe becoming evidence.
    """
    record_id = f"{PROBE_PREFIX}-{uuid.uuid4().hex[:16]}"
    now = datetime.now(UTC)
    store.put(
        record_id=record_id,
        endpoint="/internal/health-probe",
        query_params={},
        payload=payload,
        request_started_at=now,
        response_received_at=now,
        http_status=200,
    )
    return str(store.get_payload(record_id))


def is_probe_record(record_id: str) -> bool:
    """Probe records are health checks, not evidence, and never count as either."""
    return record_id.startswith(PROBE_PREFIX)


@dataclass(slots=True)
class CaptureSession:
    """Groups the records belonging to one chain pull.

    Record ids combine the session, a monotonic request sequence, the endpoint
    and a canonical parameter hash. v2 used ``session-endpoint`` alone, so two
    requests to the same endpoint in one session collided -- and because the
    store is append-only, the second one raised rather than being captured.
    """

    store: RawResponseStore
    session_id: str
    captured: list[RawResponseRecord] = field(default_factory=list)
    #: Where this session's responses come from. Set by the client from the
    #: transport it is actually using, not by whoever opened the session.
    capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN
    #: What the repository looked like when the session opened. Stamped onto
    #: every record, so verification can ask the evidence which pipeline
    #: produced it rather than believing a manifest's own summary.
    pipeline_fingerprint: str = ""
    capture_plan_fingerprint: str = ""
    request_spec_fingerprint: str = ""
    normalization_recipe_fingerprint: str = ""
    #: Which operation this session is currently issuing records for. A session
    #: may run several -- a chain pull, a later re-pull, a paginated sweep --
    #: and each fixes its own valuation instant.
    operation_id: str = ""
    operation_fingerprint: str = ""
    normalization_recipe_hash: str = ""
    requested_as_of: datetime | None = None
    effective_valuation_timestamp: datetime | None = None
    valuation_timestamp_rule: str = ""
    #: The universe this operation expected, and the rule establishing the
    #: open-interest settlement date. Both change what a chain *means* --
    #: completeness, confidence, and the weight on every GEX term -- so both are
    #: fixed by the operation rather than supplied at calculation time. v2.1.7
    #: took the expected universe as an argument to the calculation, so one
    #: capture could be scored MEASURED_COMPLETE and replayed PARTIALLY_OBSERVED.
    expected_universe_fingerprint: str = ""
    open_interest_date_rule_fingerprint: str = ""
    #: The remaining field the operation digest covers. Stored explicitly since
    #: v2.1.9 so ``verify_capture`` can recompute that digest from the record
    #: rather than comparing stored digests to each other and calling it checked.
    spot_synchronization_policy_fingerprint: str = ""
    #: The artifacts those two digests name. Carried on the session so a fetch
    #: needs no repetition of what the session already knows, and so replay
    #: recovers the objects rather than only the digests naming them.
    #:
    #: ``settlement_artifact is None`` is the honest state of every capture this
    #: repository can currently take, and it is what makes such a capture
    #: permanently ineligible for a trusted GEX: since v2.1.9 nothing downstream
    #: accepts a settlement rule, so there is no later opportunity to supply one.
    settlement_artifact: Any = None
    #: The *verified* artifact, where one was resolved before the operation
    #: opened. Its hash is what the records are stamped with.
    expected_universe: Any = None
    #: An unresolved declaration, recorded for diagnostics and stamped nowhere.
    #: Keeping the two in separate fields is what stops a claim reaching the
    #: completeness measure by being in the same slot as evidence.
    declared_expected_universe: Any = None
    _sequence: int = 0

    @property
    def establishes_a_settlement_date(self) -> bool:
        """Whether a trusted calculation on this capture is even possible."""
        return self.settlement_artifact is not None

    @property
    def settlement_date(self) -> Any:
        """The date this operation's rule derived, or ``None``."""
        artifact = self.settlement_artifact
        return artifact.resolved_settlement_date if artifact is not None else None

    @property
    def identity(self) -> CaptureIdentity:
        """The immutable claim every record of this session carries."""
        return CaptureIdentity(
            session_id=self.session_id,
            pipeline_fingerprint=self.pipeline_fingerprint,
            capture_plan_fingerprint=self.capture_plan_fingerprint,
            request_spec_fingerprint=self.request_spec_fingerprint,
            normalization_recipe_fingerprint=self.normalization_recipe_fingerprint,
            capture_origin=self.capture_origin,
            operation_id=self.operation_id,
            operation_fingerprint=self.operation_fingerprint,
            normalization_recipe_hash=self.normalization_recipe_hash,
            requested_as_of=self.requested_as_of,
            effective_valuation_timestamp=self.effective_valuation_timestamp,
            valuation_timestamp_rule=self.valuation_timestamp_rule,
            expected_universe_fingerprint=self.expected_universe_fingerprint,
            open_interest_date_rule_fingerprint=(
                self.open_interest_date_rule_fingerprint
            ),
            spot_synchronization_policy_fingerprint=(
                self.spot_synchronization_policy_fingerprint
            ),
        )

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def mark(self) -> int:
        """Where this session's record list currently ends.

        Pass the result to ``RawCaptureManifest.from_session(session, since=...)``
        to build a manifest of only what was captured afterwards.

        v2.1.4 always took the whole list, so a session reused for a second chain
        pull gave the second snapshot a manifest naming the first snapshot's
        responses. A provenance record listing bytes that produced a different
        number is worse than no provenance record, because it looks like one.
        """
        return len(self.captured)

    def capture(
        self,
        *,
        endpoint: str,
        query_params: dict[str, Any],
        payload: str | bytes,
        request_started_at: datetime,
        response_received_at: datetime,
        http_status: int,
        request_id: str = "",
        capture_origin: CaptureOrigin | None = None,
        decode: Mapping[str, Any] | None = None,
        response_headers: Mapping[str, str] | None = None,
    ) -> RawResponseRecord:
        sequence = self.next_sequence()
        record = self.store.put(
            record_id=build_record_id(
                session_id=self.session_id,
                sequence=sequence,
                endpoint=endpoint,
                query_params=query_params,
                payload=payload,
            ),
            endpoint=endpoint,
            query_params=query_params,
            payload=payload,
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            http_status=http_status,
            request_id=request_id,
            request_sequence=sequence,
            identity=(
                self.identity
                if capture_origin is None
                else replace(self.identity, capture_origin=capture_origin)
            ),
            decode=decode,
            response_headers=response_headers,
        )
        self.captured.append(record)
        return record

    def manifest(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "parser_version": PARSER_VERSION,
            "records": [record.as_dict() for record in self.captured],
        }


@dataclass(frozen=True, slots=True)
class ManifestRecord:
    """One captured response, described completely.

    v2.1.5 kept the same information in four parallel structures: a tuple of
    record ids, a tuple of payload hashes, a tuple of request ids and a dict of
    parameter hashes. Nothing tied the entries together, so verification could
    only compare *sets* -- and two records that swapped payload hashes still
    matched, because the multiset was unchanged.

    Here every field belongs to a named record, and the manifest hash covers the
    descriptor rather than four independently sorted lists.
    """

    record_id: str
    endpoint: str
    payload_hash: str
    parameter_hash: str
    #: Where the bytes are, relative to the store root. In the manifest hash
    #: since v2.1.15: a location that can be edited without changing the
    #: manifest's identity is a location that can lie about where evidence is.
    payload_location: str = ""
    request_id: str = ""
    request_sequence: int = 0
    http_status: int = 200
    request_started_at: datetime | None = None
    response_received_at: datetime | None = None
    parser_version: str = PARSER_VERSION
    vendor_schema_version: str | None = None
    capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN
    #: How many bytes the vendor actually sent. In the hash because a payload
    #: that hashes the same but is a different length is not the same payload.
    byte_length: int = 0
    capture_complete: bool = True
    #: Copied from the record, not from the manifest. These are what let
    #: verification refuse a capture relabelled as another pipeline's.
    capture_session_id: str = ""
    pipeline_fingerprint: str = ""
    capture_plan_fingerprint: str = ""
    request_spec_fingerprint: str = ""
    normalization_recipe_fingerprint: str = ""
    #: Which capture operation issued this record. A session may run several.
    operation_id: str = ""
    operation_fingerprint: str = ""
    normalization_recipe_hash: str = ""
    requested_as_of: datetime | None = None
    effective_valuation_timestamp: datetime | None = None
    valuation_timestamp_rule: str = ""
    #: The universe this operation expected, and the rule establishing the
    #: open-interest settlement date. Both change what a chain *means* --
    #: completeness, confidence, and the weight on every GEX term -- so both are
    #: fixed by the operation rather than supplied at calculation time. v2.1.7
    #: took the expected universe as an argument to the calculation, so one
    #: capture could be scored MEASURED_COMPLETE and replayed PARTIALLY_OBSERVED.
    expected_universe_fingerprint: str = ""
    open_interest_date_rule_fingerprint: str = ""
    #: The remaining field the operation digest covers. Stored explicitly since
    #: v2.1.9 so ``verify_capture`` can recompute that digest from the record
    #: rather than comparing stored digests to each other and calling it checked.
    spot_synchronization_policy_fingerprint: str = ""
    #: What the stored bytes *are*, and under which rules. Written since v2.1.14;
    #: before that the constant existed and nothing recorded it, so a record gave
    #: no way to tell whether its digest covered entity bytes or a re-encoding of
    #: decoded text.
    raw_response_schema_version: str = RAW_RESPONSE_SCHEMA_VERSION
    body_representation: str = BODY_REPRESENTATION
    #: The allow-listed response headers, kept because several of them decide
    #: what the bytes mean and the rest are what an audit asks for.
    response_headers: Mapping[str, str] = field(default_factory=dict)
    #: How the *parser* read those bytes. Descriptive only: the bytes and their
    #: digest stay authoritative, and this says what one reading of them was.
    content_type: str = ""
    declared_charset: str = ""
    selected_charset: str = ""
    decode_status: str = ""
    decoded_text_hash: str = ""

    @property
    def interpretive_headers(self) -> dict[str, str]:
        """The retained headers that change how these bytes read.

        Content type and content encoding decide the charset and whether the
        body was compressed on the wire. A replay under a different one is a
        different reading of the same bytes, so they belong to the record's
        identity rather than beside it.
        """
        from src.adapters.http_attempts import INTERPRETIVE_RESPONSE_HEADERS

        held = {k.lower(): v for k, v in dict(self.response_headers).items()}
        return {
            name: held[name] for name in INTERPRETIVE_RESPONSE_HEADERS if name in held
        }

    @property
    def raw_response_semantics(self) -> dict[str, Any]:
        """The v2.1.14 block, as one thing, so it travels as one thing."""
        return {
            "payload_location": self.payload_location,
            "raw_response_schema_version": self.raw_response_schema_version,
            "body_representation": self.body_representation,
            "content_type": self.content_type,
            "declared_charset": self.declared_charset,
            "selected_charset": self.selected_charset,
            "decode_status": self.decode_status,
            "decoded_text_hash": self.decoded_text_hash,
            "response_headers": dict(sorted(self.response_headers.items())),
        }

    @property
    def capture_identity(self) -> CaptureIdentity:
        return CaptureIdentity(
            session_id=self.capture_session_id,
            pipeline_fingerprint=self.pipeline_fingerprint,
            capture_plan_fingerprint=self.capture_plan_fingerprint,
            request_spec_fingerprint=self.request_spec_fingerprint,
            normalization_recipe_fingerprint=self.normalization_recipe_fingerprint,
            capture_origin=self.capture_origin,
            operation_id=self.operation_id,
            operation_fingerprint=self.operation_fingerprint,
            normalization_recipe_hash=self.normalization_recipe_hash,
            requested_as_of=self.requested_as_of,
            effective_valuation_timestamp=self.effective_valuation_timestamp,
            valuation_timestamp_rule=self.valuation_timestamp_rule,
            expected_universe_fingerprint=self.expected_universe_fingerprint,
            open_interest_date_rule_fingerprint=(
                self.open_interest_date_rule_fingerprint
            ),
            spot_synchronization_policy_fingerprint=(
                self.spot_synchronization_policy_fingerprint
            ),
        )

    @classmethod
    def of(cls, record: RawResponseRecord) -> ManifestRecord:
        return cls(
            record_id=record.record_id,
            payload_location=record.payload_location,
            endpoint=record.endpoint,
            payload_hash=record.payload_hash,
            parameter_hash=canonical_parameter_hash(record.query_params),
            request_id=record.request_id,
            request_sequence=record.request_sequence,
            http_status=record.http_status,
            request_started_at=record.request_started_at,
            response_received_at=record.response_received_at,
            parser_version=record.parser_version,
            vendor_schema_version=record.vendor_schema_version,
            capture_origin=record.capture_origin,
            byte_length=record.byte_length,
            capture_complete=record.capture_complete,
            capture_session_id=record.capture_session_id,
            pipeline_fingerprint=record.pipeline_fingerprint,
            capture_plan_fingerprint=record.capture_plan_fingerprint,
            request_spec_fingerprint=record.request_spec_fingerprint,
            normalization_recipe_fingerprint=record.normalization_recipe_fingerprint,
            operation_id=record.operation_id,
            operation_fingerprint=record.operation_fingerprint,
            normalization_recipe_hash=record.normalization_recipe_hash,
            requested_as_of=record.requested_as_of,
            effective_valuation_timestamp=record.effective_valuation_timestamp,
            valuation_timestamp_rule=record.valuation_timestamp_rule,
            expected_universe_fingerprint=record.expected_universe_fingerprint,
            open_interest_date_rule_fingerprint=(
                record.open_interest_date_rule_fingerprint
            ),
            spot_synchronization_policy_fingerprint=(
                record.spot_synchronization_policy_fingerprint
            ),
        )

    def semantic_payload(self) -> dict[str, Any]:
        """Everything a change to which changes what this record *is*.

        All of it enters the manifest hash. A different request id, sequence,
        status or clock describes a different response, whatever the bytes say.
        """
        return {
            "record_id": self.record_id,
            "endpoint": self.endpoint,
            "payload_hash": self.payload_hash,
            "parameter_hash": self.parameter_hash,
            "request_id": self.request_id,
            "request_sequence": self.request_sequence,
            "http_status": self.http_status,
            "request_started_at": (
                self.request_started_at.isoformat() if self.request_started_at else None
            ),
            "response_received_at": (
                self.response_received_at.isoformat()
                if self.response_received_at
                else None
            ),
            "parser_version": self.parser_version,
            "vendor_schema_version": self.vendor_schema_version,
            "capture_origin": self.capture_origin.value,
            "byte_length": self.byte_length,
            "capture_complete": self.capture_complete,
            **self.raw_response_semantics,
            **self.capture_identity.as_dict(),
        }

    def as_dict(self) -> dict[str, Any]:
        return self.semantic_payload()


@dataclass(frozen=True, slots=True)
class RawCaptureManifest:
    """Which raw records a normalized snapshot was actually built from.

    Without this, a stored chain and a directory of captured payloads are two
    unrelated artefacts that happen to share a timestamp. Reconstructing which
    bytes produced which number -- the entire reason for capturing raw payloads
    -- meant guessing from filenames.

    ``manifest_hash`` covers the full per-record descriptors, so a snapshot
    whose sources changed in *any* audit-relevant way cannot present the same
    manifest.
    """

    session_id: str
    records: tuple[ManifestRecord, ...] = ()
    #: False when capture was disabled. Recorded explicitly rather than left to
    #: an absent key, which reads the same as "we forgot".
    capture_enabled: bool = True
    #: The plan this capture was taken against, so a later reader can tell what
    #: the capture was *meant* to contain rather than only what it does.
    capture_plan_fingerprint: str = ""
    #: Which code read these bytes, and which configuration asked for them.
    parser_version: str = PARSER_VERSION
    pipeline_fingerprint: str = ""
    #: The shape of this evidence. An older manifest is refused rather than
    #: reinterpreted -- v2.1.5's parallel arrays cannot express the per-record
    #: binding this one verifies.
    schema_version: str = MANIFEST_SCHEMA_VERSION
    #: What the manifest *says* the origin was. Descriptive only: read
    #: ``capture_origin``, which is derived from the records.
    declared_capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN

    @property
    def capture_origin(self) -> CaptureOrigin:
        """Where these bytes actually came from, according to the records.

        Derived, because v2.1.6 stored it as a plain field on the manifest and
        the manifest is a document: relabelling an offline fixture capture as
        ``LIVE_HTTP_CAPTURE`` was one assignment, and every live-capture check
        downstream read the relabelled value.

        A mixed capture is ``UNKNOWN_ORIGIN`` rather than a majority vote. Two
        transports in one session is not one session, and calling it either
        would be a claim about bytes that came from somewhere else.
        """
        origins = {record.capture_origin for record in self.records}
        if len(origins) == 1:
            return origins.pop()
        return CaptureOrigin.UNKNOWN_ORIGIN

    @property
    def origin_is_uniform(self) -> bool:
        """False when the records disagree, or when there are none to ask."""
        return len({record.capture_origin for record in self.records}) == 1

    @property
    def declared_origin_matches_records(self) -> bool:
        """Whether the document's claim survives contact with the evidence.

        An unmade claim (``UNKNOWN_ORIGIN``) is not a wrong one; a *different*
        one is, and verification refuses it.
        """
        if self.declared_capture_origin is CaptureOrigin.UNKNOWN_ORIGIN:
            return True
        return self.declared_capture_origin is self.capture_origin

    @property
    def record_ids(self) -> tuple[str, ...]:
        return tuple(sorted(r.record_id for r in self.records))

    @property
    def payload_hashes(self) -> tuple[str, ...]:
        return tuple(sorted(r.payload_hash for r in self.records))

    @property
    def request_ids(self) -> tuple[str, ...]:
        return tuple(sorted(r.request_id for r in self.records if r.request_id))

    @property
    def endpoint_records(self) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, list[str]] = {}
        for record in self.records:
            grouped.setdefault(record.endpoint, []).append(record.record_id)
        return {
            endpoint: tuple(sorted(ids)) for endpoint, ids in sorted(grouped.items())
        }

    @property
    def request_parameter_hashes(self) -> dict[str, str]:
        return {
            r.record_id: r.parameter_hash
            for r in sorted(self.records, key=lambda r: r.record_id)
        }

    def record(self, record_id: str) -> ManifestRecord | None:
        for candidate in self.records:
            if candidate.record_id == record_id:
                return candidate
        return None

    @property
    def manifest_hash(self) -> str:
        """Full SHA-256 over sorted per-record descriptors.

        v2.1.4 used the first sixteen hex characters; v2.1.5 widened it but
        still hashed four independently sorted lists, so swapping two records'
        payload hashes left the digest unchanged. The descriptor is the unit.
        """
        payload = json.dumps(
            {
                "schema_version": self.schema_version,
                "session_id": self.session_id,
                "capture_enabled": self.capture_enabled,
                "capture_plan_fingerprint": self.capture_plan_fingerprint,
                "parser_version": self.parser_version,
                "pipeline_fingerprint": self.pipeline_fingerprint,
                "capture_origin": self.capture_origin.value,
                "declared_capture_origin": self.declared_capture_origin.value,
                "records": sorted(
                    (record.semantic_payload() for record in self.records),
                    key=lambda entry: entry["record_id"],
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def records_for(self, endpoint: str) -> tuple[str, ...]:
        return tuple(
            sorted(r.record_id for r in self.records if r.endpoint == endpoint)
        )

    @property
    def endpoints(self) -> frozenset[str]:
        return frozenset(r.endpoint for r in self.records)

    @classmethod
    def disabled(cls) -> RawCaptureManifest:
        """Raw capture was off. Stated, not implied."""
        return cls(session_id="", capture_enabled=False)

    @classmethod
    def rebuilt_from(cls, payload: dict[str, Any]) -> RawCaptureManifest:
        """Reconstruct a manifest from a stored ``semantic_payload``.

        Added in v2.1.11 so a *source* capture's manifest can be persisted
        beside the artifacts it supports and re-verified later. A universe is
        resolved against records that belong to an earlier operation, so the
        chain's own manifest does not name them; without this, recovery would
        have to rebuild the claim from the store it is supposed to be checked
        against, which checks nothing.

        The manifest hash is not stored and not trusted: it is recomputed from
        the reconstructed descriptors, so a payload edited in the artifact store
        produces a manifest that no longer matches the digest the resolution
        receipt recorded.
        """
        return cls(
            session_id=str(payload.get("session_id", "")),
            records=tuple(
                sorted(
                    (
                        ManifestRecord(
                            record_id=entry["record_id"],
                            endpoint=entry["endpoint"],
                            payload_hash=entry["payload_hash"],
                            parameter_hash=entry["parameter_hash"],
                            payload_location=entry.get("payload_location", ""),
                            request_id=entry.get("request_id", ""),
                            request_sequence=int(entry.get("request_sequence", 0)),
                            http_status=int(entry.get("http_status", 200)),
                            request_started_at=_moment_or_none(
                                entry.get("request_started_at")
                            ),
                            response_received_at=_moment_or_none(
                                entry.get("response_received_at")
                            ),
                            parser_version=entry.get("parser_version", PARSER_VERSION),
                            vendor_schema_version=entry.get("vendor_schema_version"),
                            capture_origin=CaptureOrigin(
                                entry.get(
                                    "capture_origin", CaptureOrigin.UNKNOWN_ORIGIN.value
                                )
                            ),
                            byte_length=int(entry.get("byte_length", 0)),
                            capture_complete=bool(entry.get("capture_complete", True)),
                            capture_session_id=entry.get("capture_session_id", ""),
                            pipeline_fingerprint=entry.get("pipeline_fingerprint", ""),
                            capture_plan_fingerprint=entry.get(
                                "capture_plan_fingerprint", ""
                            ),
                            request_spec_fingerprint=entry.get(
                                "request_spec_fingerprint", ""
                            ),
                            normalization_recipe_fingerprint=entry.get(
                                "normalization_recipe_fingerprint", ""
                            ),
                            operation_id=entry.get("operation_id", ""),
                            operation_fingerprint=entry.get(
                                "operation_fingerprint", ""
                            ),
                            normalization_recipe_hash=entry.get(
                                "normalization_recipe_hash", ""
                            ),
                            requested_as_of=_moment_or_none(
                                entry.get("requested_as_of")
                            ),
                            effective_valuation_timestamp=_moment_or_none(
                                entry.get("effective_valuation_timestamp")
                            ),
                            valuation_timestamp_rule=entry.get(
                                "valuation_timestamp_rule", ""
                            ),
                            expected_universe_fingerprint=entry.get(
                                "expected_universe_fingerprint", ""
                            ),
                            open_interest_date_rule_fingerprint=entry.get(
                                "open_interest_date_rule_fingerprint", ""
                            ),
                            spot_synchronization_policy_fingerprint=entry.get(
                                "spot_synchronization_policy_fingerprint", ""
                            ),
                            # **Restored explicitly, not left to defaults.** A
                            # reconstructor whose round-trip works because the
                            # current defaults happen to match what was written
                            # is one release away from silently rebuilding a
                            # different manifest -- and the manifest hash is
                            # what says two captures are the same capture.
                            raw_response_schema_version=entry.get(
                                "raw_response_schema_version",
                                RAW_RESPONSE_SCHEMA_VERSION,
                            ),
                            body_representation=entry.get(
                                "body_representation", BODY_REPRESENTATION
                            ),
                            content_type=entry.get("content_type", ""),
                            declared_charset=entry.get("declared_charset", ""),
                            selected_charset=entry.get("selected_charset", ""),
                            decode_status=entry.get("decode_status", ""),
                            decoded_text_hash=entry.get("decoded_text_hash", ""),
                            response_headers=dict(
                                entry.get("response_headers", {}) or {}
                            ),
                        )
                        for entry in payload.get("records", ())
                    ),
                    key=lambda entry: entry.record_id,
                )
            ),
            capture_enabled=bool(payload.get("capture_enabled", True)),
            capture_plan_fingerprint=payload.get("capture_plan_fingerprint", ""),
            pipeline_fingerprint=payload.get("pipeline_fingerprint", ""),
            parser_version=payload.get("parser_version", PARSER_VERSION),
            schema_version=payload.get("schema_version", MANIFEST_SCHEMA_VERSION),
            declared_capture_origin=CaptureOrigin(
                payload.get(
                    "declared_capture_origin", CaptureOrigin.UNKNOWN_ORIGIN.value
                )
            ),
        )

    def semantic_payload(self) -> dict[str, Any]:
        """Everything :meth:`rebuilt_from` needs, and nothing derived."""
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "capture_enabled": self.capture_enabled,
            "capture_plan_fingerprint": self.capture_plan_fingerprint,
            "pipeline_fingerprint": self.pipeline_fingerprint,
            "parser_version": self.parser_version,
            "declared_capture_origin": self.declared_capture_origin.value,
            "records": [record.semantic_payload() for record in self.records],
        }

    @classmethod
    def from_session(
        cls,
        session: CaptureSession,
        *,
        since: int = 0,
        capture_plan_fingerprint: str = "",
        pipeline_fingerprint: str = "",
    ) -> RawCaptureManifest:
        """The records this snapshot used, and only those.

        ``since`` is a mark taken from ``CaptureSession.mark()`` before the
        fetch. v2.1.4 always took the whole session, so a second chain pull
        inherited the first pull's responses -- a provenance record naming bytes
        that produced a different number.
        """
        captured = tuple(session.captured[since:])
        origins = {r.capture_origin for r in captured}
        return cls(
            session_id=session.session_id,
            records=tuple(
                sorted(
                    (ManifestRecord.of(record) for record in captured),
                    key=lambda entry: entry.record_id,
                )
            ),
            capture_plan_fingerprint=capture_plan_fingerprint,
            pipeline_fingerprint=pipeline_fingerprint,
            # Declared to match what the records say, so the two agree by
            # construction here. Verification still checks, because a manifest
            # can be edited after this function returns.
            declared_capture_origin=(
                origins.pop() if len(origins) == 1 else CaptureOrigin.UNKNOWN_ORIGIN
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "capture_enabled": self.capture_enabled,
            "capture_origin": self.capture_origin.value,
            "declared_capture_origin": self.declared_capture_origin.value,
            "origin_is_uniform": self.origin_is_uniform,
            "record_count": len(self.records),
            "records": [record.as_dict() for record in self.records],
            "record_ids": list(self.record_ids),
            "payload_hashes": list(self.payload_hashes),
            "request_ids": list(self.request_ids),
            "endpoint_records": {
                endpoint: list(ids) for endpoint, ids in self.endpoint_records.items()
            },
            "request_parameter_hashes": dict(self.request_parameter_hashes),
            "capture_plan_fingerprint": self.capture_plan_fingerprint,
            "parser_version": self.parser_version,
            "pipeline_fingerprint": self.pipeline_fingerprint,
            "manifest_hash": self.manifest_hash,
        }
