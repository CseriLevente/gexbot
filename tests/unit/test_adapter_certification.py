"""Is this repository ready to spend money on one real ThetaData session?

Not a trading readiness check. Nothing here decides whether to trade, because
nothing here can trade. It decides whether a paid capture would produce evidence
worth having, or a directory of bytes whose provenance nobody can reconstruct
afterwards.

Two vendor-dependent unknowns get explicit treatment rather than a guess:

* **Open interest as-of.** ThetaData's snapshot endpoints do not state which
  settlement date the open interest belongs to. v2.1.1 accepted a caller-supplied
  date and recorded it as though it were observed. A number whose date we chose
  is not evidence about the date.
* **Synchronised spot.** The spot and the option chain are separate reads. If
  they are minutes apart, every gamma is computed against an underlying the
  chain never saw, and nothing in v2.1.1 required the two clocks to be close.

Both block certification when unverified. Neither is silently resolved.
"""

from __future__ import annotations

from datetime import date, timedelta

from src.adapters.certification import (
    AdapterCertificationReadiness,
    OpenInterestProvenance,
    SpotProvenance,
    assess_readiness,
)
from src.adapters.transport import FakeTransport
from src.config.pipeline import ThetaDataResearchPipeline
from src.config.thetadata import parse_thetadata_config
from src.gex.sessions import eastern

AS_OF = eastern(2026, 3, 17, 11, 0)


def pipeline(**overrides):
    return ThetaDataResearchPipeline.from_config(
        parse_thetadata_config(overrides), transport=FakeTransport()
    )


def verified_oi() -> OpenInterestProvenance:
    return OpenInterestProvenance(
        as_of=date(2026, 3, 16), source="vendor_field", caller_supplied=False
    )


def verified_spot() -> SpotProvenance:
    return SpotProvenance(
        source="vendor_index_snapshot",
        timestamp=AS_OF - timedelta(milliseconds=200),
        tolerance_seconds=1.0,
    )


def readiness(**overrides) -> AdapterCertificationReadiness:
    payload = {
        "pipeline": pipeline(),
        "as_of": AS_OF,
        "open_interest": verified_oi(),
        "spot": verified_spot(),
    }
    payload.update(overrides)
    return assess_readiness(**payload)


# =============================================================================
# §19 -- open-interest and spot provenance are explicit
# =============================================================================


def test_a_fully_configured_offline_pipeline_can_be_capture_ready():
    assert readiness().ready
    assert readiness().blockers == ()


def test_missing_open_interest_provenance_blocks_certification():
    result = readiness(open_interest=None)
    assert not result.ready
    assert any("open_interest" in blocker for blocker in result.blockers)


def test_a_caller_supplied_open_interest_date_is_recorded_as_such():
    """Accepted, but never described as observed."""
    supplied = OpenInterestProvenance(
        as_of=date(2026, 3, 16), source="caller", caller_supplied=True
    )
    result = readiness(open_interest=supplied)
    assert "open_interest_as_of" in result.unverified_fields
    assert any("caller" in warning for warning in result.warnings)


def test_a_caller_supplied_date_does_not_block_capture():
    """It is a documented limitation, not a reason to refuse the session."""
    supplied = OpenInterestProvenance(
        as_of=date(2026, 3, 16), source="caller", caller_supplied=True
    )
    assert readiness(open_interest=supplied).ready


def test_a_verified_vendor_source_replaces_caller_supplied_provenance():
    assert "open_interest_as_of" in readiness().verified_fields


def test_open_interest_without_a_date_blocks_certification():
    empty = OpenInterestProvenance(as_of=None, source="caller", caller_supplied=True)
    assert not readiness(open_interest=empty).ready


def test_missing_spot_timestamp_blocks_certification():
    result = readiness(
        spot=SpotProvenance(
            source="vendor_index_snapshot", timestamp=None, tolerance_seconds=1.0
        )
    )
    assert not result.ready
    assert any("spot" in blocker for blocker in result.blockers)


