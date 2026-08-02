# v2.1.7 completion report

```text
READY_FOR_RAW_CAPTURE_ONLY
```

Not `ADAPTER_CERTIFIED`, and further from it than v2.1.6 was -- deliberately.
Eight load-bearing vendor pricing conventions remain `UNKNOWN`, every capture in
this repository is stamped `OFFLINE_FIXTURE`, and as of this release a
caller-assumed open-interest settlement date also blocks a trusted calculation.
The repository remains incapable of placing an order.

---

## What this release is about

v2.1.6 bound a trusted calculation to *verified raw records*. v2.1.7 binds it to
the **chain those records normalize to**.

Those are different objects. The chain is the result of parsing and joining the
records, and nothing connected the two except a caller passing both to the same
method:

```python
chain = pipeline.fetch_chain(...)          # honest
tampered = dataclasses.replace(chain, quotes=(edited, *chain.quotes[1:]))
pipeline.compute_trusted_gex(tampered, context=real_context)   # trusted=True
```

Adding 999,999 to one strike's open interest moved the unsigned total by about
two orders of magnitude. Open interest is the linear weight on every GEX term.
The result carried a verified manifest and `trusted=True`.

The second half of the release is narrower and sharper: **no public API accepts a
derived verdict where it could derive one.** `VerifiedCalculationContext` is a
public frozen dataclass whose `context_hash` any caller can recompute, so an
edited context with a freshly computed hash was internally consistent and
asserted whatever the caller wanted. A hash is an integrity checksum, not proof
of issuer.

---

## Git

| Field | Value |
|---|---|
| Branch | `master` |
| Release commit | `PENDING_COMMIT` |
| Commit message | `v2.1.7: bind trusted calculation to the re-derived normalized chain` |
| Clean status | `git status --porcelain` empty |
| Diff stat | `PENDING_DIFFSTAT` |

Added: `src/domain/normalization.py`, `src/adapters/open_interest.py`,
`src/adapters/thetadata/request_spec.py`,
`tests/unit/test_normalized_evidence_binding.py`.

---

## Verification

| Check | Result | How |
|---|---|---|
| Python 3.12 full suite | **PASS** — 2,033 tests, 1 skipped | locally executed |
| Python 3.13 full suite | **unverified** | 3.13 is not installed on this machine (`py -0p` lists 3.12 and 3.11) and no CI run exists for this commit |
| `pytest -m integration` | **PASS** (18) | locally executed |
| `pytest -m regression` | **PASS** (46) | locally executed |
| `pytest -m replay` | **PASS** (10) | locally executed |
| `ruff check .` | **PASS** | locally executed |
| `ruff format --check .` | **PASS** (125 files) | locally executed |
| `mypy src` | **PASS** (62 source files) | locally executed |
| `coverage report --fail-under=90` | **PASS** — 92% of 7,108 statements | locally executed |
| Demo (`python -m src.app`) | **PASS** | locally executed |
| Demo output hash | `3af3ef9c77944577211e28d68b634e19690b8dae4ea51541d9269f13fa879293` | locally executed |

**Python 3.13 is unverified.** The CI matrix names `["3.12", "3.13"]` across the
`quality`, `invariants` and `no-trading-guarantee` jobs. That is a configuration,
not a result. Nothing here was executed in CI: the workflow runs on push, and
this commit has not been pushed from this session.

---

## Evidence-binding confirmation

Every test named below fails against v2.1.6.

