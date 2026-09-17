# v2.1.31 offline pricing diagnostics

Prepared from the supplied v2.1.30 source archive for original commit
`a3fba0ee91df75ecdaca8406d14c8bce9046cde9`. The development environment imported
that archive into a temporary local Git history for diffs and archive tests;
it does not have the original Windows checkout, GitHub history or remote.
No push, merge, remote tag or Windows-file change is implied by this package.

## Changes

- Adds `src.adapters.thetadata.pricing_diagnostics` and the offline
  `src.tools.diagnose_thetadata_pricing` command.
- Compares 192 explicit rate/day-count/clock/time-rule/timestamp/floor choices
  on one common population, using the existing Black-Scholes delta function.
- Reports OI counts and missing exposure limitations separately from fit.
- Reuses input verification and portable report hashing; refuses mismatched
  archives and output paths inside capture evidence or already in use.
- Adds focused mathematical, integrity, population and CLI regression tests.
- Updates the package version and operator command classification.

Historical capture/policy fixtures and their schemas, calculation engine,
certification inference and trust gates are unchanged. No raw capture or private
email is added to the source package. The new report is a separate deliverable.

## Evidence

Supplied source ZIP SHA-256:
`4b3ffc419a75fde9951a4d75057922ec916955dfab805419be358d93e404283c`.

Supplied capture ZIP SHA-256:
`3a206097ada2fe269c206f3e03bc49c8eceaa5849b85a6ad9f5bdcd318b1612d`.

Reproduced historical certification hash:
`07dc84dd153e61638b0aece24956b8c5765c31c7093c4e9d115ca8474f626d7b`.

Reproduced historical analytical-universe hash:
`27c3f69510bfe8b8f6aae6191ba8e004be37eb759096055b8f47cbfb36242b06`.

New diagnostic semantic hash:
`09abbd59e4728f20f5a34553e439148d4664b24436d56289a86d748c3e3a437f`.

See [PRICING_DIAGNOSTICS.md](../PRICING_DIAGNOSTICS.md) for the reproducible
command, exact population, controlled comparison, and limitations.

## Verification

- Python 3.12.14 on Linux: **2,958 passed, 0 failed, 0 skipped** across all
  85 test modules in four isolated source copies.
- Coverage (lines and branches): **90.39%**, 14,044 statements; the unchanged
  repository-wide 90% threshold passes. Per-module runs defer that aggregate
  threshold until coverage is combined.
- Ruff 0.14.14: lint clean; 189 files format-clean.
- Mypy 1.20.2: 95 source files, no issues.
- Application smoke test: passed.
- All three supplied input hashes remain unchanged. Historical capture and
  policy fixtures and existing numerical/certification source are untouched.
- The diagnostic JSON was reproduced byte-for-byte from the supplied capture.

The interrupted run encountered an environment-only SOCKS dependency error in
an existing client-construction test. It passed when the inherited proxy settings
were removed for that offline test process. The complete resumed run applies
that same process-local setting only to its execution-authority module. No test
was changed or skipped. Test results and coverage were checkpointed per module.
The runtime used compatible dependencies, with their exact installed versions
recorded in the verification evidence; it did not reproduce every lockfile pin.
This is not a Windows/Python 3.13 or GitHub CI verification.


## Next development boundary

1. Test a floor-aware inference method using synthetic floor-binding and
   nonbinding rows and independent already-preserved captures. A floored 0DTE
   delta constrains a time floor; it cannot identify an exact expiration clock.
2. Require an explicit model decision or retain the ambiguity; do not convert
   the lowest diagnostic RMSE into a trusted model label.
3. Define a research universe and missing-data policy, with identity hashes,
   prior-session OI availability and spot alignment. Keep full-universe trust
   separate from any intentionally scoped subset.
4. Build historical intraday replay and compare the same strategy with and
   without GEX using chronological holdouts, fees, spread, slippage and latency.
5. Only evidence of repeatable net performance justifies a paper-execution
   phase. A close capture and successful software tests establish no return.
