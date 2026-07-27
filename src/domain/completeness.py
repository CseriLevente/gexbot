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

from enum import Enum

__all__ = ["COMPLETENESS_WARNING_CODE", "CompletenessStatus"]

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

    #: Rows arrived and joined, but nothing independent says how many should
    #: have. The chain may be whole; it may be the first page of a truncated
    #: response. Nothing here can tell the difference.
    PARTIALLY_OBSERVED = "PARTIALLY_OBSERVED"

    #: Not even the received counts are meaningful -- an empty expectation, or a
    #: caller that never supplied one.
    UNKNOWN = "UNKNOWN"

    @property
    def is_measured(self) -> bool:
        """True only when an independent universe backed the number."""
        return self in (
            CompletenessStatus.MEASURED_COMPLETE,
            CompletenessStatus.MEASURED_COMPLETE_WITH_EXTRAS,
            CompletenessStatus.MEASURED_INCOMPLETE,
        )

    @property
    def implies_complete(self) -> bool:
        """True only for a chain measured against a universe and found whole.

        Deliberately *not* true for ``PARTIALLY_OBSERVED``: "we received
        everything we were sent" is not "we were sent everything".
        """
        return self in (
            CompletenessStatus.MEASURED_COMPLETE,
            CompletenessStatus.MEASURED_COMPLETE_WITH_EXTRAS,
        )
