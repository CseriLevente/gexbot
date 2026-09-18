# Validation

Two different things are called validation in this repository:

1. **Record validation** — the gate every contract passes before arithmetic.
2. **Test strategy** — how the engine itself is checked.

Both are below.

---

## 1. Record validation

### The failure being prevented

A single NaN gamma summed into a chain total produces a NaN total, and a NaN
total that reaches a chart looks like a rendering bug rather than a data bug.
Rejection happens at the contract boundary, before any arithmetic.

The subtle case is why ordering matters: **`NaN < 0` is `False`**, so a NaN bid
sails through a naive negativity check. Finiteness is checked first, and every
ordering comparison is guarded so an unorderable value cannot reach it.

### Three-way status

| Status | Meaning | Reaches aggregates? |
|---|---|---|
| `ACCEPTED` | no issues | yes |
| `ACCEPTED_WITH_WARNING` | usable, with a caveat | yes |
| `REJECTED` | at least one error | no |

The middle case is real and common: a zero-bid deep-wing option has usable gamma
but untrustworthy IV. Treating it as either fully good or fully bad loses
information.

### Rules

**Numeric hygiene** — `not_finite` (NaN, infinities, wrong type including `bool`,
since `isinstance(True, int)` is `True` in Python), `negative_open_interest`,
`negative_bid`, `negative_ask`, `crossed_market`, `locked_market` (warn),
`zero_bid` (warn), `invalid_strike`, `invalid_multiplier`,
`non_positive_implied_vol` (warn), `implied_vol_out_of_range` (warn),
`negative_gamma`, `gamma_out_of_range`, `extreme_iv_spread` (warn),
`no_gamma_source`, `missing_open_interest`.

**Structure** — `invalid_expiration`, `invalid_option_right`,
`duplicate_contract`, `unknown_root`.

**Time** — `naive_timestamp`, `future_timestamp`, `stale_snapshot` (warn),
`timestamp_skew` (warn), `missing_timestamp`.

Crossed markets are an error by default and a warning when
`drop_crossed_quotes: false` — explicit classification either way, never silent.

Duplicate identities reject **both** copies. There is no principled way to choose,
and silently keeping the first is how a stale record wins over a fresh one.

### Machine-readable output

```json
{
  "total": 250, "accepted": 248, "accepted_with_warning": 1, "rejected": 1,
  "acceptance_ratio": 0.996,
  "error_counts": {"not_finite": 1},
  "warning_counts": {"zero_bid": 1},
  "examples": [{"code": "not_finite", "field": "quote.gamma",
                "severity": "error", "observed": "nan"}]
}
```

Counters rather than a transcript — an SPX chain can produce tens of thousands of
records, and what a confidence component needs is "how many, of which kind". The
example list is bounded at 25; an unbounded one is a memory leak on a bad feed
day.

### Timestamp integrity

Every source clock is kept separately: `quote_timestamp`, `greeks_timestamp`,
`iv_timestamp`, `underlying_timestamp`, `open_interest_as_of`,
`request_started_at`, `response_received_at`, `normalized_at`.

**Nothing is ever back-stamped to `as_of`.** That was the v1 failure: assigning
the request instant to every record makes a five-minute-old quote and a fresh one
indistinguishable, and the whole chain reads as perfectly fresh regardless of
what the vendor sent. Freshness that is assigned rather than measured is worse
than no freshness metric at all.

A future-dated vendor timestamp beyond the clock-skew allowance is a **hard
failure**: it zeroes `future_timestamp_penalty`, zeroes the whole confidence
score, and is flagged `DATA_HALT`-eligible. Small skew (2 s by default) is
ordinary disagreement between two machines and is tolerated.

Open interest is a `date`, not an instant, and is aged in **trading sessions**.
Friday's settlement read on Monday is one session old; a holiday weekend does not
make it look worse.

---

## 2. Test strategy

### Layers

| Layer | Marker | What it proves | Label |
|---|---|---|---|
| Unit | — | Each rule and formula in isolation | `TESTED_SYNTHETICALLY` |
| Integration | `integration` | Fixture to parser to validation to GEX to confidence to metadata | `TESTED_WITH_OFFLINE_FIXTURES` |
| Regression | `regression` | Frozen expected values, hand-transcribed | `TESTED_SYNTHETICALLY` |
| Replay | `replay` | Same inputs produce the same output hash | `TESTED_SYNTHETICALLY` |
| Release integrity | — | Bare-interpreter run, pinned build, reproducible archive | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Core isolation | — | Static import graph + `-S -E` subprocess, host-independent | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Store integrity | — | Orphans, hash/size mismatch, incomplete writes, duplicate ids | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |

**No test touches the network.** `FakeTransport` raises on an unregistered route
rather than silently succeeding, so an accidental real call surfaces as a loud
failure — `test_no_unit_test_performs_a_real_network_call` asserts exactly that.

**What these layers prove.** Synthetic and integration fixtures remain offline
and no test contacts ThetaData. Separately, the generated reports in
tests/fixtures/live_capture/ are derived from preserved paid captures whose raw
payloads are not committed. The 2026-09-02 report validates the live transport,
parser, capture verification and v2.1.28 analytical-universe partition. It does
not validate a trusted GEX: missing open interest and unresolved pricing evidence
remain explicit blockers.

### Environment independence

`tests/unit/test_release_integrity.py` runs the engine in a subprocess under
`python -S -E`: no site-packages, no `PYTHON*` environment influence. Under `-S`
the third-party packages are not merely unimported, they are *unimportable*, so
an accidental `import yaml` in the engine core fails there even though it would
succeed in the dev environment. The bare run's GEX total is then asserted equal
to the installed run's — if those diverge, something in the maths depends on an
installed package.

CI goes one step further: the `bare-interpreter` job installs nothing at all.

### What makes a test worth having here

The fixtures are built so the **answers are known in advance**: open interest is
placed at chosen strikes, put weight exceeds call weight so signed GEX must cross
zero above spot, and the smile is calibrated to a realistic SPX skew so
`sticky_strike` and `sticky_moneyness` cannot collapse onto each other. A test
that asserts "the engine returned a number" proves nothing about a GEX engine.

Several tests exist specifically as **negative controls** — proof that the test
could fail:

- `test_the_credential_scanner_actually_catches_a_planted_secret` — a scanner
  nobody has seen fire is a scanner nobody should trust.
- `test_wrong_settlement_clock_would_break_the_gamma_cross_check` — confirms the
  cross-check is sensitive to the clock rather than passing by coincidence.
- `test_the_floor_is_inert_when_no_contract_is_close_enough_to_expiry` — the
  0DTE sensitivity sweep is meaningless five hours before settlement, and this
  says so.
- `test_flat_smile_collapses_sticky_moneyness_onto_sticky_strike` — with no skew
  there is nothing for a translating smile to change.

### Regression case

`tests/regression/test_frozen_reference_case.py` pins totals, per-bucket values,
per-strike values, walls, voids, roots, all 17 confidence components, and three
fingerprints.

**Nothing regenerates its own expectation.** Values were printed once, read,
hand-checked and typed in as literals. A regression test that recomputes its
expectations proves only that the code equals itself.

Hand checks recorded in the module docstring make the numbers believable rather
than merely recorded — for example, 5 expiries times 252,633 open interest equals
1,263,165, which matches the frozen total.

Tolerance is `rel=1e-12` on floats and exact on the hash. That split is
deliberate and has already paid off: when canonical contract ordering was
introduced, every numeric expectation held while the hash moved, which is exactly
how a representation change should look.

### Replay

Proves *same raw fixtures + same config + same model version produce the same
output hash*, and covers the three ways that breaks:

- a hidden `datetime.now()` — caught by running the same fixtures twice
- dict or set iteration order — caught by reversing the input row order
- `PYTHONHASHSEED` — caught by re-running in subprocesses with different seeds

The row-order test found a real defect: float addition is not associative, so
vendor row order changed the last bits of every sum. Contracts are now sorted
into canonical order before aggregation, making the output a function of the
data rather than of arrival order.

The hash quantises floats to 12 significant figures, so it is stable across
platforms rather than only within one machine.

### Architecture tests

Rules that are easy to break by accident and expensive to discover later:

- `src/` never imports from `tests/` — AST-based, per file
- `src/gex`, `src/domain`, `src/synthetic` import no third-party package, checked
  both by AST and by importing them in a subprocess
- no order-placement code exists — AST-based, so prose *about* order placement is
  fine and a definition or call is not
