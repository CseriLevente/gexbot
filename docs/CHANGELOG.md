# Changelog

## 2.1.12 - the first real session, and two evidence-recovery gaps

One objective: make the first raw-only ThetaData session operationally safe,
fully configured, failure-preserving and honestly attributed. Plus the two
evidence gaps the v2.1.11 review found.

The operator command shipped in v2.1.11. Reviewing it against a session that
actually happens found three things it was quietly getting wrong when nothing
went wrong, and one thing it did badly when something did.

**Status:** `IMPLEMENTED` | `TESTED_SYNTHETICALLY` |
`TESTED_WITH_OFFLINE_FIXTURES` | `READY_FOR_RAW_CAPTURE_ONLY` |
`NOT_READY_FOR_ANALYTICAL_DATASET` | `NOT_VALIDATED_WITH_LIVE_THETADATA`.

The repository remains incapable of placing an order.

### Defects fixed

| S | Defect in v2.1.11 | Why it mattered | Fix |
|---|---|---|---|
| 1 | The CLI called `HttpxTransport()` with no arguments | The connect timeout, read timeout, response cap and authentication in the profile never reached the wire; the first paid session would have run on library defaults | The command builds nothing. `build_thetadata_client` constructs the configured transport, and both reports carry the effective settings with no credential value in them |
| 2 | `capture_origin_of` read a class attribute | `HttpxTransport.origin_for` distinguished a local Theta Terminal from a vendor call and nothing called it, so the shipped profile — which points at `127.0.0.1` — would have stamped every record `LIVE_HTTP_CAPTURE` | The origin is derived from the effective base URL and bound to the records, the manifest, the summary and the intent |
| 3 | Retryable non-2xx bodies were consumed inside the retry loop | The 429 naming a quota and the 503 naming a maintenance window were exactly the responses that would explain a partial capture, and they were the ones nobody kept — while the docs said every response was preserved | An attempt observer inside `RetryingTransport`; `HttpAttemptRecord` per attempt, bodies content-addressed under `attempts/`, counts in the summary |
| 4 | An exception left raw files with no manifest | Two endpoints' bytes on disk, nothing describing them, and no state saying whether the run had started | `RawCaptureRunState`, a `run-intent.json` written before the first request, and a manifest + summary on every exit path. A partial manifest says it is partial and cannot pass `verify_capture` |
| 5 | The destination check compared the literal path | A symlink in `/tmp` pointing at the checkout passed, and the paid capture landed in the working tree | `resolve(strict=False)` first; symlinks, existing files, non-empty directories and directories holding an earlier `run-intent.json` are each refused |
| 6 | Session ids were second-resolution timestamps | Two runs in the same second produced the same id, and record ids derive from it | `capture-<timestamp>-<nonce>` |
| 7 | The dry run built a `FileRawStore` at the destination | It left `raw/` and `raw.health/` behind, so the following real run refused the directory it had just created | The store capability is probed in a temporary directory that is deleted before the report returns; the destination does not exist afterwards |
| 8 | Reports were written with `write_text` | An interrupted process could leave a plausible-looking half-JSON | Serialise, write to a temporary file, `fsync`, `os.replace` |
| 9 | The CLI caught only `CaptureRunError` | Everything else was a traceback | Eleven documented exit codes, no secret on any path, a pointer to the written summary, and `--debug` for a traceback |
| 10 | A documentation resolution needed a process-global registry | `capture_session` re-runs the resolution *without* the caller's registry, and production keeps the global one empty — so no documentation universe could open a capture at all, and none could be recovered | `UniverseDocumentationEvidenceArtifact` plus the exact verified bytes, both content-addressed. Re-running and recovery consult no global state |
| 11 | `UniverseOnlyCompatibilityRule` took `differing_parameters` from the caller | The caller stating the difference was the caller asking for the waiver; two pipelines differing in `min_time` were waived by a rule naming `timeout_seconds` | `derive_parameter_diff` computes it from two configurations; the rule carries only `approved_diff_hash`, and any contract-set-affecting difference is refused whatever the rule says |
| 12 | `assess_analytical_readiness` took six loose `Any` arguments | Six `SimpleNamespace` objects with the right attribute names returned `READY_FOR_ANALYTICAL_DATASET` | It takes only a `VerifiedAnalyticalEvidenceContext`, and `build_analytical_evidence` re-derives the chain and re-verifies the capture itself |

### The operator path

```bash
python -m src.tools.capture_thetadata_once \
  --config config/thetadata_capture.yaml \
  --output /absolute/path/outside/this/repo/capture-2026-08-05
```

Dry run by default; `--execute-live` to contact the vendor. Produces
`run-intent.json`, `raw/`, `attempts/`, `artifacts/`, `manifest.json` and
`capture-summary.json`. Exit 0 only when every planned endpoint answered and the
manifest verified against the store.

### Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f` | unchanged | **no change** |
| `EXPECTED_MODEL_FINGERPRINT` | `32b4694cef709838` | unchanged | **no change** |
| `EXPECTED_OUTPUT_HASH` | `0e536883...` | unchanged | **no change** |

The operator layer changed; the maths did not. `MODEL_VERSION` and
`PARSER_VERSION` stay at `2.1.10` for the same reason.

### Behavioural changes worth knowing

* `UniverseOnlyCompatibilityRule(differing_parameters=...)` is **gone**; use
  `approved_diff_hash=diff_fingerprint(derive_parameter_diff(source, target))`.
* `assess_analytical_readiness(**six_kwargs)` is **gone**; build a context with
  `build_analytical_evidence(pipeline=..., chain=..., manifest=..., store=...)`.
* `capture_origin_of(transport)` takes an optional second argument, the URL. A
  call without it still works and cannot distinguish a local terminal.
* `build_thetadata_client`, `ThetaDataRuntime.from_config` and
  `ThetaDataResearchPipeline.from_config/from_loaded_config` accept
  `attempt_observer=`.
* `RawCaptureManifest` gained `rebuilt_from` and `semantic_payload`.
* `VerifiedExpectedUniverseArtifact` gained `documentation_evidence_hash`.

---

## 2.1.11 - universe-evidence authenticity, and one way to capture

Two objectives. Make universe evidence follow from verified source facts rather
than from having the right dataclass type, and give the first paid ThetaData
session a single command that cannot fire by accident.

The v2.1.10 defect is short to state. `VerifiedExpectedUniverseArtifact` is a
public frozen dataclass whose `__post_init__` refuses a coverage its source kind
could not reach. That constrains what an artifact may *say*, and it was mistaken
for a constraint on who may *make* one: `capture_session` took an artifact and
checked `isinstance`. So

```python
VerifiedExpectedUniverseArtifact(
    source_kind=ExpectedUniverseSourceKind.AUTHORITATIVE_DOCUMENTATION,
    coverage_status=UniverseCoverageStatus.FULL_REQUEST_ENUMERATED,
    documentation_evidence_id="a-document-nobody-registered",
    evidence_fingerprint="f" * 64,
    ...
)
```

constructed, passed the check, and opened a capture claiming a complete
universe.

**Status:** `IMPLEMENTED` | `TESTED_SYNTHETICALLY` |
`TESTED_WITH_OFFLINE_FIXTURES` | `READY_FOR_RAW_CAPTURE_ONLY` |
`NOT_READY_FOR_ANALYTICAL_DATASET` | `NOT_VALIDATED_WITH_LIVE_THETADATA`.

The repository remains incapable of placing an order.

### Defects fixed

| S | Defect in v2.1.10 | Why it mattered | Fix |
|---|---|---|---|
| 1 | A constructible artifact authorized completeness | The type's refusals say what it may claim, not who may claim it | `capture_session(universe_resolution=...)` takes a `UniverseResolution` -- the declaration plus the source capture -- and **re-runs the resolution**, requiring the same artifact hash |
| 2 | The resolver read from any object with `records()` | An HTTP 500 body, a half-written record or an unsupported parser was evidence if it hashed to its own descriptor | `verified_universe_source` requires a `verify_capture` result covering every named record, and refuses non-2xx, `capture_complete=false` and unsupported parser versions per record |
| 3 | The pipeline comparison received one fingerprint twice | `check_source_compatibility(chain=self.fingerprint(), source=self.fingerprint())` compared a string with itself | `source_pipeline_fingerprint` is read off the verified records. `PipelineCompatibilityPolicy.IDENTICAL_PIPELINE` by default; a difference is waived only by a `UniverseOnlyCompatibilityRule`, which refuses at construction if any differing parameter decides the contract set |
| 4 | The source scope came from the declaration | A sweep taken with `min_time=15:30:00` could present itself as unbounded, and it re-derives perfectly | `derive_source_scope` reconstructs root, expiration, strike, right, `max_dte`, `strike_range` and `min_time` from the stored query parameters. The declaration's scope becomes a claim that is compared and cannot widen the derived one |
| 5 | A documentation rule carried `identities=frozenset(...)` | A hash of real bytes authenticated a list that came from the caller | The field is gone. A rule names a document and an `extractor_version`; identities come from a registered extractor reading the verified bytes, recorded with the character ranges they were read from |
| 6 | Effective periods were never checked | A rule effective from 2030 established a March 2026 universe | `period_refusals(session)` runs before resolution, against `market_session_date`. Not-yet-effective, expired and no-period-at-all are three distinct refusals |
| 7 | `observed_at` for a document was `universe.declared_at` | A caller's timestamp decided how stale a document reading was | `UniverseExtractionArtifact.extraction_executed_at`, alongside the document's verification instant and effective date |
| 8 | Recovery compared two fields of thirteen | A stale listing edited to look current recovered cleanly | Full `artifact_hash` equality, with `first_semantic_difference` naming the first field that moved |
| 9 | The evidence chain lived partly in process globals | Recovering a documentation universe needed the registry populated in the same process | The capture-verification receipt (with the source manifest), the resolution receipt, the extraction artifact and the verified universe are all content-addressed in the artifact store |
| 10 | `analytical_readiness_of` checked one of five conditions | It could return `READY_FOR_ANALYTICAL_DATASET` for a chain with an unknown settlement date and unresolved pricing | Renamed `universe_readiness_of` -> `UNIVERSE_READY` / `UNIVERSE_NOT_READY`. `assess_analytical_readiness` checks six conditions and names every one it could not establish |
| 11 | Pagination metadata was read loosely | Two responses claiming page 3 collapsed into one; a `total_results` disagreement was discarded; several terminal pages counted as one | Duplicate pages, disagreeing totals, zero or several terminal pages and duplicate partition fingerprints are each refused, and full coverage requires the identity count to equal `total_results` |
| 12 | There was no capture command | The sequence lived only in test fixtures, and the docs described `capture_and_compute` and `compute_gex`, removed in v2.1.5 | `python -m src.tools.capture_thetadata_once`, dry run by default |

### The operator path

```bash
python -m src.tools.capture_thetadata_once \
  --config config/thetadata_capture.yaml \
  --output /absolute/path/outside/this/repo/capture-2026-08-04
```

Without `--execute-live` it sends nothing -- and not by declining to: the
pipeline is built with a transport whose every method raises, so the absence of
a request is a property of the object rather than of the control flow. It prints
the resolved configuration, the pipeline and capture-plan fingerprints, the
required endpoints, the tier, the destination, the capture readiness and the
calculation and analytical blockers.

With `--execute-live` it opens one operation, fetches the index snapshot, the
quotes, the open interest and the first-order greeks, preserves every response,
writes `manifest.json` and `capture-summary.json`, scans the store and verifies
the manifest against it. It refuses an output directory inside the repository,
requires durable stores, computes no GEX and places no orders.

The capture it takes establishes no settlement rule, which makes it permanently
raw-only. That is deliberate: which session open interest settled in weights
every GEX term, and the rule is fixed when a session opens (OD-26).

### Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f` | unchanged | **no change** |
| `EXPECTED_MODEL_FINGERPRINT` | `32b4694cef709838` | unchanged | **no change** |
| `EXPECTED_OUTPUT_HASH` | `0e536883...` | unchanged | **no change** |

