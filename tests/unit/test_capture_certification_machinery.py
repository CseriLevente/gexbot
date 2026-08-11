"""Does the reconstruction recover conventions it was never given?

``test_live_capture_certification.py`` asserts what the real capture showed.
Nothing there checks that the *method* works -- an inversion wrong in a way that
happened to favour ACT/365 would pass every one of those tests.

v2.1.22 tried to close that with a synthetic capture, and closed it badly: the
generator used ``RATE = 4.2``, ``DAYS_PER_YEAR = 365`` and ``EXPIRY_TIME =
16:00``, which were the three constants the implementation contained. It proved
the code agreed with itself.

Here the fake vendor is parameterised and the parameters are varied against the
grain: a wire value of 0.042 as well as 4.2, a percent-reading vendor as well as
a decimal one, ACT/360 and ACT/252 as well as ACT/365, and closes at 15:30 and
16:30 as well as 16:00. The certification is asked to name all of them having
been told none, and the assertions are on the **final evidence objects**, not on
score arrays -- because a score table can be right while the label beside it is
a constant, which is exactly the defect this release fixes.

**Nothing here is evidence about ThetaData.** These rows were computed.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from datetime import time

import pytest

from src.adapters.errors import ThetaDataProvenanceError
from src.adapters.thetadata.capture_certification import (
    CaptureCertificationError,
    certify_capture,
    load_capture,
)
from src.adapters.thetadata.live_behavior import BehaviorDimension, CaptureIdentity
from src.tools.certify_thetadata_capture import (
    EXIT_CONFLICT,
    EXIT_OK,
    EXIT_UNREADABLE,
    main,
)
from tests.synthetic_capture import (
    OPTION_GREEKS,
    SyntheticVendor,
    rewrite_intent,
    rewrite_manifest,
    write_capture,
)


def _certify(tmp_path: pathlib.Path, vendor: SyntheticVendor, name: str = "cap"):
    return certify_capture(write_capture(tmp_path / name, vendor))


# ---------------------------------------------------------------------------
# The inference, against conventions it was not given
# ---------------------------------------------------------------------------

#: label, vendor, expected unit, expected day count, expected clock
MATRIX = (
    (
        "capture #1: wire 4.2 read as a decimal",
        SyntheticVendor(),
        "DECIMAL_ANNUAL_RATE",
        "ACT_365",
        "16:00 America/New_York",
    ),
    (
        "capture #2: wire 0.042 read as a decimal",
        SyntheticVendor(wire_rate_value=0.042, declared_economic_rate=0.042),
        "DECIMAL_ANNUAL_RATE",
        "ACT_365",
        "16:00 America/New_York",
    ),
    (
        "a vendor that reads its own documentation",
        SyntheticVendor(rate_unit="PERCENT_ANNUAL_RATE"),
        "PERCENT_ANNUAL_RATE",
        "ACT_365",
        "16:00 America/New_York",
    ),
    (
        "a 360-day vendor closing at 15:30",
        SyntheticVendor(
            wire_rate_value=0.042,
            days_per_year=360.0,
            expiry_clock=time(15, 30),
            declared_economic_rate=0.042,
        ),
        "DECIMAL_ANNUAL_RATE",
        "ACT_360",
        "15:30 America/New_York",
    ),
    (
        "a 252-day vendor closing at 16:30",
        SyntheticVendor(
            wire_rate_value=0.042,
            days_per_year=252.0,
            expiry_clock=time(16, 30),
            declared_economic_rate=0.042,
        ),
        "DECIMAL_ANNUAL_RATE",
        "ACT_252",
        "16:30 America/New_York",
    ),
)


@pytest.mark.parametrize(
    ("label", "vendor", "unit", "day_count", "clock"),
    MATRIX,
    ids=[case[0] for case in MATRIX],
)
def test_the_certification_names_conventions_it_was_never_told(
    tmp_path, label, vendor, unit, day_count, clock
):
    """Every dimension, asserted on the evidence rather than on a score table."""
    report = _certify(tmp_path, vendor)

    # The rate, as a number and as a unit.
    assert report.rate_economics.vendor_effective_rate == pytest.approx(
        vendor.effective_rate
    )
    observation = report.ledger.for_dimension(BehaviorDimension.RATE_UNITS)
    assert observation.observed_value == unit
    assert observation.governing_value == unit

    # The day count and the clock, from the ledger and from the report, so a
    # hard-coded label anywhere fails here rather than passing quietly.
    assert report.resolved_day_count == day_count
    assert report.resolved_expiration_clock == clock
    assert (
        report.ledger.for_dimension(BehaviorDimension.DAY_COUNT).observed_value
        == day_count
    )
    assert (
        report.ledger.for_dimension(
            BehaviorDimension.EXPIRATION_TIMESTAMP
        ).observed_value
        == clock
    )
    # And the winner is the best-scoring hypothesis, not merely a matching name.
    assert min(report.day_count_scores, key=lambda s: s.delta_rmse).hypothesis in {
        "ACT/365",
        "ACT/365.25",
        "ACT/360",
        "ACT/252",
    }


def test_hypotheses_derive_from_the_captured_wire_value(tmp_path):
    """DECIMAL is ``w``; PERCENT is ``w/100``. Nothing knows what ``w`` is."""
    report = _certify(
        tmp_path, SyntheticVendor(wire_rate_value=0.042, declared_economic_rate=0.042)
    )
    tested = {s.hypothesis for s in report.rate_scores}
    assert any("r=0.042" in h for h in tested)
    assert any("r=0.00042" in h for h in tested)
    # The literal first-capture pair must not appear for a 0.042 capture.
    assert not any("r=4.2" in h for h in tested)
    assert report.request.greeks_rate_value == pytest.approx(0.042)


def test_a_wrong_hypothesis_cannot_win_by_naming(tmp_path):
    """The losing rate is scored and loses by orders of magnitude."""
    report = _certify(tmp_path, SyntheticVendor())
    best = min(report.rate_scores, key=lambda s: s.delta_rmse)
    worst = max(report.rate_scores, key=lambda s: s.delta_rmse)
    assert "DECIMAL_ANNUAL_RATE" in best.hypothesis
    assert worst.delta_rmse > 100 * best.delta_rmse


# ---------------------------------------------------------------------------
# Documentation conflict versus economic correctness
# ---------------------------------------------------------------------------


def test_a_conflicting_vendor_can_still_price_the_intended_rate(tmp_path):
    """The second capture's expected state: conflict true, economics correct."""
    report = _certify(
        tmp_path, SyntheticVendor(wire_rate_value=0.042, declared_economic_rate=0.042)
    )
    economics = report.rate_economics
    assert economics.documentation_live_conflict is True
    assert economics.effective_rate_matches_intended is True
    assert economics.vendor_effective_rate == pytest.approx(0.042)
    assert economics.intended_economic_rate == pytest.approx(0.042)
    assert economics.intended_rate_source == "BOUND_TO_PREFLIGHT_APPROVAL"
    # The conflict is still recorded as a conflict ...
    assert (
        report.ledger.for_dimension(BehaviorDimension.RATE_UNITS).status.value
        == "DOCUMENTATION_LIVE_CONFLICT"
    )
    # ... and it no longer blocks the capture on rate grounds.
    assert not any("factor of" in blocker for blocker in report.gex_blockers)


