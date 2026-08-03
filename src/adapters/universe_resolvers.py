"""Turning a universe declaration into a statement about how much was covered.

v2.1.9 made a universe *resolvable*: the resolver reopened the records it named
and re-derived the identities. That closed "a caller typed a list and labelled
it a vendor listing", and it left the harder half open.

Proving a set of identities occurs in stored records is not proving those
records enumerate the **complete universe the request should have returned**. A
truncated response enumerates its own rows perfectly. Two consequences:

* the resolver accepted any endpoint with one row per contract as a
  ``VENDOR_CONTRACT_LIST``, so an ``/v3/option/snapshot/quote`` response
  established ``MEASURED_COMPLETE`` for the whole chain;
* ``complete_for_request`` was a constructor argument, hashed into the universe,
  which made a caller's Boolean look like a finding.

Here coverage is *derived*. Each source kind has its own check, each check can
only reach the coverage its evidence supports, and the result is a
:class:`VerifiedExpectedUniverseArtifact` -- a different type from the
declaration, so nothing downstream can mistake one for the other.

What each kind can reach today:

* **dedicated contract list** -- ``FULL_REQUEST_ENUMERATED``. No ThetaData
  endpoint qualifies, so this resolves to a refusal (OD-11);
* **captured pagination metadata** -- ``FULL_REQUEST_ENUMERATED`` with every
  declared page present. No ThetaData snapshot returns page metadata, so this
  also refuses;
* **authoritative documentation** -- ``FULL_REQUEST_ENUMERATED`` from a
  *universe* rule that lists or derives identities. The registry is empty;
* **observed snapshot rows** -- ``OBSERVED_SUBSET``, verifiably, today;
* **caller declared** -- ``UNKNOWN_COVERAGE``.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from src.adapters.errors import ThetaDataProvenanceError
from src.adapters.thetadata.endpoints import capabilities_of
from src.adapters.universe_evidence import read_pagination_metadata
from src.domain.completeness import ContractIdentity, contract_identity
from src.domain.digests import digest_of
from src.domain.expected_universe import (
    ExpectedContractUniverse,
    ExpectedUniverseSourceKind,
    UniverseCoverageStatus,
)
from src.domain.universe_artifact import (
    UNIVERSE_RESOLVER_SCHEMA_VERSION,
    VerifiedExpectedUniverseArtifact,
)
from src.domain.universe_scope import UniverseRequestScope

__all__ = [
    "DEFAULT_MAX_UNIVERSE_AGE",
    "IDENTITY_COLUMNS",
    "ResolvedExpectedUniverse",
    "check_source_compatibility",
    "resolve_expected_universe",
]

#: Columns a contract identity is built from. All must be present, or the
#: payload is not an enumeration of contracts whatever endpoint served it.
IDENTITY_COLUMNS = ("symbol", "expiration", "strike", "right")

#: How stale a universe source may be before it stops describing the chain it is
#: measured against. Two sessions: a listing from yesterday is a reasonable
#: thing to reuse, one from last month is a different market's contract set.
DEFAULT_MAX_UNIVERSE_AGE = timedelta(days=2)


@dataclass(frozen=True, slots=True)
class ResolvedExpectedUniverse:
    """The outcome of asking a resolver to establish a universe's coverage."""

    artifact: VerifiedExpectedUniverseArtifact | None = None
    failure: str = ""
    derived_identities: tuple[ContractIdentity, ...] = ()

    @property
    def established(self) -> bool:
        return self.artifact is not None and not self.failure

    @property
    def coverage_status(self) -> UniverseCoverageStatus:
        if self.artifact is None:
            return UniverseCoverageStatus.UNKNOWN_COVERAGE
        return self.artifact.coverage_status

    @property
    def establishes_completeness(self) -> bool:
        return self.established and bool(
            self.artifact and self.artifact.establishes_completeness
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "established": self.established,
            "coverage_status": self.coverage_status.value,
            "establishes_completeness": self.establishes_completeness,
            "failure": self.failure,
            "derived_identity_count": len(self.derived_identities),
            "artifact": self.artifact.as_dict() if self.artifact else None,
        }


