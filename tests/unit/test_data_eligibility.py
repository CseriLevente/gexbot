"""Two vendor behaviours the analytical pipeline has to model, held shut.

Both are established by repeated live ThetaData captures and neither is a defect
in the vendor. ``docs/DATA_ELIGIBILITY.md`` carries the observations; this file
carries the consequences.

**An absent open-interest row is not an open interest of zero.** The strongest
consecutive-session reading is Aug 26 -> Aug 27: 438 identities present in the
contract list, the quote snapshot and the Greeks snapshot carried no
open-interest record on the 26th, and on the 27th every one of the 438 carried
an explicit record -- 177 positive, 261 an explicit zero, none still absent.
The defect this closes is one expression, ``quote.open_interest or 0``, which
turned "the vendor said nothing" into a weight of zero and summed it: the
contract left the aggregate without incrementing any counter, and the reading
that replaced it was, 177 times out of 438, wrong in the direction that matters.

**Snapshot endpoints retain contracts past their expiration.** On Aug 26 all
three snapshot endpoints still named roughly five hundred Aug-25 contracts, with
Aug-25 market timestamps; by Aug 27 those were gone and roughly five hundred
Aug-26 contracts had replaced them. So set equality between the three responses
says they agree -- not that they agree about *this* session.

The fixtures are three small CSVs derived from those two cases and holding six
identities, one per semantic state:

===================================  ==============================  ===========
identity (session 2026-03-17)        open interest                   verdict
===================================  ==============================  ===========
``2026-03-16 5000 call``             3300                            stale
``2026-03-16 5000 put``              2900                            stale
``2026-03-17 5000 call``             5100                            eligible 0DTE
``2026-03-20 5000 call``             4200                            eligible
``2026-03-20 5000 put``              explicit ``0``                  eligible, zero
``2026-03-20 5050 call``             **no record**                   unavailable
===================================  ==============================  ===========

The two stale identities carry *positive* open interest on purpose: staleness
has to be decided before availability, or a retention artefact would be reported
as a settlement gap and the two would need the same remedy. They do not.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
from datetime import UTC, date, datetime, timedelta

import pytest

from src.adapters.thetadata.client import (
    ChainAssemblyInputs,
    assemble_chain,
    parse_csv,
)
from src.domain.analytical_universe import (
    AnalyticalExclusion,
    OpenInterestObservationState,
    TemporalEligibility,
    analytical_exclusion,
    open_interest_observation_state,
    temporal_eligibility,
)
from src.domain.normalization import canonical_chain_payload
from src.domain.normalize import validate_chain
from src.domain.validation import ValidationCode
from src.gex.config import GexEngineConfig
from src.gex.engine import compute_gex_snapshot
from src.gex.formulas import ExclusionReason, compute_contract_gex
from src.gex.sessions import eastern, market_session_date
from src.synthetic.chains import build_single_contract_chain

FIXTURES = (
    pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "vendor" / "thetadata"
)

#: 11:00 ET on Tuesday 2026-03-17. The fixture session.
AS_OF = eastern(2026, 3, 17, 11, 0)
SESSION = date(2026, 3, 17)
SPOT = 5000.25

RETAINED_CALL = ("SPXW", date(2026, 3, 16), "5000", "call")
RETAINED_PUT = ("SPXW", date(2026, 3, 16), "5000", "put")
ZERO_DTE_CALL = ("SPXW", date(2026, 3, 17), "5000", "call")
CURRENT_CALL = ("SPXW", date(2026, 3, 20), "5000", "call")
REPORTED_ZERO_PUT = ("SPXW", date(2026, 3, 20), "5000", "put")
UNREPORTED_CALL = ("SPXW", date(2026, 3, 20), "5050", "call")


def rows(name: str) -> list[dict[str, str]]:
    return parse_csv((FIXTURES / name).read_text(encoding="utf-8"))


def eligibility_chain(**overrides):
    """The six-identity chain, assembled through the real adapter join.

    Through ``assemble_chain`` rather than hand-built, because three of the
    claims below are about what the *join* does with a contract the
    open-interest response never mentioned.
    """
    base = {
        "as_of": AS_OF,
        "spot": SPOT,
        "quote_rows": rows("quotes_eligibility.csv"),
        "open_interest_rows": rows("open_interest_eligibility.csv"),
        "first_order_rows": rows("greeks_first_order_eligibility.csv"),
        "second_order_rows": [],
        "open_interest_as_of": date(2026, 3, 16),
        "risk_free_rate": 0.042,
        "dividend_yield": 0.013,
        "spot_timestamp": AS_OF,
    }
    base.update(overrides)
    return assemble_chain(ChainAssemblyInputs(**base))


def by_key(chain):
    return {q.contract.key: q for q in chain.quotes}


# --- The three open-interest states ------------------------------------------


def test_the_three_open_interest_states_are_three_different_readings():
    """``0`` is a measurement, ``None`` is a silence, and both are not-positive.

    The whole release rests on this classifier, so it is asserted directly
    rather than only through its consumers.
    """
    assert (
        open_interest_observation_state(4200)
        is OpenInterestObservationState.OI_REPORTED_POSITIVE
    )
    assert (
        open_interest_observation_state(0)
        is OpenInterestObservationState.OI_REPORTED_ZERO
    )
    assert (
        open_interest_observation_state(None)
        is OpenInterestObservationState.OI_NOT_REPORTED
    )
    assert OpenInterestObservationState.OI_REPORTED_ZERO.is_reported
    assert OpenInterestObservationState.OI_REPORTED_ZERO.permits_oi_weighting
    assert not OpenInterestObservationState.OI_NOT_REPORTED.is_reported
    assert not OpenInterestObservationState.OI_NOT_REPORTED.permits_oi_weighting


def test_a_negative_count_is_never_a_usable_weight():
    """A negative contract count is a parse failure, not a small position.

    Validation rejects it under ``NEGATIVE_OPEN_INTEREST``; this is the second
    line, so a value that reached the classifier anyway cannot be read as a
    measurement.
    """
    assert (
        open_interest_observation_state(-1)
        is OpenInterestObservationState.OI_NOT_REPORTED
    )


def test_an_explicit_zero_stays_numeric_zero_and_is_not_missing():
    """Requirement 1. A reported zero survives the join as the integer ``0``."""
    quote = by_key(eligibility_chain())[REPORTED_ZERO_PUT]
    assert quote.open_interest == 0
    assert quote.open_interest is not None
    assert quote.open_interest_state is OpenInterestObservationState.OI_REPORTED_ZERO
    assert quote.has_reported_open_interest


def test_an_explicit_positive_open_interest_stays_usable():
    """Requirement 2."""
    quote = by_key(eligibility_chain())[CURRENT_CALL]
    assert quote.open_interest == 4200
    assert (
        quote.open_interest_state is OpenInterestObservationState.OI_REPORTED_POSITIVE
    )
    assert quote.has_reported_open_interest


def test_an_absent_open_interest_record_never_becomes_a_zero():
    """Requirement 3, at all four layers it could have happened in.

    Parsing and assembly: the join leaves the field ``None`` rather than
    defaulting. Serialization: the canonical chain payload -- the value a
    trusted calculation is bound to -- writes JSON ``null``. Calculation: the
    contract is excluded under its own reason instead of being weighted zero
    and summed.
    """
    chain = eligibility_chain()
    quote = by_key(chain)[UNREPORTED_CALL]

    # parsing / assembly
    assert quote.open_interest is None
    assert quote.open_interest_state is OpenInterestObservationState.OI_NOT_REPORTED

    # serialization
    payload = canonical_chain_payload(chain)
    serialised = {entry["contract_id"]: entry for entry in payload["quotes"]}
    unreported = serialised["SPXW:2026-03-20:5050:call"]
    assert unreported["open_interest"] is None
    assert "open_interest" in json.dumps(payload)
    assert json.loads(json.dumps(payload))["quotes"] == payload["quotes"]

    # calculation
    result = compute_contract_gex(chain)
    assert UNREPORTED_CALL not in {c.contract.key for c in result.contracts}
    assert all(c.open_interest > 0 for c in result.contracts)


def test_an_unreported_open_interest_is_excluded_even_with_the_filter_off():
    """Requirement 4, at the engine.

    ``require_open_interest=False`` says a *reported* zero may contribute
    nothing. It has never been permission to manufacture one, and before v2.1.28
    it was exactly that: the contract was priced at weight zero, counted among
    the included contracts, and no counter said why it contributed nothing.
    """
    chain = build_single_contract_chain(open_interest=None)
    for config in (GexEngineConfig(), GexEngineConfig(require_open_interest=False)):
        result = compute_contract_gex(chain, config)
        assert result.contracts == ()
        # One answer whichever stage caught it: validation refuses it when the
        # flag is on, the engine loop when it is off.
        assert result.analytical_exclusions == {
            AnalyticalExclusion.OPEN_INTEREST_NOT_REPORTED.value: 1
        }
    unfiltered = compute_contract_gex(
        chain, GexEngineConfig(require_open_interest=False)
    )
    assert (
        unfiltered.exclusion_counts()[ExclusionReason.OPEN_INTEREST_NOT_REPORTED.value]
        == 1
    )


def test_a_reported_zero_is_never_counted_as_an_absent_record():
    """The converse guard. Otherwise the new reason would absorb the old one."""
    chain = build_single_contract_chain(open_interest=0)
    for config in (GexEngineConfig(), GexEngineConfig(require_open_interest=False)):
        counts = compute_contract_gex(chain, config).exclusion_counts()
        assert ExclusionReason.OPEN_INTEREST_NOT_REPORTED.value not in counts
    required = compute_contract_gex(chain, GexEngineConfig())
    assert required.exclusion_counts()[ExclusionReason.NO_OPEN_INTEREST.value] == 1
    optional = compute_contract_gex(chain, GexEngineConfig(require_open_interest=False))
    assert len(optional.contracts) == 1
    assert optional.contracts[0].open_interest == 0
    assert optional.contracts[0].unsigned_gex == 0.0


def test_the_validation_report_names_the_absent_record_rather_than_a_zero():
    """The exclusion has to be auditable, not merely effective."""
    chain = build_single_contract_chain(open_interest=None)
    report = validate_chain(chain).report
    assert report.count(ValidationCode.MISSING_OPEN_INTEREST) == 1
    issue = next(
        i for i in report.examples if i.code is ValidationCode.MISSING_OPEN_INTEREST
    )
    assert issue.observed == OpenInterestObservationState.OI_NOT_REPORTED.value
    assert "not a zero" in issue.detail


# --- Temporal eligibility ----------------------------------------------------


def test_temporal_eligibility_is_a_strict_inequality():
    """Requirements 5 and 6 as one statement about the rule itself."""
    assert (
        temporal_eligibility(date(2026, 3, 16), market_session_date=SESSION)
        is TemporalEligibility.EXPIRED_BEFORE_SESSION
    )
    assert (
        temporal_eligibility(date(2026, 3, 17), market_session_date=SESSION)
        is TemporalEligibility.EXPIRING_THIS_SESSION
    )
    assert (
        temporal_eligibility(date(2026, 3, 18), market_session_date=SESSION)
        is TemporalEligibility.CURRENT
    )
    assert not TemporalEligibility.EXPIRED_BEFORE_SESSION.is_eligible
    assert TemporalEligibility.EXPIRING_THIS_SESSION.is_eligible
    assert TemporalEligibility.CURRENT.is_eligible


def test_a_contract_that_expired_before_this_session_cannot_contribute():
    """Requirement 5.

    Its open interest is positive and its book is quotable; it is out because
    the contract stopped existing before this session opened, which is a fact
    about the calendar and not about the data quality of the row.
    """
    chain = eligibility_chain()
    assert by_key(chain)[RETAINED_CALL].open_interest == 3300
    result = compute_contract_gex(chain)
    surviving = {c.contract.key for c in result.contracts}
    assert RETAINED_CALL not in surviving
    assert RETAINED_PUT not in surviving
    assert result.exclusion_counts()[ExclusionReason.EXPIRED_BEFORE_SESSION.value] == 2


def test_a_zero_dte_contract_is_not_rejected_for_expiring_today():
    """Requirement 6, and the guard that keeps this release from being a filter.

    0DTE is the series an intraday gamma model is mostly about. A rule that
    removed same-session expirations would remove the product.
    """
    result = compute_contract_gex(eligibility_chain())
    surviving = {c.contract.key for c in result.contracts}
    assert ZERO_DTE_CALL in surviving
    contribution = next(c for c in result.contracts if c.contract.key == ZERO_DTE_CALL)
    assert contribution.dte == 0
    assert contribution.open_interest == 5100
    assert contribution.unsigned_gex > 0.0


def test_staleness_is_decided_before_open_interest_availability():
    """Two findings with different remedies must not be merged into one count.

    A retained contract with a settled open interest is a vendor-retention
    artefact the next capture clears on its own. An unreported open interest is
    a settlement figure that has not arrived. Reporting the first under the
    second's reason would attribute one to the other.
    """
    assert (
        analytical_exclusion(
            expiration=date(2026, 3, 16),
            market_session_date=SESSION,
            open_interest_state=OpenInterestObservationState.OI_NOT_REPORTED,
        )
        is AnalyticalExclusion.EXPIRED_BEFORE_SESSION
    )
    assert (
        analytical_exclusion(
            expiration=date(2026, 3, 20),
            market_session_date=SESSION,
            open_interest_state=OpenInterestObservationState.OI_NOT_REPORTED,
        )
        is AnalyticalExclusion.OPEN_INTEREST_NOT_REPORTED
    )
    assert (
        analytical_exclusion(
            expiration=date(2026, 3, 20),
            market_session_date=SESSION,
            open_interest_state=OpenInterestObservationState.OI_REPORTED_ZERO,
        )
        is None
    )


def test_the_session_is_the_markets_and_not_the_calendar_day_of_the_instant():
    """A contract must not go stale an hour early because a machine holds UTC.

    22:00 ET on the 17th is the 18th in UTC. Read ``as_of.date()`` anywhere and
    a contract expiring on the 17th becomes retained-from-a-previous-session
    while the session it expires in is still the current one.
    """
    evening = datetime(2026, 3, 18, 1, 0, tzinfo=UTC)
    assert evening.date() == date(2026, 3, 18)
    assert market_session_date(evening) == SESSION
    chain = eligibility_chain(as_of=eastern(2026, 3, 17, 21, 0))
    assert chain.market_session_date == SESSION
    assert (
        temporal_eligibility(
            date(2026, 3, 17), market_session_date=chain.market_session_date
        )
        is TemporalEligibility.EXPIRING_THIS_SESSION
    )


# --- Counting and exposure ---------------------------------------------------


def test_both_exclusions_are_counted_and_exposed_side_by_side():
    """Requirement 7.

    Two surfaces, because two questions get asked. ``analytical_exclusions``
    answers "what may this session's analysis not use", over the quotes as
    supplied and before any engine filter. The engine's exclusion counts answer
    "what did this calculation drop, and why", and reach the snapshot.
    """
    chain = eligibility_chain()
    assert chain.analytical_exclusions() == {
        AnalyticalExclusion.EXPIRED_BEFORE_SESSION.value: 2,
        AnalyticalExclusion.OPEN_INTEREST_NOT_REPORTED.value: 1,
    }

    result = compute_contract_gex(chain)
    assert result.analytical_exclusions == chain.analytical_exclusions()
    counts = result.exclusion_counts()
    assert counts[ExclusionReason.EXPIRED_BEFORE_SESSION.value] == 2
    # The explicit zero is dropped too, and under its own name -- which is not
    # the absent record's name.
    assert counts[ExclusionReason.NO_OPEN_INTEREST.value] == 1
    assert ExclusionReason.OPEN_INTEREST_NOT_REPORTED.value not in counts
    # ... because validation caught it first, and said so.
    assert result.validation.count(ValidationCode.MISSING_OPEN_INTEREST) == 1

    snapshot = compute_gex_snapshot(chain)
    reasons = snapshot.chain_universe.filter_reasons
    assert reasons[ExclusionReason.EXPIRED_BEFORE_SESSION.value] == 2
    assert snapshot.meta["analytical_exclusions"] == {
        AnalyticalExclusion.EXPIRED_BEFORE_SESSION.value: 2,
        AnalyticalExclusion.OPEN_INTEREST_NOT_REPORTED.value: 1,
    }
    assert any("expired_before_session" in w for w in snapshot.warnings)


def test_an_eligible_chain_reports_no_exclusions_at_all():
    """A count that is never zero is a count nobody can read."""
    chain = build_single_contract_chain()
    assert chain.analytical_exclusions() == {}
    result = compute_contract_gex(chain)
    assert result.analytical_exclusions == {}
    counts = result.exclusion_counts()
    assert ExclusionReason.EXPIRED_BEFORE_SESSION.value not in counts
    assert ExclusionReason.OPEN_INTEREST_NOT_REPORTED.value not in counts
    # And the snapshot says nothing rather than saying zero, so a chain the
    # rules find nothing in hashes exactly as it did before they existed.
    assert "analytical_exclusions" not in compute_gex_snapshot(chain).meta


def test_an_excluded_contract_is_still_in_the_chain_and_in_the_evidence():
    """Excluded, not deleted. The exclusion has to stay auditable.

    A contract dropped at the join would be a contract nobody could reconcile
    against the vendor's response, and the response really did contain it.
    """
    chain = eligibility_chain()
    assert len(chain.quotes) == 6
    identities = {
        entry["contract_id"] for entry in canonical_chain_payload(chain)["quotes"]
    }
    assert "SPXW:2026-03-16:5000:call" in identities
    assert "SPXW:2026-03-20:5050:call" in identities
    # And it survives validation, so it is visible to a reader of the
    # normalized chain rather than only to a reader of the rejection list.
    normalized = validate_chain(chain, require_open_interest=False)
    assert len(normalized.snapshot.quotes) == 6


# --- Replay ------------------------------------------------------------------


def test_replaying_the_same_raw_bytes_reproduces_the_same_classifications():
    """Requirement 8.

    The classification is a function of the bytes, not of the run: two
    assemblies of the same fixtures produce the same states, the same
    exclusions, and the same output hash.
    """
    first, second = eligibility_chain(), eligibility_chain()
    states = [
        {q.contract.canonical_id: q.open_interest_state.value for q in chain.quotes}
        for chain in (first, second)
    ]
    assert states[0] == states[1]
    assert states[0]["SPXW:2026-03-20:5050:call"] == "OI_NOT_REPORTED"
    assert states[0]["SPXW:2026-03-20:5000:put"] == "OI_REPORTED_ZERO"
    assert states[0]["SPXW:2026-03-20:5000:call"] == "OI_REPORTED_POSITIVE"

    assert first.analytical_exclusions() == second.analytical_exclusions()
    assert canonical_chain_payload(first) == canonical_chain_payload(second)
    assert (
        compute_gex_snapshot(first).output_hash()
        == compute_gex_snapshot(second).output_hash()
    )


def test_row_order_does_not_change_a_single_classification():
    """The vendor does not promise row order, so nothing may depend on it."""
    baseline = eligibility_chain()
    shuffled = eligibility_chain(
        quote_rows=list(reversed(rows("quotes_eligibility.csv"))),
        open_interest_rows=list(reversed(rows("open_interest_eligibility.csv"))),
        first_order_rows=list(reversed(rows("greeks_first_order_eligibility.csv"))),
    )
    assert canonical_chain_payload(shuffled) == canonical_chain_payload(baseline)
    assert shuffled.analytical_exclusions() == baseline.analytical_exclusions()


# --- The trusted-calculation gate --------------------------------------------


@pytest.fixture(scope="module")
def gate():
    """A pipeline and a chain it really fetched, so the gate has provenance."""
    from tests.certification_fixtures import AS_OF as FIXTURE_AS_OF
    from tests.certification_fixtures import resolved_pipeline

    pipeline = resolved_pipeline()
    return pipeline, pipeline.fetch_chain(as_of=FIXTURE_AS_OF)


def test_the_fixture_chain_clears_the_gate_before_anything_is_changed(gate):
    """Otherwise the two refusals below would prove nothing."""
    pipeline, chain = gate
    assert pipeline.calculation_blockers(chain) == ()
    assert chain.analytical_exclusions() == {}


def test_an_unreported_open_interest_blocks_a_trusted_calculation(gate):
    """Requirement 4, at the pipeline.

    The per-contract exclusion keeps a bad weight out of the arithmetic. This
    keeps the *aggregate* from being called trusted while the universe it ran
    over was decided by a settlement figure that had not arrived.
    """
    pipeline, chain = gate
    quotes = chain.quotes
    tampered = dataclasses.replace(
        chain,
        quotes=(dataclasses.replace(quotes[0], open_interest=None), *quotes[1:]),
    )
    blockers = pipeline.calculation_blockers(tampered)
    assert any("no open-interest record" in b for b in blockers)
    assert any("not a zero" in b for b in blockers)


def test_a_retained_expired_contract_blocks_a_trusted_calculation(gate):
    """Requirement 5, at the pipeline.

    Set equality between the three snapshot endpoints is exactly what a capture
    carrying five hundred retained contracts has. It is not a current universe.
    """
    pipeline, chain = gate
    quotes = chain.quotes
    stale = dataclasses.replace(
        quotes[0].contract,
        expiry=chain.market_session_date - timedelta(days=1),
    )
    tampered = dataclasses.replace(
        chain,
        quotes=(dataclasses.replace(quotes[0], contract=stale), *quotes[1:]),
    )
    blockers = pipeline.calculation_blockers(tampered)
    assert any("expired before the" in b for b in blockers)
    assert any("does not say they agree about this session" in b for b in blockers)


def test_a_chain_answered_entirely_with_explicit_zeros_carries_open_interest(gate):
    """The truthiness bug, from the other side.

    ``not any(q.open_interest ...)`` read a chain the vendor answered in full,
    with a zero for every contract, as a chain carrying no open interest at all.
    The states distinguish them; the number's truthiness never could.
    """
    pipeline, chain = gate
    zeroed = dataclasses.replace(
        chain,
        quotes=tuple(dataclasses.replace(q, open_interest=0) for q in chain.quotes),
    )
    assert not any(
        "carries no open interest" in b for b in pipeline.calculation_blockers(zeroed)
    )
