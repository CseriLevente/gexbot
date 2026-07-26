# gex-futures-bot

GEX-based regime engine for automated MES/ES futures trading.

Implements the GEX engine (v1) from
[`docs/handoff/implementation-plan.md`](docs/handoff/implementation-plan.md), with
the rest of the pipeline scaffolded.

**Core principle, from the plan:** GEX is a *regime and level model*, not a trade
trigger.

> The bot does not ask "is GEX positive or negative?"
> It asks "which hedging regime is likely, how reliable is that estimate, and does
> price/volume/volatility right now actually offer an executable setup inside that
> regime?"

## Try it

No subscription, no API key, no network:

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install pytest
.venv/Scripts/python.exe -m src.app        # full GEX snapshot on a synthetic chain
.venv/Scripts/python.exe -m pytest         # 232 tests
```

The engine core (`src/gex`, `src/domain`) has **no third-party dependencies** —
pure stdlib maths. `pytest` is the only dev requirement.

## Status

| Component | State |
|---|---|
| Black-Scholes shadow pricer | ✅ gamma/delta/vega/price + IV inversion |
| US Eastern clock + SPX/SPXW settlement | ✅ self-contained, no `tzdata` needed |
| View 1 — unsigned gamma concentration | ✅ |
| View 2 — naive signed GEX | ✅ with explicit sign convention |
| View 3 — expiry buckets (0DTE…GT_30) | ✅ always all five, zero-filled |
| View 4 — strike level + walls/nodes/voids | ✅ gamma-weighted, never raw OI |
| View 5 — zero-gamma grid | ✅ 3 of 4 IV conventions (`surface_refit` deferred) |
| Confidence score | ✅ all 8 components, uncalibrated ones flagged |
| ThetaData adapter | 🟡 endpoints + tier map + chain join done; HTTP transport not wired |
| Synthetic adapter | ✅ full pipeline runs with no vendor |
| Databento / Cboe / IBKR adapters | ⬜ protocols only |
| Feature store, regime, strategy, risk | ⬜ scaffolded, empty |
| Backtester, replay, API, monitoring | ⬜ scaffolded, empty |

**Nothing here can place an order.** There is no broker implementation, and the
confidence score reports `uncalibrated` by default, which the risk engine is
specified to treat as a hard block.

## Read these first

| Document | Why |
|---|---|
| [`docs/handoff/data-requirements.md`](docs/handoff/data-requirements.md) | **Which subscriptions to buy, and which not to.** Verified endpoints, tiers, prices. |
| [`docs/specs/gex-engine.md`](docs/specs/gex-engine.md) | Engine spec + the five known limitations. |
| [`docs/handoff/next-steps.md`](docs/handoff/next-steps.md) | What to build next, in order. |
| [`docs/handoff/implementation-plan.md`](docs/handoff/implementation-plan.md) | The original roadmap (Hungarian). |

## Three decisions worth knowing about

**1. Gamma is computed in-house, not bought.**
On ThetaData, gamma is a second-order greek behind the **Pro** tier ($160/mo),
while `implied_vol` is available at **Standard** ($80/mo). The zero-gamma grid has
to reprice gamma at hypothetical spot levels anyway, so deriving it from IV is both
$80/mo cheaper and internally consistent — one gamma model instead of two meeting
at the current spot. Details in `data-requirements.md` §1.

**2. `UNSPECIFIED_CALIBRATE` is a sentinel object, not a number.**
It is falsy and **raises `TypeError` on comparison**, so an uncalibrated threshold
cannot silently participate in a `>=` and pass. Components still using placeholders
are flagged, and any flag makes `ConfidenceScore.calibrated` False. Net effect: the
engine produces research output today and cannot trade until the calibration work
is done.

**3. US Eastern is implemented, not imported.**
`zoneinfo` needs `tzdata`, which is absent on a bare Windows install — and being an
hour out on an SPXW expiration afternoon does not produce a slightly wrong gamma,
it produces a completely wrong one. `src/gex/sessions.py` implements the post-2007
DST rule directly, and a test cross-checks two years of daily offsets against the
real tz database whenever `tzdata` *is* installed.

## Layout

```
config/          research.yaml / paper.yaml / live.yaml
docs/
  handoff/       data-requirements.md, next-steps.md, implementation-plan.md
  specs/         gex-engine.md
src/
  domain/        contracts.py, gex.py, states.py     -- value objects, enums
  gex/           pricing, sessions, formulas, walls, zero_gamma, confidence, engine
  adapters/      base.py (protocols), thetadata/, fixtures/
  features/ regime/ strategy/ risk/ broker/
  reconcile/ backtest/ replay/ api/                  -- scaffolded
  app.py         runnable demo
tests/
  unit/          pricing, sessions, formulas, walls, zero_gamma, confidence,
                 engine, thetadata_adapter
  fixtures/      chains.py -- deterministic synthetic SPX/SPXW chains
```

## About the tests

The fixtures are built so the **answers are known in advance**: open interest is
placed at chosen strikes, put weight exceeds call weight so signed GEX is negative
at spot and must cross zero above it, and the smile is calibrated to a realistic
SPX skew so `sticky_strike` and `sticky_delta` cannot collapse onto each other.

Three tests worth reading, because each encodes a claim from the plan:

- `test_gamma_weighted_peak_is_not_the_raw_open_interest_peak` — why walls must not
  come from OI. On the fixture, OI peaks at 5100 but gamma-weighted GEX peaks at
  5025.
- `test_curve_turns_back_down_above_the_call_open_interest_peak` — signed GEX is
  double-humped, not monotone in spot, which is why the search counts crossings
  instead of assuming one.
- `test_default_config_yields_an_uncalibrated_untradeable_snapshot` — the safety
  interlock between calibration state and live trading.
