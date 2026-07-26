# Formulas

Every formula the engine implements, with the design note that matters for each.
Source of truth is the code; this is the readable version.

Notation: `S` spot, `K` strike, `T` time to expiry in years, `σ` implied
volatility, `r` risk-free rate, `q` dividend yield, `M` contract multiplier
(100 for SPX/SPXW), `OI` open interest.

---

## Black-Scholes-Merton

```
d1 = [ln(S/K) + (r − q + σ²/2)·T] / (σ·√T)
d2 = d1 − σ·√T

gamma = e^(−qT) · φ(d1) / (S · σ · √T)
delta_call = e^(−qT) · Φ(d1)
delta_put  = −e^(−qT) · Φ(−d1)
vega  = S · e^(−qT) · φ(d1) · √T

call = S·e^(−qT)·Φ(d1) − K·e^(−rT)·Φ(d2)
put  = K·e^(−rT)·Φ(−d2) − S·e^(−qT)·Φ(−d1)
```

`Φ` via `math.erf`; no scipy. **Gamma is identical for calls and puts** at the
same strike and expiry — the identity the zero-gamma grid relies on.

**Degenerate inputs return 0, never NaN.** `T ≤ 0`, `σ ≤ 0` or `S ≤ 0` yield zero
gamma and zero vega rather than propagating a NaN into a chain total. Expired
delta reports the intrinsic limit (±1 or 0) instead of zero.

### Implied volatility inversion

Newton with a vega step, bisection fallback when vega collapses (deep wings,
near expiry). Bracket `[1e-4, 5.0]`.

A target outside the reachable price range returns `None` rather than a clamped
value. Clamping would hand back a plausible-looking σ that solves nothing; the
caller needs to drop the contract and register the miss.

### Year fraction

```
T = max(seconds_to_expiry / seconds_per_year, floor)
```

`seconds_per_year` from the day-count convention; `floor` from the model spec.
Precedence is explicit — `spec` wins over an explicit `floor` argument — so a
snapshot's reported conventions can never disagree with the numbers it contains.

---

## The core GEX quantity

```
GEX_i = gamma_i · OI_i · M · S · ΔS       where ΔS = spot_move_pct · S
```

With the 1% convention: `gamma · OI · M · S² · 0.01`.

**Reading:** dollars of dealer delta that must be re-hedged for a 1% move in
spot. Always non-negative — magnitude and sign are separate concerns, so a vendor
that signs put gamma cannot flip the magnitude.

Sign is applied on top:

```
sign = +1 for calls, −1 for puts     (DEALER_LONG_CALLS_SHORT_PUTS)
signed_GEX_i = GEX_i · sign
```

The sign is **stored per contract**, not re-derived from `signed_gex`. Deriving
it would break for contracts whose gamma rounds to zero at the current spot —
exactly the far-wing strikes that come alive once the grid moves spot toward
them.

---

## View 1 — unsigned concentration

```
total_unsigned = Σ GEX_i
```

Needs no dealer-positioning assumption at all. The least model-sensitive view in
the engine.

## View 2 — naive signed

```
total_signed = Σ signed_GEX_i
```

The classic public proxy. Not dealer inventory. `|signed| ≤ unsigned` always
holds, since calls and puts partly cancel.

## View 3 — expiry buckets

| Bucket | Calendar DTE |
|---|---|
| `0DTE` | 0 |
| `1_2_DTE` | 1–2 |
| `3_5_DTE` | 3–5 |
| `6_30_DTE` | 6–30 |
| `GT_30_DTE` | > 30 |

All five are **always present and zero-filled**. A consumer must be able to tell
"no 0DTE gamma today" from "this snapshot didn't compute the bucket".

```
dte0_dominance = unsigned_GEX(0DTE) / Σ unsigned_GEX(all buckets)
```

Bucketing uses calendar DTE (0DTE means "expires today"), deliberately distinct
from the continuous `seconds_to_expiry` used for pricing.

## View 4 — strike level

Per strike, call and put legs kept separate:

```
call_gex(K)     = Σ GEX_i over calls at K
put_gex(K)      = Σ GEX_i over puts at K
unsigned_gex(K) = call_gex(K) + put_gex(K)
signed_gex(K)   = Σ signed_GEX_i at K
```

### Walls

Two layers, deliberately separated.

**Neutral observations** — facts:

```
largest_call_gamma_strike    = argmax_K call_gex(K)
largest_put_gamma_strike     = argmax_K put_gex(K)
largest_unsigned_gamma_strike = argmax_K unsigned_gex(K)
```

**Directional interpretations** — claims, and only valid on the right side of
spot:

```
upside_call_wall  = argmax{ call_gex(K) : K > S + buffer }   or None
downside_put_wall = argmax{ put_gex(K)  : K < S − buffer }   or None
```

