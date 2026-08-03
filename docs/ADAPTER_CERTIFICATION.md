# Adapter certification

Status: `IMPLEMENTED` · `TESTED_SYNTHETICALLY` · `NOT_VALIDATED_WITH_LIVE_THETADATA`.

**The shipped default configuration is `READY_FOR_RAW_CAPTURE_ONLY`.** Six
load-bearing vendor pricing unknowns block any *calculation* from that session,
and none of them block the capture itself -- capturing is how several of them get
answered. v2.1.3 refused the capture too, which made the unknowns permanent.

Resolving them means recording a typed `PricingAssumptionAttestation` per
dimension: where the answer came from, a reference to it, and when it was
established. An attestation cannot overturn a measured mismatch, and one sourced
from `VENDOR_DOCUMENTATION` never reaches `ADAPTER_CERTIFIED` -- documentation
records what the vendor says it does.

> **This is not a trading readiness check.** This repository has no broker, no
> order type and no execution path. Certification readiness confers none of
> those, and `AdapterCertificationReadiness.trading_enabled` is a constant
> `False` so that a serialised report cannot be quoted as though it did.

---

## What certification is for

One paid ThetaData session produces a directory of vendor bytes. The question
this report answers is narrow: **would anybody be able to reconstruct, months
later, what those numbers meant?**

A capture taken without recording which assumptions were in force is not
evidence. It is a directory.

```python
from src.adapters.certification import assess_readiness

readiness = assess_readiness(
    pipeline=pipeline,          # ThetaDataResearchPipeline
    as_of=as_of,
    open_interest=oi_provenance,
    spot=spot_provenance,
    raw_store=store,
    capture=None,               # CaptureVerification, from verify_capture()
    validation=None,            # AdapterValidationReport, bound to its manifest
)
if not readiness.ready:
    print(readiness.blockers)             # cannot capture
if not readiness.calculation_trusted:
    print(readiness.calculation_blockers)  # can capture; cannot compute
```

`capture` and `validation` are **typed and rejected outright if they are not**.
In v2.1.3 both were `Any` and both were tested with `is not None`, so
`assess_readiness(capture_manifest=object(), validation_report=object())`
returned `ADAPTER_CERTIFIED`.

---

## Two kinds of blocker

The report answers two questions, and v2.1.3 ran them through one list. *May we
capture?* and *may we trust a number computed from the capture?* have different
answers and different remedies.

### Capture blockers -- `readiness.blockers`

The capture itself would produce data whose meaning cannot be stated.

| Blocker | Why it blocks |
|---|---|
| Missing open-interest provenance | Open interest is the weight on every GEX term. A capture with no settlement date cannot be interpreted afterwards. |
| Missing spot source or timestamp | Every gamma is computed against this print. Without its clock there is no way to show it was contemporaneous with the chain. |
| Spot skew beyond tolerance | The chain and the underlying describe different moments, so the pairing is not meaningful. |
| Raw capture disabled, or no path, or no store | The bytes are the deliverable. A paid session whose responses are discarded produces numbers nobody can re-derive. |
| Raw store not clean | Starting a paid session on top of an inconsistent audit trail makes new evidence hard to separate from old. |
| Subscription tier cannot serve the request | The mode is a wish. Discovered at the first paid request otherwise. |
| Credentials unavailable | An unauthenticated client turns a configuration error into an unexplained 401. |

### Calculation blockers -- `readiness.calculation_blockers`

The bytes are worth having; a gamma computed from them would not have a stated
meaning.

| Blocker | Why it blocks |
|---|---|
| A load-bearing pricing dimension is `UNKNOWN` | Each one changes the gamma. The capture is permitted, and is how several of them get answered. |
| A load-bearing pricing dimension is `MISMATCHED` | We know the two models differ, so mixing them produces a number that is wrong rather than merely unexplained. |
| `hard_failures` on the assessment | Not about one dimension -- an unsupported mode, an attestation aimed at a mismatch. Always honoured. |
| The capture manifest does not match its store | A manifest listing three records against a store holding two cannot say which bytes produced which number. |
| The validation report describes a different manifest | A report about another session is not a report about this one. |

## Warnings

A warning is a documented limitation the capture should record, not a reason to
refuse it.

