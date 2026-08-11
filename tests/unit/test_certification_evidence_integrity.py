"""The v2.1.24 evidence-authority defects, each one held shut.

Every test here fails on v2.1.24. They are grouped by the thing that was wrong
rather than by the code that was changed, because the code moved and the
defects are what has to stay fixed.

The common shape of all of them: v2.1.24 checked that a record was *internally
consistent* and treated that as proof the record was *true*. An extraction
whose digest matched its bundle, an archive whose bytes hashed, a rate intent
whose fields agreed with each other, an open-interest response whose rows
parsed -- each was self-consistent, and each could still be describing
something other than the capture it was filed under.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import zipfile

import pytest

from src.adapters.errors import ThetaDataProvenanceError
from src.adapters.raw_store import RawCaptureManifest
from src.adapters.thetadata.capture_certification import (
    AdmissibleModels,
    CaptureCertificationError,
    OpenInterestCoverage,
    _admissible,
    _collapse,
    certify_capture,
)
from src.adapters.thetadata.live_behavior import (
    CAPTURE_RATE_INTENT_SCHEMA_VERSION,
    CaptureRateIntent,
    InferenceDecision,
)
from src.tools.certify_thetadata_capture import EXIT_CONFLICT
from src.tools.certify_thetadata_capture import main as certify_main
from tests.synthetic_capture import (
    SyntheticVendor,
    rewrite_intent,
    write_capture,
)


def _corrected(**overrides) -> SyntheticVendor:
    """The planned second capture: wire 0.042, read as a decimal."""
    return SyntheticVendor(
        wire_rate_value=0.042, declared_economic_rate=0.042, **overrides
    )


def _legacy(**overrides) -> SyntheticVendor:
    """The first capture's shape: wire 4.2 read as a decimal, no bound intent."""
    return SyntheticVendor(wire_rate_value=4.2, **overrides)


def _restamp(root: pathlib.Path, endpoint_fragment: str, body: bytes) -> None:
    """Rewrite one raw payload and repair the manifest around it.

    The manifest is made *consistent* deliberately: the point of these tests is
    what certification concludes from a coherent capture, not whether it
    notices a broken digest, which is covered elsewhere.
    """
    name = {
        "open_interest": "v3-option-snapshot-open_interest.raw",
    }[endpoint_fragment]
    (root / "raw" / name).write_bytes(body)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for record in manifest["records"]:
        if endpoint_fragment in record["endpoint"]:
            record["payload_hash"] = hashlib.sha256(body).hexdigest()
            record["byte_length"] = len(body)
    manifest["manifest_hash"] = RawCaptureManifest.rebuilt_from(manifest).manifest_hash
    (root / "manifest.json").write_text(json.dumps(manifest, default=str), "utf-8")


