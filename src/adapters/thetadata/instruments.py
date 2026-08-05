"""Which symbol goes to which endpoint.

The v2.1.15 defect, reproduced against the exact profile the first paid session
was going to run:

    option chain symbol: SPXW
    index price request: symbol=SPXW

``SPXW`` is an option **root** -- the Wednesday/weekly-settled SPX options
series. ``SPX`` is the **index** those options are written on. They are not two
spellings of one thing, and ``/v3/index/snapshot/price?symbol=SPXW`` is a
request for the price of an instrument that does not exist. Every gamma in the
resulting chain is computed against whatever that request returned.

One symbol was used for both because a ``ChainRequest`` carries a single
``symbol`` and the index fetch reached for it. The fix is not a string rule --
"strip a trailing W" would silently produce ``SP`` from ``SPW`` and would invent
an answer for a root nobody has modelled. It is an explicit table, one entry per
root this repository is prepared to make a claim about, and a refusal for
anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "INSTRUMENT_MAPPINGS",
    "InstrumentMapping",
    "UnknownInstrumentRootError",
    "mapping_for",
]


class UnknownInstrumentRootError(ValueError):
    """A root with no declared underlying index.

    Raised rather than guessed. A capture taken against an index symbol this
    repository derived by string manipulation would carry a spot nobody chose,
    and the spot is what every gamma is divided by.
    """


@dataclass(frozen=True, slots=True)
class InstrumentMapping:
    """One option root and the index its options are written on.

    Both are part of the pipeline's identity. A capture taken with
    ``underlying_index_symbol="SPX"`` and one taken with ``"SPXW"`` are captures
    of different things, and until v2.1.16 they produced the same fingerprint --
    so a corrected mapping could have reused an old capture's evidence.
    """

    option_symbol: str
    underlying_index_symbol: str

    def __post_init__(self) -> None:
        for name in ("option_symbol", "underlying_index_symbol"):
            value = getattr(self, name)
            if not isinstance(value, str) or value != value.strip() or not value:
                raise UnknownInstrumentRootError(
                    f"{name} must be a non-empty untrimmed-free string, got {value!r}"
                )

    def symbol_for(self, endpoint: Any) -> str:
        """The symbol this endpoint must be asked with.

        The index snapshot -- and only the index snapshot -- takes the
        underlying. Everything else is an option-market request and takes the
        root.
        """
        from src.adapters.thetadata.endpoints import INDEX_ENDPOINTS

        value = getattr(endpoint, "value", endpoint)
        if value in INDEX_ENDPOINTS:
            return self.underlying_index_symbol
        return self.option_symbol

    def semantic_payload(self) -> dict[str, str]:
        """Both symbols, so a fingerprint covers the distinction."""
        return {
            "option_symbol": self.option_symbol,
            "underlying_index_symbol": self.underlying_index_symbol,
        }

    def as_dict(self) -> dict[str, str]:
        return self.semantic_payload()


#: The roots this repository is prepared to make a claim about, and nothing
#: else. Adding an entry is a deliberate act: it asserts that the options with
#: that root are written on that index, which is a statement about the market
#: and not about spelling.
#:
#: ``SPX`` maps to itself because the AM-settled monthly root *is* the index
#: root; ``SPXW`` is the PM-settled weekly series on the same index.
INSTRUMENT_MAPPINGS: Final[dict[str, InstrumentMapping]] = {
    "SPX": InstrumentMapping(option_symbol="SPX", underlying_index_symbol="SPX"),
    "SPXW": InstrumentMapping(option_symbol="SPXW", underlying_index_symbol="SPX"),
}


def mapping_for(option_symbol: str) -> InstrumentMapping:
    """The declared mapping for a root, or a refusal naming what is modelled.

    No fallback. A root that is not in the table is one nobody has decided the
    underlying for, and defaulting to "the same symbol" is exactly the defect:
    ``SPXW`` defaulted to itself and the index request asked for a price that
    does not exist.
    """
    root = str(option_symbol).strip()
    try:
        return INSTRUMENT_MAPPINGS[root]
    except KeyError:
        raise UnknownInstrumentRootError(
            f"no underlying index is declared for option root {root!r}. This "
            f"repository maps {sorted(INSTRUMENT_MAPPINGS)}. Add an entry "
            "deliberately -- an index symbol derived by trimming characters "
            "would put an unchosen spot under every gamma in the chain."
        ) from None
