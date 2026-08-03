# v2.1.10 completion report

**Status: `READY_FOR_RAW_CAPTURE_ONLY`** · `NOT_READY_FOR_ANALYTICAL_DATASET` ·
`NOT_VALIDATED_WITH_LIVE_THETADATA`

Version: v2.1.10 · Code commit: `214c3b4c7691aaa5517da13ffcf34efbfa4be66c` ·
Base: v2.1.9 `ab5d4f890651454e6be9891b953f0756b11bfc67`

Every verification result below was produced at `214c3b4`. The release archive is
cut from the tip of `master`, which is `214c3b4` plus the commit that adds this
document — no source, test or configuration file differs between the two.

---

## The defect this release corrects

v2.1.9 made an expected universe re-derive its contract identities from the
stored records it named. That check is real and it survives. It answers a
different question from the one completeness depends on.

**Proving a set of identities occurs in stored records is not proving those
records enumerate the complete universe the request should have returned.** A
truncated response enumerates its own rows perfectly. Nothing in it says how many
rows were owed.

So under v2.1.9 an `/v3/option/snapshot/quote` response — a market-data
snapshot — could be labelled `VENDOR_CONTRACT_LIST`, re-derive its identities
correctly, and establish `MEASURED_COMPLETE` for an entire chain. One page of a
paged response could do the same. A settlement-convention document plus an
arbitrary option identity could do the same. In every case the evidence proved
*"these contracts arrived"* and the system read it as *"these are all the
contracts there are."*

---

## What changed

### §1 — Coverage is a state, and the caller does not get to pick it

`UniverseCoverageStatus` replaces `complete_for_request: bool`:

| Status | Establishes | Can find a hole | Verified |
|---|---|---|---|
| `FULL_REQUEST_ENUMERATED` | completeness | yes | yes |
| `PARTIAL_PAGE` | nothing | yes | yes |
| `OBSERVED_SUBSET` | nothing | no | yes |
| `UNKNOWN_COVERAGE` | nothing | no | no |

A caller may still record what it expected, in `declared_coverage`. No
completeness decision reads that field, and `ExpectedContractUniverse` no longer
has a `complete_for_request` argument at all — it was hashed into the universe,
which made an assertion look like a finding.

### §2 — A declaration and a finding are different types

`ExpectedContractUniverse` is what somebody believes. It carries
`source_record_ids`, a `scope`, a `documentation_evidence_id`, a
`declared_coverage` and a `declaration_hash`, and it establishes nothing.

`VerifiedExpectedUniverseArtifact` is what `resolve_expected_universe`
established. It carries the derived identities, the resolved coverage, the
evidence fingerprint, the resolver version and an `observed_at` **derived from
the source records** (`max(response_received_at)`), not supplied by the caller.

`capture_session` takes one or the other, never both:

```python
capture_session(..., verified_expected_universe=artifact)   # can measure
capture_session(..., declared_expected_universe=universe)   # diagnostic only
```

The artifact refuses at construction to carry a coverage its own source kind
cannot reach:

```python
if coverage.establishes_completeness and not (
    kind.best_possible_coverage.establishes_completeness
):
    raise UniverseArtifactError(...)
```

No amount of downstream checking promotes a snapshot into a listing.

### §3 — Snapshots are not contract lists

`ResponseCapabilities` separates four questions v2.1.9 asked as one:

| Question | Field | True for any ThetaData snapshot? |
|---|---|---|
| Does it list its own rows? | `enumerates_rows` | **yes** |
| Does it enumerate the *request's* universe? | `enumerates_request_universe` | no |
| Does it carry page / total / continuation metadata? | `carries_pagination_metadata` | no |
| Is it a dedicated contract-list endpoint? | `is_dedicated_contract_list` | no |

`DEDICATED_CONTRACT_LIST_ENDPOINTS` is empty. An unknown endpoint gets the empty
capability rather than a permissive default, and an index endpoint supplies no
contract identities whatsoever.

### §4 — Pagination evidence is read from the bytes, or it is unsupported

`PaginationCoverageEvidence` is built by `read_pagination_metadata(payloads)`,
which looks for page index, page count, total count and continuation token in the
stored responses. Partial metadata is refused; absent metadata returns `None`
rather than a permissive default; a caller-supplied page total is not evidence.

