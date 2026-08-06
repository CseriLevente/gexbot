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

**Current behaviour.** `src/domain/vendor_time.parse_vendor_timestamp` attaches
US Eastern. Since v2.1.6 that is the *only* place a zone may be assumed, and it
is used by chain normalization, field observation, validation comparison, spot
synchronisation and replay alike.

**Why this is a decision and not a fact.** It is an inference from the venue,
not something the payload states. The engine itself refuses naive datetimes
precisely so this assumption has to be made somewhere visible rather than
drifting into the maths.

**Consequence if wrong.** A four- or five-hour error in time-to-expiry, which on
0DTE does not produce a slightly wrong gamma — it produces a completely wrong
one.

**Why it had to be centralised (v2.1.6).** There were two implementations. The
adapter localised a naive vendor string to Eastern; `src/adapters/validation.py`,
written later, read the same string as UTC. So `"2026-03-17T11:00:00.000"` was
15:00 UTC when the adapter normalised a chain and 11:00 UTC when the validator
re-read the same bytes to check it. Four hours — and the module disagreeing was
the one whose job is to catch disagreements. A parsed timestamp now carries the
raw text, the zone assumed, the normalised UTC instant, whether localisation was
applied, and how any ambiguity was resolved.

**The DST fold policy (v2.1.7).** On the autumn transition Sunday the wall clock
01:30 occurs twice: once at `-04:00` and again an hour later at `-05:00`. The
choice is recorded as an `ambiguity_resolution` value (`EARLIER` / `LATER`) *and*
carried as `datetime.fold`, because the zone is now
`zoneinfo.ZoneInfo("America/New_York")` and honours it. A nonexistent
spring-forward reading is normalised and labelled
`NONEXISTENT_WALL_CLOCK_NORMALISED` rather than silently accepted.

**The reversal, and why.** v2.1.6 implemented US Eastern by hand, to keep the
engine core free of the `tzdata` wheel. The DST rule has been stable since 2007
and re-implementing it is twenty lines, so the rule was never the problem — the
*representation* was. A hand-written `tzinfo` resolves its offset from the wall
clock, and a wall clock that occurs twice cannot tell the two occurrences apart.
Converting the second instant back into Eastern returned `02:30-05:00`: the
right offset on the wrong hour, an hour of error on an instant the IANA database
has always had correct.

`tzdata` is now a pinned dependency. It is data, not code: the engine core still
executes nothing third-party, which is the property the bare-interpreter check
actually protects, and the CI job makes exactly that one exception. Carrying a
wrong instant to avoid a data dependency was the wrong trade.

**What would settle the underlying question.** One live response compared
against a known wall-clock instant. The zone is now right; which zone the vendor
*means* is still an inference.

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

**What v2.1.11 changed.** Who may authorize a universe, and what a document
has to do to state one.

Coverage being a resolver output is worth nothing if the resolver's *output
type* is the thing the capture checks. `VerifiedExpectedUniverseArtifact` is a
public frozen dataclass, and v2.1.10's `capture_session` checked `isinstance`,
so a caller could construct one claiming `AUTHORITATIVE_DOCUMENTATION` and
`FULL_REQUEST_ENUMERATED` against an evidence id nobody had registered. A
capture is now opened against a `UniverseResolution` -- the declaration and the
source capture -- which the pipeline re-runs before the chain operation opens.

Three narrower things followed:

* a universe source must come from a capture that passed `verify_capture`, and
  each named record must be 2xx, completely written and read by a supported
  parser. Hashing to your own descriptor is a statement about storage;
* the source scope and pipeline fingerprint are reconstructed from the stored
  request rather than taken from the declaration, so `min_time` -- which decides
  which contracts come back -- reaches the comparison;
* a documentation rule can no longer carry an identity list. It names a document
  and a versioned extractor, and the identities are what that extractor reads
  out of the verified bytes, recorded with the character ranges it read them
  from. Effective periods are checked against the market session.

**None of this makes a full universe reachable.** `VENDOR_CONTRACT_LIST` and
`CAPTURED_PAGINATION_METADATA` are still unsupported for want of an endpoint,
and the universe documentation registry is still empty for want of a document.
The shipped profile is unchanged: `NOT_READY_FOR_ANALYTICAL_DATASET`.

