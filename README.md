# gex-futures-bot

A **GEX research engine** for SPX/SPXW option chains.

> **This is not a trading bot.** It cannot place an order. There is no broker
> adapter, no risk engine, no strategy layer and no execution path — and an
> architecture test enforces that none appear by accident. It computes gamma
> exposure from an option chain and reports how much to trust the result.

Everything it has been run against is synthetic or fixture data. It has never
seen a live vendor response.

---

## Try it

No subscription, no API key, no network:

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m src.app        # full GEX snapshot, synthetic chain
.venv/Scripts/python.exe -m pytest         # 2289 tests, 90% coverage
```

The engine core (`src/gex`, `src/domain`, `src/synthetic`) executes **no
third-party code**, so the maths is verifiable on a bare interpreter. Its one
runtime dependency is `tzdata` — the IANA timezone database, which ships data
and no importable logic. Before v2.1.7 US Eastern was hand-written to avoid even
that, and the hand-written zone could not represent the repeated hour of the
autumn DST transition: it returned an instant an hour wrong. A wrong instant is
worse than a data dependency.

---

## Status

Every row below carries one of six explicit labels. They mean exactly what they
say and nothing more:

| Label | Meaning |
|---|---|
| `IMPLEMENTED` | The code exists and runs. |
| `TESTED_SYNTHETICALLY` | Verified against generated inputs and closed-form identities. |
| `TESTED_WITH_OFFLINE_FIXTURES` | Verified against recorded vendor-shaped payloads. No network. |
| `NOT_VALIDATED_WITH_LIVE_THETADATA` | Never run against a real subscription. |
| `READY_FOR_RAW_CAPTURE_ONLY` | Offline checks pass and the capture may proceed; one paid vendor session is the next evidence. Says nothing about whether a number computed from it could be trusted. |
| `PLANNED` | Designed, not built. |
| `NOT_IMPLEMENTED` | Absent. Some of these are absent on purpose. |

**No component in this repository has ever been validated against live vendor
data.** Every integration row is `NOT_VALIDATED_WITH_LIVE_THETADATA`.

| Component | Status |
|---|---|
| Black-Scholes shadow pricer | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` against closed-form identities |
| Effective-model resolver (single source of pricing inputs) | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| US Eastern clock, SPX/SPXW settlement | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Trading calendar (holidays, early closes, session ageing) | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Record validation (finiteness, structure, time) | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Per-record timestamp preservation, DST boundary handling | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| View 1 — unsigned gamma concentration | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| View 2 — naive signed GEX | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| View 3 — expiry buckets | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| View 4 — strike level, walls, classified voids, local strike ladder | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| View 5 — zero-gamma grid, 3 of 5 IV conventions | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Option-universe accounting, SPX/SPXW separation | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Confidence model | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; thresholds `NOT_IMPLEMENTED` (uncalibrated by design) |
| Typed config, ThetaData config section, single client factory | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| ThetaData parsing, join, tier map | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` · `NOT_VALIDATED_WITH_LIVE_THETADATA` |
| HTTP transport, retries, `Retry-After`, size caps | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` (deterministic fake) |
| Raw-response store (atomic, collision-safe) | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| Real network transport (`HttpxTransport`) | `IMPLEMENTED` · **never executed** · `NOT_VALIDATED_WITH_LIVE_THETADATA` |
| Chain completeness vs an independent source | `IMPLEMENTED`, reports `PARTIALLY_OBSERVED`/`UNKNOWN` end to end — no contract-list endpoint is wired (OD-11) |
| Unknown completeness cannot score full confidence | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| ThetaData config → effective runtime (`ThetaDataRuntime`) | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Strict config validation (finite, typed, non-empty) | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Current-GEX eligibility by underlying-price source | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Exact integer parsing (no float round trip) | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Malformed vs missing float classification | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| Streaming response-size cap (`ByteLimitedReader`) | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| BASIC auth fails construction without credentials | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Per-source timezone localisation summaries | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| Raw-store integrity scanning (`verify_integrity`) | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Host-independent minimal-core check | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Identity-based chain completeness | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Unified research pipeline (`ThetaDataResearchPipeline`) | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Vendor-IV / local-gamma compatibility report | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; vendor conventions `UNKNOWN` |
| Mixed effective-model reporting and policy | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Structured parsing of every vendor float | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| CSV body validation for zero-row responses | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| Exact decimal strike identity | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Per-contract selected-source timestamp provenance | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| One adapter exception hierarchy | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Schema-safe raw-store integrity scanning | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Collision-safe capture sessions | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Adapter-certification readiness | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; the shipped default is `READY_FOR_RAW_CAPTURE_ONLY`, and cannot be trusted to calculate while eight load-bearing vendor conventions are unknown |
| Derived certification (verifier and validator run inside readiness) | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; a caller cannot supply a verdict |
| Field-level provenance re-read from the payload | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| Capture plan: every endpoint the session needs | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Vendor index spot captured in the same session | `IMPLEMENTED` · **never run against a vendor** · `NOT_VALIDATED_WITH_LIVE_THETADATA` |
| Trusted vs diagnostic calculation | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; unresolved pricing refuses a trusted GEX |
| Exact decimal strike carried through the domain | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Typed capture and validation evidence | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; `verify_capture` checks a manifest against its store |
| Trusted calculation requires independently verified evidence | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; `compute_trusted_gex(chain, context=...)`, and a chain's own metadata authorizes nothing |
| Per-record manifest descriptors, bound field by field | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; the manifest hash covers full per-record semantics |
| Durable storage required for paid capture | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; `InMemoryRawStore` stays supported for tests and cannot be capture-ready |
| Capture origin stamped by the transport | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES`; an offline fixture cannot read as a live capture |
| Post-capture pricing compatibility | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES`; validated observations reach the gate, and a live mismatch blocks |
| Chain-level convention coverage (every row, every record) | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES`; one matching contract cannot characterise a chain |
| One vendor timestamp interpretation (`src/domain/vendor_time.py`) | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; adapter and validator read the same string identically |
| Trusted calculation bound to the re-derived chain | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES`; the stored payloads are normalized again and the two canonical hashes must agree |
| Capture-operation identity on every record | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; both timestamps, the rule that chose one, the spot policy, the settlement rule and the expected universe, hashed whole |
| Valuation instant derived from the verified index print | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES`; the chain under test no longer chooses the timestamp it is checked against |
| Spot timestamp and skew tolerance derived, never supplied | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES`; `max_spot_skew_seconds` is configuration and enters the pipeline fingerprint |
| Settlement-date evidence resolved rather than declared | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; the kind selects which check runs, and the production documentation registry is empty (OD-26) |
| Content-bound documentation evidence | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES`; a rewritten page moves the pipeline fingerprint |
| Chain completeness as a typed field | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; an architecture test fails the build when GEX code reads calculation-affecting data from `meta` |
| Capture-bound expected contract universe | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; declared on the session, checked at replay, never adopted from the caller |
| Exact record consumption on replay | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; a second response per endpoint needs a plan that declares why |
| Settlement rule chosen before the capture | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; a capture that established none can never become trusted, because no later call accepts one |
| Settlement dates derived from typed rule semantics | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; applied through the real trading calendar, weekends, holidays and Good Friday included |
| Documentation bytes read and hashed at registration | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES`; a missing file or a mismatched hash is refused |
| Resolved OI date through normalization, chain and replay | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES`; the trusted path refuses a chain carrying a different date, or none |
| One authoritative `ExpectedContractUniverse` | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; an architecture test fails the build if a second definition appears |
| Expected-universe evidence re-derived from its records | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; identities parsed out of the named bytes and compared |
| Partial universes cannot claim full completeness | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; two distinct partial statuses, neither implying complete |
| Operation digests recomputed from their own fields | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; `OPERATION_FINGERPRINT_MISMATCH` on any edited field |
| Field evidence rereads the exact named record | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; required before pagination or partitions can be certified |
| Content-addressed artifact store | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; the stamped digest is the lookup key, so replay recovers the object rather than only its name |
| Snapshots cannot pass as contract lists | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; a response with one row per contract enumerates its own rows, not the request's universe |
| Universe coverage derived, not declared | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; `UniverseCoverageStatus` is a resolver output and a caller cannot grant `FULL_REQUEST_ENUMERATED` |
| Pagination coverage read from the responses | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; unsupported where no ThetaData endpoint returns page metadata, rather than simulated |
| Universe documentation separate from settlement documentation | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; a settlement-convention document establishes no contracts |
| Verified expected-universe artifact | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; only a resolver-produced artifact can make completeness independent |
| Universe source scope and timing checked | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; root, expirations, strikes, rights, ordering and staleness, before the chain operation opens |
| Completeness independence is typed | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; the artifact hash and coverage status decide it, never the `expected_source` label |
| One market-session date helper | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; `America/New_York`, with an AST test that no other site derives one |
| `READY_FOR_ANALYTICAL_DATASET` | `NOT_READY` — requires `FULL_REQUEST_ENUMERATED` coverage, and no verified contract-list or pagination source exists (OD-11) |
| One-shot raw-capture command | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES`; `python -m src.tools.capture_thetadata_once`, dry run by default, refuses a destination inside the repository, computes no GEX |
| Universe evidence authorized by a resolution, not a type | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; `capture_session` re-runs the resolution and compares the artifact hash |
| Universe sources come from a verified capture | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; non-2xx, incomplete writes and unsupported parsers are refused |
| Source pipeline and request scope derived from the records | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; `min_time` and every other contract-set filter is read back out of the stored request |
| Documentation identities extracted from bytes | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; a rule names a document and an extractor version and cannot carry an identity list |
| Documentation effective periods enforced | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; via the shared New York market-session helper |
| Recovery compares the whole semantic artifact | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; artifact-hash equality, with the first differing field named |
| `assess_analytical_readiness` checks all six conditions | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; the completeness-only function is now `universe_readiness_of` |
| Trusted API derives its own authority | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; it takes evidence, not a verdict |
| Records stamped with pipeline, plan, request spec and recipe | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; a capture cannot be relabelled as another pipeline's |
| Canonical expected request per endpoint | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; a capture taken at `rate_value=4.2` does not verify against a pipeline configured with 3.1 |
| OI value evidence separated from OI settlement-date evidence | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; a caller-assumed date blocks a trusted GEX and permits capture and diagnostics |
| US Eastern via `zoneinfo` with pinned `tzdata` | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; the repeated autumn hour is two instants |
| `READY_FOR_ANALYTICAL_DATASET` as a separate axis | `PLANNED` — the requirements are written down; nothing consumes an analytical dataset yet, by design |
| Graded provenance (PLANNED / OBSERVED / VALIDATED) | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; derived from a named raw record, never asserted |
| Typed pricing dimensions and attestations | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; no comparison has been run, so nothing carries `LIVE_COMPARISON` evidence |
| Canonical pipeline API (`capture_session` / `fetch_chain` / `compute_diagnostic_gex` / `compute_trusted_gex`) | `IMPLEMENTED` · `TESTED_SYNTHETICALLY`; `compute_gex` and `capture_and_compute` were removed in v2.1.5 when capturing and computing were separated |
| ThetaData capture profile (`config/thetadata_capture.yaml`) | `IMPLEMENTED` · **never run** · `NOT_VALIDATED_WITH_LIVE_THETADATA` |
| Pricing mode derived from IV provenance | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Vendor/local rate and dividend value comparison | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Tier capability matrix | `IMPLEMENTED` · `NOT_VALIDATED_WITH_LIVE_THETADATA` |
| One pipeline from `LoadedConfig` | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Raw-capture manifest linking payloads to snapshots | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| ThetaData NBBO bid/mid/ask IV | vendor-computed; an NBBO *price* basis does not make the IV local |
| `LOCAL_IV_LOCAL_GAMMA` | `NOT_IMPLEMENTED` — needs a local IV solver; refused at config load |
| `TRADE_IV`, `LOCALLY_SOLVED_MID_IV` | `NOT_IMPLEMENTED` — refused at config load, not silently substituted |
| Local gamma vs vendor gamma | `NOT_VALIDATED_WITH_LIVE_THETADATA` |
| Whether ThetaData Standard tier suffices | `NOT_VALIDATED_WITH_LIVE_THETADATA` — it *appears* to expose the required inputs; that is not the same claim |
| Zero-gamma stability across real intraday sequences | `NOT_VALIDATED_WITH_LIVE_THETADATA` |
| `STICKY_DELTA`, `SURFACE_REFIT` conventions | `NOT_IMPLEMENTED` (refuse explicitly rather than approximate) |
| `CALENDAR_MIDNIGHT` expiration rule | `NOT_IMPLEMENTED` — declared but rejected; no index option settles at midnight |
| Feature store, regime classifier, strategies | `NOT_IMPLEMENTED` |
| Risk engine, broker adapter, order placement, paper trading | `NOT_IMPLEMENTED`, and out of scope |
| Futures data (Databento), IBKR, Cboe Open-Close | `NOT_IMPLEMENTED` |

