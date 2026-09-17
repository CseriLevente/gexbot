"""Declared point-in-time checks, never a substitute for verified source bytes."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from src.domain.canonical import canonical_payload
from src.domain.digests import digest_of
from src.gex.calendar import (
    is_trading_session,
    previous_session,
    session_close_datetime,
)
from src.gex.sessions import EASTERN

SCHEMA_VERSION = "intraday-research-contract/2.1.33"
AUDIT_SCHEMA_VERSION = "research-input-audit/2.1.33"
REQUIRED_INPUTS = frozenset(
    {
        "contract_list",
        "option_quote",
        "greeks",
        "spx_price",
        "open_interest",
        "model_evidence",
    }
)
MARKET_INPUTS = frozenset({"option_quote", "greeks", "spx_price"})


def _aware(stamp: datetime) -> None:
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timestamps must carry a UTC offset")


@dataclass(frozen=True)
class ResearchContract:
    """Research design parameters; freshness budgets are not vendor guarantees."""

    root: str = "SPXW"
    max_dte: int = 7
    cadence_seconds: int = 60
    open_delay_minutes: int = 5
    close_buffer_minutes: int = 15
    max_market_age_seconds: float = 60.0
    max_market_skew_seconds: float = 2.0
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.root != "SPXW" or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported research scope or schema")
        if (
            any(
                type(v) is not int or v < 0
                for v in (
                    self.max_dte,
                    self.cadence_seconds,
                    self.open_delay_minutes,
                    self.close_buffer_minutes,
                )
            )
            or self.cadence_seconds == 0
        ):
            raise ValueError("invalid research interval")
        if any(
            type(v) not in (int, float) or not math.isfinite(v) or v < 0
            for v in (
                self.max_market_age_seconds,
                self.max_market_skew_seconds,
            )
        ):
            raise ValueError("invalid freshness budget")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> str:
        return digest_of(canonical_payload(self.as_dict()))


@dataclass(frozen=True)
class Observation:
    kind: str
    event_at: datetime
    available_at: datetime
    source_sha256: str

    def __post_init__(self) -> None:
        _aware(self.event_at)
        _aware(self.available_at)
        if self.kind not in REQUIRED_INPUTS:
            raise ValueError("unknown observation kind")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise ValueError("source digest must be SHA-256")


@dataclass(frozen=True)
class ResearchFrame:
    identity: str
    decision_at: datetime
    root: str
    expiration: date
    open_interest: int | None
    oi_as_of: date | None
    observations: tuple[Observation, ...]

    def __post_init__(self) -> None:
        _aware(self.decision_at)
        if not isinstance(self.identity, str) or not self.identity.strip():
            raise ValueError("frame identity required")
        if self.open_interest is not None and (
            type(self.open_interest) is not int or self.open_interest < 0
        ):
            raise ValueError("OI must be a nonnegative integer or unavailable")


def audit_frame(frame: ResearchFrame, contract: ResearchContract) -> dict[str, Any]:
    """Audit declarations only. A matching digest string proves no source bytes."""
    decision = frame.decision_at.astimezone(EASTERN)
    session = decision.date()
    blockers: list[str] = []
    if not is_trading_session(session):
        blockers.append("NOT_TRADING_SESSION")
    else:
        start = decision.replace(
            hour=9, minute=30, second=0, microsecond=0
        ) + timedelta(minutes=contract.open_delay_minutes)
        end = session_close_datetime(session) - timedelta(
            minutes=contract.close_buffer_minutes
        )
        if not start <= decision <= end:
            blockers.append("OUTSIDE_RESEARCH_WINDOW")
        elif (decision.timestamp() - start.timestamp()) % contract.cadence_seconds:
            blockers.append("OFF_DECISION_GRID")
    dte = (frame.expiration - session).days
    if frame.root != contract.root or not 0 <= dte <= contract.max_dte:
        blockers.append("OUTSIDE_FIXED_UNIVERSE")
    if frame.open_interest is None:
        blockers.append("OI_UNAVAILABLE")
    if frame.oi_as_of != previous_session(session):
        blockers.append("OI_NOT_PRIOR_COMPLETED_SESSION")
    counts = Counter(o.kind for o in frame.observations)
    blockers.extend(
        f"MISSING_{kind.upper()}" for kind in sorted(REQUIRED_INPUTS - counts.keys())
    )
    blockers.extend(
        f"DUPLICATE_{kind.upper()}"
        for kind, count in sorted(counts.items())
        if count != 1
    )
    market = []
    for observation in frame.observations:
        kind = observation.kind.upper()
        if observation.event_at.timestamp() > observation.available_at.timestamp():
            blockers.append(f"EVENT_AFTER_AVAILABILITY_{kind}")
        if observation.available_at.timestamp() > decision.timestamp():
            blockers.append(f"NOT_YET_AVAILABLE_{kind}")
        if observation.event_at.timestamp() > decision.timestamp():
            blockers.append(f"FUTURE_EVENT_{kind}")
        if observation.kind in MARKET_INPUTS:
            market.append(observation.event_at.timestamp())
            if (
                decision.timestamp() - observation.event_at.timestamp()
            ) > contract.max_market_age_seconds:
                blockers.append(f"STALE_{kind}")
    if market and max(market) - min(market) > contract.max_market_skew_seconds:
        blockers.append("MARKET_INPUTS_MISALIGNED")
    inputs = sorted(
        (
            {
                "kind": o.kind,
                "event_at": o.event_at.astimezone(UTC).isoformat(),
                "available_at": o.available_at.astimezone(UTC).isoformat(),
                "source_sha256": o.source_sha256,
            }
            for o in frame.observations
        ),
        key=lambda o: (o["kind"], o["event_at"], o["available_at"], o["source_sha256"]),
    )
    payload = {
        "identity": frame.identity,
        "decision_at": decision.isoformat(),
        "root": frame.root,
        "expiration": frame.expiration.isoformat(),
        "open_interest": frame.open_interest,
        "oi_as_of": frame.oi_as_of.isoformat() if frame.oi_as_of else None,
        "observations": inputs,
        "contract_hash": contract.fingerprint(),
        "declared_checks_passed": not blockers,
        "blockers": sorted(set(blockers)),
        "provenance_verified": False,
        "ready_for_backtest": False,
    }
    return {**payload, "frame_audit_hash": digest_of(canonical_payload(payload))}


def audit_document(document: dict[str, Any]) -> dict[str, Any]:
    """Read a strict declaration format; never silently drop unknown fields."""
    if set(document) != {"contract", "frames"} or not isinstance(
        document["frames"], list
    ):
        raise ValueError("expected contract and frame declarations")
    if set(document["contract"]) != set(ResearchContract().as_dict()):
        raise ValueError("contract fields must explicitly describe the whole design")
    contract = ResearchContract(**document["contract"])
    audits = []
    seen = set()
    for raw in document["frames"]:
        if set(raw) != {
            "identity",
            "decision_at",
            "root",
            "expiration",
            "open_interest",
            "oi_as_of",
            "observations",
        }:
            raise ValueError("unexpected frame fields")
        observations = tuple(
            Observation(
                **{
                    **o,
                    "event_at": datetime.fromisoformat(o["event_at"]),
                    "available_at": datetime.fromisoformat(o["available_at"]),
                }
            )
            for o in raw["observations"]
        )
        frame = ResearchFrame(
            **{
                **raw,
                "decision_at": datetime.fromisoformat(raw["decision_at"]),
                "expiration": date.fromisoformat(raw["expiration"]),
                "oi_as_of": date.fromisoformat(raw["oi_as_of"])
                if raw["oi_as_of"]
                else None,
                "observations": observations,
            }
        )
        key = (frame.identity, frame.decision_at.timestamp())
        if key in seen:
            raise ValueError("duplicate frame identity at decision time")
        seen.add(key)
        audits.append(audit_frame(frame, contract))
    audits.sort(key=lambda a: (a["decision_at"], a["identity"]))
    payload = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "contract": contract.as_dict(),
        "frame_count": len(audits),
        "passing_declarations": sum(a["declared_checks_passed"] for a in audits),
        "frames": audits,
        "provenance_verified": False,
        "ready_for_backtest": False,
        "remaining_requirements": [
            "VERIFY_RAW_SOURCE_BYTES",
            "BUILD_INTRADAY_REPLAY",
            "EXECUTABLE_FUTURES_QUOTES_AND_COSTS",
        ],
    }
    return {**payload, "report_hash": digest_of(canonical_payload(payload))}