| Warning | Why it is not a blocker |
|---|---|
| `PLANNED` open-interest date or spot | Usable, provided the report says it was ours rather than the vendor's. Listed in `unverified_fields`, with the grade in `provenance_grades`. |
| A load-bearing dimension resting on `VENDOR_DOCUMENTATION` | Enough to permit a calculation, not enough to certify. Documentation records what the vendor says it does. |
| Chain completeness will be `PARTIALLY_OBSERVED` | No verified contract-list endpoint is wired. **The session is how that endpoint gets identified** — refusing to capture would make the problem permanent. See [OPEN_DECISIONS.md](OPEN_DECISIONS.md) OD-11. |
| Pricing compatibility unestablished in a local-only mode | Nothing vendor-computed enters the maths, so nothing has to agree. |

---

## The two vendor-dependent unknowns

### Open-interest as-of

ThetaData's snapshot endpoints do not state which settlement date their open
interest belongs to.

v2.1.1 accepted a caller-supplied date and stored it in the same field as an
observed one, so a snapshot could not distinguish *"the vendor said 16 March"*
from *"we assumed 16 March"*.

`OpenInterestProvenance` carries `source` and an optional
`ProvenanceEvidence`, and its `grade` is **derived** from what it can point at.
v2.1.3 used a `caller_supplied` boolean, which is the caller describing its own
confidence; a claim to have observed something is not an observation.

| Grade | Means |
|---|---|
| `PLANNED` | what the configuration intends. No response has been seen. |
| `OBSERVED` | read out of a stored raw record, which the evidence names by id, field and manifest hash. |
| `VALIDATED` | observed, and checked by a validation report bound to that capture. |

A `PLANNED` date is accepted and surfaced in `unverified_fields` — never
described as observed.

**What would settle it:** one live response inspected for a settlement-date
field, or vendor documentation stating the convention.

### Synchronised spot

The spot print and the option chain are separate reads.

`SpotProvenance` carries the source, the timestamp and a
`tolerance_seconds` policy. Skew beyond tolerance blocks certification. The
tolerance is a **local policy**, not a vendor fact — 1.0 s by default, and the
right value is an open question.

**What would settle it:** measured round-trip and staleness distributions from a
real session.

---

## Pricing compatibility

Two independent questions, and v2.1.3 answered them with one enum.

**Where does the IV come from?** — `IvGammaPricingMode`.

| Mode | Requires agreement | Status |
|---|---|---|
| `VENDOR_IV_LOCAL_GAMMA` | **yes** | `IMPLEMENTED`, blocked while conventions are `UNKNOWN` |
| `LOCAL_IV_LOCAL_GAMMA` | no | `DECLARED_BUT_UNREACHABLE` — needs a local IV solver |

**What do we do with the vendor's gamma?** — `VendorGammaPolicy`.

| Policy | Needs | Aggregated into GEX |
|---|---|---|
| `DISABLED` | nothing extra | no |
| `COMPARE_ONLY` | Pro (second-order greeks) | **no** — compared, never aggregated |

The two are orthogonal. `VENDOR_GAMMA_VALIDATION` used to be a third *mode*, so
switching the comparison on moved a session out of `VENDOR_IV_LOCAL_GAMMA` and
out of the checks it still needed — vendor IV was still feeding the local gamma.
The old value is now refused at load time rather than translated, because the
checks it skipped now run and may refuse to compute.

Each dimension is a typed `PricingDimensionResult`: a `PricingDimension`, a
`CompatibilityStatus`, a machine-readable code, the two values, and optional
evidence. Whether a dimension is load-bearing is a property of the dimension.
v2.1.3 stored findings as sentences and decided which mattered by searching
those sentences for a field name, so rewording a message turned a blocker into a
warning.

Seven vendor-side dimensions are undocumented and are reported as `UNKNOWN`
rather than assumed compatible:

- the settlement instant the vendor used for its own IV solve
- the vendor's day-count convention
- the vendor's short-dated time floor
- which price the vendor solved against
- the vendor's IV solver version

Two more are *knowable* but must be stated:

- **rate units.** `rate_value: 4.2` is either 4.2% or 420%. A vendor 4.2 and a
  local 4.2 agree only if the vendor's is a decimal; if it is a percentage, a
  match on the raw numbers is the bug rather than the confirmation.
- **dividend convention.** `annual_dividend` may be an annual cash amount or a
  continuous yield. Black–Scholes discounts spot by `exp(-qT)`, which a cash
  figure cannot substitute for without the spot and the payment schedule.

---

## Supported IV sources

