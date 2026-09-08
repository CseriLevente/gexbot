"""Bind private ThetaData support correspondence to capture evidence safely.

The raw email is private evidence and stays outside the repository.  This
module extracts only a content digest, non-identifying mailbox metadata and a
closed vocabulary of claims.  A second report then binds those claims to an
already-derived capture certification without changing either historical
artifact.

This is an evidence overlay.  It never parses paid market-data payloads, never
changes an option's open interest, and never grants a trusted GEX calculation.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from src.adapters.thetadata.analytical_universe import (
    ANALYTICAL_UNIVERSE_REPORT_SCHEMA_VERSION,
)
from src.adapters.thetadata.capture_certification import (
    CAPTURE_CERTIFICATION_SCHEMA_VERSION,
    LOCAL_DIAGNOSTIC_FIELDS,
)
from src.domain.canonical import (
    CANONICAL_REPORT_SCHEMA_VERSION,
    canonical_payload,
    without_fields,
)
from src.domain.digests import digest_of

__all__ = [
    "OI_POLICY_DERIVATION_VERSION",
    "OI_POLICY_RESOLUTION_SCHEMA_VERSION",
    "THETADATA_SUPPORT_EVIDENCE_SCHEMA_VERSION",
    "V2_1_30_SUPPORT_MESSAGE_DATE",
    "V2_1_30_SUPPORT_MESSAGE_ID_SHA256",
    "V2_1_30_SUPPORT_SENDER_DOMAIN",
    "V2_1_30_SUPPORT_SOURCE_BYTES",
    "V2_1_30_SUPPORT_SOURCE_SHA256",
    "FullExpirationReview",
    "OpenInterestPolicyEvidenceError",
    "OpenInterestPolicyResolution",
    "ThetaDataSupportEvidence",
    "extract_thetadata_support_evidence",
    "resolve_open_interest_policy",
]

THETADATA_SUPPORT_EVIDENCE_SCHEMA_VERSION = "thetadata-support-evidence/2.1.30"
OI_POLICY_RESOLUTION_SCHEMA_VERSION = "oi-policy-resolution/2.1.30"
OI_POLICY_DERIVATION_VERSION = "thetadata-oi-policy/1"

# The policy resolution in this release is authority-bound to the exact private
# message reviewed by the operator. Authentication-Results stays descriptive:
# the standard library does not reverify DKIM. The content digest is what stops
# a hand-built receipt or a different email with similar prose from being used.
V2_1_30_SUPPORT_SOURCE_SHA256 = (
    "29d6678e2fa3e3d1bd2886fb2fcee2ec7bdca73dda65666b05dbc9c0c3ce40fa"
)
V2_1_30_SUPPORT_SOURCE_BYTES = 31_411
V2_1_30_SUPPORT_MESSAGE_ID_SHA256 = (
    "4fe3c3a43a714ea4f0357069e2c1ca9298dc365944f0125d2989ddf8166db550"
)
V2_1_30_SUPPORT_SENDER_DOMAIN = "thetadata.net"
V2_1_30_SUPPORT_MESSAGE_DATE = "2026-09-06T20:57:48Z"

_EXPECTED_SUBJECT = (
    "meaning of contracts omitted from the option open-interest snapshot"
)
_VENDOR_DOMAINS = frozenset({"thetadata.net", "thetadata.us"})
_SHA256 = re.compile(r"[0-9a-f]{64}")

# Every claim requires all of its markers.  The derived artifact carries only
# the claim names; neither these snippets nor the source body are serialized.
_CLAIM_MARKERS: dict[str, tuple[str, ...]] = {
    "OI_ORIGIN_OPRA_DAILY_MESSAGE": (
        "open interest isn't computed by theta data",
        "message opra sends once per day",
    ),
    "MISSING_ROW_AMBIGUOUS_ZERO_OR_UNAVAILABLE": (
        "missing row can mean zero oi",
        "equally mean the message hasn't arrived yet",
    ),
    "MISSING_ROW_EXCLUDE_AS_UNAVAILABLE": (
        "correct and recommended approach",
        "excluding it from aggregation rather than treating it as zero",
    ),
    "NEW_LISTING_WAITS_FOR_NEXT_OPRA_CYCLE": (
        "starts trading intraday won't have an oi message until the next",
        "opra cycle",
    ),
    "NO_ZERO_VS_PENDING_STATUS_FIELD": (
        "no status flag",
        "missing row is the only signal",
    ),
    "OI_REPRESENTS_PRIOR_COMPLETED_SESSION": (
        "prior completed trading session's end-of-day open interest",
        "never same-day/live oi",
    ),
    "ONE_DAY_EXPIRED_CONTRACT_RETENTION": (
        "filter out a contract only once it's more than one day past expiration",
        "intentional",
    ),
    "FULL_EXPIRATION_GAP_VENDOR_REQUESTED_DETAILS": (
        "entire expiration",
        "unusual enough that it's worth us checking directly",
    ),
}


class OpenInterestPolicyEvidenceError(ValueError):
    """The correspondence or capture report cannot support this resolution."""


def _normalise_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("\u2019", "'").replace("\u2018", "'")
    value = value.replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", value).strip()


def _plain_body(message: Any) -> str:
    if message.is_multipart():
        for part in message.walk():
            if (
                part.get_content_type() == "text/plain"
                and part.get_content_disposition() != "attachment"
            ):
                content = part.get_content()
                if isinstance(content, str):
                    return content
    elif message.get_content_type() == "text/plain":
        content = message.get_content()
        if isinstance(content, str):
            return content
    raise OpenInterestPolicyEvidenceError("the message has no readable text/plain body")


_QUOTED_REPLY_SEPARATORS = (
    re.compile(r"^\s*on .+ wrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*original message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*>"),
)


def _unquoted_reply(value: str) -> str:
    """Return only the new reply, never claims quoted from the question."""

    lines = value.splitlines()
    for index, line in enumerate(lines):
        if any(pattern.search(line) for pattern in _QUOTED_REPLY_SEPARATORS):
            return "\n".join(lines[:index])
    return value


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ThetaDataSupportEvidence:
    """A privacy-safe receipt derived from one preserved RFC 5322 message."""

    source_artifact_sha256: str
    source_artifact_bytes: int
    message_id_sha256: str
    sender_domain: str
    message_date: str
    subject: str
    claims: tuple[str, ...]
    reported_dkim_pass: bool
    reported_spf_pass: bool
    reported_dmarc_pass: bool
    dkim_signature_present: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("source_artifact_sha256", self.source_artifact_sha256),
            ("message_id_sha256", self.message_id_sha256),
        ):
            if not _SHA256.fullmatch(value):
                raise OpenInterestPolicyEvidenceError(
                    f"{name} is not a lowercase SHA-256 digest"
                )
        if self.source_artifact_bytes <= 0:
            raise OpenInterestPolicyEvidenceError("source email is empty")
        if self.sender_domain not in _VENDOR_DOMAINS:
            raise OpenInterestPolicyEvidenceError(
                f"sender domain {self.sender_domain!r} is not a ThetaData domain"
            )
        expected_claims = tuple(_CLAIM_MARKERS)
        if self.claims != expected_claims:
            raise OpenInterestPolicyEvidenceError(
                "support evidence claims are not the exact ordered closed set"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": THETADATA_SUPPORT_EVIDENCE_SCHEMA_VERSION,
            "source_artifact": {
                "format": "RFC5322_EML",
                "sha256": self.source_artifact_sha256,
                "bytes": self.source_artifact_bytes,
                "storage": "EXTERNAL_PRIVATE_EVIDENCE",
            },
            "mailbox_metadata": {
                "sender_domain": self.sender_domain,
                "message_date": self.message_date,
                "subject": self.subject,
                "message_id_sha256": self.message_id_sha256,
                "reported_authentication_results": {
                    "dkim_pass": self.reported_dkim_pass,
                    "spf_pass": self.reported_spf_pass,
                    "dmarc_pass": self.reported_dmarc_pass,
                    "dkim_signature_present": self.dkim_signature_present,
                },
            },
            "claims": list(self.claims),
            "limitations": [
                "Mailbox authentication-result headers are recorded, not "
                "cryptographically reverified by this parser.",
                "message_date is normalized from the sender-authored Date "
                "header; it is not mailbox receipt telemetry.",
                "The private source body, sender address, recipients and local "
                "filesystem path are deliberately omitted.",
                "The message requests details for a 216-contract expiration "
                "gap but does not identify its symbol or expiration date.",
            ],
        }

    def evidence_hash(self) -> str:
        return digest_of(
            {
                "canonical_schema_version": CANONICAL_REPORT_SCHEMA_VERSION,
                **canonical_payload(self._payload()),
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "evidence_hash": self.evidence_hash()}


def extract_thetadata_support_evidence(
    source: pathlib.Path | bytes,
) -> ThetaDataSupportEvidence:
    """Extract the closed set of OI claims without serializing private content."""

    body = source if isinstance(source, bytes) else source.read_bytes()
    message = BytesParser(policy=policy.default).parsebytes(body)

    subject = str(message.get("Subject", "")).strip()
    normalized_subject = _normalise_text(subject)
    subject_without_reply = re.sub(r"^(?:re:\s*)+", "", normalized_subject)
    if subject_without_reply != _EXPECTED_SUBJECT:
        raise OpenInterestPolicyEvidenceError(
            "message subject is not the ThetaData open-interest clarification"
        )

    _, sender_address = parseaddr(str(message.get("From", "")))
    sender_domain = (
        sender_address.rsplit("@", 1)[-1].casefold() if "@" in sender_address else ""
    )
    if sender_domain not in _VENDOR_DOMAINS:
        raise OpenInterestPolicyEvidenceError(
            f"sender domain {sender_domain!r} is not a ThetaData domain"
        )

    raw_date = str(message.get("Date", ""))
    try:
        received = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError) as exc:
        raise OpenInterestPolicyEvidenceError("message Date header is invalid") from exc
    if received is None or received.utcoffset() is None:
        raise OpenInterestPolicyEvidenceError("message Date header has no timezone")
    message_date = received.astimezone(UTC).isoformat().replace("+00:00", "Z")

    message_id = str(message.get("Message-ID", "")).strip()
    if not message_id:
        raise OpenInterestPolicyEvidenceError("message has no Message-ID")

    normalized_body = _normalise_text(_unquoted_reply(_plain_body(message)))
    claims = tuple(
        claim
        for claim, markers in _CLAIM_MARKERS.items()
        if all(_normalise_text(marker) in normalized_body for marker in markers)
    )
    missing = sorted(set(_CLAIM_MARKERS) - set(claims))
    if missing:
        raise OpenInterestPolicyEvidenceError(
            f"message does not establish required claims: {missing}"
        )

    authentication = _normalise_text(
        " ".join(str(value) for value in message.get_all("Authentication-Results", []))
    )
    return ThetaDataSupportEvidence(
        source_artifact_sha256=hashlib.sha256(body).hexdigest(),
        source_artifact_bytes=len(body),
        message_id_sha256=_sha256(message_id),
        sender_domain=sender_domain,
        message_date=message_date,
        subject=subject,
        claims=claims,
        reported_dkim_pass=bool(re.search(r"\bdkim=pass\b", authentication)),
        reported_spf_pass=bool(re.search(r"\bspf=pass\b", authentication)),
        reported_dmarc_pass=bool(re.search(r"\bdmarc=pass\b", authentication)),
        dkim_signature_present=bool(message.get_all("DKIM-Signature", [])),
    )


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OpenInterestPolicyEvidenceError(f"{name} is not an object")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OpenInterestPolicyEvidenceError(f"{name} is not an integer")
    return int(value)


@dataclass(frozen=True, slots=True)
class FullExpirationReview:
    """The capture case matching support's request for the 216-contract gap."""

    status: str
    symbol: str | None = None
    expiration: str | None = None
    listed: int | None = None
    missing: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "symbol": self.symbol,
            "expiration": self.expiration,
            "listed": self.listed,
            "missing": self.missing,
            "identity_source": (
                "BOUND_CAPTURE_REPORT"
                if self.status == "VENDOR_REQUESTED_DETAILS"
                else None
            ),
            "interpretation": (
                "ThetaData support described a fully absent expiration as "
                "unusual and requested the exact case. The saved reply does "
                "not determine whether the cause was OPRA or ThetaData ingestion."
            ),
        }


