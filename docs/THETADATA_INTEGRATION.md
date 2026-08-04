# ThetaData integration

**Status: technically ready, never run against live data.**

Everything below is implemented and covered by offline tests using stored vendor
response fixtures and a deterministic fake transport. Nothing here has been
executed against a real Theta Terminal or a real subscription.

| Capability | State |
|---|---|
| Endpoint map with per-endpoint tier requirements | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| CSV parsing, schema checks, vendor error detection | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| Per-record parse issues (one bad cell costs one record) | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| Duplicate-row policy (`reject` by default) | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| Multi-response join preserving every source timestamp | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| DST-boundary and fold-aware timestamp parsing | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| Typed `thetadata:` config → single client factory | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Requested / supported / sent / effective parameter split | `IMPLEMENTED` · `TESTED_SYNTHETICALLY` |
| Transport protocol, retries, `Retry-After`, size caps, redaction | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` (deterministic fake) |
| Raw response store (append-only, atomic, collision-safe) | `IMPLEMENTED` · `TESTED_WITH_OFFLINE_FIXTURES` |
| Real HTTP transport (`HttpxTransport`) | `IMPLEMENTED`, **never executed** |
| Chain completeness vs an independent source | `IMPLEMENTED`, reports `PARTIALLY_OBSERVED` — no contract-list endpoint is wired (OD-11) |
| Live vendor response validated | `NOT_VALIDATED_WITH_LIVE_THETADATA` |
| Local gamma compared against vendor gamma | `NOT_VALIDATED_WITH_LIVE_THETADATA` |
| Whether the Standard tier suffices in practice | `NOT_VALIDATED_WITH_LIVE_THETADATA` |

Everything above marked `TESTED_WITH_OFFLINE_FIXTURES` was verified against
recorded, vendor-*shaped* payloads that this repository wrote. They are not
captured vendor responses. **No request in this repository has ever reached
ThetaData.**

---

## Architecture

```
ThetaDataClient
   -> HttpTransport (protocol)
        -> HttpxTransport      real network; needs the `http` extra
        -> RetryingTransport   wraps any transport; retries, backoff, caps
        -> FakeTransport       deterministic, in-memory, used by every test
   -> RawResponseStore (protocol)
        -> FileRawStore / InMemoryRawStore / NullRawStore (default)
```

The transport is a seam, not an implementation detail. It is what lets the retry
semantics be tested with no network and no vendor account.

**The default transport is `UnconfiguredTransport`, which raises.** A silent
fallback to synthetic data would be worse than stopping: a research run that
quietly used made-up numbers is harder to detect than one that failed.

---

## Access model

ThetaData currently routes requests through a **local Theta Terminal** process at
`http://127.0.0.1:25503`, not a cloud endpoint. The Terminal must be running or
every call fails — an operational dependency the monitoring layer has to watch
alongside the feeds themselves.

The client does not assume this is permanent. `base_url` and `auth_mode` are both
configurable:

```yaml
thetadata:
  base_url: http://127.0.0.1:25503
  auth_mode: local_terminal   # or "basic"
  username_env: THETADATA_USERNAME
  password_env: THETADATA_PASSWORD
```

**Credentials come from environment variables and are never in the repository.**
The config holds the *names* of the variables, never values.
`ThetaDataSettings.as_dict()` is serialisable precisely because it contains no
secret, and `tests/unit/test_architecture.py` scans for credential-shaped
literals in both source and config.

---

## Endpoints and tiers

| Endpoint | Returns | Minimum tier |
|---|---|---|
| `/v3/option/snapshot/quote` | bid/ask, sizes, quote timestamp | Value |
| `/v3/option/snapshot/open_interest` | open interest | Value |
| `/v3/option/snapshot/greeks/first_order` | delta, theta, vega, rho, **implied_vol**, underlying price | **Standard** |
| `/v3/option/snapshot/greeks/second_order` | **gamma**, vanna, charm, vomma, veta | **Pro** |
| `/v3/index/snapshot/price` | index spot | Value |

