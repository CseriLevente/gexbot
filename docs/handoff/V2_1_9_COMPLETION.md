# v2.1.9 completion report

```text
READY_FOR_RAW_CAPTURE_ONLY
```

Not `ADAPTER_CERTIFIED`. Eight load-bearing vendor pricing conventions remain
`UNKNOWN`, every capture in this repository is stamped `OFFLINE_FIXTURE`, and no
ThetaData settlement rule is registered — so the shipped configuration opens
captures with **no settlement artifact**, which since this release makes them
permanently ineligible for a trusted GEX. The repository remains incapable of
placing an order.

---

## What this release is about

v2.1.8 bound every non-payload input to the capture operation. Two of those
inputs were bound to something nobody had checked.

**The settlement date.** v2.1.8 replaced an authorizing enum with resolvers,
which was right, and stopped one step short:

```python
rule = rules.get(evidence.reference)
if not rule.covers(evidence.as_of):
    return failure
return ResolvedSettlementDate(as_of=evidence.as_of, ...)
```

The date still came from the caller. The rule was consulted only to confirm it
was *in force* on the day already chosen, so a single rule saying "prior trading
session" would authorize 2026-03-16, 2026-03-15 and 2026-03-01 alike for a
2026-03-17 chain. `normalized_value: str` is why: free text cannot be applied to
a session date, so the answer had to come from the argument list.

A `DocumentationRule` could also carry any 64-character string as a content
hash, because nothing opened the file. And a capture stamped with an empty
settlement fingerprint still returned a trusted result if the *call* supplied
documentation evidence — the capture said no rule had been established, the
calculation said one had, and the calculation won because it held the argument.

**The expected universe.** `source="vendor_contract_list"` was a string a caller
typed, and `source_record_ids` was read as a boolean. No record was ever opened.
There were **two** `ExpectedContractUniverse` classes and the engine read the one
with no provenance. `complete_for_request` existed and was consulted nowhere, so
page one of a paginated listing reported the whole chain `MEASURED_COMPLETE`.

---

## Git

| Field | Value |
|---|---|
| Branch | `master` |
| Base commit | `8a27906be41e902810dd28d3014d2fa3ee03ad19` |
| Code commit | `4ef9f01` |
| Commit message | `v2.1.9: derive settlement dates and verify expected universes` |
| Archived commit | the documentation commit that follows `4ef9f01`; it changes no code |
| Clean status | `git status --porcelain` empty at the archived commit |
| Diff stat | 43 files changed, 4,264 insertions, 744 deletions |

Added: `src/domain/settlement.py`, `src/adapters/universe_resolvers.py`,
`src/adapters/artifact_store.py`,
`tests/unit/test_expected_universe_evidence.py`,
`tests/unit/test_operation_digest_and_records.py`,
`tests/unit/test_artifact_store.py`.

Removed: the duplicate `ExpectedContractUniverse` in `src/domain/completeness.py`.

---

## Verification

| Check | Result | How |
|---|---|---|
| Python 3.12 full suite | **PASS** — 2,186 tests, 0 skipped | locally executed |
| Python 3.13 full suite | **unverified** | 3.13 is not installed on this machine (`py -0p` lists 3.12 and 3.11) and no CI run exists for this commit |
| `pytest -m integration` | **PASS** (18) | locally executed |
| `pytest -m regression` | **PASS** (46) | locally executed |
| `pytest -m replay` | **PASS** (10) | locally executed |
| `ruff check .` | **PASS** | locally executed |
| `ruff format --check .` | **PASS** (137 files) | locally executed |
| `mypy src` | **PASS** (69 source files) | locally executed |
| `coverage report --fail-under=90` | **PASS** — 91% of 8,127 statements | locally executed |
| Demo (`python -m src.app`) | **PASS** | locally executed |
| Demo output hash | `d0be719931de451dd8ef88a178ec8287bec899b93ed605e8f5be4275eedb1961` | locally executed |
| Post-extraction smoke tests | **PASS** | locally executed from an extraction of the delivered zip |

**Python 3.13 is unverified.** The CI matrix names `["3.12", "3.13"]` across the
`quality`, `invariants` and `no-trading-guarantee` jobs. That is a configuration,
not a result. Nothing here was executed in CI: the workflow runs on push, and
this commit has not been pushed from this session.

---

## Settlement confirmation

