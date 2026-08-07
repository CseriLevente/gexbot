"""Every way the documentation machinery refuses, exercised.

A refusal nobody tests is a refusal that might not fire. These are the paths
that only run when something is wrong: a document that is not a document, a
normalizer handed text that says the opposite of what it looks for, a spec that
names a reader nobody wrote.

Separated from ``test_openapi_evidence.py`` because that file is the *argument*
for the release and reads as one. This file is the closed set of ways it says
no.
"""

from __future__ import annotations

import hashlib
import pathlib
from datetime import UTC, datetime

import pytest

from src.adapters.thetadata.openapi_evidence import (
    PRODUCTION_PINNED_DOCUMENT,
    DriftKind,
    ExtractionSpec,
    OpenApiEvidenceExtraction,
    OpenApiExtractionError,
    PinnedDocument,
    VendorDocumentationBundle,
    endpoint_drift,
    load_vendor_documentation_bundle,
    production_bundle,
    repository_documentation_root,
    settlement_documentation_rule,
    verified_settlement_artifact,
)
from src.adapters.thetadata.vendor_documentation import (
    DocumentedRule,
    store_document,
)

RETRIEVED = datetime(2026, 8, 6, 14, 36, 13, tzinfo=UTC)
DIGEST = "a" * 64


def pinned(**overrides) -> PinnedDocument:
    fields = {
        "source_url": "https://docs.thetadata.us/openapiv3.yaml",
        "retrieved_at": RETRIEVED,
        "http_status": 200,
        "content_type": "application/octet-stream",
        "byte_length": 10,
        "document_sha256": DIGEST,
        "content_location": f"thetadata/aa/{DIGEST}.yaml",
        "document_schema_version": "3.1.0",
    }
    return PinnedDocument(**{**fields, **overrides})


# =============================================================================
# A pin that cannot be checked is a citation
# =============================================================================


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"document_sha256": "not-a-digest"}, r"(?i)full sha-256"),
        ({"document_sha256": "z" * 64}, r"(?i)full sha-256"),
        ({"http_status": 302}, r"(?i)answered 302"),
        ({"http_status": 404}, r"(?i)answered 404"),
        ({"byte_length": 0}, r"(?i)empty body"),
        ({"source_url": "http://docs.thetadata.us/openapiv3.yaml"}, r"(?i)https"),
    ],
)
def test_a_pin_that_cannot_be_trusted_is_refused(overrides, expected):
    """A redirect or an error page hashes just as well as a document."""
    with pytest.raises(OpenApiExtractionError, match=expected):
        pinned(**overrides)


def test_a_pin_naming_bytes_this_checkout_lacks_is_refused(tmp_path):
    with pytest.raises(OpenApiExtractionError, match=r"(?i)missing"):
        pinned().read_bytes(tmp_path)


def test_a_document_of_the_right_length_and_wrong_content_is_refused(tmp_path):
    """Length alone is not identity."""
    body = b"x" * 10
    document = pinned(document_sha256=hashlib.sha256(b"y" * 10).hexdigest())
    target = tmp_path / document.content_location
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)

    with pytest.raises(OpenApiExtractionError, match=r"(?i)hashes to"):
        document.read_bytes(tmp_path)


def test_the_pin_round_trips_as_a_dictionary():
    payload = PRODUCTION_PINNED_DOCUMENT.as_dict()
    assert payload["source_url"].startswith("https://")
    assert payload["byte_length"] == 812792
    assert payload["retrieved_at"].startswith("2026-08-06T")


# =============================================================================
# The normalizers read, and say so when they cannot
# =============================================================================


def extract_from(text: str, *, rule: DocumentedRule, normalizer: str, fragment: str):
    """Run one normalizer over synthetic text through the real spec path."""
    spec = ExtractionSpec(
        rule=rule,
        yaml_path=("info", "description"),
        expected_source_fragment=fragment,
        normalizer=normalizer,
    )
    return spec.extract({"info": {"description": text}}, document_sha256=DIGEST)


