# Data dictionary

Every field a consumer sees, what it means, and what it does *not* mean.

---

## `OptionQuote`

| Field | Type | Notes |
|---|---|---|
| `contract` | `OptionContract` | identity: root, expiry, strike, right, multiplier |
| `timestamps` | `ContractTimestamps` | all eight clocks, see below |
| `bid` / `ask` | `float \| None` | `None` means not supplied, not zero |
| `bid_size` / `ask_size` | `int \| None` | |
| `open_interest` | `int \| None` | `None` (unknown) and `0` (known empty) are different |
| `iv` | `ImpliedVolQuote` | never a bare float — always carries its source |
| `delta` / `gamma` / `vega` / `theta` | `float \| None` | vendor-supplied; `gamma` absent below Pro tier |
| `underlying_price` | `float \| None` | per contract, so a vendor disagreeing across expiries is measurable |

Derived: `effective_iv` (the IV the engine will price with, `None` when quality is
unusable), `mid`, `spread`, `spread_pct_of_mid`, `is_crossed`, `is_locked`,
`is_zero_bid`, `is_quotable`.

`effective_iv` is named that rather than `implied_vol` so that reading a bare
volatility off a quote is impossible without also having `quote.iv.source`.

ThetaData support confirmed in v2.1.30 that an absent OI row is ambiguous: it
can represent zero or a message that is not yet available, and no status field
distinguishes those cases. `None` therefore remains unavailable and excluded;
the approximately 06:30 ET delivery schedule never converts it to `0`.

## `ContractTimestamps`

| Field | Type | Meaning |
|---|---|---|
| `quote_timestamp` | `datetime \| None` | when the book was observed |
| `greeks_timestamp` | `datetime \| None` | when second-order greeks were computed |
| `iv_timestamp` | `datetime \| None` | when IV was computed |
| `underlying_timestamp` | `datetime \| None` | the underlying print used for greeks |
| `open_interest_as_of` | `date \| None` | settlement **date**, not an instant |
| `request_started_at` | `datetime \| None` | our clock |
| `response_received_at` | `datetime \| None` | our clock |
| `normalized_at` | `datetime \| None` | our clock |

All timezone-aware. A naive datetime is rejected, never assumed — assuming is how
16:00 ET becomes 16:00 UTC and every 0DTE gamma goes wrong by four hours.

Derived: `internal_spread_seconds`, `skew_seconds(a, b)`, `round_trip_seconds()`.

## `ImpliedVolQuote`

| Field | Meaning |
|---|---|
| `value` | the IV that will be used |
| `source` | `NBBO_BID_IV` / `NBBO_MID_IV` / `NBBO_ASK_IV` / `TRADE_IV` / `VENDOR_DEFAULT_IV` / `LOCALLY_SOLVED_MID_IV` |
| `quality` | `OK` / `SINGLE_SIDED` / `ZERO_BID` / `CROSSED_MARKET` / `WIDE_SPREAD` / `SOLVER_FAILED` / `VENDOR_ERROR` / `OUT_OF_RANGE` / `NON_FINITE_INPUT` / `MISSING` |
| `bid_iv` / `mid_iv` / `ask_iv` | all three legs retained when available |
| `vendor_iv_error` | the vendor's own solver residual |
| `iv_spread` | `ask_iv − bid_iv`; `None` on a one-sided book |

`VENDOR_DEFAULT_IV` means the vendor did not document which price it implied
from — a known unknown, labelled rather than assumed to be mid.

`NON_FINITE_INPUT` means the vendor sent NaN or an infinity. The value is
sanitised to `None` so it cannot reach the pricer, but the flag survives so
validation reports a data error rather than "not supplied".

## `ChainSnapshot`

| Field | Meaning |
|---|---|
| `as_of` | the **request** instant; the reference for freshness and future-drift checks. Explicitly *not* any record's timestamp |
| `spot` | index level; must be finite and positive |
| `quotes` | the chain |
| `risk_free_rate` / `dividend_yield` | pricing inputs |
| `clocks` | request/response/normalised, shared by the pull |
| `spot_timestamp` | when the spot print was taken |
| `source` | provenance string |
| `expected_contract_count` | what the adapter expected; feeds `chain_completeness` |

Derived: `options_feed_timestamp` (the **oldest** quote clock — a partially
refreshed chain must report as stale, the safe direction), `open_interest_as_of`
(oldest, same reasoning), `expiries`, `strikes`.

---

## `GexSnapshot`

### Totals

| Field | Meaning |
|---|---|
| `total_unsigned_gex` | view 1: dollars of dealer delta to re-hedge per 1% spot move, direction-agnostic |
| `total_signed_gex` | view 2: the same under a **proxy** sign convention |
| `contract_count` | contracts that survived validation and filtering |
| `total_open_interest` | across those contracts |
| `sign_convention` | which proxy produced the sign |
| `model_spec` | every pricing assumption |
| `config_fingerprint` | traces the snapshot to the config file that produced it |

### `BucketGex` (view 3)

`bucket`, `unsigned_gex`, `signed_gex`, `contract_count`, `open_interest`.
All five buckets always present, zero-filled when empty.

### `StrikeGex` (view 4)