def _failed(reason: str, derived: tuple[str, ...] = ()) -> ResolvedExpectedUniverse:
    return ResolvedExpectedUniverse(failure=reason, derived_identities=derived)


def resolve_expected_universe(
    universe: ExpectedContractUniverse,
    *,
    manifest: Any = None,
    store: Any = None,
    operation: Any = None,
    registry: Any = None,
) -> ResolvedExpectedUniverse:
    """Establish what a declaration actually covers, or say why nothing follows.

    ``manifest`` names the *source* capture -- the responses the universe was
    read out of. ``operation`` is that capture's operation identity, which is
    what the artifact records so a later chain can check scope and timing.
    """
    kind = ExpectedUniverseSourceKind(universe.source_kind)

    if kind is ExpectedUniverseSourceKind.CALLER_DECLARED:
        # A caller really did state a list, and stating one is not observing
        # one. It resolves, so diagnostics can use it, and it carries
        # UNKNOWN_COVERAGE, so nothing measures against it.
        return _resolve_caller_declared(universe)

    if kind is ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION:
        return _resolve_documented(universe, registry=registry)

    if kind is ExpectedUniverseSourceKind.VENDOR_CONTRACT_LIST:
        return _resolve_contract_list(
            universe, manifest=manifest, store=store, operation=operation
        )

    if kind is ExpectedUniverseSourceKind.CAPTURED_PAGINATION_METADATA:
        return _resolve_paginated(
            universe, manifest=manifest, store=store, operation=operation
        )

    return _resolve_snapshot_rows(
        universe, manifest=manifest, store=store, operation=operation
    )


# =============================================================================
# The four source-specific checks
# =============================================================================


def _resolve_caller_declared(
    universe: ExpectedContractUniverse,
) -> ResolvedExpectedUniverse:
    identities = tuple(sorted(universe.identity_set))
    scope = universe.scope or UniverseRequestScope(root="UNSPECIFIED")
    if universe.declared_at is None:
        return _failed(
            "a caller-declared universe must say when it was declared; an "
            "undated list cannot even be reported honestly"
        )
    return ResolvedExpectedUniverse(
        artifact=VerifiedExpectedUniverseArtifact(
            identities=universe.identity_set,
            source_kind=ExpectedUniverseSourceKind.CALLER_DECLARED,
            coverage_status=UniverseCoverageStatus.UNKNOWN_COVERAGE,
            source_operation_fingerprint="",
            source_record_ids=(),
            source_request_spec_fingerprint="",
            source_scope=scope,
            observed_at=universe.declared_at,
            evidence_fingerprint=digest_of(
                {"kind": "CALLER_DECLARED", "identities": sorted(identities)}
            ),
            declaration_hash=universe.declaration_hash,
        ),
        derived_identities=identities,
    )


