# GEX engine v1 — specification and known limitations

Implementation of views 1–5 from the roadmap, plus the confidence score.
Source of truth is the code; this document records the decisions and the caveats
that the code cannot state for itself.

## Core formula

```
GEX_i = gamma_i · OI_i · M · S · ΔS,    ΔS = spot_move_pct · S
```

With the 1% convention this is `gamma · OI · M · S² · 0.01`. Read it as: dollars
of dealer delta that must be re-hedged for a 1% move in spot. `M = 100` for
SPX/SPXW.

Sign is applied on top of magnitude and is always a *proxy*. Every stored snapshot
records which `SignConvention` produced it, because a signed GEX number without its
assumption is not interpretable.

## The five views

| View | Module | Notes |
|---|---|---|
| 1. Unsigned concentration | `formulas.total_unsigned_gex` | Needs no dealer-positioning assumption. The least model-sensitive view, and the one to trust for level mapping. |
| 2. Naive signed | `formulas.total_signed_gex` | Classic public proxy. Not dealer inventory. |
| 3. Expiry buckets | `formulas.aggregate_by_bucket` | `0DTE / 1_2 / 3_5 / 6_30 / GT_30`. Always all five, zero-filled when empty. |
| 4. Strike level | `formulas.aggregate_by_strike`, `walls` | Walls from strike-aggregated gamma, never raw OI. |
| 5. Zero-gamma grid | `zero_gamma` | Repriced under three IV conventions; disagreement is reported, not averaged. |

### Why buckets are zero-filled

A consumer reading `snapshot.bucket(DTE_0)` must be able to distinguish "no 0DTE
gamma today" from "this snapshot didn't compute the 0DTE bucket". Returning `None`
for an empty bucket conflates the two.

### Why walls ignore open interest

A strike can carry enormous OI in far-dated series that contribute almost no
gamma. An OI-ranked "wall" points the strategy at a level with no hedging pressure
behind it. `tests/unit/test_formulas.py::test_gamma_weighted_peak_is_not_the_raw_open_interest_peak`
demonstrates the gap on the synthetic chain: OI peaks at 5100, gamma-weighted GEX
peaks at 5025, because near-dated ATM gamma dominates.

## IV conventions on the zero-gamma grid

The roadmap lists four. Their operational definitions here:

| Convention | σ at grid point `S*` | Status |
|---|---|---|
| `FROZEN_IV` | each contract's raw snapshot IV, unchanged | baseline |
| `STICKY_STRIKE` | fitted per-expiry smile evaluated at the contract's *original* moneyness | **default** |
| `STICKY_DELTA` | fitted smile evaluated at the contract's *new* moneyness under `S*` — the smile travels with spot | implemented |
| `SURFACE_REFIT` | full surface re-estimation per grid point | **not implemented** |

The roadmap describes `frozen_iv` and `sticky_strike` in terms that are
operationally identical (both hold IV at the strike). The distinction drawn here
is raw vs. fitted: `STICKY_STRIKE` reads σ off a least-squares quadratic in
standardised moneyness `m = ln(K/S)/√T`, so one torn quote cannot move the level.
That makes it the denoised sibling of `FROZEN_IV` rather than a duplicate, and
`test_sticky_strike_tracks_frozen_iv_closely_on_a_smooth_smile` pins the
relationship.

`SURFACE_REFIT` returns an unresolved result with `no_crossing=True` and the engine
emits a warning. It does not return a fabricated number.

### Convention disagreement is the output, not the noise

`GexSnapshot.zero_gamma_spread_pct` is the max disagreement between conventions as
a percentage of spot. This is the honest error bar on the level, and it feeds
`zero_gamma_stability` in the confidence score. Averaging the conventions together
would discard exactly the information a strategy needs to know how hard to lean on
the level.

## Known limitations

### 1. Time-to-expiry floor on 0DTE

Gamma diverges as `T → 0` for an ATM option. The singularity is real, not a bug,
but it makes aggregate GEX explode and the root-finder unstable. `T` is floored at
**30 minutes of a 365-day year** (`MIN_TIME_TO_EXPIRY_YEARS`).

**Calibration target:** sweep this floor and confirm neither the zero-gamma level
nor the 0DTE bucket weight is dominated by it. If they are, the floor is doing the
modelling.