`strike`, `call_gex`, `put_gex`, `unsigned_gex`, `signed_gex`,
`call_open_interest`, `put_open_interest`.

### `WallSet`

**Neutral observations** (facts):

| Field | Meaning |
|---|---|
| `largest_call_gamma_strike` | strike with the most call gamma, wherever it is |
| `largest_put_gamma_strike` | strike with the most put gamma |
| `largest_unsigned_gamma_strike` | strike with the most total gamma |

**Directional interpretations** (claims, `None` when nothing qualifies):

| Field | Meaning |
|---|---|
| `upside_call_wall` | largest call gamma **above** spot |
| `downside_put_wall` | largest put gamma **below** spot |

A `None` here means no qualifying strike exists. It is never silently replaced
with a same-side or opposite-side substitute.

`positive_gamma_nodes` / `negative_gamma_nodes`: strikes carrying at least
`node_min_share_of_max` of the peak, ranked by signed magnitude then by strike.

### `GammaVoid`

`low_strike`, `high_strike`, `width`, `kind`, `detail`, `missing_strike_count`,
`observed_strike_count`, `max_unsigned_gex_in_range`.

Only `TRUE_LOW_GEX_VOID` has `is_tradable_structure = True`. Every other kind is
a data artefact. `WallSet.tradable_voids` filters accordingly.

### `ZeroGammaResult` (view 5)

| Field | Meaning |
|---|---|
| `selected_root` | nearest crossing to spot — a **reporting convention** |
| `all_roots` | every crossing found |
| `root_count` | how many |
| `selection_method` | `nearest_to_spot` / `none_found` / `curve_identically_zero` / `convention_unimplemented` |
| `selected_root_distance_from_spot_pct` | signed |
| `local_slope_at_selected_root` | dGEX/dS across the bracketing interval |
| `normalised_slope` | slope scaled by max abs GEX — comparable across chains |
| `nearest_root_spacing_pct` | gap to the next root, as % of spot |
| `grid_lower_bound` / `grid_upper_bound` / `grid_points` | the search window |
| `grid_expansions` | how many bounded widenings were applied |
| `root_near_boundary` | the root may be an artefact of where the search stopped |
| `identically_zero_curve` | no level exists; `selected_root` is `None` |
| `no_root_found` | no sign change inside the (possibly expanded) grid |
| `max_abs_gex_on_grid` | scale reference for the slope |
| `unimplemented_reason` | set for `STICKY_DELTA` / `SURFACE_REFIT` |
| `curve` | the full `(spot, signed_gex)` series |

### `OptionUniverse`

| Field | Meaning |
|---|---|
| `total_contract_count` / `included_contract_count` / `excluded_contract_count` | counts |
| `included_expirations` / `excluded_expirations` | ISO dates |
| `max_dte_used` | the cap applied, if any |
| `included_unsigned_gex` / `excluded_unsigned_gex` | how much gamma each side carries |
| `included_unsigned_gex_share` / `excluded_unsigned_gex_share` | as fractions |
| `coverage_ratio` | contracts included / total |
| `filter_reasons` | counts by reason |

Reported **twice**: `chain_universe` for the totals, `zero_gamma_universe` for the
grid. They are different populations, and comparing across them without knowing
that invites a false conclusion.

### `ValidationReport`

`total`, `accepted`, `accepted_with_warning`, `rejected`, `acceptance_ratio`,
`error_counts`, `warning_counts`, `examples` (bounded at 25).

### `ConfidenceScore`

`score` (0–100), `calibrated`, `components`, `warnings`, `hard_failures`.

**`calibrated` is a research flag, not an enforcement mechanism.** There is no
risk engine in this repository and nothing consumes it. It reports that market
thresholds are still `UNSPECIFIED_CALIBRATE`.

Components (17): `chain_completeness`, `quote_freshness`, `oi_freshness`,
`crossed_market_penalty`, `zero_gamma_stability`*, `sign_model_agreement`*,
`0dte_dominance_alert`*, `vendor_lag_alert`, `multiple_root_penalty`,
`root_slope_score`, `root_boundary_penalty`, `root_identity_stability`,
`timestamp_alignment_score`, `future_timestamp_penalty`,
`option_universe_coverage_score`, `iv_spread_quality`,
`model_parameter_completeness`.

`*` = threshold is still a sentinel.

Each carries `name`, `score` (0–1), `weight`, `detail`, `uncalibrated`,
`hard_failure`. A `hard_failure` zeroes the entire score rather than reducing it.

### Serialisation

`as_dict()` returns JSON-safe primitives. `output_hash()` returns a SHA-256 over
the numeric content with floats quantised to 12 significant figures, excluding
warning prose and validation examples — a hash that trips on a reworded warning
is a hash nobody trusts.


---

## v2.1.1 fields

Status: `IMPLEMENTED` · `TESTED_SYNTHETICALLY` · `NOT_VALIDATED_WITH_LIVE_THETADATA`.

### `completeness_status` (on `ChainSnapshot`, and in `meta.chain_completeness`)