def _add_unexpected_oi_row(root: pathlib.Path) -> None:
    """An open-interest row for a contract the listing never named."""
    payload = root / "raw" / "v3-option-snapshot-open_interest.raw"
    lines = payload.read_text(encoding="utf-8").splitlines()
    # A strike far outside the generated ladder, so it cannot collide.
    lines.append("SPXW,2026-08-12,99999.000,CALL,500")
    _restamp(root, "open_interest", ("\n".join(lines) + "\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# 1. Documentary readings are re-derived, not believed
# ---------------------------------------------------------------------------


def test_editing_a_recorded_reading_no_longer_rewrites_history(tmp_path):
    """The v2.1.24 defect, on a legacy capture.

    Flipping ``RATE_UNITS`` from percent to decimal in ``run-intent.json`` moved
    the first capture's recovered intent from 0.042 to 4.2 and made it
    economically valid, touching no payload, no manifest and no digest.
    """
    root = write_capture(tmp_path / "cap", _legacy())
    before = certify_capture(root)
    assert before.rate_economics.intended_economic_rate == pytest.approx(0.042)
    assert before.rate_economics.effective_rate_matches_intended is False

    rewrite_intent(
        root,
        lambda p: p["vendor_documentation"]["extractions"][1].update(
            normalized_value="DECIMAL_ANNUAL_RATE"
        ),
    )
    with pytest.raises(CaptureCertificationError, match="could not be re-derived"):
        certify_capture(root)


def test_a_reading_edited_with_its_hash_recomputed_is_still_refused(tmp_path):
    """Recomputing the extraction hash does not make the document say it.

    This is the tamper that survives every *internal* consistency check: the
    record agrees with itself perfectly. It is caught by re-reading the
    document, which is the only thing that was ever evidence.
    """
    from src.adapters.thetadata.openapi_evidence import OpenApiEvidenceExtraction
    from src.adapters.thetadata.vendor_documentation import DocumentedRule

    root = write_capture(tmp_path / "cap", _legacy())

    def _forge(payload):
        entry = payload["vendor_documentation"]["extractions"][1]
        assert entry["rule"] == "RATE_UNITS"
        forged = OpenApiEvidenceExtraction(
            rule=DocumentedRule.RATE_UNITS,
            document_sha256=entry["document_sha256"],
            yaml_path=tuple(entry["yaml_path"]),
            expected_source_fragment=entry["expected_source_fragment"],
            normalized_value="DECIMAL_ANNUAL_RATE",
            normalizer=entry["normalizer"],
            extractor_version=entry["extractor_version"],
            schema_version=entry["schema_version"],
        )
        payload["vendor_documentation"]["extractions"][1] = forged.as_dict()

    rewrite_intent(root, _forge)
    with pytest.raises(
        CaptureCertificationError, match="edited away from the document"
    ):
        certify_capture(root)


def test_a_bound_captures_conflict_cannot_be_edited_away(tmp_path):
    """Same edit against a v2.1.24+ capture: the conflict must survive it."""
    root = write_capture(tmp_path / "cap", _corrected())
    assert certify_capture(root).rate_economics.documentation_live_conflict is True

    rewrite_intent(
        root,
        lambda p: p["vendor_documentation"]["extractions"][1].update(
            normalized_value="DECIMAL_ANNUAL_RATE"
        ),
    )
    with pytest.raises(CaptureCertificationError, match="could not be re-derived"):
        certify_capture(root)


def test_documentation_not_bound_by_an_approval_is_refused(tmp_path):
    """A legacy capture, so the rate-intent binding is not what refuses it.

    On a v2.1.24+ capture the missing approval trips the intent check first,
    which is a different defect being caught. This one has no declared intent,
    so the documentation is the only thing hanging off the approval.
    """
    root = write_capture(tmp_path / "cap", _legacy())
    rewrite_intent(root, lambda p: p.pop("preflight_approval"))
    with pytest.raises(CaptureCertificationError, match="no preflight approval binds"):
        certify_capture(root)


def test_a_bundle_the_approval_never_covered_is_refused(tmp_path):
    root = write_capture(tmp_path / "cap", _corrected())
    rewrite_intent(
        root, lambda p: p["vendor_documentation"].update(bundle_fingerprint="7" * 64)
    )
    with pytest.raises(CaptureCertificationError, match="not the readings the capture"):
        certify_capture(root)


def test_a_document_this_checkout_does_not_hold_is_refused(tmp_path):
    root = write_capture(tmp_path / "cap", _corrected())
    for stored in (root / "vendor_documentation").rglob("*.yaml"):
        stored.unlink()
    with pytest.raises(CaptureCertificationError, match="does not hold"):
        certify_capture(root)


def test_the_documented_unit_comes_from_the_document(tmp_path):
    """A vendor documenting a decimal produces no conflict, from its own bytes."""
    root = write_capture(
        tmp_path / "cap", _corrected(documented_rate_unit="DECIMAL_ANNUAL_RATE")
    )
    report = certify_capture(root)
    assert report.rate_economics.documented_unit == "DECIMAL_ANNUAL_RATE"
    assert report.rate_economics.documentation_live_conflict is False
    assert report.documentation["documentary_authority"] == (
        "REDERIVED_FROM_PINNED_DOCUMENT_BYTES"
    )


def test_a_bound_intent_disagreeing_with_the_document_is_refused(tmp_path):
    """The intent's documented unit is bound; it must still match the document."""
    root = write_capture(tmp_path / "cap", _corrected())

    def _swap(payload):
        payload["rate_semantics"]["documented_rate_unit"] = "DECIMAL_ANNUAL_RATE"
        intent = CaptureRateIntent.from_payload(payload["rate_semantics"])
        payload["preflight_approval"]["rate_intent_fingerprint"] = intent.fingerprint
        from src.adapters.thetadata.capture_certification import (
            _recomputed_approval_hash,
        )

        payload["preflight_approval"]["approval_hash"] = _recomputed_approval_hash(
            payload["preflight_approval"]
        )
        return payload["preflight_approval"]["approval_hash"]

    stored: dict[str, str] = {}
    rewrite_intent(root, lambda p: stored.update(hash=_swap(p)))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for record in manifest["records"]:
        record["preflight_approval_hash"] = stored["hash"]
    manifest["manifest_hash"] = RawCaptureManifest.rebuilt_from(manifest).manifest_hash
    (root / "manifest.json").write_text(json.dumps(manifest, default=str), "utf-8")

    with pytest.raises(CaptureCertificationError, match="do not agree about what"):
        certify_capture(root)


# ---------------------------------------------------------------------------
# 2. The dividend convention is not documentary evidence
# ---------------------------------------------------------------------------


def test_the_dividend_convention_is_not_reported_as_documented(tmp_path):
    report = certify_capture(write_capture(tmp_path / "cap", _corrected()))
    observation = report.ledger.for_dimension(
        __import__(
            "src.adapters.thetadata.live_behavior", fromlist=["BehaviorDimension"]
        ).BehaviorDimension.DIVIDEND_CONVENTION
    )
    assert observation.status.value == "UNRESOLVED"
    assert observation.documented_value == ""
    assert observation.documentation_matched is False
    claims = {c["dimension"] for c in report.as_dict()["unbound_documentary_claims"]}
    assert "DIVIDEND_CONVENTION" in claims


def test_the_dividend_value_is_request_bound_evidence(tmp_path):
    """The amount is proved even though its interpretation is not."""
    report = certify_capture(write_capture(tmp_path / "cap", _corrected()))
    data = report.as_dict()
    assert "DIVIDEND_VALUE" in data["pricing_dimensions_supported_by_evidence"]
    assert "DIVIDEND_CONVENTION" in data["pricing_dimensions_still_unresolved"]
    assert data["capture_request"]["greeks_annual_dividend"] == 0.0


# ---------------------------------------------------------------------------
# 3. An unresolved rate leaves the economics indeterminate
# ---------------------------------------------------------------------------


def test_an_unidentifiable_rate_produces_no_economic_verdict(tmp_path):
    """``rate_value=0`` makes both readings the same computation.

    v2.1.24 still published an observed unit, an effective rate, a
    documentation conflict and an economic verdict -- four definite claims from
    a comparison the same report labelled as having settled nothing.
    """
    vendor = SyntheticVendor(wire_rate_value=0.0, declared_economic_rate=0.0)
    report = certify_capture(write_capture(tmp_path / "cap", vendor))

    assert report.decisions["RATE_UNITS"] == InferenceDecision.NOT_IDENTIFIABLE.value
    economics = report.rate_economics
    assert economics.vendor_rate_identified is False
    assert economics.vendor_effective_rate is None
    assert economics.documentation_live_conflict is None
    assert economics.effective_rate_matches_intended is None
    assert economics.magnitude_ratio is None
    assert economics.observed_unit == ""
    # And it is named as a blocker rather than passing quietly.
    assert any("not identified" in b for b in report.gex_blockers)


def test_an_unresolved_rate_says_nothing_about_agreeing_with_the_document(tmp_path):
    """The prose must not claim agreement it did not establish."""
    from src.adapters.thetadata.live_behavior import BehaviorDimension

    vendor = SyntheticVendor(wire_rate_value=0.0, declared_economic_rate=0.0)
    report = certify_capture(write_capture(tmp_path / "cap", vendor))
    observation = report.ledger.for_dimension(BehaviorDimension.RATE_UNITS)
    assert "reads rate_value as documented" not in observation.notes
    assert "does not establish" in observation.notes
    assert observation.observed_value == ""


def test_the_json_reports_unknown_as_null_not_false(tmp_path):
    """A Boolean false would read as a finding. It has to be absent."""
    vendor = SyntheticVendor(wire_rate_value=0.0, declared_economic_rate=0.0)
    report = certify_capture(write_capture(tmp_path / "cap", vendor))
    economics = report.as_dict()["rate_economics"]
    assert economics["rate_units_documentation_live_conflict"] is None
    assert economics["capture_effective_rate_matches_intended_rate"] is None
    assert economics["vendor_rate_identified"] is False


# ---------------------------------------------------------------------------
# 4. Downstream conclusions never rest on an unresolved upstream winner
# ---------------------------------------------------------------------------


def test_an_unresolved_rate_keeps_every_admissible_model(tmp_path):
    vendor = SyntheticVendor(wire_rate_value=0.0, declared_economic_rate=0.0)
    report = certify_capture(write_capture(tmp_path / "cap", vendor))
    models = report.as_dict()["inference_models"]
    assert models["admissible_model_count"] == 2
    assert models["resolved_selected"] is None
    # The minimum still exists and is still published -- as a best fit, which
    # is a different claim from a selected model.
    assert models["numerical_best"]["rate_interpretation"]


def test_downstream_resolves_when_every_admissible_path_agrees(tmp_path):
    """Rate unidentifiable, clock still established, because r=0 either way."""
    vendor = SyntheticVendor(wire_rate_value=0.0, declared_economic_rate=0.0)
    report = certify_capture(write_capture(tmp_path / "cap", vendor))
    assert report.decisions["RATE_UNITS"] == InferenceDecision.NOT_IDENTIFIABLE.value
    assert report.decisions["EXPIRATION_TIMESTAMP"] == InferenceDecision.RESOLVED.value
    assert report.resolved_expiration_clock == "16:00 America/New_York"


def test_a_resolved_rate_pins_its_own_coordinate():
    grid = {
        ("DECIMAL_ANNUAL_RATE", "ACT/365"): object(),
        ("DECIMAL_ANNUAL_RATE", "ACT/360"): object(),
        ("PERCENT_ANNUAL_RATE", "ACT/365"): object(),
        ("PERCENT_ANNUAL_RATE", "ACT/360"): object(),
    }
    best = ("DECIMAL_ANNUAL_RATE", "ACT/365")
    both = _admissible(
        grid,
        best=best,
        rate_decision=InferenceDecision.RESOLVED,
        day_count_decision=InferenceDecision.RESOLVED,
    )
    assert both.keys == (best,)
    assert both.is_unique

    rate_open = _admissible(
        grid,
        best=best,
        rate_decision=InferenceDecision.AMBIGUOUS,
        day_count_decision=InferenceDecision.RESOLVED,
    )
    assert set(rate_open.keys) == {
        ("DECIMAL_ANNUAL_RATE", "ACT/365"),
        ("PERCENT_ANNUAL_RATE", "ACT/365"),
    }

    both_open = _admissible(
        grid,
        best=best,
        rate_decision=InferenceDecision.AMBIGUOUS,
        day_count_decision=InferenceDecision.AMBIGUOUS,
    )
    assert len(both_open.keys) == 4
    assert both_open.as_dict()["resolved_selected"] is None


def test_downstream_agreement_rules():
    """The four §4 cases, at the level the rule is written.

    A capture whose admissible paths genuinely disagree downstream cannot be
    manufactured honestly -- the paths surviving an undiscriminated rate are
    identical computations -- so the collapse is exercised directly rather
    than through a capture that would have to be rigged to reach it.
    """
    resolved = InferenceDecision.RESOLVED
    # Upstream open, downstream invariant across the paths -> established.
    assert _collapse(((resolved, "16:00"), (resolved, "16:00"))) == (resolved, "16:00")
    # Upstream open, downstream differs -> the answer was a function of the
    # upstream choice, and nothing chose.
    assert _collapse(((resolved, "16:00"), (resolved, "15:30"))) == (
        InferenceDecision.AMBIGUOUS,
        "",
    )
    # One path could not settle it: propagated by name, not flattened.
    assert _collapse(((resolved, "16:00"), (InferenceDecision.AMBIGUOUS, ""))) == (
        InferenceDecision.AMBIGUOUS,
        "",
    )
    assert _collapse(
        ((resolved, "16:00"), (InferenceDecision.NO_ADEQUATE_FIT, ""))
    ) == (InferenceDecision.NO_ADEQUATE_FIT, "")
    assert _collapse(((InferenceDecision.INSUFFICIENT_DATA, ""),)) == (
        InferenceDecision.INSUFFICIENT_DATA,
        "",
    )
    assert _collapse(()) == (InferenceDecision.INSUFFICIENT_DATA, "")


def test_an_unresolved_downstream_dimension_publishes_no_value(tmp_path):
    """The label is the decision's name, never the best of an undecided set."""
    assert _collapse(((InferenceDecision.RESOLVED, "16:00"),))[1] == "16:00"
    assert _collapse(((InferenceDecision.AMBIGUOUS, ""),))[1] == ""


# ---------------------------------------------------------------------------
# 5. Open-interest coverage is closed over the expected universe
# ---------------------------------------------------------------------------


def test_an_unexpected_open_interest_identity_is_reported(tmp_path):
    """v2.1.24 gave 567 answers for a 566-contract universe a ratio of 1.0018."""
    root = write_capture(tmp_path / "cap", _corrected(complete_open_interest=True))
    assert certify_capture(root).open_interest.coverage_ratio == 1.0

    _add_unexpected_oi_row(root)
    coverage = certify_capture(root).open_interest
    assert coverage.unexpected_count == 1
    assert coverage.coverage_ratio <= 1.0
    assert coverage.permits_trusted_aggregate is False
    assert coverage.state == "OI_UNEXPECTED"
    assert len(coverage.unexpected_identities_hash) == 64
    assert coverage.unexpected_by_expiration == (("2026-08-12", 1),)


def test_an_unexpected_identity_blocks_a_trusted_aggregate(tmp_path):
    root = write_capture(tmp_path / "cap", _corrected(complete_open_interest=True))
    _add_unexpected_oi_row(root)
    report = certify_capture(root)
    assert any("not in the expected universe" in b for b in report.gex_blockers)


def test_the_coverage_ratio_is_bounded_by_definition():
    """Not by clipping an invalid calculation afterwards."""
    coverage = OpenInterestCoverage(
        universe_count=100,
        present_count=140,
        explicit_zero_count=20,
        missing_count=0,
        missing_by_expiration=(),
        missing_identities_hash="",
        fully_missing_expirations=(),
        unexpected_count=60,
    )
    assert coverage.covered_count == 100
    assert coverage.coverage_ratio == 1.0
    assert coverage.permits_trusted_aggregate is False
    assert coverage.state == "OI_UNEXPECTED"


def test_a_complete_capture_reports_a_matched_universe(tmp_path):
    root = write_capture(tmp_path / "cap", _corrected(complete_open_interest=True))
    coverage = certify_capture(root).open_interest
    assert coverage.state == "OI_MATCHES_EXPECTED_UNIVERSE"
    assert coverage.permits_trusted_aggregate is True
    # ... which still does not authorize a GEX.
    assert certify_capture(root).trusted_for_gex is False


# ---------------------------------------------------------------------------
# 6. The CLI can verify an archive
# ---------------------------------------------------------------------------


def test_the_cli_accepts_an_archive_path(tmp_path, capsys):
    root = write_capture(tmp_path / "cap", _corrected())
    archive = tmp_path / "capture.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(root / "manifest.json", "capture/manifest.json")
        bundle.write(root / "run-intent.json", "capture/run-intent.json")
        for payload in sorted((root / "raw").iterdir()):
            bundle.write(payload, f"capture/raw/{payload.name}")

    report_path = tmp_path / "report.json"
    code = certify_main(
        [str(root), "--archive-path", str(archive), "--json", str(report_path)]
    )
    # 3, not 0: this vendor documents a percent and reads a decimal, which is
    # the documentation/live conflict exit code. Certified, and conflicting.
    assert code == EXIT_CONFLICT
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["capture"]["archive_identity_known"] is True
    assert payload["capture"]["archive_matches_capture"] is True
    assert payload["capture"]["archive_sha256"] == (
        hashlib.sha256(archive.read_bytes()).hexdigest()
    )


def test_an_archive_written_with_backslash_entries_still_matches(tmp_path):
    """The real first capture's archive is like this, and nothing generated it.

    The ZIP specification says forward slashes and ``zipfile.write`` produces
    them, so every synthetic archive in this suite agreed with the lookup by
    construction. The archive an operator actually made on Windows holds
    ``raw\\name``, and against a path joined with ``/`` that resolves to
    nothing -- reporting a complete archive as missing every payload.
    """
    root = write_capture(tmp_path / "cap", _corrected())
    archive = tmp_path / "backslashes.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manifest.json", (root / "manifest.json").read_bytes())
        bundle.writestr("run-intent.json", (root / "run-intent.json").read_bytes())
        for payload in sorted((root / "raw").iterdir()):
            bundle.writestr(f"raw\\{payload.name}", payload.read_bytes())

    report = certify_capture(root, archive_path=archive)
    assert report.archive.matches_capture is True
    assert report.archive.payloads_verified == 5
    assert report.archive.known is True


def test_the_cli_records_a_naked_digest_as_an_unverified_claim(tmp_path):
    root = write_capture(tmp_path / "cap", _corrected())
    report_path = tmp_path / "report.json"
    code = certify_main(
        [str(root), "--archive-sha256", "d" * 64, "--json", str(report_path)]
    )
    assert code == EXIT_CONFLICT
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["capture"]["archive_digest_provenance"] == (
        "UNVERIFIED_EXTERNAL_ARCHIVE_DIGEST_CLAIM"
    )
    assert payload["capture"]["archive_identity_known"] is False
    assert payload["capture"]["archive_matches_capture"] is False


# ---------------------------------------------------------------------------
# 7. The rate intent must name the rules it was written under
# ---------------------------------------------------------------------------


def test_a_rate_intent_without_a_schema_is_refused(tmp_path):
    """Deleting the field left the fingerprint reconstructing identically."""
    root = write_capture(tmp_path / "cap", _corrected())
    rewrite_intent(root, lambda p: p["rate_semantics"].pop("schema_version"))
    with pytest.raises(CaptureCertificationError, match="names no schema_version"):
        certify_capture(root)


def test_a_rate_intent_naming_an_unknown_schema_is_refused():
    with pytest.raises(ThetaDataProvenanceError, match="cannot read"):
        CaptureRateIntent(
            economic_rate_percent=4.2,
            economic_rate_decimal=0.042,
            local_model_rate=0.042,
            vendor_request_rate_value=0.042,
            vendor_observed_rate_unit="DECIMAL_ANNUAL_RATE",
            documented_rate_unit="PERCENT_ANNUAL_RATE",
            schema_version="pricing-evidence/9.9.9",
        )


def test_from_payload_requires_the_schema_field_explicitly():
    payload = {
        "economic_rate_percent": 4.2,
        "economic_rate_decimal": 0.042,
        "local_model_rate": 0.042,
        "vendor_request_rate_value": 0.042,
        "vendor_observed_rate_unit": "DECIMAL_ANNUAL_RATE",
        "documented_rate_unit": "PERCENT_ANNUAL_RATE",
    }
    with pytest.raises(ThetaDataProvenanceError, match="names no schema_version"):
        CaptureRateIntent.from_payload(payload)
    # ... and with it present, the capture-#2 fingerprint is unchanged.
    intent = CaptureRateIntent.from_payload(
        {**payload, "schema_version": CAPTURE_RATE_INTENT_SCHEMA_VERSION}
    )
    assert intent.fingerprint.startswith("e3e8aa4a1bd70464")


# ---------------------------------------------------------------------------
# 8. Evidence accounting does not become analytical authority
# ---------------------------------------------------------------------------


def test_a_capture_with_nothing_wrong_is_still_not_trusted(tmp_path):
    root = write_capture(tmp_path / "cap", _corrected(complete_open_interest=True))
    report = certify_capture(root)
    assert report.gex_blockers == ()
    assert report.trusted_for_gex is False
    assert report.analytical_readiness == "ADAPTER_CERTIFICATION_EVIDENCE"


def test_the_admissible_model_report_separates_best_from_selected():
    models = AdmissibleModels(
        keys=(("DECIMAL_ANNUAL_RATE", "ACT/365"),),
        numerical_best=("DECIMAL_ANNUAL_RATE", "ACT/365"),
    )
    payload = models.as_dict()
    assert payload["numerical_best"]["day_count"] == "ACT/365"
    assert payload["resolved_selected"]["day_count"] == "ACT/365"
    assert payload["admissible_model_count"] == 1
