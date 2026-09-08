# Data eligibility: what this repository will compute over

Two rules, introduced in v2.1.28, decide which contracts an analytical dataset
may be built from:

1. a contract with **no open-interest record for the resolved settlement
   session** is ineligible for anything that needs an open-interest weight;
2. a contract whose **`expiration_date < market_session_date`** is ineligible for
   the current analytical universe.

Both come out of longitudinal observation of ThetaData responses. Neither is a
statement about what ThetaData means by anything.

## The distinction this document exists to protect

> **These are eligibility rules for this repository's analytics. They are not
> claims about vendor semantics.**

Saying "a contract with no open-interest record is ineligible" is a decision
about what *we* will compute. Saying "an absent open-interest record means the
open interest is zero" would be a claim about what *the vendor* is asserting by
its silence. The first is ours to make and is written down here. The second is
not established by anything in this repository, is not made anywhere in it, and
would change every aggregate that consumed it.

The same separation applies to expirations. "A contract that expired before this
session is not part of this session's analytical universe" is an eligibility
rule. "ThetaData returns expired contracts in error" would be a claim about the
vendor's intent, and nothing here establishes one — a snapshot endpoint that
serves the last known state of a contract for a day after it expires is a
perfectly defensible design, and it is not the universe an intraday gamma model
runs over.

`OpenInterestTransitionReport.policy_status` continues to carry
`OI_IMPUTATION_POLICY_UNRESOLVED` because that historical comparison contains
only capture observations. v2.1.30 does not rewrite it. Instead, a separate
`oi-policy-resolution/2.1.30` report composes the frozen capture evidence with
the later vendor support clarification.

## v2.1.30 vendor clarification

On 2026-09-06 ThetaData support confirmed that absence is ambiguous: it can
mean zero OI or that the daily OPRA message is not available, and no response
field distinguishes the two. Support recommended exactly the conservative
handling already implemented here: treat the identity as unavailable, exclude
it from aggregation, and do not impute zero. The private email remains outside
Git; its SHA-256 and sanitized claims are bound in
`tests/fixtures/live_capture/oi_policy_resolution_v2_1_30.json`. See
`docs/evidence/THETADATA_OI_CLARIFICATION_2026-09-06.md`.

That resolves the handling policy, not the missing number. Excluding an
unknown prevents invented GEX; it does not make a full-universe aggregate
complete. Every existing trust gate therefore stays closed when any listed
identity lacks OI.

## Finding 1 — an absent open-interest record is not an open interest of zero

Across multiple immutable captures, contracts present in the contract list, the
quote snapshot and the Greeks snapshot but **absent from the open-interest
response** later acquired explicit open-interest records after a settlement
boundary, often with a positive figure.

The strongest consecutive-session observation is **Aug 26 → Aug 27**:

| | |
|---|---|
| Aug 26 | 13,310 list/quote/Greek identities; 12,872 open-interest identities; **438 with no record** |
| Aug 27 | **all 438** of the prior missing identities carried an explicit record |
| of those 438 | **177** became positive open interest |
| | **261** became an explicit zero |
| | **0** remained missing |
| | **0** disappeared |
| Aug 27's own gaps | 302 identities with no record, **all 302 new** relative to Aug 26 |

Earlier captures show the same pattern. v2.1.27's committed transition report
(`tests/fixtures/live_capture/oi_transition_first_to_second.json`) records the
Aug-10 → Aug-12 pair: 426 unanswered identities on the 10th, 422 still listed on
the 12th, **422 of 422** answered by then — 206 explicit zero, 216 positive.

### What follows, and what does not

**Follows.** Absence is a *state that later resolves*, and it resolves to a
positive figure often enough — 177 of 438, 216 of 422 — that zero is not a
conservative reading of it. Open interest is the linear weight on every GEX
term, so a contract weighted zero for want of a number is a contract deleted
from the aggregate. Before v2.1.28 one expression did exactly that:

```python
open_interest = quote.open_interest or 0   # src/gex/formulas.py, until v2.1.28
```

The contract left the aggregate silently, no counter was incremented, and the
completeness measure did not notice — the quote had arrived, so the chain was as
long as it should be.

**Does not follow.** That an absent record means zero. That an absent record
means "too new to have settled". That the contract may be dropped from the
record. The first two are claims about the vendor and this repository has no
evidence for either; the third would make the exclusion unauditable.

### How it is modelled

`src/domain/analytical_universe.py` defines three states, and
`OptionQuote.open_interest_state` derives one for every contract:

| state | `open_interest` | meaning |
|---|---|---|
| `OI_REPORTED_POSITIVE` | `> 0` | the vendor answered, with a position |
| `OI_REPORTED_ZERO` | `0` | the vendor answered, and nobody holds it. **A measurement** |
| `OI_NOT_REPORTED` | `None` | no record exists for the resolved settlement session. **Not a number** |

