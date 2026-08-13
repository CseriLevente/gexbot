"""What happened to each contract's open interest between two captures.

426 contract identities in the first capture had no open-interest row. That is
the one blocker standing between a corrected capture and a trusted aggregate,
and a single capture cannot say anything about it: a missing row could be a
zero the vendor declines to state, a contract too new to have settled, or a gap.
All three look identical in one snapshot.

Two snapshots can distinguish them, because the contracts are the same
contracts. If an identity that was missing on Monday carries a settled figure
on Wednesday, the row was not permanently absent. If every missing identity in
the later capture is one that did not exist in the earlier universe, absence
tracks newness rather than the contract.

**This module observes. It does not decide.** Nothing here concludes that a
missing row means zero, or that a missing row may be dropped. An observed
association between absence and newness is evidence about the vendor; an
imputation rule is a decision about the model, and turning the first into the
second silently is how a research pipeline acquires an assumption nobody
approved. The transition report says what was seen and names the policy
question as open.

No network access. Both captures are certified first, so a comparison never
runs over bytes that failed their own verification.
"""

from __future__ import annotations

import pathlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

from src.adapters.thetadata.capture_certification import (
    CaptureCertificationError,
    CaptureUniverse,
    ContractKey,
    capture_universe,
    certify_capture,
    load_capture,
)
from src.domain.canonical import CANONICAL_REPORT_SCHEMA_VERSION

__all__ = [
    "LONGITUDINAL_OI_SCHEMA_VERSION",
    "OI_TRANSITION_ALGORITHM_VERSION",
    "OpenInterestTransitionReport",
    "TransitionClass",
    "compare_captures",
]

#: Bumped when what a transition report must carry changes.
LONGITUDINAL_OI_SCHEMA_VERSION = "longitudinal-oi/2.1.27"

#: Bumped when the *classification* changes -- when an identity that used to
#: land in one class would now land in another. Separate from the schema
#: because two reports built under different algorithms are not comparable even
#: if they carry the same fields, and the identity hash has to say so.
OI_TRANSITION_ALGORITHM_VERSION = "oi-transition/1"


class TransitionClass(str, Enum):
    """Where one contract identity went between two captures.

    Mutually exclusive and jointly exhaustive over the union of the two
    expected universes. Every identity lands in exactly one, which is what
    makes the accounting checkable: the class counts must sum to the size of
    that union, and :meth:`OpenInterestTransitionReport.accounting_is_exhaustive`
    asserts it rather than assuming it.

    The states a single capture can put an identity in are four -- not in the
    universe, in it with a positive figure, in it with an explicit zero, in it
    with no row -- and the classes below are the pairs of those that can occur.
    """

    # In both universes. The four combinations of answered figures ...
    PRESENT_BOTH_PREVIOUS_OI_ZERO_CURRENT_ZERO = (
        "PRESENT_BOTH_PREVIOUS_OI_ZERO_CURRENT_ZERO"
    )
    PRESENT_BOTH_PREVIOUS_OI_ZERO_CURRENT_POSITIVE = (
        "PRESENT_BOTH_PREVIOUS_OI_ZERO_CURRENT_POSITIVE"
    )
    PRESENT_BOTH_PREVIOUS_OI_POSITIVE_CURRENT_ZERO = (
        "PRESENT_BOTH_PREVIOUS_OI_POSITIVE_CURRENT_ZERO"
    )
    PRESENT_BOTH_PREVIOUS_OI_POSITIVE_CURRENT_POSITIVE = (
        "PRESENT_BOTH_PREVIOUS_OI_POSITIVE_CURRENT_POSITIVE"
    )
    #: ... and the regression: answered before, unanswered now. Added because
    #: the accounting has to have somewhere to put it, and because a row that
    #: disappears is a different fact from one that never arrived.
    PRESENT_BOTH_OI_NOW_MISSING = "PRESENT_BOTH_OI_NOW_MISSING"

    # In both universes, unanswered before.
    PREVIOUSLY_MISSING_OI_NOW_ZERO = "PREVIOUSLY_MISSING_OI_NOW_ZERO"
    PREVIOUSLY_MISSING_OI_NOW_POSITIVE = "PREVIOUSLY_MISSING_OI_NOW_POSITIVE"
    PREVIOUSLY_MISSING_OI_STILL_MISSING = "PREVIOUSLY_MISSING_OI_STILL_MISSING"

    # Only in the later universe.
    NEW_CONTRACT_OI_PRESENT_ZERO = "NEW_CONTRACT_OI_PRESENT_ZERO"
    NEW_CONTRACT_OI_PRESENT_POSITIVE = "NEW_CONTRACT_OI_PRESENT_POSITIVE"
    NEW_CONTRACT_OI_MISSING = "NEW_CONTRACT_OI_MISSING"

    #: Only in the earlier universe, split by what it had carried. Split
    #: because "a contract with open interest expired" and "a contract we never
    #: got a figure for expired" answer different questions, and the second is
    #: the one that decides whether a missing identity was ever resolvable.
    REMOVED_OR_EXPIRED_PREVIOUS_OI_POSITIVE = "REMOVED_OR_EXPIRED_PREVIOUS_OI_POSITIVE"
    REMOVED_OR_EXPIRED_PREVIOUS_OI_ZERO = "REMOVED_OR_EXPIRED_PREVIOUS_OI_ZERO"
    REMOVED_OR_EXPIRED_PREVIOUS_OI_MISSING = "REMOVED_OR_EXPIRED_PREVIOUS_OI_MISSING"


