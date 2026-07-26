# Changelog

## 0.2.0 — corrective engineering pass

A hardening pass over the mathematical engine, ThetaData adapter, timestamp
integrity, confidence model, configuration system, tests and repository quality.
No trading, execution, strategy or calibration work.

### Corrections to v1 claims

These are the statements the v1 documentation made that were wrong or
unsupported. Listed first because they are the most important thing in this
changelog.

| v1 claim | Correction |
|---|---|
| "`ConfidenceScore.calibrated` is enforced by the risk engine, blocking live trading" | **Wrong.** There is no risk engine and no broker. Nothing consumes the flag. It is a research signal; nothing is blocked because nothing can trade. |
| "Standard tier is superior to Pro" | **Unsupported.** Standard is *sufficient*. Whether our gamma matches the vendor's has never been measured. |
| "local gamma matches ThetaData" | **Never validated.** The fixture cross-check compares our pricer against a fixture we generated with our own pricer. |
| `STICKY_DELTA` convention | **Misnamed.** It shifted IV using log-moneyness, which is not sticky-delta. Renamed `STICKY_MONEYNESS`; the real thing is unimplemented and now refuses explicitly. |
| "60-minute floor matches vendor handling" | **Not verified.** Now configurable, with a sensitivity report. |

### Architecture

- Synthetic chain generation moved from `tests/fixtures/` to `src/synthetic/`.
  Production code no longer imports from `tests/`, enforced by an AST-based
  architecture test.
- `src/adapters/fixtures/` renamed `src/adapters/synthetic/`.
- Engine core (`src/gex`, `src/domain`, `src/synthetic`) is stdlib-only, enforced
  both by AST inspection and by importing it in a clean subprocess.
- `pyproject.toml` with pinned dependency ranges, ruff, mypy (strict) and
  coverage configuration. `.gitattributes` for line-ending normalisation.
- CI workflow running lint, format check, type check and the full suite.

### Validation

- New `src/domain/validation.py`: three-way status, machine-readable
  `ValidationCode` enum, bounded example collection, aggregated report.
- `math.isfinite` checks before every numeric comparison. The specific trap
  closed: `NaN < 0` is `False`, so a NaN bid passed a naive negativity check.
- Booleans rejected as numbers (`isinstance(True, int)` is `True` in Python).
- Duplicate contract identities reject **both** copies.
- Chain-level guards: non-finite spot, non-positive spot, naive `as_of`.

### Timestamps

- Per-record clocks: `quote_timestamp`, `greeks_timestamp`, `iv_timestamp`,
  `underlying_timestamp`, `open_interest_as_of`, `request_started_at`,
  `response_received_at`, `normalized_at`. **Nothing is back-stamped to `as_of`.**
- Configurable skew tolerances per join pair, tightest on quote-vs-underlying.
- A future-dated timestamp beyond the clock-skew allowance is a **hard failure**
  that zeroes the confidence score and is flagged `DATA_HALT`-eligible. It can no
  longer earn a perfect freshness score.
- New `src/gex/calendar.py`: NYSE holidays from rules (including Good Friday via
  the Gregorian computus), 13:00 ET early closes, ad-hoc closure injection. Open
  interest is aged in **trading sessions**.
  - Fixed: the v1 weekend discount only handled whole weeks, so Friday-to-Monday
    OI read as three sessions stale.

### Model specification

- New `ModelSpec` embedded in every snapshot and hashed into a fingerprint:
  pricing model, day count, rate and dividend sources, expiration rule, minimum
  time-to-expiry, underlying price source, IV source, effective values, version.
- Minimum time-to-expiry is configurable (default 60 min, was a hard-coded 30).
  `compute_floor_sensitivity()` reports the answer across ~0 / 30 / 60 minutes.
- `ACT/365F`, `ACT/360` and `ACT/252` day counts selectable.
- Optional early-close-aware expiration rule.

### IV provenance

- `IVSource` enum; IV is never stored as a bare float.
- Bid / mid / ask legs retained with `iv_spread` and an `IVQualityFlag`.
- `NON_FINITE_INPUT` flag: a NaN IV is sanitised so it cannot reach the pricer
  **and** reported, instead of silently vanishing into "not supplied".
- `GammaComparison` structure for local-vs-vendor validation, sliceable by DTE,
  moneyness, right and IV. Pro access not required for normal operation.

### Zero gamma

- Full diagnostics: `all_roots`, `root_count`, `selection_method`,
  `local_slope_at_selected_root`, `normalised_slope`, `nearest_root_spacing_pct`,
  `root_near_boundary`, `identically_zero_curve`, `no_root_found`,
  `max_abs_gex_on_grid`, `grid_expansions`.
