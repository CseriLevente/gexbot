# Changelog

## 2.1.2 - adapter-certification readiness

Twenty defects cleared before any paid ThetaData capture. Every one shares a
shape: **something that looked wired up, was not**, and the gap was invisible
because each half was individually valid.

Counts stood in for identities. Two objects configured separately were never
compared. A cap was enforced at the layer above the one that reads bytes. A
session id derived its uniqueness from a timestamp that repeats. A scanner
resolved a path from the metadata it existed to validate.

**Status:** `IMPLEMENTED` | `TESTED_SYNTHETICALLY` |
`TESTED_WITH_OFFLINE_FIXTURES` | `READY_FOR_ADAPTER_CERTIFICATION` |
`NOT_VALIDATED_WITH_LIVE_THETADATA`.

The repository remains incapable of placing an order.

### Defects fixed

| S | Defect in v2.1.1 | Why it mattered | Fix |
|---|---|---|---|
| 1 | Completeness compared `joined_count / expected_count` | Two received where two were expected scored `MEASURED_COMPLETE` regardless of *which* two -- two missing and two unexpected cancel exactly | Identity set differences; `MEASURED_COMPLETE_WITH_EXTRAS`; missing/unexpected identity lists, sorted and bounded |
| 2 | `ThetaDataRuntime.iv_source` and `ModelSpec.iv_price_source` were never compared | A session could fetch NBBO-mid IV and price with the vendor default, both objects looking correct | `ThetaDataResearchPipeline.from_config` builds both and refuses a mismatch |
| 3 | Vendor IV fed straight into local gamma | Possessing the number is not evidence it was produced our way | `PricingCompatibilityReport`; `DividendConvention`; `VendorRateUnits`; five undocumented dimensions reported `UNKNOWN` |
| 4 | `model_fingerprint` reported one model per chain | Per-contract IV fallback yields several; which one was reported depended on iteration order | `ModelDistribution` with per-source counts; `effective_model_uniformity` component; optional strict mode |
| 5 | Only `bid`/`ask` used the structured float parser | A malformed vendor gamma became `None`, indistinguishable from absent, and silently triggered fallback | Every vendor float structured; `VENDOR_GAMMA_MALFORMED`/`NON_FINITE`/`MISSING` told apart |
| 6 | `max_response_bytes` reached only `RetryingTransport` | The cap governed a check *after* the body was in memory, not the streaming read | `httpx_transport_kwargs()` -- one authoritative limit reaching `HttpxTransport` |
| 7 | Capture session ids derived from market `as_of` | Two fetches at one market instant collided in an append-only store | `new_capture_session_id()` -- nonce for uniqueness, market time as audit metadata |
| 8 | `model_parameter_completeness` read only surviving contracts | An empty result set reported a fully specified model, going quiet exactly when asked "why did nothing survive?" | Static configuration completeness, evaluated without reference to any contract |
| 9 | `MODEL_VERSION` still `gex-engine/2.1.0` | Two releases of numerics changes that a replay could not detect | `gex-engine/2.1.2`, one constant, in the model fingerprint |
| 10 | `TRADE_IV` / `LOCALLY_SOLVED_MID_IV` accepted, unimplemented | Fell through to the vendor default, so the operator got an IV they had not chosen | Refused at configuration load with the supported set named |
| 11 | Integrity scanner resolved a path before validating metadata | Malformed metadata crashed the scanner that exists to report malformed metadata | `validate_metadata()` first; `UNSAFE_RECORD_ID`, `INVALID_BYTE_LENGTH`, `INVALID_HASH`, `INVALID_TIMESTAMP` |
| 12 | `base_url` checked for scheme and netloc only | `http://user:secret@host` put a credential in every logged URL; `raw_capture_path` was `str()`-converted, so `42` became a directory | Userinfo, query and fragment refused; path must be a string or `Path` |
| 13 | `rate_type: null` replaced with `"sofr"` when building the client | Stored config and outgoing request disagreed, and only the request was true | Null means omit; `rate_type_policy()` states it |
| 14 | Replay hashing excluded warnings entirely | A snapshot that began reporting a new condition hashed identically to one that did not | Deterministic codes hashed; prose still excluded |
| 15 | Any 200 body went to `parse_csv` | An HTML error page parses to zero rows, and zero rows is legitimate -- so an error page became an empty chain | `validate_csv_body()` with five outcomes |
| 16 | `float(row["strike"])` built the contract identity | `"NaN"` produced an identity unequal to itself; `"5000"` vs `"5000.00"` agreed by luck of formatting | `Decimal` parsing and one canonical spelling |
| 17 | Provenance recorded sources *inspected*, not *selected* | A chain with aware quotes and naive greeks reported both, and never said which supplied a given contract's IV clock | Per-contract `selected_timestamp_sources` |
| 18 | Four unrelated exception bases across four layers | `except ThetaDataError` caught roughly half the ways an adapter can fail | `src/adapters/errors.py`; every failure wrapped; secrets redacted |
| 19 | OI date and spot skew unrecorded | A number whose date we chose is not evidence about the date | `OpenInterestProvenance`, `SpotProvenance` with tolerance |
| 20 | No machine-readable capture readiness | -- | `AdapterCertificationReadiness`; see [ADAPTER_CERTIFICATION.md](ADAPTER_CERTIFICATION.md) |

