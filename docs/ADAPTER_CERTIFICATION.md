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
| Chain completeness will be `PARTIALLY_OBSERVED` | The contract-list endpoint is requested and captured as evidence, and its scope has never been compared against a filtered snapshot request. **The session is how that comparison becomes possible** — refusing to capture would make the problem permanent. See [OPEN_DECISIONS.md](OPEN_DECISIONS.md) OD-11. |
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

---

## v2.1.11: who may authorize, and one way to capture

v2.1.10 made coverage a resolver output and refused what a source kind could not
support. The refusals live in `VerifiedExpectedUniverseArtifact.__post_init__`,
and they answer *what an artifact may claim*. They were being read as answering
*who may make one*.

`capture_session` took an artifact and checked `isinstance`. The type is a public
frozen dataclass. So a caller could construct one naming
`AUTHORITATIVE_DOCUMENTATION`, `FULL_REQUEST_ENUMERATED`, an evidence id nobody
had registered and an evidence fingerprint of nothing, and the capture opened
against it.

### A resolution, re-run

```python
resolved = pipeline.resolve_expected_universe(
    declaration=declaration,
    source_manifest=source_manifest,
    source_store=source_store,
)

session = pipeline.capture_session(..., universe_resolution=resolved)
```

A `UniverseResolution` carries the *inputs*: the declaration, the source
capture, the verification, and the document extraction where one was involved.
`capture_session` re-verifies the source and re-derives the artifact, and refuses
unless the same `artifact_hash` comes out. A forged resolution therefore has to
supply a source that genuinely produces the claimed artifact — at which point it
is not a forgery, it is a resolution.

The artifact remains as a serialisable report. Constructing one authorizes
nothing.

### The source has to be a capture that passed

| Refused | Because |
|---|---|
| no `verify_capture` result | existing in a store is not having been verified |
| a record outside the verified manifest | it was checked against nothing in particular |
| HTTP 400–599 | an error body parses into whatever rows it happens to contain |
| `capture_complete=false` | the response is truncated by this repository, not by the vendor |
| an unsupported `parser_version` | a payload read under different rules is a different payload |
| an empty operation or request-spec fingerprint | nothing says which request produced it |

A *universe source* is verified with `verify_universe_source`, which waives
exactly one failure class — `MISSING_ENDPOINT` — because a listing sweep is not
a chain calculation and holds no index print or open interest. The waived
failures are carried on the receipt and persisted, so a set-aside check leaves a
trace.

### The scope and the pipeline are read off the records

`derive_source_scope` reconstructs the request from the stored endpoint and query
parameters: root, expiration, strike, right, `max_dte`, `strike_range` and
`min_time`. The declaration's scope is a claim that is compared against it and
cannot widen it.

`min_time` is the one that matters and the one v2.1.10 dropped. A sweep taken
with `min_time=15:30:00` returns the contracts that traded after 15:30 — a
smaller set than the same request without it, and one that re-derives perfectly.

`source_pipeline_fingerprint` is read off the verified records.
`IDENTICAL_PIPELINE` is the default policy; a difference is waived only by a
`UniverseOnlyCompatibilityRule` naming both fingerprints and every differing
parameter, and that rule refuses at construction if any of them decides the
contract set.

### Documentation identities are extracted

A rule no longer carries contracts. It names a document, an effective period and
an `extractor_version`; a registered extractor reads the verified bytes and
emits a `UniverseExtractionArtifact` recording the document hash, the extractor
version, the rule id, the character ranges read, the identities found and the
instant the extraction ran. `observed_at` comes from that instant, not from the
declaration.

Effective periods are enforced against `market_session_date`, and a rule that
states no period establishes nothing. The production registry is empty: no
document stating which SPX/SPXW contracts exist has been read (OD-11).

### Recovery compares everything

`recover_capture_artifacts` requires `rederived.artifact_hash ==
stored.artifact_hash`, and names the first semantic field that moved. v2.1.10
compared the identity set and the coverage status, so `observed_at`, the source
scope and the source fingerprints were free.

The whole chain is persisted content-addressed — capture verification (with the
source manifest), resolution receipt, extraction artifact, verified universe — so
recovery after a process restart needs no registry anyone populated.

### Readiness names what it checked

`analytical_readiness_of` is now `universe_readiness_of`, returning
`UNIVERSE_READY` / `UNIVERSE_NOT_READY`. `assess_analytical_readiness` checks all
six conditions and returns `NOT_ANALYTICALLY_READY` naming each one it could not
establish. A function returning a dataset-ready state on one of six checks was
the defect.

### The command

```bash
python -m src.tools.capture_thetadata_once \
  --config config/thetadata_capture.yaml \
  --output /absolute/path/outside/this/repo/capture-2026-08-04
```

Dry run by default, `--execute-live` to contact the vendor. The dry run's
pipeline is built with a transport whose every method raises. The live run
refuses a destination inside the repository, requires durable stores, verifies
what it wrote, computes no GEX and places no orders.

**The `--output` directory must not exist.** The run creates it and owns it, so
that everything in it afterwards is what that run wrote. Its parent may exist.
Both modes apply the same rule, so what the dry run accepts the live run accepts.

### What this changes about the shipped state

