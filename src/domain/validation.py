"""Machine-readable validation results.

Two design rules drive this module:

1. **A malformed contract must never contaminate an aggregate.** A single NaN
   gamma summed into a chain total produces a NaN total, and a NaN total that
   reaches a chart looks like a rendering bug rather than a data bug. Rejection
   happens at the contract boundary, before any arithmetic.

2. **Rejection must be countable, not just loggable.** Every rejection carries a
   machine-readable :class:`ValidationCode`, so ``chain_completeness`` can say
   *why* 40 contracts went missing rather than only that they did.

The three-way status (accepted / accepted-with-warning / rejected) exists because
the middle case is real and common: a zero-bid deep-wing option has a usable
gamma but an untrustworthy IV, and silently treating it as either fully good or
fully bad loses information.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class ValidationStatus(str, Enum):
    ACCEPTED = "accepted"
    ACCEPTED_WITH_WARNING = "accepted_with_warning"
    REJECTED = "rejected"

    @property
    def is_usable(self) -> bool:
        return self is not ValidationStatus.REJECTED


class Severity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


class ValidationCode(str, Enum):
    """Stable identifiers. Renaming one breaks stored audit trails."""

    # Numeric hygiene
    NOT_FINITE = "not_finite"
    NEGATIVE_OPEN_INTEREST = "negative_open_interest"
    NEGATIVE_BID = "negative_bid"
    NEGATIVE_ASK = "negative_ask"
    CROSSED_MARKET = "crossed_market"
    LOCKED_MARKET = "locked_market"
    ZERO_BID = "zero_bid"
    NON_POSITIVE_SPOT = "non_positive_spot"
    INVALID_STRIKE = "invalid_strike"
    INVALID_MULTIPLIER = "invalid_multiplier"
    NON_POSITIVE_IMPLIED_VOL = "non_positive_implied_vol"
    IMPLIED_VOL_OUT_OF_RANGE = "implied_vol_out_of_range"
    NEGATIVE_GAMMA = "negative_gamma"
    GAMMA_OUT_OF_RANGE = "gamma_out_of_range"
    EXTREME_IV_SPREAD = "extreme_iv_spread"
    NO_GAMMA_SOURCE = "no_gamma_source"
    MISSING_OPEN_INTEREST = "missing_open_interest"

    # Structure
    INVALID_DTE = "invalid_dte"
    EXPIRED_CONTRACT = "expired_contract"
    INVALID_EXPIRATION = "invalid_expiration"
    INVALID_OPTION_RIGHT = "invalid_option_right"
    DUPLICATE_CONTRACT = "duplicate_contract"
    UNKNOWN_ROOT = "unknown_root"

    # Time
    NAIVE_TIMESTAMP = "naive_timestamp"
    FUTURE_TIMESTAMP = "future_timestamp"
    STALE_SNAPSHOT = "stale_snapshot"
    TIMESTAMP_SKEW = "timestamp_skew"
    MISSING_TIMESTAMP = "missing_timestamp"
    STALE_OPEN_INTEREST = "stale_open_interest"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: ValidationCode
    field: str
    detail: str
    severity: Severity = Severity.ERROR
    # The offending value, kept for the audit trail. Rendered as a string because
    # the original may be NaN or an infinity, neither of which survives JSON.
    observed: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "field": self.field,
            "detail": self.detail,
            "severity": self.severity.value,
            "observed": self.observed,
        }


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of validating one record."""

    issues: tuple[ValidationIssue, ...] = ()

    @property
    def status(self) -> ValidationStatus:
        if any(issue.severity is Severity.ERROR for issue in self.issues):
            return ValidationStatus.REJECTED
        if self.issues:
            return ValidationStatus.ACCEPTED_WITH_WARNING
        return ValidationStatus.ACCEPTED

    @property
    def is_usable(self) -> bool:
        return self.status.is_usable

    @property
    def rejection_codes(self) -> tuple[ValidationCode, ...]:
        return tuple(
            issue.code for issue in self.issues if issue.severity is Severity.ERROR
        )

    @property
    def warning_codes(self) -> tuple[ValidationCode, ...]:
        return tuple(
            issue.code for issue in self.issues if issue.severity is Severity.WARNING
        )

    def with_issue(self, issue: ValidationIssue) -> ValidationResult:
        return ValidationResult(issues=(*self.issues, issue))

    def merge(self, other: ValidationResult) -> ValidationResult:
        return ValidationResult(issues=(*self.issues, *other.issues))

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "issues": [issue.as_dict() for issue in self.issues],
        }