- `BrokerAdapter` exposes no order method
- no credential literals — literal-shaped rather than keyword-shaped, because
  reading a credential from the environment necessarily mentions the word
  "password"
- **no calculation-affecting read from `snapshot.meta`** (v2.1.8) — AST-based,
  over the modules that compute. `meta` is an open dictionary a caller can write
  anything into, and a forged `chain_completeness_object` moved a *trusted*
  confidence score from 52.0619 to 57.3394. Anything that changes a number is a
  typed field; `meta` may describe a calculation and must not alter one

### Coverage

Target 90% on the modules that compute numbers; scaffolding packages with no
implementation are excluded in `pyproject.toml`.

Not chasing 100%: the uncovered remainder is defensive branches and the real HTTP
transport, which cannot be covered without either mocking `httpx` internals
(testing the mock) or making a network call (which unit tests must never do). Its
retry, redaction and size-cap behaviour lives in `RetryingTransport`, which *is*
covered.

Current: **91.19%** line-and-branch across 17,124 statements, against a fail_under of 90 (v2.1.36 r3 gate; v2.1.35 measured 91.08% across 15,518 statements; v2.1.34 90.94% across 14,832; v2.1.33 90.64% across 14,386).

### Three versions, three meanings

They move independently, and conflating them is how a change hides.

