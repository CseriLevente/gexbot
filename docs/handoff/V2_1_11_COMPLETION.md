# v2.1.11 completion report

**Status: `READY_FOR_RAW_CAPTURE_ONLY`** · `NOT_READY_FOR_ANALYTICAL_DATASET` ·
`NOT_VALIDATED_WITH_LIVE_THETADATA`

Version: v2.1.11 · Code commit: `acafc169ae15a7964dadf3a8c50b1eff791f56f5` ·
Base: v2.1.10 `e02ee603ec2af71c986c8531f49f0691b341987a`

Every verification result below was produced at `acafc16`. The release archive is
cut from the tip of `master`, which is `acafc16` plus the commit that adds this
document — no source, test or configuration file differs between the two.

---

## The defect this release corrects

v2.1.10 made universe coverage a *resolver output* and gave
`VerifiedExpectedUniverseArtifact` a `__post_init__` that refuses a coverage its
source kind could not reach. Those refusals answer **what an artifact may
claim**. They were being read as answering **who may make one**.

`capture_session` took an artifact and checked `isinstance`. The type is a public
frozen dataclass, so:

```python
VerifiedExpectedUniverseArtifact(
    identities=...,
    source_kind=ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION,
    coverage_status=UniverseCoverageStatus.FULL_REQUEST_ENUMERATED,
    documentation_evidence_id="a-document-nobody-registered",
    evidence_fingerprint="f" * 64,
    ...
)
```

constructs cleanly — the source kind *can* reach full coverage, so no refusal
fires — passes the type check, and opens a capture claiming a complete universe
against a document nobody registered.

---

## What changed

### §1 — A resolution, re-run

```python
resolved = pipeline.resolve_expected_universe(
    declaration=declaration,
    source_manifest=source_manifest,
    source_store=source_store,
)

session = pipeline.capture_session(..., universe_resolution=resolved)
```

A `UniverseResolution` carries the *inputs*: the declaration, the source
capture, its verification, and the document extraction where one was involved.
`capture_session` re-verifies the source, re-derives the artifact, and refuses
unless the same `artifact_hash` comes out — naming the first field that
disagreed. A forged resolution has to supply a source capture that genuinely
produces the claimed artifact, at which point it is a resolution.

`verified_expected_universe=` is gone. Passing an artifact raises with a message
naming the problem; passing a declaration still raises the v2.1.10 message.

### §2 — The source must be a capture that passed

| Refused | Because |
|---|---|
| no `verify_capture` result | existing in a store is not having been verified |
| a record outside the verified manifest | it was checked against nothing in particular |
| HTTP 400–599 | an error body parses into whatever rows it happens to contain |
| `capture_complete=false` | truncated by this repository, not by the vendor |
| an unsupported `parser_version` | a payload read under different rules is a different payload |
| an empty operation or request-spec fingerprint | nothing says which request produced it |

A universe source is verified by `verify_universe_source`, which waives exactly
one failure class — `MISSING_ENDPOINT` — because a listing sweep holds no index
print or open interest and is not a chain calculation. The waived failures and
the reason are carried on the receipt and persisted, so a set-aside check leaves
a trace.

`VerifiedExpectedUniverseArtifact` now refuses at construction if a
record-backed source names no `source_verification_fingerprint`.

### §3 — The source pipeline is derived and compared

v2.1.10 called `check_source_compatibility(chain_pipeline_fingerprint=
self.fingerprint(), source_pipeline_fingerprint=self.fingerprint())` — a string
compared with itself. The artifact now carries
`source_pipeline_fingerprint`, read off the verified records.

`PipelineCompatibilityPolicy.IDENTICAL_PIPELINE` is the default. A difference is
waived only by a `UniverseOnlyCompatibilityRule` naming both fingerprints, every
differing parameter and a rationale — and that rule refuses at construction if
any differing parameter is in `CONTRACT_SET_PARAMETERS`.

### §4 — The scope is reconstructed from the stored request

`derive_source_scope` reads root, expiration, strike, right, `max_dte`,
`strike_range` and `min_time` back out of the stored query parameters. The
declaration's scope becomes a claim that is compared against the derived one and
cannot widen it.

`min_time` is the one that matters. A sweep taken with `min_time=15:30:00`
contains the contracts that traded after 15:30 — a smaller set than the same
request without it, and one that re-derives perfectly. The chain scope is built
through the same function, from the same `as_query` the client sends.

