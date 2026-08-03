# v2.1.8 completion report

```text
READY_FOR_RAW_CAPTURE_ONLY
```

Not `ADAPTER_CERTIFIED`. Eight load-bearing vendor pricing conventions remain
`UNKNOWN`, every capture in this repository is stamped `OFFLINE_FIXTURE`, no
ThetaData settlement-date rule is registered, and a caller-assumed open-interest
settlement date blocks a trusted calculation. The repository remains incapable of
placing an order.

---

## What this release is about

v2.1.7 re-derived the chain from its raw bytes and compared the two. That closed
every *payload* mutation. What it did not bind was the inputs that are **not in
the payload**, and the rebuild took the sharpest of them from the thing under
test:

```python
recipe = self.normalization_recipe(as_of=chain.as_of)
rederived = self.rebuild_chain_from_capture(..., recipe=recipe)
```

The chain chose the instant it was checked against, so shifting `chain.as_of`
shifted the rebuild with it and the two agreed. A tenth of a second is a real
change in time-to-expiry on a 0DTE afternoon; an hour is a different market. The
re-derivation was exact about everything except the one input it took from the
object it was supposed to be checking.

The same shape ran through four more inputs, and v2.1.8 closes all five by
binding each to the **capture operation** that produced the chain.

---

## Git

| Field | Value |
|---|---|
| Branch | `master` |
| Base commit | `fa3fd92849a939726290277286834785ffe051de` |
| Code commit | `a1012cd` |
| Commit message | `v2.1.8: bind every non-payload input to the capture operation` |
| Archived commit | the documentation commit that follows `a1012cd`; it changes no code |
| Clean status | `git status --porcelain` empty at the archived commit |
| Diff stat | 45 files changed, 3,788 insertions, 120 deletions |

Added: `src/adapters/capture_operation.py`,
`src/adapters/evidence_resolvers.py`, `src/domain/expected_universe.py`,
`src/domain/digests.py`, `tests/unit/test_capture_operation_binding.py`,
`tests/unit/test_settlement_evidence_resolution.py`.

This document is added by a follow-up commit that changes no code, for the
reason set out under **Artifact**.

---

## Verification

| Check | Result | How |
|---|---|---|
| Python 3.12 full suite | **PASS** — 2,104 tests, 0 skipped | locally executed |
| Python 3.13 full suite | **unverified** | 3.13 is not installed on this machine (`py -0p` lists 3.12 and 3.11) and no CI run exists for this commit |
| `pytest -m integration` | **PASS** (18) | locally executed |
| `pytest -m regression` | **PASS** (46) | locally executed |
| `pytest -m replay` | **PASS** (10) | locally executed |
| `ruff check .` | **PASS** | locally executed |
| `ruff format --check .` | **PASS** (131 files) | locally executed |
| `mypy src` | **PASS** (66 source files) | locally executed |
| `coverage report --fail-under=90` | **PASS** — 92% of 7,595 statements | locally executed |
| Demo (`python -m src.app`) | **PASS** | locally executed |
| Demo output hash | `128acd06a9a00e12d7e19ff60eef55c3635bd7a9920b6a18ac8aa1db3dcb1e04` | locally executed |
| Post-extraction smoke tests | **PASS** | locally executed from an extraction of the delivered zip |

**Python 3.13 is unverified.** The CI matrix names `["3.12", "3.13"]` across the
`quality`, `invariants` and `no-trading-guarantee` jobs. That is a configuration,
not a result. Nothing here was executed in CI: the workflow runs on push, and
this commit has not been pushed from this session.

---

## The five unbound inputs

Each row is a value that decided a number and that a caller could set.

| Input | v2.1.7 | v2.1.8 |
|---|---|---|
| Valuation instant | `chain.as_of` — the object under test | The index print read out of the verified capture |
| Spot timestamp | A field on a caller-built `SpotProvenance` | The verified index record |
| Skew tolerance | Another field on the same object | `ThetaDataConfig.max_spot_skew_seconds`, in the pipeline fingerprint |
| Settlement date | An `EvidenceKind` that authorized itself | A resolver that opens the record, or looks the rule up by id, or requires the derivation artefact |
| Chain completeness | `snapshot.meta["chain_completeness_object"]` | A typed `ChainSnapshot.completeness` field, in the chain hash |
| Expected universe | An argument to the calculation | Declared on `capture_session`, stamped on every record, checked at replay |

`CaptureOperationIdentity` is what holds them together: both timestamps, the rule
that chose one, the spot policy fingerprint, the settlement rule fingerprint, the
expected universe fingerprint, plus the pipeline, plan, request-spec, recipe and
parser identities v2.1.7 already stamped. Hashed whole; the digest goes on every
record.

The provisional/resolved split is deliberate. `begin_operation` stamps the
*requested* instant because no response has arrived yet; `resolve_operation`
reads the *effective* one out of the verified index print afterwards. A value
stamped before the evidence existed would be an assertion, and the entire point
is that the instant is derived.

