"""Frozen reference case.

Every expected value below was printed once from a known-good run, read, sanity
checked by hand, and typed in as a literal. **Nothing here recomputes its own
expectation** -- a regression test that regenerates its expected values proves
only that the code equals itself.

When one of these fails, that is the question to answer: *was the change
intended?* If yes, re-derive the numbers deliberately and update them in a commit
that says why. If no, the change is a regression.

Hand checks that make these numbers believable rather than merely recorded:

* ``total_open_interest`` -- 250 contracts, 5 expiries x 50 contracts, each
  expiry carrying an identical OI profile summing to 252,633. 5 x 252,633 =
  1,263,165. Matches.
* Bucket open interest is identical across all five buckets, as it must be when
  every expiry uses the same OI profile.
* Bucket unsigned GEX peaks in ``3_5_DTE`` and falls off in both directions --
  short-dated has high gamma but the 0DTE series has five hours left, while
  long-dated has more time value but flatter gamma.
* The zero-gamma level (~5039) sits above spot (5000), which is what a
  put-heavy book requires.
* ``|signed| < unsigned`` everywhere, since calls and puts partly cancel.
"""

from __future__ import annotations

import pathlib

import pytest

from src.config.schema import load_config
from src.domain.gex import ExpiryBucket, GammaVoidKind, IVConvention
from src.gex.engine import compute_gex_snapshot
from src.synthetic.chains import SyntheticChainSpec, build_synthetic_chain

pytestmark = pytest.mark.regression

CONFIG_DIR = pathlib.Path(__file__).resolve().parents[2] / "config"

# --- Frozen expectations ----------------------------------------------------

EXPECTED_CONTRACT_COUNT = 250
EXPECTED_TOTAL_OPEN_INTEREST = 1_263_165
EXPECTED_TOTAL_UNSIGNED_GEX = 59_228_408_806.90227
EXPECTED_TOTAL_SIGNED_GEX = -24_836_100_698.992706

EXPECTED_BUCKETS = {
    ExpiryBucket.DTE_0: (11_630_172_605.410402, -5_109_197_708.427713, 50, 252_633),
    ExpiryBucket.DTE_1_2: (14_616_068_326.702065, -6_628_795_158.6886835, 50, 252_633),
    ExpiryBucket.DTE_3_5: (15_382_186_815.372179, -6_719_473_525.59469, 50, 252_633),
    ExpiryBucket.DTE_6_30: (11_305_543_441.561419, -4_222_764_224.643679, 50, 252_633),
    ExpiryBucket.DTE_GT_30: (6_294_437_617.856191, -2_155_870_081.6379356, 50, 252_633),
}

EXPECTED_STRIKES = {
    4700.0: (34_227.767829952696, 3_970_421.0682745124, -3_936_193.30044456),
    4725.0: (38_450.66729353078, 16_379_984.267044112, -16_341_533.599750582),
    4750.0: (43_716.611267636836, 57_618_493.65074535, -57_574_777.039477706),
    5300.0: (1_521_893.8339271343, 33_084.648563633345, 1_488_809.1853635008),
}

EXPECTED_LARGEST_CALL_GAMMA_STRIKE = 5025.0
EXPECTED_LARGEST_PUT_GAMMA_STRIKE = 4975.0
EXPECTED_LARGEST_UNSIGNED_GAMMA_STRIKE = 5000.0
EXPECTED_UPSIDE_CALL_WALL = 5025.0
EXPECTED_DOWNSIDE_PUT_WALL = 4975.0
EXPECTED_POSITIVE_NODES = (5075.0, 5050.0, 5025.0)
EXPECTED_NEGATIVE_NODES = (4975.0, 4950.0, 4925.0, 4900.0, 5000.0)
EXPECTED_VOIDS = (
    (4700.0, 4800.0, GammaVoidKind.TRUE_LOW_GEX_VOID),
    (5175.0, 5300.0, GammaVoidKind.TRUE_LOW_GEX_VOID),
)

