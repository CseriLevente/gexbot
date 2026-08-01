# v2.1.5 completion report

## Readiness state

**`READY_FOR_RAW_CAPTURE_ONLY`.**

The shipped configuration may take a raw ThetaData capture. It may **not** be
trusted to compute a GEX number from one: eight load-bearing vendor pricing
conventions are `UNKNOWN`, and each of them changes gamma.

`ADAPTER_CERTIFIED` is not reported and is not reachable. The reason is now
mechanical rather than a policy: `AdapterValidator` opens the captured payloads,
reads what it can, and reports the conventions it could not establish — which
keeps the validation from passing. Nothing has to remember to withhold the
claim.

No component has been validated against live vendor data. No ThetaData request
has ever been made.

## What this release was about

v2.1.4 made the evidence typed. v2.1.5 makes it *derived*.

One pattern ran through the three defects that mattered most: **an object whose
presence was the answer.** Each was a public dataclass, each was accepted by the
production path, and none had to have come from the code that checks anything.

```python
CaptureVerification(confirmed_record_ids=("fake",), failures=())   # verified
ValidationCheck(name="anything", passed=True)                      # validated
PricingAssumptionAttestation(dimension=DAY_COUNT, evidence=...)     # MATCHED
```

The third is the sharpest illustration. It carried a `vendor_value` field and
nothing read it, so recording that ThetaData uses ACT/360 while the local model
uses ACT/365F produced `MATCHED`. Observing a disagreement is the thing evidence
most needs to be able to express, and it was the one thing this could not say.

And the calculation had no gate at all. `pipeline.compute_gex(chain)` called the
engine. It ran with six load-bearing dimensions `UNKNOWN`, on a chain that had
never been through that pipeline, with no capture behind it — and the number it
returned was indistinguishable from one computed under settled assumptions.

---

## Sections

| § | Requirement | State |
|---|---|---|
| 1 | Diagnostic and trusted calculation separated | done |
| 2 | No caller-constructed capture verdicts | done |
| 3 | Validation report derived by a validator | done |
| 4 | `LIVE_COMPARISON` removed from static configuration | done |
| 5 | Evidence is an observed value; comparators derive the status | done |
| 6 | Pricing evidence bound to the validation report | done |
| 7 | Field-level provenance verified from raw payloads | done |
| 8 | Configured endpoint set required | done |
| 9 | Vendor index spot fetched inside the capture | done |
| 10 | Raw-store readiness requires a real healthy store | done |
| 11 | Rate units and dividend convention are vendor-owned | done |
| 12 | Provenance timestamps and dates strictly valid | done |
| 13 | Exact contract identity after domain construction | done |
| 14 | Snapshot manifests hold only their own records | done |
| 15 | Derived pipeline decisions recomputed | done |
| 16 | Adapter exception hierarchy completed | done |
| 17 | Versions | done |
| 18 | Regression tests | done |
| 19 | CI and Python versions | partially — 3.13 unverified, see below |
| 20 | Release delivery | done |
| 21 | Completion report | this document |

---

## Evidence-integrity confirmation

| Claim | Test |
|---|---|
| Certification is derived from raw evidence | `test_adapter_validator.py::test_readiness_takes_a_manifest_and_a_store_not_a_verdict`, `::test_a_hand_built_capture_verification_is_refused`, `::test_readiness_recomputes_verification_every_time` |
| A written validation report cannot certify | `::test_a_hand_built_validation_report_cannot_certify`, `::test_dropping_any_required_check_prevents_validation` |
| Live comparison cannot be asserted in YAML | `test_observations_and_comparators.py::test_live_comparison_in_yaml_is_rejected`, `::test_live_comparison_cannot_be_built_through_the_config_object` |
| Observed values are compared, not matched | `::test_act_360_against_act_365f_is_a_mismatch`, `::test_a_thirty_minute_vendor_floor_against_sixty_is_a_mismatch`, `::test_every_comparable_dimension_can_report_a_mismatch` (10 dimensions) |
| Field-level provenance is verified | `test_adapter_validator.py::test_a_missing_field_cannot_be_observed`, `::test_the_wrong_endpoint_cannot_support_the_claim`, `::test_a_greeks_record_cannot_prove_the_index_price`, `::test_a_value_differing_from_the_payload_is_rejected` |
| Capture endpoint requirements are enforced | `test_capture_plan.py::test_one_record_is_not_a_complete_capture`, `::test_a_missing_index_snapshot_is_named_specifically` |
| Vendor index spot is captured automatically | `test_calculation_gate.py::test_the_index_snapshot_is_fetched_in_the_same_capture_session`, `::test_the_spot_and_its_clock_come_from_the_index_payload`, `::test_the_index_record_is_in_the_snapshot_manifest` |