### Frozen values

The output hash moved three times in this release; each step was verified
independently and is documented in place in
`tests/regression/test_frozen_reference_case.py`.

| Step | Change | Classification |
|---|---|---|
| `181db88a` -> `890bf073` | New confidence component, engine version, distribution metadata | `BEHAVIORAL` |
| `890bf073` -> `9f40dfa9` | Warning codes entered the hash payload | `REPRESENTATIONAL` |
| `9f40dfa9` -> `35def8d5` | Per-contract selected-source provenance added | `REPRESENTATIONAL` |

Also: `EXPECTED_CONFIDENCE_SCORE` 93.6831 -> 93.857 (`BEHAVIORAL`, one component
added at weight 0.03) and `EXPECTED_MODEL_FINGERPRINT` `db8d44db4b51d7c4` ->
`d367d4d4aabbbb69` (`VERSION_METADATA_ONLY`).

**No GEX number changed.** Totals, buckets, per-strike values, walls, voids and
every zero-gamma root are asserted individually and were confirmed unchanged
after each step: after the first, exactly three assertions in the file had
moved; after the second and third, exactly one each.

### Behavioural changes worth knowing

* `ChainCompleteness` takes identity sets, not counts. The count-based
  constructor is gone.
* `ContractKey` carries the canonical *string* strike, not a float.
* A 200 response with a non-CSV body now raises rather than yielding an empty
  chain. `tests/fixtures/vendor/thetadata/empty.csv` became header-only, because
  a zero-byte body is not an empty chain.
* 401/403 raise `ThetaDataAuthenticationError` and 429 raises
  `ThetaDataRateLimitError`; both subclass `ThetaDataHTTPError`.
* An absent vendor gamma is *not* a finding -- that is the whole Standard-tier
  design. Only a second-order record that arrived with an unreadable gamma is.

### Not added, deliberately

Databento, MES/ES futures data, feature-store work, trading strategies, regime
thresholds, a risk engine, position sizing, IBKR, broker execution, paper
trading, live trading, order types, and arbitrary calibrated values.

---

## 2.1.1 — correctness at the layer below

v2.1 fixed a class of defect at the layer where it was first noticed. This
release fixes the seventeen places where the fix was correct there and something
downstream still behaved as though it had not happened.

The recurring shape: **a value was computed correctly and then discarded**.
`ChainCompleteness` worked out that a chain's universe was unknown, and
`assemble_chain` overwrote the answer. `index_rows` selected a canonical row per
identity, and assembly iterated the original list. `_resolve_underlying`
recorded that a spot was missing, and returned one anyway. In each case the
diagnostic was right and the behaviour was unchanged — which is worse than no
diagnostic, because the diagnostic makes it look handled.

