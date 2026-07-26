"""Domain validation: every rule, plus the contamination guard.

The single most important property under test: a malformed contract must not
reach an aggregate. ``NaN`` is the dangerous case because it defeats naive
checks -- ``NaN < 0`` is ``False``, so a NaN bid passes a negativity test -- and
because once summed it turns the whole chain total into ``NaN``, which looks like
a rendering bug rather than a data bug three layers downstream.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from src.domain.contracts import (
    OptionContract,
    OptionQuote,
    OptionRight,
    OptionRoot,
)
from src.domain.iv import IVQualityFlag, IVSource, build_iv_quote
from src.domain.normalize import validate_chain, validate_quote
from src.domain.timestamps import ContractTimestamps, DataQualityLimits
from src.domain.validation import (
    Severity,
    ValidationCode,
    ValidationIssue,
    ValidationReport,
    ValidationResult,
    ValidationStatus,
    check_finite,
    check_not_future,
    check_skew,
    check_timezone_aware,
)
from src.gex.engine import compute_gex_snapshot
from src.gex.formulas import compute_contract_gex
from src.gex.sessions import eastern
from src.synthetic.chains import (
    build_single_contract_chain,
    build_synthetic_chain,
    with_quote,
)

AS_OF = eastern(2026, 3, 17, 11, 0)
LIMITS = DataQualityLimits()


def make_quote(**changes: object) -> OptionQuote:
    base = OptionQuote(
        contract=OptionContract(
            root=OptionRoot.SPXW,
            expiry=date(2026, 3, 31),
            strike=5000.0,
            right=OptionRight.CALL,
        ),
        timestamps=ContractTimestamps(
            quote_timestamp=AS_OF,
            greeks_timestamp=AS_OF,
            iv_timestamp=AS_OF,
            underlying_timestamp=AS_OF,
            open_interest_as_of=date(2026, 3, 16),
        ),
        bid=10.0,
        ask=10.5,
        open_interest=1000,
        iv=build_iv_quote(bid_iv=0.19, mid_iv=0.20, ask_iv=0.21),
        gamma=0.001,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def check(quote: OptionQuote, **kwargs: object) -> ValidationResult:
    return validate_quote(quote, reference=AS_OF, limits=LIMITS, **kwargs)  # type: ignore[arg-type]


# --- Status model -----------------------------------------------------------


def test_clean_quote_is_accepted():
    result = check(make_quote())
    assert result.status is ValidationStatus.ACCEPTED
    assert result.is_usable
    assert result.issues == ()


def test_warning_is_usable_but_distinguishable():
    """The three-way status exists because the middle case is real: a zero-bid
    wing option has usable gamma but untrustworthy IV.
    """
    result = check(make_quote(bid=0.0))
    assert result.status is ValidationStatus.ACCEPTED_WITH_WARNING
    assert result.is_usable
    assert ValidationCode.ZERO_BID in result.warning_codes


def test_error_makes_the_record_unusable():
    result = check(make_quote(open_interest=-5))
    assert result.status is ValidationStatus.REJECTED
    assert not result.is_usable
    assert ValidationCode.NEGATIVE_OPEN_INTEREST in result.rejection_codes


def test_issues_are_machine_readable():
    issue = check(make_quote(open_interest=-5)).issues[0]
    payload = issue.as_dict()
    assert payload["code"] == "negative_open_interest"
    assert payload["severity"] == "error"
    assert payload["field"] == "quote.open_interest"
    assert payload["observed"] == "-5"


# --- Non-finite numbers -----------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["bid", "ask", "gamma", "open_interest"])
def test_non_finite_values_are_rejected(bad, field):
    result = check(make_quote(**{field: bad}))
    assert not result.is_usable
    assert ValidationCode.NOT_FINITE in result.rejection_codes


def test_nan_bid_does_not_slip_through_the_negativity_check():
    """``NaN < 0`` is False, so a naive negativity test accepts a NaN bid.

    The finiteness check has to run first and the ordering comparison has to be
    guarded, or the NaN reaches a sum and turns the chain total into NaN.
    """
    assert not (float("nan") < 0.0)  # the trap, stated explicitly
    result = check(make_quote(bid=float("nan")))
    assert ValidationCode.NOT_FINITE in result.rejection_codes


def test_non_finite_iv_is_rejected():
    quote = make_quote(iv=build_iv_quote(bid_iv=None, mid_iv=float("nan"), ask_iv=None))
    assert ValidationCode.NOT_FINITE in check(quote).rejection_codes


def test_boolean_is_not_accepted_as_a_number():
    """``isinstance(True, int)`` is True in Python, so booleans need excluding
    explicitly or a ``True`` gamma silently prices as 1.0.
    """
    issue = check_finite(True, field_name="quote.gamma")  # type: ignore[arg-type]
    assert issue is not None
    assert issue.code is ValidationCode.NOT_FINITE


def test_none_is_not_a_finiteness_error():
    """Absence is handled by the field's own required/optional rule."""
    assert check_finite(None, field_name="quote.gamma") is None


