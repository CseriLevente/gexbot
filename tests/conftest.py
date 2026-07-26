"""Shared fixtures.

Chain construction runs the Black-Scholes pricer over every strike and expiry, so
the full synthetic chain is built once per session and reused. The engine treats
``ChainSnapshot`` as immutable, so sharing it between tests is safe.
"""

from __future__ import annotations

import pytest

from src.domain.contracts import ChainSnapshot
from src.gex.config import GexEngineConfig
from src.gex.engine import compute_gex_snapshot
from src.gex.formulas import ContractGexResult, compute_contract_gex
from src.synthetic.chains import SyntheticChainSpec, build_synthetic_chain


@pytest.fixture(scope="session")
def spec() -> SyntheticChainSpec:
    return SyntheticChainSpec()


@pytest.fixture(scope="session")
def chain(spec: SyntheticChainSpec) -> ChainSnapshot:
    return build_synthetic_chain(spec)


@pytest.fixture(scope="session")
def config() -> GexEngineConfig:
    return GexEngineConfig()


@pytest.fixture(scope="session")
def contract_gex(chain: ChainSnapshot, config: GexEngineConfig) -> ContractGexResult:
    return compute_contract_gex(chain, config)


@pytest.fixture(scope="session")
def snapshot(chain: ChainSnapshot, config: GexEngineConfig):
    return compute_gex_snapshot(chain, config)
