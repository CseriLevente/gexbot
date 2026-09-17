# v2.1.35: native-data normalization and intraday replay readiness

## Scope and status

Implemented on the accepted v2.1.34 tree (imported-history commit
`c20e9d7643502b7449fcb2825e763717d7a49dca`, archive
`gex-bot-v2.1.34-c20e9d764350.zip`, SHA-256 `67ef6642…`). The work was done in
the same isolated Linux extraction with the same clearly labelled imported Git
history; it does not modify the Windows checkout or the GitHub remote, and no
remote push, merge, tag or CI run is claimed. No raw capture, original
certification, vendor correspondence, historical fixture, frozen v2.1.34 report
or trust rule changes. This milestone evaluates data usability; it computes no
GEX, strategy, position or PnL and places no order.

New functional components:

- `src/adapters/thetadata/research_events.py`: `normalize_capture(root,
  receipt_clock_tolerance_ms=0)` turns one verified capture directory into a
  `research-events/2.1.35` document (records bound to raw payload digest,
  native row and versioned rule; document bound to the capture) plus a
  coverage accounting. Rules: verify first (`load_capture`, then re-hash each
  payload while reading), native column sets, canonical identities checked
  against the listing, vendor timestamps as America/New_York, availability
  only from recorded receipts (manifest `response_received_at` inside the
  verified manifest hash, corroborated by the attempt log), open interest
  attributed by the re-derived documented convention applied to the row's own
  Eastern date, Greeks under the verified request's model, one model-evidence
  record available with the Greeks receipt, and named exclusion reasons for
  every refused row. Origin follows the capture origin inside the manifest hash.
- `src/replay/event_store.py`: accepts `research-events/2.1.35` (mandatory
  `lineage` and `provenance`, strictly validated) beside the unchanged 2.1.34
  schema; `Event.lineage`; `Event.reference()` unchanged; receipts for 2.1.35
  sources carry `schema_version` and `provenance`.
- `src/replay/pilot_readiness.py`: `research-pilot-readiness/2.1.35` report and
  a Markdown rendering: capture identity and hashes, parameters and whether
  they are the research default, per-endpoint coverage and receipts, eleven
  pilot requirements as PRESENT/PARTIAL/MISSING, usable and blocked decisions,
  blockers by decisions and contract-decisions, trust flags all false.
- `src/tools/normalize_thetadata_capture.py`: offline command; refuses used or
  nested output paths; writes `events.json`, `replay-plan.json`,
  `replay-report.json`, `pilot-readiness.json`, `pilot-readiness.md`.
- `config/intraday_pilot.json` and `docs/INTRADAY_PILOT_COLLECTION.md`: the
  collection specification and runbook for a small multi-session intraday
  pilot, grounded in the pinned vendor document (66 paths, none for futures)
  and the existing capture command; futures inputs and the multi-cycle merge
  are named as remaining requirements, not implemented.
- `src/domain/iv.py`: the vendor IV error limit is a named constant
  (`VENDOR_IV_ERROR_LIMIT = 0.5`) shared by the classifier and the normalizer.
- Tests: `tests/native_capture.py` (synthetic capture in the native v3
  schema), `tests/unit/test_native_normalization.py` (62),
  `tests/unit/test_intraday_pilot_config.py` (6),
  `tests/regression/test_native_capture_fixture.py` (4) with
  `tests/fixtures/native/synthetic_2026-09-08/` (capture pinned by digest --
  no captured payload is tracked -- plus frozen events, plan, readiness JSON
  and Markdown); `tests/synthetic_capture.write_capture` gained keyword-only
  options (`bodies`, `timing`, `attempts`, `origin`) whose defaults leave every
  existing capture byte-identical.

## Input inventory (what the September 2 capture actually contains)

Capture `capture-20260902T195700Z-0bccc563691a2ad9`, parser
`thetadata-v3-parser/2.1.17`, intent `raw-capture-intent/2.1.24`, manifest
SHA-256 `78ea0186…5f2898d`, run intent SHA-256 `691da45a…6374a5f8`; five
payloads, each verified by digest and by planned-request binding:

| Endpoint | Native columns | Rows | Vendor timestamps (ET) | Receipt (UTC) |
| --- | --- | --- | --- | --- |
| `/v3/index/snapshot/price` | `timestamp,symbol,price` | 1 | 15:57:04.000 | 19:57:03.643 |
| `/v3/option/snapshot/quote` | `timestamp,symbol,expiration,strike,right,bid_size,bid_exchange,bid,bid_condition,ask_size,ask_exchange,ask,ask_condition` | 13,536 | 09-01 15:59:31 … 09-02 15:57:04.352 | 19:57:04.267 |
| `/v3/option/snapshot/open_interest` | `timestamp,symbol,expiration,strike,right,open_interest` | 12,808 | 09-01 06:30 (496) / 09-02 06:30 (12,312) | 19:57:04.520 |
| `/v3/option/snapshot/greeks/first_order` | `symbol,expiration,strike,right,timestamp,bid,ask,delta,theta,vega,rho,epsilon,lambda,implied_vol,iv_error,underlying_timestamp,underlying_price` | 13,536 | 09-01 15:59:31 … 09-02 15:57:05.234; underlying 15:57:05.000 | 19:57:05.345 |
| `/v3/option/list/contracts/quote` | `symbol,expiration,strike,right` | 13,536 | none | 19:57:07.186 |

Receive times are recorded twice and agree within 3–7 ms (manifest
`response_received_at`, local offset +02:00, inside the verified manifest hash;
attempt log `received_at`, UTC, fingerprinted and bound to the body digest).
Strikes are vendor text (`7600.000`); all rows carry exchange `5` and
condition `50`; 1,023 quotes have a zero bid and zero bid size, none a zero
ask; 728 listed identities have no open-interest row; the expired 2026-09-01
expiry (496 contracts) is retained with 09-01 timestamps; 1,829 Greeks rows
have an implied volatility outside `[0.0001, 5]` (1,718 of them exactly zero);
21 rows carry `|iv_error| > 0.5`, every one of them already outside the IV
range; no delta lies outside `[-1, 1]`; 254 quote rows (max 86 ms) and the
SPX row (357 ms) carry vendor timestamps after the local receipt. Verified request parameters: Greeks
`rate_type=sofr`, `rate_value=0.042`, `annual_dividend=0.0`, `version=latest`,
`max_dte=60`; listing `date=2026-09-02`. Re-derived documentation:
`OPEN_INTEREST_SETTLEMENT = PRIOR_TRADING_SESSION`, `RATE_UNITS = PERCENT`
(the known documentation/live conflict), `MINIMUM_TIME_FLOOR = 60`. There is
no futures contract, quote, size, tick size, point value or fee anywhere in the
capture, and the pinned vendor document has no futures path.

## Independent review before promotion

An independent review of the first cut (imported-history commit
`a07ca7832efa`, archive SHA-256 `0ede286b…`, bundle `44aa6d4f…`) reconciled the
supplied evidence (93 modules, 3,256 cases, zero failures/errors/skips, 91.03%
coverage), reproduced the v2.1.34→v2.1.35 patch byte for byte, confirmed the
historical fixtures and the frozen v2.1.34 report unchanged, reproduced the
September 2 digests, and reported one material defect (P2): a repeated
identity inside one snapshot payload was resolved by keeping the first row
(`DUPLICATE_IDENTITY_IN_SNAPSHOT` counted the second), so CSV order decided
whether a conflicting observation reached the replay -- a clean quote followed
by a crossed one passed the 09:41 decision, the reverse order was blocked, and
two valid but different quotes each won when first. The reviewer's five-case
reproduction was rerun against the shipped source and confirmed.

Correction: rows are grouped by identity before anything is emitted. A group
whose rows agree on every column the rule reads is coalesced to its lowest row
index with the rest counted as `IDENTICAL_DUPLICATE_COALESCED`; a group whose
rows disagree in any of those columns (a malformed twin included) is excluded
whole as `CONFLICTING_DUPLICATE_OBSERVATIONS` and the identity stays in the
inventory, so the frame is refused as missing input in either order. The policy
is one shared helper for quotes, Greeks, open interest and the index rows; the
four row rules moved to revision 2 and the normalizer identifies itself as
`thetadata-research-events/2.1.35-r2`. Regression tests cover the reviewer's
cases in both orders, two valid conflicting quotes, differing vendor times,
identical repeats (control still passing, lowest row kept), repeats differing
only in unread columns, a malformed twin, conflicting Greeks and open-interest
groups with the resulting replay blockers, and repeated index rows. The frozen
native fixture was regenerated once under the revised rules with a conflicting
pair and a verbatim repeat added to its scenario; the reviewer's script exits 0
against this source. The September 2 results are unchanged (the capture has no
repeated identity); its digests moved because every record names its rule. The
first-cut archive is preserved and marked superseded; this document describes
the re-gated, re-archived release.

