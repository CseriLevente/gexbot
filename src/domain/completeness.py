"""How much of a chain we actually observed, and how confident we are in that.

Kept in ``domain`` rather than in the ThetaData adapter because two different
layers need it and neither may import the other: the adapter produces it, the
confidence model consumes it, and ``ChainSnapshot`` carries it between them.

The distinction this module exists to protect is between *measuring* a chain
against a universe that came from somewhere else, and *asserting* that a chain
is complete because it is as long as itself. v2.1 collapsed the second into the
first at two separate layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "COMPLETENESS_WARNING_CODE",
    "ChainCompleteness",
    "CompletenessStatus",
    "ContractIdentity",
    "contract_identity",
]

#: Emitted verbatim by the confidence model whenever completeness could not be
#: measured. Deterministic so that a log scraper can match on it.
COMPLETENESS_WARNING_CODE = "CHAIN_COMPLETENESS_NOT_INDEPENDENTLY_OBSERVED"


class CompletenessStatus(Enum):
    """Whether the chain's completeness is a measurement or a guess."""

    #: An independent universe was supplied and every member of it arrived,
    #: with nothing else alongside.
    MEASURED_COMPLETE = "MEASURED_COMPLETE"

    #: Every expected identity arrived, and so did contracts nobody predicted.
    #: A distinct state because it says something different: the chain is whole
    #: and the *expectation* was incomplete. Reporting it as plain
    #: MEASURED_COMPLETE would discard that; reporting it as INCOMPLETE would
    #: claim contracts are missing when none are.
    MEASURED_COMPLETE_WITH_EXTRAS = "MEASURED_COMPLETE_WITH_EXTRAS"

    #: An independent universe was supplied and some of it did not arrive.
    MEASURED_INCOMPLETE = "MEASURED_INCOMPLETE"

    #: The universe listed only part of the request -- one page of a paginated
    #: listing, say -- and everything it listed arrived. A real measurement, and
    #: a narrower one than ``MEASURED_COMPLETE``: it says nothing about the
    #: contracts the page never mentioned.
    #:
    #: v2.1.8 had ``complete_for_request`` on the universe and read it nowhere,
    #: so a page-one listing whose members all arrived reported
    #: ``MEASURED_COMPLETE`` for the whole chain.
    PARTIAL_UNIVERSE_ALL_LISTED_PRESENT = "PARTIAL_UNIVERSE_ALL_LISTED_PRESENT"

    #: A partial universe, and some of what it did list is absent. Strictly
    #: worse news than the state above: an incomplete page still found a hole.
    PARTIAL_UNIVERSE_MISSING_IDENTITIES = "PARTIAL_UNIVERSE_MISSING_IDENTITIES"

    #: Rows arrived and joined, but nothing independent says how many should
    #: have. The chain may be whole; it may be the first page of a truncated
    #: response. Nothing here can tell the difference.
    PARTIALLY_OBSERVED = "PARTIALLY_OBSERVED"

    #: Not even the received counts are meaningful -- an empty expectation, or a
    #: caller that never supplied one.
    UNKNOWN = "UNKNOWN"

    @property
    def is_measured(self) -> bool:
        """True when an independent universe backed the number, whole or partial."""
        return self in (
            CompletenessStatus.MEASURED_COMPLETE,
            CompletenessStatus.MEASURED_COMPLETE_WITH_EXTRAS,
            CompletenessStatus.MEASURED_INCOMPLETE,
            CompletenessStatus.PARTIAL_UNIVERSE_ALL_LISTED_PRESENT,
            CompletenessStatus.PARTIAL_UNIVERSE_MISSING_IDENTITIES,
        )

    @property
    def measured_against_the_whole_request(self) -> bool:
        """True only where the universe claimed to enumerate the full request.

        The distinction confidence and analytical readiness need: a partial
        expectation is evidence, and it is not evidence about completeness.
        """
        return self in (
            CompletenessStatus.MEASURED_COMPLETE,
            CompletenessStatus.MEASURED_COMPLETE_WITH_EXTRAS,
            CompletenessStatus.MEASURED_INCOMPLETE,
        )

    @property
    def implies_complete(self) -> bool:
        """True only for a chain measured against a *full* universe and found whole.

        Deliberately *not* true for ``PARTIALLY_OBSERVED`` -- "we received
        everything we were sent" is not "we were sent everything" -- nor for
        either partial-universe state, where the expectation itself admitted it
        was incomplete.
        """
        return self in (
            CompletenessStatus.MEASURED_COMPLETE,
            CompletenessStatus.MEASURED_COMPLETE_WITH_EXTRAS,
        )

    @property
    def has_missing_identities(self) -> bool:
        """Whether something the expectation named did not arrive."""
        return self in (
            CompletenessStatus.MEASURED_INCOMPLETE,
            CompletenessStatus.PARTIAL_UNIVERSE_MISSING_IDENTITIES,
        )


