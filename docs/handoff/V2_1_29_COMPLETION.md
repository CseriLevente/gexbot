# v2.1.29 completion report

**LIVE EVIDENCE INTEGRATED; NOT READY FOR TRUSTED GEX.**

v2.1.29 is an evidence-only release. It commits the deterministic certification
report derived from the controlled 2026-09-02 ThetaData capture and regression
tests for the v2.1.28 analytical-universe rules. The paid raw payloads, capture
archive and operator transcript remain outside the repository.

## Capture identity

- session: capture-20260902T195700Z-0bccc563691a2ad9
- source commit: 57fffa48c0a736176aeee7b32c9dc0f28798717b
- archive SHA-256: 3a206097ada2fe269c206f3e03bc49c8eceaa5849b85a6ad9f5bdcd318b1612d
- manifest hash: 78ea018613022e546001744be1b71b8d4ec853fc1bc627cc0d1f4222c5f2898d
- certification report hash: 07dc84dd153e61638b0aece24956b8c5765c31c7093c4e9d115ca8474f626d7b
- analytical-universe report hash: 27c3f69510bfe8b8f6aae6191ba8e004be37eb759096055b8f47cbfb36242b06

## What the live capture establishes

All five planned responses were acquired and verified. The contract list, quote
snapshot and first-order Greeks snapshot name the same 13,536 identities with no
duplicates. The v2.1.28 partition is exhaustive and its two independently derived
session dates agree:

| Class | Count |
|---|---:|
| eligible, current | 11,816 |
| eligible, expiring in the captured session | 496 |
| excluded, expired before the captured session | 496 |
| excluded, open interest not reported | 728 |

Open-interest accounting remains explicit: 9,435 positive answers, 3,373 reported
zeroes and 728 identities with no row. An absent row is not rewritten as zero.

## What it does not establish

trusted_for_gex remains False and readiness remains
ADAPTER_CERTIFICATION_EVIDENCE. The capture has two GEX blockers:

1. the vendor effective pricing rate was not identified by this capture; and
2. 728 listed identities have no open-interest record and no evidence-backed
   imputation policy exists.

No certification, compatibility, calculation or trust gate changed in this
release. No production source module or schema version moved.
## Verification

- ruff check: clean
- ruff format --check: 183 files formatted
- mypy: 91 source files, no issues
- pytest with coverage: 2,907 passed
- coverage: 90.62 percent, satisfying the 90 percent gate
- application smoke test: passed
