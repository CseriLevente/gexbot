"""One chain can contain several models. The snapshot must say so.

Three v2.1.1 defects about a snapshot describing itself inaccurately:

* **§4.** IV resolution falls back per contract. A chain configured for
  NBBO-mid IV where half the contracts have no usable mid ends up with two IV
  sources and two effective models. ``model_fingerprint`` reported one
  fingerprint, and which one depended on iteration order. A reader has no way
  to tell a uniform chain from a mixed one.
* **§8.** ``model_parameter_completeness`` read ``inputs.result.contracts``. An
  empty result set therefore had no missing inputs, so a chain where every
  contract was excluded reported a fully specified model. The moment the answer
  matters most -- nothing survived, why? -- is the moment it went quiet.
* **§9.** ``MODEL_VERSION`` was still ``gex-engine/2.1.0`` two releases after
  the numerics changed.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.domain.iv import IVSource, build_iv_quote, missing_iv
from src.domain.model_spec import MODEL_VERSION, ModelSpec, RateSource
from src.gex.config import GexEngineConfig
from src.gex.engine import compute_gex_snapshot
from src.gex.sessions import eastern
from src.synthetic.chains import build_synthetic_chain, with_quote

AS_OF = eastern(2026, 3, 17, 11, 0)


def mixed_iv_chain():
    """A chain where one contract falls back to the vendor default."""
    chain = build_synthetic_chain()
    fallback = build_iv_quote(
        bid_iv=None,
        mid_iv=None,
        ask_iv=None,
        vendor_iv=0.20,
        vendor_iv_error=0.0001,
        preferred_source=IVSource.NBBO_MID_IV,
    )
    return with_quote(chain, 0, iv=fallback)


def distribution(snapshot) -> dict:
    return snapshot.meta["model_distribution"]


# =============================================================================
# §4 -- mixed sources are visible
# =============================================================================


def test_a_uniform_chain_reports_one_iv_source():
    payload = distribution(compute_gex_snapshot(build_synthetic_chain()))
    assert len(payload["iv_source_counts"]) == 1
    assert payload["mixed_iv_sources"] is False


def test_a_fallback_makes_the_chain_mixed():
    """The regression: v2.1.1 reported one model for this chain."""
    payload = distribution(compute_gex_snapshot(mixed_iv_chain()))
    assert payload["mixed_iv_sources"] is True
    assert len(payload["iv_source_counts"]) >= 2


def test_every_required_distribution_is_reported():
    payload = distribution(compute_gex_snapshot(mixed_iv_chain()))
    for key in (
        "iv_source_counts",
        "gamma_source_counts",
        "effective_model_fingerprint_counts",
        "fallback_reason_counts",
        "mixed_iv_sources",
        "mixed_gamma_sources",
        "mixed_effective_models",
        "contracts_by_iv_source",
        "contracts_by_effective_model",
    ):
        assert key in payload, key


def test_the_first_contract_does_not_define_the_chain():
    """Reordering the chain must not change what the snapshot claims."""
    chain = mixed_iv_chain()
    reversed_chain = replace(chain, quotes=tuple(reversed(chain.quotes)))
    assert distribution(compute_gex_snapshot(chain)) == distribution(
        compute_gex_snapshot(reversed_chain)
    )


def test_counts_are_deterministic_and_sorted():
    payload = distribution(compute_gex_snapshot(mixed_iv_chain()))
    for key in ("iv_source_counts", "effective_model_fingerprint_counts"):
        assert list(payload[key]) == sorted(payload[key]), key


def test_the_counts_sum_to_the_included_contracts():
    snapshot = compute_gex_snapshot(mixed_iv_chain())
    payload = distribution(snapshot)
    assert sum(payload["iv_source_counts"].values()) == snapshot.contract_count


def test_a_mixed_chain_is_marked_uncalibrated():
    snapshot = compute_gex_snapshot(mixed_iv_chain())
    assert "effective_model_uniformity" in snapshot.confidence.uncalibrated_components


def test_a_mixed_chain_is_penalised_deterministically():
    uniform = compute_gex_snapshot(build_synthetic_chain())
    mixed = compute_gex_snapshot(mixed_iv_chain())
    assert mixed.confidence.value != uniform.confidence.value
    assert (
        compute_gex_snapshot(mixed_iv_chain()).confidence.value
        == mixed.confidence.value
    )


def test_mixed_models_change_the_replay_hash():
    assert compute_gex_snapshot(mixed_iv_chain()).output_hash() != (
        compute_gex_snapshot(build_synthetic_chain()).output_hash()
    )


def test_strict_mode_rejects_a_mixed_chain():
    from src.gex.formulas import MixedModelError

    with pytest.raises(MixedModelError, match=r"(?i)uniform"):
        compute_gex_snapshot(
            mixed_iv_chain(), GexEngineConfig(require_uniform_effective_model=True)
        )


def test_strict_mode_accepts_a_uniform_chain():
    snapshot = compute_gex_snapshot(
        build_synthetic_chain(), GexEngineConfig(require_uniform_effective_model=True)
    )
    assert snapshot.contract_count > 0


def test_research_mode_is_the_default():
    """Mixed chains are reported and penalised, not refused, unless asked."""
    assert GexEngineConfig().require_uniform_effective_model is False
    assert compute_gex_snapshot(mixed_iv_chain()).contract_count > 0


def test_a_uniform_chain_is_not_penalised():
    snapshot = compute_gex_snapshot(build_synthetic_chain())
    assert (
        "effective_model_uniformity" not in snapshot.confidence.uncalibrated_components
    )


# =============================================================================
# §8 -- static model completeness survives an empty result
# =============================================================================


def empty_chain():
    """Every contract excluded: no gamma anywhere."""
    chain = build_synthetic_chain()
    return replace(
        chain,
        quotes=tuple(replace(q, gamma=None, iv=missing_iv()) for q in chain.quotes),
    )


def completeness(snapshot) -> dict:
    return snapshot.meta["model_completeness"]


def test_zero_surviving_contracts_still_reports_a_missing_rate():
    """The regression: an empty result had no missing inputs, so the model
    looked fully specified at exactly the moment it mattered most."""
    spec = ModelSpec(
        risk_free_rate_source=RateSource.CONFIGURED_CONSTANT, risk_free_rate=None
    )
    snapshot = compute_gex_snapshot(empty_chain(), GexEngineConfig(model_spec=spec))
    assert snapshot.contract_count == 0
    assert not completeness(snapshot)["static_model_complete"]
    assert any(
        "risk_free_rate" in entry
        for entry in completeness(snapshot)["static_missing_inputs"]
    )


def test_zero_surviving_contracts_still_reports_a_missing_underlying():
    from src.domain.model_spec import UnderlyingPriceSource

    spec = ModelSpec(
        underlying_price_source=UnderlyingPriceSource.CONFIGURED_CONSTANT,
        configured_underlying_price=None,
    )
    snapshot = compute_gex_snapshot(empty_chain(), GexEngineConfig(model_spec=spec))
    assert any(
        "underlying" in entry
        for entry in completeness(snapshot)["static_missing_inputs"]
    )


def test_a_fully_specified_model_with_zero_contracts_is_statically_complete():
    from src.domain.model_spec import DividendSource

    spec = ModelSpec(
        risk_free_rate_source=RateSource.ZERO,
        dividend_yield_source=DividendSource.ZERO,
        iv_price_source=IVSource.NBBO_MID_IV,
    )
    snapshot = compute_gex_snapshot(empty_chain(), GexEngineConfig(model_spec=spec))
    assert snapshot.contract_count == 0
    assert completeness(snapshot)["static_model_complete"]


def test_static_completeness_and_data_availability_are_separate():
    payload = completeness(compute_gex_snapshot(empty_chain()))
    assert "static_model_complete" in payload
    assert payload["resolved_contract_count"] == 0
    assert payload["unresolved_contract_count"] >= 0


def test_an_empty_chain_does_not_erase_model_warnings():
    spec = ModelSpec(
        risk_free_rate_source=RateSource.CONFIGURED_CONSTANT, risk_free_rate=None
    )
    snapshot = compute_gex_snapshot(empty_chain(), GexEngineConfig(model_spec=spec))
    assert any(
        "risk_free_rate" in warning for warning in snapshot.confidence.warnings
    ) or any(
        "risk_free_rate" in c.detail
        for c in snapshot.confidence.components
        if c.name == "model_parameter_completeness"
    )


def test_per_input_failure_counts_are_reported():
    payload = completeness(compute_gex_snapshot(empty_chain()))
    assert "per_input_failure_counts" in payload


def test_a_healthy_chain_reports_resolved_contracts():
    payload = completeness(compute_gex_snapshot(build_synthetic_chain()))
    assert payload["resolved_contract_count"] > 0
    assert payload["static_model_complete"] in (True, False)


# =============================================================================
# §9 -- the engine version
# =============================================================================


def test_the_engine_version_reflects_this_release():
    assert MODEL_VERSION == "gex-engine/2.1.6"


def test_the_engine_version_is_defined_once():
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2] / "src"
    literal = re.compile(r'"gex-engine/[\d.]+"')
    hits = [
        path.name
        for path in root.rglob("*.py")
        if literal.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == ["model_spec.py"]


def test_the_engine_version_reaches_the_model_fingerprint():
    assert ModelSpec().model_version == MODEL_VERSION


def test_the_engine_version_reaches_the_snapshot():
    snapshot = compute_gex_snapshot(build_synthetic_chain())
    assert snapshot.meta["engine_version"] == MODEL_VERSION


def test_changing_the_engine_version_changes_the_replay_hash():
    baseline = compute_gex_snapshot(build_synthetic_chain()).output_hash()
    shifted = compute_gex_snapshot(
        build_synthetic_chain(),
        GexEngineConfig(model_spec=ModelSpec(model_version="gex-engine/9.9.9")),
    ).output_hash()
    assert shifted != baseline


def test_parser_version_and_engine_version_stay_distinct():
    from src.adapters.raw_store import PARSER_VERSION

    assert PARSER_VERSION != MODEL_VERSION
    assert PARSER_VERSION.startswith("thetadata-v3-parser/")
    assert MODEL_VERSION.startswith("gex-engine/")


def test_all_three_versions_are_documented():
    import pathlib
    import tomllib

    root = pathlib.Path(__file__).resolve().parents[2]
    package = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert package["project"]["version"] == "2.1.6"

    text = (root / "docs" / "VALIDATION.md").read_text(encoding="utf-8")
    assert "gex-engine/2.1.6" in text
    assert "thetadata-v3-parser/2.1.6" in text
