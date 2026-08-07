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
| Chain completeness vs an independent source | `IMPLEMENTED`, reports `PARTIALLY_OBSERVED` — the contract-list endpoint is captured as evidence and grants no coverage authority until its scope has been compared against a filtered request (OD-11) |
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

**The checklist. Every line, in order, before spending money.**

1. **Python 3.12 CI green** on the commit you are about to run.
2. **Python 3.13 CI green** on the same commit. Both, not either: the matrix
   exists to catch what one interpreter hides, and a job nobody has watched is
   not a passing job. A workflow file that *would* run them is not a result.
3. **Dry run during the session you are about to capture**, from that commit:

   ```bash
   python -m src.tools.capture_thetadata_once \
       --config config/thetadata_capture.yaml \
       --output /absolute/path/outside/the/repo
   ```

4. **Read the five planned requests.** Symbols, dates, strike range, DTE
   window. This is the only moment they are cheap to be wrong.
5. **Copy the printed `approval_hash`** and rerun with `--execute-live
   --approve <hash>`.

The approval covers the market session date, so it stops matching at the next
session boundary — a Friday approval refuses a Monday run. That is deliberate:
Monday's contract-list request carries Monday's date, so it is a different
request and needs a fresh look. Rerun the dry run; it costs seconds.

There is no flag that skips the approval. If it does not match, the refusal
says what changed and the answer is always a new dry run.

Read [ADAPTER_CERTIFICATION.md](ADAPTER_CERTIFICATION.md). It lists what must
hold before a capture produces evidence rather than a directory of bytes, and
names the two vendor-dependent unknowns -- the open-interest settlement date and
the spot synchronisation -- that only a live session can resolve.

On the first of those: the session opens under a settlement rule read from the
vendor's pinned OpenAPI document, and **the capture cannot confirm it**. No
snapshot endpoint carries a settlement-date field, so the bytes look identical
whether the convention holds or not. The rule stays
`AUTHORITATIVE_VENDOR_DOCUMENTATION` afterwards. See OD-26.

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

**Windows (PowerShell).** The backtick is the line continuation, and the path
needs quoting when it contains a space:

```powershell
py -3.12 -m src.tools.capture_thetadata_once `
  --config config/thetadata_capture.yaml `
  --output "D:\ThetaData\capture-2026-08-05"

py -3.12 -m src.tools.capture_thetadata_once `
  --config config/thetadata_capture.yaml `
  --output "D:\ThetaData\capture-2026-08-05" `
  --execute-live
```

The destination must be a new directory — the command creates it, and refuses
if it is already there. On Windows that includes a drive-qualified path such as
`D:\ThetaData\...`; a bare `\ThetaData\...` is refused as relative, because
which drive it lands on then depends on the shell's current location.

The dry run prints the resolved configuration, the **effective transport
settings**, the expected capture origin, the pipeline fingerprint, the
capture-plan fingerprint, the required endpoints, the subscription tier, the
destinations, the capture readiness, and the calculation and analytical
blockers. It builds the pipeline with a transport whose every method raises, so
"no request was made" is a property of the object rather than of the control
flow — and **it writes nothing**: the store capability is probed in a temporary
directory that is deleted before it returns, so the requested destination does
not exist afterwards unless it existed before.

An invalid destination makes the dry run exit non-zero. Printing a refusal and
exiting 0 is a refusal nobody's script sees.

The live run writes `run-intent.json` *before the first request*, opens one
capture operation, fetches the index snapshot, the option quotes, the open
interest and the first-order greeks, preserves every response **and every
retried attempt**, writes `manifest.json` and `capture-summary.json`, scans the
store for integrity and verifies the manifest against it.

### The effective transport

The live command builds its transport through `build_thetadata_client`, the same
factory every other configured client comes from, so the connect timeout, the
read timeout, the response cap and the authentication in the profile are what
reaches the wire. Until v2.1.12 the command called `HttpxTransport()` with no
arguments — library defaults, while the YAML said otherwise.

Both reports carry the effective settings: base URL with any embedded userinfo
replaced, authentication mode, whether credentials resolved and from which
environment variables, connect timeout, read timeout, maximum response bytes,
retry count and backoff. **No credential value is ever written.**

### Local terminal versus remote vendor

`config/thetadata_capture.yaml` points at `http://127.0.0.1:25503`, a local Theta
Terminal, and the capture is stamped `LOCAL_TERMINAL_CAPTURE`. A direct vendor
URL is stamped `LIVE_HTTP_CAPTURE`. Both are live and they fail differently, and
any later claim about vendor behaviour rests on knowing which produced the bytes.
Until v2.1.12 the origin was read off a class attribute and every capture,
including a local one, was stamped `LIVE_HTTP_CAPTURE`.

### Where a capture may go

The destination is resolved with symlinks followed and refused if it is inside
this repository, a symlink, an existing file, or a directory that already holds
anything. **There is no resume**: give each run its own directory.

The run then **claims** it with `mkdir(exist_ok=False)` — atomically, before any
store, attempt log or intent document exists. v2.1.12 checked that the path was
empty and created the stores afterwards, so two processes could both observe an
empty path, both proceed, and mix their records into one manifest while
overwriting each other's summary. Exactly one `mkdir` wins; the other run is
refused before it sends anything.

### No hidden store