Nothing. `READY_FOR_RAW_CAPTURE_ONLY`, `NOT_READY_FOR_ANALYTICAL_DATASET`, not
`ADAPTER_CERTIFIED`. What changed is that the reasons are now checks rather than
sentences, and there is a command to run when the account exists.

---

## v2.1.12: the session that actually happens

v2.1.11 gave the first paid capture a command. v2.1.12 is what a review found
when it asked what that command does on a session that really runs — and what it
does when the vendor answers 503.

### The transport is the configured one

The command called `HttpxTransport()` with no arguments and handed it to the
pipeline, bypassing `build_thetadata_client` — which is where the connect
timeout, the read timeout, the response cap and the authentication are applied.
The profile said 30 seconds and 64 MiB; the wire would have had `httpx`
defaults.

It now builds nothing. Both reports carry the effective settings — base URL with
any embedded userinfo replaced, authentication mode, whether credentials
resolved and from which environment variables, timeouts, cap, retries, backoff —
and **no credential value**.

### The origin says which live it was

`HttpxTransport.origin_for` has always distinguished a local Theta Terminal from
a direct vendor call, and nothing called it: `capture_origin_of` read the class
attribute, which is `LIVE_HTTP_CAPTURE`. The shipped profile points at
`http://127.0.0.1:25503`, so every record of the first real session would have
claimed a direct vendor round trip.

Both are live and they fail differently, and any later claim about vendor
behaviour rests on knowing which produced the bytes.

### Every attempt is accounted for

`RetryingTransport` consumes a retryable 429 or 503 body, logs a warning, sleeps
and tries again — so the responses that would explain a partial capture were the
ones thrown away, while the operator documentation said every response was
preserved. That sentence was wrong, and the fix is the sentence becoming true.

An attempt observer inside the retry loop records one `HttpAttemptRecord` per
attempt: endpoint, attempt number, redacted URL, parameter digest, timings,
status, an allow-listed header subset, and either a body hash and location or a
transport error code where nothing came back. Bodies are written
content-addressed under `attempts/`, and **they are not chain data** — the raw
store holds the responses a snapshot was built from.

### A run has a state, and a failure has a report

| State | Means |
|---|---|
| `PLANNED` | the intent document is written; nothing sent |
| `IN_PROGRESS` | at least one request issued |
| `COMPLETED_VERIFIED` | every planned endpoint answered and the manifest verified |
| `COMPLETED_UNVERIFIED` | every endpoint answered; verification or integrity did not pass |
| `FAILED_PARTIAL` | some endpoints answered, then something failed. The bytes are kept |
| `FAILED_BEFORE_REQUEST` | nothing was sent |

`run-intent.json` is written before the first request. A manifest and a summary
are written on every exit path. A partial manifest identifies itself as partial
and cannot pass `verify_capture` — it is missing endpoints the plan requires,
which is the check that should refuse it. Nothing is deleted automatically.

All three documents are serialised, written to a temporary file, fsynced and
renamed.

### Where a capture may go

Resolved with symlinks followed. Refused if inside the repository, a symlink, an
existing file, or a directory holding anything — including an earlier
`run-intent.json`. There is no resume in v2.1.12: each run gets its own
directory, so two captures can never share a manifest. Run ids carry a
cryptographic nonce, because record ids derive from the session id and two runs
in the same second used to collide.

The dry run writes nothing at all: the store capability is probed in a temporary
directory deleted before the report returns.

### Documentation evidence survives the process

The v2.1.11 documentation path could not be used. `capture_session` re-runs a
resolution before opening the chain operation, and the re-run consulted
`UNIVERSE_DOCUMENTATION_RULES` — so a resolution made with a caller's own
registry was refused by the capture that had just accepted it, and the global
registry is empty in production. Recovery had the same shape.

A `UniverseDocumentationEvidenceArtifact` now carries the rule in portable form
(no host path), the digest of the exact verified bytes, the artifact key those
bytes live under, the extractor version and the extraction. The bytes are stored
content-addressed. Re-running and recovering both consult no global state, and a
changed document or a changed rule fails.

### Differences are derived; readiness is derived

`UniverseOnlyCompatibilityRule` took `differing_parameters` from the caller — who
was the one asking for the waiver. `derive_parameter_diff` computes the diff from
two configurations, any contract-set-affecting difference is refused whatever the
rule says, and the rule carries only the digest of the difference it approves.

`assess_analytical_readiness` took six loose `Any` arguments, and six
`SimpleNamespace` objects with the right attribute names were ready. It takes
only a `VerifiedAnalyticalEvidenceContext`, and `build_analytical_evidence`
re-derives the chain, re-verifies the capture and recovers the capture-bound
artifacts itself.

### What this changes about the shipped state

Nothing. `READY_FOR_RAW_CAPTURE_ONLY`, `NOT_READY_FOR_ANALYTICAL_DATASET`, not
`ADAPTER_CERTIFIED`. What changed is that the session which produces the first
evidence is now safe to run, and honest about what it produced.

---

## v2.1.13: what running it found

v2.1.12's command was reviewed against a session that actually happens. The
finding that matters is small to state and was invisible from inside the report.

