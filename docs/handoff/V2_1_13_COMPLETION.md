# v2.1.13 completion report

**Status: `READY_FOR_RAW_CAPTURE_ONLY`** · `NOT_READY_FOR_ANALYTICAL_DATASET` ·
`NOT_VALIDATED_WITH_LIVE_THETADATA`

Version: v2.1.13 · Code commit: `e8a014f56afbf3cc628d51c61fb2c289d1ea8277` ·
Base: v2.1.12 `f3624c78289323eaae9bc2066e480b76f1f258bf`

Every verification result below was produced at `e8a014f`. The release archive is
cut from the tip of `master`, which is `e8a014f` plus the commit that adds this
document — no source, test or configuration file differs between the two.

---

## The defect this release corrects

v2.1.12 made the capture command safe to run. Running it found that the
safest-looking part of it was writing into the checkout.

`build_thetadata_client` executed `FileRawStore(config.raw_capture_path)` during
pipeline construction, for every caller. The operator writes its capture to
`<output>/raw` and hands *that* store to the session, so the configured one
received nothing — and the shipped profile names `artifacts/raw`. Merely
building a pipeline created that directory inside the repository, and the dry
run, whose entire promise is that it writes nothing and whose report says
`wrote_files=false`, created it too.

It was in the working tree of this checkout when the release started, which is
how it was found.

---

## What changed

### §1 — No store is created from a configuration path

Nothing constructs a filesystem raw store because a path appears in
configuration. `build_thetadata_client`, `ThetaDataRuntime.from_config` and
`ThetaDataResearchPipeline.from_config/from_loaded_config` take
`default_raw_store=`; without it the client uses `NullRawStore`. The operator
constructs exactly one `FileRawStore`, under the run root it has claimed, and
passes it as the default.

The reports distinguish `configured_fallback_raw_capture_path` — what the
profile *names* — from `effective_raw_store_path`, which is the store that
receives the records.

### §2 — The destination is claimed, not checked

`destination.mkdir(parents=True, exist_ok=False)`, after path validation and
**before** any store, attempt log or intent document exists. v2.1.12 checked
that the path was empty and created the stores afterwards, so two processes
could both observe an empty path and both proceed — reproduced here with two
threads completing into one directory. Exactly one `mkdir` wins; the loser is
refused before it sends anything.

### §3 — Stored bodies are the vendor's bytes

`HttpResponse.body` carries the HTTP entity body after content decoding
(`BODY_REPRESENTATION` states which layer, and why not the compressed wire
bytes). `FileRawStore` writes and hashes exactly those bytes — binary mode, no
re-encoding — and `decode_text()` returns a typed `DecodedBody` recording the
content type, the declared and selected charset, whether any byte had to be
replaced, and the digest of the text alongside the digest of the bytes.

v2.1.12 decoded in the transport with `errors="replace"` and the store
re-encoded that string as UTF-8: two lossy conversions between the socket and
the file, and the digest was described as the hash of the vendor's response.

Attempt bodies use the same representation, stored as `.bin`.

### §4 — An oversized response is an attempt

A typed `RESPONSE_TOO_LARGE` attempt carrying the configured cap and the bytes
read, recorded from both places the failure can surface — the retry layer's cap
check and the streaming reader's mid-body abort. v2.1.12 raised from both and
the attempt log reported zero attempts, on the one failure where the size of the
thing is the whole finding. It has its own exit code rather than being mapped
onto `SCHEMA_ERROR`.

### §5 — Run states come from the attempt log

| State | Means |
|---|---|
| `FAILED_BEFORE_REQUEST` | zero attempts; no request left this process |
| `FAILED_NO_RESPONSE` | attempts were made and **nothing answered** |
| `FAILED_PARTIAL` | a response arrived or a record was stored, then a failure |

v2.1.12 derived the state from stored records, so four attempts against a Theta
Terminal that was not running reported `FAILED_BEFORE_REQUEST`.

### §6 — A vendor's refusal is not an internal error

The classifier covers the whole public `ThetaDataError` hierarchy and follows the
cause chain, so a retry budget spent on 429s is `RATE_LIMITED` rather than a bare
`RETRY_EXHAUSTED`. Categories: `AUTHENTICATION_REJECTED`, `VENDOR_HTTP_ERROR`,
`RATE_LIMITED`, `RESPONSE_TOO_LARGE`, `SCHEMA_ERROR`, `STORAGE_ERROR`,
`PROVENANCE_ERROR`, `VALIDATION_ERROR`, `CONFIGURATION_ERROR`,
`RETRY_EXHAUSTED`, `TRANSPORT_FAILURE` — each with its own exit code.