def _resolve_documented(
    universe: ExpectedContractUniverse, *, registry: Any
) -> ResolvedExpectedUniverse:
    """A universe stated by a registered, content-verified *universe* document.

    Looked up in ``UNIVERSE_DOCUMENTATION_RULES``, not in the settlement
    registry. v2.1.9 consulted the settlement one, so a document about
    open-interest settlement conventions -- content-verified, entirely genuine,
    and silent on which options exist -- established a universe of whatever
    identities sat beside it in the declaration.
    """
    from src.adapters.universe_evidence import UNIVERSE_DOCUMENTATION_RULES

    rules = registry if registry is not None else UNIVERSE_DOCUMENTATION_RULES
    evidence_id = (universe.documentation_evidence_id or "").strip()
    if not hasattr(rules, "get"):
        return _failed(f"{type(rules).__name__} is not a universe rule registry")

    rule = rules.get(evidence_id)
    if rule is None:
        return _failed(
            f"{evidence_id!r} is not a registered *universe* documentation rule. "
            "A settlement-convention document is content-verified and says "
            "nothing about which contracts exist; looking one up here is how "
            "v2.1.9 let it define a universe. "
            f"Registered universe ids: {list(rules.registered_ids())}"
        )
    if not getattr(rule, "established", False):
        return _failed(
            f"universe rule {evidence_id!r} has not been content verified; a "
            "hash nobody computed is not a hash"
        )
    if universe.scope is not None and not rule.scope.covers(universe.scope).compatible:
        reasons = rule.scope.covers(universe.scope).reasons
        return _failed(
            f"universe rule {evidence_id!r} documents a different scope than the "
            f"declaration asks about: {list(reasons)}"
        )

    try:
        derived = rule.derive_identities()
    except ThetaDataProvenanceError as error:
        return _failed(str(error))

    claimed = universe.identity_set
    if derived != claimed:
        return _failed(
            f"universe rule {evidence_id!r} derives {len(derived)} identities "
            f"and the declaration claims {len(claimed)}; a document that does "
            "not produce the claimed contracts has not established them",
            tuple(sorted(derived)),
        )

    observed = universe.declared_at or rule.scope.requested_at
    if observed is None:
        return _failed(
            f"universe rule {evidence_id!r} records no instant it was read at, "
            "and the declaration gives none either, so its staleness relative "
            "to a chain cannot be measured"
        )
    return ResolvedExpectedUniverse(
        artifact=VerifiedExpectedUniverseArtifact(
            identities=derived,
            source_kind=ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION,
            coverage_status=UniverseCoverageStatus.FULL_REQUEST_ENUMERATED,
            source_operation_fingerprint="",
            source_record_ids=(),
            source_request_spec_fingerprint="",
            source_scope=rule.scope,
            # A documented universe is observed when the document was read,
            # which the rule's own scope records. The declaration's instant is
            # a fallback for a rule whose scope predates that field.
            observed_at=observed,
            evidence_fingerprint=rule.evidence_fingerprint,
            declaration_hash=universe.declaration_hash,
            documentation_evidence_id=evidence_id,
        ),
        derived_identities=tuple(sorted(derived)),
    )


def _resolve_contract_list(
    universe: ExpectedContractUniverse, *, manifest: Any, store: Any, operation: Any
) -> ResolvedExpectedUniverse:
    """A dedicated vendor listing endpoint. None exists, so this refuses.

    The named regression. v2.1.9 accepted every option snapshot here, which is
    how a quote response came to establish a complete universe.
    """
    read = _read_source_records(universe, manifest=manifest, store=store)
    if isinstance(read, ResolvedExpectedUniverse):
        return read
    payloads, descriptors = read

    not_listings = sorted(
        {
            descriptor.endpoint
            for descriptor in descriptors.values()
            if not capabilities_of(descriptor.endpoint).is_dedicated_contract_list
        }
    )
    if not_listings:
        return _failed(
            f"{not_listings} are not dedicated contract-list endpoints. Having "
            "one row per returned contract is not the same as listing every "
            "contract the request owed: a truncated response enumerates its own "
            "rows perfectly. No verified ThetaData contract-list endpoint "
            "exists (OPEN_DECISIONS OD-11), so VENDOR_CONTRACT_LIST is "
            "unsupported; use OBSERVED_SNAPSHOT_ROWS, which resolves to "
            "OBSERVED_SUBSET and says what it is."
        )

    return _artifact_from_records(  # pragma: no cover - unreachable until OD-11
        universe,
        payloads=payloads,
        descriptors=descriptors,
        operation=operation,
        coverage=UniverseCoverageStatus.FULL_REQUEST_ENUMERATED,
        extra_evidence={"listing": "dedicated_contract_list"},
    )


