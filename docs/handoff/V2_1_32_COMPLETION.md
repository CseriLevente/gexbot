# v2.1.32 floor-aware pricing inference

## Scope

Prepared on top of the delivered v2.1.31 source package, using the uploaded
v2.1.30 archive as the original baseline. Development commits belong to a
local imported history, not the original Windows or GitHub repository.

The new `floor_inference` module and `infer_thetadata_pricing` CLI screen the
fixed 192-candidate grid jointly and preserve every adequate near-best model.
Clock evidence uses a second common population with all potentially floor-bound
observations removed. A floored observation is a bound on time, not an exact
expiration-clock measurement. A dimension is replicated only if distinct
sessions each support the same value individually. Reports are recomputed
from verified capture directories and ZIPs, never caller-built summaries.

The historical inference, numerical engine, OI policy, trust gates and capture
fixtures are unchanged. New reports do not authorize GEX or orders. No raw
payload or private email is included in the source package.

## Evaluation

The protocol in [FLOOR_AWARE_INFERENCE.md](../FLOOR_AWARE_INFERENCE.md) was
frozen before evaluating August 24, 26 and 27. September 2 is the development
capture; the earlier sessions are independent retrospective observations,
not prospective holdouts. No thresholds or grid candidates are tuned from
those results.

The fixed protocol produced the same six retained global candidates on all
four sessions. Each session independently supports decimal rate units, a
365-day denominator, the 60-minute floor candidate (versus zero), and the 16:00
clock candidate (versus 17:00). Clock support also holds on uncensored rows;
it still does not identify an exact clock or validate an early-close schedule.

Continuous time versus either front-week/calendar-day rule remains ambiguous,
as does option versus underlying timestamp. The six models are the Cartesian
product of those three time rules and two timestamps. No full model is selected.

| Session | Admitted rows | Removed from clock comparison | Best delta RMSE |
|---|---:|---:|---:|
| 2026-08-24 | 11,557 | 364 | 0.0000802830 |
| 2026-08-26 | 10,477 | 328 | 0.0000810388 |
| 2026-08-27 | 9,868 | 350 | 0.0000879052 |
| 2026-09-02 | 11,326 | 362 | 0.0000824334 |

These are conditional reconstruction results, not parameter confidence intervals.
Untried floors/clocks, IV rounding and solver choices remain outside the grid.
All captures retain their historical trust blockers, and unavailable OI remains
excluded. Count coverage cannot bound the gamma exposure of missing contracts.

Cross-capture report semantic hash:
`4199cd3dfda419980a58278de2aa07f6b93667209cd5be4d652fcaf654eafcf6`.

The separate validation JSON binds all four original ZIP identities and every
per-capture report. No raw input is added to the source distribution. Original
Windows archive separators were normalized when extracting working copies;
archive bytes and member payload bytes were preserved and verified.

## Verification

- Python 3.12.14 on Linux: **2,987 passed, 0 failed, 0 skipped**
  across all 86 modules, with coverage checkpointed per completed module.
- Coverage (lines and branches): **90.48%** across
  14,164 statements, satisfying the unchanged 90% aggregate gate.
- Ruff 0.14.14: lint clean; 192 files format-clean.
- Mypy 1.20.2: 97 source files, no issues. Application smoke test passed.
- All six original input hashes are unchanged. Nested report hashes verify.
- Four sessions independently retain the same six global model alternatives.

The interruption completed 27 modules (967 tests). Their passing checkpoints
and coverage were retained; the remaining 59 modules were resumed on the same
source. No test was skipped or replaced. Six isolated copies prevent temporary
Git-probe tests from interfering. The aggregate coverage gate runs after all
coverage data is combined. Compatible dependency versions are recorded in the
verification evidence; this is not an exact-lockfile, Windows/Python 3.13 or
GitHub CI verification. Inherited proxy variables are removed only for the
existing offline client-construction test module, as in v2.1.31.


## Next development boundary

1. Specify a point-in-time research dataset: target SPXW expirations, trading
   instrument, timestamp alignment, prior-session OI availability and explicit
   treatment of unavailable observations. Compare OI-complete scopes separately
   from the full universe; a reduced scope does not repair missing exposure.
2. Resolve or propagate remaining pricing-model alternatives. An ensemble or
   sensitivity analysis must show when a proposed signal changes with the model;
   a convenient single minimum-RMSE candidate is insufficient.
3. Build intraday replay on preserved historical inputs. Close-only snapshots
   cannot evaluate entries, exits, intraday slippage or strategy profitability.
4. Freeze a simple strategy and a matched baseline without GEX. Compare them
   on chronological held-out sessions with fees, spread, slippage, latency and
   day-level uncertainty; retain losing sessions and rejected signals.
5. Consider paper execution only after reproducible net-performance evidence.

These are research and data tasks. Software tests and price reconstruction do
not establish profitable trading. No live capture, broker integration, remote
push or merge is part of this release.