`build_thetadata_client` constructed `FileRawStore(config.raw_capture_path)`
during pipeline construction. The operator writes its capture to `<output>/raw`
and hands that store to the session, so the configured one received nothing --
and the shipped profile names `artifacts/raw`. Building a pipeline created that
directory inside the checkout. The dry run, which exists to write nothing and
reports `wrote_files=false`, created it too.

Nothing constructs a filesystem store from a configuration path any more. A
`raw_capture_path` names where a store *would* go; the operator builds exactly
one, under a run root it has claimed, and the report distinguishes the configured
fallback from the effective destination.

### The destination is claimed, not checked

`mkdir(exist_ok=False)`, after path validation and before any store, attempt log
or intent document exists. v2.1.12 checked that the path was empty and created
the stores afterwards, so two processes could both observe an empty path and both
proceed -- reproduced, with two runs completing into one directory, mixing eight
records and overwriting each other's top-level documents. Exactly one `mkdir`
wins now.

### The stored bytes are the vendor's bytes

`HttpResponse.body` carries the HTTP entity body after content decoding, the
store writes and hashes exactly those bytes, and decoding is a separate recorded
step: content type, declared and selected charset, whether any byte was replaced,
and the digest of the text alongside the digest of the bytes.

v2.1.12 decoded in the transport with `errors="replace"` and the store
re-encoded that string as UTF-8. Two lossy conversions between the socket and the
file, and the digest was called the hash of the vendor's response.

### Every attempt, including the ones with nothing in them

An oversized response now produces a typed `RESPONSE_TOO_LARGE` attempt carrying
the configured cap and the bytes read -- from the cap check and from the
streaming reader, which are two different places the same failure can surface.
v2.1.12 raised from both and the attempt log reported zero attempts, on the one
failure where the size of the thing is the finding.

Run state is derived from the attempt log rather than from stored records, so
"nothing was sent", "nothing answered" and "something answered and then stopped"
are three states rather than one.

### A vendor's refusal is not our bug

The classifier covers the whole public `ThetaDataError` hierarchy and follows the
cause chain, so a 401 is `AUTHENTICATION_REJECTED`, a 400 is `VENDOR_HTTP_ERROR`,
and a retry budget spent on 429s is `RATE_LIMITED`. All three were
`INTERNAL_ERROR`, which points an operator at this code instead of at their
environment.

### Evidence that outlives the run

`attempts/index.jsonl` is appended and fsynced as each attempt happens, so the
attempt evidence survives an interpreter that dies or a finalization that fails.
The lifecycle is wrapped and the transport is closed in `finally`; when
finalization is itself what broke, the run writes an emergency summary carrying
what is in memory and saying plainly that there is no manifest.

**The guarantee, stated accurately:** every ordinary controlled failure produces
a manifest and a summary; a storage or finalization failure produces a
best-effort emergency summary.

### Two identities, kept apart

A destination path is an operational fact. It was inside the pipeline
fingerprint, so the same session written to two disks was two pipelines --
and that fingerprint is stamped on every record and compared by every replay.
Storage keys are excluded from the semantic payload and recorded in the run
report instead.

### What this changes about the shipped state

Nothing. `READY_FOR_RAW_CAPTURE_ONLY`, `NOT_READY_FOR_ANALYTICAL_DATASET`, not
`ADAPTER_CERTIFIED`. What changed is that the command can now be run without
modifying the repository it was run from.

---

## v2.1.14: integrity is a statement about bytes

### The scan read text and reported on bytes

`verify_integrity()` opened every payload with `read_text()`. Two consequences,
both of which would have hit the first paid session:

* **CRLF.** Text mode translates `\r\n` to `\n` on read, so the bytes hashed are
  not the bytes on disk. A vendor sending Windows line endings would have had
  *every record* report `HASH_MISMATCH`. An operator reading that concludes the
  capture is corrupt.
* **Anything not UTF-8.** One undecodable byte raised `UnicodeDecodeError`,
  which aborted the scan -- so a single odd payload made every *other* record
  unverified too.

It now reads bytes and hashes bytes. Decoding is the parser's job; this layer
answers one question and nothing else.

### What a stored payload is, written down

The digest says the bytes have not changed. It does not say what they are. Every
record and every manifest entry now carries `raw_response_schema_version`,
`body_representation`, the content type, the declared and the selected charset,
the decode status and the digest of the decoded text -- inside the manifest hash,
so the description travels with the evidence and cannot be edited away from it.

A record written before v2.1.14 states no schema, and `validate_metadata` and
`verify_capture` refuse it rather than reinterpreting it under rules it was not
written under. Its digest may cover a UTF-8 re-encoding of decoded text.

### The timeout that was reported and the timeout that was applied

`httpx` reads a scalar `timeout=` as connect *and* read *and* write *and* pool.
Passing a per-request read budget therefore discarded the configured connect
budget -- the one the profile states, the client is constructed with, and the dry
run prints. The transport now holds a full `httpx.Timeout`; a per-request budget
rebuilds every dimension rather than replacing all of them.

### Provenance is not decided by the vendor's own text

Whether a capture is evidence about the vendor or a local fixture was answered by
searching the whole URL -- path and query included -- for `localhost` or
`127.0.0.1`. A redirect carrying `?next=localhost`, or a host called
`notlocalhost.com`, would have answered it for us. The host is parsed, lowercased
and compared: `localhost`, or an address `ipaddress` calls loopback.

