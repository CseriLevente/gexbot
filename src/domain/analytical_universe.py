"""Which contracts this session's analysis is allowed to be about.

Two vendor behaviours, both established by repeated live ThetaData captures and
neither of them a defect in the vendor. See ``docs/DATA_ELIGIBILITY.md`` for the
observations themselves and for the boundary this module is careful about: these
are **data-eligibility rules for this repository's analytics**, not claims about
what ThetaData means by anything.

**An absent open-interest row is not an open interest of zero.** Aug 26 → Aug 27
is the strongest consecutive-session reading: 438 identities present in the
contract list, the quote snapshot and the Greeks snapshot carried no
open-interest row on the 26th, and on the 27th every one of the 438 carried an
explicit record -- 177 positive, 261 an explicit zero, none still absent, none
gone. So absence is a state the settlement boundary later resolves, and it
resolves to a positive figure often enough that treating it as zero is not
conservative. It is a different market.

Open interest is the linear weight on every GEX term, so a contract weighted
zero for want of a number is a contract deleted from the aggregate with no
counter incremented. The three states below keep that impossible to do quietly:
a contract whose settled open interest is unavailable is *ineligible*, counted
under a reason, and still present in the chain and in the evidence.

**Snapshot endpoints retain expired contracts for a session.** On Aug 26 the
snapshots still returned roughly five hundred Aug-25 contracts, carrying Aug-25
market timestamps; by Aug 27 those were gone and roughly five hundred Aug-26
contracts had taken their place. Set equality between the contract list, the
quote snapshot and the Greeks snapshot therefore says the three endpoints agree
-- it does not say they agree about *this* session. So temporal eligibility is
decided against the capture's resolved market session:

    expiration_date <  market_session_date  ->  ineligible, and counted
    expiration_date == market_session_date  ->  eligible (0DTE)
    expiration_date >  market_session_date  ->  eligible

The middle line is deliberate. A contract that expires today is the whole point
of an intraday gamma model, and the rule above must never be read as a filter on
0DTE. A same-session contract that has already passed its settlement clock is
excluded elsewhere, by ``ResolutionIssue.EXPIRED``, which is a different fact
measured against a different quantity -- the settlement *instant*, root by root.

Nothing here decides that a missing row means zero, and nothing here decides
that an excluded contract may be dropped from the record. Both would be model
decisions; these are eligibility findings, and every one of them is counted.
"""

from __future__ import annotations

from datetime import date
from enum import Enum

__all__ = [
    "ANALYTICAL_UNIVERSE_SCHEMA_VERSION",
    "AnalyticalExclusion",
    "OpenInterestObservationState",
    "TemporalEligibility",
    "analytical_exclusion",
    "open_interest_observation_state",
    "temporal_eligibility",
]

#: Bumped when the *meaning* of an eligibility verdict changes -- when an
#: identity that used to land in one class would now land in another. A stored
#: accounting taken under older semantics must be refused rather than compared.
ANALYTICAL_UNIVERSE_SCHEMA_VERSION = "analytical-universe/2.1.28"


class OpenInterestObservationState(str, Enum):
    """What the open-interest response said about ONE contract identity.

    Three states, not two, and the third is the one this release exists for. An
    absent record and a record reading ``0`` are different vendor acts: the
    second is a measurement -- the contract exists and nobody holds it -- while
    the first is a silence that the next settlement boundary has repeatedly been
    observed to break, often with a positive figure.

    Named to cross-walk with the certification layer's existing vocabulary,
    ``OpenInterestCoverageState`` (``OI_PRESENT`` / ``OI_EXPLICIT_ZERO`` /
    ``OI_MISSING``) and ``CaptureUniverse.state_of``. ``REPORTED`` rather than
    ``PRESENT`` because ``OpenInterestCoverage.present_count`` already means
    *positive* one layer up, and a second reading of the same word is how two
    counts come to disagree while both look right.
    """

    #: The vendor sent a record, and it carries a positive contract count.
    OI_REPORTED_POSITIVE = "OI_REPORTED_POSITIVE"
    #: The vendor sent a record, and it reads zero. A usable observation.
    OI_REPORTED_ZERO = "OI_REPORTED_ZERO"
    #: No open-interest record exists for this identity in the resolved
    #: settlement session. Explicitly **not a number**: there is no weight, and
    #: inventing one -- in particular inventing ``0`` -- is the failure mode the
    #: longitudinal evidence rules out.
    OI_NOT_REPORTED = "OI_NOT_REPORTED"

    @property
    def is_reported(self) -> bool:
        """Whether the vendor answered for this identity, whatever it answered."""
        return self is not OpenInterestObservationState.OI_NOT_REPORTED

    @property
    def permits_oi_weighting(self) -> bool:
        """Whether a calculation that needs an open-interest weight may proceed.

        Identical to :attr:`is_reported` today and kept separate on purpose: one
        is a statement about the vendor's response, the other is a permission
        this repository grants. Collapsing them is how a reporting distinction
        turns into an eligibility decision nobody wrote down.
        """
        return self.is_reported