EXPECTED_ROOTS = {
    IVConvention.STICKY_STRIKE: 5039.133782540731,
    IVConvention.FROZEN_IV: 5039.133782540731,
    IVConvention.STICKY_MONEYNESS: 5039.924571979842,
}
EXPECTED_ZERO_GAMMA_SPREAD_PCT = 0.015815788782219897

# v2.1.2: 93.6831 -> 93.857. Classification: BEHAVIORAL.
#
# A new confidence component, ``effective_model_uniformity`` (weight 0.03),
# joined the weighted mean. On this fixture it scores 1.0 -- the chain is priced
# under a single effective model -- and the previous mean was below 1.0, so
# adding it raises the total.
#
# Every pre-existing component keeps its exact v2.1.1 score, which the
# per-component assertions below still pin. No GEX total, bucket, per-strike
# value, wall, void or zero-gamma root moved; those assertions were unchanged
# and passing before this constant was updated.
EXPECTED_CONFIDENCE_SCORE = 93.857
EXPECTED_COMPONENT_SCORES = {
    "chain_completeness": 1.0,
    "quote_freshness": 1.0,
    "oi_freshness": 1.0,
    "crossed_market_penalty": 1.0,
    "zero_gamma_stability": 0.9367368448711204,
    "sign_model_agreement": 0.0,
    "0dte_dominance_alert": 1.0,
    "vendor_lag_alert": 1.0,
    "multiple_root_penalty": 1.0,
    "root_slope_score": 1.0,
    "root_boundary_penalty": 1.0,
    # v2.1: was 1.0 under count-only comparison. Full topology now applies a
    # proportionate deduction because the conventions' roots, while matched,
    # sit ~0.016% of spot apart. A genuine behaviour change, not a
    # representation one -- see the note on the hash below.
    "root_identity_stability": 0.9841842112177801,
    "timestamp_alignment_score": 1.0,
    "future_timestamp_penalty": 1.0,
    "option_universe_coverage_score": 1.0,
    "iv_spread_quality": 1.0,
    "model_parameter_completeness": 1.0,
}

