# v2.1.12 completion report

**Status: `READY_FOR_RAW_CAPTURE_ONLY`** · `NOT_READY_FOR_ANALYTICAL_DATASET` ·
`NOT_VALIDATED_WITH_LIVE_THETADATA`

Version: v2.1.12 · Code commit: `2d044f3ec7e77bc2dae427d388e7746a1178ba5f` ·
Base: v2.1.11 `16b7f4f8c2d289881917ccebc3c2d369de8b5b6d`

Every verification result below was produced at `2d044f3`. The release archive is
cut from the tip of `master`, which is `2d044f3` plus the commit that adds this
document — no source, test or configuration file differs between the two.

---

## What this release corrects

v2.1.11 gave the first paid capture a command. This release is what a review
found when it asked what that command does on a session that *really runs*, and
what it does when the vendor answers 503.

### §1 — The transport is the configured one

The CLI called `HttpxTransport()` with no arguments and handed it to the
pipeline, bypassing `build_thetadata_client` — which is where the connect
timeout, the read timeout, the response cap and the authentication are applied.
The profile said 30 seconds and 64 MiB; the wire would have had library defaults.

The command now builds nothing. Both reports carry the effective settings: base
URL with any embedded userinfo replaced, authentication mode, whether
credentials resolved and from which environment variables, connect timeout, read
timeout, maximum response bytes, retry count, backoff. **No credential value is
written anywhere.**

### §2 — The origin says which kind of live it was

`HttpxTransport.origin_for` has always distinguished a local Theta Terminal from
a direct vendor call. Nothing called it: `capture_origin_of` read the class
attribute, `LIVE_HTTP_CAPTURE`. The shipped profile points at
`http://127.0.0.1:25503`, so every record of the first real session would have
claimed a direct vendor round trip. The origin is derived from the effective base
URL and bound to the records, the manifest, the summary and the run intent.

### §3/§5/§8 — A run has a state, and a failure has a report

`RawCaptureRunState`: `PLANNED`, `IN_PROGRESS`, `COMPLETED_VERIFIED`,
`COMPLETED_UNVERIFIED`, `FAILED_PARTIAL`, `FAILED_BEFORE_REQUEST`.

`run-intent.json` is written before the first request, naming the run id, the
session and operation ids, the configuration and capture-plan fingerprints, the
requested endpoints, the origin, the start instant and every output path. A
manifest and a summary are written on **every** exit path; a partial manifest
identifies itself as partial and cannot pass `verify_capture`, because it is
missing endpoints the plan requires. Nothing is deleted automatically.

All three documents are serialised, written to a temporary file, `fsync`ed and
`os.replace`d.

### §4 — Every HTTP attempt is accounted for

`RetryingTransport` consumes a retryable 429 or 503 body, logs a warning, sleeps
and tries again — so the responses that would explain a partial capture were
exactly the ones thrown away, while the documentation said every response was
preserved. That sentence was wrong.

An attempt observer inside the retry loop records one `HttpAttemptRecord` per
attempt: logical request id, attempt number, endpoint, redacted URL, parameter
digest, start and receive instants, status, an allow-listed header subset, and
either a body hash and location or a `transport_error_code` where nothing came
back. Bodies are content-addressed under `attempts/`. **They are not chain
data** — the raw store holds the responses a snapshot was built from.

### §6 — Where a capture may go

Resolved with `resolve(strict=False)` *before* the repository comparison, so a
symlink pointing at the checkout no longer passes. Refused: a relative path, a
path inside the repository, a symlink, an existing file, and a directory holding
anything at all — including one holding an earlier `run-intent.json`, which is
named as belonging to another run. **v2.1.12 has no resume.**

Run ids are `capture-<timestamp>-<8-byte nonce>`; record ids derive from the
session id, and two runs in the same second used to collide.

### §7 — The dry run touches nothing

v2.1.11 built a `FileRawStore` at the destination to check its durability,
leaving `raw/` and `raw.health/` behind — so a dry run created the directory the
following real run then refused as non-empty. The store capability is probed in a
temporary directory deleted before the report returns, and an invalid destination
makes the dry run exit non-zero rather than printing a refusal and returning 0.

### §12 — Operator error handling

Eleven documented exit codes (0 verified, 1 unverified, 2 refused, 3
configuration, 4 missing HTTP extra, 5 missing credentials, 6 transport, 7 retry
exhausted, 8 schema, 9 storage, 10 internal). No secret on any path, a pointer to
the written failure summary, `--debug` for a traceback, and the HTTP transport is
closed cleanly on both the success and the failure path.

---

## Evidence-recovery gaps

### §9 — Documentation evidence survives the process