Nothing moved, and that is the finding. v2.1.11 changed who may authorize a
universe, where a source scope is read from and what recovery compares. None of
that is an input to a GEX computed from a synthetic chain with no capture and no
universe, so a release that moved these would have changed the maths while
claiming to change the evidence rules.

`MODEL_VERSION` and `PARSER_VERSION` stay at `2.1.10` for the same reason: a
version bumped because a release happened conveys nothing.

### Behavioural changes worth knowing

* `capture_session(verified_expected_universe=...)` is **gone**. Use
  `pipeline.resolve_expected_universe(declaration=..., source_manifest=...,
  source_store=...)` and pass the result as `universe_resolution=`.
* `analytical_readiness_of` is **gone**. Use `universe_readiness_of` for the
  completeness question or `assess_analytical_readiness` for the verdict.
* `UniverseDocumentationRule(identities=...)` and `UniverseDerivation` are
  **gone**. A rule names an `extractor_version`; `effective_from` is now
  optional so that "states no period" is representable and refusable.
* `VerifiedExpectedUniverseArtifact` gained `source_pipeline_fingerprint` and
  `source_verification_fingerprint`, both required for a record-backed source.
  `declaration_hash` left the semantic payload: it is a hash of a caller
  statement, and hashing one into the evidence is the pattern being removed.
* `read_pagination_metadata` now raises where it previously returned a
  permissive result.

---

## 2.1.10 - expected-universe coverage evidence

v2.1.9 made a universe *resolvable*: the resolver reopened the records it named
and re-derived the identities. That closed "a caller typed a list and labelled it
a vendor listing", and it left the harder half open.

**Proving that a set of identities occurs in stored records is not proving that
those records enumerate the complete universe the request should have
returned.** A truncated response enumerates its own rows perfectly.

Five things followed from that gap:

* the resolver accepted any endpoint with one row per contract as a
  `VENDOR_CONTRACT_LIST`, so an `/v3/option/snapshot/quote` response established
  `MEASURED_COMPLETE` for the whole chain;
* `CAPTURED_PAGINATION_METADATA` named a check nobody had written — its resolver
  re-derived identities and never read a page number, a total or a continuation
  token, so one ordinary quote response satisfied it;
* the universe resolver looked its evidence id up in `DOCUMENTATION_RULES`,
  which is the *settlement* registry. A content-verified document about
  open-interest settlement says nothing about which options exist, and it
  established a universe of whatever identities sat beside it;
* `complete_for_request: bool` was a constructor argument, hashed into the
  universe. A caller passing `True` was the entire evidence for full coverage,
  and hashing an assertion made it look like a finding;
* `observed_at` came from the caller, so a listing captured three weeks ago
  could present itself as observed this morning — and staleness is measured
  against that instant.

**Status:** `IMPLEMENTED` | `TESTED_SYNTHETICALLY` |
`TESTED_WITH_OFFLINE_FIXTURES` | `READY_FOR_RAW_CAPTURE_ONLY` |
`NOT_READY_FOR_ANALYTICAL_DATASET` | `NOT_VALIDATED_WITH_LIVE_THETADATA`.

The repository remains incapable of placing an order.

### Defects fixed

| S | Defect in v2.1.9 | Why it mattered | Fix |
|---|---|---|---|
| 1 | Any row-per-contract endpoint counted as a contract list | A quote snapshot established `MEASURED_COMPLETE` for the whole request | `ResponseCapabilities` separates "enumerates rows" from "enumerates the request universe", "carries pagination metadata" and "is a dedicated contract list". No ThetaData endpoint has the last three |
| 2 | Pagination coverage was never read | The source kind named a check that did not exist | `PaginationCoverageEvidence`, built by `read_pagination_metadata` from the stored bytes. Unsupported where no metadata exists, rather than simulated |
| 3 | `complete_for_request` was a caller Boolean | Full coverage was whatever the caller said | `UniverseCoverageStatus`, produced by the resolver. A declaration may carry `declared_coverage` for the audit trail and nothing reads it |
| 4 | Settlement documentation defined universes | A genuine document about the wrong subject | `UniverseDocumentationRule` in its own registry, requiring an identity set or a typed derivation |
| 5 | A declaration could measure completeness | The engine measured against what somebody expected | `VerifiedExpectedUniverseArtifact` is a different type, and the engine takes only that |
| 6 | Independence came from a source *string* | `expected_source="VENDOR_CONTRACT_LIST"` made a chain independently observed | `ChainCompleteness` carries the artifact hash, evidence fingerprint, coverage status and resolver version, and decides from those |
| 7 | Nothing compared the source's scope to the chain's | A narrower or older sweep re-derives perfectly and covers a different set | `UniverseRequestScope` and `check_source_compatibility`: root, expirations, strikes, rights, filters, ordering, staleness, resolver version |
| 8 | The chain operation was stamped with an unresolved claim | Whether it held was discovered at replay, after the capture | `capture_session(verified_expected_universe=...)` checks scope and timing before the operation opens; `declared_expected_universe=...` is the diagnostic form |
| 9 | Recovery returned the declaration | What reached completeness was the caller's description of the evidence | `recover_capture_artifacts` rebuilds the artifact, checks its hash against the stamp, and re-derives it from its records |
| 10 | Session dates came from `as_of.date()` | 2026-03-18T01:00Z is the 18th in UTC and the 17th in New York | One `market_session_date(as_of)` through `ZoneInfo("America/New_York")`, with an AST test that no other site produces one |

### Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f` | unchanged | — |
| `EXPECTED_MODEL_FINGERPRINT` | `6accfab618292203` | `32b4694cef709838` | `VERSION_METADATA_ONLY` |
| `EXPECTED_OUTPUT_HASH` | `d0be7199...` | `0e536883...` | `REPRESENTATIONAL` |

Measured in two parts, separately, and neither assumed.

Pinning `model_version` back to `gex-engine/2.1.9` reproduces
`6accfab618292203c9af97789874a238786c8884446fe5898a1d845f59a5cc16` exactly.

The output hash covers the serialised snapshot, which includes the
chain-completeness *report*. v2.1.10 replaces one key with four:
`expected_complete_for_request` gives way to `coverage_status`,
`universe_artifact_hash`, `universe_evidence_fingerprint` and
`resolver_version`. Removing those four and restoring the old key reproduces
`d0be719931de451dd8ef88a178ec8287bec899b93ed605e8f5be4275eedb1961` exactly, so
nothing else moved. Every numeric literal in the reference case is unchanged:
59,228,408,806.90227 unsigned, −24,836,100,698.992706 signed, 93.857 confidence,
250 contracts, 1,263,165 open interest, 5039.1337825 primary zero-gamma root.

### Behavioural changes worth knowing

* **`VENDOR_CONTRACT_LIST` and `CAPTURED_PAGINATION_METADATA` are unsupported in
  production.** No verified ThetaData endpoint is a listing endpoint or returns
  page metadata (OD-11). A snapshot resolves as `OBSERVED_SNAPSHOT_ROWS` to
  `OBSERVED_SUBSET`, which is honest and useful and not completeness.
* `capture_session` takes `verified_expected_universe` **or**
  `declared_expected_universe`, never both, and no longer takes
  `expected_universe`.
* `ExpectedContractUniverse` is a declaration. It has no `observed_at` and no
  `complete_for_request`; it has `declared_at` and `declared_coverage`, and it
  carries a `scope`.
* `resolve_expected_universe` returns a `ResolvedExpectedUniverse` whose
  `artifact` is the verified object. The old `.universe` attribute is gone.
* `ChainCompleteness.NON_INDEPENDENT_SOURCES` is gone. Independence is decided
  from the artifact hash and the coverage status.
* Analytical readiness now requires `FULL_REQUEST_ENUMERATED` or a written
  incomplete-chain policy. **Raw-capture readiness deliberately does not consult
  it**: bytes are worth collecting whatever their coverage.
* A universe source older than two sessions, or from a narrower scope, is
  refused at `capture_session` rather than at replay.

## 2.1.9 - settlement-date and expected-universe evidence

v2.1.8 bound every non-payload input to the capture operation. Two of those
inputs turned out to be bound to something that had never been checked.

**The settlement date.** v2.1.8 replaced an authorizing enum with resolvers,
which was right, and stopped one step short:

```python
rule = rules.get(evidence.reference)
if not rule.covers(evidence.as_of):
    return failure
return ResolvedSettlementDate(as_of=evidence.as_of, ...)
```

The date still came from the caller. The rule was consulted only to confirm it
was *in force* on the day already chosen, so one registered rule saying "prior
trading session" would authorize 2026-03-16, 2026-03-15 and 2026-03-01 alike for
a 2026-03-17 chain. `normalized_value: str` is why: free text cannot be applied
to a session date, so the answer had to come from the argument list.

And a `DocumentationRule` could carry any 64-character string as a content hash,
because nothing ever opened the file. `document_reference="/definitely/missing"`
with `document_content_hash="0" * 64` registered cleanly.

Worse, a capture stamped `open_interest_date_rule_fingerprint=""` — no rule
established — would still return a trusted result if the *call* supplied
documentation evidence. The capture said no rule had been established and the
calculation said one had; the calculation won, because it was the one holding
the argument.

**The expected universe.** `source="vendor_contract_list"` was a string a caller
typed, and `source_record_ids` was read as a boolean — non-empty meant
"independently observed". No record was ever opened. There were also **two**
`ExpectedContractUniverse` classes, and the one the engine read carried no
provenance at all. `complete_for_request` existed on the type and was read
nowhere, so page one of a paginated listing whose members all arrived reported
the entire chain `MEASURED_COMPLETE`.

**Status:** `IMPLEMENTED` | `TESTED_SYNTHETICALLY` |
`TESTED_WITH_OFFLINE_FIXTURES` | `READY_FOR_RAW_CAPTURE_ONLY` |
`NOT_VALIDATED_WITH_LIVE_THETADATA`.

The repository remains incapable of placing an order.

### Defects fixed

| S | Defect in v2.1.8 | Why it mattered | Fix |
|---|---|---|---|
| 1 | A settlement rule could be supplied *after* the capture | A capture that established nothing became trusted on the strength of an argument | `capture_session(settlement_rule=...)` takes a `SettlementDateRuleArtifact` before any response exists. `compute_trusted_gex` accepts no settlement evidence at all |
| 2 | A documented rule *approved* a date instead of deriving one | One rule authorized every date, because the rule was never applied to anything | Typed `SettlementRule` semantics — kind, session offset, calendar — applied through the real trading calendar. There is no `as_of` parameter anywhere in the resolver |
| 3 | A content hash was a 64-character string nobody computed | A vendor page could be missing entirely and the rule still registered | Registration opens the file, hashes the bytes and compares. Missing files, hash mismatches, absolute paths and URLs are all refused |
| 4 | The resolved OI date reached nothing | It weights every GEX term, and the chain could carry a different one — or none | It flows into the recipe, every contract's timestamps, the chain hash, the receipt and replay, and the trusted path refuses when the chain disagrees with the capture |
| 5 | Two `ExpectedContractUniverse` classes | The engine read the one with no provenance | One authoritative type, with an architecture test that fails the build if a second appears |
| 6 | A universe was a label, not evidence | A typed list called `vendor_contract_list` established `MEASURED_COMPLETE` exactly as a real listing would | `resolve_expected_universe` reopens the named records, re-derives the identities and compares. Fake ids, wrong identities and non-enumerating endpoints all fail |
| 7 | `complete_for_request` was read nowhere | One page of a listing reported the whole chain complete | Two new statuses — `PARTIAL_UNIVERSE_ALL_LISTED_PRESENT` and `PARTIAL_UNIVERSE_MISSING_IDENTITIES` — and `implies_complete` is false for both |
| 8 | The operation fingerprint was compared, never recomputed | Editing `requested_as_of` and leaving the digest alone verified cleanly | `verify_capture` rebuilds the identity from the stored fields and recomputes the digest: `OPERATION_FINGERPRINT_MISMATCH` |
| 9 | `observe_field` always read `records_for(endpoint)[0]` | Evidence about page two was confirmed against page one's bytes | Both `observe_field` and `confirm_field` take and assert `record_id` |
| 10 | A stamped digest named an object nobody stored | Replay worked only while the caller still held the original in memory | A content-addressed `ArtifactStore`, keyed by the artifact's own hash so the stamped digest *is* the lookup key |

### Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f` | unchanged | — |
| `EXPECTED_MODEL_FINGERPRINT` | `79f3abe506978342` | `6accfab618292203` | `VERSION_METADATA_ONLY` |
| `EXPECTED_OUTPUT_HASH` | `128acd06...` | `d0be7199...` | `REPRESENTATIONAL` |

Both measured rather than asserted, and separately.

Pinning `model_version` back to `gex-engine/2.1.8` reproduces
`79f3abe506978342c52b31481f16f7ff61ac6f4824b586d4d7020a37a4e73d83` exactly, so
the model fingerprint moved on the version string.

The output hash covers the serialised snapshot, which includes the
chain-completeness *report*, and v2.1.9 adds one key to it:
`expected_complete_for_request`. Removing that single key from the payload and
re-hashing reproduces `128acd06...` exactly — so nothing else moved. Every
numeric literal in the reference case is unchanged and all of them still hold:
59,228,408,806.90227 unsigned, −24,836,100,698.992706 signed, 93.857 confidence,
250 contracts, 1,263,165 open interest, 5039.1337825 primary zero-gamma root.

### Behavioural changes worth knowing

* **`compute_trusted_gex` takes neither settlement evidence nor an expected
  universe.** It takes `manifest`, `store`, `artifact_store` and an optional
  `open_interest_provenance`, and recovers the rest from the capture operation.
* **A capture opened without a settlement rule can never produce a trusted GEX.**
  It remains fully usable for raw storage, diagnostic calculation and
  vendor-schema research. This is the shipped production state: no ThetaData
  settlement document has been read (OD-26).
* `capture_session` takes `settlement_rule` and `artifact_store`, and no longer
  takes `open_interest_as_of` — the date is what the rule derives.
* `fetch_chain` uses the session's universe and settlement date automatically.
  Passing `expected_contract_ids` alongside a session that owns a universe is
  refused rather than merged.
* `ExpectedContractUniverse` moved to `src.domain.expected_universe` and takes a
  `source_kind` rather than a `source` string. The copy in
  `src.domain.completeness` is gone.
* `ScheduleDerivation` no longer carries `derived_settlement_date`. A derivation
  that stated its own answer would be the same defect one level down.
* Captures written by v2.1.8 do not carry
  `spot_synchronization_policy_fingerprint` and so cannot have their operation
  digest recomputed; they are refused rather than exempted.

## 2.1.8 - capture-operation and normalization-input binding

v2.1.7 re-derived the chain from the raw bytes and compared the two. That closed
every *payload* mutation. What it did not bind was the inputs that are **not in
the payload**, and the sharpest of those is the instant:

```python
recipe = self.normalization_recipe(as_of=chain.as_of)
rederived = self.rebuild_chain_from_capture(..., recipe=recipe)
```

Read it twice. The chain under test chose the timestamp it was tested against,
so shifting `chain.as_of` shifted the rebuild with it and the two agreed. A
tenth of a second is a real change in time-to-expiry on a 0DTE afternoon; an
hour is a different market. The same shape ran through spot provenance (a
caller-supplied timestamp *and* tolerance), the open-interest settlement date
(an enum that authorized itself), chain completeness (an open metadata key that
moved the confidence score) and record consumption (a replay that never checked
it had used everything it was given).

**Status:** `IMPLEMENTED` | `TESTED_SYNTHETICALLY` |
`TESTED_WITH_OFFLINE_FIXTURES` | `READY_FOR_RAW_CAPTURE_ONLY` |
`NOT_VALIDATED_WITH_LIVE_THETADATA`.

The repository remains incapable of placing an order.

### Defects fixed

| S | Defect in v2.1.7 | Why it mattered | Fix |
|---|---|---|---|
| 1 | Nothing named the capture *operation* | A session may run several fetches; only the standing configuration was stamped, so per-fetch inputs were unbound | `CaptureOperationIdentity` -- both timestamps, the rule that chose one, the spot policy, the settlement rule, the expected universe -- stamped whole onto every record |
| 2 | The replay took its valuation instant from the chain | The thing under test chose the input it was tested against; 0.1s, 0.5s, 1s and 1h shifts were all trusted | `resolve_operation` reads the instant out of the verified index print. The chain is never asked |
| 3 | `SpotProvenance` was a caller argument | Its `timestamp` and `tolerance_seconds` were the two numbers the skew check compared, so a caller could claim 12:00 for an 11:00 print and be checked against its own claim | Both derived: the timestamp from the verified index record, the tolerance from `max_spot_skew_seconds`, which is now real configuration and enters the pipeline fingerprint |
| 4 | `EvidenceKind` authorized a settlement date by existing | `VENDOR_FIELD` with `record_ids=("fake-record",)` and `AUTHORITATIVE_VENDOR_DOCUMENTATION` with `reference="lol"` both permitted a trusted calculation | Four resolvers in `evidence_resolvers.py`. The kind selects *which check runs*; supplying it does not pass the check |
| 5 | Chain completeness travelled in `snapshot.meta` | A forged `chain_completeness_object` moved the confidence score from 52.0619 to 57.3394 with `trusted=True`; metadata was altering a calculation | A typed `ChainSnapshot.completeness` field, the full semantic payload in the chain hash, and an architecture test that fails when production GEX code reads calculation-affecting data from `meta` |
| 6 | The expected universe was a calculation argument | The same capture could be scored `MEASURED_COMPLETE` against one universe and replayed `PARTIALLY_OBSERVED` against another | `ExpectedContractUniverse` declared on `capture_session`, its hash stamped on every record, checked -- not adopted -- at replay |
| 7 | A replay never checked it consumed the whole capture | An extra quote response replayed from the first record, matched, and verified; the second sat in the store looking like evidence | `RecordConsumptionReport`: assigned == consumed, each exactly once, hashed into the receipt. A second response per endpoint needs a plan that declares pagination, batched expirations, retries or partitions |
| 8 | Documentation evidence was a citation | A vendor can rewrite a page without renaming it, and the fingerprint would not move | `document_content_hash`, derived at construction and carried into the pipeline fingerprint |

### Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f` | unchanged | -- |
| `EXPECTED_MODEL_FINGERPRINT` | `1b353ba18cefb0a2` | `79f3abe506978342` | `VERSION_METADATA_ONLY` |
| `EXPECTED_OUTPUT_HASH` | `3af3ef9c...` | `128acd06...` | `VERSION_METADATA_ONLY` |

Measured in two parts rather than asserted. Pinning `model_version` back to
`gex-engine/2.1.7` and changing nothing else reproduces both v2.1.7 digests
exactly, so the movement is the version string and not the arithmetic. Every
GEX number, bucket, wall, node, void, zero-gamma root and confidence component
in the reference case is unchanged.

The three fingerprints that *did* change behaviourally --
`pipeline_fingerprint`, `capture_plan.fingerprint` and
`normalization_recipe.rules_fingerprint` -- are not frozen values. They are
recomputed from configuration on every run and compared against what a capture
was stamped with, which is the point of them.

### Behavioural changes worth knowing

* `compute_trusted_gex` no longer takes `spot_provenance`. It takes
  `open_interest_provenance`, `open_interest_as_of_evidence` and
  `expected_universe`, and derives the spot itself.
* **An expected contract universe must be declared on `capture_session`.**
  Supplying one only at calculation time is refused, and so is dropping one the
  capture declared. A universe produced after the responses arrived can be
  shaped to fit whatever arrived.
* `max_spot_skew_seconds` is a `ThetaDataConfig` field (default `1.0`). It
  enters the pipeline fingerprint, so widening it is a configuration change that
  every previously stamped record disagrees with.
* Documentation evidence for a settlement date now resolves through
  `DOCUMENTATION_RULES`, which is **deliberately empty in production**. This
  repository has read no ThetaData document establishing an open-interest
  settlement convention (OD-26), and pre-populating the registry with a
  plausible-looking entry would be the defect this closes.
* `NormalizationRecipe.rules_fingerprint` now excludes
  `expected_universe_fingerprint` alongside `as_of` and `open_interest_as_of`.
  All three belong to one operation rather than to the standing configuration.
* Captures written by v2.1.7 have no operation stamp and are refused rather than
  given a timestamp this process invented.

## 2.1.7 - normalized-evidence binding

v2.1.6 bound a trusted calculation to *verified raw records*. v2.1.7 binds it to
the **chain those records normalize to**.

Those are different objects, and nothing connected them. The chain is the result
of parsing and joining the records; verification proved a great deal about the
bytes and asked nothing about the `ChainSnapshot` a caller then handed in. So:

```python
chain = pipeline.fetch_chain(...)          # honest
tampered = dataclasses.replace(chain, quotes=(edited, *chain.quotes[1:]))
pipeline.compute_trusted_gex(tampered, context=real_context)   # trusted=True
```

Adding 999,999 to one strike's open interest moved the unsigned total by about
two orders of magnitude. Open interest is the linear weight on every GEX term.
The result carried a verified manifest and `trusted=True`.

**Status:** `IMPLEMENTED` | `TESTED_SYNTHETICALLY` |
`TESTED_WITH_OFFLINE_FIXTURES` | `READY_FOR_RAW_CAPTURE_ONLY` |
`NOT_VALIDATED_WITH_LIVE_THETADATA`.

The repository remains incapable of placing an order.

### Defects fixed

| S | Defect in v2.1.6 | Why it mattered | Fix |
|---|---|---|---|
| 1 | The normalized chain was never checked against its records | Any calculation-relevant field could be edited after an honest fetch and the result stayed `trusted=True` | `NormalizationRecipe`, `canonical_chain_hash` over every calculation-relevant field, and `rebuild_chain_from_capture` replaying the stored bytes through the ordinary fetch path |
| 2 | `compute_trusted_gex` accepted a derived verdict | `VerifiedCalculationContext` is public and its `context_hash` recomputable, so an edited context with a fresh hash was internally consistent and said whatever the caller wanted | The trusted API takes manifest, store and provenance, and derives verification, validation, compatibility and re-derivation itself. The context is what it *returns* |
| 3 | `pricing_observations` was outside validator equivalence | Relabelling an observed `MIXED_ACROSS_CHAIN` as agreement survived re-derivation, because re-derivation never compared it | Observations are in `semantic_payload`, sorted, prose excluded |
| 4 | A *failed* check could still revise a dimension | The loop never read `check.passed` | An observation is admitted only behind a passing, dimension-matching, manifest-matching, record-verified, settled check that is in the semantic payload |
| 5 | An OI *value* observation graded an OI *date* as OBSERVED | An OI response carries a number and no settlement date; confirming the number said nothing about the session, and open interest is the weight on every term | `OpenInterestValueObservation` and `OpenInterestAsOfEvidence` with an `EvidenceKind`. `CALLER_ASSUMPTION` permits raw capture and diagnostics, never a trusted calculation |
| 6 | Only the manifest carried the pipeline fingerprint | Relabelling a capture as another pipeline's was one field on a document the evidence could not contradict | `CaptureIdentity` stamped on every record at capture time: pipeline, plan, request specification, normalization recipe, session |
| 7 | The request that produced a capture was never checked | `rate_value` reaches the vendor and changes the greeks it returns | A canonical `RequestSpec` per endpoint, recomputed at verification and compared against each record's stored parameters |
| 8 | `capture_origin` was a mutable field on the manifest | Relabelling an offline fixture as `LIVE_HTTP_CAPTURE` was one assignment | Derived from the records; a contradicting declaration or a mixed capture fails verification |
| 9 | `capture_origin_of` read the outermost transport | `build_thetadata_client` always wraps in `RetryingTransport`, so **every real capture** would have been stamped `UNKNOWN_ORIGIN` | The wrapper delegates |
| 10 | The hand-written US Eastern zone ignored `fold` | 01:30 on the fall-back Sunday came back as `02:30-05:00`: an hour wrong on an instant IANA has always had right | `zoneinfo.ZoneInfo("America/New_York")` with `tzdata` pinned |
| 11 | Parameter hashes were truncated to 16 hex characters | Sixty-four bits is a lot for an accident and little for an audit identity | Full SHA-256 for every audit identity; short forms only in filenames |

### Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f` | unchanged | -- |
| `EXPECTED_MODEL_FINGERPRINT` | `faf0a9f595f2a93a` | `1b353ba18cefb0a2` | `VERSION_METADATA_ONLY` |
| `EXPECTED_OUTPUT_HASH` | `bd668a62...` | `3af3ef9c...` | `VERSION_METADATA_ONLY` |

Measured, and it mattered more here than usual: v2.1.7 changed the *clock*, and
time-to-expiry drives gamma. Pinning `model_version` back to `gex-engine/2.1.6`
and reverting nothing else reproduces both v2.1.6 digests exactly. The two zone
implementations agree on every instant outside the DST transition windows, and
the reference case is an ordinary March session.

### Behavioural changes worth knowing

* `compute_trusted_gex(chain, context=...)` is gone. It is
  `compute_trusted_gex(chain, manifest=..., store=..., ...)`.
* A **caller-assumed open-interest settlement date now blocks a trusted GEX.**
  This is the honest state of the repository: ThetaData does not publish the
  date (OD-26). Raw capture and diagnostics are unaffected.
* `tzdata` is a runtime dependency. It is data, not code; the bare-interpreter
  guarantee is narrowed from "no third-party packages" to "no third-party code"
  and the CI job checks exactly that.
* Captures written by v2.1.6 have unstamped records and will not verify. They
  are refused rather than reinterpreted, as v2.1.6 manifests were before them.
* `AnalyticalReadiness` is a new, separate axis from `CertificationState`.
  Nothing consumes it yet; the requirements are written down so the gate exists
  before something needs it.

## 2.1.6 - evidence binding and capture readiness

v2.1.5 made the evidence derived. v2.1.6 makes the *authorization* independent
of the thing being authorized.

The pattern this release removes, stated once: **a snapshot was a witness to its
own provenance.** `compute_trusted_gex` decided trust by reading `chain.meta` --
the pipeline fingerprint, the raw-capture manifest, the spot provenance. All
three are metadata the producing code writes into the snapshot, and
`ChainSnapshot` is a public dataclass, so:

```python
dataclasses.replace(
    build_synthetic_chain(),
    meta={"pipeline": {...}, "raw_capture_manifest": {...}, "spot_provenance": {...}},
)
```

satisfied every gate. The chain had never been near a capture.

Trust now requires a `VerifiedCalculationContext`, which only
`build_verified_calculation_context` produces and which re-derives every
verification from the manifest and the raw store. The manifest inside
`chain.meta` remains useful -- it says which bytes to go and look at -- and on
its own it authorizes nothing.

**Status:** `IMPLEMENTED` | `TESTED_SYNTHETICALLY` |
`TESTED_WITH_OFFLINE_FIXTURES` | `READY_FOR_RAW_CAPTURE_ONLY` |
`NOT_VALIDATED_WITH_LIVE_THETADATA`.

The repository remains incapable of placing an order.

### Defects fixed

| S | Defect in v2.1.5 | Why it mattered | Fix |
|---|---|---|---|
| 1 | Trust was decided from `chain.meta` | A synthetic chain with the right keys computed a trusted GEX | `compute_trusted_gex(chain, context=...)` requires independently verified evidence; nine bindings checked, including that the chain's spot equals the verified index print |
| 2 | An empty fingerprint meant "no claim to check" | A manifest that said nothing about its pipeline verified against any pipeline | `verify_capture(..., expected_pipeline_fingerprint=...)`; empty is a failure, not a skip |
| 3 | Only payload hashes were checked, as a *set* | Two records could swap payload hashes undetected | `ManifestRecord` per record; every field bound to its own record id and to the store |
| 4 | The manifest hash covered four sorted lists | Mutating a request id, sequence, status or clock left the digest unchanged | The hash covers sorted per-record semantic descriptors |
| 5 | `InMemoryRawStore` could be capture-ready | A paid session's only copy of the evidence would not survive the process | `StoreDurability`; readiness also requires clean integrity, a write/read probe, a location outside the source tree, free space and append-only behaviour |
| 6 | Validated observations never reached the gate | A capture could observe the vendor's underlying source and the report still read `UNKNOWN` -- and a *disagreement* could not block anything | `derive_post_capture_compatibility`; a live mismatch overrides a documented match, a live match only fills an unknown |
| 7 | Chain conventions were read from row zero | One agreeing contract characterised every strike | Every row of every relevant record, with `ChainCoverage`: rows, records, matches, mismatches, missing, non-finite, coverage ratio, distinct values, maximum deviation |
| 8 | Two timestamp interpretations | The adapter localised a naive vendor string to Eastern while the validator read it as UTC -- four hours apart, in the module whose job is to catch disagreements | `src/domain/vendor_time.py`, used by normalization, observation, validation, spot sync and replay |
| 9 | `live_capture` was a hardcoded `False` | Correct then, and it would have stayed correct-looking through the first real session | `CaptureOrigin` stamped by the transport onto each record, in the manifest hash; `live_capture` is derived |
| 10 | Provenance leaked untyped errors | `SpotProvenance(timestamp="...")` raised `AttributeError` from a provenance constructor | Exact types required; `datetime` is refused where a `date` belongs; `observed_at` is parsed as a real ISO date and stored as `observed_on` |
| 11 | The health probe wrote into the capture namespace | Checking an append-only store permanently added to it | A sibling health directory plus a scratch write in the capture root; neither enters the index or consumes a request sequence |
| 12 | `validate_metadata` checked presence, not type | A string HTTP status or a negative sequence loaded | Types, ranges, tz-awareness, ordering and parser support all checked |

### Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f` | unchanged | -- |
| `EXPECTED_MODEL_FINGERPRINT` | `d3d458592b6f87e0` | `faf0a9f595f2a93a` | `VERSION_METADATA_ONLY` |
| `EXPECTED_OUTPUT_HASH` | `568d2c2d...` | `bd668a62...` | `VERSION_METADATA_ONLY` |

**No GEX number changed**, and this time it was measured rather than argued:
recomputing the reference case with `model_version` pinned back to
`gex-engine/2.1.5`, and nothing else reverted, reproduces both v2.1.5 digests
exactly. The version string is the whole of both moves.

### Behavioural changes worth knowing

* `compute_trusted_gex(chain)` no longer exists. It is
  `compute_trusted_gex(chain, context=build_verified_calculation_context(...))`.
* `verify_capture` requires `expected_pipeline_fingerprint` and a `plan`.
  Omitting either produces a failed verification rather than a passing one.
* Manifests written by v2.1.5 are **refused**, not reinterpreted: the schema
  version is checked and the old parallel arrays cannot express the per-record
  binding this release verifies.
* `InMemoryRawStore` stays fully supported for unit tests and offline fixtures.
  It cannot reach `READY_FOR_RAW_CAPTURE_ONLY`.
* The ambiguous hour of the autumn DST transition is resolved by a recorded
  `ambiguity_resolution`, not by `datetime.fold`. `src/gex/sessions.USEastern` is
  hand-written -- there is no `tzdata` wheel on every machine this runs on -- and
  it deliberately ignores `fold`. See OPEN_DECISIONS OD-2.

## 2.1.5 - evidence integrity and calculation trust

v2.1.4 made the evidence typed. v2.1.5 makes it *derived*.

The pattern this release removes, stated once: **an object whose presence was
the answer.** Each of these was a public dataclass that the production path
accepted, and none of them had to have come from the code that checks anything.

```python
CaptureVerification(confirmed_record_ids=("fake",), failures=())   # verified
ValidationCheck(name="anything", passed=True)                      # validated
PricingAssumptionAttestation(dimension=DAY_COUNT, evidence=...)     # MATCHED
```

The third is the clearest. It carried a ``vendor_value`` field, and nothing read
it: recording that the vendor uses ACT/360 while the local model uses ACT/365F
produced ``MATCHED``. Observing a disagreement is the thing evidence most needs
to be able to express, and it was the one thing it could not say.

Alongside those, the calculation had no gate at all. ``pipeline.compute_gex()``
called the engine -- with six load-bearing dimensions ``UNKNOWN``, on a chain
from another pipeline, with no capture behind it -- and the number that came out
was indistinguishable from one computed under settled assumptions.

**Status:** `IMPLEMENTED` | `TESTED_SYNTHETICALLY` |
`TESTED_WITH_OFFLINE_FIXTURES` | `READY_FOR_RAW_CAPTURE_ONLY` |
`NOT_VALIDATED_WITH_LIVE_THETADATA`.

The repository remains incapable of placing an order.

### Defects fixed

| S | Defect in v2.1.4 | Why it mattered | Fix |
|---|---|---|---|
| 1 | `compute_gex()` consulted nothing | A number computed under six unknowns looked like one computed under none | `compute_diagnostic_gex` / `compute_trusted_gex`; the trusted one refuses unless pricing, fingerprints, capture and provenance all hold |
| 2 | `assess_readiness(capture=...)` took a verdict | A hand-built `CaptureVerification` advanced the state machine | Takes a manifest and a store; runs the verifier itself |
| 3 | Any non-empty passing check set was a validation | `ValidationCheck(name="anything", passed=True)` certified | `AdapterValidator.validate` derives the report; readiness re-derives and compares |
| 4 | `LIVE_COMPARISON` was writable in YAML | A file claimed a comparison that had not been run | Refused in the loader *and* in the config object; only the validator emits it |
| 5 | Evidence set `MATCHED` directly | ACT/360 against ACT/365F was agreement | `VendorObservation` carries the observed value; per-dimension comparators derive the status |
| 6 | Pricing evidence was independent of validation | A static attestation counted as live-observed | Only a bound, passing validation check naming the dimension counts |
| 7 | `ProvenanceEvidence` proved a record id existed | A Greeks response was evidence about open interest | `VerifiedFieldObservation` re-reads the payload: endpoint, field, value, hash |
| 8 | `verify_capture` never asked whether the manifest claimed *enough* | A one-record capture certified | A `CapturePlan` derived from mode, policy, underlying and tier; every required endpoint must be present |
| 9 | `fetch_chain(spot=...)` took the caller's number | Every gamma is computed against it | The vendor index snapshot is fetched inside the same capture session and read back from the stored payload |
| 10 | `raw_store=object()` skipped the integrity check | A store that could not store anything passed | `probe_raw_store`: protocol, integrity, write, read-back |
| 11 | `rate_units` and `dividend_convention` were "local" | We do not send them; how the vendor reads 4.2 is its API's business | Both are vendor-owned; a local YAML entry cannot settle them |
| 12 | Provenance accepted naive datetimes, NaN tolerances, future dates | A tolerance of NaN compares true against every skew | Strict `__post_init__` on both provenance types, with source enums |
| 13 | `OptionContract` held only a float strike | Two strikes differing below double precision became one contract | `strike_decimal` carried alongside; identity, keys and dedup use it |
| 14 | `from_session` took the whole session | A second chain pull inherited the first's records | `CaptureSession.mark()` and `from_session(since=...)` |
| 15 | Derived reports were read, never recomputed | `dataclasses.replace(pipeline, pricing_compatibility=...)` bypassed every check | `validate_integrity()`, called before fetch, calculation and readiness |
| 16 | Public certification raised bare `TypeError` | "The adapter refused" meant enumerating builtins | `ThetaDataCertificationError` / `ProvenanceError` / `ValidationError` |

### Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f` | unchanged | -- |
| `EXPECTED_MODEL_FINGERPRINT` | `70b3afda56f505e7` | `d3d458592b6f87e0` | `VERSION_METADATA_ONLY` |
| `EXPECTED_OUTPUT_HASH` | `89f38199...` | `568d2c2d...` | `VERSION_METADATA_ONLY` |

**No GEX number changed.** Across the release exactly two assertions in the
regression file moved, both driven by `MODEL_VERSION`. Five v2.1.5 changes could
plausibly have reached the output hash; four provably do not, checked by
searching the serialised payload rather than reasoned about. The note in
`tests/regression/test_frozen_reference_case.py` records each.

### Behavioural changes worth knowing

* `pipeline.compute_gex()` and `capture_and_compute()` are gone. Use
  `compute_diagnostic_gex` (always untrusted) or `compute_trusted_gex` (refuses
  under unresolved assumptions).
* `pipeline.fetch_chain()` takes no `spot`. Under `vendor_index_snapshot` it
  fetches the index itself; for an external spot use
  `fetch_chain_with_external_spot`, whose snapshots cannot be trusted.
* `assess_readiness` takes `manifest=` and `raw_store=` instead of `capture=`.
* `rate_units` and `dividend_convention` no longer resolve from configuration.
  A previously "compatible" profile now reports two more unknowns, correctly.
* A `pricing_attestations` entry needs a `vendor_value`, and its `reference`
  must resolve to a file in the repository or a URL.
* `OptionContract.key` carries the canonical strike string, not a float.

### Not added, deliberately

Real ThetaData requests, Databento, MES/ES futures feeds, feature-store work,
trading strategies, regime thresholds, a risk engine, position sizing, IBKR,
broker execution, order classes, paper trading, live trading, and arbitrary
calibrated trading values.

## 2.1.4 - certification state and provenance integrity

v2.1.3 built the machinery for deciding whether vendor numbers may be trusted.
v2.1.4 is about the machinery being *checkable*. Three things ran through it:

**Evidence was untyped, so it could be fabricated by accident.**
`assess_readiness(capture_manifest=object(), validation_report=object())`
returned `ADAPTER_CERTIFIED`. Both parameters were `Any` and both were tested
with `is not None`, so the strongest claim in the repository was two truthy
values away. Provenance had the same shape one level down: a `caller_supplied`
boolean is the caller describing its own confidence.

**Decisions were made out of prose.** Compatibility findings were stored as
sentences and the load-bearing ones were identified by searching those sentences
for a field name. Rewording a message turned a blocker into a warning, and it
also moved the replay digest of a calculation that had not changed.

**Two questions shared one enum, and one ladder.** `VENDOR_GAMMA_VALIDATION` was
a third `PricingMode`, so asking to compare the vendor's gamma moved a session
*out of* `VENDOR_IV_LOCAL_GAMMA` -- and out of the vendor-IV checks it still
needed, because vendor IV still fed the local gamma. Separately, capture
readiness and calculation trust shared a state ladder, so an unresolved vendor
convention blocked the capture that would have resolved it.

**Status:** `IMPLEMENTED` | `TESTED_SYNTHETICALLY` |
`TESTED_WITH_OFFLINE_FIXTURES` | `READY_FOR_RAW_CAPTURE_ONLY` |
`NOT_VALIDATED_WITH_LIVE_THETADATA`.

The repository remains incapable of placing an order.

### Defects fixed

| S | Defect in v2.1.3 | Why it mattered | Fix |
|---|---|---|---|
| 1 | `VENDOR_GAMMA_VALIDATION` was a third `PricingMode` | Selecting it skipped the vendor-IV compatibility checks while vendor IV still fed the local gamma | `IvGammaPricingMode` and `VendorGammaPolicy` are separate fields; the assessment runs on the IV question regardless of the gamma policy |
| 2 | Compatibility was prose, and load-bearing was decided by substring | Rewording a message flipped a blocker to a warning, and moved the replay hash | Typed `PricingDimension` / `CompatibilityStatus` / `PricingDimensionResult`; `compatible` is derived and `hard_failures` is honoured |
| 3 | One ladder for capture readiness and calculation trust | An unresolved vendor convention blocked the capture that would resolve it | Six states; unknown pricing permits a raw capture and never a trusted calculation |
| 4 | `capture_manifest: Any`, tested `is not None` | `object()` counted as a capture | `verify_capture` checks the manifest against the store; `assess_readiness` rejects anything else outright |
| 5 | `validation_report: Any`, tested `is not None` | A report about a different session counted the same as one about this one | `AdapterValidationReport` bound to a `manifest_hash`, with checks that can fail |
| 6 | Raw capture was optional for capture readiness | `READY_FOR_CAPTURE_ONLY` with capture disabled is the one thing it was not ready for | Capture enabled, a path, and a healthy store are all required |
| 7 | Four optional steps to compute from a session, plus `request=` | Omitting `pipeline=pipeline` produced a plausible snapshot missing its provenance; `request=` could fetch something other than what was assessed | `pipeline.fetch_chain()`, `.compute_gex()`, `.capture_and_compute()`; no request or model-parameter overrides |
| 8 | Nothing stopped a ThetaData profile carrying synthetic provenance | Real vendor gammas against an underlying labelled invented | `config/thetadata_capture.yaml`, and a loader rule that refuses the combination |
| 9 | `ThetaDataConfigError` was a bare `ValueError` | `except ThetaDataError` caught the runtime failures and missed the configuration ones | Configuration errors join the hierarchy, keeping `ValueError` as a second base |
| 10 | `ThetaDataConfig()` constructed with four fields at `None` | `as_dict()` raised `AttributeError` from inside the audit trail | Valid by construction: derived fields resolved and coherence checked in `__post_init__` |
| 11 | `contract_identity` went `Decimal -> float -> .4f` | Two formatters that agreed on tested strikes, not by construction | Both sides call `canonical_strike`; `SPXW:2026-03-20:4900:call` |
| 12 | Prose entered the replay hash | A documentation edit moved a digest; a changed finding with the same wording did not | Prose keys stripped from metadata before hashing; semantic payloads carry codes, statuses and values |
| 13 | CI `push` triggered on `main` | The repository's branch is `master`, so no job had ever run on a push | Triggers on `master` and `main`, plus `workflow_dispatch` |
| 14 | Provenance carried caller-set booleans | The caller asserting it had observed something is not an observation | `ProvenanceGrade` PLANNED / OBSERVED / VALIDATED, derived from a `ProvenanceEvidence` naming a stored record |

### Found by reviewing this release, before it shipped

Seven defects in the v2.1.4 work itself, five of them in the very machinery
meant to close v2.1.3's bypass. Each was reproduced before being fixed.

| Defect | Why it mattered | Fix |
|---|---|---|
| `ProvenanceEvidence` was checked for being well-formed, never for being true | Evidence naming a record that does not exist, in a session that never happened, graded `VALIDATED` and reached `ADAPTER_CERTIFIED` — the v2.1.3 defect one type-level down | `grade_claim` compares the record id and manifest hash against the verified capture; a claim that does not hold is a calculation blocker, not a soft "not yet observed" |
| `LOCAL_CONFIGURATION` evidence settled any dimension | Seven attestations in a YAML file reached `ADAPTER_CERTIFIED` with no comparison run | `PricingDimension.vendor_owned`; local evidence on a vendor-owned dimension is a hard failure, and the certification ladder mirrors the rule |
| `ZERO_DIVIDEND` returned `BOTH_ZERO` without reading either value | `annual_dividend: 3.5` is sent to the vendor, so its IV was solved under q=3.5 against a local q=0.0 — and the report recorded the vendor's dividend as 0.0 | The values are compared; a non-zero under a zero convention is `MISMATCHED` |
| Provenance grades were computed before the validation report was checked for binding | A report describing a different manifest still promoted grades to `VALIDATED`, beside a blocker saying there was no capture to bind to | Binding is settled first |
| `verify_capture` tested payload-hash *membership* | One retry written under two ids satisfied two distinct manifest claims | The confirmed hashes must pair with the claimed ones exactly |
| `canonical_strike` raised `InvalidOperation` above ~1e28, and spelled `NaN` | `canonical_id` became a property that throws inside chain parsing; a NaN identity compares unequal to itself | Non-finite refused with `StrikeError`; the decimal context is widened for large integral values |
| `ThetaDataConfig()` bypassed the IV-source and legacy-mode guards | `ThetaDataConfig(iv_source=TRADE_IV)` constructed and then priced against a source with no implementation; a stored v2.1.3 dict raised an opaque `ValueError` | Both guards run in `__post_init__`, and `require_coherent_pricing_mode` now checks both directions |

### Additional defects found

* **An explicitly zero dividend derived a spec that called itself a continuous
  yield.** `to_model_spec` mapped `annual_dividend: 0.0` to
  `DividendSource.CONFIGURED_CONSTANT`, so a config saying `ZERO_DIVIDEND`
  produced a model saying something else and the compatibility check reported a
  convention mismatch. Invisible in v2.1.3 because its tests replaced the
  finished report with `compatible=True` instead of deriving it -- the bypass
  was hiding a real derivation bug.
* **`ModeCapability` / `MODE_CAPABILITIES`** was a table restating what an enum
  already said, and could drift from it. Folded into
  `VendorGammaPolicy.aggregates_vendor_gamma`.
* **`config/paper.yaml` and `config/live.yaml`** both set
  `options_source: thetadata` with raw capture off. They cannot run, but they
  are templates, and a template is copied.

### Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172bfee2682f` | unchanged | -- |
| `EXPECTED_MODEL_FINGERPRINT` | `e05c611b9b953372` | `70b3afda56f505e7` | `VERSION_METADATA_ONLY` -- engine 2.1.3 -> 2.1.4 |
| `EXPECTED_OUTPUT_HASH` | `4444055b...` | `89f38199...` | `VERSION_METADATA_ONLY` -- the same version string, twice |

**No GEX number changed.** Across the whole release exactly two assertions in
the regression file moved, and both are driven by `MODEL_VERSION`. The other
three v2.1.4 changes that could plausibly have reached the output hash were
checked against the serialised payload rather than reasoned about, and none of
them touch it: the parser version is absent (the reference case is synthetic),
identity *strings* are absent (the payload carries counts), and this snapshot's
metadata contains no prose key to strip.

### Behavioural changes worth knowing

* `pricing_mode: VENDOR_GAMMA_VALIDATION` is **refused**, not translated. The
  replacement is `pricing_mode: VENDOR_IV_LOCAL_GAMMA` plus
  `vendor_gamma_policy: COMPARE_ONLY` -- and the compatibility checks that the
  old value skipped now run, so the same session may be refused a calculation.
* The default configuration is `READY_FOR_RAW_CAPTURE_ONLY`, not `NOT_READY`.
  Six load-bearing pricing unknowns still block any calculation.
* A profile with `data.options_source: thetadata` must name a real underlying
  and must enable raw capture.
* `ThetaDataRuntime.fetch_chain` no longer accepts `request=`.
* Contract identities are spelled `4900`, not `4900.0000`.
* `assess_readiness` takes `capture=` and `validation=` (typed) in place of
  `capture_manifest=` and `validation_report=` (`Any`).

### Not added, deliberately

Live ThetaData collection, Databento, MES/ES futures feeds, feature-store work,
trading strategies, regime thresholds, a risk engine, position sizing, IBKR,
broker execution, order classes, paper trading, live trading, and arbitrary
calibrated trading values.

## 2.1.3 - pricing provenance

One misconception ran through v2.1.2 and produced most of what this release
fixes: **ThetaData's NBBO bid/mid/ask IV values are vendor-computed IV.**