Because no current ThetaData response carries any of it, the
`CAPTURED_PAGINATION_METADATA` source kind resolves to a refusal naming what is
missing. v2.1.9's resolver for this kind read no pagination metadata at all — it
re-derived identities and trusted a Boolean.

### §5 — Universe documentation is a separate registry

`UniverseDocumentationRule` lives in its own registry from the settlement
`DocumentationRule`. Both are content-verified against a SHA-256 of the
referenced file; they are verified to say *different things*. A rule must supply
either explicit identities or a derivation, so an OI settlement document plus an
arbitrary option identity establishes nothing. The production registry is empty.

### §6 — The source has to be about this request

`UniverseRequestScope(root, expirations, max_dte, strike_range, rights,
request_filters, requested_at)` is compared before the chain operation opens:

- a **wider** listing serves a narrower chain; a narrower one cannot serve a
  wider chain, and an unbounded request means everything;
- the universe must be observed **before** the chain — one observed after it
  describes a different market;
- it must not be stale (`DEFAULT_MAX_UNIVERSE_AGE`, two days);
- it must have been resolved by this resolver version.

### §7 — `complete_for_request` is derived

The five source kinds resolve independently:

| Source kind | Resolves to |
|---|---|
| `VENDOR_CONTRACT_LIST` | refusal — no verified listing endpoint exists |
| `CAPTURED_PAGINATION_METADATA` | refusal — no verified response carries metadata |
| `AUTHORITATIVE_DOCUMENTATION` | `FULL_REQUEST_ENUMERATED`, registry empty |
| `OBSERVED_SNAPSHOT_ROWS` | `OBSERVED_SUBSET` |
| `CALLER_DECLARED` | `UNKNOWN_COVERAGE` |

### §8 — Independence is typed, not spelled

`ChainCompleteness` carries `universe_artifact_hash`,
`universe_evidence_fingerprint`, `coverage_status` and `resolver_version`, and
`independently_observed` reads those. The `NON_INDEPENDENT_SOURCES` string set is
gone: independence was previously decided by checking `expected_source` against a
list of known-bad labels, so inventing a new label bought independence.

### §9–§10 — Verified before the operation opens, and recovered afterwards

The universe is resolved, scope-checked and time-checked *before* the chain
operation is created, so the operation is never stamped with an unresolved claim.
`recover_capture_artifacts()` rebuilds the artifact from the store, re-checks its
hash, re-resolves it and compares identities and coverage — returning the
re-derived artifact rather than the caller's object.

### §11 — One market-session date

`market_session_date(as_of)` in `src/gex/sessions.py`, via
`ZoneInfo("America/New_York")`. 2026-03-18T01:00Z is the 18th in UTC and the
**17th** in New York, where the options market was open; a settlement rule
applied to the wrong one derives the wrong prior session. An AST-based test
asserts that no `as_of.date()`, `requested_at.date()` or `observed_at.date()`
call survives anywhere in `src/`.

### §12 — Raw capture does not require a universe it cannot have

A session with no universe still captures, stores, verifies and replays.
`analytical_readiness_of` requires `FULL_REQUEST_ENUMERATED` **and**
`independently_observed`. Since no verified source reaches that state today, the
shipped profile is `READY_FOR_RAW_CAPTURE_ONLY`.

---

## §13 — Architecture checks

| Check | Asserts |
|---|---|
| `test_the_gex_engine_takes_a_verified_universe_artifact` | the engine's parameter is the artifact type |
| `test_no_snapshot_endpoint_is_a_dedicated_contract_list` | `DEDICATED_CONTRACT_LIST_ENDPOINTS` is empty |
| `test_completeness_independence_is_not_inferred_from_a_string` | no label set decides independence |
| `test_complete_for_request_is_not_a_caller_argument` | it is absent from every constructor |
| `test_settlement_documentation_cannot_be_universe_documentation` | two registries, no shared entries |
| `test_exactly_one_verified_universe_artifact_type_exists` | one definition, in `src/domain/` |
| `test_a_chain_capture_cannot_take_an_unresolved_universe_as_evidence` | the capture path refuses a declaration |