**Status:** `IMPLEMENTED` · `TESTED_SYNTHETICALLY` · `TESTED_WITH_OFFLINE_FIXTURES`
· `NOT_VALIDATED_WITH_LIVE_THETADATA`.

Nothing was added toward trading. The repository remains incapable of placing an
order.

### Defects fixed

| § | Defect in v2.1 | Why it mattered | Fix |
|---|---|---|---|
| 1 | `assemble_chain` replaced `expected_contract_count=None` with `len(quote_rows)`, and `score_chain_completeness` fell back to `usable_ratio` | Two layers independently turned "we don't know the universe" into "we got everything"; a truncated chain scored 1.0 for completeness | `CompletenessStatus` carried on the snapshot; `None` stays `None`; unknown scores `None`, uncalibrated, with code `CHAIN_COMPLETENESS_NOT_INDEPENDENTLY_OBSERVED` |
| 2 | `iv_source`, `duplicate_policy`, `max_dte`, `strike_range`, `min_time` were parsed, validated, fingerprinted — and never read | A setting visible in YAML that survives review and never reaches a request | `ThetaDataRuntime.from_config()` as the one construction path; tests assert against outgoing requests, not config objects |
| 3 | `number()` range-checked without `isfinite`; strings and optionals were unvalidated | NaN compares `False` against every bound, so a range check alone passes it | `math.isfinite` before every range check; non-empty string checks; `min_time` grammar; booleans refused as integers |
| 4 | `_resolve_underlying` recorded `UNDERLYING_MISSING` then returned `snapshot.spot`, under a comment saying it deliberately did not | GEX scales by spot², so substituting a different underlying silently reprices the contract | Returns `None`; `has_valid_spot` gates current GEX; new `no_underlying_price` exclusion; per-purpose eligibility |
| 5 | Assembly iterated `inputs.quote_rows` after computing `quote_indexed` | Duplicates were reported as collapsed and assembled twice | Iterates the deduplicated rows, sorted by key so order cannot depend on the vendor |
| 6 | `if spec.risk_free_rate == 0.0: missing.append(...)` | A deliberately configured zero was reported as unspecified; the only way to satisfy the check was to change the number | Completeness reads resolved provenance; realism moved to `MODEL_REALISM_WARNING` |
| 7 | The size cap lived in `RetryingTransport`, which receives an already-buffered body | The cap protected the parser, not the process | `ByteLimitedReader` aborts mid-stream, closes the connection, discards the partial body; retry layer retained as defence in depth |
| 8 | `basic_auth=... if username and password else None` | An unset environment variable produced a working *unauthenticated* client, and the 401 looked like a vendor outage | `MissingCredentialsError` at construction, naming the variables and never the values |
| 9 | `parse_int_field` reached the integer via `float(text)` | Exact only below 2⁵³; `"9007199254740993"` became `...992`, and open interest is exactly where a large integer is plausible | `Decimal` with an exact-integrality check, plus a digit fast path |
| 10 | One chain-wide `localisation_applied`, set from the quote loop | Aware quotes + naive greeks reported "no assumption applied" while assuming a timezone for every greek | `TimestampLocalizationSummary` per `TimestampSource`, in snapshot metadata |
| 11 | `PARSER_VERSION` still `2.0.0` after v2.1 changed parsing three ways | A replay hash that does not move when the parser changes cannot detect that the parser changed | One constant, bumped to `thetadata-v3-parser/2.1.1`, carried into the replay hash |
| 12 | Payload and index writes atomic individually, not together | Nothing could say afterwards which pairs had come apart | `verify_integrity()` classifying eight states; proposes, never deletes |
| 13 | The bare-interpreter test asserted on absolute `sys.modules` | Failed on any host whose `sitecustomize` preloaded NumPy — measuring the machine, not the repository | Static transitive import graph + `-S -E` subprocess + delta measurement |
| 14 | "both $80/mo cheaper *and* internally consistent" | Asserted a numerical agreement that has never been measured | Rewritten to state what follows from the price list and what does not |
| 15 | `_to_float` returned `None` for missing and for `"oops"` | Corruption was indistinguishable from absence, and absence is normal | `FloatParseIssue` with six codes; malformed values recorded on the quote, missing ones not |
| 16 | Nothing checked the HTTP status inside the client | A custom transport returning 500 handed an HTML error page to `parse_csv` | Status checked first, unconditionally, before the body is touched |