| Source | Status |
|---|---|
| `NBBO_BID_IV` | `SUPPORTED` |
| `NBBO_MID_IV` | `SUPPORTED` |
| `NBBO_ASK_IV` | `SUPPORTED` |
| `VENDOR_DEFAULT_IV` | `SUPPORTED` |
| `TRADE_IV` | `DECLARED_BUT_UNSUPPORTED` — needs a trade-price feed this repository does not consume |
| `LOCALLY_SOLVED_MID_IV` | `DECLARED_BUT_UNSUPPORTED` — needs an IV solver with documented convergence limits and a failure state |
| `SURFACE_REFIT` | `PLANNED` |

Declared-but-unsupported sources are **refused at configuration load**. v2.1.1
accepted them and then resolved them through the vendor-default fallback, so the
operator silently got a different number than the one they selected.

---

## Before the session

- [ ] `assess_readiness(...).ready` is `True`
- [ ] `calculation_blockers` is read and understood: a capture may be permitted while a calculation from it is not
- [ ] every entry in `warnings` is recorded alongside the capture
- [ ] `raw_capture_enabled: true` with a writable `raw_capture_path`
- [ ] `verify_integrity()` on the store is clean **before** starting
- [ ] the pipeline fingerprint is recorded
- [ ] `trading_enabled` is `False` everywhere — it always is, and this is the
      last chance to notice if that ever stopped being true

## After the session

- [ ] `verify_integrity()` is clean **after** the capture
- [ ] the open-interest convention is now known, or recorded as still unknown
- [ ] measured spot skew is recorded
- [ ] whichever contract-list endpoint exists is identified, so completeness can
      become measurable
- [ ] vendor gamma is compared against local gamma, and the result is written
      down whichever way it comes out


---

## The state machine (v2.1.4)

| State | Means | Reachable offline |
|---|---|---|
| `NOT_READY` | at least one capture blocker | yes |
| `READY_FOR_RAW_CAPTURE_ONLY` | the capture may proceed. Says nothing about whether the resulting numbers could be trusted | yes |
| `RAW_CAPTURE_COMPLETED` | bytes exist and the manifest matches the store. Pricing may still be unknown | no |
| `CALCULATION_NOT_VALIDATED` | verified capture **and** resolved pricing, so a calculation is permitted — but nobody has checked its output | no |
| `CALCULATION_VALIDATED` | a validation report bound to this capture passed every check | no |
| `ADAPTER_CERTIFIED` | all of the above, plus observed provenance and every load-bearing convention settled by a live comparison | no |

Each rung needs the one below it *and* its own evidence. `ADAPTER_CERTIFIED` is
unreachable from anything this repository currently ships: every attestation it
carries is `VENDOR_DOCUMENTATION`, and only `LIVE_COMPARISON` observes what the
vendor actually did.

## Why the default cannot compute

`VENDOR_IV_LOCAL_GAMMA` is the only mode a vendor-computed IV can use, and it
mixes the vendor's IV into our gamma. Six vendor conventions are undocumented,
and each changes the gamma:

- the settlement instant the vendor used for its own solve
- its day-count convention
- its short-dated time floor
- which price it solved against
- which underlying print it used, and when
- its solver version

Plus two that are *knowable* but must be stated: `rate_units` and
`dividend_convention`. Until those are set, the rate and dividend comparisons
report `UNKNOWN` rather than agreement.

An unknown that changes gamma is not a caveat printed beside the answer. It is
the reason the answer has no stated meaning.


---

## v2.1.5: derived, not constructed

Three objects in v2.1.4 were public dataclasses whose *presence* was the answer,
and all three were accepted by the public readiness API:

```python
CaptureVerification(confirmed_record_ids=("fake",), failures=())   # verified
ValidationCheck(name="anything", passed=True)                      # validated
PricingAssumptionAttestation(dimension=DAY_COUNT, evidence=...)     # MATCHED
```

None of them had to have come from the code that checks anything.

### The capture verdict is computed here

`assess_readiness` takes a `RawCaptureManifest` and a `RawResponseStore`, and
runs the verifier itself:

```python
readiness = assess_readiness(
    pipeline=pipeline,
    as_of=as_of,
    open_interest=oi_provenance,
    spot=spot_provenance,
    manifest=manifest,       # not a CaptureVerification
    raw_store=store,
    validation=report,       # re-derived and compared
)
```

There is no verdict parameter to forge. A supplied validation report is
re-derived from the same capture and compared: a report this validator would not
have produced is refused, which needs no signature because the check is "would
this code have said that?".

### Evidence is an observed value

