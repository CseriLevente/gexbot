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
git archive --format=zip --output=gex-bot-v2.1.11.zip HEAD
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
sha256sum gex-bot-v2.1.11.zip
```

```powershell
# Windows
Get-FileHash gex-bot-v2.1.11.zip -Algorithm SHA256
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
Get-FileHash .\gex-bot-v2.1.11.zip -Algorithm SHA256   # the file being sent
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
