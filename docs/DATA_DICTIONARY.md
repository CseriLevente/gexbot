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
