"""Which of a capture's listed identities this session's analysis may use.

A certification report answers "are these bytes what they claim to be, and what
do they say about the vendor's conventions". It does not answer "which of the
contracts in them belong to the session the capture was taken in", and until
v2.1.28 nothing did. Two observed vendor behaviours make that a separate
question with a separate answer.

**Set equality is not currency.** ``UniverseCertification`` establishes that the
contract list, the quote snapshot and the Greeks snapshot name the same
identities. On Aug 26 all three named roughly five hundred contracts that had
expired on Aug 25 -- with Aug-25 market timestamps -- and on Aug 27 those were
gone and roughly five hundred Aug-26 contracts had replaced them. Three
responses agreeing is three responses agreeing. It is not a statement that they
agree about *this* session.

**An absent open-interest row is not zero.** ``OpenInterestCoverage`` already
counts the identities the open-interest endpoint did not answer, and refuses a
trusted aggregate while any exist. What it does not do is say what an unanswered
identity *is*, and the longitudinal captures are unambiguous: unanswered
identities acquire explicit records after the settlement boundary, positively
often enough that zero is not a safe reading. See ``docs/DATA_ELIGIBILITY.md``.

So this module partitions one capture's listed universe into the identities an
analytical dataset could use and the identities it could not, with a named
reason for each exclusion and a set hash per class. **It excludes; it does not
delete.** Every identity stays in the capture, stays in ``CaptureUniverse``, and
stays counted here -- an identity dropped without a counter is a universe nobody
can reconcile.

**It is deliberately a separate report, not a section of the certification
report.** Certification's schema, its findings and its two live-capture
reference artifacts are unchanged by this release: nothing about what a
certification *derives from a capture* has changed, only what a later analytical
layer is permitted to do with the result. This is the same separation
``oi_transition`` was built under, and for the same reason -- a new question
about a capture gets a new report with its own content hash, so the existing one
keeps reproducing.

Offline and deterministic. The capture is certified before it is read, so the
partition never runs over bytes that failed their own verification.
"""

from __future__ import annotations

import pathlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from src.adapters.thetadata.capture_certification import (
    CaptureCertificationError,
    CaptureUniverse,
    ContractKey,
    _set_hash,
    capture_universe,
    certify_capture,
    load_capture,
)
from src.domain.analytical_universe import (
    ANALYTICAL_UNIVERSE_SCHEMA_VERSION,
    AnalyticalExclusion,
    OpenInterestObservationState,
    TemporalEligibility,
    analytical_exclusion,
    open_interest_observation_state,
    temporal_eligibility,
)
from src.domain.canonical import CANONICAL_REPORT_SCHEMA_VERSION

__all__ = [
    "ANALYTICAL_UNIVERSE_ALGORITHM_VERSION",
    "ANALYTICAL_UNIVERSE_REPORT_SCHEMA_VERSION",
    "AnalyticalUniverseError",
    "AnalyticalUniverseReport",
    "ExpirationEligibility",
    "UniverseClass",
    "analytical_universe",
    "analytical_universe_of",
]

#: Bumped when what an analytical-universe report must carry changes.
ANALYTICAL_UNIVERSE_REPORT_SCHEMA_VERSION = "analytical-universe-report/2.1.28"

#: Bumped when the *classification* changes -- when an identity that used to
#: land in one class would now land in another. Separate from the schema
#: because two reports carrying the same fields are not comparable if they were
#: classified differently, and the report hash has to say so.
ANALYTICAL_UNIVERSE_ALGORITHM_VERSION = "analytical-universe/1"


class AnalyticalUniverseError(ValueError):
    """A capture whose analytical universe cannot be established."""