Verified against vendor documentation in July 2026. The tier map is enforced at
runtime: the client raises rather than firing a request its tier cannot serve.

**Gamma is a second-order greek**, so a Standard subscription supplies IV but not
gamma. The engine derives gamma from IV with its own pricer, which is required
for the zero-gamma grid at every tier anyway.

**What this does and does not establish.** It establishes that a Standard
subscription is *sufficient* to run the engine. It does **not** establish that
our gamma equals ThetaData's gamma, or that Standard is "better than" Pro — that
comparison has never been run. See `OPEN_DECISIONS.md` §3.

Standard also allows only **4 concurrent requests**, so a full SPX+SPXW chain
pull must be batched by expiration rather than fanned out.

---

## Explicit calculation parameters

Every request that influences IV or greeks sends its model parameters explicitly:

```yaml
thetadata:
  greeks_version: latest    # consider pinning; "latest" lets the vendor change
                            # historical answers under you
  rate_type: sofr
  rate_value: null          # omitted when null, not sent as an empty string
  annual_dividend: null
  stock_price_source: vendor_default
  use_market_value: null
```

Relying on a vendor default means the vendor can change our numbers without us
changing anything, and the change would be invisible. Unset parameters are
**omitted** rather than sent empty, since the two are not equivalent to every
vendor.

The complete effective parameter set is persisted in snapshot metadata:

```python
snapshot.as_dict()["meta"]  # includes the full thetadata_request block
```

---

## Timestamp preservation

The join keeps **every** source clock separately. Nothing is back-stamped to the
request instant.

```python
quote.timestamps.quote_timestamp        # from the quote response
quote.timestamps.iv_timestamp           # from the greeks response
quote.timestamps.underlying_timestamp   # the underlying print used for greeks
quote.timestamps.open_interest_as_of    # a date -- settlement, not an instant
quote.timestamps.request_started_at
quote.timestamps.response_received_at
quote.timestamps.normalized_at
```

The fixtures deliberately carry a half-second gap between the quote clock and the
underlying clock, and a test asserts that gap survives the join and reaches
`vendor_lag_alert`. If the join collapsed them, the drift that component exists to
measure would silently read as zero.

**Documented assumption:** ThetaData emits wall-clock timestamps without an
offset, and the adapter attaches US Eastern. That is an inference from the venue,
not something the payload states — `OPEN_DECISIONS.md` §2.

---

## Parsing contract

- **Header-driven.** Column order comes from the payload, not from the
  documentation, so a vendor adding a field cannot shift every value by one.
- **Unknown extra columns are carried through**, not rejected.
- **Missing required columns raise** `ThetaDataSchemaError` naming the columns.
  Producing `None` for every contract instead would look like an empty market.
- **Vendor error bodies are detected even with a 200 status** and raise, rather
  than parsing to zero rows.
- **Both expiration formats** (`YYYY-MM-DD` and `YYYYMMDD`) are accepted.
- **Unmodelled roots are rejected**, not coerced — only SPX and SPXW have
  correct settlement clocks here.

Fixtures in `tests/fixtures/vendor/thetadata/` cover quotes, open interest,
first- and second-order greeks, index price, empty responses, vendor errors,
missing columns, unknown extra columns and partial chains.

---

## Transport behaviour

| Concern | Behaviour |
|---|---|
| Timeouts | separate connect and read timeouts |
| Retries | bounded by `max_retries`; never unbounded |
| Retryable | 408, 425, 429, 500, 502, 503, 504, and transport errors |
| Not retryable | other 4xx — a malformed request stays malformed |
| Backoff | exponential, capped, with full jitter |
| Jitter source | injected, so tests stay deterministic |
| Rate limits | a 429 never sleeps below the base backoff |
| Response size | capped at 64 MiB; an unbounded read is a remote-controlled memory-exhaustion path |
| Request IDs | generated per request, attached to every log line |
| Logging | structured; credential-shaped query parameters redacted before anything is written |