The engine core (`src/gex`, `src/domain`, `src/synthetic`) remains stdlib-only
and imports nothing from `src/adapters` — which is why
`VerifiedExpectedUniverseArtifact` lives in `src/domain/universe_artifact.py`.

---

## §14 — Schema versions

Every version constant in the repository now reads `2.1.10`. The two introduced
by this release are the last two rows:

| Version constant | Value | Module |
|---|---|---|
| package version | `2.1.10` | `pyproject.toml` |
| `PARSER_VERSION` | `thetadata-v3-parser/2.1.10` | `src/adapters/raw_store.py` |
| `MODEL_VERSION` | `gex-engine/2.1.10` | `src/domain/model_spec.py` |
| `MANIFEST_SCHEMA_VERSION` | `raw-capture-manifest/2.1.10` | `src/adapters/raw_store.py` |
| `CERTIFICATION_SCHEMA_VERSION` | `adapter-certification/2.1.10` | `src/adapters/certification.py` |
| `VALIDATION_SCHEMA_VERSION` | `adapter-validation/2.1.10` | `src/adapters/validation.py` |
| `NORMALIZATION_SCHEMA_VERSION` | `normalized-chain/2.1.10` | `src/domain/normalization.py` |
| `REQUEST_SPEC_SCHEMA_VERSION` | `thetadata-request-spec/2.1.10` | `src/adapters/thetadata/request_spec.py` |
| `CAPTURE_OPERATION_SCHEMA_VERSION` | `capture-operation/2.1.10` | `src/adapters/capture_operation.py` |
| `EXPECTED_UNIVERSE_SCHEMA_VERSION` | `expected-universe/2.1.10` | `src/domain/expected_universe.py` |
| `SETTLEMENT_EVIDENCE_SCHEMA_VERSION` | `settlement-evidence/2.1.10` | `src/domain/settlement.py` |
| `ARTIFACT_SCHEMA_VERSION` | `capture-artifact/2.1.10` | `src/adapters/artifact_store.py` |
| **`UNIVERSE_RESOLVER_SCHEMA_VERSION`** | **`universe-resolver/2.1.10`** | `src/domain/universe_artifact.py` |
| **`UNIVERSE_SCOPE_SCHEMA_VERSION`** | **`universe-scope/2.1.10`** | `src/domain/universe_scope.py` |

`docs/VALIDATION.md` documents all of them, and a test enforces that every one
appears there.

---

## §15 — Regression tests that fail against v2.1.9

Each of the following fails on the v2.1.9 tree and passes here:

1. `test_a_quote_snapshot_labelled_a_contract_list_is_refused`
2. `test_no_snapshot_can_act_as_a_vendor_contract_list` (parametrised per endpoint)
3. `test_an_artifact_cannot_claim_more_than_its_source_supports`
4. `test_an_index_print_enumerates_nothing`
5. `test_an_ordinary_quote_response_cannot_satisfy_pagination_evidence`
6. `test_a_caller_supplied_page_total_is_not_evidence`
7. `test_a_settlement_rule_cannot_establish_a_universe`
8. `test_a_document_that_derives_other_identities_is_refused`
9. `test_observed_at_is_derived_from_the_source_records`
10. `test_an_incompatible_source_scope_is_refused` (parametrised)
11. `test_a_universe_observed_after_the_chain_is_refused`
12. `test_a_stale_universe_source_is_refused`
13. `test_a_source_label_alone_cannot_make_completeness_independent`
14. `test_only_full_request_enumerated_reports_measured_complete`
15. `test_the_engine_refuses_an_unverified_declaration`
16. `test_a_verified_artifact_is_required_before_chain_capture`
17. `test_recovery_returns_the_verified_artifact`
18. `test_analytical_readiness_requires_full_request_enumeration` (parametrised)
19. `test_the_boundary_is_midnight_eastern` (parametrised)
20. `test_the_helper_is_the_one_place_a_session_date_is_produced`

Plus the seven §13 architecture checks listed above.

---

## §16 — Verification

