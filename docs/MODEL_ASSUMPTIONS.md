# Model assumptions

Every assumption that changes a number, where it lives, and what it would take to
be wrong about it.

The operative rule: **a gamma number is only interpretable together with the
conventions that produced it.** Two engines can implement Black-Scholes perfectly
and still disagree by 20% on a 0DTE gamma because one floors time-to-expiry at 30
minutes and the other at 60. So `ModelSpec` travels inside every `GexSnapshot`
and is hashed into the model fingerprint.

```python
snapshot.model_spec.describe()
# black_scholes_merton / ACT/365F / floor=60min / iv=NBBO_MID_IV / 53a805ad198ee52b
```

Change any assumption and the fingerprint changes. That is what makes the replay
test meaningful.

---

## The specification object

`src/domain/model_spec.py` — `ModelSpec`

| Field | Default | Effect if wrong |
|---|---|---|
| `pricing_model` | `black_scholes_merton` | Different greeks entirely |
| `day_count_convention` | `ACT/365F` | ACT/252 changes `T` by ~45%, and gamma with it |
| `risk_free_rate_source` | `configured_constant` | Small on short-dated, larger on LEAPs |
| `dividend_yield_source` | `configured_constant` | SPX yield is material; zero biases gamma |
| `expiration_timestamp_rule` | `root_specific_settlement` | Hours of `T` on expiration day |
| `minimum_time_to_expiry_minutes` | `60.0` | Dominates 0DTE aggregates — see below |
| `underlying_price_source` | `vendor_index_snapshot` | Shifts every moneyness |
| `iv_price_source` | `NBBO_MID_IV` | Bid vs ask IV can differ by vol points on a wide wing |
| `risk_free_rate` / `dividend_yield` | from config | Effective values, not just provenance |
| `model_version` | `gex-engine/2.0.0` | Bumped when output changes for identical input |

---

## 1. Pricing model

Black-Scholes-Merton with continuous dividend yield. SPX/SPXW are European and
cash-settled, so no early-exercise adjustment is needed — this is the right model
family, not an approximation to one.

```
gamma = e^(-qT) · φ(d1) / (S · σ · √T)
```

Identical for calls and puts at the same strike and expiry. That identity is why
the zero-gamma grid can reprice cheaply: only `(K, T, σ)` matter.

**Verified against identities, not against another implementation:** put-call
parity, gamma as the finite-difference second derivative of price, gamma as the
finite-difference derivative of delta, and the ATM limit `φ(0)/(S·σ·√T)`.
Identities catch sign and discounting errors that a single golden number would
not.

## 2. Day count

`ACT/365F` by default. SPX index options are quoted against calendar time, so a
365-day year is the right denominator. `ACT/360` and `ACT/252` are selectable
because some vendor and academic pipelines use them, and a comparison against one
of those is meaningless unless the convention can be matched.

## 3. Expiration timestamps

| Root | Settlement | Why |
|---|---|---|
| `SPXW` | 16:00 ET on expiry | PM-settled; the expiring series trades to the close |
| `SPX` | 09:30 ET on expiry | AM-settled; SET is struck from expiration-morning opening prints |

This is why SPX and SPXW are separate roots and are never pooled — including in
smile fitting, where a shared expiry *date* with different settlement *times*
would blend two surfaces.

`root_specific_settlement_with_early_close` additionally shortens a PM-settled
expiration to 13:00 ET on an early-close session. Opt-in, because it pulls in the
trading calendar, and the rule used is recorded in the spec.

## 4. Minimum time-to-expiry floor ⚠️

**The single most consequential assumption in the engine.**

Gamma diverges as `T → 0`. The floor decides how much of that real singularity
reaches the aggregate.

```python
report = compute_floor_sensitivity(chain)
report.dte0_range_pct   # how much the 0DTE bucket moves across ~0 / 30 / 60 min
```

Measured on the synthetic fixture:

* **Five hours to settlement** — every candidate floor is inactive, spread is
  exactly 0%. A sensitivity test run at midday would pass without testing
  anything.
