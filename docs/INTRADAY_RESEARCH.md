# Intraday research contract, gamma sensitivity, verified replay, native normalization and session collection (v2.1.33–v2.1.36)

v2.1.33 implemented declaration checks and a retrospective gamma comparison.
v2.1.34 adds an offline, source-verified chronological replay with conservative
counterfactual fill probes (see [Verified replay and fill probes](#verified-replay-and-fill-probes-v2134)).
Neither release implements a data collector, a verified intraday dataset, a
strategy, a backtester, portfolio accounting or execution. Passing software
tests is not evidence of profit, and the shipped replay example is synthetic.

## Frozen first research scope

| Item | Initial specification |
|---|---|
| Option root | SPXW; every listed strike and both rights within scope |
| Main scope | 0 through 7 calendar DTE, inclusive, measured in New York |
| Separate views | 0DTE shadow view; 0 through 60 DTE diagnostic context |
| Decision grid | Every 60 seconds from 09:35 ET to 15 minutes before session close, inclusive |
| Calendar | Existing exchange-calendar implementation; holidays excluded, early close respected |
| OI | Explicit nonnegative quantity or unavailable; prior completed trading session as-of date |
| Availability | Every source and model choice must have been available by the decision instant |
| Market age | At most 60 seconds for option quote, Greeks and SPX price |
| Market skew | At most 2 seconds across those three event timestamps |
| Execution research target | MES, with a specific expiry identity; no synthetic continuous-contract fills |

The age/skew budgets are initial research design choices, not calibrated values
or vendor guarantees. They must be measured on a development dataset. Any change
creates a new contract hash and an explicit protocol revision before evaluating
held-out sessions. This release neither selects an actual futures expiry nor
asserts that a particular vendor provides the necessary historical fields.

The executable declaration template is `config/intraday_research.json`. It has
no observations; an empty template cannot produce a backtest-ready dataset.
Main, shadow and diagnostic scopes must retain separate denominators. Excluding
missing contracts does not make the original universe complete. Skip a full-scope
feature when required rows are unavailable. A reduced-universe research feature
would need a separately declared scope, coverage record and independent evaluation.

## Data required before replay

For every decision instant retain immutable raw source bytes, source identity,
request parameters, schema, event time, actual receive/availability time and
content digest. Use offset-aware timestamps and compare absolute instants.
A historical event timestamp is not proof that the value was available then:
late arrivals, revisions, backfills and model selections need their own clocks.
The four-session model set from v2.1.32 was selected retrospectively and must not
be backdated to any of its input sessions for a trading experiment.

Each option frame requires:

- Canonical root/expiration/strike/right identity, linked to the as-of contract
  inventory. The declaration audit treats `identity` as a label; it does not
  prove the identity join or a complete minute-by-minute inventory.
- Contract-list, option-quote, Greeks, SPX-price, OI and model-evidence observations,
  each with `event_at`, `available_at` and `source_sha256`.
- Decision timestamp, root, expiration, explicit OI quantity or null and OI as-of
  session. Missing OI is never converted to zero. OI on the Tuesday following
  Labor Day must refer to the preceding Friday, not the holiday Monday.
- Versioned IV source/price basis, rate and dividend assumptions, pricing-model
  alternatives, exclusion reasons and count denominators. These require future
  raw-source adapters; the declaration checker does not certify their semantics.

Futures data additionally needs a specific contract identity, as-of instrument
metadata, timestamped bid/ask and sizes, trades if used for features, session
calendar, and roll decisions known at the time. Preserve measured delivery latency
and effective-dated fees. Backtests need executable-side prices, spread, latency,
slippage and unfilled-order handling. Close snapshots or minute OHLC alone cannot
establish the fills of an intraday strategy. No fees or fills are invented here.

The first replay must enumerate the expected decision grid and option inventory,
report missing frames, verify source bytes and joins, and reject future-available
inputs. The present audit checks supplied declarations only. It always emits
`provenance_verified=false` and `ready_for_backtest=false`, even when all declared
checks pass. A string with SHA-256 syntax is not verified evidence.

## Commands (offline)

```bash
python -m src.tools.audit_research_inputs frames config/intraday_research.json --json research-input-audit.json
```

Replace the template with genuine declarations when available. Output paths must
be unused. The report includes a hash of the declared contract, frame-level
blockers and stable hashes. It refuses unknown fields, naive timestamps and
repeated identity/decision pairs. Input observation order and equivalent timezone
offsets do not affect frame hashes. No source data is fetched by this command.

```bash
python -m src.tools.audit_research_inputs gamma --capture CAPTURE_A CAPTURE_A.zip --capture CAPTURE_B CAPTURE_B.zip --json gamma-sensitivity.json
```

At least two distinct sessions are required. This reruns v2.1.32 model screening
from verified raw directories and ZIPs; it does not trust a caller-built model
summary. Put the new JSON outside every capture directory. Original captures,
archives, certifications and report hashes remain unchanged.

## Fixed gamma evaluation method

Use the intersection of retained complete models across the supplied sessions.
Use the same admitted, current, OI-answered contract identities for every model;
explicit zero OI remains an answer, and missing OI is excluded. IV and embedded
spot are held fixed. Compute per-contract Black-Scholes gamma with each model's
time/rate convention, in delta change per index point. Apply no OI, multiplier,
call/put sign convention or dealer-inventory assumption.

For each identity, absolute range is maximum minus minimum model gamma, and
relative range is 100 times that difference divided by maximum gamma. An all-zero
row has undefined relative range and is counted separately. Report median,
nearest-rank 95th percentile and maximum relative range, maximum absolute range,
and the ten largest relative rows with both absolute gamma endpoints. Group by
expiration and by 0DTE, 0-7 DTE and 0-60 DTE; these groups overlap and must not be
summed. Sort identities/models deterministically.

There is no fitted materiality cutoff. Large percentages on tiny gamma are not
large economic exposure. These unweighted ranges do not quantify missing OI,
IV-solver uncertainty, untested models, dealer inventory, zero-gamma locations,
walls, strategy stability or PnL. A one-model result has zero within-set spread
but does not establish certainty. A missing common model set computes no gamma.

## Verified replay and fill probes (v2.1.34)

`src/replay/event_store.py`, `src/replay/fill_probe.py` and
`src/replay/session.py` replay one trading session from a **bundle**: a
directory holding `replay-plan.json` and the normalized event files it names.
The command is offline and never overwrites anything:

```bash
python -m src.tools.replay_research_session BUNDLE_DIR --json NEW_REPORT_OUTSIDE_BUNDLE.json
```

Exit 0 writes a deterministic, sorted-key, UTF-8, LF-terminated JSON report and
prints its `report_hash`. Exit 2 refuses and writes nothing: a digest mismatch,
a duplicate JSON key, an unsupported schema, a malformed record, a source path
that escapes the bundle, an existing output path or an output path inside the
bundle are all refusals. The frozen example lives in
`tests/fixtures/replay/synthetic_2026-09-08/` with its report beside it; both
are reproduced byte for byte by `tests/regression/test_synthetic_replay_fixture.py`.

### Event files — `research-events/2.1.34`

```json
{"schema_version": "research-events/2.1.34", "origin": "SYNTHETIC", "records": [
  {"kind": "open_interest", "key": "SPXW|2026-09-08|6000|CALL",
   "event_at": "2026-09-08T09:30:00-04:00", "available_at": "2026-09-08T09:30:00-04:00",
   "sequence": 0, "data": {"quantity": 0, "as_of": "2026-09-04"}}
]}
```

`origin` is `SYNTHETIC` or `RECORDED_NORMALIZED`; the report lists every origin
it saw and `synthetic_only`. Every record carries an offset-aware `event_at`
(when the market state occurred), an offset-aware `available_at` (when the
research process could first have read it; never earlier than `event_at`), an
integer `sequence` for revisions of the same event, and a `data` object whose
fields are exactly those of its `kind`:

| `kind` | `key` | `data` |
|---|---|---|
| `contract_list` | `SPXW` | `contracts`: canonical option identities, no duplicates |
| `option_quote` | option identity | `bid` (may be `"0"`), `ask` as decimal strings |
| `greeks` | option identity | `implied_vol` (0.0001–5), `delta` (−1–1), `rate`, `dividend_yield`, `model_id` |
| `open_interest` | option identity | `quantity` (nonnegative integer **or `null` = unavailable**), `as_of` date |
| `spx_price` | `SPX` | `price` decimal string |
| `model_evidence` | `MODEL` | `model_ids` (unique, nonempty), `iv_source`, `iv_price_basis` |
| `futures_quote` | futures identity | `bid`, `ask` decimal strings; `bid_size`, `ask_size` integers |
| `instrument` | futures identity | `tick_size`, `point_value`, `currency` (`USD`), `valid_from`, `valid_to` |
| `costs` | futures identity | `fee_per_contract_side` (may be `"0"`), `extra_slippage_ticks`, `valid_from`, `valid_to` |

Option identities are `SPXW|YYYY-MM-DD|<normalized decimal strike>|CALL/PUT`
(`6000`, `6012.5`; never `6000.0` or `6E+3`). Futures identities name an explicit
expiration, `ROOT|YYYY-MM-DD`; there is no continuous contract. Prices and costs
are decimal strings with at most twelve integer and twelve fractional digits;
integers are bounded by `MAX_INTEGER = 10**9`. JSON is read strictly: duplicate
keys, `NaN`/`Infinity`, a BOM or non-UTF-8 bytes refuse the file. Each source
file is at most 50 MiB and is bound by its SHA-256 in the plan; two sources with
identical bytes, or two records with the same `(kind, key, event_at, sequence)`,
are refused.

### Plan — `research-replay-plan/2.1.34`

```json
{"schema_version": "research-replay-plan/2.1.34", "session_date": "2026-09-08",
 "contract": {"root": "SPXW", "max_dte": 7, "cadence_seconds": 60, "open_delay_minutes": 5,
              "close_buffer_minutes": 15, "max_market_age_seconds": 60.0,
              "max_market_skew_seconds": 2.0, "schema_version": "intraday-research-contract/2.1.33"},
 "sources": [{"path": "events.json", "sha256": "…"}],
 "fill_policy": {"latency_ms": 250, "max_wait_ms": 2000, "max_quote_age_ms": 1000},
 "probes": [{"id": "demo-35", "decision_at": "2026-09-08T09:35:00-04:00",
             "instrument": "SIMFUT|2026-09-18", "side": "BUY", "quantity": 2}]}
```

`contract` is the unchanged v2.1.33 research contract and must list every field.
`sources` are forward-slash relative paths inside the bundle (no `..`, no
absolute paths, no backslashes, no symlink that resolves outside). `probes` may
be empty; each probe's `decision_at` must be a declared grid instant of the
session, and two probes on one instrument may not have overlapping
`[arrival, deadline]` windows, because displayed liquidity may not be reused.

### Time conventions

- The **decision grid** is the contract's: 09:30 ET + `open_delay_minutes`
  through the repository calendar's session close − `close_buffer_minutes`,
  inclusive, every `cadence_seconds`. Holidays refuse the plan, early closes
  shorten the grid (371 decisions on a regular day, 191 on a 13:00 close).
- **State at an instant** is the record with the greatest `(event_at, sequence)`
  among those with `available_at <= instant`. A late delivery of an older
  event never replaces a newer known state; a revision is usable only once it
  is available. Inventory (`contract_list`) and `model_evidence` follow the same
  rule, so a later inventory or model set cannot reach an earlier decision.
- **Availability is a bound declaration, not a verified clock.** Digests prove
  the bytes match the plan; nothing here proves the vendor, the normalization
  or the clocks.
- For every decision the report lists the as-of inventory reference, the
  in-scope (0–7 calendar DTE in New York) contracts, out-of-scope inventory
  excluded, per-contract blockers from the v2.1.33 audit plus
  `CROSSED_OPTION_QUOTE` and `MODEL_NOT_AVAILABLE`, and hashes of every frame.
  An absent inventory reports `INVENTORY_NOT_AVAILABLE`; an inventory with no
  in-scope contract reports `EMPTY_AS_OF_SCOPE`; neither can pass. Explicit zero
  OI passes; `null` OI or an `as_of` other than the prior completed session
  (Friday before a Monday or a holiday Tuesday) blocks the frame.
- The contract's 60-second age and 2-second skew limits are research design
  assumptions, restated in the report's `limitations`; they are not calibrated
  strategy parameters.

### Fill probes

Each probe is an independent counterfactual with no position, portfolio, order
or PnL. It runs only when the research decision at `decision_at` passed for
every in-scope contract (`RESEARCH_FRAME_BLOCKED` otherwise).

- `arrival = decision_at + latency_ms`; `deadline = arrival + max_wait_ms`. The
  arrival must be before the research session close (`OUTSIDE_RESEARCH_SESSION`)
  and the search window ends at `min(deadline, close)`; this bounds the research,
  it is not a model of futures exchange hours. The instrument's expiration date
  (New York) must not precede the arrival date (`EXPIRED_INSTRUMENT`); intraday
  last-trade times are not modelled.
- Instrument metadata and costs are selected among the records **available at
  the decision**. Each declared event is first collapsed to its highest known
  revision (`sequence`), so a known correction supersedes every earlier version
  of the same declaration even when the corrected interval no longer covers the
  arrival; of the surviving declarations, the newest whose
  `[valid_from, valid_to)` covers the arrival applies
  (`METADATA_OR_COSTS_NOT_AVAILABLE_AT_DECISION`,
  `METADATA_OR_COSTS_NOT_EFFECTIVE_AT_ARRIVAL`), and it must still be in force at
  the fill instant (`METADATA_OR_COSTS_EXPIRED_BEFORE_FILL`). A preannounced
  future profile (a distinct `event_at`) does not hide an older still-effective
  one, and a correction delivered after the decision cannot change it.
- The candidate quote is the **first** `futures_quote` record made available in
  the window. Nothing later is examined (`NO_QUOTE_WITHIN_WAIT`). Its state must
  describe a market event at or after arrival (`QUOTE_PREDATES_ARRIVAL`, which
  also covers a delayed old row arriving while a newer pre-arrival state is
  known) and must have been delivered within `max_quote_age_ms`
  (`QUOTE_DELIVERY_TOO_OLD`). Crossed or off-tick quotes and displayed size below
  the quantity refuse (`CROSSED_QUOTE`, `OFF_TICK_QUOTE`,
  `INSUFFICIENT_DISPLAYED_SIZE`); a locked quote is allowed.
- `BUY` pays the ask plus `extra_slippage_ticks × tick_size`; `SELL` receives
  the bid minus it (`INVALID_ADVERSE_PRICE` if that is not positive). `fee` is
  `fee_per_contract_side × quantity`; `additional_slippage_cost` is
  `slippage × point_value × quantity`. The spread is paid by taking the
  executable side and is reported once as `spread_points`, not added again.
  Arithmetic is exact `Decimal` under a trapping context; a result that would
  need rounding refuses (`DECIMAL_PRECISION_EXCEEDED`).
- Every result carries the source record references it used, the observed
  update instant, `liquidity_guaranteed: false` and, when unfilled, one explicit
  `reason`. `probe_counts` totals them.

The shipped synthetic example uses bid 100.00 / ask 100.25, tick 0.25, point
value 10, quantity 2, fee 0.70 per contract per side and one adverse tick:
BUY 100.50, SELL 99.75, fee 1.40, additional slippage 5.00, total 6.40. These are
illustrative numbers, not a contract specification or observed costs.

### What the replay does not establish

`source_bytes_verified` is the only positive flag. `authenticity_verified`,
`normalization_verified`, `ready_for_backtest`, `trusted_for_gex`,
`gex_computed`, `strategy_tested` and `pnl_computed` are always `false` and
`orders_placed` is `0`. A complete declared grid means the supplied declarations
were complete, not that the exchange inventory was. Before this replay can
carry evidence about a strategy it still needs genuine, immutable intraday
option observations with measured availability, matching executable futures
quotes with sizes and effective-dated costs, portfolio and cost accounting, one
frozen strategy specification, and chronological held-out sessions.

## Native-data normalization and pilot readiness (v2.1.35)

`src/adapters/thetadata/research_events.py` turns **one verified capture
directory** (manifest, raw payloads, run intent and, when present, the attempt
log) into a `research-events/2.1.35` document, and the offline command runs the
whole chain:

```bash
python -m src.tools.normalize_thetadata_capture CAPTURE_DIR --out NEW_DIR \
    [--receipt-clock-tolerance-ms 0] [--close-buffer-minutes 15] [--label TEXT]
```

`NEW_DIR` must not exist and must be outside the capture. On success it holds
`events.json` (compact, sorted, LF), `replay-plan.json`
(`research-replay-plan/2.1.34`, default fill policy, no probes),
`replay-report.json` (`research-session-replay/2.1.34`), `pilot-readiness.json`
(`research-pilot-readiness/2.1.35`) and `pilot-readiness.md`. Exit 2 writes
nothing: a capture that fails `load_capture`, a payload whose native columns
are not the ones the rules name, a listing date that disagrees with the
capture's valuation instant, an undocumented open-interest convention, a
missing endpoint or an out-of-range tolerance are refusals.

### Rules

- **Verification first.** `load_capture` recomputes the manifest hash, every
  payload digest, the planned-request binding and the documentary readings.
  The normalizer additionally re-hashes each payload as it reads it and refuses
  ragged CSV rows.
- **Native schema.** Each endpoint has a named column set observed on the
  2026-09-02 capture (`thetadata-v3-parser/2.1.17`); extra columns are listed as
  unused, never interpreted; a missing required column refuses the run.
- **Identity.** `symbol|expiration|strike|right` canonicalised as the event
  store requires (`SPXW|2026-09-17|7600|PUT`); the vendor's `7600.000` is
  recoverable through the lineage. Rows whose identity is not in the listing,
  non-SPXW symbols and unparseable identities are excluded with named reasons.
- **Repeated identities.** A snapshot is expected to carry one row per
  identity. Rows are grouped by identity *before* anything is emitted: a group
  whose rows agree on every column the rule reads (identity, timestamp and the
  data fields) is coalesced to the lowest row index and the rest are counted
  as `IDENTICAL_DUPLICATE_COALESCED`; a group whose rows disagree in any of
  those columns -- including one row that is malformed -- is excluded whole as
  `CONFLICTING_DUPLICATE_OBSERVATIONS`, because a snapshot offers no
  authoritative order or revision among them. The identity stays in the
  inventory, so the replay refuses the frame as `MISSING_*`. Row order never
  decides which observation survives. The index payload follows the same
  policy keyed by symbol. (Revision 2 of the row rules; the first v2.1.35 cut
  kept the first row, which an independent review reproduced as a defect.)
- **Event time.** The vendor's row `timestamp`, read as America/New_York wall
  clock (ambiguous autumn hour: earlier; nonexistent spring hour: excluded).
- **Availability.** The later of the manifest record's `response_received_at`
  (inside the verified manifest hash) and the attempt log's `received_at` for
  the same request and body; the two must agree within five seconds and both
  must follow the request start. A payload with neither yields no events
  (`AVAILABILITY_UNKNOWN`). A row whose vendor time postdates the receipt is
  `VENDOR_TIMESTAMP_AFTER_RECEIPT` unless a tolerance (at most 5,000 ms) is
  given, in which case availability is deferred to the vendor time and the
  lineage says `RECEIPT_DEFERRED_TO_VENDOR_EVENT_TIME`. Nothing moves
  availability earlier, and no request time, event time or download time ever
  stands in for a receipt.
- **Open interest.** `as_of` is the re-derived documented convention
  (`OPEN_INTEREST_SETTLEMENT`, currently `PRIOR_TRADING_SESSION`) applied to the
  row's own Eastern date, so a row last updated a day earlier settles a day
  earlier and the replay refuses it as `OI_NOT_PRIOR_COMPLETED_SESSION`. A
  weekend-stamped row is excluded; an identity without a row has no event.
- **Greeks and model.** `implied_vol` in `[0.0001, 5]`, `|iv_error| <= 0.5`
  (`src/domain/iv.py`), `|delta| <= 1`, all finite. `rate` and
  `dividend_yield` are the verified request's `rate_value` and
  `annual_dividend`; `model_id` is derived from the endpoint and the verified
  `rate_type`, `rate_value`, `annual_dividend`, `version`. One `model_evidence`
  record is emitted with `event_at` = the capture's valuation instant (when the
  request plan was fixed) and `available_at` = the Greeks receipt.
- **Inventory.** One `contract_list` event at the listing receipt.
- **Origin.** `RECORDED_NORMALIZED` only when every manifest record's capture
  origin is a live transport; `OFFLINE_FIXTURE` or unknown origins yield
  `SYNTHETIC`.

### Event files — `research-events/2.1.35`

The 2.1.34 record fields plus a mandatory `lineage`, and a document-level
`provenance`:

```json
{"schema_version": "research-events/2.1.35", "origin": "RECORDED_NORMALIZED",
 "provenance": {"capture_session_id": "capture-20260902T195700Z-0bccc563691a2ad9",
   "manifest_sha256": "78ea0186…", "run_intent_sha256": "691da45a…",
   "normalizer": "thetadata-research-events/2.1.35-r2", "session_date": "2026-09-02",
   "payloads": {"/v3/option/snapshot/quote": {"sha256": "a8b8b5ac…",
                "location": "raw/capture-…-0002-v3-option-snapshot-quote-….raw"}, "…": {}}},
 "records": [
  {"kind": "option_quote", "key": "SPXW|2026-09-17|7600|PUT",
   "event_at": "2026-09-02T19:57:03.614000+00:00",
   "available_at": "2026-09-02T19:57:04.266579+00:00", "sequence": 0,
   "data": {"bid": "47.60", "ask": "48.00"},
   "lineage": {"raw_sha256": "a8b8b5ac…", "row_index": 1,
               "rule": "thetadata-v3/option_quote/2", "availability_basis": "RECEIPT"}}
 ]}
```

`row_index` is the 1-based data row (header excluded) inside the payload named
by `raw_sha256`; it is `null` for the inventory and the model evidence, which
describe a whole payload. The event store refuses a 2.1.35 document without
`provenance`, a record without `lineage`, a lineage digest the provenance does
not name, a non-positive row index or a payload location that escapes the
capture. `Event.reference()` is unchanged (`source_sha256`, `record_index`),
so a report locates a record in its hash-bound source file, and the record
carries the raw binding. Replay receipts for 2.1.35 sources add
`schema_version` and `provenance`.

### Readiness report — `research-pilot-readiness/2.1.35`

`src/replay/pilot_readiness.py` combines the normalizer's coverage with the
replay report: capture identity and hashes (events, plan, replay report),
parameters and whether they are the research default (`diagnostic`), per-
endpoint rows, emitted and excluded counts, duplicate groups, receipts and
evidence, vendor clock leads, identities with and without open interest, the eleven pilot
requirements (option inventory, quotes, IV/model inputs, prior-session OI, SPX
observations, futures bid/ask/size, futures instrument metadata, futures
costs, receive times, intraday coverage, multi-session coverage) as PRESENT,
PARTIAL or MISSING, usable and blocked decisions, blockers by decisions and by
contract-decisions, the best decision, and the trust flags, all false.
`usable_for_intraday_pilot` is true only with no MISSING requirement, at least
one passing decision and research-default parameters; PARTIAL requirements are
listed separately because their gaps are refused frame by frame.

### The September 2 capture

`docs/evidence/PILOT_READINESS_2026-09-02.md` is the research-default report:
13,536 listed contracts, 13,282 usable quotes (254 rows with vendor timestamps
up to 86 ms after the receipt excluded), 11,707 usable Greeks (1,829 with IV
outside range), 12,808 open-interest rows (728 identities without one; 496 rows
settling on 2026-08-31 because their vendor timestamp is 09-01), one SPX row
excluded because its vendor timestamp postdates the receipt by 357 ms, all five
receipts corroborated by the attempt log. **371 decisions, 0 usable**: every
payload was received at 15:57 ET, after the last 15:45 decision. The labelled
diagnostic (`…_DIAGNOSTIC.md`, close buffer 0, tolerance 1,000 ms) reaches
15:58 ET with 1,109 of 2,534 in-scope contract frames passing and 1,425 blocked
(misaligned quote/Greeks times, stale rows, missing Greeks, missing OI); it is
an exercise of the pipeline, not a research result, and no decision passes.

### Not implemented in v2.1.35

Merging several capture cycles of one session, any futures source, and any
collection loop. v2.1.36 implements the first and the third (below); a futures
source remains an operational decision outside the repository.

## Intraday session collection and multi-cycle assembly (v2.1.36)

Three offline-tested commands turn the pilot specification into an executable
runbook (`docs/INTRADAY_PILOT_COLLECTION.md`); none has been run live.

- `python -m src.tools.collect_intraday_session` runs the existing one-shot
  capture command once per approved slot. The schedule
  (`src/ingest/schedule.py`) is built from the repository calendar and
  `config/intraday_pilot.json`: slots every 60 s from the calendar open,
  09:30:00 ET, while before the calendar close (390 on a regular session, 210
  on an early close); quotes, Greeks and the index print every slot, open
  interest and the listing on the first slot and every 30 minutes. Each cycle
  is `run_capture` with an explicit `scheduled_endpoints` subset of the
  approved plan -- preflight, per-request authorisation, manifest, attempt
  log and verification unchanged, scope recorded in the run intent. A second
  approval binds the session date, schedule fingerprint, policy, budget and
  destination. One cycle is in flight: a cycle may start up to 5 s after its
  boundary; an overrunning cycle finishes, the slots it spans are
  `MISSED_OVERRUN`, and collection resumes at the next future boundary. Late
  starts and `--resume` record the passed slots the same way. The session
  stops itself after five empty cycles or a systemic refusal, and (r3) on the
  operator's interrupt wherever it strikes -- during a request the one-shot's
  partial capture is kept, the cycle is logged `OPERATOR_CANCELLED`, no later
  slot runs, and continuing is an explicit `--resume`. Every executed slot
  carries `requests` (r3): scheduled, attempted, HTTP attempts with retries,
  receipts, acquired, not attempted and begun-without-receipt, read from the
  cycle's own report and attempt log, never from the schedule. The collector
  reads one injected clock and hands it to the transport, so offline tests on
  a fake clock and a fake vendor (`tests/synthetic_session.py`) have
  deterministic receipts.
- `python -m src.tools.assemble_intraday_session` verifies the session
  structure (approvals, schedule fingerprint, every executed cycle against the
  log), normalizes each cycle under its scheduled scope (`NOT_SCHEDULED` /
  `NOT_ACQUIRED` receipts, no model evidence without Greeks) and merges the
  cycles in recorded-availability order: an observation equal to the current
  known revision of its `(kind, key, event_at)` is not emitted and the
  original keeps its receipt; a differing one is the next revision at its own
  receipt (A → B → A is three revisions); an older event arriving late is
  emitted and counted, and the replay's `(event_at, sequence)` selection keeps
  the newer state. Open interest and the inventory are reused between
  refreshes only through their original receipts; a conflicting observation
  excludes its identity for that cycle and revises nothing. Output
  `research-events/2.1.36` carries `cycle` and `request_id` in every lineage
  and every cycle's manifest and payload digests in the provenance; the event
  store refuses lineage outside them. The assembler (r3) also recounts every
  executed cycle's request activity from its `capture-summary.json` and
  `attempts/index.jsonl` and refuses a session log that disagrees. The session
  readiness (`research-pilot-readiness/2.1.36-r3`) reports `option_side_usable`
  apart from `usable_for_intraday_pilot`, which stays false without recorded
  futures, plus the request accounting and any interruption.
- `python -m src.tools.summarize_intraday_pilot` validates each readiness
  report before reading it (r3: embedded semantic `report_hash` recomputed,
  counts non-negative integers that agree with each other, origins known,
  verdicts consistent with their reasons; a report failing any check is
  refused by name) and restates the rest as `research-pilot-summary/2.1.36-r3`:
  usable decisions, coverage, request activity on the basis each report
  supports, interruptions, open-interest gaps, stale and skewed decisions,
  clock leads, request failures, overruns and restarts per session and in
  total. It refuses to mix synthetic and recorded sessions; a summary with any
  synthetic session is synthetic and never "ready". A matching hash is an
  integrity check, not vendor authenticity.

The end-to-end regression (`tests/regression/test_synthetic_session_end_to_end.py`)
collects two clean synthetic sessions, assembles, replays and summarises them:
every artefact says `SYNTHETIC`, the option side is usable on each (7 usable
decisions in the 09:35–09:41 window that was collected; everything after the
last cycle is stale, never backfilled), and the summary refuses to call the
pair ready because it is synthetic. Nothing in it is trading evidence.

### Not implemented in v2.1.36

A live collection session; a futures source; any strategy, GEX, PnL, sizing or
order logic. `docs/INTRADAY_PILOT_COLLECTION.md` §7 is the runbook for the
first live session; `docs/OPEN_DECISIONS.md` lists the choices it leaves open.

## Path to a test of profitability

1. Obtain a timestamped intraday development dataset and matching futures quote
   data. Inspect availability, coverage, latency and spread distributions before
   choosing strategy parameters. Keep the close captures as diagnostics.
2. Feed that data through the v2.1.36 collector and assembler, the v2.1.35 normalizer rules and the v2.1.34 source-verified replay and fill probes,
   then add portfolio/cost accounting. Freeze one simple baseline and the
   identical strategy with one GEX input added, including its model-uncertainty
   treatment. Record every variant.
3. Use chronological development/validation/test sessions. Keep whole sessions
   together, purge overlapping forward labels and prevent later model evidence
   from entering earlier decisions. Specify evaluation metrics and acceptance
   criteria before looking at test-session PnL; do not invent a minimum sample
   size from the number of available snapshots.
4. Compare net returns, turnover, drawdown, fill sensitivity and uncertainty
   clustered by session, including rejected signals and losing days. Reserve a
   final untouched period and stress execution costs. Positive gross PnL or a
   lucky subset is insufficient.
5. Only after reproducible net evidence, implement paper execution and compare
   observed fills with the backtest. The engine currently cannot trade.
