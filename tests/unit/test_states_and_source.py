"""Research-stage enum and the synthetic data source.

Small modules, but both are production code with real invariants: the stage
ladder must not allow skipping PAPER, and the synthetic source must satisfy the
same Protocol a real vendor adapter will.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.adapters.base import OptionsDataSource
from src.adapters.synthetic.source import SyntheticOptionsDataSource
from src.domain.contracts import OptionRoot
from src.domain.states import Regime, ResearchStage, next_stage
from src.gex.engine import compute_gex_snapshot
from src.gex.sessions import eastern
from src.synthetic.chains import SyntheticChainSpec

AS_OF = eastern(2026, 3, 17, 11, 0)


# --- Regime -----------------------------------------------------------------


def test_only_the_two_directional_regimes_are_tradeable():
    assert Regime.POSITIVE_GAMMA.is_tradeable
    assert Regime.NEGATIVE_GAMMA.is_tradeable
    for regime in (
        Regime.NEUTRAL,
        Regime.UNCERTAIN,
        Regime.DATA_HALT,
        Regime.RISK_HALT,
    ):
        assert not regime.is_tradeable


def test_halt_states_are_identified():
    assert Regime.DATA_HALT.is_halt
    assert Regime.RISK_HALT.is_halt
    assert not Regime.NEUTRAL.is_halt


def test_the_regime_enum_is_closed():
    """Anything outside these six values is a bug, not a new regime."""
    assert {r.value for r in Regime} == {
        "POSITIVE_GAMMA",
        "NEGATIVE_GAMMA",
        "NEUTRAL",
        "UNCERTAIN",
        "DATA_HALT",
        "RISK_HALT",
    }


# --- Research stages --------------------------------------------------------


def test_stage_ladder_advances_one_step_at_a_time():
    assert next_stage(ResearchStage.DEVELOPMENT) is ResearchStage.VALIDATION
    assert next_stage(ResearchStage.VALIDATION) is ResearchStage.OOS
    assert next_stage(ResearchStage.OOS) is ResearchStage.PAPER
    assert next_stage(ResearchStage.PAPER) is ResearchStage.LIVE_STAGE_1
    assert next_stage(ResearchStage.LIVE_STAGE_1) is ResearchStage.LIVE_STAGE_2


def test_the_ladder_terminates():
    assert next_stage(ResearchStage.LIVE_STAGE_2) is None


def test_paper_cannot_be_skipped_on_the_way_to_live():
    """Structural, not procedural: OOS's only successor is PAPER."""
    assert next_stage(ResearchStage.OOS) is ResearchStage.PAPER
    assert not ResearchStage.OOS.is_live
    assert not ResearchStage.PAPER.is_live


def test_live_stages_are_identified():
    assert ResearchStage.LIVE_STAGE_1.is_live
    assert ResearchStage.LIVE_STAGE_2.is_live
    for stage in (
        ResearchStage.DEVELOPMENT,
        ResearchStage.VALIDATION,
        ResearchStage.OOS,
        ResearchStage.PAPER,
    ):
        assert not stage.is_live


# --- Synthetic data source --------------------------------------------------


def test_synthetic_source_satisfies_the_options_protocol():
    """It must be substitutable for a real vendor adapter, or the offline tests
    are exercising a different shape from production.
    """
    assert isinstance(SyntheticOptionsDataSource(), OptionsDataSource)


def test_source_produces_a_usable_chain():
    source = SyntheticOptionsDataSource()
    chain = source.fetch_chain(as_of=AS_OF)
    assert chain.quotes
    assert chain.as_of == AS_OF
    assert compute_gex_snapshot(chain).contract_count > 0


def test_source_reports_the_expected_contract_count():
    source = SyntheticOptionsDataSource()
    assert source.expected_contract_count(as_of=AS_OF) == len(
        source.fetch_chain(as_of=AS_OF).quotes
    )


def test_source_filters_by_root():
    source = SyntheticOptionsDataSource()
    spxw_only = source.fetch_chain(as_of=AS_OF, roots=(OptionRoot.SPXW,))
    assert {q.contract.root for q in spxw_only.quotes} == {OptionRoot.SPXW}


def test_source_filters_by_max_dte():
    source = SyntheticOptionsDataSource()
    near = source.fetch_chain(as_of=AS_OF, max_dte=2)
    assert near.quotes
    assert all((q.contract.expiry - AS_OF.date()).days <= 2 for q in near.quotes)
    assert len(near.quotes) < len(source.fetch_chain(as_of=AS_OF).quotes)


def test_source_honours_a_custom_spec():
    spec = SyntheticChainSpec(
        spot=4000.0, expiries=((OptionRoot.SPXW, date(2026, 3, 31)),)
    )
    chain = SyntheticOptionsDataSource(spec).fetch_chain(as_of=AS_OF)
    assert chain.spot == pytest.approx(4000.0)
    assert chain.expiries == (date(2026, 3, 31),)


def test_source_is_deterministic():
    source = SyntheticOptionsDataSource()
    first = compute_gex_snapshot(source.fetch_chain(as_of=AS_OF))
    second = compute_gex_snapshot(source.fetch_chain(as_of=AS_OF))
    assert first.output_hash() == second.output_hash()
