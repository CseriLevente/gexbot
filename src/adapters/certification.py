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

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from src.adapters.raw_store import RawCaptureManifest
from src.config.compatibility import EvidenceSource, PricingDimension

__all__ = [
    "CERTIFICATION_SCHEMA_VERSION",
    "AdapterCertificationReadiness",
    "AdapterValidationReport",
    "CaptureVerification",
    "CertificationState",
    "OpenInterestProvenance",
    "ProvenanceEvidence",
    "ProvenanceGrade",
    "SpotProvenance",
    "ValidationCheck",
    "assess_readiness",
    "verify_capture",
]

#: Bumped when the *meaning* of a certification report changes, so a stored
#: report says which rules produced it. v2.1.4 split the states and added typed
#: capture and validation evidence, which changes how every field reads.
CERTIFICATION_SCHEMA_VERSION = "adapter-certification/2.1.4"

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


class ProvenanceError(ValueError):
    """A provenance claim that does not point at anything."""


@dataclass(frozen=True, slots=True)
class ProvenanceEvidence:
    """Where an observed value was actually read from.

    Every field is required. An evidence object that names no record is a
    boolean wearing a dataclass, which is what this replaces.
    """

    #: The stored raw record the value came out of.
    raw_record_id: str
    #: Which field of that payload. ``open_interest``, ``index_price``, and so on.
    field_path: str
    #: The capture manifest the record belongs to, so evidence cannot be
    #: transplanted from one session onto another.
    manifest_hash: str
    observed_at: str = ""

    def __post_init__(self) -> None:
        for name in ("raw_record_id", "field_path", "manifest_hash"):
            if not str(getattr(self, name)).strip():
                raise ProvenanceError(
                    f"ProvenanceEvidence.{name} is empty. Evidence that names no "
                    "record cannot be checked, which makes it an assertion."
                )

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_record_id": self.raw_record_id,
            "field_path": self.field_path,
            "manifest_hash": self.manifest_hash,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class OpenInterestProvenance:
    """Where the open-interest settlement date came from."""

    as_of: date | None
    #: ``vendor_field`` when the payload stated it; ``caller`` when a human did.
    source: str
    #: Present only when the date was read out of a stored response.
    evidence: ProvenanceEvidence | None = None

    @property
    def grade(self) -> ProvenanceGrade:
        """Derived. v2.1.3 let the caller set this with a boolean."""
        if self.as_of is None or not self.source:
            return ProvenanceGrade.PLANNED
        if self.evidence is None:
            return ProvenanceGrade.PLANNED
        return ProvenanceGrade.OBSERVED

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "source": self.source,
            "grade": self.grade.value,
            "evidence": self.evidence.as_dict() if self.evidence else None,
        }


@dataclass(frozen=True, slots=True)
class SpotProvenance:
    """Which underlying print was used, when it was taken, and how close it was."""

    source: str
    timestamp: datetime | None
    #: How far the spot print may be from the chain instant before the pairing
    #: stops being meaningful. A local policy, not a vendor fact.
    tolerance_seconds: float = 1.0
    evidence: ProvenanceEvidence | None = None

    @property
    def grade(self) -> ProvenanceGrade:
        if self.timestamp is None or not self.source:
            return ProvenanceGrade.PLANNED
        if self.evidence is None:
            return ProvenanceGrade.PLANNED
        return ProvenanceGrade.OBSERVED

    def skew_seconds(self, as_of: datetime) -> float | None:
        if self.timestamp is None:
            return None
        return abs((as_of - self.timestamp).total_seconds())

    def as_dict(self, as_of: datetime | None = None) -> dict[str, Any]:
        return {
            "source": self.source,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "tolerance_seconds": self.tolerance_seconds,
            "skew_seconds": self.skew_seconds(as_of) if as_of else None,
            "grade": self.grade.value,
            "evidence": self.evidence.as_dict() if self.evidence else None,
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
        }


def verify_capture(manifest: RawCaptureManifest, store: Any) -> CaptureVerification:
    """Check that every record a manifest claims is really in the store.

    Compares identities and payload hashes as sets. A manifest listing three
    records against a store holding two is not a capture that mostly happened;
    it is a manifest that cannot be relied on to say which bytes produced which
    number.
    """
    if not isinstance(manifest, RawCaptureManifest):
        raise TypeError(
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
    )


# =============================================================================
# Validation evidence -- bound to one capture
# =============================================================================


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    """One thing somebody checked about a capture."""

    name: str
    passed: bool
    detail: str = ""
    #: The pricing dimension this check settles, when it settles one. Lets a
    #: validation report upgrade a documented convention to an observed one.
    dimension: PricingDimension | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "dimension": self.dimension.value if self.dimension else None,
        }


class ValidationReportError(ValueError):
    """A validation report that does not describe a specific capture."""