---

## Raw response store

The audit trail is the **raw payload**, not the parsed object. A parser bug found
three months later can only be diagnosed against what the vendor actually sent.

- **Append-only.** Re-using a record id raises. Silently replacing a stored
  response would destroy the only copy of the evidence.
- **Content-addressed.** Every record carries a SHA-256 of the payload.
- **Plain files.** `FileRawStore` writes `<id>.raw` plus a JSONL index, readable
  without this codebase.

Each record holds: endpoint, query parameters, request and receipt timestamps,
HTTP status, payload hash, payload location, parser version, vendor schema
version (when available), byte length and request id.

`tests/replay/` proves captured payloads replay to a byte-identical snapshot.

---

## Wiring it up

```python
from src.adapters.thetadata.client import (
    ChainRequest, GreeksParameters, ThetaDataClient, ThetaDataSettings,
)
from src.adapters.transport import HttpxTransport, RetryingTransport, RetryPolicy
from src.adapters.raw_store import CaptureSession, FileRawStore
from src.adapters.thetadata.endpoints import Tier

transport = RetryingTransport(HttpxTransport(), policy=RetryPolicy(max_retries=3))
client = ThetaDataClient(
    settings=ThetaDataSettings(tier=Tier.STANDARD),
    greeks=GreeksParameters(rate_type="sofr", annual_dividend=1.3),
    transport=transport,
    raw_store=FileRawStore("raw_responses/2026-03-17"),
)

chain = client.fetch_chain(
    ChainRequest(symbol="SPXW", max_dte=60),
    as_of=now, spot=spot, open_interest_as_of=prior_session,
    risk_free_rate=0.042, dividend_yield=0.013,
    capture=CaptureSession(store=client.raw_store, session_id="20260317-1100"),
)
```

Install the HTTP extra first: `pip install -e ".[http]"`.

---

## First real run: what to check

1. **Timestamp zone.** Compare one response against a known wall-clock instant.
   This is assumption §2 and it is the cheapest one to falsify.
2. **Gamma agreement.** One Pro day, second-order greeks alongside first-order,
   then `formulas.gamma_comparisons()` sliced by DTE, moneyness, right and IV.
   This is the claim the tier recommendation rests on.
3. **The 0DTE time floor.** Whether the vendor's implied floor is recoverable
   from expiration-afternoon data.
4. **Concurrency.** Whether a full chain fits inside the Standard tier's 4
   concurrent requests at an acceptable latency.
5. **Zero-gamma stability.** Distribution of `zero_gamma_spread_pct` across live
   sessions. If the level is unstable on most days, that is worth knowing before
   anything is built on top of it.


---

## Before a paid session

Read [ADAPTER_CERTIFICATION.md](ADAPTER_CERTIFICATION.md). It lists what must
hold before a capture produces evidence rather than a directory of bytes, and
names the two vendor-dependent unknowns -- the open-interest settlement date and
the spot synchronisation -- that only a live session can resolve.

Construct the session through one path:

```python
from src.config.pipeline import ThetaDataResearchPipeline

pipeline = ThetaDataResearchPipeline.from_config(config.thetadata)
```

Nothing else builds a runtime and a `ModelSpec` separately, because that is how
they came to disagree.


---

## v2.1.3: what the IV source actually tells you

`NBBO_MID_IV` names the *price basis the vendor solved against*. It is not a
local calculation. All four supported IV sources are vendor output, so every
current session runs `VENDOR_IV_LOCAL_GAMMA` and carries real compatibility
requirements.

Build the session from the whole configuration file:

```python
from src.config.pipeline import ThetaDataResearchPipeline

pipeline = ThetaDataResearchPipeline.from_loaded_config(loaded_config)
```