### A run that never starts leaves nothing behind

The destination used to be created first, and the configuration loaded,
credentials resolved, pipeline built and readiness graded afterwards. Every one
of those failures left an empty directory -- which the next attempt refused,
because a capture directory is created by the run that owns it. So an operator
had to delete the evidence of their own typo before they could retry.

Preflight now does all of it before the `mkdir`, against a temporary store, and
the same destination policy applies in both modes: **the destination itself must
not exist. Its parent may.**

### Evidence that survives being moved

Every recorded location was absolute, so copying a run directory to an archive
host produced an index describing a directory that is not there. Locations are
relative to their store root and resolved against where the store is now; the run
summary names one absolute `output_root` and everything else relative to it. The
regression copies a completed run elsewhere, deletes the original, and verifies
from the new location.

### What this changes about the shipped state

Nothing. `READY_FOR_RAW_CAPTURE_ONLY`, `NOT_READY_FOR_ANALYTICAL_DATASET`, not
`ADAPTER_CERTIFIED`. What changed is that a capture taken with it can be verified
by somebody who is not the machine that took it.

---

## v2.1.15: getting the bytes is not understanding them

### The command was raw-only and parsed as it went

`pipeline.fetch_chain()` fetches the index snapshot, **parses it** to read the
spot, and uses that to build the chain request. Every endpoint after the first
is downstream of a successful parse of the one before.

For a session whose entire purpose is to find out what the vendor actually
sends, that is exactly backwards. An HTML error page on the index endpoint --
maintenance, a proxy, a schema nobody had seen -- raised before the quote
request existed. The operator paid for four endpoints and got one, and the
reason was the single most interesting thing the session had found.

Acquisition and interpretation are now separate operations:

* `ThetaDataClient.acquire(endpoint, params, capture=...)` issues one request
  and stores whatever comes back. It raises only when nothing was stored.
* `ThetaDataClient.interpret(acquired)` turns stored bytes into rows, or says
  precisely why they are not rows. Every raise is a finding about a response
  that is already on disk.
* `_get()` is the two composed, so every existing chain reader behaves exactly
  as before.

`pipeline.capture_required_endpoints_raw()` builds every planned request up
front -- from the capture plan, the chain request, the index symbol, the Greeks
parameters and the tier, none of which needs a `ChainSnapshot` -- issues them
independently, and returns one `RawEndpointCaptureResult` per endpoint.

**A 200 that does not parse is `ACQUIRED`.** A non-2xx is `VENDOR_REFUSED`,
with the body preserved byte-exact in the attempt log, and the sweep continues.

### Stopping early is a policy, not an escaping exception

Five named reasons, and only five: rejected credentials, a retry budget spent on
**429s specifically**, two consecutive endpoints answering with nothing at all,
operator cancellation, and a store that cannot write. The reason is recorded in
the run report whether or not it fired.

A 503 retried to exhaustion on one endpoint is a finding about that endpoint.
Treating it as systemic -- which the first draft of this release did -- would
have reintroduced the defect the release exists to remove.

### Two states, because they are two questions

`run_state` is about bytes: did every planned response arrive, and is it stored
and verified. `parser_state` is about what those bytes say. A capture where all
four endpoints answered and none of them parse is a **successful** discovery
session, and reporting it as a failed run is what made a schema error look like
a reason to stop requesting.

Parsing runs after finalization, against the store, into `parser-report.json`
with its own schema version. No GEX is computed.

### The claim is guarded from the moment it is made

`mkdir(exist_ok=False)` makes the directory this run's responsibility. v2.1.14
then constructed the attempt log, three stores, the transport and the pipeline,
validated durability, derived the origin, opened the capture session and wrote
the intent -- all before the guard. Any of them raising left an empty directory
nobody had written a word about, which the next invocation refused as an earlier
run's.

A bootstrap run object now exists before anything else can fail, and everything
after the claim is inside one `try/except/finally`. Every post-claim failure
produces `capture-bootstrap-failure.json` naming which resources had been built;
a directory containing nothing at all is removed rather than left ownerless.

### Replay consumes the bytes, not a reading of them

`StoredPayloadTransport.from_capture()` used `store.get_payload()`, which
decodes UTF-8 with replacement. A latin-1 body replayed as U+FFFD; a body with
one invalid byte was re-encoded into something the capture never contained. A
replay that changes the evidence is not a replay, and the comparison it feeds --
"does the rebuilt chain equal the original?" -- was answering a different
question from the one it claimed to.

It now reads `get_body()` and carries the captured headers, so the ordinary
decoder selects the charset the capture selected. Before anything is parsed, the
reconstructed response is checked against the record's own decode: body hash,
byte length, decode status, selected charset, decoded-text hash. A disagreement
refuses the replay.

### Evidence that verifies after the process is gone

`HttpAttemptLog(root)` starts with an empty in-memory list, and `verify_bodies`
iterates over it -- so opening an archived capture and asking whether it had
been tampered with always answered "no". `open_existing()` derives everything
from disk: the index is parsed, every schema validated, every fingerprint
recomputed, every body located, hashed and measured, and orphaned bodies
reported. A malformed line in the *middle* of the index is a finding; only a
torn final line is forgiven, because that is an interrupted append and
forgiving it is what append-only buys.

