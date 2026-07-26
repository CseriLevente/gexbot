# Open decisions

Every ambiguity that was resolved by exposing a configurable interface rather
than by silently picking a financial assumption. Each entry states what is
currently configured, why, and what evidence would settle it.

Ordered by how much the answer could change a number.

---

## 1. Minimum time-to-expiry floor — **UNRESOLVED, configurable**

**The question.** Gamma diverges as `T → 0` for an at-the-money option. On
expiration day that singularity is real, but it makes the aggregate explode and
the zero-gamma root-finder unstable. Where should `T` be floored?

**Current setting.** `model.min_time_to_expiry_minutes: 60.0`.

**Why not resolved.** There is no correct answer available from first principles.
The floor is a modelling choice about how much of a real singularity to admit.

**What was done instead.** The floor is a `ModelSpec` field, it is hashed into
the model fingerprint, and `compute_floor_sensitivity()` re-runs the engine
across ~0 / 30 / 60 minutes and reports the spread. On the synthetic fixture 15
minutes before settlement, the 0DTE bucket moves materially across those floors;
five hours before settlement it does not move at all, because no candidate floor
binds.

**Explicitly not claimed.** The 60-minute default has **not** been verified
against ThetaData's own short-dated handling. Do not describe the engine as
vendor-compatible on this point.

**What would settle it.** Pull real 0DTE chains through both paths on expiration
afternoons and compare our gamma against vendor gamma as a function of remaining
time. If the vendor's implied floor is recoverable, match it and record that it
was matched.

---

## 2. Vendor timestamp timezone — **RESOLVED BY ASSUMPTION, documented**

**The question.** ThetaData v3 emits timestamps as wall-clock strings with no
offset. Which zone are they in?

**Current behaviour.** `src/adapters/thetadata/client._to_datetime` attaches US
Eastern.

**Why this is a decision and not a fact.** It is an inference from the venue,
not something the payload states. The engine itself refuses naive datetimes
precisely so this assumption has to be made somewhere visible rather than
drifting into the maths.

**Consequence if wrong.** A four- or five-hour error in time-to-expiry, which on
0DTE does not produce a slightly wrong gamma — it produces a completely wrong
one.

**What would settle it.** One live response compared against a known wall-clock
instant.

---

## 3. Local gamma vs vendor gamma — **NOT VALIDATED**

**Status.** The fixture cross-check in `tests/unit/test_thetadata_adapter.py`
compares our gamma against a gamma column that *we generated with our own
pricer*. It is a consistency check on the settlement clock, day count and floor.
It is **not** evidence that our gamma matches live ThetaData output.

**Why it matters.** The recommendation to buy the Standard tier and compute gamma
locally rests on the two agreeing. That has never been measured.

**What was done instead.** `GammaComparison` in `src/domain/iv.py` carries
`local_gamma`, `vendor_gamma`, absolute and relative differences and a
`comparison_status`, sliceable by DTE, moneyness, right and IV level.
`formulas.gamma_comparisons()` produces them whenever vendor gamma is present.
Nothing requires Pro access for normal operation.

**What would settle it.** One Pro-tier day, second-order greeks pulled alongside
first-order, comparison report generated across the slices.

---

## 4. Sticky-delta — **NOT IMPLEMENTED, renamed**

**The question.** The v1 engine exposed a convention called `STICKY_DELTA` that
shifted IV using log-moneyness. That is not sticky-delta.

**Resolution.** Renamed to `STICKY_MONEYNESS`, which is what it does: a smile
fitted in standardised log-moneyness translates with spot. `STICKY_DELTA` still
exists in the enum so it can be *requested and explicitly refused* — configuring
it raises a `ConfigError` naming the approximation, and requesting it
programmatically returns an unresolved result carrying the reason.

**Why not just implement it.** A true sticky-delta model parameterises the
surface in delta coordinates, which needs an iterative solve because delta itself
depends on the volatility being solved for, plus deterministic convergence limits
and a failure state. That is a real piece of work and it should be done
deliberately, not smuggled in under a name that already exists.

---

## 5. Sign convention — **PROXY, never resolved**