This checks the top-level `model:` block against the `thetadata:` block. v2.1.2
read only the latter, and the repository's own `research.yaml` disagreed with
itself as a result.

### Tier requirements

| Mode | Minimum tier | Why |
|---|---|---|
| `VENDOR_IV_LOCAL_GAMMA` | Standard | `implied_vol` arrives on the first-order greeks endpoint |
| `vendor_gamma_policy: COMPARE_ONLY` | Pro | gamma is a second-order greek. Not a pricing mode -- it sits alongside one, and does not relax the vendor-IV checks. |

`contract_list_endpoint` is `UNCERTAIN` at every tier, since no such endpoint
has been verified. That is why chain completeness stays `PARTIALLY_OBSERVED`.


---

## v2.1.4: running a capture

`config/thetadata_capture.yaml` is the profile that would spend money. It is
committed so the settings can be reviewed line by line before the session, not
reconstructed from a shell history afterwards. **It has never been run.**

A profile with `data.options_source: thetadata` is refused at load time if it
names a synthetic underlying or leaves raw capture off. Both were possible in
v2.1.3: the first computes real vendor gammas against an underlying labelled
invented, the second pays for responses and discards them.

### The command (v2.1.11)

There is one supported way to run it, and it is a dry run unless told otherwise.

```bash
# Resolve the configuration and print what a live run would do. Sends nothing.
python -m src.tools.capture_thetadata_once \
  --config config/thetadata_capture.yaml \
  --output /absolute/path/outside/this/repo/capture-2026-08-04

# The same, actually contacting the vendor.
python -m src.tools.capture_thetadata_once \
  --config config/thetadata_capture.yaml \
  --output /absolute/path/outside/this/repo/capture-2026-08-04 \
  --execute-live
```

The dry run prints the resolved configuration, the pipeline fingerprint, the
capture-plan fingerprint, the required endpoints, the subscription tier, the
raw-store destination, the capture readiness, and the calculation and analytical
blockers. It builds the pipeline with a transport whose every method raises, so
"no request was made" is a property of the object rather than of the control
flow.

The live run opens one capture operation, fetches the index snapshot, the option
quotes, the open interest and the first-order greeks, preserves every response,
writes `manifest.json` and `capture-summary.json`, scans the store for integrity
and verifies the manifest against it. Then it prints the session id, the
operation id, the manifest hash, the record ids, the per-endpoint status, the
parser version and where everything went.

It refuses an output directory inside this repository. v2.1.5 shipped 573
fixture payloads in a release archive because a capture was written into the
namespace the checkout manages.

**It computes no GEX.** Eight load-bearing vendor conventions are unknown, so a
number from these bytes would have no stated meaning -- and comparing those
conventions against the captured responses is what the session is for. The
capture also establishes no open-interest settlement rule, which makes it
permanently raw-only: the rule is chosen when a session opens and there is no
argument through which one can be supplied later (OD-26).

> Earlier drafts of this page described `pipeline.capture_and_compute(...)`,
> removed in v2.1.5, alongside `pipeline.compute_gex(...)`, removed in the same
> release when computing and capturing were separated and the calculation gained
> a gate. The instructions were not updated, so an operator following them got
> an `AttributeError` — which is what the command above replaces.

`ThetaDataRuntime.fetch_chain` no longer accepts `request=`. The request is the
session's, derived once from the configuration: a caller who could substitute
one could fetch a different symbol, DTE window or strike range from the one the
compatibility assessment was made about.

### Migrating a v2.1.3 configuration

`pricing_mode: VENDOR_GAMMA_VALIDATION` is **refused**, with a message naming
its replacement:

```yaml
pricing_mode: VENDOR_IV_LOCAL_GAMMA
vendor_gamma_policy: COMPARE_ONLY
```

It is not translated silently, because the old value *skipped* the vendor-IV
compatibility checks. The same file re-read under v2.1.4 runs them, and may
refuse to compute. That is a change in what the configuration does, so the
operator writes the new form themselves.