The index hash, the counts and the schema are recorded in the capture summary at
finalization, so a later reader can tell whether the log has changed since.

### A location that cannot lie

`verify_integrity` confirmed `payload_location` was relative and then derived
the path from `record_id` instead. An index could name `missing/other.raw` and
the scan reported VALID. The location must now equal the store's canonical
location for that record, it is carried on `ManifestRecord`, it is inside the
manifest hash, and `verify_capture` compares it against the store.

### The disk requirement comes from the configuration

The shipped profile allows a 64 MiB response per endpoint, four endpoints, four
attempts each. The preflight asked for 64 MiB total -- so it passed on a disk
that could not hold one endpoint's response. It is now derived: endpoints x cap
x (attempts + 1), plus fixed overhead, times a stated safety margin, printed by
the dry run with its inputs.

### What this changes about the shipped state

Nothing. `READY_FOR_RAW_CAPTURE_ONLY`, `NOT_READY_FOR_ANALYTICAL_DATASET`, not
`ADAPTER_CERTIFIED`. What changed is that the first session will now come back
with all four endpoints' bytes whatever any of them turn out to contain.

---

## v2.1.16: the request plan the first session will actually run

### An option root and an index are two different instruments

The shipped profile captures `SPXW` -- the PM-settled weekly series on the S&P
500 index. The index itself is `SPX`. v2.1.15 held one symbol on the chain
request and gave it to every endpoint, so the first paid session would have
asked:

    GET /v3/index/snapshot/price?symbol=SPXW

for the price of something that is not an index. Whatever that returned would
have been recorded as the vendor's underlying print and become the denominator
of every gamma in the chain.

The fix is a table, not a rule:

| option root | underlying index |
|---|---|
| `SPX` | `SPX` |
| `SPXW` | `SPX` |

"Strip a trailing W" would turn `SPW` into `SP` and would invent an answer for a
root nobody has modelled. A root that is not in the table is **refused**,
because an index symbol this repository derived by string manipulation would put
an unchosen spot under every number it produces.

**Why it survived review.** The rule existed three times. The fetch path derived
it; `build_request_spec` derived it again to state "what this session would
send"; `assess_readiness` rebuilt the capture plan from four of the pipeline's
inputs. All three were wrong in the same way, so the verifier agreed with the
defect it was verifying. There is now one mapping, and certification consults
the pipeline's own plan rather than reconstructing it.

Both symbols are in the capture-plan fingerprint, so a capture taken under the
old mapping cannot be presented as one taken under the corrected one.

### The contract listing is captured, and settles nothing

`/v3/option/list/contracts/quote` exists. The first session requests it, with
`symbol=SPXW`, the New York market-session date, and the same `max_dte` as the
chain request -- a listing over a wider window would enumerate contracts the
snapshot never asked for.

It is an **evidence** endpoint, not a required one. `verify_capture` insists on
every required endpoint, so requiring the listing would have retroactively
invalidated every capture taken before it existed, and a chain does not need it.
`CapturePlan` separates the two; the raw sweep requests both.

And it authorises nothing. `is_dedicated_contract_list` is True --
that is what the endpoint is *for*. `enumerates_request_universe` stays False,
because a list of everything quoted on a session is a different set from the
contracts a request bounded by `max_dte` and `strike_range` was owed, and nobody
has compared the two against real bytes. Until that comparison exists the
evidence state is `DEDICATED_CONTRACT_LIST_OBSERVED_UNVERIFIED`, and no path
leads from it to `FULL_REQUEST_ENUMERATED`, `MEASURED_COMPLETE` or
`READY_FOR_ANALYTICAL_DATASET`.

Promoting it on the strength of its name would be the v2.1.9 defect with better
vocabulary.

### The plan is visible before it is authorised, and binding after

The v2.1.15 dry run printed a count of endpoints and a tier. That is not enough
to notice `symbol=SPXW` on an index request, which is precisely why nobody did.

`RawRequestPlan` is derived up front and printed in full: every endpoint, its
safe path, its sorted query parameters as strings, its required tier, its
request-spec hash and its stop policy. The two symbols, the DTE window, the
listing date and the rate and dividend parameters are all on the page before
`--execute-live`. No credential is in it.

The live run authorises each request against the same document -- a request that
differs from the plan raises `RequestPlanViolation` before it reaches the
transport -- and the plan is persisted in the run intent and the summary.

### Attempt evidence is part of what "verified" means

A run could report `COMPLETED_RAW_VERIFIED` beside `attempt_evidence.ok =
false`. Four layers now gate it: raw-store integrity, the capture manifest,
required-endpoint acquisition, and HTTP attempt evidence. A failure in any of
them names `verification_layer` and `verification_findings`.

The captured responses are not discarded and not downgraded. What changes is the
claim the run makes about itself.

### Two doors, named for what they do

`HttpAttemptLog.create_new(root)` refuses a directory that already holds an
index. `open_existing(root)` loads and verifies. `verify_bodies()` refuses when
an index exists that this log never read, so an empty result means "nothing is
wrong" and never "I looked at nothing".

