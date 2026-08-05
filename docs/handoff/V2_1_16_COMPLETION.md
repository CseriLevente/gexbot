# v2.1.16 completion report

**Baseline:** `Version: v2.1.15 / Commit: bf182f3ab6c9250885834c89c7cd73f9af7b5afd`

`READY_FOR_RAW_CAPTURE_ONLY`

Not `ADAPTER_CERTIFIED`. Not `READY_FOR_ANALYTICAL_DATASET`. Eight load-bearing
vendor conventions remain `UNKNOWN`, no capture has been taken, and the
repository is still incapable of placing an order.

---

## The principal defect

    option chain symbol: SPXW
    index price request: symbol=SPXW

`SPXW` is the PM-settled weekly SPX option root. `SPX` is the index those
options are written on. The shipped profile trades the first and would have
asked `/v3/index/snapshot/price` for its price by that name -- a request for an
instrument that does not exist. Whatever came back would have become the spot
under every gamma in the chain, and the dry run did not print the symbol at all.

**Why it survived review.** The symbol rule existed in three places: the fetch
path derived it, `build_request_spec` derived it again to state "what this
session would send", and `assess_readiness` rebuilt the capture plan from four
of the pipeline's inputs. All three were wrong in the same way, so the verifier
agreed with the defect it was verifying. There is now one mapping table, read by
the fetch path and the request spec, and certification consults the pipeline's
own plan rather than reconstructing it.

---

## Request-plan confirmation

**SPXW option requests use SPXW.**
`test_every_option_market_request_asks_for_spxw` -- quote, open interest,
first-order greeks and the contract listing all carry `symbol=SPXW`.

**The index request uses SPX.**
`test_an_spxw_chain_asks_the_index_endpoint_for_spx` --
`by_endpoint["/v3/index/snapshot/price"]["symbol"] == "SPX"`.
`test_an_spx_chain_asks_the_index_endpoint_for_spx` covers the identity case.
`test_an_undeclared_root_is_refused_rather_than_guessed` proves there is no
string rule.
`test_changing_the_index_mapping_changes_the_request_plan_hash` and
`test_a_capture_taken_under_the_wrong_mapping_does_not_verify` prove a corrected
mapping cannot silently reuse an old capture.

**The contract-list request is included.**
`test_the_contract_list_is_in_the_first_session_plan` -- present in the request
plan and in `acquisition_endpoints`, absent from `required_endpoints`, and
`is_evidence_only` is True.
`test_the_contract_list_request_carries_the_session_date_and_scope` -- the date
is the New York market session (01:00Z on the 18th is the 17th), the symbol is
`SPXW`, and `max_dte` matches the chain scope.
`test_the_shipped_standard_tier_can_serve_the_contract_list` -- the profile's
tier check passes.

**The dry-run plan equals the live request plan.**
`test_the_live_run_records_the_plan_it_was_authorised_against` -- the live plan
hash equals `raw_acquisition.request_plan_hash`, the two derivations agree on
every parameter except the listing's session date (which the dry run was not run
at `as_of`), and the plan is in `run-intent.json` before the first request.
`test_a_request_that_differs_from_the_plan_is_refused` -- refused before the
transport.
`test_the_dry_run_reveals_every_request`, `test_planned_parameters_are_deterministically_sorted`
and `test_no_secret_reaches_the_request_plan` cover visibility, determinism and
redaction.

## Capture-evidence confirmation

**Attempt evidence participates in raw verification.**
`test_a_failed_attempt_receipt_prevents_a_verified_run` -- with
`attempt_evidence.ok = false`, the run is `COMPLETED_RAW_UNVERIFIED`,
`verification_layer` is `HTTP_ATTEMPT_EVIDENCE`, and the raw-store and manifest
layers still read True. The five captured records and `integrity_ok` are
unchanged: the responses are not discarded, only the claim.

**Persisted attempt logs cannot be falsely verified as empty.**
`test_a_fresh_log_cannot_verify_a_directory_it_never_read` -- `HttpAttemptLog(root).verify_bodies()`
raises rather than returning `()`, `create_new` refuses an existing index, and
`open_existing` loads and verifies.
`test_create_new_accepts_a_directory_with_no_index` shows the honest empty case.

**Contract-list evidence remains analytically untrusted.**
`test_a_good_contract_list_response_grants_no_coverage` -- a complete capture
including the listing leaves readiness at `READY_FOR_RAW_CAPTURE_ONLY`, the
dataset blockers unchanged including the universe one, and the evidence state at
`DEDICATED_CONTRACT_LIST_OBSERVED_UNVERIFIED`. `capabilities_of` reports
`is_dedicated_contract_list=True` and `enumerates_request_universe=False`.
`test_a_malformed_contract_list_does_not_stop_the_other_endpoints` -- an
unusable listing costs nothing else.

