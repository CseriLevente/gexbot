"""What a settlement convention *says*, expressed so it can be applied.

v2.1.8 made settlement-date evidence something a resolver had to establish
rather than something an enum authorized. That closed the hole where
``reference="lol"`` passed. It left a narrower one open, and it is the one that
matters most:

    resolved = ResolvedSettlementDate(as_of=evidence.as_of, ...)

The documentation resolver looked the rule up, confirmed the rule was in force
on the claimed date, and then returned **the claimed date**. So a registered
rule saying "open interest settles on the prior trading session" would happily
authorize 2026-03-16, 2026-03-15 and 2026-03-01 for a 2026-03-17 chain. The
rule was a permission slip, not a calculation.

``normalized_value: str`` is why. A rule whose content is free text cannot be
applied to anything; the only thing a caller can do with ``"prior_session"`` is
read it. So the date had to come from somewhere else, and the only somewhere
else available was the caller.

Here a rule is *typed semantics* plus a calendar, and it computes::

    resolved = rule.resolve(chain_session_date)

The chain session date comes from the capture operation. Nothing accepts a
settlement date as an input, so there is nothing for a caller to assert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

__all__ = [
    "SETTLEMENT_EVIDENCE_SCHEMA_VERSION",
    "SUPPORTED_CALENDARS",
    "SettlementRule",
    "SettlementRuleError",
    "SettlementRuleKind",
]

#: Bumped when the *meaning* of a settlement rule changes, so a rule recorded
#: under older semantics is refused rather than reinterpreted.
SETTLEMENT_EVIDENCE_SCHEMA_VERSION = "settlement-evidence/2.1.9"

#: Calendars this repository can actually apply. Named rather than assumed: a
#: rule citing a calendar nobody implemented would otherwise be silently
#: evaluated against the only one that exists, and "the prior session" means
#: different days on different exchanges.
SUPPORTED_CALENDARS = frozenset({"US_EQUITY_INDEX_OPTIONS"})


class SettlementRuleError(ValueError):
    """A settlement rule that cannot be applied, or was applied to nonsense."""


class SettlementRuleKind(str, Enum):
    """How a settlement date follows from a chain's session date.

    Three kinds because three are enough to express every convention this
    repository has encountered a description of, and adding a fourth should be
    a deliberate act with a document behind it.
    """

    #: Open interest belongs to the session the chain was captured in. Unusual
    #: -- OI is normally a post-session settlement artefact -- but it is what an
    #: intraday-updating feed would mean, and refusing to express it would push
    #: somebody towards the free-text field this type replaces.
    SAME_SESSION = "SAME_SESSION"

    #: The session before this one, skipping weekends and holidays. The
    #: convention most US index-option venues describe.
    PRIOR_TRADING_SESSION = "PRIOR_TRADING_SESSION"

    #: N trading sessions back, N given by ``trading_session_offset``. Negative
    #: offsets are forward, which no settlement convention should need -- so
    #: they are refused rather than quietly supported.
    TRADING_SESSION_OFFSET = "TRADING_SESSION_OFFSET"

    @property
    def needs_offset(self) -> bool:
        return self is SettlementRuleKind.TRADING_SESSION_OFFSET


@dataclass(frozen=True, slots=True)
class SettlementRule:
    """A settlement convention this repository can apply to a session date.

    Deliberately holds no date. A rule that carried the answer would be an
    assertion wearing a rule's clothes, which is exactly the v2.1.8 defect:
    ``normalized_value="prior_session"`` was carried alongside a caller-supplied
    ``as_of``, and the resolver returned the ``as_of``.
    """

    kind: SettlementRuleKind
    #: Required for ``TRADING_SESSION_OFFSET``, refused for the other two --
    #: an offset on a rule that does not use one is a claim nobody applies.
    trading_session_offset: int | None = None
    calendar_id: str = "US_EQUITY_INDEX_OPTIONS"
    schema_version: str = SETTLEMENT_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        kind = SettlementRuleKind(self.kind)
        object.__setattr__(self, "kind", kind)
        if self.calendar_id not in SUPPORTED_CALENDARS:
            raise SettlementRuleError(
                f"calendar {self.calendar_id!r} is not implemented; this "
                f"repository knows {sorted(SUPPORTED_CALENDARS)}. 'The prior "
                "session' is a different day on a different exchange, so a rule "
                "naming an unknown calendar cannot be applied to anything."
            )
        if kind.needs_offset:
            if self.trading_session_offset is None:
                raise SettlementRuleError(
                    "TRADING_SESSION_OFFSET needs trading_session_offset; a "
                    "rule that does not say how many sessions back cannot "
                    "produce a date"
                )
            if self.trading_session_offset < 0:
                raise SettlementRuleError(
                    f"trading_session_offset is {self.trading_session_offset}; a "
                    "settlement session after the chain session describes a "
                    "settlement that has not happened"
                )
        elif self.trading_session_offset is not None:
            raise SettlementRuleError(
                f"{kind.value} takes no trading_session_offset, got "
                f"{self.trading_session_offset}. An unused parameter on a rule "
                "reads as though it were applied."
            )

    # -- application ---------------------------------------------------------

    def resolve(self, chain_session_date: date) -> date:
        """The settlement date this rule produces for a chain session.

        The only way to obtain a settlement date in this repository. It takes
        the session the chain was captured in and returns what the convention
        says; there is no parameter through which a caller can suggest an
        answer.
        """
        from src.gex.calendar import is_trading_session, previous_session

        if not isinstance(chain_session_date, date):
            raise SettlementRuleError(
                f"chain_session_date must be a date, got "
                f"{type(chain_session_date).__name__}"
            )
        if not is_trading_session(chain_session_date):
            raise SettlementRuleError(
                f"{chain_session_date.isoformat()} is not a trading session "
                f"({chain_session_date.strftime('%A')}), so no chain was "
                "captured in it and no settlement date follows from it"
            )

        if self.kind is SettlementRuleKind.SAME_SESSION:
            return chain_session_date

        steps = (
            1
            if self.kind is SettlementRuleKind.PRIOR_TRADING_SESSION
            else int(self.trading_session_offset or 0)
        )
        cursor = chain_session_date
        for _ in range(steps):
            cursor = previous_session(cursor)
        return cursor

    def produces(self, settlement_date: date, *, chain_session_date: date) -> bool:
        """Whether this rule really yields that date. For checks, never for
        derivation -- ``resolve`` is the only thing that produces a date."""
        try:
            return self.resolve(chain_session_date) == settlement_date
        except SettlementRuleError:
            return False

    # -- identity ------------------------------------------------------------

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "trading_session_offset": self.trading_session_offset,
            "calendar_id": self.calendar_id,
        }

    @property
    def fingerprint(self) -> str:
        from src.domain.digests import digest_of

        return digest_of(self.semantic_payload())

    def describe(self) -> str:
        if self.kind is SettlementRuleKind.SAME_SESSION:
            return "open interest belongs to the chain's own session"
        if self.kind is SettlementRuleKind.PRIOR_TRADING_SESSION:
            return "open interest settles on the prior trading session"
        return (
            f"open interest settles {self.trading_session_offset} trading "
            "sessions before the chain session"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(),
            "fingerprint": self.fingerprint,
            "description": self.describe(),
        }