def _resolve_paginated(
    universe: ExpectedContractUniverse, *, manifest: Any, store: Any, operation: Any
) -> ResolvedExpectedUniverse:
    """Every declared page, read out of the responses themselves."""
    read = _read_source_records(universe, manifest=manifest, store=store)
    if isinstance(read, ResolvedExpectedUniverse):
        return read
    payloads, descriptors = read

    without_metadata = sorted(
        {
            descriptor.endpoint
            for descriptor in descriptors.values()
            if not capabilities_of(descriptor.endpoint).carries_pagination_metadata
        }
    )
    if without_metadata:
        return _failed(
            f"{without_metadata} return no pagination metadata, so how much of "
            "the request they cover cannot be read back. v2.1.9 named this "
            "source kind and re-derived identities instead, so one ordinary "
            "quote response satisfied it. No verified ThetaData snapshot "
            "endpoint exposes page or total metadata (OPEN_DECISIONS OD-11)."
        )

    try:
        evidence = read_pagination_metadata(payloads)
    except ThetaDataProvenanceError as error:  # pragma: no cover - see above
        return _failed(str(error))
    if evidence is None:  # pragma: no cover - unreachable while none carry it
        return _failed(
            "the named records carry no pagination metadata, so nothing states "
            "how many pages this listing has"
        )
    if not evidence.complete:  # pragma: no cover - unreachable today
        return _failed(
            f"pages {list(evidence.missing_pages)} of {evidence.total_pages} were "
            "never captured"
            if evidence.missing_pages
            else "the listing has no terminating page, so a continuation may "
            "remain uncaptured",
        )
    return _artifact_from_records(  # pragma: no cover - unreachable today
        universe,
        payloads=payloads,
        descriptors=descriptors,
        operation=operation,
        coverage=UniverseCoverageStatus.FULL_REQUEST_ENUMERATED,
        extra_evidence={"pagination": evidence.semantic_payload()},
    )


def _resolve_snapshot_rows(
    universe: ExpectedContractUniverse, *, manifest: Any, store: Any, operation: Any
) -> ResolvedExpectedUniverse:
    """Rows out of ordinary market-data responses. Honest, and a subset.

    This is what an option snapshot can support and it is genuinely useful: the
    identities really did arrive, so a diagnostic can say what the chain held.
    It cannot say what the chain was owed, and the coverage status says so.
    """
    read = _read_source_records(universe, manifest=manifest, store=store)
    if isinstance(read, ResolvedExpectedUniverse):
        return read
    payloads, descriptors = read

    non_enumerating = sorted(
        {
            descriptor.endpoint
            for descriptor in descriptors.values()
            if not capabilities_of(descriptor.endpoint).can_supply_identities
        }
    )
    if non_enumerating:
        return _failed(
            f"{non_enumerating} do not enumerate contracts. One index print "
            "cannot say which options exist."
        )
    return _artifact_from_records(
        universe,
        payloads=payloads,
        descriptors=descriptors,
        operation=operation,
        coverage=UniverseCoverageStatus.OBSERVED_SUBSET,
        extra_evidence={},
    )


# =============================================================================
# Shared machinery
# =============================================================================


def _read_source_records(
    universe: ExpectedContractUniverse, *, manifest: Any, store: Any
) -> tuple[dict[str, str], dict[str, Any]] | ResolvedExpectedUniverse:
    """Open every named record and check its bytes still hash as recorded."""
    if store is None:
        return _failed(
            f"{universe.source_kind.value} was offered with no capture to read "
            "it from, so the records it names were never opened. v2.1.9 used "
            "``source_record_ids`` as a boolean and never opened one."
        )
    named = tuple(universe.source_record_ids)
    # Resolved against the *store*, not one manifest: a contract listing is
    # captured before the chain it describes, so it belongs to an earlier
    # operation and is not in the chain operation's manifest slice.
    known = {r.record_id: r for r in store.records()}
    unknown = sorted(set(named) - set(known))
    if unknown:
        return _failed(
            f"the universe names records {unknown} which this store does not "
            "hold. A universe read from responses nobody can produce is a list "
            "somebody typed."
        )

    payloads: dict[str, str] = {}
    descriptors: dict[str, Any] = {}
    for record_id in named:
        descriptor = known[record_id]
        try:
            payload = store.get_payload(record_id)
        except Exception as error:
            return _failed(f"record {record_id!r} could not be read: {error}")
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != (
            descriptor.payload_hash
        ):
            return _failed(
                f"record {record_id!r} does not hash to what the store "
                "recorded; the bytes have changed since capture"
            )
        payloads[record_id] = payload
        descriptors[record_id] = descriptor
    return payloads, descriptors


