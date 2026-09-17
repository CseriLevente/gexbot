# Offline pricing diagnostics

v2.1.31 adds a direct comparison of model assumptions against a saved SPXW
first-order Greeks response. It addresses an omission in the historical
certification reconstruction: that reconstruction scores time to expiry without
the 60-minute floor recorded in the capture's pinned documentation.

This command produces a new `pricing-diagnostics/2.1.31` report. It does not
rewrite the certification algorithm, its thresholds, its conclusions, or any
previous report. It computes delta residuals and contract-count coverage, not
GEX, trading signals, orders or profitability.

## Run

Use Python 3.12 or 3.13 with the repository's dependencies. Both inputs remain
read-only; the report destination must be new and outside the capture directory.

```powershell
.\.venv\Scripts\python.exe -m src.tools.diagnose_thetadata_pricing `
  "C:\ThetaDataCaptures\capture-2026-09-02-close" `
  --archive-path "C:\ThetaDataCaptures\capture-2026-09-02-close.zip" `
  --json "C:\ThetaDataCaptures\capture-2026-09-02-pricing-diagnostics-v2.1.31.json"
```

Exit 0 means the diagnostic report was written. Exit 2 means an input or output
was refused. Neither exit code authorizes analytical or trading use. Omitting
`--archive-path` verifies the capture directory but records archive identity as
unknown; it does not fabricate archive evidence.

## Method and limits

The command reuses `certify_capture`, `load_capture`, the analytical-universe
partition, and the existing standard-library Black-Scholes delta function.
The report carries the recomputed capture, certification and analytical hashes.
A supplied ZIP must verify against that same capture. Output uses exclusive
creation and LF line endings. Its semantic digest uses the existing portable
canonicalization, with no machine paths in the payload.

Every candidate uses the same admitted identities. Admission requires a listed,
nonexpired SPXW identity, positive spot and strike, finite inputs, IV within the
engine's existing 0.0001-to-5 range, a nonsaturated delta with the correct sign,
and option/underlying timestamps in the capture session before 16:00 ET.
Excluded rows are counted by reason. This timestamp filter is a diagnostic scope,
not a complete trading-calendar or quote-freshness authorization.

The command supports an explicitly requested zero annual dividend. It assumes
zero yield in every candidate; it does not identify the dividend convention.
Every candidate uses the Greeks row's embedded underlying price.

The fixed 192-candidate grid is the Cartesian product below. It is declared in
code before execution, not fitted separately to each expiration.

| Dimension | Candidate values |
|---|---|
| Rate reading | Captured wire value; captured wire value divided by 100 |
| Days per year | 365; 365.25; 360; 252 |
| Model expiration clock | 16:00 ET; 17:00 ET |
| Time rule | Continuous elapsed time; whole days at DTE >= 7; whole days at DTE > 7 |
| Valuation timestamp | Embedded underlying timestamp; option timestamp |
| Minimum remaining time | None; 60 minutes |

The 17:00 candidate is a clock perturbation, **not a claim about SPXW trading
hours**. Clock hypotheses do not adjust for early closes. Continuous time counts
elapsed UTC seconds across DST changes. Whole-day rules use calendar dates.
The floor applies to remaining time, not as a one-hour shift to every expiration.

Ranking is exploratory and in-sample. The development exploration also tested
a legacy 0.15-day/whole-day convention (256 candidates including redundant
cases); the shipped grid omits that legacy convention. This is not a held-out
model-selection exercise. Close-to-close timing and rounded IV/delta fields can
leave several candidates observationally similar. The command intentionally
selects no vendor model and changes no acceptance threshold.

All candidates have aggregate residual metrics. For the numerical best, the
report also compares exactly the same parameters with the other time floor,
overall and by expiration and strike/spot bucket. These are absolute delta
errors, not percentage P&L errors. Per-expiration groups partition the sample;
moneyness groups are a separate partition, so their row counts must not be added
together.

OI coverage is reported against temporally current listed identities as well
as by expiration. Missing and explicit zero remain separate. Pricing rows can
include identities without OI, because delta reconstruction does not use OI;
their count and the OI-available intersection hash are explicit. That
intersection is not certified as a usable GEX dataset. Unknown OI implies
unknown missing exposure, so `gamma_weighted_coverage` is null.

## September 2 findings

The original certification and analytical hashes reproduce exactly. There are
13,536 listed identities, of which 496 expired before the session. The 13,040
current identities have 12,312 OI answers and 728 missing rows: **94.4172%
contract-count coverage**, not gamma coverage. SPXW 2026-10-23 still has all 216
listed identities missing from OI.

The direct comparison admits 11,326 rows; 1,625 have degenerate pricing inputs,
89 have saturated/invalid deltas, and 496 expired before the session. Of admitted
rows, 10,661 have OI and 665 do not. The historical reconstruction admitted
11,379 rows: the diagnostic's explicit IV ceiling excludes 53 additional rows.
Do not present the old and new aggregate scores as a controlled comparison.

The controlled comparison below holds the 11,326 rows, rate 0.042, ACT/365,
16:00 model clock, continuous time and option timestamp fixed.

| Remaining-time floor | All-row delta RMSE | 0DTE delta RMSE (362 rows) |
|---|---:|---:|
| None | 0.00562470790 | 0.0314585079 |
| 60 minutes | 0.0000824334477 | 0.0000602947490 |

The all-row RMSE is approximately **68.2 times smaller** with the floor.
Later expirations are unchanged in this controlled contrast. This isolates the
short-dated floor as the principal defect in that direct reconstruction.

The underlying-timestamp version scores 0.0000832868619 and the alternative
seven-day boundaries approximately 0.0000871–0.0000878. Those small differences
are not evidence that one convention has been uniquely identified. IV price
basis, solver behavior, and missing exposure remain unresolved. The result
supports a floor-aware, censored inference investigation as the next numerical
step; it does not establish a profitable strategy.
