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

## 11. Chain completeness has no independent source — **PARTIALLY OBSERVED**

**The question.** How do we know a vendor response was not silently truncated?

**What v2 did.** `expected_contract_count = len(quote_rows)` — the length of the
response being judged. Every response was therefore 100% complete by
construction, including a truncated one.

**Current behaviour.** `ChainCompleteness` requires the expectation to come from
somewhere else and records where (`expected_source`). No ThetaData contract-list
endpoint is wired in this release, so with no caller-supplied universe the
status is reported as `PARTIALLY_OBSERVED` — never `COMPLETE`.

**Why not resolved.** ThetaData v3 exposes no contract-list endpoint that this
repository has verified. Inventing a URL and shipping it as though it were
confirmed would be worse than reporting the limitation.

**What would settle it.** A live account, one call to whatever list endpoint the
subscription actually exposes, and its response shape recorded as a fixture.
Then pass those identities as `expected_contract_ids` with
`expected_source="contract_list"` and completeness becomes measurable.

---

## 12. Retry-After cap — **RESOLVED BY POLICY**

**The question.** A vendor `Retry-After: 99999` asks this process to block for
27 hours. Honour it?

**Current behaviour.** Honoured up to `RetryPolicy.max_retry_after_seconds`
(default 120 s), then capped. Beyond the cap the request fails rather than
sleeping.

**Why this is a decision.** Honouring the header exactly is the polite reading of
the spec, but it lets a remote server decide how long local code blocks. The cap
is a local availability choice, not a vendor-compatibility claim.

**What would settle it.** ThetaData's documented rate-limit semantics, and
whether a long `Retry-After` there means "wait" or "you are cut off".

---

## 13. Duplicate rows — **SAFEST BEHAVIOUR CHOSEN, configurable**

**The question.** Two rows claim the same contract with different values. Which
one is true?

**Current behaviour.** `duplicate_policy: "reject"` — the chain fails. The
alternatives (`first`, `last`) exist and are configurable, but the default
refuses to guess, because taking either silently produces a chain whose numbers
depend on response ordering.

**What would settle it.** Whether ThetaData's snapshot endpoints can legitimately
emit duplicates at all (e.g. multi-exchange rows), and if so what distinguishes
them. If they can, the right fix is a documented tie-break on that field, not a
positional one.

---

## 14. Local strike spacing window — **HEURISTIC, uncalibrated**

**The question.** SPX ladders are 5-wide near the money and 25-wide in the
wings. A single global spacing misreports gaps at every strike outside the
region it was fitted to.

**Current behaviour.** `StrikeLadder` infers spacing piecewise from a rolling
local median over `LOCAL_WINDOW = 4` neighbours.

**Why this is a decision.** 4 is a smoothing choice. Too small and one missing
strike redefines the local spacing; too large and the estimate smears across a
genuine regime change in the ladder.

**What would settle it.** Real SPX/SPXW chains across several expiries, checking
where the inferred spacing disagrees with the exchange's published increments.

---

## 15. Root topology matching tolerance — **HEURISTIC, uncalibrated**

**The question.** Two consecutive snapshots produce zero-gamma roots at 5050.0
and 5050.5. Is that the same root that drifted, or a different root?

**Current behaviour.** `match_roots()` pairs roots across snapshots by proximity
and `score_root_identity_stability` penalises matched drift mildly and
appearance/disappearance heavily.

**Why not resolved.** The distinction is only knowable from the underlying
surface, not from the root list. The current pairing is a nearest-neighbour
heuristic that is stable on synthetic data.

**What would settle it.** Intraday sequences of real chains, where a root that
genuinely persists can be followed by eye.

---

## 16. `CALENDAR_MIDNIGHT` expiration rule — **REJECTED, not implemented**

**The question.** v2 offered a `CALENDAR_MIDNIGHT` expiration rule.

**Current behaviour.** The rule is declared but `is_supported` is `False` and
resolution refuses it with `ExpirationTimestampRule.unsupported_reason`.

**Why.** No listed index option expires at midnight. Offering it as a selectable
rule implied a settlement convention that does not exist, and any operator who
chose it would have got silently wrong time-to-expiry for every contract.

**What would settle it.** Nothing — the rule is wrong rather than unverified. It
remains declared only so that an existing config naming it fails loudly rather
than being ignored.

---

## 17. Ambiguous local times during the DST fall-back — **EXPLICIT, fold-aware**

**The question.** A vendor wall-clock timestamp of `01:30` on the fall-back
Sunday occurs twice. Which one?

**Current behaviour.** `parse_vendor_timestamp(..., fold=...)` makes the choice
explicit and records it; `strict_dst=True` refuses the ambiguity outright rather
than silently resolving to the first occurrence.

