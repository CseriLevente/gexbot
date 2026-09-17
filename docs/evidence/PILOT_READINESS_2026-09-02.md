# Pilot readiness: session 2026-09-02

Schema `research-pilot-readiness/2.1.35`; report hash `f439a76f42ef336df8d59d3e641f9f692f6cd7fbfd9b54898519c749c4d72bf0`.
Label: research default: close-of-session capture 2026-09-02, receipt clock tolerance 0 ms.

**Verdict: NOT usable for an intraday pilot.** Usable decisions 0, blocked 371 of 371.

Blocking reasons: `spx_observations`, `futures_bid_ask_size`, `futures_instrument_metadata`, `futures_costs`, `intraday_coverage`, `multi_session_coverage`, `NO_PASSING_DECISIONS`.

Partially met (decisions touching the gaps are refused frame by frame): `prior_session_open_interest`.

## Source

- Capture session `capture-20260902T195700Z-0bccc563691a2ad9` (parser `thetadata-v3-parser/2.1.17`, intent `raw-capture-intent/2.1.24`), valuation instant `2026-09-02T19:57:00.201068+00:00`.
- Manifest SHA-256 `78ea018613022e546001744be1b71b8d4ec853fc1bc627cc0d1f4222c5f2898d`; run intent SHA-256 `691da45a2b2bdcd40e1088a7e2582662a9f2141360c45946effdd09c6374a5f8`.
- Normalizer `thetadata-research-events/2.1.36`; events SHA-256 `d8ffd149d1549a9a3e4f798cf7c85f525dc1f8121129d7c958632e7ec9b0b680`; plan SHA-256 `8da14e0b151bc8309d6056a63386f0a8fbd0ceee896a68fbfe5a89fde8fc7aaa`; replay report hash `acee7746e2571a82f05e8620a608620d04beca4b1a56f633e471b2a9632ae841`.
- Origins ['RECORDED_NORMALIZED']; synthetic only: False.

## Requirements

| Requirement | Status | Detail |
| --- | --- | --- |
| option_inventory | PRESENT | 13536 of 13536 native rows usable; received 2026-09-02T19:57:07.185852+00:00 |
| option_quotes | PRESENT | 13282 of 13536 native rows usable; received 2026-09-02T19:57:04.266579+00:00; excluded VENDOR_TIMESTAMP_AFTER_RECEIPT 254 |
| iv_model_inputs | PRESENT | 11707 of 13536 native rows usable; received 2026-09-02T19:57:05.345400+00:00; excluded IV_OUT_OF_RANGE 1829; model thetadata-v3/option/snapshot/greeks/first_order|rate_type=sofr|rate_value=0.042|annual_dividend=0.0|version=latest fixed at 2026-09-02T19:57:00.201068+00:00 |
| prior_session_open_interest | PARTIAL | 12808 of 12808 native rows usable; received 2026-09-02T19:57:04.520135+00:00; 728 of 13536 listed identities have no open-interest row (left unavailable, never zero); rows by as-of 2026-08-31 496, 2026-09-01 12312 |
| spx_observations | MISSING | 1 native rows, none usable (VENDOR_TIMESTAMP_AFTER_RECEIPT 1) |
| futures_bid_ask_size | MISSING | no futures_quote source in this capture; the pinned vendor document describes no futures endpoint |
| futures_instrument_metadata | MISSING | no instrument source in this capture; the pinned vendor document describes no futures endpoint |
| futures_costs | MISSING | no costs source in this capture; the pinned vendor document describes no futures endpoint |
| receive_times | PRESENT | 5 of 5 payloads carry a recorded receipt (5 corroborated by the attempt log) |
| intraday_coverage | MISSING | first receipt 2026-09-02T19:57:03.643476+00:00 is after the last grid decision 2026-09-02T19:45:00+00:00; nothing was available during the research window |
| multi_session_coverage | MISSING | one recorded session (2026-09-02); a pilot needs several |

## Native payloads and receipts

| Endpoint | Kind | Rows | Emitted | Excluded | Available at (UTC) | Evidence | Payload |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `/v3/index/snapshot/price` | spx_price | 1 | 0 | VENDOR_TIMESTAMP_AFTER_RECEIPT 1 | 2026-09-02T19:57:03.643476+00:00 | manifest.response_received_at+attempts.received_at | `4bcdb70970e601ec…` |
| `/v3/option/list/contracts/quote` | contract_list | 13536 | 13536 | - | 2026-09-02T19:57:07.185852+00:00 | manifest.response_received_at+attempts.received_at | `c4ebb21c4ec26b42…` |
| `/v3/option/snapshot/greeks/first_order` | greeks | 13536 | 11707 | IV_OUT_OF_RANGE 1829 | 2026-09-02T19:57:05.345400+00:00 | manifest.response_received_at+attempts.received_at | `0d5eec9a6abfad29…` |
| `/v3/option/snapshot/open_interest` | open_interest | 12808 | 12808 | - | 2026-09-02T19:57:04.520135+00:00 | manifest.response_received_at+attempts.received_at | `ee015548cb6b1239…` |
| `/v3/option/snapshot/quote` | option_quote | 13536 | 13282 | VENDOR_TIMESTAMP_AFTER_RECEIPT 254 | 2026-09-02T19:57:04.266579+00:00 | manifest.response_received_at+attempts.received_at | `a8b8b5acb2dfe410…` |

Vendor timestamps read as America/New_York; rows whose vendor time postdates the receipt: spx_price 1 (max 357 ms), option_quote 254 (max 86 ms).

## Identities and open interest

- Listed 13536; quoted 13282; with usable Greeks 11707; with open interest 12808; without open interest 728 (unavailable, never zero).
- Open-interest as-of rule: open interest settles on the prior trading session (settlement rule applied to the vendor row timestamp's Eastern date); rows by as-of 2026-08-31 496, 2026-09-01 12312.
- Model `thetadata-v3/option/snapshot/greeks/first_order|rate_type=sofr|rate_value=0.042|annual_dividend=0.0|version=latest` fixed at 2026-09-02T19:57:00.201068+00:00; IV source VENDOR_DEFAULT_IV, price basis UNDOCUMENTED_BY_VENDOR.

## Replay

- Contract hash `5512402968f8a1e5e655e9f8d96059d2c4a8c7d17dd96c896a7a0062b6cefc08`; grid 2026-09-02T13:35:00+00:00 → 2026-09-02T19:45:00+00:00 (371 decisions).
- Decisions with an inventory available: 0.
- Blockers by decisions affected: INVENTORY_NOT_AVAILABLE 371.
- Blockers by contract-decisions: INVENTORY_NOT_AVAILABLE 371.
- No grid decision had an inventory and contracts in scope.

## Limitations

- Readiness measures whether recorded inputs satisfy the declared research contract; it is not a strategy, GEX or profitability result.
- Receipts are the capturing process's own clocks, verified as recorded bytes, not independently authenticated time.
- A single close-of-session snapshot cannot reconstruct the session's intraday history; missing history stays missing.
- Exclusion counts describe rows this normalizer refused under versioned rules; they are not vendor error rates.