A `VendorObservation` records **what the vendor does**, and a per-dimension
comparator decides what follows:

```yaml
pricing_attestations:
  - dimension: DAY_COUNT
    source: VENDOR_DOCUMENTATION
    reference: docs/THETADATA_INTEGRATION.md   # must resolve
    observed_at: "2026-08-XX"
    vendor_value: "ACT/360"                    # -> MISMATCHED against ACT/365F
```

`source: LIVE_COMPARISON` is refused in configuration. A file cannot witness an
event; only `AdapterValidator` emits live evidence, bound to the capture it read.

### The capture must be complete

A `CapturePlan` is derived from the pricing mode, the vendor-gamma policy, the
underlying source and the tier. For a Standard vendor-IV session reading the
vendor index:

| Endpoint | Why |
|---|---|
| `/v3/option/snapshot/quote` | the chain itself |
| `/v3/option/snapshot/open_interest` | the weight on every GEX term |
| `/v3/option/snapshot/greeks/first_order` | the vendor IV that feeds local gamma |
| `/v3/index/snapshot/price` | the underlying every gamma is computed against |

Pro with `vendor_gamma_policy: COMPARE_ONLY` adds
`/v3/option/snapshot/greeks/second_order`. A one-record capture verified in
v2.1.4; it now reports `MISSING_ENDPOINT` per absent response.

### Field provenance is re-read

`VerifiedFieldObservation` names the record, the endpoint, the payload hash, the
parser version, the field and the value -- and the verifier opens the payload
and checks each. A Greeks response has no `open_interest` column, so it is
refused as evidence about open interest rather than treated as weak evidence.

### Why the validator still fails

It opens the captured payloads and reads them back. Most vendor conventions are
not in the payload: a snapshot reports what the vendor computed, not the
convention it computed under. Two dimensions are partially recoverable by
comparison; the rest are named, recorded as unestablished, and keep the report
from passing.

That is the mechanical reason `ADAPTER_CERTIFIED` is unreachable today. It is
not a policy switch. See [OPEN_DECISIONS.md](OPEN_DECISIONS.md) OD-35.

## v2.1.5: what a number is allowed to claim

| Method | Runs when | Marks the result |
|---|---|---|
| `compute_diagnostic_gex(chain)` | always | `trusted=False`, `DIAGNOSTIC_UNTRUSTED`, every blocker listed, both fingerprints |
| `compute_trusted_gex(chain, context=...)` | only when nothing is outstanding | `trusted=True`, `TRUSTED`, the evidence context hash |

`compute_trusted_gex` refuses unless the pricing report has no load-bearing
unknowns, no mismatches and no hard failures; the chain carries this pipeline's
fingerprint; it carries a raw-capture manifest; the engine settings match; the
spot is a vendor index observation read back from a stored payload; and the
capture holds every endpoint the plan requires.

A diagnostic result cannot be fed back as trusted input. Untrusted is permanent.

## v2.1.6: who is allowed to say so

Every check above read `chain.meta`. `ChainSnapshot` is a public dataclass, so a
synthetic chain carrying the right keys satisfied all of them:

```python
dataclasses.replace(
    build_synthetic_chain(),
    meta={"pipeline": {...}, "raw_capture_manifest": {...}, "spot_provenance": {...}},
)
```

A snapshot cannot be a witness to its own provenance. Authorization now comes
from a separate object:

```python
context = build_verified_calculation_context(
    pipeline=pipeline,
    manifest=manifest,
    store=store,
    validation=validation,       # optional; re-derived and compared if present
    spot=spot_provenance,
    open_interest=open_interest_provenance,
)
snapshot = pipeline.compute_trusted_gex(chain, context=context)
```

The builder takes no verdict. There is no `capture_verification` parameter and
no compatibility report parameter, because a caller who could pass either could
pass a passing one. It runs `verify_capture` itself, re-derives the validation
report and compares it, folds the verified observations into an *effective*
pricing-compatibility report, and grades the provenance claims against the
stored bytes.

`compute_trusted_gex` then checks that the context:

| Binding | Refuses when |
|---|---|
| context hash | the object has been edited since it was verified |
| pipeline fingerprint | the context was built for a different configuration |
| capture plan | the capture was taken against a different plan |
| parser version | the evidence was read by different code |
| chain fingerprint | the chain came from a different pipeline |
| manifest hash | the verified bytes are not the bytes behind this number |
| capture verification | the manifest does not match its store |
| effective compatibility | a load-bearing dimension is unknown or mismatched |
| spot and OI provenance | the evidence is about a different session -- including that `chain.spot` equals the verified index print |