# --- Fingerprint re-derivations, with the reason for each -------------------
#
# v2.0 -> v2.0.1 (canonical ordering + hash quantisation): representation only.
# v2.1 (this release): re-derived again, for two stated reasons.
#
#   1. ModelSpec gained `configured_underlying_price`, so the payload the model
#      fingerprint hashes has one more key. REPRESENTATION ONLY.
#   2. MODEL_VERSION moved to 2.1.0 because an explicitly configured zero rate or
#      dividend is now honoured instead of falling through to the snapshot value.
#      BEHAVIOUR: the version exists precisely to signal this.
#   3. `output_hash` now covers the confidence component structure and the root
#      topology, which v2 excluded entirely. REPRESENTATION ONLY -- it widens what
#      is hashed without changing any number.
#   4. `root_identity_stability` compares full root topology instead of just
#      counts. BEHAVIOUR: this moved the component from 1.0 to 0.984 and the
#      aggregate score from 93.7428 to 93.6831.
#
# Review performed before touching any constant: all 43 other frozen
# expectations -- totals, five buckets, four strikes, walls, voids, three roots,
# the convention spread and the remaining 16 confidence components -- were
# verified UNCHANGED at rel=1e-12. The regression case configures its rates
# explicitly, so the falsy-fallback fix does not move its numbers. Only the two
# values that the topology change genuinely affects were re-derived, and the hash
# followed them.
# v2.1.2: 181db88a... -> 9f40dfa9...
#
# Two separate changes landed on this digest in one release, and they are
# classified separately because they are different kinds of change:
#
#   1. BEHAVIORAL (181db88a -> 890bf073). A new confidence component,
#      effective_model_uniformity, joined the weighted mean, the engine
#      version moved to gex-engine/2.1.2, and model_distribution /
#      model_completeness metadata was added.
#
#   2. REPRESENTATIONAL (890bf073 -> 9f40dfa9). Deterministic warning
#      *codes* entered the hash payload (per-component ``warning_code`` and
#      the canonicalised snapshot ``warning_codes`` set). No value the
#      engine computes changed; the payload now carries a field it always
#      could have. Free-form prose remains excluded.
#
#   3. REPRESENTATIONAL (9f40dfa9 -> 35def8d5). Per-contract
#      ``selected_timestamp_sources`` metadata was added, recording which
#      vendor record supplied each clock. Again no computed value moved.
#
# v2.1.3 moved it again:
#
#   4. REPRESENTATIONAL (35def8d5 -> 5b43d604). config/research.yaml was
#      made internally coherent, so the config fingerprint this snapshot
#      carries changed. No engine setting and no GEX number moved -- the
#      reference fixture overrides the model spec with the synthetic one,
#      and only the two frozen digests in this file reacted.
#
#   5. 5b43d604 -> 4444055b, from three v2.1.3 changes that landed together:
#      - VERSION_METADATA_ONLY: gex-engine/2.1.2 -> 2.1.3 and
#        thetadata-v3-parser/2.1.1 -> 2.1.3, both of which the hash covers
#        deliberately so a change in the maths or the parser cannot be
#        invisible to a replay.
#      - REPRESENTATIONAL: identity-based ``chain_completeness`` and the
#        ``raw_capture_manifest`` joined the snapshot metadata.
#      - REPRESENTATIONAL: ``zero_gamma_root_identity_stable`` was renamed
#        to ``zero_gamma_root_count_stable``. Same value, honest name.
#
#      No GEX number changed at any step. The totals, buckets, per-strike
#      values, walls, voids, roots and all confidence component scores are
#      asserted individually in this module and held throughout -- across
#      the whole of v2.1.3 exactly three assertions in this file moved, and
#      all three are the digests documented here.
#
# Neither reflects a change to a computed GEX number. The unsigned and
# signed totals, per-bucket and per-strike values, walls, voids and every
# zero-gamma root are asserted individually in this module and were verified
# unchanged at each step: after change (1) exactly three assertions in this
# file had moved (score, model fingerprint, hash), and after change (2)
# exactly one had (hash); the same was true after change (3).
#   6. 4444055b -> 89f38199, from v2.1.4. Classification:
#      VERSION_METADATA_ONLY, and this one was measured rather than
#      assumed. Four v2.1.4 changes could plausibly have reached this
#      digest; three of them provably do not touch it:
#
#      - the engine version, gex-engine/2.1.3 -> 2.1.4, which appears in the
#        payload twice: as ``meta.engine_version`` and inside
#        ``meta.model_fingerprint``. This is the whole of the move.
#      - the parser version, thetadata-v3-parser/2.1.3 -> 2.1.4. Not in this
#        payload at all: the reference case is built from the synthetic
#        chain, which no parser touches.
#      - the canonical contract identity, ``4900.0000`` -> ``4900``. The
#        payload carries identity *counts*, never the strings, so no
#        identity appears in it.
#      - prose stripped from ``meta`` before hashing. This snapshot's meta
#        contains no prose key to strip.
#
#      Each of the three was checked by searching the serialised payload
#      rather than reasoned about. No GEX number moved: every total,
#      bucket, per-strike value, wall, void, root and confidence component
#      is asserted individually in this module, and across the whole of
#      v2.1.4 exactly two assertions in this file changed -- this digest and
#      the model fingerprint, both driven by the same version string.
#   7. 89f38199 -> 568d2c2d, from v2.1.5. Classification:
#      VERSION_METADATA_ONLY, measured the same way as (6). Five v2.1.5
#      changes could plausibly have reached this digest; four provably do
#      not:
#
#      - the engine version, gex-engine/2.1.4 -> 2.1.5, appearing twice as
#        before. This is the whole of the move.
#      - the parser version, thetadata-v3-parser/2.1.4 -> 2.1.5. Absent:
#        the reference case is synthetic and no parser touches it.
#      - ``OptionContract.strike_decimal``, the exact strike carried
#        alongside the float. Absent: the payload has no per-contract
#        strike representation, and for these strikes the canonical
#        spelling is unchanged anyway.
#      - ``calculation_mode`` and ``trusted``, stamped by the two new
#        calculations. Absent: the reference snapshot is computed by
#        ``compute_gex_snapshot`` directly, not through a pipeline.
#      - ``spot_provenance``. Absent for the same reason.
#
#      Each was checked by searching the serialised payload. No GEX number
#      moved: across the whole of v2.1.5 exactly two assertions in this file
#      changed, both driven by the one version string.
#   8. 568d2c2d -> bd668a62, from v2.1.6. Classification:
#      VERSION_METADATA_ONLY, and this time measured directly rather than by
#      elimination: recomputing this reference case with ``model_version``
#      pinned back to ``gex-engine/2.1.5``, and *nothing else* reverted,
#      reproduces 568d2c2d... exactly -- and reproduces d3d45859 for the
#      model fingerprint at the same time. The version string is therefore
#      the whole of both moves, with no residue to attribute elsewhere.
#
#      That check is stronger than the search used for (6) and (7), because
#      it does not depend on knowing which v2.1.6 changes to look for. For
#      the record, none of them appear in this payload anyway: the reference
#      case is built from the synthetic chain and computed by
#      ``compute_gex_snapshot`` directly, so it has no capture manifest, no
#      capture origin, no evidence context, no post-capture compatibility
#      report and no vendor timestamp -- the parser version is absent for
#      the same reason it was in (6) and (7).
#
#      No GEX number moved: every total, bucket, per-strike value, wall,
#      void, root and confidence component is asserted individually below,
#      and across the whole of v2.1.6 exactly two assertions in this file
#      changed -- this digest and the model fingerprint.
#   9. bd668a62 -> 3af3ef9c, from v2.1.7. Classification:
#      VERSION_METADATA_ONLY, measured the same way: pinning ``model_version``
#      back to ``gex-engine/2.1.6`` and reverting nothing else reproduces
#      bd668a62... and faf0a9f5... exactly.
#
#      That measurement matters more here than in (8), because v2.1.7 changed
#      the *clock*: US Eastern moved from a hand-written rule to
#      ``zoneinfo.ZoneInfo("America/New_York")``. Time-to-expiry is measured
#      in that zone and drives gamma, so a change there is exactly the kind
#      that could move a number without anyone intending it.
#
#      It did not, and the reason is structural rather than lucky. The two
#      implementations agree on every instant outside the two DST transition
#      windows; they disagree only inside the repeated autumn hour, which the
#      old zone rendered an hour late. This reference case is an ordinary
#      March session. Every total, bucket, per-strike value, wall, void, root
#      and confidence component below is a hand-typed literal and all of them
#      held.
#  10. 3af3ef9c -> 128acd06, from v2.1.8. Classification:
#      VERSION_METADATA_ONLY *and* REPRESENTATIONAL, which is why this one
#      needs two measurements rather than one.
#
#      Two changes could reach this digest. The engine version moved,
#      gex-engine/2.1.7 -> 2.1.8, as it does every release. And every
#      internal fingerprint widened from sixteen hex characters to the full
#      sixty-four -- the same SHA-256, more of it -- which shows up inside
#      this payload because ``meta.model_fingerprint`` is part of it.
#
#      Measured separately. Pinning ``model_version`` back to 2.1.7 gives
#      a1ff18f0..., not the v2.1.7 digest, so the version string is not the
#      whole of the move. And the model fingerprint under that pin is
#      ``1b353ba18cefb0a2cc8b4afd...``: character for character the v2.1.7
#      value with forty-eight more characters after it. Same input, same
#      hash, longer rendering. The config fingerprint behaves identically --
#      ``ded3172bfee2682f`` -> ``ded3172bfee2682f7986dd9b...``.
#
#      So the two moves are: a version string, and a digest getting longer.
#      Neither is an input to any calculation. Every total, bucket,
#      per-strike value, wall, void, root and confidence component below is
#      a hand-typed literal, and all of them held.
# v2.1.9: 128acd06... -> d0be7199.... Classification: REPRESENTATIONAL.
#
#      Measured, in the narrowest way available. The hash covers the serialised
#      snapshot, which includes the chain-completeness *report*, and v2.1.9 adds
#      one key to that report: ``expected_complete_for_request``. Removing that
#      single key from the payload and re-hashing reproduces
#      ``128acd06a9a00e12d7e19ff60eef55c3635bd7a9920b6a18ac8aa1db3dcb1e04``
#      exactly, so nothing else in the snapshot moved.
#
#      Every numeric literal below is unchanged and all of them still hold:
#      59,228,408,806.90227 unsigned, -24,836,100,698.992706 signed, 93.857
#      confidence, 250 contracts, 1,263,165 open interest, 5039.1337825
#      primary zero-gamma root.
#
#      The new key exists because a partial expected universe -- one page of a
#      paginated listing -- must not report the whole chain complete, and a
#      report that could not say which kind of expectation it measured against
#      could not distinguish the two.
# v2.1.10: d0be7199... -> 0e536883.... Classification: REPRESENTATIONAL.
#
#      Measured the same narrow way. The hash covers the serialised snapshot,
#      which includes the chain-completeness *report*. v2.1.10 replaces one key
#      with four: ``expected_complete_for_request`` -- a caller-supplied Boolean
#      -- gives way to ``coverage_status``, ``universe_artifact_hash``,
#      ``universe_evidence_fingerprint`` and ``resolver_version``, which are
#      what a resolver established.
#
#      Removing those four from the payload and restoring the old key
#      reproduces
#      ``d0be719931de451dd8ef88a178ec8287bec899b93ed605e8f5be4275eedb1961``
#      exactly, so nothing else in the snapshot moved.
#
#      Every numeric literal below is unchanged and all of them still hold:
#      59,228,408,806.90227 unsigned, -24,836,100,698.992706 signed, 93.857
#      confidence, 250 contracts, 1,263,165 open interest, 5039.1337825
#      primary zero-gamma root.
#
#      The four keys exist because a Boolean cannot distinguish "one page of
#      several" from "whatever the vendor happened to send", and -- being an
#      argument -- was answered by whoever was asking.
# v2.1.11: unchanged. Classification: **no change**, and that is the finding.
#
# v2.1.11 changed who may authorize a universe, where a source scope is read
# from, and what recovery compares. None of that is an input to a GEX: the
# reference snapshot is computed from a synthetic chain with no capture, no
# universe and no resolution, so every number and every serialised key is
# identical to v2.1.10. A release that moved this hash would have changed the
# maths while claiming to change the evidence rules.
EXPECTED_OUTPUT_HASH = (
    "0e536883c9927f65032877c94c1c59998c0f94fb4fb3885fa7fb14777e38e307"
)
# v2.1.3: 8b5b7454ba7c5500 -> ded3172bfee2682f. Classification: BEHAVIORAL.
#
# config/research.yaml changed. It shipped `model.iv_price_source:
# NBBO_MID_IV` beside a thetadata section that defaulted to
# VENDOR_DEFAULT_IV -- two different implied volatilities in one file. The
# thetadata section now states iv_source, underlying_price_source, the time
# floor, the expiration rule and the pricing mode explicitly, so
# from_loaded_config can verify the two halves agree.
#
# The fingerprint is *supposed* to move when the configuration does; a
# config change that left it fixed would be the defect.
# v2.1.8: ded3172bfee2682f -> ded3172bfee2682f7986dd9b7b65f2b582d216736da7c795
# c030554ac6b763b9. Classification: REPRESENTATIONAL.
#
# The configuration did not change. The digest is the same SHA-256 rendered in
# full rather than truncated to sixteen characters -- the old value is a literal
# prefix of the new one, which is the check that distinguishes this from a
# content change. Truncation was fine while a fingerprint was a description; it
# stopped being fine once captures started being refused on fingerprint
# inequality.
EXPECTED_CONFIG_FINGERPRINT = (
    "ded3172bfee2682f7986dd9b7b65f2b582d216736da7c795c030554ac6b763b9"
)
# v2.1.11: unchanged. Classification: **no change**. ``MODEL_VERSION`` stays at
# gex-engine/2.1.10 because the numerics did not move: a version bumped because a
# release happened conveys nothing, and this fingerprint exists to say that a
# pricing input changed.
#
# v2.1.10: 6accfab618292203 -> 32b4694cef709838678b5973a9ce8cfcb8ffff90906ebe2d
# 6aef9fdb76ccc0fa. Classification: VERSION_METADATA_ONLY.
#
# The only input that changed is ``model_version``, gex-engine/2.1.9 -> 2.1.10.
# Measured: pinning it back reproduces
# ``6accfab618292203c9af97789874a238786c8884446fe5898a1d845f59a5cc16`` exactly.
# Rate, dividend, IV source, expiration rule, time floor, day count, underlying
# price source and pricing model are all untouched -- v2.1.10 changed what
# establishes an expected universe, not what a gamma is.
#
# v2.1.9: 79f3abe506978342 -> 6accfab618292203c9af97789874a238786c8884446fe589
# 8a1d845f59a5cc16. Classification: VERSION_METADATA_ONLY.
#
# The only input that changed is ``model_version``, gex-engine/2.1.8 -> 2.1.9.
# Measured: pinning it back reproduces
# ``79f3abe506978342c52b31481f16f7ff61ac6f4824b586d4d7020a37a4e73d83`` exactly.
# Rate, dividend, IV source, expiration rule, time floor, day count, underlying
# price source and pricing model are all untouched -- v2.1.9 changed where a
# settlement date and an expected universe come from, not what a gamma is.
#
# v2.1.8: 1b353ba18cefb0a2 -> 79f3abe506978342c52b31481f16f7ff61ac6f4824b5
# 86d4d7020a37a4e73d83. Classification: VERSION_METADATA_ONLY and
# REPRESENTATIONAL, measured apart.
#
# Pin ``model_version`` back to gex-engine/2.1.7 and the fingerprint becomes
# ``1b353ba18cefb0a2cc8b4afd120124b9d17c39be2491998e5da2a738f7173912`` -- the
# v2.1.7 value with forty-eight more characters after it. So the widening is a
# rendering change and the remaining move is the version string. Rate, dividend,
# IV source, expiration rule, time floor, day count, underlying price source and
# pricing model are all untouched.
#
# v2.1.7: faf0a9f595f2a93a -> 1b353ba18cefb0a2.
# Classification: VERSION_METADATA_ONLY.
#
# The only input that changed is ``model_version``, gex-engine/2.1.6 ->
# 2.1.7. Measured: pinning it back reproduces faf0a9f595f2a93a. Rate,
# dividend, IV source, expiration rule, time floor, day count, underlying
# price source and pricing model are all untouched -- v2.1.7 changed what
# a trusted calculation must prove, not what one computes.
#
# v2.1.6: d3d458592b6f87e0 -> faf0a9f595f2a93a.
# Classification: VERSION_METADATA_ONLY.
#
# The only input that changed is ``model_version``, gex-engine/2.1.5 ->
# 2.1.6. Measured: pinning it back reproduces d3d458592b6f87e0. Rate,
# dividend, IV source, expiration rule, time floor, day count, underlying
# price source and pricing model are all untouched -- v2.1.6 changed what
# authorizes a calculation, not what one computes.
#
# v2.1.5: 70b3afda56f505e7 -> d3d458592b6f87e0.
# Classification: VERSION_METADATA_ONLY.
#
# The only input that changed is ``model_version``, gex-engine/2.1.4 ->
# 2.1.5. Rate, dividend, IV source, expiration rule, time floor, day count
# and pricing model are all untouched.
#
# v2.1.4: e05c611b9b953372 -> 70b3afda56f505e7.
# Classification: VERSION_METADATA_ONLY.
#
# The only input that changed is ``model_version``, gex-engine/2.1.3 ->
# 2.1.4. Rate, dividend, IV source, expiration rule, time floor, day count
# and pricing model are all untouched.
#
# v2.1.3: d367d4d4aabbbb69 -> e05c611b9b953372.
# Classification: VERSION_METADATA_ONLY.
#
# The only input that changed is ``model_version``, gex-engine/2.1.2 ->
# 2.1.3. No rate, dividend, IV source, expiration rule, time floor, day
# count or pricing model moved.
#
# v2.1.2: db8d44db4b51d7c4 -> d367d4d4aabbbb69.
# Classification: VERSION_METADATA_ONLY.
#
# The only input that changed is ``model_version``, bumped from gex-engine/2.1.0
# to gex-engine/2.1.2 so that the numerical engine version stops lagging two
# releases behind the code. No rate, dividend, IV source, expiration rule, time
# floor, day count or pricing model changed. The fingerprint is *supposed* to
# move when the engine version does -- an engine change that left it fixed would
# be undetectable in replay, which is the whole point of including it.
EXPECTED_MODEL_FINGERPRINT = (
    "32b4694cef709838678b5973a9ce8cfcb8ffff90906ebe2d6aef9fdb76ccc0fa"
)