**Why this matters even though markets are shut at 01:30.** History endpoints
and any future overnight session would hit it, and a silent default here is
exactly the class of bug that only manifests twice a year.

**What would settle it.** Confirmation of whether ThetaData emits UTC internally
and formats to Eastern (in which case the ambiguity is theirs to resolve and
they may expose the offset), or stores wall-clock.

---

## 18. Unknown completeness is scored as `None`, not as a number — **RESOLVED BY POLICY**

**The question.** When the chain's universe is unknown, what should the
`chain_completeness` confidence component contribute?

**Current behaviour.** `score = None`. The component is excluded from the
weighted mean, marked `uncalibrated`, and emits the deterministic code
`CHAIN_COMPLETENESS_NOT_INDEPENDENTLY_OBSERVED`.

**Why not a conservative constant.** Any constant is an assertion. 0.0 says the
chain is bad; 1.0 says it is good; 0.5 says it is half good. All three are
claims the data does not support, and picking one would be exactly the
"arbitrary calibrated value" this repository refuses elsewhere.

**Consequence.** The overall score is computed over fewer components, so it is
renormalised rather than dragged in either direction — and the snapshot reports
`calibrated = False` so nothing can mistake it for a measured result.

**What would settle it.** A verified contract-list endpoint (see OD-11). Then
completeness is measured and the question does not arise.

---

## 19. `reject` and `collapse_exact` behave identically today — **DELIBERATE**

**The question.** Should `duplicate_policy: reject` refuse *any* duplicate
identity, including byte-identical rows?

**Current behaviour.** No. Both `reject` and `collapse_exact` collapse
byte-identical rows and refuse rows that disagree. `collapse_exact` exists as
the explicit spelling of that behaviour.

**Why.** Two identical rows carry no conflicting information — there is nothing
to arbitrate, and no ordering dependence to worry about. Failing on them would
reject a chain over a vendor retransmission.

**Consequence.** An operator choosing between the two names gets the same
behaviour. That is a naming redundancy, not a silent difference.

**What would settle it.** Whether ThetaData's snapshot endpoints can emit
duplicates at all, and if so whether an exact repeat is normal or a symptom.

---

## 20. Underlying-price fallback policy — **NOT IMPLEMENTED, deliberately**

**The question.** When `underlying_price_source = VENDOR_PER_CONTRACT` and a
contract has no per-contract underlying, should the chain-level spot be used?

**Current behaviour.** No. The contract is excluded from current GEX with the
machine-readable reason `no_underlying_price`, and the resolved spot is `None`
rather than a placeholder.

**Why this is a decision.** GEX scales by spot squared, so substituting a
different underlying silently reprices the contract. v2.1 recorded the issue and
then returned `snapshot.spot` anyway, under a comment saying it did not.

**What is NOT implemented.** A configured fallback source. Adding one would
require naming it in `ModelSpec`, recording which contracts used it, and
hashing it into the model fingerprint. Until an operator asks for it with a
reason, the safest behaviour is to exclude and count.

**What would settle it.** Evidence of how often ThetaData omits the
per-contract underlying, and whether the omissions cluster (illiquid wings) or
scatter.

---

## 21. Realism warnings are not thresholds — **UNCALIBRATED by design**

**The question.** A zero risk-free rate is fully specified but implausible for
a USD index chain. What should the engine do about it?

**Current behaviour.** `EffectiveModelInputs.realism_warnings` emits
`MODEL_REALISM_WARNING` naming the field, separately from `missing_inputs`.
Nothing is scored on it and nothing is blocked.

**Why separate.** v2.1 asked `if spec.risk_free_rate == 0.0` and called the
result *missing* — so a deliberately configured zero, chosen via
`RateSource.ZERO`, was reported as an unspecified parameter. The operator was
told to configure something they had already configured, and the only way to
satisfy the check was to change the number.

**What is explicitly not claimed.** That zero is wrong. Only that it is
unusual, and unusual is a different question from unspecified.

**What would settle it.** Nothing in this repository — it is a market
observation, and the threshold for "implausible" would be a calibrated value.

---

## Deferred, with reasons

| Item | Why deferred | Revisit when |
|---|---|---|
| `SURFACE_REFIT` IV convention | Full per-grid-point surface re-estimation; the other three conventions have to disagree materially first | The convention spread is large enough that which one is right matters |
| numpy vectorisation of the grid | A dependency-free core is worth more during development than the speed | The sub-1s SLA binds; currently ~1–2 s for a real chain |
| Cboe DataShop Open-Close | From $2,499/mo, and only needed for the flow-adjusted sign model | A specific hypothesis justifies the price |
| Futures data (Databento) | Out of scope for this pass | Feature store needs VWAP/realised vol |
| Risk engine, broker, strategies | Out of scope, and deliberately absent | Never, from this repository |