**What v2.1.10 changed.** Coverage is a *resolver output*, and the resolver
refuses what it cannot establish.

The v2.1.9 resolver re-derived identities from the records a universe named --
necessary, and not the question. Proving a set of identities occurs in stored
records is not proving those records enumerate the **complete universe the
request should have returned**, and a truncated response enumerates its own rows
perfectly. So an `/v3/option/snapshot/quote` response, labelled
`VENDOR_CONTRACT_LIST`, established `MEASURED_COMPLETE` for the whole chain.

`UniverseCoverageStatus` names what a source actually reached:

| Status | What may rest on it |
|---|---|
| `FULL_REQUEST_ENUMERATED` | `MEASURED_COMPLETE`, and analytical readiness |
| `PARTIAL_PAGE` | a missing listed contract is a finding; completeness is not |
| `OBSERVED_SUBSET` | diagnostics. The rows arrived; nothing says which were owed |
| `UNKNOWN_COVERAGE` | nothing |

A caller may record what it *expected* (`declared_coverage`) and nothing reads
it. `complete_for_request: bool` is gone: it was a constructor argument hashed
into the universe, which made an assertion look like a finding.

**Which sources can reach which state today.** `ResponseCapabilities` separates
four questions a response was previously asked as one — does it enumerate rows,
does it enumerate the *request's* universe, does it carry pagination metadata,
is it a dedicated listing endpoint. Every ThetaData snapshot answers yes to the
first and no to the rest, so:

* `VENDOR_CONTRACT_LIST` — **unsupported**. No verified listing endpoint exists;
* `CAPTURED_PAGINATION_METADATA` — **unsupported**. No verified response returns
  a page, a total or a continuation token, and v2.1.9's resolver for this kind
  read none of them;
* `AUTHORITATIVE_DOCUMENTATION` — supported, and the registry is empty;
* `OBSERVED_SNAPSHOT_ROWS` — supported, reaching `OBSERVED_SUBSET`. Honest,
  useful, and not completeness.

**So no production capture can measure completeness**, and the shipped profile
is `NOT_READY_FOR_ANALYTICAL_DATASET`. That is the same limitation this decision
has always recorded, now stated by a check rather than by prose.

**What v2.1.9 changed.** There is exactly **one**
`ExpectedContractUniverse` — there were two, and the one the engine read carried
no provenance — and a universe is *resolved* before it can measure anything:
`src/adapters/universe_resolvers.py` reopens the records it names, re-derives the
contract identities from those bytes and compares. `source` was a string a caller
typed and `source_record_ids` was read as a boolean, so a hand-written list
labelled `vendor_contract_list` established `MEASURED_COMPLETE` exactly as a real
listing would.

`complete_for_request` is also read now. It existed on the type and nothing
consulted it, so page one of a paginated listing whose members all arrived
reported the entire chain complete. Two statuses distinguish the cases:
`PARTIAL_UNIVERSE_ALL_LISTED_PRESENT` and
`PARTIAL_UNIVERSE_MISSING_IDENTITIES`, neither of which implies completeness.

A `CALLER_DECLARED` universe resolves — somebody really did state a list — and
establishes nothing. It permits raw capture and diagnostics and cannot support
measured completeness or analytical readiness.

**What v2.1.8 changed.** The universe is a typed `ExpectedContractUniverse`
declared on the *capture session*, and its hash is stamped on every record. A
universe supplied only at calculation time is refused, and so is dropping one
the capture declared.

The reason is that the universe decides completeness, which feeds the confidence
score, which decides whether a dataset is fit to build on. In v2.1.7 it was an
argument to the calculation, so the same capture could be scored
`MEASURED_COMPLETE` against one universe and replayed `PARTIALLY_OBSERVED`
against another — two answers from the same bytes, and nothing in the receipt
distinguished them. A universe produced after the responses arrived can be
shaped to fit whatever arrived.