| Value | Means | Does **not** mean |
|---|---|---|
| `MEASURED_COMPLETE` | An independent universe was supplied and every member arrived | that the universe itself was correct |
| `MEASURED_INCOMPLETE` | An independent universe was supplied and some of it did not arrive | which contracts are missing, unless `missing_by_source` says |
| `PARTIALLY_OBSERVED` | Rows arrived and joined; nothing independent says how many were owed | that the chain is whole. It may be page one of a truncated response |
| `UNKNOWN` | Not even the received counts are meaningful | that an error occurred |

`expected_contract_count` is `None` whenever no independent universe exists, and
**must stay `None`**. Substituting the received count is what let a truncated
chain score full completeness in v2.1.

### `chain_completeness` score component

`score` is `None` when completeness is not measured — distinct from `0.0`, which
would assert the chain is bad. A `None` component is excluded from the weighted
mean and carries `warning_code = CHAIN_COMPLETENESS_NOT_INDEPENDENTLY_OBSERVED`.

### `meta.timestamp_localization`

Per-source summaries keyed by `TimestampSource` (`quote`, `open_interest`,
`first_order_greeks`, `second_order_greeks`, `underlying`). Each carries
`rows_seen`, `naive_rows_localized`, `aware_rows_preserved`, `invalid_rows` and
`assumed_timezone` — the last is `null` unless an assumption was actually applied
*to that source*. `any_assumption_applied` rolls them up.

A chain-wide boolean cannot express "aware quotes, naive greeks", which is why
v2.1's single flag reported no assumption while assuming one for every greek.

### `meta.parser_version`

`thetadata-v3-parser/2.1.1`. Defined once, in `src/adapters/raw_store.py`, and
carried into the replay hash. If parsing behaviour changes and this does not, a
replay cannot detect the change.

### `exclusions.no_underlying_price`

Contracts excluded from current GEX because the *selected* underlying-price
source produced nothing usable. A vendor gamma does not rescue these: gamma is
one factor of the product and spot² is another.

### `parse_issues` (on `OptionQuote`)

`(field, code)` pairs. Codes come from `IntegerParseIssue` or `FloatParseIssue`.
`missing_value` is **not** recorded — absence is ordinary and would drown the
signal. `malformed_value` and `non_finite_input` are, because those are the
vendor sending something wrong rather than sending nothing.

### `IntegrityReport` (from `FileRawStore.verify_integrity()`)

Classifies each artefact as `VALID`, `ORPHAN_PAYLOAD`, `MISSING_PAYLOAD`,
`HASH_MISMATCH`, `SIZE_MISMATCH`, `INCOMPLETE_WRITE`, `DUPLICATE_ID` or
`INVALID_METADATA`. `recovery_plan()` returns proposed actions as strings and
executes nothing — deleting an artefact destroys the evidence of how the store
came apart.


---

## v2.1.2 fields

Status: `IMPLEMENTED` | `TESTED_SYNTHETICALLY` | `NOT_VALIDATED_WITH_LIVE_THETADATA`.

### `chain_completeness` (identity-based)

| Field | Means | Does **not** mean |
|---|---|---|
| `expected_identity_count` | distinct identities an independent source predicted | that the prediction was right |
| `matched_identity_count` | predicted identities that arrived | anything about the ones that did not |
| `missing_expected_identities` | sorted, bounded to 100 entries | the full list -- see `missing_expected_count` |
| `unexpected_received_identities` | arrived but unpredicted | that they are wrong; the expectation may be |
| `identity_completeness_ratio` | matched / expected, capped at 1.0 | received / expected -- extras cannot compensate for a miss |

`MEASURED_COMPLETE_WITH_EXTRAS` is a distinct status: the chain is whole and the
*expectation* was incomplete. Counting alone cannot distinguish that from a
chain that is short by the same number.

### `model_distribution`

`iv_source_counts`, `gamma_source_counts`,
`effective_model_fingerprint_counts`, `fallback_reason_counts`, plus
`mixed_iv_sources` / `mixed_gamma_sources` / `mixed_effective_models`.

Counts are of *included* contracts and are sorted. `model_fingerprint` in the
snapshot metadata is the **configured** spec; when `mixed_effective_models` is
true, no single fingerprint describes the chain.

### `model_completeness`

`static_model_complete` and `static_missing_inputs` are properties of the
configuration and hold for an empty chain. `resolved_contract_count`,
`unresolved_contract_count` and `per_input_failure_counts` describe the data.

The two are separate because v2.1.1 conflated them and lost the first: an empty
result set reported a fully specified model.

### `pricing_compatibility`

A list of `dimensions`, one per `PricingDimension`, each carrying a `status`, a
machine-readable `code`, the two values, and an `evidence` fingerprint when one
resolved it. `compatible` is **derived** from them and from `hard_failures`; it
is not a field anybody sets.

Three statuses, three remedies. `MISMATCHED` means "we checked and they differ"
and needs a config change; `UNKNOWN` means "we cannot tell" and needs vendor
documentation or a live comparison; `NOT_APPLICABLE` means the dimension does
not arise in this configuration. The first two both block a calculation on a
load-bearing dimension, and neither is silence.

For convenience the serialised form also carries `load_bearing_unknowns` and
`load_bearing_mismatches` as flat lists of dimension names.

v2.1.3 wrote `compatible_fields` / `incompatible_fields` / `unknown_fields`:
lists of *sentences*, with a settable `compatible` flag beside them. Which
unknowns blocked was decided by searching those sentences for a field name, so
rewording one changed what the report meant. Nothing emits those keys now.