def _artifact_from_records(
    universe: ExpectedContractUniverse,
    *,
    payloads: dict[str, str],
    descriptors: dict[str, Any],
    operation: Any,
    coverage: UniverseCoverageStatus,
    extra_evidence: dict[str, Any],
) -> ResolvedExpectedUniverse:
    """Re-derive identities, compare, and build the artifact."""
    derived: set[ContractIdentity] = set()
    for record_id, payload in payloads.items():
        try:
            derived |= _identities_in(payload, record_id=record_id)
        except ThetaDataProvenanceError as error:
            return _failed(str(error))

    claimed = universe.identity_set
    if derived != claimed:
        missing = sorted(claimed - derived)
        extra = sorted(derived - claimed)
        return _failed(
            f"the universe claims {len(claimed)} identities and the named "
            f"records yield {len(derived)}: "
            f"{len(missing)} claimed but not present in the records "
            f"({missing[:5]}), {len(extra)} present but not claimed "
            f"({extra[:5]}). A universe that its own source does not produce is "
            "an assertion about which contracts should exist.",
            tuple(sorted(derived)),
        )

    scope = universe.scope
    if scope is None:
        return _failed(
            "the universe records no request scope, so nothing says which "
            "question it answered -- and a listing of a narrower request "
            "re-derives perfectly while covering a different set of contracts"
        )

    # Derived from the records, not from the caller. v2.1.9 took ``observed_at``
    # from the declaration, so a listing captured three weeks ago could present
    # itself as observed this morning.
    observed_at = max(
        descriptor.response_received_at for descriptor in descriptors.values()
    )
    stamps = {descriptor.operation_fingerprint for descriptor in descriptors.values()}
    if len(stamps) != 1:
        return _failed(
            f"the named records come from {len(stamps)} different capture "
            "operations; a universe assembled from several sweeps is not one "
            "listing"
        )

    return ResolvedExpectedUniverse(
        artifact=VerifiedExpectedUniverseArtifact(
            identities=derived,
            source_kind=universe.source_kind,
            coverage_status=coverage,
            source_operation_fingerprint=stamps.pop(),
            source_record_ids=tuple(sorted(payloads)),
            source_request_spec_fingerprint=next(
                iter(
                    {
                        descriptor.request_spec_fingerprint
                        for descriptor in descriptors.values()
                    }
                )
            ),
            source_scope=scope,
            observed_at=observed_at,
            evidence_fingerprint=digest_of(
                {
                    "resolver_version": UNIVERSE_RESOLVER_SCHEMA_VERSION,
                    "kind": universe.source_kind.value,
                    "coverage": coverage.value,
                    "records": [
                        {
                            "record_id": descriptors[r].record_id,
                            "endpoint": descriptors[r].endpoint,
                            "payload_hash": descriptors[r].payload_hash,
                        }
                        for r in sorted(payloads)
                    ],
                    "identities": sorted(derived),
                    "scope": scope.semantic_payload(),
                    **extra_evidence,
                }
            ),
            declaration_hash=universe.declaration_hash,
        ),
        derived_identities=tuple(sorted(derived)),
    )