| Claim | Test |
|---|---|
| A capture with no settlement rule cannot later become trusted | `test_a_capture_with_no_settlement_rule_cannot_later_become_trusted` |
| The trusted API accepts no settlement evidence | `test_the_trusted_api_accepts_no_settlement_evidence`, `test_the_trusted_api_accepts_no_retroactive_authority` |
| A trusted capture stamps a non-empty rule fingerprint | `test_a_trusted_capture_stamps_a_nonempty_rule_fingerprint` |
| The rule is selected on the session and derives the date | `test_the_capture_session_takes_the_rule_and_derives_the_date` |
| A prior-session rule derives exactly the prior trading session | `test_a_prior_session_rule_derives_exactly_the_prior_session` |
| …over weekends, fixed holidays and Good Friday | `test_the_derivation_walks_the_real_calendar` (6 cases), `test_good_friday_is_not_a_trading_session` |
| The same rule cannot authorize two different dates | `test_the_same_rule_cannot_authorize_two_different_dates` |
| Free text is not a rule | `test_a_rule_with_no_typed_semantics_establishes_nothing`, `test_free_text_is_not_accepted_as_a_rule` |
| A missing document fails registration | `test_a_missing_document_fails_registration`, `test_an_absolute_path_is_refused` |
| A content-hash mismatch fails registration | `test_a_content_hash_mismatch_fails_registration`, `test_a_url_cannot_be_content_verified` |
| An artifact whose rule does not produce its date is refused | `test_an_artifact_whose_rule_does_not_produce_its_date_is_refused` |
| A trusted chain carries the resolved date on every contract | `test_the_trusted_chain_carries_the_resolved_date_on_every_contract`, `test_the_rebuilt_chain_carries_the_captures_settlement_date` |
| A chain carrying a different date, or none, is refused | `test_a_chain_carrying_a_different_date_is_refused`, `test_a_chain_carrying_no_date_cannot_claim_an_established_one` |
| Replay carries the same date | `test_the_rebuilt_chain_carries_the_captures_settlement_date`, `test_the_receipt_records_the_settlement_date_the_capture_derived` |
| Provenance and the capture-bound rule must agree | `test_provenance_and_the_capture_bound_rule_must_agree` |
| The production registry holds no ThetaData rule | `test_the_production_registry_holds_no_thetadata_settlement_rule` |

---

## Universe confirmation

| Claim | Test |
|---|---|
| Exactly one `ExpectedContractUniverse` type exists | `test_only_one_expected_contract_universe_class_exists`, `test_exactly_one_expected_contract_universe_type_exists`, `test_the_completeness_module_no_longer_exports_one` |
| The engine takes the typed one | `test_the_engine_refuses_an_untyped_universe`, `test_the_gex_engine_takes_a_typed_expected_universe` |
| A fake record id fails verification | `test_a_fake_record_id_fails_verification` |
| A universe its records do not produce fails | `test_a_universe_its_records_do_not_produce_fails`, `test_a_universe_missing_a_listed_contract_fails` |
| A non-enumerating endpoint cannot state a universe | `test_an_endpoint_that_enumerates_nothing_cannot_state_a_universe` |
| An unverified universe is not independently observed | `test_an_unverified_universe_is_not_independently_observed` |
| Caller-declared establishes nothing | `test_a_caller_declared_universe_is_not_independently_observed`, `test_a_caller_declared_universe_cannot_measure_completeness`, `test_completeness_cannot_be_measured_from_a_caller_declared_universe` |
| A partial universe cannot report `MEASURED_COMPLETE` | `test_a_partial_universe_cannot_report_measured_complete`, `test_a_partial_universe_still_finds_a_missing_identity`, `test_the_same_identities_complete_for_the_request_do_report_complete` |
| The capture-owned universe reaches assembly automatically | `test_the_capture_owned_universe_reaches_fetch_chain_automatically`, `test_a_partial_universe_reaches_the_chain_through_the_capture` |
| A fetch cannot supply a second universe | `test_a_fetch_cannot_supply_a_second_universe` |
| The trusted API takes no expected universe | `test_the_trusted_api_accepts_no_expected_universe` |
| Trusted replay recovers and re-verifies it | `test_trusted_replay_recovers_and_reverifies_the_capture_universe`, `test_a_universe_its_own_records_do_not_produce_fails_at_replay` |

**Also new**: operation digests recomputed from their fields
(`test_editing_the_requested_instant_fails_with_a_named_code`,
`test_editing_any_digest_covered_field_fails` — 6 fields), field evidence
targeting exact records (`test_observe_field_opens_the_record_it_is_given`,
`test_confirm_field_checks_the_claim_against_the_record_it_names`), and the
content-addressed artifact store (18 tests).

---

## One deviation from the brief, with the reason

**§6 asks that a universe's source record "belongs to this capture operation".
The resolver checks the store, not the operation.**

A contract listing has to be captured *before* the chain it describes — it is
what the expectation is built from — so it necessarily belongs to an earlier
operation. Requiring same-operation membership would make a vendor-sourced
universe impossible to declare rather than hard to forge: the universe hash is
stamped on the records of the operation that *uses* it, and that stamp is fixed
when the session opens, before the listing could be captured into it.