def test_a_description_naming_both_sessions_settles_nothing():
    """An ambiguous source is how a document comes to confirm whatever it was
    consulted to confirm."""
    with pytest.raises(
        OpenApiExtractionError, match=r"(?i)both a previous and a current"
    ):
        extract_from(
            "reflects the previous trading day, updated on the current trading day",
            rule=DocumentedRule.OPEN_INTEREST_SETTLEMENT,
            normalizer="settlement_session/1",
            fragment="reflects",
        )


def test_a_description_naming_no_session_settles_nothing():
    with pytest.raises(OpenApiExtractionError, match=r"(?i)no settlement session"):
        extract_from(
            "Retrieve the last open interest message of an option contract.",
            rule=DocumentedRule.OPEN_INTEREST_SETTLEMENT,
            normalizer="settlement_session/1",
            fragment="Retrieve",
        )


def test_a_same_session_description_normalizes_to_same_session():
    """The normalizer reads. It would answer differently for a different vendor."""
    from src.domain.settlement import SettlementRuleKind

    extraction = extract_from(
        "Open interest for the current trading day, with intraday updates.",
        rule=DocumentedRule.OPEN_INTEREST_SETTLEMENT,
        normalizer="settlement_session/1",
        fragment="current trading day",
    )
    assert extraction.normalized_value is SettlementRuleKind.SAME_SESSION


def test_a_decimal_rate_description_normalizes_to_decimal():
    from src.config.pipeline import RateUnit

    extraction = extract_from(
        "The interest rate, as a decimal, to be used in a Greeks calculation.",
        rule=DocumentedRule.RATE_UNITS,
        normalizer="rate_units/1",
        fragment="as a decimal",
    )
    assert extraction.normalized_value is RateUnit.DECIMAL_ANNUAL_RATE


@pytest.mark.parametrize(
    ("text", "fragment", "expected"),
    [
        (
            "The rate, as a percent or as a decimal, whichever you prefer.",
            "The rate",
            r"(?i)both percent and decimal",
        ),
        ("The interest rate to use.", "interest rate", r"(?i)no rate unit"),
    ],
)
def test_an_unreadable_rate_description_settles_nothing(text, fragment, expected):
    """Guessing here is a factor of one hundred in every gamma."""
    with pytest.raises(OpenApiExtractionError, match=expected):
        extract_from(
            text,
            rule=DocumentedRule.RATE_UNITS,
            normalizer="rate_units/1",
            fragment=fragment,
        )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("uses real TTE with no stated floor", r"(?i)no minimum time"),
        ("down to a minimum of 0 minutes", r"(?i)not a floor"),
    ],
)
def test_an_unreadable_time_floor_settles_nothing(text, expected):
    with pytest.raises(OpenApiExtractionError, match=expected):
        extract_from(
            text,
            rule=DocumentedRule.MINIMUM_TIME_FLOOR,
            normalizer="minimum_time_floor_minutes/1",
            fragment=text[:10],
        )


def test_a_fractional_floor_is_refused_rather_than_rounded():
    """The model spec carries integer minutes. A fractional floor is a
    different quantity wearing the same name."""
    with pytest.raises(OpenApiExtractionError, match=r"(?i)whole number of minutes"):
        extract_from(
            "down to a minimum of 1.5 minutes",
            rule=DocumentedRule.MINIMUM_TIME_FLOOR,
            normalizer="minimum_time_floor_minutes/1",
            fragment="down to a minimum",
        )


@pytest.mark.parametrize(
    ("text", "minutes"),
    [
        ("down to a minimum of 30 minutes", 30),
        ("down to a minimum of 2 hours", 120),
        ("down to a minimum of hour", 60),
    ],
)
def test_a_stated_floor_is_converted_to_minutes(text, minutes):
    extraction = extract_from(
        text,
        rule=DocumentedRule.MINIMUM_TIME_FLOOR,
        normalizer="minimum_time_floor_minutes/1",
        fragment="down to a minimum",
    )
    assert extraction.normalized_value == minutes


