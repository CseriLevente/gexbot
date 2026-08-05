# v2.1.15 completion report

**Baseline:** `Version: v2.1.14 / Commit: a6c1504662c500ac750134315b5ba0d328215982`

`READY_FOR_RAW_CAPTURE_ONLY`

Not `ADAPTER_CERTIFIED`. Not `READY_FOR_ANALYTICAL_DATASET`. Eight load-bearing
vendor conventions remain `UNKNOWN`, no capture has been taken, and the
repository is still incapable of placing an order.

---

## The principal defect

The command was described as raw-only. It reached the wire through
`pipeline.fetch_chain()`, which requests one endpoint, **parses it**, and uses
the result to build the next request. Every endpoint after the first was
downstream of a successful parse of the one before.

So an index snapshot that came back as an HTML error page -- a maintenance
window, a proxy, a schema nobody had seen -- raised before the quote request was
ever issued. The session whose entire purpose is to discover the real vendor
schemas would have captured one response out of four, at full price, and the
thing that ended it would have been the thing it was paid to find.

Acquisition and interpretation are now separate operations, and the second
cannot reach the first.

---

## Raw-acquisition confirmation

**All planned endpoints are attempted independently of parsing.**
`test_an_index_schema_error_does_not_prevent_the_other_endpoints` --
the required regression, with the index answering HTTP 200 HTML and the other
three answering normally. Asserts all four planned, all four attempted, all four
acquired, `missing_endpoints == []`, `stopped_early is False`, four `.raw` files
on disk, and the maintenance page among them.

**Parser failures cannot prevent later raw responses from being captured.**
`test_a_parser_failure_cannot_downgrade_a_complete_raw_acquisition` -- a
capture with one unparseable endpoint finishes `COMPLETED_RAW_VERIFIED` with
`integrity_ok`, and the parser report names exactly one `PARSER_FAILED` against
three `PARSER_VALID`. The three are only knowable because they were requested.

`test_a_partial_failure_still_writes_a_manifest_and_a_summary` -- a 503 retried
to exhaustion on the quote endpoint no longer cancels the sweep: open interest
and first-order greeks are still acquired.

**Raw and parser states are reported separately.**
`test_a_two_hundred_malformed_csv_is_a_parser_finding_not_a_lost_capture` and
`test_a_two_hundred_vendor_error_document_is_captured_then_reported` -- both
assert `run_state` and `parser_state` independently.
`test_requests_are_derivable_without_a_chain_snapshot` -- every planned request
is built from configuration, with no `ChainSnapshot` in existence.

## Lifecycle confirmation

**Every post-claim constructor and intent failure is controlled.**
`test_a_constructor_failure_leaves_no_ownerless_directory` (3 cases:
`HttpAttemptLog`, `FileRawStore`, `ArtifactStore`),
`test_a_pipeline_construction_failure_is_controlled`,
`test_a_capture_session_failure_is_controlled`,
`test_an_intent_writing_failure_produces_a_report`. Each asserts a typed
`capture-bootstrap-failure.json` naming which resources had been constructed,
and a non-empty `error_code`.

**No ownerless capture directory remains.** `_assert_not_ownerless` is applied
in all six: either the typed report is present, or the directory is gone.

**Every constructed transport is closed.**
`test_every_constructed_transport_is_closed` -- `_close` runs in the `finally`
that now covers the whole post-claim lifecycle, and tolerates a pipeline that
was never built.

## Replay confirmation

**Replay consumes exact stored bytes.**
`test_replay_reproduces_the_stored_bytes_and_the_captured_reading` (5 cases:
non-UTF-8, latin-1 with a declared charset, UTF-8 BOM, CRLF, empty body).
Asserts `replayed.body == body` and equal byte length in every case.

**Recorded charset and decode semantics are reproduced.** The same test asserts
equal body hash, decode status, selected charset and decoded-text hash against
the capture's own record, and that a real empty body is not `SUPPLIED_AS_TEXT`.
`test_a_real_empty_response_is_not_supplied_as_text` covers the sentinel
directly.

**Decode metadata is independently verified.**
`StoredPayloadTransport.from_capture` raises `ReplayFidelityError` before
parsing when the derived reading disagrees --
`test_replay_refuses_when_the_capture_and_its_metadata_disagree`.
`FileRawStore.verify_integrity` re-derives content type, charsets, decode status
and decoded-text hash from the stored bytes and compares; `validate_metadata`
refuses an unknown decode status, a short digest, an uncanonicalised charset,
and a live capture claiming `SUPPLIED_AS_TEXT`.

## Persistence confirmation

**Attempt logs are reloaded and verified after restart.**
`HttpAttemptLog.open_existing()` parses the index, validates every schema,
recomputes every fingerprint, locates and hashes every body, and reports
orphans.

**Attempt-body tampering is detected.**
`test_a_reopened_attempt_log_detects_a_modified_body` -- completes a capture,
modifies a persisted body, shows that `HttpAttemptLog(root).verify_bodies()`
still returns `()` (the v2.1.14 behaviour, kept visible), and that
`open_existing` reports the mismatch.
`test_a_malformed_middle_index_line_is_a_finding` -- a damaged middle line is a
finding; a torn final line is still forgiven.
The index hash, counts and schema are bound into the capture summary at
finalization as `attempt_evidence`.