#: ``(earlier state, later state)`` -> class. A total mapping over the sixteen
#: state pairs, written out rather than computed, so a missing combination is a
#: ``KeyError`` at the point of classification instead of a silent default.
#: ``(NOT_IN_UNIVERSE, NOT_IN_UNIVERSE)`` cannot occur -- the union is built
#: from the two universes -- and is absent for that reason.
_CLASSIFICATION: dict[tuple[str, str], TransitionClass] = {
    ("OI_ZERO", "OI_ZERO"): TransitionClass.PRESENT_BOTH_PREVIOUS_OI_ZERO_CURRENT_ZERO,
    ("OI_ZERO", "OI_POSITIVE"): (
        TransitionClass.PRESENT_BOTH_PREVIOUS_OI_ZERO_CURRENT_POSITIVE
    ),
    ("OI_POSITIVE", "OI_ZERO"): (
        TransitionClass.PRESENT_BOTH_PREVIOUS_OI_POSITIVE_CURRENT_ZERO
    ),
    ("OI_POSITIVE", "OI_POSITIVE"): (
        TransitionClass.PRESENT_BOTH_PREVIOUS_OI_POSITIVE_CURRENT_POSITIVE
    ),
    ("OI_ZERO", "OI_MISSING"): TransitionClass.PRESENT_BOTH_OI_NOW_MISSING,
    ("OI_POSITIVE", "OI_MISSING"): TransitionClass.PRESENT_BOTH_OI_NOW_MISSING,
    ("OI_MISSING", "OI_ZERO"): TransitionClass.PREVIOUSLY_MISSING_OI_NOW_ZERO,
    ("OI_MISSING", "OI_POSITIVE"): TransitionClass.PREVIOUSLY_MISSING_OI_NOW_POSITIVE,
    ("OI_MISSING", "OI_MISSING"): TransitionClass.PREVIOUSLY_MISSING_OI_STILL_MISSING,
    ("NOT_IN_UNIVERSE", "OI_ZERO"): TransitionClass.NEW_CONTRACT_OI_PRESENT_ZERO,
    ("NOT_IN_UNIVERSE", "OI_POSITIVE"): (
        TransitionClass.NEW_CONTRACT_OI_PRESENT_POSITIVE
    ),
    ("NOT_IN_UNIVERSE", "OI_MISSING"): TransitionClass.NEW_CONTRACT_OI_MISSING,
    ("OI_POSITIVE", "NOT_IN_UNIVERSE"): (
        TransitionClass.REMOVED_OR_EXPIRED_PREVIOUS_OI_POSITIVE
    ),
    ("OI_ZERO", "NOT_IN_UNIVERSE"): TransitionClass.REMOVED_OR_EXPIRED_PREVIOUS_OI_ZERO,
    ("OI_MISSING", "NOT_IN_UNIVERSE"): (
        TransitionClass.REMOVED_OR_EXPIRED_PREVIOUS_OI_MISSING
    ),
}