**What would settle it.** A live account, one call to whatever list endpoint the
subscription actually exposes, and its response shape recorded as a fixture.
Then mark that endpoint `is_dedicated_contract_list=True` in
`RESPONSE_CAPABILITIES`, capture it, resolve it with
`pipeline.resolve_expected_universe(declaration=..., source_manifest=...,
source_store=...)`, and pass the resulting `UniverseResolution` to
`capture_session(universe_resolution=...)`. Completeness becomes measurable at
that point and not before.

Since v2.1.11 there is a command that takes the capture:

```bash
python -m src.tools.capture_thetadata_once \
  --config config/thetadata_capture.yaml \
  --output /absolute/path/outside/this/repo/capture --execute-live
```

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

## 22. ThetaData rate units - **UNKNOWN, blocks vendor-IV mixing**

**The question.** Is `rate_value: 4.2` four-point-two percent, or four-point-two
as a decimal?

**Current behaviour.** `VendorRateUnits.UNKNOWN` is the default, and it blocks
`VENDOR_IV_LOCAL_GAMMA` compatibility. An operator who knows the answer can set
`percent` or `decimal` and the conversion becomes explicit.

**Why it matters.** A factor of one hundred in the rate. A vendor 4.2 matching a
local 4.2 on the raw numbers is the *bug*, not the confirmation.

**What would settle it.** Vendor documentation, or one live IV compared against
a local solve at both interpretations.

---

## 23. ThetaData `annual_dividend` convention - **UNKNOWN, blocks mixing**

**The question.** Is it an annual cash amount, or a continuous yield?

**Current behaviour.** `DividendConvention.UNKNOWN_VENDOR_DIVIDEND_CONVENTION`
by default; blocks compatibility. `ANNUAL_CASH_DIVIDEND` is explicitly *not*
convertible here -- Black-Scholes discounts spot by `exp(-qT)`, and turning a
cash figure into `q` needs the spot and the payment schedule.

**Why v2.1.1 got this wrong.** It passed the number through and let
`DividendSource` treat it as a yield.

**What would settle it.** Vendor documentation, or a live comparison against a
known-dividend underlying.

---

## 24. Seven undocumented vendor IV conventions - **UNKNOWN**

Each is a `PricingDimension` with status `UNKNOWN`, never compatible: the
settlement instant the vendor used, its day count, its short-dated floor, which
price it solved against, which underlying print it used and when it read it, and
its solver version.

All seven are **vendor-owned**, which decides what can settle them.
`LOCAL_CONFIGURATION` evidence is refused on a vendor-owned dimension: there is
nothing local to read, and accepting it would let a YAML edit stand in for an
observation of vendor behaviour.

**Consequence.** `VENDOR_IV_LOCAL_GAMMA` cannot claim model consistency. It can
still be *selected*; it just cannot be described as verified.

**What would settle it.** The adapter-certification session - see
[ADAPTER_CERTIFICATION.md](ADAPTER_CERTIFICATION.md).

---

## 25. Spot synchronisation tolerance - **LOCAL POLICY, uncalibrated**

**Current setting.** `ThetaDataConfig.max_spot_skew_seconds = 1.0`.

**Why this is a decision.** One second is a guess at how stale a spot print may
be before pairing it with a chain stops being meaningful. It is a local policy,
not a vendor fact, and it blocks certification when exceeded.

**What v2.1.8 changed.** It is *configuration* rather than an argument. Until
v2.1.7 it lived on a caller-built `SpotProvenance`, so a caller could grant one
calculation a wider window than the session was configured for -- and the skew
check is the only thing between a chain and an underlying it never saw.
The value now enters the pipeline fingerprint, the spot synchronisation policy
fingerprint and every stamped record, so widening it is a configuration change
that a previously taken capture visibly disagrees with. The number is still a
guess; it is now a guess made once, in the open.

**What would settle it.** Measured round-trip and staleness distributions from a
real session.

---

## 26. Open-interest settlement date - **CALLER-SUPPLIED, unverified**

**The question.** Which settlement date does ThetaData's open interest belong
to?

**Current behaviour.** `OpenInterestProvenance` records `source` and an
optional `ProvenanceEvidence`, and its grade is *derived*: `PLANNED` without
evidence, `OBSERVED` when a stored raw record is named, `VALIDATED` when a
validation report bound to that capture has checked it. A `PLANNED` date is
accepted and listed in `unverified_fields`; it is never described as observed.

