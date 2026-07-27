# Validation

Two different things are called validation in this repository:

1. **Record validation** — the gate every contract passes before arithmetic.
2. **Test strategy** — how the engine itself is checked.

Both are below.

---

## 1. Record validation

### The failure being prevented

A single NaN gamma summed into a chain total produces a NaN total, and a NaN
total that reaches a chart looks like a rendering bug rather than a data bug.
Rejection happens at the contract boundary, before any arithmetic.

The subtle case is why ordering matters: **`NaN < 0` is `False`**, so a NaN bid
sails through a naive negativity check. Finiteness is checked first, and every
ordering comparison is guarded so an unorderable value cannot reach it.

### Three-way status

| Status | Meaning | Reaches aggregates? |
|---|---|---|
| `ACCEPTED` | no issues | yes |
| `ACCEPTED_WITH_WARNING` | usable, with a caveat | yes |
| `REJECTED` | at least one error | no |

The middle case is real and common: a zero-bid deep-wing option has usable gamma
but untrustworthy IV. Treating it as either fully good or fully bad loses
information.

### Rules

**Numeric hygiene** — `not_finite` (NaN, infinities, wrong type including `bool`,
since `isinstance(True, int)` is `True` in Python), `negative_open_interest`,
`negative_bid`, `negative_ask`, `crossed_market`, `locked_market` (warn),
`zero_bid` (warn), `invalid_strike`, `invalid_multiplier`,
`non_positive_implied_vol` (warn), `implied_vol_out_of_range` (warn),
`negative_gamma`, `gamma_out_of_range`, `extreme_iv_spread` (warn),
`no_gamma_source`, `missing_open_interest`.

**Structure** — `invalid_expiration`, `invalid_option_right`,
`duplicate_contract`, `unknown_root`.

**Time** — `naive_timestamp`, `future_timestamp`, `stale_snapshot` (warn),
`timestamp_skew` (warn), `missing_timestamp`.

Crossed markets are an error by default and a warning when
`drop_crossed_quotes: false` — explicit classification either way, never silent.

Duplicate identities reject **both** copies. There is no principled way to choose,
and silently keeping the first is how a stale record wins over a fresh one.

### Machine-readable output

```json
{
  "total": 250, "accepted": 248, "accepted_with_warning": 1, "rejected": 1,
  "acceptance_ratio": 0.996,
  "error_counts": {"not_finite": 1},
  "warning_counts": {"zero_bid": 1},
  "examples": [{"code": "not_finite", "field": "quote.gamma",
                "severity": "error", "observed": "nan"}]
}
```

Counters rather than a transcript — an SPX chain can produce tens of thousands of
records, and what a confidence component needs is "how many, of which kind". The
example list is bounded at 25; an unbounded one is a memory leak on a bad feed
day.

### Timestamp integrity

Every source clock is kept separately: `quote_timestamp`, `greeks_timestamp`,
`iv_timestamp`, `underlying_timestamp`, `open_interest_as_of`,
`request_started_at`, `response_received_at`, `normalized_at`.

**Nothing is ever back-stamped to `as_of`.** That was the v1 failure: assigning
the request instant to every record makes a five-minute-old quote and a fresh one
indistinguishable, and the whole chain reads as perfectly fresh regardless of
what the vendor sent. Freshness that is assigned rather than measured is worse
than no freshness metric at all.

A future-dated vendor timestamp beyond the clock-skew allowance is a **hard
failure**: it zeroes `future_timestamp_penalty`, zeroes the whole confidence
score, and is flagged `DATA_HALT`-eligible. Small skew (2 s by default) is
ordinary disagreement between two machines and is tolerated.

Open interest is a `date`, not an instant, and is aged in **trading sessions**.
Friday's settlement read on Monday is one session old; a holiday weekend does not
make it look worse.

---

## 2. Test strategy

### Layers

| Layer | Marker | What it proves | Label |
|---|---|---|---|
| Unit | — | Each rule and formula in isolation | `TESTED_SYNTHETICALLY` |
| Integration | `integration` | Fixture to parser to validation to GEX to confidence to metadata | `TESTED_WITH_OFFLINE_FIXTURES` |
| Regression | `regression` | Frozen expected values, hand-transcribed | `TESTED_SYNTHETICALLY` |
| Replay | `replay` | Same inputs produce the same output hash | `TESTED_SYNTHETICALLY` |
| Release integrity | — | Bare-interpreter run, pinned build, reproducible archive | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |

**No test touches the network.** `FakeTransport` raises on an unregistered route
rather than silently succeeding, so an accidental real call surfaces as a loud
failure — `test_no_unit_test_performs_a_real_network_call` asserts exactly that.

**What none of these layers prove.** The fixtures are vendor-*shaped* payloads
written by this repository, not captured vendor responses. Passing them
establishes that the parser, the join and the maths behave as specified on the
inputs we imagined. It does not establish that ThetaData emits those inputs.
Every integration claim in this repository is
`NOT_YET_VALIDATED_WITH_LIVE_VENDOR_DATA`.