400, 401 and 403 were all `INTERNAL_ERROR`, which sends an operator to read this
code instead of their environment.

### §7 — `Retry-After` cannot shorten a wait

`min(max(retry_after, computed_backoff), cap)`. v2.1.12 used
`max(retry_after, backoff_base_seconds)` — the *first* delay — so on attempt four
a `Retry-After: 1` cut an eight-second computed backoff to one second.

### §8 — Finalization is fail-safe

The whole lifecycle is wrapped and the transport is closed in `finally`. When
finalization is itself what breaks, the run writes
`capture-summary-emergency.json` carrying the run and session ids, the state, the
typed error, the records known in memory, the attempt count and the output root
— and says `manifest_written: false`.

**The guarantee, stated accurately:** every ordinary controlled failure produces
a manifest and a summary; a storage or finalization failure produces a
best-effort emergency summary.

### §9 — Attempt evidence outlives the run

`attempts/index.jsonl`, appended and fsynced per attempt, readable by
`HttpAttemptLog.recovered_from` without the process that wrote it. Existing
content-addressed bodies are verified against their own filenames before reuse,
and bodies are fsynced like raw records.

### §10 — Analytical readiness is derived inside the gate

`assess_analytical_readiness(pipeline=, chain=, manifest=, store=,
artifact_store=, pricing_compatibility=)` builds the context itself. The context
remains as the *derivation report* on the result. v2.1.12 accepted one, and it is
a public frozen dataclass.

### §11 — Storage is not identity

`ThetaDataConfig.semantic_payload()` excludes `raw_capture_path` and
`raw_capture_enabled`, so the pipeline fingerprint — stamped on every record and
compared by every replay — no longer moves when a capture is written to a
different disk. The actual path is recorded in the run report and the manifest
metadata.

### §12 — Windows

PowerShell examples in `docs/THETADATA_INTEGRATION.md` and `docs/RELEASE.md`,
including `--execute-live`, with a note that a drive-qualified path is required
because a bare `\ThetaData\...` is refused as relative.

---

## §13 — Versions

| Constant | Value |
|---|---|
| package version | `2.1.13` |
| `RAW_CAPTURE_RUN_SCHEMA_VERSION` | `raw-capture-run/2.1.13` |
| `RUN_INTENT_SCHEMA_VERSION` | `raw-capture-intent/2.1.13` |
| `HTTP_ATTEMPT_SCHEMA_VERSION` | `http-attempt/2.1.13` |
| `RAW_RESPONSE_SCHEMA_VERSION` | `raw-response/2.1.13` |
| `ANALYTICAL_READINESS_SCHEMA_VERSION` | `analytical-readiness/2.1.13` |
| `CERTIFICATION_SCHEMA_VERSION` | `adapter-certification/2.1.13` |

`MODEL_VERSION` stays `gex-engine/2.1.10` and `PARSER_VERSION` stays
`thetadata-v3-parser/2.1.10`: the byte-to-text interpretation of a UTF-8 CSV is
unchanged and no numerical input moved.

**Frozen values unchanged:** `EXPECTED_OUTPUT_HASH` `0e536883…`,
`EXPECTED_MODEL_FINGERPRINT` `32b4694c…`, `EXPECTED_CONFIG_FINGERPRINT`
`ded3172b…`.

---

## §15 — Verification

All commands **locally executed** at commit `e8a014f`, `git status --porcelain`
empty.

| Command | Result | Where |
|---|---|---|
| `pytest` | **2355 passed** | locally executed, Python 3.12.10 |
| `pytest -m integration` | 18 passed | locally executed, Python 3.12.10 |
| `pytest -m regression` | 46 passed | locally executed, Python 3.12.10 |
| `pytest -m replay` | 10 passed | locally executed, Python 3.12.10 |
| `ruff check .` | All checks passed | locally executed |
| `ruff format --check .` | 147 files already formatted | locally executed |
| `mypy src` | no issues in 75 source files | locally executed |
| `coverage report --fail-under=90` | **90%**, exit 0 | locally executed |
| `python -m src.app` | exit 0 | locally executed |
| dry run against the shipped profile | exit 0; repository tree 252 files before and after; destination absent; `artifacts/raw` absent; `capture_readiness=READY_FOR_RAW_CAPTURE_ONLY`; `expected_capture_origin=LOCAL_TERMINAL_CAPTURE` | locally executed |
| partial-failure simulation | `FAILED_PARTIAL`, partial manifest and summary written, 500 bodies preserved, exit 7 | locally executed (fake transport) |