v2.1.3 recorded a `caller_supplied` boolean, which is the caller describing its
own confidence rather than pointing at anything.

**Why it matters.** Open interest is the weight on every GEX term.

**What would settle it.** One live response inspected for a settlement-date
field.

---

## 27. Mixed-model policy default - **RESEARCH MODE, documented**

**The question.** Should a chain priced under several effective models be
refused, or reported?

**Current behaviour.** Reported. `require_uniform_effective_model` defaults to
`False`; the distribution is published, the snapshot is marked uncalibrated, and
`effective_model_uniformity` scores the dominant model's share.

**Why not strict by default.** Per-contract IV fallback is normal on a real
chain. Refusing every mixed chain would refuse most of them, which would not
make the data more uniform - it would make it absent.

**What would settle it.** Measured fallback rates on real chains. If mixing is
rare, strict becomes a reasonable default.

---

## 28. ThetaData NBBO IV is vendor-computed - **RESOLVED, was a misconception**

**What v2.1.2 believed.** That `NBBO_MID_IV` was in some sense a local IV,
because the price basis is an NBBO midpoint.

**What is true.** ThetaData runs the solver. The price basis is an input to
*their* calculation, not evidence about ours. All four supported IV sources are
vendor output.

**Consequence.** `LOCAL_IV_LOCAL_GAMMA` -- the mode that requires no
vendor/local agreement -- is unreachable until a local solver exists, and every
current session is `VENDOR_IV_LOCAL_GAMMA` with real compatibility
requirements.

**What would change it.** Implementing `LOCALLY_SOLVED_MID_IV` with documented
convergence limits and a failure state.

---

## 29. Vendor IV calculation conventions - **UNKNOWN, blocks certification**

Six dimensions are undocumented and reported as `UNKNOWN`: the vendor's
settlement instant for its own solve, its day count, its short-dated floor,
which price it solved against, which underlying print it used, and its solver
version.

Each changes gamma, so each is load-bearing and each blocks any *calculation*.
They are not caveats printed beside a result; they are the reason the result has
no stated meaning.

Since v2.1.4 they do **not** block the raw capture. v2.1.3 refused it, which
made the unknowns permanent: the capture is how several of them get answered.

**What would settle it.** Vendor documentation, recorded as a
`PricingAssumptionAttestation` with `source: VENDOR_DOCUMENTATION` -- enough to
permit a calculation, with the caveat recorded, and never enough to certify. Or
the capture plus a local/vendor comparison, recorded as `LIVE_COMPARISON`, which
is the only evidence of what the vendor actually did.

---

## 30. Tier capability matrix is unverified - **DOCUMENTED, uncertain**

`src/adapters/thetadata/capabilities.py` derives from the endpoint tier map read
from vendor documentation in July 2026. No entry has been checked against a live
subscription.

`contract_list_endpoint` is `UNCERTAIN` at every tier, which is why chain
completeness stays `PARTIALLY_OBSERVED`. `UNCERTAIN` counts against a
requirement rather than for it: the alternative is discovering the gap at the
first paid request.

**What would settle it.** One session per tier, or vendor confirmation.

---

## 31. Spot/OI provenance and the certification states - **UNCHANGED**

OD-25 and OD-26 still stand. The state machine around them:
`NOT_READY`, `READY_FOR_RAW_CAPTURE_ONLY`, `RAW_CAPTURE_COMPLETED`,
`CALCULATION_NOT_VALIDATED`, `CALCULATION_VALIDATED`,
`ADAPTER_CERTIFIED`. The last is unreachable without both a live capture and a
validation report, by construction rather than by policy.

---

## 32. Vendor-gamma aggregation - **NOT BUILT, deliberately**

**The question.** Should the vendor's gamma ever be aggregated into the GEX
totals, rather than only compared against ours?

**Current behaviour.** No. `VendorGammaPolicy` has two values, `DISABLED` and
`COMPARE_ONLY`, and `aggregates_vendor_gamma` is `False` for both. The pipeline
refuses an engine configured with `prefer_vendor_gamma=True`.

