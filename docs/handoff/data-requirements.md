# Data requirements: exactly which endpoints, and what they cost

Written to answer one question: **which subscriptions do you actually need to buy?**

Verified against vendor documentation in July 2026. Prices are the public list
prices at that date; treat them as a starting point for a conversation, not a
quote. Every claim below has a source link at the bottom.

---

## TL;DR — the recommendation

| Phase | Buy | Monthly |
|---|---|---|
| **Now (engine + backtester)** | nothing | **$0** |
| **Phase 1 (real GEX research)** | ThetaData **Standard** | **$80** |
| **Phase 2 (futures execution research)** | + Databento CME **Standard** | **+$199** |
| **Phase 3 (paper trading)** | + IBKR paper account | **+$0** |
| **Phase 4 (live)** | + IBKR market data + OPRA clarification | see §5 |
| **Optional (validation)** | Cboe DataShop Open-Close | from **$2,499** |

**Do not buy anything yet.** The engine in this repo runs end-to-end today against
the synthetic adapter. Buy ThetaData Standard when you want the GEX engine pointed
at a real SPX chain, and not before.

---

## 1. The finding that saves $80/month

The implementation plan assumed vendor-supplied greeks. On ThetaData's v3 API that
assumption costs an extra tier, because **gamma is a second-order greek**:

| Endpoint | Returns | Minimum tier | Price |
|---|---|---|---|
| `/v3/option/snapshot/greeks/first_order` | delta, theta, vega, rho, epsilon, lambda, **`implied_vol`**, `underlying_price` | **Standard** | $80/mo |
| `/v3/option/snapshot/greeks/second_order` | **`gamma`**, vanna, charm, vomma, veta, `implied_vol` | **Pro** | $160/mo |
| `/v3/option/snapshot/greeks/all` | all orders in one response | **Pro** | $160/mo |

Note what the Standard tier *does* give you: `implied_vol`. And gamma is a
deterministic function of IV under Black-Scholes — which is the same model
ThetaData uses to produce its own greeks.

**So: take IV from Standard and compute gamma yourself.**

This is not a cost-driven compromise. It is the better engineering choice, for a
reason specific to this system:

> The zero-gamma grid has to reprice gamma at hypothetical spot levels that no
> vendor will ever quote. That code path is mandatory at every tier. If you *also*
> take gamma from the vendor at the current spot, you have two different gamma
> models meeting at exactly the point the root-finder cares about most — the
> current spot. One model everywhere is cleaner.

Implemented as `prefer_vendor_gamma: false` in the configs, and
`src/gex/pricing.py` is the shared model. `tests/unit/test_thetadata_adapter.py`
proves a Standard-tier response (IV, no gamma) still fills every GEX view, and
cross-checks in-house gamma against a genuine Black-Scholes reference so a
day-count or settlement-clock drift cannot pass silently.

**Buy Pro only if** you later want a vendor cross-check on your own gamma, or need
the higher-order greeks (vanna/charm) for research the plan does not currently ask
for.

---

## 2. Options data — ThetaData

**Architectural fact:** ThetaData is *not* a cloud API. Requests go to a local
**Theta Terminal** process at `http://127.0.0.1:25503`. The Terminal must be
running or every call fails. That is an operational dependency the monitoring
layer has to watch, alongside the feeds themselves — budget a VPS for it.

### Endpoints the engine needs

| Data group | Endpoint | Key params | Tier |
|---|---|---|---|
| Option quotes (NBBO) | `/v3/option/snapshot/quote` | `symbol`, `expiration=*`, `strike_range`, `max_dte` | Value |
| Open interest | `/v3/option/snapshot/open_interest` | `symbol`, `expiration=*` | Value |
| IV + first-order greeks | `/v3/option/snapshot/greeks/first_order` | `symbol`, `expiration=*`, `rate_type`, `annual_dividend` | **Standard** |
| Index spot (SPX) | `/v3/index/snapshot/price` | `symbol=SPX` | Value |
| Historical quotes | `/v3/option/history/quote` | + date range | Value |
| Historical OI | `/v3/option/history/open_interest` | + date range | Value |
| Vendor gamma *(optional)* | `/v3/option/snapshot/greeks/second_order` | | Pro |

Encoded in `src/adapters/thetadata/endpoints.py`, with the tier map enforced at
runtime — the client raises rather than firing a request its tier cannot serve.

### Tier comparison

| | Value $40 | **Standard $80** | Pro $160 |
|---|---|---|---|
| Option granularity | 1 minute | **tick** | tick |
| Option history from | 2020-01-01 | **2016-01-01** | 2012-06-01 |
| Concurrent requests | 2 | **4** | 8 |
| NBBO quotes | ✅ | ✅ | ✅ |
| Open interest | ✅ | ✅ | ✅ |
| `implied_vol` | ❌ | **✅** | ✅ |
| `gamma` | ❌ | ❌ | ✅ |
| SPX/VIX real-time index | ✅ | ✅ | ✅ |

**Value is not enough** — no greeks endpoint at all, so no IV, so no gamma by any
route.

