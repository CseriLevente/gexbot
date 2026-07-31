"""Identity completeness must survive the trip to the score.

v2.1.2 taught the adapter to measure completeness by identity: matched, missing
and unexpected contract identities, with a ratio over the *expected set*. Then
``score_chain_completeness`` threw that away and recomputed::

    ratio = len(result.contracts) / expected_contract_count

which is the count arithmetic the identity work replaced. A chain that received
two contracts where two were expected -- at entirely different strikes -- scored
1.0, because two over two is one.

The same reconstruction had a second entrance: ``compute_gex_snapshot`` accepted
``expected_contract_count: int``. A count cannot express which contracts were
expected, so no integer override can establish measured completeness.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from src.adapters.thetadata.client import assemble_chain
from src.domain.completeness import CompletenessStatus
from src.gex.confidence import score_chain_completeness
from src.gex.config import ConfidenceConfig
from src.gex.engine import compute_gex_snapshot
from src.gex.sessions import eastern
from tests.unit.test_chain_completeness import build, cid
from tests.unit.test_completeness_confidence import confidence_inputs

AS_OF = eastern(2026, 3, 17, 11, 0)


def score_for(snapshot, **overrides):
    return score_chain_completeness(
        confidence_inputs(snapshot, **overrides), ConfidenceConfig()
    )


# =============================================================================
# §8 -- the score reads identities
# =============================================================================


def test_equal_counts_with_no_matching_identities_scores_zero():
    """The regression. Two expected, two received, none of them the same."""
    expected = tuple(cid(k) for k in (4900, 4910))
    snapshot = assemble_chain(
        build([5100, 5110], expected=expected, source="contract_list")
    )
    assert snapshot.completeness_status is CompletenessStatus.MEASURED_INCOMPLETE
    component = score_for(snapshot)
    assert component.score == pytest.approx(0.0)


def test_equal_counts_alone_cannot_produce_full_confidence():
    expected = tuple(cid(k) for k in (4900, 4910))
    snapshot = assemble_chain(
        build([5100, 5110], expected=expected, source="contract_list")
    )
    assert score_for(snapshot).score != pytest.approx(1.0)


def test_one_missing_and_one_unexpected_remains_incomplete():
    expected = tuple(cid(k) for k in (4900, 4910))
    snapshot = assemble_chain(
        build([4900, 5100], expected=expected, source="contract_list")
    )
    component = score_for(snapshot)
    assert component.score < 1.0


def test_extras_do_not_erase_a_missing_expected_identity():
    expected = tuple(cid(k) for k in (4900, 4910))
    snapshot = assemble_chain(
        build([4900, 5100, 5110], expected=expected, source="contract_list")
    )
    assert score_for(snapshot).score < 1.0


def test_a_genuinely_complete_chain_still_scores_full_marks():
    expected = tuple(cid(k) for k in (4900, 4910))
    snapshot = assemble_chain(
        build([4900, 4910], expected=expected, source="contract_list")
    )
    assert score_for(snapshot).score == pytest.approx(1.0)


def test_the_detail_reports_identity_metrics():
    expected = tuple(cid(k) for k in (4900, 4910))
    snapshot = assemble_chain(
        build([4900, 5100], expected=expected, source="contract_list")
    )
    detail = score_for(snapshot).detail
    assert "identit" in detail.lower()
    assert "missing" in detail.lower()


def test_the_scorer_no_longer_divides_contract_counts():
    """Guard the guard: the arithmetic must not creep back."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(score_chain_completeness)))
    body = ast.unparse(ast.Module(body=tree.body[0].body[1:], type_ignores=[]))
    # The count may still be *reported*; it must not be *divided* to
    # manufacture a completeness ratio.
    assert "len(result.contracts) /" not in body
    assert "/ expected" not in body


def test_the_typed_completeness_object_reaches_the_scorer():
    snapshot = assemble_chain(
        build([4900], expected=(cid(4900),), source="contract_list")
    )
    inputs = confidence_inputs(snapshot)
    assert inputs.chain_completeness is not None
    assert inputs.chain_completeness.identity_completeness_ratio is not None


# =============================================================================
# §9 -- no integer-only expected universe
# =============================================================================


def test_an_integer_override_cannot_claim_completeness():
    """A count says nothing about which contracts were expected."""
    from src.synthetic.chains import build_synthetic_chain

    chain = build_synthetic_chain().with_completeness(CompletenessStatus.UNKNOWN)
    with pytest.raises(TypeError):
        compute_gex_snapshot(chain, expected_contract_count=250)


