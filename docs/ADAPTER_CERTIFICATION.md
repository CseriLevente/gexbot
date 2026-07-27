# Adapter certification

Status: `IMPLEMENTED` · `TESTED_SYNTHETICALLY` · `READY_FOR_ADAPTER_CERTIFICATION`
· `NOT_VALIDATED_WITH_LIVE_THETADATA`.

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
)
if not readiness.ready:
    print(readiness.blockers)
```

---

## Blockers

A blocker means the capture would produce data whose meaning cannot be stated.

| Blocker | Why it blocks |
|---|---|
| Pricing assumptions not established | Only under `VENDOR_IV_LOCAL_GAMMA`, which mixes a vendor-computed IV into a local gamma. Five vendor-side conventions are undocumented (see below). |
| Missing open-interest provenance | Open interest is the weight on every GEX term. A capture with no settlement date cannot be interpreted afterwards. |
| Missing spot source or timestamp | Every gamma is computed against this print. Without its clock there is no way to show it was contemporaneous with the chain. |
| Spot skew beyond tolerance | The chain and the underlying describe different moments, so the pairing is not meaningful. |
| Raw store not clean | Starting a paid session on top of an inconsistent audit trail makes new evidence hard to separate from old. |

## Warnings

A warning is a documented limitation the capture should record, not a reason to
refuse it.

| Warning | Why it is not a blocker |
|---|---|
| Caller-supplied open-interest date | Usable, provided the report says it was ours rather than the vendor's. Listed in `unverified_fields`. |
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

`OpenInterestProvenance` now carries `source` and `caller_supplied`, and
`is_verified` is true only when the vendor supplied it. A caller-supplied date
is accepted and surfaced in `unverified_fields` — never described as observed.

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

Only `VENDOR_IV_LOCAL_GAMMA` requires it, because it is the only mode that mixes
vendor and local quantities inside one calculation.

| Mode | Requires agreement | Status |
|---|---|---|
| `LOCAL_IV_LOCAL_GAMMA` | no | `IMPLEMENTED` |
| `VENDOR_GAMMA_VALIDATION` | no — the vendor gamma is compared, not aggregated | `IMPLEMENTED` |
| `VENDOR_IV_LOCAL_GAMMA` | **yes** | `IMPLEMENTED`, blocked while conventions are `UNKNOWN` |

Five vendor-side dimensions are undocumented and are reported as `UNKNOWN`
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
