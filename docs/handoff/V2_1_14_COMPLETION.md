# v2.1.14 completion report

**Baseline:** `Version: v2.1.13 / Commit: c674cdbeef761825480880746ae3287a6184cc2a`

`READY_FOR_RAW_CAPTURE_ONLY`

Not `ADAPTER_CERTIFIED`. Not `READY_FOR_ANALYTICAL_DATASET`. Eight load-bearing
vendor conventions remain `UNKNOWN`, no capture has been taken, and the
repository is still incapable of placing an order.

---

## The two blockers

### 1. `verify_integrity()` read text and reported on bytes

`FileRawStore.verify_integrity()` opened every payload with
`read_text(encoding="utf-8")`. That is text mode, which translates `\r\n` to
`\n` on read -- so the bytes hashed were not the bytes on disk. A vendor sending
Windows line endings, which is not exotic and not a defect, would have had
**every record** report `HASH_MISMATCH`, and an operator reading that concludes
their paid capture is corrupt. A body that is not valid UTF-8 raised
`UnicodeDecodeError`, which aborted the scan -- so one odd payload left every
*other* record unverified as well.

Both are failures of the reader, reported as findings against the evidence.

```python
payload = path.read_bytes()
actual_hash = hashlib.sha256(payload).hexdigest()
actual_length = len(payload)
```

Verified for LF, CRLF, CR, BOM, empty, latin-1, NUL bytes and 1024 bytes of
arbitrary binary, and end to end: a full capture where every endpoint answers
with CRLF now finishes `COMPLETED_VERIFIED` with `integrity_ok = true`.

### 2. The timeout that was reported and the timeout that was applied

`HttpxTransport.get()` passed `timeout=<float>` per request. `httpx` reads a
scalar as connect *and* read *and* write *and* pool, so passing a per-request
read budget silently discarded the connect budget the profile states, the client
was constructed with, and the dry run prints.

The transport now holds an `httpx.Timeout` naming every dimension, and a
per-request read budget rebuilds it rather than replacing it. The regression
asserts that `effective_transport_settings(...)` and
`transport.effective_timeout` agree, and that `_timeout_for(9.0)` still carries
`connect=7.5`.

---

## Everything else, by section

| § | Change |
|---|---|
| 1 | Byte-based integrity. `probe_write` reads bytes and decodes for comparison. Never raises on an undecodable payload |
| 2 | One authoritative timeout policy, stored as an object, applied on every request |
| 3 | Two-phase lifecycle. Phase A validates the destination, loads the config, resolves credentials, checks the HTTP extra, builds a pipeline that cannot send, grades readiness against a temporary store, resolves transport settings and origin, and checks free space -- writing nothing. Phase B does `mkdir(parents=True, exist_ok=False)` and only then builds the run, stores, transport, intent and session |
| 4 | `src.config.schema.ConfigError` -> `CONFIGURATION_ERROR` / `ExitCode.CONFIGURATION_ERROR`, in both `_classify()` and `_handle()` |
| 5 | `build_transport` removed. It chose between two ways of passing `None` to a factory that builds a transport for `None`, so a caller asking for none got an `HttpxTransport` |
| 6 | `local_or_live_origin()` parses the hostname with `urllib.parse.urlsplit` and tests it with `ipaddress.ip_address(...).is_loopback`. The path and query are not consulted |
| 7 | One destination policy in both modes: the destination itself must not exist; its parent may |
| 8 | `raw_response_schema_version`, `body_representation`, content type, declared and selected charset, decode status and decoded-text hash on `RawResponseRecord` and `ManifestRecord`, inside the manifest hash. `validate_metadata()` and `verify_capture()` refuse an unsupported schema rather than reinterpreting it |
| 9 | `_emergency_state()` derives the state from the attempt log and the store; a run that already reached a failure state keeps it |
| 10 | Payload and attempt-body locations relative to their store root, resolved against where the store is now. `run_path()` joins a summary's relative paths to its `output_root`. `validate_metadata()` refuses an absolute location |
| 11 | `config/thetadata_capture.yaml`: the `--output` rule, and the fallback separated from the effective store |
| 12 | Package `2.1.14`; `raw-response/2.1.14`, `raw-capture-run/2.1.14`, `raw-capture-intent/2.1.14`, `http-attempt/2.1.14`, `raw-capture-manifest/2.1.14`. Engine and parser unmoved |

## Frozen values

**No change.** `gex-engine/2.1.10` and `thetadata-v3-parser/2.1.10` are
untouched, and the 46 frozen-reference regressions pass unmodified. v2.1.14
changed how evidence is verified, transported and described -- not how a payload
is read or how a gamma is computed.

Classification: **`VERSION_METADATA_ONLY`** for the four bumped schemas, and
**`BEHAVIORAL`** for integrity scanning, timeout application, origin
classification, lifecycle ordering and error classification. No `REPRESENTATIONAL`
change reaches a GEX output.

## The eleven named regressions

Each fails against v2.1.13.

| § | Test |
|---|---|
| 1 | `test_integrity_is_a_statement_about_bytes` (8 cases) |
| 1 | `test_a_crlf_vendor_completes_a_verified_run` |
| 2 | `test_the_dry_run_settings_are_what_the_request_actually_applies` |
| 3 | `test_a_run_that_never_starts_leaves_no_directory` (3 cases) |
| 4 | `test_a_missing_profile_is_a_configuration_error_not_an_internal_one` |
| 5 | `test_there_is_no_switch_that_builds_a_transport_it_promised_not_to` |
| 6 | `test_the_origin_comes_from_the_parsed_host` (11 cases) |
| 7 | `test_an_existing_empty_destination_is_refused_by_both_modes` |
| 8 | `test_a_record_under_older_raw_response_semantics_does_not_verify` (2 cases) |
| 9 | `test_an_emergency_summary_reports_the_state_the_evidence_supports` |
| 10 | `test_a_run_directory_verifies_after_it_has_been_moved` |

## Verification

| Check | Python 3.12 | Python 3.13 |
|---|---|---|
| `pytest` (2388 passed) | **locally executed** | **unverified** |
| `ruff check .` | **locally executed** | **unverified** |
| `ruff format --check` | **locally executed** | **unverified** |
| `mypy src` (75 files) | **locally executed** | **unverified** |
| frozen-reference regressions (46) | **locally executed** | **unverified** |

**Python 3.13 is `unverified`.** It is not installed on this machine and the
checkout has no git remote, so the CI matrix -- which does cover 3.12 and 3.13 --
has not been executed for this commit. Reporting it as "executed in CI" would be
a claim about a run that did not happen.

Operator command, executed locally against no vendor:

| Invocation | Result |
|---|---|
| dry run to a path that does not exist | `PLANNED`, `destination_refusals: 0`, `wrote_files: False`, exit `0`, **destination not created** |
| dry run to an existing empty directory | `REFUSED`, exit `2`, message naming the policy |

No live request was made. No test makes a network request.

## Scope

No futures data, no strategy logic, no backtesting, no feature storage, no risk
controls, no position sizing, no IBKR, no broker execution, no order classes, no
paper trading, no live trading, no calibrated thresholds. **The repository
remains incapable of placing an order.**

## Next

The first raw-only ThetaData session, once CI is green on Python 3.12 and 3.13.