### What this changes about the shipped state

Nothing. `READY_FOR_RAW_CAPTURE_ONLY`, `NOT_READY_FOR_ANALYTICAL_DATASET`, not
`ADAPTER_CERTIFIED`. What changed is that the requests the first session makes
are the requests it should make, and an operator can read them before paying.

---

# v2.1.22: the first live capture

The first paid ThetaData session ran on 2026-08-10 at 14:01:29Z, inside a
regular trading session. It completed `COMPLETED_RAW_VERIFIED`: five endpoints,
five responses, every payload hashing to what the manifest says.

    session   capture-20260810T140129Z-2ef4f56270c1447b
    manifest  2f45534bbb569dfeb3e251b4fe3e27a8bdebbb716d5c0ac5b22f821d43ecbd20
    archive   5fc258007a3390b11960d7f3fa46a329f1277a899faf5a9a0a4f56598882d638

The raw payloads are **not** committed. They are paid market data and this
project has no answer to the licensing question, so what is committed is
`tests/fixtures/live_capture/first_capture.json` -- the digests, the identity-set
hashes and every statistic below, all emitted by the certification command
rather than typed in. Re-derive the lot with:

```bash
python -m src.tools.certify_thetadata_capture <capture-root> \
    --archive-sha256 5fc258007a3390b11960d7f3fa46a329f1277a899faf5a9a0a4f56598882d638
```

It makes no network request, and two runs over an untouched capture produce the
same `report_hash`.

## Rate units

**The pinned documentation is wrong, and both readings are kept.**

| | |
|---|---|
| `DOCUMENTATION` | `rate_value` is expressed as a percent |
| `LIVE_VENDOR_BEHAVIOR` | `rate_value` is consumed as a decimal annual rate |
| `EVIDENCE_STATUS` | `DOCUMENTATION_LIVE_CONFLICT` |

The OpenAPI description says *"The interest rate, as a percent"*. v2.1.18 read
that out of the bytes correctly, and the capture profile therefore sent
`rate_value=4.2` intending 4.2%. Reconstructing the returned Greeks over 7,348
usable rows:

| `r` | median abs delta error | delta RMSE |
|---|---|---|
| `4.2` | 4.45e-05 | **1.75e-04** |
| `0.042` | 2.21e-01 | 2.32e-01 |

Three orders of magnitude, with nothing ambiguous left in it. The v3
implementation puts the number into Black-Scholes unchanged, so the first
capture was priced at **420%**.

What follows from that:

* the **observed** unit governs request construction. The request is answered by
  the implementation, and a parameter built to satisfy the document instead is
  wrong by exactly the factor the two differ by;
* the **documented** unit is preserved exactly as extracted. The pinned document
  is not edited. "The vendor's published description of this parameter is wrong"
  is a finding about the vendor worth more than the parameter value;
* the conflict does not resolve. A dry run reports
  `RATE_UNITS_AGREE_WITH_OBSERVED_IMPLEMENTATION` -- `MATCHED`, because the
  request is correct, under a distinct code so nobody reads it as the
  documentation having been confirmed.

The corrected profile states the five quantities separately, because a bare
number meaning both percent and decimal is what caused this:

    economic rate            4.2%
    local model r            0.042
    vendor request value     0.042
    vendor observed unit     DECIMAL_ANNUAL_RATE
    documented unit          PERCENT_ANNUAL_RATE

## Day count

`ACT/365`, by reconstruction over the same 7,348 rows.

| convention | delta RMSE |
|---|---|
| **ACT/365** | **1.75e-04** |
| ACT/365.25 | 7.21e-04 |
| ACT/360 | 1.59e-02 |
| ACT/252 | 9.86e-02 |

`evidence_kind = LIVE_NUMERICAL_RECONSTRUCTION`, scoped to SPXW first-order
greeks. Not generalised to other endpoint families.

## Expiration timestamp -- and where it stops

`16:00 America/New_York`, **for the capture week only**.

This one is not a scored hypothesis. Given the reported delta and implied
volatility, `d1` is determined and time-to-expiry follows from a quadratic, so
each row can be inverted for the clock the vendor actually used. Grouped by
expiration:

| expirations | implied − calendar days | reading |
|---|---|---|
| 2026-08-10 … 2026-08-14 | +0.2490 … +0.2503 | 16:00 ET |
| 2026-08-17 … 2026-09-30 | +0.0015 … +0.0198 | whole calendar days |

`0.2489` days is exactly 16:00 ET minus the 10:01:34 valuation stamp. Inside the
capture week the vendor uses an intraday clock to a 16:00 close; from the
following Monday on it uses whole calendar days and no time of day at all.

Scored on the front week alone, 16:00 ET wins cleanly:

| expiry time | delta RMSE |
|---|---|
| **16:00 ET** | **3.48e-04** |
| 16:15 ET | 5.61e-03 |
| 15:30 ET | 9.04e-03 |
| 16:30 ET | 1.21e-02 |

**Do not apply this rule beyond the front week**, and do not apply it to any
other option root. The boundary itself is not pinned down: the capture has
expirations at 4 and 7 calendar days out and nothing between them, so the rule
could be "expiring this week", "DTE ≤ 5 business days" or "DTE < 7". That
remains open.

