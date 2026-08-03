"""Reading the captured bytes back, and saying what they show.

Certification is a claim about vendor behaviour. v2.1.4 let that claim be
*constructed*: ``AdapterValidationReport`` accepted any non-empty list of
passing checks, so ``ValidationCheck(name="anything", passed=True)`` was a
validation, and ``CaptureVerification(confirmed_record_ids=("fake",),
failures=())`` was a verified capture. Both were public dataclasses, both were
accepted by the public readiness API, and neither had to have come from the code
that checks anything.

Here the report is *derived*. ``AdapterValidator.validate`` opens the stored
payloads, re-reads the fields, compares what it finds against the pipeline's
model, and returns what it found. A caller may pass a report to
``assess_readiness``, and readiness re-derives one and compares -- so a report
that the validator would not have produced is refused without needing a secret,
a signature, or trust.

**No comparison against real vendor output has been run.** The validator works;
it has never had real bytes to work on. Every check that would need a live
response reports what it could not establish rather than assuming.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.adapters.errors import (
    ThetaDataProvenanceError,
    ThetaDataValidationError,
)
from src.adapters.raw_store import (
    PARSER_VERSION,
    CaptureOrigin,
    RawCaptureManifest,
)
from src.adapters.thetadata.endpoints import RESPONSE_FIELDS, Endpoint
from src.config.compatibility import (
    EvidenceSource,
    PricingDimension,
    VendorObservation,
)
from src.domain.vendor_time import parse_vendor_timestamp

__all__ = [
    "VALIDATION_SCHEMA_VERSION",
    "VALIDATOR_VERSION",
    "AdapterValidationReport",
    "AdapterValidator",
    "ChainCoverage",
    "ValidationCheck",
    "VerifiedFieldObservation",
    "parse_observation_timestamp",
]

#: Bumped when the *meaning* of a validation report changes.
VALIDATION_SCHEMA_VERSION = "adapter-validation/2.1.10"

#: Bumped when the validator's own logic changes, so two reports that disagree
#: can be told apart by which code produced them.
VALIDATOR_VERSION = "adapter-validator/2.1.10"

#: Parser versions whose output this validator understands. A payload read by
#: something else is not evidence this code can interpret.
KNOWN_PARSER_VERSIONS = frozenset({PARSER_VERSION})

#: Fields the index snapshot returns. Not in ``RESPONSE_FIELDS``, which covers
#: the option endpoints only.
INDEX_RESPONSE_FIELDS = ("timestamp", "symbol", "index_price")


def _fields_for(endpoint: Endpoint) -> tuple[str, ...]:
    if endpoint is Endpoint.INDEX_PRICE_SNAPSHOT:
        return INDEX_RESPONSE_FIELDS
    return RESPONSE_FIELDS.get(endpoint, ())


def _value_hash(value: object) -> str:
    payload = json.dumps(
        {"v": value}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _observation_payload(observation: Any) -> dict[str, Any]:
    """One vendor observation, as the part of it that is a finding.

    The observed value *and* its hash: the value so a reader can see what was
    found, the hash so a type change -- ``4.2`` for ``"4.2"`` -- is a
    difference rather than a formatting detail. The ``note`` is prose and stays
    out, like every other explanation in this repository's hashes.
    """
    return {
        "dimension": observation.dimension.value,
        "observed_value": _normalised_value(observation.observed_value),
        "observed_value_hash": _value_hash(observation.observed_value),
        "source": observation.source.value,
        "reference": observation.reference,
        "observed_on": observation.observed_on.isoformat(),
        "manifest_hash": observation.manifest_hash,
        "record_ids": sorted(observation.record_ids),
    }


def _normalised_value(value: object) -> Any:
    """JSON-safe rendering of an observed value, without losing its type."""
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


@dataclass(frozen=True, slots=True)
class VerifiedFieldObservation:
    """One field, read out of one stored payload, with the bytes named.

    v2.1.4's ``ProvenanceEvidence`` proved that a record id existed. It did not
    prove the record *contained* the field, that the field held the claimed
    value, or that the endpoint could have supplied it -- so a Greeks response
    was accepted as evidence about open interest, which it has no column for.
    """

    record_id: str
    endpoint: str
    payload_hash: str
    parser_version: str
    field_path: str
    observed_value: object
    observed_value_hash: str = ""
    source_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("record_id", "endpoint", "payload_hash", "field_path"):
            if not str(getattr(self, name)).strip():
                raise ThetaDataProvenanceError(
                    f"VerifiedFieldObservation.{name} is empty; an observation "
                    "that does not say what it read, or where from, cannot be "
                    "checked"
                )
        if self.parser_version not in KNOWN_PARSER_VERSIONS:
            raise ThetaDataProvenanceError(
                f"parser_version {self.parser_version!r} is not one this "
                f"validator understands ({sorted(KNOWN_PARSER_VERSIONS)}). A "
                "payload read by different code is not evidence this code can "
                "interpret."
            )
        if not self.observed_value_hash:
            object.__setattr__(
                self, "observed_value_hash", _value_hash(self.observed_value)
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "endpoint": self.endpoint,
            "payload_hash": self.payload_hash,
            "parser_version": self.parser_version,
            "field_path": self.field_path,
            "observed_value": self.observed_value,
            "observed_value_hash": self.observed_value_hash,
            "source_timestamp": (
                self.source_timestamp.isoformat() if self.source_timestamp else None
            ),
        }


@dataclass(frozen=True, slots=True)
class ChainCoverage:
    """How much of the chain a convention was actually measured across.

    v2.1.5 read row zero of one record and let that stand for the chain. One
    agreeing contract characterised every strike, so a vendor that priced a
    single strike against the index print and the rest against something else
    read as agreement -- and the dimension it settled, ``UNDERLYING_SOURCE``,
    multiplies every gamma in the snapshot.

    The counts are separate because they mean different things. A missing row
    had no such column; a non-finite row had one that could not be read as a
    number; a mismatching row was read and disagreed. Only the last is evidence
    about the vendor.
    """

    rows_inspected: int = 0
    records_inspected: int = 0
    matching_rows: int = 0
    mismatching_rows: int = 0
    missing_rows: int = 0
    non_finite_rows: int = 0
    #: Every distinct value seen, as text. A chain-level convention that is not
    #: one value is not a convention.
    distinct_observed_values: tuple[str, ...] = ()
    #: Largest absolute difference where the comparison is numeric.
    maximum_deviation: float | None = None

    @property
    def coverage_ratio(self) -> float:
        """Comparable rows over rows looked at. 0.0 when nothing was readable."""
        if self.rows_inspected <= 0:
            return 0.0
        comparable = self.matching_rows + self.mismatching_rows
        return comparable / self.rows_inspected

    @property
    def uniform(self) -> bool:
        """Every readable row agreed, and there was something to read."""
        return self.settled and self.matching_rows > 0

    @property
    def settled(self) -> bool:
        """Every row gave the *same* answer -- agreement or disagreement.

        The distinction ``uniform`` alone could not draw. A chain where every
        contract priced against something other than the index print is a
        measured fact about the vendor, as definite as one where every contract
        agreed, and it has to be able to move a dimension to ``MISMATCHED``.
        What must not move a dimension is a chain that is *two things*: that is
        ``mixed``, and it settles nothing.

        Missing and unreadable rows disqualify either answer. A convention read
        from half a chain is a convention read from half a chain.
        """
        return (
            self.rows_inspected > 0
            and self.missing_rows == 0
            and self.non_finite_rows == 0
            and not self.mixed
        )

    @property
    def mixed(self) -> bool:
        """Some rows agreed and some did not. Not a chain-level answer."""
        return self.matching_rows > 0 and self.mismatching_rows > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_inspected": self.rows_inspected,
            "records_inspected": self.records_inspected,
            "matching_rows": self.matching_rows,
            "mismatching_rows": self.mismatching_rows,
            "missing_rows": self.missing_rows,
            "non_finite_rows": self.non_finite_rows,
            "coverage_ratio": self.coverage_ratio,
            "settled": self.settled,
            "distinct_observed_values": list(self.distinct_observed_values),
            "maximum_deviation": self.maximum_deviation,
        }


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    """One thing the validator checked, and what it found."""

    name: str
    passed: bool
    detail: str = ""
    #: The pricing dimension this check settles, when it settles one.
    dimension: PricingDimension | None = None
    #: Which stored records the check read. A check that names no bytes has not
    #: read any.
    record_ids: tuple[str, ...] = ()
    observed: VerifiedFieldObservation | None = None
    #: How far across the chain the check actually looked. ``None`` for checks
    #: that are not chain-level, so an absent measurement is distinguishable
    #: from a measurement that found nothing.
    coverage: ChainCoverage | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "dimension": self.dimension.value if self.dimension else None,
            "record_ids": list(self.record_ids),
            "observed": self.observed.as_dict() if self.observed else None,
            "coverage": self.coverage.as_dict() if self.coverage else None,
        }

    def semantic_payload(self) -> dict[str, Any]:
        """What a re-derived report must reproduce. Prose excluded."""
        return {
            "name": self.name,
            "passed": self.passed,
            "dimension": self.dimension.value if self.dimension else None,
            "record_ids": sorted(self.record_ids),
            "observed_value_hash": (
                self.observed.observed_value_hash if self.observed else None
            ),
            "coverage": self.coverage.as_dict() if self.coverage else None,
        }


@dataclass(frozen=True, slots=True)
class AdapterValidationReport:
    """What was checked, about which capture, by which validator, and when.

    The ``manifest_hash`` binding is one half. The other half is that
    ``assess_readiness`` re-derives the report and compares: a report this
    validator would not have produced is refused, whatever it says about itself.
    """

    manifest_hash: str
    checks: tuple[ValidationCheck, ...] = ()
    #: Every check this validation was *required* to make. A report that omits
    #: one has not validated; v2.1.4 accepted any non-empty passing set.
    required_checks: tuple[str, ...] = ()
    validated_at: str = ""
    validator: str = ""
    schema_version: str = VALIDATION_SCHEMA_VERSION
    validator_version: str = VALIDATOR_VERSION
    parser_version: str = PARSER_VERSION
    pipeline_fingerprint: str = ""
    #: Where the captured bytes came from, as the transport stamped it on every
    #: record. v2.1.5 carried a ``live_capture`` boolean that was hardcoded
    #: ``False`` at the only construction site -- true at the time, and it would
    #: have stayed ``False`` through the first real session, because nothing
    #: derived it from anything.
    capture_origin: CaptureOrigin = CaptureOrigin.UNKNOWN_ORIGIN
    pricing_observations: tuple[VendorObservation, ...] = ()
    spot_observation: VerifiedFieldObservation | None = None
    open_interest_observation: VerifiedFieldObservation | None = None

    def __post_init__(self) -> None:
        if not str(self.manifest_hash).strip():
            raise ThetaDataValidationError(
                "AdapterValidationReport.manifest_hash is empty. A report that "
                "does not name a capture cannot be shown to describe one."
            )
        if not self.checks:
            raise ThetaDataValidationError(
                "AdapterValidationReport carries no checks. An empty report is "
                "not a passing report."
            )

    @property
    def live_capture(self) -> bool:
        """Whether these bytes came from a real vendor round trip.

        Derived, not stored. An offline fixture reports ``OFFLINE_FIXTURE`` and
        can never read as live, which is what keeps a synthetic capture out of
        ``ADAPTER_CERTIFIED``.
        """
        return self.capture_origin.is_live

    @property
    def completed_checks(self) -> tuple[str, ...]:
        return tuple(sorted(check.name for check in self.checks))

    @property
    def failed(self) -> tuple[ValidationCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    @property
    def missing_checks(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.required_checks) - set(self.completed_checks)))

    @property
    def passed(self) -> bool:
        return not self.failed and not self.missing_checks

    @property
    def validated_dimensions(self) -> frozenset[PricingDimension]:
        """Dimensions a *passing* check observed, with the bytes named.

        A check that passed without reading anything settles nothing, so the
        record-id requirement is part of the definition rather than a nicety.
        """
        return frozenset(
            check.dimension
            for check in self.checks
            if check.passed and check.dimension is not None and check.record_ids
        )

    def describes(self, manifest_hash: str) -> bool:
        return self.manifest_hash == manifest_hash

    def semantic_payload(self) -> dict[str, Any]:
        """The comparable content, so a re-derivation can be checked against it.

        ``pricing_observations`` belongs here, and until v2.1.7 it did not.
        The omission mattered because ``assess_readiness`` and the trusted path
        both accept a supplied report and refuse it unless re-deriving produces
        the same payload -- so a field outside the payload was a field that
        could be edited freely. The observations are precisely the values that
        move a compatibility dimension:

            replace(report, pricing_observations=(
                replace(observation, observed_value="vendor_index_snapshot"),
            ))

        turned an observed ``MIXED_ACROSS_CHAIN`` into agreement, and the
        re-derivation compared equal because it never looked.

        The prose ``note`` is excluded, as it is on every other payload here: a
        reworded explanation must not read as a different finding.
        """
        return {
            "manifest_hash": self.manifest_hash,
            "schema_version": self.schema_version,
            "validator_version": self.validator_version,
            "parser_version": self.parser_version,
            "pipeline_fingerprint": self.pipeline_fingerprint,
            "capture_origin": self.capture_origin.value,
            "live_capture": self.live_capture,
            "required_checks": sorted(self.required_checks),
            "checks": sorted(
                (check.semantic_payload() for check in self.checks),
                key=lambda entry: entry["name"],
            ),
            "pricing_observations": sorted(
                (_observation_payload(o) for o in self.pricing_observations),
                key=lambda entry: (entry["dimension"], entry["record_ids"]),
            ),
        }

    def observation_is_in_the_semantic_payload(self, observation: Any) -> bool:
        """Whether this exact observation is one the report is committed to.

        Asked by ``derive_post_capture_compatibility`` before an observation may
        revise a dimension: an observation the equivalence check does not cover
        is one nobody has re-derived.
        """
        return (
            _observation_payload(observation)
            in self.semantic_payload()["pricing_observations"]
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest_hash": self.manifest_hash,
            "schema_version": self.schema_version,
            "validator_version": self.validator_version,
            "validator": self.validator,
            "validated_at": self.validated_at,
            "parser_version": self.parser_version,
            "pipeline_fingerprint": self.pipeline_fingerprint,
            "capture_origin": self.capture_origin.value,
            "live_capture": self.live_capture,
            "passed": self.passed,
            "required_checks": sorted(self.required_checks),
            "completed_checks": list(self.completed_checks),
            "failed_checks": [check.name for check in self.failed],
            "missing_checks": list(self.missing_checks),
            "checks": [check.as_dict() for check in self.checks],
            "pricing_observations": [
                observation.as_dict() for observation in self.pricing_observations
            ],
            "spot_observation": (
                self.spot_observation.as_dict() if self.spot_observation else None
            ),
            "open_interest_observation": (
                self.open_interest_observation.as_dict()
                if self.open_interest_observation
                else None
            ),
            "validated_dimensions": sorted(d.value for d in self.validated_dimensions),
        }


#: Which endpoint could carry each field the validator wants to read. A Greeks
#: response has no ``open_interest`` column and no ``index_price`` column; asking
#: it for one is a category error, not a missing value.
FIELD_ENDPOINTS: dict[str, Endpoint] = {
    "open_interest": Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT,
    "index_price": Endpoint.INDEX_PRICE_SNAPSHOT,
    "implied_vol": Endpoint.OPTION_GREEKS_FIRST_ORDER,
    "underlying_price": Endpoint.OPTION_GREEKS_FIRST_ORDER,
    "underlying_timestamp": Endpoint.OPTION_GREEKS_FIRST_ORDER,
    "gamma": Endpoint.OPTION_GREEKS_SECOND_ORDER,
}


class AdapterValidator:
    """Derives a validation report from a capture. Never from a caller."""

    @staticmethod
    def observe_field(
        *,
        manifest: RawCaptureManifest,
        store: Any,
        endpoint: Endpoint,
        field_path: str,
        row_index: int = 0,
        record_id: str | None = None,
    ) -> VerifiedFieldObservation:
        """Read one field out of one stored payload.

        Four separate refusals, because they mean different things: the endpoint
        cannot carry the field, the manifest holds no record for the endpoint,
        the store does not have the record, or the payload has no such column.

        ``record_id`` names *which* response to open. Until v2.1.9 this method
        always took ``records_for(endpoint)[0]``, so evidence citing the second
        page of a paginated sweep was confirmed against the first -- and
        ``confirm_field`` compared the claim to whatever that first record
        happened to say. With one record per endpoint the two agree by accident;
        pagination, partitions and retained retries are exactly the cases where
        they would not, and all three are things this repository intends to
        certify.
        """
        expected = FIELD_ENDPOINTS.get(field_path)
        if expected is not None and expected is not endpoint:
            raise ThetaDataProvenanceError(
                f"{field_path!r} cannot be read from the {endpoint.value} "
                f"endpoint; it is returned by {expected.value}. A response that "
                "has no such column is not weak evidence about the field, it is "
                "no evidence about it."
            )
        if field_path not in _fields_for(endpoint):
            raise ThetaDataProvenanceError(
                f"{endpoint.value} returns {list(_fields_for(endpoint))}, which "
                f"does not include the field {field_path!r}"
            )

        record_ids = manifest.records_for(endpoint.value)
        if not record_ids:
            raise ThetaDataProvenanceError(
                f"the manifest holds no record for {endpoint.value}, so nothing "
                f"in this capture could have supplied {field_path!r}"
            )

        if record_id is None:
            record_id = record_ids[0]
        elif record_id not in record_ids:
            raise ThetaDataProvenanceError(
                f"record {record_id!r} is not one of this capture's "
                f"{endpoint.value} responses ({list(record_ids)}). Reading a "
                "different record and reporting it under the requested id is "
                "how evidence about one response becomes a claim about another."
            )
        records = {r.record_id: r for r in store.records()}
        record = records.get(record_id)
        if record is None:
            raise ThetaDataProvenanceError(
                f"the store does not hold record {record_id!r} that the manifest names"
            )
        payload = store.get_payload(record_id)
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != record.payload_hash:
            raise ThetaDataProvenanceError(
                f"record {record_id!r} does not hash to what the store recorded; "
                "the bytes have changed since capture"
            )

        rows = list(csv.DictReader(io.StringIO(payload)))
        if len(rows) <= row_index:
            raise ThetaDataProvenanceError(
                f"record {record_id!r} has no row {row_index}; the payload "
                f"carries {len(rows)}"
            )
        row = rows[row_index]
        if field_path not in row:
            raise ThetaDataProvenanceError(
                f"record {record_id!r} has no field {field_path!r}; its columns "
                f"are {sorted(row)}"
            )

        raw = row[field_path]
        return VerifiedFieldObservation(
            record_id=record_id,
            endpoint=record.endpoint,
            payload_hash=record.payload_hash,
            parser_version=record.parser_version,
            field_path=field_path,
            observed_value=_coerce(raw),
            source_timestamp=_parse_timestamp(row.get("timestamp")),
        )

    @staticmethod
    def confirm_field(
        *,
        manifest: RawCaptureManifest,
        store: Any,
        observation: VerifiedFieldObservation,
    ) -> VerifiedFieldObservation:
        """Check a claimed observation against the bytes it names."""
        if observation.record_id not in manifest.record_ids:
            raise ThetaDataProvenanceError(
                f"record {observation.record_id!r} is not in this manifest; "
                "evidence cannot be transplanted from one capture onto another"
            )
        try:
            endpoint = Endpoint(observation.endpoint)
        except ValueError as error:
            raise ThetaDataProvenanceError(
                f"{observation.endpoint!r} is not a ThetaData endpoint"
            ) from error

        actual = AdapterValidator.observe_field(
            manifest=manifest,
            store=store,
            endpoint=endpoint,
            field_path=observation.field_path,
            # The record the claim names, not the endpoint's first record.
            record_id=observation.record_id,
        )
        if actual.record_id != observation.record_id:
            raise ThetaDataProvenanceError(
                f"the observation claims record {observation.record_id!r} and "
                f"the reread opened {actual.record_id!r}. Confirming a claim "
                "against different bytes than it names is not confirmation."
            )
        if actual.observed_value_hash != observation.observed_value_hash:
            raise ThetaDataProvenanceError(
                f"{observation.field_path!r} in record {observation.record_id!r} "
                f"is {actual.observed_value!r}; the claimed value was "
                f"{observation.observed_value!r}"
            )
        if actual.payload_hash != observation.payload_hash:
            raise ThetaDataProvenanceError(
                f"record {observation.record_id!r} does not have the claimed "
                "payload hash"
            )
        return actual

    @staticmethod
    def validate(
        *,
        manifest: RawCaptureManifest,
        store: Any,
        pipeline: Any,
    ) -> AdapterValidationReport:
        """Open the capture, read what it holds, and report what it shows.

        Deterministic given the same capture and pipeline, because
        ``assess_readiness`` re-derives this and compares. ``validated_at`` is
        therefore excluded from the comparable payload.
        """
        checks: list[ValidationCheck] = []
        required: list[str] = []

        spot = _try_observe(
            manifest, store, Endpoint.INDEX_PRICE_SNAPSHOT, "index_price"
        )
        required.extend(("spot_source", "spot_timestamp"))
        checks.append(
            ValidationCheck(
                name="spot_source",
                passed=spot is not None,
                detail=(
                    "the underlying was read from the vendor index snapshot in "
                    "this capture"
                    if spot is not None
                    else "no index snapshot in this capture"
                ),
                record_ids=(spot.record_id,) if spot else (),
                observed=spot,
            )
        )
        checks.append(
            ValidationCheck(
                name="spot_timestamp",
                passed=spot is not None and spot.source_timestamp is not None,
                detail=(
                    "the index payload carries its own clock"
                    if spot is not None and spot.source_timestamp is not None
                    else "no timestamp could be read from an index payload"
                ),
                record_ids=(spot.record_id,) if spot else (),
                observed=spot,
            )
        )

        open_interest = _try_observe(
            manifest, store, Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT, "open_interest"
        )
        required.append("open_interest_as_of")
        checks.append(
            ValidationCheck(
                name="open_interest_as_of",
                passed=False,
                detail=(
                    "the open-interest response carries a value but no "
                    "settlement date; ThetaData does not publish which session "
                    "the figure belongs to. See docs/OPEN_DECISIONS.md OD-26."
                    if open_interest is not None
                    else "no open-interest snapshot in this capture"
                ),
                record_ids=((open_interest.record_id,) if open_interest else ()),
                observed=open_interest,
            )
        )

        # One check per vendor-owned load-bearing dimension. Most of them cannot
        # be read out of a snapshot at all -- a response says what the vendor
        # computed, not how -- so they are named, recorded as unestablished, and
        # they keep the report from passing. That is the honest result, and it
        # is why no arrangement of this code reaches ADAPTER_CERTIFIED today.
        observations: list[VendorObservation] = []
        for dimension in sorted(PricingDimension, key=lambda d: d.value):
            if not (dimension.vendor_owned and dimension.load_bearing):
                continue
            name = f"vendor_{dimension.value.lower()}"
            required.append(name)
            observed, evidence_records, detail, coverage = _observe_dimension(
                dimension, manifest, store, pipeline
            )
            if observed is not None:
                observations.append(observed)
            checks.append(
                ValidationCheck(
                    name=name,
                    # A chain-level convention that was measured and found to
                    # *vary* has not been established, whatever value the
                    # observation carries -- v2.1.5 passed on the mere existence
                    # of an observation. A chain that uniformly *disagrees* is a
                    # different case: the check did its job, and the dimension
                    # it settles is MISMATCHED.
                    passed=observed is not None
                    and (coverage is None or coverage.settled),
                    detail=detail,
                    coverage=coverage,
                    dimension=dimension,
                    record_ids=evidence_records,
                )
            )

        return AdapterValidationReport(
            manifest_hash=manifest.manifest_hash,
            checks=tuple(checks),
            required_checks=tuple(sorted(set(required))),
            validated_at=datetime.now(UTC).isoformat(),
            validator=VALIDATOR_VERSION,
            pipeline_fingerprint=(
                pipeline.fingerprint() if hasattr(pipeline, "fingerprint") else ""
            ),
            # Read off the capture, which read it off the transport. The
            # validator is not in a position to know, and must not assert.
            capture_origin=manifest.capture_origin,
            pricing_observations=tuple(observations),
            spot_observation=spot,
            open_interest_observation=open_interest,
        )


def _try_observe(
    manifest: RawCaptureManifest, store: Any, endpoint: Endpoint, field_path: str
) -> VerifiedFieldObservation | None:
    try:
        return AdapterValidator.observe_field(
            manifest=manifest, store=store, endpoint=endpoint, field_path=field_path
        )
    except ThetaDataProvenanceError:
        return None


#: What a chain-level scan reports back: the observation, the records read, a
#: sentence for a human, and how much of the chain the answer rests on.
DimensionFinding = tuple[
    "VendorObservation | None", tuple[str, ...], str, "ChainCoverage | None"
]

#: Recorded when a chain-level convention holds for some contracts and not
#: others. Compared as a token like any other observed value, so it lands as
#: MISMATCHED -- a chain that is two things is not evidence of either.
MIXED_ACROSS_CHAIN = "MIXED_ACROSS_CHAIN"


def _rows_of(
    manifest: RawCaptureManifest, store: Any, endpoint: Endpoint
) -> tuple[tuple[str, list[dict[str, str]]], ...]:
    """Every row of every record the manifest holds for one endpoint.

    The payload is re-hashed before it is read. Bytes that no longer match what
    the store recorded are not evidence, and reading them anyway would let a
    corrupted record contribute to a coverage count.
    """
    records = {r.record_id: r for r in store.records()}
    out: list[tuple[str, list[dict[str, str]]]] = []
    for record_id in manifest.records_for(endpoint.value):
        record = records.get(record_id)
        if record is None:
            continue
        payload = store.get_payload(record_id)
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != record.payload_hash:
            continue
        out.append((record_id, list(csv.DictReader(io.StringIO(payload)))))
    return tuple(out)


def _observe_dimension(
    dimension: PricingDimension,
    manifest: RawCaptureManifest,
    store: Any,
    pipeline: Any,
) -> DimensionFinding:
    """What this capture can establish about one vendor convention.

    Today: almost nothing. A snapshot reports the vendor's *output*; the
    conventions that produced it are not in the payload. Two dimensions are
    partially readable and the rest are not readable at all, which is recorded
    rather than glossed.

    The two that are readable are read **across the chain**. v2.1.5 asked
    ``observe_field`` for row zero of the first matching record and let the
    answer characterise every strike.
    """
    greeks = manifest.records_for(Endpoint.OPTION_GREEKS_FIRST_ORDER.value)
    index = manifest.records_for(Endpoint.INDEX_PRICE_SNAPSHOT.value)

    if dimension is PricingDimension.UNDERLYING_SOURCE:
        return _observe_underlying_source(manifest, store, pipeline, greeks, index)

    if dimension is PricingDimension.UNDERLYING_TIMESTAMP:
        return _observe_underlying_timestamp(manifest, store, greeks)

    # Everything else: the convention is not in the response. Naming the records
    # that *would* have carried it keeps the check honest about what was looked
    # at, and the failure keeps the report from passing.
    return (
        None,
        tuple(greeks) or tuple(manifest.record_ids[:1]),
        (
            f"{dimension.value} is not recoverable from a snapshot response: the "
            "payload reports what the vendor computed, not the convention it "
            "computed under. Vendor documentation or a purpose-built comparison "
            "is required."
        ),
        None,
    )


def _observe_underlying_source(
    manifest: RawCaptureManifest,
    store: Any,
    pipeline: Any,
    greeks: tuple[str, ...],
    index: tuple[str, ...],
) -> DimensionFinding:
    """Did the vendor price *every* contract against the index print?

    The greeks endpoint reports the underlying it used, per row, so comparing
    one row answers the question for one strike and no others.
    """
    records = tuple(sorted({*index, *greeks}))
    spot = _try_observe(manifest, store, Endpoint.INDEX_PRICE_SNAPSHOT, "index_price")
    scanned = _rows_of(manifest, store, Endpoint.OPTION_GREEKS_FIRST_ORDER)
    if spot is None or not scanned:
        return (
            None,
            records,
            "needs both an index snapshot and a first-order greeks response",
            None,
        )

    reference = _as_number(spot.observed_value)
    if reference is None:
        return (
            None,
            records,
            f"the index snapshot carries {spot.observed_value!r}, not a price",
            None,
        )

    matching = mismatching = missing = non_finite = 0
    rows_inspected = 0
    seen: set[str] = set()
    deviation: float | None = None
    for _record_id, rows in scanned:
        for row in rows:
            rows_inspected += 1
            raw = row.get("underlying_price")
            if raw is None or not str(raw).strip():
                missing += 1
                continue
            value = _as_number(raw)
            if value is None:
                non_finite += 1
                continue
            seen.add(repr(value))
            gap = abs(value - reference)
            deviation = gap if deviation is None else max(deviation, gap)
            if gap <= 1e-9:
                matching += 1
            else:
                mismatching += 1

    coverage = ChainCoverage(
        rows_inspected=rows_inspected,
        records_inspected=len(scanned),
        matching_rows=matching,
        mismatching_rows=mismatching,
        missing_rows=missing,
        non_finite_rows=non_finite,
        distinct_observed_values=tuple(sorted(seen)),
        maximum_deviation=deviation,
    )

    if coverage.uniform:
        observed: object = pipeline.model_spec.underlying_price_source.value
        detail = (
            f"every one of {rows_inspected} rows across {len(scanned)} records "
            "priced against the index print"
        )
    elif coverage.mixed:
        observed = MIXED_ACROSS_CHAIN
        detail = (
            f"{mismatching} of {rows_inspected} rows priced against something "
            f"other than the index print (largest gap {deviation}). One matching "
            "contract does not characterise the chain."
        )
    elif mismatching > 0:
        observed = "DIFFERS_FROM_INDEX_PRINT"
        detail = (
            f"none of {rows_inspected} rows priced against the index print "
            f"(largest gap {deviation})"
        )
    else:
        return (
            None,
            records,
            (
                f"no greeks row carried a readable underlying_price "
                f"({missing} missing, {non_finite} unreadable)"
            ),
            coverage,
        )

    return (
        VendorObservation(
            dimension=PricingDimension.UNDERLYING_SOURCE,
            observed_value=observed,
            source=EvidenceSource.LIVE_COMPARISON,
            reference=f"records {list(records)}",
            observed_at=datetime.now(UTC).date().isoformat(),
            record_ids=records,
            manifest_hash=manifest.manifest_hash,
            note=detail,
        ),
        records,
        detail,
        coverage,
    )


def _observe_underlying_timestamp(
    manifest: RawCaptureManifest, store: Any, greeks: tuple[str, ...]
) -> DimensionFinding:
    """Was the underlying read at the same instant as the option, row by row?

    Both sides go through ``src.domain.vendor_time``, so this compares two
    values built the same way. v2.1.5 compared a raw vendor string against an
    ``isoformat()`` with ``+00:00`` stripped out, which could only ever agree
    because the validator happened to read the string as UTC.
    """
    scanned = _rows_of(manifest, store, Endpoint.OPTION_GREEKS_FIRST_ORDER)
    if not scanned:
        return (
            None,
            tuple(greeks),
            "no first-order greeks response carrying underlying_timestamp",
            None,
        )

    matching = mismatching = missing = 0
    rows_inspected = 0
    seen: set[str] = set()
    deviation: float | None = None
    for _record_id, rows in scanned:
        for row in rows:
            rows_inspected += 1
            underlying = parse_vendor_timestamp(row.get("underlying_timestamp"))
            quoted = parse_vendor_timestamp(row.get("timestamp"))
            if underlying is None or quoted is None:
                missing += 1
                continue
            seen.add(underlying.normalized_utc.isoformat())
            gap = abs(
                (underlying.normalized_utc - quoted.normalized_utc).total_seconds()
            )
            deviation = gap if deviation is None else max(deviation, gap)
            if gap == 0.0:
                matching += 1
            else:
                mismatching += 1

    coverage = ChainCoverage(
        rows_inspected=rows_inspected,
        records_inspected=len(scanned),
        matching_rows=matching,
        mismatching_rows=mismatching,
        missing_rows=missing,
        distinct_observed_values=tuple(sorted(seen)),
        maximum_deviation=deviation,
    )

    if coverage.uniform:
        observed: object = "OPTION_QUOTE_INSTANT"
        detail = f"all {rows_inspected} rows share one instant with their quote"
    elif coverage.mixed:
        observed = MIXED_ACROSS_CHAIN
        detail = (
            f"{mismatching} of {rows_inspected} rows read the underlying at a "
            f"different instant (largest gap {deviation}s)"
        )
    elif mismatching > 0:
        observed = "DIFFERS_FROM_QUOTE"
        detail = f"no row read the underlying at its quote instant ({deviation}s)"
    else:
        return (
            None,
            tuple(greeks),
            f"no greeks row carried a readable underlying_timestamp ({missing} missing)",
            coverage,
        )

    return (
        VendorObservation(
            dimension=PricingDimension.UNDERLYING_TIMESTAMP,
            observed_value=observed,
            source=EvidenceSource.LIVE_COMPARISON,
            reference=f"records {list(greeks)}",
            observed_at=datetime.now(UTC).date().isoformat(),
            record_ids=tuple(greeks),
            manifest_hash=manifest.manifest_hash,
            note=detail,
        ),
        tuple(greeks),
        detail,
        coverage,
    )


def _as_number(value: object) -> float | None:
    """A finite float, or nothing."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _coerce(raw: str) -> object:
    """The value as the payload carries it, typed where that is unambiguous."""
    text = raw.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def parse_observation_timestamp(raw: str | None) -> datetime | None:
    """The validator's reading of a vendor clock. Identical to the adapter's.

    v2.1.5 read a naive vendor string as UTC here while the adapter localised it
    to US Eastern. The same four characters were two instants four hours apart,
    and the module disagreeing was the one whose job is to check the other. Both
    now go through ``src.domain.vendor_time``.

    Returned in the vendor's own zone, so a comparison against a chain
    timestamp -- which the adapter also returns in that zone -- is a comparison
    of two values built the same way.
    """
    observed = parse_vendor_timestamp(raw)
    return observed.vendor_local if observed is not None else None


def _parse_timestamp(raw: str | None) -> datetime | None:
    """Retained name for the module's internal call sites."""
    return parse_observation_timestamp(raw)
