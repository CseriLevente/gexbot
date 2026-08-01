# v2.1.4 completion report

## Readiness state

**`READY_FOR_RAW_CAPTURE_ONLY`.**

The shipped configuration may take a raw ThetaData capture. It may **not** be
trusted to compute a GEX number from one: six load-bearing vendor pricing
conventions are `UNKNOWN`, and each of them changes gamma.

`calculation_trusted` is `False`. `ADAPTER_CERTIFIED` is not reported, is not
reachable from anything this repository ships, and would require a live
comparison against real vendor output that has never been run.

No component has been validated against live vendor data. No ThetaData request
has ever been made.

## What this release was about

v2.1.3 built the machinery for deciding whether vendor numbers may be trusted.
v2.1.4 is about that machinery being checkable rather than merely present.

### The severest defect

```python
assess_readiness(pipeline=..., capture_manifest=object(), validation_report=object())
# -> CertificationState.ADAPTER_CERTIFIED
```

Both parameters were typed `Any` and both were tested with `is not None`. The
strongest claim in the repository was two truthy values away, and no evidence of
any kind was required to reach it.

It is now a `TypeError`. `capture` must be a `CaptureVerification` produced by
`verify_capture()`, which compares the manifest's record ids and payload hashes
against the store that is supposed to hold them. `validation` must be an
`AdapterValidationReport` carrying a non-empty check list and bound to a
specific `manifest_hash`.

### Prose was load-bearing

Compatibility findings were stored as sentences, and which unknowns blocked was
decided by searching those sentences for a field name:

```python
if any(name in field for name in LOAD_BEARING_COMPATIBILITY_FIELDS)
```

Rewording `"risk_free_rate: units undocumented"` to `"the interest rate
convention is not published"` turned a blocker into a warning, silently. The
same prose entered the replay hash, so a documentation edit moved the digest of
an unchanged calculation while a genuine change of finding with unchanged
wording did not move it at all.

Findings are now `PricingDimensionResult` objects: a `PricingDimension`, a
`CompatibilityStatus`, a machine-readable code, both values, and optional
evidence. Whether a dimension is load-bearing is a property of the dimension.
`detail` is carried for humans and excluded from every decision and every hash.

### Two questions, one enum

`VENDOR_GAMMA_VALIDATION` was a third `PricingMode`. Selecting it moved a
session *out of* `VENDOR_IV_LOCAL_GAMMA` — and out of the vendor-IV
compatibility checks, which it still needed, because vendor IV was still feeding
the local gamma. Asking for a gamma comparison switched off the checks that
comparison had nothing to do with.

`IvGammaPricingMode` and `VendorGammaPolicy` are now separate fields. The
assessment runs whenever the IV is vendor-computed, whatever the gamma policy
says. Tier requirements are additive rather than substitutive.

### Two questions, one ladder

Capture readiness and calculation trust shared a state machine, so an unresolved
vendor convention blocked the capture that would have resolved it. The repository
refused to collect the evidence that would have unblocked it.

Six states now, and unknown pricing permits a raw capture and never a trusted
calculation.

---

## Sections

| § | Requirement | State |
|---|---|---|
| 1 | Separate `IvGammaPricingMode` from `VendorGammaPolicy` | done |
| 2 | Typed `PricingDimension` / `CompatibilityStatus` / `PricingDimensionResult`; `hard_failures` honoured | done |
| 3 | Six certification states; unknown pricing permits capture, never calculation | done |
| 4 | Typed, verified capture evidence | done |
| 5 | Typed `AdapterValidationReport` bound to a manifest hash | done |
| 6 | Raw capture mandatory for capture readiness | done |
| 7 | Canonical pipeline API; no `pipeline=pipeline`; no `request=` overrides | done |
| 8 | `config/thetadata_capture.yaml`; synthetic provenance refused | done |
| 9 | Configuration errors join `ThetaDataError` | done |
| 10 | `ThetaDataConfig()` valid by construction; `as_dict()` never raises | done |
| 11 | `contract_identity` uses `canonical_strike`, no float round-trip | done |
| 12 | Replay hashing semantic, not prose | done |
| 13 | CI triggers on `master`; 3.12 and 3.13 evidence | partially — see below |
| 14 | Provenance distinguishes planned / observed / validated | done |
| 15 | Versions: package, engine, certification schema, parser | done |
| 16 | Regression tests | done |
| 17 | CI job list | done |
| 18 | Clean release archive | done |
| 19 | Completion report | this document |

---

## Defects fixed

Fourteen numbered defects, three found while fixing them, and seven found by
reviewing this release before it shipped. The full tables are in
[CHANGELOG.md](../CHANGELOG.md).

