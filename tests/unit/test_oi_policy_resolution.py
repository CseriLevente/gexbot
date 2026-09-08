"""v2.1.30 binds vendor OI semantics without importing private correspondence."""

from __future__ import annotations

import copy
import json
import pathlib
from dataclasses import replace
from email.message import EmailMessage
from email.policy import default
from unittest import mock

import pytest

import src.adapters.thetadata.oi_policy as oi_policy_module
from src.adapters.thetadata.oi_policy import (
    OI_POLICY_RESOLUTION_SCHEMA_VERSION,
    THETADATA_SUPPORT_EVIDENCE_SCHEMA_VERSION,
    OpenInterestPolicyEvidenceError,
    extract_thetadata_support_evidence,
    resolve_open_interest_policy,
)
from src.domain.canonical import CANONICAL_REPORT_SCHEMA_VERSION, canonical_payload
from src.domain.digests import digest_of

ROOT = pathlib.Path(__file__).resolve().parents[2]
THIRD_CAPTURE = ROOT / "tests" / "fixtures" / "live_capture" / "third_capture.json"
POLICY_FIXTURE = (
    ROOT / "tests" / "fixtures" / "live_capture" / "oi_policy_resolution_v2_1_30.json"
)

BODY = """
Open interest isn't computed by Theta Data. It is a message OPRA sends once per day.
A missing row can mean zero OI, but it can equally mean the message hasn't arrived yet.
For a contract that starts trading intraday won't have an OI message until the next
approximately 06:30 ET OPRA cycle.
There is no status flag for confirmed zero versus pending; a missing row is the only
signal. It is the prior completed trading session's end-of-day open interest and is
never same-day/live OI.
Treating a row as unavailable is the correct and recommended approach, excluding it
from aggregation rather than treating it as zero.
This is intentional. We filter out a contract only once it's more than one day past
expiration.
An entire expiration is unusual enough that it's worth us checking directly.
"""


def message_bytes(
    *,
    body: str = BODY,
    sender: str = "Theta <a@thetadata.net>",
    authenticated: bool = True,
) -> bytes:
    message = EmailMessage(policy=default)
    message["Subject"] = (
        "Re: Meaning of contracts omitted from the option open-interest snapshot"
    )
    message["From"] = sender
    message["To"] = "private-user@example.invalid"
    message["Date"] = "Sun, 06 Sep 2026 20:57:48 +0000"
    message["Message-ID"] = "<support-case@example.invalid>"
    if authenticated:
        message["Authentication-Results"] = (
            "mailbox.example; dkim=pass header.i=@thetadata.net; "
            "spf=pass smtp.mailfrom=thetadata.net; dmarc=pass header.from=thetadata.net"
        )
        message["DKIM-Signature"] = "v=1; d=thetadata.net; b=fixture"
    message.set_content(body)
    return message.as_bytes()


def resolve_synthetic(
    certification: dict,
    *,
    source: bytes | None = None,
    pinned_source: bytes | None = None,
):
    """Exercise the public raw-EML resolver against a synthetic pinned receipt."""

    source = message_bytes() if source is None else source
    pinned_source = source if pinned_source is None else pinned_source
    receipt = extract_thetadata_support_evidence(pinned_source)
    with mock.patch.multiple(
        oi_policy_module,
        V2_1_30_SUPPORT_SOURCE_SHA256=receipt.source_artifact_sha256,
        V2_1_30_SUPPORT_SOURCE_BYTES=receipt.source_artifact_bytes,
        V2_1_30_SUPPORT_MESSAGE_ID_SHA256=receipt.message_id_sha256,
        V2_1_30_SUPPORT_SENDER_DOMAIN=receipt.sender_domain,
        V2_1_30_SUPPORT_MESSAGE_DATE=receipt.message_date,
    ):
        return resolve_open_interest_policy(source, certification)


@pytest.fixture
def third_capture() -> dict:
    return json.loads(THIRD_CAPTURE.read_text(encoding="utf-8"))