# --- Individual numeric rules -----------------------------------------------


def test_negative_bid_and_ask_are_rejected():
    assert ValidationCode.NEGATIVE_BID in check(make_quote(bid=-1.0)).rejection_codes
    assert ValidationCode.NEGATIVE_ASK in check(make_quote(ask=-1.0)).rejection_codes


def test_crossed_market_is_an_error_by_default():
    result = check(make_quote(bid=11.0, ask=10.0))
    assert ValidationCode.CROSSED_MARKET in result.rejection_codes


def test_crossed_market_can_be_classified_as_a_warning_instead():
    """Explicit classification, per the requirement that a crossed book may be
    accepted only when deliberately classified as such.
    """
    result = check(make_quote(bid=11.0, ask=10.0), treat_crossed_as_error=False)
    assert result.status is ValidationStatus.ACCEPTED_WITH_WARNING
    assert ValidationCode.CROSSED_MARKET in result.warning_codes


def test_locked_market_is_a_warning():
    assert (
        ValidationCode.LOCKED_MARKET
        in check(make_quote(bid=10.0, ask=10.0)).warning_codes
    )


def test_negative_open_interest_is_rejected():
    assert (
        ValidationCode.NEGATIVE_OPEN_INTEREST
        in check(make_quote(open_interest=-1)).rejection_codes
    )


def test_missing_open_interest_is_rejected_when_required():
    assert (
        ValidationCode.MISSING_OPEN_INTEREST
        in check(make_quote(open_interest=None)).rejection_codes
    )
    assert check(make_quote(open_interest=None), require_open_interest=False).is_usable


def test_negative_gamma_is_rejected():
    assert (
        ValidationCode.NEGATIVE_GAMMA in check(make_quote(gamma=-0.001)).rejection_codes
    )


def test_absurd_gamma_is_rejected():
    assert (
        ValidationCode.GAMMA_OUT_OF_RANGE
        in check(make_quote(gamma=50.0)).rejection_codes
    )


def test_invalid_strike_is_rejected():
    for strike in (0.0, -5000.0, float("nan")):
        contract = OptionContract(
            root=OptionRoot.SPXW,
            expiry=date(2026, 3, 31),
            strike=strike,
            right=OptionRight.CALL,
        )
        assert not check(make_quote(contract=contract)).is_usable


def test_invalid_multiplier_is_rejected():
    contract = OptionContract(
        root=OptionRoot.SPXW,
        expiry=date(2026, 3, 31),
        strike=5000.0,
        right=OptionRight.CALL,
        multiplier=0.0,
    )
    assert (
        ValidationCode.INVALID_MULTIPLIER
        in check(make_quote(contract=contract)).rejection_codes
    )


def test_non_positive_iv_is_a_warning_not_a_rejection():
    """The IV is unusable but a vendor gamma may still be present, so the
    contract survives with its IV flagged.
    """
    quote = make_quote(iv=build_iv_quote(bid_iv=None, mid_iv=-0.2, ask_iv=None))
    result = check(quote)
    assert result.status is ValidationStatus.ACCEPTED_WITH_WARNING


def test_absurd_iv_is_flagged():
    quote = make_quote(iv=build_iv_quote(bid_iv=None, mid_iv=12.0, ask_iv=None))
    assert ValidationCode.IMPLIED_VOL_OUT_OF_RANGE in check(quote).warning_codes


def test_extreme_iv_spread_is_flagged():
    quote = make_quote(iv=build_iv_quote(bid_iv=0.10, mid_iv=0.30, ask_iv=0.50))
    assert quote.iv.quality is IVQualityFlag.WIDE_SPREAD
    assert ValidationCode.EXTREME_IV_SPREAD in check(quote).warning_codes