### The review of this release found more than the release did

Worth stating plainly, because it is the most useful thing in this document.
A review of the v2.1.4 diff surfaced seven defects, **five of them inside the
machinery written to close v2.1.3's bypass**. Two recreated that bypass exactly:

```python
ProvenanceEvidence(raw_record_id="no-such-record", field_path="x",
                   manifest_hash="qqq")   # -> VALIDATED -> ADAPTER_CERTIFIED
```

Evidence was checked for being *well-formed* — three non-empty strings — and
never for being *true*. Nothing compared the record id against the store or the
manifest hash against the capture. The tests did not catch it because the
fixtures wrote `manifest_hash="deadbeefdeadbeef"` as a literal, which named a
session that had never happened, and every assertion passed anyway.

The second: `LOCAL_CONFIGURATION` evidence was accepted on every dimension, so
seven attestations typed into a YAML file reached `ADAPTER_CERTIFIED` without a
comparison having been run.

The lesson generalises past this repository. Replacing a boolean with a typed
object moves the failure from "anyone can assert it" to "anyone can assert it in
a well-formed way" — which is progress only if something downstream checks the
assertion against the world. Both fixes are that check, and the fixtures now
*derive* their evidence from the capture rather than stating it alongside.

All seven were reproduced before being fixed and are regression-tested.

### Found while fixing, not from the list

**An explicitly zero dividend derived a spec that called itself a continuous
yield.** `to_model_spec` mapped `annual_dividend: 0.0` to
`DividendSource.CONFIGURED_CONSTANT`, so a config stating `ZERO_DIVIDEND`
produced a model stating something else, and the compatibility check correctly
reported a convention mismatch on a configuration that was internally fine.

This is worth recording because of *why* it was invisible: v2.1.3's certification
tests reached a compatible state with

```python
replace(built, pricing_compatibility=PricingCompatibilityReport(compatible=True, ...))
```

which asserted the conclusion instead of deriving it. The bypass was hiding a
real derivation bug. Removing the bypass — as instructed — surfaced it on the
first run.

**`MODE_CAPABILITIES` restated an enum.** A table saying what
`vendor_gamma_used_for_gex` was per mode, which could drift from the modes it
described. Folded into `VendorGammaPolicy.aggregates_vendor_gamma`.

**`config/paper.yaml` and `config/live.yaml`** both set
`options_source: thetadata` with raw capture off. Neither can run, but both are
templates, and a template is copied.

---

## Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f` | unchanged | — |
| `EXPECTED_MODEL_FINGERPRINT` | `e05c611b9b953372` | `70b3afda56f505e7` | `VERSION_METADATA_ONLY` |
| `EXPECTED_OUTPUT_HASH` | `4444055b…` | `89f38199…` | `VERSION_METADATA_ONLY` |

**No GEX number changed.** Every total, bucket, per-strike value, wall, void,
root and confidence component score is asserted individually in
`tests/regression/test_frozen_reference_case.py`, and across the whole release
exactly two assertions in that file moved. Both are the digests above, and both
are driven by one string: `MODEL_VERSION`, `gex-engine/2.1.3` → `2.1.4`.

Four v2.1.4 changes could plausibly have reached the output hash. Three
provably do not, and this was **measured against the serialised payload rather
than reasoned about**:

| Change | Reaches the hash | How it was checked |
|---|---|---|
| Engine version | **yes**, twice — `meta.engine_version` and inside `meta.model_fingerprint` | present in the payload |
| Parser version `2.1.3` → `2.1.4` | no | absent: the reference case is synthetic, and no parser touches it |
| Identity spelling `4900.0000` → `4900` | no | absent: the payload carries identity *counts*, never the strings |
| Prose stripped from `meta` | no | this snapshot's metadata contains no prose key to strip |

---

## Versions

| Version | Value | Moved because |
|---|---|---|
| Package | `2.1.4` | the release |
| Engine | `gex-engine/2.1.4` | the release; it is the deliberate way to invalidate a replay |
| Certification schema | `adapter-certification/2.1.4` | the states split and the evidence became typed, so every field reads differently |
| Parser | `thetadata-v3-parser/2.1.4` | the canonical contract identity is spelled differently |

The parser bump deserves its reasoning stated, because the rule is "bump only if
interpretation changes". No value the parser reads has changed meaning: the
contract is the same contract and the strike is the same number. But the
identity string is the **join key** between an expected universe and a received
chain, and two parser versions spelling it differently would not match each
other's output. A replay across that boundary has to be able to see it.

---

## Verification

Every number below was produced on this machine, now.