`None` is preserved end to end — through parsing (`_to_int_recorded` returns
`None` for an absent or blank cell), through the join (`assemble_chain` does not
default), through serialization (`canonical_chain_payload` writes JSON `null`)
and into the calculation, where it produces an exclusion rather than a weight.

## Finding 2 — snapshot endpoints retain contracts past their expiration

Reproduced independently on multiple dates. On **Aug 26**, ThetaData still
returned approximately 500 contracts with an **Aug-25 expiration**; their quote
and Greek market timestamps were from **Aug 25**. On **Aug 27** those Aug-25
contracts were gone, and approximately 500 **Aug-26** contracts were retained
with Aug-26 market timestamps.

### What follows

Successful set equality between the contract list, the quote snapshot and the
Greeks snapshot does **not** establish a valid current analytical universe. The
strongest state this repository awards a capture's universe —
`DEDICATED_CONTRACT_LIST_MATCHED_SNAPSHOT_UNIVERSE` — is precisely what a
capture holding five hundred retained contracts gets, because the three
responses genuinely do name the same identities. They agree; they do not agree
about *this* session.

So, for a regular-session analytical dataset:

```
expiration_date <  market_session_date   ->  ineligible
expiration_date == market_session_date   ->  eligible  (0DTE)
expiration_date >  market_session_date   ->  eligible
```

**The middle line is not an oversight.** A contract that expires today is the
series an intraday gamma model is mostly about, and a rule that removed
same-session expirations would remove the product. A same-session contract that
has already passed its settlement *clock* — an AM-settled SPX series at midday —
is excluded separately, by `ResolutionIssue.EXPIRED`, which measures a different
quantity: the settlement instant, root by root.

`market_session_date` is the market's session, not the calendar day of whatever
zone an instant happens to carry. The two disagree for six hours out of every
twenty-four, and 22:00 ET on the 17th is the 18th in UTC — so reading
`as_of.date()` anywhere would make a contract expiring on the 17th go stale
while the session it expires in was still open. Every derivation goes through
`src.gex.sessions.market_session_date`.

## Where the rules are enforced

| layer | what happens | reason recorded |
|---|---|---|
| `src/domain/analytical_universe.py` | the rules themselves — one implementation, shared by the engine and the capture layer | — |
| `ChainSnapshot.analytical_exclusions()` | counts both findings over the quotes as supplied, before any stage | `AnalyticalExclusion` |
| `src/gex/formulas.py` (`compute_contract_gex`) | a retained contract and an unreported open interest never reach the arithmetic | `ExclusionReason.EXPIRED_BEFORE_SESSION`, `ExclusionReason.OPEN_INTEREST_NOT_REPORTED` |
| `src/domain/normalize.py` (`validate_quote`) | an unreported open interest is a validation error while `require_open_interest` is on | `ValidationCode.MISSING_OPEN_INTEREST` |
| `src/config/pipeline.py` (`calculation_blockers`) | a chain carrying either finding cannot produce a **trusted** GEX | blocker text |
| `src/adapters/thetadata/analytical_universe.py` | one certified capture's listed universe is partitioned, with a set hash per class | `UniverseClass` |

**Excluded is not deleted.** An ineligible contract stays in the chain, stays in
the normalized evidence, stays in the capture's `CaptureUniverse`, and is counted
under its reason. A contract dropped without a counter is a universe nobody can
reconcile against the response the vendor actually sent.

## What this release does not do

- It does not resolve **OD-26** (the open-interest settlement date is still
  caller-supplied and unverified) or **OD-11** (there is still no independent
  contract-universe evidence).
- It does not move `trusted_for_gex`, which is still the constant `False`.
- It does not move `analytical_readiness`, which is still
  `ADAPTER_CERTIFICATION_EVIDENCE`.
- It does not reduce either live capture's open-interest blocker. Checkable
  against the committed fixtures rather than asserted: every expiration in
  either capture's `missing_by_expiration` is **on or after** that capture's
  session — the earliest is `2026-08-10` itself on the first capture and
  `2026-08-18` on the second — so the temporal rule removes none of the
  426 / 416 unanswered identities. That earliest row is worth reading twice: it
  is the first capture's own session, 4 unanswered of 562 listed, and a rule
  written `<=` instead of `<` would have deleted four real 0DTE identities from
  it.
- The capture comparison by itself does not establish a numeric imputation.
  v2.1.30 adds the vendor-confirmed `NONE` / exclude-as-unavailable policy as
  a separate evidence overlay; it does not alter this historical report.

## Reproducing the fixtures

The regression fixtures are three small CSVs derived from the two cases above —
`tests/fixtures/vendor/thetadata/{quotes,greeks_first_order,open_interest}_eligibility.csv`
— holding six identities, one per semantic state. The live capture bodies are
paid vendor data and are not committed; what is committed is the certification
output derived from them, as it has been since v2.1.22.

`tests/synthetic_capture.py` gained `stale_expirations` so a generated capture
can reproduce Finding 2 at all: every row previously went through the pricer,
and a Black-Scholes input with negative time is degenerate, so the generator
silently could not emit the behaviour this release exists to account for.