So the resolver reopens the named records from the **store**, which holds every
record of the session, and checks what actually matters: that the records exist,
that their payload hashes still match, that their endpoint enumerates contracts,
that every identity parses, and that the derived set equals the claimed set
exactly. What binds the universe to *this* capture is elsewhere and is stronger —
its hash is on every record of the operation, so a different universe is a
different operation, which `resolve_operation` refuses.

---

## Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f` | unchanged | — |
| `EXPECTED_MODEL_FINGERPRINT` | `79f3abe506978342` | `6accfab618292203` | `VERSION_METADATA_ONLY` |
| `EXPECTED_OUTPUT_HASH` | `128acd06...` | `d0be7199...` | `REPRESENTATIONAL` |

Both measured, separately, and neither assumed.

Pinning `model_version` back to `gex-engine/2.1.8` reproduces
`79f3abe506978342c52b31481f16f7ff61ac6f4824b586d4d7020a37a4e73d83` exactly, so
the model fingerprint moved on the version string.

The output hash covers the serialised snapshot, which includes the
chain-completeness *report*, and v2.1.9 adds one key to it:
`expected_complete_for_request`. Removing that single key from the payload and
re-hashing reproduces `128acd06a9a00e12d7e19ff60eef55c3635bd7a9920b6a18ac8aa1db3dcb1e04`
exactly, so nothing else moved. Every numeric literal in the reference case is a
hand-typed constant and all of them still hold: 59,228,408,806.90227 unsigned,
−24,836,100,698.992706 signed, 93.857 confidence, 250 contracts, 1,263,165 open
interest, 5039.1337825 primary zero-gamma root.

The key exists because a partial expected universe must not report the whole
chain complete, and a report that could not say which kind of expectation it
measured against could not distinguish the two.

---

## Versions

| Constant | Value |
|---|---|
| Package | `2.1.9` |
| Engine | `gex-engine/2.1.9` |
| Parser | `thetadata-v3-parser/2.1.9` |
| Manifest schema | `raw-capture-manifest/2.1.9` |
| Capture-operation schema | `capture-operation/2.1.9` |
| Normalization schema | `normalized-chain/2.1.9` |
| Settlement-evidence schema | `settlement-evidence/2.1.9` |
| Expected-universe schema | `expected-universe/2.1.9` |
| Certification schema | `adapter-certification/2.1.9` |
| Validation schema | `adapter-validation/2.1.9` |
| Request-spec schema | `thetadata-request-spec/2.1.9` |
| Capture-artifact envelope | `capture-artifact/2.1.9` |

---

## Artifact

| Field | Value |
|---|---|
| File | `gex-bot-v2.1.9.zip` |
| Files tracked | 185 |
| ZIP entries | 226 (185 files plus 41 directory entries) |

Built with:

```
git archive --format=zip --output=gex-bot-v2.1.9.zip HEAD
```

**This document cannot state the archive's own SHA-256** — it is inside the
archive, so any digest written here would be the hash of a different file. The
digest and byte count are reported alongside the delivered `.zip`, and that
digest describes the uploaded file itself: the `git archive` output goes out
as-is, not wrapped in an outer ZIP and not a copy of the development checkout.

Verified by enumerating the entry list of the delivered file: no `.venv`, no
`artifacts/`, no `__pycache__`, no `.pyc`, no `.coverage`, no captured `.raw`
payload, no nested `.zip`.

---

## What a reviewer should know is still open

**The shipped configuration opens captures with no settlement rule**, and since
v2.1.9 that is permanent for those captures: `compute_trusted_gex` takes no
settlement argument, so nothing can supply one afterwards. This is the honest
consequence of OD-26 — this repository has read no ThetaData document
establishing an open-interest settlement convention, and no snapshot endpoint
carries a settlement-date field. Raw capture, diagnostic calculation and
vendor-schema research are unaffected.

**No contract-list endpoint is wired** (OD-11), so no production universe can be
built from vendor bytes. The type, the resolver and the binding all exist and
are exercised against a stand-in listing; the day a real endpoint is available it
is a `capture_session` argument rather than a new design question.

**The spot skew tolerance is still an uncalibrated guess** (OD-25), unchanged
from v2.1.8.

**Captures written by v2.1.8 will not verify.** They carry no
`spot_synchronization_policy_fingerprint`, so their operation digest cannot be
recomputed — and a digest that cannot be recomputed is a digest nobody has
checked. They are refused rather than exempted, the same treatment every prior
release gave its predecessor.

---

## Scope

Nothing was added towards trading. No ThetaData request was made. No Databento,
no futures feed, no feature store, no strategy, no regime classification, no
backtesting, no risk engine, no position sizing, no IBKR, no broker, no order
class, no paper trading, no live trading, no calibrated threshold.

`tests/unit/test_architecture.py` asserts the absence, and it passes.
