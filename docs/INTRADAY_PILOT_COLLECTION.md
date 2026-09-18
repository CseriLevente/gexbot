# Intraday pilot collection: specification, implementation and runbook (v2.1.36)

Status: **implemented and tested offline, not executed live.** The collector,
the multi-cycle assembler and the multi-session summary exist and run
end-to-end on a fake clock and a fake vendor; no live session has been
collected with them, no subscription has been bought, and nothing here is a
trading result. The machine-readable form is `config/intraday_pilot.json`
(schema `intraday-pilot-collection/2.1.36`); the capture profile the collector
runs is `config/thetadata_intraday.yaml`. Tests check that every endpoint the
specification names exists in the pinned vendor document, that its research
contract equals the repository default, that the intraday profile plans exactly
the specified requests, and that the schedule the collector builds is the one
this document describes.

## 1. What the pilot has to answer

Milestone 1 after v2.1.35 asks whether genuine intraday sessions, replayed
through the v2.1.34 chronological replay under the declared research contract,
yield usable decisions at all: does the declared SPXW 0–7 DTE universe, with
prior-session open interest, sixty-second freshness and two-second skew, pass
frame by frame during a real session? That is a data-usability question. It is
answered by one `research-pilot-readiness/2.1.36` report per collected session
and one `research-pilot-summary/2.1.36` across sessions, produced offline from
immutable captures. The option side is judged separately from the pilot as a
whole: without recorded futures data the whole stays unusable, and nothing is
fabricated to change that.

## 2. What was inspected before writing this

- The preserved September 2, 2026 close capture (`capture-20260902T195700Z-
  0bccc563691a2ad9`): five endpoints, native CSV schemas, vendor timestamps as
  naive Eastern wall clock, receive times recorded twice (manifest record inside
  the verified manifest hash, and the attempt log). Its readiness report is
  `docs/evidence/PILOT_READINESS_2026-09-02.md`.
- The pinned vendor document (SHA-256 `1b65f93c…b40b50`, 66 paths). Snapshot,
  history, at-time, list and calendar endpoints exist for options, indices and
  stocks. **No path serves futures.**
- The existing capture command `src/tools/capture_thetadata_once.py`: one
  approved cycle of the five requests per invocation, refusing to run outside
  the session window without an explicit override, writing the manifest, raw
  payloads, run intent and attempt log that `load_capture` re-verifies. Its
  capture window is `[09:30, close)` Eastern on the repository calendar; the
  09:29 first cycle the v2.1.35 specification proposed was outside it and is
  corrected below, not overridden.
- The v2.1.35 r2 normalizer, which turns one such directory into hash-bound
  research events and a replay, and its repeated-identity rules.

## 3. Data the pilot needs, and where each comes from

| Input | Source in the pinned document | Recorded receipt | Status |
| --- | --- | --- | --- |
| Option inventory (SPXW, 0–7 DTE) | `/option/list/contracts/quote` (`date`, `max_dte`) | yes, via existing command | supported; FULL slots |
| Option NBBO bid/ask (+ displayed sizes in the raw bytes) | `/option/snapshot/quote` | yes | supported; every slot |
| IV, delta and the model that produced them | `/option/snapshot/greeks/first_order` with `rate_type`, `rate_value`, `annual_dividend`, `version` fixed in the run intent | yes | supported; every slot |
| Prior-session open interest | `/option/snapshot/open_interest` (documented: reported ~06:30 ET for the previous trading day) | yes | supported; FULL slots; missing rows stay unavailable |
| SPX observations | `/index/snapshot/price` | yes | supported; every slot |
| Futures bid/ask/size | none | — | **unsupported by this source** |
| Futures instrument metadata (tick size, point value, effective dates) | none | — | **unsupported**; must come from an inspected, effective-dated declaration with its own receipt |
| Futures fees / slippage assumptions | none (research assumptions) | declaration receipt | must be declared before the session and recorded with a receipt |
| Session calendar | repository calendar (`src/gex/calendar.py`), cross-checked against `/calendar/on_date` by the operator | — | the schedule is built from the repository calendar, early closes included |

Historical (`/option/history/*`, `/index/history/*`) and at-time endpoints can
return past sessions, but a download made later carries no evidence of what
was available *during* that session. They are therefore not a source for this
pilot's availability timestamps and must not be used to backfill gaps. The
collector plans only the five snapshot and list requests above; a request plan
containing anything else would not match the approved plan and is refused per
request by the one-shot.

## 4. Collection cycle

