# v2.1.30 completion report

**VENDOR OI POLICY EVIDENCE COMPOSED; TRUSTED GEX REMAINS BLOCKED.**

v2.1.30 preserves the 2026-09-06 ThetaData support reply as a privacy-safe,
content-addressed receipt and composes it with the frozen 2026-09-02 capture
certification. The raw email, paid market-data responses, capture archive and
operator transcript remain outside the repository.

## Evidence identity

- private EML SHA-256:
  `29d6678e2fa3e3d1bd2886fb2fcee2ec7bdca73dda65666b05dbc9c0c3ce40fa`
- support-evidence hash:
  `7aef83fdde0778ccc5081afaad61d0574aa948d582340a0d30a948734a64b828`
- bound capture-certification hash:
  `07dc84dd153e61638b0aece24956b8c5765c31c7093c4e9d115ca8474f626d7b`
- bound analytical-universe hash:
  `27c3f69510bfe8b8f6aae6191ba8e004be37eb759096055b8f47cbfb36242b06`
- OI-policy-resolution hash:
  `339656fc086be98eda8281b0754f848836b5769068c21de620077cbf845ea575`

## What changed

- Added a standard-library EML extractor that emits no body, address, recipient
  or local path.
- Pinned resolution authority to the exact raw EML receipt; mailbox
  authentication-result headers remain informational metadata.
- Added `oi-policy-resolution/2.1.30`, which binds sanitized vendor claims to a
  named certification and analytical-universe report.
- Recomputed both bound report hashes and required their manifest, session,
  universe and OI identities to describe the same capture.
- Derived the fully missing case from the capture as `SPXW 2026-10-23`, with all
  216 listed identities missing OI.
- Recorded the only justified follow-up state: `VENDOR_REQUESTED_DETAILS`.
- Added release guards that refuse tracked or archived `.eml` and `.msg` files.

## Policy outcome

- Missing-row semantics: `AMBIGUOUS_ZERO_OR_NOT_YET_AVAILABLE`.
- Numeric imputation: `NONE`.
- Treatment: `EXCLUDE_AS_UNAVAILABLE`.
- The approximate 06:30 ET delivery time is informational, not a readiness gate.
- The one-day vendor retention window does not weaken temporal eligibility.
- Incomplete OI coverage still refuses a trusted full-universe aggregate.
- No calculation, parser, capture, classification or trading behavior changed.

## Version boundaries

New:

- package `2.1.30`
- `thetadata-support-evidence/2.1.30`
- `oi-policy-resolution/2.1.30`
- `thetadata-oi-policy/1`

Unchanged:

- `capture-certification/2.1.27`
- `analytical-universe-report/2.1.28`
- `analytical-universe/2.1.28`
- `analytical-universe/1`
- `longitudinal-oi/2.1.27`
- `oi-transition/1`
- `thetadata-v3-parser/2.1.17`
- `gex-engine/2.1.10`

## Verification

- Ruff check: clean.
- Ruff format check: all 186 files formatted.
- mypy: 93 source files, no issues.
- pytest with coverage: 2,933 passed in 31m15s.
- coverage: 90.32% across 13,850 statements, satisfying the 90% gate.
- application smoke test: passed; the repository still cannot place an order.
- real-source regeneration: byte-identical 4,663-byte LF-normalized derived
  JSON, SHA-256
  `cf58dc4d4ad67b8d8a4526018b804dbc35002c8d93d1af6e88236cbd244b43fc`.
- private correspondence guard: no tracked `.eml` or `.msg` file.
- diff whitespace check: clean.
