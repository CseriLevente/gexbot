# gex-futures-bot

A **GEX research engine** for SPX/SPXW option chains.

> **This is not a trading bot.** It cannot place an order. There is no broker
> adapter, no risk engine, no strategy layer and no execution path — and an
> architecture test enforces that none appear by accident. It computes gamma
> exposure from an option chain and reports how much to trust the result.

Everything it has been run against is synthetic or fixture data. It has never
seen a live vendor response.

---

## Try it

No subscription, no API key, no network:

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m src.app        # full GEX snapshot, synthetic chain
.venv/Scripts/python.exe -m pytest         # 702 tests, 92% coverage
```

The engine core (`src/gex`, `src/domain`, `src/synthetic`) imports **nothing**
outside the standard library, so the maths is verifiable on a bare interpreter.

---

## Status

Precise language throughout: *implemented*, *tested synthetically*, *tested with
fixtures*, *validated with live vendor data*, *planned*, *not implemented*.

| Component | Status |
|---|---|
| Black-Scholes shadow pricer | implemented; tested synthetically against closed-form identities |
| US Eastern clock, SPX/SPXW settlement | implemented; tested synthetically |
| Trading calendar (holidays, early closes, session ageing) | implemented; tested synthetically |
| Record validation (finiteness, structure, time) | implemented; tested synthetically |
| Per-record timestamp preservation | implemented; tested with fixtures |
| View 1 — unsigned gamma concentration | implemented; tested synthetically |
| View 2 — naive signed GEX | implemented; tested synthetically |
| View 3 — expiry buckets | implemented; tested synthetically |
| View 4 — strike level, walls, classified voids | implemented; tested synthetically |
| View 5 — zero-gamma grid, 3 of 5 IV conventions | implemented; tested synthetically |
| Option-universe accounting | implemented; tested synthetically |
| Confidence model (17 components) | implemented; tested synthetically |
| Typed config with schema validation | implemented; tested synthetically |
| ThetaData parsing, join, tier map | implemented; **tested with fixtures only** |
| HTTP transport, retries, raw-response store | implemented; tested with a deterministic fake |
| Real network transport (`HttpxTransport`) | implemented; **never executed** |
| Local gamma vs vendor gamma | **not validated** |
| `STICKY_DELTA`, `SURFACE_REFIT` conventions | **not implemented** (refuse explicitly) |
| Feature store, regime classifier, strategies | **not implemented** |
| Risk engine, broker adapter, order placement | **not implemented, and out of scope** |
| Futures data (Databento), Cboe Open-Close | **not implemented** |

### What is deliberately absent

There is no risk engine. Earlier documentation claimed `ConfidenceScore.calibrated`
was "enforced by the risk engine" and that live trading was "blocked" — that was
wrong, and is corrected in [`docs/CHANGELOG.md`](docs/CHANGELOG.md). Nothing is
blocked because nothing can trade. `calibrated` is a research signal that market
thresholds are still unresearched.

---

## Read these

| Document | Why |
|---|---|
| [`docs/OPEN_DECISIONS.md`](docs/OPEN_DECISIONS.md) | **Start here.** Every assumption that could be wrong, and what would settle it. |
| [`docs/MODEL_ASSUMPTIONS.md`](docs/MODEL_ASSUMPTIONS.md) | Every parameter that changes a number. |
| [`docs/FORMULAS.md`](docs/FORMULAS.md) | The maths, with the design note for each. |
| [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md) | Every output field and what it does *not* mean. |
| [`docs/VALIDATION.md`](docs/VALIDATION.md) | Record validation rules and test strategy. |
| [`docs/THETADATA_INTEGRATION.md`](docs/THETADATA_INTEGRATION.md) | Wiring up a real subscription, and what to check first. |
| [`docs/CHANGELOG.md`](docs/CHANGELOG.md) | What changed in v2, including corrected claims. |
| [`docs/handoff/data-requirements.md`](docs/handoff/data-requirements.md) | Vendor endpoints, tiers, prices. |

---

## Design decisions worth knowing

**1. Nothing is back-stamped to the request timestamp.**
Each contract keeps `quote_timestamp`, `greeks_timestamp`, `iv_timestamp`,
`underlying_timestamp`, `open_interest_as_of`, and our own request/response/
normalisation clocks. Assigning `as_of` to every record — the v1 behaviour —
makes a five-minute-old quote indistinguishable from a fresh one and reports the
whole chain as perfectly fresh regardless of what arrived. Freshness that is
assigned rather than measured is worse than none.

**2. Every model assumption travels with the number.**
Two correct Black-Scholes implementations can disagree by 20% on a 0DTE gamma
because one floors time-to-expiry at 30 minutes and the other at 60. `ModelSpec`
is embedded in every snapshot and hashed into a fingerprint, so a disagreement is
investigable instead of mysterious.

**3. Observations are separated from interpretations.**
"The strike with the most call gamma" is a fact. "Resistance above" is a claim
that is only true if that strike is above spot. `largest_call_gamma_strike` and
`upside_call_wall` are different fields, and the second is `None` when nothing
qualifies rather than silently degrading into the first.

**4. Absent data is not quiet data.**
A strike range the vendor never sent looks identical to a genuinely low-gamma
region. Voids are classified against an inferred strike ladder, and only
`TRUE_LOW_GEX_VOID` reports as tradable structure.

**5. `UNSPECIFIED_CALIBRATE` is an object, not a number.**
It is falsy and raises `TypeError` on ordering comparisons, `float()`, `int()`
and arithmetic. The likeliest accident is a caller coercing it to make types line
up, converting a loud "not researched" into a quiet, arbitrary threshold.

**6. US Eastern is implemented, not imported.**
`zoneinfo` needs `tzdata`, absent on a bare Windows install. Being an hour out on
an SPXW expiration afternoon does not produce a slightly wrong gamma — it
produces a completely wrong one. A test cross-checks two years of daily offsets
against the real tz database whenever `tzdata` *is* installed.

**7. The output is a function of the data, not of arrival order.**
Float addition is not associative, so vendor row order changed the last bits of
every sum. Contracts are sorted into canonical order before aggregation, and a
replay test reverses the input rows to prove it.

---

## Layout

```
config/          research.yaml (usable) / paper.yaml, live.yaml (disabled)
docs/            model assumptions, formulas, data dictionary, open decisions
src/
  domain/        contracts, iv, timestamps, validation, model_spec, gex, states
  gex/           pricing, sessions, calendar, formulas, walls, zero_gamma,
                 confidence, engine, config
  adapters/      base (protocols), transport, raw_store, thetadata/, synthetic/
  config/        typed schema loading
  synthetic/     deterministic chain generation (production, not test-only)
  app.py         runnable demo
