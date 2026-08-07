# v2.1.20 completion report

**READY_FOR_RAW_CAPTURE_ONLY.**

The final planned pre-capture release. No ThetaData request has been made by
any release of this repository.

---

## What this release closes

Four gaps, all of the same shape: something was checked, and then something
*else* was used.

### 1. The execution pipeline was not the approved pipeline

v2.1.19 checked the approval against the pipeline built in `_preflight()`, then
built a second one in `run_capture()` to do the work. Nothing compared them.

    preflight pipeline fingerprint:   0872bb7245ec...
    execution pipeline fingerprint:   418fd521b915...
    responses acquired:               all of them

The run ended unverified. That is a report written after the money was spent.

`_require_execution_matches_approval` now re-derives the approval from the
object that will actually send, compares the hash *and* each semantic field,
and returns the approved plan. It runs before `capture_session`, before the run
intent, before the sweep and before any transport call. On a mismatch it names
the first field that moved and sends nothing.

### 2. The sweep derived its own plan after authorization

Three derivations of the same plan — preflight, execution, sweep — identical by
coincidence rather than by construction. The check now returns the plan object
and `capture_required_endpoints_raw` takes it. A supplied plan is sanity-checked
against what the pipeline would derive and refused on a mismatch; it is never
silently replaced.

### 3. Documentation forgery survived construction

v2.1.19 argued that the cheap byte-hash check sufficed because a bundle could
only reach a pipeline through the loader. `ThetaDataResearchPipeline` is a
public frozen dataclass, so that premise was false:

```python
forged = dataclasses.replace(
    genuine,
    documentation_bundle=bundle_with_invented_values,   # real bytes, real digest
    pricing_compatibility=report_recomputed_from_it,
)
forged.validate_integrity()      # passed
```

`PRIOR_TRADING_SESSION` became `SAME_SESSION`, the percent rate became a
decimal, the one-hour floor became thirty minutes. Every one of those moves a
gamma. The document was untouched, so its hash still matched.

`require_documentation_authority()` re-reads the pinned bytes, rehashes,
reparses, rewalks every extraction path, rechecks every fragment, reruns every
normalizer, rebuilds the bundle and compares hashes. It is called at
`capture_session`, `compute_trusted_gex`, `build_verified_calculation_context`,
`assess_analytical_readiness` and `assess_readiness`.

**The cheap check stays where it is.** `validate_integrity` runs before every
fetch, every calculation and every readiness assessment; re-deriving the bundle
costs 326 ms against 0.8 ms, and on a path that authorizes nothing the
difference buys nothing. v2.1.19's error was not the optimisation — it was the
premise the optimisation rested on.

### 4. HTTP routing was inherited from the environment

`httpx.Client` defaults to `trust_env=True`, so `HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY` and `NO_PROXY` were live. The first-session profile targets
`http://127.0.0.1:25503`, and the capture origin is derived from the URL — so a
capture could have recorded itself `LOCAL_TERMINAL_CAPTURE` while the bytes
went through a proxy.

`trust_env=False`, named rather than defaulted, and carried through
`effective_transport_settings`, the dry run, the approval's transport
fingerprint, the run intent and the capture summary. Proxy support, if it is
ever wanted, is configuration an operator approves.

---

## Two smaller corrections

**The plan now describes the executor.** Every planned request printed
`CONTINUE_ON_FAILURE` while the sweep stopped on five systemic conditions — two
hand-written descriptions of one behaviour, and the plan's was the one read
before paying. One `RequestFailurePolicy.CONTINUE_UNLESS_SYSTEMIC`, with the
systemic reasons derived from the executor's own enum so a new one cannot fail
to appear. It is inside the plan hash, which is why `raw-request-plan` moved.

**The report stopped contradicting itself.** `analytical_blockers` held a
static requirements list, so the same report said `settlement_evidence:
ESTABLISHED` and listed the settlement date as a blocker. Now
`analytical_requirements` (standing) and `actual_analytical_blockers` (derived
from this configuration) are separate fields.

---

## Still documentary

The prior-session open-interest rule remains
`AUTHORITATIVE_VENDOR_DOCUMENTATION`, and will remain so after the first
capture. A snapshot carries open-interest values, response timestamps and
contract identities. It carries no settlement-date field — that is OD-26 — so
the bytes look identical whether the documented convention holds or not.

---

## Versions

| Schema | Version | Why it moved |
|---|---|---|
| package | `2.1.20` | |
| `capture-preflight-approval` | `2.1.20` | the transport fingerprint now covers the routing policy |
| `raw-request-plan` | `2.1.20` | the stop policy is inside the plan hash and its value changed |
| `raw-capture-run` | `2.1.20` | the report gained `analytical_requirements` and `actual_analytical_blockers` |

Unchanged, because their semantics are: `gex-engine/2.1.10`, the ThetaData
parser, the documentation extractor. **The numerical GEX outputs are
unchanged**, asserted by the frozen regression case and by
`test_the_frozen_gex_outputs_are_unchanged`.

---

## Verification

| Check | Python 3.12 | Python 3.13 |
|---|---|---|
| `pytest` — 2596 passed | **locally executed** | `unverified` |
| `pytest -m integration` — 18 | **locally executed** | `unverified` |
| `pytest -m regression` — 46 | **locally executed** | `unverified` |
| `pytest -m replay` — 10 | **locally executed** | `unverified` |
| `ruff check .` | **locally executed**, clean | `unverified` |
| `ruff format --check .` — 162 files | **locally executed**, clean | `unverified` |
| `mypy src` — 83 files | **locally executed**, clean | `unverified` |
| `coverage report --fail-under=90` — 90% | **locally executed**, gate satisfied | `unverified` |

Python 3.12.10 in `.venv`.

**Python 3.13 is `unverified`, not "executed in CI".** There is no 3.13
interpreter on this machine and this checkout has no git remote, so the matrix
has never run. The workflow file exists; a workflow that has not run is not a
result.

---

## Before the first paid session

1. Python 3.12 CI green on this commit.
2. Python 3.13 CI green on this commit. Both, not either.
3. Dry run during the session you are about to capture.
4. Read the five planned requests.
5. Copy the `approval_hash` and rerun with `--execute-live --approve <hash>`.

The approval is for one session and stops matching at the next boundary. There
is no flag that skips it.

---

## Scope

Nothing was added outside the release: no futures feeds, no strategy logic, no
feature storage, no backtesting, no regime classification, no risk controls, no
position sizing, no broker integration, no IBKR, no order classes, no paper
trading, no live trading, no calibrated parameters, no new vendor-convention
guesses.

**The repository remains incapable of placing an order.**

---

## Stop condition

No further pre-capture release unless full 3.12/3.13 CI exposes a concrete
defect, or the first real dry run or live session does.