## Results on the September 2 capture

Research default (`docs/evidence/PILOT_READINESS_2026-09-02.md`, events
SHA-256 `f3069af2…0005a6`, replay report hash `236a857d…cba3e3`): 13,536
inventory identities, 13,282 quotes, 11,707 Greeks, 12,808 open-interest rows
(12,312 as of 2026-09-01, 496 as of 2026-08-31), no SPX row (vendor time after
receipt), one model-evidence record. **371 decisions, 0 usable, 371 blocked**
(`INVENTORY_NOT_AVAILABLE` at every instant: the first receipt, 19:57:03Z, is
after the last grid decision, 19:45:00Z). Requirements: inventory, quotes,
IV/model inputs and receive times PRESENT; prior-session OI PARTIAL (728
identities without a row); SPX MISSING under the default; futures quotes,
instrument metadata and costs MISSING; intraday and multi-session coverage
MISSING. `usable_for_intraday_pilot = false`.

Labelled diagnostic (`…_DIAGNOSTIC.md`; close buffer 0, receipt clock
tolerance 1,000 ms): 386 decisions, 0 usable; at 15:58 ET 1,109 of 2,534
in-scope contract frames pass and 1,425 are blocked (1,364 with market
inputs more than two seconds apart, 825 stale Greeks, 1,316 stale quotes, 503
missing Greeks, 88 missing open interest; a frame can carry several). This demonstrates the chain end to end on
real bytes; it is not the research result and no decision passes.

The capture is not an intraday dataset and cannot reconstruct the session's
history; the normalizer is finished and the remaining inputs are named. No
real-data pilot is claimed.

## Verification

- Environment: Python 3.12.3 on Linux-6.18.44-fc-v24-x86_64-with-glibc2.39; every
  `requirements-lock.txt` pin installed offline from the downloaded wheels, plus
  one package the lockfile does not list: the `setuptools` build backend
  (within the declared `>=68,<86` range) needed for the offline editable
  install. ruff 0.14.14; mypy 1.20.2 (compiled: yes); pytest 9.1.1; coverage 7.15.2.
- `python -m ruff check .`: exit 0 (All checks passed!).
- `python -m ruff format --check .`: exit 0 (211 files already formatted).
- `python -m mypy src` (strict): exit 0 (Success: no issues found in 107 source files).
- `python -m src.app`: exit 0.
- Tests: **3,272 passed, 0 failed, 0 errors,
  0 skipped** across all 93 test modules, each module run in
  one of two isolated copies of the committed tree (with `.git`, so the
  release-integrity archive tests ran rather than skipped) under
  `pytest --cov --cov-append`; every module's exit code is 0.
- Coverage: per-worker data combined only after every module finished;
  `coverage report` with the repository's own `fail_under = 90`:
  **91.08%** line-and-branch over 15,518 statements
  (1,067 missing lines), exit 0.
- Extracted-archive checks from `docs/RELEASE.md` (imports, config load, demo,
  release-integrity, integration, certification smoke, replay CLI against the
  frozen bundle, replay tests, native normalization tests against the frozen
  native fixture, architecture) were run on the produced archive; their logs
  ship with the release evidence.
- The real September 2 capture was normalized by the extracted archive's
  command as well; the events digest and replay report hash equal the ones in
  `docs/evidence/PILOT_READINESS_2026-09-02.json`.

This is a Linux, Python 3.12 verification of the imported-history commit. It
is not a Windows, Python 3.13 or remote GitHub CI result. The gate was run once
to obtain these numbers and once more on the final commit that records them;
both runs, and the two passes over the superseded first cut, are in the
evidence folder.

## Remaining limits

Matching digests verify bytes, not vendor authenticity; recorded receipts are
the capturing host's clock, not authenticated time; the open-interest as-of
basis and the treatment of vendor clock leads are explicit readings recorded
in `docs/OPEN_DECISIONS.md`. Count coverage is not exposure coverage. Nothing
in this release establishes dealer positioning, trusted aggregate GEX, a
strategy or profitability.

## Next deliverable

Collect and replay a small set of genuine intraday sessions under
`docs/INTRADAY_PILOT_COLLECTION.md` (multi-cycle merge to implement first;
futures source to be decided separately); measure usable decisions, coverage
and blockers across sessions; then positions, round-trip accounting, costs and
risk limits; only then one frozen GEX strategy against a matched no-GEX
baseline on chronological held-out sessions.