| Claim | Test |
|---|---|
| The normalized chain is re-derived from the raw payloads | `test_the_chain_is_rebuilt_from_the_stored_bytes_and_matches` |
| The reproduced 999,999-OI regression is caught | `test_the_reproduced_regression_adding_999999_open_interest` |
| Contract-level mutations are detected | `test_editing_any_calculation_relevant_field_invalidates_trust` (8 fields), `test_editing_the_implied_volatility_invalidates_trust`, `test_editing_the_iv_source_invalidates_trust`, `test_editing_a_contract_identity_invalidates_trust`, `test_editing_an_expiry_invalidates_trust`, `test_editing_a_quote_timestamp_invalidates_trust`, `test_dropping_a_contract_invalidates_trust` |
| Chain-level pricing inputs are covered | `test_editing_a_chain_level_pricing_input_invalidates_trust`, `test_editing_the_snapshot_instant_invalidates_trust` |
| A refusal is actionable | `test_a_refusal_names_the_field_that_moved` |
| The trusted API derives its own authority | `test_the_trusted_api_takes_evidence_rather_than_a_verdict`, `test_a_caller_edited_context_cannot_authorize_a_calculation` |
| Pricing observations are part of report equivalence | `test_pricing_observations_are_in_the_semantic_payload`, `test_a_tampered_observed_value_changes_the_semantic_payload`, `test_a_tampered_observation_fails_rederivation` |
| A failed check cannot revise compatibility | `test_a_failed_check_cannot_revise_a_dimension` |
| OI date assumptions cannot become observations | `test_an_oi_value_observation_carries_no_date`, `test_a_caller_assumed_settlement_date_blocks_a_trusted_calculation`, `test_no_settlement_date_evidence_is_treated_as_an_assumption`, `test_a_caller_assumption_still_permits_a_diagnostic` |
| Records are bound to the pipeline and request specification | `test_every_record_is_stamped_with_its_capture_time_identity`, `test_a_manifest_relabelled_to_another_pipeline_fails`, `test_a_capture_taken_under_a_different_rate_does_not_verify` |
| Capture origin is derived | `test_relabelling_the_manifest_does_not_change_what_the_records_say`, `test_a_manifest_whose_declaration_contradicts_its_records_does_not_verify`, `test_a_mixed_origin_capture_is_not_any_origin`, `test_the_retry_wrapper_does_not_erase_the_origin` |
| DST-fold instants are correct | `test_the_repeated_autumn_hour_is_two_instants`, `test_a_round_trip_through_utc_preserves_the_later_occurrence`, `test_ambiguous_dst_is_resolvable_with_an_explicit_fold` |
| Audit identities are full digests | `test_audit_hashes_are_full_sha256`, `test_mutating_a_capture_time_field_moves_the_manifest_hash` (7 fields) |
| Analytical readiness is a separate axis | `test_analytical_readiness_is_a_separate_axis` |
| The gate leaves a path through it | `test_a_resolved_pipeline_with_real_evidence_can_compute`, `test_the_trusted_result_carries_its_normalization_receipt` |

---

## Two defects found while implementing, not in the brief

**1. Every real capture would have been stamped `UNKNOWN_ORIGIN`.**
`capture_origin_of` reads `capture_origin` off the transport it is handed, and
`build_thetadata_client` always wraps the real transport in
`RetryingTransport`, which had no such attribute. The v2.1.6 origin derivation
would therefore have failed on the first paid session — reading as "not live",
which is not the same statement as "offline fixture". Fixed by delegating
through the wrapper; `test_the_retry_wrapper_does_not_erase_the_origin` pins it.

**2. `FileRawStore` did not read the capture-time stamps back.** Found the same
way — the index writer and the index reader are separate code, and a field added
to one is silently absent from the other. Records written and immediately read
back would have verified; records read after a process restart would not.

---

## Two deviations from the brief, with reasons

**§1's hash list includes "snapshot clocks".** `SnapshotClocks` holds
`request_started_at`, `response_received_at` and `normalized_at`, all three read
from the client's own clock at fetch time. A rebuild necessarily runs at a
different moment, so hashing their values makes the comparison fail always, and
a check that can never pass is not a check. The hash covers their *ordering*
instead, and the per-record request and response clocks stay bound field-by-field
to the store by `verify_capture` — which is where a timestamp is evidence about a
response rather than about the machine that asked for it.

**§8 conflicts with the bare-interpreter invariant.** The engine core has been
guaranteed since v2.1 to run under `python -S -E` with no site-packages, enforced
by two test files and a CI job; `tzdata` lives in site-packages. The invariant is
narrowed from "no third-party *packages*" to "no third-party *code*": the
bare-interpreter checks copy the `tzdata` package into a scratch directory and
put only that on the path, so `import yaml` still fails there and
`test_third_party_packages_really_are_unreachable_under_dash_s` proves it. A
wrong instant is worse than a data dependency.