| Constant | Value | Defined in | Moves when |
|---|---|---|---|
| Package version | `2.1.36` | `pyproject.toml` | anything ships |
| Parser version | `thetadata-v3-parser/2.1.17` | `src/adapters/raw_store.py` | vendor-payload interpretation changes -- v2.1.15 replays the exact stored bytes under the captured content type and charset rather than a UTF-8-with-replacement reading of them |
| Engine version | `gex-engine/2.1.10` | `src/domain/model_spec.py` | the numerics change |
| Manifest schema | `raw-capture-manifest/2.1.17` | `src/adapters/raw_store.py` | the *shape* of capture evidence changes |
| Certification schema | `adapter-certification/2.1.13` | `src/adapters/certification.py` | what a readiness verdict means changes |
| Validation schema | `adapter-validation/2.1.10` | `src/adapters/validation.py` | what a validation report means changes |
| Normalization schema | `normalized-chain/2.1.18` | `src/domain/normalization.py` | which chain fields a trusted calculation is bound to changes |
| Request-spec schema | `thetadata-request-spec/2.1.10` | `src/adapters/thetadata/request_spec.py` | what counts as the same vendor request changes |
| Capture-operation schema | `capture-operation/2.1.24` | `src/adapters/capture_operation.py` | what one capture operation fixes changes. Moved in v2.1.19: an operation now carries the preflight approval a human gave, so every record stamped with the operation is bound to what was approved |
| Preflight-approval schema | `capture-preflight-approval/2.1.24` | `src/adapters/thetadata/preflight_approval.py` | what an approval covers changes. An approval computed under older rules must not match a live run checked under newer ones — the digest would agree while the two sides disagreed about what it promised |
| Expected-universe schema | `expected-universe/2.1.11` | `src/domain/expected_universe.py` | what a universe hash covers changes |
| Settlement-evidence schema | `settlement-evidence/2.1.10` | `src/domain/settlement.py` | what a settlement rule *means* changes |
| Capture-artifact envelope | `capture-artifact/2.1.10` | `src/adapters/artifact_store.py` | how a stored artifact is wrapped changes |
| Universe-resolver schema | `universe-resolver/2.1.12` | `src/domain/universe_artifact.py` | what a coverage state *means* changes |
| Universe-scope schema | `universe-scope/2.1.10` | `src/domain/universe_scope.py` | what makes two requests comparable changes |
| Universe-documentation schema | `universe-documentation/2.1.12` | `src/adapters/universe_evidence.py` | what a universe document has to say changes |
| Universe-extraction schema | `universe-extraction/2.1.11` | `src/adapters/universe_evidence.py` | what a reading of a document records changes |
| Capture-verification receipt | `capture-verification/2.1.11` | `src/adapters/universe_resolvers.py` | what a verification receipt has to carry changes |
| Operator-report schema | `raw-capture-run/2.1.24` | `src/tools/capture_thetadata_once.py` | the shape of the capture report changes |
| Run-intent schema | `raw-capture-intent/2.1.24` | `src/tools/capture_thetadata_once.py` | what a run states before its first request changes |
| HTTP-attempt schema | `http-attempt/2.1.17` | `src/adapters/http_attempts.py` | what is recorded about one request attempt changes |
| Analytical-readiness schema | `analytical-readiness/2.1.13` | `src/adapters/certification.py` | what a dataset-ready verdict rests on changes |
| Raw-response schema | `raw-response/2.1.17` | `src/adapters/raw_store.py` | what a stored payload *is* changes -- v2.1.13 stores entity bytes rather than a re-encoding of a lossily decoded string, and v2.1.14 records the content type, the declared and selected charset, the decode status and the decoded-text hash alongside them |
| Pricing-compatibility schema | `pricing-compatibility/2.1.22` | `src/config/compatibility.py` | what a dimension result *means* changes. Moved in v2.1.22: `MATCHED` on `RATE_UNITS` used to mean the vendor's published description was confirmed, and can now also mean it was contradicted by a live capture and the configuration matches the measured behaviour instead |
| Pricing-evidence schema | `pricing-evidence/2.1.25` | `src/adapters/thetadata/live_behavior.py` | what a documented-versus-observed record must carry changes. New in v2.1.22: a v2.1.21 reader saw only the documented side and would report agreement where there is a conflict. Moved in v2.1.25: an unresolved inference no longer yields a documented/observed verdict, and `DIVIDEND_CONVENTION` stopped being reported as documentation-resolved on evidence no capture carries |
| Capture-rate-intent schema | `pricing-evidence/2.1.24` | `src/adapters/thetadata/live_behavior.py` | what a capture's *declared economic intent* must carry changes. **Deliberately not tracking the row above**, which is why it still reads `2.1.24`: this value is inside `rate_intent_fingerprint`, which an operator approves at dry-run time and a later capture is checked against, and the intent's fields did not change in v2.1.25 -- only the strictness of its reader. Bumping it would have moved every declared intent to announce a change in a different record |
| Capture-certification schema | `capture-certification/2.1.27` | `src/adapters/thetadata/capture_certification.py` | what an offline certification report derives from a capture changes. Moved in v2.1.27: the universe evidence scope is derived from the verified request plan instead of a first-capture literal, so a capture's scope names its own session |
| Canonical-report schema | `certification-report-canonical/1` | `src/domain/canonical.py` | how a report is rendered *for hashing* changes. Absolute filesystem paths are excluded and real numbers become decimal strings at nine significant digits, so one immutable capture hashes the same on Windows and Linux. A digest taken under one rendering and compared under another would disagree about two identical reports |
| Data-eligibility schema | `analytical-universe/2.1.28` | `src/domain/analytical_universe.py` | the *meaning* of an eligibility verdict changes -- when a contract that used to be eligible would now be excluded, or excluded under a different reason. New in v2.1.28: an open-interest record that does not exist stops being spelled `0`, and a contract whose expiration precedes the capture's market session stops being part of the current analytical universe |
| Analytical-universe report schema | `analytical-universe-report/2.1.28` | `src/adapters/thetadata/analytical_universe.py` | what a capture's eligibility partition must carry changes. Separate from the certification schema **and deliberately not part of it**: nothing about what a certification derives from a capture changed in v2.1.28, so `capture-certification/2.1.27` did not move and both committed live-capture reports still reproduce their `report_hash` |
| Analytical-universe algorithm | `analytical-universe/1` | `src/adapters/thetadata/analytical_universe.py` | the *classification* changes -- when an identity that used to land in one class would now land in another. Separate from the schema above for the same reason `oi-transition/1` is separate from `longitudinal-oi`: two reports with the same fields are incomparable if they were classified differently |
| Pricing-diagnostics schema | `pricing-diagnostics/2.1.31` | `src/adapters/thetadata/pricing_diagnostics.py` | the offline diagnostic report shape changes |
| Direct delta diagnostic algorithm | `direct-delta-grid/1` | `src/adapters/thetadata/pricing_diagnostics.py` | the candidate grid, sample admission or scoring changes |
| Floor-aware inference schema | `floor-aware-pricing-inference/2.1.32` | `src/adapters/thetadata/floor_inference.py` | candidate support and censoring report shape changes |
| Cross-capture validation schema | `pricing-inference-validation/2.1.32` | `src/adapters/thetadata/floor_inference.py` | replication report shape changes |
| Floor-aware inference algorithm | `common-population-censoring/1` | `src/adapters/thetadata/floor_inference.py` | joint support, censoring or replication rules change |
| ThetaData support-evidence schema | `thetadata-support-evidence/2.1.30` | `src/adapters/thetadata/oi_policy.py` | what privacy-safe metadata and closed claims are extracted from a private vendor email changes |
| OI-policy-resolution schema | `oi-policy-resolution/2.1.30` | `src/adapters/thetadata/oi_policy.py` | how vendor support claims are composed with a frozen capture report changes |
| OI-policy derivation | `thetadata-oi-policy/1` | `src/adapters/thetadata/oi_policy.py` | the policy conclusions or capture-binding rules change |
| Longitudinal-OI schema | `longitudinal-oi/2.1.27` | `src/adapters/thetadata/oi_transition.py` | what a cross-capture open-interest transition report must carry changes |
| OI-transition algorithm | `oi-transition/1` | `src/adapters/thetadata/oi_transition.py` | the *classification* changes — when an identity that used to land in one transition class would now land in another. Separate from the schema above because two reports with the same fields are still incomparable if they were classified differently |
| Archive-identity schema | `archive-identity/2.1.26` | `src/adapters/thetadata/capture_certification.py` | what it takes for an archive to *be* a capture's archive changes. New in v2.1.26, and separate from the certification schema because it moves for its own reasons: the archived manifest is now rebuilt from its own descriptors rather than read, the archived run intent must equal this capture's byte for byte, and an archive whose entry names collide once separators are normalised is refused rather than resolved by entry order |
| Raw-acquisition schema | `raw-acquisition/2.1.17` | `src/adapters/thetadata/raw_acquisition.py` | what an endpoint-by-endpoint acquisition report records changes |
| Parser-report schema | `parser-report/2.1.17` | `src/adapters/thetadata/raw_acquisition.py` | what a parser claims about already-stored bytes changes |
| Attempt-evidence schema | `attempt-evidence/2.1.17` | `src/adapters/http_attempts.py` | what reopening a persisted attempt log checks changes |

