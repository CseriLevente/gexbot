# v2.1.18 completion report

**READY_FOR_RAW_CAPTURE_ONLY.**

Not `ADAPTER_CERTIFIED`, not `READY_FOR_ANALYTICAL_DATASET`. No ThetaData
request has been made by any release of this repository.

---

## What this release is about

v2.1.17 recorded that the exact official ThetaData v3 documentation bytes could
not be obtained, and left its documentation registry empty on that basis. That
conclusion was wrong. The OpenAPI description is a public static file:

    https://docs.thetadata.us/openapiv3.yaml

The pages v2.1.17 checked were under `http-docs.thetadata.us`, where the v3
operation URLs really do return 404. It generalised from that to the whole
documentation host and stopped looking.

The bytes are now in the repository, and the pipeline reads them.

### Network access during construction

Exactly one host was contacted: `docs.thetadata.us`, over HTTPS, twice.

Not contacted: `127.0.0.1`, `localhost`, the Theta Terminal, any authenticated
API, any market-data endpoint. **No paid market data was requested.** Fetching a
public OpenAPI description is not a market-data request, and the code and the
prose both keep the three separate.

The test suite makes no network request at all. `test_the_loader_makes_no_network_request`
proves it by making every socket construction raise.

---

## The pinned document

| Field | Value |
|---|---|
| Source URL | `https://docs.thetadata.us/openapiv3.yaml` |
| Retrieved at | `2026-08-06T14:36:13+00:00` |
| HTTP status | `200` |
| Content type | `application/octet-stream` |
| Byte length | `812792` |
| SHA-256 | `1b65f93c879a5ca4477a0ff9177235138e0c81840e0c7dddfbd9e34164b40b50` |
| Stored at | `vendor_documentation/thetadata/1b/1b65f93c…b40b50.yaml` |
| Declared OpenAPI version | `3.1.0` |
| Bundle fingerprint | `e82371a7cf54644633ba95e6684029923a18bbb54336a6c67d47afd8a8b88a4d` |

The digest is over the **exact response body bytes**. Not a markdown rendering,
not a reserialization of the parsed YAML, not a summary, not copied page text —
a reserialization would pin PyYAML's output formatting and file it under the
vendor's name.

Fetched twice, minutes apart, byte-identical both times. Two fetches do not make
the vendor's document stable; they rule out the failure that would be silent, a
partial read hashing cleanly as a shorter document.

---

## The central change

A genuine document hash cannot support a fabricated statement any more.

v2.1.17's binding carried two free-text fields, `extracted_statement` and
`resolved_value`, both supplied by whoever built the artifact. So the real
SHA-256 of the real document would have carried the sentence "open interest
settles same-session" exactly as happily as the true one. The hash proved which
bytes were held and nothing about what was read out of them.

An extraction is now:

```python
@dataclass(frozen=True)
class OpenApiEvidenceExtraction:
    rule: DocumentedRule
    document_sha256: str
    yaml_path: tuple[str, ...]
    expected_source_fragment: str
    normalized_value: object
    extractor_version: str
    extraction_hash: str
```

A path into the parsed document, plus the fragment that path must contain, plus
a named normalizer that reads the text found there. Nobody passes a sentence in:
there is no argument through which one could be supplied. `extraction_hash` is
recomputed in `__post_init__` and a supplied value that disagrees is refused.

The normalizers genuinely read. `_normalize_settlement` looks for a previous-
session phrase *and* a same-session phrase, refuses if both appear, and refuses
if neither does. A normalizer that returned a constant would make the mechanism
decorative, because a document saying the opposite would produce the same
answer.

### What the three readings produce

| Rule | Path | Value |
|---|---|---|
| `OPEN_INTEREST_SETTLEMENT` | `paths` / `/option/snapshot/open_interest` / `get` / `description` | `SettlementRuleKind.PRIOR_TRADING_SESSION` |
| `RATE_UNITS` | `components` / `parameters` / `rate_value` / `description` | `RateUnit.PERCENT_ANNUAL_RATE` |
| `MINIMUM_TIME_FLOOR` | `components` / `parameters` / `greeks_version` / `description` | `60` minutes |

The open-interest description reads, verbatim:

> Open interest is reported around 06:30 ET every morning by OPRA and reflects
> the open interest at the of the previous trading day.

"at the of the" is the vendor's typo. The expected fragment matches it as
written — matching a corrected version would be matching our own edit.

---

## The mutable global is gone

`PRODUCTION_VENDOR_DOCUMENTATION` was a module-level `dict` any importer could
write to. That is authority with no gate. It was also never read by anything in
the pipeline, so an entry would have changed no behaviour — which made adding
one feel like progress while being none.

