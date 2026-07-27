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

EXPECTED_CONFIDENCE_SCORE = 93.6831
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
EXPECTED_OUTPUT_HASH = (
    "181db88a7a343eda4d874322161e8b236b57faf93db4282f6e383983260d0b16"
)
EXPECTED_CONFIG_FINGERPRINT = "8b5b7454ba7c5500"
EXPECTED_MODEL_FINGERPRINT = "db8d44db4b51d7c4"

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
