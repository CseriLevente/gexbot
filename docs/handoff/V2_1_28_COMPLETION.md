# v2.1.28 completion report

**READY_FOR_RAW_CAPTURE_ONLY.**

No capture was taken for this release and no network request was made. No
certification gate was relaxed, no blocker was cleared, and nothing in this
repository claims `ADAPTER_CERTIFIED` or `READY_FOR_ANALYTICAL_DATASET`.

---

## What this release closes

Repeated live ThetaData captures establish two vendor behaviours the analytical
pipeline was not modelling. Both become **data-eligibility rules** with named
reasons and counts. Neither is a claim about vendor semantics —
`docs/DATA_ELIGIBILITY.md` exists to hold that line, and OD-37 and OD-38 record
it in the decision log.

### 1. An absent open-interest record was being read as a zero

Aug 26 → Aug 27, the strongest consecutive-session reading:

| | |
|---|---|
| Aug 26 | 13,310 list/quote/Greek identities, 12,872 open-interest identities, **438 with no record** |
| Aug 27 | **all 438** carried an explicit record: **177** positive, **261** an explicit zero, **0** still missing, **0** gone |
| Aug 27's own gaps | 302 with no record, **all 302 new** relative to Aug 26 |

The silence resolves, and it resolves upward 177 times out of 438. One
expression was reading it as zero:

```python
open_interest = quote.open_interest or 0      # src/gex/formulas.py, until now
```

The contract was weighted zero, summed, counted among the included contracts,
and no counter said why it contributed nothing. Under `require_open_interest`
the validator caught it first — so the defect was reachable only through the
flag that says a *reported* zero may contribute nothing, which has never been
permission to manufacture one.

Two truthiness readings of the same field went with it:
`sum(1 for q in quotes if q.open_interest)` counted an explicit zero as an open
interest that never arrived, and `not any(q.open_interest for q in chain.quotes)`
reported a fully-answered all-zero chain as carrying no open interest at all.

### 2. Set equality between three endpoints is not a current universe

On 2026-08-26 the contract list, the quote snapshot and the Greeks snapshot all
still named approximately 500 contracts with a 2026-08-25 expiration, carrying
2026-08-25 market timestamps. On 2026-08-27 those were gone and approximately
500 2026-08-26 contracts had replaced them.

A capture like that earns `DEDICATED_CONTRACT_LIST_MATCHED_SNAPSHOT_UNIVERSE` —
the strongest state in the universe vocabulary — and is right to. The three
responses really do name the same identities. They agree; they do not agree
about *this* session.

```
expiration_date <  market_session_date   ->  ineligible, counted
expiration_date == market_session_date   ->  eligible (0DTE)
```

The second line is load-bearing. 0DTE is the series an intraday gamma model is
mostly about; a same-session series past its settlement *clock* is excluded
separately by `ResolutionIssue.EXPIRED`, which measures the settlement instant
root by root. The session comes from `market_session_date(...)` — the market's,
not the calendar day of whatever zone an instant carries.

---

## Semantic changes

| Where | Before | After |
|---|---|---|
| `OptionQuote.open_interest_state` | did not exist | `OI_REPORTED_POSITIVE` / `OI_REPORTED_ZERO` / `OI_NOT_REPORTED`, derived from the field so the two cannot disagree |
| `src/gex/formulas.py` | `quote.open_interest or 0` | an unreported record is `ExclusionReason.OPEN_INTEREST_NOT_REPORTED` and never reaches the arithmetic, **whatever `require_open_interest` says** |
| `src/gex/formulas.py` | `expiry < session` fell under `ExclusionReason.EXPIRED` | its own reason, `EXPIRED_BEFORE_SESSION`, checked first |
| `ExclusionReason.NO_OPEN_INTEREST` | "zero or absent" | narrowed to **reported, and reported as zero**. Spelling unchanged: it appears in stored `exclusion_counts()` payloads |
| `src/gex/engine.py` | `received_oi_count=sum(... if q.open_interest)` | counts the *state*, so an explicit zero is a received answer |
| `src/domain/normalize.py` | `MISSING_OPEN_INTEREST` on `is None` | on the state, with the state as `observed` and "an absent record is not a zero" in the detail |
| `src/config/pipeline.py` | one truthiness check | two new fail-closed blockers on a trusted calculation, plus the truthiness fix |
| `ChainSnapshot` | — | `market_session_date`, `analytical_exclusions()` |
| `ContractGexResult` | — | `analytical_exclusions`, one answer whichever stage caught the contract |
| `GexSnapshot.meta` | — | `analytical_exclusions`, **published only when non-empty** |