def check_source_compatibility(
    artifact: VerifiedExpectedUniverseArtifact,
    *,
    chain_scope: UniverseRequestScope,
    chain_requested_at: Any,
    chain_pipeline_fingerprint: str = "",
    source_pipeline_fingerprint: str = "",
    max_age: timedelta = DEFAULT_MAX_UNIVERSE_AGE,
) -> tuple[str, ...]:
    """Every reason this universe cannot serve this chain.

    v2.1.9 accepted a universe because its identities re-derived. That check is
    necessary and says nothing about whether the listing was *about this chain*:
    a narrower or older sweep re-derives just as cleanly, and on a narrower
    scope a perfect re-derivation is exactly what a false ``MEASURED_COMPLETE``
    looks like.
    """
    reasons: list[str] = []

    compatibility = artifact.source_scope.covers(chain_scope)
    reasons.extend(compatibility.reasons)

    age = artifact.source_scope.age_against(chain_requested_at)
    if artifact.observed_at > chain_requested_at:
        reasons.append(
            f"the universe was observed at {artifact.observed_at.isoformat()}, "
            f"after the chain request at {chain_requested_at.isoformat()}; a "
            "listing cannot describe a chain captured before it"
        )
    elif age is None:
        reasons.append(
            "the universe records no request instant, so its staleness relative "
            "to this chain cannot be measured"
        )
    elif age > max_age:
        reasons.append(
            f"the universe was collected {age} before this chain, beyond the "
            f"{max_age} tolerance; contract sets change between sessions"
        )

    if (
        chain_pipeline_fingerprint
        and source_pipeline_fingerprint
        and chain_pipeline_fingerprint != source_pipeline_fingerprint
    ):
        reasons.append(
            "the universe was captured under a different pipeline "
            "configuration than the chain, so the two asked different questions"
        )

    if artifact.resolver_version != UNIVERSE_RESOLVER_SCHEMA_VERSION:
        reasons.append(
            f"the universe was resolved by {artifact.resolver_version!r} and "
            f"this repository reads {UNIVERSE_RESOLVER_SCHEMA_VERSION!r}; a "
            "coverage state established under other rules is not comparable"
        )

    return tuple(reasons)


def _identities_in(payload: str, *, record_id: str) -> set[ContractIdentity]:
    """Every canonical contract identity a stored payload enumerates."""
    rows = list(csv.DictReader(io.StringIO(payload)))
    if not rows:
        raise ThetaDataProvenanceError(
            f"record {record_id!r} has no rows, so it enumerates no contracts. "
            "An empty listing does not mean an empty universe; it means the "
            "listing failed."
        )
    missing = [column for column in IDENTITY_COLUMNS if column not in rows[0]]
    if missing:
        raise ThetaDataProvenanceError(
            f"record {record_id!r} is missing {missing}, so a contract identity "
            f"cannot be built from it; its columns are {sorted(rows[0])}"
        )
    identities: set[ContractIdentity] = set()
    for index, row in enumerate(rows):
        try:
            identities.add(
                contract_identity(
                    symbol=str(row["symbol"]),
                    expiry=str(row["expiration"]),
                    strike=row["strike"],
                    right=_canonical_right(str(row["right"])),
                )
            )
        except ValueError as error:
            raise ThetaDataProvenanceError(
                f"record {record_id!r} row {index} does not parse into a "
                f"contract identity: {error}"
            ) from error
    return identities


def _canonical_right(value: str) -> str:
    """The one spelling of a right, matching ``OptionContract.canonical_id``.

    ``C`` and ``call`` are the same contract, and a resolver that spelled them
    differently from the chain parser would report one missing identity and one
    unexpected identity for the same instrument -- a completeness shortfall that
    does not exist. Unrecognised values pass through so the identity builder
    raises with the offending text rather than this silently choosing.
    """
    from src.domain.contracts import OptionRight

    text = value.strip().lower()
    if text in ("c", "call"):
        return OptionRight.CALL.value
    if text in ("p", "put"):
        return OptionRight.PUT.value
    return text
