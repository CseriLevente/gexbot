"""What a request asked for, so two requests can be compared.

A set of identities read out of one capture says nothing about another capture
unless the two asked the same question. v2.1.9 checked that a universe's source
records existed, hashed correctly and produced exactly the claimed identities --
all true, all necessary, and none of it establishing that the listing was *about
this chain*.

A listing of SPXW options expiring within seven days, captured three weeks ago,
can be re-derived perfectly and is still the wrong universe for today's SPX
chain out to sixty days. The identities matching is not reassurance; on a
narrower scope it is exactly what a false ``MEASURED_COMPLETE`` looks like.

So a universe carries the scope it was collected under, and a chain operation
accepts it only when the two scopes are compatible and the timing makes sense.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

__all__ = [
    "UNIVERSE_SCOPE_SCHEMA_VERSION",
    "ScopeCompatibility",
    "UniverseRequestScope",
]

#: Bumped when the *meaning* of a scope comparison changes.
UNIVERSE_SCOPE_SCHEMA_VERSION = "universe-scope/2.1.10"


@dataclass(frozen=True, slots=True)
class ScopeCompatibility:
    """Whether one scope may stand in for another, and why not if it may not."""

    compatible: bool
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"compatible": self.compatible, "reasons": list(self.reasons)}


@dataclass(frozen=True, slots=True)
class UniverseRequestScope:
    """The question a request asked, in the terms that decide the answer set.

    ``requested_at`` is here rather than on the universe because staleness is a
    property of *when the question was asked*, and the answer set is only as
    current as that.
    """

    root: str
    #: Explicit expirations, where the request named them. ``None`` means the
    #: request was expressed as a DTE window instead.
    expirations: tuple[date, ...] | None = None
    max_dte: int | None = None
    #: Strike window in points either side of spot, where the request had one.
    strike_range: int | None = None
    rights: tuple[str, ...] = ("call", "put")
    #: Everything else the request sent that could narrow the answer, as sorted
    #: key/value pairs. A filter nobody recorded is a filter nobody can compare.
    request_filters: tuple[tuple[str, str], ...] = ()
    requested_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", str(self.root).strip().upper())
        if not self.root:
            raise ValueError(
                "UniverseRequestScope.root is empty; a universe that does not "
                "say which underlying it enumerated cannot be compared with "
                "anything"
            )
        object.__setattr__(
            self, "rights", tuple(sorted({str(r).strip().lower() for r in self.rights}))
        )
        object.__setattr__(
            self,
            "request_filters",
            tuple(sorted((str(k), str(v)) for k, v in self.request_filters)),
        )
        if self.expirations is not None:
            object.__setattr__(self, "expirations", tuple(sorted(self.expirations)))
        if self.requested_at is not None and (
            self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None
        ):
            raise ValueError(
                "UniverseRequestScope.requested_at must be timezone-aware; "
                "staleness measured against a naive instant is measured against "
                "whichever zone the reading machine happens to be in"
            )

    # -- comparison ----------------------------------------------------------

    def covers(self, other: UniverseRequestScope) -> ScopeCompatibility:
        """Whether this scope's answer set can serve a request of ``other``.

        Directional on purpose. A *wider* listing can serve a narrower chain --
        every contract the chain owes is in it -- and a narrower listing cannot
        serve a wider chain, because the contracts outside its window would read
        as unexpected extras rather than as never having been enumerated.
        """
        reasons: list[str] = []

        if self.root != other.root:
            reasons.append(
                f"the universe enumerated {self.root} and the chain requested "
                f"{other.root}; a listing of one underlying says nothing about "
                "another"
            )

        if not set(other.rights) <= set(self.rights):
            missing = sorted(set(other.rights) - set(self.rights))
            reasons.append(
                f"the chain requests {missing} and the universe enumerated only "
                f"{list(self.rights)}; the missing rights were never listed, so "
                "their absence from the chain cannot be measured"
            )

        reasons.extend(self._expiration_reasons(other))
        reasons.extend(self._strike_reasons(other))

        mine = dict(self.request_filters)
        theirs = dict(other.request_filters)
        narrowing = sorted(
            key
            for key, value in mine.items()
            if theirs.get(key) != value and key not in ("root", "symbol")
        )
        if narrowing:
            reasons.append(
                f"the universe was collected under filters {narrowing} that the "
                "chain request does not share, so it may enumerate a narrower "
                "set than the chain owes"
            )

        return ScopeCompatibility(compatible=not reasons, reasons=tuple(reasons))

    @property
    def expirations_unbounded(self) -> bool:
        """Whether this request placed no limit on expirations.

        ``expiration="*"`` with no DTE window asks for everything, and a source
        that also asked for everything covers it. Reading ``None`` as "nobody
        said" rather than "no limit" would make the widest possible listing
        unable to serve the widest possible chain.
        """
        return self.expirations is None and self.max_dte is None

    def _expiration_reasons(self, other: UniverseRequestScope) -> list[str]:
        if self.expirations_unbounded:
            # The universe enumerated every expiration, so it covers any subset.
            return []
        if other.expirations_unbounded:
            return [
                "the chain requests every expiration and the universe enumerated "
                f"only {self.describe_expirations()}; contracts outside that "
                "window were never listed"
            ]
        if other.expirations is not None and self.expirations is not None:
            missing = sorted(set(other.expirations) - set(self.expirations))
            if missing:
                return [
                    f"the chain requests expirations {[d.isoformat() for d in missing]}"
                    " which the universe never enumerated"
                ]
            return []
        if self.max_dte is not None and other.max_dte is not None:
            if self.max_dte < other.max_dte:
                return [
                    f"the universe covers {self.max_dte} days to expiry and the "
                    f"chain requests {other.max_dte}; contracts beyond the "
                    "universe's window were never listed"
                ]
            return []
        # One side names explicit expirations and the other a window. Comparing
        # them needs a calendar and a spot; refusing is the honest answer.
        return [
            "the universe and the chain express their expiration scope "
            "differently (explicit expirations against a DTE window), so no "
            "comparison establishes coverage"
        ]

    def describe_expirations(self) -> str:
        if self.expirations is not None:
            return f"{len(self.expirations)} explicit expirations"
        if self.max_dte is not None:
            return f"{self.max_dte} days to expiry"
        return "every expiration"

    def _strike_reasons(self, other: UniverseRequestScope) -> list[str]:
        if self.strike_range is None:
            # No strike filter: the universe covered the whole ladder.
            return []
        if other.strike_range is None:
            return [
                "the chain requests every strike and the universe covered only a "
                f"{self.strike_range}-point window; strikes outside it were "
                "never listed"
            ]
        if self.strike_range < other.strike_range:
            return [
                f"the universe covers a {self.strike_range}-point strike window "
                f"and the chain requests {other.strike_range}; strikes outside "
                "it were never listed"
            ]
        return []

    def age_against(self, chain_requested_at: datetime) -> timedelta | None:
        """How stale this scope is relative to a chain request."""
        if self.requested_at is None:
            return None
        return chain_requested_at - self.requested_at

    # -- identity ------------------------------------------------------------

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": UNIVERSE_SCOPE_SCHEMA_VERSION,
            "root": self.root,
            "expirations": (
                [d.isoformat() for d in self.expirations]
                if self.expirations is not None
                else None
            ),
            "max_dte": self.max_dte,
            "strike_range": self.strike_range,
            "rights": list(self.rights),
            "request_filters": [list(pair) for pair in self.request_filters],
            "requested_at": (
                self.requested_at.isoformat() if self.requested_at else None
            ),
        }

    @property
    def fingerprint(self) -> str:
        from src.domain.digests import digest_of

        return digest_of(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        return {**self.semantic_payload(), "fingerprint": self.fingerprint}