`DEALER_LONG_CALLS_SHORT_PUTS` is the classic public convention and the default.
It is an assumption about who holds what, not an observation.

`sign_model_agreement` scores **zero** and says why, because no second model
exists to compare against. That is the correct answer, not a penalty-free pass: a
single unverified sign model is the largest unquantified risk in the whole
engine.

**What would settle it.** Cboe Open-Close (participant type, buy/sell,
open/close) to build a flow-informed second model. `compute_gex_snapshot` already
accepts `flow_adjusted_signed_gex` for exactly this.

---

## 6. Confidence thresholds — **UNCALIBRATED by design**

Three thresholds are market claims and remain `UNSPECIFIED_CALIBRATE`:

| Threshold | What it would assert |
|---|---|
| `max_zero_gamma_shift_pct` | how much convention disagreement makes a level untradeable |
| `max_sign_model_disagreement` | how far two sign models may diverge |
| `max_0dte_dominance_ratio` | when same-day gamma masks the longer-dated structure |

Everything else in the confidence config is a data-plumbing fact with a
defensible value ("a 60-second-old option snapshot is stale" is true whatever the
strategy turns out to be) and therefore carries a real number.

**Correction to earlier documentation.** The v1 README said `calibrated` is
"enforced by a risk engine" and that live trading is "blocked". **That was
wrong.** There is no risk engine in this repository and nothing consumes the
flag. It is a research signal. Nothing is blocked because nothing can trade.

---

## 7. Void classification: one wide gap vs a coarser ladder

**The ambiguity.** A gap wider than the modal strike spacing has two very
different causes: the vendor omitted strikes, or the chain genuinely uses a
coarser increment there (SPX really does, in the wings).

**Resolution.** A *single* wide gap is classified `MISSING_STRIKE_DATA`; two or
more consecutive gaps of the same wider size are `IRREGULAR_STRIKE_SPACING`. One
gap is not evidence that the ladder changed.

**Why this direction.** `MISSING_STRIKE_DATA` is not tradable structure, so
guessing wrong toward "missing" costs a false negative. Guessing wrong toward
"irregular" would strip the warning off a region where we simply have no data.

---

## 8. Root selection: nearest to spot

When the curve crosses zero more than once, `selected_root` is the crossing
nearest spot. This is a **reporting convention**, stated as such in
`selection_method`. Every root is retained in `all_roots`, and the confidence
model penalises both the count and the tightness of the spacing.

**Not resolved:** whether nearest-to-spot is the right convention at all. A
larger-gamma or steeper crossing further away may matter more.

---

## 9. Holiday calendar scope

`src/gex/calendar.py` encodes the NYSE rules from 2022 onward (post-Juneteenth)
and raises for earlier years rather than guessing. Ad-hoc closures — days of
mourning, weather — cannot be derived from rules and must be injected via
`add_ad_hoc_closure()`.

**Consequence:** a research window crossing an unregistered ad-hoc closure will
age open interest by one session too few.

---

## 10. Snapshot hash quantisation

`output_hash()` rounds floats to 12 significant figures before hashing.

**The trade.** Full float repr would make the digest sensitive to last-bit
summation differences between platforms and libm versions, so "same data, same
hash" would hold on one machine and fail on another. Twelve significant figures
is far tighter than any change of substance.

**Consequence:** a change smaller than 1 part in 10¹² will not move the hash. No
such change is meaningful for a GEX total measured in billions.

---

## Deferred, with reasons

| Item | Why deferred | Revisit when |
|---|---|---|
| `SURFACE_REFIT` IV convention | Full per-grid-point surface re-estimation; the other three conventions have to disagree materially first | The convention spread is large enough that which one is right matters |
| numpy vectorisation of the grid | A dependency-free core is worth more during development than the speed | The sub-1s SLA binds; currently ~1–2 s for a real chain |
| Cboe DataShop Open-Close | From $2,499/mo, and only needed for the flow-adjusted sign model | A specific hypothesis justifies the price |
| Futures data (Databento) | Out of scope for this pass | Feature store needs VWAP/realised vol |
| Risk engine, broker, strategies | Out of scope, and deliberately absent | Never, from this repository |