**Watch the concurrency limit.** Standard allows 4 concurrent requests. A full
SPX + SPXW chain across all expiries is large, so the ingest service must batch by
expiration rather than fan out. Budget for that in the ingest design; it is the
main reason a naive full-chain pull feels slow.

### Useful parameters

- `expiration=*` returns the whole chain — one request per data group, not one per
  expiry.
- `strike_range=N` returns N strikes either side of spot plus ATM. Cuts payload
  size dramatically and is the right lever for the ±10% band the wall extractor
  uses anyway.
- `max_dte=N` pairs well with the engine's `max_dte_for_grid: 60`.
- `rate_type` / `annual_dividend` feed the vendor's greeks calculation. SPX carries
  a material dividend yield, so if you *do* use vendor greeks, set these — and set
  the same values in `ChainSnapshot.dividend_yield` so the two models agree.

---

## 3. Futures data — Databento

| Item | Value |
|---|---|
| Dataset code | `GLBX.MDP3` (CME Globex MDP 3.0) |
| Schemas | `ohlcv-1m` (bars), `trades`, `tbbo`, `mbp-1` (top of book), `mbp-10` (depth), `definition` (contract metadata), `statistics` (settlement, OI) |
| Continuous symbology | `stype_in=continuous`, symbols like `MES.v.0` |
| Parent symbology | `stype_in=parent`, symbols like `ES.FUT` |
| Endpoint | `GET /v0/timeseries.get_range` |

### Roll rules — pick deliberately

| Suffix | Rule | Use |
|---|---|---|
| `.c.0` | **Calendar** — rolls on expiry dates | Deterministic, but can hold an illiquid contract past the real liquidity shift |
| `.v.0` | **Volume** — highest previous-day volume | **Recommended.** Tracks where the liquidity actually is |
| `.n.0` | **Open interest** — highest previous-day OI | Smoother than volume, slower to shift |

All three rank on the **previous day's** figures, which is what makes them
point-in-time safe: no hindsight about which contract turned out to be the front
month. The plan's `FuturesDataSource.front_contract` protocol exists to keep that
property explicit.

CME publishes the conventional equity-index lead-month roll as the Monday
preceding the third Friday of the contract month. **Do not hard-code that.** Use it
as a sanity check against the volume roll and alert when they disagree — the
research engine must not bake in a calendar assumption the market did not follow.

### Pricing (changed 2026-06-22)

| Plan | Monthly |
|---|---|
| CME Standard | **$199** (was $179; existing subscribers grandfathered at $179 for 12 months) |
| CME Plus | $1,750 |
| CME Unlimited | $4,500 |

Standard is sufficient for MES 1-minute bars and contract metadata. Plus/Unlimited
are about live-feed breadth and historical volume, not about anything the plan
needs at LIVE_STAGE_1.

---

## 4. Broker — IBKR

**Use the TWS API, not the Web API.** The rate limits are not close:

| API | Limit |
|---|---|
| Web API | **10 requests/second** per username; `429` on breach, and violating IPs can be boxed for 10 minutes |
| TWS API | **50 messages/second** |
| FIX (IB Gateway) | 250 messages/second |

A 5-minute decision cycle placing one bracket order does not need 50/s. But the
bracket order plus its child orders, position queries, and reconciliation polling
all share the budget, and a 10/s ceiling is uncomfortably tight for the
reconciliation loop. TWS API also gives cleaner order-state callbacks
(`OnOrderUpdate`-style), which is what the reconciliation service needs.

Bracket orders are natively supported: a parent entry with child stop and
profit-target, children activating only on parent fill, and one child's fill
cancelling the other (OCO). That maps directly onto the plan's requirement that no
position is ever unprotected.

**Paper account caveat.** Paper accounts work over the API and behave much like
live, but fills come from a **simulator**. A clean paper run proves the plumbing,
the state machine, and the operational discipline. It proves **nothing** about
slippage or fill quality. Treat paper results as an operational gate, not a
performance estimate.

**Market data.** IBKR subscriptions are per-username, and API access to market data
requires the same subscriptions as the TWS UI — there is no free API feed. You
generally need a subscription for the underlying *and* the derivative. Given the
10/s Web API ceiling and per-username licensing, **do not plan to source the SPX
option chain from IBKR.** IBKR's job here is execution; ThetaData's job is the
chain.

---

## 5. The licensing risk — read this before going live

**OPRA Non-Display Use.** OPRA's own documentation describes Non-Display Use as
including consumption where data is processed for purposes beyond display, and
lists automated trading, algorithmic order generation, price referencing and
"black box" trading engines among the examples. A bot that consumes OPRA data and
automatically generates orders from it fits that description closely. The OPRA fee
schedule prices non-display use separately from display use.

There are limited exemptions for certain single-user, own-account cases. **Do not
assume one applies.** Get the answer in writing from the specific vendor before
LIVE_STAGE_1, and treat it as a blocking checklist item — it is item 2 on the
promotion checklist in `config/live.yaml`.

Two related points:

- Databento notes that many commercial users of real-time data need a direct venue
  licence or must complete a licensing questionnaire.