def open_interest_observation_state(
    open_interest: int | None,
) -> OpenInterestObservationState:
    """Classify one contract's open-interest reading.

    ``None`` means no record was joined for this identity. A negative count is
    classified as unreported rather than as a measurement -- validation rejects
    it separately under ``NEGATIVE_OPEN_INTEREST``, and a negative contract
    count is a parse failure, not a small position -- so nothing downstream can
    read it as a usable weight even if it reaches here.
    """
    if open_interest is None or open_interest < 0:
        return OpenInterestObservationState.OI_NOT_REPORTED
    if open_interest == 0:
        return OpenInterestObservationState.OI_REPORTED_ZERO
    return OpenInterestObservationState.OI_REPORTED_POSITIVE


class TemporalEligibility(str, Enum):
    """Where one contract's expiration sits relative to the session.

    Three classes, mutually exclusive and jointly exhaustive over any
    ``(expiration, session)`` pair, so the accounting can be checked by addition
    rather than trusted.
    """

    #: Expires after this session. The ordinary case.
    CURRENT = "CURRENT"
    #: Expires in this session. 0DTE, eligible, and the reason this rule is a
    #: strict inequality: removing same-session expirations would remove the
    #: series an intraday gamma model is mostly about.
    EXPIRING_THIS_SESSION = "EXPIRING_THIS_SESSION"
    #: Expired before this session opened, and the vendor is still returning it.
    #: Real rows, really sent, describing a contract that no longer exists.
    EXPIRED_BEFORE_SESSION = "EXPIRED_BEFORE_SESSION"

    @property
    def is_eligible(self) -> bool:
        """Whether this contract may enter the current analytical universe."""
        return self is not TemporalEligibility.EXPIRED_BEFORE_SESSION


def temporal_eligibility(
    expiration: date, *, market_session_date: date
) -> TemporalEligibility:
    """Classify one expiration against the session the capture belongs to.

    ``market_session_date`` must come from
    :func:`src.gex.sessions.market_session_date` or from the session parameter a
    verified request actually carried. It is deliberately a ``date`` argument
    rather than an instant: this module may not choose a timezone, and the
    difference between the UTC day and the market's session is six hours out of
    every twenty-four.
    """
    if expiration < market_session_date:
        return TemporalEligibility.EXPIRED_BEFORE_SESSION
    if expiration == market_session_date:
        return TemporalEligibility.EXPIRING_THIS_SESSION
    return TemporalEligibility.CURRENT


class AnalyticalExclusion(str, Enum):
    """Why a contract the vendor returned is not in the analytical universe.

    Two reasons, kept apart because they are answered by different evidence and
    fixed by different things. A stale identity is a vendor-retention artefact
    that a later capture removes on its own; an unreported open interest is a
    settlement figure that has not arrived yet. Collapsing them into one
    "excluded" count would make a universe nobody can reconcile.
    """

    EXPIRED_BEFORE_SESSION = "EXPIRED_BEFORE_SESSION"
    OPEN_INTEREST_NOT_REPORTED = "OPEN_INTEREST_NOT_REPORTED"


def analytical_exclusion(
    *,
    expiration: date,
    market_session_date: date,
    open_interest_state: OpenInterestObservationState,
) -> AnalyticalExclusion | None:
    """Why this contract is ineligible, or ``None`` when it is eligible.

    Staleness is decided first. A contract that expired before the session is
    out whatever its open interest says, and reporting it under the
    open-interest reason instead would attribute a vendor-retention artefact to
    a settlement gap -- two findings with different remedies, merged.
    """
    if not temporal_eligibility(
        expiration, market_session_date=market_session_date
    ).is_eligible:
        return AnalyticalExclusion.EXPIRED_BEFORE_SESSION
    if not open_interest_state.permits_oi_weighting:
        return AnalyticalExclusion.OPEN_INTEREST_NOT_REPORTED
    return None