The manifest inside `chain.meta` is still worth having. It is a description of
where to look, and it is not evidence.

### What a capture must now prove about itself

`verify_capture(manifest, store, plan=..., expected_pipeline_fingerprint=...)`
binds each `ManifestRecord` to the stored record of the same id: payload hash,
endpoint, parameter hash, request id, sequence, status, parser version, vendor
schema version, capture origin and both clocks. It also requires a supported
manifest schema and parser version, a non-empty pipeline and capture-plan
fingerprint that both match, record ids that belong to the named session, unique
ids and sequences, a successful HTTP status, a complete capture, tz-aware
timestamps and a response no earlier than its request.

**An empty fingerprint is a failure, not a skip.** That is the v2.1.5 hole:
`if manifest.capture_plan_fingerprint and ...` meant a manifest that claimed
nothing was checked against nothing.

## v2.1.7: bound to the chain, not only to the bytes

Everything above is about raw records. The `ChainSnapshot` a caller hands to a
trusted calculation is a *different object* — the result of parsing and joining
those records — and until v2.1.7 nothing connected the two:

```python
chain = pipeline.fetch_chain(...)          # honest
tampered = dataclasses.replace(chain, quotes=(edited, *chain.quotes[1:]))
pipeline.compute_trusted_gex(tampered, context=real_context)   # trusted=True
```

Adding 999,999 to one strike's open interest moved the unsigned total by about
two orders of magnitude, with a verified manifest and `trusted=True`.

### Re-derivation

```python
snapshot = pipeline.compute_trusted_gex(
    chain,
    manifest=manifest,
    store=store,
    validation_report=validation_report,          # optional
    spot_provenance=spot_provenance,
    open_interest_provenance=open_interest_provenance,
    open_interest_as_of_evidence=settlement_date_evidence,
)
```

Note what is **not** a parameter: any verdict. v2.1.6 took a
`VerifiedCalculationContext`, a public frozen dataclass whose `context_hash` any
caller can recompute — so an edited context with a fresh hash was internally
consistent and asserted whatever the caller wanted. A hash proves the fields
agree with the digest; it says nothing about who computed them. The method now
takes evidence and does the deriving, in this order:

1. validate pipeline integrity;
2. verify the capture (records, identity, request specification);
3. re-derive the validation report and compare it;
4. derive the effective pricing compatibility;
5. rebuild the normalized chain from the stored payloads;
6. compare `canonical_chain_hash(supplied)` against the rebuilt one;
7. check spot and open-interest provenance against this chain;
8. only then compute.

The rebuild replays the capture through the **ordinary fetch path** with a
transport that answers from the store. A separate rebuilder that re-read the
CSVs itself would be a second implementation of normalization, and two
implementations of the same thing drift.

The `VerifiedCalculationContext` still exists. It is what the call *returns*.

### What the canonical chain hash covers

Contract identity, exact strike, expiry, right, multiplier; bid, ask, last, both
sizes, volume, open interest; IV value *and* source *and* usability; gamma and
whether the vendor supplied it; delta, theta, vega; the per-contract underlying;
all five source clocks and the selected-source provenance; parse issues;
exclusion state. Chain level: `as_of`, spot, spot timestamp, rate, dividend,
source, expected contract count, completeness status, effective-model
fingerprint.

Not covered: the chain-level `SnapshotClocks` *values*. All three are read from
the client's own clock at fetch time, so a rebuild necessarily differs and
hashing them would make the comparison fail always. Their ordering is hashed,
and the per-record request and response clocks are compared against the store by
`verify_capture` — which is where a timestamp is evidence about a response
rather than about the machine that asked for it.

### Records carry what was true when the bytes arrived

`CaptureIdentity` — session, pipeline fingerprint, capture-plan fingerprint,
request-spec fingerprint, normalization-rules fingerprint — is fixed when a
session opens and stamped on every record. Verification compares it twice:
descriptor against record, and record against what this pipeline is *now*. In
v2.1.6 the pipeline fingerprint lived on the manifest alone, so relabelling a
capture as another pipeline's was one field on a document the evidence could not
contradict.

The request specification is the sharpest of these. `rate_value` and
`annual_dividend` are sent to the vendor's greeks endpoints and change the IV
and greeks that come back, so a capture taken at `rate_value=4.2` presented as
one from a pipeline configured with `3.1` describes numbers computed under a
different rate. `RequestSpec` states, per endpoint, exactly what this session
would send; verification recomputes it and compares against each record's stored
query parameters.