class UniverseClass(str, Enum):
    """Where one listed identity stands for this session.

    Mutually exclusive and jointly exhaustive over the capture's listed
    universe: every identity lands in exactly one, and
    :attr:`AnalyticalUniverseReport.accounting_is_exhaustive` asserts the sum
    rather than assuming it.

    The two eligible classes are kept apart because 0DTE is the series an
    intraday gamma model is mostly about, and a rule that removes stale
    contracts must be checkable against the claim that it does not remove
    same-session ones.
    """

    #: Expires after this session, and the vendor answered its open interest.
    ELIGIBLE_CURRENT = "ELIGIBLE_CURRENT"
    #: Expires in this session, and the vendor answered its open interest.
    #: Eligible. It expires today; that is not a reason to exclude it.
    ELIGIBLE_EXPIRING_THIS_SESSION = "ELIGIBLE_EXPIRING_THIS_SESSION"
    #: Retained by the vendor from an earlier session. Not part of this
    #: session's universe whatever its open interest says.
    EXCLUDED_EXPIRED_BEFORE_SESSION = "EXCLUDED_EXPIRED_BEFORE_SESSION"
    #: Temporally current, and no open-interest record exists for the resolved
    #: settlement session. Ineligible for anything that needs a weight, and
    #: explicitly not a zero.
    EXCLUDED_OPEN_INTEREST_NOT_REPORTED = "EXCLUDED_OPEN_INTEREST_NOT_REPORTED"

    @property
    def is_eligible(self) -> bool:
        return self in (
            UniverseClass.ELIGIBLE_CURRENT,
            UniverseClass.ELIGIBLE_EXPIRING_THIS_SESSION,
        )


_EXCLUSION_CLASS: dict[AnalyticalExclusion, UniverseClass] = {
    AnalyticalExclusion.EXPIRED_BEFORE_SESSION: (
        UniverseClass.EXCLUDED_EXPIRED_BEFORE_SESSION
    ),
    AnalyticalExclusion.OPEN_INTEREST_NOT_REPORTED: (
        UniverseClass.EXCLUDED_OPEN_INTEREST_NOT_REPORTED
    ),
}

_ELIGIBLE_CLASS: dict[TemporalEligibility, UniverseClass] = {
    TemporalEligibility.CURRENT: UniverseClass.ELIGIBLE_CURRENT,
    TemporalEligibility.EXPIRING_THIS_SESSION: (
        UniverseClass.ELIGIBLE_EXPIRING_THIS_SESSION
    ),
}


@dataclass(frozen=True, slots=True)
class ExpirationEligibility:
    """One expiration's contribution to the partition."""

    expiration: str
    listed: int
    class_counts: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "expiration": self.expiration,
            "listed": self.listed,
            "class_counts": dict(sorted(self.class_counts.items())),
        }