#: A canonical contract identity. A plain string by design -- it has to survive
#: JSON round-trips into snapshot metadata and out again -- but it is only ever
#: produced by ``contract_identity``, so the normalisation is not optional.
ContractIdentity = str


def contract_identity(
    *, symbol: str, expiry: str, strike: str | float | None, right: str
) -> ContractIdentity:
    """Build the one canonical spelling of a contract identity.

    Shares ``parse_strike``/``canonical_strike`` with the chain parser. An
    expected universe that formatted its strikes differently from the received
    chain would produce a missing identity and an unexpected one for the *same
    contract*, and the completeness measure would report a shortfall that does
    not exist.

    Raises rather than returning a sentinel: an identity that cannot be built is
    not an identity, and a set containing a NaN strike compares unequal to
    itself.
    """
    from src.domain.strikes import canonical_strike, parse_strike

    parsed, issue = parse_strike(strike)
    if parsed is None:
        raise ValueError(f"strike {strike!r} is {issue}; no identity can be built")
    # ``canonical_strike`` on the Decimal, with no float in between.
    # ``OptionContract.canonical_id`` calls the same formatter, so the two
    # spellings are equal by construction rather than because both happen to
    # produce four decimal places. v2.1.3 wrote ``float(parsed)`` here, putting
    # the exact parse through binary floating point on its way into the identity
    # that parse exists to keep exact.
    return f"{symbol.strip().upper()}:{expiry}:{canonical_strike(parsed)}:{right}"


# ``ExpectedContractUniverse`` used to live here as well as in
# ``src.domain.expected_universe``. Two types with the same name and different
# fields is worse than one in the wrong place: the engine read this one, the
# capture path built that one, and only the second carried provenance -- so the
# object that decided completeness was the object nobody had verified.
#
# There is now exactly one, in ``src.domain.expected_universe``, and
# ``tests/unit/test_architecture.py`` fails the build if a second appears.


