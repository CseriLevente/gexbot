"""A contract cannot contribute GEX without a valid selected spot.

The v2.1 defect. ``_resolve_underlying`` recorded ``UNDERLYING_MISSING`` and
then returned ``snapshot.spot`` anyway, with a comment stating it was
"deliberately NOT falling back to the snapshot spot". The issue was recorded
faithfully and nothing downstream read it: ``compute_contract_gex`` excluded on
expiry, DTE, open interest and gamma, but never on the spot, so a contract whose
selected underlying source produced nothing was priced against a chain-level
spot the operator had explicitly not selected.

    GEX = gamma x OI x multiplier x spot^2 x 0.01

The spot enters squared. Substituting a different one is not a rounding
difference; it silently reprices the contract.

Eligibility is per-purpose. A contract with a vendor gamma but no per-contract
underlying can still be compared against our shadow gamma -- it just cannot
contribute a current GEX number.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from src.domain.effective_model import ResolutionIssue, resolve_effective_inputs
from src.domain.model_spec import ModelSpec, UnderlyingPriceSource
from src.gex.formulas import ExclusionReason, compute_contract_gex
from src.gex.sessions import eastern
from src.synthetic.chains import build_single_contract_chain, with_quote

AS_OF = eastern(2026, 3, 17, 11, 0)
PER_CONTRACT = ModelSpec(
    underlying_price_source=UnderlyingPriceSource.VENDOR_PER_CONTRACT
)


def chain(**quote_overrides):
    base = build_single_contract_chain(as_of=AS_OF)
    if not quote_overrides:
        return base
    return with_quote(base, 0, **quote_overrides)


def resolve(snapshot, spec=PER_CONTRACT):
    return resolve_effective_inputs(
        quote=snapshot.quotes[0], snapshot=snapshot, spec=spec
    )


def gex_for(snapshot, spec=PER_CONTRACT):
    from src.gex.config import GexEngineConfig

    return compute_contract_gex(snapshot, GexEngineConfig(model_spec=spec))


# =============================================================================
# §4 -- no placeholder spot
# =============================================================================


def test_a_missing_per_contract_underlying_yields_no_spot():
    """v2.1 returned ``snapshot.spot`` here while recording the issue."""
    resolved = resolve(chain(underlying_price=None))
    assert ResolutionIssue.UNDERLYING_MISSING in resolved.issues
    assert resolved.spot is None


def test_a_missing_underlying_excludes_the_contract_from_current_gex():
    result = gex_for(chain(underlying_price=None))
    assert result.contracts == ()
    assert result.exclusion_counts()[ExclusionReason.NO_UNDERLYING_PRICE.value] == 1


def test_a_non_finite_underlying_excludes_the_contract():
    for bad in (float("nan"), float("inf"), float("-inf")):
        result = gex_for(chain(underlying_price=bad))
        assert result.contracts == (), bad


def test_a_non_positive_underlying_excludes_the_contract():
    for bad in (0.0, -1.0):
        assert gex_for(chain(underlying_price=bad)).contracts == (), bad


def test_vendor_gamma_does_not_bypass_the_spot_requirement():
    """A usable gamma is not a licence to invent the other factor."""
    snapshot = chain(underlying_price=None, gamma=0.0021)
    result = gex_for(snapshot)
    assert result.contracts == ()


def test_the_exclusion_is_machine_readable():
    counts = gex_for(chain(underlying_price=None)).exclusion_counts()
    assert ExclusionReason.NO_UNDERLYING_PRICE.value in counts


def test_the_exclusion_appears_in_universe_accounting():
    """An excluded contract must be counted, not silently absent."""
    result = gex_for(chain(underlying_price=None))
    counts = result.exclusion_counts()
    assert counts[ExclusionReason.NO_UNDERLYING_PRICE.value] == 1
    assert result.total_quotes == 1
    assert result.usable_ratio == 0.0


def test_a_present_per_contract_underlying_still_works():
    """The fix must not exclude contracts that are genuinely fine."""
    result = gex_for(chain(underlying_price=5001.5))
    assert len(result.contracts) == 1
    assert result.contracts[0].effective.spot == pytest.approx(5001.5)


def test_the_snapshot_source_still_uses_the_chain_spot():
    """Only VENDOR_PER_CONTRACT is per-contract; the others are unaffected."""
    snapshot = chain(underlying_price=None)
    spec = ModelSpec(
        underlying_price_source=UnderlyingPriceSource.VENDOR_INDEX_SNAPSHOT
    )
    resolved = resolve(snapshot, spec)
    assert resolved.spot == pytest.approx(snapshot.spot)
    assert len(gex_for(snapshot, spec).contracts) == 1


def test_no_fallback_happens_without_an_explicit_policy():
    """There is no configured fallback source, so none is applied."""
    resolved = resolve(chain(underlying_price=None))
    assert resolved.spot is None
    assert resolved.underlying_price_source is UnderlyingPriceSource.VENDOR_PER_CONTRACT


def test_the_recorded_reason_names_the_source_that_failed():
    resolved = resolve(chain(underlying_price=None))
    assert any("vendor_per_contract" in entry for entry in resolved.missing_inputs)


# =============================================================================
# Eligibility is per-purpose
# =============================================================================


def test_eligibility_is_reported_separately_for_each_purpose():
    """A contract may be eligible for one purpose and not another.

    No selected spot means no *current* GEX and no local gamma at the current
    spot. Zero-gamma repricing is a different question: the grid supplies the
    spot, so every other input having resolved is enough.
    """
    resolved = resolve(chain(underlying_price=None))
    assert not resolved.eligible_for_current_gex
    assert not resolved.eligible_for_local_gamma
    assert not resolved.eligible_for_vendor_gamma_comparison
    assert resolved.eligible_for_zero_gamma_repricing


def test_repricing_stops_when_a_non_spot_input_is_missing():
    """Repricing replaces the spot; it cannot replace a missing IV."""
    from src.domain.iv import missing_iv

    resolved = resolve(chain(underlying_price=None, iv=missing_iv()))
    assert not resolved.eligible_for_zero_gamma_repricing


def test_a_fully_resolved_contract_is_eligible_for_everything():
    resolved = resolve(chain(underlying_price=5001.5))
    assert resolved.eligible_for_current_gex
    assert resolved.eligible_for_local_gamma
    assert resolved.eligible_for_zero_gamma_repricing


def test_vendor_comparison_survives_a_missing_spot_only_when_gamma_exists():
    """The comparison needs both sides; a missing spot kills the local side."""
    assert not resolve(
        chain(underlying_price=None)
    ).eligible_for_vendor_gamma_comparison


def test_pricing_with_an_unresolved_spot_raises_rather_than_guessing():
    from src.domain.effective_model import ModelResolutionError

    with pytest.raises(ModelResolutionError):
        resolve(chain(underlying_price=None)).gamma()


def test_a_placeholder_spot_never_reaches_the_arithmetic():
    """The property the section exists for, stated directly."""
    result = gex_for(chain(underlying_price=None))
    assert all(
        contract.effective.spot is not None and math.isfinite(contract.effective.spot)
        for contract in result.contracts
    )


# =============================================================================
# §6 -- completeness from provenance, not from the value being zero
# =============================================================================

from src.domain.model_spec import DividendSource, RateSource  # noqa: E402


@pytest.mark.parametrize(
    ("source", "value", "complete"),
    [
        (RateSource.ZERO, 0.0, True),
        (RateSource.ZERO, None, True),
        (RateSource.CONFIGURED_CONSTANT, 0.0, True),
        (RateSource.CONFIGURED_CONSTANT, None, False),
        (RateSource.SNAPSHOT, 0.0, True),
    ],
)
def test_rate_completeness_follows_the_source(source, value, complete):
    """An explicit zero is a decision, not an absence."""
    spec = ModelSpec(risk_free_rate_source=source, risk_free_rate=value)
    assert resolve(chain(), spec).is_fully_specified is complete


def test_snapshot_rate_absent_is_incomplete():
    snapshot = replace(build_single_contract_chain(as_of=AS_OF), risk_free_rate=None)
    spec = ModelSpec(risk_free_rate_source=RateSource.SNAPSHOT)
    assert not resolve(snapshot, spec).is_fully_specified


@pytest.mark.parametrize(
    ("source", "value", "complete"),
    [
        (DividendSource.ZERO, 0.0, True),
        (DividendSource.CONFIGURED_CONSTANT, 0.0, True),
        (DividendSource.CONFIGURED_CONSTANT, None, False),
        (DividendSource.VENDOR_ANNUAL_DIVIDEND, None, False),
    ],
)
def test_dividend_completeness_follows_the_source(source, value, complete):
    spec = ModelSpec(dividend_yield_source=source, dividend_yield=value)
    assert resolve(chain(), spec).is_fully_specified is complete


def test_a_zero_rate_is_complete_but_flagged_as_unusual():
    """Completeness and realism are different questions and get different fields."""
    resolved = resolve(chain(), ModelSpec(risk_free_rate_source=RateSource.ZERO))
    assert resolved.is_fully_specified
    assert "MODEL_REALISM_WARNING" in [w.code for w in resolved.realism_warnings]


def test_a_realistic_rate_produces_no_realism_warning():
    spec = ModelSpec(
        risk_free_rate_source=RateSource.CONFIGURED_CONSTANT,
        risk_free_rate=0.042,
        dividend_yield_source=DividendSource.CONFIGURED_CONSTANT,
        dividend_yield=0.013,
    )
    assert resolve(chain(), spec).realism_warnings == ()


def test_realism_is_not_reported_as_missing():
    """The v2.1-adjacent trap: calling an intentional zero "missing"."""
    resolved = resolve(chain(), ModelSpec(risk_free_rate_source=RateSource.ZERO))
    assert not any("risk_free_rate" in entry for entry in resolved.missing_inputs)


def test_the_confidence_component_reads_provenance_not_values():
    """model_parameter_completeness must not recompute completeness itself."""
    import ast
    import inspect
    import textwrap

    from src.gex.confidence import score_model_parameter_completeness

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(score_model_parameter_completeness))
    )
    function = tree.body[0]
    # Drop the docstring: it *describes* the defect, which is not the same as
    # committing it.
    body = ast.unparse(ast.Module(body=function.body[1:], type_ignores=[]))
    assert "== 0.0" not in body
    assert "!= 0.0" not in body