`capture_origin` is likewise no longer a field on the manifest. It is derived
from the records: one uniform origin, or `UNKNOWN_ORIGIN` for none or mixed. A
declaration that contradicts the records fails verification.

### Paid capture needs somewhere the evidence survives

`READY_FOR_RAW_CAPTURE_ONLY` additionally requires a `DURABLE_APPEND_ONLY`
store: a clean integrity scan, a successful write and byte-identical read-back,
a filesystem location outside the source tree, and free space above a
configurable minimum. `InMemoryRawStore` is `TEST_ONLY_VOLATILE` — a working
store that forgets everything when the process exits — and stays fully supported
for unit tests and offline fixtures.

---

## v2.1.8: bound to the operation, not only to the configuration

v2.1.7 re-derived the chain from the bytes, which closed every payload mutation.
It did not bind the inputs that are **not in the payload**. The rebuild built its
recipe with `as_of=chain.as_of`, so the chain under test chose the instant it was
tested against; shifting it by 0.1s, 0.5s, 1s or an hour shifted the rebuild too,
and the two agreed.

### The capture operation

A session may run several fetches — a chain pull, a re-pull, a paginated sweep —
and each fixes its own inputs. `CaptureOperationIdentity` records them:

| Field | Why it is here |
|---|---|
| `requested_as_of` | What the caller asked for |
| `effective_valuation_timestamp` | What Black-Scholes is actually priced against. Usually a different value, and the difference is the point of recording both |
| `valuation_timestamp_rule` | Which of `INDEX_PRINT_TIMESTAMP`, `SYNCHRONIZED_MARKET_TIMESTAMP`, `CAPTURE_REQUEST_INSTANT` chose it |
| `spot_synchronization_policy_fingerprint` | Tolerance and source, so a caller cannot widen the window for one calculation |
| `open_interest_date_rule_fingerprint` | Which rule established the settlement date, where one has been |
| `expected_universe_fingerprint` | What completeness is measured against |
| pipeline / plan / request-spec / recipe / parser | The standing configuration v2.1.7 already stamped |

The whole identity is hashed and the digest goes on every record.

The split between *provisional* and *resolved* is deliberate. `begin_operation`
stamps the requested instant, because no response has arrived yet;
`resolve_operation` reads the effective instant out of the verified index print
afterwards. A value stamped before the evidence existed would be an assertion,
and the point is that the instant is derived.

### Nothing that decides a number is accepted from the caller

| Input | v2.1.7 | v2.1.8 |
|---|---|---|
| Valuation instant | `chain.as_of` | The index print in the verified capture |
| Spot timestamp | A field on a caller-built `SpotProvenance` | The verified index record |
| Skew tolerance | A field on the same object | `ThetaDataConfig.max_spot_skew_seconds` |
| Settlement date | An `EvidenceKind` that authorized itself | A resolver that opens the record, or looks the rule up, or requires the derivation artefact |
| Chain completeness | `snapshot.meta["chain_completeness_object"]` | A typed `ChainSnapshot.completeness` field, in the chain hash |
| Expected universe | An argument to the calculation | Declared on the capture session, stamped, checked at replay |

The completeness one had a measured cost: forging that metadata key moved a
trusted confidence score from 52.0619 to 57.3394. Metadata may describe a
calculation; it must not alter one, and `tests/unit/test_architecture.py` now
fails the build when production GEX code reads calculation-affecting data from
`meta`.

### Every record is consumed, exactly once

A replay that used *some* of a capture proved something about those bytes and
nothing about the rest. `RecordConsumptionReport` compares what the manifest
assigned against what normalization consumed, in order, and the digest goes into
the receipt. An endpoint may answer more than once only where the capture plan
declares why: `PAGINATION`, `BATCHED_EXPIRATIONS`, `RETRY` or
`PARTITIONED_UNIVERSE`. No shipped plan declares any of them.

### Documentation evidence is bound to its content

A reference says where somebody looked. `DocumentationRule.document_content_hash`
says what was there. A vendor rewriting a page without renaming it changes the
evidence fingerprint and therefore the pipeline fingerprint, which is the only
way a claim about vendor behaviour can go stale visibly rather than quietly.

---

## v2.1.9: the two remaining inputs, derived rather than declared

v2.1.8 bound every non-payload input to the capture operation. Two of them were
bound to something nobody had checked.