### 2. 0DTE gamma bell can be narrower than the strike spacing

On the synthetic fixture at 5 hours to expiry, `σ√T · S ≈ 22` index points, against
a 25-point SPX strike interval. The 0DTE gamma contribution is therefore sensitive
to whether a grid point happens to coincide with a strike.

Consequences to be aware of:
- The zero-gamma curve can be locally jagged on expiration afternoons.
- `grid_step_pct` interacts with strike spacing. At `0.001` (5 points on a 5000
  index) the grid is finer than the strikes, which is the safer direction.

**Not yet addressed.** Options if it proves material: intra-strike smoothing, or
weighting the 0DTE bucket by a wider effective kernel. Do not fix it before
measuring it on real chains.

### 3. Performance is not optimised

Pure-stdlib maths, no numpy. Cost is `contracts × grid_points × conventions`. With
`max_dte_for_grid: 60` a real SPX + SPXW chain is on the order of 2,000 contracts ×
81 points × 3 conventions ≈ 500k Black-Scholes evaluations, roughly 1–2 s.

Acceptable for a 5-minute decision cycle; **above the roadmap's sub-1s end-to-end
SLA.** The vectorisation path is straightforward when needed: gamma depends only on
`(K, T, σ)` and is identical for calls and puts, so the grid reduces to an array
operation. Deliberately deferred — a dependency-free core is worth more during
development than the speed is.

### 4. No holiday calendar

`weekdays_between` (used to age open interest) counts weekdays, not sessions. OI
can look one session older than it is across an exchange holiday. Errs toward
caution. The real calendar belongs in `reference_service`.

### 5. Flow-adjusted sign model is a hook only

`compute_gex_snapshot(..., flow_adjusted_signed_gex=...)` accepts a second signed
estimate but nothing produces one yet — it needs Cboe Open-Close. Until then
`sign_model_agreement` scores zero and reports why.

## Time and settlement

`src/gex/sessions.py` implements US Eastern directly rather than via
`zoneinfo.ZoneInfo("America/New_York")`, because `zoneinfo` needs the `tzdata`
package on Windows and raises without it. A trading engine must not depend on
whether an optional data wheel got installed, and being one hour out on an SPXW
expiration afternoon does not produce a slightly wrong gamma — it produces a
completely wrong one.

The post-2007 US DST rule is hard-coded, and
`test_matches_zoneinfo_when_tzdata_is_available` cross-checks two years of daily
offsets against the real tz database whenever `tzdata` *is* present, so the two
cannot silently diverge in production. Years before 2007 are rejected outright.

Settlement clocks:

| Root | Settlement | Why |
|---|---|---|
| `SPXW` | 16:00 ET on expiry | PM-settled; the expiring series trades to the close |
| `SPX` | 09:30 ET on expiry | AM-settled; SET is struck from expiration-morning opening prices |

This is why SPX and SPXW are separate roots and never pooled — including in smile
fitting, where a shared expiry *date* with different settlement *times* would blend
two surfaces.

## Calibration contract

`UNSPECIFIED_CALIBRATE` is a sentinel object, not a magic number. It is falsy and
**raises `TypeError` on comparison**, so an uncalibrated threshold cannot silently
participate in a `>=` and pass.

Components whose thresholds are still sentinels are computed with a documented,
deliberately pessimistic placeholder and flagged `uncalibrated`. Any flagged
component makes `ConfidenceScore.calibrated` False, which the risk engine must
treat as a hard block on live orders.

The result: **the engine produces research output on day one and cannot trade until
the calibration work is done.** Three components are genuine calibration targets
(`zero_gamma_stability`, `sign_model_agreement`, `0dte_dominance_alert`) because
their thresholds are claims about the market. The other five measure data quality,
where a defensible value exists without any backtest — a 60-second-old option
snapshot is stale whatever the strategy turns out to be.

## Determinism

`compute_gex_snapshot` is a pure function of its inputs: no clock reads, no
network, no randomness, no dict-ordering dependence. `test_engine.py` asserts that
the same chain produces an identical snapshot, and that rebuilding the fixture from
scratch produces an identical snapshot.

This is the property the whole validation layer rests on. Anything added to the
engine that breaks it — a `datetime.now()`, a set iteration, a cache — breaks
point-in-time backtesting and replay along with it.
