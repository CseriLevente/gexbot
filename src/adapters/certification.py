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

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from src.adapters.errors import (
    ThetaDataCertificationError,
    ThetaDataProvenanceError,
)
from src.adapters.raw_store import RawCaptureManifest, probe_raw_store
from src.adapters.thetadata.capture_plan import CapturePlan, capture_plan_for
from src.adapters.thetadata.endpoints import Tier
from src.adapters.validation import (
    AdapterValidationReport,
    AdapterValidator,
    ValidationCheck,
    VerifiedFieldObservation,
)
from src.config.compatibility import EvidenceSource

__all__ = [
    "CERTIFICATION_SCHEMA_VERSION",
    "AdapterCertificationReadiness",
    "AdapterValidationReport",
    "AdapterValidator",
    "CaptureVerification",
    "CertificationState",
    "OpenInterestProvenance",
    "OpenInterestSource",
    "ProvenanceGrade",
    "SpotProvenance",
    "SpotSource",
    "ValidationCheck",
    "VerifiedFieldObservation",
    "assess_readiness",
    "verify_capture",
]

#: Bumped when the *meaning* of a certification report changes, so a stored
#: report says which rules produced it. v2.1.4 split the states and added typed
#: capture and validation evidence, which changes how every field reads.
CERTIFICATION_SCHEMA_VERSION = "adapter-certification/2.1.5"

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

        if self.as_of is not None and not isinstance(self.as_of, date):
            raise ThetaDataProvenanceError(
                f"as_of must be a date, got {type(self.as_of).__name__}"
            )
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

    @property
    def verified(self) -> bool:
        return (
            not self.failures
            and self.manifest.capture_enabled
            and bool(self.confirmed_record_ids)
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


def verify_capture(
    manifest: RawCaptureManifest,
    store: Any,
    *,
    plan: CapturePlan | None = None,
) -> CaptureVerification:
    """Check that every record a manifest claims is really in the store, and
    that the manifest claims everything the session needs.

    Two questions, and v2.1.4 asked only the first. A one-record capture -- a
    quote snapshot with no open interest, no implied volatility and no
    underlying -- verified cleanly, because every record it named was present.
    It could not have produced a GEX number, and it advanced the certification
    ladder anyway.

    ``plan`` is the answer to the second question. It is optional only so that
    the record-level checks stay usable on their own; certification always
    supplies one.
    """
    if not isinstance(manifest, RawCaptureManifest):
        raise ThetaDataCertificationError(
            f"verify_capture needs a RawCaptureManifest, got {type(manifest).__name__}"
        )

    failures: list[str] = []
    if not manifest.capture_enabled:
        failures.append("CAPTURE_DISABLED: the manifest records that capture was off")
    if not manifest.record_ids:
        failures.append("EMPTY_MANIFEST: no record ids were captured")

    records = {r.record_id: r for r in getattr(store, "records", lambda: ())()}
    confirmed: list[str] = []
    for record_id in manifest.record_ids:
        record = records.get(record_id)
        if record is None:
            failures.append(f"MISSING_RECORD:{record_id}")
            continue
        if record.payload_hash not in manifest.payload_hashes:
            failures.append(f"PAYLOAD_HASH_NOT_IN_MANIFEST:{record_id}")
            continue
        if not record.capture_complete:
            failures.append(f"INCOMPLETE_CAPTURE:{record_id}")
            continue
        confirmed.append(record_id)

    # Every payload hash the manifest claims must be accounted for exactly once.
    # Membership alone is too weak: two records carrying the same bytes -- a
    # retry written under two ids -- would satisfy two distinct manifest claims,
    # and the bytes claimed under the second hash would never have been stored.
    claimed = sorted(manifest.payload_hashes)
    confirmed_hashes = sorted(
        records[record_id].payload_hash for record_id in confirmed
    )
    if confirmed and claimed != confirmed_hashes:
        failures.append("PAYLOAD_HASHES_DO_NOT_PAIR_WITH_RECORDS")

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
        if (
            manifest.capture_plan_fingerprint
            and manifest.capture_plan_fingerprint != plan.fingerprint
        ):
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
    )


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
        capture = verify_capture(manifest, raw_store, plan=plan)
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