def test_a_capture_priced_at_the_wrong_magnitude_is_blocked(tmp_path):
    """Capture #1's state: the same conflict, and a rate blocker with it."""
    report = _certify(tmp_path, SyntheticVendor())
    economics = report.rate_economics
    assert economics.documentation_live_conflict is True
    assert economics.effective_rate_matches_intended is False
    assert economics.magnitude_ratio == pytest.approx(100.0)
    assert economics.intended_rate_source == "LEGACY_CAPTURE_DOCUMENTATION_DERIVED"
    assert any("factor of 100" in blocker for blocker in report.gex_blockers)
    assert report.trusted_for_gex is False


def test_an_agreeing_vendor_records_no_conflict(tmp_path):
    """A percent-reading vendor matches its documentation, so nothing conflicts."""
    report = _certify(tmp_path, SyntheticVendor(rate_unit="PERCENT_ANNUAL_RATE"))
    assert report.rate_economics.documentation_live_conflict is False
    assert (
        report.ledger.for_dimension(BehaviorDimension.RATE_UNITS).status.value
        == "DOCUMENTATION_LIVE_AGREE"
    )
    assert "RATE_UNITS" not in report.ledger.as_dict()["conflicts"]


# ---------------------------------------------------------------------------
# The request rate is bound to the capture
# ---------------------------------------------------------------------------


def test_certification_takes_no_rate_from_its_caller(tmp_path):
    """There is no argument through which a rate could be supplied."""
    import inspect

    parameters = set(inspect.signature(certify_capture).parameters)
    assert parameters == {"root", "archive_path", "archive_sha256"}
    # Nothing resembling a rate, a unit or an economic intent.
    assert not any("rate" in name or "intent" in name for name in parameters)


