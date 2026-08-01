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