def test_support_email_is_reduced_to_a_closed_privacy_safe_receipt():
    evidence = extract_thetadata_support_evidence(message_bytes())
    data = evidence.as_dict()

    assert data["schema_version"] == THETADATA_SUPPORT_EVIDENCE_SCHEMA_VERSION
    assert data["source_artifact"]["storage"] == "EXTERNAL_PRIVATE_EVIDENCE"
    assert data["mailbox_metadata"]["sender_domain"] == "thetadata.net"
    assert data["mailbox_metadata"]["message_date"] == "2026-09-06T20:57:48Z"
    assert set(data["claims"]) == {
        "OI_ORIGIN_OPRA_DAILY_MESSAGE",
        "MISSING_ROW_AMBIGUOUS_ZERO_OR_UNAVAILABLE",
        "MISSING_ROW_EXCLUDE_AS_UNAVAILABLE",
        "NEW_LISTING_WAITS_FOR_NEXT_OPRA_CYCLE",
        "NO_ZERO_VS_PENDING_STATUS_FIELD",
        "OI_REPRESENTS_PRIOR_COMPLETED_SESSION",
        "ONE_DAY_EXPIRED_CONTRACT_RETENTION",
        "FULL_EXPIRATION_GAP_VENDOR_REQUESTED_DETAILS",
    }
    encoded = json.dumps(data, sort_keys=True)
    assert "private-user" not in encoded
    assert "a@" not in encoded
    assert BODY not in encoded
    assert ":\\" not in encoded


def test_mailbox_authentication_is_recorded_not_overclaimed():
    data = extract_thetadata_support_evidence(message_bytes()).as_dict()
    results = data["mailbox_metadata"]["reported_authentication_results"]
    assert results == {
        "dkim_pass": True,
        "spf_pass": True,
        "dmarc_pass": True,
        "dkim_signature_present": True,
    }
    assert any(
        "not cryptographically reverified" in item for item in data["limitations"]
    )


def test_release_pins_the_exact_private_source_receipt():
    assert oi_policy_module.V2_1_30_SUPPORT_SOURCE_SHA256 == (
        "29d6678e2fa3e3d1bd2886fb2fcee2ec7bdca73dda65666b05dbc9c0c3ce40fa"
    )
    assert oi_policy_module.V2_1_30_SUPPORT_SOURCE_BYTES == 31_411
    assert oi_policy_module.V2_1_30_SUPPORT_MESSAGE_ID_SHA256 == (
        "4fe3c3a43a714ea4f0357069e2c1ca9298dc365944f0125d2989ddf8166db550"
    )
    assert oi_policy_module.V2_1_30_SUPPORT_SENDER_DOMAIN == "thetadata.net"
    assert oi_policy_module.V2_1_30_SUPPORT_MESSAGE_DATE == "2026-09-06T20:57:48Z"


def test_a_hand_built_extra_claim_is_refused():
    support = extract_thetadata_support_evidence(message_bytes())
    with pytest.raises(OpenInterestPolicyEvidenceError, match="closed set"):
        replace(support, claims=(*support.claims, "UNREVIEWED_CLAIM"))


def test_a_missing_claim_is_refused_instead_of_inferred():
    incomplete = BODY.replace(
        "A missing row can mean zero OI, but it can equally mean the message hasn't arrived yet.\n",
        "",
    )
    with pytest.raises(OpenInterestPolicyEvidenceError, match="required claims"):
        extract_thetadata_support_evidence(message_bytes(body=incomplete))


def test_claims_in_quoted_correspondence_are_not_treated_as_vendor_answers():
    quoted = "Thanks.\n\nOn Sat, 5 Sep 2026, Example wrote:\n> " + BODY.replace(
        "\n", "\n> "
    )
    with pytest.raises(OpenInterestPolicyEvidenceError, match="required claims"):
        extract_thetadata_support_evidence(message_bytes(body=quoted))


def test_a_non_vendor_sender_is_refused():
    with pytest.raises(OpenInterestPolicyEvidenceError, match="not a ThetaData domain"):
        extract_thetadata_support_evidence(
            message_bytes(sender="Someone <a@example.invalid>")
        )


def test_mailbox_authentication_headers_are_informational_not_authority(third_capture):
    source = message_bytes(authenticated=False)
    report = resolve_synthetic(third_capture, source=source).as_dict()
    results = report["support_evidence"]["mailbox_metadata"][
        "reported_authentication_results"
    ]
    assert results == {
        "dkim_pass": False,
        "spf_pass": False,
        "dmarc_pass": False,
        "dkim_signature_present": False,
    }