The v2.1.11 documentation path **could not be used at all**. `capture_session`
re-runs a resolution before opening the chain operation, and the re-run consulted
`UNIVERSE_DOCUMENTATION_RULES` — so a resolution made with a caller's own
registry was refused by the capture that had just accepted it, and the global
registry is empty in production. Recovery had the same shape one level on.

`UniverseDocumentationEvidenceArtifact` carries the rule in portable form (with
`verified_location` replaced by a deliberately unopenable marker), the digest of
the exact verified bytes, the artifact key those bytes live under, the extractor
version and the extraction. The bytes are stored content-addressed under
`ArtifactKind.DOCUMENT_BYTES`. Re-running and recovering consult no global state.

### §10 — Pipeline differences are derived

`UniverseOnlyCompatibilityRule` took `differing_parameters` from the caller — who
was the one asking for the waiver. `derive_parameter_diff` computes the diff from
two flattened configurations; any difference in a contract-set-affecting key is
refused whatever the rule says; and the rule carries only `approved_diff_hash`,
the digest of the difference it approves. `pipeline.configuration_payload()`
exposes the unhashed configuration and it is persisted content-addressed.

### §11 — Analytical readiness is derived

`assess_analytical_readiness` took six loose `Any` arguments, and six
`SimpleNamespace` objects with the right attribute names returned
`READY_FOR_ANALYTICAL_DATASET`. It now takes only a
`VerifiedAnalyticalEvidenceContext`, and `build_analytical_evidence` produces one
by running `verify_capture`, `recover_capture_artifacts`, `rebuild_from_capture`
and the two normalized-chain receipts itself. Anything it could not establish is a
derivation failure, which is a blocker.

---

## §14 — Versions

| Constant | Value |
|---|---|
| package version | `2.1.12` |
| `UNIVERSE_RESOLVER_SCHEMA_VERSION` | `universe-resolver/2.1.12` |
| `UNIVERSE_DOCUMENTATION_SCHEMA_VERSION` | `universe-documentation/2.1.12` |
| `CERTIFICATION_SCHEMA_VERSION` | `adapter-certification/2.1.12` |
| `RAW_CAPTURE_RUN_SCHEMA_VERSION` | `raw-capture-run/2.1.12` |
| `RUN_INTENT_SCHEMA_VERSION` | `raw-capture-intent/2.1.12` |
| `HTTP_ATTEMPT_SCHEMA_VERSION` | `http-attempt/2.1.12` |
| `ANALYTICAL_READINESS_SCHEMA_VERSION` | `analytical-readiness/2.1.12` |

`MODEL_VERSION` stays `gex-engine/2.1.10` and `PARSER_VERSION` stays
`thetadata-v3-parser/2.1.10`: the operator layer changed and the numerics did
not.

### Frozen values

| Value | v2.1.12 | Classification |
|---|---|---|
| `EXPECTED_OUTPUT_HASH` | `0e536883…` unchanged | **no change** |
| `EXPECTED_MODEL_FINGERPRINT` | `32b4694c…` unchanged | **no change** |
| `EXPECTED_CONFIG_FINGERPRINT` | `ded3172b…` unchanged | **no change** |

---

## §16 — Verification

All commands **locally executed** at commit `2d044f3`, `git status --porcelain`
empty.

| Command | Result | Where |
|---|---|---|
| `pytest` | **2328 passed** | locally executed, Python 3.12.10 |
| `pytest -m integration` | 18 passed | locally executed, Python 3.12.10 |
| `pytest -m regression` | 46 passed | locally executed, Python 3.12.10 |
| `pytest -m replay` | 10 passed | locally executed, Python 3.12.10 |
| `ruff check .` | All checks passed | locally executed |
| `ruff format --check .` | 146 files already formatted | locally executed |
| `mypy src` | no issues in 75 source files | locally executed |
| `coverage report --fail-under=90` | **90%**, exit 0 | locally executed |
| `python -m src.app` | exit 0 | locally executed |
| dry run against the shipped profile | exit 0, destination did not exist afterwards, `capture_readiness=READY_FOR_RAW_CAPTURE_ONLY`, `expected_capture_origin=LOCAL_TERMINAL_CAPTURE`, `wrote_files=false` | locally executed |
| partial-failure simulation | `FAILED_PARTIAL`, partial manifest + summary written, 500 bodies preserved, exit 7 | locally executed (fake transport) |

### Python versions

| Version | Status |
|---|---|
| **3.12.10** | **locally executed** — every command above |
| **3.13** | **unverified** — not installed on this machine (`py -0p` lists 3.12 and 3.11 only) |