A `raw_capture_path` in the profile names a **fallback** destination for library
callers. It does not cause a store to be created. Until v2.1.13 it did, inside
`build_thetadata_client`, during pipeline construction, for every caller — so
the shipped profile's `artifacts/raw` was created inside the checkout the moment
a pipeline existed, including by the dry run that reports `wrote_files=false`.
The operator constructs exactly one `FileRawStore` under its claimed run root and
passes it as the pipeline's default; the report names that path as
`effective_raw_store_path`, separately from the configured fallback.

v2.1.5 shipped 573 fixture payloads in a release archive because a capture was
written into the namespace the checkout manages; v2.1.11 compared the literal
path, so a symlink pointing at the checkout got through.

Run ids are `capture-<timestamp>-<nonce>`. Record ids derive from the session
id, and two runs started in the same second used to collide.

### What a failure leaves behind

Every exit path writes a manifest and a summary. A vendor 500 on the third
endpoint leaves the first two endpoints' bytes on disk with a **partial**
manifest that identifies itself as partial and cannot pass `verify_capture` —
it is missing endpoints the plan requires, which is the check that should refuse
it. Nothing is deleted automatically.

| File | When |
|---|---|
| `run-intent.json` | before the first request |
| `raw/` | as each response arrives |
| `attempts/` | every HTTP attempt's body, content-addressed |
| `artifacts/` | capture-bound artifacts |
| `manifest.json` | at the end, success or failure |
| `capture-summary.json` | at the end, success or failure |

All three top-level documents are written to a temporary file, fsynced and
renamed, so an interrupted process cannot leave a plausible-looking half-JSON.

### Stored bytes are the vendor's bytes

`raw/<record>.raw` holds the **HTTP entity body after content decoding** — what
the transport read off the socket, decompressed but not decoded — and
`payload_hash` is taken over exactly those bytes. Text is a separate, recorded
reading: the manifest carries the content type, the declared and selected
charsets, whether any byte had to be replaced, and the digest of the decoded
text alongside the digest of the bytes.

v2.1.12 decoded in the transport with `errors="replace"` and the store
re-encoded that string as UTF-8, so one invalid byte became a U+FFFD and the
digest was described as the hash of the vendor's response.

### Retried attempts are preserved

`RetryingTransport` consumes a retryable 429 or 503 body, logs a warning and
tries again. Until v2.1.12 those bodies were dropped — so the responses that
would explain a partial capture were the ones nobody kept, while this page said
every response was preserved. An attempt observer inside the retry loop now
records one entry per attempt (endpoint, attempt number, safe URL, parameter
hash, timings, status, a safe header subset, body hash and location, or a
transport error code where there was no response) and writes the bodies
content-addressed under `attempts/`.

**Attempt bodies are not chain data.** The raw store holds the responses a
snapshot was built from; a preserved 500 is evidence about a failure.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | every endpoint answered and the capture verified |
| 1 | every endpoint answered and verification or integrity did not pass |
| 2 | refused before sending: destination, readiness, unusable profile |
| 3 | configuration error |
| 4 | the `http` extra is not installed |
| 5 | the configured credential environment variables are unset or empty |
| 6 | the vendor could not be reached |
| 7 | reached, and the retry budget was spent |
| 8 | a response did not have the shape this parser reads |
| 9 | the raw store or the artifact store could not do its job |
| 10 | an unexpected internal error; `--debug` prints the traceback |
| 11 | the vendor rejected the credentials — 401 or 403 |
| 12 | a non-2xx the vendor is entitled to send, a 400 most often |
| 13 | rate limited — 429, after the retry budget |
| 14 | the response exceeded the configured cap |
| 15 | evidence that does not follow from what was captured |
| 16 | a validation report that does not hold against its capture |

No secret is printed on any path, and a failure names the summary it wrote.

A vendor 400, 401 or 403 was reported as an internal error until v2.1.13, which
sent an operator to read this code instead of their environment.

### Run states

| State | Means |
|---|---|
| `COMPLETED_VERIFIED` | every planned endpoint answered and the manifest verified |
| `COMPLETED_UNVERIFIED` | every endpoint answered; verification or integrity did not pass |
| `FAILED_PARTIAL` | at least one response arrived or one record was stored, then a failure |
| `FAILED_NO_RESPONSE` | requests were attempted and **nothing answered** |
| `FAILED_BEFORE_REQUEST` | nothing was attempted; no request left this process |

Derived from the attempt log, not from stored records. v2.1.12 reported four
attempts against a Theta Terminal that was not running as
`FAILED_BEFORE_REQUEST`, which is the opposite of the finding.

### When finalization itself fails

Every ordinary controlled failure produces a manifest and a summary. If the
*finalization* is what breaks — a store that cannot be scanned, a disk that
filled between the last response and the summary — the run writes
`capture-summary-emergency.json` instead, carrying the run and session ids, the
state, the typed error, the records known in memory, the attempt count and the
output root, and saying `manifest_written: false`. The HTTP transport is closed
in a `finally` either way.

The attempt index (`attempts/index.jsonl`) is appended and fsynced as each
attempt happens, so the attempt evidence survives a finalization failure or an
interpreter that dies.

**It computes no GEX.** Six load-bearing vendor conventions are unknown, so a
number from these bytes would have no stated meaning -- and comparing those
conventions against the captured responses is what the session is for. The rate
units and the minimum time floor were settled in v2.1.18 from the pinned
OpenAPI document; both rest on `VENDOR_DOCUMENTATION`, which records what the
vendor says rather than what it did.

Since v2.1.18 the capture *does* open under a documented open-interest
settlement rule, derived from the vendor's own description of
`/option/snapshot/open_interest`. That is documentary evidence and stays
classified as such: the rule is chosen when a session opens and there is no
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
