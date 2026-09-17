"""A scripted vendor for offline collector tests. Nothing here is market data.

``SyntheticFeed`` answers the five v3 endpoints with native-schema bodies
generated from a :class:`FakeClock` at request time, so every cycle the
collector takes sees fresh vendor timestamps and receipts on the same fake
clock. It is labelled ``OFFLINE_FIXTURE`` like every fake transport, so the
captures, the assembled events and every report say ``SYNTHETIC``.

The feed learns which cycle it is serving from the session root: the one-shot
claims ``cycles/<HHMMSS>`` before it issues a request, so the newest cycle
directory is the cycle in flight. That keeps the script keyed by slot label
even when a scripted delay moves the clock past later boundaries.

The script (all knobs optional):

- ``fail`` -- ``{label: {endpoint, ...}}``: those requests answer HTTP 404 on
  that cycle, which the one-shot records as ``VENDOR_REFUSED`` and continues.
- ``reject`` -- cycle labels answered HTTP 401 throughout: the one-shot stops
  its sweep with ``AUTHENTICATION_REJECTED`` and so does the session.
- ``slow`` -- ``{label: seconds}``: the first request of that cycle advances
  the clock by that much, so the cycle overruns later boundaries.
- ``conflict`` -- ``{label: identity}``: the quote payload of that cycle
  carries two disagreeing rows for the identity (r2 conflicting group).
- ``pattern_from`` -- the ``A -> B -> A`` contract: its quote keeps one vendor
  timestamp for three consecutive executed cycles from that label with prices
  A, B, A, so the assembler must emit three revisions of one event.
- ``missing_open_interest`` -- identities the open-interest payload omits.

- ``frozen`` -- ``FROZEN`` is quoted with one unchanging timestamp and price
  from its first observation on: every later cycle re-observes it.
- ``unlisted_in`` -- those cycles' quote payloads carry ``UNLISTED``, an
  identity no listing names.
- ``stale_in`` -- ``{label: identity}``: that cycle re-serves the identity
  with the vendor timestamp of an earlier observation and another price, a
  late revision of an older event that must not displace the newer state.

Every other contract is quoted fresh at the cycle instant with a price that
moves by one cent per cycle.
"""

from __future__ import annotations

import pathlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from src.adapters.thetadata.capture_certification import (
    INDEX_PRICE,
    OPTION_CONTRACT_LIST,
    OPTION_GREEKS,
    OPTION_OPEN_INTEREST,
    OPTION_QUOTE,
)
from src.adapters.transport import HttpResponse, RecordedCall
from src.gex.sessions import EASTERN
from src.ingest.clock import FakeClock
from tests.native_capture import (
    GREEKS_COLUMNS,
    LIST_COLUMNS,
    OI_COLUMNS,
    QUOTE_COLUMNS,
    _csv,
    greeks_row,
    identity,
    oi_row,
    quote_row,
    vendor_stamp,
)

SESSION = date(2026, 9, 8)
EXPIRIES = (date(2026, 9, 8), date(2026, 9, 9))
STRIKES = (6000, 6050)
CONTRACTS = tuple(
    identity(expiry, strike, right)
    for expiry in EXPIRIES
    for strike in STRIKES
    for right in ("CALL", "PUT")
)
PATTERN = identity(date(2026, 9, 8), 6000, "CALL")
FROZEN = identity(date(2026, 9, 9), 6050, "PUT")
UNLISTED = identity(date(2026, 9, 9), 6100, "CALL")
PATTERN_PRICES = (("12.30", "12.60"), ("12.40", "12.70"), ("12.30", "12.60"))


def _key(ident: Mapping[str, str]) -> tuple[str, str, str]:
    return (ident["expiration"], ident["strike"], ident["right"])