def test_no_gamma_source_is_rejected():
    quote = make_quote(
        gamma=None, iv=build_iv_quote(bid_iv=None, mid_iv=None, ask_iv=None)
    )
    assert ValidationCode.NO_GAMMA_SOURCE in check(quote).rejection_codes


# --- Structure --------------------------------------------------------------


def test_duplicate_contract_identities_are_both_rejected():
    """Neither copy is arbitrarily kept.

    There is no principled way to choose, and silently keeping the first is how a
    stale record wins over a fresh one. Keeping both would double-count.
    """
    chain = build_single_contract_chain()
    duplicated = replace(chain, quotes=(chain.quotes[0], chain.quotes[0]))
    normalized = validate_chain(duplicated)
    assert normalized.snapshot.quotes == ()
    assert normalized.report.rejected == 2
    assert normalized.report.error_counts[ValidationCode.DUPLICATE_CONTRACT] == 2


def test_distinct_contracts_at_the_same_strike_are_not_duplicates():
    """A call and a put at one strike share everything but the right."""
    chain = build_single_contract_chain()
    put = replace(
        chain.quotes[0],
        contract=replace(chain.quotes[0].contract, right=OptionRight.PUT),
    )
    normalized = validate_chain(replace(chain, quotes=(chain.quotes[0], put)))
    assert len(normalized.snapshot.quotes) == 2


def test_spx_and_spxw_at_the_same_strike_and_expiry_are_distinct():
    """Root is part of the identity: the two settle hours apart."""
    chain = build_single_contract_chain()
    spx = replace(
        chain.quotes[0],
        contract=replace(chain.quotes[0].contract, root=OptionRoot.SPX),
    )
    normalized = validate_chain(replace(chain, quotes=(chain.quotes[0], spx)))
    assert len(normalized.snapshot.quotes) == 2


# --- Timestamps -------------------------------------------------------------


def test_naive_timestamp_is_rejected_never_assumed():
    """Assuming a timezone is how 16:00 ET becomes 16:00 UTC and every 0DTE
    gamma goes wrong by four hours of time-to-expiry.
    """
    naive = datetime(2026, 3, 17, 11, 0)
    quote = make_quote(timestamps=ContractTimestamps(quote_timestamp=naive))
    assert ValidationCode.NAIVE_TIMESTAMP in check(quote).rejection_codes


def test_timezone_aware_timestamp_passes():
    assert (
        check_timezone_aware(datetime(2026, 3, 17, 15, 0, tzinfo=UTC), field_name="ts")
        is None
    )


def test_missing_required_timestamp_is_reported():
    issue = check_timezone_aware(None, field_name="ts", required=True)
    assert issue is not None
    assert issue.code is ValidationCode.MISSING_TIMESTAMP


def test_small_clock_skew_into_the_future_is_tolerated():
    """Two machines disagreeing by a second is ordinary, not a fault."""
    slightly_ahead = AS_OF + timedelta(seconds=1)
    assert (
        check_not_future(
            slightly_ahead, reference=AS_OF, field_name="ts", tolerance_seconds=2.0
        )
        is None
    )


def test_large_future_drift_is_rejected():
    far_ahead = AS_OF + timedelta(seconds=45)
    issue = check_not_future(
        far_ahead, reference=AS_OF, field_name="ts", tolerance_seconds=2.0
    )
    assert issue is not None
    assert issue.code is ValidationCode.FUTURE_TIMESTAMP


def test_future_timestamp_on_a_quote_rejects_the_record():
    quote = make_quote(
        timestamps=ContractTimestamps(quote_timestamp=AS_OF + timedelta(minutes=5))
    )
    assert ValidationCode.FUTURE_TIMESTAMP in check(quote).rejection_codes


def test_stale_quote_is_warned_about():
    quote = make_quote(
        timestamps=ContractTimestamps(quote_timestamp=AS_OF - timedelta(seconds=300))
    )
    assert ValidationCode.STALE_SNAPSHOT in check(quote).warning_codes


def test_skew_between_quote_and_greeks_is_flagged():
    quote = make_quote(
        timestamps=ContractTimestamps(
            quote_timestamp=AS_OF,
            greeks_timestamp=AS_OF - timedelta(seconds=30),
        )
    )
    assert ValidationCode.TIMESTAMP_SKEW in check(quote).warning_codes