@dataclass(frozen=True, slots=True)
class ChainCompleteness:
    """How complete a chain is, measured against an INDEPENDENT expectation.

    Lives in the domain rather than the ThetaData adapter because two layers
    need it and neither may import the other: the adapter produces it, the
    confidence model consumes it, and ChainSnapshot carries it between them.
    v2.1.2 kept it in the adapter, so the engine could not read it -- which
    is why the scorer recomputed a ratio from contract counts instead.

    Two defects shaped this class, one release apart, and they are the same
    mistake at different depths.

    v2 inferred the expected universe from the response being judged, so a
    truncated response was complete by construction.

    v2.1.1 fixed that and then compared *counts*::

        joined_contract_count / expected_contract_count

    A chain that received two contracts where two were expected therefore
    reported ``MEASURED_COMPLETE``, whether or not they were the two expected.
    Two missing and two unexpected cancel exactly in that arithmetic. Counting
    cannot distinguish the thing being measured from a coincidence of the same
    size.

    Completeness is now a statement about **identity sets**::

        missing_expected    = expected - received
        unexpected_received = received - expected
        matched             = expected & received

    with ``MEASURED_COMPLETE`` requiring ``missing_expected`` to be empty.
    Extras get their own status rather than being averaged away, because
    "contracts arrived that nobody predicted" is a fact about the expectation
    and needs to survive to whoever reads the report.
    """

    received_quote_count: int
    received_oi_count: int
    received_iv_count: int
    received_greeks_count: int
    #: Identities an INDEPENDENT source said to expect. ``None`` means no such
    #: source existed -- distinct from an empty tuple, which means one existed
    #: and claimed nothing.
    expected_contract_ids: tuple[str, ...] | None = None
    #: Identities that actually joined into contracts.
    received_contract_ids: tuple[str, ...] = ()
    expected_source: str = "none"
    missing_by_source: dict[str, int] = field(default_factory=dict)
    #: Whether the source claimed to enumerate the *whole* request. False for a
    #: page of a paginated listing. v2.1.8 carried this on the universe and read
    #: it nowhere, so a page-one listing whose members all arrived reported
    #: ``MEASURED_COMPLETE`` for the entire chain.
    expected_complete_for_request: bool = True

    #: Identity lists are truncated in serialised metadata at this length. A
    #: 5,000-contract mismatch is a real possibility on a full SPX chain, and a
    #: 5,000-entry blob in every snapshot's metadata is not useful to anyone.
    #: The *counts* are never truncated.
    IDENTITY_SAMPLE_LIMIT = 100

    # -- identity sets -------------------------------------------------------

    @property
    def _expected(self) -> frozenset[str]:
        return frozenset(self.expected_contract_ids or ())

    @property
    def _received(self) -> frozenset[str]:
        return frozenset(self.received_contract_ids)

    @property
    def expected_identity_count(self) -> int:
        """Distinct expected identities. Duplicates in the input do not count
        twice -- an expectation listing a contract twice still expects it once.
        """
        return len(self._expected)

    @property
    def received_identity_count(self) -> int:
        return len(self._received)

    @property
    def matched_identity_count(self) -> int:
        return len(self._expected & self._received)

    @property
    def missing_expected_identities(self) -> tuple[str, ...]:
        """Expected but absent, sorted. Sorted so that two runs over the same
        data produce byte-identical metadata."""
        return tuple(sorted(self._expected - self._received))

    @property
    def unexpected_received_identities(self) -> tuple[str, ...]:
        return tuple(sorted(self._received - self._expected))

    @property
    def missing_expected_count(self) -> int:
        return len(self._expected - self._received)

    @property
    def unexpected_received_count(self) -> int:
        return len(self._received - self._expected)

    @property
    def identity_completeness_ratio(self) -> float | None:
        """Matched over expected. Extras cannot push it above 1.0, and cannot
        compensate for a miss."""
        if not self._expected:
            return None
        return self.matched_identity_count / len(self._expected)

    # -- compatibility -------------------------------------------------------

    @property
    def expected_contract_count(self) -> int | None:
        """Retained for the snapshot contract; ``None`` when nothing was
        independently expected."""
        if self.expected_contract_ids is None:
            return None
        return self.expected_identity_count

    @property
    def joined_contract_count(self) -> int:
        return self.received_identity_count

    @property
    def completeness_ratio(self) -> float | None:
        return self.identity_completeness_ratio

    #: Expected-source labels that are not independent statements about the
    #: vendor. ``quote_response`` takes the expectation from the thing being
    #: judged; ``CALLER_DECLARED`` is somebody's list, which is a legitimate
    #: thing to hold and not evidence about what the vendor sent.
    #:
    #: ``snapshot_declared`` is deliberately *not* here. It is set only where a
    #: snapshot already carries a measured status -- a synthetic chain that
    #: constructed its own universe, which really does know it -- and the engine
    #: is reconstructing that measure rather than inventing one.
    NON_INDEPENDENT_SOURCES = frozenset({"none", "quote_response", "CALLER_DECLARED"})

    @property
    def independently_observed(self) -> bool:
        """False when the expectation came from the response, or from a caller."""
        return (
            self.expected_contract_ids is not None
            and self.expected_source not in self.NON_INDEPENDENT_SOURCES
        )

    @property
    def status(self) -> CompletenessStatus:
        """Measurement or absence. Never "complete" on the strength of counts."""
        if not self.independently_observed:
            # An expectation taken from the response being judged, or typed by
            # the caller, is not an independent expectation. Rows arrived and
            # joined; whether more were owed is unknowable from here.
            return (
                CompletenessStatus.UNKNOWN
                if self.received_identity_count == 0
                else CompletenessStatus.PARTIALLY_OBSERVED
            )
        if not self._expected:
            # A universe was supplied and claimed nothing. Nothing is measured.
            return CompletenessStatus.UNKNOWN
        if not self.expected_complete_for_request:
            # A page, not the request. It can still find a hole in what it
            # listed, and it cannot say the chain is whole.
            return (
                CompletenessStatus.PARTIAL_UNIVERSE_MISSING_IDENTITIES
                if self.missing_expected_count
                else CompletenessStatus.PARTIAL_UNIVERSE_ALL_LISTED_PRESENT
            )
        if self.missing_expected_count:
            return CompletenessStatus.MEASURED_INCOMPLETE
        return (
            CompletenessStatus.MEASURED_COMPLETE_WITH_EXTRAS
            if self.unexpected_received_count
            else CompletenessStatus.MEASURED_COMPLETE
        )

    def _sample(self, identities: tuple[str, ...]) -> tuple[list[str], bool]:
        limit = self.IDENTITY_SAMPLE_LIMIT
        return list(identities[:limit]), len(identities) > limit

    def semantic_payload(self) -> dict[str, Any]:
        """Everything a calculation depends on, with no identity list truncated.

        Distinct from ``as_dict`` on purpose. ``as_dict`` is for reports and
        samples the identity lists at 100 entries, because a 5,000-contract
        mismatch is a real possibility on a full SPX chain and a 5,000-entry
        blob in every snapshot's metadata helps nobody.

        A *hash* cannot sample. Two chains differing only in the 101st missing
        identity are two different chains, and the confidence score reads the
        counts those identities produce. So this returns the whole sets, sorted,
        and it is what ``canonical_chain_payload`` covers.
        """
        return {
            "status": self.status.value,
            "expected_source": self.expected_source,
            "expected_complete_for_request": self.expected_complete_for_request,
            "independently_observed": self.independently_observed,
            "expected_contract_ids": (
                sorted(self.expected_contract_ids)
                if self.expected_contract_ids is not None
                else None
            ),
            "received_contract_ids": sorted(self.received_contract_ids),
            "matched_identities": sorted(self._expected & self._received),
            "missing_expected_identities": list(self.missing_expected_identities),
            "unexpected_received_identities": list(self.unexpected_received_identities),
            "identity_completeness_ratio": self.identity_completeness_ratio,
            "received_quote_count": self.received_quote_count,
            "received_oi_count": self.received_oi_count,
            "received_iv_count": self.received_iv_count,
            "received_greeks_count": self.received_greeks_count,
            "missing_by_source": dict(sorted(self.missing_by_source.items())),
        }

    def as_dict(self) -> dict[str, Any]:
        missing, missing_truncated = self._sample(self.missing_expected_identities)
        unexpected, unexpected_truncated = self._sample(
            self.unexpected_received_identities
        )
        return {
            "expected_contract_count": self.expected_contract_count,
            "expected_source": self.expected_source,
            "expected_complete_for_request": self.expected_complete_for_request,
            "received_quote_count": self.received_quote_count,
            "received_oi_count": self.received_oi_count,
            "received_iv_count": self.received_iv_count,
            "received_greeks_count": self.received_greeks_count,
            "joined_contract_count": self.joined_contract_count,
            "expected_identity_count": self.expected_identity_count,
            "received_identity_count": self.received_identity_count,
            "matched_identity_count": self.matched_identity_count,
            "missing_expected_count": self.missing_expected_count,
            "unexpected_received_count": self.unexpected_received_count,
            "missing_expected_identities": missing,
            "missing_expected_identities_truncated": missing_truncated,
            "unexpected_received_identities": unexpected,
            "unexpected_received_identities_truncated": unexpected_truncated,
            "identity_completeness_ratio": self.identity_completeness_ratio,
            "completeness_ratio": self.completeness_ratio,
            "independently_observed": self.independently_observed,
            "status": self.status.value,
            "missing_by_source": dict(sorted(self.missing_by_source.items())),
            # Retained under its v2.1 name so existing readers keep working.
            "unexpected_identities": unexpected,
        }