@dataclass
class SyntheticFeed:
    """Deterministic fake transport driven by a fake clock and a session root."""

    clock: FakeClock
    root: pathlib.Path
    fail: dict[str, set[str]] = field(default_factory=dict)
    #: Cycle labels every request of which answers HTTP 401, the systemic
    #: refusal after which the session stops itself.
    reject: frozenset[str] = frozenset()
    slow: dict[str, float] = field(default_factory=dict)
    conflict: dict[str, dict[str, str]] = field(default_factory=dict)
    pattern_from: str | None = None
    #: Quote ``FROZEN`` with one unchanging timestamp and price from its first
    #: observation on, so every later cycle re-observes it.
    frozen: bool = False
    #: Cycle labels whose quote payload carries ``UNLISTED`` (absent from every
    #: listing), so the assembler's membership check has something to refuse.
    unlisted_in: frozenset[str] = frozenset()
    #: ``{label: identity}``: that cycle quotes the identity with the vendor
    #: timestamp of its observation two cycles earlier (one, if that is all
    #: there is) and a different price -- a late revision of an older event.
    stale_in: dict[str, dict[str, str]] = field(default_factory=dict)
    missing_open_interest: tuple[Mapping[str, str], ...] = ()
    capture_origin: str = "OFFLINE_FIXTURE"
    calls: list[RecordedCall] = field(default_factory=list)
    #: Cycle labels served, in order, so the script can count cycles.
    cycles: list[str] = field(default_factory=list)
    _frozen_at: datetime | None = None
    _pattern_at: datetime | None = None
    _stamps: dict[tuple[str, str, str], list[datetime]] = field(default_factory=dict)

    def _label(self) -> str:
        """The cycle in flight: the newest directory the one-shot claimed."""
        cycles = pathlib.Path(self.root) / "cycles"
        names = sorted(p.name for p in cycles.iterdir() if p.is_dir())
        if not names:
            raise AssertionError("a request arrived before any cycle directory")
        return names[-1]

    def _cycle_index(self, label: str) -> int:
        if not self.cycles or self.cycles[-1] != label:
            self.cycles.append(label)
        return len(self.cycles) - 1

    def get(self, url: str, params: Mapping[str, Any], timeout_seconds: float) -> Any:
        self.calls.append(
            RecordedCall(url=url, params=dict(params), timeout_seconds=timeout_seconds)
        )
        label = self._label()
        index = self._cycle_index(label)
        if label in self.slow:
            self.clock.advance(timedelta(seconds=self.slow.pop(label)))
        endpoint = next((e for e in _BODIES if e in url), None)
        if endpoint is None:
            raise AssertionError(f"SyntheticFeed has no route for {url!r}")
        if label in self.reject:
            return HttpResponse(
                status_code=401,
                text="unauthorized\n",
                headers={"content-type": "text/plain"},
            )
        if endpoint in self.fail.get(label, set()):
            return HttpResponse(
                status_code=404,
                text="no data for this request\n",
                headers={"content-type": "text/plain"},
            )
        return HttpResponse(
            status_code=200,
            text=_BODIES[endpoint](self, index, label),
            headers={"content-type": "text/csv"},
        )

    # -- bodies ------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock.peek()

    def _pattern_phase(self, label: str) -> int | None:
        """0, 1, 2 for the three pattern cycles from ``pattern_from``; else None."""
        if self.pattern_from is None or self.pattern_from not in self.cycles:
            return None
        offset = self.cycles.index(label) - self.cycles.index(self.pattern_from)
        return offset if 0 <= offset < len(PATTERN_PRICES) else None

    def _quote_rows(self, index: int, label: str) -> list[dict[str, str]]:
        fresh = self._now() - timedelta(milliseconds=300)
        rows = []
        for ident in CONTRACTS:
            key = _key(ident)
            if self.frozen and key == _key(FROZEN):
                self._frozen_at = self._frozen_at or fresh
                rows.append(
                    quote_row(ident, at=self._frozen_at, bid="7.00", ask="7.20")
                )
                continue
            phase = self._pattern_phase(label) if key == _key(PATTERN) else None
            if phase is not None:
                self._pattern_at = self._pattern_at or fresh
                bid, ask = PATTERN_PRICES[phase]
                rows.append(quote_row(ident, at=self._pattern_at, bid=bid, ask=ask))
                continue
            stale = self.stale_in.get(label)
            history = self._stamps.setdefault(key, [])
            if stale is not None and _key(stale) == key and history:
                at = history[-2] if len(history) >= 2 else history[-1]
                rows.append(quote_row(ident, at=at, bid="99.00", ask="99.20"))
                continue
            cents = index % 50
            history.append(fresh)
            rows.append(
                quote_row(
                    ident,
                    at=fresh,
                    bid=f"{12.30 + cents / 100:.2f}",
                    ask=f"{12.60 + cents / 100:.2f}",
                )
            )
        ambiguous = self.conflict.get(label)
        if ambiguous is not None:
            rows.append(quote_row(ambiguous, at=fresh, bid="1.00", ask="1.10"))
            rows.append(quote_row(ambiguous, at=fresh, bid="2.00", ask="2.10"))
        if label in self.unlisted_in:
            rows.append(quote_row(UNLISTED, at=fresh, bid="0.50", ask="0.70"))
        return rows

    def _greeks_rows(self, index: int, label: str) -> list[dict[str, str]]:
        fresh = self._now() - timedelta(milliseconds=250)
        rows = []
        for ident in CONTRACTS:
            row = greeks_row(ident, at=fresh, delta=f"{0.48 + (index % 5) / 1000:.4f}")
            row["underlying_timestamp"] = vendor_stamp(fresh)
            rows.append(row)
        return rows

    def _oi_rows(self, index: int, label: str) -> list[dict[str, str]]:
        """Prior-session open interest as published at 06:30 ET on the session day."""
        skipped = {_key(i) for i in self.missing_open_interest}
        published = (
            self._now()
            .astimezone(EASTERN)
            .replace(hour=6, minute=30, second=3, microsecond=0)
        )
        return [
            oi_row(ident, at=published, quantity=str(125 + 10 * position))
            for position, ident in enumerate(CONTRACTS)
            if _key(ident) not in skipped
        ]

    def _list_rows(self, index: int, label: str) -> list[dict[str, str]]:
        return [dict(ident) for ident in CONTRACTS]

    def _index_rows(self, index: int, label: str) -> list[dict[str, str]]:
        fresh = self._now() - timedelta(milliseconds=200)
        return [
            {
                "timestamp": vendor_stamp(fresh),
                "symbol": "SPX",
                "price": f"{6001.20 + index * 0.05:.2f}",
            }
        ]


_BODIES = {
    INDEX_PRICE: lambda feed, index, label: _csv(
        ("timestamp", "symbol", "price"), feed._index_rows(index, label)
    ),
    OPTION_QUOTE: lambda feed, index, label: _csv(
        QUOTE_COLUMNS, feed._quote_rows(index, label)
    ),
    OPTION_OPEN_INTEREST: lambda feed, index, label: _csv(
        OI_COLUMNS, feed._oi_rows(index, label)
    ),
    OPTION_GREEKS: lambda feed, index, label: _csv(
        GREEKS_COLUMNS, feed._greeks_rows(index, label)
    ),
    OPTION_CONTRACT_LIST: lambda feed, index, label: _csv(
        LIST_COLUMNS, feed._list_rows(index, label)
    ),
}