---

## Everything else, by section

| § | Change |
|---|---|
| 1 | `InstrumentMapping` table; index endpoints take the underlying; both symbols in the capture-plan fingerprint, the request-plan hash and the request spec |
| 2 | `Endpoint.OPTION_CONTRACT_LIST_QUOTE` at the documented Value tier, captured as an evidence endpoint, granting no coverage |
| 3 | `RawRequestPlan` / `PlannedEndpointRequest`, printed by the dry run, persisted in the intent and the summary, and binding on every live request |
| 4 | Four verification layers, with `verification_layer` and `verification_findings` |
| 5 | `create_new` / `open_existing`; `verify_bodies` refuses an unloaded index |
| 6 | **Deferred.** The offline diagnostic chain assembly is explicitly optional in the brief and threatens nothing that the first session needs. The parser report already runs against stored bytes and reports per-endpoint status |
| 7 | `STOP_REASON_FOR` removed; the architecture assertion states the narrower accurate position |
| 8 | `access_mode = THETA_TERMINAL_REST_V3` recorded in the dry run and the summary |
| 9 | Package `2.1.16`; `raw-capture-run`, `raw-capture-intent`, `http-attempt`, `raw-capture-manifest`, `capture-plan`, `raw-request-plan`, `raw-acquisition`, `parser-report`, `attempt-evidence` at `2.1.16`; parser at `thetadata-v3-parser/2.1.16` |

## Frozen values

**No change.** `gex-engine/2.1.10` is untouched and the frozen-reference
regressions pass unmodified.

The **parser** moved to `thetadata-v3-parser/2.1.16` because it reads a response
shape it did not read before -- the contract listing. `raw-response` stays at
`2.1.15`: what a stored payload *is* did not change.

## Verification

| Command | Python 3.12 | Python 3.13 |
|---|---|---|
| `python -m pytest` (2436 passed) | **locally executed** | **unverified** |
| `python -m pytest -m integration` (18) | **locally executed** | **unverified** |
| `python -m pytest -m regression` (46) | **locally executed** | **unverified** |
| `python -m pytest -m replay` (10) | **locally executed** | **unverified** |
| `python -m ruff check .` | **locally executed** | **unverified** |
| `python -m ruff format --check .` (152 files) | **locally executed** | **unverified** |
| `python -m mypy src` (78 files) | **locally executed** | **unverified** |
| `python -m coverage run -m pytest` | **locally executed** | **unverified** |
| `python -m coverage report --fail-under=90` (90%, exit 0) | **locally executed** | **unverified** |

**Python 3.13 is `unverified`.** It is not installed on this machine and the
checkout has no git remote, so the CI matrix -- which does cover 3.12 and 3.13
-- has not been executed for this commit. Reporting it as "executed in CI" would
be a claim about a run that did not happen.

Operator dry run, executed locally against no vendor: exit `0`,
`wrote_files: False`, destination not created, and the plan printed in full --

| | |
|---|---|
| `/v3/index/snapshot/price` | `symbol=SPX` |
| `/v3/option/snapshot/quote` | `expiration=*`, `max_dte=60`, `symbol=SPXW` |
| `/v3/option/snapshot/open_interest` | `expiration=*`, `max_dte=60`, `symbol=SPXW` |
| `/v3/option/snapshot/greeks/first_order` | `annual_dividend=0.0`, `expiration=*`, `max_dte=60`, `rate_type=sofr`, `rate_value=4.2`, `symbol=SPXW`, `version=latest` |
| `/v3/option/list/contracts/quote` | `date=<session>`, `max_dte=60`, `symbol=SPXW` |

`access_mode: THETA_TERMINAL_REST_V3`,
`contract_list_evidence_state: DEDICATED_CONTRACT_LIST_OBSERVED_UNVERIFIED`.

No live request was made. No test in this repository makes a vendor request.

## Artifact

Built from a clean tree (`git status --porcelain` empty) with:

```bash
git archive --format=zip --output=gex-bot-v2.1.16.zip HEAD
```

**The SHA-256 and byte count are reported at delivery, not here.** A digest of
an archive that contains the document stating the digest cannot exist, and
quoting a hash for a *different* archive than the one uploaded is the failure
the requirement guards against. The commit is the anchor; the archive is a pure
function of it.

## Scope

No futures data, no strategy logic, no feature storage, no backtesting, no
regime classification, no risk controls, no position sizing, no IBKR, no broker
execution, no order classes, no paper trading, no live trading, no calibrated
thresholds. **The repository remains incapable of placing an order.**

## Next

The first raw-only ThetaData session, once CI is green on Python 3.12 and 3.13.