#: Rollups, published beside the leaves. Named groupings rather than a second
#: classification: every rollup is a union of leaves and the leaves stay the
#: authority, so a reader can always get back to the exact accounting.
_ROLLUPS: dict[str, tuple[TransitionClass, ...]] = {
    "PRESENT_BOTH_OI_PRESENT_BOTH": (
        TransitionClass.PRESENT_BOTH_PREVIOUS_OI_ZERO_CURRENT_ZERO,
        TransitionClass.PRESENT_BOTH_PREVIOUS_OI_ZERO_CURRENT_POSITIVE,
        TransitionClass.PRESENT_BOTH_PREVIOUS_OI_POSITIVE_CURRENT_ZERO,
        TransitionClass.PRESENT_BOTH_PREVIOUS_OI_POSITIVE_CURRENT_POSITIVE,
    ),
    "PREVIOUSLY_MISSING_SURVIVING": (
        TransitionClass.PREVIOUSLY_MISSING_OI_NOW_ZERO,
        TransitionClass.PREVIOUSLY_MISSING_OI_NOW_POSITIVE,
        TransitionClass.PREVIOUSLY_MISSING_OI_STILL_MISSING,
    ),
    "PREVIOUSLY_MISSING_RESOLVED": (
        TransitionClass.PREVIOUSLY_MISSING_OI_NOW_ZERO,
        TransitionClass.PREVIOUSLY_MISSING_OI_NOW_POSITIVE,
    ),
    "NEW_CONTRACTS": (
        TransitionClass.NEW_CONTRACT_OI_PRESENT_ZERO,
        TransitionClass.NEW_CONTRACT_OI_PRESENT_POSITIVE,
        TransitionClass.NEW_CONTRACT_OI_MISSING,
    ),
    "NEW_CONTRACTS_WITH_OI_ROW": (
        TransitionClass.NEW_CONTRACT_OI_PRESENT_ZERO,
        TransitionClass.NEW_CONTRACT_OI_PRESENT_POSITIVE,
    ),
    "REMOVED_OR_EXPIRED": (
        TransitionClass.REMOVED_OR_EXPIRED_PREVIOUS_OI_POSITIVE,
        TransitionClass.REMOVED_OR_EXPIRED_PREVIOUS_OI_ZERO,
        TransitionClass.REMOVED_OR_EXPIRED_PREVIOUS_OI_MISSING,
    ),
}


class CaptureComparisonError(ValueError):
    """Two captures that cannot be compared without inventing a shared scope."""


@dataclass(frozen=True, slots=True)
class ExpirationTransitions:
    """One expiration's contribution, in the later capture's terms."""

    expiration: str
    expected_contracts: int
    new_contracts: int
    existing_contracts: int
    oi_present: int
    oi_explicit_zero: int
    oi_positive: int
    oi_missing: int
    class_counts: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "expiration": self.expiration,
            "expected_contracts": self.expected_contracts,
            "new_contracts": self.new_contracts,
            "existing_contracts": self.existing_contracts,
            "oi_present": self.oi_present,
            "oi_explicit_zero": self.oi_explicit_zero,
            "oi_positive": self.oi_positive,
            "oi_missing": self.oi_missing,
            "transition_counts": dict(sorted(self.class_counts.items())),
        }


