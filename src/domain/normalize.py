"""Chain-level validation: the gate every contract passes before arithmetic.

Runs once, at the point where vendor records become engine inputs. The output is
a :class:`~src.domain.validation.ValidationReport` carried alongside the accepted
contracts, so every later stage can answer "how many contracts were dropped, and
why" without re-deriving it.

Ordering matters and is deliberate:

1. Structural checks first (right, expiry, strike, multiplier). A contract whose
   identity is malformed cannot be meaningfully checked for anything else.
2. Finiteness before every numeric comparison. ``NaN < 0`` is ``False``, so a
   NaN bid sails through a naive negativity check and lands in a sum.
3. Time checks last, because they need the chain-level reference instant.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

from src.domain.contracts import ChainSnapshot, OptionQuote, OptionRight
from src.domain.iv import IVQualityFlag
from src.domain.timestamps import DataQualityLimits
from src.domain.validation import (
    Severity,
    ValidationCode,
    ValidationIssue,
    ValidationReport,
    ValidationResult,
    check_expiration,
    check_finite,
    check_in_range,
    check_non_negative,
    check_not_future,
    check_positive,
    check_skew,
    check_timezone_aware,
    collect,
)
from src.gex.sessions import to_eastern

# Sanity bounds. These are not market predictions -- they are the range outside
# which a number is certainly a data error rather than an unusual market.
MAX_PLAUSIBLE_IMPLIED_VOL = 5.0  # 500% vol
MIN_PLAUSIBLE_IMPLIED_VOL = 1e-4
MAX_PLAUSIBLE_GAMMA = 1.0  # per $1 of spot, per contract
MAX_PLAUSIBLE_STRIKE = 1e7
MAX_PLAUSIBLE_DTE = 3653  # ~10 years; beyond this the expiry is a parse error


@dataclass(frozen=True, slots=True)
class NormalizedChain:
    """Validated chain: only usable quotes, plus the full accounting."""

    snapshot: ChainSnapshot
    report: ValidationReport
    # Parallel to ``snapshot.quotes``: the per-contract validation outcome, kept
    # so a warning (e.g. zero-bid) can still influence downstream scoring.
    results: tuple[ValidationResult, ...]
    rejected: tuple[tuple[OptionQuote, ValidationResult], ...] = ()

    @property
    def chain_level_issues(self) -> tuple[ValidationIssue, ...]:
        return tuple(
            issue for issue in self.report.examples if issue.field.startswith("chain.")
        )


def validate_quote(
    quote: OptionQuote,
    *,
    reference: datetime,
    limits: DataQualityLimits,
    require_open_interest: bool = True,
    treat_crossed_as_error: bool = True,
) -> ValidationResult:
    """Validate one contract in isolation."""
    contract = quote.contract
    issues: list[ValidationIssue | None] = []

    # --- Structure -----------------------------------------------------------
    issues.append(check_expiration(contract.expiry))
    # Runtime guard against an object that bypassed the type annotation -- a
    # deserialised record or a hand-built quote, both of which a vendor adapter
    # can realistically produce. Written as a membership check on the value
    # rather than an isinstance narrow, so mypy does not (correctly) call the
    # isinstance branch unreachable and push an ignore comment onto code that
    # really does execute.
    if contract.right not in (OptionRight.CALL, OptionRight.PUT):
        issues.append(
            ValidationIssue(
                code=ValidationCode.INVALID_OPTION_RIGHT,
                field="contract.right",
                detail="right must be OptionRight.CALL or OptionRight.PUT",
                observed=repr(contract.right),
            )
        )
    issues.append(check_finite(contract.strike, field_name="contract.strike"))
    issues.append(
        check_positive(
            contract.strike,
            field_name="contract.strike",
            code=ValidationCode.INVALID_STRIKE,
        )
    )
    if contract.strike is not None and _is_finite(contract.strike):
        issues.append(
            check_in_range(
                contract.strike,
                field_name="contract.strike",
                code=ValidationCode.INVALID_STRIKE,
                low=0.0,
                high=MAX_PLAUSIBLE_STRIKE,
            )
        )
    issues.append(check_finite(contract.multiplier, field_name="contract.multiplier"))
    issues.append(
        check_positive(
            contract.multiplier,
            field_name="contract.multiplier",
            code=ValidationCode.INVALID_MULTIPLIER,
        )
    )

    # --- Book ----------------------------------------------------------------
    for name, value in (("bid", quote.bid), ("ask", quote.ask)):
        issues.append(check_finite(value, field_name=f"quote.{name}"))
    issues.append(
        check_non_negative(
            _finite_or_none(quote.bid),
            field_name="quote.bid",
            code=ValidationCode.NEGATIVE_BID,
        )
    )
    issues.append(
        check_non_negative(
            _finite_or_none(quote.ask),
            field_name="quote.ask",
            code=ValidationCode.NEGATIVE_ASK,
        )
    )
    if quote.is_crossed:
        issues.append(
            ValidationIssue(
                code=ValidationCode.CROSSED_MARKET,
                field="quote.book",
                detail="ask is below bid",
                severity=(
                    Severity.ERROR if treat_crossed_as_error else Severity.WARNING
                ),
                observed=f"bid={quote.bid} ask={quote.ask}",
            )
        )
    elif quote.is_locked:
        issues.append(
            ValidationIssue(
                code=ValidationCode.LOCKED_MARKET,
                field="quote.book",
                detail="bid equals ask",
                severity=Severity.WARNING,
                observed=f"bid={quote.bid}",
            )
        )
    if quote.is_zero_bid:
        issues.append(
            ValidationIssue(
                code=ValidationCode.ZERO_BID,
                field="quote.bid",
                detail="zero bid; mid and any IV implied from it are unreliable",
                severity=Severity.WARNING,
                observed=repr(quote.bid),
            )
        )

    # --- Open interest -------------------------------------------------------
    issues.append(check_finite(quote.open_interest, field_name="quote.open_interest"))
    issues.append(
        check_non_negative(
            _finite_or_none(quote.open_interest),
            field_name="quote.open_interest",
            code=ValidationCode.NEGATIVE_OPEN_INTEREST,
        )
    )
    if require_open_interest and quote.open_interest is None:
        issues.append(
            ValidationIssue(
                code=ValidationCode.MISSING_OPEN_INTEREST,
                field="quote.open_interest",
                detail="open interest is required to weight GEX",
            )
        )

    # --- Adapter parse issues -------------------------------------------------
    # Recorded by the adapter rather than swallowed. One corrupt cell costs one
    # contract, not the whole chain -- but it is never invisible.
    for field_name, issue_code in quote.parse_issues:
        issues.append(
            ValidationIssue(
                code=ValidationCode.MALFORMED_INTEGER,
                field=f"quote.{field_name}",
                detail=f"vendor value could not be parsed as an integer ({issue_code})",
                observed=issue_code,
            )
        )

    # --- Greeks and IV -------------------------------------------------------
    issues.append(check_finite(quote.gamma, field_name="quote.gamma"))
    issues.append(
        check_non_negative(
            _finite_or_none(quote.gamma),
            field_name="quote.gamma",
            code=ValidationCode.NEGATIVE_GAMMA,
        )
    )
    issues.append(
        check_in_range(
            _finite_or_none(quote.gamma),
            field_name="quote.gamma",
            code=ValidationCode.GAMMA_OUT_OF_RANGE,
            low=0.0,
            high=MAX_PLAUSIBLE_GAMMA,
        )
    )
    issues.extend(_validate_iv(quote))

    # --- Time ----------------------------------------------------------------
    issues.extend(_validate_timestamps(quote, reference=reference, limits=limits))

    # --- Usability -----------------------------------------------------------
    has_gamma_source = quote.gamma is not None or quote.effective_iv is not None
    if not has_gamma_source:
        issues.append(
            ValidationIssue(
                code=ValidationCode.NO_GAMMA_SOURCE,
                field="quote",
                detail="neither vendor gamma nor a usable implied volatility",
            )
        )

    return collect(*issues)


def _is_finite(value: float) -> bool:
    import math

    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _finite_or_none(value: float | None) -> float | None:
    """Guard for comparisons.

    Without this, ``NaN < 0`` returns False and a NaN bid passes the negativity
    check. Finiteness is reported separately by ``check_finite``; here we simply
    refuse to compare a value that cannot be ordered.
    """
    if value is None:
        return None
    return value if _is_finite(value) else None


def _validate_iv(quote: OptionQuote) -> list[ValidationIssue | None]:
    issues: list[ValidationIssue | None] = []
    iv = quote.iv
    for name, value in (
        ("value", iv.value),
        ("bid_iv", iv.bid_iv),
        ("mid_iv", iv.mid_iv),
        ("ask_iv", iv.ask_iv),
    ):
        issues.append(check_finite(value, field_name=f"quote.iv.{name}"))
    if iv.value is not None and _is_finite(iv.value):
        issues.append(
            check_positive(
                iv.value,
                field_name="quote.iv.value",
                code=ValidationCode.NON_POSITIVE_IMPLIED_VOL,
                # A non-positive IV disqualifies this contract's IV but not the
                # contract, which may still carry a usable vendor gamma.
                severity=Severity.WARNING,
            )
        )
        issues.append(
            check_in_range(
                iv.value,
                field_name="quote.iv.value",
                code=ValidationCode.IMPLIED_VOL_OUT_OF_RANGE,
                low=MIN_PLAUSIBLE_IMPLIED_VOL,
                high=MAX_PLAUSIBLE_IMPLIED_VOL,
                severity=Severity.WARNING,
            )
        )
    if iv.quality is IVQualityFlag.NON_FINITE_INPUT:
        # The value was sanitised to None before it could reach the pricer, but
        # a corrupt vendor field is a data error and must be reported as one --
        # not allowed to look like "the vendor did not send an IV".
        issues.append(
            ValidationIssue(
                code=ValidationCode.NOT_FINITE,
                field="quote.iv",
                detail="vendor supplied a NaN or infinite implied volatility",
            )
        )
    if iv.quality is IVQualityFlag.WIDE_SPREAD:
        issues.append(
            ValidationIssue(
                code=ValidationCode.EXTREME_IV_SPREAD,
                field="quote.iv",
                detail="bid/ask implied volatilities are far apart",
                severity=Severity.WARNING,
                observed=repr(iv.iv_spread),
            )
        )
    if iv.quality is IVQualityFlag.SOLVER_FAILED:
        issues.append(
            ValidationIssue(
                code=ValidationCode.NON_POSITIVE_IMPLIED_VOL,
                field="quote.iv",
                detail="local IV solver did not converge",
                severity=Severity.WARNING,
            )
        )
    return issues


def _validate_timestamps(
    quote: OptionQuote, *, reference: datetime, limits: DataQualityLimits
) -> list[ValidationIssue | None]:
    stamps = quote.timestamps
    issues: list[ValidationIssue | None] = []

    for name, value in stamps.source_clocks.items():
        issues.append(check_timezone_aware(value, field_name=f"timestamps.{name}"))
        issues.append(
            check_not_future(
                value,
                reference=reference,
                field_name=f"timestamps.{name}",
                tolerance_seconds=limits.max_future_timestamp_seconds,
            )
        )

    # Open interest is a settlement DATE, not a clock reading, so it gets no
    # sub-second skew tolerance: tomorrow's settlement date is not clock drift,
    # it is impossible data.
    if stamps.open_interest_as_of is not None:
        reference_date = to_eastern(reference).date()
        if stamps.open_interest_as_of > reference_date:
            issues.append(
                ValidationIssue(
                    code=ValidationCode.FUTURE_OPEN_INTEREST,
                    field="timestamps.open_interest_as_of",
                    detail=(
                        f"open interest is dated {stamps.open_interest_as_of}, "
                        f"after the snapshot date {reference_date}; settlement "
                        "data cannot come from the future"
                    ),
                    observed=stamps.open_interest_as_of.isoformat(),
                )
            )

    quote_ts = stamps.quote_timestamp
    if quote_ts is not None and quote_ts.tzinfo is not None:
        age = (reference - quote_ts).total_seconds()
        if age > limits.max_snapshot_age_seconds:
            issues.append(
                ValidationIssue(
                    code=ValidationCode.STALE_SNAPSHOT,
                    field="timestamps.quote_timestamp",
                    detail=(
                        f"quote is {age:.1f}s old, limit is "
                        f"{limits.max_snapshot_age_seconds:.1f}s"
                    ),
                    severity=Severity.WARNING,
                    observed=quote_ts.isoformat(),
                )
            )

    issues.append(
        check_skew(
            quote_ts,
            stamps.greeks_timestamp,
            field_name="timestamps.quote_vs_greeks",
            tolerance_seconds=limits.max_quote_greeks_skew_seconds,
        )
    )
    issues.append(
        check_skew(
            quote_ts,
            stamps.iv_timestamp,
            field_name="timestamps.quote_vs_iv",
            tolerance_seconds=limits.max_quote_iv_skew_seconds,
        )
    )
    issues.append(
        check_skew(
            quote_ts,
            stamps.underlying_timestamp,
            field_name="timestamps.quote_vs_underlying",
            tolerance_seconds=limits.max_quote_underlying_skew_seconds,
        )
    )
    return issues


def validate_chain(
    snapshot: ChainSnapshot,
    *,
    limits: DataQualityLimits | None = None,
    require_open_interest: bool = True,
    treat_crossed_as_error: bool = True,
) -> NormalizedChain:
    """Validate every contract plus the chain-level invariants.

    Duplicate identity is necessarily a chain-level check: a duplicated contract
    looks perfectly valid on its own, and only double-counts once summed. Both
    copies are rejected rather than one arbitrarily kept, because there is no
    principled way to choose and silently keeping the first is how a stale record
    wins over a fresh one.
    """
    active_limits = limits or DataQualityLimits()
    reference = snapshot.as_of

    duplicates = _duplicate_keys(snapshot.quotes)

    report = ValidationReport()
    accepted: list[OptionQuote] = []
    accepted_results: list[ValidationResult] = []
    rejected: list[tuple[OptionQuote, ValidationResult]] = []

    for quote in snapshot.quotes:
        result = validate_quote(
            quote,
            reference=reference,
            limits=active_limits,
            require_open_interest=require_open_interest,
            treat_crossed_as_error=treat_crossed_as_error,
        )
        if quote.contract.key in duplicates:
            result = result.with_issue(
                ValidationIssue(
                    code=ValidationCode.DUPLICATE_CONTRACT,
                    field="contract.key",
                    detail=(
                        f"{duplicates[quote.contract.key]} records share this "
                        "contract identity; all copies rejected"
                    ),
                    observed=quote.contract.canonical_id,
                )
            )
        report.record(result)
        if result.is_usable:
            accepted.append(quote)
            accepted_results.append(result)
        else:
            rejected.append((quote, result))

    from dataclasses import replace as _replace

    return NormalizedChain(
        snapshot=_replace(snapshot, quotes=tuple(accepted)),
        report=report,
        results=tuple(accepted_results),
        rejected=tuple(rejected),
    )


def _duplicate_keys(
    quotes: tuple[OptionQuote, ...],
) -> dict[tuple[str, date, float, str], int]:
    counts = Counter(quote.contract.key for quote in quotes)
    return {key: count for key, count in counts.items() if count > 1}