## Calculation-gate confirmation

| Claim | Test |
|---|---|
| Diagnostic and trusted outputs are distinct | `test_calculation_gate.py::test_an_incompatible_pipeline_can_still_run_a_diagnostic`, `::test_a_diagnostic_result_cannot_be_passed_back_as_trusted_input` |
| Unresolved pricing cannot produce a trusted GEX | `::test_an_incompatible_pipeline_cannot_run_a_trusted_calculation`, `::test_the_capture_profile_cannot_silently_compute_a_trusted_gex` |
| Pipeline and chain fingerprints must match | `::test_a_chain_without_a_pipeline_fingerprint_cannot_be_trusted`, `::test_a_chain_from_another_pipeline_cannot_be_trusted` |
| A replaced derived report fails integrity | `::test_a_replaced_compatibility_report_fails_integrity`, `::test_a_tampered_pipeline_cannot_fetch` |

---

## Two findings worth stating

**The `dataclasses.replace` bypass was hiding a real derivation bug — again.**
Reclassifying `rate_units` and `dividend_convention` as vendor-owned (§11) is
not a policy preference. The adapter sends `rate_value` and `annual_dividend`;
it does not send the two labels. `rate_value: 4.2` is 4.2% or 420% depending on
a convention that lives entirely inside ThetaData's API, and the difference is a
factor of a hundred in every gamma. v2.1.4 marked both `MATCHED` from local
configuration — a local declaration settling a remote semantic. Two previously
"resolved" dimensions correctly became unknown, and the shipped profile now
reports eight rather than six.

**Most vendor conventions are not recoverable from a snapshot.** The validator
works: it opens the payloads, re-reads fields, and compares. What it finds is
that a snapshot reports what the vendor *computed*, not the convention it
computed *under*. There is no `day_count` column. Two of the eight dimensions
are partially recoverable by comparison — does the greeks endpoint's
`underlying_price` equal the index print, and does its clock match the quote
instant — and the other six are not in the bytes at all. Recorded as
OPEN_DECISIONS OD-35 rather than papered over.

---

## Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f` | unchanged | — |
| `EXPECTED_MODEL_FINGERPRINT` | `70b3afda56f505e7` | `d3d458592b6f87e0` | `VERSION_METADATA_ONLY` |
| `EXPECTED_OUTPUT_HASH` | `89f38199…` | `568d2c2d…` | `VERSION_METADATA_ONLY` |

**No GEX number changed.** Across the release exactly two assertions in
`tests/regression/test_frozen_reference_case.py` moved, both driven by
`MODEL_VERSION`. Five v2.1.5 changes could plausibly have reached the output
hash; four provably do not, **measured against the serialised payload rather
than reasoned about**:

| Change | Reaches the hash | How it was checked |
|---|---|---|
| Engine version `2.1.4` → `2.1.5` | **yes**, twice | present as `meta.engine_version` and inside `meta.model_fingerprint` |
| Parser version `2.1.4` → `2.1.5` | no | absent: the reference case is synthetic |
| `OptionContract.strike_decimal` | no | absent: the payload has no per-contract strike |
| `calculation_mode` / `trusted` | no | absent: the reference is computed directly, not through a pipeline |
| `spot_provenance` | no | absent, same reason |

## Versions

| Version | Value |
|---|---|
| Package | `2.1.5` |
| Engine | `gex-engine/2.1.5` |
| Parser | `thetadata-v3-parser/2.1.5` |
| Certification schema | `adapter-certification/2.1.5` |
| Validation schema | `adapter-validation/2.1.5` |