ACCEPTED = ValidationResult()


@dataclass(slots=True)
class ValidationReport:
    """Aggregated outcome across a whole chain.

    Counters rather than a list of every issue: an SPX chain can produce tens of
    thousands of records, and what a confidence component needs is "how many, of
    which kind", not a transcript.
    """

    accepted: int = 0
    accepted_with_warning: int = 0
    rejected: int = 0
    error_counts: Counter[ValidationCode] = field(default_factory=Counter)
    warning_counts: Counter[ValidationCode] = field(default_factory=Counter)
    # A bounded sample kept for human debugging. Bounded on purpose -- an
    # unbounded example list is a memory leak on a bad feed day.
    examples: list[ValidationIssue] = field(default_factory=list)
    max_examples: int = 25

    def record(self, result: ValidationResult) -> ValidationResult:
        status = result.status
        if status is ValidationStatus.ACCEPTED:
            self.accepted += 1
        elif status is ValidationStatus.ACCEPTED_WITH_WARNING:
            self.accepted_with_warning += 1
        else:
            self.rejected += 1
        for issue in result.issues:
            if issue.severity is Severity.ERROR:
                self.error_counts[issue.code] += 1
            else:
                self.warning_counts[issue.code] += 1
            if len(self.examples) < self.max_examples:
                self.examples.append(issue)
        return result

    @property
    def total(self) -> int:
        return self.accepted + self.accepted_with_warning + self.rejected

    @property
    def usable(self) -> int:
        return self.accepted + self.accepted_with_warning

    @property
    def acceptance_ratio(self) -> float:
        return self.usable / self.total if self.total else 0.0

    def count(self, code: ValidationCode) -> int:
        return self.error_counts[code] + self.warning_counts[code]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "accepted": self.accepted,
            "accepted_with_warning": self.accepted_with_warning,
            "rejected": self.rejected,
            "acceptance_ratio": round(self.acceptance_ratio, 6),
            "error_counts": {
                code.value: count for code, count in sorted(self.error_counts.items())
            },
            "warning_counts": {
                code.value: count for code, count in sorted(self.warning_counts.items())
            },
            "examples": [issue.as_dict() for issue in self.examples],
        }


# --- Primitive checks -------------------------------------------------------


def check_finite(
    value: float | None,
    *,
    field_name: str,
    severity: Severity = Severity.ERROR,
) -> ValidationIssue | None:
    """The check that has to happen before every other numeric check.

    ``None`` is a legitimate "not supplied" and is not an error here -- absence is
    handled by the caller that knows whether the field is required. NaN and the
    infinities are always errors, because they propagate silently through every
    downstream sum and comparison.
    """
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        return ValidationIssue(
            code=ValidationCode.NOT_FINITE,
            field=field_name,
            detail=f"expected a real number, got {type(value).__name__}",
            severity=severity,
            observed=repr(value),
        )
    if not math.isfinite(value):
        return ValidationIssue(
            code=ValidationCode.NOT_FINITE,
            field=field_name,
            detail="value is NaN or infinite",
            severity=severity,
            observed=repr(value),
        )
    return None


def check_non_negative(
    value: float | None,
    *,
    field_name: str,
    code: ValidationCode,
    severity: Severity = Severity.ERROR,
) -> ValidationIssue | None:
    if value is None:
        return None
    if value < 0.0:
        return ValidationIssue(
            code=code,
            field=field_name,
            detail="value must not be negative",
            severity=severity,
            observed=repr(value),
        )
    return None


