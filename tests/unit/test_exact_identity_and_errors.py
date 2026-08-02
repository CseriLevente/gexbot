"""Exact strike identity in the domain, and one exception hierarchy.

**§13.** ``OptionContract.strike`` was the only representation, and it is a
float. Identity was recovered from it with ``str(float)``, which is the shortest
round-tripping spelling -- so two strikes that differ beyond double precision
arrive as the same contract. The parser reads the strike exactly, as a
``Decimal``, and then threw that exactness away one layer later.

**§16.** ``assess_readiness`` raised bare ``TypeError`` for an untyped capture,
and ``ProvenanceEvidence`` raised a ``ValueError`` subclass that was not part of
the adapter hierarchy. A caller handling "the adapter refused" had to enumerate
builtins.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.adapters.errors import ThetaDataError
from src.domain.contracts import OptionContract, OptionRight, OptionRoot

# =============================================================================
# §13 -- the exact strike survives domain construction
# =============================================================================

#: Two strikes a double cannot tell apart. ``float`` collapses both onto
#: 5000.0, so any identity derived from the float alone merges them.
NEAR_A = "5000.0000000000000001"
NEAR_B = "5000.0000000000000002"


def contract(strike: str) -> OptionContract:
    return OptionContract(
        root=OptionRoot.SPXW,
        expiry=date(2026, 3, 20),
        strike=float(strike),
        right=OptionRight.CALL,
        strike_decimal=Decimal(strike),
    )


def test_the_two_strikes_are_indistinguishable_as_floats():
    """The premise. If this ever fails the rest of the file proves nothing."""
    assert float(NEAR_A) == float(NEAR_B)


def test_high_precision_strikes_stay_distinct_after_construction():
    assert contract(NEAR_A).canonical_id != contract(NEAR_B).canonical_id


def test_high_precision_strikes_stay_distinct_as_dictionary_keys():
    ladder = {contract(NEAR_A).key: "a", contract(NEAR_B).key: "b"}
    assert len(ladder) == 2


def test_the_exact_strike_is_carried_not_recovered():
    """Recovering it from the float is what loses it."""
    assert contract(NEAR_A).strike_decimal == Decimal(NEAR_A)


def test_a_contract_without_an_exact_strike_still_works():
    """Existing construction sites pass a float and must keep working."""
    plain = OptionContract(
        root=OptionRoot.SPXW,
        expiry=date(2026, 3, 20),
        strike=4900.5,
        right=OptionRight.CALL,
    )
    assert plain.canonical_id.endswith(":4900.5:call")
    assert plain.strike_decimal == Decimal("4900.5")


def test_an_exact_strike_disagreeing_with_the_float_is_refused():
    """Two representations of one number must not be able to disagree."""
    from src.domain.strikes import StrikeError

    with pytest.raises(StrikeError, match=r"disagree"):
        OptionContract(
            root=OptionRoot.SPXW,
            expiry=date(2026, 3, 20),
            strike=4900.5,
            right=OptionRight.CALL,
            strike_decimal=Decimal("5100.25"),
        )


def test_the_float_is_still_available_for_the_maths():
    built = contract(NEAR_A)
    assert isinstance(built.strike, float)
    assert built.strike == pytest.approx(5000.0)


def test_serialisation_carries_the_exact_strike():
    """``canonical_id`` is how a contract identity leaves the process.

    It reaches the completeness measure, the snapshot metadata and the replay
    hash, so an identity that has already lost precision loses it everywhere.
    """
    assert contract(NEAR_A).canonical_id.endswith(f":{NEAR_A}:call")


def test_the_expected_universe_and_the_contract_agree_on_the_exact_strike():
    """Both sides of the join, spelled by the same formatter from the same value."""
    from src.domain.completeness import contract_identity

    assert contract(NEAR_A).canonical_id == contract_identity(
        symbol="SPXW", expiry="2026-03-20", strike=NEAR_A, right="call"
    )


# =============================================================================
# §16 -- certification and provenance failures join the hierarchy
# =============================================================================


@pytest.mark.parametrize(
    "name",
    [
        "ThetaDataCertificationError",
        "ThetaDataProvenanceError",
        "ThetaDataValidationError",
    ],
)
def test_the_new_failures_are_thetadata_failures(name):
    import src.adapters.errors as errors

    assert issubclass(getattr(errors, name), ThetaDataError)


def test_provenance_evidence_raises_a_structured_error():
    from src.adapters.certification import VerifiedFieldObservation
    from src.adapters.errors import ThetaDataProvenanceError

    with pytest.raises(ThetaDataProvenanceError):
        VerifiedFieldObservation(
            record_id="",
            endpoint="/v3/x",
            payload_hash="0" * 64,
            parser_version="thetadata-v3-parser/2.1.7",
            field_path="open_interest",
            observed_value=1,
        )


def test_readiness_refuses_an_untyped_store_with_a_structured_error():
    """A bare TypeError from a public certification API is not a contract."""
    from src.adapters.errors import ThetaDataCertificationError

    assert issubclass(ThetaDataCertificationError, ThetaDataError)
    assert not issubclass(ThetaDataCertificationError, TypeError)