Returning `None` rather than falling back to the other side is the point. A
"resistance" level below the market is not resistance, and silently supplying one
is worse than supplying nothing.

Ties break to the **lower strike**. `max()` returns whichever equal element it
met first, which depends on aggregation order — replay determinism needs a stated
rule.

All of this reads gamma, never raw open interest. A strike can carry enormous OI
in far-dated series contributing almost no gamma; an OI-ranked "wall" points at a
level with no hedging pressure behind it. On the synthetic fixture, OI peaks at
5100 while gamma-weighted GEX peaks at 5025.

### Gamma voids

A contiguous run of strikes with `unsigned_gex(K) ≤ void_max_share_of_max · max`.
Each is then classified against an **expected strike ladder** whose modal spacing
is the median of observed gaps (median, not mean — robust to the missing strikes
this exists to detect):

| Kind | Condition | Tradable? |
|---|---|---|
| `TRUE_LOW_GEX_VOID` | ladder coverage ≥ 80%, ≥2 strikes observed | ✅ |
| `MISSING_STRIKE_DATA` | coverage < 80%, gaps not uniformly wider | ❌ |
| `IRREGULAR_STRIKE_SPACING` | ≥2 consecutive gaps of the same wider size | ❌ |
| `FILTERED_STRIKE_REGION` | outside the configured band | ❌ |
| `INSUFFICIENT_COVERAGE` | no inferable ladder, or too few strikes | ❌ |

A single wide gap is an omission; a *repeated* wide gap is a coarser increment.
One gap is never enough evidence that the ladder changed, and the safe direction
is "missing" — that classification carries a warning, "irregular" does not.

## View 5 — zero-gamma grid

1. Grid: `S* ∈ [S(1−span), S(1+span)]` in steps of `S·step`. The realised span is
   quantised to a whole number of steps; keeping the *step* exact matters more,
   because that is what interpolation accuracy depends on and it must stay finer
   than the strike ladder.
2. Reprice gamma at each `S*` under the stated IV convention.
3. `Total_signed_GEX(S*) = Σ gamma_i(S*) · OI_i · M · sign_i · S* · (spot_move_pct · S*)`
4. Interpolate every sign change:
   ```
   root = S1 + (S2 − S1) · (−y1) / (y2 − y1)
   ```

**An identically-zero curve has no roots.** Without that guard, an empty contract
set reports a root at every grid point and the nearest-to-spot pick returns spot
itself — a fabricated level that looks entirely plausible downstream.

### Diagnostics

```
selected_root          nearest to spot -- a reporting convention, stated as such
all_roots              every crossing, retained
local_slope            (y2 − y1)/(S2 − S1) across the bracketing interval
normalised_slope       slope · (S/100) / max|GEX|   -- comparable across chains
nearest_root_spacing   gap to the next root, as % of spot
root_near_boundary     within boundary_tolerance_pct of the grid edge
grid_expansions        bounded adaptive widening (default max 3, factor 1.75)
```

A shallow crossing moves a long way for a small data change; a boundary root may
be an artefact of where the search stopped. Both are reported rather than hidden.

### Smile fit

Least-squares quadratic in standardised moneyness:

```
m = ln(K/S) / √T
σ(m) = a + b·m + c·m²
```

Solved via 3×3 normal equations with partial pivoting; returns `None` when
singular. Output clamped to `[1e-4, 5.0]`.

Fitted per `(expiry, time_to_expiry)` — SPX and SPXW sharing an expiry *date* but
settling hours apart never get pooled.

---

## Option universe accounting

```
included_unsigned_gex_share = included_unsigned / (included + excluded)
coverage_ratio              = included_count / total_count
```

Reported once for the chain totals and once for the zero-gamma grid, because they
run on different populations. Comparing a chain total against a DTE-capped
zero-gamma level without saying so invites a false conclusion about how much
gamma the level accounts for.

---

## Confidence

Weighted mean over 17 components, scaled to 0–100:

```
score = 100 · Σ(component_score · weight) / Σ(weight)
```

A **hard failure** zeroes the whole score rather than reducing it. Letting a
snapshot with a future-dated feed score 85/100 because everything else was fine
is exactly the averaged-away warning this model exists to prevent.

Most components use linear decay:

```
score = clamp(1 − value/zero_at, 0, 1)
```

Uncalibrated thresholds use a documented, deliberately pessimistic placeholder
and set `uncalibrated=True`, so an unresearched system looks worse than it is
rather than better.

---

## Session ageing

```
sessions_between(start, end) = |{d : start < d ≤ end, d is a trading session}|
```

Bounded at 400 days — an `open_interest_as_of` that is years stale is a bug, and
looping over it should not hang the ingest loop.

Holidays from NYSE rules; Good Friday from the anonymous Gregorian computus.