**Payload locations are enforced.**
`test_a_payload_location_naming_another_file_fails_integrity` -- editing a valid
record's location to `missing/other.raw` produces an integrity failure.
`payload_location` is on `ManifestRecord`, inside the manifest semantic hash,
and compared against `store.canonical_location()` by `verify_capture`.

---

## Everything else, by section

| § | Change |
|---|---|
| 1 | `acquire`/`interpret` split; `capture_required_endpoints_raw`; typed per-endpoint results; a closed stop policy; post-capture `parser-report.json`; four raw states and four parser states |
| 2 | Bootstrap run object immediately after the claim; one outer `try/except/finally`; `capture-bootstrap-failure.json`; an empty directory is given back |
| 3 | Byte-exact replay under the captured headers, with a fidelity gate; `b""` distinguished from "not supplied" |
| 4 | `HttpAttemptLog.open_existing()`; malformed-middle vs torn-final; attempt-evidence receipt at finalization |
| 5 | `payload_location` must equal the store's canonical location; in the manifest and its hash; checked by `verify_capture` |
| 6 | Decode metadata re-derived from the bytes and compared; enum, digest, charset and canonicalisation validated; live captures may not claim `SUPPLIED_AS_TEXT` |
| 7 | Disk requirement derived from the plan. For the shipped profile: **1,688,207,360 bytes** against v2.1.14's flat 67,108,864 |
| 8 | `endpoint`, `safe_url`, `request_id`, `status_code` structural on `ThetaDataError`; `endpoint_of_error`; one classification table shared by the endpoint results and the run report |
| 9 | Safe-header allow-list extended with content-encoding, pagination and vendor request-id headers; retained on raw records; the interpretive subset is part of record identity |
| 10 | Package `2.1.15`; `raw-capture-run/2.1.15`, `raw-response/2.1.15`, `http-attempt/2.1.15`, `raw-capture-manifest/2.1.15`, `parser-report/2.1.15`, `raw-acquisition/2.1.15`, `attempt-evidence/2.1.15`, `thetadata-v3-parser/2.1.15` |

## Frozen values

**No change.** `gex-engine/2.1.10` is untouched and the 46 frozen-reference
regressions pass unmodified.

The **parser** version moved to `thetadata-v3-parser/2.1.15` because how a
stored payload becomes text changed: replay consumes the exact bytes under the
captured content type and charset rather than a UTF-8-with-replacement reading
of them. How rows become a gamma did not change, which is why the engine version
did not move.

## Verification

| Command | Python 3.12 | Python 3.13 |
|---|---|---|
| `python -m pytest` (2414 passed) | **locally executed** | **unverified** |
| `python -m pytest -m integration` (18) | **locally executed** | **unverified** |
| `python -m pytest -m regression` (46) | **locally executed** | **unverified** |
| `python -m pytest -m replay` (10) | **locally executed** | **unverified** |
| `python -m ruff check .` | **locally executed** | **unverified** |
| `python -m ruff format --check .` (149 files) | **locally executed** | **unverified** |
| `python -m mypy src` (76 files) | **locally executed** | **unverified** |
| `python -m coverage run -m pytest` | **locally executed** | **unverified** |
| `python -m coverage report --fail-under=90` (90%, exit 0) | **locally executed** | **unverified** |

**Python 3.13 is `unverified`.** It is not installed on this machine and the
checkout has no git remote, so the CI matrix -- which does cover 3.12 and 3.13
-- has not been executed for this commit. Reporting it as "executed in CI" would
be a claim about a run that did not happen.

Operator command, executed locally against no vendor:

| Invocation | Result |
|---|---|
| dry run to a path that does not exist | `PLANNED`, `destination_refusals: 0`, `wrote_files: False`, exit `0`, **destination not created** |
| the same, `disk_space` block | `required_endpoint_count: 4`, `max_response_bytes: 67108864`, `max_attempts_per_endpoint: 4`, `safety_margin: 1.25`, `minimum_required_free_bytes: 1688207360`, `available_free_bytes: 78666276864` |

No live request was made. **No test in this repository makes a network
request** -- `test_no_test_in_this_file_reaches_the_network` checks the new file
for it as a rule rather than a promise.

## Artifact

Built from a clean tree (`git status --porcelain` empty) with:

```bash
git archive --format=zip --output=gex-bot-v2.1.15.zip HEAD
```

**The SHA-256 and byte count are reported at delivery, not here.** A digest of
an archive that contains the document stating the digest cannot exist, and
quoting a hash for a *different* archive than the one uploaded is the exact
failure the requirement is guarding against. The commit is the anchor; the
archive is a pure function of it.

The archive contains no `artifacts/`, no `.venv`, no nested archive and no
scratch file. `src/broker/`, `src/strategy/`, `src/risk/`, `src/backtest/` and
`src/adapters/ibkr/` are present as zero-byte `__init__.py` scaffolding and
nothing else: the only occurrences of order-shaped identifiers anywhere in it
are a docstring saying no `place_order` exists, a numerical root-finding
bracket, and `"would_place_orders": False`.

## Scope

No futures data, no strategy logic, no backtesting, no feature storage, no
regime classification, no risk controls, no position sizing, no IBKR, no broker
execution, no order classes, no paper trading, no live trading, no calibrated
thresholds. **The repository remains incapable of placing an order.**

## Next

The first raw-only ThetaData session, once CI is green on Python 3.12 and 3.13.