One schedule, built by `src/ingest/schedule.py` from the repository calendar
and the policy in `config/intraday_pilot.json` (`collection` block), serves
three purposes at once: it says when the day's preparation can begin, which
instants collection is allowed at, and which research decisions are covered.
The collector (`src/ingest/session_collector.py`, command
`python -m src.tools.collect_intraday_session`) runs the existing one-shot
capture command once per slot with an explicit endpoint scope; the one-shot's
preflight, per-request authorisation against the approved plan, manifest,
attempt log and verification are unchanged.

- **Slots.** One slot per `cadence_seconds` (60) from the calendar open,
  09:30:00 ET, while the slot instant is before the calendar close: 09:30:00 …
  15:59:00 ET on a regular session (390 slots), 09:30:00 … 12:59:00 ET on an
  early close (210 slots). Every slot is inside the one-shot's capture window,
  so no out-of-session override is ever passed, and none exists on the
  collector's command line.
- **Scopes.** A slot is `FULL` (index price, option quotes, Greeks, open
  interest, contract listing) on the first slot and every
  `refresh_every_seconds` (1800) thereafter — 09:30, 10:00, …, 15:30 ET — and
  `MARKET` (index price, option quotes, Greeks) otherwise. Quotes, Greeks and
  the index print therefore run at market cadence; open interest and the
  inventory at their own declared refresh cadence. Request order inside a
  cycle is the existing request plan's: index price, quotes, open interest,
  Greeks, listing (the v2.1.35 specification listed Greeks before open
  interest; the plan is reused, not reordered, and a MARKET cycle's quote and
  Greeks requests are adjacent).
- **One cycle in flight.** A cycle may start until `start_tolerance_seconds`
  (5) after its slot boundary. A cycle that has not finished by the next
  boundary is not overlapped and is not interrupted: it finishes, it is
  recorded as overrunning, every slot whose boundary passed by more than the
  tolerance meanwhile is recorded `MISSED_OVERRUN`, and collection resumes at
  the next boundary still in the future. There is no catch-up burst. A process
  that starts late records the passed slots as `MISSED_LATE_START`; a resumed
  process records them as `MISSED_RESTART_GAP`.