* **Fifteen minutes to settlement** — the 30- and 60-minute floors both bind, a
  near-zero floor does not, and the three answers differ materially.

**Not verified against ThetaData.** See `OPEN_DECISIONS.md` §1.

## 5. Implied volatility provenance

A bare field called `implied_vol` is ambiguous in a way that changes the answer.
Bid IV, mid IV, ask IV and trade IV can differ by several vol points on a wide
0DTE wing, and gamma is a function of whichever one was picked.

`IVSource` values: `NBBO_BID_IV`, `NBBO_MID_IV`, `NBBO_ASK_IV`, `TRADE_IV`,
`VENDOR_DEFAULT_IV`, `LOCALLY_SOLVED_MID_IV`.

`VENDOR_DEFAULT_IV` means the vendor returned an IV without documenting which
price it used — usable, but labelled as a known unknown rather than assumed to be
mid.

When the whole book is available, all three legs are retained alongside
`iv_spread` and an `IVQualityFlag`: `OK`, `SINGLE_SIDED`, `ZERO_BID`,
`CROSSED_MARKET`, `WIDE_SPREAD`, `SOLVER_FAILED`, `VENDOR_ERROR`, `OUT_OF_RANGE`,
`NON_FINITE_INPUT`.

## 6. Gamma source

Default is `prefer_vendor_gamma: false` — derive gamma from IV with our own
pricer.

**Reasoning, and its limits.** The zero-gamma grid has to reprice gamma at
hypothetical spot levels no vendor will ever quote, so that code path is
mandatory at every tier. Taking vendor gamma at the *current* spot as well puts
two different gamma models in contact at exactly the point the root-finder cares
about most. One model throughout avoids that.

It also happens to work on ThetaData's Standard tier, where `implied_vol` is
available and `gamma` is not. **That is a tier-access fact, not a claim that the
two agree numerically** — see `OPEN_DECISIONS.md` §3.

When both sources appear in one snapshot the engine emits a `mixed gamma sources`
warning, because the grid always uses the shadow pricer and the two are then not
directly comparable.

## 7. Sign convention

`DEALER_LONG_CALLS_SHORT_PUTS`: customers are assumed net buyers of puts and net
sellers of calls, so dealers end up long call gamma and short put gamma.

**This is a proxy, never dealer inventory truth.** Every snapshot records which
convention produced its sign, because a stored signed GEX without its assumption
is not interpretable.

`FLOW_ADJUSTED` exists in the enum but has no static sign — asking for one raises,
because the sign must come from classified flow.

## 8. GEX scaling

```
GEX_i = gamma_i · OI_i · M · S · ΔS,    ΔS = spot_move_pct · S
```

With the 1% convention: `gamma · OI · M · S² · 0.01`. Read it as dollars of
dealer delta that must be re-hedged for a 1% move in spot. `M = 100` for
SPX/SPXW.

On the zero-gamma grid, **both** the gamma evaluation and the `S · ΔS` scaling
use the grid point. Holding the scaling at the original spot would tilt the curve
and move the crossing.

## 9. Volatility conventions on the grid

| Convention | σ at grid point `S*` | Status |
|---|---|---|
| `FROZEN_IV` | each contract's raw snapshot IV, unchanged | baseline |
| `STICKY_STRIKE` | fitted per-expiry smile at the contract's *original* moneyness | **default** |
| `STICKY_MONEYNESS` | fitted smile at the contract's *new* moneyness under `S*` | implemented |
| `STICKY_DELTA` | delta-coordinate surface, iterative solve | **not implemented** |
| `SURFACE_REFIT` | full per-grid-point re-estimation | **not implemented** |

The smile is a least-squares quadratic in standardised moneyness
`m = ln(K/S)/√T` — enough structure to be a real smile (level, skew, curvature),
few enough parameters that a handful of torn quotes cannot bend it. Output is
clamped to the pricer's usable vol range because a quadratic extrapolates badly
in the wings.