`VendorDocumentationBundle` replaces it. Immutable, produced only by
`load_vendor_documentation_bundle`, which on every call rereads the bytes,
recomputes the digest, reparses the YAML, rewalks each path, rechecks each
fragment and reruns each normalizer. `verify_against` re-extracts rather than
comparing the bundle's fields against its own fields, which would pass for any
bundle.

---

## The operator opens a capture under a real settlement rule

v2.1.17's `capture_thetadata_once` passed `settlement_rule=None` unconditionally
and explained that a raw run makes no claims about meaning. But the settlement
convention is not this run's claim to make or withhold — it is something the
vendor documents, and the document is now in the repository. Passing `None`
while holding it would be discarding evidence and calling the result modesty.

The artifact is derived in preflight, before the destination is claimed, and it
goes the long way round: the rule is registered (reopening and rehashing the
document), resolved against the session date, then turned into an artifact that
re-derives its own date in `__post_init__`. Three independent chances to notice
that the answer does not follow from the document.

A run that cannot derive one is refused. `--allow-unsettled-raw-only` collects
the bytes anyway, warns on stderr, and is recorded in the run intent and the
capture summary — the resulting capture can never become a trusted GEX.

### The rule is not backdated, and that has a visible cost

`effective_from` is the retrieval date. The document describes what the vendor
does *now* and carries no statement about when the convention began.

So a capture of an earlier session gets no documentary settlement authority. The
offline fixtures replay a 2026-03-17 session and now pass
`--allow-unsettled-raw-only`, which is the honest reading: a convention read in
August is not evidence about March.

Backdating it would have made those fixtures resolve without the flag. It would
also have been inventing coverage the source does not provide, which is the
shape of the defect this release exists to close.

---

## Percent and decimal are a conversion, not a disagreement

v2.1.17 compared `PERCENT_ANNUAL_RATE` against `DECIMAL_ANNUAL_RATE` as literal
tokens, found them unequal, and reported `RATE_UNITS = MISMATCHED`. But the
question is not "are these the same word".

Six quantities are modelled now — vendor input unit, configured input,
normalization factor, normalized vendor rate, local unit, local rate:

    4.2  x  0.01  =  0.042        →  RATE_UNITS MATCHED, RISK_FREE_RATE MATCHED

And the case that has to be refused is refused. A configuration declaring
`DECIMAL_ANNUAL_RATE` while sending `4.2` to an API documented to read percents
is sending 420%; both dimensions come back `MISMATCHED`, with the detail naming
the number. What made that look fine before is that the local model agreed with
itself.

---

## Eight load-bearing unknowns become six

Settled by the document:

* `RATE_UNITS`
* `MINIMUM_TIME_FLOOR`

Still unresolved, because the document is silent about them and holding a
document does not answer questions it does not address:

* `DAY_COUNT`
* `DIVIDEND_CONVENTION`
* `EXPIRATION_TIMESTAMP`
* `IV_PRICE_BASIS`
* `UNDERLYING_SOURCE`
* `UNDERLYING_TIMESTAMP`

Both settled dimensions rest on `VENDOR_DOCUMENTATION` evidence, which records
what the vendor *says* rather than what it *did*. That distinction has been in
the certification layer since v2.1.5 and still blocks `CALCULATION_VALIDATED`.

A session built with `documentation_bundle=None` keeps all eight. Evidence is
per session, not a property of the process, and `validate_integrity` recomputes
under the session's own documentation so a session with none cannot pass by
borrowing another's.

---

## The five first-session endpoints agree with the document

Checked, not asserted. Tier and CSV response fields, both read out of the pinned
bytes:

| Endpoint | Documented tier | Modelled | Documented CSV fields |
|---|---|---|---|
| `/index/snapshot/price` | `standard` | `STANDARD` | `timestamp, symbol, price` |
| `/option/snapshot/quote` | `value` | `VALUE` | 13 columns |
| `/option/snapshot/open_interest` | `value` | `VALUE` | 6 columns |
| `/option/snapshot/greeks/first_order` | `standard` | `STANDARD` | 17 columns |
| `/option/list/contracts/{request_type}` | `value` | `VALUE` | `symbol, expiration, strike, right` |

No drift. A conflict would refuse the run naming the endpoint and the
difference, because finding out during a paid session is the expensive way.

Two details that would have produced false conflicts:

* the document's paths carry no `/v3` prefix — it is the base path of the
  document's own `servers` entry, so the mapping is read out of the document
  rather than assumed;
* the contract list is templated, `/option/list/contracts/{request_type}`. A
  literal comparison would report an endpoint the document describes perfectly
  well as absent.

`DriftKind` separates `TIER_NOT_DOCUMENTED` from `TIER_CONFLICT`. A silent
document has neither confirmed us nor contradicted us, and collapsing the two
would let silence read as agreement.

---