# =============================================================================
# A spec that cannot be applied is not a spec
# =============================================================================


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"yaml_path": ()}, r"(?i)no yaml_path"),
        ({"expected_source_fragment": "   "}, r"(?i)no expected_source_fragment"),
        ({"normalizer": "wishful/1"}, r"(?i)not defined"),
    ],
)
def test_an_unusable_extraction_spec_is_refused(overrides, expected):
    fields = {
        "rule": DocumentedRule.RATE_UNITS,
        "yaml_path": ("components", "parameters", "rate_value", "description"),
        "expected_source_fragment": "as a percent",
        "normalizer": "rate_units/1",
    }
    with pytest.raises(OpenApiExtractionError, match=expected):
        ExtractionSpec(**{**fields, **overrides})


def test_a_path_holding_something_other_than_text_is_refused():
    spec = ExtractionSpec(
        rule=DocumentedRule.RATE_UNITS,
        yaml_path=("components", "parameters"),
        expected_source_fragment="as a percent",
        normalizer="rate_units/1",
    )
    with pytest.raises(OpenApiExtractionError, match=r"(?i)not readable text"):
        spec.resolve_text({"components": {"parameters": {"rate_value": {}}}})


def test_a_path_stopping_partway_names_where_it_stopped():
    spec = ExtractionSpec(
        rule=DocumentedRule.RATE_UNITS,
        yaml_path=("components", "parameters", "rate_value", "description"),
        expected_source_fragment="as a percent",
        normalizer="rate_units/1",
    )
    with pytest.raises(OpenApiExtractionError, match=r"components / parameters"):
        spec.resolve_text({"components": {}})


def test_a_normalized_value_that_cannot_be_hashed_canonically_is_refused():
    """``str()`` of an object moves when its ``__repr__`` does, so the
    extraction hash would drift without the evidence drifting."""
    with pytest.raises(OpenApiExtractionError, match=r"(?i)cannot be hashed"):
        OpenApiEvidenceExtraction(
            rule=DocumentedRule.RATE_UNITS,
            document_sha256=DIGEST,
            yaml_path=("components",),
            expected_source_fragment="x",
            normalized_value=object(),
        )


# =============================================================================
# A bundle is one document, read once
# =============================================================================


def test_a_bundle_cannot_carry_one_rule_twice():
    """A rule with two readings resolves to whichever a lookup finds first."""
    extraction = production_bundle().extractions[0]
    with pytest.raises(OpenApiExtractionError, match=r"(?i)extracted twice"):
        VendorDocumentationBundle(
            document=PRODUCTION_PINNED_DOCUMENT,
            extractions=(extraction, extraction),
        )


def test_a_rule_the_bundle_does_not_carry_reads_as_unresolved():
    """``None`` means unresolved. It is not a default, and there is no default."""
    from src.adapters.thetadata.openapi_evidence import PRODUCTION_EXTRACTION_SPECS

    rate_only = tuple(
        spec
        for spec in PRODUCTION_EXTRACTION_SPECS
        if spec.rule is DocumentedRule.RATE_UNITS
    )
    bundle = load_vendor_documentation_bundle(
        root=repository_documentation_root(), specs=rate_only
    )
    assert bundle.rules == (DocumentedRule.RATE_UNITS,)
    assert bundle.extraction_for(DocumentedRule.MINIMUM_TIME_FLOOR) is None
    assert bundle.value_for(DocumentedRule.MINIMUM_TIME_FLOOR) is None


def test_a_bundle_that_settles_no_open_interest_rule_yields_no_settlement():
    from src.adapters.thetadata.openapi_evidence import PRODUCTION_EXTRACTION_SPECS

    rate_only = tuple(
        spec
        for spec in PRODUCTION_EXTRACTION_SPECS
        if spec.rule is DocumentedRule.RATE_UNITS
    )
    bundle = load_vendor_documentation_bundle(
        root=repository_documentation_root(), specs=rate_only
    )
    with pytest.raises(OpenApiExtractionError, match=r"(?i)settles no open-interest"):
        settlement_documentation_rule(bundle)