The parser bump is warranted on the same rule as v2.1.4: `OptionContract.key`
now carries the canonical strike *string* rather than a float, so the join key
between an expected universe and a received chain is spelled differently. Two
parser versions producing different join keys would not match each other's
output, and a replay across the boundary has to be able to see that.

---

## Verification

Every number below was produced on this machine, now.

| Check | Result |
|---|---|
| `ruff check .` | clean |
| `ruff format --check .` | 117 files already formatted |
| `mypy src` | no issues in 58 source files |
| `pytest` | 1876 tests, all passing |
| `pytest -m integration` | 18 passing |
| `pytest -m regression` | 46 passing |
| `pytest -m replay` | 10 passing |
| `coverage report --fail-under=90` | 92% |
| `python -m src.app` | runs; prints the research-only notice |
| Demo output hash | `568d2c2d39507fa6779d998754eac6c98b0465f0793d68ad1a6982117671b494` |

### Python 3.13: **UNVERIFIED**

Unchanged from v2.1.4, and for the same two reasons:

- the only interpreters on this machine are 3.12.10 and 3.11;
- the repository has **no git remote**, so no GitHub Actions run has ever
  occurred.

Tests are written for 3.13 and the CI matrix requests it. Neither is evidence
that anything passed on it. **Report Python 3.13 as unverified.**

---

## Release archive

Built with:

```
git archive --format=zip --output=gex-bot-v2.1.5.zip HEAD
```

**This document cannot state the archive's own SHA-256** — it is inside the
archive, so any digest written here would be the hash of a different file. The
digest is reported alongside the delivered `.zip`, and the archive is
reproducible from its commit, so `git archive` followed by `sha256sum` returns
the same value on any machine.

What was verified about the delivered file:

- **Reproducible.** Two consecutive `git archive` runs are byte-identical.
- **Extracted and smoke-tested.** The engine core computes a snapshot under
  `python -S -E` with no site-packages, from the extracted tree.
- **Contains no** `.git`, `.venv`, `.coverage`, `__pycache__`, `.pyc`,
  `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, previous release archives,
  `artifacts/`, or `.env`.
- **Contains no credentials.** Configuration carries environment variable
  *names* only; credential-shaped strings appear solely in the redaction
  machinery and the tests that assert it works.
- **Contains no raw market data.** No capture has ever been taken.

---

## Confirmations

- **No stored credentials.** `username_env` / `password_env` are names.
  `ThetaDataError` redacts credential-shaped text from every message, and
  configuration and certification errors inherit that redaction.
- **No live network in tests.** Every adapter test runs against
  `FakeTransport`, which raises on an unregistered URL. `HttpxTransport` exists
  and has never been executed.
- **No broker execution, no order code, no trading path.** No broker adapter, no
  order type, no execution module, no position sizing.
  `tests/unit/test_architecture.py` asserts their absence, and
  `AdapterCertificationReadiness.trading_enabled` is a constant `False`.
- **No raw data in the release.**

The repository remains incapable of placing an order.

---

## What would move this forward

1. **Give the repository a remote and let CI run.** Python 3.13 becomes verified
   or a real incompatibility surfaces.
2. **Take one raw capture** with `config/thetadata_capture.yaml`. Permitted
   today. The state becomes `RAW_CAPTURE_COMPLETED`, and no trusted calculation
   is permitted from it yet.
3. **Establish the vendor conventions.** Two are recoverable by comparison
   against the captured bytes and the validator already attempts them. The other
   six need vendor documentation, or a purpose-built inference — solving for the
   day count that reproduces the vendor's IV from a known price, for instance.
   Neither exists, and neither should be guessed at.
4. **Identify a contract-list endpoint** so chain completeness stops being
   `PARTIALLY_OBSERVED` (OD-11).
5. **Compare vendor gamma against local gamma** on Pro with
   `vendor_gamma_policy: COMPARE_ONLY`, and write down the result whichever way
   it comes out.

Only after all of those does `ADAPTER_CERTIFIED` mean anything, and it still
would not mean the repository could trade.

## Not added, deliberately

Real ThetaData requests, Databento, futures feeds, feature-store work, trading
strategies, regime thresholds, a risk engine, position sizing, IBKR, broker
execution, order classes, paper trading, live trading, and arbitrary calibrated
trading values.