---

## Regression confirmation

Every test named below fails against v2.1.7.

| Claim | Test |
|---|---|
| A 0.1s, 0.5s, 1s or 1h shift of the chain instant is refused | `test_shifting_the_chain_instant_invalidates_trust` (4 cases) |
| The instant comes from the index print, not the chain | `test_the_valuation_instant_comes_from_the_index_print`, `test_the_rebuild_does_not_take_its_instant_from_the_chain` |
| An operation is an identity and records belong to exactly one | `test_changing_the_requested_instant_changes_the_operation_identity`, `test_changing_the_valuation_instant_changes_the_operation_identity`, `test_two_operations_in_one_session_stay_distinct`, `test_records_from_one_operation_cannot_verify_under_another`, `test_every_record_names_its_operation`, `test_a_capture_with_no_operation_stamp_is_refused` |
| The 11:00-raw / 12:00-claimed spot bypass fails | `test_a_claimed_spot_instant_cannot_replace_the_one_the_vendor_sent`, `test_the_trusted_api_takes_no_spot_provenance` |
| Tolerance is configuration, not an argument | `test_the_synchronisation_tolerance_comes_from_configuration`, `test_widening_the_tolerance_is_a_configuration_change_records_disagree_with` |
| Fake vendor-field OI date evidence fails | `test_fake_vendor_field_evidence_does_not_resolve`, `test_a_fake_vendor_field_date_cannot_authorize_a_trusted_calculation`, `test_vendor_field_evidence_naming_a_real_record_still_needs_the_field` |
| `reference="lol"` cannot authorize an OI date | `test_an_arbitrary_reference_does_not_resolve`, `test_an_arbitrary_reference_cannot_authorize_a_trusted_calculation` |
| Schedule evidence needs its derivation artefact | `test_schedule_evidence_without_a_derivation_does_not_resolve`, `test_a_derivation_that_disagrees_with_the_claim_does_not_resolve`, `test_a_derivation_resting_on_unregistered_evidence_does_not_resolve` |
| OI provenance and OI date evidence must agree | `test_provenance_and_settlement_evidence_must_agree` |
| Documentation content changes the evidence fingerprint | `test_changing_the_document_content_changes_the_evidence_fingerprint`, `test_rewriting_the_referenced_document_moves_the_pipeline_fingerprint` |
| Forged completeness cannot move a trusted confidence | `test_forged_completeness_cannot_alter_a_trusted_confidence` (52.0619 → 57.3394 reproduced), `test_completeness_is_a_typed_field_not_a_metadata_key`, `test_the_completeness_payload_is_not_truncated` |
| GEX code reads no calculation-affecting data from `meta` | `test_no_calculating_module_reads_a_calculation_input_from_meta` and the two beside it in `test_architecture.py` |
| A replay receives the exact captured universe | `test_a_capture_declares_the_universe_it_expects`, `test_a_replay_cannot_substitute_a_different_universe`, `test_a_replay_cannot_drop_the_universe_the_capture_expected`, `test_a_universe_cannot_be_introduced_after_the_capture`, `test_the_trusted_path_refuses_a_substituted_universe` |
| Every assigned record is consumed exactly once | `test_every_assigned_record_is_consumed_exactly_once`, `test_an_extra_unused_record_invalidates_replay`, `test_the_consumption_report_reaches_the_receipt` |
| A second response per endpoint needs a declared reason | `test_no_shipped_plan_declares_multiple_records`, `test_declaring_a_reason_changes_the_plan_fingerprint`, `test_an_undeclared_second_response_is_named_as_such` |
| Every compared identity is a full SHA-256 | `test_every_trust_identity_is_a_full_sha256`, `test_short_id_is_available_and_is_not_what_gets_compared` |
| The four readiness gates stay four questions | `test_the_four_readiness_questions_stay_four_questions` |
| The production documentation registry stays empty | `test_the_production_registry_holds_no_thetadata_settlement_rule` |
| The gate leaves a path through it | `test_a_registered_rule_resolves`, `test_a_complete_derivation_resolves`, `test_a_measured_universe_survives_replay` |

---

## One deviation from the brief, with the reason

**§10 asks the pipeline fingerprint to include "the evidence fingerprints it
relies on". The `DocumentationRuleRegistry` is deliberately not one of them.**

A global registry is mutable at runtime, so folding it into the pipeline
fingerprint would make that fingerprint depend on import order and on whatever a
test had registered — a capture taken before a registration would stop verifying
against the same configuration afterwards, for a reason that has nothing to do
with the vendor.