def test_resolution_rejects_hand_built_evidence(third_capture):
    support = extract_thetadata_support_evidence(message_bytes())
    with pytest.raises(OpenInterestPolicyEvidenceError, match="raw EML"):
        resolve_open_interest_policy(support, third_capture)  # type: ignore[arg-type]


def test_resolution_rejects_bytes_that_differ_from_the_pinned_source(third_capture):
    pinned = message_bytes()
    altered = pinned + b"\n"
    with pytest.raises(OpenInterestPolicyEvidenceError, match=r"pinned v2\.1\.30"):
        resolve_synthetic(third_capture, source=altered, pinned_source=pinned)


def test_resolution_binds_the_216_contract_case_to_capture_evidence(third_capture):
    report = resolve_synthetic(third_capture).as_dict()

    assert report["schema_version"] == OI_POLICY_RESOLUTION_SCHEMA_VERSION
    assert report["capture_binding"]["symbol"] == "SPXW"
    assert report["capture_binding"]["market_session_date"] == "2026-09-02"
    assert report["full_expiration_review"] == {
        "status": "VENDOR_REQUESTED_DETAILS",
        "symbol": "SPXW",
        "expiration": "2026-10-23",
        "listed": 216,
        "missing": 216,
        "identity_source": "BOUND_CAPTURE_REPORT",
        "interpretation": (
            "ThetaData support described a fully absent expiration as unusual "
            "and requested the exact case. The saved reply does not determine "
            "whether the cause was OPRA or ThetaData ingestion."
        ),
    }


def test_vendor_advice_does_not_relax_incomplete_coverage(third_capture):
    report = resolve_synthetic(third_capture).as_dict()
    coverage = report["open_interest_coverage"]
    decision = report["policy_resolution"]

    assert coverage == {
        "universe_count": 13_536,
        "oi_covered": 12_808,
        "oi_missing": 728,
        "oi_explicit_zero": 3_373,
        "unexpected_oi_count": 0,
        "duplicate_oi_identity_count": 0,
        "coverage_state": "OI_MISSING",
        "permits_trusted_aggregate": False,
    }
    assert decision["missing_row_semantics"] == ("AMBIGUOUS_ZERO_OR_NOT_YET_AVAILABLE")
    assert decision["imputation_policy"] == "NONE"
    assert decision["zero_imputation_permitted"] is False
    assert decision["missing_row_treatment"] == "EXCLUDE_AS_UNAVAILABLE"
    assert decision["permits_trusted_full_universe_aggregate"] is False
    assert decision["gex_trust_gate_relaxed"] is False


def test_approximate_delivery_time_is_never_a_readiness_threshold(third_capture):
    decision = resolve_synthetic(third_capture).as_dict()["policy_resolution"]
    assert decision["delivery"] == {
        "cadence": "DAILY",
        "time_et": "06:30",
        "qualifier": "APPROXIMATE",
    }
    assert decision["delivery_schedule_role"] == "INFORMATIONAL_NOT_READINESS_GATE"


def test_zero_or_multiple_216_contract_matches_are_refused(third_capture):
    no_match = copy.deepcopy(third_capture)
    row = next(
        item
        for item in no_match["open_interest_coverage"]["missing_by_expiration"]
        if item["listed"] == item["missing"] == 216
    )
    row["listed"] = row["missing"] = 215
    with pytest.raises(OpenInterestPolicyEvidenceError, match="216-contract"):
        resolve_synthetic(no_match)

    multiple = copy.deepcopy(third_capture)
    multiple["open_interest_coverage"]["missing_by_expiration"].append(
        {"expiration": "2026-10-24", "listed": 216, "missing": 216}
    )
    multiple["open_interest_coverage"]["fully_missing_expirations"].append("2026-10-24")
    with pytest.raises(OpenInterestPolicyEvidenceError, match="216-contract"):
        resolve_synthetic(multiple)


def test_inconsistent_coverage_is_refused(third_capture):
    broken = copy.deepcopy(third_capture)
    broken["open_interest_coverage"]["oi_missing"] = 727
    with pytest.raises(OpenInterestPolicyEvidenceError, match="does not account"):
        resolve_synthetic(broken)