ThetaData solves the implied volatility. That the option *price* it solved
against was an NBBO bid, midpoint or ask says nothing about who did the solving,
or under what rate, dividend, expiration instant and day count. v2.1.2 read
"NBBO_MID_IV" as though the NBBO part made it ours, and shipped a default
configuration that paired vendor-computed IV with `LOCAL_IV_LOCAL_GAMMA` -- the
one pricing mode that requires *no* vendor/local agreement.

Everything downstream followed. The compatibility assessment short-circuited on
the mode, so every vendor convention went unchecked. Adapter certification read
the same short-circuit and reported ready. A session could fetch under one set
of assumptions and price under another, and every object involved looked
correctly configured in isolation.

**Status:** `IMPLEMENTED` | `TESTED_SYNTHETICALLY` |
`TESTED_WITH_OFFLINE_FIXTURES` | `READY_FOR_CAPTURE_ONLY` |
`NOT_VALIDATED_WITH_LIVE_THETADATA`.

The repository remains incapable of placing an order.

### Defects fixed

| S | Defect in v2.1.2 | Why it mattered | Fix |
|---|---|---|---|
| 1 | `iv_source: VENDOR_DEFAULT_IV` with `pricing_mode: LOCAL_IV_LOCAL_GAMMA` | The mode that skips every compatibility check was the default, on a configuration that needed them all | Mode derived from IV provenance; `LOCAL_IV_LOCAL_GAMMA` unreachable until a local solver exists |
| 2 | Certification read the mode enum, not the effective assumptions | A mislabelled session was reported ready with six vendor conventions unknown | Load-bearing `UNKNOWN` fields block; `compatible=True` cannot bypass an unresolved field list |
| 3 | `from_config` took only `thetadata:` and derived its own `ModelSpec` | The repo's own `research.yaml` set `model.iv_price_source: NBBO_MID_IV` beside a section defaulting to `VENDOR_DEFAULT_IV` | `from_loaded_config` builds one session from the whole file and refuses a mismatch |
| 4 | Rate compatibility ignored units and treated a null vendor value as agreement | A vendor 4.2 matching a local 4.2 is the *bug* if the vendor's is a percentage | Typed `RateAssumption` with source, raw value, unit and normalised decimal; a vendor default is `UNKNOWN_VENDOR_DEFAULT` |
| 5 | Dividend compatibility compared conventions and stopped | Two continuous yields of 0.02 and 0.01 are the same kind and different numbers | `DividendAssumption` compares convention *and* value |
| 6 | `prefer_vendor_gamma` was independent of the pricing mode | A session could claim comparison-only and aggregate the vendor's gamma | Mode capability table; no supported mode aggregates vendor gamma |
| 7 | No tier capability check | Selecting vendor-gamma validation on Standard fails at the first paid request | Capability matrix; `UNCERTAIN` counts against, not for |
| 8 | Confidence recomputed `len(result.contracts) / expected_contract_count` | Two received where two were expected scored 1.0 whichever two they were | The scorer reads the identity measure |
| 9 | `compute_gex_snapshot(expected_contract_count=int)` | A count cannot say *which* contracts were expected | Typed `ExpectedContractUniverse` of `ContractIdentity` |
| 10 | Per-source shortfalls used count arithmetic | Two missing and two unexpected net to zero | Identity set differences |
| 11 | `GexSnapshot.effective_model` took `contracts[0]` | Iteration order decided what the snapshot claimed about itself | `None` unless the distribution proves uniformity |
| 12 | Pipeline compatibility never reached the output | A GEX number could not show what permitted it | Pipeline metadata into `ChainSnapshot`, `GexSnapshot` and the replay hash |
| 13 | Raw payloads and normalized chains were unlinked | Reconstructing which bytes produced which number meant guessing from filenames | `RawCaptureManifest` with a deterministic hash |
| 14 | Parser stuck at 2.1.1, engine at 2.1.2 | A replay could not detect that the parser had changed underneath it | Both at 2.1.3, from one constant each |
| 15 | `ResponseTooLargeError(RuntimeError)` | The failure most likely to follow a deliberate setting escaped `except ThetaDataError` | `ThetaDataResponseTooLargeError` |
| 16 | BOM stripped for validation but not for parsing | A BOM'd response validated and then lost its first column | One `normalize_response_body`, used by both |
| 17 | Static completeness never read `source.is_available` | Selecting `VENDOR_SOFR` reported a fully specified model | Unimplemented sources are statically missing |
| 18 | `zero_gamma_root_identity_stable` compared counts | Same count at different levels read as identity-stable | Renamed `zero_gamma_root_count_stable`; `match_roots` is the identity measure |
| 19 | Safety test substring-matched `dir()` | Python 3.13's `__replace__` contains "place", so the check failed on a supported interpreter | Inspects the declared API, not runtime attributes |
| 20 | Expected universes were raw strings | Formatting alone could manufacture a missing identity | `contract_identity` shares the chain's exact strike normalisation |
| 23 | One `ready` boolean | "Ready to capture" and "certified" are different claims | Four-state machine; `ADAPTER_CERTIFIED` unreachable without a capture *and* a validation report |

### Additional defects found

* **`config/research.yaml` was internally inconsistent** -- the shipped file set
  two different implied volatilities. Found by writing §3's factory, not by
  reading the file.
* **`ChainCompleteness` lived in the ThetaData adapter**, so the engine core
  could not read it without violating dependency isolation -- which is *why*
  v2.1.2's scorer rebuilt a ratio from counts. Moved to `src/domain/`.
* **Strike parsing lived in the adapter** for the same reason. Moved to
  `src/domain/strikes.py`; identity is not a vendor's idea.
* **A second `dir()` substring scan** in `test_research_pipeline.py` shared the
  §19 defect and would have failed on 3.13 too.

### Frozen values

| Value | Before | After | Classification |
|---|---|---|---|
| `EXPECTED_CONFIG_FINGERPRINT` | `8b5b7454ba7c5500` | `ded3172bfee2682f` | `BEHAVIORAL` -- research.yaml changed |
| `EXPECTED_MODEL_FINGERPRINT` | `d367d4d4aabbbb69` | `e05c611b9b953372` | `VERSION_METADATA_ONLY` -- engine 2.1.2 -> 2.1.3 |
| `EXPECTED_OUTPUT_HASH` | `35def8d5...` | `4444055b...` | mixed; see the note in the test file |

**No GEX number changed.** Totals, buckets, per-strike values, walls, voids,
roots and every confidence component score are asserted individually and held
throughout: across the whole release exactly three assertions in the regression
file moved, and all three are the digests above.

### Behavioural changes worth knowing

* `LOCAL_IV_LOCAL_GAMMA` now fails configuration. It is documented, unreachable,
  and will stay so until a local IV solver exists.
* The default configuration is `NOT_READY` for certification, on six
  load-bearing unknowns. That is the correct answer to what we currently know.
* `compute_gex_snapshot` no longer accepts `expected_contract_count`.
* `zero_gamma_root_identity_stable` is now `zero_gamma_root_count_stable`.
* Standard tier cannot select `VENDOR_GAMMA_VALIDATION`; Value tier cannot use
  vendor IV.

### Not added, deliberately

Live ThetaData collection, Databento, MES/ES futures feeds, feature-store work,
trading strategies, regime thresholds, a risk engine, position sizing, IBKR,
broker execution, order classes, paper trading, live trading, and arbitrary
calibrated trading values.

---

## 2.1.2 - adapter-certification readiness

Twenty defects cleared before any paid ThetaData capture. Every one shares a
shape: **something that looked wired up, was not**, and the gap was invisible
because each half was individually valid.

Counts stood in for identities. Two objects configured separately were never
compared. A cap was enforced at the layer above the one that reads bytes. A
session id derived its uniqueness from a timestamp that repeats. A scanner
resolved a path from the metadata it existed to validate.

**Status:** `IMPLEMENTED` | `TESTED_SYNTHETICALLY` |
`TESTED_WITH_OFFLINE_FIXTURES` | `READY_FOR_ADAPTER_CERTIFICATION` |
`NOT_VALIDATED_WITH_LIVE_THETADATA`.

The repository remains incapable of placing an order.

### Defects fixed

| S | Defect in v2.1.1 | Why it mattered | Fix |
|---|---|---|---|
| 1 | Completeness compared `joined_count / expected_count` | Two received where two were expected scored `MEASURED_COMPLETE` regardless of *which* two -- two missing and two unexpected cancel exactly | Identity set differences; `MEASURED_COMPLETE_WITH_EXTRAS`; missing/unexpected identity lists, sorted and bounded |
| 2 | `ThetaDataRuntime.iv_source` and `ModelSpec.iv_price_source` were never compared | A session could fetch NBBO-mid IV and price with the vendor default, both objects looking correct | `ThetaDataResearchPipeline.from_config` builds both and refuses a mismatch |
| 3 | Vendor IV fed straight into local gamma | Possessing the number is not evidence it was produced our way | `PricingCompatibilityReport`; `DividendConvention`; `VendorRateUnits`; five undocumented dimensions reported `UNKNOWN` |
| 4 | `model_fingerprint` reported one model per chain | Per-contract IV fallback yields several; which one was reported depended on iteration order | `ModelDistribution` with per-source counts; `effective_model_uniformity` component; optional strict mode |
| 5 | Only `bid`/`ask` used the structured float parser | A malformed vendor gamma became `None`, indistinguishable from absent, and silently triggered fallback | Every vendor float structured; `VENDOR_GAMMA_MALFORMED`/`NON_FINITE`/`MISSING` told apart |
| 6 | `max_response_bytes` reached only `RetryingTransport` | The cap governed a check *after* the body was in memory, not the streaming read | `httpx_transport_kwargs()` -- one authoritative limit reaching `HttpxTransport` |
| 7 | Capture session ids derived from market `as_of` | Two fetches at one market instant collided in an append-only store | `new_capture_session_id()` -- nonce for uniqueness, market time as audit metadata |
| 8 | `model_parameter_completeness` read only surviving contracts | An empty result set reported a fully specified model, going quiet exactly when asked "why did nothing survive?" | Static configuration completeness, evaluated without reference to any contract |
| 9 | `MODEL_VERSION` still `gex-engine/2.1.0` | Two releases of numerics changes that a replay could not detect | `gex-engine/2.1.2`, one constant, in the model fingerprint |
| 10 | `TRADE_IV` / `LOCALLY_SOLVED_MID_IV` accepted, unimplemented | Fell through to the vendor default, so the operator got an IV they had not chosen | Refused at configuration load with the supported set named |
| 11 | Integrity scanner resolved a path before validating metadata | Malformed metadata crashed the scanner that exists to report malformed metadata | `validate_metadata()` first; `UNSAFE_RECORD_ID`, `INVALID_BYTE_LENGTH`, `INVALID_HASH`, `INVALID_TIMESTAMP` |
| 12 | `base_url` checked for scheme and netloc only | `http://user:secret@host` put a credential in every logged URL; `raw_capture_path` was `str()`-converted, so `42` became a directory | Userinfo, query and fragment refused; path must be a string or `Path` |
| 13 | `rate_type: null` replaced with `"sofr"` when building the client | Stored config and outgoing request disagreed, and only the request was true | Null means omit; `rate_type_policy()` states it |
| 14 | Replay hashing excluded warnings entirely | A snapshot that began reporting a new condition hashed identically to one that did not | Deterministic codes hashed; prose still excluded |
| 15 | Any 200 body went to `parse_csv` | An HTML error page parses to zero rows, and zero rows is legitimate -- so an error page became an empty chain | `validate_csv_body()` with five outcomes |
| 16 | `float(row["strike"])` built the contract identity | `"NaN"` produced an identity unequal to itself; `"5000"` vs `"5000.00"` agreed by luck of formatting | `Decimal` parsing and one canonical spelling |
| 17 | Provenance recorded sources *inspected*, not *selected* | A chain with aware quotes and naive greeks reported both, and never said which supplied a given contract's IV clock | Per-contract `selected_timestamp_sources` |
| 18 | Four unrelated exception bases across four layers | `except ThetaDataError` caught roughly half the ways an adapter can fail | `src/adapters/errors.py`; every failure wrapped; secrets redacted |
| 19 | OI date and spot skew unrecorded | A number whose date we chose is not evidence about the date | `OpenInterestProvenance`, `SpotProvenance` with tolerance |
| 20 | No machine-readable capture readiness | -- | `AdapterCertificationReadiness`; see [ADAPTER_CERTIFICATION.md](ADAPTER_CERTIFICATION.md) |