### A settlement rule is chosen before the capture, and applied

Three changes, and the order matters because each closes what the previous one
left:

1. **Selected on `capture_session`.** A `SettlementDateRuleArtifact` is fixed
   before any response arrives. A session opened without one produces a capture
   that is fully usable for raw storage, diagnostic calculation and vendor-schema
   research, and that can never become eligible for a trusted GEX — because
   `compute_trusted_gex` accepts no settlement evidence at all. v2.1.8 stamped
   `open_interest_date_rule_fingerprint=""` and then let the *call* supply the
   evidence: the capture said no rule had been established, the calculation said
   one had, and the calculation won because it held the argument.

2. **Typed, so it computes.** `SettlementRule(kind, trading_session_offset,
   calendar_id)` is applied through the real trading calendar:

   | Chain session | Prior-session rule produces | Over |
   |---|---|---|
   | 2026-03-17 (Tue) | 2026-03-16 | — |
   | 2026-03-16 (Mon) | 2026-03-13 | the weekend |
   | 2026-04-06 (Mon) | 2026-04-02 | Good Friday, 2026-04-03 |
   | 2026-01-02 | 2025-12-31 | New Year's Day |
   | 2026-11-27 | 2026-11-25 | Thanksgiving |

   `resolve_settlement_date` has no `as_of` parameter. The v2.1.8 signature took
   one and returned it, so a single rule authorized every date.

3. **Content-verified.** Registration reads the referenced file and compares its
   SHA-256. A missing file, a mismatched hash, an absolute path or a URL is
   refused. v2.1.8 accepted `"0" * 64` for `/definitely/missing`.

The resolved date then travels: into `NormalizationRecipe.open_interest_as_of`,
onto every contract's `timestamps.open_interest_as_of`, into the canonical chain
hash, the receipt and the replay. The trusted path refuses a chain whose
contracts carry a different date, carry none, or disagree among themselves.

### An expected universe is re-derived from its source

`source="vendor_contract_list"` was a string. `source_record_ids` was read as a
boolean. There were also two `ExpectedContractUniverse` classes, and the engine
read the one with no provenance.

There is now one type and a resolver:

| Source kind | What must actually happen |
|---|---|
| `VENDOR_CONTRACT_LIST` | Every named record is reopened, its payload hash re-checked and its contract identities parsed out again; the derived set must equal the claimed set exactly |
| `CAPTURED_PAGINATION_METADATA` | The same, from the records that carried the metadata |
| `AUTHORITATIVE_DOCUMENTATION` | A registered, content-verified documentation rule |
| `CALLER_DECLARED` | Resolves, and establishes nothing — permits raw capture and diagnostics, never measured completeness |

`complete_for_request` is read now. A partial listing gets
`PARTIAL_UNIVERSE_ALL_LISTED_PRESENT` or
`PARTIAL_UNIVERSE_MISSING_IDENTITIES`, and neither implies completeness: a page
can prove a contract is *missing* and cannot prove none is.

### Digests are recomputed, and the objects they name are stored

`verify_capture` rebuilds `CaptureOperationIdentity` from the fields each record
stores and recomputes the fingerprint —
`OPERATION_FINGERPRINT_MISMATCH`. v2.1.8 compared the stored digests to each
other, so editing `requested_as_of` on every record while leaving the digest
alone verified cleanly. Every field the digest covers is stored explicitly,
including the spot synchronization policy fingerprint.

A digest also has to name something recoverable. The `ArtifactStore` is
content-addressed by the artifact's own hash, so the digest stamped on a record
*is* the lookup key — and a replay that cannot produce the artifact refuses
rather than falling through to "no rule".

### Field evidence names its record

`observe_field` and `confirm_field` take a `record_id` and assert on it. Until
v2.1.9 the first record for the endpoint was always opened, so a claim about
page two was confirmed against page one's bytes. With one record per endpoint the
two agree by accident; pagination, partitions and retained retries are exactly
the cases this repository intends to certify.

---

## v2.1.10: coverage, not identities

v2.1.9 made an expected universe re-derive its identities from the records it
named. That check is necessary and it answers the wrong question.

Proving a set of identities occurs in stored records does not prove those records
**enumerate the complete universe the request should have returned**. A truncated
response enumerates its own rows perfectly. So a quote snapshot, one page of a
paged response, or a document about something else could be labelled
`VENDOR_CONTRACT_LIST`, pass every v2.1.9 check, and establish
`MEASURED_COMPLETE` for the entire chain.

