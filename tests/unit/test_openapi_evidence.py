"""The pinned vendor document, and what it is allowed to establish.

**Every test here fails against v2.1.17, and none of them touches the network.**
The document is read from the copy stored in this repository; the one test that
exercises a fetch failure uses a fake transport that raises.

The headline is the third one. v2.1.17 could bind a convention to a document
through two free-text fields, so a *genuine* SHA-256 of the real ThetaData
OpenAPI description would have carried the sentence "open interest settles
same-session" exactly as happily as the true one -- nothing ever opened the file
to check. A hash proves which bytes are held. It proves nothing about what was
read out of them unless the reading is itself derived from the bytes.
"""

from __future__ import annotations

import pathlib
import shutil
from datetime import UTC, date, datetime

import pytest

from src.adapters.thetadata.openapi_evidence import (
    OPENAPI_EXTRACTOR_VERSION,
    PRODUCTION_PINNED_DOCUMENT,
    DriftKind,
    ExtractionSpec,
    OpenApiExtractionError,
    endpoint_drift,
    load_vendor_documentation_bundle,
    production_bundle,
    repository_documentation_root,
    verified_settlement_artifact,
)
from src.adapters.thetadata.vendor_documentation import DocumentedRule
from tests.certification_fixtures import DOCUMENTED_SESSION

CAPTURE_CONFIG = "config/thetadata_capture.yaml"

#: A session the pinned rule covers. The document was retrieved on 2026-08-06
#: and says nothing about when the convention started, so its rule is in force
#: from that date forward and no earlier.
COVERED_SESSION = date(2026, 8, 6)


@pytest.fixture
def documentation_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """A private copy of the pinned document, so a test may corrupt it."""
    source = (
        repository_documentation_root() / PRODUCTION_PINNED_DOCUMENT.content_location
    )
    target = tmp_path / PRODUCTION_PINNED_DOCUMENT.content_location
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return tmp_path


# =============================================================================
# §1 -- the exact bytes, or nothing
# =============================================================================


def test_the_official_document_is_pinned_by_its_exact_response_bytes():
    """**Regression 1.** v2.1.17 concluded these bytes were unobtainable.

    They are served publicly at ``docs.thetadata.us``. The digest is over the
    response body: the stored file rehashes to the pinned value, and the pin
    records the status, content type and length the fetch actually carried.
    """
    bundle = production_bundle()
    document = bundle.document

    assert document.source_url == "https://docs.thetadata.us/openapiv3.yaml"
    assert document.http_status == 200
    assert document.byte_length == 812792
    assert len(document.document_sha256) == 64
    assert document.document_schema_version == "3.1.0"
    # Content-addressed: the location is the digest, so a file that stops
    # hashing to its own name has stopped being the document it claims to be.
    assert document.document_sha256 in document.content_location
    # And reading it re-verifies rather than trusting the filename.
    assert len(document.read_bytes(repository_documentation_root())) == 812792


def test_a_truncated_document_is_refused_rather_than_parsed(documentation_root):
    """A partial read hashes cleanly as a shorter document. It must not pass."""
    held = documentation_root / PRODUCTION_PINNED_DOCUMENT.content_location
    held.write_bytes(held.read_bytes()[: 400 * 1024])

    with pytest.raises(OpenApiExtractionError, match=r"(?i)bytes|hashes"):
        load_vendor_documentation_bundle(root=documentation_root)


def test_a_parseable_document_with_no_paths_is_not_the_api_description(
    documentation_root,
):
    """Parsing is not enough. A YAML file is not an OpenAPI description."""
    held = documentation_root / PRODUCTION_PINNED_DOCUMENT.content_location
    held.write_bytes(b"openapi: 3.1.0\ninfo: {}\n")

    with pytest.raises(OpenApiExtractionError):
        load_vendor_documentation_bundle(root=documentation_root)


def test_a_missing_document_refuses_instead_of_inventing_a_digest(tmp_path):
    with pytest.raises(OpenApiExtractionError, match=r"(?i)missing"):
        load_vendor_documentation_bundle(root=tmp_path)