### Python versions

| Version | Status |
|---|---|
| **3.12.10** | **locally executed** — every command above |
| **3.13** | **unverified** — not installed on this machine (`py -0p` lists 3.12 and 3.11 only) |

The CI matrix covers 3.12 and 3.13 across the `quality`, `invariants` and
`no-trading-guarantee` jobs, and `workflow_dispatch` is enabled. **No CI run has
been observed for this commit**, so 3.13 is reported as unverified rather than as
`executed in CI`. Getting it green on both remains the first item of the
pre-capture checklist.

---

## Operator confirmation

| Claim | Tests |
|---|---|
| dry run modifies no persistent path | `test_a_dry_run_modifies_nothing_in_the_repository` (whole-tree snapshot), `test_a_dry_run_modifies_nothing_at_the_requested_destination` |
| live run has exactly one effective raw store | `test_exactly_one_raw_store_is_constructed_for_a_live_run`, `test_a_live_run_does_not_create_the_configured_fallback_path`, `test_the_reported_store_is_the_store_that_received_the_records`, `test_a_configured_path_alone_creates_no_store` |
| destination ownership is atomic | `test_two_concurrent_runs_cannot_both_acquire_one_destination` (real threads), `test_the_refused_run_writes_nothing_into_the_acquired_directory` |
| stored response bytes round-trip exactly | `test_non_utf8_bytes_round_trip_byte_identically`, `test_a_utf8_bom_round_trips_byte_identically`, `test_a_response_carries_its_bytes_and_a_separate_reading`, `test_a_captured_record_hashes_the_bytes_on_disk`, `test_attempt_bodies_are_hashed_over_their_bytes` |
| every attempt category is represented | `test_an_oversized_response_produces_an_attempt_record`, `test_a_refused_connection_run_reports_no_response`, `test_a_connection_that_never_answers_is_not_failed_before_request` |
| HTTP failures receive correct classifications | `test_a_vendor_status_gets_its_own_classification` (400/401/403/429/500), `test_a_two_hundred_vendor_error_document_is_a_vendor_error`, `test_a_two_hundred_malformed_csv_is_a_schema_error`, `test_an_oversized_live_response_has_its_own_exit_code` |
| controlled failures finalize durably | `test_a_partial_failure_still_writes_a_manifest_and_a_summary`, `test_a_finalization_failure_still_closes_the_transport`, `test_attempt_metadata_is_readable_without_the_process_that_wrote_it`, `test_top_level_reports_are_written_atomically` |
| retry timing cannot be shortened | `test_retry_after_cannot_reduce_a_later_exponential_delay` |
| readiness cannot be handed a context | `test_a_manually_built_context_cannot_reach_the_readiness_gate` |

---

## Artifact

```
gex-bot-v2.1.13.zip
SHA-256   see below
```

---

## What this release does **not** claim

- **Not `ADAPTER_CERTIFIED`.** Eight load-bearing vendor conventions are unknown.
- **Not `READY_FOR_ANALYTICAL_DATASET`.** No verified source reaches
  `FULL_REQUEST_ENUMERATED`, and five other conditions are unestablished.
- **Not validated against live vendor data.** Nothing here has met a real
  ThetaData response. That is what happens next.
- **Not able to trade.** No broker adapter, no order type, no position sizing, no
  execution path.

## Next

Run the first raw-only ThetaData session:

```bash
python -m src.tools.capture_thetadata_once \
  --config config/thetadata_capture.yaml \
  --output /absolute/path/outside/this/repo/capture-YYYY-MM-DD --execute-live
```

```powershell
py -3.12 -m src.tools.capture_thetadata_once `
  --config config/thetadata_capture.yaml `
  --output "D:\ThetaData\capture-YYYY-MM-DD" --execute-live
```

Then compare the eight conventions in `docs/ADAPTER_CERTIFICATION.md` against the
captured bytes.
