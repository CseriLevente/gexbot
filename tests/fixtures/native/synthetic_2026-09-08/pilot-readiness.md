# Pilot readiness: session 2026-09-08

Schema `research-pilot-readiness/2.1.35`; report hash `2a67899f375bea6c8c5a8ed5e2008f5f1fe2bfe62b917ef76870a9faa148031b`.
Label: (none).

**Verdict: NOT usable for an intraday pilot.** Usable decisions 1, blocked 370 of 371.

Blocking reasons: `futures_bid_ask_size`, `futures_instrument_metadata`, `futures_costs`, `multi_session_coverage`.

Partially met (decisions touching the gaps are refused frame by frame): `prior_session_open_interest`, `intraday_coverage`.

## Source

- Capture session `capture-synthetic` (parser `thetadata-v3-parser/2.1.17`, intent `raw-capture-intent/2.1.24`), valuation instant `2026-09-08T13:40:10+00:00`.
- Manifest SHA-256 `4444426dd432976d026ad2742df27ea8f5b5a9cd84bacda42a0146a6ffbfa3fb`; run intent SHA-256 `67cfc7f9413b2ae226cced1e3ba30624ae8f9d10faddfec2eae881e02d751b5c`.
- Normalizer `thetadata-research-events/2.1.36`; events SHA-256 `6bffebc571040172a9023b99d65a0b013131b557de21c1c3c9a6d4ec88f08aa3`; plan SHA-256 `47c198afa8051b45e9558ea474938c732d9fc02738bbbef1600dce6227b47fb4`; replay report hash `7e289ef457e62c7fde86e8eb956b000dbd048c819daa0e63588664cb27ee16bf`.
- Origins ['SYNTHETIC']; synthetic only: True.

## Requirements

| Requirement | Status | Detail |
| --- | --- | --- |
| option_inventory | PRESENT | 19 of 19 native rows usable; received 2026-09-08T13:40:13+00:00 |
| option_quotes | PRESENT | 14 of 22 native rows usable; received 2026-09-08T13:40:11+00:00; excluded CONFLICTING_DUPLICATE_OBSERVATIONS 2, IDENTICAL_DUPLICATE_COALESCED 1, NOT_IN_INVENTORY 1, UNEXPECTED_SYMBOL 1, UNPARSEABLE_TIMESTAMP 1, VENDOR_TIMESTAMP_AFTER_RECEIPT 1, ZERO_OR_INVALID_ASK 1 |
| iv_model_inputs | PRESENT | 8 of 16 native rows usable; received 2026-09-08T13:40:12+00:00; excluded CONFLICTING_DUPLICATE_OBSERVATIONS 2, DELTA_OUT_OF_RANGE 1, IV_OUT_OF_RANGE 3, NON_FINITE_INPUT 1, VENDOR_IV_ERROR 1; model thetadata-v3/option/snapshot/greeks/first_order|rate_type=sofr|rate_value=0.042|annual_dividend=0.0|version=latest fixed at 2026-09-08T13:40:10+00:00 |
| prior_session_open_interest | PARTIAL | 11 of 14 native rows usable; received 2026-09-08T13:40:11.300000+00:00; excluded IDENTICAL_DUPLICATE_COALESCED 1, INVALID_OPEN_INTEREST 1, OI_TIMESTAMP_NOT_TRADING_SESSION 1; 8 of 19 listed identities have no open-interest row (left unavailable, never zero); rows by as-of 2026-09-03 3, 2026-09-04 8 |
| spx_observations | PRESENT | 1 of 1 native rows usable; received 2026-09-08T13:40:10.500000+00:00 |
| futures_bid_ask_size | MISSING | no futures_quote source in this capture; the pinned vendor document describes no futures endpoint |
| futures_instrument_metadata | MISSING | no instrument source in this capture; the pinned vendor document describes no futures endpoint |
| futures_costs | MISSING | no costs source in this capture; the pinned vendor document describes no futures endpoint |
| receive_times | PRESENT | 5 of 5 payloads carry a recorded receipt (5 corroborated by the attempt log) |
| intraday_coverage | PARTIAL | 365 of 371 grid decisions had an inventory available; first receipt 2026-09-08T13:40:10.500000+00:00 |
| multi_session_coverage | MISSING | one recorded session (2026-09-08); a pilot needs several |

