# v2.1.34: source-verified session replay and conservative fill probes

## Scope and status

Implemented on the delivered v2.1.33 tree (source archive SHA-256
`8996bc0fdee0499cd550589ceb235e3bc4ad30aec8e6fdcb364b25d2c834d5a9`). The work
was done in an isolated Linux extraction with a freshly initialised, clearly
labelled imported Git history; it does not modify the Windows checkout or the
GitHub remote, and no remote push, merge, tag or CI run is claimed. No raw
capture, original certification, vendor correspondence, historical fixture or
trust rule changes.

New functional components:

- `src/replay/event_store.py`: strict UTF-8 JSON reading (duplicate keys,
  `NaN`/`Infinity`, BOM refused), canonical option and explicit-expiry futures
  identities, bounded decimals and integers, source SHA-256 binding with bounded
  relative paths, duplicate-bytes and duplicate-revision refusal, and an
  availability-indexed `EventStore` whose state at an instant is the newest
  `(event_at, sequence)` among records available by then. It also exposes every
  record available at an instant so effective-dated declarations can be chosen
  correctly.
- `src/replay/fill_probe.py`: structural probe validation (grid instants,
  sides, quantities, non-overlapping windows per instrument) and independent
  counterfactual fills: first quote observed after latency inside a bounded
  wait capped at the research close, BUY at ask / SELL at bid with adverse
  slippage and per-side fees in exact trapping `Decimal` arithmetic, profiles
  known at the decision and effective at arrival and fill, and one explicit
  refusal reason per unfilled probe.
- `src/replay/session.py`: calendar-aware decision grid, per-decision as-of
  inventory and scope accounting, the unchanged v2.1.33 research-contract
  audit plus crossed-quote and model-availability checks, per-frame hashes,
  probe accounting and a deterministic `research-session-replay/2.1.34` report
  whose trust flags stay false.
- `src/tools/replay_research_session.py`: offline CLI; refuses existing or
  in-bundle output paths and writes sorted-key UTF-8 LF JSON only on success.
- `tests/synthetic_replay.py`, `tests/unit/test_verified_replay.py`,
  `tests/regression/test_synthetic_replay_fixture.py` and the frozen bundle
  `tests/fixtures/replay/synthetic_2026-09-08/` with its report.

No GEX aggregation, broker path, position, portfolio, order, strategy, data
acquisition or PnL is added. The shipped example is synthetic and labelled so.

## Review findings corrected in this release

The handoff checkpoint listed inspection questions rather than diagnosed
failures. Resolved as follows:

- **Effective-dated profiles.** The WIP selected the newest known instrument or
  cost record and then checked validity, so a preannounced future profile could
  refuse a probe although an older profile was still in force. The release
  selects, among records available at the decision, the newest whose
  `[valid_from, valid_to)` covers the arrival, and additionally requires it to
  be in force at the fill instant. Reasons distinguish "not known at decision",
  "not effective at arrival" and "expired before fill".
- **Delayed old quotes.** The first record made available inside the window is
  binding. If the resulting state still predates the arrival (a late old row
  while a newer pre-arrival state is known), the probe is refused with
  `QUOTE_PREDATES_ARRIVAL`; nothing scans forward. The observed update instant
  is now reported separately from the quote's own event and availability times.
- **Decimal bounds.** Integers are bounded at `10**9`, decimals at twelve integer
  and twelve fractional digits, and cost arithmetic runs under a 120-digit
  context trapping `Inexact`/`Rounded`, so a result that would need rounding is
  refused (`DECIMAL_PRECISION_EXCEEDED`) instead of silently rounded.
- **Probe/grid consistency.** A probe whose decision is not a declared grid
  instant now refuses the plan instead of being reported as a blocked frame.
- **Strictness.** Kinds, keys, identities and `as_of` must be nonempty strings;
  source files are decoded as strict UTF-8; field-set errors name the expected
  fields. Locked (bid = ask) futures quotes are allowed; crossed ones are not.
- **Reporting.** The report now carries the contract, `probe_counts` by status
  and reason, and a limitation stating that the contract's age and skew limits
  are research design assumptions.

Unrelated observations recorded as follow-ups, not changed here: `README.md`
and `docs/RELEASE.md` carry a UTF-8 BOM and mojibake em dashes from an earlier
Windows round trip; `ruff` 0.15 introduces `UP042` findings in baseline files
that the pinned 0.14 line does not report.

## Independent review before promotion

An independent review of the first cut (imported-history commit
`9398798c4172`, bundle SHA-256 `2f61033a…ea8b75`) reconciled the supplied
evidence (90 modules, 3,171 cases, 0 failures/errors/skips; 90.94% coverage),
reproduced the v2.1.33 patch byte-for-byte, confirmed the five historical
live-capture fixtures unchanged, and reported one material defect (P2):
`effective_profile()` filtered every known version of an instrument or cost
declaration by its `[valid_from, valid_to)` interval before collapsing
revisions, so a known correction that shortened a profile's validity could fail
the filter while its superseded original survived and permitted a fill. The
reviewer's reproduction (two failing cases, two passing controls) was rerun
against the shipped source and confirmed.