### What is deliberately absent

**This repository cannot trade.** There is no broker adapter, no order type, no
position sizing, and no execution path — live or paper.
`tests/unit/test_architecture.py` fails the build if one appears, and CI runs it
as its own visible check.

There is no risk engine. Earlier documentation claimed `ConfidenceScore.calibrated`
was "enforced by the risk engine" and that live trading was "blocked" — that was
wrong, and is corrected in [`docs/CHANGELOG.md`](docs/CHANGELOG.md). Nothing is
blocked because nothing can trade. `calibrated` is a research signal that market
thresholds are still unresearched.

No claim is made anywhere in this repository that a strategy is profitable,
because no strategy exists to make a claim about.

---

## Read these

| Document | Why |
|---|---|
| [`docs/OPEN_DECISIONS.md`](docs/OPEN_DECISIONS.md) | **Start here.** Every assumption that could be wrong, and what would settle it. |
| [`docs/MODEL_ASSUMPTIONS.md`](docs/MODEL_ASSUMPTIONS.md) | Every parameter that changes a number. |
| [`docs/FORMULAS.md`](docs/FORMULAS.md) | The maths, with the design note for each. |
| [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md) | Every output field and what it does *not* mean. |
| [`docs/VALIDATION.md`](docs/VALIDATION.md) | Record validation rules and test strategy. |
| [`docs/THETADATA_INTEGRATION.md`](docs/THETADATA_INTEGRATION.md) | Wiring up a real subscription, and what to check first. |
| [`docs/CHANGELOG.md`](docs/CHANGELOG.md) | What changed in v2.1.2, including corrected claims. |
| [`docs/RELEASE.md`](docs/RELEASE.md) | Bootstrap, verification, and how the release archive is produced. |
| [`docs/ADAPTER_CERTIFICATION.md`](docs/ADAPTER_CERTIFICATION.md) | What must hold before spending one session on real vendor data. |
| [`docs/handoff/data-requirements.md`](docs/handoff/data-requirements.md) | Vendor endpoints, tiers, prices. |