def check_positive(
    value: float | None,
    *,
    field_name: str,
    code: ValidationCode,
    severity: Severity = Severity.ERROR,
) -> ValidationIssue | None:
    if value is None:
        return None
    if value <= 0.0:
        return ValidationIssue(
            code=code,
            field=field_name,
            detail="value must be strictly positive",
            severity=severity,
            observed=repr(value),
        )
    return None


def check_in_range(
    value: float | None,
    *,
    field_name: str,
    code: ValidationCode,
    low: float,
    high: float,
    severity: Severity = Severity.ERROR,
) -> ValidationIssue | None:
    if value is None:
        return None
    if not (low <= value <= high):
        return ValidationIssue(
            code=code,
            field=field_name,
            detail=f"value outside [{low}, {high}]",
            severity=severity,
            observed=repr(value),
        )
    return None


def check_timezone_aware(
    value: datetime | None,
    *,
    field_name: str,
    required: bool = False,
) -> ValidationIssue | None:
    """A naive datetime is rejected, never assumed.

    Assuming a timezone is how a 16:00 ET expiration silently becomes 16:00 UTC,
    which shortens time-to-expiry by four hours and makes every 0DTE gamma wrong.
    """
    if value is None:
        if required:
            return ValidationIssue(
                code=ValidationCode.MISSING_TIMESTAMP,
                field=field_name,
                detail="timestamp is required but absent",
            )
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return ValidationIssue(
            code=ValidationCode.NAIVE_TIMESTAMP,
            field=field_name,
            detail="timestamp has no timezone information",
            observed=value.isoformat(),
        )
    return None


def check_not_future(
    value: datetime | None,
    *,
    reference: datetime,
    field_name: str,
    tolerance_seconds: float,
) -> ValidationIssue | None:
    """Reject a vendor timestamp dated materially after our reference instant.

    A future timestamp must not be able to earn a *perfect* freshness score, which
    is what a naive ``age = now - ts`` clamped at zero would give it. Small skew is
    ordinary clock drift between two machines; large skew means the feed, the
    parser or our own clock is wrong, and none of those should look healthy.
    """
    if value is None or value.tzinfo is None:
        return None
    drift = (value - reference).total_seconds()
    if drift > tolerance_seconds:
        return ValidationIssue(
            code=ValidationCode.FUTURE_TIMESTAMP,
            field=field_name,
            detail=(
                f"timestamp is {drift:.3f}s in the future, tolerance is "
                f"{tolerance_seconds:.3f}s"
            ),
            observed=value.isoformat(),
        )
    return None


def check_skew(
    left: datetime | None,
    right: datetime | None,
    *,
    field_name: str,
    tolerance_seconds: float,
    severity: Severity = Severity.WARNING,
) -> ValidationIssue | None:
    """Absolute time gap between two source records being joined.

    Gamma is computed from a chain and a spot that are supposed to describe the
    same instant. When they drift apart the result is a blend of two moments, and
    on 0DTE that blend is where the error lives.
    """
    if left is None or right is None:
        return None
    if left.tzinfo is None or right.tzinfo is None:
        return None
    skew = abs((left - right).total_seconds())
    if skew > tolerance_seconds:
        return ValidationIssue(
            code=ValidationCode.TIMESTAMP_SKEW,
            field=field_name,
            detail=f"skew {skew:.3f}s exceeds tolerance {tolerance_seconds:.3f}s",
            severity=severity,
            observed=f"{skew:.3f}s",
        )
    return None


def check_expiration(
    value: date | None, *, field_name: str = "expiry"
) -> ValidationIssue | None:
    if value is None:
        return ValidationIssue(
            code=ValidationCode.INVALID_EXPIRATION,
            field=field_name,
            detail="expiration is absent",
        )
    if not isinstance(value, date) or isinstance(value, datetime):
        return ValidationIssue(
            code=ValidationCode.INVALID_EXPIRATION,
            field=field_name,
            detail="expiration must be a date, not a datetime",
            observed=repr(value),
        )
    return None


def collect(*issues: ValidationIssue | None) -> ValidationResult:
    """Build a result from a sequence of optional issues."""
    return ValidationResult(
        issues=tuple(issue for issue in issues if issue is not None)
    )