- IBKR market data is licensed per username; API usage is not a loophole.

This is the one item on the whole roadmap that can invalidate the project after the
engineering is finished. Resolve it early — the cost of asking is an email.

---

## 6. Optional: Cboe DataShop

Not needed to build anything. Needed for one specific capability the plan calls
for: the **flow-adjusted sign model**.

The naive signed GEX assumes dealers are long call gamma and short put gamma. That
is a proxy, not dealer inventory. Cboe **Open-Close** breaks volume down by
participant type, buy/sell, and open/close at 1-minute or 10-minute aggregation —
enough to build a second, flow-informed sign model and compare the two.

Until that data exists, the engine's `sign_model_agreement` confidence component
scores **zero** and says why. That is deliberate: a single unverified sign model is
exactly the risk the plan warns about, and it should depress confidence rather
than pass unnoticed.

| Product | Public entry price |
|---|---|
| Cboe All Access API | $2,499/mo (higher tier $4,599/mo) |
| Open-Close Volume Summary | Sold separately, selection-based |

Caveat: Open-Close covers **Cboe exchanges only**, and industry-wide OI appears
there as a best-effort, value-add field. It improves the sign model; it does not
make it true.

At this price, treat it as a Phase 5 research purchase justified by a specific
hypothesis, not a Phase 1 dependency.

---

## 7. What each field is actually for

| Field | Consumer | Consequence if missing |
|---|---|---|
| `strike`, `expiry`, `right` | join key everywhere | nothing works |
| `open_interest` | GEX weight | contract dropped, `chain_completeness` falls |
| `implied_vol` | shadow pricer → gamma | contract dropped (unless vendor gamma present) |
| `gamma` | GEX magnitude | falls back to shadow pricer — the normal path |
| `bid` / `ask` | spread, crossed-market detection | `crossed_market_penalty` cannot be measured |
| `underlying_price` | gamma input, distance features | engine cannot run |
| snapshot timestamp | `quote_freshness` | component scores zero |
| spot feed timestamp | `vendor_lag_alert` | component scores zero |
| OI as-of date | `oi_freshness` | component scores zero |
| participant/open-close flow | flow-adjusted sign model | `sign_model_agreement` scores zero |

Note the pattern: a missing field never silently degrades the answer. It either
drops the contract (and shows up in `chain_completeness`) or zeroes a confidence
component. The confidence score is the mechanism that makes data gaps visible
instead of invisible.

---

## Sources

- [ThetaData v3 — Subscriptions](https://docs.thetadata.us/Articles/Getting-Started/Subscriptions.html)
- [ThetaData v3 — option quote snapshot](https://docs.thetadata.us/operations/option_snapshot_quote.html)
- [ThetaData v3 — open interest snapshot](https://docs.thetadata.us/operations/option_snapshot_open_interest.html)
- [ThetaData v3 — first-order greeks](https://docs.thetadata.us/operations/option_snapshot_greeks_first_order.html)
- [ThetaData v3 — second-order greeks](https://docs.thetadata.us/operations/option_snapshot_greeks_second_order.html)
- [ThetaData v3 — all greeks](https://docs.thetadata.us/operations/option_snapshot_greeks_all.html)
- [ThetaData v2→v3 migration guide](https://docs.thetadata.us/Articles/Getting-Started/v2-migration-guide.html)
- [ThetaData pricing](https://www.thetadata.net/pricing)
- [Databento — CME Globex MDP 3.0 dataset](https://databento.com/datasets/GLBX.MDP3)
- [Databento — continuous contracts](https://databento.com/docs/examples/symbology/continuous)
- [Databento — symbology guide](https://databento.com/docs/standards-and-conventions/symbology)
- [Databento — subscription pricing update (2026-06-22)](https://databento.com/blog/updates-to-subscription-pricing)
- [Databento — live data licensing](https://databento.com/docs/portal/live-data)
- [IBKR — Web API documentation](https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/)
- [IBKR — TWS API order limitations](https://interactivebrokers.github.io/tws-api/order_limitations.html)
- [IBKR — placing complex orders (brackets)](https://www.interactivebrokers.com/campus/trading-lessons/python-complex-orders/)
- [IBKR — paper trading account](https://www.interactivebrokers.com/campus/glossary-terms/paper-trading-account/)
- [OPRA — Non-Display Use Declaration](https://cdn.opraplan.com/documents/OPRA_Non_Display_Declaration.pdf)
- [Cboe DataShop — All Access API](https://datashop.cboe.com/cboe-all-access-api)
- [Cboe DataShop — Open-Close Volume Summary](https://datashop.cboe.com/cboe-options-open-close-volume-summary)
- [Cboe — SPX options specifications](https://www.cboe.com/en/tradable-products/sp-500/spx-options/spx-specifications/)
- [CME — Micro E-mini S&P 500 futures](https://www.cmegroup.com/markets/equities/sp/micro-e-mini-sandp-500.html)
- [CME — equity index roll dates](https://www.cmegroup.com/trading/equity-index/rolldates.html)