# Tight but not exact: the last bit or two of a float sum can differ between
# platforms without anything being wrong. A relative tolerance of 1e-12 still
# catches any change of substance.
REL = 1e-12


@pytest.fixture(scope="module")
def reference():
    spec = SyntheticChainSpec()
    config = load_config(CONFIG_DIR / "research.yaml").engine.with_(
        model_spec=spec.model_spec()
    )
    return compute_gex_snapshot(build_synthetic_chain(spec), config)


# --- Totals -----------------------------------------------------------------


def test_contract_count_and_open_interest(reference):
    assert reference.contract_count == EXPECTED_CONTRACT_COUNT
    assert reference.total_open_interest == EXPECTED_TOTAL_OPEN_INTEREST


def test_unsigned_gex(reference):
    assert reference.total_unsigned_gex == pytest.approx(
        EXPECTED_TOTAL_UNSIGNED_GEX, rel=REL
    )


def test_signed_gex(reference):
    assert reference.total_signed_gex == pytest.approx(
        EXPECTED_TOTAL_SIGNED_GEX, rel=REL
    )


def test_signed_is_dominated_by_unsigned(reference):
    """Hand check, not a recomputation: calls and puts partly cancel."""
    assert abs(reference.total_signed_gex) < reference.total_unsigned_gex