def test_a_bundle_assembled_by_hand_reports_that_it_cannot_be_reverified(tmp_path):
    """``verify_against`` re-extracts, so a root with no document fails."""
    problems = production_bundle().verify_against(tmp_path)
    assert problems
    assert "missing" in problems[0].lower()


def test_the_bundle_round_trips_as_a_dictionary():
    payload = production_bundle().as_dict()
    assert payload["bundle_hash"]
    assert len(payload["extractions"]) == 3
    assert payload["document"]["document_sha256"] == (
        PRODUCTION_PINNED_DOCUMENT.document_sha256
    )


# =============================================================================
# A file that parses is not thereby the document
# =============================================================================


def written(tmp_path: pathlib.Path, body: bytes) -> PinnedDocument:
    digest = hashlib.sha256(body).hexdigest()
    target = tmp_path / f"thetadata/{digest[:2]}/{digest}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return pinned(
        document_sha256=digest,
        byte_length=len(body),
        content_location=f"thetadata/{digest[:2]}/{digest}.yaml",
    )


def test_unparseable_bytes_are_held_and_establish_nothing(tmp_path):
    document = written(tmp_path, b"openapi: 3.1.0\n  paths: [unbalanced\n")
    with pytest.raises(OpenApiExtractionError, match=r"(?i)not parseable yaml"):
        load_vendor_documentation_bundle(root=tmp_path, document=document)


def test_a_document_declaring_another_openapi_version_is_refused(tmp_path):
    document = written(tmp_path, b"openapi: 3.0.3\npaths:\n  /x: {}\n")
    with pytest.raises(OpenApiExtractionError, match=r"(?i)declares openapi"):
        load_vendor_documentation_bundle(root=tmp_path, document=document)


def test_a_yaml_scalar_is_not_an_api_description(tmp_path):
    document = written(tmp_path, b"just a string\n")
    with pytest.raises(OpenApiExtractionError, match=r"(?i)no ``paths``|paths"):
        load_vendor_documentation_bundle(root=tmp_path, document=document)


# =============================================================================
# Drift findings the pinned document does not currently produce
# =============================================================================


def synthetic(tmp_path: pathlib.Path, paths: dict) -> PinnedDocument:
    import yaml

    body = yaml.safe_dump(
        {
            "openapi": "3.1.0",
            "servers": [{"url": "http://127.0.0.1:25503/v3"}],
            "paths": paths,
        }
    ).encode("utf-8")
    return written(tmp_path, body)


def test_an_endpoint_the_document_does_not_describe_blocks_a_capture(tmp_path):
    document = synthetic(tmp_path, {"/index/snapshot/price": {"get": {}}})
    findings = endpoint_drift(
        root=tmp_path,
        document=document,
        endpoints=("/v3/option/snapshot/quote",),
    )
    assert [f.kind for f in findings] == [DriftKind.ENDPOINT_ABSENT]
    assert findings[0].blocks_capture
    assert findings[0].as_dict()["document_path"] == "/option/snapshot/quote"


def test_a_silent_document_is_reported_as_silence_not_as_agreement(tmp_path):
    """``NOT_DOCUMENTED`` is a separate finding from ``CONFLICT``.

    Collapsing the two would let silence read as confirmation, which is the
    shape of every defect this repository has spent releases removing.
    """
    document = synthetic(tmp_path, {"/index/snapshot/price": {"get": {}}})
    findings = endpoint_drift(
        root=tmp_path,
        document=document,
        endpoints=("/v3/index/snapshot/price",),
    )
    kinds = {f.kind for f in findings}
    assert kinds == {DriftKind.TIER_NOT_DOCUMENTED, DriftKind.FIELDS_NOT_DOCUMENTED}
    assert not any(f.blocks_capture for f in findings)