@dataclass(frozen=True, slots=True)
class OpenInterestPolicyResolution:
    """Vendor clarification composed with, but separate from, capture evidence."""

    support: ThetaDataSupportEvidence
    capture_binding: dict[str, Any]
    coverage: dict[str, Any]
    full_expiration_review: FullExpirationReview

    def _payload(self) -> dict[str, Any]:
        missing = _integer(self.coverage["oi_missing"], "coverage.oi_missing")
        permits = bool(self.coverage["permits_trusted_aggregate"])
        return {
            "schema_version": OI_POLICY_RESOLUTION_SCHEMA_VERSION,
            "derivation_version": OI_POLICY_DERIVATION_VERSION,
            "support_evidence": self.support.as_dict(),
            "capture_binding": dict(self.capture_binding),
            "open_interest_coverage": dict(self.coverage),
            "policy_resolution": {
                "missing_row_semantics": "AMBIGUOUS_ZERO_OR_NOT_YET_AVAILABLE",
                "imputation_policy": "NONE",
                "zero_imputation_permitted": False,
                "missing_row_treatment": "EXCLUDE_AS_UNAVAILABLE",
                "recommended_treatment": "UNAVAILABLE_EXCLUDE_NO_IMPUTATION",
                "status_discriminator": "NONE",
                "settlement_session": "PRIOR_COMPLETED_TRADING_SESSION",
                "delivery": {
                    "cadence": "DAILY",
                    "time_et": "06:30",
                    "qualifier": "APPROXIMATE",
                },
                "delivery_schedule_role": "INFORMATIONAL_NOT_READINESS_GATE",
                "new_listing_availability": "NEXT_OPRA_CYCLE",
                "expired_contract_retention": (
                    "UNTIL_MORE_THAN_ONE_DAY_PAST_EXPIRATION"
                ),
                "full_universe_coverage_complete": permits,
                "permits_trusted_full_universe_aggregate": permits,
                "gex_trust_gate_relaxed": False,
            },
            "full_expiration_review": self.full_expiration_review.as_dict(),
            "findings": [
                "ThetaData support confirms that absence alone cannot "
                "distinguish zero open interest from a message that is not "
                "available, so no number is imputed.",
                "A missing identity is excluded as unavailable. Exclusion "
                "prevents invention; it does not make incomplete full-universe "
                "coverage complete.",
                "The approximately 06:30 ET delivery time is descriptive and "
                "is not used as a readiness threshold.",
                f"This capture has {missing} listed identities without an "
                "open-interest row and remains ineligible for a trusted "
                "full-universe aggregate.",
            ],
        }

    def report_hash(self) -> str:
        return digest_of(
            {
                "canonical_schema_version": CANONICAL_REPORT_SCHEMA_VERSION,
                **canonical_payload(self._payload()),
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "report_hash": self.report_hash()}


def resolve_open_interest_policy(
    source: pathlib.Path | bytes,
    certification: Mapping[str, Any],
) -> OpenInterestPolicyResolution:
    """Bind vendor semantics to one certification without rewriting either."""

    if not isinstance(source, (pathlib.Path, bytes)):
        raise OpenInterestPolicyEvidenceError(
            "policy resolution requires raw EML bytes or a pathlib.Path"
        )
    support = extract_thetadata_support_evidence(source)
    actual_receipt = (
        support.source_artifact_sha256,
        support.source_artifact_bytes,
        support.message_id_sha256,
        support.sender_domain,
        support.message_date,
    )
    pinned_receipt = (
        V2_1_30_SUPPORT_SOURCE_SHA256,
        V2_1_30_SUPPORT_SOURCE_BYTES,
        V2_1_30_SUPPORT_MESSAGE_ID_SHA256,
        V2_1_30_SUPPORT_SENDER_DOMAIN,
        V2_1_30_SUPPORT_MESSAGE_DATE,
    )
    if actual_receipt != pinned_receipt:
        raise OpenInterestPolicyEvidenceError(
            "support email does not match the pinned v2.1.30 private-source receipt"
        )

    capture = _mapping(certification.get("capture"), "capture")
    universe = _mapping(certification.get("universe"), "universe")
    coverage = _mapping(
        certification.get("open_interest_coverage"), "open_interest_coverage"
    )
    analytical = _mapping(
        certification.get("analytical_universe"), "analytical_universe"
    )
    analytical_capture = _mapping(analytical.get("capture"), "analytical capture")

    if certification.get("schema_version") != CAPTURE_CERTIFICATION_SCHEMA_VERSION:
        raise OpenInterestPolicyEvidenceError(
            "unsupported capture-certification schema version"
        )
    if analytical.get("schema_version") != ANALYTICAL_UNIVERSE_REPORT_SCHEMA_VERSION:
        raise OpenInterestPolicyEvidenceError(
            "unsupported analytical-universe report schema version"
        )

    report_hash = str(certification.get("report_hash", ""))
    manifest_hash = str(capture.get("manifest_hash", ""))
    universe_hash = str(universe.get("contract_list_set_hash", ""))
    analytical_hash = str(analytical.get("analytical_universe_report_hash", ""))
    for name, value in (
        ("certification report hash", report_hash),
        ("capture manifest hash", manifest_hash),
        ("contract-list universe hash", universe_hash),
        ("analytical-universe report hash", analytical_hash),
    ):
        if not _SHA256.fullmatch(value):
            raise OpenInterestPolicyEvidenceError(f"{name} is not a SHA-256 digest")

    universe_count = _integer(coverage.get("universe_count"), "universe_count")
    oi_covered = _integer(coverage.get("oi_covered"), "oi_covered")
    oi_missing = _integer(coverage.get("oi_missing"), "oi_missing")
    explicit_zero = _integer(coverage.get("oi_explicit_zero"), "oi_explicit_zero")
    if min(universe_count, oi_covered, oi_missing, explicit_zero) < 0:
        raise OpenInterestPolicyEvidenceError(
            "open-interest coverage counts cannot be negative"
        )
    if explicit_zero > oi_covered:
        raise OpenInterestPolicyEvidenceError(
            "explicit-zero count exceeds answered open-interest coverage"
        )
    if oi_covered + oi_missing != universe_count:
        raise OpenInterestPolicyEvidenceError(
            "open-interest coverage does not account for the listed universe"
        )
    unexpected = _integer(coverage.get("unexpected_oi_count"), "unexpected_oi_count")
    duplicates = _integer(
        coverage.get("duplicate_oi_identity_count"),
        "duplicate_oi_identity_count",
    )
    linkage_checks = {
        "manifest_hash": analytical_capture.get("manifest_hash") == manifest_hash,
        "session_id": analytical_capture.get("session_id") == capture.get("session_id"),
        "session_date": analytical_capture.get("session_date")
        == analytical.get("market_session_date")
        == analytical.get("valuation_session_date"),
        "expected_universe_hash": analytical_capture.get("expected_universe_hash")
        == universe_hash,
        "expected_universe_count": analytical_capture.get("expected_universe_count")
        == universe.get("contract_list_count")
        == analytical.get("listed_count")
        == universe_count,
        "oi_answered_count": analytical_capture.get("oi_answered_count") == oi_covered,
        "oi_missing_count": analytical_capture.get("oi_missing_count") == oi_missing,
        "oi_explicit_zero_count": analytical_capture.get("oi_explicit_zero_count")
        == explicit_zero,
        "oi_missing_hash": analytical_capture.get("oi_missing_hash")
        == coverage.get("missing_identities_hash"),
    }
    failed_linkage = sorted(
        name for name, agrees in linkage_checks.items() if not agrees
    )
    if failed_linkage:
        raise OpenInterestPolicyEvidenceError(
            "certification and analytical-universe reports are not bound to "
            f"the same capture: {failed_linkage}"
        )
    calculated_permits = oi_missing == unexpected == duplicates == 0
    if bool(coverage.get("permits_trusted_aggregate")) != calculated_permits:
        raise OpenInterestPolicyEvidenceError(
            "open-interest coverage verdict disagrees with its counts"
        )
    if analytical.get("sessions_agree") is not True:
        raise OpenInterestPolicyEvidenceError(
            "analytical-universe session sources do not agree"
        )
    if analytical.get("accounting_is_exhaustive") is not True:
        raise OpenInterestPolicyEvidenceError(
            "analytical-universe accounting is not exhaustive"
        )

    missing_rows = coverage.get("missing_by_expiration")
    if not isinstance(missing_rows, list):
        raise OpenInterestPolicyEvidenceError("missing_by_expiration is not a list")
    fully_missing_expirations = coverage.get("fully_missing_expirations")
    if not isinstance(fully_missing_expirations, list) or not all(
        isinstance(item, str) and item for item in fully_missing_expirations
    ):
        raise OpenInterestPolicyEvidenceError(
            "fully_missing_expirations is not a list of dates"
        )

    candidates: list[tuple[str, int, int]] = []
    computed_fully_missing: set[str] = set()
    for index, raw in enumerate(missing_rows):
        row = _mapping(raw, f"missing_by_expiration[{index}]")
        listed = _integer(row.get("listed"), f"missing_by_expiration[{index}].listed")
        missing = _integer(
            row.get("missing"), f"missing_by_expiration[{index}].missing"
        )
        expiration = str(row.get("expiration", ""))
        if listed > 0 and listed == missing:
            computed_fully_missing.add(expiration)
        if listed == missing == 216:
            candidates.append((expiration, listed, missing))

    if set(fully_missing_expirations) != computed_fully_missing:
        raise OpenInterestPolicyEvidenceError(
            "fully missing expiration summary disagrees with its rows"
        )

    symbol = str(analytical_capture.get("symbol", ""))
    if len(candidates) != 1 or not symbol:
        raise OpenInterestPolicyEvidenceError(
            "capture does not identify exactly one 216-contract fully missing "
            "expiration for the vendor-requested case"
        )
    expiration, listed, missing = candidates[0]

    certification_payload = {
        key: value
        for key, value in certification.items()
        if key not in {"report_hash", "analytical_universe"}
    }
    certification_payload = without_fields(
        certification_payload, LOCAL_DIAGNOSTIC_FIELDS
    )
    computed_report_hash = digest_of(
        {
            "canonical_schema_version": CANONICAL_REPORT_SCHEMA_VERSION,
            **canonical_payload(certification_payload),
        }
    )
    if computed_report_hash != report_hash:
        raise OpenInterestPolicyEvidenceError(
            "certification content does not match its report hash"
        )

    analytical_payload = dict(analytical)
    analytical_payload.pop("analytical_universe_report_hash", None)
    computed_analytical_hash = digest_of(
        {
            "canonical_schema_version": CANONICAL_REPORT_SCHEMA_VERSION,
            **canonical_payload(analytical_payload),
        }
    )
    if computed_analytical_hash != analytical_hash:
        raise OpenInterestPolicyEvidenceError(
            "analytical-universe content does not match its report hash"
        )

    full_review = FullExpirationReview(
        status="VENDOR_REQUESTED_DETAILS",
        symbol=symbol,
        expiration=expiration,
        listed=listed,
        missing=missing,
    )

    binding = {
        "certification_schema_version": str(certification.get("schema_version", "")),
        "certification_report_hash": report_hash,
        "capture_session_id": str(capture.get("session_id", "")),
        "capture_manifest_hash": manifest_hash,
        "market_session_date": str(analytical.get("market_session_date", "")),
        "symbol": symbol,
        "contract_list_universe_hash": universe_hash,
        "analytical_universe_report_hash": analytical_hash,
    }
    selected_coverage = {
        "universe_count": universe_count,
        "oi_covered": oi_covered,
        "oi_missing": oi_missing,
        "oi_explicit_zero": explicit_zero,
        "unexpected_oi_count": unexpected,
        "duplicate_oi_identity_count": duplicates,
        "coverage_state": str(coverage.get("coverage_state", "")),
        "permits_trusted_aggregate": bool(coverage.get("permits_trusted_aggregate")),
    }
    return OpenInterestPolicyResolution(
        support=support,
        capture_binding=binding,
        coverage=selected_coverage,
        full_expiration_review=full_review,
    )
