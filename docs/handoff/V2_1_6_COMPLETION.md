# v2.1.6 completion report

```text
READY_FOR_RAW_CAPTURE_ONLY
```

Not `ADAPTER_CERTIFIED`, and not reachable: eight load-bearing vendor pricing
conventions remain `UNKNOWN`, six of them are not in a snapshot response at all,
and every capture in this repository is stamped `OFFLINE_FIXTURE`. The
repository remains incapable of placing an order.

---

## What this release is about

v2.1.5 made evidence *derived*. v2.1.6 makes the **authorization independent of
the thing being authorized**.

`compute_trusted_gex(chain)` decided trust by reading `chain.meta` — the pipeline
fingerprint, the raw-capture manifest, the spot provenance. All three are written
into the snapshot by the code that produced it, and `ChainSnapshot` is a public
frozen dataclass with an open `meta` dict. So:

```python
dataclasses.replace(
    build_synthetic_chain(),
    meta={
        "pipeline": {"pipeline_fingerprint": pipeline.fingerprint(), ...},
        "raw_capture_manifest": real_manifest.as_dict(),
        "spot_provenance": {"source": "vendor_index_snapshot", ...},
    },
)
```

satisfied every gate, on a chain that had never been near a capture. A snapshot
cannot be a witness to its own provenance.

---

## Git

| Field | Value |
|---|---|
| Branch | `master` |
| Commit | `PENDING — filled in by the release commit` |
| Commit message | `v2.1.6: bind trusted calculation to independently verified evidence` |
| Clean status | `git status --porcelain` empty after the commit |
| Diff stat | 33 files changed (29 modified, 4 added) |

Added: `src/domain/vendor_time.py`, `tests/unit/test_evidence_binding.py`,
`tests/unit/test_post_capture_compatibility.py`,
`tests/unit/test_vendor_timestamps.py`.

---

## Verification

| Check | Result | How |
|---|---|---|
| Python 3.12 full suite | **PASS** — 1 skipped | locally executed |
| Python 3.13 full suite | **unverified** | 3.13 is not installed on this machine (`py -0p` lists 3.12 and 3.11 only) and no CI run exists for this commit |
| `pytest -m integration` | **PASS** (18) | locally executed |
| `pytest -m regression` | **PASS** (46) | locally executed |
| `pytest -m replay` | **PASS** (10) | locally executed |
| `ruff check .` | **PASS** | locally executed |
| `ruff format --check .` | **PASS** (121 files) | locally executed |
| `mypy src` | **PASS** (59 source files) | locally executed |
| `coverage report --fail-under=90` | **PASS** — 92% of 6,699 statements | locally executed |
| Demo (`python -m src.app`) | **PASS** | locally executed |
| Demo output hash | `bd668a626632abadd4aa0dec4ee9b19689ed3b16ebb43db6ea7862de2de58586` | locally executed |

**Python 3.13 is unverified.** The CI matrix names `["3.12", "3.13"]` across the
`quality`, `invariants` and `no-trading-guarantee` jobs, and tests were written
with 3.13 in mind, but no 3.13 result exists. Neither of those is a result. Do
not read the green 3.12 column as covering both.

---

## Trust-boundary confirmation

Each row names a test that fails against v2.1.5.

| Claim | Test |
|---|---|
| Chain metadata cannot verify itself | `test_a_forged_chain_cannot_authorize_itself` |
| Trusted GEX requires independently verified evidence | `test_trusted_gex_requires_a_verified_calculation_context`, `test_the_context_cannot_be_hand_built`, `test_the_builder_refuses_a_precomputed_verification` |
| The context must be for this pipeline and this capture | `test_a_context_from_another_pipeline_is_refused`, `test_a_context_for_another_capture_is_refused` |
| The gate leaves a path through it | `test_a_resolved_pipeline_with_real_evidence_can_compute` |
| Manifest fields are bound to raw records | `test_an_incorrect_request_parameter_hash_does_not_verify`, `test_a_payload_hash_bound_to_the_wrong_record_does_not_verify`, `test_a_wrong_session_id_does_not_verify`, `test_a_duplicate_record_id_does_not_verify` |
| An empty fingerprint is unverifiable, not exempt | `test_an_empty_pipeline_fingerprint_does_not_verify`, `test_an_empty_capture_plan_fingerprint_does_not_verify` |
| The manifest hash binds per-record semantics | `test_mutating_any_audit_relevant_field_moves_the_manifest_hash` (9 cases), `test_swapping_two_payload_hashes_changes_the_manifest_hash` |
| An old manifest schema is refused, not reinterpreted | `test_an_old_schema_manifest_is_refused_rather_than_reinterpreted` |
| Volatile storage cannot be capture-ready | `test_an_in_memory_store_cannot_be_capture_ready`, `test_a_durable_store_can_be_capture_ready`, `test_readiness_requires_free_space` |
| The probe leaves nothing behind | `test_the_probe_does_not_enter_the_capture_index`, `test_an_unwritable_store_is_not_usable` |
| Post-capture observations alter effective compatibility | `test_a_validated_observation_settles_its_dimension`, `test_a_live_mismatch_overrides_a_documented_match`, `test_the_trusted_calculation_reads_the_post_capture_report` |
| A capture cannot launder a documented mismatch | `test_act_360_against_act_365f_stays_mismatched_after_a_capture` |
| One contract cannot characterise a chain | `test_every_row_is_inspected_not_only_the_first`, `test_one_mismatching_row_blocks_chain_level_agreement`, `test_a_mixed_result_does_not_settle_the_dimension` |
| Validator and adapter share timestamp semantics | `test_the_validator_and_the_adapter_agree_on_the_same_string`, `test_the_spot_clock_uses_the_same_parser` |
| An offline fixture is never live | `test_an_offline_fixture_capture_is_labelled_as_one`, `test_an_offline_fixture_never_reads_as_a_live_capture`, `test_an_offline_fixture_cannot_reach_certified` |
| Provenance fails with typed adapter errors | `test_a_datetime_as_an_open_interest_date_is_refused`, `test_a_non_datetime_spot_timestamp_is_refused`, `test_no_public_provenance_path_leaks_an_untyped_error` |
| An invalid `observed_at` fails configuration | `test_an_invalid_observed_at_fails_configuration`, `test_a_valid_observed_at_is_stored_as_a_date` |
| A non-2xx record is not a successful capture | `test_a_non_2xx_record_cannot_verify_as_a_successful_capture` |