`dimension_detail` and `warnings` are prose, and are excluded from the replay
hash wherever they appear -- see `warning_codes` below.

### `selected_timestamp_sources`

Per role (`quote`, `implied_vol`, `underlying`, `open_interest`, `gamma`): which
vendor record supplied the clock, and whether a timezone was assumed **for that
record**. Aggregated into counts on the snapshot.

Distinct from `timestamp_localization`, which counts every source *inspected*.

### `warning_codes` (in the replay hash)

Deterministic, sorted, deduplicated. Per-component `warning_code` is hashed too.
Free-form `detail` prose is not: rewording a message is not a change in the
finding, but reporting a new condition is.

### Parse issue codes

`VENDOR_GAMMA_MALFORMED`, `VENDOR_GAMMA_NON_FINITE` and
`VENDOR_GAMMA_MISSING` are recorded only when a second-order record actually
arrived. No second-order response at all is the normal Standard-tier case and is
not a finding.

### `AdapterCertificationReadiness`

`ready`, `blockers`, `warnings`, `verified_fields`, `unverified_fields`, `scope`,
`trading_enabled` (always `False`). See
[ADAPTER_CERTIFICATION.md](ADAPTER_CERTIFICATION.md).


---

## v2.1.3 fields

### `pipeline` (in `ChainSnapshot.meta` and `GexSnapshot.meta`)

`pipeline_fingerprint`, `pricing_mode`, `pricing_compatibility`,
`subscription_capability`, `load_bearing_unknowns`, `model_fingerprint`,
`iv_source`. A GEX number can show which compatibility decision permitted it.

### `raw_capture_manifest`

`session_id`, `record_ids`, `request_ids`, `payload_hashes`, `record_count`,
`manifest_hash`, `capture_enabled`. Links a normalized snapshot to the exact raw
records it was built from. `capture_enabled: false` is stated explicitly:
absent metadata reads the same as forgotten metadata.

### `RateAssumption` / `DividendAssumption`

Rate: `source`, `raw_value`, `unit` (`DECIMAL_ANNUAL_RATE`,
`PERCENT_ANNUAL_RATE`, `UNKNOWN`), `normalized`, `vendor_default`.
Dividend: `convention` (`ANNUAL_CASH_DIVIDEND`, `CONTINUOUS_DIVIDEND_YIELD`,
`ZERO_DIVIDEND`, `UNKNOWN_VENDOR_CONVENTION`), `value`.

### `CertificationState`

`NOT_READY`, `READY_FOR_RAW_CAPTURE_ONLY`, `RAW_CAPTURE_COMPLETED`,
`CALCULATION_NOT_VALIDATED`, `CALCULATION_VALIDATED`,
`ADAPTER_CERTIFIED`. The last requires both a live capture and a validation
report, so it is unreachable offline by construction.

### `zero_gamma_root_count_stable`

Renamed from `zero_gamma_root_identity_stable`. It compares root *counts*. Two
runs with the same number of roots at different levels are count-stable and not
identity-stable; `match_roots` answers the identity question.

### `effective_model`

`None` when the chain was priced under more than one effective model. Read
`model_distribution` instead, which can answer honestly.

## v2.1.34 fields (`research-session-replay/2.1.34`)

Emitted by `python -m src.tools.replay_research_session`; the event and plan
formats it reads are specified in `INTRADAY_RESEARCH.md`.

### Report

`schema_version`, `session_date`, `plan_sha256` (digest of the plan bytes),
`contract` and `contract_hash` (the unchanged v2.1.33 research contract),
`sources` (one receipt per bound file: `source_sha256`, `bytes`, `origin`,
`records`), `observed_source_origins`, `synthetic_only`, `expected_decisions`,
`passing_decisions`, `blocked_decisions`, `declared_grid_complete`,
`decisions`, `fill_policy`, `fill_probes`, `probe_counts`, `limitations`,
`report_hash`. `declared_grid_complete` means every declared grid instant
passed for every in-scope contract of the *supplied* inventory; it is not
exchange completeness.

Trust flags: `source_bytes_verified` is `true` whenever a report exists (a
mismatch refuses the run). `authenticity_verified`, `normalization_verified`,
`ready_for_backtest`, `trusted_for_gex`, `gex_computed`, `strategy_tested` and
`pnl_computed` are always `false`; `orders_placed` is always `0`.

### `decisions[]`

`decision_at` (UTC), `inventory_source` (`source_sha256`, `record_index`, or
`null`), `expected_contracts` (as-of in-scope identities),
`excluded_inventory_contracts` (supplied but outside 0–7 DTE),
`passing_contracts`, `blocked_contracts`, `blocker_counts` (v2.1.33 audit
blockers plus `CROSSED_OPTION_QUOTE`, `MODEL_NOT_AVAILABLE`,
`INVENTORY_NOT_AVAILABLE`, `EMPTY_AS_OF_SCOPE`), `decision_passed`,
`frame_set_hash` (digest over every per-contract frame hash), `decision_hash`.

### `fill_probes[]`