def test_skew_within_tolerance_is_not_flagged():
    quote = make_quote(
        timestamps=ContractTimestamps(
            quote_timestamp=AS_OF,
            greeks_timestamp=AS_OF - timedelta(seconds=1),
            iv_timestamp=AS_OF,
            underlying_timestamp=AS_OF,
        )
    )
    assert ValidationCode.TIMESTAMP_SKEW not in check(quote).warning_codes


def test_underlying_skew_uses_the_tightest_tolerance():
    """Quote-vs-underlying is the gamma input pair, so it is held tightest."""
    limits = DataQualityLimits(
        max_quote_greeks_skew_seconds=30.0, max_quote_underlying_skew_seconds=2.0
    )
    quote = make_quote(
        timestamps=ContractTimestamps(
            quote_timestamp=AS_OF,
            greeks_timestamp=AS_OF - timedelta(seconds=10),
            underlying_timestamp=AS_OF - timedelta(seconds=10),
        )
    )
    result = validate_quote(quote, reference=AS_OF, limits=limits)
    skews = [i for i in result.issues if i.code is ValidationCode.TIMESTAMP_SKEW]
    assert len(skews) == 1
    assert skews[0].field == "timestamps.quote_vs_underlying"


def test_skew_helper_ignores_absent_or_naive_inputs():
    assert check_skew(None, AS_OF, field_name="x", tolerance_seconds=1.0) is None
    assert (
        check_skew(
            datetime(2026, 3, 17, 11, 0), AS_OF, field_name="x", tolerance_seconds=1.0
        )
        is None
    )


def test_timestamps_are_never_back_stamped_to_as_of():
    """The failure this whole design exists to prevent.

    A five-minute-old quote must still *look* five minutes old after
    normalisation. If ``as_of`` were assigned to each record, every chain would
    read as perfectly fresh regardless of what the vendor sent.
    """
    stale = AS_OF - timedelta(seconds=300)
    chain = build_synthetic_chain()
    corrupted = with_quote(
        chain,
        0,
        timestamps=replace(chain.quotes[0].timestamps, quote_timestamp=stale),
    )
    normalized = validate_chain(corrupted, limits=DataQualityLimits())
    surviving = [
        q for q in normalized.snapshot.quotes if q.timestamps.quote_timestamp == stale
    ]
    assert surviving, "the stale record should survive as a warning, not vanish"
    assert corrupted.options_feed_timestamp == stale


# --- Chain-level snapshot invariants ----------------------------------------


def test_non_finite_spot_is_refused_at_construction():
    chain = build_single_contract_chain()
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite"):
            replace(chain, spot=bad)


def test_non_positive_spot_is_refused_at_construction():
    chain = build_single_contract_chain()
    with pytest.raises(ValueError, match="positive"):
        replace(chain, spot=0.0)


def test_naive_as_of_is_refused_at_construction():
    chain = build_single_contract_chain()
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(chain, as_of=datetime(2026, 3, 17, 11, 0))


def test_non_numeric_spot_is_refused():
    chain = build_single_contract_chain()
    with pytest.raises(TypeError):
        replace(chain, spot="5000")  # type: ignore[arg-type]


# --- The contamination guard ------------------------------------------------


def test_a_single_nan_gamma_cannot_poison_the_chain_total():
    """The end-to-end version of the whole module's purpose."""
    chain = build_synthetic_chain()
    poisoned = with_quote(chain, 0, gamma=float("nan"))
    snapshot = compute_gex_snapshot(poisoned)
    assert math.isfinite(snapshot.total_unsigned_gex)
    assert math.isfinite(snapshot.total_signed_gex)
    assert snapshot.total_unsigned_gex > 0.0
    assert snapshot.validation.rejected == 1


def test_a_negative_open_interest_cannot_subtract_from_the_chain_total():
    """A negative OI would contribute a large *negative* notional if it reached
    the sum, because the magnitude is ``|gamma| * OI * ...``.

    The corrupted contract is chosen as the largest real contributor in the
    chain, so its removal is measurable; corrupting a far-wing strike with an
    open interest of 1 would make this test pass without proving anything.
    """
    chain = build_synthetic_chain()
    clean = compute_gex_snapshot(chain)
    heaviest = max(
        range(len(chain.quotes)),
        key=lambda i: (chain.quotes[i].open_interest or 0)
        * (chain.quotes[i].gamma or 0.0),
    )
    poisoned = compute_gex_snapshot(
        with_quote(chain, heaviest, open_interest=-1_000_000)
    )
    assert poisoned.total_unsigned_gex > 0.0
    # Dropped, not negated: the total falls by that contract's share and no more.
    assert poisoned.total_unsigned_gex < clean.total_unsigned_gex
    assert poisoned.contract_count == clean.contract_count - 1
    assert poisoned.validation.error_counts[ValidationCode.NEGATIVE_OPEN_INTEREST] == 1