### Frozen values

**No frozen hash changed.** `EXPECTED_OUTPUT_HASH` remains
`181db88a7a343eda4d874322161e8b236b57faf93db4282f6e383983260d0b16`.

This is a result, not an oversight. The reference case is built by
`build_synthetic_chain()`, which knows its own universe exactly and therefore
declares `MEASURED_COMPLETE` — so the completeness fix does not perturb it, and
the parser-version and localisation metadata belong to the ThetaData adapter,
which the synthetic path does not use. Every individual numeric assertion was
reviewed and none moved.

One test bound was widened deliberately:
`test_a_broken_snapshot_scores_near_zero` from `< 10.0` to `< 12.0`, because an
explicitly configured zero rate is no longer counted as an unspecified
parameter (§6). Documented in place.

### Behavioural changes worth knowing

* `ConfidenceComponent.score` is now `float | None`. A `None` component is
  excluded from the weighted mean rather than contributing an invented number.
* `EffectiveModelInputs.spot` is now `float | None`.
* `duplicate_policy` accepts `collapse_exact` as an explicit third value; see
  OPEN_DECISIONS OD-19 for why it behaves identically to `reject`.
* The client now raises `ThetaDataVendorError` on any non-2xx status.

### Not added, deliberately

Databento, futures features, trading strategies, regime thresholds, a risk
engine, position sizing, IBKR, broker integration, paper trading, live trading,
order definitions, execution code, and arbitrary calibrated values.

---

## 2.1.0 — correctness and integration hardening

A narrowly-scoped pass over defects found by review of v2. Every fix was
introduced test-first: a failing test that reproduced the defect, then the
smallest correct change, then regression coverage. No financial assumption was
changed silently; where one was ambiguous it went to
[OPEN_DECISIONS.md](OPEN_DECISIONS.md).

Nothing was added toward trading. The repository still cannot place an order.

**Status of this release:** `IMPLEMENTED` and
`TESTED_WITH_OFFLINE_FIXTURES`. `NOT_YET_VALIDATED_WITH_LIVE_VENDOR_DATA` —
no request in this repository has ever reached ThetaData.

### Defects fixed