**Why not.** Aggregating the vendor's gamma is a different policy with its own
compatibility requirements -- it would need the vendor's *gamma* conventions
established, not just its IV conventions, and the same seven undocumented
dimensions apply again one level down.

v2.1.3 expressed this as a `MODE_CAPABILITIES` table restating what the enum
already said, which could drift from it. It is now a property on the enum.

**What would change it.** A live comparison showing the two gammas agree, plus a
stated reason to prefer the vendor's.

---

## 33. Attestations are a resolution path, not a bypass - **BY DESIGN**

**The question.** If `UNKNOWN` blocks a calculation and the vendor documents
nothing, how does anything ever get computed?

**Current behaviour.** A `PricingAssumptionAttestation` moves one dimension from
`UNKNOWN` to `MATCHED`. Constructing one requires an `EvidenceSource`, a
non-empty reference and a date; there is no boolean form and no shorthand.

Three guards keep it from being a switch:

* it cannot overturn a `MISMATCHED` dimension -- attempting it is a hard
  failure, because a measured disagreement is not an open question;
* the `EvidenceSource` is carried into the certification report, so a reader can
  see that a dimension rests on documentation rather than observation;
* only `LIVE_COMPARISON` reaches `ADAPTER_CERTIFIED`.

**The residual risk.** Somebody writes an attestation for an answer nobody
established. Nothing in code can prevent that; the reference and the date exist
so that a reviewer can check. `config/thetadata_capture.yaml` ships with an
empty list, and a test asserts it stays empty until a comparison has been run.

---

## 34. Rate units and dividend convention are the vendor's - **UNKNOWN**

**The question.** ThetaData accepts ``rate_value`` and ``annual_dividend`` as
query parameters. It does not accept ``rate_units`` or ``dividend_convention``.
So when we send ``rate_value=4.2``, how does the API read it?

**Why this changed in v2.1.5.** Both were treated as locally owned on the
reasoning that we configure them. Configuring a *label* for a number does not
tell the vendor how to read the number. ``4.2`` is 4.2% or 420% depending on a
convention that lives entirely inside the vendor's API, and the difference is a
factor of a hundred in every gamma. Writing ``rate_units: DECIMAL_ANNUAL_RATE``
in our YAML expresses a hope.

**Current behaviour.** Both dimensions are ``vendor_owned``. The configuration's
stated units are recorded and used to normalise the value, and the report says
the units themselves are unverified. ``RISK_FREE_RATE`` can still be compared,
conditionally, and says so in its detail.

A **zero** dividend is the one exception: ``exp(-0*T)`` is 1 whether the vendor
read it as cash or as a yield, so the *value* is settled and the *convention*
stays unverified. A non-zero dividend is not settled by its magnitude alone.

**What would settle it.** Vendor documentation stating the convention, recorded
as a `VENDOR_DOCUMENTATION` observation; or a capture where a known rate
produces a computable IV, recorded as a `LIVE_COMPARISON` by the validator.

---

## 35. Most vendor conventions are not in the response - **STRUCTURAL**

**The question.** ``AdapterValidator`` opens the captured payloads and reads
fields back. Why does it still fail most of its pricing checks?

**Because a snapshot reports what the vendor computed, not the convention it
computed under.** There is no ``day_count`` column. Two of the eight
load-bearing vendor dimensions are partially recoverable by comparison --
``UNDERLYING_SOURCE`` (does the greeks endpoint's ``underlying_price`` equal the
index print?) and ``UNDERLYING_TIMESTAMP`` (does its clock match the quote
instant?) -- and the rest are not in the bytes at all.

The validator names each of them, records that it could not establish them, and
therefore does not pass. That is the honest result, and it is the mechanical
reason `ADAPTER_CERTIFIED` is unreachable today rather than a policy.

**What would settle it.** Vendor documentation, or a purpose-built comparison
that infers a convention from behaviour -- e.g. solving for the day count that
reproduces the vendor's IV from a known price. Neither has been built, and
neither should be guessed at.

