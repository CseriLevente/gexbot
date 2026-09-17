# Pilot readiness: session 2026-09-02

Schema `research-pilot-readiness/2.1.35`; report hash `14342c20021f8da7a5c63e18846717534d13b1f306719d2e4324ee9cae0a9b5d`.
Label: DIAGNOSTIC ONLY: close buffer 0 and 1000 ms receipt clock tolerance to exercise the pipeline after the research window; not the research result.

**Verdict: NOT usable for an intraday pilot.** Usable decisions 0, blocked 386 of 386.

Blocking reasons: `futures_bid_ask_size`, `futures_instrument_metadata`, `futures_costs`, `multi_session_coverage`, `NO_PASSING_DECISIONS`, `DIAGNOSTIC_PARAMETERS_NOT_RESEARCH_DEFAULT`.

Partially met (decisions touching the gaps are refused frame by frame): `prior_session_open_interest`, `intraday_coverage`.

> Diagnostic run: parameters differ from the research default (clock tolerance 1000 ms, contract default False). Its counts illustrate the pipeline; they are not the research result.

## Source

- Capture session `capture-20260902T195700Z-0bccc563691a2ad9` (parser `thetadata-v3-parser/2.1.17`, intent `raw-capture-intent/2.1.24`), valuation instant `2026-09-02T19:57:00.201068+00:00`.
- Manifest SHA-256 `78ea018613022e546001744be1b71b8d4ec853fc1bc627cc0d1f4222c5f2898d`; run intent SHA-256 `691da45a2b2bdcd40e1088a7e2582662a9f2141360c45946effdd09c6374a5f8`.
- Normalizer `thetadata-research-events/2.1.36`; events SHA-256 `6a9c8689151ad7421da57afb298413f8787640cade421780ccb84179ce160f92`; plan SHA-256 `14477725c32eddb648527ebe183f78ade20aaf524e820c6014a823a4566e3fd3`; replay report hash `229d14a9341cba5ec4bc89a970ec871be2eed09aff41023041cd88a54ef7567d`.
- Origins ['RECORDED_NORMALIZED']; synthetic only: False.

## Requirements

| Requirement | Status | Detail |
| --- | --- | --- |
| option_inventory | PRESENT | 13536 of 13536 native rows usable; received 2026-09-02T19:57:07.185852+00:00 |
| option_quotes | PRESENT | 13536 of 13536 native rows usable; received 2026-09-02T19:57:04.266579+00:00 |
| iv_model_inputs | PRESENT | 11707 of 13536 native rows usable; received 2026-09-02T19:57:05.345400+00:00; excluded IV_OUT_OF_RANGE 1829; model thetadata-v3/option/snapshot/greeks/first_order|rate_type=sofr|rate_value=0.042|annual_dividend=0.0|version=latest fixed at 2026-09-02T19:57:00.201068+00:00 |
| prior_session_open_interest | PARTIAL | 12808 of 12808 native rows usable; received 2026-09-02T19:57:04.520135+00:00; 728 of 13536 listed identities have no open-interest row (left unavailable, never zero); rows by as-of 2026-08-31 496, 2026-09-01 12312 |
| spx_observations | PRESENT | 1 of 1 native rows usable; received 2026-09-02T19:57:03.643476+00:00 |
| futures_bid_ask_size | MISSING | no futures_quote source in this capture; the pinned vendor document describes no futures endpoint |
| futures_instrument_metadata | MISSING | no instrument source in this capture; the pinned vendor document describes no futures endpoint |
| futures_costs | MISSING | no costs source in this capture; the pinned vendor document describes no futures endpoint |
| receive_times | PRESENT | 5 of 5 payloads carry a recorded receipt (5 corroborated by the attempt log) |
| intraday_coverage | PARTIAL | 3 of 386 grid decisions had an inventory available; first receipt 2026-09-02T19:57:03.643476+00:00 |
| multi_session_coverage | MISSING | one recorded session (2026-09-02); a pilot needs several |

## Native payloads and receipts