def test_the_loader_makes_no_network_request(monkeypatch):
    """The bundle comes off disk. Nothing here opens a socket.

    Asserted by making every socket construction raise: a loader that reached
    for the network would fail loudly rather than quietly working on a machine
    that happens to be online.
    """
    import socket

    def refuse(*args, **kwargs):  # pragma: no cover - only runs on regression
        raise AssertionError("the documentation loader must not use the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    bundle = production_bundle()
    assert bundle.extraction_for(DocumentedRule.RATE_UNITS) is not None


# =============================================================================
# §2 -- a reading is a path into the document, not a sentence
# =============================================================================


def test_a_fabricated_statement_on_the_genuine_document_fails():
    """**Regression 2, and the point of the release.**

    The document hash is real. The claimed source fragment is not in the
    document. v2.1.17 had no way to notice, because the statement was an input
    rather than something read.
    """
    fabricated = ExtractionSpec(
        rule=DocumentedRule.OPEN_INTEREST_SETTLEMENT,
        yaml_path=("paths", "/option/snapshot/open_interest", "get", "description"),
        expected_source_fragment="open interest reflects the current trading session",
        normalizer="settlement_session/1",
    )
    with pytest.raises(OpenApiExtractionError, match=r"(?i)does not contain"):
        load_vendor_documentation_bundle(
            root=repository_documentation_root(), specs=(fabricated,)
        )


def test_an_extraction_path_that_no_longer_resolves_is_refused():
    """The vendor can reorganise the document without renaming anything."""
    moved = ExtractionSpec(
        rule=DocumentedRule.RATE_UNITS,
        yaml_path=("components", "parameters", "rate_units", "description"),
        expected_source_fragment="The interest rate, as a percent",
        normalizer="rate_units/1",
    )
    with pytest.raises(OpenApiExtractionError, match=r"(?i)nothing at"):
        load_vendor_documentation_bundle(
            root=repository_documentation_root(), specs=(moved,)
        )


def test_the_three_values_are_derived_from_the_document():
    """Each one read out of its declared path by a named normalizer."""
    from src.config.pipeline import RateUnit
    from src.domain.settlement import SettlementRuleKind

    bundle = production_bundle()
    assert (
        bundle.value_for(DocumentedRule.OPEN_INTEREST_SETTLEMENT)
        is SettlementRuleKind.PRIOR_TRADING_SESSION
    )
    assert bundle.value_for(DocumentedRule.RATE_UNITS) is RateUnit.PERCENT_ANNUAL_RATE
    assert bundle.value_for(DocumentedRule.MINIMUM_TIME_FLOOR) == 60

    for extraction in bundle.extractions:
        assert extraction.extractor_version == OPENAPI_EXTRACTOR_VERSION
        assert len(extraction.extraction_hash) == 64
        assert extraction.yaml_path  # a reading names where it was read


def test_an_extraction_hash_that_does_not_follow_from_its_fields_is_refused():
    """A hash a caller supplies is a claim, not a derivation."""
    from src.adapters.thetadata.openapi_evidence import OpenApiEvidenceExtraction

    genuine = production_bundle().extractions[0]
    with pytest.raises(OpenApiExtractionError, match=r"(?i)hash"):
        OpenApiEvidenceExtraction(
            rule=genuine.rule,
            document_sha256=genuine.document_sha256,
            yaml_path=genuine.yaml_path,
            expected_source_fragment=genuine.expected_source_fragment,
            normalized_value=genuine.normalized_value,
            normalizer=genuine.normalizer,
            extraction_hash="0" * 64,
        )


# =============================================================================
# §3 -- the bundle is immutable and rederived
# =============================================================================


def test_the_bundle_reverifies_against_the_bytes_rather_than_itself(
    documentation_root,
):
    """**Regression 3.** Comparing a bundle's fields against its own fields
    passes for every bundle. Verification re-extracts."""
    bundle = production_bundle()
    assert bundle.verify_against(documentation_root) == ()

    held = documentation_root / PRODUCTION_PINNED_DOCUMENT.content_location
    held.write_bytes(b"openapi: 3.1.0\npaths: {}\n")
    assert bundle.verify_against(documentation_root)


def test_a_bundle_cannot_mix_two_documents():
    from src.adapters.thetadata.openapi_evidence import VendorDocumentationBundle

    bundle = production_bundle()
    stranger = production_bundle().extractions[0]
    object.__setattr__(stranger, "document_sha256", "a" * 64)
    with pytest.raises(OpenApiExtractionError, match=r"(?i)bundle is one document"):
        VendorDocumentationBundle(document=bundle.document, extractions=(stranger,))


# =============================================================================
# §4 -- the operator opens a capture under a real settlement rule
# =============================================================================


def test_the_documented_settlement_rule_resolves_a_real_date():
    """**Regression 4.** v2.1.17's operator passed ``settlement_rule=None``."""
    from src.domain.settlement import SettlementRuleKind

    artifact = verified_settlement_artifact(
        production_bundle(), chain_session_date=COVERED_SESSION
    )
    assert artifact.normalized_rule.kind is SettlementRuleKind.PRIOR_TRADING_SESSION
    # 2026-08-06 is a Thursday, so the prior trading session is Wednesday.
    assert artifact.resolved_settlement_date == date(2026, 8, 5)
    # The artifact names the bytes it rests on.
    assert artifact.documentation_content_hash == (
        PRODUCTION_PINNED_DOCUMENT.document_sha256
    )
    assert artifact.evidence_kind.permits_trusted_calculation


def test_a_session_the_pinned_rule_does_not_cover_gets_no_authority():
    """The document says what the vendor does *now*.

    It carries no statement about when the convention began, so a March session
    read against an August document establishes nothing. Backdating the rule to
    make it resolve would be inventing coverage the source does not provide.
    """
    with pytest.raises(
        OpenApiExtractionError, match=r"(?i)does not cover|no settlement"
    ):
        verified_settlement_artifact(
            production_bundle(), chain_session_date=date(2026, 3, 17)
        )


def test_the_dry_run_reports_an_established_settlement_date(tmp_path):
    """The operator's own report, not a unit-level construction."""
    from src.tools.capture_thetadata_once import plan_capture

    report = plan_capture(
        CAPTURE_CONFIG, output=str(tmp_path / "capture"), as_of=DOCUMENTED_SESSION
    )
    documentation = report["vendor_documentation"]

    assert documentation["documentation_available"] is True
    assert documentation["settlement_evidence"] == "ESTABLISHED"
    assert documentation["settlement_rule"] == "PRIOR_TRADING_SESSION"
    assert documentation["resolved_open_interest_settlement_date"]
    assert documentation["source_url"] == "https://docs.thetadata.us/openapiv3.yaml"
    assert documentation["document_sha256"] == (
        PRODUCTION_PINNED_DOCUMENT.document_sha256
    )
    assert documentation["byte_length"] == 812792
    assert len(documentation["bundle_fingerprint"]) == 64
    assert documentation["rate_input_units"] == "PERCENT_ANNUAL_RATE"
    assert documentation["minimum_time_floor_minutes"] == 60

    # Still raw-only, and still not a dataset.
    assert report["capture_readiness"] == "READY_FOR_RAW_CAPTURE_ONLY"
    assert report["would_compute_trusted_gex"] is False
    # The standing requirements, and what is missing right now. Two fields
    # since v2.1.20: reporting the standing list under a name ending in
    # "blockers" made the same report say settlement was both ESTABLISHED and
    # a blocker.
    assert report["analytical_requirements"]
    assert report["actual_analytical_blockers"]
    settlement_blockers = [
        blocker
        for blocker in report["actual_analytical_blockers"]
        if "settlement" in blocker.lower()
    ]
    assert settlement_blockers == [], settlement_blockers


# =============================================================================
# §5 -- eight load-bearing unknowns become six
# =============================================================================


def test_the_dry_run_reports_exactly_six_remaining_unknowns(tmp_path):
    """**Regression 5.** ``RATE_UNITS`` and ``MINIMUM_TIME_FLOOR`` leave.

    The other six stay, because the document does not settle them and holding a
    document is not the same as it answering a question.
    """
    from src.tools.capture_thetadata_once import plan_capture

    report = plan_capture(
        CAPTURE_CONFIG, output=str(tmp_path / "capture"), as_of=DOCUMENTED_SESSION
    )
    remaining = report["vendor_documentation"]["remaining_documentation_unknowns"]

    assert sorted(remaining) == [
        "DAY_COUNT",
        "DIVIDEND_CONVENTION",
        "EXPIRATION_TIMESTAMP",
        "IV_PRICE_BASIS",
        "UNDERLYING_SOURCE",
        "UNDERLYING_TIMESTAMP",
    ]


# =============================================================================
# §6 -- percent and decimal are a conversion, not a disagreement
# =============================================================================


def test_four_point_two_percent_matches_a_local_decimal_rate():
    """**Regression 6.** v2.1.17 compared the unit tokens and said MISMATCHED.

    ``4.2 x 0.01 = 0.042``, which is what the local model prices with. The
    units differ; the rate does not.
    """
    from src.config.compatibility import CompatibilityStatus, PricingDimension
    from src.config.pipeline import (
        DocumentedVendorConventions,
        RateAssumption,
        RateUnit,
        check_rate_compatibility,
    )

    conventions = DocumentedVendorConventions.from_bundle(production_bundle())
    vendor = RateAssumption(
        source="configured", raw_value=4.2, unit=RateUnit.PERCENT_ANNUAL_RATE
    )
    local = RateAssumption(
        source="configured", raw_value=0.042, unit=RateUnit.DECIMAL_ANNUAL_RATE
    )
    assert vendor.normalization_factor == 0.01
    assert vendor.normalized == pytest.approx(0.042)

    report = check_rate_compatibility(
        vendor=vendor, local=local, documented_unit=conventions.rate_units
    )
    verdicts = {d.dimension: d.status for d in report.dimensions}
    assert verdicts[PricingDimension.RATE_UNITS] is CompatibilityStatus.MATCHED
    assert verdicts[PricingDimension.RISK_FREE_RATE] is CompatibilityStatus.MATCHED


def test_a_config_claiming_decimal_while_sending_four_point_two_is_refused():
    """Sending 4.2 to an API documented to read percents is sending 420%.

    The local model agreeing with itself is what makes this look fine, which is
    why the comparison has to be against the document.
    """
    from src.config.compatibility import CompatibilityStatus, PricingDimension
    from src.config.pipeline import (
        DocumentedVendorConventions,
        RateAssumption,
        RateUnit,
        check_rate_compatibility,
    )

    conventions = DocumentedVendorConventions.from_bundle(production_bundle())
    lying = RateAssumption(
        source="configured", raw_value=4.2, unit=RateUnit.DECIMAL_ANNUAL_RATE
    )
    report = check_rate_compatibility(
        vendor=lying, local=lying, documented_unit=conventions.rate_units
    )
    verdicts = {d.dimension: d.status for d in report.dimensions}
    assert verdicts[PricingDimension.RATE_UNITS] is CompatibilityStatus.MISMATCHED
    assert verdicts[PricingDimension.RISK_FREE_RATE] is CompatibilityStatus.MISMATCHED


def test_without_a_document_the_rate_units_stay_unknown():
    """Evidence is per session. A caller with none gets none."""
    from src.config.compatibility import CompatibilityStatus, PricingDimension
    from src.config.pipeline import (
        RateAssumption,
        RateUnit,
        check_rate_compatibility,
    )

    vendor = RateAssumption(
        source="configured", raw_value=4.2, unit=RateUnit.PERCENT_ANNUAL_RATE
    )
    local = RateAssumption(
        source="configured", raw_value=0.042, unit=RateUnit.DECIMAL_ANNUAL_RATE
    )
    report = check_rate_compatibility(vendor=vendor, local=local, documented_unit=None)
    verdicts = {d.dimension: d.status for d in report.dimensions}
    assert verdicts[PricingDimension.RATE_UNITS] is CompatibilityStatus.UNKNOWN


# =============================================================================
# §7 -- the bundle fingerprint is bound into the identities
# =============================================================================


def test_a_capture_under_one_bundle_does_not_verify_under_another():
    """**The §7 requirement, stated as two digests that must differ.**"""
    from src.adapters.transport import FakeTransport
    from src.config.pipeline import ThetaDataResearchPipeline
    from src.config.schema import load_config

    loaded = load_config(CAPTURE_CONFIG)
    with_document = ThetaDataResearchPipeline.from_loaded_config(
        loaded, transport=FakeTransport()
    )
    without = ThetaDataResearchPipeline.from_config(
        loaded.thetadata,
        model_spec=loaded.engine.model_spec,
        engine_config=loaded.engine,
        transport=FakeTransport(),
        documentation_bundle=None,
    )

    assert with_document.documentation_fingerprint
    assert without.documentation_fingerprint == ""
    assert with_document.fingerprint() != without.fingerprint()

    moment = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    assert (
        with_document.normalization_recipe(as_of=moment).recipe_hash
        != without.normalization_recipe(as_of=moment).recipe_hash
    )


def test_the_recipe_carries_the_bundle_fingerprint():
    from src.adapters.transport import FakeTransport
    from src.config.pipeline import ThetaDataResearchPipeline
    from src.config.schema import load_config

    pipeline = ThetaDataResearchPipeline.from_loaded_config(
        load_config(CAPTURE_CONFIG), transport=FakeTransport()
    )
    recipe = pipeline.normalization_recipe(
        as_of=datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    )
    assert recipe.documentation_bundle_fingerprint == (production_bundle().bundle_hash)


# =============================================================================
# §8 -- the five first-session endpoints, checked against the document
# =============================================================================


def test_no_endpoint_drift_against_the_pinned_document():
    """Tier and CSV fields, for all five, read out of the document."""
    assert endpoint_drift(root=repository_documentation_root()) == ()


def test_a_modelled_tier_below_the_documented_one_is_a_conflict(monkeypatch):
    """The check has to be able to fail, or it is decoration."""
    from src.adapters.thetadata import endpoints as endpoint_module

    patched = dict(endpoint_module.MINIMUM_TIER)
    patched[endpoint_module.Endpoint.INDEX_PRICE_SNAPSHOT] = endpoint_module.Tier.VALUE
    monkeypatch.setattr(endpoint_module, "MINIMUM_TIER", patched)

    findings = endpoint_drift(root=repository_documentation_root())
    assert findings
    conflict = findings[0]
    assert conflict.kind is DriftKind.TIER_CONFLICT
    assert conflict.endpoint == "/v3/index/snapshot/price"
    assert conflict.blocks_capture
    # The document path, named, so an operator knows where to look.
    assert conflict.document_path == "/index/snapshot/price"


def test_a_modelled_column_the_document_does_not_carry_is_a_conflict(monkeypatch):
    """The v2.1.16 index defect, as a check that runs before the session."""
    from src.adapters.thetadata import endpoints as endpoint_module

    patched = dict(endpoint_module.RESPONSE_FIELDS)
    patched[endpoint_module.Endpoint.INDEX_PRICE_SNAPSHOT] = (
        "timestamp",
        "symbol",
        "index_price",
    )
    monkeypatch.setattr(endpoint_module, "RESPONSE_FIELDS", patched)

    findings = endpoint_drift(root=repository_documentation_root())
    assert any(f.kind is DriftKind.FIELDS_CONFLICT for f in findings)
    detail = next(f for f in findings if f.kind is DriftKind.FIELDS_CONFLICT).detail
    assert "index_price" in detail
    assert "price" in detail


def test_the_document_path_mapping_comes_from_the_document(monkeypatch):
    """``/v3`` is the server base path, read out of ``servers``, not assumed.

    The document's own paths carry no prefix; the local ``Endpoint`` values do.
    Which of the two is right about it is a question the document answers about
    itself.
    """
    import yaml

    from src.adapters.thetadata.openapi_evidence import _server_base_path

    parsed = yaml.safe_load(
        PRODUCTION_PINNED_DOCUMENT.read_bytes(repository_documentation_root()).decode(
            "utf-8"
        )
    )
    assert _server_base_path(parsed) == "/v3"
    # And the contract list is templated, so a literal comparison would report
    # an endpoint the document describes perfectly well as absent.
    assert "/option/list/contracts/{request_type}" in parsed["paths"]
    assert "/option/list/contracts/quote" not in parsed["paths"]


# =============================================================================
# §10 -- the stale statements are gone
# =============================================================================


def test_no_module_still_claims_the_documentation_is_unobtainable():
    """v2.1.17's reasons are removed, not merely contradicted elsewhere."""
    root = pathlib.Path(__file__).resolve().parents[2]
    for name in (
        "src/adapters/thetadata/vendor_documentation.py",
        "docs/VALIDATION.md",
    ):
        text = (root / name).read_text(encoding="utf-8")
        lowered = text.lower()
        assert "return 404" not in lowered, name
        assert "the production registry is empty" not in lowered, name


def test_the_three_kinds_of_request_stay_distinguished():
    """Public documentation, paid market data, Theta Terminal.

    Only the first happened while building this release, and the distinction
    has to survive in the prose as well as in the code.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    text = (root / "src/adapters/thetadata/openapi_evidence.py").read_text(
        encoding="utf-8"
    )
    assert "docs.thetadata.us" in text
    assert "no paid market data" in text.lower()
    assert "theta terminal" in text.lower()