def test_inconsistent_fully_missing_expiration_summary_is_refused(third_capture):
    broken = copy.deepcopy(third_capture)
    broken["open_interest_coverage"]["fully_missing_expirations"] = []
    with pytest.raises(OpenInterestPolicyEvidenceError, match="summary disagrees"):
        resolve_synthetic(broken)


def test_analytical_report_must_be_from_the_same_capture(third_capture):
    broken = copy.deepcopy(third_capture)
    other = copy.deepcopy(broken["analytical_universe"])
    other["capture"]["session_id"] = "capture-from-another-session"
    other.pop("analytical_universe_report_hash")
    other["analytical_universe_report_hash"] = digest_of(
        {
            "canonical_schema_version": CANONICAL_REPORT_SCHEMA_VERSION,
            **canonical_payload(other),
        }
    )
    broken["analytical_universe"] = other
    with pytest.raises(OpenInterestPolicyEvidenceError, match="same capture"):
        resolve_synthetic(broken)


def test_stale_certification_hash_is_refused(third_capture):
    broken = copy.deepcopy(third_capture)
    broken["analytical_readiness"] = "EDITED_WITHOUT_REHASHING"
    with pytest.raises(OpenInterestPolicyEvidenceError, match="certification content"):
        resolve_synthetic(broken)


def test_stale_analytical_universe_hash_is_refused(third_capture):
    broken = copy.deepcopy(third_capture)
    broken["analytical_universe"]["imputation_policy"] += " EDITED"
    with pytest.raises(
        OpenInterestPolicyEvidenceError, match="analytical-universe content"
    ):
        resolve_synthetic(broken)


def test_resolution_hash_is_deterministic_and_carries_no_local_path(third_capture):
    first = resolve_synthetic(third_capture)
    second = resolve_synthetic(third_capture)
    assert first.as_dict() == second.as_dict()
    assert first.report_hash() == second.report_hash()
    assert len(first.report_hash()) == 64
    encoded = json.dumps(first.as_dict(), sort_keys=True)
    assert ":\\" not in encoded
    assert "/Users/" not in encoded
    assert "/home/" not in encoded


def test_committed_resolution_is_bound_to_the_preserved_sources():
    report = json.loads(POLICY_FIXTURE.read_text(encoding="utf-8"))
    support = report["support_evidence"]
    source = support["source_artifact"]
    mailbox = support["mailbox_metadata"]

    assert source["sha256"] == (
        "29d6678e2fa3e3d1bd2886fb2fcee2ec7bdca73dda65666b05dbc9c0c3ce40fa"
    )
    assert source["bytes"] == 31_411
    assert mailbox["message_date"] == "2026-09-06T20:57:48Z"
    assert mailbox["sender_domain"] == "thetadata.net"
    assert report["capture_binding"]["certification_report_hash"] == (
        "07dc84dd153e61638b0aece24956b8c5765c31c7093c4e9d115ca8474f626d7b"
    )
    assert report["capture_binding"]["analytical_universe_report_hash"] == (
        "27c3f69510bfe8b8f6aae6191ba8e004be37eb759096055b8f47cbfb36242b06"
    )
    assert report["full_expiration_review"]["status"] == "VENDOR_REQUESTED_DETAILS"
    assert report["policy_resolution"]["gex_trust_gate_relaxed"] is False

    stored_report_hash = report.pop("report_hash")
    assert stored_report_hash == (
        "339656fc086be98eda8281b0754f848836b5769068c21de620077cbf845ea575"
    )
    assert stored_report_hash == digest_of(
        {
            "canonical_schema_version": CANONICAL_REPORT_SCHEMA_VERSION,
            **canonical_payload(report),
        }
    )

    stored_evidence_hash = support.pop("evidence_hash")
    assert stored_evidence_hash == (
        "7aef83fdde0778ccc5081afaad61d0574aa948d582340a0d30a948734a64b828"
    )
    assert stored_evidence_hash == digest_of(
        {
            "canonical_schema_version": CANONICAL_REPORT_SCHEMA_VERSION,
            **canonical_payload(support),
        }
    )
