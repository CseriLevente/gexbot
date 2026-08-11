"""The v2.1.23 certification-authority defects, each one held shut.

Every test here fails on v2.1.23. They are grouped by the thing that was wrong
rather than by the code that was changed, because the code moved and the
defects are what has to stay fixed.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import zipfile
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from src.adapters.errors import ThetaDataProvenanceError
from src.adapters.raw_store import RawCaptureManifest
from src.adapters.thetadata.capture_certification import (
    CaptureCertificationError,
    _expiry_day_window,
    _implied_year_candidates,
    _recomputed_approval_hash,
    _resolve_root,
    certify_capture,
)
from src.adapters.thetadata.live_behavior import (
    BehaviorDimension,
    CaptureRateIntent,
    InferenceDecision,
)
from src.domain.contracts import OptionRight
from tests.synthetic_capture import (
    SyntheticVendor,
    rewrite_intent,
    write_capture,
)

EASTERN = ZoneInfo("America/New_York")


def _corrected(**overrides) -> SyntheticVendor:
    """The planned second capture: wire 0.042, read as a decimal."""
    return SyntheticVendor(
        wire_rate_value=0.042, declared_economic_rate=0.042, **overrides
    )


# ---------------------------------------------------------------------------
# 1. The declared economic intent is bound
# ---------------------------------------------------------------------------


def test_a_bound_rate_intent_survives_an_honest_round_trip(tmp_path):
    report = certify_capture(write_capture(tmp_path / "cap", _corrected()))
    binding = report.rate_intent_binding
    assert binding["bound"] is True
    assert binding["source"] == "BOUND_TO_PREFLIGHT_APPROVAL"
    assert len(binding["rate_intent_fingerprint"]) == 64
    assert report.rate_economics.effective_rate_matches_intended is True


@pytest.mark.parametrize(
    "field",
    [
        "economic_rate_decimal",
        "economic_rate_percent",
        "local_model_rate",
        "vendor_request_rate_value",
        "vendor_observed_rate_unit",
        "documented_rate_unit",
    ],
)
def test_editing_any_bound_rate_field_refuses_certification(tmp_path, field):
    """The reproduced attack, and every neighbouring one.

    v2.1.23 read ``economic_rate_decimal`` out of the run intent and believed
    it. Changing 0.042 to 4.2 -- touching no response, no request plan and no
    manifest -- flipped the capture from economically valid to invalid and
    certification reported the new answer without complaint.
    """
    root = write_capture(tmp_path / "cap", _corrected())
    replacement = {
        "economic_rate_decimal": 4.2,
        "economic_rate_percent": 420.0,
        "local_model_rate": 4.2,
        "vendor_request_rate_value": 4.2,
        "vendor_observed_rate_unit": "PERCENT_ANNUAL_RATE",
        "documented_rate_unit": "DECIMAL_ANNUAL_RATE",
    }[field]
    rewrite_intent(root, lambda p: p["rate_semantics"].update({field: replacement}))

    with pytest.raises(CaptureCertificationError):
        certify_capture(root)


def test_an_intent_that_contradicts_itself_is_refused():
    """4.2% beside 0.5 as a decimal is not an unusual convention."""
    with pytest.raises(ThetaDataProvenanceError, match="not the same rate"):
        CaptureRateIntent(
            economic_rate_percent=4.2,
            economic_rate_decimal=0.5,
            local_model_rate=0.5,
            vendor_request_rate_value=0.5,
            vendor_observed_rate_unit="DECIMAL_ANNUAL_RATE",
            documented_rate_unit="PERCENT_ANNUAL_RATE",
        )


def test_the_wire_value_must_express_the_economic_rate():
    """Sending 4.2 for 4.2% under decimal semantics is the original defect."""
    with pytest.raises(ThetaDataProvenanceError, match="is the value that expresses"):
        CaptureRateIntent(
            economic_rate_percent=4.2,
            economic_rate_decimal=0.042,
            local_model_rate=0.042,
            vendor_request_rate_value=4.2,
            vendor_observed_rate_unit="DECIMAL_ANNUAL_RATE",
            documented_rate_unit="PERCENT_ANNUAL_RATE",
        )


def test_an_intent_the_approval_never_covered_is_refused(tmp_path):
    """Link 2 on its own, with links 3 and 4 left intact.

    Editing ``rate_intent_fingerprint`` and stopping there is caught by the
    approval's own hash, which is a different check. To reach the
    intent-vs-approval comparison the approval has to be internally coherent
    *and* bound to the records -- so the hash is recomputed and restamped here,
    which is what somebody rewriting history would have to do.
    """
    root = write_capture(tmp_path / "cap", _corrected())

    def _reapprove(payload):
        approval = payload["preflight_approval"]
        approval["rate_intent_fingerprint"] = "0" * 64
        approval["approval_hash"] = _recomputed_approval_hash(approval)
        return approval["approval_hash"]

    stored: dict[str, str] = {}
    rewrite_intent(root, lambda p: stored.update(hash=_reapprove(p)))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for record in manifest["records"]:
        record["preflight_approval_hash"] = stored["hash"]
    manifest["manifest_hash"] = RawCaptureManifest.rebuilt_from(manifest).manifest_hash
    (root / "manifest.json").write_text(json.dumps(manifest, default=str), "utf-8")

    with pytest.raises(CaptureCertificationError, match="approval was taken against"):
        certify_capture(root)


def test_an_approval_whose_hash_does_not_follow_is_refused(tmp_path):
    root = write_capture(tmp_path / "cap", _corrected())
    rewrite_intent(
        root, lambda p: p["preflight_approval"].update(approval_hash="1" * 64)
    )
    with pytest.raises(CaptureCertificationError, match="contents hash to"):
        certify_capture(root)


def test_a_legacy_capture_derives_intent_from_its_own_documentation(tmp_path):
    """No bound intent, so the reading comes from what *that capture* recorded.

    Not from a constant in today's certification code: if ThetaData corrects
    its description, the constant follows and a historical capture silently
    acquires a different intent.
    """
    vendor = SyntheticVendor(documented_rate_unit="PERCENT_ANNUAL_RATE")
    report = certify_capture(write_capture(tmp_path / "cap", vendor))
    assert report.rate_intent_binding["bound"] is False
    assert (
        report.rate_economics.intended_rate_source
        == "LEGACY_CAPTURE_DOCUMENTATION_DERIVED"
    )
    # wire 4.2 read as the capture's own documented percent => 0.042 intended.
    assert report.rate_economics.intended_economic_rate == pytest.approx(0.042)


def test_a_new_capture_missing_its_intent_is_refused(tmp_path):
    """The legacy fallback is for history, not for holes in current work."""
    root = write_capture(tmp_path / "cap", _corrected())
    rewrite_intent(root, lambda p: p.pop("rate_semantics"))
    with pytest.raises(CaptureCertificationError, match=r"should have\s+recorded"):
        certify_capture(root)


# ---------------------------------------------------------------------------
# 2. The inversion keeps every root and resolves it honestly
# ---------------------------------------------------------------------------


def test_the_inversion_returns_every_positive_root():
    """Both roots come back, smallest first.

    Both roots are positive when their product ``log(S/K) / (r + sigma^2/2)``
    is -- so an in-the-money call, where the strike sits below spot and ``d1``
    is positive. The real capture has 818 such rows. Searching a strike ladder
    rather than asserting one hand-picked contract, so the test keeps its
    meaning if the pricing constants move.
    """
    found = [
        _implied_year_candidates(
            spot=6000.0,
            strike=float(strike),
            sigma=0.9,
            delta_value=0.90,
            rate=0.042,
            right=OptionRight.CALL,
        )
        for strike in range(3000, 6000, 50)
    ]
    two_root = [candidates for candidates in found if len(candidates) == 2]
    assert two_root, "no two-root contract on the ladder"
    for candidates in two_root:
        assert all(years > 0 for years in candidates)
        assert candidates[0] < candidates[1]


def test_the_larger_root_is_rejected_when_it_cannot_be_that_date():
    """The reproduced row: true T 0.006161, roots 9.012970 and 0.006164.

    v2.1.23 returned the first positive root, which is the larger one, and
    reported nine years for a two-day option.
    """
    valuation = datetime(2026, 8, 10, 10, 1, 34, tzinfo=EASTERN)
    expiration = date(2026, 8, 12)
    years, outcome = _resolve_root(
        (0.006164, 9.012970),
        expiration=expiration,
        valuation=valuation,
        days_per_year=365.0,
    )
    assert outcome == "disambiguated"
    assert years == pytest.approx(0.006164)


def test_the_window_uses_only_independently_known_facts():
    """Start of the expiration date to the end of it. No assumed close."""
    valuation = datetime(2026, 8, 10, 10, 1, 34, tzinfo=EASTERN)
    low, high = _expiry_day_window(date(2026, 8, 12), valuation)
    assert high - low == pytest.approx(1.0)
    # A 16:00 close and a whole-calendar-day convention both sit inside it,
    # which is the point: the window discriminates roots without choosing
    # between the conventions being inferred.
    assert low < 2.0 < high
    assert low < 2.2489 < high


def test_two_possible_roots_stay_ambiguous():
    valuation = datetime(2026, 8, 10, 10, 1, 34, tzinfo=EASTERN)
    _years, outcome = _resolve_root(
        (1.7, 1.8),
        expiration=date(2026, 8, 12),
        valuation=valuation,
        days_per_year=1.0,
    )
    assert outcome == "ambiguous"


def test_no_possible_root_is_inconsistent():
    valuation = datetime(2026, 8, 10, 10, 1, 34, tzinfo=EASTERN)
    _years, outcome = _resolve_root(
        (50.0,),
        expiration=date(2026, 8, 12),
        valuation=valuation,
        days_per_year=365.0,
    )
    assert outcome == "inconsistent"


def test_the_corrected_capture_has_no_absurd_clock_spreads(tmp_path):
    """v2.1.23 passed this capture while carrying 2,737-day spreads."""
    report = certify_capture(write_capture(tmp_path / "cap", _corrected()))
    assert report.clock_readings
    assert max(reading.spread for reading in report.clock_readings) < 1.0
    assert report.roots.disambiguated_multi_root_rows > 0
    assert report.roots.ambiguous_root_rows == 0


# ---------------------------------------------------------------------------
# 3. Identifiability and adequacy
# ---------------------------------------------------------------------------


def test_identical_rate_hypotheses_are_not_identifiable(tmp_path):
    """``wire = 0`` makes decimal and percent the same computation.

    v2.1.23 reported ``DECIMAL_ANNUAL_RATE`` and a documentation conflict
    because decimal was first in the candidate tuple.
    """
    report = certify_capture(
        write_capture(tmp_path / "cap", SyntheticVendor(wire_rate_value=0.0))
    )
    assert report.decisions["RATE_UNITS"] == InferenceDecision.NOT_IDENTIFIABLE.value
    observation = report.ledger.for_dimension(BehaviorDimension.RATE_UNITS)
    assert observation.observed_value == ""
    assert observation.status.value == "UNRESOLVED"
    assert "RATE_UNITS" not in report.ledger.as_dict()["conflicts"]
    # The scores really are identical, which is why nothing separates them.
    assert len({s.delta_rmse for s in report.rate_scores}) == 1


def test_a_resolved_status_cannot_rest_on_an_unresolved_comparison():
    from src.adapters.thetadata.live_behavior import (
        EvidenceStatus,
        LiveBehaviorObservation,
        ObservationBasis,
    )

    with pytest.raises(ThetaDataProvenanceError, match="settled nothing"):
        LiveBehaviorObservation(
            dimension=BehaviorDimension.DAY_COUNT,
            status=EvidenceStatus.LIVE_ONLY,
            basis=ObservationBasis.LIVE_NUMERICAL_RECONSTRUCTION,
            observed_value="ACT_365",
            decision=InferenceDecision.AMBIGUOUS,
            capture=None,
        )


def test_too_few_rows_is_insufficient_data(tmp_path):
    vendor = _corrected(strikes=(5900, 6000, 6100), expirations=(date(2026, 8, 12),))
    report = certify_capture(write_capture(tmp_path / "cap", vendor))
    assert report.decisions["RATE_UNITS"] == InferenceDecision.INSUFFICIENT_DATA.value


def test_a_zero_spread_book_cannot_establish_the_iv_basis(tmp_path):
    """BID, ASK and the midpoint are the same number when the book is locked."""
    vendor = _corrected(quote_half_spread=0.0)
    report = certify_capture(write_capture(tmp_path / "cap", vendor))
    assert (
        report.decisions["IV_PRICE_BASIS"] == InferenceDecision.NOT_IDENTIFIABLE.value
    )
    observation = report.ledger.for_dimension(BehaviorDimension.IV_PRICE_BASIS)
    assert observation.observed_value == ""


# ---------------------------------------------------------------------------
# 4. The full joint grid
# ---------------------------------------------------------------------------


def test_the_whole_rate_day_count_grid_is_published(tmp_path):
    report = certify_capture(write_capture(tmp_path / "cap", _corrected()))
    grid = report.joint_grid
    assert len(grid) == 8
    assert {entry["rate_interpretation"] for entry in grid} == {
        "DECIMAL_ANNUAL_RATE",
        "PERCENT_ANNUAL_RATE",
    }
    assert {entry["day_count"] for entry in grid} == {
        "ACT/365",
        "ACT/365.25",
        "ACT/360",
        "ACT/252",
    }
    assert sum(1 for entry in grid if entry["selected"]) == 1
    for entry in grid:
        assert entry["rows"] > 0
        assert entry["delta_rmse"] >= 0.0


def test_the_grid_is_deterministic(tmp_path):
    root = write_capture(tmp_path / "cap", _corrected())
    assert certify_capture(root).joint_grid == certify_capture(root).joint_grid


# ---------------------------------------------------------------------------
# 5. Certification cannot authorize a trusted GEX
# ---------------------------------------------------------------------------


def test_a_clean_capture_still_cannot_authorize_trusted_gex(tmp_path):
    """The reproduced case: correct rate, no missing OI, and still not trusted.

    v2.1.23 returned ``not self.gex_blockers``, so a capture that cleared the
    two things this report happens to check came out ``trusted_for_gex = true``
    beside ``ADAPTER_CERTIFICATION_EVIDENCE`` -- the report contradicting its
    own docstring.
    """
    report = certify_capture(
        write_capture(tmp_path / "cap", _corrected(complete_open_interest=True))
    )
    assert report.open_interest.missing_count == 0
    assert report.rate_economics.effective_rate_matches_intended is True
    assert report.gex_blockers == ()
    assert report.analytical_readiness == "ADAPTER_CERTIFICATION_EVIDENCE"
    assert report.trusted_for_gex is False


# ---------------------------------------------------------------------------
# 6. Archive identity is computed, never claimed
# ---------------------------------------------------------------------------


def test_a_caller_digest_is_recorded_as_a_claim_not_an_identity(tmp_path):
    report = certify_capture(
        write_capture(tmp_path / "cap", _corrected()), archive_sha256="d" * 64
    )
    assert report.archive.provenance == "UNVERIFIED_EXTERNAL_ARCHIVE_DIGEST_CLAIM"
    assert report.archive.known is False
    assert report.archive_sha256 == ""


def _full_archive(root: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
    """A ZIP holding everything a recipient would need to re-verify."""
    with zipfile.ZipFile(target, "w") as bundle:
        bundle.write(root / "manifest.json", "capture/manifest.json")
        bundle.write(root / "run-intent.json", "capture/run-intent.json")
        for payload in sorted((root / "raw").iterdir()):
            bundle.write(payload, f"capture/raw/{payload.name}")
    return target


def test_a_complete_archive_becomes_the_capture_archive_identity(tmp_path):
    root = write_capture(tmp_path / "cap", _corrected())
    archive = _full_archive(root, tmp_path / "capture.zip")

    report = certify_capture(root, archive_path=archive)
    assert report.archive.bytes_hashed is True
    assert report.archive.contains_capture_manifest is True
    assert report.archive.matches_capture is True
    assert report.archive.known is True
    assert report.archive.payloads_verified == 5
    assert report.archive.sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert report.archive.byte_length == archive.stat().st_size
    # Only now does the digest reach the identity every observation carries.
    assert report.archive_sha256 == report.archive.sha256


def test_a_manifest_only_archive_is_not_the_capture_archive(tmp_path):
    """v2.1.24 called this a verified archive. A recipient could check nothing."""
    root = write_capture(tmp_path / "cap", _corrected())
    archive = tmp_path / "manifest-only.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(root / "manifest.json", "capture/manifest.json")

    report = certify_capture(root, archive_path=archive)
    assert report.archive.bytes_hashed is True
    assert report.archive.contains_capture_manifest is True
    assert report.archive.matches_capture is False
    assert report.archive.known is False
    assert report.archive_sha256 == ""
    assert any("run-intent" in r for r in report.archive.mismatch_reasons)
    assert any("raw payload" in r for r in report.archive.mismatch_reasons)


def test_an_archive_with_a_corrupted_payload_is_not_the_capture_archive(tmp_path):
    root = write_capture(tmp_path / "cap", _corrected())
    archive = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(root / "manifest.json", "capture/manifest.json")
        bundle.write(root / "run-intent.json", "capture/run-intent.json")
        for payload in sorted((root / "raw").iterdir()):
            body = payload.read_bytes()
            if "greeks" in payload.name:
                body += b"# appended\n"
            bundle.writestr(f"capture/raw/{payload.name}", body)

    report = certify_capture(root, archive_path=archive)
    assert report.archive.matches_capture is False
    assert report.archive.known is False
    assert any("hashes differently" in r for r in report.archive.mismatch_reasons)


def test_an_unrelated_archive_is_never_stamped_onto_observations(tmp_path):
    """The v2.1.24 defect exactly: a source ZIP hashed and adopted as identity."""
    root = write_capture(tmp_path / "cap", _corrected())
    other = write_capture(tmp_path / "other", SyntheticVendor())
    archive = _full_archive(other, tmp_path / "wrong.zip")

    report = certify_capture(root, archive_path=archive)
    assert report.archive.bytes_hashed is True
    assert report.archive.contains_capture_manifest is False
    assert report.archive.matches_capture is False
    assert report.archive.known is False
    assert report.archive_sha256 == ""
    for observation in report.ledger.observations:
        assert observation.capture is None or observation.capture.archive_sha256 == ""


# ---------------------------------------------------------------------------
# 7. Duplicate identities
# ---------------------------------------------------------------------------


def _duplicate_a_listing_row(root: pathlib.Path) -> None:
    listing = root / "raw" / "v3-option-list-contracts-quote.raw"
    lines = listing.read_text(encoding="utf-8").splitlines()
    lines.append(lines[1])
    body = ("\n".join(lines) + "\n").encode("utf-8")
    listing.write_bytes(body)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for record in manifest["records"]:
        if "list/contracts" in record["endpoint"]:
            record["payload_hash"] = hashlib.sha256(body).hexdigest()
            record["byte_length"] = len(body)
    manifest["manifest_hash"] = RawCaptureManifest.rebuilt_from(manifest).manifest_hash
    (root / "manifest.json").write_text(json.dumps(manifest, default=str), "utf-8")


def test_a_duplicated_listing_identity_blocks_the_strongest_state(tmp_path):
    """Set equality survives duplication; the strongest state must not."""
    root = write_capture(tmp_path / "cap", _corrected())
    _duplicate_a_listing_row(root)
    report = certify_capture(root)

    assert report.universe.duplicate_identity_count == 1
    assert (
        report.universe.state
        == "DEDICATED_CONTRACT_LIST_MATCHED_WITH_DUPLICATE_IDENTITIES"
    )
    listing = next(
        entry for entry in report.universe.identities if "list" in entry.endpoint
    )
    assert listing.row_count == listing.unique_identity_count + 1
    assert listing.duplicate_identity_hash


def test_every_endpoint_reports_rows_against_identities(tmp_path):
    report = certify_capture(write_capture(tmp_path / "cap", _corrected()))
    assert len(report.universe.identities) == 4
    for entry in report.universe.identities:
        assert entry.row_count == entry.unique_identity_count
        assert entry.duplicate_identity_count == 0


def test_a_fractional_open_interest_is_refused(tmp_path):
    """``int(float("12.5"))`` is 12, which is a different open interest."""
    root = write_capture(tmp_path / "cap", _corrected())
    path = root / "raw" / "v3-option-snapshot-open_interest.raw"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].rsplit(",", 1)[0] + ",12.5"
    body = ("\n".join(lines) + "\n").encode("utf-8")
    path.write_bytes(body)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for record in manifest["records"]:
        if "open_interest" in record["endpoint"]:
            record["payload_hash"] = hashlib.sha256(body).hexdigest()
            record["byte_length"] = len(body)
    manifest["manifest_hash"] = RawCaptureManifest.rebuilt_from(manifest).manifest_hash
    (root / "manifest.json").write_text(json.dumps(manifest, default=str), "utf-8")

    with pytest.raises(CaptureCertificationError, match="not an integer"):
        certify_capture(root)


# ---------------------------------------------------------------------------
# 8. Certification dimensions against pricing dimensions
# ---------------------------------------------------------------------------


def test_the_underlying_timestamp_is_its_own_dimension(tmp_path):
    report = certify_capture(write_capture(tmp_path / "cap", _corrected()))
    observation = report.ledger.for_dimension(BehaviorDimension.UNDERLYING_TIMESTAMP)
    assert observation is not None
    assert observation.observed_value == "GREEKS_RESPONSE_EMBEDDED_UNDERLYING_TIMESTAMP"
    assert observation.dimension.pricing_dimension == "UNDERLYING_TIMESTAMP"


def test_pricing_dimensions_are_reported_apart_from_behaviour_dimensions(tmp_path):
    """An empty behaviour list must not read as analytical completeness."""
    report = certify_capture(write_capture(tmp_path / "cap", _corrected()))
    data = report.as_dict()
    supported = data["pricing_dimensions_supported_by_evidence"]
    unresolved = data["pricing_dimensions_still_unresolved"]
    assert "UNDERLYING_TIMESTAMP" in supported
    # A correctly-approved capture *does* establish the economic rate, because
    # a bound intent and a resolved live reading are both present.
    assert "RISK_FREE_RATE" in supported
    assert "DIVIDEND_VALUE" in supported
    assert "MINIMUM_TIME_FLOOR" in supported
    # ... and the analytical layer still needs things this cannot give.
    assert "DIVIDEND_CONVENTION" in unresolved
    assert "SOLVER_VERSION" in unresolved
    assert not set(supported) & set(unresolved)


def test_universe_coverage_maps_to_no_pricing_dimension():
    """Coverage is not a pricing convention and must not satisfy one."""
    assert BehaviorDimension.CONTRACT_LIST_UNIVERSE.pricing_dimension == ""


def test_the_iv_documentary_claim_is_labelled_as_unbound(tmp_path):
    """A code constant must not masquerade as capture-time documentation."""
    report = certify_capture(write_capture(tmp_path / "cap", _corrected()))
    claims = report.as_dict()["unbound_documentary_claims"]
    assert claims
    claim = next(entry for entry in claims if entry["dimension"] == "IV_PRICE_BASIS")
    assert claim["origin"] == "REPOSITORY_CONSTANT_NOT_CAPTURE_BOUND"
    # And the live finding stands on its own rather than against that claim.
    observation = report.ledger.for_dimension(BehaviorDimension.IV_PRICE_BASIS)
    assert observation.documented_value == ""
    assert observation.status.value == "LIVE_ONLY"


def test_an_extraction_from_another_document_is_refused(tmp_path):
    """Repointing one extraction at a different document is refused.

    v2.1.24 caught this by comparing the extraction's ``document_sha256``
    against the bundle's. v2.1.25 catches it earlier and harder: the recorded
    extraction is rebuilt as a typed object first, and its ``extraction_hash``
    covers the document digest, so the edit contradicts the record's own hash
    before anything is re-derived.
    """
    root = write_capture(tmp_path / "cap", _corrected())
    rewrite_intent(
        root,
        lambda p: p["vendor_documentation"]["extractions"][0].update(
            document_sha256="9" * 64
        ),
    )
    with pytest.raises(CaptureCertificationError, match="could not be re-derived"):
        certify_capture(root)