def test_editing_the_recorded_rate_breaks_the_binding(tmp_path):
    """Pairing one capture's responses with another capture's rate is refused."""
    root = write_capture(tmp_path / "cap", SyntheticVendor())

    def swap(payload):
        for entry in payload["request_plan"]["requests"]:
            if entry["endpoint"] == OPTION_GREEKS:
                for pair in entry["canonical_query_parameters"]:
                    if pair[0] == "rate_value":
                        pair[1] = "0.042"

    rewrite_intent(root, swap)
    with pytest.raises(
        CaptureCertificationError, match="not the request that produced"
    ):
        certify_capture(root)


def test_a_capture_without_a_run_intent_is_refused(tmp_path):
    root = write_capture(tmp_path / "cap", SyntheticVendor())
    (root / "run-intent.json").unlink()
    with pytest.raises(CaptureCertificationError, match="run intent"):
        certify_capture(root)


def test_a_greeks_request_without_a_rate_is_refused(tmp_path):
    root = write_capture(tmp_path / "cap", SyntheticVendor())

    def drop(payload):
        for entry in payload["request_plan"]["requests"]:
            if entry["endpoint"] == OPTION_GREEKS:
                entry["canonical_query_parameters"] = [
                    pair
                    for pair in entry["canonical_query_parameters"]
                    if pair[0] != "rate_value"
                ]

    rewrite_intent(root, drop)
    # The binding still verifies -- the parameters and the stamp disagree now,
    # so this is refused at the binding rather than at the rate.
    with pytest.raises(CaptureCertificationError):
        certify_capture(root)


# ---------------------------------------------------------------------------
# The manifest hash is recomputed, not read
# ---------------------------------------------------------------------------


def test_a_valid_manifest_passes(tmp_path):
    capture = load_capture(write_capture(tmp_path / "cap", SyntheticVendor()))
    assert capture.verified_records == 5
    assert capture.manifest_hash


def test_a_mutated_descriptor_without_a_new_digest_is_refused(tmp_path):
    root = write_capture(tmp_path / "cap", SyntheticVendor())
    rewrite_manifest(
        root, lambda payload: payload["records"][0].update(byte_length=999_999)
    )
    with pytest.raises(CaptureCertificationError, match="descriptors hash to"):
        load_capture(root)


def test_a_mutated_stored_digest_is_refused(tmp_path):
    root = write_capture(tmp_path / "cap", SyntheticVendor())
    rewrite_manifest(root, lambda payload: payload.update(manifest_hash="f" * 64))
    with pytest.raises(CaptureCertificationError, match="descriptors hash to"):
        load_capture(root)


def test_a_manifest_with_no_digest_at_all_is_refused(tmp_path):
    root = write_capture(tmp_path / "cap", SyntheticVendor())
    rewrite_manifest(root, lambda payload: payload.pop("manifest_hash"))
    with pytest.raises(CaptureCertificationError, match="no manifest_hash"):
        load_capture(root)


def test_the_manifest_round_trip_is_lossless(tmp_path):
    """``rebuilt_from`` must restore every field the digest covers.

    It dropped ``preflight_approval_hash`` until v2.1.23, so rebuilding the
    first live capture produced a digest that did not match its own stored one.
    Any check built on the recomputation would have refused every honest
    capture -- and told nobody anything about a dishonest one.
    """
    from src.adapters.raw_store import RawCaptureManifest

    root = write_capture(tmp_path / "cap", SyntheticVendor())
    payload = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    payload["records"][0]["preflight_approval_hash"] = "c" * 64
    rebuilt = RawCaptureManifest.rebuilt_from(payload)
    assert rebuilt.records[0].preflight_approval_hash == "c" * 64
    # And the digest moves when it changes, which is what makes it covered.
    assert rebuilt.manifest_hash != payload["manifest_hash"]


# ---------------------------------------------------------------------------
# Archive identity is not manifest identity
# ---------------------------------------------------------------------------


def test_an_absent_archive_digest_stays_absent(tmp_path):
    report = _certify(tmp_path, SyntheticVendor())
    assert report.archive_sha256 == ""
    assert report.as_dict()["capture"]["archive_sha256"] == ""
    # Never silently filled from the manifest.
    assert report.as_dict()["capture"]["archive_sha256"] != report.manifest_hash


