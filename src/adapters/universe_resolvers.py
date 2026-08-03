"""Re-deriving an expected contract universe from whatever stated it.

v2.1.8 bound the universe to the capture operation, so a replay could not be
measured against a different one. That was the right binding around the wrong
object: nothing ever checked that the universe was *true*.

    ExpectedContractUniverse(
        identities=frozenset({...}),
        source="vendor_contract_list",
        source_record_ids=("some-record",),
    )

``source`` was a string a caller typed. ``source_record_ids`` was used as a
boolean -- non-empty meant "independently observed" -- and no record was ever
opened. So a hand-written list labelled as a vendor listing established
``MEASURED_COMPLETE`` exactly as a real vendor listing would have, and the
confidence score moved accordingly.

Here a universe is *resolved*: the named records are reopened, the identities
parsed out of them again, and the result compared against what the universe
claims. The kind selects which check runs; naming a kind does not pass it.

The four kinds and what each requires:

* **vendor contract list** and **captured pagination metadata** -- every named
  record exists in this capture, belongs to this operation, hashes to what the
  store recorded, parses, and yields exactly the claimed identities;
* **authoritative documentation** -- a registered, content-verified rule;
* **caller declared** -- resolves, and establishes nothing. It is a legitimate
  thing to hold and it is not evidence about the vendor.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, replace
from typing import Any

from src.adapters.errors import ThetaDataProvenanceError
from src.adapters.thetadata.endpoints import Endpoint
from src.domain.completeness import ContractIdentity, contract_identity
from src.domain.digests import digest_of
from src.domain.expected_universe import (
    ExpectedContractUniverse,
    ExpectedUniverseSourceKind,
)

__all__ = [
    "UNIVERSE_ENUMERATING_ENDPOINTS",
    "ResolvedExpectedUniverse",
    "resolve_expected_universe",
]

#: Endpoints whose payloads enumerate contracts, one row per contract. A quote
#: snapshot does: it returns a row per contract in the requested chain. An index
#: snapshot does not, and a universe claiming to come from one is claiming
#: something the response cannot say.
UNIVERSE_ENUMERATING_ENDPOINTS = frozenset(
    {
        Endpoint.OPTION_QUOTE_SNAPSHOT.value,
        Endpoint.OPTION_OPEN_INTEREST_SNAPSHOT.value,
        Endpoint.OPTION_GREEKS_FIRST_ORDER.value,
        Endpoint.OPTION_GREEKS_SECOND_ORDER.value,
        Endpoint.OPTION_GREEKS_ALL.value,
    }
)

#: Columns a contract identity is built from. All five must be present, or the
#: payload is not an enumeration of contracts whatever endpoint served it.
IDENTITY_COLUMNS = ("symbol", "expiration", "strike", "right")


@dataclass(frozen=True, slots=True)
class ResolvedExpectedUniverse:
    """The outcome of asking a resolver to establish an expected universe."""

    universe: ExpectedContractUniverse | None
    #: Empty when the universe was established. Otherwise why it was not.
    failure: str = ""
    #: Identities the resolver derived from the source, for a report.
    derived_identities: tuple[ContractIdentity, ...] = ()

    @property
    def established(self) -> bool:
        return self.universe is not None and not self.failure

    @property
    def establishes_completeness(self) -> bool:
        """Whether a ``MEASURED_COMPLETE`` verdict may rest on this."""
        return self.established and bool(
            self.universe and self.universe.establishes_completeness
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "established": self.established,
            "establishes_completeness": self.establishes_completeness,
            "failure": self.failure,
            "derived_identity_count": len(self.derived_identities),
            "universe": self.universe.as_dict() if self.universe else None,
        }


def resolve_expected_universe(
    universe: ExpectedContractUniverse,
    *,
    manifest: Any = None,
    store: Any = None,
    operation: Any = None,
    registry: Any = None,
) -> ResolvedExpectedUniverse:
    """Establish that a universe is what it says it is, or say why not."""
    kind = ExpectedUniverseSourceKind(universe.source_kind)

    if kind is ExpectedUniverseSourceKind.CALLER_DECLARED:
        # Resolves, and establishes nothing. The distinction matters: a caller
        # really did state a list, and stating a list is not observing one.
        return ResolvedExpectedUniverse(
            universe=replace(universe, evidence_fingerprint=""),
            derived_identities=tuple(sorted(universe.identity_set)),
        )

    if kind is ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION:
        return _resolve_documented(universe, registry=registry)

    return _resolve_from_records(
        universe, manifest=manifest, store=store, operation=operation
    )


def _resolve_documented(
    universe: ExpectedContractUniverse, *, registry: Any
) -> ResolvedExpectedUniverse:
    """A universe stated by a registered, content-verified document."""
    from src.adapters.evidence_resolvers import DOCUMENTATION_RULES

    rules = registry if registry is not None else DOCUMENTATION_RULES
    evidence_id = (universe.documentation_evidence_id or "").strip()
    rule = rules.get(evidence_id)
    if rule is None:
        return ResolvedExpectedUniverse(
            universe=None,
            failure=(
                f"{evidence_id!r} is not a registered documentation rule, so "
                "the document said to list this universe is one nobody has read"
            ),
        )
    if not rule.verified_location:
        return ResolvedExpectedUniverse(
            universe=None,
            failure=(
                f"documentation rule {evidence_id!r} has not been content "
                "verified; a hash nobody computed is not a hash"
            ),
        )
    return ResolvedExpectedUniverse(
        universe=replace(
            universe,
            evidence_fingerprint=digest_of(
                {
                    "kind": universe.source_kind.value,
                    "documentation": rule.semantic_payload(),
                    "identities": sorted(universe.identity_set),
                }
            ),
        ),
        derived_identities=tuple(sorted(universe.identity_set)),
    )


def _resolve_from_records(
    universe: ExpectedContractUniverse,
    *,
    manifest: Any,
    store: Any,
    operation: Any,
) -> ResolvedExpectedUniverse:
    """Reopen the named records and parse the identities out again."""
    kind = universe.source_kind
    if manifest is None or store is None:
        return ResolvedExpectedUniverse(
            universe=None,
            failure=(
                f"{kind.value} was offered with no capture to read it from, so "
                "the records it names were never opened. v2.1.8 used "
                "``source_record_ids`` as a boolean and never opened one."
            ),
        )

    named = tuple(universe.source_record_ids)
    # Resolved against the *store*, not against one manifest. A contract listing
    # is captured before the chain it describes -- it is what the expectation is
    # built from -- so it belongs to an earlier operation and is therefore not in
    # the chain operation's manifest slice. The store holds every record of the
    # session, which is where the evidence actually lives.
    known = {r.record_id: r for r in store.records()}
    unknown = sorted(set(named) - set(known))
    if unknown:
        return ResolvedExpectedUniverse(
            universe=None,
            failure=(
                f"the universe names records {unknown} which this store does "
                "not hold. A universe read from responses nobody can produce is "
                "a list somebody typed."
            ),
        )

    # Which operation a source record belongs to is *not* required to be the
    # operation being replayed, and the reason is structural: a contract listing
    # is captured before the chain it describes, so it necessarily belongs to an
    # earlier operation. Requiring otherwise would make a vendor-sourced universe
    # impossible to declare rather than hard to forge.
    #
    # What binds the universe to this capture is elsewhere and is stronger: its
    # hash is stamped on every record of the operation, so a different universe
    # is a different operation. What is checked here is that the universe is
    # *true* -- that the records it names really do enumerate exactly these
    # contracts.
    derived: set[ContractIdentity] = set()
    for record_id in named:
        descriptor = known[record_id]
        if descriptor.endpoint not in UNIVERSE_ENUMERATING_ENDPOINTS:
            return ResolvedExpectedUniverse(
                universe=None,
                failure=(
                    f"record {record_id!r} is a {descriptor.endpoint} response, "
                    "which does not enumerate contracts. One index print cannot "
                    "say which options exist."
                ),
            )
        if (
            operation is not None
            and descriptor.operation_id
            and descriptor.operation_id == operation.operation_id
            and descriptor.operation_fingerprint != operation.operation_fingerprint
        ):
            return ResolvedExpectedUniverse(
                universe=None,
                failure=(
                    f"record {record_id!r} claims operation "
                    f"{descriptor.operation_id!r} with a different fingerprint "
                    "than the operation being replayed"
                ),
            )
        try:
            payload = store.get_payload(record_id)
        except Exception as error:
            return ResolvedExpectedUniverse(
                universe=None,
                failure=f"record {record_id!r} could not be read: {error}",
            )
        actual = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if actual != descriptor.payload_hash:
            return ResolvedExpectedUniverse(
                universe=None,
                failure=(
                    f"record {record_id!r} does not hash to what the store "
                    "recorded; the bytes have changed since capture"
                ),
            )
        try:
            derived |= _identities_in(payload, record_id=record_id)
        except ThetaDataProvenanceError as error:
            return ResolvedExpectedUniverse(universe=None, failure=str(error))

    claimed = universe.identity_set
    if derived != claimed:
        missing = sorted(claimed - derived)
        extra = sorted(derived - claimed)
        return ResolvedExpectedUniverse(
            universe=None,
            failure=(
                f"the universe claims {len(claimed)} identities and the named "
                f"records yield {len(derived)}: "
                f"{len(missing)} claimed but not present in the records "
                f"({missing[:5]}), {len(extra)} present but not claimed "
                f"({extra[:5]}). A universe that its own source does not "
                "produce is an assertion about which contracts should exist."
            ),
            derived_identities=tuple(sorted(derived)),
        )

    return ResolvedExpectedUniverse(
        universe=replace(
            universe,
            evidence_fingerprint=digest_of(
                {
                    "kind": kind.value,
                    "records": [
                        {
                            "record_id": known[r].record_id,
                            "endpoint": known[r].endpoint,
                            "payload_hash": known[r].payload_hash,
                        }
                        for r in sorted(named)
                    ],
                    "identities": sorted(derived),
                    "complete_for_request": universe.complete_for_request,
                }
            ),
        ),
        derived_identities=tuple(sorted(derived)),
    )


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