| Capture-plan schema | `capture-plan/2.1.17` | `src/adapters/thetadata/capture_plan.py` | what a plan says changes -- v2.1.16 splits endpoints a chain needs from endpoints captured as evidence, and carries both the option root and the underlying index |
| Request-plan schema | `raw-request-plan/2.1.20` | `src/adapters/thetadata/request_plan.py` | what an authorised request looks like changes |

| Vendor-documentation schema | `vendor-documentation/2.1.18` | `src/adapters/thetadata/vendor_documentation.py` | what a rule a document may settle changes |

| Documentation-bundle schema | `vendor-documentation-bundle/2.1.18` | `src/adapters/thetadata/openapi_evidence.py` | what a verified bundle must carry changes. **The official OpenAPI document is pinned**: `https://docs.thetadata.us/openapiv3.yaml`, 812,792 bytes, SHA-256 `1b65f93c879a5ca4477a0ff9177235138e0c81840e0c7dddfbd9e34164b40b50`, stored content-addressed under `vendor_documentation/`. The digest is over the exact response body bytes -- not a markdown rendering, not a reserialization of the parsed YAML, not a summary |

| Documentation-extractor version | `openapi-evidence-extractor/2.1.18` | `src/adapters/thetadata/openapi_evidence.py` | *how* a value is read out of the document changes. Separate from the schema: the same bytes read under different normalizers yield different claims |