### §5 — Documentation identities are extracted

`UniverseDocumentationRule(identities=...)` is gone, and so is
`UniverseDerivation`: both were caller-supplied contract sets sitting beside a
hash of real bytes. A rule now names a document, an effective period, a scope and
an `extractor_version`. A registered extractor reads the verified bytes and emits
a `UniverseExtractionArtifact` recording the document hash, the extractor
version, the rule id, the **character ranges read**, the identities found and the
instant the extraction ran.

One extractor ships (`contract-table/2.1.11`, a delimited machine-readable
block). The production rule registry is empty: no document stating which
SPX/SPXW contracts exist has been read (OD-11).

### §6 — Effective periods enforced

`period_refusals(session)` runs before resolution, against `market_session_date`.
Not yet effective, expired, and states no period at all are three distinct
refusals. `effective_from` is optional precisely so the third is representable
rather than something a caller invents to satisfy a constructor.

### §7 — The observation time is the extraction

`observed_at` for a documentation universe is
`UniverseExtractionArtifact.extraction_executed_at`. v2.1.10 used
`universe.declared_at`, so how stale a document reading was became whatever the
declaration said.

### §8 — Recovery compares the whole artifact

`rederived.artifact_hash == stored.artifact_hash`, with
`first_semantic_difference` naming the first field that moved. v2.1.10 compared
the identity set and the coverage status — two fields of thirteen — leaving
`observed_at`, `source_scope` and the source fingerprints free.

`declaration_hash` left the semantic payload. It is a hash of a caller statement
(`declared_at` in particular, which nothing reads), and hashing one into the
evidence is the pattern this release removes.

### §9 — The evidence chain is persisted

Content-addressed in the artifact store: the capture-verification receipt
**including the source manifest**, the universe-resolution receipt, the
documentation extraction artifact and the verified universe. `ArtifactKind`
gained `CAPTURE_VERIFICATION` and `UNIVERSE_RESOLUTION`, and
`DOCUMENTATION_EVIDENCE` — declared in v2.1.9 and unused — is now written.

The source manifest travels with the receipt because a universe is resolved
against records belonging to an earlier operation, so the chain's own manifest
does not name them. Without it recovery would have to rebuild the claim out of
the store it is meant to be checked against, and a manifest derived from a store
always matches that store.

### §10 — Readiness names what it checked

`analytical_readiness_of` → `universe_readiness_of`, returning `UNIVERSE_READY` /
`UNIVERSE_NOT_READY`. `assess_analytical_readiness` checks six conditions —
trusted normalization, a verified OI settlement date, resolved pricing
compatibility, `FULL_REQUEST_ENUMERATED`, no material source exclusions,
matching capture and reading pipeline fingerprints — and returns
`NOT_ANALYTICALLY_READY` naming each one it could not establish. Anything absent
is a blocker, not a pass.

### §11 — Pagination hardened

Duplicate page numbers, disagreeing `total_results`, zero or several terminal
pages, page numbers outside `1..total_pages` and duplicate partition
fingerprints are each refused. Full coverage additionally requires the derived
identity count to equal `total_results`, and a sweep with no stated total cannot
reach it. Unreachable today, which is when the semantics are cheap to fix.

### §12 — One capture command

```bash
python -m src.tools.capture_thetadata_once \
  --config config/thetadata_capture.yaml \
  --output /absolute/path/outside/this/repo/capture-2026-08-04
```

Dry run by default. Its pipeline is built with a transport whose every method
raises, so "no request was made" is a property of the object rather than of the
control flow — and it works without the `http` extra installed.

---

## §13 — Operator documentation

`README.md`, `docs/THETADATA_INTEGRATION.md` and `docs/ADAPTER_CERTIFICATION.md`
no longer instruct anyone to call `capture_and_compute` or `compute_gex`, both
removed in v2.1.5 when capturing and computing were separated. A test now fails
the build if a removed method appears inside a fenced code block in any
documentation page — prose recording the removal is the useful kind of mention
and stays possible.

The four states are stated together: **raw capture ready**, **trusted
calculation not ready**, **analytical dataset not ready**, **adapter not
certified**.

---

## §14 — Versions