# --- Buckets ----------------------------------------------------------------


@pytest.mark.parametrize("bucket", list(ExpiryBucket))
def test_bucket_values(reference, bucket):
    unsigned, signed, count, open_interest = EXPECTED_BUCKETS[bucket]
    entry = reference.bucket(bucket)
    assert entry is not None
    assert entry.unsigned_gex == pytest.approx(unsigned, rel=REL)
    assert entry.signed_gex == pytest.approx(signed, rel=REL)
    assert entry.contract_count == count
    assert entry.open_interest == open_interest


def test_buckets_sum_to_the_chain_total(reference):
    assert sum(b.unsigned_gex for b in reference.buckets) == pytest.approx(
        EXPECTED_TOTAL_UNSIGNED_GEX, rel=REL
    )


# --- Strikes ----------------------------------------------------------------


@pytest.mark.parametrize("strike", sorted(EXPECTED_STRIKES))
def test_strike_values(reference, strike):
    call, put, signed = EXPECTED_STRIKES[strike]
    entry = next(s for s in reference.strikes if s.strike == strike)
    assert entry.call_gex == pytest.approx(call, rel=REL)
    assert entry.put_gex == pytest.approx(put, rel=REL)
    assert entry.signed_gex == pytest.approx(signed, rel=REL)