`STICKY_STRIKE` differs from `FROZEN_IV` only in reading σ off the fit rather
than off the raw quote — the denoised sibling, not a different model.

**Convention disagreement is the output, not the noise.**
`zero_gamma_spread_pct` is the honest error bar on the level. Averaging the
conventions together would discard exactly the information a consumer needs.

## 10. Time and the trading calendar

US Eastern is **implemented, not imported**. `zoneinfo` needs the `tzdata`
package, which is absent on a bare Windows install, and a trading engine must not
depend on whether an optional data wheel happened to get installed. The post-2007
DST rule is encoded directly, and a test cross-checks two years of daily offsets
against the real tz database whenever `tzdata` *is* present.

Open interest is aged in **trading sessions**, not calendar days: Friday's
settlement read on Monday is one session old, and a holiday weekend does not make
it look worse. The calendar (`src/gex/calendar.py`) derives holidays from NYSE
rules including Good Friday via the Gregorian computus, plus 13:00 ET early
closes.

## 11. Data-quality tolerances

Research limits describing when a joined record stops representing a single
instant. **Not trading parameters**, and nothing here turns them into an order.

| Limit | Default | Rationale |
|---|---|---|
| `max_quote_greeks_skew_seconds` | 5.0 | Greeks computed from a quote that no longer exists |
| `max_quote_iv_skew_seconds` | 5.0 | Same |
| `max_quote_underlying_skew_seconds` | 2.0 | The gamma input pair — tightest of the three |
| `max_future_timestamp_seconds` | 2.0 | Ordinary clock drift; beyond it, a fault |
| `max_snapshot_age_seconds` | 60.0 | Beyond this it is not the current market |
| `max_open_interest_age_sessions` | 2 | T-1 is normal; this catches a stalled job |

## 12. What is a sentinel and what is not

**Data-plumbing facts** carry real numbers: a 60-second-old option snapshot is
stale whatever the strategy turns out to be.

**Market claims** stay `UNSPECIFIED_CALIBRATE`: `max_zero_gamma_shift_pct`,
`max_sign_model_disagreement`, `max_0dte_dominance_ratio`.

The sentinel is an object, not a magic number. It is falsy, raises `TypeError` on
any ordering comparison, and raises on `float()` / `int()` / arithmetic. The most
likely accident is a caller coercing it "just to make the types line up", which
would convert a loud "not researched" into a quiet, arbitrary threshold.


---

## v2.1.2: which assumptions are ours and which are the vendor's

The engine version is `gex-engine/2.1.2` (`src/domain/model_spec.py`), distinct
from the parser version `thetadata-v3-parser/2.1.1`
(`src/adapters/raw_store.py`). Both enter the replay hash; they move for
different reasons.

### Pricing modes

| Mode | Vendor quantities inside the calculation | Agreement required |
|---|---|---|
| `LOCAL_IV_LOCAL_GAMMA` | none | no |
| `VENDOR_GAMMA_VALIDATION` | compared, not aggregated | no |
| `VENDOR_IV_LOCAL_GAMMA` | vendor IV feeds local gamma | **yes** |

### What we cannot infer about the vendor

`rate_units` and `dividend_convention` default to `UNKNOWN` and block
`VENDOR_IV_LOCAL_GAMMA`. They are not guesses waiting for a better guess -- a
vendor `rate_value: 4.2` is either 4.2% or 420%, and `annual_dividend` is either
cash or a yield. Neither pair is interchangeable, and both would silently change
every gamma.

Five further conventions are undocumented and reported as `UNKNOWN`: the
vendor's settlement instant, day count, short-dated floor, solved-against price,
and solver version.

**Explicitly not claimed:** that our gamma matches ThetaData's. See
OPEN_DECISIONS OD-3, OD-22, OD-23, OD-24.

### Mixed models

Per-contract IV fallback can leave one chain priced under several effective
models. Research mode (the default) reports the distribution and marks the
snapshot uncalibrated; strict mode
(`require_uniform_effective_model=True`) refuses the chain. Neither silently
reports one model for a chain that had several.