## Native payloads and receipts

| Endpoint | Kind | Rows | Emitted | Excluded | Available at (UTC) | Evidence | Payload |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `/v3/index/snapshot/price` | spx_price | 1 | 1 | - | 2026-09-08T13:40:10.500000+00:00 | manifest.response_received_at+attempts.received_at | `ed37143a3cf00f70…` |
| `/v3/option/list/contracts/quote` | contract_list | 19 | 19 | - | 2026-09-08T13:40:13+00:00 | manifest.response_received_at+attempts.received_at | `a35e18eda310050f…` |
| `/v3/option/snapshot/greeks/first_order` | greeks | 16 | 8 | CONFLICTING_DUPLICATE_OBSERVATIONS 2, DELTA_OUT_OF_RANGE 1, IV_OUT_OF_RANGE 3, NON_FINITE_INPUT 1, VENDOR_IV_ERROR 1 | 2026-09-08T13:40:12+00:00 | manifest.response_received_at+attempts.received_at | `e37b502fbcc3636b…` |
| `/v3/option/snapshot/open_interest` | open_interest | 14 | 11 | IDENTICAL_DUPLICATE_COALESCED 1, INVALID_OPEN_INTEREST 1, OI_TIMESTAMP_NOT_TRADING_SESSION 1 | 2026-09-08T13:40:11.300000+00:00 | manifest.response_received_at+attempts.received_at | `673fa5dbd6100ae9…` |
| `/v3/option/snapshot/quote` | option_quote | 22 | 14 | CONFLICTING_DUPLICATE_OBSERVATIONS 2, IDENTICAL_DUPLICATE_COALESCED 1, NOT_IN_INVENTORY 1, UNEXPECTED_SYMBOL 1, UNPARSEABLE_TIMESTAMP 1, VENDOR_TIMESTAMP_AFTER_RECEIPT 1, ZERO_OR_INVALID_ASK 1 | 2026-09-08T13:40:11+00:00 | manifest.response_received_at+attempts.received_at | `016dd65744ab5aee…` |

Vendor timestamps read as America/New_York; rows whose vendor time postdates the receipt: option_quote 1 (max 200 ms).

## Identities and open interest

- Listed 19; quoted 14; with usable Greeks 8; with open interest 11; without open interest 8 (unavailable, never zero).
- Open-interest as-of rule: open interest settles on the prior trading session (settlement rule applied to the vendor row timestamp's Eastern date); rows by as-of 2026-09-03 3, 2026-09-04 8.
- Model `thetadata-v3/option/snapshot/greeks/first_order|rate_type=sofr|rate_value=0.042|annual_dividend=0.0|version=latest` fixed at 2026-09-08T13:40:10+00:00; IV source VENDOR_DEFAULT_IV, price basis UNDOCUMENTED_BY_VENDOR.

## Replay

- Contract hash `5512402968f8a1e5e655e9f8d96059d2c4a8c7d17dd96c896a7a0062b6cefc08`; grid 2026-09-08T13:35:00+00:00 → 2026-09-08T19:45:00+00:00 (371 decisions).
- Decisions with an inventory available: 365.
- Blockers by decisions affected: INVENTORY_NOT_AVAILABLE 6, STALE_GREEKS 364, STALE_OPTION_QUOTE 364, STALE_SPX_PRICE 364.
- Blockers by contract-decisions: INVENTORY_NOT_AVAILABLE 6, STALE_GREEKS 2912, STALE_OPTION_QUOTE 2912, STALE_SPX_PRICE 2912.
- Best decision 2026-09-08T13:41:00+00:00: 8 passing, 0 blocked of 8 in scope (11 outside the fixed universe); decision hash `76806b0c5cb8cbe9e9fd2cec56a6504de48e1447243f5381ce0f5940b9e12544`.

## Limitations

- Readiness measures whether recorded inputs satisfy the declared research contract; it is not a strategy, GEX or profitability result.
- Receipts are the capturing process's own clocks, verified as recorded bytes, not independently authenticated time.
- A single close-of-session snapshot cannot reconstruct the session's intraday history; missing history stays missing.
- Exclusion counts describe rows this normalizer refused under versioned rules; they are not vendor error rates.
