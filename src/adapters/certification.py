"""Whether a paid ThetaData session would produce evidence worth having.

**This is not a trading readiness check.** Nothing in this repository can place
an order, and nothing here changes that. The question is narrower and entirely
about data provenance: if we spend one session capturing real vendor responses,
will anybody be able to reconstruct afterwards what those numbers meant?

Two vendor-dependent unknowns are handled explicitly rather than guessed:

**Open interest as-of.** ThetaData's snapshot endpoints do not state which
settlement date their open interest belongs to. v2.1.1 accepted a caller-supplied
date and stored it in the same field as an observed one, so the snapshot could
not distinguish "the vendor said 16 March" from "we assumed 16 March". Open
interest is the weight on every GEX term; a date we chose is not evidence about
the date.

**Synchronised spot.** The spot print and the option chain are separate reads.
If they are minutes apart then every gamma is computed against an underlying the
chain never saw. Nothing in v2.1.1 required the two clocks to be close, or even
recorded how far apart they were.

Three v2.1.4 corrections shape the rest of this module:

* **Capture readiness and calculation trust are different questions.** v2.1.3
  had one ladder, so an unresolved vendor convention blocked the capture that
  would help resolve it. Unknown pricing now permits a raw capture and never
  permits a trusted calculation.
* **Evidence is typed and verified.** ``assess_readiness(capture_manifest=
  object(), validation_report=object())`` returned ``ADAPTER_CERTIFIED``. Both
  parameters were ``Any`` and both were tested with ``is not None``, so the
  strongest claim the repository can make was one truthy value away.
* **Provenance is graded, not asserted.** A ``caller_supplied=False`` boolean is
  a claim by the caller about the caller. Observation now means a reference to a
  stored raw record.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from src.adapters.errors import (
    ThetaDataCertificationError,
    ThetaDataProvenanceError,
)
from src.adapters.open_interest import (
    EvidenceKind,
    OpenInterestAsOfEvidence,
    OpenInterestValueObservation,
)
from src.adapters.raw_store import (
    MANIFEST_SCHEMA_VERSION,
    PARSER_VERSION,
    SUPPORTED_PARSER_VERSIONS,
    RawCaptureManifest,
    canonical_parameter_hash,
    probe_raw_store,
    record_id_belongs_to,
)
from src.adapters.thetadata.capture_plan import CapturePlan, capture_plan_for
from src.adapters.thetadata.endpoints import Tier
from src.adapters.validation import (
    AdapterValidationReport,
    AdapterValidator,
    ValidationCheck,
    VerifiedFieldObservation,
)
from src.config.compatibility import (
    EvidenceSource,
    derive_post_capture_compatibility,
)

__all__ = [
    "CERTIFICATION_SCHEMA_VERSION",
    "AdapterCertificationReadiness",
    "AdapterValidationReport",
    "AdapterValidator",
    "CaptureVerification",
    "CertificationState",
    "EvidenceKind",
    "OpenInterestAsOfEvidence",
    "OpenInterestProvenance",
    "OpenInterestSource",
    "OpenInterestValueObservation",
    "ProvenanceGrade",
    "SpotProvenance",
    "SpotSource",
    "ValidationCheck",
    "VerifiedCalculationContext",
    "VerifiedFieldObservation",
    "assess_readiness",
    "build_verified_calculation_context",
    "verify_capture",
]

#: Bumped when the *meaning* of a certification report changes, so a stored
#: report says which rules produced it. v2.1.4 split the states and added typed
#: capture and validation evidence, which changes how every field reads.
CERTIFICATION_SCHEMA_VERSION = "adapter-certification/2.1.8"

#: Stamped onto every readiness report so the object cannot be quoted out of
#: context as clearance for anything else.
CERTIFICATION_SCOPE = (
    "Adapter data-capture readiness only. This is NOT a trading readiness "
    "check: this repository has no broker, no order type and no execution "
    "path, and readiness here confers none."
)


class ProvenanceGrade(str, Enum):
    """How well a provenance claim is supported.

    The three are ordered, and the distinction is the whole point: v2.1.3
    recorded a ``caller_supplied`` boolean, which is the caller describing its
    own confidence. A grade is derived from what the object can point at.
    """

    #: What the configuration intends to fetch. No response has been seen.
    PLANNED = "PLANNED"
    #: Read out of a stored raw record, which the evidence names.
    OBSERVED = "OBSERVED"
    #: Observed, and checked by a validation report bound to that capture.
    VALIDATED = "VALIDATED"

    @property
    def is_observation(self) -> bool:
        return self in (ProvenanceGrade.OBSERVED, ProvenanceGrade.VALIDATED)


class SpotSource(str, Enum):
    """Which print the underlying came from.

    An enum rather than a string because ``source="whatever_i_like"`` used to
    pass, and the certification report then named a source nobody could look up.
    """

    VENDOR_INDEX_SNAPSHOT = "vendor_index_snapshot"
    VENDOR_PER_CONTRACT = "vendor_per_contract"
    CALLER_SUPPLIED = "caller_supplied"
    SYNTHETIC = "synthetic"

    @property
    def is_vendor_observed(self) -> bool:
        return self in (
            SpotSource.VENDOR_INDEX_SNAPSHOT,
            SpotSource.VENDOR_PER_CONTRACT,
        )


class OpenInterestSource(str, Enum):
    """Where the open-interest settlement date came from."""

    VENDOR_FIELD = "vendor_field"
    CALLER = "caller"
    SYNTHETIC = "synthetic"


#: How far ahead of now a timestamp may be before it is a mistake rather than
#: clock skew. Generous, because the point is to catch a date somebody typed.
FUTURE_TOLERANCE_SECONDS = 3600.0


def _require_a_plain_date(value: object, *, field: str) -> None:
    """A calendar date, and not a datetime pretending to be one.

    ``datetime`` subclasses ``date``, so ``isinstance(value, date)`` accepted
    both -- and the two compare and serialise differently. A settlement date
    carrying a time of day is a date somebody constructed from a timestamp
    without deciding which session it belongs to.
    """
    if value is None or type(value) is date:
        return
    if isinstance(value, datetime):
        raise ThetaDataProvenanceError(
            f"{field} must be a date, got a datetime ({value.isoformat()}). "
            "Open interest settles per session, not per instant; call .date() "
            "and decide which session you mean."
        )
    raise ThetaDataProvenanceError(
        f"{field} must be a date, got {type(value).__name__} {value!r}"
    )


@dataclass(frozen=True, slots=True)
class OpenInterestProvenance:
    """Where the open-interest settlement date came from."""

    as_of: date | None
    source: OpenInterestSource | str
    #: The session the chain belongs to. Open interest settles *before* the
    #: chain it weights, so a date after it is not a stale figure -- it is a
    #: figure from a session that has not happened.
    chain_date: date | None = None
    #: Present only when the date was read out of a stored response.
    observation: VerifiedFieldObservation | None = None

    def __post_init__(self) -> None:
        try:
            resolved = OpenInterestSource(self.source)
        except ValueError as error:
            raise ThetaDataProvenanceError(
                f"{self.source!r} is not a recognised open-interest source; "
                f"valid values are {[s.value for s in OpenInterestSource]}"
            ) from error
        object.__setattr__(self, "source", resolved)

        for name in ("as_of", "chain_date"):
            _require_a_plain_date(getattr(self, name), field=name)
        if (
            self.as_of is not None
            and self.chain_date is not None
            and self.as_of > self.chain_date
        ):
            raise ThetaDataProvenanceError(
                f"open interest as_of {self.as_of.isoformat()} is after the "
                f"chain date {self.chain_date.isoformat()}. Open interest "
                "settles before the session it weights; a later date describes "
                "a session that has not happened."
            )

    @property
    def claims_observation(self) -> bool:
        return (
            self.as_of is not None
            and bool(self.source)
            and self.observation is not None
        )

    def as_dict(self) -> dict[str, Any]:
        source = self.source
        return {
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "source": source.value if isinstance(source, Enum) else str(source),
            "chain_date": self.chain_date.isoformat() if self.chain_date else None,
            "claims_observation": self.claims_observation,
            "observation": (self.observation.as_dict() if self.observation else None),
        }


@dataclass(frozen=True, slots=True)
class SpotProvenance:
    """Which underlying print was used, when it was taken, and how close it was."""

    source: SpotSource | str
    timestamp: datetime | None
    #: How far the spot print may be from the chain instant before the pairing
    #: stops being meaningful. A local policy, not a vendor fact.
    tolerance_seconds: float = 1.0
    observation: VerifiedFieldObservation | None = None

    def __post_init__(self) -> None:
        try:
            resolved = SpotSource(self.source)
        except ValueError as error:
            raise ThetaDataProvenanceError(
                f"{self.source!r} is not a recognised spot source; valid values "
                f"are {[s.value for s in SpotSource]}"
            ) from error
        object.__setattr__(self, "source", resolved)

        if not isinstance(self.tolerance_seconds, int | float) or isinstance(
            self.tolerance_seconds, bool
        ):
            raise ThetaDataProvenanceError(
                f"tolerance_seconds must be a number, got "
                f"{type(self.tolerance_seconds).__name__}"
            )
        if not math.isfinite(self.tolerance_seconds):
            raise ThetaDataProvenanceError(
                f"tolerance_seconds is {self.tolerance_seconds}. A non-finite "
                "tolerance compares true against every skew, so nothing would "
                "ever be out of tolerance."
            )
        if self.tolerance_seconds < 0:
            raise ThetaDataProvenanceError(
                f"tolerance_seconds is {self.tolerance_seconds}; a negative "
                "tolerance rejects every spot, including a perfectly "
                "synchronised one"
            )

        if self.timestamp is not None:
            if not isinstance(self.timestamp, datetime):
                # An ISO string and an epoch integer both *denote* an instant,
                # and neither is one. v2.1.5 went straight to ``.tzinfo`` and
                # leaked an AttributeError out of a provenance constructor.
                raise ThetaDataProvenanceError(
                    f"spot timestamp must be a timezone-aware datetime, got "
                    f"{type(self.timestamp).__name__} {self.timestamp!r}. Parse "
                    "it before it gets here: this field is compared against the "
                    "chain instant, and a string cannot be."
                )
            if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
                raise ThetaDataProvenanceError(
                    "spot timestamp must be timezone-aware; a naive datetime "
                    "silently means whatever the reading machine's zone is, and "
                    "the whole point of this field is comparing two clocks"
                )
            ahead = (self.timestamp - datetime.now(UTC)).total_seconds()
            if ahead > FUTURE_TOLERANCE_SECONDS:
                raise ThetaDataProvenanceError(
                    f"spot timestamp {self.timestamp.isoformat()} is "
                    f"{ahead / 3600:.1f}h in the future. A print that has not "
                    "happened cannot have been read."
                )

    @property
    def claims_observation(self) -> bool:
        return (
            self.timestamp is not None
            and bool(self.source)
            and self.observation is not None
        )

    def skew_seconds(self, as_of: datetime) -> float | None:
        if self.timestamp is None:
            return None
        skew = abs((as_of - self.timestamp).total_seconds())
        if not math.isfinite(skew):
            raise ThetaDataProvenanceError(
                "spot skew is not finite; one of the two clocks is unusable"
            )
        return skew

    def as_dict(self, as_of: datetime | None = None) -> dict[str, Any]:
        source = self.source
        return {
            "source": source.value if isinstance(source, Enum) else str(source),
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "tolerance_seconds": self.tolerance_seconds,
            "skew_seconds": self.skew_seconds(as_of) if as_of else None,
            "claims_observation": self.claims_observation,
            "observation": (self.observation.as_dict() if self.observation else None),
        }


# =============================================================================
# Capture evidence -- typed, and checked against the store that holds it
# =============================================================================


@dataclass(frozen=True, slots=True)
class CaptureVerification:
    """A capture manifest that has been checked against its store.

    Produced only by ``verify_capture``. There is no constructor path that says
    "a capture happened" without a store having agreed, which is what
    ``capture_manifest: Any`` allowed.
    """

    manifest: RawCaptureManifest
    #: Record ids the manifest claims that the store actually holds, with a
    #: payload whose hash matches.
    confirmed_record_ids: tuple[str, ...] = ()
    #: Claims the store could not support. Non-empty means not verified.
    failures: tuple[str, ...] = ()
    store_description: str = ""
    #: Which capture plan this was checked against. Empty means the endpoint
    #: requirements were not checked at all, which certification treats as
    #: unverified rather than as "no requirements".
    plan_fingerprint: str = ""
    #: The pipeline the manifest was required to match. Empty means verification
    #: was not told which configuration to expect, and an unanchored check is
    #: recorded as such rather than passing quietly.
    expected_pipeline_fingerprint: str = ""

    @property
    def verified(self) -> bool:
        return (
            not self.failures
            and self.manifest.capture_enabled
            and bool(self.confirmed_record_ids)
            # A verification that named no pipeline and no plan checked the
            # records against nothing in particular. It is not a verdict.
            and bool(self.expected_pipeline_fingerprint)
            and bool(self.plan_fingerprint)
            # Every record the manifest claims has to be one of the confirmed
            # ones. Without this a manifest could carry an unconfirmable record
            # alongside good ones and still read as verified.
            and len(self.confirmed_record_ids) == len(self.manifest.records)
        )

    @property
    def manifest_hash(self) -> str:
        return self.manifest.manifest_hash

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "manifest_hash": self.manifest_hash,
            "manifest": self.manifest.as_dict(),
            "confirmed_record_ids": list(self.confirmed_record_ids),
            "failures": list(self.failures),
            "store_description": self.store_description,
            "plan_fingerprint": self.plan_fingerprint,
            "expected_pipeline_fingerprint": self.expected_pipeline_fingerprint,
        }


def grade_claim(
    claim: OpenInterestProvenance | SpotProvenance | None,
    *,
    manifest: RawCaptureManifest | None,
    store: Any,
    validated: bool,
) -> tuple[ProvenanceGrade, str]:
    """Decide what a provenance claim is actually worth.

    Returns the grade and, when the claim does not hold, a complaint naming why.

    v2.1.4 checked that the named record existed. v2.1.5 re-reads the field out
    of the payload and compares the value, because "record r1 exists" says
    nothing about whether r1 contains an open interest, whether it holds the
    number claimed, or whether its endpoint has such a column at all.
    """
    if claim is None or not claim.claims_observation:
        return ProvenanceGrade.PLANNED, ""

    observation = claim.observation
    assert observation is not None  # implied by claims_observation

    if manifest is None:
        return ProvenanceGrade.PLANNED, (
            f"evidence names raw record {observation.record_id!r}, but no "
            "capture was supplied, so nothing confirms that record exists"
        )
    try:
        AdapterValidator.confirm_field(
            manifest=manifest, store=store, observation=observation
        )
    except ThetaDataProvenanceError as error:
        return ProvenanceGrade.PLANNED, str(error)
    if validated:
        return ProvenanceGrade.VALIDATED, ""
    return ProvenanceGrade.OBSERVED, ""


def _render_parameter(value: Any) -> str:
    """Render a stored query value the way the request spec renders it.

    Text on both sides, because text is what reaches the wire: ``4.2`` and
    ``"4.2"`` are the same request, and comparing a float against a value read
    back from JSON would fail on formatting rather than on substance.
    """
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def verify_capture(
    manifest: RawCaptureManifest,
    store: Any,
    *,
    plan: CapturePlan | None = None,
    expected_pipeline_fingerprint: str = "",
    expected_identity: Any = None,
    expected_request_spec: Any = None,
) -> CaptureVerification:
    """Check that a manifest describes the capture it claims to describe.

    Three questions. v2.1.4 asked only the first -- are the named records in the
    store? -- so a one-record quote snapshot with no open interest, no implied
    volatility and no underlying verified cleanly and advanced the ladder. v2.1.5
    added the second: does the manifest claim *enough*? That is ``plan``.

    v2.1.6 adds the third, which is the one that matters here: is every
    audit-relevant field of every record bound to the bytes in the store? The
    manifest used to be believed about its own request ids, parameter hashes,
    statuses and clocks. Only the payload hashes were checked, and only as a
    multiset. A manifest is a claim about evidence; each field of it now has to
    survive being compared with the evidence.

    Both ``plan`` and ``expected_pipeline_fingerprint`` default to "absent" so
    that a caller who forgets one gets a *failed verification* rather than a
    ``TypeError`` -- but absent is a failure, not a skip. An empty fingerprint
    verifying against anything is exactly the hole this closes.
    """
    if not isinstance(manifest, RawCaptureManifest):
        raise ThetaDataCertificationError(
            f"verify_capture needs a RawCaptureManifest, got {type(manifest).__name__}"
        )

    failures: list[str] = []
    if not manifest.capture_enabled:
        failures.append("CAPTURE_DISABLED: the manifest records that capture was off")
    if not manifest.records:
        failures.append("EMPTY_MANIFEST: no record ids were captured")

    # -- what this capture was taken by, and against ------------------------
    if manifest.schema_version != MANIFEST_SCHEMA_VERSION:
        # Refused rather than reinterpreted: v2.1.5's parallel arrays cannot
        # express the per-record binding checked below, so an older manifest
        # would be verified against fields it never carried.
        failures.append(
            f"MANIFEST_SCHEMA_UNSUPPORTED:{manifest.schema_version!r}: "
            f"this verifier reads {MANIFEST_SCHEMA_VERSION!r}"
        )
    if manifest.parser_version not in SUPPORTED_PARSER_VERSIONS:
        failures.append(
            f"PARSER_VERSION_UNSUPPORTED:{manifest.parser_version!r}: "
            f"accepted versions are {sorted(SUPPORTED_PARSER_VERSIONS)}"
        )
    if not expected_pipeline_fingerprint:
        failures.append(
            "EXPECTED_PIPELINE_FINGERPRINT_MISSING: verification was asked to "
            "check a manifest against no particular pipeline"
        )
    if not manifest.pipeline_fingerprint:
        failures.append(
            "MANIFEST_PIPELINE_FINGERPRINT_EMPTY: pipeline_fingerprint is "
            "empty, so the capture does not say which configuration produced it"
        )
    if (
        expected_pipeline_fingerprint
        and manifest.pipeline_fingerprint
        and manifest.pipeline_fingerprint != expected_pipeline_fingerprint
    ):
        failures.append(
            f"PIPELINE_FINGERPRINT_MISMATCH:{manifest.pipeline_fingerprint!r}: "
            f"this pipeline is {expected_pipeline_fingerprint!r}"
        )
    if plan is None:
        failures.append(
            "CAPTURE_PLAN_NOT_SUPPLIED: without a plan nothing states what the "
            "capture was meant to contain"
        )
    if not manifest.capture_plan_fingerprint:
        failures.append(
            "MANIFEST_CAPTURE_PLAN_FINGERPRINT_EMPTY: capture_plan_fingerprint "
            "is empty, so the capture does not say what it was meant to contain"
        )
    if not manifest.session_id and manifest.records:
        failures.append("MANIFEST_SESSION_ID_EMPTY: the capture names no session")
    if not manifest.declared_origin_matches_records:
        failures.append(
            f"DECLARED_ORIGIN_CONTRADICTS_RECORDS:"
            f"{manifest.declared_capture_origin.value}: the records say "
            f"{manifest.capture_origin.value}"
        )
    if manifest.records and not manifest.origin_is_uniform:
        failures.append(
            "MIXED_CAPTURE_ORIGIN: the records came from more than one "
            "transport, so this is not one session"
        )

    records = {r.record_id: r for r in getattr(store, "records", lambda: ())()}

    # -- record ids must be unique before anything is keyed by them ---------
    #
    # ``record_ids`` is a sorted tuple, so a duplicate descriptor would silently
    # collapse in every dict built below and the second one would never be
    # checked against anything.
    seen: set[str] = set()
    duplicated = sorted(
        {
            entry.record_id
            for entry in manifest.records
            if entry.record_id in seen or seen.add(entry.record_id)  # type: ignore[func-returns-value]
        }
    )
    if duplicated:
        failures.append(f"DUPLICATE_RECORD_ID:{duplicated}")

    sequences: dict[int, str] = {}
    confirmed: list[str] = []
    for entry in sorted(manifest.records, key=lambda e: e.record_id):
        record_id = entry.record_id
        record = records.get(record_id)
        if record is None:
            failures.append(f"MISSING_RECORD:{record_id}")
            continue

        # -- each field of the descriptor against the stored record ---------
        #
        # Bound to *this* record id, not looked up in a pooled set. Two records
        # that swapped payload hashes used to satisfy the old membership test
        # unchanged, because the multiset of hashes was the same.
        problems: list[str] = []
        if entry.payload_hash != record.payload_hash:
            problems.append(f"PAYLOAD_HASH_MISMATCH:{record_id}")
        if entry.endpoint != record.endpoint:
            problems.append(
                f"ENDPOINT_MISMATCH:{record_id}:{entry.endpoint}!={record.endpoint}"
            )
        if entry.parameter_hash != canonical_parameter_hash(record.query_params):
            problems.append(f"PARAMETER_HASH_MISMATCH:{record_id}")
        if entry.request_id != record.request_id:
            problems.append(f"REQUEST_ID_MISMATCH:{record_id}")
        if entry.request_sequence != record.request_sequence:
            problems.append(f"REQUEST_SEQUENCE_MISMATCH:{record_id}")
        if entry.http_status != record.http_status:
            problems.append(f"HTTP_STATUS_MISMATCH:{record_id}")
        if entry.parser_version != record.parser_version:
            problems.append(f"RECORD_PARSER_VERSION_MISMATCH:{record_id}")
        if entry.vendor_schema_version != record.vendor_schema_version:
            problems.append(f"VENDOR_SCHEMA_VERSION_MISMATCH:{record_id}")
        if entry.capture_origin != record.capture_origin:
            problems.append(f"CAPTURE_ORIGIN_MISMATCH:{record_id}")
        if entry.request_started_at != record.request_started_at:
            problems.append(f"REQUEST_TIMESTAMP_MISMATCH:{record_id}")
        if entry.response_received_at != record.response_received_at:
            problems.append(f"RESPONSE_TIMESTAMP_MISMATCH:{record_id}")
        if entry.byte_length != record.byte_length:
            problems.append(f"BYTE_LENGTH_MISMATCH:{record_id}")
        if entry.capture_complete != record.capture_complete:
            problems.append(f"CAPTURE_COMPLETE_MISMATCH:{record_id}")

        # -- what the repository looked like when these bytes arrived -------
        #
        # The v2.1.6 gap. The pipeline fingerprint lived on the manifest and
        # nowhere else, so relabelling a capture as another pipeline's was one
        # field on a document the evidence could not contradict. Each of these
        # was stamped by the capture session and is compared twice: descriptor
        # against record, and record against what this pipeline is *now*.
        if entry.capture_identity != record.capture_identity:
            problems.append(f"CAPTURE_IDENTITY_MISMATCH:{record_id}")
        if not record.capture_identity.complete:
            problems.append(
                f"RECORD_NOT_STAMPED:{record_id}: the record does not say which "
                "pipeline, plan, request or recipe it was captured under"
            )
        if manifest.session_id and record.capture_session_id != manifest.session_id:
            problems.append(
                f"RECORD_SESSION_MISMATCH:{record_id}:"
                f"{record.capture_session_id!r}!={manifest.session_id!r}"
            )
        if (
            expected_pipeline_fingerprint
            and record.pipeline_fingerprint
            and record.pipeline_fingerprint != expected_pipeline_fingerprint
        ):
            problems.append(
                f"RECORD_PIPELINE_MISMATCH:{record_id}:"
                f"{record.pipeline_fingerprint!r} was stamped at capture; this "
                f"pipeline is {expected_pipeline_fingerprint!r}"
            )
        if plan is not None and record.capture_plan_fingerprint != plan.fingerprint:
            problems.append(f"RECORD_CAPTURE_PLAN_MISMATCH:{record_id}")
        if expected_identity is not None:
            if (
                record.request_spec_fingerprint
                != expected_identity.request_spec_fingerprint
            ):
                problems.append(
                    f"RECORD_REQUEST_SPEC_MISMATCH:{record_id}: captured under a "
                    "different request specification"
                )
            if (
                record.normalization_recipe_fingerprint
                != expected_identity.normalization_recipe_fingerprint
            ):
                problems.append(f"RECORD_NORMALIZATION_RECIPE_MISMATCH:{record_id}")

        # -- and the request it actually sent -------------------------------
        if expected_request_spec is not None:
            expected_params = expected_request_spec.parameters_for(record.endpoint)
            if expected_params is None:
                problems.append(
                    f"UNEXPECTED_ENDPOINT:{record_id}:{record.endpoint} is not "
                    "part of this session's request specification"
                )
            else:
                actual = {
                    str(k): _render_parameter(v) for k, v in record.query_params.items()
                }
                if actual != expected_params:
                    problems.append(
                        f"REQUEST_PARAMETERS_MISMATCH:{record_id}: sent "
                        f"{sorted(actual.items())}, this pipeline would send "
                        f"{sorted(expected_params.items())}"
                    )

        # -- the record's own internal coherence ----------------------------
        if manifest.session_id and not record_id_belongs_to(
            record_id, manifest.session_id
        ):
            problems.append(f"RECORD_NOT_FROM_SESSION:{record_id}")
        if not 200 <= record.http_status < 300:
            # A vendor error page is a real response and worth storing. It is
            # not evidence of a successful capture, and a certification capture
            # that contains one did not get what it went for.
            problems.append(
                f"UNSUCCESSFUL_HTTP_STATUS:{record_id}:{record.http_status}"
            )
        if not record.capture_complete:
            problems.append(f"INCOMPLETE_CAPTURE:{record_id}")
        if not isinstance(record.request_sequence, int) or record.request_sequence < 0:
            problems.append(f"INVALID_REQUEST_SEQUENCE:{record_id}")
        else:
            owner = sequences.setdefault(record.request_sequence, record_id)
            if owner != record_id:
                problems.append(
                    f"DUPLICATE_REQUEST_SEQUENCE:{record.request_sequence}:"
                    f"{sorted((owner, record_id))}"
                )
        for label, moment in (
            ("REQUEST", record.request_started_at),
            ("RESPONSE", record.response_received_at),
        ):
            if not isinstance(moment, datetime):
                problems.append(f"{label}_TIMESTAMP_NOT_A_DATETIME:{record_id}")
            elif moment.tzinfo is None or moment.utcoffset() is None:
                problems.append(f"{label}_TIMESTAMP_NAIVE:{record_id}")
        if (
            isinstance(record.request_started_at, datetime)
            and isinstance(record.response_received_at, datetime)
            and record.request_started_at.tzinfo is not None
            and record.response_received_at.tzinfo is not None
            and record.response_received_at < record.request_started_at
        ):
            problems.append(f"RESPONSE_BEFORE_REQUEST:{record_id}")

        if problems:
            failures.extend(problems)
            continue
        confirmed.append(record_id)

    # -- the endpoint map has to describe the same records as the id list ----
    #
    # Checked whatever the plan requires: a manifest naming a record under any
    # endpoint is claiming that record exists, and a claim the store cannot
    # support is a claim regardless of whether this session needed that
    # endpoint.
    if manifest.endpoint_records:
        mapped = {
            record_id for ids in manifest.endpoint_records.values() for record_id in ids
        }
        stray = sorted(mapped - set(manifest.record_ids))
        if stray:
            failures.append(f"ENDPOINT_RECORD_NOT_IN_MANIFEST:{stray}")
        unmapped = sorted(set(manifest.record_ids) - mapped)
        if unmapped:
            failures.append(f"RECORD_WITHOUT_AN_ENDPOINT:{unmapped}")
        for endpoint, ids in sorted(manifest.endpoint_records.items()):
            wrong = sorted(
                record_id
                for record_id in ids
                if record_id in records and records[record_id].endpoint != endpoint
            )
            if wrong:
                failures.append(f"ENDPOINT_MISATTRIBUTED:{endpoint}:{wrong}")

    # -- does the manifest claim everything the session needs? ---------------
    #
    # Checked against the *store's* view of which endpoint answered each record,
    # not against the manifest's own claim about itself. A manifest that says a
    # record answered the open-interest endpoint proves nothing if the record it
    # names came back from the quote endpoint.
    if plan is not None:
        confirmed_ids = set(confirmed)
        served: dict[str, set[str]] = {}
        for record_id in confirmed:
            served.setdefault(records[record_id].endpoint, set()).add(record_id)

        for endpoint in plan.required_endpoints:
            claimed_ids: set[str] = set(manifest.records_for(endpoint.value))
            actually = served.get(endpoint.value, set())
            if not actually:
                failures.append(
                    f"MISSING_ENDPOINT:{endpoint.value}: "
                    f"{plan.reason_for(endpoint) or 'required by this session'}"
                )
                continue
            unconfirmed = claimed_ids - confirmed_ids
            if unconfirmed:
                failures.append(
                    f"UNCONFIRMED_ENDPOINT_RECORD:{endpoint.value}:"
                    f"{sorted(unconfirmed)}"
                )
        if manifest.capture_plan_fingerprint != plan.fingerprint:
            failures.append(
                "CAPTURE_PLAN_MISMATCH: the manifest was taken against a "
                f"different plan ({manifest.capture_plan_fingerprint[:16]}...)"
            )

    integrity = getattr(store, "verify_integrity", None)
    if callable(integrity):
        report = integrity()
        if not report.ok:
            failures.append(f"STORE_NOT_CLEAN:{report.counts()}")

    return CaptureVerification(
        manifest=manifest,
        confirmed_record_ids=tuple(sorted(confirmed)),
        failures=tuple(sorted(failures)),
        store_description=type(store).__name__,
        plan_fingerprint=plan.fingerprint if plan is not None else "",
        expected_pipeline_fingerprint=expected_pipeline_fingerprint,
    )


# =============================================================================
# The evidence a trusted calculation is authorized by
# =============================================================================


@dataclass(frozen=True, slots=True)
class VerifiedCalculationContext:
    """Independently verified evidence that a calculation may be trusted.

    The v2.1.5 defect this exists to close: ``compute_trusted_gex`` decided
    trust from ``chain.meta`` -- the pipeline fingerprint, the raw-capture
    manifest, the spot provenance. All three are metadata the producing code
    wrote into the snapshot, and ``ChainSnapshot.with_meta`` is public, so a
    synthetic chain carrying the right keys satisfied every gate. A snapshot
    cannot be a witness to its own provenance.

    Everything here is *recomputed* by ``build_verified_calculation_context``
    from the manifest and the store. There is no constructor argument that
    carries a verdict in, which is why ``context_hash`` is checkable: the gate
    rebuilds it from the fields and refuses a context that has been edited.

    The manifest inside ``chain.meta`` remains useful -- it says which bytes a
    reader should go and look at -- but it is descriptive, and on its own it
    authorizes nothing.
    """

    pipeline_fingerprint: str
    capture_plan_fingerprint: str
    manifest: RawCaptureManifest
    capture_verification: CaptureVerification
    validation_report: AdapterValidationReport | None
    effective_pricing_compatibility: Any
    spot_provenance: SpotProvenance | None
    open_interest_provenance: OpenInterestProvenance | None
    raw_store_description: str
    context_hash: str
    #: What establishes the settlement session the open interest belongs to.
    #: Separate from ``open_interest_provenance``, which is about the *value*:
    #: an OI response carries a number and no date, so confirming the number
    #: says nothing about the session -- and v2.1.6 graded the date OBSERVED on
    #: the strength of that confirmation.
    open_interest_as_of_evidence: Any = None
    parser_version: str = PARSER_VERSION
    #: Everything that stopped this context from being usable. Empty is the
    #: only value a trusted calculation accepts.
    failures: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return not self.failures and self.capture_verification.verified

    @property
    def manifest_hash(self) -> str:
        return self.manifest.manifest_hash

    def semantic_payload(self) -> dict[str, Any]:
        """Everything the hash covers. Recomputable from the fields alone."""
        return {
            "schema_version": CERTIFICATION_SCHEMA_VERSION,
            "pipeline_fingerprint": self.pipeline_fingerprint,
            "capture_plan_fingerprint": self.capture_plan_fingerprint,
            "parser_version": self.parser_version,
            "manifest_hash": self.manifest.manifest_hash,
            "capture_verified": self.capture_verification.verified,
            "capture_failures": sorted(self.capture_verification.failures),
            "confirmed_record_ids": sorted(
                self.capture_verification.confirmed_record_ids
            ),
            "validation": (
                self.validation_report.semantic_payload()
                if self.validation_report is not None
                else None
            ),
            "compatibility": self.effective_pricing_compatibility.semantic_payload(),
            "spot": (
                self.spot_provenance.as_dict()
                if self.spot_provenance is not None
                else None
            ),
            "open_interest": (
                self.open_interest_provenance.as_dict()
                if self.open_interest_provenance is not None
                else None
            ),
            "open_interest_as_of_evidence": (
                self.open_interest_as_of_evidence.as_dict()
                if self.open_interest_as_of_evidence is not None
                else None
            ),
            "raw_store_description": self.raw_store_description,
            "failures": sorted(self.failures),
        }

    def recomputed_hash(self) -> str:
        payload = json.dumps(
            self.semantic_payload(), sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "context_hash": self.context_hash,
            "verified": self.verified,
            "capture": self.capture_verification.as_dict(),
            "effective_pricing_compatibility": (
                self.effective_pricing_compatibility.as_dict()
            ),
        }


def _expected_identity(pipeline: Any, *, manifest: RawCaptureManifest) -> Any:
    """The stamp this pipeline would put on a capture of this session.

    The ``as_of`` a recipe needs is taken from the records themselves -- the
    session already happened, and recomputing the recipe against *now* would
    make every capture look stale by the time anyone verified it. What is being
    compared is the configuration, not the clock.
    """
    from src.adapters.raw_store import CaptureIdentity

    moments = [
        record.request_started_at
        for record in manifest.records
        if record.request_started_at is not None
    ]
    as_of = min(moments) if moments else datetime.now(UTC)
    recipe = pipeline.normalization_recipe(as_of=as_of)
    return CaptureIdentity(
        session_id=manifest.session_id,
        pipeline_fingerprint=pipeline.fingerprint(),
        capture_plan_fingerprint=pipeline.capture_plan.fingerprint,
        request_spec_fingerprint=recipe.request_spec_fingerprint,
        normalization_recipe_fingerprint=recipe.rules_fingerprint,
        capture_origin=manifest.capture_origin,
    )


def build_verified_calculation_context(
    *,
    pipeline: Any,
    manifest: RawCaptureManifest,
    store: Any,
    validation: AdapterValidationReport | None = None,
    spot: SpotProvenance | None = None,
    open_interest: OpenInterestProvenance | None = None,
    open_interest_as_of_evidence: Any = None,
) -> VerifiedCalculationContext:
    """Verify a capture from scratch and package what it authorizes.

    Deliberately takes no ``capture_verification`` and no compatibility report.
    A caller who could pass either could pass a passing one, which is the whole
    class of defect this release is about: ``assess_readiness(capture_manifest=
    object())`` returned ``ADAPTER_CERTIFIED`` in v2.1.4 because the evidence
    was a parameter rather than a derivation.

    Failures are recorded, not raised. A context that does not hold is a useful
    object -- it says what is missing -- and it simply cannot authorize a
    trusted calculation.
    """
    if not isinstance(manifest, RawCaptureManifest):
        raise ThetaDataCertificationError(
            "build_verified_calculation_context needs a RawCaptureManifest, got "
            f"{type(manifest).__name__}"
        )
    pipeline.validate_integrity()

    plan = pipeline.capture_plan
    # What this pipeline *would* stamp and *would* send, recomputed now. The
    # records carry their own copies from capture time, and verification
    # compares the two: a capture taken under a different rate, plan or recipe
    # cannot be presented as this one's.
    request_spec = pipeline.request_spec()
    expected_identity = _expected_identity(pipeline, manifest=manifest)
    capture = verify_capture(
        manifest,
        store,
        plan=plan,
        expected_pipeline_fingerprint=pipeline.fingerprint(),
        expected_identity=expected_identity,
        expected_request_spec=request_spec,
    )

    failures: list[str] = []
    if not capture.verified:
        failures.append(f"CAPTURE_NOT_VERIFIED:{list(capture.failures)}")
    if manifest.parser_version not in SUPPORTED_PARSER_VERSIONS:
        failures.append(f"PARSER_VERSION:{manifest.parser_version!r}")

    # -- the validation report, re-derived rather than believed ---------------
    report = validation
    if report is not None:
        if not report.describes(manifest.manifest_hash):
            failures.append(
                f"VALIDATION_DESCRIBES_ANOTHER_CAPTURE:{report.manifest_hash!r}"
            )
            report = None
        else:
            rederived = AdapterValidator.validate(
                manifest=manifest, store=store, pipeline=pipeline
            )
            if rederived.semantic_payload() != report.semantic_payload():
                failures.append(
                    "VALIDATION_NOT_ISSUED_BY_THIS_VALIDATOR: re-running the "
                    "validation over the same capture produces a different "
                    "result, so the report is not a record of what was checked"
                )
                report = None

    effective = derive_post_capture_compatibility(
        base_report=pipeline.pricing_compatibility,
        validation_report=report if capture.verified else None,
        model_spec=pipeline.model_spec,
        manifest=manifest,
    )

    # -- provenance, graded against the capture rather than asserted ----------
    validated_names = (
        {c.name for c in report.checks if c.passed and c.dimension is None}
        if report is not None and report.passed
        else set()
    )
    for claim, name in (
        (open_interest, "open_interest_as_of"),
        (spot, "spot"),
    ):
        if claim is None:
            continue
        _grade, complaint = grade_claim(
            claim,
            manifest=manifest if capture.verified else None,
            store=store,
            validated=name in validated_names,
        )
        if complaint:
            failures.append(f"{name.upper()}:{complaint}")

    # -- the settlement date, which the OI value cannot establish -------------
    #
    # An open-interest response carries a number and no date. Confirming the
    # number proves the vendor sent it; it proves nothing about which session it
    # settled in, and v2.1.6 promoted a caller's assumption to OBSERVED on
    # exactly that confirmation.
    evidence = open_interest_as_of_evidence
    if evidence is None and open_interest is not None and open_interest.as_of:
        from src.adapters.open_interest import EvidenceKind, OpenInterestAsOfEvidence

        # No evidence supplied means nobody stated one, which is a caller
        # assumption whether or not it was framed as such.
        evidence = OpenInterestAsOfEvidence(
            as_of=open_interest.as_of,
            source=str(getattr(open_interest.source, "value", open_interest.source)),
            chain_date=open_interest.chain_date,
            evidence_kind=EvidenceKind.CALLER_ASSUMPTION,
        )
    # Resolved, not classified. v2.1.7 asked the *enum* whether evidence
    # permitted a trusted calculation, so ``VENDOR_FIELD`` with
    # ``record_ids=("fake-record",)`` did, and
    # ``AUTHORITATIVE_VENDOR_DOCUMENTATION`` with ``reference="lol"`` did. The
    # kind now selects which check runs; supplying it does not pass the check.
    if evidence is not None:
        from src.adapters.evidence_resolvers import resolve_settlement_date

        resolved = resolve_settlement_date(evidence, manifest=manifest, store=store)
        if not resolved.established:
            failures.append(f"OPEN_INTEREST_AS_OF:{resolved.failure}")
        elif not resolved.permits_trusted_calculation:
            failures.append(f"OPEN_INTEREST_AS_OF:{evidence.blocker}")
        elif open_interest is not None and open_interest.as_of != resolved.as_of:
            # The value's provenance and the date's evidence have to agree. Two
            # settlement dates in one calculation is not a milder version of
            # one; it is two different markets, averaged by accident.
            failures.append(
                f"OPEN_INTEREST_AS_OF:the open-interest provenance says "
                f"{open_interest.as_of} and the settlement evidence resolves "
                f"to {resolved.as_of}"
            )

    context = VerifiedCalculationContext(
        pipeline_fingerprint=pipeline.fingerprint(),
        capture_plan_fingerprint=plan.fingerprint,
        manifest=manifest,
        capture_verification=capture,
        validation_report=report,
        effective_pricing_compatibility=effective,
        spot_provenance=spot,
        open_interest_provenance=open_interest,
        open_interest_as_of_evidence=evidence,
        raw_store_description=type(store).__name__,
        context_hash="",
        parser_version=PARSER_VERSION,
        failures=tuple(sorted(failures)),
    )
    return replace(context, context_hash=context.recomputed_hash())


# =============================================================================
# Validation evidence -- bound to one capture
# =============================================================================


class CertificationState(str, Enum):
    """How far certification has actually got.

    v2.1.3 had four states on one ladder, which forced two different questions
    through one ordering: an unresolved *pricing* convention blocked
    ``READY_FOR_CAPTURE_ONLY``, so the repository refused to capture the data
    that would resolve it.

    Capture readiness and calculation trust are separate. Raw bytes are worth
    having whatever we know about the vendor's day count; a gamma computed under
    an unknown day count is not.
    """

    NOT_READY = "NOT_READY"
    #: Offline checks pass and the store is ready to receive bytes. Says nothing
    #: about whether the resulting numbers could be trusted.
    READY_FOR_RAW_CAPTURE_ONLY = "READY_FOR_RAW_CAPTURE_ONLY"
    #: Bytes exist, the manifest matches the store. Pricing may still be unknown.
    RAW_CAPTURE_COMPLETED = "RAW_CAPTURE_COMPLETED"
    #: The capture is verified and the pricing questions are answered, so a
    #: calculation is permitted -- but nobody has checked its output yet.
    CALCULATION_NOT_VALIDATED = "CALCULATION_NOT_VALIDATED"
    #: A validation report bound to this capture passed every check.
    CALCULATION_VALIDATED = "CALCULATION_VALIDATED"
    #: Everything above, plus provenance observed rather than planned and every
    #: load-bearing convention settled by a live comparison rather than by
    #: documentation. Unreachable without a real paid session.
    ADAPTER_CERTIFIED = "ADAPTER_CERTIFIED"


class AnalyticalReadiness(str, Enum):
    """Whether this data may feed anything downstream of a chart.

    A **separate axis** from ``CertificationState``, and the separation is the
    point. Capture readiness asks "are the bytes worth collecting?"; this asks
    "is the resulting dataset fit to build on?". Conflating them would either
    block the first capture on questions only a capture can answer, or let a
    dataset with an unknown contract universe reach a backtest.

    Nothing in this repository consumes an analytical dataset -- there is no
    strategy, no feature store and no backtester, by design. The state exists
    so that when something does, the gate is already written down rather than
    invented by whoever gets there first.
    """

    #: Raw capture only. The honest state today.
    NOT_ANALYTICALLY_READY = "NOT_ANALYTICALLY_READY"
    #: Trusted normalization, a vendor-established OI settlement date, resolved
    #: pricing compatibility, verified chain completeness, no material source
    #: exclusions.
    READY_FOR_ANALYTICAL_DATASET = "READY_FOR_ANALYTICAL_DATASET"


#: What ``READY_FOR_ANALYTICAL_DATASET`` requires beyond a verified capture.
#: Listed as prose because none of it is implemented as a gate yet: writing the
#: list down is the deliverable, and pretending to enforce it would be worse
#: than saying so.
ANALYTICAL_DATASET_REQUIREMENTS = (
    "trusted normalization: the chain re-derived from its raw records, and the "
    "two canonical hashes equal",
    "a settlement date for open interest established by the vendor rather than "
    "assumed by this repository (OPEN_DECISIONS OD-26)",
    "pricing compatibility resolved: no load-bearing dimension UNKNOWN or "
    "MISMATCHED in the post-capture report",
    "chain completeness measured against an independent contract universe, or "
    "the limitation explicitly modelled (OPEN_DECISIONS OD-11)",
    "no material source exclusions: contracts dropped by validation accounted "
    "for rather than silently absent",
)


@dataclass(frozen=True, slots=True)
class AdapterCertificationReadiness:
    """Machine-readable answer to "may we spend a session on this?"."""

    state: CertificationState = CertificationState.NOT_READY
    #: "No blockers to a raw capture". Deliberately narrower than it reads in
    #: v2.1.3, where the same word covered trusting the numbers.
    ready: bool = False
    #: Whether a computed GEX from this session would have a stated meaning.
    calculation_trusted: bool = False
    blockers: tuple[str, ...] = ()
    #: Reasons a calculation may not be trusted. Not capture blockers.
    calculation_blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    verified_fields: tuple[str, ...] = ()
    unverified_fields: tuple[str, ...] = ()
    #: Per-field provenance grade, so a reader does not have to infer it.
    provenance_grades: tuple[tuple[str, str], ...] = ()
    schema_version: str = CERTIFICATION_SCHEMA_VERSION
    scope: str = CERTIFICATION_SCOPE
    #: Always False. Present so that a reader of the serialised report does not
    #: have to infer it from the absence of a field.
    trading_enabled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state": self.state.value,
            "ready": self.ready,
            "calculation_trusted": self.calculation_trusted,
            "blockers": list(self.blockers),
            "calculation_blockers": list(self.calculation_blockers),
            "warnings": list(self.warnings),
            "verified_fields": list(self.verified_fields),
            "unverified_fields": list(self.unverified_fields),
            "provenance_grades": dict(self.provenance_grades),
            "scope": self.scope,
            "trading_enabled": self.trading_enabled,
        }


def assess_readiness(
    *,
    pipeline: Any,
    as_of: datetime,
    open_interest: OpenInterestProvenance | None = None,
    spot: SpotProvenance | None = None,
    raw_store: Any = None,
    manifest: RawCaptureManifest | None = None,
    validation: AdapterValidationReport | None = None,
) -> AdapterCertificationReadiness:
    """Evaluate every blocker. Deterministic, sorted, and never cached.

    Takes the **inputs to a verdict**, never a verdict.

    v2.1.4 accepted a ``CaptureVerification``, which is a public dataclass:
    ``CaptureVerification(confirmed_record_ids=("fake",), failures=())`` reports
    ``verified=True``, and the state machine believed it. The verifier existed
    and the public API did not have to use it. This takes a manifest and a
    store and runs the verifier itself, so there is no verdict to forge.

    ``validation`` is the same problem one level up, and gets the same answer:
    a supplied report is re-derived from the capture and compared. A report the
    validator would not have produced is refused -- no signature needed, because
    the check is "would this code have said that?".
    """
    if manifest is not None and not isinstance(manifest, RawCaptureManifest):
        raise ThetaDataCertificationError(
            "manifest must be a RawCaptureManifest; got "
            f"{type(manifest).__name__}. Readiness takes the inputs to a "
            "verdict, not a verdict -- a pre-computed CaptureVerification is "
            "exactly what v2.1.4 accepted and could not check."
        )
    if validation is not None and not isinstance(validation, AdapterValidationReport):
        raise ThetaDataCertificationError(
            "validation must be an AdapterValidationReport; got "
            f"{type(validation).__name__}. A truthy object is not a validation."
        )

    # Recompute the pipeline's derived reports before reading any of them. A
    # pipeline whose compatibility report was replaced still answers every
    # question this function asks; it just answers them about something else.
    validate = getattr(pipeline, "validate_integrity", None)
    if callable(validate):
        validate()

    blockers: list[str] = []
    calculation_blockers: list[str] = []
    warnings: list[str] = []
    verified: list[str] = []
    unverified: list[str] = []
    grades: dict[str, str] = {}

    config = pipeline.config

    # -- pricing coherence ---------------------------------------------------
    #
    # A *calculation* concern, not a capture concern. v2.1.3 made these capture
    # blockers, so the repository refused to fetch the bytes that would settle
    # them. Inspects the effective compatibility report rather than the pricing
    # mode: v2.1.2 could be told LOCAL_IV_LOCAL_GAMMA and would skip this
    # entirely, which is how a vendor-IV session reported ready with every
    # vendor convention unknown.
    report = pipeline.pricing_compatibility

    if report.hard_failures:
        calculation_blockers.append(
            f"pricing assessment reports hard failures {list(report.hard_failures)}"
        )
        unverified.append("pricing_compatibility")
    elif report.load_bearing_mismatches:
        calculation_blockers.append(
            "pricing assumptions are incompatible for "
            f"{pipeline.pricing_mode.value}: "
            f"{[d.value for d in report.load_bearing_mismatches]}. Computing "
            "under these settings produces numbers whose meaning cannot be "
            "stated."
        )
        unverified.append("pricing_compatibility")
    elif report.load_bearing_unknowns:
        # An unknown that changes gamma is not a caveat to note beside the
        # result; it is the reason the result has no stated meaning. It does
        # not, however, stop us from storing what the vendor sent.
        calculation_blockers.append(
            f"{len(report.load_bearing_unknowns)} load-bearing pricing "
            f"assumption(s) are UNKNOWN for {pipeline.pricing_mode.value}: "
            f"{[d.value for d in report.load_bearing_unknowns]}. Each changes "
            "the gamma, so none may be left unresolved while claiming the "
            "vendor and local models agree. A raw capture is still permitted "
            "and is how several of them get answered."
        )
        unverified.append("pricing_compatibility")
    elif not report.compatible:
        warnings.append(
            "pricing compatibility is not fully established, but every "
            "unresolved dimension is non-load-bearing for "
            f"{pipeline.pricing_mode.value}."
        )
        unverified.append("pricing_compatibility")
    else:
        verified.append("pricing_compatibility")

    # How the resolved dimensions were resolved. Documentation is a claim about
    # what the vendor intends; only a live comparison observes what it did.
    documented_only = sorted(
        d.dimension.value
        for d in report.dimensions
        if d.evidence is not None
        and d.dimension.load_bearing
        and d.evidence.source is EvidenceSource.VENDOR_DOCUMENTATION
    )
    if documented_only:
        warnings.append(
            f"{len(documented_only)} load-bearing pricing dimension(s) rest on "
            f"vendor documentation rather than a live comparison: "
            f"{documented_only}. Documentation records what the vendor says it "
            "does."
        )

    # -- the subscription actually exposes what the mode needs ---------------
    capability = getattr(pipeline, "subscription_capability", None)
    if capability is not None and not capability.satisfied:
        blockers.append(
            f"subscription tier {capability.tier.value} does not expose "
            f"missing={list(capability.missing)} "
            f"uncertain={list(capability.uncertain)}"
        )
        unverified.append("subscription_capability")
    elif capability is not None:
        verified.append("subscription_capability")

    # -- credentials ---------------------------------------------------------
    try:
        config.resolved_credentials()
    except Exception as exc:
        blockers.append(f"credentials are not available: {exc}")
        unverified.append("credentials")
    else:
        verified.append("credentials")

    # -- raw capture is mandatory, not optional ------------------------------
    #
    # The session is only worth paying for because the bytes are kept. v2.1.3
    # treated the store as an optional extra: with capture disabled the
    # readiness report said READY_FOR_CAPTURE_ONLY, which is precisely the one
    # thing it was not ready for.
    if not config.raw_capture_enabled:
        blockers.append(
            "raw_capture_enabled is False. A paid session whose responses are "
            "discarded produces numbers nobody can re-derive; the capture is "
            "the deliverable."
        )
        unverified.append("raw_capture")
    elif config.raw_capture_path is None:
        blockers.append(
            "raw_capture_enabled is True but raw_capture_path is unset, so the "
            "captured payloads have nowhere to go."
        )
        unverified.append("raw_capture")
    elif raw_store is None:
        blockers.append(
            "raw capture is configured but no store was supplied to check, so "
            "nothing confirms the destination is usable."
        )
        unverified.append("raw_capture")
    else:
        verified.append("raw_capture")

    # -- the audit trail itself ---------------------------------------------
    #
    # Probed, not inspected for attribute names. v2.1.4 called
    # ``verify_integrity`` if the object happened to have one, so
    # ``raw_store=object()`` skipped the check rather than failing it -- a store
    # that could not have stored anything passed by having no methods at all.
    if raw_store is not None:
        health = probe_raw_store(raw_store)
        if not health.usable:
            blockers.append(
                f"the raw store is not usable: {list(health.failures)}. A paid "
                "session's only copy of the evidence has to go somewhere that "
                "is real, clean and writable."
            )
            unverified.append("raw_store_integrity")
        else:
            verified.append("raw_store_integrity")

    # -- capture and validation evidence -------------------------------------
    #
    # Both derived here. The caller supplies a manifest and a store; the
    # verifier and the validator run inside this function, so neither verdict
    # can arrive pre-formed.
    plan = capture_plan_for(
        pricing_mode=pipeline.pricing_mode,
        vendor_gamma_policy=pipeline.vendor_gamma_policy,
        underlying_price_source=config.underlying_price_source,
        tier=Tier(config.tier),
    )
    capture: CaptureVerification | None = None
    if manifest is not None:
        capture = verify_capture(
            manifest,
            raw_store,
            plan=plan,
            expected_pipeline_fingerprint=pipeline.fingerprint(),
        )
        if not capture.verified:
            calculation_blockers.append(
                f"the capture manifest does not match its store: "
                f"{list(capture.failures)}"
            )
            unverified.append("capture_manifest")
        else:
            verified.append("capture_manifest")

    validation_binds = False
    if validation is not None:
        if manifest is None or capture is None or not capture.verified:
            calculation_blockers.append(
                "a validation report was supplied with no verified capture to validate"
            )
            unverified.append("validation_report")
        elif not validation.describes(manifest.manifest_hash):
            calculation_blockers.append(
                f"validation report describes manifest "
                f"{validation.manifest_hash!r}, not {manifest.manifest_hash!r}"
            )
            unverified.append("validation_report")
        else:
            # Every remaining problem is reported, not just the first. A report
            # can be wrong about its pipeline *and* not be something this
            # validator would produce, and a caller fixing one at a time learns
            # more from being told both.
            faults: list[str] = []
            if validation.pipeline_fingerprint != pipeline.fingerprint():
                faults.append(
                    "the validation report describes a different pipeline "
                    f"({validation.pipeline_fingerprint!r}); this one is "
                    f"{pipeline.fingerprint()!r}"
                )
            # Re-derive and compare. A report this validator would not have
            # produced is not a validation, whatever it says about itself --
            # which is how one arbitrary passing check certified in v2.1.4.
            rederived = AdapterValidator.validate(
                manifest=manifest, store=raw_store, pipeline=pipeline
            )
            if rederived.semantic_payload() != validation.semantic_payload():
                faults.append(
                    "the validation report was not issued by this validator: "
                    "re-running the validation over the same capture produces a "
                    "different result. A report is a record of what was checked, "
                    "not a statement that checking happened."
                )
            if faults:
                calculation_blockers.extend(faults)
                unverified.append("validation_report")
            elif not validation.passed:
                calculation_blockers.append(
                    f"validation checks failed: "
                    f"{[c.name for c in validation.failed]}; missing: "
                    f"{list(validation.missing_checks)}"
                )
                unverified.append("validation_report")
            else:
                validation_binds = True
                verified.append("validation_report")

    validated_provenance: set[str] = set()
    if validation is not None and validation_binds:
        validated_provenance = {
            c.name for c in validation.checks if c.passed and c.dimension is None
        }

    def graded(claim: Any, *, name: str) -> ProvenanceGrade:
        """Grade a claim against the capture, and complain if it does not hold."""
        grade, complaint = grade_claim(
            claim,
            manifest=manifest if capture is not None and capture.verified else None,
            store=raw_store,
            validated=name in validated_provenance,
        )
        if complaint:
            # A claim that names a record nothing confirms is not a gap in the
            # evidence; it is a false statement in the audit trail, and it must
            # not be reported as a soft "not yet observed".
            calculation_blockers.append(f"{name}: {complaint}")
        return grade

    # -- open-interest provenance -------------------------------------------
    if open_interest is None or open_interest.as_of is None:
        blockers.append(
            "open_interest provenance is missing: no settlement date and no "
            "source. Open interest is the weight on every GEX term, so a "
            "capture without it cannot be interpreted later."
        )
        unverified.append("open_interest_as_of")
        grades["open_interest_as_of"] = ProvenanceGrade.PLANNED.value
    else:
        grade = graded(open_interest, name="open_interest_as_of")
        grades["open_interest_as_of"] = grade.value
        if grade.is_observation:
            verified.append("open_interest_as_of")
        else:
            # Usable, but the date is ours rather than the vendor's, and the
            # report must not let that distinction quietly disappear.
            warnings.append(
                f"open_interest_as_of={open_interest.as_of.isoformat()} is "
                f"{grade.value} (source={open_interest.source!r}): it has not "
                "been shown to come from a stored vendor payload. Record this "
                "alongside the capture; the capture is what upgrades it."
            )
            unverified.append("open_interest_as_of")

    # -- synchronised spot ---------------------------------------------------
    if spot is None:
        blockers.append(
            "spot provenance is missing: no source and no timestamp. Every "
            "gamma is computed against this print."
        )
        unverified.append("spot_source")
        grades["spot"] = ProvenanceGrade.PLANNED.value
    elif not spot.source:
        blockers.append("spot source is unnamed; the selected spot must be documented")
        unverified.append("spot_source")
        grades["spot"] = ProvenanceGrade.PLANNED.value
    elif spot.timestamp is None:
        blockers.append(
            "spot timestamp is missing, so the spot cannot be shown to be "
            "synchronised with the chain"
        )
        unverified.append("spot_timestamp")
        grades["spot"] = ProvenanceGrade.PLANNED.value
    else:
        grade = graded(spot, name="spot_timestamp")
        grades["spot"] = grade.value
        skew = spot.skew_seconds(as_of)
        if skew is not None and skew > spot.tolerance_seconds:
            blockers.append(
                f"spot skew {skew:.3f}s exceeds the configured tolerance "
                f"{spot.tolerance_seconds:.3f}s; the chain and the underlying "
                "describe different moments"
            )
            unverified.append("spot_timestamp")
        elif grade.is_observation:
            verified.extend(("spot_source", "spot_timestamp"))
        else:
            warnings.append(
                f"spot provenance is {grade.value}: the timestamp is within "
                "tolerance but it has not been shown to come from a stored "
                "vendor payload."
            )
            unverified.extend(("spot_source", "spot_timestamp"))

    # -- known and accepted limitations -------------------------------------
    warnings.append(
        "chain completeness will be PARTIALLY_OBSERVED: no verified ThetaData "
        "contract-list endpoint is wired, so the captured chain cannot be "
        "measured against an independent universe. This is a reason to capture, "
        "not a reason to refuse -- the session is how the endpoint gets "
        "identified. See docs/OPEN_DECISIONS.md OD-11."
    )
    unverified.append("chain_completeness")

    state = _resolve_state(
        blockers=blockers,
        calculation_blockers=calculation_blockers,
        capture=capture,
        validation=validation if validation_binds else None,
        report=report,
        grades=grades,
    )

    return AdapterCertificationReadiness(
        state=state,
        ready=not blockers,
        calculation_trusted=not blockers and not calculation_blockers,
        blockers=tuple(sorted(blockers)),
        calculation_blockers=tuple(sorted(calculation_blockers)),
        warnings=tuple(sorted(warnings)),
        verified_fields=tuple(sorted(set(verified))),
        unverified_fields=tuple(sorted(set(unverified))),
        provenance_grades=tuple(sorted(grades.items())),
    )


def _resolve_state(
    *,
    blockers: list[str],
    calculation_blockers: list[str],
    capture: CaptureVerification | None,
    validation: AdapterValidationReport | None,
    report: Any,
    grades: dict[str, str],
) -> CertificationState:
    """The ladder, climbed one rung at a time.

    Each rung needs the one below it *and* its own evidence. There is no
    argument combination that reaches ``ADAPTER_CERTIFIED`` without a verified
    capture, a validation report bound to it, observed provenance, and every
    load-bearing pricing dimension settled by a live comparison -- which is to
    say, without a real paid session having happened.
    """
    if blockers:
        return CertificationState.NOT_READY
    if capture is None or not capture.verified:
        return CertificationState.READY_FOR_RAW_CAPTURE_ONLY
    if calculation_blockers:
        # Bytes are on disk and match their manifest. That is worth stating even
        # though nothing may be computed from them yet.
        return CertificationState.RAW_CAPTURE_COMPLETED
    if validation is None:
        return CertificationState.CALCULATION_NOT_VALIDATED

    # A dimension counts as settled for certification only when the *bound
    # validation report* names it, having read the bytes. v2.1.4 read this off
    # the pipeline's static attestations, so a YAML entry counted as a live
    # observation with no validation check behind it at all.
    #
    # ``LOCAL_CONFIGURATION`` still settles the two values we send, because
    # there is no vendor claim to be wrong about -- but that is a property of
    # the dimension, and ``vendor_owned`` decides it.
    live = {
        d.dimension
        for d in report.dimensions
        if d.evidence is not None and not d.dimension.vendor_owned
    }
    live |= validation.validated_dimensions
    outstanding = {
        d.dimension
        for d in report.dimensions
        if d.dimension.load_bearing and d.dimension not in live
    }
    observed = all(
        grade in (ProvenanceGrade.OBSERVED.value, ProvenanceGrade.VALIDATED.value)
        for grade in grades.values()
    )
    if outstanding or not observed:
        return CertificationState.CALCULATION_VALIDATED
    return CertificationState.ADAPTER_CERTIFIED
