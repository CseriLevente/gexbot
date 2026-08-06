# v2.1.17 completion report

**Baseline:** `Version: v2.1.16 / Commit: 6f48b9185ce41d09b1300a7c0d7fc3fdb71697de`

`READY_FOR_RAW_CAPTURE_ONLY`

Not `ADAPTER_CERTIFIED`. Not `READY_FOR_ANALYTICAL_DATASET`. Eight load-bearing
vendor conventions remain `UNKNOWN`, no capture has been taken, and the
repository is still incapable of placing an order.

---

## The principal defect

    ThetaData index response:  timestamp,symbol,price
    repository adapter:        row.get("index_price")

Two true statements about the same bytes. The parser reported `PARSER_VALID`,
because the CSV became a list of dictionaries. `fetch_index_snapshot()` returned
`None`, because the column it read is not one the vendor sends. The first
statement is about punctuation; the second is about the number every gamma in
the chain is divided by.

The adapter now reads the documented `price` column and validates it into a
typed `IndexSnapshot`. A non-empty response that cannot supply a spot **raises**
rather than returning `None`: silence is the one answer nobody can check.

---

## Confirmations

**The documented index schema produces a valid `IndexSnapshot`.**
`test_the_documented_index_response_produces_a_snapshot` -- against
`timestamp,symbol,price\n2026-03-17T11:00:00.000,SPX,5000.25`, the snapshot
carries `spot == 5000.25` and `symbol == "SPX"`.
`test_the_documented_index_response_is_semantically_valid` -- and the parser
agrees, which is the half that used to be true on its own.
`test_the_legacy_index_column_is_refused_under_the_v3_parser` and
`test_an_unusable_index_response_raises_rather_than_returning_none` (4 cases:
negative, non-finite, wrong symbol, ambiguous multi-row).

**The index tier is correct.**
`test_the_index_endpoint_requires_standard`,
`test_a_value_tier_index_profile_is_refused_at_plan_derivation`,
`test_the_shipped_profile_is_still_ready_for_raw_capture`.

**Market-session preflight is active.**
`test_a_live_capture_outside_the_session_is_refused` (3 cases: non-trading day,
before open, after close) -- refused, and no destination created.
`test_the_override_is_recorded_everywhere_it_matters` -- `--allow-out-of-session`
appears in the summary, in `run-intent.json`, and as a stderr warning.
`test_the_dry_run_prints_the_market_clock`,
`test_the_command_exposes_the_override_flag`.

**Contract-list status reflects what actually happened.**
`test_a_refused_contract_list_is_not_reported_as_observed` -- an HTTP 400
listing reports `VENDOR_REFUSED`, and the word `OBSERVED` does not appear.
`test_a_failed_evidence_endpoint_does_not_contradict_itself` -- `CORE_ACQUIRED`,
`partial: false`, empty `missing_required_endpoints`, and the gap visible as
`EVIDENCE_INCOMPLETE` with `missing_evidence_endpoints == [contract list]`.
`test_the_summary_exposes_each_layer_separately`.

**The approved request plan is bound to the raw records.**
`test_every_raw_record_names_the_plan_it_was_captured_under` -- all five records
carry the run's `request_plan_hash`, a `planned_request_hash`, and the schema
version; the manifest entries carry the same, inside the manifest hash.
`test_a_record_captured_under_another_plan_does_not_verify` --
`PLANNED_REQUEST_MISMATCH`, which is what refuses a contract-list record
captured under another session date or DTE scope.

## The settlement rule is **not** content-verified, and this is the honest state

§3 asked for the official ThetaData v3 documentation to be pinned
content-addressed, and forbade inventing a source hash. Both instructions were
followed, and they lead here:

* `http-docs.thetadata.us` serves the **v2** operation set. Every v3 operation
  URL tried returns 404.
* The one substantive page that resolved --
  `get-v2-hist-index-snapshot-price.html` -- states response columns
  `["ms_of_day","price","date"]` and tier `StandardPro`. That corroborates §1's
  `price` field and §2's Standard tier, and it is a v2 document.
* It says nothing about open-interest settlement, `rate_value` units, or a
  minimum time to expiration.
* A fetch through a markdown-converting reader returns a *rendering*. Hashing
  one would pin this repository's own paraphrase and label it the vendor's.

So `VendorDocumentationArtifact`, the content-addressed store and the registry
exist and are tested end to end -- and `PRODUCTION_VENDOR_DOCUMENTATION` is
**empty**. Open-interest settlement, rate units and the one-hour floor remain
`UNKNOWN`.

`test_the_production_documentation_registry_is_empty_and_says_why`,
`test_undocumented_pricing_dimensions_remain_unknown` (4 dimensions),
`test_open_interest_settlement_remains_unresolved`,
`test_an_artifact_without_a_real_digest_is_refused`,
`test_a_pinned_artifact_verifies_against_its_bytes`.

**What this means for §4.** The capture is not opened under a verified
settlement artifact, because there is no verified settlement artifact to open it
under. It is not silently opened under *no* rule either: the readiness report
names the settlement blocker, and the resulting capture is honestly
settlement-unusable until a document or a real response settles it. Registering
one entry is all that is needed; the bytes are the missing input.

**§6 is deferred**, as v2.1.16's brief permitted for its own §6: the offline
diagnostic chain assembly threatens nothing the first session needs, and the
parser report already runs against stored bytes with per-endpoint semantic
status.

## Frozen values

**No change.** `gex-engine/2.1.10` is untouched and the frozen-reference
regressions pass unmodified. The parser moves to `thetadata-v3-parser/2.1.17`
because the index response is read under its documented columns and parser
validity now means the endpoint can supply its domain value.

## Verification

| Command | Python 3.12 | Python 3.13 |
|---|---|---|
| `python -m pytest` (2470 passed) | **locally executed** | **unverified** |
| `python -m pytest -m integration` (18) | **locally executed** | **unverified** |
| `python -m pytest -m regression` (46) | **locally executed** | **unverified** |
| `python -m pytest -m replay` (10) | **locally executed** | **unverified** |
| `python -m ruff check .` | **locally executed** | **unverified** |
| `python -m ruff format --check .` | **locally executed** | **unverified** |
| `python -m mypy src` (81 files) | **locally executed** | **unverified** |
| `python -m coverage run -m pytest` | **locally executed** | **unverified** |
| `python -m coverage report --fail-under=90` (90%, exit 0) | **locally executed** | **unverified** |

**Python 3.13 is `unverified`.** It is not installed on this machine and the
checkout has no git remote, so the CI matrix -- which does cover 3.12 and 3.13
-- has not been executed for this commit.

No command contacted the ThetaData API. The only network access in this release
was to the public documentation site, at the user's explicit direction, and it
is reported above exactly as it went.

## Artifact

Built from a clean tree (`git status --porcelain` empty) with:

```bash
git archive --format=zip --output=gex-bot-v2.1.17.zip HEAD
```

The SHA-256 and byte count are reported at delivery: a digest of an archive
containing the document that states the digest cannot exist.

## Scope

No futures data, no strategy logic, no backtesting, no feature storage, no
regime classification, no risk controls, no position sizing, no IBKR, no broker
execution, no order classes, no paper trading, no live trading, no calibrated
thresholds. **The repository remains incapable of placing an order.**

## Next

The first raw-only ThetaData session, during a valid US options-market session,
once CI is green on Python 3.12 and 3.13.