tests/
  unit/          per-module rules and formulas
  integration/   offline pipeline through the fake transport
  regression/    frozen, hand-transcribed expectations
  replay/        determinism and output-hash stability
  fixtures/      stored vendor responses
```

---

## Commands

```bash
python -m src.app                    # demo
python -m pytest                     # all tests
python -m pytest -m integration      # offline pipeline
python -m pytest -m regression       # frozen values
python -m pytest -m replay           # determinism
python -m pytest --cov --cov-report=term-missing
python -m ruff check .               # lint
python -m ruff format --check .      # format
python -m mypy src                   # types (strict)
```

Python 3.12 or 3.13.

---

## If you are picking this up

Read [`docs/OPEN_DECISIONS.md`](docs/OPEN_DECISIONS.md) first. It lists the
assumptions that could be wrong and what evidence would settle each one. The
three that matter most:

1. **Vendor timestamp timezone** — one live response settles it, and getting it
   wrong invalidates every 0DTE number.
2. **Local gamma vs vendor gamma** — never measured; the tier recommendation
   rests on it.
3. **The 0DTE time floor** — configurable and sensitivity-reported, but not
   resolved.

And before any live data: the OPRA non-display licensing question in
[`docs/handoff/data-requirements.md`](docs/handoff/data-requirements.md) §5. It is
the one item that can invalidate the project after the engineering is finished.