---

## Design decisions worth knowing

**1. Nothing is back-stamped to the request timestamp.**
Each contract keeps `quote_timestamp`, `greeks_timestamp`, `iv_timestamp`,
`underlying_timestamp`, `open_interest_as_of`, and our own request/response/
normalisation clocks. Assigning `as_of` to every record — the v1 behaviour —
makes a five-minute-old quote indistinguishable from a fresh one and reports the
whole chain as perfectly fresh regardless of what arrived. Freshness that is
assigned rather than measured is worse than none.

**2. Every model assumption travels with the number.**
Two correct Black-Scholes implementations can disagree by 20% on a 0DTE gamma
because one floors time-to-expiry at 30 minutes and the other at 60. `ModelSpec`
is embedded in every snapshot and hashed into a fingerprint, so a disagreement is
investigable instead of mysterious.

**3. Observations are separated from interpretations.**
"The strike with the most call gamma" is a fact. "Resistance above" is a claim
that is only true if that strike is above spot. `largest_call_gamma_strike` and
`upside_call_wall` are different fields, and the second is `None` when nothing
qualifies rather than silently degrading into the first.

**4. Absent data is not quiet data.**
A strike range the vendor never sent looks identical to a genuinely low-gamma
region. Voids are classified against an inferred strike ladder, and only
`TRUE_LOW_GEX_VOID` reports as tradable structure.

