# ThetaData open-interest clarification — 2026-09-06

Status: **VENDOR SUPPORT CLARIFICATION PRESERVED; NO TRUST GATE RELAXED.**

This record joins two different kinds of evidence without pretending they are
the same thing:

1. a private ThetaData support reply establishes the vendor's stated semantics;
2. the frozen 2026-09-02 certification establishes the observed counts and the
   identity of the fully missing expiration.

The composition is emitted as `oi-policy-resolution/2.1.30`. Neither source is
rewritten.

## Private source receipt

The original RFC 5322 `.eml` remains outside Git. Only this receipt is published:

| Field | Value |
|---|---|
| Message date (`Date` header) | `2026-09-06T20:57:48Z` |
| Sender domain | `thetadata.net` |
| Bytes | `31,411` |
| SHA-256 | `29d6678e2fa3e3d1bd2886fb2fcee2ec7bdca73dda65666b05dbc9c0c3ce40fa` |
| Message-ID SHA-256 | `4fe3c3a43a714ea4f0357069e2c1ca9298dc365944f0125d2989ddf8166db550` |

The preserved mailbox headers report DKIM, SPF and DMARC `pass`, and the message
contains a DKIM signature. The extractor records those header results; it does
not cryptographically reverify them or use them as authority. `message_date` is
the normalized sender-authored `Date` header, not mailbox receipt telemetry. The
source body, sender address, recipients and local path never enter the derived
artifact.

The v2.1.30 resolver rereads the raw EML and requires the exact pinned SHA-256,
byte count, message-ID hash, sender domain and message date above. A hand-built
receipt or a different email with similar words is refused.

## Vendor statements, normalized

ThetaData support states that:

- open interest comes from a daily OPRA message rather than a ThetaData
  calculation;
- the message normally arrives around 06:30 ET and represents the previous
  completed trading session's end-of-day OI;
- a missing row is ambiguous: it can reflect zero OI or a message that has not
  arrived, and the response has no field that distinguishes those states;
- a newly listed contract can remain without OI until the next OPRA cycle;
- the recommended treatment is unavailable and excluded, never silently zero;
- contracts are deliberately retained until they are more than one day past
  expiration; and
- a whole expiration being absent is unusual enough that support requested the
  exact case for review.

The approximate delivery time is descriptive. It is not a readiness deadline,
and an absent row after 06:30 ET does not become zero.

## Capture binding

The exact case is derived from the existing report, not from the email:

| Field | Value |
|---|---|
| Capture session | `capture-20260902T195700Z-0bccc563691a2ad9` |
| Market session | `2026-09-02` |
| Symbol | `SPXW` |
| Certification report | `07dc84dd153e61638b0aece24956b8c5765c31c7093c4e9d115ca8474f626d7b` |
| Analytical-universe report | `27c3f69510bfe8b8f6aae6191ba8e004be37eb759096055b8f47cbfb36242b06` |
| Listed / answered / missing | `13,536 / 12,808 / 728` |
| Explicit zero | `3,373` |
| Fully missing expiration | `SPXW 2026-10-23`, `216 / 216` missing |

The resolver recomputes both report hashes and also requires their manifest,
session, universe and OI accounting identities to name the same capture. Two
individually valid reports from different captures cannot be combined.

The email refers only to the “216-contract case.” It does not name `SPXW` or
`2026-10-23` and does not diagnose the cause. The composed report therefore says
`VENDOR_REQUESTED_DETAILS`; it does not say OPRA gap, ingestion failure or
investigation completed.

## Project decision

Numeric imputation is `NONE`. Missing identities remain
`OI_NOT_REPORTED`, are excluded as unavailable, and never reach GEX arithmetic.
That treatment avoids inventing values; it does not repair the missing portion of
the universe. With 728 missing identities, a trusted full-universe OI aggregate
remains blocked. Pricing evidence remains a separate blocker.

Historical `capture-certification/2.1.27`,
`analytical-universe-report/2.1.28` and `longitudinal-oi/2.1.27` artifacts retain
their original wording and hashes. The vendor clarification is an overlay rather
than a retroactive edit.

## Reproduce the sanitized report

From the repository root, with the private email available locally:

```powershell
.\.venv\Scripts\python.exe -m src.tools.resolve_thetadata_oi_policy `
  <path-to-private-email.eml> `
  tests\fixtures\live_capture\third_capture.json `
  --json <output.json>
```

The committed output is
`tests/fixtures/live_capture/oi_policy_resolution_v2_1_30.json`.