| Research-contract schema | `intraday-research-contract/2.1.33` | `src/replay/research_contract.py` | the declared research design (scope, grid, freshness budgets) changes meaning. Unchanged in v2.1.34: the replay consumes it as-is |
| Research-input audit schema | `research-input-audit/2.1.33` | `src/replay/research_contract.py` | what a declared-frame audit carries changes |
| Research-events schema | `research-events/2.1.34` | `src/replay/event_store.py` | what a normalized, hash-bound event record must carry changes -- kinds, canonical identities, event/availability clocks, revision sequence, origin labels. Still accepted unchanged in v2.1.35 |
| Research-events schema (lineage-bound) | `research-events/2.1.35` | `src/replay/event_store.py` | the 2.1.34 record plus a mandatory `lineage` (raw payload digest, native row, rule, availability basis) and a document `provenance` naming the verified capture and every payload. Replay semantics are the 2.1.34 ones |
| Research-events schema (session-bound) | `research-events/2.1.36` | `src/replay/event_store.py` | the 2.1.35 record whose `lineage` also names the collection `cycle` and the logical `request_id`, under a document `provenance` naming the session approval, schedule fingerprint, intent and log digests and every cycle's verified capture. Replay semantics are still the 2.1.34 ones |
| Capture normalizer | `thetadata-research-events/2.1.36` (row rules `thetadata-v3/<kind>/2`, inventory and model evidence `/1`) | `src/adapters/thetadata/research_events.py` | how a native ThetaData v3 row becomes a research event changes -- columns read, identity canonicalisation, timestamp zone, receipt evidence, repeated-identity policy, exclusion reasons, open-interest attribution -- or, as in v2.1.36, what the normalizer accepts as a capture (a partial-scope cycle with `NOT_SCHEDULED` / `NOT_ACQUIRED` receipts) and reports. Row rules moved to revision 2 after the independent review of the first v2.1.35 cut and are unchanged in v2.1.36 |
| Pilot-readiness schema | `research-pilot-readiness/2.1.35` | `src/replay/pilot_readiness.py` | what a readiness report carries or what a requirement status means changes. Every trust flag is pinned false |
| Session-readiness schema | `research-pilot-readiness/2.1.36-r3` (the pilot summary also accepts `/2.1.36` and `/2.1.35`) | `src/replay/session_readiness.py` | what an assembled session's readiness report carries -- the `session` block, the option-side verdict apart from the whole-pilot verdict -- or what a status means changes. Every trust flag is pinned false |
| Pilot-summary schema | `research-pilot-summary/2.1.36-r3` | `src/replay/pilot_summary.py` | what the multi-session summary restates or how it judges the option side across sessions changes |
| Session assembler | `thetadata-session-assembly/2.1.36-r3` (report `intraday-session-assembly/2.1.36-r3`) | `src/adapters/thetadata/session_assembly.py` | the merge rule (current-revision comparison, availability order, tie-break, membership, ambiguity) or the structural verification changes |
| Session collector | `intraday-session-collector/2.1.36-r3` (`intraday-collection-schedule/2.1.36`, `intraday-session-intent/-approval/2.1.36`, `intraday-session-log/-summary/2.1.36-r3`) | `src/ingest/schedule.py`, `src/ingest/session_collector.py` | how slots, scopes, budgets, approvals or missed-slot accounting are derived or recorded changes |
| Pilot-collection specification | `intraday-pilot-collection/2.1.36` | `config/intraday_pilot.json` | what the pilot collector is required to record or refuse changes. v2.1.36 added the `collection` policy block and the corrected 09:30 schedule |
| Research-replay plan schema | `research-replay-plan/2.1.34` | `src/replay/session.py` | what a replay declares up front changes -- session, contract, bound sources, fill policy, probes |
| Research-session replay schema | `research-session-replay/2.1.34` | `src/replay/session.py` | what the replay report carries or what one of its flags means changes. Every trust flag other than `source_bytes_verified` is pinned false in this schema |

The engine version is part of the model fingerprint and therefore of the replay
hash: a change to the maths that did not move the hash would be undetectable.

### Commands

**Unix (bash):**

```bash
python -m pytest                     # everything
python -m pytest -m integration      # offline pipeline
python -m pytest -m regression       # frozen values
python -m pytest -m replay           # determinism
python -m pytest tests/unit/test_architecture.py   # cannot trade
python -m pytest tests/unit/test_release_integrity.py
python -m pytest --cov --cov-report=term-missing
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

**Windows (PowerShell):**

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest -m integration
.\.venv\Scripts\python.exe -m pytest -m regression
.\.venv\Scripts\python.exe -m pytest -m replay
.\.venv\Scripts\python.exe -m pytest tests\unit\test_architecture.py
.\.venv\Scripts\python.exe -m pytest tests\unit\test_release_integrity.py
.\.venv\Scripts\python.exe -m pytest --cov --cov-report=term-missing
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src
```

See [RELEASE.md](RELEASE.md) for the bootstrap and the release procedure.

## v2.1.36 intraday collection and session assembly