All commands below were **locally executed** on this machine, at commit
`214c3b4c7691aaa5517da13ffcf34efbfa4be66c`, with `git status --porcelain` empty.

| Command | Result | Where |
|---|---|---|
| `pytest` | **2238 passed** | locally executed, Python 3.12.10 |
| `pytest -m integration` | 18 passed | locally executed, Python 3.12.10 |
| `pytest -m regression` | 46 passed | locally executed, Python 3.12.10 |
| `pytest -m replay` | 10 passed | locally executed, Python 3.12.10 |
| `ruff check .` | All checks passed | locally executed |
| `ruff format --check .` | 141 files already formatted | locally executed |
| `mypy src` | no issues in 72 source files | locally executed |
| `coverage report --fail-under=90` | **90%**, exit 0 | locally executed |
| `python -m src.app` | exit 0, full snapshot printed | locally executed |

### Python versions

| Version | Status |
|---|---|
| **3.12.10** | **locally executed** — every command above |
| **3.13** | **unverified** — not installed on this machine (`py -0p` lists 3.12 and 3.11 only) |

The CI matrix in `.github/workflows/ci.yml` covers 3.12 and 3.13 across the
`quality`, `invariants` and `no-trading-guarantee` jobs, and `workflow_dispatch`
is enabled so the evidence can be produced on demand. **No CI run has been
observed for this commit**, so 3.13 is reported as unverified rather than as
`executed in CI`.

---

## Frozen reference values

All three were **measured**, not asserted:

| Value | v2.1.10 | Classification |
|---|---|---|
| `EXPECTED_OUTPUT_HASH` | `0e536883c9927f65032877c94c1c59998c0f94fb4fb3885fa7fb14777e38e307` | `REPRESENTATIONAL` — the four new completeness fields serialise into the receipt; stripping them reproduces v2.1.9's `d0be7199…` exactly |
| `EXPECTED_MODEL_FINGERPRINT` | `32b4694cef709838678b5973a9ce8cfcb8ffff90906ebe2d6aef9fdb76ccc0fa` | `VERSION_METADATA_ONLY` — pinning `ENGINE_VERSION` back to `2.1.9` reproduces `6accfab6…` exactly |
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f7986dd9b7b65f2b582d216736da7c795c030554ac6b763b9` | **unchanged** from v2.1.9 |

No `BEHAVIORAL` change. The reference GEX numbers are identical to v2.1.9:

```
total_unsigned_gex    59228408806.90227
total_signed_gex     -24836100698.992706
confidence            93.857
contract_count        250
total_open_interest   1263165
primary zero gamma    5039.133782540731
```

---

## §17 — Release archive

```
git archive --format=zip --output=gex-bot-v2.1.10.zip HEAD
```

Produced from a clean tree at `214c3b4c7691aaa5517da13ffcf34efbfa4be66c`. The
archive goes out **as-is**: not re-zipped, not wrapped in a directory, not
placed inside another archive. A wrapper is a different file with a different
digest, so the SHA-256 published with the release would describe something nobody
downloaded, and a recipient checking the hash could not distinguish an innocent
re-wrap from a substituted artefact.

---

## What this release does **not** claim

- **Not `ADAPTER_CERTIFIED`.** Eight load-bearing vendor conventions remain
  unknown, and nothing here has met a live ThetaData response.
- **Not `READY_FOR_ANALYTICAL_DATASET`.** No verified source reaches
  `FULL_REQUEST_ENUMERATED`, which is now a check rather than a sentence in a
  document.
- **Not validated against live vendor data.** Every integration row in the README
  is still `NOT_VALIDATED_WITH_LIVE_THETADATA`.
- **Not able to trade.** No broker adapter, no order type, no position sizing, no
  execution path; `tests/unit/test_architecture.py` fails the build if one
  appears.

## What would move it forward

One paid ThetaData session. Call whatever contract-list endpoint the
subscription actually exposes, record its response shape, mark that endpoint
`is_dedicated_contract_list=True` in `RESPONSE_CAPABILITIES`, capture it, resolve
it, and pass the artifact to `capture_session(verified_expected_universe=...)`.
Completeness becomes measurable at that point and not before — which is what
OD-11 has recorded since v2.0, now enforced rather than described.