- Bounded adaptive grid expansion when a root lands near the boundary.
- `selection_method` states that nearest-to-spot is a convention, not a claim
  that other roots are irrelevant.
- `STICKY_DELTA` and `SURFACE_REFIT` return an unresolved result with a reason;
  configuring either raises a `ConfigError`.

### Universe accounting

- `OptionUniverse` reported separately for the chain totals and the zero-gamma
  grid, with contract counts, expirations, and **GEX shares** on both sides.
- An explicit warning when the two universes differ.

### Walls and voids

- Neutral observations (`largest_*_gamma_strike`) separated from directional
  claims (`upside_call_wall`, `downside_put_wall`).
- A directional wall must be on the correct side of spot, or it is `None`. No
  silent same-side or opposite-side substitution.
- Deterministic tie-breaking to the lower strike.
- Gamma voids classified against an inferred strike ladder:
  `TRUE_LOW_GEX_VOID`, `MISSING_STRIKE_DATA`, `IRREGULAR_STRIKE_SPACING`,
  `FILTERED_STRIKE_REGION`, `INSUFFICIENT_COVERAGE`. Only the first is tradable
  structure.

### Confidence

- Nine new components: `multiple_root_penalty`, `root_slope_score`,
  `root_boundary_penalty`, `root_identity_stability`, `timestamp_alignment_score`,
  `future_timestamp_penalty`, `option_universe_coverage_score`,
  `iv_spread_quality`, `model_parameter_completeness`.
- Output exposes `score`, `calibrated`, `components`, `warnings`,
  `hard_failures`.
- The sentinel now also refuses `float()`, `int()` and arithmetic, not only
  ordering comparisons. The likeliest accident was a caller coercing it "to make
  the types line up".

### ThetaData adapter

- `HttpTransport` protocol, real `HttpxTransport`, deterministic `FakeTransport`,
  and `RetryingTransport` with bounded retries, jittered backoff, rate-limit
  handling, response size caps, request IDs, structured logging and credential
  redaction.
- Credentials from environment variables only; config stores variable *names*.
- Explicit calculation parameters, persisted in snapshot metadata.
- Append-only, content-addressed raw response store (in-memory and file-backed).
- Schema tests across quotes, open interest, first- and second-order greeks,
  index price, empty responses, vendor errors, missing columns, unknown extra
  columns and partial chains.

### Configuration

- Typed loading with fail-fast validation: unknown keys, missing keys, wrong
  types, out-of-range values and invalid enums all raise with the offending path.
- Duplicate YAML keys rejected — PyYAML silently keeps the last occurrence, so
  both values look applied in review while only one takes effect.
- `yaml.SafeLoader` subclass; a config file cannot construct Python objects.
- Environment overrides via `${VAR}` / `${VAR:-default}`, **recorded** in the
  profile so they are not invisible in the audit trail.
- `trading_enabled: true` and any non-`none` broker are rejected unconditionally.
- Execution-capable stages refuse to load while any sentinel remains.
- Config fingerprint in every snapshot.
- `research.yaml` usable; `paper.yaml` and `live.yaml` explicitly disabled with
  stated reasons.

### Determinism

- **Fixed:** float addition is not associative, so vendor row order changed the
  last bits of every sum. Contracts are now sorted into canonical order before
  aggregation.
- `output_hash()` quantises floats to 12 significant figures, so the digest is
  stable across platforms rather than only within one machine.
- Replay tests cover repeated runs, reversed row order and varying
  `PYTHONHASHSEED`.

### Tests

- Unit suites for validation, timestamps, calendar, transport, config,
  architecture, states and the synthetic source.
- Offline integration test: fixture to parser to validation to GEX to confidence
  to persisted metadata, via the fake transport.
- Frozen regression case with hand-transcribed expectations.
- Deterministic replay test with output-hash comparison.
- Negative controls proving the credential scanner, the settlement-clock
  cross-check and the 0DTE sensitivity sweep can actually fail.

### Still true

- The repository cannot place an order. No broker adapter, no risk engine, no
  strategies, no execution path.
- Market thresholds remain `UNSPECIFIED_CALIBRATE`.
- All data is synthetic or fixture-based. Nothing has run against live vendor
  data.

---

## 0.1.0 — initial GEX engine

Five GEX views, Black-Scholes shadow pricer, self-contained US Eastern clock,
eight-component confidence score, synthetic chain fixtures, ThetaData endpoint
map with tier requirements.