## What the dry run reports

    capture_readiness                        READY_FOR_RAW_CAPTURE_ONLY
    would_compute_trusted_gex                False
    vendor_documentation
      source_url                             https://docs.thetadata.us/openapiv3.yaml
      document_sha256                        1b65f93c…b40b50
      byte_length                            812792
      bundle_fingerprint                     e82371a7…8b88a4d
      settlement_rule                        PRIOR_TRADING_SESSION
      resolved_…_settlement_date             2026-08-05
      settlement_evidence                    ESTABLISHED
      rate_input_units                       PERCENT_ANNUAL_RATE
      minimum_time_floor_minutes             60
      remaining_documentation_unknowns       6
      endpoint_drift                         []

2026-08-06 is a Thursday; the prior trading session is Wednesday the 5th.

---

## Verification

| Check | Python 3.12 | Python 3.13 |
|---|---|---|
| `pytest` | **locally executed** — 2541 passed | `unverified` |
| `pytest -m integration` | **locally executed** — 18 passed | `unverified` |
| `pytest -m regression` | **locally executed** — 46 passed | `unverified` |
| `pytest -m replay` | **locally executed** — 10 passed | `unverified` |
| `ruff check .` | **locally executed** — clean | `unverified` |
| `ruff format --check .` | **locally executed** — 159 files formatted | `unverified` |
| `mypy src` | **locally executed** — clean, 82 source files | `unverified` |
| `coverage run -m pytest` | **locally executed** | `unverified` |
| `coverage report --fail-under=90` | **locally executed** — 90%, gate satisfied | `unverified` |

Python 3.12.10 in `.venv`.

**Python 3.13 is `unverified`, not "executed in CI".** There is no 3.13
interpreter on this machine and this checkout has no git remote, so the CI
matrix has never run. Reporting it as CI-green would be reporting a job nobody
has watched.

### The coverage gate failed first

At 89% against the configured floor of 90. The new module was the cause — 81%,
with the gap almost entirely in the refusal branches.

That is the worst place to have one. A refusal nobody tests is a refusal that
might not fire, and each of these exists to stop a bad document from settling a
convention that weights every strike. 44 tests in
`tests/unit/test_openapi_evidence_refusals.py` close it.

90% is the floor rather than a comfortable margin. The largest remaining gap is
error handling in `capture_thetadata_once.py` around a live transport that no
test can reach without a vendor.

### Two contaminations caught during verification

* `_commit_msg.txt` was swept into two commits by `git add -A` and from there
  into the first archive. Harmless in content, wrong in principle: a build
  somebody downloads should not carry the note explaining how it was built, and
  the same sweep would take a `_patch.py` holding whatever was being debugged.
  Root-level `/_*.py`, `/_*.txt` and `/_*.md` are now ignored, and
  `test_no_scratch_file_reaches_a_release` checks both the tracked file list and
  the archive. The existing release-integrity tests covered caches, `artifacts/`
  and credential-shaped strings; none of them looked at the repository root.

* The archive was rebuilt after both fixes, so its SHA-256 differs from the one
  computed before them.

---

## Release

    git status --porcelain      # empty
    git archive --format=zip --output=gex-bot-v2.1.18.zip HEAD

| | |
|---|---|
| File | `gex-bot-v2.1.18.zip` |
| SHA-256 | *recomputed after this commit — see the completion message* |
| Entries | 263 |
| Files | 218 |
| Commit | this commit |

Verified inside the archive: the pinned document is present, 812,792 bytes, and
still hashes to `1b65f93c…b40b50`. No scratch file is present.

---

## Scope

Nothing was added outside the release: no futures data, no strategy logic, no
feature storage, no backtesting, no regime classification, no risk controls, no
broker integration, no order classes, no paper trading, no live trading, no
calibrated trading parameters.

**The repository remains incapable of placing an order.**

---

## What happens next

Run the first raw-only ThetaData session during a valid US options-market
session, on Python 3.12, with the shipped capture profile.

**Corrected in v2.1.19.** This section previously said the session "turns the
vendor's documented claims into observations — including whether the open
interest it returns actually belongs to the session the document says it does".
The second half is wrong and worth being precise about.

A first capture observes open-interest *values*, response timestamps and
contract identities. It contains no vendor settlement-date field — no ThetaData
snapshot endpoint has one, which is OD-26. So nothing in the captured bytes can
confirm or contradict the claim that open interest reflects the previous
trading day; the numbers look the same either way.

The prior-session rule therefore stays classified as
`AUTHORITATIVE_VENDOR_DOCUMENTATION` after the capture, exactly as before it.
Upgrading it to `LIVE_COMPARISON` because a capture exists would be recording
that we watched the vendor do something we did not watch. Confirming it needs
an independent method — comparing successive sessions' figures against a
separate source, or a vendor field that does not exist yet.

What the session *does* settle: whether the five endpoints answer, what they
actually return, whether the documented CSV columns are the real ones, and
whether the contract listing's scope matches a filtered request.