@dataclass(frozen=True, slots=True)
class OpenInterestTransitionReport:
    """What two captures say about open-interest availability. Observation only."""

    schema_version: str
    algorithm_version: str
    earlier: CaptureUniverse
    later: CaptureUniverse
    session_distance_days: int | None
    #: identity -> class, for every identity in the union of both universes.
    classified: dict[ContractKey, TransitionClass]
    per_expiration: tuple[ExpirationTransitions, ...]
    scope_differences: tuple[str, ...]

    @property
    def class_counts(self) -> dict[str, int]:
        counts = dict.fromkeys((c.value for c in TransitionClass), 0)
        for outcome in self.classified.values():
            counts[outcome.value] += 1
        return counts

    @property
    def rollups(self) -> dict[str, int]:
        counts = self.class_counts
        return {
            name: sum(counts[member.value] for member in members)
            for name, members in sorted(_ROLLUPS.items())
        }

    def identities_in(self, outcome: TransitionClass) -> tuple[ContractKey, ...]:
        return tuple(
            sorted(key for key, value in self.classified.items() if value is outcome)
        )

    @property
    def class_hashes(self) -> dict[str, str]:
        """A set hash per class, so a later reader can check membership.

        Counts alone would let two different sets of 426 identities look
        identical, which is the mistake set hashing exists to prevent.
        """
        from src.adapters.thetadata.capture_certification import _set_hash

        return {
            outcome.value: _set_hash(set(self.identities_in(outcome)))
            for outcome in TransitionClass
        }

    @property
    def accounting_is_exhaustive(self) -> bool:
        """Every identity in either universe was classified exactly once."""
        union = set(self.earlier.expected) | set(self.later.expected)
        return len(self.classified) == len(union) and set(self.classified) == union

    @property
    def findings(self) -> tuple[str, ...]:
        """What the two captures show, in sentences that stay observations.

        Each one is a statement about these two captures. None of them is a
        rule, and the wording is deliberately about what *was seen* rather than
        about what may be assumed -- see :attr:`policy_status`.
        """
        counts = self.class_counts
        rolled = self.rollups
        said: list[str] = []
        earlier_missing = len(self.earlier.missing)
        surviving = rolled["PREVIOUSLY_MISSING_SURVIVING"]
        resolved = rolled["PREVIOUSLY_MISSING_RESOLVED"]
        if earlier_missing:
            said.append(
                f"{earlier_missing} identities had no open-interest row in "
                f"{self.earlier.session_id}. {surviving} were still in the "
                f"later expected universe and "
                f"{counts['REMOVED_OR_EXPIRED_PREVIOUS_OI_MISSING']} were not."
            )
        if surviving:
            said.append(
                f"Of those {surviving} survivors, {resolved} carried an "
                f"open-interest row in {self.later.session_id} "
                f"({counts['PREVIOUSLY_MISSING_OI_NOW_ZERO']} explicit zero, "
                f"{counts['PREVIOUSLY_MISSING_OI_NOW_POSITIVE']} positive) and "
                f"{counts['PREVIOUSLY_MISSING_OI_STILL_MISSING']} did not."
            )
        later_missing = len(self.later.missing)
        if later_missing:
            new_missing = counts["NEW_CONTRACT_OI_MISSING"]
            said.append(
                f"{later_missing} identities have no open-interest row in "
                f"{self.later.session_id}. {new_missing} of them are absent "
                f"from the earlier expected universe; "
                f"{counts['PRESENT_BOTH_OI_NOW_MISSING']} were answered "
                f"earlier and are unanswered now; "
                f"{counts['PREVIOUSLY_MISSING_OI_STILL_MISSING']} were "
                "unanswered in both."
            )
        said.append(
            f"{rolled['NEW_CONTRACTS']} identities are new relative to the "
            f"earlier universe, of which {rolled['NEW_CONTRACTS_WITH_OI_ROW']} "
            "already carry an open-interest row."
        )
        return tuple(said)

    @property
    def policy_status(self) -> tuple[str, ...]:
        """The distinction this module exists to keep.

        An association between missing rows and new contracts is evidence. It
        is not a rule that missing means zero, and it is not permission to drop
        the contract. Both would change every aggregate that consumed them, and
        neither follows from two observations.
        """
        return (
            "OI_SEMANTICS_LONGITUDINAL_EVIDENCE_AVAILABLE",
            "OI_IMPUTATION_POLICY_UNRESOLVED",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "algorithm_version": self.algorithm_version,
            "earlier_capture": self.earlier.as_dict(),
            "later_capture": self.later.as_dict(),
            "session_distance_days": self.session_distance_days,
            "scope_differences": list(self.scope_differences),
            "transition_counts": dict(sorted(self.class_counts.items())),
            "transition_rollups": self.rollups,
            "transition_identity_hashes": dict(sorted(self.class_hashes.items())),
            "accounting_is_exhaustive": self.accounting_is_exhaustive,
            "classified_identity_count": len(self.classified),
            "union_universe_count": len(
                set(self.earlier.expected) | set(self.later.expected)
            ),
            "per_expiration": [row.as_dict() for row in self.per_expiration],
            "longitudinal_findings": list(self.findings),
            "analytical_evidence_status": list(self.policy_status),
            "imputation_policy": (
                "NONE. This report records what was observed between two "
                "captures. It does not establish that a missing open-interest "
                "row means zero, and it does not authorise dropping the "
                "contract. Either would be an analytical decision that these "
                "observations do not make."
            ),
        }

    def canonical_payload(self) -> dict[str, Any]:
        from src.domain.canonical import canonical_payload

        return {
            "canonical_schema_version": CANONICAL_REPORT_SCHEMA_VERSION,
            **canonical_payload(self.as_dict()),
        }

    def transition_report_hash(self) -> str:
        """Content identity for the comparison itself.

        Covers both manifest hashes, both session ids, the algorithm version
        and the canonical rendering -- so a hash cannot be reproduced by a
        different pair of captures, nor by the same pair under a classification
        that has since changed.
        """
        from src.domain.digests import digest_of

        return digest_of(self.canonical_payload())


def _session_distance(earlier: CaptureUniverse, later: CaptureUniverse) -> int | None:
    try:
        return (
            date.fromisoformat(later.session_date)
            - date.fromisoformat(earlier.session_date)
        ).days
    except ValueError:
        return None