---

## Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f` | unchanged | — |
| `EXPECTED_MODEL_FINGERPRINT` | `d3d458592b6f87e0` | `faf0a9f595f2a93a` | `VERSION_METADATA_ONLY` |
| `EXPECTED_OUTPUT_HASH` | `568d2c2d…` | `bd668a62…` | `VERSION_METADATA_ONLY` |

**Measured, not argued.** Recomputing the reference case with `model_version`
pinned back to `gex-engine/2.1.5` — and *nothing else* reverted — reproduces both
v2.1.5 digests exactly. The version string is the whole of both moves, with no
residue to attribute elsewhere. This is a stronger check than the
search-by-elimination used in v2.1.4 and v2.1.5, because it does not depend on
knowing which changes to look for.

No GEX number moved. Every total, bucket, per-strike value, wall, void, root and
confidence component is asserted individually in
`tests/regression/test_frozen_reference_case.py`, and across the whole release
exactly two assertions in that file changed.

---

## Versions

| Constant | Value |
|---|---|
| Package | `2.1.6` |
| Engine | `gex-engine/2.1.6` |
| Parser | `thetadata-v3-parser/2.1.6` |
| Manifest schema | `raw-capture-manifest/2.1.6` |
| Certification schema | `adapter-certification/2.1.6` |
| Validation schema | `adapter-validation/2.1.6` |

---

## Artifact

| Field | Value |
|---|---|
| File | `gex-bot-v2.1.6.zip` |
| SHA-256 | `PENDING — computed from the archive of the release commit` |
| Files tracked | 161 |
| Commit | see Git above |

Produced by `git archive --format=zip --output=gex-bot-v2.1.6.zip HEAD`, which
contains exactly the tracked files at that commit. It is not wrapped in an outer
ZIP and contains no development checkout, no `.venv`, no `artifacts/`, no
`__pycache__` and no captured payloads. The SHA-256 above applies to that exact
file.

---

## Two things a reviewer should know

**1. The chain-to-capture binding is not yet complete.** The context is bound to
the chain by the manifest hash and by `chain.spot` equalling the verified index
print. That catches a chain from a different session. It does not yet prove that
every *quote* came from the captured payloads, so a chain assembled from the
right session's bytes and then altered row by row would still pass. Closing it
needs a per-contract digest carried from assembly into the snapshot. Recorded as
OPEN_DECISIONS §36.

**2. The hand-written Eastern zone is imprecise inside the repeated autumn
hour.** `src/gex/sessions.USEastern` resolves its offset from the wall clock and
deliberately ignores `fold`, because there is no `tzdata` wheel on every machine
this runs on. Rendering an instant *inside* the 01:00–02:00 window on the
fall-back Sunday back into Eastern can therefore be an hour out. The normalised
UTC instant — which is what every calculation uses — is correct, and the
ambiguity resolution is recorded on the parsed value. One hour a year, outside
any US index-option session. Recorded as OPEN_DECISIONS §2.

---

## Scope

Nothing was added towards trading. No ThetaData request was made. No Databento,
no futures feed, no feature store, no strategy, no regime classification, no risk
engine, no position sizing, no IBKR, no broker, no order class, no paper trading,
no live trading, no calibrated trading parameter.

`tests/unit/test_architecture.py` asserts the absence, and it passes.