@dataclass(frozen=True, slots=True)
class AnalyticalUniverseReport:
    """What one capture's listed universe may and may not be used for.

    Findings only. Nothing here authorizes a calculation, and nothing here
    removes an identity from the capture: an excluded contract is named,
    counted, hashed into its class set and left exactly where it was.
    """

    schema_version: str
    algorithm_version: str
    eligibility_schema_version: str
    universe: CaptureUniverse
    #: The session temporal eligibility was decided against, and where it came
    #: from. Read off the verified contract-list request rather than derived
    #: from an instant here -- see :func:`analytical_universe_of`.
    market_session_date: date
    market_session_source: str
    #: The session the capture's own valuation timestamp resolves to, when that
    #: timestamp can be read at all. Recorded so the two can be compared instead
    #: of one of them being trusted silently.
    valuation_session_date: date | None
    #: identity -> class, over the whole listed universe.
    classified: dict[ContractKey, UniverseClass]

    # -- counts -------------------------------------------------------------

    @property
    def class_counts(self) -> dict[str, int]:
        counts = dict.fromkeys((c.value for c in UniverseClass), 0)
        for outcome in self.classified.values():
            counts[outcome.value] += 1
        return counts

    @property
    def listed_count(self) -> int:
        return len(self.universe.expected)

    @property
    def eligible_count(self) -> int:
        return sum(1 for c in self.classified.values() if c.is_eligible)

    @property
    def excluded_count(self) -> int:
        return self.listed_count - self.eligible_count

    @property
    def exclusion_counts(self) -> dict[str, int]:
        """Exclusions by reason, in the domain's own vocabulary.

        Published beside the class counts rather than instead of them: the
        classes are the authority, and this is the two-line answer to "why is
        the analytical universe smaller than the listing".
        """
        counts = self.class_counts
        return {
            reason.value: counts[_EXCLUSION_CLASS[reason].value]
            for reason in AnalyticalExclusion
        }

    def identities_in(self, outcome: UniverseClass) -> tuple[ContractKey, ...]:
        return tuple(
            sorted(key for key, value in self.classified.items() if value is outcome)
        )

    @property
    def class_hashes(self) -> dict[str, str]:
        """A set hash per class, so a later reader can check membership.

        Counts alone would let two different sets of 302 identities look
        identical, which is the mistake set hashing exists to prevent.
        """
        return {
            outcome.value: _set_hash(set(self.identities_in(outcome)))
            for outcome in UniverseClass
        }

    @property
    def per_expiration(self) -> tuple[ExpirationEligibility, ...]:
        grouped: dict[str, list[ContractKey]] = defaultdict(list)
        for key in self.universe.expected:
            grouped[key.expiration.isoformat()].append(key)
        rows: list[ExpirationEligibility] = []
        for expiration in sorted(grouped):
            keys = grouped[expiration]
            counts: dict[str, int] = defaultdict(int)
            for key in keys:
                counts[self.classified[key].value] += 1
            rows.append(
                ExpirationEligibility(
                    expiration=expiration,
                    listed=len(keys),
                    class_counts=dict(counts),
                )
            )
        return tuple(rows)

    # -- verdicts -----------------------------------------------------------

    @property
    def accounting_is_exhaustive(self) -> bool:
        """Every listed identity was classified exactly once."""
        listed = set(self.universe.expected)
        return (
            set(self.classified) == listed
            and len(self.classified) == len(listed)
            and sum(self.class_counts.values()) == len(listed)
        )

    @property
    def sessions_agree(self) -> bool:
        """Whether the request's session and the capture's instant agree.

        ``True`` when the valuation timestamp could not be read at all: an
        unreadable clock is not a disagreement, and it is reported separately
        through :attr:`valuation_session_date` being ``None``. It still costs
        the capture :attr:`permits_trusted_analytical_universe`, below.
        """
        return (
            self.valuation_session_date is None
            or self.valuation_session_date == self.market_session_date
        )

    @property
    def blockers(self) -> tuple[str, ...]:
        """Why this capture's listed universe is not a trusted analytical one.

        Disqualifying, not qualifying. An empty tuple means this partition found
        nothing wrong in what *it* looks at, which is a long way from a capture
        being usable -- the settlement-date authority, the universe-completeness
        evidence and the pricing-compatibility matrix are all elsewhere and all
        still unresolved. See :attr:`permits_trusted_analytical_universe`.
        """
        counts = self.class_counts
        found: list[str] = []
        stale = counts[UniverseClass.EXCLUDED_EXPIRED_BEFORE_SESSION.value]
        if stale:
            found.append(
                f"{stale} of {self.listed_count} listed identities expired "
                f"before the {self.market_session_date.isoformat()} session and "
                "are still being returned. Contract-list, quote and greeks set "
                "equality says the three responses agree; it does not say they "
                "agree about this session."
            )
        unreported = counts[UniverseClass.EXCLUDED_OPEN_INTEREST_NOT_REPORTED.value]
        if unreported:
            found.append(
                f"{unreported} of {self.listed_count} listed identities have no "
                "open-interest record for the resolved settlement session. An "
                "absent record is not a zero, and no evidence-backed policy "
                "exists for an absent one."
            )
        if self.valuation_session_date is None:
            found.append(
                "the capture's valuation timestamp could not be resolved to a "
                "market session, so the session temporal eligibility was decided "
                "against could not be cross-checked against the capture's own "
                "clock."
            )
        elif not self.sessions_agree:
            found.append(
                f"the contract-list request names session "
                f"{self.market_session_date.isoformat()} and the capture's "
                f"valuation timestamp resolves to "
                f"{self.valuation_session_date.isoformat()}. Which session the "
                "universe is of decides which contracts are stale, and the two "
                "records of it disagree."
            )
        if not self.accounting_is_exhaustive:
            found.append(
                "the partition does not account for every listed identity "
                "exactly once, so the counts below do not describe the listing."
            )
        return tuple(found)

    @property
    def permits_trusted_analytical_universe(self) -> bool:
        """Whether the listed universe is, in itself, currently analysable.

        Necessary and nowhere near sufficient -- which is why the name says
        *universe* and not *calculation*. A capture with no stale identity and
        no unreported open interest has cleared the two findings this report
        knows about; the settlement-date authority (OD-26), the independent
        universe evidence (OD-11) and the pricing-compatibility matrix are
        untouched by it, and each of them independently blocks a trusted GEX.
        """
        return not self.blockers

    @property
    def findings(self) -> tuple[str, ...]:
        """What this capture shows, as statements about this capture."""
        counts = self.class_counts
        said = [
            f"{self.listed_count} identities are listed for session "
            f"{self.market_session_date.isoformat()}. "
            f"{self.eligible_count} are eligible for an analytical dataset "
            f"({counts[UniverseClass.ELIGIBLE_CURRENT.value]} current, "
            f"{counts[UniverseClass.ELIGIBLE_EXPIRING_THIS_SESSION.value]} "
            "expiring in this session) and "
            f"{self.excluded_count} are not."
        ]
        stale = counts[UniverseClass.EXCLUDED_EXPIRED_BEFORE_SESSION.value]
        if stale:
            expirations = sorted(
                {
                    key.expiration.isoformat()
                    for key in self.identities_in(
                        UniverseClass.EXCLUDED_EXPIRED_BEFORE_SESSION
                    )
                }
            )
            said.append(
                f"{stale} identities expired before this session and are still "
                f"listed, across expirations {expirations}."
            )
        unreported = counts[UniverseClass.EXCLUDED_OPEN_INTEREST_NOT_REPORTED.value]
        if unreported:
            said.append(
                f"{unreported} temporally current identities carry no "
                "open-interest record. They are excluded as unavailable, not "
                "weighted at zero."
            )
        return tuple(said)

    @property
    def policy_status(self) -> tuple[str, ...]:
        """The distinction this module is careful to keep.

        Eligibility is a rule about what *this repository* will compute over.
        It is not a claim that the vendor defines an absent row as zero, and it
        is not an imputation policy. Both would change every aggregate that
        consumed them, and neither follows from an eligibility rule.
        """
        return (
            "ANALYTICAL_UNIVERSE_TEMPORAL_ELIGIBILITY_APPLIED",
            "OPEN_INTEREST_AVAILABILITY_ELIGIBILITY_APPLIED",
            "OI_IMPUTATION_POLICY_UNRESOLVED",
        )

    # -- serialisation ------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "algorithm_version": self.algorithm_version,
            "eligibility_schema_version": self.eligibility_schema_version,
            "capture": self.universe.as_dict(),
            "market_session_date": self.market_session_date.isoformat(),
            "market_session_source": self.market_session_source,
            "valuation_session_date": (
                self.valuation_session_date.isoformat()
                if self.valuation_session_date
                else None
            ),
            "sessions_agree": self.sessions_agree,
            "listed_count": self.listed_count,
            "eligible_count": self.eligible_count,
            "excluded_count": self.excluded_count,
            "class_counts": dict(sorted(self.class_counts.items())),
            "exclusion_counts": dict(sorted(self.exclusion_counts.items())),
            "class_identity_hashes": dict(sorted(self.class_hashes.items())),
            "accounting_is_exhaustive": self.accounting_is_exhaustive,
            "per_expiration": [row.as_dict() for row in self.per_expiration],
            "findings": list(self.findings),
            "analytical_universe_blockers": list(self.blockers),
            "permits_trusted_analytical_universe": (
                self.permits_trusted_analytical_universe
            ),
            "analytical_evidence_status": list(self.policy_status),
            "imputation_policy": (
                "NONE. An identity with no open-interest record is excluded as "
                "unavailable. It is not weighted at zero, and this report does "
                "not establish that the vendor means zero by an absent record. "
                "An identity retained past its expiration is excluded from this "
                "session's universe; it is not deleted from the capture."
            ),
        }

    def canonical_payload(self) -> dict[str, Any]:
        from src.domain.canonical import canonical_payload

        return {
            "canonical_schema_version": CANONICAL_REPORT_SCHEMA_VERSION,
            **canonical_payload(self.as_dict()),
        }

    def report_hash(self) -> str:
        """Content identity for the partition itself.

        Covers the capture's manifest hash and session id through
        ``CaptureUniverse.as_dict``, the algorithm version and the canonical
        rendering -- so a hash cannot be reproduced by a different capture, nor
        by the same capture under a classification that has since changed.
        """
        from src.domain.digests import digest_of

        return digest_of(self.canonical_payload())


