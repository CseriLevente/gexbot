# v2.1.33: research contract and gamma-model sensitivity

## Scope and status

Implemented on the delivered v2.1.32 tree. This local imported Git history does
not modify the Windows checkout or GitHub remote. No raw capture, original
certification, vendor correspondence, historical fixture or trust rule changes.

New functional components:

- `src/replay/research_contract.py`: strict declared-input checks for prior-session
  OI, event/availability times, decision grids, holidays, early closes and market
  age/skew. Reports always remain unverified and not ready for backtesting.
- `src/adapters/thetadata/gamma_sensitivity.py`: verified-capture model selection,
  common OI-answered contract population and unweighted per-contract gamma ranges.
- `src/tools/audit_research_inputs.py`: offline, nonoverwriting commands for both.
- `config/intraday_research.json` and `docs/INTRADAY_RESEARCH.md`: explicit initial
  scope and requirements for a future source-verified intraday replay.

No GEX aggregation, broker path, strategy, data acquisition or PnL is added.
The gamma comparison is retrospective: its models use all four capture dates.
The declared-input audit does not verify raw bytes, canonical identity joins,
full inventory coverage or a complete intraday grid. Those belong to the next
replay implementation. Missing OI stays unavailable, including the previously
reported fully missing SPXW 2026-10-23 expiration.

## Preserved-capture evaluation

The unchanged v2.1.32 screening retains the same six complete models on August
24, 26, 27 and September 2. The source cross-capture semantic hash remains
`4199cd3dfda419980a58278de2aa07f6b93667209cd5be4d652fcaf654eafcf6`.

The 0-7 DTE, admitted OI-answered sample yields these unweighted per-contract
ranges (100 × (maximum - minimum) / maximum model gamma):

| Session | Contracts | Median | 95th percentile | Maximum | Largest absolute gamma range |
|---|---:|---:|---:|---:|---:|
| 2026-08-24 | 3,147 | 0.014176% | 0.147821% | 3.447045% | 1.76804564e-06 |
| 2026-08-26 | 2,589 | 0.000588% | 0.046536% | 0.825071% | 5.31454346e-07 |
| 2026-08-27 | 2,394 | 0.000870% | 0.051579% | 15.170224% | 5.51176812e-06 |
| 2026-09-02 | 1,918 | 0.000043% | 0.100315% | 2.534366% | 5.73774571e-07 |

Absolute gamma units are delta change per index point. The largest relative
range is SPXW 2026-08-28 7990 CALL in the August 27 capture, spanning
0.00003082103735660076 to 0.00003633280547319347. It is not an exposure-weighted
portfolio estimate. OI-answered admitted samples across all expirations contain
11,498 / 10,088 / 9,610 / 10,661 contracts respectively. Missing OI removes
59 / 389 / 258 / 665 pricing-admitted rows respectively; these are sample counts,
not the full listed-universe missing counts.

All admitted 0DTE rows have zero within-set gamma range because every retained
model applies the same floor. This does not establish that the floor or the
computed gamma is correct, or rule out IV/solver/untested-model uncertainty.
Small median ranges cannot establish stability of signed GEX, walls or a trading
strategy. No materiality threshold was fitted from this diagnostic.

Gamma report semantic hash: `5f053122040f0b033af9309ca0b20e56b6556f2fa02a0026a4f594d6e158cdb2`.
The independently emitted empty-template audit reports zero frames and stays
unverified/not ready for a backtest. No intraday observations are fabricated.

## Verification

- Python 3.12.14 on Linux: **3,032 passed, 0 failed, 0 skipped**
  across all 88 test modules.
- Aggregate line-and-branch coverage: **90.64%** across
  14,386 statements, passing the unchanged 90% gate.
- Ruff 0.14.14: lint and formatting clean (197 files).
- Mypy 1.20.2: 100 source files, no issues. Application smoke passed.
- Six original input hashes unchanged; gamma report and capture bindings verify.

All modules ran on six isolated copies of the same final source. Coverage is
checkpointed per worker and combined only after every module completes. No
failed or skipped module is omitted. The existing offline client-construction
test removes inherited proxy variables; it sends no vendor requests. Recorded
compatible dependencies were used. This is not an exact-lockfile, Windows,
Python 3.13 or remote GitHub CI claim.

## Next deliverable

Build source-verified chronological replay against an intraday development
dataset and matching futures bid/ask data. Verify as-of inventories, actual
availability, missing decision frames, and effective-dated contract/cost data.
Then compare one frozen strategy with and without a GEX input on held-out
sessions after costs, retaining the same fill assumptions and all attempted
variants. The close-only captures cannot supply those intraday observations.
