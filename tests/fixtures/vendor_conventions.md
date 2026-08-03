# Fabricated vendor conventions, for tests only

**Nothing in this file is a statement about ThetaData.** No live subscription has
been queried, no vendor documentation says any of this, and no comparison has
been run. These are invented answers whose only purpose is to give the tests a
real, referenceable target so they can exercise the production path that turns an
`UNKNOWN` pricing dimension into a resolved one.

The alternative would be for tests to construct a compatibility report that
already says `compatible`, which is how v2.1.3 arrived at a certification result
that no production code path could have produced. A test that fabricates the
*answer* proves nothing about the code that computes it; a test that fabricates
the *evidence* and feeds it through `assess_pricing_compatibility` proves that
the resolution mechanism works and that its refusals are real.

`src/config/compatibility.py` records the `EvidenceSource` alongside every
resolved dimension. Every attestation referencing this file uses
`VENDOR_DOCUMENTATION`, which is a claim. Certification treats only
`LIVE_COMPARISON` as an observation, so nothing here can produce
`ADAPTER_CERTIFIED`.

| Dimension | Invented answer |
| --- | --- |
| `IV_PRICE_BASIS` | the vendor solves against the NBBO midpoint |
| `UNDERLYING_SOURCE` | the vendor uses the index print, not a synthetic forward |
| `UNDERLYING_TIMESTAMP` | the vendor reads the underlying at the option quote instant |
| `EXPIRATION_TIMESTAMP` | the vendor settles PM-expiring roots at 16:00 America/New_York |
| `DAY_COUNT` | the vendor uses ACT/365 fixed |
| `MINIMUM_TIME_FLOOR` | the vendor floors time to expiry at 60 minutes |
| `SOLVER_VERSION` | the vendor exposes no solver version |

## Settlement convention (also fabricated)

| Rule | Invented answer |
| --- | --- |
| open-interest settlement session | the prior trading session, on the US equity/index-option calendar |

Registered by `tests/certification_fixtures.py` as a `DocumentationRule` with
typed `SettlementRule` semantics, so the tests exercise the production path that
*derives* a settlement date from a session date. Since v2.1.9 a documentation
rule cannot supply a date; it can only state a convention that computes one.

`src/adapters/evidence_resolvers.py::DOCUMENTATION_RULES` is empty in
production, and `test_the_production_registry_holds_no_thetadata_settlement_rule`
fails the build if a rule about ThetaData appears there.

When a real comparison is eventually run, its output replaces this file and the
attestations move to `LIVE_COMPARISON`. Until then, every number this repository
produces from vendor IV carries the caveat recorded in
`docs/ADAPTER_CERTIFICATION.md`.