The CI matrix covers 3.12 and 3.13 across the `quality`, `invariants` and
`no-trading-guarantee` jobs, and `workflow_dispatch` is enabled. **No CI run has
been observed for this commit**, so 3.13 is reported as unverified rather than as
`executed in CI`. Getting it green on both is the first item of the pre-capture
checklist.

---

## Operator confirmation

| Claim | Tests |
|---|---|
| configured HTTP settings reach the real transport | `test_the_configured_connect_timeout_reaches_the_real_transport`, `test_the_configured_read_timeout_reaches_the_real_transport`, `test_the_configured_response_cap_reaches_the_real_transport`, `test_configured_basic_auth_reaches_httpx`, `test_the_cli_does_not_instantiate_an_unconfigured_transport` |
| effective settings reported, no secrets | `test_the_effective_transport_settings_are_reported_without_secrets`, `test_a_base_url_with_embedded_credentials_is_redacted` |
| local-terminal origin is correct | `test_the_shipped_profile_is_a_local_terminal_capture`, `test_the_dry_run_reports_the_origin_a_live_run_would_stamp`, `test_a_fixture_capture_is_still_an_offline_fixture` |
| every HTTP attempt accounted for | `test_a_partial_failure_preserves_the_failed_attempt_bodies`, `test_failed_attempts_are_not_chain_data` |
| partial failures produce durable reports | `test_a_partial_failure_still_writes_a_manifest_and_a_summary`, `test_a_run_intent_is_written_before_the_first_request`, `test_a_failed_run_returns_a_documented_nonzero_exit_code`, `test_top_level_reports_are_written_atomically` |
| dry run is non-mutating | `test_a_dry_run_creates_no_files_or_directories`, `test_a_dry_run_makes_no_network_call`, `test_a_live_run_requires_the_explicit_flag`, `test_a_dry_run_on_a_bad_destination_returns_nonzero` |
| unsafe and reused destinations refused | `test_a_symlink_resolving_into_the_repository_is_refused`, `test_an_existing_nonempty_destination_is_refused`, `test_a_second_run_cannot_reuse_the_first_directory`, `test_a_destination_that_is_a_file_is_refused`, `test_two_runs_in_the_same_second_get_different_ids` |

## Evidence confirmation

| Claim | Tests |
|---|---|
| documentation resolutions survive a process restart | `test_a_custom_registry_resolution_can_open_a_capture`, `test_recovery_works_with_an_empty_global_registry`, `test_a_fresh_process_recovers_from_the_stores_alone`, `test_the_rebuilt_rule_carries_no_host_path`, `test_changed_document_bytes_fail_recovery`, `test_a_tampered_rule_fails_recovery` |
| pipeline differences are derived | `test_a_min_time_difference_cannot_be_waived`, `test_a_waiver_for_a_different_difference_is_refused`, `test_a_waiver_without_the_two_configurations_is_refused`, `test_a_documented_waiver_permits_a_derived_difference` |
| analytical readiness cannot be fabricated | `test_fabricated_analytical_inputs_cannot_return_ready`, `test_analytical_readiness_requires_every_condition_it_names`, `test_the_shipped_capture_is_not_analytically_ready` |

---

## Pre-capture checklist

- [ ] remote CI green on **both** Python 3.12 and 3.13 for the released commit
- [ ] Theta Terminal installed and running, reachable at the configured `base_url`
- [ ] subscription tier confirmed against the account, not against the YAML
- [ ] licensing and data-use terms confirmed for storing raw responses
- [ ] output destination **new, empty and outside this repository**
- [ ] sufficient disk space for a full SPX+SPXW chain plus retry bodies
- [ ] dry run completed and its report reviewed line by line

```bash
python -m src.tools.capture_thetadata_once \
  --config config/thetadata_capture.yaml \
  --output /absolute/path/outside/this/repo/capture-YYYY-MM-DD
```

Confirm in that output: `capture_readiness=READY_FOR_RAW_CAPTURE_ONLY`,
`expected_capture_origin` matches your `base_url`, `effective_transport` shows
the timeouts and cap you configured, `destination_refusals` is empty. Then add
`--execute-live`.

---

## What this release does **not** claim

- **Not `ADAPTER_CERTIFIED`.** Eight load-bearing vendor conventions are unknown.
- **Not `READY_FOR_ANALYTICAL_DATASET`.** No verified source reaches
  `FULL_REQUEST_ENUMERATED`, and five other conditions are unestablished.
- **Not validated against live vendor data.** Nothing here has met a real
  ThetaData response. That is the next thing to change.
- **Not able to trade.** No broker adapter, no order type, no position sizing, no
  execution path.

## Next

Run the first raw-only ThetaData session with the command above, then compare
the eight conventions in `docs/ADAPTER_CERTIFICATION.md` against the captured
bytes.