What the pipeline *relies on* is narrower and static: the vendor-documentation
observations in its own `pricing_attestations`, which resolve load-bearing
pricing dimensions. Those now carry a `document_content_hash` derived at
construction, and `documentation_evidence_fingerprints` puts them in the
fingerprint by name. Settlement-rule evidence is bound where it belongs — to the
capture operation, through `open_interest_date_rule_fingerprint`, which is
per-operation rather than per-configuration.

Both halves are tested: rewriting a cited document moves the pipeline
fingerprint, and substituting settlement evidence is refused at replay.

---

## Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f` | unchanged | — |
| `EXPECTED_MODEL_FINGERPRINT` | `1b353ba18cefb0a2` | `79f3abe506978342` | `VERSION_METADATA_ONLY` |
| `EXPECTED_OUTPUT_HASH` | `3af3ef9c...` | `128acd06...` | `VERSION_METADATA_ONLY` |

Measured in two parts rather than asserted. Pinning `model_version` back to
`gex-engine/2.1.7` and changing nothing else reproduces both v2.1.7 digests
exactly, so the movement is the version string and not the arithmetic. Every
total, bucket, per-strike value, wall, node, void, zero-gamma root and confidence
component in the reference case is a hand-typed literal, and all of them held.

`EXPECTED_CONFIG_FINGERPRINT` is the *engine* config, which this release did not
touch. Three fingerprints did change behaviourally — `pipeline_fingerprint` (the
spot policy and documentation evidence entered it), `capture_plan.fingerprint`
(declared multiple records entered it) and
`normalization_recipe.rules_fingerprint` (the expected universe left it, being a
per-operation input). None of the three is a frozen literal: they are recomputed
from configuration on every run and compared against what a capture was stamped
with, which is what they are for.

---

## Versions

| Constant | Value |
|---|---|
| Package | `2.1.8` |
| Engine | `gex-engine/2.1.8` |
| Parser | `thetadata-v3-parser/2.1.8` |
| Manifest schema | `raw-capture-manifest/2.1.8` |
| Capture-operation schema | `capture-operation/2.1.8` |
| Normalization schema | `normalized-chain/2.1.8` |
| Certification schema | `adapter-certification/2.1.8` |
| Validation schema | `adapter-validation/2.1.8` |
| Expected-universe schema | `expected-universe/2.1.8` |
| Request-spec schema | `thetadata-request-spec/2.1.8` |

---

## Artifact

| Field | Value |
|---|---|
| File | `gex-bot-v2.1.8.zip` |
| Files tracked | 178 |
| ZIP entries | 219 (178 files plus 41 directory entries) |

Built with:

```
git archive --format=zip --output=gex-bot-v2.1.8.zip HEAD
```

**This document cannot state the archive's own SHA-256** — it is inside the
archive, so any digest written here would be the hash of a different file. The
digest and byte count are reported alongside the delivered `.zip`, and that
digest describes the uploaded file itself: the `git archive` output goes out
as-is, not wrapped in an outer ZIP and not a copy of the development checkout.

Verified by enumerating the entry list of the delivered file: no `.venv`, no
`artifacts/`, no `__pycache__`, no `.pyc`, no `.coverage`, no captured `.raw`
payload, no nested `.zip`.

The archive was extracted to a temporary directory and smoke-tested there: core
imports, config load, `python -m src.app` reproducing the frozen output hash,
release integrity, architecture invariants, the integration suite, and both new
v2.1.8 regression files.

---

## What a reviewer should know is still open

**No ThetaData settlement-date rule is registered.** `DOCUMENTATION_RULES` is
empty in production because this repository has read no vendor document that
establishes the convention, and no snapshot endpoint carries a settlement-date
field (OD-26, OD-37). v2.1.8 turns that from prose into a check: the vendor-field
resolver opens the record and finds no such field, and the documentation resolver
finds no registered rule. The shipped configuration therefore cannot produce a
trusted number, which is the honest position rather than a regression. Raw
capture and diagnostic calculations are unaffected.

**Chain completeness is still unmeasured.** No contract-list endpoint is wired,
so no `ExpectedContractUniverse` can be built from vendor bytes (OD-11). The type
and the binding now exist, so the day one is available it is a capture-session
argument rather than a new design question.

**The spot skew tolerance is still an uncalibrated guess** (OD-25). One second is
a policy, not a vendor fact. What changed is that it is a guess made once, in
configuration, visible in every fingerprint — rather than one a caller could
widen for a single calculation.

**Captures written by v2.1.7 will not verify.** Their records carry no operation
stamp, and an unstamped record is refused rather than given a timestamp this
process invented — the same treatment v2.1.7 gave v2.1.6 captures.

---

## Scope

Nothing was added towards trading. No ThetaData request was made. No Databento,
no futures feed, no feature store, no strategy, no regime classification, no
backtesting, no risk engine, no position sizing, no IBKR, no broker, no order
class, no paper trading, no live trading, no calibrated threshold.

`tests/unit/test_architecture.py` asserts the absence, and it passes.