def test_a_typed_universe_can_establish_measured_completeness():
    from src.domain.completeness import ExpectedContractUniverse

    identities = tuple(cid(k) for k in (4900, 4910))
    universe = ExpectedContractUniverse(
        identities=frozenset(identities),
        source="contract_list",
        observed_at=datetime.now(tz=AS_OF.tzinfo),
    )
    snapshot = compute_gex_snapshot(
        assemble_chain(build([4900, 4910])), expected_universe=universe
    )
    assert snapshot.meta["chain_completeness"]["status"] == "MEASURED_COMPLETE"


def test_a_typed_universe_with_wrong_identities_stays_incomplete():
    from src.domain.completeness import ExpectedContractUniverse

    universe = ExpectedContractUniverse(
        identities=frozenset(cid(k) for k in (5100, 5110)),
        source="contract_list",
        observed_at=datetime.now(tz=AS_OF.tzinfo),
    )
    snapshot = compute_gex_snapshot(
        assemble_chain(build([4900, 4910])), expected_universe=universe
    )
    assert snapshot.meta["chain_completeness"]["status"] == "MEASURED_INCOMPLETE"


# =============================================================================
# §10 -- per-source shortfalls use identity differences
# =============================================================================


def test_two_missing_and_two_unexpected_reports_two_missing():
    """Count arithmetic nets these to zero; identity differences do not."""
    expected = tuple(cid(k) for k in (4900, 4910, 4920, 4930))
    snapshot = assemble_chain(
        build([4900, 4910, 5100, 5110], expected=expected, source="contract_list")
    )
    payload = snapshot.meta["chain_completeness"]
    assert payload["missing_expected_count"] == 2
    assert payload["unexpected_received_count"] == 2


def test_per_source_missing_uses_identities():
    from datetime import date as _date

    from src.adapters.thetadata.client import ChainAssemblyInputs
    from tests.unit.test_chain_completeness import greek_row, oi_row, quote_row

    expected = tuple(cid(k) for k in (4900, 4910))
    inputs = ChainAssemblyInputs(
        as_of=AS_OF,
        spot=5000.25,
        quote_rows=[quote_row(k) for k in (4900, 4910)],
        open_interest_rows=[oi_row(4900)],
        first_order_rows=[greek_row(k) for k in (4900, 4910)],
        open_interest_as_of=_date(2026, 3, 16),
        expected_contract_ids=expected,
        expected_source="contract_list",
    )
    payload = assemble_chain(inputs).meta["chain_completeness"]
    assert payload["missing_by_source"]["open_interest"] == 1


def test_missing_identities_are_listed_per_source():
    expected = tuple(cid(k) for k in (4900, 4910, 4920))
    snapshot = assemble_chain(build([4900], expected=expected, source="contract_list"))
    payload = snapshot.meta["chain_completeness"]
    assert payload["missing_expected_identities"] == [cid(4910), cid(4920)]


# =============================================================================
# §20 -- expected identities are typed and normalised the same way
# =============================================================================


def test_equivalent_strike_spellings_produce_one_expected_identity():
    from src.domain.completeness import ExpectedContractUniverse, contract_identity

    a = contract_identity(
        symbol="SPXW", expiry="2026-03-20", strike="5000", right="call"
    )
    b = contract_identity(
        symbol="SPXW", expiry="2026-03-20", strike="5000.00", right="call"
    )
    assert a == b
    universe = ExpectedContractUniverse(
        identities=frozenset({a, b}), source="contract_list", observed_at=AS_OF
    )
    assert len(universe.identities) == 1


def test_expected_and_received_identities_use_the_same_form():
    from src.domain.completeness import contract_identity

    built = contract_identity(
        symbol="SPXW", expiry="2026-03-20", strike="4900", right="call"
    )
    assert built == cid(4900)


@pytest.mark.parametrize("strike", ["NaN", "inf", "-inf", "abc", "", "-1", "0"])
def test_a_bad_expected_strike_is_refused(strike):
    from src.domain.completeness import contract_identity

    with pytest.raises(ValueError):
        contract_identity(
            symbol="SPXW", expiry="2026-03-20", strike=strike, right="call"
        )


def test_formatting_cannot_manufacture_a_false_missing_identity():
    """The failure this normalisation prevents: an expected "5000.00" and a
    received "5000" reading as one missing and one unexpected."""
    from src.domain.completeness import ExpectedContractUniverse, contract_identity

    universe = ExpectedContractUniverse(
        identities=frozenset(
            {
                contract_identity(
                    symbol="SPXW", expiry="2026-03-20", strike="4900.00", right="call"
                )
            }
        ),
        source="contract_list",
        observed_at=AS_OF,
    )
    snapshot = compute_gex_snapshot(
        assemble_chain(build([4900])), expected_universe=universe
    )
    assert snapshot.meta["chain_completeness"]["missing_expected_count"] == 0


def test_open_interest_as_of_is_unaffected():
    snapshot = assemble_chain(build([4900, 4910]))
    assert snapshot.quotes[0].timestamps.open_interest_as_of == date(2026, 3, 16)