def test_rejected_contracts_are_reported_not_silently_absent():
    chain = build_synthetic_chain()
    poisoned = with_quote(chain, 0, bid=float("inf"))
    result = compute_contract_gex(poisoned)
    assert result.validation.rejected == 1
    assert result.validation.error_counts[ValidationCode.NOT_FINITE] == 1
    assert result.validation.total == len(chain.quotes)


# --- Report aggregation -----------------------------------------------------


def test_report_counts_and_ratios():
    report = ValidationReport()
    report.record(ValidationResult())
    report.record(
        ValidationResult(
            issues=(
                ValidationIssue(
                    code=ValidationCode.ZERO_BID,
                    field="quote.bid",
                    detail="",
                    severity=Severity.WARNING,
                ),
            )
        )
    )
    report.record(
        ValidationResult(
            issues=(
                ValidationIssue(
                    code=ValidationCode.NOT_FINITE, field="quote.bid", detail=""
                ),
            )
        )
    )
    assert report.total == 3
    assert report.usable == 2
    assert report.acceptance_ratio == pytest.approx(2 / 3)
    assert report.count(ValidationCode.NOT_FINITE) == 1
    assert report.count(ValidationCode.ZERO_BID) == 1


def test_report_example_list_is_bounded():
    """An unbounded example list is a memory leak on a bad feed day."""
    report = ValidationReport(max_examples=3)
    for _ in range(50):
        report.record(
            ValidationResult(
                issues=(
                    ValidationIssue(
                        code=ValidationCode.NOT_FINITE, field="quote.bid", detail=""
                    ),
                )
            )
        )
    assert len(report.examples) == 3
    assert report.rejected == 50


def test_report_serialises_to_sorted_machine_readable_counts():
    chain = build_synthetic_chain()
    poisoned = with_quote(chain, 0, gamma=float("nan"))
    payload = compute_contract_gex(poisoned).validation.as_dict()
    assert payload["rejected"] == 1
    assert "not_finite" in payload["error_counts"]
    assert list(payload["error_counts"]) == sorted(payload["error_counts"])


# --- IV provenance ----------------------------------------------------------


def test_iv_carries_its_source_and_spread():
    quote = make_quote(iv=build_iv_quote(bid_iv=0.18, mid_iv=0.20, ask_iv=0.22))
    assert quote.iv.source is IVSource.NBBO_MID_IV
    assert quote.iv.iv_spread == pytest.approx(0.04)
    assert quote.effective_iv == pytest.approx(0.20)


def test_effective_iv_is_none_when_quality_is_unusable():
    quote = make_quote(
        iv=build_iv_quote(bid_iv=None, mid_iv=0.2, ask_iv=None, crossed=True)
    )
    assert quote.iv.quality is IVQualityFlag.CROSSED_MARKET
    assert quote.effective_iv is None


def test_iv_falls_back_and_records_which_leg_it_used():
    quote = make_quote(
        iv=build_iv_quote(
            bid_iv=None,
            mid_iv=None,
            ask_iv=0.25,
            preferred_source=IVSource.NBBO_MID_IV,
        )
    )
    assert quote.iv.source is IVSource.NBBO_ASK_IV
    assert quote.iv.quality is IVQualityFlag.SINGLE_SIDED


def test_vendor_solver_residual_marks_a_vendor_error():
    quote = make_quote(
        iv=build_iv_quote(bid_iv=0.19, mid_iv=0.20, ask_iv=0.21, vendor_iv_error=0.9)
    )
    assert quote.iv.quality is IVQualityFlag.VENDOR_ERROR


def test_snapshot_records_the_iv_source_in_its_model_spec():
    from src.gex.config import GexEngineConfig
    from src.synthetic.chains import SyntheticChainSpec

    spec = SyntheticChainSpec(iv_source=IVSource.NBBO_MID_IV)
    snapshot = compute_gex_snapshot(
        build_synthetic_chain(spec), GexEngineConfig(model_spec=spec.model_spec())
    )
    assert snapshot.model_spec.iv_price_source is IVSource.NBBO_MID_IV
    assert snapshot.as_dict()["model_spec"]["iv_price_source"] == "NBBO_MID_IV"