`probe` (the declared probe), `arrival_at`, `deadline`, `status`
(`SIMULATED_FILL` or `UNFILLED`), `reason` (`null` when filled, otherwise one
of `RESEARCH_FRAME_BLOCKED`, `EXPIRED_INSTRUMENT`, `OUTSIDE_RESEARCH_SESSION`,
`METADATA_OR_COSTS_NOT_AVAILABLE_AT_DECISION`,
`METADATA_OR_COSTS_NOT_EFFECTIVE_AT_ARRIVAL`, `NO_QUOTE_WITHIN_WAIT`,
`QUOTE_PREDATES_ARRIVAL`, `QUOTE_DELIVERY_TOO_OLD`,
`METADATA_OR_COSTS_EXPIRED_BEFORE_FILL`, `CROSSED_QUOTE`, `OFF_TICK_QUOTE`,
`INSUFFICIENT_DISPLAYED_SIZE`, `INVALID_ADVERSE_PRICE`,
`DECIMAL_PRECISION_EXCEEDED`). Once profiles were selected: `instrument_source`,
`cost_source`. Once an update was observed: `update_at`, `quote_source`,
`quote_event_at`, `quote_available_at`. On a simulated fill: `fill_at`, `price`
(executable side plus adverse slippage), `quoted_side_price`, `spread_points`
(reported once, never added to costs), `fee`, `additional_slippage_cost`,
`fee_plus_additional_slippage`, `currency`, `point_value`,
`liquidity_guaranteed` (always `false`). Prices and costs are decimal strings.

### `probe_counts`

`total`, `simulated_fills`, `unfilled`, `refusal_reasons` (reason → count).

## v2.1.35 fields

### `research-events/2.1.35` (emitted by `src/adapters/thetadata/research_events.py`)