- **Budget and approvals.** The dry run prints the per-cycle approval (the
  one-shot's own, binding the request plan and the session date) and the
  **session approval**, which binds the session date, the schedule fingerprint
  (every slot, its instant and its scope), the policy, the request budget (390
  cycles, 1,196 requests, at most 4 attempts each on the default profile) and
  the destination directory. Both approvals are stamped into
  `session-intent.json`; every cycle's run intent carries the per-cycle
  approval, and the assembler checks all of it before it reads a row.
- **Self-stop.** The session stops itself after `max_consecutive_failed_cycles`
  (5) cycles that acquired nothing, or when the one-shot reports
  `AUTHENTICATION_REJECTED` or `STORAGE_FAILURE`; the stop is logged and the
  summary written.
- **Operator stop (r3).** `Ctrl+C` stops the whole session wherever it strikes.
  During a wait or between cycles nothing more is requested. During a cycle the
  one-shot keeps what it has -- the payloads already received, the manifest and
  the attempt log under `cycles/<HHMMSS>/` -- reports `OPERATOR_CANCELLED`, and
  the session takes no later slot; an interrupt in the moments before a
  cycle's first request leaves only `capture-bootstrap-failure.json` in that
  directory and is logged `FAILED_TO_START`. In every case the log gets a
  `STOP` entry naming the slot and phase (`REQUEST`, `WAIT`,
  `BETWEEN_CYCLES`), the summary is written with `status INTERRUPTED`,
  `stop_reason OPERATOR_INTERRUPT` and an `interruption` record, the lock is
  released and the command exits 130 with one message saying the same. Nothing
  continues on its own: continuing is an explicit `--resume` with the same
  approval, and a slot that already has a log entry is never retaken. (The
  2.1.36 r2 collector only honoured interrupts between cycles; an interrupt in
  a request produced a cancelled cycle and the next slot still ran.)
- **Request accounting (r3).** Every executed slot's log entry and the session
  summary carry `requests`: `scheduled` (the approved scope), `attempted`
  (logical requests the sweep began), `http_attempts` (every attempt record,
  retries included), `http_attempts_failed`, `with_receipt`, `acquired`,
  `not_attempted` and `without_receipt` (begun but without an attempt record
  -- the request in flight when the operator interrupted; reported as the
  uncertainty it is). All of it is read from the cycle's own report and
  attempt log; the assembler recounts it from the cycle directories and
  refuses a log that disagrees. The approved budget stays the budget: 1,196
  logical requests and at most 4,784 HTTP attempts on the default profile.
  (2.1.36 r2 reported `requests_issued` = the summed scheduled scope of
  executed slots, so an immediate authentication rejection on the first
  request was reported as five requests issued; it was one.)
- **Layout.** `<session>/session-intent.json` (written first),
  `<session>/session-log.jsonl` (one append-only line per slot and lifecycle
  event, fsynced), `<session>/cycles/<HHMMSS>/` (one complete one-shot capture
  per executed slot, never modified afterwards), `<session>/session-summary.json`
  (written when the process ends, however it ends), `<session>/session.lock`
  (held while a collector owns the root).

Requested scope is `max_dte=7` for the option endpoints
(`config/thetadata_intraday.yaml`) so that payloads stay around a few hundred
kilobytes per request (the September 2 capture at `max_dte=60` was 1.2 MB of
quotes and 2.3 MB of Greeks per cycle). The replay applies its own 0–7
calendar-day filter regardless of what the vendor parameter means.

## 5. Receipt recording

Every attempt records `request_started_at`, `received_at` (when the last body
byte is read), status or transport error, body SHA-256 and byte length, and the
logical request id that ties it to its manifest record. Both are already
implemented (`HttpAttemptLog`, `RawCaptureManifest`); the normalizer uses the
later of the two receipts and refuses a payload whose two records disagree by
more than five seconds. The collector reads time from one injected clock and
hands the same clock to the transport, so a cycle's receipts and the session
log come from one source; the offline tests drive that clock deterministically.

Clock discipline: one UTC wall clock per host. The collector does not measure
NTP synchronisation; the runbook has the operator check it before the open and
note the result with the session. The September 2 capture showed vendor row
timestamps up to 0.36 s *after* the local receipt. Such rows are counted and
excluded by default; a bounded tolerance may defer their availability to the
vendor time for a labelled diagnostic run, and nothing may ever move
availability earlier.

## 6. Assembly of a multi-cycle session

`src/adapters/thetadata/session_assembly.py` (command
`python -m src.tools.assemble_intraday_session`) turns one session directory
into `research-events/2.1.36` records, a replay report and a session readiness
report. The single-capture command and its five-endpoint rule are unchanged;
the assembler is the only caller that reads a cycle under a partial scope.

1. **Verify the structure.** The intent's session approval recomputes from its
   fields; the schedule's fingerprint recomputes from its slots and equals the
   approved one; every `EXECUTED` log entry names a cycle directory that
   `load_capture` verifies, whose manifest hash, capture session id, declared
   scope, approved session date and per-cycle approval equal what the log and
   the schedule say. Any disagreement refuses the assembly. Directories the log
   does not vouch for are reported as orphans and never read.
2. **Normalize each cycle under its scheduled scope.** Endpoints outside a
   slot's scope are reported `NOT_SCHEDULED`; a scheduled request that produced
   no verified payload is reported `NOT_ACQUIRED` with what the attempt log
   recorded (attempts, last status, last receipt), and the payloads that were
   acquired are normalized under the single-capture rules — repeated identities
   grouped, identical rows coalesced, disagreeing rows excluded whole. A cycle
   without Greeks carries no model evidence, because the model is read from
   the verified Greeks request and nothing else. A partial cycle is never
   reported as a five-endpoint capture.
3. **Merge in recorded-availability order**, ties broken by
   `(available_at, cycle label, kind, key, event_at, row_index)`. For each
   `(kind, key, event_at)` the assembler keeps the *current known revision*.
   A record equal to it is a re-observation: not emitted, counted
   `REOBSERVED_UNCHANGED`; the original keeps its original availability, and
   the event's age is never refreshed by re-observation. A record that differs
   is the next revision, `sequence + 1`, available at its own receipt:
   A → B → A yields three revisions and the final A is never discarded because
   it once appeared. A new `event_at` is a new event with sequence 0, also when
   an older event arrives after a newer one (counted `LATE_OLDER_EVENT`); the
   replay's selection by `(event_at, sequence)` before validity is unchanged,
   so a late revision of an older event never displaces the newer state.
4. **Open interest and inventory.** Each contract-list receipt is its own
   inventory event (`event_at` = receipt). Open interest and inventory acquired
   in a FULL cycle are reused between refreshes only through their own earlier
   availability; nothing is relabelled as observed in a later cycle, and a
   missing open-interest row stays unavailable. A MARKET cycle's option records
   are membership-checked against the latest listing available at their
   receipt (`NOT_IN_LATEST_INVENTORY` excluded and counted); before any listing
   is available they are retained and counted `UNCHECKED_NO_INVENTORY_YET`.
5. **Ambiguity.** A cycle whose payload carries conflicting rows for one
   identity excludes that identity for that cycle. It revises nothing and
   revives nothing: the previously available state stands on its original
   receipt, and the incident is counted `AMBIGUOUS_AFTER_KNOWN_STATE` (or
   `AMBIGUOUS_WITHOUT_KNOWN_STATE`) with the cycle, kind, key and the known
   event time, so the readiness report can say the state was left standing on
   older evidence.
6. **Lineage.** Every merged record names its cycle, the logical request id,
   the raw payload SHA-256, the native row index, the rule version and the
   availability basis; the document's provenance names the session date, the
   session approval hash, the schedule fingerprint, the intent and log digests,
   the collector, normalizer and assembler versions, and every cycle's capture
   session id, manifest and run-intent digests and payload digests. The event
   store refuses a record whose cycle or payload is not in that provenance.
   Collection origin (`SYNTHETIC` versus `RECORDED_NORMALIZED`, from the
   manifests' capture origin) is reported separately from byte verification and
   from model trust; every trust flag stays false. `rate_value` and
   `annual_dividend` remain wire parameters in the model evidence; neither is
   promoted to a resolved economic rate or dividend yield.

Outputs per session: `events.json`, `replay-plan.json`, `replay-report.json`,
`session-assembly.json` (per-cycle accounting and merge counts),
`session-readiness.json` and `.md` (option-side and whole-pilot verdicts, usable
decisions, coverage, open-interest gaps, stale and skewed decisions, clock
leads, request failures, overruns, restarts).
`python -m src.tools.summarize_intraday_pilot` restates several readiness
reports as one `research-pilot-summary/2.1.36`; it refuses to mix synthetic and
recorded sessions, refuses two reports for one session date, and a summary
containing any synthetic session is itself synthetic and never "ready".

## 7. Runbook (Windows, PowerShell, from the repository checkout)

All commands run from the checkout with the release virtual environment
(`docs/RELEASE.md`). `<session>` below is a directory that must not exist yet,
outside the checkout, on a disk with room for the day (at `max_dte=7`, budget
for roughly 150–300 MB per session; the September 2 capture at `max_dte=60`
was 3.5 MB per full cycle). Nothing below places an order or computes a GEX.

1. **Once, before the first session.**
   - Name the sessions (at least five regular sessions) and confirm each on
     the repository calendar and with the vendor's `/calendar/on_date`:
     ```powershell
     .\.venv\Scripts\python.exe -m src.tools.collect_intraday_session --show-schedule 2026-09-15
     ```
     prints the slots, scopes, budget and decision coverage for that date and
     writes nothing; a non-trading day is refused.
   - Confirm the subscription tier the preflight approval expects, set
     `THETADATA_USERNAME` / `THETADATA_PASSWORD` if the terminal needs them,
     and read `config/thetadata_intraday.yaml` line by line.
   - **Futures leg.** Decide separately whether a futures source with recorded
     receipts exists for the pilot. If not, run the option/SPX legs anyway: the
     readiness report states `futures_*` as MISSING, the option side is judged
     on its own, and nothing is fabricated.
2. **Each session day, after midnight ET and before 09:30 ET.**
   - Check the host clock (`w32tm /query /status`) and note the result with the
     session.
   - Dry run (writes nothing, sends nothing):
     ```powershell
     .\.venv\Scripts\python.exe -m src.tools.collect_intraday_session `
       --config config\thetadata_intraday.yaml `
       --policy config\intraday_pilot.json `
       --output C:\ThetaDataCaptures\sessions\2026-09-15
     ```
     Read the readiness line (`READY_FOR_RAW_CAPTURE_ONLY`), the schedule
     (first slot `093000` ET, last `155900` ET or `125900` on an early close),
     the request budget and both approvals. The approval printed as
     `SESSION APPROVAL:` is valid only for that date, that schedule, that
     budget, that policy and that destination.
   - Start the session before the open, pasting the approval:
     ```powershell
     .\.venv\Scripts\python.exe -m src.tools.collect_intraday_session `
       --config config\thetadata_intraday.yaml `
       --policy config\intraday_pilot.json `
       --output C:\ThetaDataCaptures\sessions\2026-09-15 `
       --execute-live --approve <SESSION APPROVAL>
     ```
     The process sleeps until 09:30:00 ET, prints one line per slot
     (`093000 EXECUTED COMPLETED_RAW_VERIFIED acquired 5 missing - in 3.2 s;
     requests scheduled 5 attempted 5 http 5`, `MISSED_OVERRUN`,
     `FAILED_TO_START`, …) and ends after the last slot with the session
     summary, whose last line states the request activity against the
     approved budget (`requests scheduled N, attempted N, HTTP attempts N
     (retries included) of the approved budget 1196 logical / 4784 HTTP; 0
     begun without a receipt`). Do not touch the output tree while it runs.
     An approval that does not match (another day, another policy, another
     directory) is refused before any request; so is an existing output
     directory.
   - **To stop the session yourself**, press `Ctrl+C` once and wait for the
     `INTERRUPTED by the operator …` message (exit code 130). It says where the
     interrupt struck; if it struck during a cycle, that cycle's partial
     capture and attempt log are preserved under `cycles\<HHMMSS>` and the
     slot is logged `EXECUTED` with `OPERATOR_CANCELLED` (or `FAILED_TO_START`
     if no request had been sent yet). The session log, `session-summary.json`
     (`INTERRUPTED` / `OPERATOR_INTERRUPT`, with the `interruption` record) and
     the released lock are in the session directory. The collector never
     continues on its own after an interrupt.
   - **If the process stopped** (your `Ctrl+C`, a crash, a reboot) and you want
     to continue the same session, resume it with the same approval:
     ```powershell
     .\.venv\Scripts\python.exe -m src.tools.collect_intraday_session `
       --config config\thetadata_intraday.yaml `
       --policy config\intraday_pilot.json `
       --output C:\ThetaDataCaptures\sessions\2026-09-15 `
       --execute-live --approve <SESSION APPROVAL> --resume
     ```
     Slots that passed during the gap are recorded `MISSED_RESTART_GAP`, a slot
     that already has a log entry (including the cancelled one) is never
     retaken, and collection continues at the next future slot. If
     `session.lock` is still present and no collector is running (a crash, not
     an operator interrupt, which releases it), delete that one file and
     resume again; delete nothing else.
3. **After each session.**
   - Archive the session directory read-only and record its digests.
   - Assemble, replay and judge readiness (offline, research defaults):
     ```powershell
     .\.venv\Scripts\python.exe -m src.tools.assemble_intraday_session `
       C:\ThetaDataCaptures\sessions\2026-09-15 `
       --out C:\ThetaDataCaptures\assembled\2026-09-15 `
       --label "session 2026-09-15"
     ```
     The output directory must not exist. The assembler first recounts every
     executed cycle's request activity from its `capture-summary.json` and
     `attempts\index.jsonl` and refuses a session whose log disagrees
     (`CYCLE_REQUESTS_DIFFER_FROM_LOG:<slot>:<keys>`), or whose log is not the
     r3 schema. Read `session-readiness.md`: the option-side verdict, usable
     decisions, the `Requests:` line (scheduled of budget, attempted, HTTP
     attempts with retries, receipts, acquired, not attempted, begun without a
     receipt, operator-cancelled cycles), the `Interruptions:` line, per-cycle
     failures, missed slots, overruns, restarts, open-interest gaps, stale and
     skewed decisions, clock leads. Keep any diagnostic run (nonzero
     `--receipt-clock-tolerance-ms`, altered `--close-buffer-minutes`) in its
     own directory; its report says `diagnostic`.
4. **Across sessions.**
   ```powershell
   .\.venv\Scripts\python.exe -m src.tools.summarize_intraday_pilot `
     C:\ThetaDataCaptures\assembled\2026-09-15\session-readiness.json `
     C:\ThetaDataCaptures\assembled\2026-09-16\session-readiness.json `
     --out C:\ThetaDataCaptures\summary\2026-09-week `
     --label "pilot week 1"
   ```
   The summary first validates every readiness report it is given -- its
   embedded `report_hash` recomputed over its contents, its decision counts
   non-negative integers that agree with each other (`usable + blocked ==
   expected`, top level equal to the replay block), its origins known and its
   verdicts consistent with their reasons -- and refuses a report that fails by
   name (r3; the r2 summary accepted a report whose usable decisions had been
   edited to 372 of 371). It then names, per session and in total, usable
   decisions, coverage, request activity on the basis each report supports
   (attempt evidence for r3 sessions; the scheduled scope only for a 2.1.36
   report, whose attempted and HTTP counts are shown as unknown), interruptions,
   open-interest gaps, stale and skewed decisions, clock leads, request
   failures, overruns and restarts, and states whether the option side reached
   the minimum number of usable sessions (5 by default). A matching report
   hash means the report is the one its writer produced, not that the vendor
   data behind it is authentic. Resolve data limitations explicitly before
   any strategy evaluation; the summary is not one.

## 8. Explicitly out of scope

Purchasing or subscribing to any feed; any endpoint outside the pinned
document; using downloads as availability evidence; a futures source; strategy,
position, PnL or GEX computation; any order.