def test_a_subscription_name_this_repository_does_not_model_is_a_conflict(tmp_path):
    document = synthetic(
        tmp_path,
        {"/index/snapshot/price": {"x-min-subscription": "platinum", "get": {}}},
    )
    findings = endpoint_drift(
        root=tmp_path,
        document=document,
        endpoints=("/v3/index/snapshot/price",),
    )
    conflict = next(f for f in findings if f.kind is DriftKind.TIER_CONFLICT)
    assert "platinum" in conflict.detail
    assert conflict.blocks_capture


def test_a_reordered_column_list_is_a_conflict(tmp_path):
    """Same columns, different order. ``RESPONSE_FIELDS`` documents the order
    the CSV returns them in, so the order is part of the claim."""
    document = synthetic(
        tmp_path,
        {
            "/index/snapshot/price": {
                "x-min-subscription": "standard",
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "text/csv": {
                                    "schema": {
                                        "items": {
                                            "properties": {
                                                "symbol": {},
                                                "timestamp": {},
                                                "price": {},
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                },
            }
        },
    )
    findings = endpoint_drift(
        root=tmp_path,
        document=document,
        endpoints=("/v3/index/snapshot/price",),
    )
    conflict = next(f for f in findings if f.kind is DriftKind.FIELDS_CONFLICT)
    assert "different order" in conflict.detail


def test_a_document_with_no_servers_entry_falls_back_to_a_literal_path(tmp_path):
    """The prefix is read from the document. A document that states none gets
    no prefix stripped, rather than ``/v3`` assumed on its behalf."""
    import yaml

    from src.adapters.thetadata.openapi_evidence import _server_base_path

    assert _server_base_path({}) == ""
    assert _server_base_path({"servers": ["not-a-mapping"]}) == ""
    body = yaml.safe_dump({"openapi": "3.1.0", "paths": {"/v3/x": {}}}).encode("utf-8")
    document = written(tmp_path, body)
    findings = endpoint_drift(
        root=tmp_path, document=document, endpoints=("/v3/option/snapshot/quote",)
    )
    assert findings[0].document_path == "/v3/option/snapshot/quote"


# =============================================================================
# Content-addressed storage
# =============================================================================


def test_storing_a_document_names_it_after_its_own_digest(tmp_path):
    body = b"rate_value is expressed as a percent.\n"
    digest, location = store_document(body, root=tmp_path)

    assert digest == hashlib.sha256(body).hexdigest()
    assert location == f"{digest[:2]}/{digest}.bin"
    assert (tmp_path / location).read_bytes() == body

    # Storing the same bytes twice is one file, and does not rewrite it.
    again, same = store_document(body, root=tmp_path)
    assert (again, same) == (digest, location)


def test_a_stored_document_can_take_the_extension_of_its_format(tmp_path):
    _, location = store_document(b"openapi: 3.1.0\n", root=tmp_path, suffix=".yaml")
    assert location.endswith(".yaml")
    assert (tmp_path / location).is_file()


# =============================================================================
# Settlement refusals
# =============================================================================


def test_a_settlement_rule_applied_to_a_non_session_is_refused():
    """2026-08-08 is a Saturday. No chain was captured in it."""
    import datetime as dt

    with pytest.raises(
        OpenApiExtractionError, match=r"(?i)no settlement|not a trading"
    ):
        verified_settlement_artifact(
            production_bundle(), chain_session_date=dt.date(2026, 8, 8)
        )


def test_the_documentation_rule_names_the_document_it_was_read_from():
    rule = settlement_documentation_rule(production_bundle())
    assert rule.document_reference.startswith("vendor_documentation/")
    assert rule.document_content_hash == PRODUCTION_PINNED_DOCUMENT.document_sha256
    assert rule.effective_from == PRODUCTION_PINNED_DOCUMENT.retrieved_at.date()
    # The rule identifier is the path the reading came from, not a label.
    assert "open_interest" in rule.rule_identifier