### Environment independence

`tests/unit/test_release_integrity.py` runs the engine in a subprocess under
`python -S -E`: no site-packages, no `PYTHON*` environment influence. Under `-S`
the third-party packages are not merely unimported, they are *unimportable*, so
an accidental `import yaml` in the engine core fails there even though it would
succeed in the dev environment. The bare run's GEX total is then asserted equal
to the installed run's — if those diverge, something in the maths depends on an
installed package.

CI goes one step further: the `bare-interpreter` job installs nothing at all.

### What makes a test worth having here

The fixtures are built so the **answers are known in advance**: open interest is
placed at chosen strikes, put weight exceeds call weight so signed GEX must cross
zero above spot, and the smile is calibrated to a realistic SPX skew so
`sticky_strike` and `sticky_moneyness` cannot collapse onto each other. A test
that asserts "the engine returned a number" proves nothing about a GEX engine.

Several tests exist specifically as **negative controls** — proof that the test
could fail:

- `test_the_credential_scanner_actually_catches_a_planted_secret` — a scanner
  nobody has seen fire is a scanner nobody should trust.
- `test_wrong_settlement_clock_would_break_the_gamma_cross_check` — confirms the
  cross-check is sensitive to the clock rather than passing by coincidence.
- `test_the_floor_is_inert_when_no_contract_is_close_enough_to_expiry` — the
  0DTE sensitivity sweep is meaningless five hours before settlement, and this
  says so.
- `test_flat_smile_collapses_sticky_moneyness_onto_sticky_strike` — with no skew
  there is nothing for a translating smile to change.

### Regression case

`tests/regression/test_frozen_reference_case.py` pins totals, per-bucket values,
per-strike values, walls, voids, roots, all 17 confidence components, and three
fingerprints.

**Nothing regenerates its own expectation.** Values were printed once, read,
hand-checked and typed in as literals. A regression test that recomputes its
expectations proves only that the code equals itself.

Hand checks recorded in the module docstring make the numbers believable rather
than merely recorded — for example, 5 expiries times 252,633 open interest equals
1,263,165, which matches the frozen total.

Tolerance is `rel=1e-12` on floats and exact on the hash. That split is
deliberate and has already paid off: when canonical contract ordering was
introduced, every numeric expectation held while the hash moved, which is exactly
how a representation change should look.

### Replay

Proves *same raw fixtures + same config + same model version produce the same
output hash*, and covers the three ways that breaks:

- a hidden `datetime.now()` — caught by running the same fixtures twice
- dict or set iteration order — caught by reversing the input row order
- `PYTHONHASHSEED` — caught by re-running in subprocesses with different seeds

The row-order test found a real defect: float addition is not associative, so
vendor row order changed the last bits of every sum. Contracts are now sorted
into canonical order before aggregation, making the output a function of the
data rather than of arrival order.

The hash quantises floats to 12 significant figures, so it is stable across
platforms rather than only within one machine.

### Architecture tests

Rules that are easy to break by accident and expensive to discover later:

- `src/` never imports from `tests/` — AST-based, per file
- `src/gex`, `src/domain`, `src/synthetic` import no third-party package, checked
  both by AST and by importing them in a subprocess
- no order-placement code exists — AST-based, so prose *about* order placement is
  fine and a definition or call is not
- `BrokerAdapter` exposes no order method
- no credential literals — literal-shaped rather than keyword-shaped, because
  reading a credential from the environment necessarily mentions the word
  "password"

### Coverage

Target 90% on the modules that compute numbers; scaffolding packages with no
implementation are excluded in `pyproject.toml`.

Not chasing 100%: the uncovered remainder is defensive branches and the real HTTP
transport, which cannot be covered without either mocking `httpx` internals
(testing the mock) or making a network call (which unit tests must never do). Its
retry, redaction and size-cap behaviour lives in `RetryingTransport`, which *is*
covered.

Current: **93.30%** across 3,879 statements, against a `fail_under` of 90.

### Commands

**Unix (bash):**

```bash
python -m pytest                     # everything
python -m pytest -m integration      # offline pipeline
python -m pytest -m regression       # frozen values
python -m pytest -m replay           # determinism
python -m pytest tests/unit/test_architecture.py   # cannot trade
python -m pytest tests/unit/test_release_integrity.py
python -m pytest --cov --cov-report=term-missing
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

**Windows (PowerShell):**

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest -m integration
.\.venv\Scripts\python.exe -m pytest -m regression
.\.venv\Scripts\python.exe -m pytest -m replay
.\.venv\Scripts\python.exe -m pytest tests\unit\test_architecture.py
.\.venv\Scripts\python.exe -m pytest tests\unit\test_release_integrity.py
.\.venv\Scripts\python.exe -m pytest --cov --cov-report=term-missing
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src
```

See [RELEASE.md](RELEASE.md) for the bootstrap and the release procedure.