def test_a_supplied_archive_digest_is_never_treated_as_identity(tmp_path):
    """Sixty-four hex characters from a caller are not evidence.

    v2.1.23 reported ``archive_sha256="d"*64`` as the capture's archive
    identity without opening a file, so a reader checking a download against it
    would have been checking it against an assertion.
    """
    root = write_capture(tmp_path / "cap", SyntheticVendor())
    report = certify_capture(root, archive_sha256="d" * 64)
    assert report.archive.provenance == "UNVERIFIED_EXTERNAL_ARCHIVE_DIGEST_CLAIM"
    assert report.archive.known is False
    # The claim is visible, and it is not the identity.
    assert report.archive.sha256 == "d" * 64
    assert report.archive_sha256 == ""
    assert report.as_dict()["capture"]["archive_identity_known"] is False


def test_an_archive_is_hashed_from_its_own_bytes(tmp_path):
    """Supply the file and certification computes the digest itself."""
    import zipfile

    root = write_capture(tmp_path / "cap", SyntheticVendor())
    archive = tmp_path / "capture.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(root / "manifest.json", "capture/manifest.json")

    report = certify_capture(root, archive_path=archive)
    assert report.archive.provenance == "VERIFIED_FROM_BYTES"
    assert report.archive.known is True
    assert report.archive.contains_capture_manifest is True
    assert report.archive_sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()


def test_an_archive_that_disagrees_with_the_caller_is_refused(tmp_path):
    import zipfile

    root = write_capture(tmp_path / "cap", SyntheticVendor())
    archive = tmp_path / "capture.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(root / "manifest.json", "capture/manifest.json")

    with pytest.raises(CaptureCertificationError, match="hashes to"):
        certify_capture(root, archive_path=archive, archive_sha256="e" * 64)


def test_an_archive_digest_equal_to_the_manifest_is_refused():
    """The substitution v2.1.22 performed, refused by construction."""
    with pytest.raises(ThetaDataProvenanceError, match="identify different"):
        CaptureIdentity(session_id="s", manifest_hash="a" * 64, archive_sha256="a" * 64)


def test_an_identity_without_an_archive_is_valid_and_says_so():
    identity = CaptureIdentity(session_id="s", manifest_hash="a" * 64)
    assert identity.archive_identity_known is False
    identity = CaptureIdentity(
        session_id="s", manifest_hash="a" * 64, archive_sha256="b" * 64
    )
    assert identity.archive_identity_known is True


# ---------------------------------------------------------------------------
# Hybrid clock evidence
# ---------------------------------------------------------------------------


def test_the_clock_evidence_separates_the_two_regimes(tmp_path):
    report = _certify(tmp_path, SyntheticVendor())
    evidence = report.clock_evidence
    assert evidence.intraday_expirations == ("2026-08-12",)
    assert evidence.whole_day_expirations == ("2026-08-24", "2026-09-16")
    assert evidence.contradicting_count == 2
    assert evidence.scope_is_global is False
    assert not evidence.unexplained_expirations


def test_a_gap_in_the_sample_leaves_the_boundary_open(tmp_path):
    """The transition is only claimed when it was actually bracketed."""
    report = _certify(tmp_path, SyntheticVendor())
    evidence = report.clock_evidence
    assert evidence.boundary_last_intraday == "2026-08-12"
    assert evidence.boundary_first_whole_day == "2026-08-24"
    assert evidence.boundary_gap_days == 12
    assert evidence.boundary_status == "OPEN"


def test_adjacent_expirations_resolve_the_boundary(tmp_path):
    """With nothing skipped, the transition *is* observed and says so."""
    from datetime import date

    vendor = SyntheticVendor(
        expirations=(date(2026, 8, 12), date(2026, 8, 13), date(2026, 8, 14)),
        front_week_end=date(2026, 8, 13),
    )
    evidence = _certify(tmp_path, vendor).clock_evidence
    assert evidence.boundary_last_intraday == "2026-08-13"
    assert evidence.boundary_first_whole_day == "2026-08-14"
    assert evidence.boundary_gap_days == 1
    assert evidence.boundary_status == "RESOLVED"


def test_a_uniform_vendor_reports_a_global_scope(tmp_path):
    """No contradiction means the clock may be applied everywhere."""
    from datetime import date

    vendor = SyntheticVendor(front_week_end=date(2027, 1, 1))
    evidence = _certify(tmp_path, vendor).clock_evidence
    assert evidence.whole_day_expirations == ()
    assert evidence.scope_is_global is True
    assert evidence.boundary_status == "NOT_OBSERVED"


# ---------------------------------------------------------------------------
# Everything else the report still has to get right
# ---------------------------------------------------------------------------


def test_the_generated_universe_is_certified_by_set_hash(tmp_path):
    universe = _certify(tmp_path, SyntheticVendor()).universe
    assert universe.quote_matches_list
    assert universe.greeks_matches_list
    assert universe.state == "DEDICATED_CONTRACT_LIST_MATCHED_SNAPSHOT_UNIVERSE"