def test_spot_skew_beyond_tolerance_blocks_certification():
    result = readiness(
        spot=SpotProvenance(
            source="vendor_index_snapshot",
            timestamp=AS_OF - timedelta(seconds=90),
            tolerance_seconds=1.0,
        )
    )
    assert not result.ready
    assert any(
        "skew" in blocker or "tolerance" in blocker for blocker in result.blockers
    )


def test_spot_skew_within_tolerance_is_accepted():
    assert readiness(
        spot=SpotProvenance(
            source="vendor_index_snapshot",
            timestamp=AS_OF - timedelta(milliseconds=500),
            tolerance_seconds=1.0,
        )
    ).ready


def test_an_unnamed_spot_source_blocks_certification():
    result = readiness(
        spot=SpotProvenance(source="", timestamp=AS_OF, tolerance_seconds=1.0)
    )
    assert not result.ready


def test_the_selected_spot_source_is_documented():
    assert "spot_source" in readiness().verified_fields


def test_missing_spot_provenance_entirely_blocks_certification():
    assert not readiness(spot=None).ready


# =============================================================================
# §20 -- the readiness report
# =============================================================================


def test_the_report_exposes_every_required_field():
    result = readiness()
    for attribute in (
        "ready",
        "blockers",
        "warnings",
        "verified_fields",
        "unverified_fields",
    ):
        assert hasattr(result, attribute), attribute


def test_a_pricing_mismatch_blocks_readiness():
    """VENDOR_IV_LOCAL_GAMMA with undocumented vendor conventions."""
    mismatched = pipeline(
        pricing_mode="VENDOR_IV_LOCAL_GAMMA",
        annual_dividend=1.3,
        dividend_convention="UNKNOWN_VENDOR_DIVIDEND_CONVENTION",
    )
    result = readiness(pipeline=mismatched)
    assert not result.ready
    assert any("pricing" in blocker for blocker in result.blockers)


def test_a_compatible_pricing_mode_does_not_block():
    assert readiness(pipeline=pipeline(pricing_mode="LOCAL_IV_LOCAL_GAMMA")).ready


def test_unknown_chain_completeness_is_a_warning_not_a_blocker():
    """No contract-list endpoint is wired, and that must not stop a capture --
    the capture is how we find out what the vendor offers."""
    result = readiness()
    assert result.ready
    assert any("completeness" in warning.lower() for warning in result.warnings)


def test_the_report_is_serialisable():
    import json

    json.dumps(readiness().as_dict())


def test_blockers_are_deterministic_and_sorted():
    first = readiness(open_interest=None, spot=None)
    second = readiness(spot=None, open_interest=None)
    assert first.blockers == second.blockers
    assert list(first.blockers) == sorted(first.blockers)


def test_the_report_names_what_it_verified():
    result = readiness()
    assert result.verified_fields
    assert set(result.verified_fields) & {"spot_source", "open_interest_as_of"}


def test_an_unsafe_raw_store_blocks_readiness(tmp_path):
    from src.adapters.raw_store import FileRawStore

    store = FileRawStore(tmp_path / "raw")
    (store.root / ".partial-abc.tmp").write_text("half", encoding="utf-8")
    result = readiness(raw_store=store)
    assert not result.ready
    assert any("raw store" in blocker.lower() for blocker in result.blockers)


def test_a_healthy_raw_store_does_not_block(tmp_path):
    from src.adapters.raw_store import FileRawStore

    assert readiness(raw_store=FileRawStore(tmp_path / "raw")).ready


def test_readiness_never_implies_trading_readiness():
    """Stated in the object itself, so nobody can quote it out of context."""
    result = readiness()
    assert "not a trading" in result.scope.lower()
    assert result.trading_enabled is False


def test_the_report_cannot_enable_trading():
    result = readiness()
    for banned in ("place", "order", "execute", "broker", "position", "submit"):
        assert not any(banned in name.lower() for name in dir(result)), banned


def test_readiness_is_recomputed_not_cached():
    """A blocker that is fixed must clear, and one that appears must register."""
    assert readiness().ready
    assert not readiness(spot=None).ready
    assert readiness().ready