| Constant | Value |
|---|---|
| package version | `2.1.11` |
| `EXPECTED_UNIVERSE_SCHEMA_VERSION` | `expected-universe/2.1.11` |
| `UNIVERSE_RESOLVER_SCHEMA_VERSION` | `universe-resolver/2.1.11` |
| `CERTIFICATION_SCHEMA_VERSION` | `adapter-certification/2.1.11` |
| `RAW_CAPTURE_RUN_SCHEMA_VERSION` | `raw-capture-run/2.1.11` |
| universe documentation schema | `universe-documentation/2.1.11` |
| `UNIVERSE_EXTRACTION_SCHEMA_VERSION` | `universe-extraction/2.1.11` |
| capture-verification receipt | `capture-verification/2.1.11` |

`MODEL_VERSION` stays `gex-engine/2.1.10` and `PARSER_VERSION` stays
`thetadata-v3-parser/2.1.10`. Neither's canonical semantics changed, and a
version bumped because a release happened conveys nothing.

### Frozen values

| Value | v2.1.11 | Classification |
|---|---|---|
| `EXPECTED_OUTPUT_HASH` | `0e536883…` unchanged | **no change** |
| `EXPECTED_MODEL_FINGERPRINT` | `32b4694c…` unchanged | **no change** |
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172b…` unchanged | **no change** |

Nothing moved, and that is the finding. This release changed who may authorize a
universe, where a source scope is read from and what recovery compares — none of
which is an input to a GEX computed from a synthetic chain with no capture and no
universe. A release that moved these would have changed the maths while claiming
to change the evidence rules.

The reference numbers are identical to v2.1.10: `59228408806.90227` unsigned,
`-24836100698.992706` signed, confidence `93.857`, 250 contracts, 1,263,165 open
interest, `5039.133782540731` primary zero-gamma root.

---

## §15 — Regressions that fail against v2.1.10

| # | Requirement | Test |
|---|---|---|
| 1 | hand-constructed artifact cannot authorize completeness | `test_a_hand_built_artifact_cannot_authorize_completeness`, `test_wrapping_a_forged_artifact_in_a_resolution_is_re_run_and_refused` |
| 2 | unrelated content-hashed document establishes no identities | `test_an_unrelated_content_hashed_document_establishes_no_identities`, `test_a_rule_cannot_carry_a_caller_supplied_identity_list` |
| 3 | future documentation rule cannot establish a 2026 universe | `test_a_future_documentation_rule_cannot_establish_a_2026_universe` |
| 4 | expired documentation rule cannot establish a universe | `test_an_expired_documentation_rule_cannot_establish_a_universe`, `test_a_rule_with_no_effective_period_establishes_nothing` |
| 5 | HTTP 500 source record cannot become verified evidence | `test_an_http_500_response_cannot_become_verified_evidence` |
| 6 | source capture must pass capture verification | `test_a_source_capture_must_pass_capture_verification`, `test_a_record_outside_the_verified_manifest_is_refused`, `test_a_record_backed_artifact_must_name_its_verification` |
| 7 | source and target pipeline fingerprints are compared | `test_source_and_target_pipeline_fingerprints_are_compared`, `test_a_min_time_difference_cannot_be_waived`, `test_a_documented_waiver_permits_a_named_pair` |
| 8 | `min_time` is derived from the stored request | `test_min_time_is_read_back_out_of_the_source_request` |
| 9 | source scope cannot be widened by the declaration | `test_a_declaration_cannot_widen_the_source_scope`, `test_the_source_scope_is_derived_from_the_stored_query_parameters` |
| 10 | stale `observed_at` replacement fails recovery | `test_recovery_compares_the_whole_semantic_artifact[observed_at]` |
| 11 | recovered semantic artifact must equal the stored one | `test_recovery_compares_the_whole_semantic_artifact` (6 params), `test_the_evidence_chain_is_persisted_beside_the_capture` |
| 12 | analytical readiness requires every condition | `test_analytical_readiness_requires_every_condition_it_names`, `test_the_completeness_only_function_no_longer_returns_a_dataset_verdict` |
| 13 | documentation no longer references `capture_and_compute` | `test_the_documentation_describes_apis_that_exist` |
| 14 | raw-capture dry run performs no network calls | `test_a_dry_run_makes_no_network_call` |
| 15 | live mode requires the explicit flag | `test_a_live_run_requires_the_explicit_flag`, `test_the_dry_run_is_the_default` |
| 16 | the command never computes a trusted GEX | `test_the_capture_command_never_computes_a_trusted_gex`, `test_the_capture_command_cannot_trade_or_calculate`, `test_the_capture_is_permanently_raw_only` |

Plus the extraction-time regression
(`test_the_observation_time_is_the_extraction_not_the_declaration`), the
pagination strictness set (duplicate pages, disagreeing totals, several terminal
pages, identity count vs `total_results`, overlapping partitions), the incomplete
write and unsupported parser refusals, and
`test_the_only_operator_command_is_the_raw_capture`.

---

## §16 — Verification

All commands **locally executed** at commit `acafc16`, `git status --porcelain`
empty.

| Command | Result | Where |
|---|---|---|
| `pytest` | **2289 passed** | locally executed, Python 3.12.10 |
| `pytest -m integration` | 18 passed | locally executed, Python 3.12.10 |
| `pytest -m regression` | 46 passed | locally executed, Python 3.12.10 |
| `pytest -m replay` | 10 passed | locally executed, Python 3.12.10 |
| `ruff check .` | All checks passed | locally executed |
| `ruff format --check .` | 144 files already formatted | locally executed |
| `mypy src` | no issues in 74 source files | locally executed |
| `coverage report --fail-under=90` | **90%**, exit 0 | locally executed |
| `python -m src.app` | exit 0 | locally executed |
| `python -m src.tools.capture_thetadata_once --output …` | exit 0, dry run | locally executed |

### Python versions

| Version | Status |
|---|---|
| **3.12.10** | **locally executed** — every command above |
| **3.13** | **unverified** — not installed on this machine (`py -0p` lists 3.12 and 3.11 only) |

The CI matrix covers 3.12 and 3.13 across the `quality`, `invariants` and
`no-trading-guarantee` jobs, and `workflow_dispatch` is enabled. **No CI run has
been observed for this commit**, so 3.13 is reported as unverified rather than as
`executed in CI`.

---

## Operator path

**Dry run** — sends nothing, and cannot:

```bash
python -m src.tools.capture_thetadata_once \
  --config config/thetadata_capture.yaml \
  --output /absolute/path/outside/this/repo/capture-2026-08-04