## IV price basis

`NBBO_MID`. Solving each candidate price for the volatility that reprices it and
comparing against the reported `implied_vol`:

| basis | median abs IV error |
|---|---|
| **NBBO mid** | **5.08e-05** |
| bid | 4.99e-03 |
| ask | 5.06e-03 |

`implied_vol` is reported to four decimals, so 5.0e-05 is half a tick -- the
rounding floor of the field being compared against. There is nothing left for a
better hypothesis to explain. Bid and ask are two orders of magnitude worse.

The current first-order OpenAPI description refers to a **trade price**, so this
is a second documentation/live conflict and is recorded as one.

## Underlying synchronisation

The Greeks response carries its own underlying, and it is not the index
snapshot:

    vendor_greeks_underlying_price       7759.27
    vendor_greeks_underlying_timestamp   2026-08-10T10:01:34.000 ET

    index snapshot price                 7759.54
    index snapshot timestamp             2026-08-10T10:01:33.000 ET

One distinct underlying price and one distinct timestamp across all 14,556 rows.
For reproducing ThetaData's model the embedded fields are authoritative:
`UNDERLYING_SOURCE = GREEKS_RESPONSE_EMBEDDED_VENDOR_UNDERLYING`.

The separately captured `/index/snapshot/price` response is **not** the
underlying state these Greeks were computed from and must not be described as
synchronised with them. `gex_evaluation_spot` and `gex_evaluation_timestamp` are
a different pair of concepts and are kept separate.

## Contract-list universe

    contract list  14,556
    quote          14,556
    greeks         14,556

Set-identical, by SHA-256 over the sorted canonical identities -- not by count,
because two responses of 14,556 rows can hold different contracts. State
promoted for **this request form only**:

    DEDICATED_CONTRACT_LIST_MATCHED_SNAPSHOT_UNIVERSE

Scope: SPXW, session 2026-08-10, `max_dte=60`, quote and first-order greeks
snapshots. Not generalised across dates, symbols, tiers or endpoint families.

## Open-interest coverage

    universe           14,556
    OI rows            14,130   (97.073%)
    explicit zero       3,692
    missing               426

Three states, and the middle one is not the last one:

| state | meaning |
|---|---|
| `OI_PRESENT` | the vendor answered with a positive figure |
| `OI_EXPLICIT_ZERO` | the vendor answered zero. A real observation |
| `OI_MISSING` | no row. Could be zero, could be ten thousand |

**A missing row is not a proven zero.** Filling one with zero converts the third
state into the second and deletes the contract from the aggregate without
changing any count a completeness check looks at. Open interest is the linear
weight on every GEX term.

The 426 absent identities are accounted for by set hash and by expiration. One
of them is a whole expiration: **2026-09-16 has 150 contracts listed, quoted and
greeked, and zero open-interest rows.** A 97% coverage figure reads like
scattered gaps; this one is not scattered.

No trusted aggregate GEX until there is an evidence-backed policy for these
identities. There is not one yet, and inventing one here would be choosing
silently.

## Quote and Greeks are not atomic

Two sequential HTTP requests, not one observation:

    identical timestamp   74.9%
    identical bid         96.9%
    identical ask         97.0%
    p99 gap                2.444 s
    max gap               51.593 s

For reproducing the vendor's IV, use the bid/ask **embedded in the Greeks
payload**. Quote-sourced analytics keep the quote's own timestamp. Joining the
two on contract identity and calling the result a single snapshot asserts an
atomicity the capture disproves.

## What this capture is, and is not

    ADAPTER_CERTIFICATION_EVIDENCE
    NOT_TRUSTED_FOR_GEX

Its Greeks were generated at 420%, so every implied volatility and delta in it
describes a market that does not exist. It is still the only reason this
repository knows how the rate parameter is consumed, and it must never be
discarded.

Two independent blockers, and fixing the rate does not fix the second:

1. the rate the Greeks were computed under;
2. 426 contract identities with no open interest.

## The next capture

A new session with `rate_value = 0.042`, acquiring the same five endpoint
classes independently. Afterwards, run the certification command again and check
that IV and delta now land at ordinary SPX volatility levels and reproduce local
Black-Scholes under `r = 0.042`.

Do not assume that before observing it.

---

# v2.1.23: certification stops assuming the first capture

v2.1.22 reconstructed the first capture correctly and then encoded its answers
into the machinery that was supposed to find them.

    inversion              rate = 4.2
    rate hypotheses        {4.2, 0.042}
    day-count scoring      rate = 4.2
    IV reconstruction      rate = 4.2
    ledger labels          "ACT_365", "16:00 America/New_York"

Every one of those is the first capture's own result. The planned second capture
sends `rate_value = 0.042`, so it would have been scored against the wrong pair
of hypotheses and then labelled from the wrong literals — a confident, wrong
certification.

## The wire value comes from the capture

Hypotheses are derived from whatever the capture actually sent:

    DECIMAL_ANNUAL_RATE   r = w
    PERCENT_ANNUAL_RATE   r = w / 100

For `w = 4.2` that is `{4.2, 0.042}`; for `w = 0.042` it is `{0.042, 0.00042}`.
Nothing in the code knows which.

