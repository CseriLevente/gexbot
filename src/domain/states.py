"""Closed enumerations for regime and pipeline state.

The plan requires the regime output to be a fixed, closed enumeration. Anything
that is not one of these six values is a bug, not a new regime.
"""

from __future__ import annotations

from enum import Enum


class Regime(str, Enum):
    POSITIVE_GAMMA = "POSITIVE_GAMMA"
    NEGATIVE_GAMMA = "NEGATIVE_GAMMA"
    NEUTRAL = "NEUTRAL"
    UNCERTAIN = "UNCERTAIN"
    DATA_HALT = "DATA_HALT"
    RISK_HALT = "RISK_HALT"

    @property
    def is_tradeable(self) -> bool:
        """Only the two directional regimes may produce trade candidates."""
        return self in (Regime.POSITIVE_GAMMA, Regime.NEGATIVE_GAMMA)

    @property
    def is_halt(self) -> bool:
        return self in (Regime.DATA_HALT, Regime.RISK_HALT)


class ResearchStage(str, Enum):
    """Walk-forward promotion gates. A build may only advance one step at a time
    and never skips PAPER.
    """

    DEVELOPMENT = "DEVELOPMENT"
    VALIDATION = "VALIDATION"
    OOS = "OOS"
    PAPER = "PAPER"
    LIVE_STAGE_1 = "LIVE_STAGE_1"
    LIVE_STAGE_2 = "LIVE_STAGE_2"

    @property
    def is_live(self) -> bool:
        return self in (ResearchStage.LIVE_STAGE_1, ResearchStage.LIVE_STAGE_2)


_ORDER = tuple(ResearchStage)


def next_stage(stage: ResearchStage) -> ResearchStage | None:
    index = _ORDER.index(stage)
    return _ORDER[index + 1] if index + 1 < len(_ORDER) else None