```

Prints: resolved configuration, pipeline fingerprint, capture-plan fingerprint,
required endpoints, subscription tier, raw-store destination, capture readiness,
capture blockers, calculation blockers, analytical blockers, destination
refusals.

**Live capture:**

```bash
python -m src.tools.capture_thetadata_once \
  --config config/thetadata_capture.yaml \
  --output /absolute/path/outside/this/repo/capture-2026-08-04 \
  --execute-live
```

Expected output files:

```
<output>/raw/                    the payloads, sharded, plus the store index
<output>/artifacts/              content-addressed artifacts
<output>/manifest.json           the capture manifest
<output>/capture-summary.json    the operator report (raw-capture-run/2.1.11)
```

Printed: session id, operation id and fingerprint, manifest hash, record ids,
per-endpoint HTTP status, parser version, contract count, integrity counts,
verification result, and every path written.

**Safety behaviour:**

- dry run unless `--execute-live`; the dry run's transport raises on any use;
- refuses a relative output path, and any path inside this repository;
- refuses to proceed unless readiness is exactly `READY_FOR_RAW_CAPTURE_ONLY`;
- requires `DURABLE_APPEND_ONLY` raw and artifact stores;
- establishes no settlement rule and no universe, so the capture is permanently
  raw-only and no later call can make it trusted (OD-26);
- computes no GEX — enforced by an AST check in the architecture tests;
- places no orders and constructs no broker; the repository has neither.

---

## What this release does **not** claim

- **Not `ADAPTER_CERTIFIED`.** Eight load-bearing vendor conventions are unknown.
- **Not `READY_FOR_ANALYTICAL_DATASET`.** No verified source reaches
  `FULL_REQUEST_ENUMERATED`, and five other conditions are unestablished.
- **Not validated against live vendor data.** Nothing here has met a real
  ThetaData response.
- **Not able to trade.** No broker adapter, no order type, no position sizing, no
  execution path.

## What would move it forward

The command above, with `--execute-live`, on a real subscription. Then compare
the eight conventions in `docs/ADAPTER_CERTIFICATION.md` against the captured
bytes, and — if the subscription exposes a contract-list endpoint — mark it
`is_dedicated_contract_list=True`, capture it, resolve it, and pass the resulting
`UniverseResolution` to `capture_session`. Completeness becomes measurable at
that point and not before.