@dataclass(frozen=True, slots=True)
class AdapterValidationReport:
    """What somebody checked, about which capture, and when.

    The ``manifest_hash`` binding is the point. v2.1.3 took ``validation_report:
    Any`` and asked only whether it was ``None``, so a report about a different
    session -- or no session -- counted the same as a report about this one.
    """

    manifest_hash: str
    checks: tuple[ValidationCheck, ...] = ()
    validated_at: str = ""
    validator: str = ""

    def __post_init__(self) -> None:
        if not str(self.manifest_hash).strip():
            raise ValidationReportError(
                "AdapterValidationReport.manifest_hash is empty. A report that "
                "does not name a capture cannot be shown to describe one."
            )
        if not self.checks:
            raise ValidationReportError(
                "AdapterValidationReport carries no checks. An empty report is "
                "not a passing report."
            )

    @property
    def failed(self) -> tuple[ValidationCheck, ...]:
        return tuple(c for c in self.checks if not c.passed)

    @property
    def passed(self) -> bool:
        return not self.failed

    @property
    def validated_dimensions(self) -> frozenset[PricingDimension]:
        return frozenset(
            c.dimension for c in self.checks if c.passed and c.dimension is not None
        )

    def describes(self, capture: CaptureVerification) -> bool:
        return self.manifest_hash == capture.manifest_hash

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest_hash": self.manifest_hash,
            "validator": self.validator,
            "validated_at": self.validated_at,
            "passed": self.passed,
            "checks": [c.as_dict() for c in self.checks],
            "validated_dimensions": sorted(d.value for d in self.validated_dimensions),
        }


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
    capture: CaptureVerification | None = None,
    validation: AdapterValidationReport | None = None,
) -> AdapterCertificationReadiness:
    """Evaluate every blocker. Deterministic, sorted, and never cached.

    ``capture`` and ``validation`` are typed and are rejected outright if they
    are not. v2.1.3 accepted ``Any`` for both and tested ``is not None``, so
    ``assess_readiness(capture_manifest=object(), validation_report=object())``
    returned ``ADAPTER_CERTIFIED`` -- the strongest claim in the repository,
    reachable with two truthy values and no evidence at all.
    """
    if capture is not None and not isinstance(capture, CaptureVerification):
        raise TypeError(
            "capture must be a CaptureVerification produced by verify_capture(); "
            f"got {type(capture).__name__}. A truthy object is not a capture."
        )
    if validation is not None and not isinstance(validation, AdapterValidationReport):
        raise TypeError(
            "validation must be an AdapterValidationReport; got "
            f"{type(validation).__name__}. A truthy object is not a validation."
        )

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
    if raw_store is not None and hasattr(raw_store, "verify_integrity"):
        integrity = raw_store.verify_integrity()
        if not integrity.ok:
            blockers.append(
                f"raw store is not clean before capture: {integrity.counts()}. "
                "Starting a paid session on top of an inconsistent audit trail "
                "makes the new evidence hard to separate from the old."
            )
            unverified.append("raw_store_integrity")
        else:
            verified.append("raw_store_integrity")

    # -- open-interest provenance -------------------------------------------
    validated_provenance: set[str] = set()
    if validation is not None and validation.passed:
        validated_provenance = {
            c.name for c in validation.checks if c.passed and c.dimension is None
        }

    if open_interest is None or open_interest.as_of is None:
        blockers.append(
            "open_interest provenance is missing: no settlement date and no "
            "source. Open interest is the weight on every GEX term, so a "
            "capture without it cannot be interpreted later."
        )
        unverified.append("open_interest_as_of")
        grades["open_interest_as_of"] = ProvenanceGrade.PLANNED.value
    else:
        grade = open_interest.grade
        if grade.is_observation and "open_interest_as_of" in validated_provenance:
            grade = ProvenanceGrade.VALIDATED
        grades["open_interest_as_of"] = grade.value
        if grade.is_observation:
            verified.append("open_interest_as_of")
        else:
            # Usable, but the date is ours rather than the vendor's, and the
            # report must not let that distinction quietly disappear.
            warnings.append(
                f"open_interest_as_of={open_interest.as_of.isoformat()} is "
                f"{grade.value} (source={open_interest.source!r}): no stored raw "
                "record was named, so it has not been observed from a vendor "
                "payload. Record this alongside the capture; the capture is what "
                "upgrades it."
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
        grade = spot.grade
        if grade.is_observation and "spot_timestamp" in validated_provenance:
            grade = ProvenanceGrade.VALIDATED
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
                "tolerance but no stored raw record was named, so it has not "
                "been observed from a vendor payload."
            )
            unverified.extend(("spot_source", "spot_timestamp"))

    # -- capture and validation evidence -------------------------------------
    if capture is not None and not capture.verified:
        calculation_blockers.append(
            f"the capture manifest does not match its store: {list(capture.failures)}"
        )
        unverified.append("capture_manifest")
    elif capture is not None:
        verified.append("capture_manifest")

    if validation is not None:
        if capture is None:
            calculation_blockers.append(
                "a validation report was supplied with no capture to validate"
            )
            unverified.append("validation_report")
        elif not validation.describes(capture):
            calculation_blockers.append(
                f"validation report describes manifest "
                f"{validation.manifest_hash!r}, not {capture.manifest_hash!r}"
            )
            unverified.append("validation_report")
        elif not validation.passed:
            calculation_blockers.append(
                f"validation checks failed: {[c.name for c in validation.failed]}"
            )
            unverified.append("validation_report")
        else:
            verified.append("validation_report")

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
        validation=validation,
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

    # Only a live comparison observes what the vendor did. A validation report
    # says the *capture* was checked; it cannot promote a documented claim about
    # a convention into an observation of one, so it is not folded in here.
    live = {
        d.dimension
        for d in report.dimensions
        if d.evidence is not None and not d.evidence.source.rests_on_a_vendor_claim
    }
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