| # | Defect in v2 | Why it mattered | Fix |
|---|---|---|---|
| 1 | Model inputs were resolved independently in the pricer, the GEX aggregator, the zero-gamma solver and the comparison path | Four code paths could price the same contract differently while each looked correct in isolation | `src/domain/effective_model.py` — one resolver, consumed by all four |
| 2 | `spec.risk_free_rate or snapshot.risk_free_rate` | `0.0` is falsy, so an explicitly configured zero rate silently borrowed the snapshot's rate; the fingerprint recorded the rate the operator asked for, not the one used | Resolution follows the source enum, never truthiness |
| 3 | `CALENDAR_MIDNIGHT` expiration rule was selectable | No listed index option settles at midnight; choosing it produced wrong time-to-expiry for every contract | Declared but `is_supported = False`; resolution refuses it |
| 4 | `underlying_price_source` was declared and ignored | The setting looked applied in review and never reached a calculation | Resolver honours it; unsupported values raise |
| 5 | The `thetadata:` YAML section was validated then discarded | A setting could be present in the file and never reach a request | `src/config/thetadata.py`, typed, with `build_thetadata_client()` as the single construction path |
| 6 | "Effective parameters" conflated requested with sent | A parameter the endpoint does not accept was reported as effective | `VendorParameterSet` splits requested / supported / sent / effective-local / unsupported |
| 7 | Gamma comparison recomputed its own inputs | The comparison could differ from the engine it was auditing | Comparison consumes `contract.effective` |
| 8 | Zero-gamma pooled SPX and SPXW | AM- and PM-settled contracts have different expiration instants; pooling them mixes two surfaces | `zero_gamma_eligible()` separates roots and reports what it excluded |
| 9 | One malformed integer killed the whole chain | A single corrupt cell cost every contract in the response | `parse_int_field` records per-record `parse_issues`; one bad record costs one record |
| 10 | Duplicate rows were resolved positionally | Chain numbers depended on response ordering | `duplicate_policy` defaults to `reject` |
| 11 | Naive datetimes flowed into the maths | A missing timezone silently became a 4–5 hour error in time-to-expiry | `to_eastern()` raises `NaiveTimestampError` |
| 12 | The DST fall-back hour resolved silently | `01:30` occurs twice; the parser picked one without saying so | `parse_vendor_timestamp(fold=...)`, `strict_dst` refuses ambiguity |
| 13 | Snapshot hashing included prose and warnings | Reworded text moved the hash; changed numbers sometimes did not | `hash_payload()` hashes scores and structure, not narration |
| 14 | Root identity was compared by index | Two roots reordering read as two roots changing | `match_roots()` / `compare_root_topology()` |
| 15 | Strike spacing was one global number | SPX is 5-wide near the money and 25-wide in the wings, so gaps were misreported everywhere else | `StrikeLadder` infers spacing from a rolling local median |
| 16 | Callers hand-assembled `ThetaDataClient` | Config drift between call sites | Single factory |
| 17 | `Retry-After` was ignored | The client hammered a server that had told it to wait | Parsed (delta-seconds and HTTP-date), honoured, capped at 120 s |
| 18 | Response size was checked after reading | An oversized payload was fully materialised before rejection | `HttpxTransport` aborts mid-stream |
| 19 | Raw-capture ids were `session-endpoint` | The second request to an endpoint in one session collided, and the store is append-only, so it raised | `build_record_id()` includes sequence and parameter hash; writes are atomic via `mkstemp`/`fsync`/`os.replace` |
| 20 | `expected_contract_count = len(quote_rows)` | Completeness was measured against the response being measured, so a truncated chain scored 100% | `ChainCompleteness` requires an independent expectation and reports `PARTIALLY_OBSERVED` without one |
| 21 | Coverage score saturated at its own floor | A grid that skipped a material contract reported 100% coverage | `0.0 if share < floor else share` |
| 22 | Future-dated open interest aged through the session logic | An impossible timestamp was treated as merely stale | Hard failure before ageing; `latest_open_interest_as_of` added because the chain-level value is the *oldest* |
| 23 | Voids were classified from coverage alone | Exactly-at-threshold coverage with a missing strike read as a true void | Triggers on `missing > 0 or coverage < threshold` |
| 24 | Build tooling was unbounded above | A future setuptools release could change the artefact without a commit here | `setuptools>=68,<86` |

### Added

- `docs/RELEASE.md` — the release procedure of record, with Windows and Unix
  commands and the clean-tree requirement.
- `tests/unit/test_release_integrity.py` — the build is pinned, the archive is
  reproducible and credential-free, and the engine computes a snapshot on a
  bare interpreter (`-S -E`, no site-packages) that agrees with the installed
  run.
- CI jobs: `bare-interpreter` (installs nothing), `reproducible-build` (two
  archives of one commit must be byte-identical), alongside the existing
  `no-trading-guarantee`.

### Frozen values re-derived

Two frozen regression values changed. Each was re-derived only after every
other numerical assertion in the suite was confirmed unchanged, and each is
documented in place in `tests/regression/test_frozen_reference_case.py` with
the reason and whether the change was representational or behavioural.

### Not added, deliberately

Databento, IBKR, order placement, broker adapters, strategies, position sizing,
live or paper execution, regime thresholds, and calibrated constants. See
[MODEL_ASSUMPTIONS.md](MODEL_ASSUMPTIONS.md).

---

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
