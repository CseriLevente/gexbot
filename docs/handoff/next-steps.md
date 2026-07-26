# Next steps

Ordered by dependency, not by appeal. Each step is a thing that can be finished and
verified before the next one starts.

---

## Step 0 — Send one email (blocking, do it today)

Ask ThetaData, in writing, whether the intended use falls under OPRA **Non-Display
Use**: a single-user, own-account automated system that consumes SPX/SPXW option
data and generates futures orders from it, with no redistribution and no display to
third parties.

Why first: it is the only item on the roadmap that can invalidate the project *after*
the engineering is done, and the cost of asking is an email. See
[`data-requirements.md`](data-requirements.md) §5.

**Do not buy any subscription yet.** The engine runs on the synthetic adapter.

---

## Step 1 — Point-in-time replay harness

**Why before real data:** the plan requires bit-identical replay, and it is far
cheaper to build that guarantee into an engine that is already deterministic than
to retrofit it after four services exist. `compute_gex_snapshot` is a pure function
today; keep it that way.

Build:
- `src/replay/recorder.py` — persist raw vendor responses verbatim, timestamped,
  before any parsing. The audit trail is the *raw* payload, not the parsed object.
- `src/replay/player.py` — feed recorded payloads back through the adapters in
  original order.
- `tests/replay/` — same raw input ⇒ identical `GexSnapshot`, asserted on the whole
  object, not on a summary metric.

Do not let anything into the engine that reads a clock, iterates a set, or caches.
Each of those silently breaks replay.

---

## Step 2 — Feature store

The engine already produces the inputs; this step turns them into the plan's named
fields.

Available now from `GexSnapshot`:
`spot_to_zero_gamma_distance_pct`, `spot_to_call_wall_distance_pct`,
`spot_to_put_wall_distance_pct`, `bucket_gex_ratio_0dte_vs_rest`,
`gex_stability_score` (= `zero_gamma_spread_pct`).

Still needs futures bars: `intraday_vwap_distance`, `opening_range_break_state`,
`realized_vol_short`, `realized_vol_medium`, `bar_volume_zscore`,
`futures_basis_proxy`.

Needs Cboe Open-Close: `flow_adjusted_put_call_pressure`.

Point-in-time rule: a feature computed for timestamp `t` may only read data with
timestamp `≤ t`. Write the test that proves it before writing the feature.

---

## Step 3 — Buy ThetaData Standard ($80/mo), wire the transport

Everything is prepared: `src/adapters/thetadata/endpoints.py` has the verified
endpoints and the tier map, `client.py` has the three-way join, and
`tests/unit/test_thetadata_adapter.py` covers both the Standard path (IV, no gamma)
and the Pro path.

To do:
1. Install and run the local **Theta Terminal** (`127.0.0.1:25503`). It is a
   process, not a cloud endpoint — add it to monitoring.
2. Pass a `transport=` callable (`httpx`) into `ThetaDataClient`.
3. Batch full-chain pulls by expiration: Standard allows only **4 concurrent
   requests**.
4. Set `risk_free_rate` and `dividend_yield` on `ChainSnapshot`. SPX carries a
   material dividend yield; leaving `q=0` biases every gamma.

**First real measurement to make**, before building anything on top: run the engine
across a few live sessions and check whether the zero-gamma level is stable enough
to be tradeable. Specifically:

- Distribution of `zero_gamma_spread_pct` — this calibrates
  `max_zero_gamma_shift_pct`, which is currently a sentinel.
- Whether the 0DTE gamma bell is narrower than the strike spacing on expiration
  afternoons (limitation 2 in [`../specs/gex-engine.md`](../specs/gex-engine.md)).
- How often the curve produces more than one crossing.

If the level turns out to be unstable on most days, that is a finding worth having
before writing two strategies that depend on it.

---

## Step 4 — Buy Databento CME Standard ($199/mo), futures ingest

- Dataset `GLBX.MDP3`, schema `ohlcv-1m`, symbol `MES.v.0` with
  `stype_in=continuous` (volume roll — ranks on the *previous* day, so it is
  point-in-time safe).
- Also pull `definition` for contract metadata and `statistics` for settlement/OI.
- Do **not** hard-code the CME calendar roll (Monday before the third Friday). Use
  it as a cross-check against the volume roll and alert on disagreement.

---

## Step 5 — Regime classifier

Only after step 3 has produced enough sessions to calibrate the thresholds. Output
must be one of the six `Regime` enum values; anything else is a bug.

The three sentinel thresholds in `ConfidenceConfig` are the deliverable here, and
each needs out-of-sample evidence — not a value that makes the backtest look good.

---

## Step 6 — Backtester, then strategies, then risk, then paper

Deliberately in that order. Writing a strategy before the point-in-time backtester
exists means the first version of the strategy will be fitted to a look-ahead leak,
and it is very hard to tell afterwards.

Risk engine constraints from the plan, non-negotiable:
- Strategies never send orders to the broker. The risk service is the only caller.
- One open position, one direction, no averaging down, no martingale.
- Mandatory flatten before session end. No overnight.
- Kill switch.

---

## Step 7 — IBKR paper, then supervised live

Use the **TWS API**, not the Web API (50 msg/s vs 10 req/s per username). Bracket
orders: parent entry + child stop + child target, OCO.

Paper fills come from a simulator. Treat a clean paper run as an *operational* gate
— it proves the state machine and the discipline, not the slippage.

Live promotion checklist is in `config/live.yaml` and starts with the OPRA answer
from step 0.

---

## Deferred deliberately

| Item | Why deferred | Revisit when |
|---|---|---|
| `SURFACE_REFIT` IV convention | The plan itself calls it a later phase | The other three conventions disagree materially and it matters which is right |
| numpy vectorisation of the zero-gamma grid | Dependency-free core is worth more during development | The sub-1s SLA actually binds; currently ~1–2 s for a real chain |
| Cboe DataShop Open-Close ($2,499/mo) | Only needed for the flow-adjusted sign model | A specific hypothesis justifies the price, not before |
| Holiday calendar | OI ageing errs toward caution without it | `reference_service` gets built |
| ThetaData **Pro** ($160/mo) | Standard + own pricer is cheaper *and* more consistent | A vendor cross-check on in-house gamma is wanted, or higher-order greeks become relevant |

---

## Two things not to do

**Do not calibrate a threshold to make a backtest look good.** The sentinel design
exists so that an uncalibrated system is visibly uncalibrated. Replacing a sentinel
with a fitted value and no out-of-sample check converts a loud "not ready" into a
quiet "ready", which is the worst possible trade.

**Do not let GEX become a trade trigger.** It is a regime and level model. Every
time a shortcut suggests itself — "just go long when signed GEX flips positive" —
that is the plan's central warning being ignored. The GEX estimate is derived from
public data with a proxy sign convention and T-1 open interest; it is an estimate
wearing an error bar, and the error bar is `confidence`.