`w` is **not** a caller argument. Adding one would be worse than the constant it
replaced: it would let anyone pair one capture's responses with another
capture's rate and get a certification out. Instead the value is read from the
run intent and proved against the manifest —

    run-intent      request_plan.requests[].canonical_query_parameters
    manifest        records[].planned_request_hash
                    records[].request_spec_fingerprint

— by recomputing the request digest from the parameters and comparing it with
the stamp taken at capture time. Edit `rate_value` in the intent and the
recomputation no longer matches, and certification refuses. The manifest itself
is verified by recomputing its own digest from its own descriptors, so the
anchor is not a number somebody could also have edited.

## The rate and the day count are searched together

Neither resolves alone. The inversion that reads the clock needs a rate *and* a
denominator, and the clock decides which expirations belong to which regime.
Read a 360-day vendor at 365 and every implied day is 1.4% long — enough to push
a whole-calendar-day expiration past the intraday threshold and misclassify the
regime outright.

So all `2 × 4` combinations are reconstructed end to end and the best fit wins.
The published tables are slices through that grid, relabelled for the dimension
each is about, so a table can never disagree with the winner it was drawn from.

## The clock is read, not matched

`implied_days − calendar_days` is the gap between the valuation stamp's time of
day and the expiry's, so the intraday readings *state* the close. Candidates are
then built on a five-minute grid around that estimate and scored.

The snap matters. On the first capture the implied offset drifts with maturity —
0.2490 at the front, 0.2503 a week out — so the median lands at **16:01** for a
close that is really 16:00, and 16:01 even scores marginally better
(3.10e-04 against 3.48e-04). That gap is inside the noise the four-decimal
`implied_vol` creates. Exchanges close on the clock; reconstructions do not. The
unrounded estimate stays in the evidence as `implied_clock_et`, so both numbers
are visible and a reader can disagree.

## Two different questions about the rate

v2.1.22 ran these together and blocked the first capture on the documentation
conflict. That was right by accident.

| | |
|---|---|
| `rate_units_documentation_live_conflict` | does the vendor read its own parameter as it documents it? |
| `capture_effective_rate_matches_intended_rate` | did *this request* buy the rate it meant to? |

The first is permanent and is a statement about the vendor. The second is about
one capture, and the corrected profile fixes it while the conflict stands.
Conflated, a correct second capture would inherit the first one's blocker
forever.

    capture #1   wire 4.2     vendor r = 4.2     intended 0.042   ratio 100   BLOCKED
    capture #2   wire 0.042   vendor r = 0.042   intended 0.042   ratio 1     not blocked
    both         documentation/live conflict = TRUE

The intended rate cannot be recovered from the wire value — that ambiguity *is*
the defect — so the run intent now records it. Captures taken before v2.1.23 do
not carry one, and rather than guess, certification reads the wire value under
the vendor's **documented** unit and says so
(`intended_rate_source = DERIVED_FROM_DOCUMENTED_UNIT`). For the first capture
that gives 0.042 against a vendor 4.2, which is exactly what happened.

## Expiration scope is data, not prose

`expiration_clock_evidence` carries the scope structurally:

    implied_clock_et          16:01 America/New_York   (unrounded estimate)
    intraday_expirations      2026-08-10 ... 2026-08-14
    whole_day_expirations     26 expirations, named
    contradicting_count       26
    scope_is_global           false
    boundary_last_intraday    2026-08-14
    boundary_first_whole_day  2026-08-17
    boundary_gap_days         3
    boundary_status           OPEN

`OPEN` because the sample jumps from four days out to seven with nothing
between, so "expiring this week", "five business days" and "under seven days"
are all still consistent with it. A downstream consumer can establish that
without parsing English.

## Manifest identity and archive identity

`archive_sha256 = supplied or capture.manifest_hash` filled a field named after
one artefact with the digest of another. It is optional now, empty when no
archive has been hashed, and an `archive_sha256` equal to the `manifest_hash` is
refused by the type — two artefacts cannot share a digest, so equality is the
substitution and nothing else.

### An adjacent defect, fixed because this one needed it

`RawCaptureManifest.rebuilt_from` restored every descriptor field except
`preflight_approval_hash`, which `semantic_payload()` — and therefore
`manifest_hash` — covers. Rebuilding the first live capture produced
`c46633ae…` against a stored `2f45534b…`.

The round trip was silently lossy, so any check built on recomputing the digest
would have refused every honest capture and learned nothing about a dishonest
one. Same omission as v2.1.20's `ManifestRecord.of`, in the other direction: a
field added to the descriptor has to be added in three places, and nothing
failed when it was added in two.

## What the tests now prove

The synthetic vendor is parameterised, and varied against the grain: wire values
of 4.2 *and* 0.042, a percent-reading vendor as well as a decimal one, ACT/360
and ACT/252 as well as ACT/365, closes at 15:30 and 16:30 as well as 16:00.
Assertions are on the final evidence objects, because a score table can be
correct while the label beside it is a constant.

The first capture's findings are unchanged and reproduce from the fixture:
`ACT_365`, 16:00 ET front-week, whole calendar days beyond, documentation
conflict retained, and still blocked from a trusted GEX.