def _scope_check(earlier: CaptureUniverse, later: CaptureUniverse) -> tuple[str, ...]:
    """Refuse an incomparable pair; report a merely different one.

    The distinction matters. Two captures of different symbols are not a
    longitudinal series and comparing them would produce a transition table
    where every identity is "new" or "removed" -- arithmetically valid and
    meaningless. Two captures whose ``max_dte`` differs *are* comparable, and
    the difference explains part of the universe change, so it is reported
    rather than refused.
    """
    if earlier.symbol != later.symbol:
        raise CaptureComparisonError(
            f"the earlier capture is {earlier.symbol!r} and the later is "
            f"{later.symbol!r}. Different underlyings do not form a series, "
            "and every identity would classify as new or removed."
        )
    if earlier.manifest_hash == later.manifest_hash:
        raise CaptureComparisonError(
            "both captures have the same manifest hash, so this is one capture "
            "compared with itself. That yields a transition table of zeros and "
            "answers nothing about open interest over time."
        )
    distance = _session_distance(earlier, later)
    if distance is not None and distance <= 0:
        raise CaptureComparisonError(
            f"the earlier capture's contract-list session is "
            f"{earlier.session_date} and the later one's is "
            f"{later.session_date}. A transition needs the earlier capture to "
            "be earlier; passing them the wrong way round would report every "
            "resolution as a regression."
        )
    differences: list[str] = []
    if earlier.max_dte != later.max_dte:
        differences.append(
            f"max_dte differs: {earlier.max_dte} then {later.max_dte}. Part of "
            "the universe change is scope rather than the market."
        )
    return tuple(differences)


def compare_captures(
    earlier_root: pathlib.Path | str,
    later_root: pathlib.Path | str,
    *,
    earlier_archive: pathlib.Path | str | None = None,
    later_archive: pathlib.Path | str | None = None,
) -> OpenInterestTransitionReport:
    """Compare two captures' open-interest coverage. Offline, deterministic.

    Both captures are certified first. A comparison over a capture that cannot
    certify would be reading numbers out of bytes nothing verified, and the
    resulting transition table would look exactly as authoritative as a real
    one.
    """
    earlier_path, later_path = pathlib.Path(earlier_root), pathlib.Path(later_root)
    for label, root, archive in (
        ("earlier", earlier_path, earlier_archive),
        ("later", later_path, later_archive),
    ):
        try:
            certify_capture(
                root,
                archive_path=pathlib.Path(archive) if archive else None,
            )
        except CaptureCertificationError as error:
            raise CaptureComparisonError(
                f"the {label} capture at {root} does not certify: {error}"
            ) from error

    earlier = capture_universe(load_capture(earlier_path))
    later = capture_universe(load_capture(later_path))
    differences = _scope_check(earlier, later)

    classified: dict[ContractKey, TransitionClass] = {}
    for key in set(earlier.expected) | set(later.expected):
        classified[key] = _CLASSIFICATION[(earlier.state_of(key), later.state_of(key))]

    return OpenInterestTransitionReport(
        schema_version=LONGITUDINAL_OI_SCHEMA_VERSION,
        algorithm_version=OI_TRANSITION_ALGORITHM_VERSION,
        earlier=earlier,
        later=later,
        session_distance_days=_session_distance(earlier, later),
        classified=classified,
        per_expiration=_per_expiration(earlier, later, classified),
        scope_differences=differences,
    )


def _per_expiration(
    earlier: CaptureUniverse,
    later: CaptureUniverse,
    classified: dict[ContractKey, TransitionClass],
) -> tuple[ExpirationTransitions, ...]:
    """Group the later capture's universe by expiration.

    Grouped by the *later* universe because the question this answers is
    whether unanswered open interest clusters on particular expirations --
    notably newly introduced ones. Identities that only existed earlier are
    counted in the transition classes and have no row here; they belong to
    expirations the later capture no longer lists.
    """
    grouped: dict[str, list[ContractKey]] = defaultdict(list)
    for key in later.expected:
        grouped[key.expiration.isoformat()].append(key)

    rows: list[ExpirationTransitions] = []
    for expiration in sorted(grouped):
        keys = grouped[expiration]
        counts: dict[str, int] = defaultdict(int)
        new = existing = zero = positive = missing = 0
        for key in keys:
            counts[classified[key].value] += 1
            if key in earlier.expected:
                existing += 1
            else:
                new += 1
            value = later.answered.get(key)
            if value is None:
                missing += 1
            elif value == 0:
                zero += 1
            else:
                positive += 1
        rows.append(
            ExpirationTransitions(
                expiration=expiration,
                expected_contracts=len(keys),
                new_contracts=new,
                existing_contracts=existing,
                oi_present=zero + positive,
                oi_explicit_zero=zero,
                oi_positive=positive,
                oi_missing=missing,
                class_counts=dict(counts),
            )
        )
    return tuple(rows)