**Excluded is not deleted.** An ineligible contract stays in the chain, in the
normalized evidence and in the capture's `CaptureUniverse`, and is counted under
its reason.

### New modules

- `src/domain/analytical_universe.py` — the two rules, one implementation,
  stdlib only, shared by the engine and the capture layer so they cannot drift.
- `src/adapters/thetadata/analytical_universe.py` — partitions one **certified**
  capture's listed universe into four classes with a set hash each, an
  exhaustiveness assertion, and its own content hash. Published by
  `python -m src.tools.certify_thetadata_capture` under `analytical_universe`.

The capture's session is read off the **verified contract-list request** — the
`date` parameter inside the preflight-approval digest stamped on every manifest
record — and cross-checked against the session the capture's own valuation
timestamp resolves to. A capture whose two records of its session disagree, or
whose clock cannot be read at all, is refused a trusted analytical universe
rather than partitioned against whichever record was read first.

---

## Files changed

| File | Change |
|---|---|
| `src/domain/analytical_universe.py` | **new** — `OpenInterestObservationState`, `TemporalEligibility`, `AnalyticalExclusion`, and the three classifiers |
| `src/adapters/thetadata/analytical_universe.py` | **new** — `AnalyticalUniverseReport`, `UniverseClass`, `analytical_universe[_of]` |
| `src/domain/contracts.py` | `open_interest_state`, `has_reported_open_interest`, `ChainSnapshot.market_session_date`, `ChainSnapshot.analytical_exclusions()` |
| `src/gex/formulas.py` | two new exclusion reasons; the temporal gate; the state-led open-interest gate; `ContractGexResult.analytical_exclusions` |
| `src/gex/engine.py` | `received_oi_count` reads the state; `meta["analytical_exclusions"]` when non-empty |
| `src/domain/normalize.py` | the missing-open-interest issue reads the state and names it |
| `src/config/pipeline.py` | two new `calculation_blockers`; the truthiness fix |
| `src/tools/certify_thetadata_capture.py` | publishes the partition beside an **unchanged** certification report |
| `tests/synthetic_capture.py` | `stale_expirations`, `stale_with_open_interest` |
| `tests/unit/test_data_eligibility.py` | **new** — 22 tests |
| `tests/unit/test_analytical_universe.py` | **new** — 22 tests |
| `tests/fixtures/vendor/thetadata/{quotes,greeks_first_order,open_interest}_eligibility.csv` | **new** — 1,968 bytes total, six identities, one per semantic state |
| `tests/unit/test_model_distribution.py` | package version, three new schema strings |
| `pyproject.toml` | `2.1.27` → `2.1.28` |
| `docs/DATA_ELIGIBILITY.md` | **new** — the observations, and the eligibility-versus-semantics line |
| `docs/CHANGELOG.md`, `docs/VALIDATION.md`, `docs/OPEN_DECISIONS.md`, `docs/ADAPTER_CERTIFICATION.md`, `README.md` | release documentation |

---

## Regression coverage

Forty-four new tests across two modules. The ten required proofs, mapped:

| # | Requirement | Test |
|---|---|---|
| 1 | explicit `OI=0` stays numeric zero, is not missing | `test_an_explicit_zero_stays_numeric_zero_and_is_not_missing` |
| 2 | explicit positive OI stays usable | `test_an_explicit_positive_open_interest_stays_usable` |
| 3 | absent OI cannot become zero through parsing, assembly, serialization or calculation | `test_an_absent_open_interest_record_never_becomes_a_zero` (all four layers in one test) |
| 4 | a missing-OI contract cannot contribute to trusted GEX | `test_an_unreported_open_interest_is_excluded_even_with_the_filter_off` (engine), `test_an_unreported_open_interest_blocks_a_trusted_calculation` (pipeline) |
| 5 | `expiration < market_session_date` cannot contribute | `test_a_contract_that_expired_before_this_session_cannot_contribute`, `test_a_retained_expired_contract_blocks_a_trusted_calculation`, `test_the_three_endpoints_agree_and_the_universe_is_still_not_this_session` |
| 6 | same-day 0DTE is not rejected for expiring today | `test_a_zero_dte_contract_is_not_rejected_for_expiring_today`, `test_a_same_session_expiration_is_eligible` |
| 7 | both exclusions counted and exposed | `test_both_exclusions_are_counted_and_exposed_side_by_side`, `test_both_exclusion_reasons_are_counted_apart`, `test_excluded_identities_are_named_by_set_hash_not_only_counted` |
| 8 | round-trip/replay preserves the classifications | `test_replaying_the_same_raw_bytes_reproduces_the_same_classifications`, `test_row_order_does_not_change_a_single_classification`, `test_the_partition_is_a_function_of_the_stored_bytes` |
| 9 | existing capture verification unchanged | `test_certification_is_untouched_by_the_partition`, `test_the_open_interest_coverage_blocker_is_not_made_smaller`, `test_the_tool_publishes_the_partition_beside_an_unchanged_report` |
| 10 | existing tests still pass | the whole suite, below |

Guards that keep the rules from becoming filters, and the counters from becoming
decorations: `test_a_reported_zero_is_never_counted_as_an_absent_record`,
`test_staleness_is_decided_before_open_interest_availability`,
`test_an_eligible_chain_reports_no_exclusions_at_all`,
`test_an_excluded_contract_is_still_in_the_chain_and_in_the_evidence`,
`test_a_clean_capture_is_still_not_a_trusted_analytical_universe`.

`tests/synthetic_capture.py` needed `stale_expirations` because the generator
**could not previously emit the behaviour this release exists to account for**:
every row went through the pricer, and a Black-Scholes input with negative time
is degenerate, so a past expiration was dropped before any CSV row was written.
The default is `()`, so every capture built without it is byte-identical to the
one the existing tests certify.

---

## Verification

| Check | Python 3.12 | Python 3.13 |
|---|---|---|
| `pytest` — 2901 passed, 0 failed | **locally executed** | `unverified` |
| `pytest -m integration` — 18 | **locally executed** | `unverified` |
| `pytest -m regression` — 46 | **locally executed** | `unverified` |
| `pytest -m replay` — 10 | **locally executed** | `unverified` |
| `ruff check .` | **locally executed**, clean | `unverified` |
| `ruff format --check .` — 182 files | **locally executed**, clean | `unverified` |
| `mypy src` — 91 files | **locally executed**, clean | `unverified` |
| `coverage report --fail-under=90` — 90.62% | **locally executed**, gate satisfied | `unverified` |

Python 3.12.10 in `.venv`. Baseline at `6c38eb0`, before this release: **2854
passed**. The 47-test difference is 44 new tests in the two new modules plus
three cases `test_architecture.py` parametrizes automatically over the two new
source files — checked by diffing collected test ids against a worktree at
`6c38eb0`, so nothing was removed or silently renamed.

**Python 3.13 is `unverified`, not "passing in CI".** There is no 3.13
interpreter on this machine and this checkout has no git remote, so the matrix
has never run. A workflow that has not run is not a result.

---

## Frozen and reference artifacts

**None changed.** Checked, not assumed:

| Artifact | Status |
|---|---|
| `tests/regression/test_frozen_reference_case.py` — `EXPECTED_OUTPUT_HASH`, config and model fingerprints, every GEX total, bucket, strike, wall, void, root and confidence component | unchanged and re-executed. The synthetic chain floors open interest at 1 and has no expiry before its session, so neither new rule fires; the new counters are `Counter`-derived and add no key when they do not |
| `tests/fixtures/live_capture/first_capture.json`, `second_capture.json` — `report_hash` | unchanged. `capture-certification/2.1.27` did not move, and the partition is a second report, not a section |
| `tests/fixtures/live_capture/oi_transition_first_to_second.json` — `transition_report_hash` | unchanged. `oi_transition.py` was not touched |
| `normalized_chain_hash` / `NORMALIZATION_SCHEMA_VERSION` | unchanged. No field was added to the canonical chain payload — a stored capture still re-derives |
| `PARSER_VERSION`, `MODEL_VERSION`, `CANONICAL_REPORT_SCHEMA_VERSION`, `ARCHIVE_IDENTITY_SCHEMA_VERSION` | unchanged. Raw capture, the numerics, the canonical rendering and archive identity are untouched |

`GexSnapshot.meta["analytical_exclusions"]` is published **only when one of the
findings fired**, which is the same habit `exclusions` already has — a `Counter`
carries no key for a reason that never occurred. A count of zero printed on
every snapshot would read as "checked and clean" in a field that means "nothing
to report", and it would have moved the replay hash of every chain the rules
find nothing in.

---

## Remaining adapter-certification blockers

Unchanged by this release. Every one of them still stands:

1. **OD-26 — the open-interest settlement date is `CALLER_ASSUMPTION`.** No
   ThetaData snapshot endpoint carries a settlement-date field, so
   `VENDOR_FIELD` resolves to a failure. The documented convention
   (`PRIOR_TRADING_SESSION`) is in force only from the moment the OpenAPI
   document was retrieved, so an earlier session gets no documentary authority.
2. **OD-11 — no verified contract-list endpoint establishes an independent
   universe.** Completeness stays `PARTIALLY_OBSERVED` for a vendor chain.
3. **Open-interest coverage on both live captures** — 426 unanswered identities
   on 2026-08-10, 416 on 2026-08-12. **Not reduced by this release**, and
   checkable against the committed fixtures: every expiration in either
   capture's `missing_by_expiration` is on or after that capture's session — the
   earliest is `2026-08-10` itself on the first capture (4 unanswered of 562
   listed, a same-session 0DTE row the rule keeps) and `2026-08-18` on the
   second — so the temporal rule removes none of them.
4. **The first capture's rate** — its Greeks were generated at 420%.
5. **OD-22 / OD-23 / OD-24 / OD-29 / OD-34** — rate units are resolved by live
   reconstruction but contradicted by the pinned documentation; the
   `annual_dividend` convention is undetermined (a zero-dividend request cannot
   settle it); seven vendor IV conventions are unread.
6. **OD-25 — the spot synchronisation tolerance is local policy, uncalibrated.**
7. **New, and open: OD-38** — why the vendor retains expired contracts is not
   established. One observed behaviour on several dates, applied as an
   eligibility rule rather than as a model of the vendor.

`trusted_for_gex` is the constant `False`. `analytical_readiness` is
`ADAPTER_CERTIFICATION_EVIDENCE`. The shipped default state is
`READY_FOR_RAW_CAPTURE_ONLY`. `OI_IMPUTATION_POLICY_UNRESOLVED` still holds:
this release makes unavailability explicit and excludes on it, which is not the
same as deciding what the absent value would have been.

---

## Scope

Nothing was added outside the release: no broker integration, no order classes,
no execution path, no strategy logic, no futures feeds, no feature storage, no
backtesting, no regime classification, no risk controls, no position sizing, no
paper trading, no live trading, no calibrated parameters, and no new
vendor-convention guesses. Rate semantics — `RateSource`, `DividendSource`,
`RateUnit`, `rate_semantics_for`, `CaptureRateIntent` — are untouched. Raw
capture is untouched: no row is dropped at capture time, and every eligibility
statement is about bytes that were already stored.

One pre-existing drift was found and deliberately **not** fixed here:
`CERTIFICATION_SCHEMA_VERSION` in `src/adapters/certification.py` reads
`adapter-certification/2.1.12` while `docs/VALIDATION.md` and
`tests/unit/test_model_distribution.py` require `2.1.13`. Raising the constant
changes what every readiness report announces and lowering the doc breaks the
test; neither belongs in a release about data eligibility.

---

## Stop condition

This release ends here. The next certification problem is not started, and no
strategy logic is implemented.