def _valuation_session(universe: CaptureUniverse) -> date | None:
    """The session the capture's own valuation timestamp belongs to.

    ``None`` when the recorded timestamp is absent, unparseable or naive.
    Deliberately not defaulted to the request's session: the point of reading it
    is to have a second, independent record of which session the capture is of,
    and a fallback would quietly make the cross-check compare a value with
    itself.
    """
    from src.gex.sessions import market_session_date

    text = (universe.valuation_timestamp or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return market_session_date(moment)


def analytical_universe_of(universe: CaptureUniverse) -> AnalyticalUniverseReport:
    """Partition one already-loaded capture universe. Pure and offline.

    The session comes from ``CaptureUniverse.session_date`` -- the ``date``
    parameter the contract-list request actually carried, which the capturing
    session set from :func:`src.gex.sessions.market_session_date` and which is
    inside the preflight-approval digest stamped on every manifest record. So
    the session temporal eligibility is decided against is the session a human
    approved and the vendor was asked about, rather than one recomputed here
    from an instant.

    The capture's own valuation timestamp is resolved separately and compared,
    so a capture whose two records of its session disagree is refused a trusted
    analytical universe instead of being partitioned against whichever one this
    function happened to read.
    """
    text = (universe.session_date or "").strip()
    try:
        session = date.fromisoformat(text)
    except ValueError:
        raise AnalyticalUniverseError(
            f"the contract-list request records session date {text!r}, which is "
            "not a date. Temporal eligibility is decided against the session "
            "this capture is of, and a universe cannot be partitioned against a "
            "session nobody can name."
        ) from None

    classified: dict[ContractKey, UniverseClass] = {}
    for key in universe.expected:
        state = _open_interest_state(universe, key)
        reason = analytical_exclusion(
            expiration=key.expiration,
            market_session_date=session,
            open_interest_state=state,
        )
        classified[key] = (
            _EXCLUSION_CLASS[reason]
            if reason is not None
            else _ELIGIBLE_CLASS[
                temporal_eligibility(key.expiration, market_session_date=session)
            ]
        )

    return AnalyticalUniverseReport(
        schema_version=ANALYTICAL_UNIVERSE_REPORT_SCHEMA_VERSION,
        algorithm_version=ANALYTICAL_UNIVERSE_ALGORITHM_VERSION,
        eligibility_schema_version=ANALYTICAL_UNIVERSE_SCHEMA_VERSION,
        universe=universe,
        market_session_date=session,
        market_session_source="VERIFIED_CONTRACT_LIST_REQUEST_DATE",
        valuation_session_date=_valuation_session(universe),
        classified=classified,
    )


def _open_interest_state(
    universe: CaptureUniverse, key: ContractKey
) -> OpenInterestObservationState:
    """The capture's open-interest reading for one listed identity.

    Goes through the domain classifier rather than testing ``is None`` here, so
    the certification layer and the engine cannot drift into two readings of the
    same three states.
    """
    return open_interest_observation_state(universe.answered.get(key))


def analytical_universe(
    capture_root: pathlib.Path | str,
    *,
    archive: pathlib.Path | str | None = None,
) -> AnalyticalUniverseReport:
    """Certify a capture, then partition its listed universe. No network access.

    The capture is certified first, for the same reason the cross-capture
    comparison certifies first: a partition over bytes that failed their own
    verification would look exactly as authoritative as one over bytes that
    passed.
    """
    root = pathlib.Path(capture_root)
    try:
        certify_capture(root, archive_path=pathlib.Path(archive) if archive else None)
    except CaptureCertificationError as error:
        raise AnalyticalUniverseError(
            f"the capture at {root} does not certify: {error}"
        ) from error
    return analytical_universe_of(capture_universe(load_capture(root)))