def test_strike_count_matches_the_fixture_ladder(reference):
    # +/-6% of 5000 at 25-point steps: 4700..5300 inclusive.
    assert len(reference.strikes) == 25
    assert reference.strikes[0].strike == 4700.0
    assert reference.strikes[-1].strike == 5300.0


# --- Walls ------------------------------------------------------------------


def test_neutral_maxima(reference):
    walls = reference.walls
    assert walls.largest_call_gamma_strike == EXPECTED_LARGEST_CALL_GAMMA_STRIKE
    assert walls.largest_put_gamma_strike == EXPECTED_LARGEST_PUT_GAMMA_STRIKE
    assert walls.largest_unsigned_gamma_strike == (
        EXPECTED_LARGEST_UNSIGNED_GAMMA_STRIKE
    )


def test_directional_walls(reference):
    assert reference.walls.upside_call_wall == EXPECTED_UPSIDE_CALL_WALL
    assert reference.walls.downside_put_wall == EXPECTED_DOWNSIDE_PUT_WALL
    # Hand check: they must straddle spot, or they are not directional walls.
    assert reference.walls.downside_put_wall < reference.spot
    assert reference.walls.upside_call_wall > reference.spot


def test_nodes(reference):
    assert reference.walls.positive_gamma_nodes == EXPECTED_POSITIVE_NODES
    assert reference.walls.negative_gamma_nodes == EXPECTED_NEGATIVE_NODES