Correction: among records available at the decision, each declaration
(`kind`, `key`, `event_at`) is first collapsed to its highest known `sequence`;
only then are effective intervals evaluated and the newest eligible declaration
selected. Distinct preannounced declarations remain separate. Regression tests
cover corrections delivered before or exactly at the decision (fee and
instrument), corrections delivered after the decision, delayed lower sequences,
corrections that extend validity, revised profiles expiring before the fill,
and the interplay with separate future-effective declarations. The reviewer's
reproduction script now exits 0. Two nonblocking packaging notes were also
applied: per-module test logs are included in the evidence, and the "lockfile-
exact" wording names the extra setuptools build backend. The first-cut bundle
is preserved and marked superseded; this document describes the re-gated,
re-archived release.

## Synthetic demonstration

`tests/fixtures/replay/synthetic_2026-09-08/` (events
`4f2bbf9984363d7485d639642e9ea7c14f07675d7cf6c40d48f7f9c57d9fcd8b`, plan
`7de930df6db9c8bb898f30e407d5de1b40921befaef986b316ed5d1b0d7bc9fe`) replays the
Tuesday after Labor Day with explicit zero OI as of Friday 2026-09-04. Default
grid: **371 decisions, 2 passing (09:35, 09:36), 369 blocked** by stale market
inputs. Probes: BUY 2 `SIMFUT|2026-09-18` at 09:35 → `100.50`, SELL 2 at 09:36 →
`99.75`, each with fee `1.40`, additional slippage `5.00`, total `6.40`; the
09:37 probe is `RESEARCH_FRAME_BLOCKED`. Report hash
`67e1fc90ba6b8ed8e1341cfc136c3e9c3e9d7316a6f8422c1c9ae659593010eb`. These are
hypothetical `SIMFUT` numbers, not contract specifications, observed fees or
strategy PnL. There is no observed strategy PnL to report.

## Verification

- Environment: Python 3.12.3 on Linux-6.18.44-fc-v24-x86_64-with-glibc2.39; every
  `requirements-lock.txt` pin installed offline from the downloaded wheels, plus
  one package the lockfile does not list: the `setuptools` build backend
  (within the declared `>=68,<86` range) needed for the offline editable
  install. ruff 0.14.14; mypy 1.20.2 (compiled: yes); pytest 9.1.1; coverage 7.15.2.
- `python -m ruff check .`: exit 0 (All checks passed!).
- `python -m ruff format --check .`: exit 0 (204 files already formatted).
- `python -m mypy src` (strict): exit 0 (Success: no issues found in 104 source files).
- `python -m src.app`: exit 0.
- Tests: **3,181 passed, 0 failed, 0 errors,
  0 skipped** across all 90 test modules, each module run in
  one of two isolated copies of the committed tree (with `.git`, so the
  release-integrity archive tests ran rather than skipped) under
  `pytest --cov --cov-append`; every module's exit code is 0.
- Coverage: per-worker data combined only after every module finished;
  `coverage report` with the repository's own `fail_under = 90`:
  **90.94%** line-and-branch over 14,832 statements
  (1,041 missing lines), exit 0.
- Extracted-archive checks from `docs/RELEASE.md` (imports, config load, demo,
  release-integrity, integration, certification smoke, replay CLI against the
  frozen bundle, replay tests, architecture) were run on the produced archive;
  their logs ship with the release evidence.

This is a Linux, Python 3.12 verification of the imported-history commit. It
is not a Windows, Python 3.13 or remote GitHub CI result. The gate was run once
to obtain these numbers and once more on the final commit that records them;
both runs, and the passes over the superseded first cut, are in the evidence
folder.

## Remaining limits

Matching digests verify bytes, not vendor authenticity, normalization
correctness, exchange-wide inventory completeness or independently authenticated
clocks. `SYNTHETIC` and `RECORDED_NORMALIZED` origins stay distinguishable and
neither is trusted. The 60-second age and 2-second skew limits are research
assumptions. The research-session cutoff is not a model of futures exchange
hours. The whole-expiration SPXW 2026-10-23 missing-OI case remains an
unresolved vendor investigation; the September 2 capture's 728 missing-OI
contracts remain excluded, never zero. Count coverage does not establish
exposure coverage or a reliable signed aggregate. Nothing in this release
establishes dealer positioning, trusted aggregate GEX or profitability.

## Next deliverable

Obtain and validate genuine, immutable intraday option observations with
measured availability and matching executable futures quotes (sizes,
effective-dated costs); run them through this replay; implement portfolio and
cost accounting; freeze one strategy specification and compare it with a
matched no-GEX baseline on chronological held-out sessions, tracking every
variant, costs, uncertainty and drawdown. Those are later milestones, not part
of v2.1.34.
