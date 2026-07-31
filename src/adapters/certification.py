"""Whether a paid ThetaData session would produce evidence worth having.

**This is not a trading readiness check.** Nothing in this repository can place
an order, and nothing here changes that. The question is narrower and entirely
about data provenance: if we spend one session capturing real vendor responses,
will anybody be able to reconstruct afterwards what those numbers meant?

Two vendor-dependent unknowns are handled explicitly rather than guessed:

**Open interest as-of.** ThetaData's snapshot endpoints do not state which
settlement date their open interest belongs to. v2.1.1 accepted a caller-supplied
date and stored it in the same field as an observed one, so the snapshot could
not distinguish "the vendor said 16 March" from "we assumed 16 March". Open
interest is the weight on every GEX term; a date we chose is not evidence about
the date.

**Synchronised spot.** The spot print and the option chain are separate reads.
If they are minutes apart then every gamma is computed against an underlying the
chain never saw. Nothing in v2.1.1 required the two clocks to be close, or even
recorded how far apart they were.

Both block certification when unverified. Neither is silently resolved, because
a live capture is exactly the evidence that would resolve them -- and a capture
taken without recording which was which cannot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from src.config.pipeline import load_bearing_unknowns

__all__ = [
    "AdapterCertificationReadiness",
    "CertificationState",
    "OpenInterestProvenance",
    "SpotProvenance",
    "assess_readiness",
]

#: Stamped onto every readiness report so the object cannot be quoted out of
#: context as clearance for anything else.
CERTIFICATION_SCOPE = (
    "Adapter data-capture readiness only. This is NOT a trading readiness "
    "check: this repository has no broker, no order type and no execution "
    "path, and readiness here confers none."
)


@dataclass(frozen=True, slots=True)
class OpenInterestProvenance:
    """Where the open-interest settlement date came from."""

    as_of: date | None
    #: ``vendor_field`` when the payload stated it; ``caller`` when a human did.
    source: str
    #: True when a human supplied the date. Accepted, but never described as
    #: observed, and always surfaced as an unverified field.
    caller_supplied: bool = True

    @property
    def is_verified(self) -> bool:
        return self.as_of is not None and not self.caller_supplied and bool(self.source)

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "source": self.source,
            "caller_supplied": self.caller_supplied,
            "is_verified": self.is_verified,
        }


@dataclass(frozen=True, slots=True)
class SpotProvenance:
    """Which underlying print was used, when it was taken, and how close it was."""

    source: str
    timestamp: datetime | None
    #: How far the spot print may be from the chain instant before the pairing
    #: stops being meaningful. A local policy, not a vendor fact.
    tolerance_seconds: float = 1.0

    def skew_seconds(self, as_of: datetime) -> float | None:
        if self.timestamp is None:
            return None
        return abs((as_of - self.timestamp).total_seconds())

    def as_dict(self, as_of: datetime | None = None) -> dict[str, Any]:
        return {
            "source": self.source,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "tolerance_seconds": self.tolerance_seconds,
            "skew_seconds": self.skew_seconds(as_of) if as_of else None,
        }


class CertificationState(str, Enum):
    """How far certification has actually got.

    v2.1.2 had one boolean, which could only say "ready" -- and readiness for a
    *capture* is not the same claim as a *certified adapter*. The distinction
    matters because only one of them can be reached without spending money, and
    the boolean invited reading the cheap one as the expensive one.
    """

    NOT_READY = "NOT_READY"
    #: Offline checks pass. A capture may proceed. Nothing about vendor
    #: behaviour has been confirmed, because nothing has been fetched.
    READY_FOR_CAPTURE_ONLY = "READY_FOR_CAPTURE_ONLY"
    #: Bytes exist and are linked to a snapshot; nobody has checked them yet.
    CAPTURE_COMPLETED_NOT_VALIDATED = "CAPTURE_COMPLETED_NOT_VALIDATED"
    #: A live capture happened AND a validation report exists. Unreachable in
    #: this release by construction -- see ``assess_readiness``.
    ADAPTER_CERTIFIED = "ADAPTER_CERTIFIED"


@dataclass(frozen=True, slots=True)
class AdapterCertificationReadiness:
    """Machine-readable answer to "may we spend a session on this?"."""

    state: CertificationState = CertificationState.NOT_READY
    #: Convenience for "no blockers". Kept as a field rather than a property so
    #: the serialised report reads the same as the object.
    ready: bool = False
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    verified_fields: tuple[str, ...] = ()
    unverified_fields: tuple[str, ...] = ()
    scope: str = CERTIFICATION_SCOPE
    #: Always False. Present so that a reader of the serialised report does not
    #: have to infer it from the absence of a field.
    trading_enabled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "ready": self.ready,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "verified_fields": list(self.verified_fields),
            "unverified_fields": list(self.unverified_fields),
            "scope": self.scope,
            "trading_enabled": self.trading_enabled,
        }


def assess_readiness(
    *,
    pipeline: Any,
    as_of: datetime,
    open_interest: OpenInterestProvenance | None = None,
    spot: SpotProvenance | None = None,
    raw_store: Any = None,
    capture_manifest: Any = None,
    validation_report: Any = None,
) -> AdapterCertificationReadiness:
    """Evaluate every blocker. Deterministic, sorted, and never cached."""
    blockers: list[str] = []
    warnings: list[str] = []
    verified: list[str] = []
    unverified: list[str] = []

    # -- pricing coherence ---------------------------------------------------
    #
    # Inspects the pipeline's *effective* compatibility report rather than
    # trusting the pricing-mode enum. v2.1.2 could be told LOCAL_IV_LOCAL_GAMMA
    # and would then skip this entirely -- which is how a vendor-IV session
    # reported ready with every vendor convention unknown.
    report = pipeline.pricing_compatibility
    blocking_unknowns = load_bearing_unknowns(report)

    if report.incompatible_fields:
        blockers.append(
            "pricing assumptions are incompatible for "
            f"{pipeline.pricing_mode.value}: {list(report.incompatible_fields)}. "
            "Capturing under these settings produces numbers whose meaning "
            "cannot be stated."
        )
        unverified.append("pricing_compatibility")
    elif blocking_unknowns:
        # An unknown that changes gamma is not a caveat to note beside the
        # result; it is the reason the result has no stated meaning.
        blockers.append(
            f"{len(blocking_unknowns)} load-bearing pricing assumption(s) are "
            f"UNKNOWN for {pipeline.pricing_mode.value}: "
            f"{[f.split(':')[0] for f in blocking_unknowns]}. Each changes the "
            "gamma, so none may be left unresolved while claiming the vendor "
            "and local models agree."
        )
        unverified.append("pricing_compatibility")
    elif not report.compatible:
        warnings.append(
            "pricing compatibility is not fully established, but every "
            "unresolved field is non-load-bearing for "
            f"{pipeline.pricing_mode.value}."
        )
        unverified.append("pricing_compatibility")
    else:
        verified.append("pricing_compatibility")

    # -- the subscription actually exposes what the mode needs ---------------
    capability = getattr(pipeline, "subscription_capability", None)
    if capability is not None and not capability.satisfied:
        blockers.append(
            f"subscription tier {capability.tier.value} does not expose "
            f"missing={list(capability.missing)} "
            f"uncertain={list(capability.uncertain)}"
        )
        unverified.append("subscription_capability")
    elif capability is not None:
        verified.append("subscription_capability")

    # -- credentials ---------------------------------------------------------
    try:
        pipeline.config.resolved_credentials()
    except Exception as exc:
        blockers.append(f"credentials are not available: {exc}")
        unverified.append("credentials")
    else:
        verified.append("credentials")

    # -- open-interest provenance -------------------------------------------
    if open_interest is None or open_interest.as_of is None:
        blockers.append(
            "open_interest provenance is missing: no settlement date and no "
            "source. Open interest is the weight on every GEX term, so a "
            "capture without it cannot be interpreted later."
        )
        unverified.append("open_interest_as_of")
    elif open_interest.is_verified:
        verified.append("open_interest_as_of")
    else:
        # Usable, but the date is ours rather than the vendor's, and the report
        # must not let that distinction quietly disappear.
        warnings.append(
            f"open_interest_as_of={open_interest.as_of.isoformat()} was "
            f"caller-supplied (source={open_interest.source!r}), not observed "
            "from the vendor payload. Record this alongside the capture; a "
            "later vendor-verified source can replace it."
        )
        unverified.append("open_interest_as_of")

    # -- synchronised spot ---------------------------------------------------
    if spot is None:
        blockers.append(
            "spot provenance is missing: no source and no timestamp. Every "
            "gamma is computed against this print."
        )
        unverified.append("spot_source")
    elif not spot.source:
        blockers.append("spot source is unnamed; the selected spot must be documented")
        unverified.append("spot_source")
    elif spot.timestamp is None:
        blockers.append(
            "spot timestamp is missing, so the spot cannot be shown to be "
            "synchronised with the chain"
        )
        unverified.append("spot_timestamp")
    else:
        skew = spot.skew_seconds(as_of)
        if skew is not None and skew > spot.tolerance_seconds:
            blockers.append(
                f"spot skew {skew:.3f}s exceeds the configured tolerance "
                f"{spot.tolerance_seconds:.3f}s; the chain and the underlying "
                "describe different moments"
            )
            unverified.append("spot_timestamp")
        else:
            verified.extend(("spot_source", "spot_timestamp"))

    # -- the audit trail itself ---------------------------------------------
    if raw_store is not None and hasattr(raw_store, "verify_integrity"):
        integrity = raw_store.verify_integrity()
        if not integrity.ok:
            blockers.append(
                f"raw store is not clean before capture: {integrity.counts()}. "
                "Starting a paid session on top of an inconsistent audit trail "
                "makes the new evidence hard to separate from the old."
            )
            unverified.append("raw_store_integrity")
        else:
            verified.append("raw_store_integrity")

    # -- known and accepted limitations -------------------------------------
    warnings.append(
        "chain completeness will be PARTIALLY_OBSERVED: no verified ThetaData "
        "contract-list endpoint is wired, so the captured chain cannot be "
        "measured against an independent universe. This is a reason to capture, "
        "not a reason to refuse -- the session is how the endpoint gets "
        "identified. See docs/OPEN_DECISIONS.md OD-11."
    )
    unverified.append("chain_completeness")

    # A live capture has to have happened, AND somebody has to have checked
    # it, before "certified" means anything. Neither is possible offline, so
    # ADAPTER_CERTIFIED is unreachable from here by construction rather than by
    # policy -- there is no argument combination that produces it.
    if blockers:
        state = CertificationState.NOT_READY
    elif capture_manifest is not None and validation_report is not None:
        state = CertificationState.ADAPTER_CERTIFIED
    elif capture_manifest is not None:
        state = CertificationState.CAPTURE_COMPLETED_NOT_VALIDATED
    else:
        state = CertificationState.READY_FOR_CAPTURE_ONLY

    return AdapterCertificationReadiness(
        state=state,
        ready=not blockers,
        blockers=tuple(sorted(blockers)),
        warnings=tuple(sorted(warnings)),
        verified_fields=tuple(sorted(set(verified))),
        unverified_fields=tuple(sorted(set(unverified))),
    )
