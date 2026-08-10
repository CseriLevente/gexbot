# Release procedure

Status: `IMPLEMENTED` â€” the archive step, the integrity checks, and the CI job
that runs them all exist and pass. `NOT_VALIDATED_WITH_LIVE_THETADATA` â€”
no release has been cut against a live ThetaData subscription.

This repository is a **research engine**. A release publishes analysis code and
its test evidence. It does not publish anything that can trade, and the release
checklist below includes a step that verifies that.

---

## 1. Bootstrap a clean environment

The engine core (`src/gex`, `src/domain`, `src/synthetic`) is stdlib-only. You
need dependencies only for the config loader, the real HTTP transport, and the
dev tooling.

**Windows (PowerShell):**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

**Unix (bash):**

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e ".[dev]"
```

To reproduce a known-good environment exactly rather than re-resolving, install
the lockfile first:

```bash
python -m pip install -r requirements-lock.txt
python -m pip install -e . --no-deps
```

`requirements-lock.txt` pins what a known-good environment resolved to;
`pyproject.toml` holds the compatible ranges. Regenerate the lock deliberately,
in a commit that says why:

```bash
python -m pip freeze --exclude-editable | sort > requirements-lock.txt
```

Both ends of every range are bounded â€” including `build-system.requires` â€” so a
future upstream release cannot change the produced artefact without a commit
here. `tests/unit/test_release_integrity.py` enforces this.

---

## 2. Verify

Run all five. Every one must pass before a release is cut.

**Windows (PowerShell):**

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m pytest --cov --cov-report=term-missing
.\.venv\Scripts\python.exe -m src.app
```

**Unix (bash):**

```bash
./.venv/bin/python -m ruff check .
./.venv/bin/python -m ruff format --check .
./.venv/bin/python -m mypy src
./.venv/bin/python -m pytest --cov --cov-report=term-missing
./.venv/bin/python -m src.app
```

No test performs a network call. The ThetaData tests run against a deterministic
fake transport, and `FakeTransport` raises rather than reaching out when a route
is unregistered.

### The two checks that matter most

```bash
python -m pytest tests/unit/test_architecture.py -v     # the repository cannot trade
python -m pytest tests/unit/test_release_integrity.py -v # the release is reproducible
```

---

## 3. Confirm the tree is clean

`git archive` exports the **commit**, not the working tree. An uncommitted edit
is silently absent from the artefact, so the archive must only ever be produced
from a clean tree:

```bash
git status --porcelain
```

This must print **nothing**. If it prints anything at all, commit or discard it
first and re-run the verification in step 2.

---

## 4. Produce the archive

```bash
git archive --format=zip --output=gex-bot-v2.1.17.zip HEAD
```

The archive is:

- **content-complete** â€” `pyproject.toml`, `requirements-lock.txt`, `src/`,
  `tests/`, `docs/`, `config/`;
- **noise-free** â€” no `.venv/`, no `__pycache__/`, no `.pytest_cache/`;
- **reproducible** â€” two archives of the same commit are byte-identical, because
  `git archive` derives its timestamps from the commit rather than the clock;
- **credential-free** â€” no tracked file contains anything shaped like a secret,
  and no `.env`, `.pem` or `.key` is tracked.

Record the digest alongside the artefact so it can be verified later:

```bash
# Unix
sha256sum gex-bot-v2.1.17.zip
```

```powershell
# Windows
Get-FileHash gex-bot-v2.1.17.zip -Algorithm SHA256
```

### The digest must describe the file that was uploaded

`git archive` output goes out **as-is**. Do not re-zip it, do not place it in a
development directory and archive that, and do not let a transfer tool wrap it.

The reason is narrow and it matters: a wrapper is a different file with a
different digest, so the SHA-256 in the release notes would describe something
nobody downloaded. A recipient checking the hash of what they actually received
would get a mismatch and have no way to tell an innocent re-wrap from a
substituted artefact. Verify the digest against the uploaded file, after upload:

```powershell
Get-FileHash .\gex-bot-v2.1.17.zip -Algorithm SHA256   # the file being sent
```

---

## 5. Post-release checklist

- [ ] `git status --porcelain` was empty at the archived commit
- [ ] all five verification commands passed
- [ ] coverage â‰Ą 90%
- [ ] `docs/CHANGELOG.md` describes the release
- [ ] `docs/OPEN_DECISIONS.md` lists every unresolved ambiguity
- [ ] no documentation claims live-vendor validation that has not happened
- [ ] the archive digest is recorded
- [ ] `docs/ADAPTER_CERTIFICATION.md` reflects what is actually blocking
- [ ] the archive was extracted to a temporary directory and the smoke tests below passed

### Post-extraction smoke test

An archive that cannot be used is not a release. From an extraction of the zip:

```bash
# Core import + engine smoke test
python -c "import src.gex.engine, src.domain.contracts, src.synthetic.chains"

# Configuration smoke test
python -c "from src.config.schema import load_config; print(load_config('config/research.yaml').thetadata.tier)"

# Demo
python -m src.app

# Release integrity
python -m pytest tests/unit/test_release_integrity.py -q

# Integration fixtures
python -m pytest -m integration -q

# Adapter-certification readiness
python -m pytest tests/unit/test_adapter_certification.py tests/unit/test_certification_states.py -q
```

---

## What a release does **not** contain

There is no broker adapter, no order type, no position sizing, no execution
path, and no strategy. `tests/unit/test_architecture.py` fails the build if one
appears. See [MODEL_ASSUMPTIONS.md](MODEL_ASSUMPTIONS.md) for the boundary of
what the numbers in this repository mean.

---

## Before the first raw ThetaData session

This is the checklist for the capture, not for the release. Every line is a
thing that has gone wrong somewhere, and the session costs money.

- [ ] remote CI green on **both** Python 3.12 and 3.13 for the released commit
- [ ] Theta Terminal installed and running, and reachable at the configured
      `base_url`
- [ ] subscription tier confirmed to be `standard` or better, against the
      account rather than against `config/thetadata_capture.yaml`
- [ ] licensing and data-use terms confirmed for storing raw responses
- [ ] output destination **new and outside this repository** — the command
      creates it and refuses a path that already exists, a symlink, or one
      that resolves inside the checkout
- [ ] sufficient disk space for **five responses plus retry bodies** — the
      dry run prints the arithmetic under `disk`

**What this session actually captures.** Five requests, not a full chain:

| # | Endpoint | Symbol |
|---|---|---|
| 1 | `/v3/index/snapshot/price` | `SPX` — the underlying index |
| 2 | `/v3/option/snapshot/quote` | `SPXW` |
| 3 | `/v3/option/snapshot/open_interest` | `SPXW` |
| 4 | `/v3/option/snapshot/greeks/first_order` | `SPXW` |
| 5 | `/v3/option/list/contracts/quote` | `SPXW` — evidence only |

**The standard `SPX` option root is not captured**, and this checklist said
"a full SPX+SPXW chain" until v2.1.21, which overstated both the scope and the
disk budget. The index request takes `SPX` because SPXW options are written on
that index; every option request takes the option root. The purpose of the
first paid session is to validate the existing SPXW adapter path, not to
collect a dataset.

- [ ] dry run completed successfully and its report reviewed line by line:

```bash
python -m src.tools.capture_thetadata_once \
  --config config/thetadata_capture.yaml \
  --output /absolute/path/outside/this/repo/capture-YYYY-MM-DD
```

```powershell
py -3.12 -m src.tools.capture_thetadata_once `
  --config config/thetadata_capture.yaml `
  --output "D:\ThetaData\capture-YYYY-MM-DD"
```

Check in that output: `capture_readiness` is `READY_FOR_RAW_CAPTURE_ONLY`,
`expected_capture_origin` is what you expect for your `base_url`,
`effective_transport` shows the timeouts and cap you configured, and
`destination_refusals` is empty.

Then add `--execute-live`.

Afterwards the run leaves `run-intent.json`, `raw/`, `attempts/`, `artifacts/`,
`manifest.json` and `capture-summary.json`. Exit 0 means every planned endpoint
answered and the manifest verified against the store; every other code is
documented in `docs/THETADATA_INTEGRATION.md`.

**No GEX is computed from that capture**, and none should be trusted until the
vendor conventions in `docs/ADAPTER_CERTIFICATION.md` have been compared against
the captured bytes.

---

## After the session: certify the capture

```bash
python -m src.tools.certify_thetadata_capture /absolute/path/to/capture \
    --archive-sha256 <digest of the archive you distributed> \
    --json certification.json
```

Offline. It re-verifies every payload against the manifest before computing
anything, then derives the rate semantics, the day count, the expiration clock,
the implied-volatility basis, the underlying the Greeks were computed against,
universe coverage and open-interest coverage — each as a table of scored
hypotheses rather than a verdict.

| exit | meaning |
|---|---|
| 0 | certified; nothing contradicts the documentation |
| 2 | the capture could not be read or verified |
| 3 | certified, **and** a documentation/live conflict is present |

Exit 3 is not a failure. It is the state the first capture is in.

Two runs over an untouched capture produce the same `report_hash`. A run over an
edited one does not, and a run over a capture whose payloads no longer match
their manifest hashes refuses before computing anything.

### What the first capture established

See `docs/ADAPTER_CERTIFICATION.md`. The headline: `rate_value` is consumed as a
**decimal**, not the percent the OpenAPI document describes, so the first
session was priced at 420%. That capture is
`ADAPTER_CERTIFICATION_EVIDENCE` / `NOT_TRUSTED_FOR_GEX` and must not be
discarded — it is the evidence.

The corrected profile sends `rate_value: 0.042`. The dry run prints the economic
rate, the local model rate, the wire value, both units and the conflict; check
that block before the second capture.