### Frozen values

The output hash moved three times in this release; each step was verified
independently and is documented in place in
`tests/regression/test_frozen_reference_case.py`.

| Step | Change | Classification |
|---|---|---|
| `181db88a` -> `890bf073` | New confidence component, engine version, distribution metadata | `BEHAVIORAL` |
| `890bf073` -> `9f40dfa9` | Warning codes entered the hash payload | `REPRESENTATIONAL` |
| `9f40dfa9` -> `35def8d5` | Per-contract selected-source provenance added | `REPRESENTATIONAL` |

Also: `EXPECTED_CONFIDENCE_SCORE` 93.6831 -> 93.857 (`BEHAVIORAL`, one component
added at weight 0.03) and `EXPECTED_MODEL_FINGERPRINT` `db8d44db4b51d7c4` ->
`d367d4d4aabbbb69` (`VERSION_METADATA_ONLY`).

**No GEX number changed.** Totals, buckets, per-strike values, walls, voids and
every zero-gamma root are asserted individually and were confirmed unchanged
after each step: after the first, exactly three assertions in the file had
moved; after the second and third, exactly one each.

### Behavioural changes worth knowing

* `ChainCompleteness` takes identity sets, not counts. The count-based
  constructor is gone.
* `ContractKey` carries the canonical *string* strike, not a float.
* A 200 response with a non-CSV body now raises rather than yielding an empty
  chain. `tests/fixtures/vendor/thetadata/empty.csv` became header-only, because
  a zero-byte body is not an empty chain.
* 401/403 raise `ThetaDataAuthenticationError` and 429 raises
  `ThetaDataRateLimitError`; both subclass `ThetaDataHTTPError`.
* An absent vendor gamma is *not* a finding -- that is the whole Standard-tier
  design. Only a second-order record that arrived with an unreadable gamma is.

### Not added, deliberately

Databento, MES/ES futures data, feature-store work, trading strategies, regime
thresholds, a risk engine, position sizing, IBKR, broker execution, paper
trading, live trading, order types, and arbitrary calibrated values.

---

## 2.1.1 — correctness at the layer below

v2.1 fixed a class of defect at the layer where it was first noticed. This
release fixes the seventeen places where the fix was correct there and something
downstream still behaved as though it had not happened.

The recurring shape: **a value was computed correctly and then discarded**.
`ChainCompleteness` worked out that a chain's universe was unknown, and
`assemble_chain` overwrote the answer. `index_rows` selected a canonical row per
identity, and assembly iterated the original list. `_resolve_underlying`
recorded that a spot was missing, and returned one anyway. In each case the
diagnostic was right and the behaviour was unchanged — which is worse than no
diagnostic, because the diagnostic makes it look handled.

**Status:** `IMPLEMENTED` · `TESTED_SYNTHETICALLY` · `TESTED_WITH_OFFLINE_FIXTURES`
· `NOT_VALIDATED_WITH_LIVE_THETADATA`.

Nothing was added toward trading. The repository remains incapable of placing an
order.

### Defects fixed

| § | Defect in v2.1 | Why it mattered | Fix |
|---|---|---|---|
| 1 | `assemble_chain` replaced `expected_contract_count=None` with `len(quote_rows)`, and `score_chain_completeness` fell back to `usable_ratio` | Two layers independently turned "we don't know the universe" into "we got everything"; a truncated chain scored 1.0 for completeness | `CompletenessStatus` carried on the snapshot; `None` stays `None`; unknown scores `None`, uncalibrated, with code `CHAIN_COMPLETENESS_NOT_INDEPENDENTLY_OBSERVED` |
| 2 | `iv_source`, `duplicate_policy`, `max_dte`, `strike_range`, `min_time` were parsed, validated, fingerprinted — and never read | A setting visible in YAML that survives review and never reaches a request | `ThetaDataRuntime.from_config()` as the one construction path; tests assert against outgoing requests, not config objects |
| 3 | `number()` range-checked without `isfinite`; strings and optionals were unvalidated | NaN compares `False` against every bound, so a range check alone passes it | `math.isfinite` before every range check; non-empty string checks; `min_time` grammar; booleans refused as integers |
| 4 | `_resolve_underlying` recorded `UNDERLYING_MISSING` then returned `snapshot.spot`, under a comment saying it deliberately did not | GEX scales by spot², so substituting a different underlying silently reprices the contract | Returns `None`; `has_valid_spot` gates current GEX; new `no_underlying_price` exclusion; per-purpose eligibility |
| 5 | Assembly iterated `inputs.quote_rows` after computing `quote_indexed` | Duplicates were reported as collapsed and assembled twice | Iterates the deduplicated rows, sorted by key so order cannot depend on the vendor |
| 6 | `if spec.risk_free_rate == 0.0: missing.append(...)` | A deliberately configured zero was reported as unspecified; the only way to satisfy the check was to change the number | Completeness reads resolved provenance; realism moved to `MODEL_REALISM_WARNING` |
| 7 | The size cap lived in `RetryingTransport`, which receives an already-buffered body | The cap protected the parser, not the process | `ByteLimitedReader` aborts mid-stream, closes the connection, discards the partial body; retry layer retained as defence in depth |
| 8 | `basic_auth=... if username and password else None` | An unset environment variable produced a working *unauthenticated* client, and the 401 looked like a vendor outage | `MissingCredentialsError` at construction, naming the variables and never the values |
| 9 | `parse_int_field` reached the integer via `float(text)` | Exact only below 2⁵³; `"9007199254740993"` became `...992`, and open interest is exactly where a large integer is plausible | `Decimal` with an exact-integrality check, plus a digit fast path |
| 10 | One chain-wide `localisation_applied`, set from the quote loop | Aware quotes + naive greeks reported "no assumption applied" while assuming a timezone for every greek | `TimestampLocalizationSummary` per `TimestampSource`, in snapshot metadata |
| 11 | `PARSER_VERSION` still `2.0.0` after v2.1 changed parsing three ways | A replay hash that does not move when the parser changes cannot detect that the parser changed | One constant, bumped to `thetadata-v3-parser/2.1.1`, carried into the replay hash |
| 12 | Payload and index writes atomic individually, not together | Nothing could say afterwards which pairs had come apart | `verify_integrity()` classifying eight states; proposes, never deletes |
| 13 | The bare-interpreter test asserted on absolute `sys.modules` | Failed on any host whose `sitecustomize` preloaded NumPy — measuring the machine, not the repository | Static transitive import graph + `-S -E` subprocess + delta measurement |
| 14 | "both $80/mo cheaper *and* internally consistent" | Asserted a numerical agreement that has never been measured | Rewritten to state what follows from the price list and what does not |
| 15 | `_to_float` returned `None` for missing and for `"oops"` | Corruption was indistinguishable from absence, and absence is normal | `FloatParseIssue` with six codes; malformed values recorded on the quote, missing ones not |
| 16 | Nothing checked the HTTP status inside the client | A custom transport returning 500 handed an HTML error page to `parse_csv` | Status checked first, unconditionally, before the body is touched |

### Frozen values

**No frozen hash changed.** `EXPECTED_OUTPUT_HASH` remains
`181db88a7a343eda4d874322161e8b236b57faf93db4282f6e383983260d0b16`.

This is a result, not an oversight. The reference case is built by
`build_synthetic_chain()`, which knows its own universe exactly and therefore
declares `MEASURED_COMPLETE` — so the completeness fix does not perturb it, and
the parser-version and localisation metadata belong to the ThetaData adapter,
which the synthetic path does not use. Every individual numeric assertion was
reviewed and none moved.

One test bound was widened deliberately:
`test_a_broken_snapshot_scores_near_zero` from `< 10.0` to `< 12.0`, because an
explicitly configured zero rate is no longer counted as an unspecified
parameter (§6). Documented in place.

### Behavioural changes worth knowing

* `ConfidenceComponent.score` is now `float | None`. A `None` component is
  excluded from the weighted mean rather than contributing an invented number.
* `EffectiveModelInputs.spot` is now `float | None`.
* `duplicate_policy` accepts `collapse_exact` as an explicit third value; see
  OPEN_DECISIONS OD-19 for why it behaves identically to `reject`.
* The client now raises `ThetaDataVendorError` on any non-2xx status.

### Not added, deliberately

Databento, futures features, trading strategies, regime thresholds, a risk
engine, position sizing, IBKR, broker integration, paper trading, live trading,
order definitions, execution code, and arbitrary calibrated values.

---

## 2.1.0 — correctness and integration hardening

A narrowly-scoped pass over defects found by review of v2. Every fix was
introduced test-first: a failing test that reproduced the defect, then the
smallest correct change, then regression coverage. No financial assumption was
changed silently; where one was ambiguous it went to
[OPEN_DECISIONS.md](OPEN_DECISIONS.md).

Nothing was added toward trading. The repository still cannot place an order.

**Status of this release:** `IMPLEMENTED` and
`TESTED_WITH_OFFLINE_FIXTURES`. `NOT_YET_VALIDATED_WITH_LIVE_VENDOR_DATA` —
no request in this repository has ever reached ThetaData.

### Defects fixed

| # | Defect in v2 | Why it mattered | Fix |
|---|---|---|---|
| 1 | Model inputs were resolved independently in the pricer, the GEX aggregator, the zero-gamma solver and the comparison path | Four code paths could price the same contract differently while each looked correct in isolation | `src/domain/effective_model.py` — one resolver, consumed by all four |
| 2 | `spec.risk_free_rate or snapshot.risk_free_rate` | `0.0` is falsy, so an explicitly configured zero rate silently borrowed the snapshot's rate; the fingerprint recorded the rate the operator asked for, not the one used | Resolution follows the source enum, never truthiness |
| 3 | `CALENDAR_MIDNIGHT` expiration rule was selectable | No listed index option settles at midnight; choosing it produced wrong time-to-expiry for every contract | Declared but `is_supported = False`; resolution refuses it |
| 4 | `underlying_price_source` was declared and ignored | The setting looked applied in review and never reached a calculation | Resolver honours it; unsupported values raise |
| 5 | The `thetadata:` YAML section was validated then discarded | A setting could be present in the file and never reach a request | `src/config/thetadata.py`, typed, with `build_thetadata_client()` as the single construction path |
| 6 | "Effective parameters" conflated requested with sent | A parameter the endpoint does not accept was reported as effective | `VendorParameterSet` splits requested / supported / sent / effective-local / unsupported |
| 7 | Gamma comparison recomputed its own inputs | The comparison could differ from the engine it was auditing | Comparison consumes `contract.effective` |
| 8 | Zero-gamma pooled SPX and SPXW | AM- and PM-settled contracts have different expiration instants; pooling them mixes two surfaces | `zero_gamma_eligible()` separates roots and reports what it excluded |
| 9 | One malformed integer killed the whole chain | A single corrupt cell cost every contract in the response | `parse_int_field` records per-record `parse_issues`; one bad record costs one record |
| 10 | Duplicate rows were resolved positionally | Chain numbers depended on response ordering | `duplicate_policy` defaults to `reject` |
| 11 | Naive datetimes flowed into the maths | A missing timezone silently became a 4–5 hour error in time-to-expiry | `to_eastern()` raises `NaiveTimestampError` |
| 12 | The DST fall-back hour resolved silently | `01:30` occurs twice; the parser picked one without saying so | `parse_vendor_timestamp(fold=...)`, `strict_dst` refuses ambiguity |
| 13 | Snapshot hashing included prose and warnings | Reworded text moved the hash; changed numbers sometimes did not | `hash_payload()` hashes scores and structure, not narration |
| 14 | Root identity was compared by index | Two roots reordering read as two roots changing | `match_roots()` / `compare_root_topology()` |
| 15 | Strike spacing was one global number | SPX is 5-wide near the money and 25-wide in the wings, so gaps were misreported everywhere else | `StrikeLadder` infers spacing from a rolling local median |
| 16 | Callers hand-assembled `ThetaDataClient` | Config drift between call sites | Single factory |
| 17 | `Retry-After` was ignored | The client hammered a server that had told it to wait | Parsed (delta-seconds and HTTP-date), honoured, capped at 120 s |
| 18 | Response size was checked after reading | An oversized payload was fully materialised before rejection | `HttpxTransport` aborts mid-stream |
| 19 | Raw-capture ids were `session-endpoint` | The second request to an endpoint in one session collided, and the store is append-only, so it raised | `build_record_id()` includes sequence and parameter hash; writes are atomic via `mkstemp`/`fsync`/`os.replace` |
| 20 | `expected_contract_count = len(quote_rows)` | Completeness was measured against the response being measured, so a truncated chain scored 100% | `ChainCompleteness` requires an independent expectation and reports `PARTIALLY_OBSERVED` without one |
| 21 | Coverage score saturated at its own floor | A grid that skipped a material contract reported 100% coverage | `0.0 if share < floor else share` |
| 22 | Future-dated open interest aged through the session logic | An impossible timestamp was treated as merely stale | Hard failure before ageing; `latest_open_interest_as_of` added because the chain-level value is the *oldest* |
| 23 | Voids were classified from coverage alone | Exactly-at-threshold coverage with a missing strike read as a true void | Triggers on `missing > 0 or coverage < threshold` |
| 24 | Build tooling was unbounded above | A future setuptools release could change the artefact without a commit here | `setuptools>=68,<86` |