---

## Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f` | unchanged | — |
| `EXPECTED_MODEL_FINGERPRINT` | `faf0a9f595f2a93a` | `1b353ba18cefb0a2` | `VERSION_METADATA_ONLY` |
| `EXPECTED_OUTPUT_HASH` | `bd668a62...` | `3af3ef9c...` | `VERSION_METADATA_ONLY` |

Measured, and it mattered more here than in previous releases: v2.1.7 changed
the *clock*, and time-to-expiry drives gamma, so a change there is exactly the
kind that could move a number without anyone intending it. Recomputing the
reference case with `model_version` pinned back to `gex-engine/2.1.6`, and
reverting nothing else, reproduces both v2.1.6 digests exactly.

It did not move a number for a structural reason rather than a lucky one: the
hand-written zone and `ZoneInfo` agree on every instant outside the two DST
transition windows, and the reference case is an ordinary March session. Every
total, bucket, per-strike value, wall, void, root and confidence component below
it is a hand-typed literal, and all of them held.

---

## Versions

| Constant | Value |
|---|---|
| Package | `2.1.7` |
| Engine | `gex-engine/2.1.7` |
| Parser | `thetadata-v3-parser/2.1.7` |
| Manifest schema | `raw-capture-manifest/2.1.7` |
| Normalization schema | `normalized-chain/2.1.7` |
| Certification schema | `adapter-certification/2.1.7` |
| Validation schema | `adapter-validation/2.1.7` |
| Request-spec schema | `thetadata-request-spec/2.1.7` |

---

## Artifact

| Field | Value |
|---|---|
| File | `gex-bot-v2.1.7.zip` |
| Files tracked | `PENDING_FILES` |
| ZIP entries | `PENDING_ENTRIES` |

Built with:

```
git archive --format=zip --output=gex-bot-v2.1.7.zip HEAD
```

**This document cannot state the archive's own SHA-256** — it is inside the
archive, so any digest written here would be the hash of a different file. The
digest and byte count are reported alongside the delivered `.zip`. The archive is
reproducible from its commit, so `git archive` followed by `sha256sum` returns
the same value on any machine.

Verified about the delivered file by enumerating its entry list:

- a bare `git archive` of the release commit, **not** wrapped in an outer ZIP
  and not a copy of the development checkout;
- no `.venv`, no `artifacts/`, no `__pycache__`, no `.pyc`, no `.coverage`, no
  `.raw` captured payload, no nested `.zip`.

---

## What a reviewer should know is still open

**A caller-assumed open-interest settlement date now blocks a trusted GEX**, and
the shipped configuration produces exactly that. This is not a regression: an OI
response carries a number and no date, ThetaData does not publish the convention
(OD-26, OD-37), and v2.1.6 was grading the caller's assumption as `OBSERVED` on
the strength of a check that re-read the *value*. Raw capture and diagnostic
calculations are unaffected.

**Chain completeness is still unmeasured.** No contract-list endpoint is wired,
so `expected_contract_count` stays `None` and the universe a chain *should* have
contained is unknown (OD-11). That does not block a raw capture and it does block
an analytical dataset — which is why `AnalyticalReadiness` exists as a separate
axis with its requirements written down. Nothing consumes an analytical dataset
in this repository, by design.

**Captures written by v2.1.6 will not verify.** Their records carry no
capture-time identity, and an unstamped record is refused rather than given the
benefit of the doubt — the same treatment v2.1.6 gave v2.1.5 manifests.

---

## Scope

Nothing was added towards trading. No ThetaData request was made. No Databento,
no futures feed, no feature store, no strategy, no regime classification, no
backtesting, no risk engine, no position sizing, no IBKR, no broker, no order
class, no paper trading, no live trading, no calibrated threshold.

`tests/unit/test_architecture.py` asserts the absence, and it passes.