Document: `schema_version`, `origin` (`RECORDED_NORMALIZED` only when every
manifest record's capture origin is a live transport, else `SYNTHETIC`),
`provenance`, `records`.

`provenance`: `capture_session_id`, `manifest_sha256` (the verified manifest
hash), `run_intent_sha256` (digest of `run-intent.json`), `normalizer`
(`thetadata-research-events/2.1.35`), `session_date` (the verified listing
`date` parameter, cross-checked against the capture's valuation instant),
`payloads` (endpoint → `sha256`, `location` relative to the capture directory).

`records[]`: the 2.1.34 fields (`kind`, `key`, `event_at`, `available_at`,
`sequence`, `data`) plus `lineage`: `raw_sha256` (must be one of the
provenance payload digests), `row_index` (1-based data row inside that payload,
`null` for the whole-payload `contract_list` and `model_evidence` records),
`rule` (`thetadata-v3/<kind>/2` for quote, Greeks, open-interest and index
rows; `/1` for the inventory and model evidence), `availability_basis` (`RECEIPT`,
`RECEIPT_DEFERRED_TO_VENDOR_EVENT_TIME`, `GREEKS_RECEIPT`).

Replay receipts (`sources[]`) for such files add `schema_version` and the
`provenance` object; 2.1.34 receipts are unchanged.

### `research-pilot-readiness/2.1.35` (emitted by `python -m src.tools.normalize_thetadata_capture`)

`schema_version`, `label`, `session_date`, `generated_from` (`normalizer`,
`capture` identity, `events_sha256`, `plan_sha256`, `replay_report_hash`,
`replay_schema_version`), `parameters` (`receipt_clock_tolerance_ms`,
`contract`, `contract_hash`, `contract_is_research_default`, `diagnostic`),
`coverage` (the normalizer's accounting: `origin`, `capture_origins`,
`capture`, `session_date`, `vendor_timestamp_policy`, `attempt_log`,
`endpoints` with `rows`, `emitted`, `excluded` by reason, `columns`,
`unused_columns`, `receipt` (`request_started_at`, `manifest_received_at`,
`attempt_received_at`, `available_at`, `evidence`, `refusal`),
`vendor_event_time`, `vendor_clock_lead`, `duplicate_groups`
(`identical_coalesced`, `conflicting_excluded`, `conflicting_keys`);
`model_evidence`; `open_interest`
with `settlement_rule`, `as_of_basis`, `rows_by_as_of`; `identities`;
`records_by_kind`), `replay` (`expected_decisions`, `passing_decisions`,
`blocked_decisions`, `grid`, `decisions_with_inventory`,
`blocker_decision_counts`, `blocker_contract_decision_counts`,
`best_decision`, `fill_probes`), `requirements[]` (`requirement`,
`description`, `status` PRESENT/PARTIAL/MISSING, `detail`, `source`),
`usable_decisions`, `blocked_decisions`, `usable_for_intraday_pilot`,
`blocking_reasons` (MISSING requirements plus `NO_PASSING_DECISIONS` and
`DIAGNOSTIC_PARAMETERS_NOT_RESEARCH_DEFAULT`), `partial_requirements`,
`observed_source_origins`, `synthetic_only`, the trust flags (all `false`),
`orders_placed` (`0`), `limitations`, `report_hash`.

Exclusion reasons the normalizer can report per endpoint:
`AVAILABILITY_UNKNOWN`, `RECEIVE_TIME_CONFLICT`, `RECEIVED_BEFORE_REQUEST`,
`INVALID_IDENTITY`, `UNEXPECTED_SYMBOL`, `NOT_IN_INVENTORY`,
`DUPLICATE_IDENTITY` (inventory rows), `IDENTICAL_DUPLICATE_COALESCED`,
`CONFLICTING_DUPLICATE_OBSERVATIONS`, `UNPARSEABLE_TIMESTAMP`,
`NONEXISTENT_WALL_CLOCK`,
`VENDOR_TIMESTAMP_AFTER_RECEIPT`, `INVALID_PRICE`, `ZERO_OR_INVALID_ASK`,
`INVALID_OPEN_INTEREST`, `OI_TIMESTAMP_NOT_TRADING_SESSION`,
`NON_FINITE_INPUT`, `IV_OUT_OF_RANGE`, `VENDOR_IV_ERROR`,
`DELTA_OUT_OF_RANGE`, `MODEL_FIXED_AFTER_GREEKS_RECEIPT`. Counts describe rows
refused under these rules; they are not vendor error rates.

## v2.1.36 fields

### Coverage additions of the single-capture normalizer (`thetadata-research-events/2.1.36`)

`scheduled_endpoints` (the scope the capture was to issue: all five for a
single capture), `acquired_endpoints`, `unacquired_endpoints`,
`inventory_membership_checked` (false for a cycle without a listing). Each
endpoint's `receipt` adds `request_id` (the manifest record's logical request
id) and `detail` (for an unacquired endpoint: `attempts`, `last_status_code`,
`last_started_at`, `last_received_at`, `succeeded` from the verified attempt
log; otherwise `null`). Two new receipt refusals: `NOT_SCHEDULED` (outside the
cycle's scope) and `NOT_ACQUIRED` (scheduled, no verified payload). The
provenance `normalizer` is `thetadata-research-events/2.1.36`; the row rules
stay at `thetadata-v3/<kind>/2`.

### `intraday-collection-schedule/2.1.36` (`src/ingest/schedule.py`)

`session_date`, `policy` (`cadence_seconds`, `refresh_every_seconds`,
`start_tolerance_seconds`, `max_consecutive_failed_cycles`, `contract`,
`one_cycle_in_flight`, `missed_slot_policy`), `session_open`,
`session_close`, `early_close`, `preparation_opens` (midnight Eastern of the
session day), `slots[]` (`index`, `label` HHMMSS Eastern, `scheduled_at`,
`kind` FULL/MARKET, `scope`), `request_budget` (`cycles`, `requests`,
`max_attempts_per_request`, `max_attempts`), `decision_coverage`,
`endpoint_cadence`, `fingerprint` (digest of everything above).

### `intraday-session-intent/2.1.36`, `-approval`, `-log`, `-summary` (`src/ingest/session_collector.py`)

Intent (`session-intent.json`): `collector_version`, `mode` (`LIVE` or
`OFFLINE_TRANSPORT`), `session_date`, `created_at`, `config_path`,
`schedule`, `policy`, `cycle_approval` (the one-shot's preflight approval),
`session_approval` (`session_date`, `cycle_approval_hash`,
`schedule_fingerprint`, `destination`, `request_budget`, `approval_hash`),
`request_budget`, `destination`, `expected_capture_origin`,
`pipeline_fingerprint`, `capture_plan_fingerprint`, `market_session`,
`overrides` (both `false`, always).

Log (`session-log.jsonl`, one JSON object per line; schema
`intraday-session-log/2.1.36-r3`): `event` `SESSION_START` / `RESTART` /
`SLOT` / `STOP` / `SESSION_END`. A `SLOT` entry carries `slot`, `label`,
`scheduled_at`, `kind`, `scope`, `status` (`EXECUTED`, `FAILED_TO_START`,
`MISSED_OVERRUN`, `MISSED_RESTART_GAP`, `MISSED_LATE_START`, `STOPPED`) and,
when executed, `started_at`, `finished_at`, `duration_seconds`,
`start_delay_seconds`, `cycle_dir`, `run_state`, `acquired`, `missing`,
`manifest_hash`, `capture_session_id`, `stop_reason`, `error_code`,
`operator_cancelled` (r3: the one-shot reported the operator's interrupt --
`raw_acquisition.stop_reason` `OPERATOR_CANCELLED`, or the typed code
`INTERNAL_ERROR:KeyboardInterrupt` of a bootstrap or finalization failure),
`overran_next_boundary` and `requests` (r3, below); a missed slot carries
`observed_at`, `late_by_seconds`, `cause`. A `FAILED_TO_START` entry carries
`error_message` and `requests`; when the one-shot claimed the cycle directory
but failed or was interrupted before its first request it also carries
`cycle_dir`, `report_path` (`capture-bootstrap-failure.json`), `run_state`
(`FAILED_BEFORE_REQUEST`), `error_code` and `operator_cancelled`, and its
`requests` are certain zeros with basis
`BOOTSTRAP_FAILURE_REPORT_BEFORE_ANY_REQUEST`.

`requests` (r3, per slot; `src/ingest/session_collector.py:request_accounting`):
`scheduled` (the slot's approved scope), `attempted` (logical requests the
sweep began, `raw_acquisition.attempted_endpoints`), `http_attempts` (every
record of the cycle's attempt log, retries included), `http_attempts_failed`,
`with_receipt` (scheduled endpoints with at least one attempt record),
`acquired` (payloads that verified), `not_attempted` (scheduled endpoints
never begun: a systemic stop, a cancellation), `without_receipt` (begun but
without an attempt record -- a request in flight when the operator
interrupted; the explicit uncertainty, never counted as attempted-and-answered),
`attempt_evidence_verified` (the one-shot's `attempt_evidence.ok`; `null` when
there is no attempt log to verify) and `basis` (`CYCLE_REPORT_AND_ATTEMPT_LOG`,
`CYCLE_DID_NOT_START` -- everything but `scheduled` is `null` -- or the
bootstrap basis above). Nothing in it is inferred from the schedule.

A `STOP` entry carries `at`, `reason` and, for `OPERATOR_INTERRUPT`,
`interruption`: `slot`, `phase` (`REQUEST`, `WAIT`, `BETWEEN_CYCLES`),
`partial_capture_preserved`, `cycle_dir`, `continuing_requires`.

Summary (`session-summary.json`, schema `intraday-session-summary/2.1.36-r3`):
`status` (`COMPLETED`, `STOPPED`, `INTERRUPTED`), `stop_reason`
(`OPERATOR_INTERRUPT` for every operator interruption, wherever it struck),
`slots_planned`, `slots_by_status`, `cycles_executed`,
`cycles_with_every_scheduled_endpoint`, `cycles_overrunning_a_boundary`,
`endpoint_failures`, `requests` (the per-slot accounting summed:
`scheduled`, `attempted`, `http_attempts`, `http_attempts_failed`,
`with_receipt`, `acquired`, `not_attempted`, `without_receipt`,
`cycles_with_unverified_attempt_evidence`, `cycles_without_a_report`,
`basis`; replaces the 2.1.36 `requests_issued`, which summed the scheduled
scope of executed slots and was not a count of requests), `request_budget`,
`interruption` (the `STOP` entry's record, or `null`), `restarts`,
`cycle_duration_seconds`, `log_entries`, `session_root`, `ended_at`.

### `research-events/2.1.36` (emitted by `src/adapters/thetadata/session_assembly.py`)

Document: `schema_version`, `origin`, `provenance`, `records`.

`provenance`: `session_date`, `session_approval_hash`, `schedule_fingerprint`,
`session_intent_sha256`, `session_log_sha256`, `collector`, `normalizer`,
`assembler` (`thetadata-session-assembly/2.1.36`), `cycles` (label →
`capture_session_id`, `manifest_sha256`, `run_intent_sha256`, `payloads`
(endpoint → `sha256`, `location` relative to the session directory)).

`records[]`: the 2.1.35 fields; `lineage` adds `cycle` (must name a
provenance cycle whose payloads include `raw_sha256`) and `request_id`.
`sequence` counts revisions of one `(kind, key, event_at)` in availability
order across cycles. Replay receipts carry `schema_version` and the session
`provenance`.

### `intraday-session-assembly/2.1.36-r3` (`session-assembly.json`)

`assembler` (`thetadata-session-assembly/2.1.36-r3`), `normalizer`,
`collector`, `origin`, `capture_origins`, `session_date`, `verification`
(`intent_sha256`, `log_sha256`, `executed_cycles`, `orphan_cycle_directories`,
`findings`, `structure_verified`; r3 adds the findings
`LOG_SCHEMA_UNSUPPORTED:<schema>` -- the log must be
`intraday-session-log/2.1.36-r3` -- `SLOT_WITHOUT_REQUEST_ACCOUNTING:<label>`,
`CYCLE_REQUESTS_NOT_RECOUNTABLE:<label>:<why>` and
`CYCLE_REQUESTS_DIFFER_FROM_LOG:<label>:<keys>`, raised when the accounting
the collector logged is not what the cycle's `capture-summary.json` and
`attempts/index.jsonl` say now, key by key), `session` (mode, approvals,
policy, budget, slots by status, cycles executed / assembled / skipped /
complete / overrunning, restarts, `stops[]` (`at`, `reason`, `interruption`),
ended, `endpoint_failures`, `requests` -- the collector's per-slot accounting
summed, plus `cycles_recounted_from_evidence` and
`operator_cancelled_cycles`; replaces `requests_issued`),
`receipt_clock_tolerance_ms`, `merge` (`rule`, `outcomes_by_kind` with
`NEW_EVENT`, `REOBSERVED_UNCHANGED`, `REVISION`, `REVERTED_REVISION`,
`LATE_OLDER_EVENT`; `membership` with `NOT_IN_LATEST_INVENTORY`,
`UNCHECKED_NO_INVENTORY_YET`; `ambiguity` with `incidents[]` (`cycle`,
`kind`, `key`, `observed_at`, `outcome` `AMBIGUOUS_AFTER_KNOWN_STATE` /
`AMBIGUOUS_WITHOUT_KNOWN_STATE`, `known_event_at`, `effect`),
`after_known_state`, `without_known_state`, `policy`), `records`,
`records_by_kind`, `identities`, `cadence` (`inventory_cycles`,
`open_interest_cycles`, `quote_cycles`, `greeks_cycles`, `inventory_events`),
`vendor_clock_lead`, `cycles[]` (per cycle: slot, instants, duration, delay,
overrun, `run_state`, `stop_reason`, `operator_cancelled`, `requests`
(recounted from the cycle directory), scheduled / acquired / unacquired
endpoints with attempt detail, capture session id, manifest, first and last
receipt, records by kind, exclusions, conflicting groups, clock leads,
membership check, model evidence, merge outcomes).

### `research-pilot-readiness/2.1.36-r3` (`session-readiness.json`, `src/replay/session_readiness.py`)

As 2.1.35, with `generated_from` naming the assembler, collector, session
approval, schedule fingerprint, `assembly_sha256` and every cycle; a `session`
block (slots, cycles, overruns, restarts, stops, budget, `requests` (the
assembly's block, verbatim; r3 -- the 2.1.36 schema carried
`requests_issued`), `interruptions[]` (every `STOP` entry's `interruption`
record), failures, cadence, merge outcomes, membership, ambiguity counts,
clock leads, identities, records, `structure_verified`); `replay` adds
`decisions_with_stale_inputs`,
`decisions_with_skewed_inputs`, `decisions_with_open_interest_gaps`; and two
verdicts: `option_side_usable` with `option_side_blocking_reasons` (over the
option-side requirements, `NO_PASSING_DECISIONS`,
`DIAGNOSTIC_PARAMETERS_NOT_RESEARCH_DEFAULT`, `SESSION_STRUCTURE_NOT_VERIFIED`)
and `usable_for_intraday_pilot` with `blocking_reasons` (every requirement,
futures and multi-session coverage included). Trust flags all `false`.

### `research-pilot-summary/2.1.36-r3` (`pilot-summary.json`, `src/replay/pilot_summary.py`)

Accepts readiness reports of schemas `research-pilot-readiness/2.1.35`,
`/2.1.36` and `/2.1.36-r3`. r3 validates each before reading it
(`validate_readiness_report`): the embedded semantic `report_hash` is
recomputed over the report minus that field (the same rule every supported
schema hashes with) and must match; every decision count must be a
non-negative integer (booleans, floats and strings are refused, not coerced);
`usable_decisions` must equal `replay.passing_decisions`, `blocked_decisions`
equal `replay.blocked_decisions`, and `passing + blocked` equal
`replay.expected_decisions`; `decisions_with_inventory` may not exceed
`expected_decisions`, and the per-decision stale / skewed / open-interest-gap
counts and every `blocker_decision_counts` value may not exceed
`blocked_decisions`; `observed_source_origins` must be a list drawn from
`SYNTHETIC` / `RECORDED_NORMALIZED` with `synthetic_only` equal to "every
origin is `SYNTHETIC`"; `usable_for_intraday_pilot` and `option_side_usable`
must be booleans equal to "no blocking reason", `NO_PASSING_DECISIONS` must be
present when `usable_decisions` is 0; every trust flag must be `false` and
`orders_placed` 0; a session report's slot, cycle and request counts must
agree with each other and stay within the approved budget. A report that
fails any check is refused by name (`PilotSummaryError`). A matching hash is
an integrity check on the report as written, not vendor authenticity.

`label`, `sessions[]` (`source`, `sha256`, `schema_version`, `kind`
`COLLECTION_SESSION` / `SINGLE_CAPTURE`, `session_date`, origins,
`synthetic_only`, `diagnostic`, both verdicts and their reasons,
`usable_decisions`, `expected_decisions`, `decisions_with_inventory`,
`blocker_decision_counts`, `counts_basis` `EXACT` / `LOWER_BOUND`, stale /
skewed / open-interest-gap decision counts, `requests` (`basis`
`CYCLE_REPORTS_AND_ATTEMPT_LOGS` for an r3 session report,
`SCHEDULED_SCOPE_OF_EXECUTED_SLOTS` for a 2.1.36 one -- its `requests_issued`
restated as `scheduled`, with `attempted`, `http_attempts` and the rest
`null` rather than invented -- or `SINGLE_CAPTURE_RECEIPTS`; `scheduled`,
`attempted`, `http_attempts`, `http_attempts_failed`, `with_receipt`,
`acquired`, `not_attempted`, `without_receipt`,
`cycles_with_unverified_attempt_evidence`, `operator_cancelled_cycles`,
`budget`), `coverage` (adds `interruptions`), `identities`,
`vendor_clock_lead`, `ambiguity_after_known_state`, `structure_verified`,
`report_hash`, `report_hash_verified`), `totals` (adds `requests` -- a total
is `null` when any session lacks that count -- with
`sessions_without_attempt_evidence`, `operator_cancelled_cycles` and
`basis_by_session`, and `interruptions`), `minimum_sessions`, `option_side_pilot_ready`,
`option_side_blocking_reasons` (`FEWER_OPTION_SIDE_USABLE_SESSIONS_THAN_MINIMUM`,
`DIAGNOSTIC_SESSION_INCLUDED`, `SYNTHETIC_SESSIONS_ONLY`),
`usable_for_intraday_pilot`, `blocking_reasons` (adds
`SESSIONS_NOT_USABLE_FOR_THE_WHOLE_PILOT`), `observed_source_origins`,
`synthetic_only`, trust flags (all `false`), `limitations`, `report_hash`.
Synthetic and recorded sessions are never summarised together.