**What v2.1.6 changed about the two that are recoverable.** They are now
measured across the whole chain rather than from row zero of the first matching
record. Each check reports rows and records inspected, matching, mismatching,
missing and non-finite rows, a coverage ratio, the distinct values seen and the
maximum deviation. A chain that is uniform reads as the convention; a chain that
disagrees everywhere reads as the disagreement; a chain that is *mixed* records
`MIXED_ACROSS_CHAIN`, which compares as a mismatch and blocks a trusted
calculation. One matching contract is not a statement about a chain.

---

## 36. A chain cannot witness its own provenance - **RESOLVED**

**The question.** Until v2.1.5, `compute_trusted_gex(chain)` decided trust by
reading `chain.meta`: the pipeline fingerprint, the raw-capture manifest, the
spot provenance. What stopped a caller writing those keys itself?

**Nothing.** `ChainSnapshot` is a public frozen dataclass and `meta` is an open
`dict[str, object]`, so `dataclasses.replace(build_synthetic_chain(), meta={...})`
with three plausible entries passed every gate. The metadata is written by the
code that produced the snapshot, which makes it a *description* of provenance
and not a *demonstration* of it.

**Resolution.** `compute_trusted_gex` requires a `VerifiedCalculationContext`,
produced only by `build_verified_calculation_context`, which re-derives every
verification from the manifest and the raw store. The context is hashed over its
own fields and the hash is recomputed at the gate, so an edited context is
refused. The manifest in `chain.meta` stays -- it tells a later reader which
bytes to open -- and it authorizes nothing.

**Closed in v2.1.7.** The gap noted here — that nothing proved every *quote*
came from the captured payloads — is closed by re-derivation rather than by a
per-contract digest. `compute_trusted_gex` replays the stored bytes through the
ordinary fetch path, hashes both chains over every calculation-relevant field,
and refuses unless they agree. A chain assembled from the right session's bytes
with one row altered afterwards now fails, naming the field that moved.

The trusted API also stopped accepting the context. It is a public frozen
dataclass whose `context_hash` any caller can recompute, so an edited context
with a fresh hash was internally consistent and said whatever the caller wanted.
A hash is an integrity checksum, not proof of issuer. The method takes the
manifest, the store and the provenance claims and derives the rest itself.

---

## 37. Open interest: the number and the session are separate facts - **OPEN**

**The question.** An open-interest response carries a figure. Which settlement
session does that figure belong to?

**Why v2.1.6 got this wrong.** `OpenInterestProvenance` held a date, a source
and a `VerifiedFieldObservation` together, and `grade_claim` confirmed the
observation by re-reading `open_interest` from the named record. That
confirmation is real, and it is about the wrong field: it proves the vendor sent
the *number*. The date came from the caller and was graded `OBSERVED` on the
strength of a value nobody disputed.

Open interest is the linear weight on every GEX term. Using Friday's figures
believing them to be Monday's is not a stale number, it is a different market.

**Current behaviour (v2.1.7).** Two types. `OpenInterestValueObservation` says
the vendor sent this number in this record; it carries no date, so the confusion
cannot be expressed. `OpenInterestAsOfEvidence` says which session, and names
the *kind* of thing being relied on.

**What v2.1.7 still got wrong.** The kind *was* the check. `EvidenceKind` had a
`permits_trusted_calculation` property, the trusted path read it, and that was
all. So both of these were trusted:

```python
OpenInterestAsOfEvidence(..., evidence_kind=EvidenceKind.VENDOR_FIELD,
                         record_ids=("fake-record",))
OpenInterestAsOfEvidence(..., evidence_kind=AUTHORITATIVE_VENDOR_DOCUMENTATION,
                         reference="lol")
```

`VENDOR_FIELD` permits a trusted calculation, so that object did; the record was
never opened. Documentation needed a non-empty `reference`, and `"lol"` is
non-empty. Naming the kind of evidence you have is not the same as having it.

**What v2.1.8 still got wrong, and v2.1.9 fixes.** The documentation resolver
looked the rule up, confirmed it was *in force* on the caller's date, and
returned **the caller's date**:

```python
if not rule.covers(evidence.as_of):
    return failure
return ResolvedSettlementDate(as_of=evidence.as_of, ...)
```