def test_missing_and_explicit_zero_open_interest_are_counted_apart(tmp_path):
    coverage = _certify(tmp_path, SyntheticVendor()).open_interest
    assert coverage.missing_count > 0
    assert coverage.explicit_zero_count > 0
    assert (
        coverage.present_count + coverage.explicit_zero_count + coverage.missing_count
        == coverage.universe_count
    )
    assert coverage.permits_trusted_aggregate is False


def test_the_reconstruction_recovers_the_iv_basis(tmp_path):
    report = _certify(tmp_path, SyntheticVendor(wire_rate_value=0.042))
    assert min(report.iv_basis_scores, key=lambda row: row[2])[0] == "NBBO_MID"


def test_the_report_is_content_addressed_and_stable(tmp_path):
    root = write_capture(tmp_path / "cap", SyntheticVendor())
    first, second = certify_capture(root), certify_capture(root)
    assert first.report_hash() == second.report_hash()
    assert first.as_dict() == second.as_dict()


def test_a_payload_that_no_longer_matches_its_hash_is_refused(tmp_path):
    root = write_capture(tmp_path / "cap", SyntheticVendor())
    payload = root / "raw" / "v3-index-snapshot-price.raw"
    payload.write_bytes(payload.read_bytes() + b"tampered\n")
    with pytest.raises(CaptureCertificationError, match="not the bytes that were"):
        load_capture(root)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def test_the_command_reports_a_conflict_with_its_own_exit_code(tmp_path, capsys):
    root = write_capture(tmp_path / "cap", SyntheticVendor())
    out = tmp_path / "report.json"
    code = main([str(root), "--json", str(out), "--archive-sha256", "e" * 64])

    assert code == EXIT_CONFLICT
    written = json.loads(out.read_text(encoding="utf-8"))
    assert "RATE_UNITS" in written["documentation_live_conflicts"]
    assert written["capture"]["archive_sha256"] == "e" * 64
    assert written["capture"]["archive_identity_known"] is False
    printed = capsys.readouterr().out
    assert "trusted for gex False" in printed
    assert "blocked by" in printed


def test_a_cleared_conflict_leaves_the_conflict_list(tmp_path, capsys):
    """Exit 3 is earned per dimension, not fixed.

    A percent-reading vendor agrees with its own recorded documentation about
    the rate, so ``RATE_UNITS`` leaves the list -- and since v2.1.24 nothing
    else is on it. The IV basis used to sit there against a ``TRADE_PRICE``
    constant living in the certification code; no capture records an
    ``IV_PRICE_BASIS`` extraction, so that comparison was a repository
    statement dressed as capture-time documentation. The live finding stands
    on its own as ``LIVE_ONLY``.
    """
    root = write_capture(
        tmp_path / "cap", SyntheticVendor(rate_unit="PERCENT_ANNUAL_RATE")
    )
    assert main([str(root)]) == EXIT_OK
    conflicts = json.loads(capsys.readouterr().out)["documentation_live_conflicts"]
    assert conflicts == []


def test_exit_zero_is_reachable_when_nothing_conflicts(tmp_path, monkeypatch, capsys):
    """Otherwise exit 3 would be a constant wearing the costume of a verdict.

    No real capture reaches this yet -- the pinned description still says the
    implied volatility was solved against a trade price and every
    reconstruction says the midpoint. The branch is exercised with a report
    whose ledger carries no conflict, so the exit code is shown to follow from
    the evidence rather than from the fact that certification ran.
    """
    from src.tools import certify_thetadata_capture as command

    root = write_capture(tmp_path / "cap", SyntheticVendor())
    real = certify_capture(root)

    class _NoConflicts:
        ledger = type("_L", (), {"conflicts": ()})()

        def as_dict(self):
            return real.as_dict()

        def report_hash(self):
            return real.report_hash()

    monkeypatch.setattr(command, "certify_capture", lambda *a, **k: _NoConflicts())
    assert command.main([str(root)]) == EXIT_OK
    capsys.readouterr()


def test_the_command_refuses_an_unreadable_capture(tmp_path, capsys):
    assert main([str(tmp_path / "nothing-here")]) == EXIT_UNREADABLE
    assert "cannot be certified" in capsys.readouterr().err


def test_the_command_makes_no_network_call(tmp_path, monkeypatch):
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("certification attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    root = write_capture(tmp_path / "cap", SyntheticVendor())
    assert main([str(root)]) == EXIT_CONFLICT