Everything runs offline on a fake clock (`src/ingest/clock.FakeClock`) and a
fake vendor (`tests/synthetic_session.SyntheticFeed`, which serves native-schema
bodies generated from that clock and learns the cycle it is serving from the
session directory); every cycle is the real one-shot command with its
preflight, per-request authorisation, manifest, attempt log and verification.
`tests/unit/test_collection_schedule.py` (22 cases): first slot 09:30:00 ET,
FULL every 30 minutes, 390/210 slots on regular/early-close sessions,
non-trading days refused, phases, fingerprint binding, policy refusals, the
repository specification, both clocks. `tests/unit/test_session_collector.py`
(17): the dry run writes nothing and its approval binds date, destination,
policy and budget; missing, wrong, stale or per-cycle approvals refused before
any request; existing destination left alone; nothing starts after the last
slot; each slot is one verified capture in its scope with receipts inside its
interval and no history/at-time request; late start, overrun and restart
accounting; resume needs the same session and no live lock; failed endpoints
recorded with the rest kept; consecutive empty cycles, a systemic 401 and an
exhausted budget stop the session; an operator interrupt is logged and the
lock released. `tests/unit/test_session_assembly.py` (28) on one scripted
session: A → B → A as three revisions the replay selects in order; an
unchanged quote emitted once and never made younger; a late revision of an
older event that does not displace the newer state; an ambiguous observation
leaving the known state standing; open interest and inventory reused only
through original receipts, a missing row unavailable; a failed Greeks request
keeping the cycle's other payloads with no model evidence; membership against
the latest listing; missed, overrun and restarted slots reported, never
backfilled; lineage from every record to cycle, request, payload, row and
rule, with the bytes rehashed; loading as `research-events/2.1.36`;
determinism across copies; tampered payload, log, intent, scope and events
lineage refused; receipt ties broken deterministically; readiness verdicts and
recomputation. `tests/unit/test_session_event_schema.py` (14): the store's
field-by-field refusals for the session schema.
`tests/regression/test_synthetic_session_end_to_end.py` (8): two clean
synthetic sessions through collector, assembler, replay, readiness and summary
-- only scheduled snapshot requests issued, every artefact `SYNTHETIC`, the
option side usable on each (7 usable decisions in the collected window, the
rest stale and reported), the summary refusing to call synthetic sessions
ready and refusing to mix them with the recorded September 2 capture; the
collector command's dry run, live refusal and schedule modes. The frozen
native fixture and the September 2 evidence were regenerated once under the
2.1.36 normalizer identifier; their records are byte-identical to the r2
ones apart from that identifier (checked while regenerating and recorded in
the completion report). The v2.1.34 replay regressions and the r2 duplicate
reproduction are retained unchanged.

The first cut of v2.1.36 was the first release verified on the operator's
Windows checkout (Python 3.12.10, win32): ruff, format, strict mypy, the demo
and the schedule command passed, 3,377 of 3,379 tests passed, and two failed
for Windows-only reasons -- the attempt log's ``index.jsonl`` written with
CRLF (text mode without ``newline``), and a tamper test whose glob matched the
listing payload before the quote payload on NTFS. The re-cut fixes both and
adds `tests/unit/test_raw_acquisition.py::test_the_attempt_index_is_written_with_lf_line_endings_on_every_host`,
which is a real regression test only on Windows; on Linux it always passed.
The Linux gate below was re-run on the re-cut; the Windows re-run is the
operator's.

r3 (after the independent review of r2; `docs/handoff/V2_1_36_COMPLETION.md`
has the findings) adds the regression tests the review asked for.
`tests/unit/test_session_collector.py` grows to 24 cases: an operator
interrupt between cycles, during a request (the one-shot's `OPERATOR_CANCELLED`
now stops the session; the partial capture is kept; resume never retakes the
slot), during a wait (`FakeClock.sleep_until` interrupted) and before a
cycle's first request (bootstrap-failure report), each asserting no
later-cycle request, `STOP` + `SESSION_END`, the summary's `interruption`, the
released lock and the raised `OperatorInterrupt`; and the request accounting
on a first-request 401 (one transport call, one attempt, four not attempted),
a partial acquisition, 503 retries (eight HTTP attempts for five requests) and
a transport failure without a response, each against the fake vendor's call
count. `tests/unit/test_session_assembly.py` grows to 35: the assembly's
`requests` block equals the collector's summary and the per-cycle recount from
each cycle's report and attempt log, the readiness report restates it, a log
whose accounting disagrees and a shortened attempt log are refused, a log of
the 2.1.36 schema is refused, a cancelled-then-resumed session assembles
with the interruption reported, and a slot the one-shot refused before it ran
is accounted as without a report and still assembles and summarises. `tests/unit/test_pilot_summary_validation.py`
(new, 24): the recorded September 2 report and the r2 synthetic session
report accepted unchanged as positive controls; the reviewer's stale-hash and
freshly-hashed-impossible-count reports refused; negative, float, string and
boolean counts refused; nested and total inconsistencies, session count and
budget inconsistencies, unknown origins, a disagreeing `synthetic_only`, a
synthetic report relabelled recorded, verdicts disagreeing with reasons,
trust claims, a missing hash and another schema refused; re-serialisation is
not a change. `tests/regression/test_synthetic_session_end_to_end.py` grows to
10: the clean sessions' accounting equal at every level through readiness and
summary, and the collector command's interruption and accounting messages.
The reviewer's probe script exits 0 on the r3 tree (its request-accounting
probe updated for the renamed field, shipped with the release beside the
original).