So one registered rule saying "prior trading session" authorized 2026-03-16,
2026-03-15 and 2026-03-01 alike for a 2026-03-17 chain. `normalized_value: str`
is why: free text cannot be applied to a session date, so the answer had to come
from somewhere, and the only somewhere was the argument list.

A rule now carries a typed `SettlementRule` — a kind, a session offset and a
calendar id — and the resolver *applies* it:

```python
resolved = rule.resolve(chain_session_date)   # through the trading calendar
```

There is no `as_of` parameter anywhere in `resolve_settlement_date`. For a
2026-03-17 chain the prior-session rule produces 2026-03-16 and nothing else;
2026-04-06 produces 2026-04-02, over Good Friday and the weekend.

The rule is also chosen **before the capture**, on `capture_session`, and a
capture that established none can never become trusted — `compute_trusted_gex`
accepts no settlement evidence at all. In v2.1.8 a capture stamped
`open_interest_date_rule_fingerprint=""` still returned a trusted result if the
*call* supplied documentation evidence.

And registration now opens the document: `document_reference="/definitely/missing"`
with `document_content_hash="0" * 64` registered cleanly in v2.1.8, because
nothing read the file.

**Current behaviour (v2.1.8).** Each kind has a *resolver*, in
`src/adapters/evidence_resolvers.py`. The kind selects which check runs;
supplying it does not pass the check:

| Kind | What must actually happen |
|---|---|
| `VENDOR_FIELD` | The named record is opened and the settlement-date field re-read out of it. No ThetaData snapshot endpoint has one, so this resolves to a failure today — which is OD-26 stated as a check rather than as prose |
| `AUTHORITATIVE_VENDOR_DOCUMENTATION` | The reference is an id looked up in `DOCUMENTATION_RULES`, and the registered rule carries a SHA-256 of the document, an effective period and a derivation version |
| `DERIVED_FROM_VERIFIED_VENDOR_SCHEDULE` | A versioned `ScheduleDerivation` artefact, agreeing with the claimed date, resting on registered documentation |
| `CALLER_ASSUMPTION` | Resolves — a caller really did state a date — and authorizes nothing |

The value's provenance and the date's evidence must also agree: an
`OpenInterestProvenance` saying 2026-03-16 alongside evidence resolving to
2026-03-13 is refused rather than silently preferring one.

**The settlement convention is now read out of the vendor's own document.**
v2.1.17 recorded that the exact ThetaData v3 documentation bytes could not be
obtained and left this unresolved on that basis. The conclusion was wrong: the
OpenAPI description is served publicly at `https://docs.thetadata.us/openapiv3.yaml`,
and `paths./option/snapshot/open_interest.get.description` states that open
interest "reflects the open interest at the of the previous trading day" (the
vendor's typo, matched verbatim -- correcting it would be matching our own
edit). That normalizes to `PRIOR_TRADING_SESSION`.

The mutable `DOCUMENTATION_RULES` global stays empty, and that is deliberate:
the rule a capture opens under is built from the verified bundle at capture
time, into a fresh registry, so there is no process-wide mutable state for an
importer to pre-populate.

**The rule is in force from the moment the document was retrieved, not
earlier.** The document describes what the vendor does now and says nothing about
when the convention started, so a capture of an earlier session gets no
documentary settlement authority and must pass `--allow-unsettled-raw-only`.
Backdating it would be inventing coverage the source does not provide.

**What is still open.** Whether the vendor's stated convention matches what the
responses actually carry. A document is a claim; the first raw session is how it
gets compared against bytes.

---

## Deferred, with reasons

| Item | Why deferred | Revisit when |
|---|---|---|
| `SURFACE_REFIT` IV convention | Full per-grid-point surface re-estimation; the other three conventions have to disagree materially first | The convention spread is large enough that which one is right matters |
| numpy vectorisation of the grid | A dependency-free core is worth more during development than the speed | The sub-1s SLA binds; currently ~1–2 s for a real chain |
| Cboe DataShop Open-Close | From $2,499/mo, and only needed for the flow-adjusted sign model | A specific hypothesis justifies the price |
| Futures data (Databento) | Out of scope for this pass | Feature store needs VWAP/realised vol |
| Risk engine, broker, strategies | Out of scope, and deliberately absent | Never, from this repository |