| Check | Result |
|---|---|
| `ruff check .` | clean |
| `ruff format --check .` | 110 files already formatted |
| `mypy src` | no issues in 56 source files |
| `pytest` | 1786 tests, all passing |
| `pytest -m integration` | 18 passing |
| `pytest -m regression` | 46 passing |
| `pytest -m replay` | 10 passing |
| `coverage report` | 93% (fail-under 90) |
| `python -m src.app` | runs; prints the research-only notice |
| Engine core on a bare interpreter | runs under `python -S -E` from the extracted archive |

### Python 3.13: **UNVERIFIED**

Tests are written for 3.13 and the CI matrix requests it. Neither is evidence
that anything passed on it.

- The only interpreters on this machine are 3.12.10 and 3.11. There is no 3.13.
- The repository has **no git remote**, so no GitHub Actions run has ever
  occurred — including under the corrected `master` trigger.

The v2.1.3 CI configuration triggered `push` on `branches: [main]` while the
repository's branch is `master`. Not one job had ever run on a push. The
workflow was green in the sense that nothing red had happened, which is a
different thing. That is fixed, and `main` is kept alongside `master` so a later
rename cannot switch CI off again — but the fix is untested for the same reason
everything else about CI is untested here.

**Report Python 3.13 as unverified.** It will stay unverified until this
repository has a remote and a run completes.

---

## Release archive

Built with:

```
git archive --format=zip --output=gex-bot-v2.1.4.zip HEAD
```

**This document cannot state the archive's own SHA-256.** It is inside the
archive, so any hash written here would be the hash of a different file. The
digest is reported alongside the delivered `.zip`, and anyone can re-derive it:
the archive is reproducible from a given commit, so `git archive` on the release
commit followed by `sha256sum` returns the same value on any machine.

What *was* verified about the delivered file:

- **Reproducible.** Two consecutive `git archive` runs produce byte-identical
  output, and both match the delivered file.
- **Extracted and smoke-tested.** The engine core computes a snapshot under
  `python -S -E` with no site-packages and no environment, from the extracted
  tree rather than from the working copy.
- **Contains no** `.git`, `.venv`, `.coverage`, `__pycache__`, `.pyc`,
  `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, previous release archives,
  `artifacts/`, or `.env` — checked by scanning every entry name.
- **Contains no credentials.** The eight credential-shaped string matches are
  all in the redaction machinery (`redact_secrets` and its regex) and in tests
  that assert redaction works. Configuration files carry environment variable
  *names* only.
- **Contains no raw market data.** No capture has ever been taken.

---

## Confirmations

- **No stored credentials.** Configuration carries `username_env` /
  `password_env` — names, never values. `ThetaDataConfig.as_dict()` serialises
  the names. `ThetaDataError` redacts credential-shaped text from every message,
  and configuration errors now inherit that redaction rather than reimplementing
  it.
- **No live network in tests.** Every adapter test runs against
  `FakeTransport`. `HttpxTransport` exists and has never been executed.
- **No broker execution, no order code, no trading path.** There is no broker
  adapter, no order type, no execution module and no position sizing.
  `tests/unit/test_architecture.py` asserts their absence, and
  `AdapterCertificationReadiness.trading_enabled` is a constant `False` so that a
  serialised readiness report cannot be quoted as clearance for anything.
- **No raw data in the release.**

The repository remains incapable of placing an order.

---

## What would move this forward

In order, and only the first is cheap:

1. **Give the repository a remote and let CI run.** Python 3.13 becomes verified
   or a real incompatibility surfaces. Nothing else on this list is blocked by
   it, but everything else is harder to trust without it.
2. **Take one raw capture** with `config/thetadata_capture.yaml`. It is permitted
   today. The state becomes `RAW_CAPTURE_COMPLETED`, and no calculation is
   permitted from it yet.
3. **Compare against the captured bytes**, one vendor convention at a time, and
   record each answer as a `PricingAssumptionAttestation` with
   `source: LIVE_COMPARISON` and a reference to the comparison. Six load-bearing
   dimensions need this; six answers make a calculation trustworthy.
4. **Identify a contract-list endpoint** so chain completeness stops being
   `PARTIALLY_OBSERVED` (OPEN_DECISIONS OD-11).
5. **Compare vendor gamma against local gamma** on a Pro subscription with
   `vendor_gamma_policy: COMPARE_ONLY`, and write down the result whichever way
   it comes out.

Only after all of those does `ADAPTER_CERTIFIED` mean anything, and it still
would not mean the repository could trade.

## Not added, deliberately

Real ThetaData requests, Databento, MES/ES futures data, feature-store work,
trading strategies, regime thresholds, a risk engine, position sizing, IBKR,
broker execution, order types, paper trading, live trading, and arbitrary
calibrated trading parameters.
