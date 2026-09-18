# v2.1.36: intraday session collector and multi-cycle assembler

## Scope and status

Implemented on the accepted v2.1.35 r2 tree (imported-history commit
`ecea660e9a5f0cfdf7872e625db0c96810af8794`, archive
`gex-bot-v2.1.35-ecea660e9a5f.zip`, SHA-256 `bf7c3b42…`); re-cut twice --
r2 after the first Windows verification, r3 after the independent review of
r2 (both described below). The work was done in
the same isolated Linux extraction with the same clearly labelled imported Git
history; it does not modify the Windows checkout or the GitHub remote, and no
remote push, merge, tag or CI run is claimed. No raw capture, original
certification, vendor correspondence, historical fixture (other than the
regenerated synthetic native fixture, below), frozen v2.1.34 report or trust
rule changes. No paid vendor request was sent: every session in this release
ran on a fake clock and a fake vendor. This milestone delivers collection and
assembly tooling; it computes no GEX, strategy, position, sizing or PnL and
places no order.

New functional components:

- `src/ingest/clock.py`: an injectable clock (`SystemClock`, `FakeClock`).
  The collector reads time from nothing else and hands the same clock to the
  transport (`RetryingTransport(clock=...)`, `build_thetadata_client`), so a
  cycle's attempt receipts and the session log come from one source.
- `src/ingest/schedule.py`: `CollectionPolicy` (cadence 60 s, refresh 1800 s,
  start tolerance 5 s, five consecutive empty cycles; read from
  `config/intraday_pilot.json` by `from_specification`) and
  `CollectionSchedule.build(session_date, policy, max_attempts_per_request)`
  from the repository calendar: slots every cadence from the calendar open
  (09:30:00 ET) while before the calendar close -- 390 on a regular session,
  210 on an early close -- `FULL` scope (index, quotes, Greeks, open interest,
  listing) on the first slot and every refresh interval, `MARKET` scope
  (index, quotes, Greeks) otherwise; preparation opens at midnight Eastern;
  phases; request budget; decision coverage; a fingerprint over all of it.
  The 09:29 first cycle of the v2.1.35 specification was outside the
  one-shot's capture window and is corrected, not overridden.
- `src/ingest/session_collector.py`: `plan_session` (the dry run: the
  one-shot's own plan plus schedule, budget and the **session approval** that
  binds session date, per-cycle approval, schedule fingerprint, request budget
  and destination) and `collect_session` (one `run_capture` per slot with
  `scheduled_endpoints=slot.scope`, `approved=<per-cycle approval>`, the
  injected clock and no override; one cycle in flight; slots passed by more
  than the tolerance recorded `MISSED_OVERRUN` / `MISSED_LATE_START` /
  `MISSED_RESTART_GAP` and never caught up; `--resume` requiring the same
  approval and no live lock; stop after five empty cycles or on
  `AUTHENTICATION_REJECTED` / `STORAGE_FAILURE`; append-only fsynced log with
  planned and actual instants, durations, acquired and missing endpoints, run
  states, manifest hashes; summary written however the process ends).
- One-shot changes (`src/tools/capture_thetadata_once.py`,
  `src/config/pipeline.py`, `src/adapters/certification.py`,
  `src/adapters/thetadata/capture_certification.py`): `run_capture` accepts a
  `clock` and a `scheduled_endpoints` subset of the approved plan, refuses a
  scope that is not a nonempty subset before it claims the directory, records
  the scope in the run intent and the summary, and verifies against it (a
  required endpoint outside the scope is not `MISSING_ENDPOINT`);
  `LoadedCapture` exposes the approved `market_session_date` and the declared
  `scheduled_endpoints`; `load_capture(require_greeks_request=False)` lets the
  assembler read a cycle whose Greeks request failed. The default behaviour of
  every existing caller is unchanged; the pinned OpenAPI document is parsed
  once per digest (`openapi_evidence._parsed_yaml`), which took the one-shot's
  preflight from about 5 s to about 0.15 s and makes a 390-cycle day cheap.