### Added

- `docs/RELEASE.md` — the release procedure of record, with Windows and Unix
  commands and the clean-tree requirement.
- `tests/unit/test_release_integrity.py` — the build is pinned, the archive is
  reproducible and credential-free, and the engine computes a snapshot on a
  bare interpreter (`-S -E`, no site-packages) that agrees with the installed
  run.
- CI jobs: `bare-interpreter` (installs nothing), `reproducible-build` (two
  archives of one commit must be byte-identical), alongside the existing
  `no-trading-guarantee`.

### Frozen values re-derived

Two frozen regression values changed. Each was re-derived only after every
other numerical assertion in the suite was confirmed unchanged, and each is
documented in place in `tests/regression/test_frozen_reference_case.py` with
the reason and whether the change was representational or behavioural.

### Not added, deliberately

Databento, IBKR, order placement, broker adapters, strategies, position sizing,
live or paper execution, regime thresholds, and calibrated constants. See
[MODEL_ASSUMPTIONS.md](MODEL_ASSUMPTIONS.md).

---

## 0.2.0 — corrective engineering pass

A hardening pass over the mathematical engine, ThetaData adapter, timestamp
integrity, confidence model, configuration system, tests and repository quality.
No trading, execution, strategy or calibration work.

### Corrections to v1 claims

These are the statements the v1 documentation made that were wrong or
unsupported. Listed first because they are the most important thing in this
changelog.

| v1 claim | Correction |
|---|---|
| "`ConfidenceScore.calibrated` is enforced by the risk engine, blocking live trading" | **Wrong.** There is no risk engine and no broker. Nothing consumes the flag. It is a research signal; nothing is blocked because nothing can trade. |
| "Standard tier is superior to Pro" | **Unsupported.** Standard is *sufficient*. Whether our gamma matches the vendor's has never been measured. |
| "local gamma matches ThetaData" | **Never validated.** The fixture cross-check compares our pricer against a fixture we generated with our own pricer. |
| `STICKY_DELTA` convention | **Misnamed.** It shifted IV using log-moneyness, which is not sticky-delta. Renamed `STICKY_MONEYNESS`; the real thing is unimplemented and now refuses explicitly. |
| "60-minute floor matches vendor handling" | **Not verified.** Now configurable, with a sensitivity report. |

### Architecture

- Synthetic chain generation moved from `tests/fixtures/` to `src/synthetic/`.
  Production code no longer imports from `tests/`, enforced by an AST-based
  architecture test.
- `src/adapters/fixtures/` renamed `src/adapters/synthetic/`.
- Engine core (`src/gex`, `src/domain`, `src/synthetic`) is stdlib-only, enforced
  both by AST inspection and by importing it in a clean subprocess.
- `pyproject.toml` with pinned dependency ranges, ruff, mypy (strict) and
  coverage configuration. `.gitattributes` for line-ending normalisation.
- CI workflow running lint, format check, type check and the full suite.

### Validation

- New `src/domain/validation.py`: three-way status, machine-readable
  `ValidationCode` enum, bounded example collection, aggregated report.
- `math.isfinite` checks before every numeric comparison. The specific trap
  closed: `NaN < 0` is `False`, so a NaN bid passed a naive negativity check.
- Booleans rejected as numbers (`isinstance(True, int)` is `True` in Python).
- Duplicate contract identities reject **both** copies.
- Chain-level guards: non-finite spot, non-positive spot, naive `as_of`.

### Timestamps

- Per-record clocks: `quote_timestamp`, `greeks_timestamp`, `iv_timestamp`,
  `underlying_timestamp`, `open_interest_as_of`, `request_started_at`,
  `response_received_at`, `normalized_at`. **Nothing is back-stamped to `as_of`.**
- Configurable skew tolerances per join pair, tightest on quote-vs-underlying.
- A future-dated timestamp beyond the clock-skew allowance is a **hard failure**
  that zeroes the confidence score and is flagged `DATA_HALT`-eligible. It can no
  longer earn a perfect freshness score.
- New `src/gex/calendar.py`: NYSE holidays from rules (including Good Friday via
  the Gregorian computus), 13:00 ET early closes, ad-hoc closure injection. Open
  interest is aged in **trading sessions**.
  - Fixed: the v1 weekend discount only handled whole weeks, so Friday-to-Monday
    OI read as three sessions stale.

### Model specification

- New `ModelSpec` embedded in every snapshot and hashed into a fingerprint:
  pricing model, day count, rate and dividend sources, expiration rule, minimum
  time-to-expiry, underlying price source, IV source, effective values, version.
- Minimum time-to-expiry is configurable (default 60 min, was a hard-coded 30).
  `compute_floor_sensitivity()` reports the answer across ~0 / 30 / 60 minutes.
- `ACT/365F`, `ACT/360` and `ACT/252` day counts selectable.
- Optional early-close-aware expiration rule.

### IV provenance

- `IVSource` enum; IV is never stored as a bare float.
- Bid / mid / ask legs retained with `iv_spread` and an `IVQualityFlag`.
- `NON_FINITE_INPUT` flag: a NaN IV is sanitised so it cannot reach the pricer
  **and** reported, instead of silently vanishing into "not supplied".
- `GammaComparison` structure for local-vs-vendor validation, sliceable by DTE,
  moneyness, right and IV. Pro access not required for normal operation.

### Zero gamma

- Full diagnostics: `all_roots`, `root_count`, `selection_method`,
  `local_slope_at_selected_root`, `normalised_slope`, `nearest_root_spacing_pct`,
  `root_near_boundary`, `identically_zero_curve`, `no_root_found`,
  `max_abs_gex_on_grid`, `grid_expansions`.
- Bounded adaptive grid expansion when a root lands near the boundary.
- `selection_method` states that nearest-to-spot is a convention, not a claim
  that other roots are irrelevant.
- `STICKY_DELTA` and `SURFACE_REFIT` return an unresolved result with a reason;
  configuring either raises a `ConfigError`.

### Universe accounting

- `OptionUniverse` reported separately for the chain totals and the zero-gamma
  grid, with contract counts, expirations, and **GEX shares** on both sides.
- An explicit warning when the two universes differ.

### Walls and voids

- Neutral observations (`largest_*_gamma_strike`) separated from directional
  claims (`upside_call_wall`, `downside_put_wall`).
- A directional wall must be on the correct side of spot, or it is `None`. No
  silent same-side or opposite-side substitution.
- Deterministic tie-breaking to the lower strike.
- Gamma voids classified against an inferred strike ladder:
  `TRUE_LOW_GEX_VOID`, `MISSING_STRIKE_DATA`, `IRREGULAR_STRIKE_SPACING`,
  `FILTERED_STRIKE_REGION`, `INSUFFICIENT_COVERAGE`. Only the first is tradable
  structure.

### Confidence

- Nine new components: `multiple_root_penalty`, `root_slope_score`,
  `root_boundary_penalty`, `root_identity_stability`, `timestamp_alignment_score`,
  `future_timestamp_penalty`, `option_universe_coverage_score`,
  `iv_spread_quality`, `model_parameter_completeness`.
- Output exposes `score`, `calibrated`, `components`, `warnings`,
  `hard_failures`.
- The sentinel now also refuses `float()`, `int()` and arithmetic, not only
  ordering comparisons. The likeliest accident was a caller coercing it "to make
  the types line up".

### ThetaData adapter

- `HttpTransport` protocol, real `HttpxTransport`, deterministic `FakeTransport`,
  and `RetryingTransport` with bounded retries, jittered backoff, rate-limit
  handling, response size caps, request IDs, structured logging and credential
  redaction.
- Credentials from environment variables only; config stores variable *names*.
- Explicit calculation parameters, persisted in snapshot metadata.
- Append-only, content-addressed raw response store (in-memory and file-backed).
- Schema tests across quotes, open interest, first- and second-order greeks,
  index price, empty responses, vendor errors, missing columns, unknown extra
  columns and partial chains.

### Configuration

- Typed loading with fail-fast validation: unknown keys, missing keys, wrong
  types, out-of-range values and invalid enums all raise with the offending path.
- Duplicate YAML keys rejected — PyYAML silently keeps the last occurrence, so
  both values look applied in review while only one takes effect.
- `yaml.SafeLoader` subclass; a config file cannot construct Python objects.
- Environment overrides via `${VAR}` / `${VAR:-default}`, **recorded** in the
  profile so they are not invisible in the audit trail.
- `trading_enabled: true` and any non-`none` broker are rejected unconditionally.
- Execution-capable stages refuse to load while any sentinel remains.
- Config fingerprint in every snapshot.
- `research.yaml` usable; `paper.yaml` and `live.yaml` explicitly disabled with
  stated reasons.

### Determinism

- **Fixed:** float addition is not associative, so vendor row order changed the
  last bits of every sum. Contracts are now sorted into canonical order before
  aggregation.
- `output_hash()` quantises floats to 12 significant figures, so the digest is
  stable across platforms rather than only within one machine.
- Replay tests cover repeated runs, reversed row order and varying
  `PYTHONHASHSEED`.

### Tests

- Unit suites for validation, timestamps, calendar, transport, config,
  architecture, states and the synthetic source.
- Offline integration test: fixture to parser to validation to GEX to confidence
  to persisted metadata, via the fake transport.
- Frozen regression case with hand-transcribed expectations.
- Deterministic replay test with output-hash comparison.
- Negative controls proving the credential scanner, the settlement-clock
  cross-check and the 0DTE sensitivity sweep can actually fail.

### Still true

- The repository cannot place an order. No broker adapter, no risk engine, no
  strategies, no execution path.
- Market thresholds remain `UNSPECIFIED_CALIBRATE`.
- All data is synthetic or fixture-based. Nothing has run against live vendor
  data.

---

## 0.1.0 — initial GEX engine

Five GEX views, Black-Scholes shadow pricer, self-contained US Eastern clock,
eight-component confidence score, synthetic chain fixtures, ThetaData endpoint
map with tier requirements.
