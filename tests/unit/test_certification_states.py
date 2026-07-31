"""Certification must not be able to bless what it has not checked.

The v2.1.2 defect. ``assess_readiness`` read ``pipeline.pricing_mode`` and, for
any mode that did not "mix vendor and local", returned compatible without
looking further. The default configuration claimed ``LOCAL_IV_LOCAL_GAMMA``
while fetching vendor-computed IV, so the check it needed most was the one it
skipped -- and the report said ready.

Two separate corrections meet here:

* the mode is now derived from provenance (§1), so it cannot be asserted; and
* an ``UNKNOWN`` on a field that changes gamma is a **blocker**, not a note
  printed beside the answer.

The state machine exists because "ready to capture" and "certified" are
different claims, and only the first is reachable without spending money.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.adapters.certification import (
    CertificationState,
    OpenInterestProvenance,
    SpotProvenance,
    assess_readiness,
)
from src.adapters.transport import FakeTransport
from src.config.pipeline import (
    PricingCompatibilityReport,
    ThetaDataResearchPipeline,
)
from src.config.thetadata import parse_thetadata_config
from src.gex.sessions import eastern

AS_OF = eastern(2026, 3, 17, 11, 0)


def pipeline(**overrides):
    return ThetaDataResearchPipeline.from_config(
        parse_thetadata_config(overrides), transport=FakeTransport()
    )


def resolved_pipeline():
    """Everything a vendor-IV session needs, stated explicitly.

    This is what the configuration looks like once somebody has read the vendor
    documentation and written the answers down. It is deliberately not the
    default: the default is what we know today, which is less.
    """
    built = pipeline(
        rate_value=4.2,
        rate_units="PERCENT_ANNUAL_RATE",
        annual_dividend=0.0,
        dividend_convention="ZERO_DIVIDEND",
    )
    # Stand in for vendor documentation settling the remaining conventions.
    from dataclasses import replace

    return replace(
        built,
        pricing_compatibility=PricingCompatibilityReport(
            compatible=True,
            compatible_fields=(
                "risk_free_rate (4.2 PERCENT_ANNUAL_RATE == 0.042 decimal)",
                "dividend_yield (ZERO_DIVIDEND)",
                "expiration_timestamp (documented)",
                "minimum_time_to_expiry (documented)",
                "underlying_price_source (documented)",
                "iv_calculation_convention (documented)",
            ),
        ),
    )


def readiness(**overrides):
    payload = {
        "pipeline": resolved_pipeline(),
        "as_of": AS_OF,
        "open_interest": OpenInterestProvenance(
            as_of=date(2026, 3, 16), source="vendor_field", caller_supplied=False
        ),
        "spot": SpotProvenance(
            source="vendor_index_snapshot",
            timestamp=AS_OF - timedelta(milliseconds=200),
            tolerance_seconds=1.0,
        ),
    }
    payload.update(overrides)
    return assess_readiness(**payload)


# =============================================================================
# §2 -- unknowns that change gamma are blockers
# =============================================================================


def test_the_default_configuration_is_not_capture_ready():
    """The regression. v2.1.2 reported ready here."""
    result = readiness(pipeline=pipeline())
    assert not result.ready
    assert result.state is CertificationState.NOT_READY


def test_the_default_blocks_specifically_on_load_bearing_unknowns():
    result = readiness(pipeline=pipeline())
    assert any("load-bearing" in blocker for blocker in result.blockers)


def test_an_unknown_vendor_rate_blocks_readiness():
    result = readiness(pipeline=pipeline(rate_value=4.2, rate_units="UNKNOWN"))
    assert not result.ready


def test_a_vendor_default_rate_blocks_readiness():
    """No rate_value sent means the vendor used *something* we cannot name."""
    result = readiness(pipeline=pipeline(rate_value=None))
    assert not result.ready


def test_an_unknown_dividend_convention_blocks_readiness():
    result = readiness(
        pipeline=pipeline(
            annual_dividend=1.3, dividend_convention="UNKNOWN_VENDOR_CONVENTION"
        )
    )
    assert not result.ready


def test_a_resolved_configuration_can_become_capture_ready():
    """The fix must leave a path to ready, or it is just a refusal."""
    result = readiness()
    assert result.ready, result.blockers
    assert result.state is CertificationState.READY_FOR_CAPTURE_ONLY


def test_a_manually_asserted_compatibility_cannot_bypass_unknowns():
    """compatible=True with load-bearing unknowns still blocks.

    The report's own ``compatible`` flag is not the authority; the unresolved
    field list is.
    """
    from dataclasses import replace

    dishonest = replace(
        pipeline(),
        pricing_compatibility=PricingCompatibilityReport(
            compatible=True,
            unknown_fields=(
                "risk_free_rate: units are undocumented",
                "dividend_yield: convention is undocumented",
            ),
        ),
    )
    assert not readiness(pipeline=dishonest).ready


def test_incompatible_fields_block_even_without_unknowns():
    from dataclasses import replace

    built = replace(
        pipeline(),
        pricing_compatibility=PricingCompatibilityReport(
            compatible=False,
            incompatible_fields=("risk_free_rate: 0.05 vs 0.042",),
        ),
    )
    result = readiness(pipeline=built)
    assert not result.ready
    assert any("incompatible" in blocker for blocker in result.blockers)


# =============================================================================
# §23 -- the state machine
# =============================================================================


def test_no_live_capture_can_ever_produce_certified():
    """ADAPTER_CERTIFIED needs bytes AND a validation report; offline has neither."""
    assert readiness().state is not CertificationState.ADAPTER_CERTIFIED


def test_a_capture_without_validation_is_not_certified():
    result = readiness(capture_manifest=object())
    assert result.state is CertificationState.CAPTURE_COMPLETED_NOT_VALIDATED
    assert result.state is not CertificationState.ADAPTER_CERTIFIED


def test_certification_requires_both_a_capture_and_a_validation():
    result = readiness(capture_manifest=object(), validation_report=object())
    assert result.state is CertificationState.ADAPTER_CERTIFIED


def test_blockers_override_every_other_state():
    result = readiness(spot=None, capture_manifest=object(), validation_report=object())
    assert result.state is CertificationState.NOT_READY


def test_missing_credentials_prevent_capture_readiness(monkeypatch):
    monkeypatch.delenv("THETA_USER", raising=False)
    monkeypatch.delenv("THETA_PASS", raising=False)
    from dataclasses import replace

    built = resolved_pipeline()
    needs_auth = replace(
        built,
        config=parse_thetadata_config(
            {
                "authentication_mode": "basic",
                "username_env": "THETA_USER",
                "password_env": "THETA_PASS",
                "rate_value": 4.2,
                "rate_units": "PERCENT_ANNUAL_RATE",
                "annual_dividend": 0.0,
                "dividend_convention": "ZERO_DIVIDEND",
            }
        ),
    )
    result = readiness(pipeline=needs_auth)
    assert not result.ready
    assert any("credential" in blocker.lower() for blocker in result.blockers)


def test_the_state_is_serialised():
    assert readiness().as_dict()["state"] == "READY_FOR_CAPTURE_ONLY"


@pytest.mark.parametrize(
    "state",
    [
        CertificationState.NOT_READY,
        CertificationState.READY_FOR_CAPTURE_ONLY,
        CertificationState.CAPTURE_COMPLETED_NOT_VALIDATED,
        CertificationState.ADAPTER_CERTIFIED,
    ],
)
def test_every_state_is_declared(state):
    assert state.value


def test_unknown_chain_completeness_is_still_only_a_warning():
    """It is a reason to capture, not a reason to refuse."""
    result = readiness()
    assert result.ready
    assert any("completeness" in warning.lower() for warning in result.warnings)


def test_a_tier_that_cannot_serve_the_mode_blocks():
    """Enforced at config load, so the pipeline cannot even be built."""
    from src.config.thetadata import ThetaDataConfigError

    with pytest.raises(ThetaDataConfigError, match=r"(?i)tier"):
        pipeline(tier="value")


def test_readiness_never_implies_trading_readiness():
    result = readiness()
    assert result.trading_enabled is False
    assert "not a trading" in result.scope.lower()