### Coverage is a state, and the caller does not get to pick it

| `UniverseCoverageStatus` | Means | Supports |
|---|---|---|
| `FULL_REQUEST_ENUMERATED` | the source listed every contract the request owed | `MEASURED_COMPLETE`, analytical readiness |
| `PARTIAL_PAGE` | a verified slice, with the rest unenumerated | finding a *missing* listed contract |
| `OBSERVED_SUBSET` | rows that arrived; nothing about what was owed | diagnostics |
| `UNKNOWN_COVERAGE` | nothing was established | nothing |

`ExpectedContractUniverse.complete_for_request: bool` is gone. It was a
constructor argument, hashed into the universe and read by the engine — hashing
an assertion is not verifying it. A caller may still record what it *expected*
via `declared_coverage`; no completeness decision reads that field.

### Four questions a response was previously asked as one

`ResponseCapabilities` separates them:

| Question | Field |
|---|---|
| Does it list its own rows? | `enumerates_rows` |
| Does it enumerate the *request's* universe? | `enumerates_request_universe` |
| Does it carry page / total / continuation metadata? | `carries_pagination_metadata` |
| Is it a dedicated contract-list endpoint? | `is_dedicated_contract_list` |

Every ThetaData option snapshot answers **yes to the first and no to the other
three**. `DEDICATED_CONTRACT_LIST_ENDPOINTS` is therefore empty, an unknown
endpoint gets the empty capability rather than a default, and an index endpoint
supplies no identities at all.

That has a blunt consequence, and it is the honest one: **no capture this
repository can currently perform reaches `FULL_REQUEST_ENUMERATED`.** The
`VENDOR_CONTRACT_LIST` and `CAPTURED_PAGINATION_METADATA` source kinds resolve to
a refusal naming what is missing, rather than to a simulated success. v2.1.9's
pagination resolver read no pagination metadata whatsoever — it re-derived
identities and trusted a Boolean.

### A declaration and a finding are different types

`ExpectedContractUniverse` is what somebody believes.
`VerifiedExpectedUniverseArtifact` is what `resolve_expected_universe`
established, and it is the only thing `resolve_chain_completeness` accepts — passing a declaration raises
`TypeError` rather than degrading quietly. The artifact refuses at construction
to carry a coverage its own source kind cannot reach
(`ExpectedUniverseSourceKind.best_possible_coverage`), so no amount of downstream
checking promotes a snapshot into a listing.

`observed_at` is derived from `max(response_received_at)` over the source
records. v2.1.9 took it from the caller, so a stale listing could be presented as
current.

### The source has to be about this request

`UniverseRequestScope` carries root, expirations, `max_dte`, strike range,
rights, request filters and `requested_at`. A *wider* listing serves a narrower
chain; a narrower one cannot serve a wider chain, and an unbounded request means
everything. Ordering is checked too — a universe observed after the chain
describes a different market — along with staleness against
`DEFAULT_MAX_UNIVERSE_AGE`.

Universe documentation lives in its own registry, separate from the settlement
one. Both are content-verified; they are verified to say *different things*, so
an OI settlement convention plus an arbitrary option identity no longer
establishes a universe.

### Verified before the operation opens

`capture_session` takes either `verified_expected_universe=` (resolved, compared
against the chain request scope, refused before a single response is fetched) or
`declared_expected_universe=` (recorded as diagnostic, and it can never establish
measured completeness). The chain operation is never stamped with an unresolved
claim, and `recover_capture_artifacts()` returns the re-derived artifact rather
than the caller's object.

`ChainCompleteness` now carries `universe_artifact_hash`,
`universe_evidence_fingerprint`, `coverage_status` and `resolver_version`, and
`independently_observed` reads those. The `NON_INDEPENDENT_SOURCES` string set is
gone: independence was decided by comparing `expected_source` against a list of
labels, so inventing a new label bought independence.

### What this changes about the shipped state

Nothing about raw capture. A session with no universe at all still captures,
stores, verifies and replays — §12's point is that raw capture must not require a
universe it cannot have.

`analytical_readiness_of` now requires `FULL_REQUEST_ENUMERATED` **and**
`independently_observed`. Since no verified source reaches that state today, the
shipped profile is `READY_FOR_RAW_CAPTURE_ONLY` and
`NOT_READY_FOR_ANALYTICAL_DATASET`. That was always true. It is now enforced by
a check rather than asserted in a document.