- `src/adapters/thetadata/research_events.py` (`thetadata-research-events/2.1.36`,
  row rules unchanged at `thetadata-v3/<kind>/2`): `normalize_capture(...,
  expected_endpoints=None, allow_unacquired=False)`. The single-capture rule
  is the default and unchanged (every endpoint; a capture declaring a partial
  scope is refused). With a scope, endpoints outside it are `NOT_SCHEDULED`;
  with `allow_unacquired`, a scheduled request without a verified payload is
  `NOT_ACQUIRED` with the attempt log's evidence (attempts, last status, last
  receipt) and the acquired payloads are normalized; a cycle without Greeks
  carries no model evidence. Receipts carry the manifest record's
  `request_id`; the session date comes from the verified listing date and/or
  the verified approval, which must agree. Coverage reports
  `scheduled_endpoints`, `acquired_endpoints`, `unacquired_endpoints` and
  `inventory_membership_checked`.
- `src/adapters/thetadata/session_assembly.py` (`thetadata-session-assembly/2.1.36`):
  `verify_session` (session approval and schedule fingerprint recomputed; every
  executed cycle loaded and checked against the log's manifest hash, session
  id, scope, session date and per-cycle approval; orphan directories reported
  and never read) and `assemble_session` (each cycle normalized under its
  scheduled scope; all records merged in recorded-availability order with the
  tie-break `(available_at, cycle, kind, key, event_at, row_index)`; the
  current known revision per `(kind, key, event_at)` decides: equal -> not
  emitted, original availability kept; different -> next sequence at its own
  receipt; an older event arriving late -> emitted and counted; inventory and
  open interest reused only through original receipts; MARKET cycles checked
  against the latest listing available at the receipt; an ambiguous
  observation excludes its identity for that cycle and revises nothing).
  Output `research-events/2.1.36` (lineage + `cycle`, `request_id`; session
  provenance with every cycle's digests) and `intraday-session-assembly/2.1.36`.
- `src/replay/event_store.py`: accepts `research-events/2.1.36`, validating
  the session provenance and that each record's cycle and payload digest are in
  it; 2.1.34 and 2.1.35 documents are unchanged.
- `src/replay/session_readiness.py` (`research-pilot-readiness/2.1.36`):
  `option_side_usable` apart from `usable_for_intraday_pilot`, a `session`
  block, stale / skewed / open-interest-gap decision counts, Markdown
  rendering. `src/replay/pilot_summary.py` (`research-pilot-summary/2.1.36`):
  per-session rows and totals over 2.1.36 session reports and 2.1.35
  single-capture reports; refuses synthetic/recorded mixes and duplicate
  dates; synthetic whenever any session is.
- Commands: `python -m src.tools.collect_intraday_session` (dry run by
  default; `--execute-live --approve HASH`; `--resume`; `--show-schedule
  DATE`), `python -m src.tools.assemble_intraday_session SESSION --out DIR`,
  `python -m src.tools.summarize_intraday_pilot REPORT... --out DIR`.
  `config/thetadata_intraday.yaml` is the reviewed capture profile with
  `max_dte=7`; `config/intraday_pilot.json` moves to
  `intraday-pilot-collection/2.1.36` with the `collection` block, the
  corrected schedule, the request order the plan actually issues (index,
  quotes, open interest, Greeks, listing) and the implemented merge rule.
  `docs/INTRADAY_PILOT_COLLECTION.md` is the Windows operator runbook for
  these commands.

## Validation of the specification's assumptions

- **09:29 ET first cycle.** `assess_capture_window` opens the one-shot's
  window at 09:30:00 ET; a 09:29 cycle would have needed
  `--allow-out-of-session`. The schedule's first slot is 09:30:00 ET; the
  collector has no such flag and its intent records both overrides as false.
- **Overrun wording (§4 versus §6 of the v2.1.35 document).** Resolved as: one
  cycle in flight; a cycle may start until 5 s after its boundary; an
  overrunning cycle finishes, the slots it spans are `MISSED_OVERRUN`, and
  collection resumes at the next future boundary; no overlap, no catch-up.
  Tested with a 125 s cycle on the fake clock.
- **Request order.** The existing plan issues open interest between quotes and
  Greeks; the specification said Greeks before open interest. The plan is
  reused, not reordered; the specification now states the real order, and a
  MARKET cycle's quote and Greeks requests are adjacent.
- **Early closes and time zones** come from the repository calendar
  (`is_trading_session`, `is_early_close`, `assess_capture_window`); the
  2026-11-27 schedule has 210 slots ending 12:59:00 ET.
- **Wire parameters.** `rate_value` and `annual_dividend` remain wire
  parameters in the model evidence; the assembler promotes neither.

## Verification

- Environment: Python 3.12.3 on Linux-6.18.44-fc-v33-x86_64-with-glibc2.39; every
  `requirements-lock.txt` pin installed offline from the downloaded wheels, plus
  one package the lockfile does not list: the `setuptools` build backend
  (within the declared `>=68,<86` range) needed for the offline editable
  install. ruff 0.14.14; mypy 1.20.2 (compiled: yes); pytest 9.1.1; coverage 7.15.2.
- `python -m ruff check .`: exit 0 (All checks passed!).
- `python -m ruff format --check .`: exit 0 (227 files already formatted).
- `python -m mypy src` (strict): exit 0 (Success: no issues found in 116 source files).
- `python -m src.app`: exit 0.
- Tests: **3,420 passed, 0 failed, 0 errors,
  0 skipped** across all 99 test modules, each module run in
  one of two isolated copies of the committed tree (with `.git`, so the
  release-integrity archive tests ran rather than skipped) under
  `pytest --cov --cov-append`; every module's exit code is 0.
- Coverage: per-worker data combined only after every module finished;
  `coverage report` with the repository's own `fail_under = 90`:
  **91.19%** line-and-branch over 17,124 statements
  (1,163 missing lines), exit 0.
- Extracted-archive checks from `docs/RELEASE.md` (imports, config load, demo,
  release-integrity, integration, certification smoke, replay CLI against the
  frozen bundle, replay tests, native normalization tests against the frozen
  native fixture, the intraday collection / assembly / summary end-to-end
  tests, the schedule command, architecture) were run on the produced
  archive; their logs ship with the release evidence.
- The real September 2 capture was normalized by the extracted archive's
  command as well; the events digest and replay report hash equal the ones in
  `docs/evidence/PILOT_READINESS_2026-09-02.json`, and its event records equal
  the v2.1.35 r2 evidence records apart from the normalizer identifier.
- A synthetic session was collected, assembled and summarised by the extracted
  archive's commands on a fake clock and a fake vendor; every artefact says
  `SYNTHETIC` (`synthetic-session-demonstration/` in the release folder).
- The independent reviewer's `reproduce_v2136_review.py` was run against the
  extracted archive together with its explicitly updated r3 probe: all four
  probes refuse on r3 and the positive control is accepted
  (`review-reproduction/` in the release folder).

This is a Linux, Python 3.12 verification of the imported-history r3 commit.
It is not a remote GitHub CI result and not a Windows result; the operator's
Windows (Python 3.12.10) run of the first cut is described in the next
section, and the Windows verification of r3 is the operator's to run. The
gate was run once on the r3 checkpoint to obtain these numbers and once more
on the final r3 commit that records them; both runs, and the earlier passes
over the superseded first cut and r2, are in the evidence folder.

## Windows verification of the first cut, and the re-cut

The first cut (imported-history commit `1137c97fb20b`, archive
`gex-bot-v2.1.36-1137c97fb20b.zip`) was applied to the operator's Windows
checkout as branch `v2.1.36-intraday-collector` on the v2.1.30 release commit
`a3fba0e` (the checkout's tree equalled the v2.1.30 archive; the cumulative
patch applied cleanly and the committed tree equalled the first-cut archive
byte for byte). The operator's verification there (Python 3.12.10, win32):
`ruff check`, `ruff format --check`, strict `mypy src` (116 files),
`python -m src.app` and `--show-schedule 2026-09-18` passed; `pytest --cov`
reported 3,377 passed, 2 failed, 91.08% coverage in 8 min 58 s. The two
failures were Windows-only:

- `test_the_builder_reproduces_every_frozen_capture_file`: the frozen
  `attempts/index.jsonl` digest differed. `HttpAttemptLog._append_index`
  opened the index in text mode without `newline="\n"`, so Windows wrote CRLF.
  The parser (`recovered_from`) always accepted either ending and the
  `index_hash` is computed over normalised text, so no verification was
  wrong; but evidence bytes depended on the writer's host. Fixed by writing
  with `newline="\n"`; a new test asserts the index bytes are LF-only.
- `test_a_tampered_payload_is_refused_before_any_row_is_read` (v2.1.35):
  `glob("*quote.raw")` matches the contract-listing payload as well as the
  quote payload, and NTFS returned the listing first, so the "tamper" changed
  nothing. Fixed by selecting `*snapshot-quote.raw` and asserting the bytes
  changed.

Both defects predate this release and had never been exercised on Windows;
the checkout was at v2.1.30 until this release was applied. The re-cut
carries only these three changes over the first cut; the first-cut archive is
preserved and marked superseded. r2 was applied to the Windows branch as a
second commit (`818d7a7`, tree equal to the r2 archive); its Windows
verification had not been reported when the review below arrived, and no
Windows result is claimed for r2 or r3 here -- the Linux gate is not one.

## The independent review of r2, and the r3 corrections

The review of `gex-bot-v2.1.36-bc68c6cd21dd.zip` (SHA-256 `cc8e8685…`,
review dated 2026-09-17) verified the bundle's digests, patch reconstruction,
gate receipts and the September 2 reproduction, re-ran 537 focused tests, and
found three reproducible defects in the new session layer. Its handoff asked
for those three corrections only, with focused regression tests, no loosening
of any data-eligibility, missing-OI, freshness or trust gate, and no trading
behaviour. All three reproduced against r2 with the review's
`reproduce_v2136_review.py` (exit 1: later executed cycle `093100` after an
interrupt in the 09:30 quote request; 5 requests "issued" for 1 transport call;
372 usable of 371 accepted twice) and none reproduces on r3 (exit 0, with the
request-accounting probe updated for the new schema as the review allows and
the updated probe shipped beside the original with its diff).

1. **Operator cancellation during a request** (`src/ingest/session_collector.py`,
   `src/tools/collect_intraday_session.py`). The session now reads each
   cycle's report for the operator's stop: `raw_acquisition.stop_reason ==
   OPERATOR_CANCELLED`, or the typed `INTERNAL_ERROR:KeyboardInterrupt` /
   `SystemExit` code of a bootstrap or finalization failure (those reports
   carry no sweep stop reason; an interrupt in the second before a cycle's
   first request lands there). A cancelled cycle is logged as the one-shot
   reported it -- `EXECUTED` with `operator_cancelled: true` and its partial
   capture, manifest and attempt log intact; or `FAILED_TO_START` with the
   bootstrap-failure report when no request had been sent -- and the session
   then raises `OperatorInterrupt`, the same exception a `KeyboardInterrupt`
   during a wait or between cycles becomes. The log gets a `STOP` entry with
   an `interruption` record (slot, phase `REQUEST` / `WAIT` / `BETWEEN_CYCLES`,
   `partial_capture_preserved`, `cycle_dir`, `continuing_requires`); the
   summary is written with `INTERRUPTED` / `OPERATOR_INTERRUPT` and the record;
   the lock is released; the command exits 130 with one message that says
   where it struck, whether a partial capture was preserved, where the log and
   summary are, and that continuing is an explicit `--resume` with the same
   approval. `--resume` never retakes a slot that has a log entry. The one-shot
   is unchanged. A cancelled-then-resumed session assembles (the cancelled
   cycle contributes what it acquired; its in-flight request is reported as
   begun without a receipt) and the interruption reaches `session-readiness`.
2. **Request activity apart from scheduled scope** (`session_collector.py`,
   `session_assembly.py`, `session_readiness.py`, `pilot_summary.py`).
   `requests_issued` (the summed scheduled scope of executed slots) is gone.
   Every executed slot entry and the summary carry `requests`: `scheduled`,
   `attempted` (`raw_acquisition.attempted_endpoints`), `http_attempts` (every
   attempt record, retries included), `http_attempts_failed`, `with_receipt`,
   `acquired`, `not_attempted`, `without_receipt` (begun, no attempt record:
   the request in flight at an interrupt, reported as uncertainty),
   `attempt_evidence_verified`, `basis`. The assembler recounts each executed
   cycle from `capture-summary.json` and `attempts/index.jsonl` (the attempt
   log re-verified against itself), refuses a log that disagrees key by key
   (`CYCLE_REQUESTS_DIFFER_FROM_LOG:<slot>:<keys>`), a slot without the
   accounting, or a log of another schema (`LOG_SCHEMA_UNSUPPORTED`), and
   reports the summed block plus `operator_cancelled_cycles`; the readiness
   report restates it with `interruptions`; the pilot summary restates each
   report on the basis it supports (`CYCLE_REPORTS_AND_ATTEMPT_LOGS`;
   `SCHEDULED_SCOPE_OF_EXECUTED_SLOTS` for a 2.1.36 report, whose attempted
   and HTTP counts are `null`, never invented; `SINGLE_CAPTURE_RECEIPTS`).
   The approved budget (1,196 logical requests, 4,784 HTTP attempts on the
   default profile) is unchanged and printed beside the activity.
3. **Readiness reports validated before aggregation** (`pilot_summary.py`,
   `validate_readiness_report`). Each report's embedded semantic `report_hash`
   is recomputed over its contents under the rule every supported schema
   hashes with and must match; every decision count must be a non-negative
   integer (booleans, floats, strings refused, not coerced); `usable ==
   replay.passing`, `blocked == replay.blocked`, `passing + blocked ==
   expected`; `decisions_with_inventory <= expected`; derived per-decision
   counts and blocker decision counts `<= blocked`; origins from `SYNTHETIC` /
   `RECORDED_NORMALIZED` with `synthetic_only` agreeing; boolean verdicts
   agreeing with their reasons, `NO_PASSING_DECISIONS` when usable is 0; every
   trust flag false, `orders_placed` 0; session slot, cycle and request counts
   consistent and within budget. Refusals name the report and the check. The
   recorded September 2 report (2.1.35) and the r2 synthetic session report
   (2.1.36, now `tests/fixtures/replay/session_readiness_2_1_36/`) are the
   positive controls and are accepted unchanged; re-serialising a report is
   not a change. A matching hash is an integrity check on the report as
   written, not vendor authenticity.

Schema identifiers move to `…/2.1.36-r3` for the session log and summary,
the assembly, the session readiness and the pilot summary. Unchanged: the
event schema `research-events/2.1.36`, the intent and approval schemas, the
normalizer and its row rules, the research contract, the replay, the one-shot,
every trust gate, every capture, certification, correspondence and earlier
archive. Nothing outside the three corrections was changed on purpose; the
synthetic feed gained one scripting knob (`flaky`: HTTP 503 for the first n
requests of an endpoint) to test retries.

Regression tests added: `tests/unit/test_session_collector.py` -- interrupt
between cycles (rewritten from the r2 boundary test), during a request, during
a wait, before a cycle's first request, each asserting no later-cycle request,
`STOP` + `SESSION_END`, the summary, the released lock and the raised
`OperatorInterrupt`, plus resume after cancellation; first-request rejection
(1 = 1), partial acquisition, 503 retries (8 HTTP attempts for 5 requests), a
transport failure without a response; `tests/unit/test_session_assembly.py` --
recount equals log equals collector summary, readiness restates it, tampered
log accounting and shortened attempt log refused, r2-schema log refused, a
cancelled-and-resumed session assembles with the interruption reported, a
slot the one-shot refused before it ran stays accounted as without a report
through readiness and summary;
`tests/unit/test_pilot_summary_validation.py` (new) -- both positive controls,
the reviewer's stale-hash and fresh-hash cases, negative / non-integer /
boolean counts, nested and total inconsistencies, session count and budget
consistency, unknown origins, disagreeing `synthetic_only`, relabelling,
verdict / reason disagreement, trust claims, missing hash, other schema,
re-serialisation; `tests/regression/test_synthetic_session_end_to_end.py` --
the clean-session equality at every level through readiness and summary, and
the command's interruption and accounting messages.

## Synthetic end-to-end demonstration (not trading evidence)

Two clean synthetic sessions (2026-09-08 and 2026-09-09, fake clock from
09:29:30 ET, cycles 09:30–09:40, fake vendor) collected 11 cycles each (35
scheduled requests, 35 attempted, 35 HTTP attempts, 35 acquired -- every one
a scheduled snapshot or list request, and equal to the fake vendor's call
count), assembled into 207 records per session, replayed and summarised. Each session's option side is
usable (7 usable decisions in the collected window; the remaining 364 are
stale, reported, never backfilled); the pilot as a whole is not (futures
missing, one session per report); the two-session summary with
`--minimum-sessions 2` refuses to call the pair ready because it is synthetic
(`SYNTHETIC_SESSIONS_ONLY`), and refuses to summarise a synthetic session with
the recorded September 2 capture. The scripted session behind the assembly
tests additionally exercises A → B → A, an unchanging quote, a conflicting pair
after a known state, a late revision of an older event, a failed Greeks
request, a 125 s overrun, a restart, a sparse refresh, a missing open-interest
row and an unlisted identity. Every artefact says `SYNTHETIC`.

## The September 2 capture under the 2.1.36 normalizer

`docs/evidence/PILOT_READINESS_2026-09-02{,_DIAGNOSTIC}.{json,md}` were
regenerated with the same commands and labels as in v2.1.35 r2. The event
records are byte-identical to the r2 ones (37,799 default records; 38,054
diagnostic) apart from `provenance.normalizer`; the default replay's 371
decisions are identical (0 usable, `INVENTORY_NOT_AVAILABLE` throughout); the
diagnostic's decisions differ only through the events digest they reference.
New digests: default events `d8ffd149…`, replay report `acee7746…`, readiness
`f439a76f…`; diagnostic events `6a9c8689…`, replay report `229d14a9…`,
readiness `14342c20…` (r2: `f3069af2…` / `236a857d…` / `4a8dc825…` and
`f88aa061…` / `af9e03d5…` / `e52f664d…`, preserved in the r2 release bundle).
The frozen synthetic native fixture was regenerated once for the same reason
plus the synthetic capture's corrected approval date; its 36 records are
byte-identical apart from the normalizer identifier.

## Remaining limits and outstanding integration

No live session has been collected; the first will be the first evidence about
the collection choices listed in `docs/OPEN_DECISIONS.md` (refresh failures,
start tolerance, overrun frequency, host clock discipline). Matching digests
verify bytes, not vendor authenticity; receipts are the collecting host's
clock. No futures source exists in the pinned vendor document, so
`usable_for_intraday_pilot` stays false by construction until one with
recorded receipts is chosen outside this repository. Nothing here establishes
dealer positioning, trusted aggregate GEX, a strategy or profitability.

Local and remote integration remain the operator's: the Windows checkout must
be compared with the supplied baselines and the matching patch applied on an
isolated branch (`APPLY_V2_1_36.md` in the release folder); nothing was pushed,
merged or tagged remotely, and the imported-history commit id is not assumed to
exist in the operator's Git history.

## Next deliverable

Run the first live sessions under `docs/INTRADAY_PILOT_COLLECTION.md` §7
(dry run on the session morning, `--execute-live --approve`, assemble, judge
readiness); measure usable decisions, coverage, gaps and blockers across
sessions with the summary; decide the futures source separately; then
positions, round-trip accounting, costs and risk limits; only then one frozen
GEX strategy against a matched no-GEX baseline on chronological held-out
sessions.
