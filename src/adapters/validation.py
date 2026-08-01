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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.adapters.errors import (
    ThetaDataProvenanceError,
    ThetaDataValidationError,
)
from src.adapters.raw_store import PARSER_VERSION, RawCaptureManifest
from src.adapters.thetadata.endpoints import RESPONSE_FIELDS, Endpoint
from src.config.compatibility import (
    EvidenceSource,
    PricingDimension,
    VendorObservation,
)

__all__ = [
    "VALIDATION_SCHEMA_VERSION",
    "VALIDATOR_VERSION",
    "AdapterValidationReport",
    "AdapterValidator",
    "ValidationCheck",
    "VerifiedFieldObservation",
]

#: Bumped when the *meaning* of a validation report changes.
VALIDATION_SCHEMA_VERSION = "adapter-validation/2.1.5"

#: Bumped when the validator's own logic changes, so two reports that disagree
#: can be told apart by which code produced them.
VALIDATOR_VERSION = "adapter-validator/2.1.5"

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

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "dimension": self.dimension.value if self.dimension else None,
            "record_ids": list(self.record_ids),
            "observed": self.observed.as_dict() if self.observed else None,
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
    #: Whether the bytes came from a real vendor session. Always False so far.
    live_capture: bool = False
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
        """The comparable content, so a re-derivation can be checked against it."""
        return {
            "manifest_hash": self.manifest_hash,
            "schema_version": self.schema_version,
            "validator_version": self.validator_version,
            "parser_version": self.parser_version,
            "pipeline_fingerprint": self.pipeline_fingerprint,
            "live_capture": self.live_capture,
            "required_checks": sorted(self.required_checks),
            "checks": sorted(
                (check.semantic_payload() for check in self.checks),
                key=lambda entry: entry["name"],
            ),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest_hash": self.manifest_hash,
            "schema_version": self.schema_version,
            "validator_version": self.validator_version,
            "validator": self.validator,
            "validated_at": self.validated_at,
            "parser_version": self.parser_version,
            "pipeline_fingerprint": self.pipeline_fingerprint,
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
    ) -> VerifiedFieldObservation:
        """Read one field out of one stored payload.

        Four separate refusals, because they mean different things: the endpoint
        cannot carry the field, the manifest holds no record for the endpoint,
        the store does not have the record, or the payload has no such column.
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

        record_id = record_ids[0]
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
            observed, evidence_records, detail = _observe_dimension(
                dimension, manifest, store, pipeline
            )
            if observed is not None:
                observations.append(observed)
            checks.append(
                ValidationCheck(
                    name=name,
                    passed=observed is not None,
                    detail=detail,
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
            live_capture=False,
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


def _observe_dimension(
    dimension: PricingDimension,
    manifest: RawCaptureManifest,
    store: Any,
    pipeline: Any,
) -> tuple[VendorObservation | None, tuple[str, ...], str]:
    """What this capture can establish about one vendor convention.

    Today: almost nothing. A snapshot reports the vendor's *output*; the
    conventions that produced it are not in the payload. Two dimensions are
    partially readable and the rest are not readable at all, which is recorded
    rather than glossed.
    """
    greeks = manifest.records_for(Endpoint.OPTION_GREEKS_FIRST_ORDER.value)
    index = manifest.records_for(Endpoint.INDEX_PRICE_SNAPSHOT.value)

    if dimension is PricingDimension.UNDERLYING_SOURCE:
        spot = _try_observe(
            manifest, store, Endpoint.INDEX_PRICE_SNAPSHOT, "index_price"
        )
        vendor_underlying = _try_observe(
            manifest, store, Endpoint.OPTION_GREEKS_FIRST_ORDER, "underlying_price"
        )
        if spot is None or vendor_underlying is None:
            return (
                None,
                tuple(sorted({*index, *greeks})),
                "needs both an index snapshot and a first-order greeks response",
            )
        # The greeks endpoint reports the underlying it used. If that equals the
        # index print, the vendor priced against the same underlying we do.
        agrees = _close(vendor_underlying.observed_value, spot.observed_value)
        return (
            VendorObservation(
                dimension=dimension,
                observed_value=(
                    pipeline.model_spec.underlying_price_source.value
                    if agrees
                    else "DIFFERS_FROM_INDEX_PRINT"
                ),
                source=EvidenceSource.LIVE_COMPARISON,
                reference=f"records {sorted({*index, *greeks})}",
                observed_at=datetime.now(UTC).date().isoformat(),
                record_ids=tuple(sorted({*index, *greeks})),
                manifest_hash=manifest.manifest_hash,
                note=(
                    f"greeks underlying_price {vendor_underlying.observed_value} "
                    f"vs index_price {spot.observed_value}"
                ),
            ),
            tuple(sorted({*index, *greeks})),
            "compared the vendor's stated underlying against the index print",
        )

    if dimension is PricingDimension.UNDERLYING_TIMESTAMP:
        vendor_clock = _try_observe(
            manifest, store, Endpoint.OPTION_GREEKS_FIRST_ORDER, "underlying_timestamp"
        )
        if vendor_clock is None:
            return (
                None,
                tuple(greeks),
                "no first-order greeks response carrying underlying_timestamp",
            )
        same_instant = vendor_clock.source_timestamp is not None and str(
            vendor_clock.observed_value
        ) == vendor_clock.source_timestamp.isoformat().replace("+00:00", "")
        return (
            VendorObservation(
                dimension=dimension,
                observed_value=(
                    "OPTION_QUOTE_INSTANT" if same_instant else "DIFFERS_FROM_QUOTE"
                ),
                source=EvidenceSource.LIVE_COMPARISON,
                reference=f"records {list(greeks)}",
                observed_at=datetime.now(UTC).date().isoformat(),
                record_ids=tuple(greeks),
                manifest_hash=manifest.manifest_hash,
                note=(
                    f"underlying_timestamp {vendor_clock.observed_value} against "
                    f"the row timestamp"
                ),
            ),
            tuple(greeks),
            "compared the vendor's underlying clock against the quote instant",
        )

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
    )


def _close(left: object, right: object, tolerance: float = 1e-9) -> bool:
    try:
        return abs(float(left) - float(right)) <= tolerance  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


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


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