def test_voids(reference):
    observed = tuple(
        (v.low_strike, v.high_strike, v.kind) for v in reference.walls.gamma_voids
    )
    assert observed == EXPECTED_VOIDS


# --- Zero gamma -------------------------------------------------------------


@pytest.mark.parametrize("convention", sorted(EXPECTED_ROOTS, key=lambda c: c.value))
def test_zero_gamma_roots(reference, convention):
    result = reference.zero_gamma_for(convention)
    assert result is not None
    assert result.selected_root == pytest.approx(EXPECTED_ROOTS[convention], rel=REL)
    assert result.root_count == 1
    assert not result.root_near_boundary


def test_zero_gamma_spread(reference):
    assert reference.zero_gamma_spread_pct == pytest.approx(
        EXPECTED_ZERO_GAMMA_SPREAD_PCT, rel=REL
    )


def test_zero_gamma_sits_above_spot_on_a_put_heavy_book(reference):
    """Hand check on the fixture's construction, not a recomputation."""
    assert reference.primary_zero_gamma.selected_root > reference.spot


# --- Confidence -------------------------------------------------------------


def test_confidence_score(reference):
    assert reference.confidence.value == pytest.approx(
        EXPECTED_CONFIDENCE_SCORE, rel=1e-9
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_COMPONENT_SCORES))
def test_confidence_component_scores(reference, name):
    component = next(c for c in reference.confidence.components if c.name == name)
    assert component.score == pytest.approx(EXPECTED_COMPONENT_SCORES[name], rel=1e-9)


def test_confidence_remains_uncalibrated(reference):
    """Market thresholds are sentinels in every shipped profile."""
    assert not reference.confidence.calibrated
    assert set(reference.confidence.uncalibrated_components) == {
        "zero_gamma_stability",
        "sign_model_agreement",
        "0dte_dominance_alert",
    }


# --- Fingerprints -----------------------------------------------------------


def test_output_hash_is_frozen(reference):
    """The strongest single assertion here: any numeric change anywhere in the
    snapshot moves this hash.
    """
    assert reference.output_hash() == EXPECTED_OUTPUT_HASH


def test_config_fingerprint_is_frozen(reference):
    assert reference.config_fingerprint == EXPECTED_CONFIG_FINGERPRINT


def test_model_fingerprint_is_frozen(reference):
    assert reference.model_spec.fingerprint() == EXPECTED_MODEL_FINGERPRINT
