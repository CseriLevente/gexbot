"""A working OptionsDataSource with no vendor account.

Lets the whole pipeline run end-to-end today: engine, features, regime, strategy
and risk can all be built and replayed against this before a single subscription
is bought. When a real adapter arrives, the only thing that changes is which
implementation gets injected.
"""

from __future__ import annotations

from datetime import datetime

from src.domain.contracts import ChainSnapshot, OptionRoot
from tests.fixtures.chains import SyntheticChainSpec, build_synthetic_chain


class SyntheticOptionsDataSource:
    """Deterministic chain generator satisfying :class:`OptionsDataSource`."""

    name = "synthetic"

    def __init__(self, spec: SyntheticChainSpec | None = None) -> None:
        self._spec = spec or SyntheticChainSpec()

    def fetch_chain(
        self,
        *,
        as_of: datetime,
        roots: tuple[OptionRoot, ...] = (OptionRoot.SPX, OptionRoot.SPXW),
        max_dte: int | None = None,
    ) -> ChainSnapshot:
        from dataclasses import replace

        spec = replace(
            self._spec,
            as_of=as_of,
            expiries=tuple(
                (root, expiry)
                for root, expiry in self._spec.expiries
                if root in roots
            ),
        )
        chain = build_synthetic_chain(spec)
        return chain if max_dte is None else chain.filter(max_dte=max_dte)

    def expected_contract_count(
        self,
        *,
        as_of: datetime,
        roots: tuple[OptionRoot, ...] = (OptionRoot.SPX, OptionRoot.SPXW),
        max_dte: int | None = None,
    ) -> int | None:
        return len(
            self.fetch_chain(as_of=as_of, roots=roots, max_dte=max_dte).quotes
        )