## v2.1.35 native normalization and readiness

The normalizer (`src/adapters/thetadata/research_events.py`) is tested on a
synthetic capture written in the vendor's native v3 column layout
(`tests/native_capture.py`) and on the lineage-bound source schema in the event
store (`tests/unit/test_native_normalization.py`, 77 cases;
`tests/unit/test_intraday_pilot_config.py`, 6 cases;
`tests/regression/test_native_capture_fixture.py`, 5 cases). Negative controls
first: a tampered payload, a native schema drift, a ragged row, a missing
endpoint, a listing date disagreeing with the valuation instant, an
undocumented settlement convention, a model fixed after its Greeks receipt,
out-of-range or non-integer tolerances, a payload without any recorded
receipt, an attempt receipt that contradicts the manifest, a receipt before
its request, a damaged attempt log, vendor timestamps after the receipt with
and without a tolerance, weekend-stamped and non-integer open interest, zero
and non-finite IV, vendor IV error, out-of-range delta, duplicates, unlisted
and non-SPXW identities, repeated identities that disagree (a crossed row
beside a clean one, two valid but different quotes, differing vendor times, a
malformed twin) in both input orders for quotes, Greeks, open interest and the
index row -- excluded whole, identity kept in the inventory, frame refused as
missing input -- and every lineage/provenance malformation the store must
refuse. Positive checks show exact repeats coalesce to the lowest row with the
clean control still passing, follow each emitted record's lineage back into
the raw bytes and re-read the row, prove availability equals the later
recorded receipt and never the event time, prove the command's five outputs
are reproducible byte for byte, and pin a frozen fixture
(`tests/fixtures/native/synthetic_2026-09-08/`: every capture file by digest --
no captured payload is tracked -- plus the frozen events, plan, readiness JSON
and Markdown). The real September 2 capture's readiness reports are in
`docs/evidence/`; they are evidence about data usability, not trading results.

## v2.1.34 verified replay

The replay (`research-events/2.1.34`, `research-replay-plan/2.1.34`,
`research-session-replay/2.1.34`) verifies source bytes against declared
digests, replays availability-indexed state over the full declared decision
grid and runs conservative counterfactual fill probes in exact decimal
arithmetic. Its tests (`tests/unit/test_verified_replay.py`,
`tests/regression/test_synthetic_replay_fixture.py`) are negative controls
first: tampering, duplicate keys and revisions, escaping paths, malformed
numbers, timestamps and identities, future inventory and model evidence,
delayed old rows, missing versus zero OI, holiday prior sessions, boundary
instants, stale or crossed quotes, undersized displayed liquidity, ineffective
profiles and overlapping probes are each shown to be refused or blocked. The
shipped bundle is synthetic; matching digests verify bytes, not vendor
authenticity or normalization. See `INTRADAY_RESEARCH.md`.

## v2.1.33 research diagnostics

The declared-data contract (`intraday-research-contract/2.1.33`) and audit
(`research-input-audit/2.1.33`) check supplied event/availability declarations.
They do not certify raw sources or a complete dataset. Gamma sensitivity
(`gamma-model-sensitivity/2.1.33`) recomputes model screening from verified
captures and preserves OI-available identity sets, without computing GEX.
See `INTRADAY_RESEARCH.md` for the fixed research scope and interpretation.