| Endpoint | Kind | Rows | Emitted | Excluded | Available at (UTC) | Evidence | Payload |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `/v3/index/snapshot/price` | spx_price | 1 | 1 | - | 2026-09-02T19:57:03.643476+00:00 | manifest.response_received_at+attempts.received_at | `4bcdb70970e601ec…` |
| `/v3/option/list/contracts/quote` | contract_list | 13536 | 13536 | - | 2026-09-02T19:57:07.185852+00:00 | manifest.response_received_at+attempts.received_at | `c4ebb21c4ec26b42…` |
| `/v3/option/snapshot/greeks/first_order` | greeks | 13536 | 11707 | IV_OUT_OF_RANGE 1829 | 2026-09-02T19:57:05.345400+00:00 | manifest.response_received_at+attempts.received_at | `0d5eec9a6abfad29…` |
| `/v3/option/snapshot/open_interest` | open_interest | 12808 | 12808 | - | 2026-09-02T19:57:04.520135+00:00 | manifest.response_received_at+attempts.received_at | `ee015548cb6b1239…` |
| `/v3/option/snapshot/quote` | option_quote | 13536 | 13536 | - | 2026-09-02T19:57:04.266579+00:00 | manifest.response_received_at+attempts.received_at | `a8b8b5acb2dfe410…` |

Vendor timestamps read as America/New_York; rows whose vendor time postdates the receipt: spx_price 1 (max 357 ms), option_quote 254 (max 86 ms).

## Identities and open interest

- Listed 13536; quoted 13536; with usable Greeks 11707; with open interest 12808; without open interest 728 (unavailable, never zero).
- Open-interest as-of rule: open interest settles on the prior trading session (settlement rule applied to the vendor row timestamp's Eastern date); rows by as-of 2026-08-31 496, 2026-09-01 12312.
- Model `thetadata-v3/option/snapshot/greeks/first_order|rate_type=sofr|rate_value=0.042|annual_dividend=0.0|version=latest` fixed at 2026-09-02T19:57:00.201068+00:00; IV source VENDOR_DEFAULT_IV, price basis UNDOCUMENTED_BY_VENDOR.

## Replay

- Contract hash `4274d5b3fcee703a7daf739b6cfb3cedd18df4dc835b5e6b4aad597cf16bb792`; grid 2026-09-02T13:35:00+00:00 → 2026-09-02T20:00:00+00:00 (386 decisions).
- Decisions with an inventory available: 3.
- Blockers by decisions affected: INVENTORY_NOT_AVAILABLE 383, MARKET_INPUTS_MISALIGNED 3, MISSING_GREEKS 3, MISSING_OPEN_INTEREST 3, OI_NOT_PRIOR_COMPLETED_SESSION 3, OI_UNAVAILABLE 3, STALE_GREEKS 3, STALE_OPTION_QUOTE 3, STALE_SPX_PRICE 2.
- Blockers by contract-decisions: INVENTORY_NOT_AVAILABLE 383, MARKET_INPUTS_MISALIGNED 4092, MISSING_GREEKS 1509, MISSING_OPEN_INTEREST 264, OI_NOT_PRIOR_COMPLETED_SESSION 264, OI_UNAVAILABLE 264, STALE_GREEKS 4887, STALE_OPTION_QUOTE 6384, STALE_SPX_PRICE 5068.
- Best decision 2026-09-02T19:58:00+00:00: 1109 passing, 1425 blocked of 2534 in scope (11002 outside the fixed universe); decision hash `e5a0d28f242ee5b17e2aaae76593053c02b3733e72f3e3671a6ba1a713e22f0f`.

## Limitations

- Readiness measures whether recorded inputs satisfy the declared research contract; it is not a strategy, GEX or profitability result.
- Receipts are the capturing process's own clocks, verified as recorded bytes, not independently authenticated time.
- A single close-of-session snapshot cannot reconstruct the session's intraday history; missing history stays missing.
- Exclusion counts describe rows this normalizer refused under versioned rules; they are not vendor error rates.
