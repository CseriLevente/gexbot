# Floor-aware pricing inference

## Frozen protocol

This v2.1.32 protocol is recorded before examining the August 24, 26 and 27
validation results. September 2 was the development capture. Earlier captures
are retrospective independent sessions, not prospective or untouched trading
holdouts. No threshold or candidate is tuned after their evaluation.

The model family remains the 192 hypotheses shipped in v2.1.31. The existing
certification heuristics are reused: at least 30 rows, adequate RMSE no greater
than 25 times the delta rounding noise (0.0001/sqrt(12)), and retention of every
adequate candidate within one noise floor of the best. These cutoffs express
model-screening policy; they are not statistical confidence levels.

All model dimensions are compared jointly on the same admitted rows. A value
is supported within this finite grid only when every retained candidate agrees.
An inadequate fit resolves nothing. Ties and alternative upstream assumptions
remain explicit. A supported model parameter is not a complete vendor model.

For clock inference, any observation that binds the floor under any tested
model is removed from a second, common comparison population. The equation
`T_effective = max(T_raw, floor)` permits an upper bound on raw remaining time
when the floor binds; inverting that observation into an exact expiry clock
would be false precision. Neither a small time offset nor a binding floor is
evidence of a whole-calendar-day convention.

Clock support requires agreement between the full-population and uncensored
comparisons. Ambiguity in either survives. If all observations are censored,
the clock verdict is `FLOOR_CENSORED`. If there are fewer than 30 uncensored
rows, clock evidence is insufficient. A finite-grid hour comparison never
identifies an exact expiration clock; early-close scheduling is not inferred.

Cross-capture replication recomputes reports from preserved directories and
their verified ZIPs. Repeated dates or capture identities cannot count as
independent evidence. A dimension replicates only if every capture supports
the same value individually. The common global candidate set is also shown;
its existence alone does not establish a full vendor model.

The original certification, diagnostic, pricing engine and OI policy are left
unchanged. New reports bind to the v2.1.31 diagnostic and source capture hashes.
Missing OI remains unavailable. These reports cannot authorize GEX or orders.

## Command

```powershell
.\.venv\Scripts\python.exe -m src.tools.infer_thetadata_pricing `
  --capture "C:\ThetaDataCaptures\capture-2026-09-02-close" "C:\ThetaDataCaptures\capture-2026-09-02-close.zip" `
  --json "C:\ThetaDataCaptures\pricing-inference-v2.1.32.json"
```

Repeat `--capture DIRECTORY ZIP` to produce a cross-capture validation report.
The output must be new and outside every input directory. Exit 0 means the
report was written; exit 2 means refusal. An ambiguous or inadequate inference
is valid report content and does not become a technical error to hide.

## Findings

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