**5. `UNSPECIFIED_CALIBRATE` is an object, not a number.**
It is falsy and raises `TypeError` on ordering comparisons, `float()`, `int()`
and arithmetic. The likeliest accident is a caller coercing it to make types line
up, converting a loud "not researched" into a quiet, arbitrary threshold.

**6. US Eastern is implemented, not imported.**
`zoneinfo` needs `tzdata`, absent on a bare Windows install. Being an hour out on
an SPXW expiration afternoon does not produce a slightly wrong gamma — it
produces a completely wrong one. A test cross-checks two years of daily offsets
against the real tz database whenever `tzdata` *is* installed.

**7. The output is a function of the data, not of arrival order.**
Float addition is not associative, so vendor row order changed the last bits of
every sum. Contracts are sorted into canonical order before aggregation, and a
replay test reverses the input rows to prove it.

---

## Layout

```
config/          research.yaml (usable) / paper.yaml, live.yaml (disabled)
docs/            model assumptions, formulas, data dictionary, open decisions
src/
  domain/        contracts, iv, timestamps, validation, model_spec, gex, states
  gex/           pricing, sessions, calendar, formulas, walls, zero_gamma,
                 confidence, engine, config
  adapters/      base (protocols), transport, raw_store, thetadata/, synthetic/
  config/        typed schema loading
  synthetic/     deterministic chain generation (production, not test-only)
  app.py         runnable demo
tests/
  unit/          per-module rules and formulas
  integration/   offline pipeline through the fake transport
  regression/    frozen, hand-transcribed expectations
  replay/        determinism and output-hash stability
  fixtures/      stored vendor responses
```

---

## Commands

```bash
python -m src.app                    # demo
python -m pytest                     # all tests
python -m pytest -m integration      # offline pipeline
python -m pytest -m regression       # frozen values
python -m pytest -m replay           # determinism
python -m pytest --cov --cov-report=term-missing
python -m ruff check .               # lint
python -m ruff format --check .      # format
python -m mypy src                   # types (strict)
```

Python 3.12 or 3.13.

---

## If you are picking this up

Read [`docs/OPEN_DECISIONS.md`](docs/OPEN_DECISIONS.md) first. It lists the
assumptions that could be wrong and what evidence would settle each one. The
three that matter most:

1. **Vendor timestamp timezone** — one live response settles it, and getting it
   wrong invalidates every 0DTE number.
2. **Local gamma vs vendor gamma** — never measured; the tier recommendation
   rests on it.
3. **The 0DTE time floor** — configurable and sensitivity-reported, but not
   resolved.

And before any live data: the OPRA non-display licensing question in
[`docs/handoff/data-requirements.md`](docs/handoff/data-requirements.md) §5. It is
the one item that can invalidate the project after the engineering is finished.
