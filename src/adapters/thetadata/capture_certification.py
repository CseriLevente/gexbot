"""Read an immutable capture and work out what the vendor actually did.

Offline, deterministic, and derived entirely from bytes on disk. Nothing here
opens a socket; the capture is the input and a content-addressed report is the
output, so running it twice on the same directory produces the same digest and
running it on a modified directory does not.

The method is the same for every dimension: state the competing hypotheses,
score each one against the vendor's own numbers, and keep the score table rather
than only the winner. A report that says "ACT/365" is an assertion. A report
that says ACT/365 scored 1.7e-4 and ACT/360 scored 1.6e-2 over 7,348 rows is a
result somebody can disagree with.

**Nothing here knows what the first capture sent.** v2.1.22 did: it inverted at
``rate=4.2``, scored day counts at ``rate=4.2``, offered exactly ``4.2`` and
``0.042`` as the rate hypotheses, and labelled the winners ``ACT_365`` and
``16:00 America/New_York`` from constants. Every one of those was the first
capture's own answer written into the machinery that was supposed to find it, so
the second capture -- which sends ``rate_value=0.042`` -- would have been scored
against the wrong pair and then labelled from the wrong literals.

So the wire value comes from the capture, proved against its manifest, and the
hypotheses are derived from it: a decimal reading uses it unchanged, a percent
reading divides by a hundred. The rate and the day count are searched
*jointly*, because neither resolves without the other -- the inversion that
reads the clock needs both, and the clock decides which expirations fall in
which regime.

For the expiration timestamp the report does something stronger than scoring.
Given the vendor's reported delta and implied volatility, ``d1`` is determined,
and time-to-expiry follows from a quadratic -- so each row can be *inverted* for
the clock the vendor used, with no hypothesis at all. Grouped by expiration that
inversion turned out to matter: the first live capture prices expirations inside
its own week to an intraday close and everything later on whole calendar days.
Scoring a single global hypothesis would have averaged those together and
reported a mediocre fit for both, and the scope of the finding is carried as
structured evidence rather than as a sentence somebody has to read.

The pricing is the engine's own :mod:`src.gex.pricing`, not a private copy. The
question certification answers is whether *this repository's* Black-Scholes
reproduces the vendor's, and a second implementation written to match would
answer a different and much less useful question.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import pathlib
import statistics
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from statistics import NormalDist
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from src.adapters.thetadata.live_behavior import InferenceDecision
from src.domain.contracts import OptionRight
from src.gex.pricing import BlackScholesInputs, norm_cdf
from src.gex.pricing import delta as bs_delta

__all__ = [
    "CAPTURE_CERTIFICATION_SCHEMA_VERSION",
    "CaptureCertificationError",
    "CaptureCertificationReport",
    "CaptureRateEconomics",
    "CaptureRequestBinding",
    "ContractKey",
    "EndpointSynchrony",
    "ExpirationClockEvidence",
    "ExpirationClockReading",
    "HypothesisScore",
    "LoadedCapture",
    "OpenInterestCoverage",
    "OpenInterestCoverageState",
    "UniverseCertification",
    "certify_capture",
    "load_capture",
]

CAPTURE_CERTIFICATION_SCHEMA_VERSION = "capture-certification/2.1.26"

#: Bumped when what it takes for an archive to *be* a capture's archive changes.
#: v2.1.26 stops believing the archived manifest's stored digest, requires the
#: archived run intent to be this capture's by bytes, and refuses an archive
#: whose entry names collide once separators are normalised.
ARCHIVE_IDENTITY_SCHEMA_VERSION = "archive-identity/2.1.26"

EASTERN = ZoneInfo("America/New_York")

#: ``implied_vol`` is reported to four decimals. Half a tick is the floor below
#: which a delta disagreement says nothing about the model -- it is the rounding
#: in the field we are comparing against.
IMPLIED_VOL_TICK = 1e-4

#: Fewer rows than this and one expiration's inversion is noise, not a reading.
MINIMUM_ROWS_PER_EXPIRATION = 20

#: Fewer usable rows than this and no comparison distinguishes anything.
MINIMUM_ROWS_FOR_INFERENCE = 30

#: ``delta`` is reported to four decimals, so a reported value is the true one
#: plus a rounding error uniform on +/- half a quantum. The RMSE of that noise
#: alone is ``quantum / sqrt(12)`` -- about 2.9e-05. It is the floor: no
#: hypothesis, however correct, reproduces the vendor better than this.
DELTA_QUANTUM = 1e-4
DELTA_NOISE_FLOOR = DELTA_QUANTUM / math.sqrt(12.0)

#: How many noise floors a fit may sit above before it stops being evidence.
#:
#: Not a confidence constant picked to make a test pass. The first capture's
#: best fit is 1.6e-04, about 5.5 floors, and carries a real systematic drift
#: with maturity; the wrong rate hypothesis on the same capture is 2.3e-01,
#: about 8,000 floors. Twenty-five leaves the honest fit comfortable room for
#: effects a reconstruction cannot model while still refusing anything that is
#: not reproducing the vendor at all.
ADEQUATE_FIT_NOISE_FLOORS = 25.0

#: ``implied_vol`` is reported to four decimals too, so a solved volatility can
#: never be closer than half a quantum to the reported one.
IMPLIED_VOL_HALF_QUANTUM = 5e-5

#: The year fractions worth testing, with the label each one earns if it wins.
#: A denominator is a hypothesis, so the label is the winner's, never a
#: constant chosen by whoever wrote the report.
DAY_COUNT_HYPOTHESES: tuple[tuple[float, str], ...] = (
    (365.0, "ACT/365"),
    (365.25, "ACT/365.25"),
    (360.0, "ACT/360"),
    (252.0, "ACT/252"),
)

#: Day-count label -> the token recorded as the resolved convention.
DAY_COUNT_TOKENS: dict[str, str] = {
    "ACT/365": "ACT_365",
    "ACT/365.25": "ACT_365_25",
    "ACT/360": "ACT_360",
    "ACT/252": "ACT_252",
}

#: Endpoint paths, as they appear in the capture record index.
INDEX_PRICE = "/v3/index/snapshot/price"
OPTION_QUOTE = "/v3/option/snapshot/quote"
OPTION_OPEN_INTEREST = "/v3/option/snapshot/open_interest"
OPTION_GREEKS = "/v3/option/snapshot/greeks/first_order"
OPTION_CONTRACT_LIST = "/v3/option/list/contracts/quote"


class CaptureCertificationError(ValueError):
    """A capture that cannot be certified from what is on disk."""


class OpenInterestCoverageState(str, Enum):
    """Three different things, one of which used to be spelled ``0``.

    The first live capture returned 14,130 open-interest rows against a 14,556
    contract universe. 3,692 of those rows say ``open_interest,0`` and 426
    identities have no row at all. Those are not the same fact:

    ``OI_EXPLICIT_ZERO``
        The vendor was asked and answered zero. A real, usable observation --
        the contract exists and nobody holds it.
    ``OI_MISSING``
        The vendor returned nothing for this identity. The open interest could
        be zero, or ten thousand, or the endpoint could have truncated.

    Filling a missing identity with zero converts the second into the first and
    loses the distinction permanently. Open interest is the linear weight on
    every GEX term, so a contract silently weighted zero is a contract deleted
    from the aggregate -- and it disappears without changing any count that a
    completeness check looks at.
    """

    OI_PRESENT = "OI_PRESENT"
    OI_EXPLICIT_ZERO = "OI_EXPLICIT_ZERO"
    OI_MISSING = "OI_MISSING"

    @property
    def is_observed(self) -> bool:
        """Whether the vendor actually answered for this identity."""
        return self is not OpenInterestCoverageState.OI_MISSING


@dataclass(frozen=True, slots=True, order=True)
class ContractKey:
    """One option contract, canonically. Sorts, hashes, and prints stably."""

    symbol: str
    expiration: date
    strike: str
    right: str

    @classmethod
    def from_row(cls, row: dict[str, str]) -> ContractKey:
        return cls(
            symbol=row["symbol"].strip().strip('"'),
            expiration=date.fromisoformat(row["expiration"].strip().strip('"')),
            # Kept as the vendor's own text. Parsing to float and back would
            # make 7600.000 and 7600.0 the same identity in this repository and
            # different identities on the wire.
            strike=row["strike"].strip().strip('"'),
            right=row["right"].strip().strip('"'),
        )

    @property
    def canonical(self) -> str:
        return f"{self.symbol}|{self.expiration.isoformat()}|{self.strike}|{self.right}"

    @property
    def option_right(self) -> OptionRight:
        return OptionRight.CALL if self.right.upper() == "CALL" else OptionRight.PUT


def _set_hash(keys: set[ContractKey]) -> str:
    """A digest over an identity set, order-independent by construction."""
    joined = "\n".join(sorted(k.canonical for k in keys))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CaptureRequestBinding:
    """What the capture actually asked the vendor, proved rather than supplied.

    The vendor's Greeks are a function of the parameters the request carried, so
    reconstructing them needs the ``rate_value`` that was really sent. v2.1.22
    hard-coded 4.2 because that is what the first capture happened to send;
    the obvious repair -- a ``rate_value=`` argument on ``certify_capture`` --
    would be worse than the bug. It would let anyone pair one capture's
    responses with another capture's rate and get a confident, wrong
    certification out, which is precisely the substitution every other check in
    this repository exists to prevent.

    So the value is read from the run intent and then *proved* against the
    manifest. Each planned request carries its canonical parameters; each
    manifest record carries the ``request_spec_fingerprint`` of the session and
    the ``planned_request_hash`` that was stamped at capture time. Recomputing
    the digest from the parameters reproduces the stamp only if the parameters
    are the ones that were stamped. Edit ``rate_value`` in the intent and the
    recomputation no longer matches, and certification refuses.

    The manifest is itself verified by recomputing its own hash from its
    descriptors, so the anchor is not a number somebody could also have edited.
    """

    #: endpoint -> canonical parameters, as proved against the manifest.
    parameters: dict[str, dict[str, str]]
    verified_endpoints: tuple[str, ...]

    def value_for(self, endpoint: str, name: str) -> str | None:
        return self.parameters.get(endpoint, {}).get(name)

    @property
    def greeks_rate_value(self) -> float:
        """The ``rate_value`` the Greeks request carried. The wire value ``w``."""
        raw = self.value_for(OPTION_GREEKS, "rate_value")
        if raw is None:
            raise CaptureCertificationError(
                "the capture's Greeks request carries no rate_value, so there "
                "is no wire value to interpret. Certification will not guess "
                "one: the vendor applied some default this response cannot "
                "recover, and every delta in the capture depends on it."
            )
        try:
            return float(raw)
        except ValueError as error:
            raise CaptureCertificationError(
                f"the captured rate_value is {raw!r}, which is not a number"
            ) from error

    @property
    def greeks_annual_dividend(self) -> float:
        raw = self.value_for(OPTION_GREEKS, "annual_dividend")
        try:
            return float(raw) if raw is not None else 0.0
        except ValueError:
            return 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified_endpoints": list(self.verified_endpoints),
            "greeks_rate_value": self.greeks_rate_value,
            "greeks_annual_dividend": self.greeks_annual_dividend,
            "binding": "RECOMPUTED_AGAINST_MANIFEST_PLANNED_REQUEST_HASH",
        }


@dataclass(frozen=True, slots=True)
class LoadedCapture:
    """The five responses, verified against the manifest that describes them."""

    root: pathlib.Path
    session_id: str
    manifest_hash: str
    captured_at: str
    tables: dict[str, list[dict[str, str]]]
    record_hashes: dict[str, str]
    verified_records: int
    parser_version: str
    request: CaptureRequestBinding
    #: What the capturing session declared it *meant* by the wire value, when it
    #: recorded one, **and proved** against the approval stamped on every
    #: manifest record. ``None`` for captures taken before v2.1.24, which
    #: recorded no bound intent.
    bound_rate_intent: Any | None = None
    #: The documentary readings this capture was taken under, **re-derived from
    #: the pinned document's own bytes** rather than read out of the run intent.
    #: ``None`` for a capture that carries no documentary extractions.
    documentation: VerifiedDocumentation | None = None
    #: The run-intent schema this capture was written under, which is what says
    #: whether a bound rate intent *should* have been present.
    intent_schema_version: str = ""
    #: The manifest rebuilt from its own descriptors, kept so an archive can be
    #: compared against *the reconstruction* rather than against a stored field.
    rebuilt_manifest: Any | None = None
    #: SHA-256 over the exact bytes of this capture's ``run-intent.json``. What
    #: an archived copy has to equal to be this capture's run intent rather than
    #: a file with the right name.
    run_intent_sha256: str = ""

    @property
    def record_count(self) -> int:
        return len(self.record_hashes)

    @property
    def records_are_post_binding(self) -> bool:
        """Was this capture taken under a schema that binds the rate intent?

        Decides whether an absent intent is history or a hole. The legacy
        documentary fallback exists for captures that predate the binding;
        applying it to a new capture would quietly restore the defect.
        """
        return _schema_release(self.intent_schema_version) >= (2, 1, 24)

    def rows(self, endpoint: str) -> list[dict[str, str]]:
        try:
            return self.tables[endpoint]
        except KeyError as error:
            raise CaptureCertificationError(
                f"the capture has no {endpoint} response. Certification "
                f"compares endpoints against each other; with one absent there "
                f"is nothing to compare. Present: {sorted(self.tables)}"
            ) from error


def _schema_release(version: str) -> tuple[int, ...]:
    """``"raw-capture-intent/2.1.24"`` -> ``(2, 1, 24)``. Unknown sorts lowest."""
    _, _, tail = str(version).partition("/")
    parts = []
    for piece in tail.split("."):
        if not piece.isdigit():
            return (0,)
        parts.append(int(piece))
    return tuple(parts) if parts else (0,)


def _verified_manifest(root: pathlib.Path) -> tuple[dict[str, Any], Any]:
    """Load the manifest and prove its digest describes its own descriptors.

    v2.1.22 read ``manifest["manifest_hash"]`` and reported it. A stored digest
    that nothing recomputes is a label, not a check: edit a descriptor and the
    label still reads correct, which is the state a verifier is supposed to
    detect.
    """
    from src.adapters.raw_store import RawCaptureManifest

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise CaptureCertificationError(
            f"{manifest_path} does not exist. A capture without its manifest "
            "cannot be verified, and an unverified capture cannot certify "
            "anything."
        )
    payload = json.loads(manifest_path.read_bytes())
    stored = str(payload.get("manifest_hash", ""))
    if not stored:
        raise CaptureCertificationError(
            "the manifest carries no manifest_hash; there is nothing to check "
            "its descriptors against"
        )
    rebuilt = RawCaptureManifest.rebuilt_from(payload)
    if rebuilt.manifest_hash != stored:
        raise CaptureCertificationError(
            f"the manifest stores {stored} and its own descriptors hash to "
            f"{rebuilt.manifest_hash}. Either a descriptor was changed without "
            "updating the digest, or the digest was changed without the "
            "descriptors. Both mean this manifest does not describe this "
            "capture."
        )
    return payload, rebuilt


def _bind_request(
    root: pathlib.Path, manifest: dict[str, Any]
) -> CaptureRequestBinding:
    """Recover the parameters the capture actually sent, and prove them."""
    from src.adapters.thetadata.request_plan import planned_request_hash

    intent_path = root / "run-intent.json"
    if not intent_path.is_file():
        raise CaptureCertificationError(
            f"{intent_path} does not exist. The run intent is where a capture "
            "records the requests it was about to make; without it the "
            "parameters the vendor answered are unknown, and certification "
            "will not accept them from a caller instead."
        )
    intent = json.loads(intent_path.read_bytes())
    planned = (intent.get("request_plan") or {}).get("requests") or []
    records = {r.get("endpoint", ""): r for r in manifest.get("records", [])}

    parameters: dict[str, dict[str, str]] = {}
    verified: list[str] = []
    for entry in planned:
        endpoint = str(entry.get("endpoint", ""))
        record = records.get(endpoint)
        if record is None:
            # Planned but never captured. Not an error here -- the endpoint
            # simply produced no record, and the acquisition report is where
            # that is accounted for.
            continue
        canonical = tuple(
            (str(pair[0]), str(pair[1]))
            for pair in entry.get("canonical_query_parameters", [])
        )
        stamped = str(record.get("planned_request_hash", ""))
        if not stamped:
            raise CaptureCertificationError(
                f"the manifest record for {endpoint} carries no "
                "planned_request_hash, so the parameters in the run intent "
                "cannot be tied to the response that was stored"
            )
        recomputed = planned_request_hash(
            str(record.get("request_spec_fingerprint", "")), endpoint, canonical
        )
        if recomputed != stamped:
            raise CaptureCertificationError(
                f"the run intent's parameters for {endpoint} hash to "
                f"{recomputed} and the manifest record was stamped {stamped}. "
                "The recorded request is not the request that produced these "
                "bytes. Certification refuses rather than reconstructing the "
                "vendor's numbers under parameters it cannot show were sent."
            )
        parameters[endpoint] = dict(canonical)
        verified.append(endpoint)

    if OPTION_GREEKS not in parameters:
        raise CaptureCertificationError(
            "no verified request was found for "
            f"{OPTION_GREEKS}. The Greeks request carries the rate the whole "
            "reconstruction turns on, and an unverified one is not usable."
        )
    return CaptureRequestBinding(
        parameters=parameters, verified_endpoints=tuple(sorted(verified))
    )


def load_capture(root: pathlib.Path | str) -> LoadedCapture:
    """Read a capture directory and re-verify everything it claims about itself.

    Three independent checks, none of which trusts a stored value on the
    strength of its being present: the manifest hashes to its own descriptors,
    every payload hashes to the digest its descriptor records, and the request
    parameters in the run intent reproduce the stamp on the manifest record.

    The verification is not decoration. Certification reads numbers out of these
    bytes and then this repository records vendor conventions on the strength of
    them; if the bytes changed after capture, every conclusion below is about a
    file rather than about the vendor.
    """
    root = pathlib.Path(root)
    if not root.is_dir():
        raise CaptureCertificationError(f"{root} is not a directory")

    manifest, rebuilt = _verified_manifest(root)

    tables: dict[str, list[dict[str, str]]] = {}
    record_hashes: dict[str, str] = {}
    verified = 0
    for record in manifest.get("records", []):
        endpoint = record.get("endpoint", "")
        location = record.get("payload_location", "")
        expected = record.get("payload_hash", "")
        payload = root / "raw" / location
        if not payload.is_file():
            raise CaptureCertificationError(
                f"manifest names {location} for {endpoint} but the file is "
                "absent; the capture is incomplete"
            )
        body = payload.read_bytes()
        actual = hashlib.sha256(body).hexdigest()
        if actual != expected:
            raise CaptureCertificationError(
                f"{location} hashes to {actual} and the manifest says "
                f"{expected}. These bytes are not the bytes that were "
                "captured, so nothing derived from them describes the vendor."
            )
        verified += 1
        record_hashes[endpoint] = actual
        tables[endpoint] = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))

    intent_path = root / "run-intent.json"
    if not intent_path.is_file():
        raise CaptureCertificationError(
            f"{intent_path} does not exist. The run intent is where a capture "
            "records the requests it was about to make and what it meant by "
            "them; without it neither can be established."
        )
    intent_bytes = intent_path.read_bytes()
    intent = json.loads(intent_bytes)
    approval = _verified_approval(intent, manifest)

    return LoadedCapture(
        root=root,
        session_id=str(manifest.get("session_id", "")),
        manifest_hash=str(manifest.get("manifest_hash", "")),
        captured_at=str(
            (manifest.get("records") or [{}])[0].get(
                "effective_valuation_timestamp", ""
            )
        ),
        tables=tables,
        record_hashes=record_hashes,
        verified_records=verified,
        parser_version=str(manifest.get("parser_version", "")),
        request=_bind_request(root, manifest),
        bound_rate_intent=_bind_rate_intent(intent, manifest, approval=approval),
        documentation=_verified_documentation(
            intent, approval=approval, capture_root=root
        ),
        intent_schema_version=str(intent.get("schema_version", "")),
        rebuilt_manifest=rebuilt,
        run_intent_sha256=hashlib.sha256(intent_bytes).hexdigest(),
    )


def _bind_rate_intent(
    intent: dict[str, Any],
    manifest: dict[str, Any],
    *,
    approval: dict[str, Any] | None,
) -> Any | None:
    """Prove the capture's declared economic intent, or refuse it.

    Four links, and breaking any one refuses the capture:

    1. the recorded ``rate_semantics`` rebuild into a coherent
       :class:`~src.adapters.thetadata.live_behavior.CaptureRateIntent` --
       so 4.2% beside 0.5 as a decimal is refused as incoherent rather than
       fingerprinted as unusual, and since v2.1.25 a block that names no
       schema version is refused rather than defaulted into this release's;
    2. its fingerprint matches the one inside the recorded approval;
    3. the approval's own contents hash to the ``approval_hash`` it carries;
    4. that hash is the one stamped on the manifest records, whose digest the
       caller has already recomputed from its own descriptors.

    Links 3 and 4 are :func:`_verified_approval`, done by the caller because the
    documentation bundle hangs off the same approval and both must be checked
    against the *same* proved artifact.

    v2.1.23 had none of these. Editing ``economic_rate_decimal`` from 0.042 to
    4.2 in the run intent -- touching no response, no request plan and no
    manifest -- flipped the capture from economically valid to invalid and
    certification reported the new answer without complaint.

    ``None`` for a capture that recorded no intent at all. That is a legacy
    state, handled explicitly by the caller, and never a silent pass.
    """
    from src.adapters.thetadata.live_behavior import CaptureRateIntent

    recorded = intent.get("rate_semantics")
    if not isinstance(recorded, dict) or not recorded:
        return None

    try:
        rate_intent = CaptureRateIntent.from_payload(recorded)
    except Exception as error:  # ThetaDataProvenanceError and friends
        raise CaptureCertificationError(
            f"the capture records a rate intent that is not internally "
            f"consistent: {error}"
        ) from error

    bound = str((approval or {}).get("rate_intent_fingerprint", ""))
    if not bound:
        raise CaptureCertificationError(
            "the capture records a rate intent but its preflight approval does "
            "not bind one. An intent nothing was approved against can be "
            "edited afterwards without disturbing a single digest, which is "
            "the defect this binding exists to close."
        )
    if rate_intent.fingerprint != bound:
        raise CaptureCertificationError(
            f"the recorded rate intent fingerprints to "
            f"{rate_intent.fingerprint} and the approval was taken against "
            f"{bound}. The capture's statement of what it meant to buy has "
            "changed since it was approved."
        )
    return rate_intent


def _recomputed_approval_hash(approval: dict[str, Any]) -> str:
    """Rehash a recorded approval from its own semantic fields.

    **Under the field set its own declared schema covered**, which is not
    necessarily the current one. ``rate_intent_fingerprint`` joined the digest in
    v2.1.24; hashing a v2.1.20 approval with that key present -- as ``None``, or
    as ``""`` -- produces a value the original run could not have computed, and
    the first live capture would be refused for carrying a correct approval.
    """
    from src.domain.digests import digest_of

    covered = [
        "schema_version",
        "market_session_date",
        "request_plan_hash",
        "capture_plan_fingerprint",
        "pipeline_fingerprint",
        "documentation_bundle_fingerprint",
        "effective_transport_fingerprint",
        "instrument_mapping_fingerprint",
        "subscription_tier",
    ]
    declared = str(approval.get("schema_version", ""))
    if not declared:
        raise CaptureCertificationError(
            "the recorded preflight approval names no schema_version, so there "
            "is no way to know which fields its hash was taken over. An "
            "approval that cannot be recomputed approves nothing."
        )
    if _schema_release(declared) >= (2, 1, 24):
        covered.append("rate_intent_fingerprint")
    return digest_of({name: approval.get(name) for name in covered})


def _verified_approval(
    intent: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any] | None:
    """The capture's preflight approval, proved against the manifest stamps.

    Extracted from ``_bind_rate_intent`` in v2.1.25 because the rate intent is
    no longer the only thing that hangs off it: the documentation bundle
    fingerprint does too, and both need the *same* approval to have been proved
    manifest-bound before either is believed.

    ``None`` when the capture recorded no approval at all.
    """
    approval = intent.get("preflight_approval") or {}
    if not isinstance(approval, dict) or not approval:
        return None
    stated = str(approval.get("approval_hash", ""))
    recomputed = _recomputed_approval_hash(approval)
    if recomputed != stated:
        raise CaptureCertificationError(
            f"the recorded approval carries hash {stated} and its own "
            f"contents hash to {recomputed}"
        )
    stamped = {
        str(record.get("preflight_approval_hash", ""))
        for record in manifest.get("records", [])
    }
    if stamped and stamped != {stated}:
        raise CaptureCertificationError(
            f"the run intent's approval is {stated} and the manifest records "
            f"were stamped {sorted(stamped)}. The approval in the run intent is "
            "not the approval this capture was taken under."
        )
    return approval


@dataclass(frozen=True, slots=True)
class VerifiedDocumentation:
    """A capture's documentary evidence, re-derived from the bytes it names.

    Not the dictionaries out of ``run-intent.json``. Those are what v2.1.24
    believed, and believing them was the defect: the only thing it checked was
    that each extraction's ``document_sha256`` matched the bundle's, so editing

        extractions[RATE_UNITS].normalized_value:
            PERCENT_ANNUAL_RATE -> DECIMAL_ANNUAL_RATE

    in a text editor changed the first capture's recovered intent from 0.042 to
    4.2 and made its documentation/live conflict disappear -- without touching a
    payload, a manifest or a digest anywhere.

    So the readings here came out of the pinned document itself, on this run,
    through the same normalizers, and the bundle they form was required to
    fingerprint to the value the manifest-bound approval carries. A reading that
    survives that is one the capture is genuinely evidence of.
    """

    bundle: Any
    bundle_fingerprint: str
    document_sha256: str
    content_location: str
    #: Where the bytes were found. Recorded so a report says which checkout
    #: answered, not merely that something did.
    documentation_root: str
    rules: tuple[str, ...]

    def value_for(self, rule: str) -> str:
        """The re-derived reading for a rule, as a string, or ``""``."""
        from src.adapters.thetadata.vendor_documentation import DocumentedRule

        try:
            typed = DocumentedRule(rule)
        except ValueError:
            return ""
        value = self.bundle.value_for(typed)
        if value is None:
            return ""
        return str(getattr(value, "value", value))

    def as_dict(self) -> dict[str, Any]:
        return {
            "documentary_authority": "REDERIVED_FROM_PINNED_DOCUMENT_BYTES",
            "document_sha256": self.document_sha256,
            "content_location": self.content_location,
            "documentation_root": self.documentation_root,
            "bundle_fingerprint": self.bundle_fingerprint,
            "rules_rederived": list(self.rules),
        }


def _locate_document(
    digest: str, roots: tuple[pathlib.Path, ...]
) -> tuple[pathlib.Path, str]:
    """Find content-addressed bytes that actually hash to ``digest``.

    The filename is a hint and nothing more -- every candidate is reread and
    rehashed before it is accepted. Content addressing makes a swapped file
    detectable; it is the rehash that detects it.
    """
    for root in roots:
        if not root.is_dir():
            continue
        for candidate in sorted(root.rglob(f"{digest}.*")):
            if not candidate.is_file():
                continue
            if hashlib.sha256(candidate.read_bytes()).hexdigest() == digest:
                return root, str(candidate.relative_to(root)).replace("\\", "/")
    raise CaptureCertificationError(
        f"no document hashing to {digest} was found under "
        f"{[str(r) for r in roots]}. The capture names documentary evidence "
        "this checkout does not hold, so the readings it recorded cannot be "
        "re-derived and must not be used as documentary authority."
    )


def _verified_documentation(
    intent: dict[str, Any],
    *,
    approval: dict[str, Any] | None,
    capture_root: pathlib.Path,
) -> VerifiedDocumentation | None:
    """Rebuild the capture's documentation bundle from the document itself.

    The chain, and every link is checked:

    1. the approval is manifest-bound -- the caller proved that already;
    2. the approval's ``documentation_bundle_fingerprint`` is what the run
       intent's bundle claims to be;
    3. the bytes named by ``document_sha256`` are found and rehashed;
    4. the recorded extraction specifications are rerun against them;
    5. the rebuilt bundle fingerprints to the approval-bound value;
    6. every re-derived reading equals the one the capture recorded.

    Step 5 catches a swapped document. Step 6 catches an edited reading whose
    ``extraction_hash`` was recomputed to match -- the tampering that step 5
    alone would sail past, because the document is untouched and therefore
    re-derives to exactly the fingerprint the approval names.

    ``None`` when the capture carries no documentary extractions at all.
    """
    from src.adapters.thetadata.openapi_evidence import (
        ExtractionSpec,
        OpenApiEvidenceExtraction,
        PinnedDocument,
        load_vendor_documentation_bundle,
        repository_documentation_root,
    )
    from src.adapters.thetadata.vendor_documentation import (
        DocumentedRule,
        VendorDocumentationError,
    )

    recorded = intent.get("vendor_documentation") or {}
    entries = [e for e in recorded.get("extractions", []) if isinstance(e, dict)]
    if not entries:
        return None

    if approval is None:
        raise CaptureCertificationError(
            "the capture carries documentary extractions and no preflight "
            "approval binds them. Documentation nothing was approved against "
            "can be edited afterwards without disturbing a digest, which is "
            "exactly how a historical capture acquires a different intent."
        )
    bound = str(approval.get("documentation_bundle_fingerprint", ""))
    claimed = str(recorded.get("bundle_fingerprint", ""))
    if not bound or bound != claimed:
        raise CaptureCertificationError(
            f"the capture's documentation bundle claims fingerprint {claimed!r} "
            f"and the approval it was taken under binds {bound!r}. The readings "
            "in this run intent are not the readings the capture was approved "
            "under."
        )

    digest = str(recorded.get("document_sha256", ""))
    roots = (capture_root / "vendor_documentation", repository_documentation_root())
    holder, location = _locate_document(digest, roots)

    try:
        # Rebuilt as typed objects first, which recomputes each extraction_hash
        # against its own contents: an edited value with a stale hash is refused
        # here, before anything is re-derived.
        rebuilt_records = tuple(
            OpenApiEvidenceExtraction(
                rule=DocumentedRule(str(entry["rule"])),
                document_sha256=str(entry["document_sha256"]),
                yaml_path=tuple(str(p) for p in entry["yaml_path"]),
                expected_source_fragment=str(entry["expected_source_fragment"]),
                normalized_value=entry["normalized_value"],
                normalizer=str(entry.get("normalizer", "")),
                extractor_version=str(entry["extractor_version"]),
                schema_version=str(entry["schema_version"]),
                extraction_hash=str(entry.get("extraction_hash", "")),
            )
            for entry in entries
        )
        pinned = PinnedDocument(
            source_url=str(recorded["source_url"]),
            retrieved_at=datetime.fromisoformat(str(recorded["retrieved_at"])),
            http_status=int(recorded.get("http_status", 200)),
            content_type=str(recorded.get("content_type", "application/octet-stream")),
            byte_length=int(recorded["byte_length"]),
            document_sha256=digest,
            content_location=location,
            document_schema_version=str(recorded["document_schema_version"]),
        )
        derived = load_vendor_documentation_bundle(
            root=holder,
            document=pinned,
            specs=tuple(
                ExtractionSpec(
                    rule=record.rule,
                    yaml_path=record.yaml_path,
                    expected_source_fragment=record.expected_source_fragment,
                    normalizer=record.normalizer,
                )
                for record in rebuilt_records
            ),
        )
    except (KeyError, TypeError, ValueError, VendorDocumentationError) as error:
        raise CaptureCertificationError(
            f"the capture's documentary evidence could not be re-derived from "
            f"{digest[:12]}...: {error}"
        ) from error

    if derived.bundle_hash != bound:
        raise CaptureCertificationError(
            f"re-reading {location} produces documentation bundle "
            f"{derived.bundle_hash} and the manifest-bound approval names "
            f"{bound}. The document behind this capture's readings is not the "
            "document it was approved under."
        )
    for was, now in zip(rebuilt_records, derived.extractions, strict=True):
        if was.semantic_payload() != now.semantic_payload():
            raise CaptureCertificationError(
                f"the capture records {was.rule.value} as "
                f"{was.normalized_value!r} and the pinned document re-reads it "
                f"as {now.normalized_value!r}. The stored reading has been "
                "edited away from the document it claims to come from."
            )
    return VerifiedDocumentation(
        bundle=derived,
        bundle_fingerprint=derived.bundle_hash,
        document_sha256=digest,
        content_location=location,
        documentation_root=str(holder),
        rules=tuple(sorted(r.rule.value for r in derived.extractions)),
    )


# ---------------------------------------------------------------------------
# Universe and coverage
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArchiveIdentity:
    """Whether an archive was actually hashed, or merely described.

    v2.1.23 took ``archive_sha256="d"*64`` from a caller and reported it as the
    capture's archive identity without opening a file. Sixty-four hex characters
    are a well-formed digest, not evidence that any archive has it -- and a
    reader checking a download against that value would be checking it against
    somebody's assertion.

    So the digest is computed here from bytes, or the claim is labelled as
    unverified and ``known`` stays false. There is no third state where a caller
    is believed.
    """

    sha256: str = ""
    #: ``VERIFIED_FROM_BYTES``, ``UNVERIFIED_EXTERNAL_ARCHIVE_DIGEST_CLAIM`` or
    #: ``ABSENT``. Whether anybody hashed anything, and nothing more.
    provenance: str = "ABSENT"
    byte_length: int = 0
    #: Whether an archived manifest **rebuilt** to this capture's manifest.
    #:
    #: Derived from the reconstruction, not from a file with the right name and
    #: the right ``session_id``: a forged manifest carrying a copied digest is
    #: not this capture's manifest, and describing the archive as containing
    #: one would be the same mistake in a different field.
    contains_capture_manifest: bool = False
    #: Whether it contains the *whole* capture: a manifest that rebuilds to this
    #: capture's, the exact run intent, and every raw payload the reconstruction
    #: names, each hashing to the digest the manifest records for it.
    matches_capture: bool = False
    #: Why not, when not. Empty when the archive matched.
    mismatch_reasons: tuple[str, ...] = ()
    payloads_verified: int = 0
    #: Records the *recomputed* archive manifest names. The denominator
    #: ``payloads_verified`` has to reach; published so a reader never has to
    #: infer whether "5 verified" was five out of five.
    records_expected: int = 0
    #: Whether the archived manifest was rebuilt from its own descriptors and
    #: agreed with both its stored digest and this capture's.
    manifest_recomputed: bool = False
    #: Whether the archived run intent is this capture's, compared by bytes.
    run_intent_verified: bool = False
    archive_run_intent_sha256: str = ""
    schema_version: str = ARCHIVE_IDENTITY_SCHEMA_VERSION

    @property
    def bytes_hashed(self) -> bool:
        """True only when this process hashed the bytes itself."""
        return self.provenance == "VERIFIED_FROM_BYTES"

    @property
    def known(self) -> bool:
        """Whether this digest may be recorded as *this capture's* archive.

        **Both halves, and v2.1.24 had only the first.** Hashing a file proves
        the digest belongs to those bytes; it says nothing about whose bytes
        they are. Certifying the real capture while passing the v2.1.24 *source*
        ZIP produced ``archive_identity_known = true`` with
        ``archive_contains_capture_manifest = false``, and that unrelated
        digest was stamped onto every live observation as the artifact they
        were traceable to.

        A digest is the capture's archive identity only when the archive was
        opened and found to hold the capture.
        """
        return self.bytes_hashed and self.matches_capture

    def as_dict(self) -> dict[str, Any]:
        return {
            "archive_identity_schema_version": self.schema_version,
            "archive_sha256": self.sha256,
            "archive_identity_known": self.known,
            "archive_bytes_hashed": self.bytes_hashed,
            "archive_matches_capture": self.matches_capture,
            "archive_digest_provenance": self.provenance,
            "archive_byte_length": self.byte_length,
            "archive_contains_capture_manifest": self.contains_capture_manifest,
            "archive_manifest_recomputed": self.manifest_recomputed,
            "archive_run_intent_verified": self.run_intent_verified,
            "archive_run_intent_sha256": self.archive_run_intent_sha256,
            "archive_payloads_verified": self.payloads_verified,
            "archive_records_expected": self.records_expected,
            "archive_mismatch_reasons": list(self.mismatch_reasons),
        }


@dataclass(frozen=True, slots=True)
class _ArchiveInspection:
    """What opening one archive established. Each field is checked separately.

    A single ``matches`` boolean assembled from a chain of ``and``s is easy to
    satisfy accidentally; these are the individual facts, and
    :attr:`matches` requires all of them.
    """

    contains_capture_manifest: bool = False
    manifest_recomputed: bool = False
    run_intent_verified: bool = False
    archive_run_intent_sha256: str = ""
    payloads_verified: int = 0
    records_expected: int = 0
    reasons: tuple[str, ...] = ()

    @property
    def disqualifying(self) -> tuple[str, ...]:
        return tuple(r for r in self.reasons if "not required" not in r)

    @property
    def matches(self) -> bool:
        return (
            self.contains_capture_manifest
            and self.manifest_recomputed
            and self.run_intent_verified
            and self.records_expected > 0
            and self.payloads_verified == self.records_expected
            and not self.disqualifying
        )


def _canonical_entries(
    bundle: zipfile.ZipFile,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Map canonical path -> stored entry name, and report every collision.

    **Separators are normalised, and normalising creates the hazard.** The ZIP
    specification says forward slashes and ``zipfile.write`` produces them, but
    the real first capture was archived by a Windows tool that wrote
    ``raw\\name``; comparing those against a path joined with ``/`` finds
    nothing, so v2.1.25 folded ``\\`` to ``/`` before looking anything up.

    That fold is many-to-one. An archive holding *both* ``raw/foo.raw`` and
    ``raw\\foo.raw`` with different bytes collapses to one key, and which one
    survives is decided by iteration order over the ZIP's central directory --
    so the evidence a verifier reads is chosen by how somebody ordered entries.
    A plain duplicate of the same name does the same thing; ZIP permits that
    too.

    So collisions are detected and the archive is refused rather than resolved.
    There is no correct winner to pick.
    """
    counts: dict[str, int] = defaultdict(int)
    entries: dict[str, str] = {}
    for name in bundle.namelist():
        canonical = name.replace("\\", "/")
        counts[canonical] += 1
        entries.setdefault(canonical, name)
    return entries, tuple(sorted(k for k, n in counts.items() if n > 1))


def _archive_contents(
    path: pathlib.Path,
    *,
    session_id: str,
    manifest_hash: str,
    rebuilt_manifest: Any,
    run_intent_sha256: str,
) -> _ArchiveInspection:
    """Open the archive and decide whether it holds *the whole capture*.

    A matching manifest is where the search starts, not where it ends, and
    v2.1.25 got the start wrong as well. It looked for a ``manifest.json``
    whose ``session_id`` and ``manifest_hash`` *fields* read correctly and never
    recomputed either. A file carrying the right session id, a copied digest and
    ``records: []`` therefore passed, alongside an empty run intent and no
    payloads at all -- and reported ``archive_matches_capture = true`` with
    ``archive_payloads_verified = 0``, because zero records have zero missing
    payloads.

    So the archived manifest is rebuilt from its own descriptors through the
    same reconstruction the root manifest goes through, and the recomputation --
    not the stored field -- has to agree with both the field beside it and the
    capture being certified. Then the record count and the descriptors
    themselves are compared, the run intent is compared by bytes, and every
    payload the reconstruction names is rehashed.
    """
    from src.adapters.raw_store import RawCaptureManifest

    reasons: list[str] = []
    try:
        with zipfile.ZipFile(path) as bundle:
            held, collisions = _canonical_entries(bundle)
            if collisions:
                return _ArchiveInspection(
                    reasons=tuple(
                        f"two archive entries share the canonical path {name!r}; "
                        "which one a verifier reads would be decided by entry "
                        "order"
                        for name in collisions
                    )
                )

            candidate: dict[str, Any] | None = None
            prefix = ""
            for canonical, stored in sorted(held.items()):
                if not canonical.endswith("manifest.json"):
                    continue
                try:
                    payload = json.loads(bundle.read(stored))
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                if str(payload.get("session_id", "")) == session_id:
                    candidate = payload
                    prefix = canonical[: -len("manifest.json")]
                    break
            if candidate is None:
                return _ArchiveInspection(
                    reasons=("no manifest for this capture session",)
                )

            # -- the manifest, recomputed ----------------------------------
            #
            # A manifest *naming* this session has been found. Whether it is
            # this capture's manifest is decided below, by rebuilding it --
            # never by the fact that a file with the right name and the right
            # session id turned up. ``contains_capture_manifest`` reports the
            # rebuilt answer, so a forgery carrying the right session id is not
            # described as containing the capture's manifest.
            stated = str(candidate.get("manifest_hash", ""))
            try:
                rebuilt = RawCaptureManifest.rebuilt_from(candidate)
            except Exception as error:  # a manifest that will not reconstruct
                return _ArchiveInspection(
                    reasons=(f"the archived manifest cannot be rebuilt: {error}",),
                )
            recomputed = rebuilt.manifest_hash
            if recomputed != stated:
                reasons.append(
                    f"the archived manifest's descriptors hash to {recomputed} "
                    f"and it states {stated}"
                )
            if recomputed != manifest_hash:
                reasons.append(
                    f"the archived manifest rebuilds to {recomputed} and this "
                    f"capture's manifest is {manifest_hash}"
                )
            expected_records = len(rebuilt_manifest.records)
            if len(rebuilt.records) != expected_records:
                reasons.append(
                    f"the archived manifest names {len(rebuilt.records)} records "
                    f"and this capture has {expected_records}"
                )
            # Descriptor equality, compared rather than inferred from the hash
            # agreeing. The hash covers these, so this cannot fail while the
            # hashes match -- which is the point: it is the assumption written
            # down as a check, and it is what fails first if the digest ever
            # stops covering a field somebody adds.
            if rebuilt.semantic_payload() != rebuilt_manifest.semantic_payload():
                reasons.append(
                    "the archived manifest's descriptors differ from this capture's"
                )
            manifest_recomputed = not reasons

            # -- the run intent, by bytes ----------------------------------
            archived_intent = ""
            intent_entry = f"{prefix}run-intent.json"
            if intent_entry not in held:
                reasons.append("run-intent.json is absent")
            else:
                archived_intent = hashlib.sha256(
                    bundle.read(held[intent_entry])
                ).hexdigest()
                if archived_intent != run_intent_sha256:
                    reasons.append(
                        f"the archived run intent hashes to {archived_intent} "
                        f"and this capture's is {run_intent_sha256}. A file with "
                        "the right name is not the run intent"
                    )
            run_intent_verified = bool(archived_intent) and (
                archived_intent == run_intent_sha256
            )

            # -- every payload the reconstruction names --------------------
            verified = 0
            for record in rebuilt.records:
                location = str(record.payload_location).replace("\\", "/")
                entry = f"{prefix}raw/{location}"
                if entry not in held:
                    reasons.append(f"raw payload {location} is absent")
                    continue
                actual = hashlib.sha256(bundle.read(held[entry])).hexdigest()
                if actual != record.payload_hash:
                    reasons.append(f"raw payload {location} hashes differently")
                    continue
                verified += 1

            # Present when a capture writes them, and worth reporting -- but a
            # capture is re-verifiable without them, so their absence is noted
            # rather than disqualifying. Named as the capture writer actually
            # writes them: the attempt index is ``attempts/index.jsonl``, and
            # looking for an ``attempt-index.json`` that has never existed
            # reports every archive as missing a file none of them has.
            for optional in (
                "attempts/index.jsonl",
                "capture-summary.json",
                "parser-report.json",
            ):
                if f"{prefix}{optional}" not in held:
                    reasons.append(f"{optional} is absent (not required)")
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError, ValueError) as error:
        return _ArchiveInspection(reasons=(f"the archive could not be read: {error}",))

    return _ArchiveInspection(
        contains_capture_manifest=(recomputed == manifest_hash),
        manifest_recomputed=manifest_recomputed,
        run_intent_verified=run_intent_verified,
        archive_run_intent_sha256=archived_intent,
        payloads_verified=verified,
        records_expected=expected_records,
        reasons=tuple(reasons),
    )


def _archive_identity(
    *,
    archive_path: pathlib.Path | str | None,
    archive_sha256: str,
    capture: LoadedCapture,
) -> ArchiveIdentity:
    """Hash the archive, check it is *this* capture's, or record that nobody did."""
    session_id = capture.session_id
    manifest_hash = capture.manifest_hash
    if archive_path is None:
        if not archive_sha256:
            return ArchiveIdentity()
        if len(archive_sha256) != 64 or not all(
            c in "0123456789abcdef" for c in archive_sha256
        ):
            raise CaptureCertificationError(
                f"archive_sha256 is {archive_sha256!r}; a full lowercase "
                "SHA-256 is required even for an unverified claim"
            )
        return ArchiveIdentity(
            sha256=archive_sha256,
            provenance="UNVERIFIED_EXTERNAL_ARCHIVE_DIGEST_CLAIM",
            mismatch_reasons=(
                "no archive was opened; a caller's digest cannot be tied to "
                "this capture",
            ),
        )

    path = pathlib.Path(archive_path)
    if not path.is_file():
        raise CaptureCertificationError(f"{path} is not a file")
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if archive_sha256 and archive_sha256 != digest:
        raise CaptureCertificationError(
            f"{path} hashes to {digest} and the caller expected {archive_sha256}"
        )
    seen = _archive_contents(
        path,
        session_id=session_id,
        manifest_hash=manifest_hash,
        rebuilt_manifest=capture.rebuilt_manifest,
        run_intent_sha256=capture.run_intent_sha256,
    )
    return ArchiveIdentity(
        sha256=digest,
        provenance="VERIFIED_FROM_BYTES",
        byte_length=len(body),
        contains_capture_manifest=seen.contains_capture_manifest,
        matches_capture=seen.matches,
        payloads_verified=seen.payloads_verified,
        records_expected=seen.records_expected,
        manifest_recomputed=seen.manifest_recomputed,
        run_intent_verified=seen.run_intent_verified,
        archive_run_intent_sha256=seen.archive_run_intent_sha256,
        mismatch_reasons=seen.reasons,
    )


@dataclass(frozen=True, slots=True)
class EndpointIdentities:
    """Row counts against identity counts, per endpoint.

    Set comparison silently collapses duplicates: a listing of 551 rows with one
    identity repeated has 550 unique identities and compares equal to a 550-row
    snapshot. v2.1.23 called that
    ``DEDICATED_CONTRACT_LIST_MATCHED_SNAPSHOT_UNIVERSE``, which is a much
    stronger claim than the evidence supports -- a vendor returning a contract
    twice is a vendor doing something the certification has not characterised.
    """

    endpoint: str
    row_count: int
    unique_identity_count: int
    duplicate_identity_count: int
    duplicate_identity_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "row_count": self.row_count,
            "unique_identity_count": self.unique_identity_count,
            "duplicate_identity_count": self.duplicate_identity_count,
            "duplicate_identity_hash": self.duplicate_identity_hash,
        }


def _identities(
    endpoint: str, rows: list[dict[str, str]]
) -> tuple[EndpointIdentities, set[ContractKey]]:
    seen: dict[ContractKey, int] = defaultdict(int)
    for row in rows:
        seen[ContractKey.from_row(row)] += 1
    duplicates = {key for key, count in seen.items() if count > 1}
    return (
        EndpointIdentities(
            endpoint=endpoint,
            row_count=len(rows),
            unique_identity_count=len(seen),
            duplicate_identity_count=len(duplicates),
            duplicate_identity_hash=_set_hash(duplicates) if duplicates else "",
        ),
        set(seen),
    )


@dataclass(frozen=True, slots=True)
class UniverseCertification:
    """Whether the dedicated contract listing described the snapshot universe.

    Set hashes rather than counts. Two responses of 14,556 rows each can hold
    different contracts, and a count comparison would call that a match.
    """

    contract_list_count: int
    quote_count: int
    greeks_count: int
    contract_list_hash: str
    quote_hash: str
    greeks_hash: str
    quote_matches_list: bool
    greeks_matches_list: bool
    only_in_list: tuple[str, ...]
    only_in_snapshots: tuple[str, ...]
    identities: tuple[EndpointIdentities, ...] = ()

    @property
    def duplicate_identity_count(self) -> int:
        return sum(entry.duplicate_identity_count for entry in self.identities)

    @property
    def state(self) -> str:
        """The evidence state this capture supports for this request form.

        ``DEDICATED_CONTRACT_LIST_MATCHED_SNAPSHOT_UNIVERSE`` means exactly
        this: every contract the dedicated listing returned for this
        symbol/date/max-DTE scope is present in both snapshots, neither snapshot
        carried anything the listing did not, **and no endpoint returned the
        same identity twice**. It says nothing about another date, another
        symbol, another tier or another endpoint family.

        The duplicate clause is not pedantry. Set equality survives duplication,
        so without it a listing that repeated a contract certified as an exact
        match -- the strongest state in the vocabulary, awarded to a response
        nobody had characterised.
        """
        if not (self.quote_matches_list and self.greeks_matches_list):
            return "DEDICATED_CONTRACT_LIST_OBSERVED_UNVERIFIED"
        if self.duplicate_identity_count:
            return "DEDICATED_CONTRACT_LIST_MATCHED_WITH_DUPLICATE_IDENTITIES"
        return "DEDICATED_CONTRACT_LIST_MATCHED_SNAPSHOT_UNIVERSE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_list_count": self.contract_list_count,
            "quote_count": self.quote_count,
            "greeks_count": self.greeks_count,
            "contract_list_set_hash": self.contract_list_hash,
            "quote_set_hash": self.quote_hash,
            "greeks_set_hash": self.greeks_hash,
            "quote_matches_list": self.quote_matches_list,
            "greeks_matches_list": self.greeks_matches_list,
            "only_in_list": list(self.only_in_list[:50]),
            "only_in_snapshots": list(self.only_in_snapshots[:50]),
            "endpoint_identities": [entry.as_dict() for entry in self.identities],
            "duplicate_identity_count": self.duplicate_identity_count,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class OpenInterestCoverage:
    """How much of the universe the open-interest endpoint actually answered.

    **Both directions.** v2.1.24 computed ``expected - answered`` and stopped,
    which measures only one of the two ways the sets can differ. An
    open-interest row for a contract the listing never named went straight into
    the numerator: 567 answered identities against a 566-contract universe gave
    a ``coverage_ratio`` of 1.0018 and ``permits_trusted_aggregate = true``,
    reporting better-than-complete coverage of a universe it did not cover.

    An unexpected identity is not a bonus. It means the listing and the
    open-interest response disagree about what the universe *is*, and a weight
    for a contract outside the universe is a weight nothing will consume.
    """

    universe_count: int
    present_count: int
    explicit_zero_count: int
    missing_count: int
    missing_by_expiration: tuple[tuple[str, int, int], ...]
    missing_identities_hash: str
    fully_missing_expirations: tuple[str, ...]
    #: Answered by open interest, absent from the expected universe.
    unexpected_count: int = 0
    unexpected_identities_hash: str = ""
    unexpected_by_expiration: tuple[tuple[str, int], ...] = ()
    #: Identities the open-interest response returned more than once.
    duplicate_count: int = 0

    @property
    def covered_count(self) -> int:
        """Expected identities the response actually answered.

        Bounded by construction: it counts members of the *intersection*, so it
        cannot exceed ``universe_count`` and the ratio below cannot exceed 1.
        v2.1.24 divided the raw answered count by the universe size, which is a
        different quantity that merely coincides when the sets agree.
        """
        return self.universe_count - self.missing_count

    @property
    def coverage_ratio(self) -> float:
        """In ``[0, 1]`` by definition rather than by clipping."""
        if not self.universe_count:
            return 0.0
        return self.covered_count / self.universe_count

    @property
    def permits_trusted_aggregate(self) -> bool:
        """Whether a trusted aggregate GEX may be computed over this universe.

        False while any identity is ``OI_MISSING``, ``OI_UNEXPECTED`` or
        duplicated. There is no evidence-backed policy for an absent
        open-interest row -- the honest options are to exclude the contract and
        report reduced coverage, or to refuse -- and until one is chosen and
        justified, computing an aggregate would be choosing silently. An
        unexpected identity is worse than absent: the two endpoints disagree
        about the universe, and neither is authoritative over the other here.
        """
        return (
            self.missing_count == 0
            and self.unexpected_count == 0
            and self.duplicate_count == 0
        )

    @property
    def state(self) -> str:
        """The coverage relationship, named rather than inferred from counts."""
        if self.duplicate_count:
            return "OI_DUPLICATE_IDENTITIES"
        if self.missing_count and self.unexpected_count:
            return "OI_MISSING_AND_UNEXPECTED"
        if self.missing_count:
            return "OI_MISSING"
        if self.unexpected_count:
            return "OI_UNEXPECTED"
        return "OI_MATCHES_EXPECTED_UNIVERSE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "universe_count": self.universe_count,
            "oi_present": self.present_count,
            "oi_explicit_zero": self.explicit_zero_count,
            "oi_missing": self.missing_count,
            "oi_covered": self.covered_count,
            "coverage_ratio": self.coverage_ratio,
            "coverage_state": self.state,
            "missing_by_expiration": [
                {"expiration": e, "listed": listed, "missing": missing}
                for e, listed, missing in self.missing_by_expiration
            ],
            "fully_missing_expirations": list(self.fully_missing_expirations),
            "missing_identities_hash": self.missing_identities_hash,
            "unexpected_oi_count": self.unexpected_count,
            "unexpected_oi_identities_hash": self.unexpected_identities_hash,
            "unexpected_oi_by_expiration": [
                {"expiration": e, "unexpected": count}
                for e, count in self.unexpected_by_expiration
            ],
            "duplicate_oi_identity_count": self.duplicate_count,
            "permits_trusted_aggregate": self.permits_trusted_aggregate,
        }


@dataclass(frozen=True, slots=True)
class EndpointSynchrony:
    """How far apart two sequentially acquired snapshots actually were.

    Quote and Greeks were separate HTTP requests. Joining them on contract
    identity and calling the result one observation would be asserting an
    atomicity the capture disproves.
    """

    overlap: int
    identical_timestamp_ratio: float
    identical_bid_ratio: float
    identical_ask_ratio: float
    p99_gap_seconds: float
    max_gap_seconds: float

    @property
    def is_atomic(self) -> bool:
        return self.max_gap_seconds == 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "overlap": self.overlap,
            "identical_timestamp_ratio": self.identical_timestamp_ratio,
            "identical_bid_ratio": self.identical_bid_ratio,
            "identical_ask_ratio": self.identical_ask_ratio,
            "p99_gap_seconds": self.p99_gap_seconds,
            "max_gap_seconds": self.max_gap_seconds,
            "is_atomic": self.is_atomic,
        }


# ---------------------------------------------------------------------------
# Numerical reconstruction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HypothesisScore:
    """One candidate convention and how well it reproduced the vendor."""

    hypothesis: str
    rows: int
    median_abs_delta_error: float
    delta_rmse: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis,
            "rows": self.rows,
            "median_abs_delta_error": self.median_abs_delta_error,
            "delta_rmse": self.delta_rmse,
        }


@dataclass(frozen=True, slots=True)
class ExpirationClockReading:
    """The time-to-expiry the vendor used for one expiration, read by inversion.

    Deliberately carries no reference to any particular close. v2.1.22 stored
    ``intraday_days_to_1600`` and classified a reading by its distance from
    that, which cannot describe a vendor using any other time and made the
    16:00 conclusion a property of the code rather than of the capture.
    """

    expiration: str
    rows: int
    implied_days: float
    calendar_days: int
    spread: float

    @property
    def offset_from_calendar(self) -> float:
        """Implied minus calendar days: the intraday component, if any."""
        return self.implied_days - self.calendar_days

    @property
    def is_whole_day_regime(self) -> bool:
        return abs(self.offset_from_calendar) < INTRADAY_OFFSET_THRESHOLD_DAYS

    @property
    def is_intraday_regime(self) -> bool:
        return 0.0 < self.offset_from_calendar < 1.0 and not self.is_whole_day_regime

    def as_dict(self) -> dict[str, Any]:
        return {
            "expiration": self.expiration,
            "rows": self.rows,
            "implied_days": self.implied_days,
            "calendar_days": self.calendar_days,
            "spread": self.spread,
            "offset_from_calendar": self.offset_from_calendar,
            "is_intraday_regime": self.is_intraday_regime,
            "is_whole_day_regime": self.is_whole_day_regime,
        }


#: An implied offset smaller than this is a whole-day reading; larger is an
#: intraday one. Roughly seventy minutes -- far above the inversion's spread on
#: the real capture (0.0015-0.0198 for whole days) and far below any plausible
#: intraday offset from a mid-session valuation stamp.
INTRADAY_OFFSET_THRESHOLD_DAYS = 0.05


@dataclass(frozen=True, slots=True)
class _Reconstruction:
    """One (rate, day-count) hypothesis, carried all the way through.

    Holds the score *and* everything the score depended on, so the winning
    combination's clock and regime split are the ones that were actually
    measured under it rather than recomputed afterwards under a different
    assumption.
    """

    rate: float
    days_per_year: float
    readings: tuple[ExpirationClockReading, ...]
    evidence: ExpirationClockEvidence
    boundary: date | None
    roots: RootResolution
    rows: int
    median_abs_delta_error: float
    delta_rmse: float

    def labelled(self, hypothesis: str) -> HypothesisScore:
        """The same numbers, named for whichever dimension is being compared."""
        return HypothesisScore(
            hypothesis=hypothesis,
            rows=self.rows,
            median_abs_delta_error=self.median_abs_delta_error,
            delta_rmse=self.delta_rmse,
        )


@dataclass(frozen=True, slots=True)
class ExpirationClockEvidence:
    """Which expirations support an intraday clock, and which refuse it.

    The first capture prices expirations inside its own week to a 16:00 close
    and everything later on whole calendar days. Recording that as
    ``EXPIRATION_TIMESTAMP = 16:00 ET`` with a sentence of prose about scope
    would leave a downstream consumer with a global-looking rule and no way to
    discover it is not one. So the scope is data:

    * ``intraday_expirations`` are the ones that demonstrate the clock;
    * ``whole_day_expirations`` are the ones that contradict applying it
      globally, named individually rather than counted;
    * ``boundary_status`` says whether the transition between the two regimes
      was actually observed.

    On the first capture it was not. The sample jumps from four days out to
    seven with nothing in between, so the rule could be "expiring this week",
    "five business days" or "under seven days" and this capture cannot tell
    them apart. ``OPEN`` is the honest answer and it is machine-readable.
    """

    intraday_expirations: tuple[str, ...]
    whole_day_expirations: tuple[str, ...]
    unexplained_expirations: tuple[str, ...]
    #: The time of day the intraday regime implies, read out of the inversion
    #: rather than matched against a guess. Empty when nothing is intraday.
    implied_clock_et: str
    implied_clock_seconds: float
    boundary_last_intraday: str
    boundary_first_whole_day: str
    boundary_gap_days: int

    @property
    def boundary_status(self) -> str:
        if not self.intraday_expirations or not self.whole_day_expirations:
            return "NOT_OBSERVED"
        # Adjacent calendar days pin the transition exactly. Anything wider
        # leaves room for a rule this capture never tested.
        return "RESOLVED" if self.boundary_gap_days <= 1 else "OPEN"

    @property
    def scope_is_global(self) -> bool:
        """Whether the intraday clock may be applied to every expiration."""
        return bool(self.intraday_expirations) and not self.whole_day_expirations

    @property
    def contradicting_count(self) -> int:
        return len(self.whole_day_expirations)

    def as_dict(self) -> dict[str, Any]:
        return {
            "implied_clock_et": self.implied_clock_et,
            "implied_clock_seconds_after_midnight": self.implied_clock_seconds,
            "intraday_expirations": list(self.intraday_expirations),
            "intraday_count": len(self.intraday_expirations),
            "whole_day_expirations": list(self.whole_day_expirations),
            "contradicting_count": self.contradicting_count,
            "unexplained_expirations": list(self.unexplained_expirations),
            "boundary_last_intraday": self.boundary_last_intraday,
            "boundary_first_whole_day": self.boundary_first_whole_day,
            "boundary_gap_days": self.boundary_gap_days,
            "boundary_status": self.boundary_status,
            "scope_is_global": self.scope_is_global,
        }


def _clock_evidence(
    readings: tuple[ExpirationClockReading, ...], valuation: datetime
) -> ExpirationClockEvidence:
    """Split the inversion into regimes and read the clock out of it.

    ``implied_days - calendar_days`` is the gap between the valuation stamp's
    time of day and the expiry's, so the intraday readings *state* the clock.
    v2.1.22 instead scored a fixed list of candidate times and labelled the
    winner ``16:00 America/New_York`` from a constant, which cannot report a
    vendor that moved its close and cannot be tested against a capture
    generated at any other time.
    """
    intraday: list[ExpirationClockReading] = []
    whole: list[ExpirationClockReading] = []
    unexplained: list[str] = []
    for reading in readings:
        offset = reading.offset_from_calendar
        if abs(offset) < INTRADAY_OFFSET_THRESHOLD_DAYS:
            whole.append(reading)
        elif 0.0 < offset < 1.0:
            intraday.append(reading)
        else:
            # Neither regime. Recorded rather than forced into one, because a
            # reading a full day out is a finding, not a rounding error.
            unexplained.append(reading.expiration)

    clock_seconds = 0.0
    clock_text = ""
    if intraday:
        midnight = valuation.replace(hour=0, minute=0, second=0, microsecond=0)
        offset_seconds = statistics.median(
            r.offset_from_calendar * 86400.0 for r in intraday
        )
        clock_seconds = (valuation - midnight).total_seconds() + offset_seconds
        # To the nearest minute: the inversion carries the implied-vol rounding
        # noise, and a label of 15:59:58 would imply a precision the reported
        # four-decimal volatility cannot support.
        minute = round(clock_seconds / 60.0)
        clock_text = f"{minute // 60 % 24:02d}:{minute % 60:02d} America/New_York"

    last_intraday = max((r.expiration for r in intraday), default="")
    later = sorted(r.expiration for r in whole if r.expiration > last_intraday)
    first_whole = (
        later[0] if later else (min((r.expiration for r in whole), default=""))
    )
    gap = 0
    if last_intraday and first_whole:
        gap = (date.fromisoformat(first_whole) - date.fromisoformat(last_intraday)).days

    return ExpirationClockEvidence(
        intraday_expirations=tuple(sorted(r.expiration for r in intraday)),
        whole_day_expirations=tuple(sorted(r.expiration for r in whole)),
        unexplained_expirations=tuple(sorted(unexplained)),
        implied_clock_et=clock_text,
        implied_clock_seconds=clock_seconds,
        boundary_last_intraday=last_intraday,
        boundary_first_whole_day=first_whole,
        boundary_gap_days=gap,
    )


def _clock_time(evidence: ExpirationClockEvidence) -> time:
    """The derived clock as a ``time``.

    Falls back to the regular US equity-index close only when *no* expiration
    showed an intraday component at all -- in which case the value is never
    used to classify anything, because every expiration is whole-day.
    """
    if not evidence.implied_clock_et:
        return time(16, 0)
    hh, mm = evidence.implied_clock_et.split(" ")[0].split(":")
    return time(int(hh), int(mm))


def _clock_candidates(derived: time) -> tuple[time, ...]:
    """The derived clock, the round time beside it, and near neighbours.

    Built around whatever was derived rather than from a fixed list, so the
    comparison discriminates for any close a vendor might use. A fixed list
    containing the answer proves only that the answer was on the list.

    Candidates sit on a five-minute grid around the derived estimate because
    the estimator cannot resolve better than that. ``implied_vol`` is reported
    to four decimals, and on the first capture the implied offset drifts with
    maturity -- 0.2490 at the front, 0.2503 a week out -- which is enough to
    move the median by a minute. Offering a per-minute candidate lets that
    noise win: 16:01 scores 3.10e-04 against 16:00's 3.48e-04 on a capture
    whose close is 16:00, and reporting a one-minute-off close as a vendor
    convention would be over-reading the data.

    Nothing is hidden by the snap. The unrounded estimate stays on
    :class:`ExpirationClockEvidence` as ``implied_clock_et``, so a reader sees
    both what was measured and what was resolved, and can disagree.
    """
    exact = derived.hour * 60 + derived.minute
    snapped = round(exact / 5.0) * 5
    # A *continuous* five-minute grid rather than a few offsets. Sparse
    # neighbours can miss the answer outright: a derived 15:33 snaps to 15:35,
    # and a grid of +/-15 and +/-30 around that never contains the 15:30 the
    # vendor actually used. Half an hour either side at five-minute steps
    # covers every plausible estimation error while still being a comparison
    # a wrong hypothesis loses.
    minutes = {snapped + step for step in range(-30, 35, 5)}
    return tuple(
        time((m // 60) % 24, m % 60) for m in sorted(minutes) if 0 <= m < 24 * 60
    )


def _invert_clock(
    rows: list[dict[str, str]],
    *,
    spot: float,
    valuation: datetime,
    rate: float,
    days_per_year: float,
) -> tuple[tuple[ExpirationClockReading, ...], RootResolution]:
    """Read the vendor's time-to-expiry per expiration, under one hypothesis.

    ``days_per_year`` matters as much as the rate. The inversion recovers a
    year fraction, and turning that back into days needs the vendor's own
    denominator: read a 360-day vendor at 365 and every implied day is 1.4%
    long, which is enough to push a whole-calendar-day expiration past the
    intraday threshold and misclassify the regime entirely.
    """
    by_expiration: dict[str, list[float]] = defaultdict(list)
    counts = {"single": 0, "disambiguated": 0, "ambiguous": 0, "inconsistent": 0}
    for row in rows:
        key = ContractKey.from_row(row)
        try:
            delta_value = float(row["delta"])
            sigma = float(row["implied_vol"])
            strike = float(row["strike"])
        except (KeyError, ValueError):
            continue
        if not (0.05 <= abs(delta_value) <= 0.95):
            continue
        candidates = _implied_year_candidates(
            spot=spot,
            strike=strike,
            sigma=sigma,
            delta_value=delta_value,
            rate=rate,
            right=key.option_right,
        )
        years, outcome = _resolve_root(
            candidates,
            expiration=key.expiration,
            valuation=valuation,
            days_per_year=days_per_year,
        )
        counts[outcome] += 1
        if years is not None and years > 0.0:
            by_expiration[key.expiration.isoformat()].append(years * days_per_year)

    readings: list[ExpirationClockReading] = []
    for exp_text in sorted(by_expiration):
        values = sorted(by_expiration[exp_text])
        if len(values) < MINIMUM_ROWS_PER_EXPIRATION:
            continue
        lo = values[int(0.05 * len(values))]
        hi = values[min(int(0.95 * len(values)), len(values) - 1)]
        readings.append(
            ExpirationClockReading(
                expiration=exp_text,
                rows=len(values),
                implied_days=statistics.median(values),
                calendar_days=(date.fromisoformat(exp_text) - valuation.date()).days,
                spread=hi - lo,
            )
        )
    resolution = RootResolution(
        single_root_rows=counts["single"],
        disambiguated_multi_root_rows=counts["disambiguated"],
        ambiguous_root_rows=counts["ambiguous"],
        inconsistent_root_rows=counts["inconsistent"],
    )
    return tuple(readings), resolution


def _parse_et(text: str) -> datetime:
    return datetime.fromisoformat(text.strip().strip('"')).replace(tzinfo=EASTERN)


def _usable_greeks(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Rows that can speak to a pricing hypothesis at all.

    A zero implied volatility is a failed solve, not a quiet market, and a delta
    pinned at 0 or +/-1 is the boundary the formula returns for anything far
    enough from the money -- it would agree with every hypothesis equally and
    dilute each score by the same amount.
    """
    out = []
    for row in rows:
        try:
            iv = float(row["implied_vol"])
            d = float(row["delta"])
        except (KeyError, ValueError):
            continue
        if iv > 0.0 and 0.0 < abs(d) < 1.0:
            out.append(row)
    return out


def _implied_year_candidates(
    *,
    spot: float,
    strike: float,
    sigma: float,
    delta_value: float,
    rate: float,
    right: OptionRight,
) -> tuple[float, ...]:
    """Every time-to-expiry consistent with the vendor's own delta.

    ``delta`` fixes ``d1``; ``d1`` and ``sigma`` fix ``T`` through a quadratic
    in ``sqrt(T)``. A quadratic has two roots, and for an out-of-the-money
    contract -- where ``log(S/K)`` and the drift term carry the same sign --
    **both can be positive**. They are both genuine solutions of the equation;
    only one of them is the vendor's clock.

    v2.1.23 returned the first positive root it found, which is the larger one.
    On the synthetic capture at ``wire=0.042`` that was wrong for 148 of 162
    two-root rows: one contract's true ``T`` is 0.006161 years and the roots are
    9.012970 and 0.006164, so the reconstruction reported nine years for a
    two-day option. The aggregate still passed, because a median over mostly
    single-root rows survives a minority of absurd ones -- and the clock
    readings quietly carried spreads of 2,737 days. A test that reports the
    right close while accepting a two-thousand-day spread is not testing the
    inversion. The real first capture has 818 two-root rows.

    So all positive roots come back, and the caller resolves them against
    something it legitimately knows independently -- see
    :func:`_resolve_root`. Returning one root and hoping is the defect.
    """
    nd = NormalDist()
    try:
        d1 = (
            nd.inv_cdf(delta_value)
            if right is OptionRight.CALL
            else -nd.inv_cdf(-delta_value)
        )
    except (ValueError, statistics.StatisticsError):
        return ()
    a = rate + 0.5 * sigma * sigma
    b = -d1 * sigma
    c = math.log(spot / strike)
    disc = b * b - 4.0 * a * c
    if disc < 0.0 or a == 0.0:
        return ()
    roots = []
    for sign in (1.0, -1.0):
        x = (-b + sign * math.sqrt(disc)) / (2.0 * a)
        if x > 0.0:
            roots.append(x * x)
    return tuple(sorted(roots))


def _decide(
    ranked: list[float],
    *,
    rows: int,
    identical_hypotheses: bool,
    floor: float = DELTA_NOISE_FLOOR,
    adequate: float | None = None,
) -> InferenceDecision:
    """Did this comparison settle anything, or merely produce a minimum?

    ``min()`` always returns something. v2.1.23 treated that as the answer, so
    with ``rate_value=0`` -- where the decimal and percent hypotheses are the
    same computation and score identically to the last bit -- it reported
    ``DECIMAL_ANNUAL_RATE`` and a documentation conflict, because decimal was
    first in the tuple. The verdict came from list order.

    The thresholds come from the precision the vendor reports its own fields
    at, not from taste. Two hypotheses whose scores differ by less than the
    rounding noise in ``delta`` are not distinguishable *by this data*, however
    much of it there is.
    """
    if identical_hypotheses:
        return InferenceDecision.NOT_IDENTIFIABLE
    if rows < MINIMUM_ROWS_FOR_INFERENCE or not ranked:
        return InferenceDecision.INSUFFICIENT_DATA
    limit = adequate if adequate is not None else ADEQUATE_FIT_NOISE_FLOORS * floor
    if ranked[0] > limit:
        return InferenceDecision.NO_ADEQUATE_FIT
    if len(ranked) > 1 and (ranked[1] - ranked[0]) < floor:
        return InferenceDecision.AMBIGUOUS
    return InferenceDecision.RESOLVED


@dataclass(frozen=True, slots=True)
class RootResolution:
    """How the inversion's ambiguity was settled, counted rather than hidden."""

    single_root_rows: int = 0
    disambiguated_multi_root_rows: int = 0
    ambiguous_root_rows: int = 0
    inconsistent_root_rows: int = 0

    @property
    def usable_rows(self) -> int:
        return self.single_root_rows + self.disambiguated_multi_root_rows

    def as_dict(self) -> dict[str, Any]:
        return {
            "single_root_rows": self.single_root_rows,
            "disambiguated_multi_root_rows": self.disambiguated_multi_root_rows,
            "ambiguous_root_rows": self.ambiguous_root_rows,
            "inconsistent_root_rows": self.inconsistent_root_rows,
            "usable_rows": self.usable_rows,
        }


def _expiry_day_window(expiration: date, valuation: datetime) -> tuple[float, float]:
    """The days-to-expiry range compatible with expiring *on that date*.

    The contract expires at some instant on calendar date ``D``. Whatever time
    of day that is, the distance from the valuation stamp must lie between "the
    start of D" and "the end of D". That bound uses only the valuation
    timestamp and the expiration date -- both known independently of anything
    being inferred -- so it can discriminate roots without assuming the close it
    is trying to discover.

    Deliberately *not* 16:00. Using the answer to select the evidence for the
    answer is how a reconstruction confirms whatever it already believed.
    """
    midnight = valuation.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (valuation - midnight).total_seconds() / 86400.0
    calendar_days = (expiration - valuation.date()).days
    return calendar_days - elapsed, calendar_days + 1.0 - elapsed


def _resolve_root(
    candidates: tuple[float, ...],
    *,
    expiration: date,
    valuation: datetime,
    days_per_year: float,
) -> tuple[float | None, str]:
    """Pick the root that could actually be an instant on the expiration date.

    Returns ``(years_or_None, outcome)`` where outcome is one of ``single``,
    ``disambiguated``, ``ambiguous`` or ``inconsistent``. Ambiguity is reported,
    never resolved by iteration order.
    """
    if not candidates:
        return None, "inconsistent"
    low, high = _expiry_day_window(expiration, valuation)
    possible = [years for years in candidates if low <= years * days_per_year <= high]
    if len(possible) == 1:
        return possible[0], ("single" if len(candidates) == 1 else "disambiguated")
    if len(possible) > 1:
        return None, "ambiguous"
    return None, "inconsistent"


def _score(
    rows: list[dict[str, str]],
    *,
    spot: float,
    valuation: datetime,
    rate: float,
    days_per_year: float,
    expiry_time: time,
    label: str,
    whole_days_from: date | None = None,
) -> HypothesisScore | None:
    """Score one convention by reproducing the vendor's delta with our pricer."""
    errors: list[float] = []
    for row in rows:
        exp = date.fromisoformat(row["expiration"].strip().strip('"'))
        if whole_days_from is not None and exp > whole_days_from:
            days = float((exp - valuation.date()).days)
        else:
            expiry_dt = datetime.combine(exp, expiry_time, tzinfo=EASTERN)
            days = (expiry_dt - valuation).total_seconds() / 86400.0
        if days <= 0.0:
            continue
        try:
            sigma = float(row["implied_vol"])
            reported = float(row["delta"])
            strike = float(row["strike"])
        except ValueError:
            continue
        key = ContractKey.from_row(row)
        inputs = BlackScholesInputs(
            spot=spot,
            strike=strike,
            time_to_expiry=days / days_per_year,
            implied_vol=sigma,
            rate=rate,
        )
        if inputs.is_degenerate():
            continue
        errors.append(abs(bs_delta(inputs, key.option_right) - reported))
    if not errors:
        return None
    return HypothesisScore(
        hypothesis=label,
        rows=len(errors),
        median_abs_delta_error=statistics.median(errors),
        delta_rmse=math.sqrt(sum(e * e for e in errors) / len(errors)),
    )


def _solve_iv(
    target: float,
    *,
    spot: float,
    strike: float,
    years: float,
    rate: float,
    right: OptionRight,
) -> float | None:
    """Bisect for the volatility that reprices ``target``. Deterministic."""

    def price(sigma: float) -> float:
        sq = sigma * math.sqrt(years)
        d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * years) / sq
        d2 = d1 - sq
        if right is OptionRight.CALL:
            return spot * norm_cdf(d1) - strike * math.exp(-rate * years) * norm_cdf(d2)
        return strike * math.exp(-rate * years) * norm_cdf(-d2) - spot * norm_cdf(-d1)

    lo, hi = 1e-6, 30.0
    try:
        if price(lo) > target or price(hi) < target:
            return None
        for _ in range(120):
            mid = 0.5 * (lo + hi)
            if price(mid) < target:
                lo = mid
            else:
                hi = mid
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------------------
# Two different questions about the rate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptureRateEconomics:
    """Whether the vendor priced the rate we meant -- a separate question.

    v2.1.22 ran these together. The documentation/live conflict was the reason
    the first capture was blocked from a GEX, which was right by accident: the
    capture was blocked because it was priced at 420%, and the conflict is why
    that happened, but they are not the same fact and they do not have the same
    lifetime.

    The conflict is permanent. It is a statement about the vendor's published
    description and it stays true until ThetaData changes one side or the other.
    Economic correctness is a statement about *one capture's request*, and the
    corrected profile fixes it while the conflict remains exactly as recorded.
    Left conflated, a correct second capture would inherit the first one's
    blocker forever and no amount of fixing the request would clear it.
    """

    wire_rate_value: float
    intended_economic_rate: float
    #: How the intended rate was established. A capture that declared its own
    #: is believed; one that did not is read under the vendor's documented unit,
    #: which is what an operator following the documentation would have meant.
    intended_rate_source: str
    documented_unit: str
    #: **Only when the live comparison actually discriminated.**
    #:
    #: v2.1.24 filled these from the numerical minimum whatever the inference
    #: decision was, so a synthetic capture whose ``RATE_UNITS`` came out
    #: ``AMBIGUOUS`` still published an observed unit, an effective rate of
    #: 0.001, ``matches intended = true`` and a documentation conflict -- four
    #: definite economic claims resting on a comparison the same report
    #: described as undecided.
    observed_unit: str = ""
    vendor_effective_rate: float | None = None

    #: Same rate to within a rounding of the wire text. Nowhere near the factor
    #: of a hundred this exists to catch.
    TOLERANCE: ClassVar[float] = 1e-9

    @property
    def vendor_rate_identified(self) -> bool:
        """Whether the vendor's effective rate was established at all.

        Everything below is a question *about* that rate. With it unidentified
        they have no answers -- not false ones.
        """
        return bool(self.observed_unit) and self.vendor_effective_rate is not None

    @property
    def documentation_live_conflict(self) -> bool | None:
        """Does the vendor read its own parameter as it documents it?

        ``None`` when the observed reading was not established, or when the
        capture carries no documentary reading to compare against. Deliberately
        three-valued: a ``False`` here would say the vendor agrees with its
        documentation, which is a finding, and "we could not tell" is not one.
        """
        if not self.vendor_rate_identified or not self.documented_unit:
            return None
        return self.observed_unit != self.documented_unit

    @property
    def effective_rate_matches_intended(self) -> bool | None:
        """Did the vendor price the rate this capture meant to buy?"""
        if self.vendor_effective_rate is None:
            return None
        return (
            abs(self.vendor_effective_rate - self.intended_economic_rate)
            <= self.TOLERANCE
        )

    @property
    def magnitude_ratio(self) -> float | None:
        if self.vendor_effective_rate is None:
            return None
        if self.intended_economic_rate == 0.0:
            return 1.0 if self.vendor_effective_rate == 0.0 else float("inf")
        return self.vendor_effective_rate / self.intended_economic_rate

    @property
    def blocker(self) -> str:
        """Why this capture's Greeks cannot support a GEX, if they cannot."""
        if not self.vendor_rate_identified:
            return (
                "the rate this vendor actually priced with was not identified "
                "from this capture: the reconstruction could not distinguish "
                "the candidate readings of rate_value. Without it there is no "
                "way to say whether the Greeks describe the intended market, "
                "and an unverified rate is not a working one."
            )
        matches = self.effective_rate_matches_intended
        if matches:
            return ""
        assert self.vendor_effective_rate is not None  # implied by identified
        return (
            f"the vendor priced this capture at r={self.vendor_effective_rate:g} "
            f"and the request meant r={self.intended_economic_rate:g}, a factor "
            f"of {self.magnitude_ratio:g}. Every implied volatility and every "
            "delta in it describes a market that does not exist. The capture "
            "remains the evidence that established the rate semantics and must "
            "not be discarded."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "wire_rate_value": self.wire_rate_value,
            "vendor_rate_identified": self.vendor_rate_identified,
            "vendor_effective_rate": self.vendor_effective_rate,
            "intended_economic_rate": self.intended_economic_rate,
            "intended_rate_source": self.intended_rate_source,
            "observed_vendor_rate_unit": self.observed_unit,
            "documented_rate_unit": self.documented_unit,
            "rate_units_documentation_live_conflict": (
                self.documentation_live_conflict
            ),
            "capture_effective_rate_matches_intended_rate": (
                self.effective_rate_matches_intended
            ),
            "magnitude_ratio": self.magnitude_ratio,
        }


def _rate_economics(
    capture: LoadedCapture,
    *,
    wire_rate: float,
    effective_rate: float | None,
    observed_unit: str,
    documented_unit: str,
) -> CaptureRateEconomics:
    """What the capture meant to buy, beside what the vendor actually priced.

    The intended rate cannot be recovered from the wire value alone -- that
    ambiguity is the entire defect -- so a capture has to say, and since v2.1.24
    it says so under a fingerprint bound into the preflight approval.

    Captures older than that binding carry no intent. Rather than guess, this
    reads the wire value under the unit *that capture's own documentation
    bundle* recorded -- the reading an operator following the documentation
    would have meant, which is exactly what happened for the first capture. The
    source is labelled ``LEGACY_CAPTURE_DOCUMENTATION_DERIVED`` so no reader
    mistakes it for something the capture proved, and it is refused outright for
    any capture new enough to have recorded a bound intent.
    """
    bound = capture.bound_rate_intent
    if bound is not None:
        source = "BOUND_TO_PREFLIGHT_APPROVAL"
        intended_rate = float(bound.economic_rate_decimal)
        # The intent's own documented unit is inside its fingerprint, which the
        # approval binds -- so it is the authority here. But an intent declaring
        # a documented unit the pinned document does not say is a capture whose
        # two documentary statements disagree, and picking either would be
        # choosing. Refuse instead.
        if documented_unit and str(bound.documented_rate_unit) != documented_unit:
            raise CaptureCertificationError(
                f"the bound rate intent declares the documentation reads "
                f"rate_value as {bound.documented_rate_unit} and the pinned "
                f"document re-derives to {documented_unit}. The capture's "
                "approved statement of intent and the document it names do not "
                "agree about what the vendor documents."
            )
        documented_unit = str(bound.documented_rate_unit) or documented_unit
    elif capture.records_are_post_binding:
        # A capture taken *after* the binding existed and carrying no intent is
        # not a legacy capture, it is an incomplete one. The fallback is for
        # history, not for filling gaps in current work.
        raise CaptureCertificationError(
            "this capture was taken under a schema that binds the economic "
            "rate intent, and it carries none. Certification will not fall "
            "back to a documentary reading for a capture that should have "
            "recorded what it meant; re-run the dry run and capture again."
        )
    else:
        source = "LEGACY_CAPTURE_DOCUMENTATION_DERIVED"
        factor = 0.01 if documented_unit == "PERCENT_ANNUAL_RATE" else 1.0
        intended_rate = wire_rate * factor
    return CaptureRateEconomics(
        wire_rate_value=wire_rate,
        vendor_effective_rate=effective_rate,
        intended_economic_rate=intended_rate,
        intended_rate_source=source,
        observed_unit=observed_unit,
        documented_unit=documented_unit,
    )


@dataclass(frozen=True, slots=True)
class AdmissibleModels:
    """The rate/day-count pairs still standing after an inference decision.

    v2.1.24 took the global minimum of the joint grid and used its rate, its
    denominator and the regime split that followed from them to infer the
    expiration clock and the IV price basis -- and then, separately, decided
    whether the rate comparison had discriminated at all. When the answer was
    ``AMBIGUOUS`` the downstream conclusions had already been computed from one
    arbitrary member of an undecided set, and nothing said so.

    A minimum always exists. Whether it is *the* model is a different question,
    and the honest downstream answer is: evaluate across everything still
    admissible, resolve only if they agree, and otherwise say they do not.
    """

    #: Every ``(unit, day_count_label)`` an inference cannot rule out.
    keys: tuple[tuple[str, str], ...]
    #: The single lowest-scoring pair, always defined, never authoritative on
    #: its own. Published beside the resolved answer so a reader can see the
    #: difference between "the best fit" and "the established model".
    numerical_best: tuple[str, str]

    @property
    def is_unique(self) -> bool:
        return len(self.keys) == 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "numerical_best": {
                "rate_interpretation": self.numerical_best[0],
                "day_count": self.numerical_best[1],
            },
            "resolved_selected": (
                {
                    "rate_interpretation": self.keys[0][0],
                    "day_count": self.keys[0][1],
                }
                if self.is_unique
                else None
            ),
            "admissible_model_count": len(self.keys),
            "admissible_models": [
                {"rate_interpretation": unit, "day_count": label}
                for unit, label in self.keys
            ],
        }


def _admissible(
    grid: dict[tuple[str, str], _Reconstruction],
    *,
    best: tuple[str, str],
    rate_decision: Any,
    day_count_decision: Any,
) -> AdmissibleModels:
    """Which grid entries survive the upstream decisions.

    A resolved dimension pins its own coordinate; an unresolved one leaves every
    value of it open. So an ambiguous rate with a resolved day count admits both
    readings at that denominator, and two unresolved dimensions admit the whole
    grid.
    """
    units = {best[0]} if rate_decision.is_resolved else {unit for unit, _ in grid}
    labels = (
        {best[1]} if day_count_decision.is_resolved else {label for _, label in grid}
    )
    keys = tuple(sorted(key for key in grid if key[0] in units and key[1] in labels))
    return AdmissibleModels(keys=keys or (best,), numerical_best=best)


def _collapse(outcomes: tuple[tuple[Any, str], ...]) -> tuple[Any, str]:
    """Collapse one downstream dimension across the admissible upstream paths.

    ``outcomes`` is one ``(decision, value)`` per path. Resolved only when every
    path resolved *and* they agree on the value: a conclusion that depends on
    which upstream model you believed is not established by data that could not
    choose between those models.

    Module-level so its branches can be exercised directly. A capture whose
    admissible paths genuinely *disagree* downstream is not something this
    repository can manufacture honestly -- the paths that survive an
    undiscriminated rate are numerically identical computations, so they agree
    by construction -- and a branch that has never run is a branch nobody has
    checked.
    """
    if not outcomes:
        return InferenceDecision.INSUFFICIENT_DATA, ""
    decisions = {decision for decision, _ in outcomes}
    values = {value for _, value in outcomes}
    if decisions == {InferenceDecision.RESOLVED}:
        # Every path settled it. Whether that is an answer depends on whether
        # they settled it the same way.
        return (
            (InferenceDecision.RESOLVED, next(iter(values)))
            if len(values) == 1
            else (InferenceDecision.AMBIGUOUS, "")
        )
    # Otherwise the weakest verdict wins, named rather than flattened to a
    # generic "unresolved", so a reader can tell insufficient data from a fit
    # that was adequate but undiscriminating.
    unresolved = sorted(d.value for d in decisions if not d.is_resolved)
    return InferenceDecision(unresolved[0]), ""


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptureCertificationReport:
    """Everything one capture established, and everything it did not."""

    schema_version: str
    session_id: str
    manifest_hash: str
    archive: ArchiveIdentity
    captured_at: str
    parser_version: str
    verified_records: int
    record_hashes: dict[str, str]
    universe: UniverseCertification
    open_interest: OpenInterestCoverage
    synchrony: EndpointSynchrony
    underlying: dict[str, Any]
    rate_scores: tuple[HypothesisScore, ...]
    day_count_scores: tuple[HypothesisScore, ...]
    expiration_time_scores: tuple[HypothesisScore, ...]
    iv_basis_scores: tuple[tuple[str, int, float, float], ...]
    clock_readings: tuple[ExpirationClockReading, ...]
    clock_evidence: ExpirationClockEvidence
    rate_economics: CaptureRateEconomics
    request: CaptureRequestBinding
    ledger: Any
    rows_reconstructed: int
    #: The winning labels, kept on the report so a consumer never has to infer
    #: them by re-scanning a score table.
    resolved_day_count: str
    resolved_expiration_clock: str
    #: Whether each comparison discriminated, by dimension name.
    decisions: dict[str, str]
    #: The whole rate x day-count search, not only the slices through it.
    joint_grid: tuple[dict[str, Any], ...]
    #: How the inversion's two-root ambiguity was settled.
    roots: RootResolution
    #: How the capture's economic intent was established, and against what.
    rate_intent_binding: dict[str, Any]
    #: The numerical minimum beside the model that was actually established --
    #: which are not the same thing when an upstream inference did not resolve.
    models: AdmissibleModels
    #: Where the documentary readings came from, and that they were re-derived.
    documentation: dict[str, Any]

    @property
    def archive_sha256(self) -> str:
        """The archive digest, or empty. Never a caller's unverified claim."""
        return self.archive.sha256 if self.archive.known else ""

    @property
    def resolved_dimensions(self) -> tuple[str, ...]:
        return tuple(
            o.dimension.value for o in self.ledger.observations if o.status.is_resolved
        )

    @property
    def unresolved_dimensions(self) -> tuple[str, ...]:
        return tuple(o.dimension.value for o in self.ledger.unresolved)

    @property
    def pricing_dimensions_supported(self) -> tuple[str, ...]:
        """Analytical pricing dimensions this evidence actually speaks to.

        Reported separately from the behaviour dimensions because the two
        vocabularies are not the same set. v2.1.23 published
        ``dimensions_unresolved: []`` from the behaviour ledger, which read as
        "every pricing dimension is settled" while ``UNDERLYING_TIMESTAMP`` was
        not in that ledger at all -- an empty list about one vocabulary
        answering a question asked about another.
        """
        return tuple(
            sorted(
                {
                    o.dimension.pricing_dimension
                    for o in self.ledger.observations
                    if o.status.is_resolved and o.dimension.pricing_dimension
                }
            )
        )

    @property
    def pricing_dimensions_unresolved(self) -> tuple[str, ...]:
        """Pricing dimensions the analytical layer needs that this cannot give.

        Everything the compatibility layer knows about, minus what this capture
        supports. Derived from the analytical enum rather than from a list kept
        here, so a dimension added there appears here as unresolved instead of
        going unmentioned.
        """
        from src.config.compatibility import PricingDimension

        supported = set(self.pricing_dimensions_supported)
        return tuple(
            sorted(d.value for d in PricingDimension if d.value not in supported)
        )

    @property
    def analytical_readiness(self) -> str:
        """What this capture may be used for.

        ``ADAPTER_CERTIFICATION_EVIDENCE``, and nothing stronger. A capture
        earns that label by being verifiable and reproducible, which this one
        is; it does not earn a GEX by being either.
        """
        return "ADAPTER_CERTIFICATION_EVIDENCE"

    @property
    def gex_blockers(self) -> tuple[str, ...]:
        """Why no GEX may be computed from this capture. Derived, not asserted.

        Listed rather than summarised because they are independent, and fixing
        the rate does not fix the open interest.

        **Disqualifying, not qualifying.** An empty list means certification
        found nothing wrong in what it looks at, which is not the same as a
        capture being usable -- see :attr:`trusted_for_gex`.
        """
        blockers: list[str] = []
        # The *economic* question, not the documentation one. A capture whose
        # request was built under the measured semantics prices the intended
        # rate and is not blocked here, while the documentation conflict it
        # still carries stays exactly as recorded.
        if self.rate_economics.blocker:
            blockers.append(self.rate_economics.blocker)
        if self.open_interest.missing_count:
            blockers.append(
                f"{self.open_interest.missing_count} contract identities have "
                "no open-interest row. Open interest is the linear weight on "
                "every GEX term and there is no evidence-backed policy for an "
                "absent one, so an aggregate would be choosing silently."
            )
        if self.open_interest.unexpected_count:
            blockers.append(
                f"{self.open_interest.unexpected_count} open-interest "
                "identities are not in the expected universe. The contract "
                "listing and the open-interest response disagree about which "
                "contracts exist, and neither is authoritative over the other "
                "here, so the universe an aggregate would run over is not "
                "established."
            )
        if self.open_interest.duplicate_count:
            blockers.append(
                f"{self.open_interest.duplicate_count} identities appear more "
                "than once in the open-interest response, which does not say "
                "which weight each contract carries."
            )
        return tuple(blockers)

    @property
    def trusted_for_gex(self) -> bool:
        """False. Not "false so far" -- false because this layer cannot say.

        v2.1.23 returned ``not self.gex_blockers``, which made the answer depend
        on whichever blockers this report happened to implement. A capture with
        a correct rate and complete open interest therefore came out
        ``trusted_for_gex = true`` beside an ``analytical_readiness`` of
        ``ADAPTER_CERTIFICATION_EVIDENCE`` -- the report contradicting its own
        docstring, and adapter certification quietly granting analytical
        authority.

        Rate correctness, open-interest completeness, universe equality and
        convention inference are *necessary* evidence for a trusted GEX. They
        are not jointly sufficient, and nothing here checks the rest: the
        settlement-date authority, the spot synchronisation policy, the
        pricing-compatibility matrix, the calculation gate. Those live behind
        the verified analytical-readiness path, which is a different layer with
        different inputs.

        So this is a constant, and :attr:`gex_blockers` stays as the list of
        what certification *can* see -- reasons a capture is disqualified, never
        a checklist whose emptiness qualifies it.
        """
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capture": {
                "session_id": self.session_id,
                "manifest_hash": self.manifest_hash,
                "captured_at": self.captured_at,
                "parser_version": self.parser_version,
                **self.archive.as_dict(),
            },
            "raw_record_verification": {
                "records_verified": self.verified_records,
                "record_payload_hashes": dict(sorted(self.record_hashes.items())),
            },
            "universe": self.universe.as_dict(),
            "open_interest_coverage": self.open_interest.as_dict(),
            "endpoint_synchrony": self.synchrony.as_dict(),
            "underlying_synchronization": self.underlying,
            "rate_semantics": [s.as_dict() for s in self.rate_scores],
            "day_count_comparison": [s.as_dict() for s in self.day_count_scores],
            "expiration_time_comparison": [
                s.as_dict() for s in self.expiration_time_scores
            ],
            "iv_basis_comparison": [
                {
                    "basis": basis,
                    "rows": rows,
                    "median_abs_iv_error": median,
                    "within_half_tick_ratio": within,
                }
                for basis, rows, median, within in self.iv_basis_scores
            ],
            "vendor_clock_by_expiration": [r.as_dict() for r in self.clock_readings],
            "expiration_clock_evidence": self.clock_evidence.as_dict(),
            "rate_economics": self.rate_economics.as_dict(),
            "capture_request": self.request.as_dict(),
            "resolved_day_count": self.resolved_day_count,
            "resolved_expiration_clock": self.resolved_expiration_clock,
            "rows_reconstructed": self.rows_reconstructed,
            "vendor_behavior": self.ledger.as_dict(),
            "documentation_live_conflicts": [
                o.dimension.value for o in self.ledger.conflicts
            ],
            "behavior_dimensions_resolved": list(self.resolved_dimensions),
            "behavior_dimensions_unresolved": list(self.unresolved_dimensions),
            "pricing_dimensions_supported_by_evidence": list(
                self.pricing_dimensions_supported
            ),
            "pricing_dimensions_still_unresolved": list(
                self.pricing_dimensions_unresolved
            ),
            "unbound_documentary_claims": [
                dict(claim) for claim in UNBOUND_DOCUMENTARY_CLAIMS
            ],
            "inference_decisions": dict(sorted(self.decisions.items())),
            "inference_models": self.models.as_dict(),
            "rate_day_count_grid": [dict(entry) for entry in self.joint_grid],
            "root_resolution": self.roots.as_dict(),
            "rate_intent_binding": self.rate_intent_binding,
            "documentary_evidence": self.documentation,
            "analytical_readiness": self.analytical_readiness,
            "trusted_for_gex": self.trusted_for_gex,
            "gex_blockers": list(self.gex_blockers),
        }

    def report_hash(self) -> str:
        from src.domain.digests import digest_of

        return digest_of(self.as_dict())


def _rate_dimension() -> Any:
    from src.adapters.thetadata.live_behavior import BehaviorDimension

    return BehaviorDimension.RATE_UNITS


def _status_for(decision: Any, *, observed: str, documented: str) -> Any:
    """Turn a decision plus two readings into an evidence status.

    An undiscriminated comparison produces ``UNRESOLVED`` whatever its best
    entry was. v2.1.23 went straight from ``min()`` to
    ``DOCUMENTATION_LIVE_CONFLICT``, so a capture that established nothing about
    the rate still recorded a conflict about it.
    """
    from src.adapters.thetadata.live_behavior import EvidenceStatus

    if not decision.is_resolved or not observed:
        return EvidenceStatus.UNRESOLVED
    if not documented:
        return EvidenceStatus.LIVE_ONLY
    return (
        EvidenceStatus.DOCUMENTATION_LIVE_CONFLICT
        if observed != documented
        else EvidenceStatus.DOCUMENTATION_LIVE_AGREE
    )


def _documented_rate_unit(capture: LoadedCapture) -> str:
    """How the capture's *own* pinned document reads ``rate_value``.

    v2.1.23 compared every live finding against a documented value written into
    the certification code: ``documented_rate_unit = "PERCENT_ANNUAL_RATE"``.
    That constant describes the document as it reads today, and it was being
    used to establish what a capture taken months earlier had intended.

    v2.1.24 replaced the constant with the capture's own recorded extraction,
    which fixed the anachronism and introduced a softer version of the same
    problem: the recorded value was believed. Editing that one string in
    ``run-intent.json`` moved the first capture's recovered intent by a factor
    of a hundred and made its documentation conflict vanish.

    So this reads the value the pinned document *re-derives to* on this run,
    through a bundle whose fingerprint had to match the manifest-bound approval.
    Empty when the capture carries no documentary evidence -- which is a state,
    not a default.
    """
    if capture.documentation is None:
        return ""
    return capture.documentation.value_for("RATE_UNITS")


#: **Readings this repository holds that no capture is evidence of.**
#:
#: Both entries name a sentence somebody read in vendor material and wrote down
#: here. Neither was ever *extracted* into the pinned bundle as a
#: ``DocumentedRule``, so no capture carries one, so nothing re-derives them and
#: nothing can check them. They are listed rather than deleted because the
#: reading may well be right -- and listed *here* rather than in the ledger
#: because presenting them beside re-derived evidence is how a repository
#: constant comes to look like something a capture proved.
#:
#: ``DIVIDEND_CONVENTION`` joined the list in v2.1.25. Until then the ledger
#: recorded it as ``DOCUMENTATION_ONLY`` with ``documentation_matched=true``,
#: citing "ThetaData greeks article, annual_div" -- an article that is not
#: pinned, not hashed, and not in any capture. It read as settled documentary
#: evidence and was a repository claim wearing that costume.
UNBOUND_DOCUMENTARY_CLAIMS: tuple[dict[str, str], ...] = (
    {
        "dimension": "IV_PRICE_BASIS",
        "claim": "TRADE_PRICE",
        "reference": "components/schemas/first_order_greeks/properties/implied_vol",
        "origin": "REPOSITORY_CONSTANT_NOT_CAPTURE_BOUND",
        "note": (
            "no capture records an IV_PRICE_BASIS extraction, so this reading "
            "cannot be compared against a live finding as documentary evidence. "
            "The live finding stands on its own."
        ),
    },
    {
        "dimension": "DIVIDEND_CONVENTION",
        "claim": "ANNUAL_CASH_DIVIDEND_CONVERTED_TO_YIELD",
        "reference": "ThetaData greeks article, annual_div",
        "origin": "REPOSITORY_CONSTANT_NOT_CAPTURE_BOUND",
        "note": (
            "the pinned document settles OPEN_INTEREST_SETTLEMENT, RATE_UNITS "
            "and MINIMUM_TIME_FLOOR, and nothing else. There is no "
            "DIVIDEND_CONVENTION extraction to re-derive, and the capture "
            "requested annual_dividend=0.0, under which every convention "
            "produces the same zero -- so the capture cannot distinguish them "
            "either. Resolving it would need a pinned extraction or a non-zero "
            "dividend, and this release has neither."
        ),
    },
)


def _documented_iv_basis(capture: LoadedCapture) -> str:
    """What the capture's documentation says about the IV price basis.

    Nothing, so far. No release has extracted an ``IV_PRICE_BASIS`` rule into
    the pinned bundle, so no capture carries one, and the live finding is
    ``LIVE_ONLY`` rather than a conflict with a claim the capture never made.
    See :data:`UNBOUND_DOCUMENTARY_CLAIMS`.
    """
    if capture.documentation is None:
        return ""
    return capture.documentation.value_for("IV_PRICE_BASIS")


def certify_capture(
    root: pathlib.Path | str,
    *,
    archive_path: pathlib.Path | str | None = None,
    archive_sha256: str = "",
) -> CaptureCertificationReport:
    """Derive every vendor convention this capture can settle. No network.

    ``archive_path`` is hashed here. ``archive_sha256`` on its own is recorded
    as an unverified external claim and never as known identity -- sixty-four
    hex characters from a caller are not evidence that any archive has them.
    """
    from src.adapters.thetadata.live_behavior import (
        BehaviorDimension,
        CaptureIdentity,
        EvidenceStatus,
        LiveBehaviorObservation,
        ObservationBasis,
        ReconstructionMetric,
        VendorBehaviorLedger,
    )

    capture = load_capture(root)
    quote = capture.rows(OPTION_QUOTE)
    greeks = capture.rows(OPTION_GREEKS)
    oi_rows = capture.rows(OPTION_OPEN_INTEREST)
    listing = capture.rows(OPTION_CONTRACT_LIST)
    index = capture.rows(INDEX_PRICE)

    # -- universe -----------------------------------------------------------
    list_identities, list_set = _identities(OPTION_CONTRACT_LIST, listing)
    quote_identities, quote_set = _identities(OPTION_QUOTE, quote)
    greeks_identities, greeks_set = _identities(OPTION_GREEKS, greeks)
    oi_identities, _oi_set = _identities(OPTION_OPEN_INTEREST, oi_rows)
    snapshots = quote_set | greeks_set
    universe = UniverseCertification(
        identities=(
            list_identities,
            quote_identities,
            greeks_identities,
            oi_identities,
        ),
        contract_list_count=len(listing),
        quote_count=len(quote),
        greeks_count=len(greeks),
        contract_list_hash=_set_hash(list_set),
        quote_hash=_set_hash(quote_set),
        greeks_hash=_set_hash(greeks_set),
        quote_matches_list=quote_set == list_set,
        greeks_matches_list=greeks_set == list_set,
        only_in_list=tuple(sorted(k.canonical for k in list_set - snapshots)),
        only_in_snapshots=tuple(sorted(k.canonical for k in snapshots - list_set)),
    )

    # -- open interest ------------------------------------------------------
    if oi_identities.duplicate_identity_count:
        raise CaptureCertificationError(
            f"the open-interest response returns "
            f"{oi_identities.duplicate_identity_count} identities more than "
            "once. Open interest is the linear weight on every GEX term, and "
            "two rows for one contract do not say which weight it carries."
        )
    oi_by_key: dict[ContractKey, int] = {}
    for row in oi_rows:
        # **Exact.** ``int(float("12.5"))`` is 12, which is a different open
        # interest quietly substituted for an unparseable one. A contract count
        # is an integer or the response is not what this parser thinks it is.
        text = str(row.get("open_interest", "")).strip().strip('"')
        try:
            value = int(text)
        except ValueError:
            raise CaptureCertificationError(
                f"open_interest {text!r} is not an integer. A fractional "
                "contract count is not a thing the vendor can send, and "
                "truncating it would substitute a different number for an "
                "unreadable one."
            ) from None
        if value < 0:
            raise CaptureCertificationError(
                f"open_interest {value} is negative; that is a parse failure, "
                "not a small position"
            )
        oi_by_key[ContractKey.from_row(row)] = value
    # Both differences, because the sets can disagree in both directions and
    # only one of them was being measured. ``unexpected`` is never discarded:
    # an open-interest row for a contract the listing does not name means the
    # two endpoints disagree about the universe, which is a finding.
    missing = list_set - set(oi_by_key)
    unexpected = set(oi_by_key) - list_set
    explicit_zero = sum(1 for v in oi_by_key.values() if v == 0)
    listed_by_exp: dict[str, int] = defaultdict(int)
    missing_by_exp: dict[str, int] = defaultdict(int)
    unexpected_by_exp: dict[str, int] = defaultdict(int)
    for key in list_set:
        listed_by_exp[key.expiration.isoformat()] += 1
    for key in missing:
        missing_by_exp[key.expiration.isoformat()] += 1
    for key in unexpected:
        unexpected_by_exp[key.expiration.isoformat()] += 1
    coverage = OpenInterestCoverage(
        universe_count=len(list_set),
        present_count=len(oi_by_key) - explicit_zero,
        explicit_zero_count=explicit_zero,
        missing_count=len(missing),
        missing_by_expiration=tuple(
            (e, listed_by_exp[e], missing_by_exp[e]) for e in sorted(missing_by_exp)
        ),
        missing_identities_hash=_set_hash(missing),
        fully_missing_expirations=tuple(
            e for e in sorted(missing_by_exp) if missing_by_exp[e] == listed_by_exp[e]
        ),
        unexpected_count=len(unexpected),
        unexpected_identities_hash=_set_hash(unexpected),
        unexpected_by_expiration=tuple(
            (e, unexpected_by_exp[e]) for e in sorted(unexpected_by_exp)
        ),
        duplicate_count=oi_identities.duplicate_identity_count,
    )

    # -- quote/greeks synchrony --------------------------------------------
    quote_by_key = {ContractKey.from_row(r): r for r in quote}
    gaps: list[float] = []
    same_ts = same_bid = same_ask = 0
    for row in greeks:
        other = quote_by_key.get(ContractKey.from_row(row))
        if other is None:
            continue
        if other["timestamp"].strip() == row["timestamp"].strip():
            same_ts += 1
        try:
            if abs(float(other["bid"]) - float(row["bid"])) < 1e-9:
                same_bid += 1
            if abs(float(other["ask"]) - float(row["ask"])) < 1e-9:
                same_ask += 1
            gaps.append(
                abs(
                    (
                        _parse_et(other["timestamp"]) - _parse_et(row["timestamp"])
                    ).total_seconds()
                )
            )
        except (KeyError, ValueError):
            continue
    gaps.sort()
    n_gap = len(gaps) or 1
    synchrony = EndpointSynchrony(
        overlap=len(gaps),
        identical_timestamp_ratio=same_ts / n_gap,
        identical_bid_ratio=same_bid / n_gap,
        identical_ask_ratio=same_ask / n_gap,
        p99_gap_seconds=gaps[min(int(0.99 * n_gap), n_gap - 1)] if gaps else 0.0,
        max_gap_seconds=max(gaps) if gaps else 0.0,
    )

    # -- underlying ---------------------------------------------------------
    embedded_prices = sorted({r["underlying_price"].strip() for r in greeks})
    embedded_times = sorted({r["underlying_timestamp"].strip() for r in greeks})
    if len(embedded_prices) != 1 or len(embedded_times) != 1:
        raise CaptureCertificationError(
            f"the Greeks response carries {len(embedded_prices)} distinct "
            f"underlying prices and {len(embedded_times)} timestamps. This "
            "certification reconstructs one snapshot against one underlying "
            "state; several would need a per-row reconstruction instead."
        )
    spot = float(embedded_prices[0])
    valuation = _parse_et(embedded_times[0])
    index_price = float(index[0]["price"]) if index else float("nan")
    index_time = index[0]["timestamp"].strip() if index else ""
    underlying = {
        "vendor_greeks_underlying_price": spot,
        "vendor_greeks_underlying_timestamp": embedded_times[0],
        "index_snapshot_price": index_price,
        "index_snapshot_timestamp": index_time,
        "price_difference": round(index_price - spot, 10),
        "synchronized": index_price == spot and index_time == embedded_times[0],
        "authoritative_for_vendor_model": "GREEKS_RESPONSE_EMBEDDED_VENDOR_UNDERLYING",
    }

    # -- the vendor's rate and clock, inferred together ---------------------
    #
    # These cannot be resolved in sequence. Reading the clock out of the
    # vendor's deltas needs a rate, and scoring a rate needs the regime
    # boundary the clock produces. v2.1.22 broke the loop by hard-coding 4.2 --
    # which is the value the first capture happened to send, so certification
    # silently assumed its own conclusion and would misread any other capture.
    #
    # The loop is broken properly by reconstructing each candidate rate all the
    # way through and keeping the one that reproduces the vendor's own numbers.
    # The candidates come from the wire value this capture is *proved* to have
    # sent: a decimal reading uses it unchanged, a percent reading divides by a
    # hundred. Nothing here knows what that value is.
    usable = _usable_greeks(greeks)
    wire_rate = capture.request.greeks_rate_value
    rate_candidates = (
        ("DECIMAL_ANNUAL_RATE", wire_rate),
        ("PERCENT_ANNUAL_RATE", wire_rate / 100.0),
    )

    def reconstruct(rate: float, basis: float) -> _Reconstruction | None:
        """Invert, split the regimes, derive the clock and score -- as one."""
        readings, roots = _invert_clock(
            usable, spot=spot, valuation=valuation, rate=rate, days_per_year=basis
        )
        evidence = _clock_evidence(readings, valuation)
        edge = (
            date.fromisoformat(evidence.boundary_last_intraday)
            if evidence.boundary_last_intraday
            else None
        )
        fit = _score(
            usable,
            spot=spot,
            valuation=valuation,
            rate=rate,
            days_per_year=basis,
            expiry_time=_clock_time(evidence),
            whole_days_from=edge,
            label="",
        )
        if fit is None:
            return None
        return _Reconstruction(
            rate=rate,
            days_per_year=basis,
            readings=readings,
            evidence=evidence,
            boundary=edge,
            roots=roots,
            rows=fit.rows,
            median_abs_delta_error=fit.median_abs_delta_error,
            delta_rmse=fit.delta_rmse,
        )

    # **Searched jointly, because the dimensions are not independent.** The
    # inversion needs a rate *and* a denominator before it can say anything
    # about the clock, and the clock decides which expirations are in which
    # regime. Resolving them in sequence works only when the first guess is
    # already right -- which is what hard-coding 4.2 and 365 quietly assumed.
    grid = {
        (unit, label): outcome
        for unit, rate in rate_candidates
        for basis, label in DAY_COUNT_HYPOTHESES
        if (outcome := reconstruct(rate, basis)) is not None
    }
    if not grid:
        raise CaptureCertificationError(
            "no rate and day-count hypothesis could be scored against this "
            "capture; the Greeks rows carry no usable implied volatilities or "
            "deltas"
        )
    (observed_rate_unit, day_count_label), winner = min(
        grid.items(), key=lambda item: item[1].delta_rmse
    )
    effective_rate = winner.rate
    readings = winner.readings
    clock_evidence = winner.evidence
    # No ``boundary``/``clock`` unpacked here any more. They were the winner's,
    # and every downstream computation reached for them regardless of whether
    # the winner had been established -- which is precisely how an undecided
    # upstream choice became silent authority. The regime split and the close
    # now travel with the path being evaluated, inside the two functions below.

    # The two published tables are slices through that grid, relabelled for
    # the dimension each one is about: rates compared at the winning day count,
    # day counts compared at the winning rate. Nothing is recomputed, so a
    # table can never disagree with the winner it was drawn from.
    rate_scores = tuple(
        grid[(unit, day_count_label)].labelled(
            f"rate_value consumed as {unit} (r={rate:g})"
        )
        for unit, rate in rate_candidates
        if (unit, day_count_label) in grid
    )
    day_count_scores = tuple(
        grid[(observed_rate_unit, label)].labelled(label)
        for _basis, label in DAY_COUNT_HYPOTHESES
        if (observed_rate_unit, label) in grid
    )

    def expiration_scores_for(entry: _Reconstruction) -> tuple[HypothesisScore, ...]:
        """Score the closing clock along one upstream path.

        Candidates built around that path's own derived clock rather than a
        fixed list, so the comparison is informative for any close the vendor
        might use and the winner cannot be right by coincidence of the list.
        """
        edge = entry.boundary
        front = [
            r
            for r in usable
            if edge is not None
            and date.fromisoformat(r["expiration"].strip().strip('"')) <= edge
        ]
        return tuple(
            s
            for s in (
                _score(
                    front,
                    spot=spot,
                    valuation=valuation,
                    rate=entry.rate,
                    # This path's denominator, not a constant. Scoring a clock
                    # at the wrong day count lets the fit trade one against the
                    # other -- an ACT/360 vendor read at 365 is short on time,
                    # and the search buys it back by moving the close later.
                    days_per_year=entry.days_per_year,
                    expiry_time=candidate,
                    label=(
                        f"{candidate.hour:02d}:{candidate.minute:02d} America/New_York"
                    ),
                )
                for candidate in _clock_candidates(_clock_time(entry.evidence))
            )
            if s is not None
        )

    def iv_scores_for(
        entry: _Reconstruction,
    ) -> tuple[list[tuple[str, int, float, float]], bool]:
        """Score the IV price basis along one upstream path.

        A zero bid/ask spread makes BID, ASK and the midpoint the same number,
        so every hypothesis reprices identically and none is established.
        Counted rather than assumed: a capture where the book happens to be
        locked cannot prove the vendor chose the midpoint specifically.
        """
        edge, clock_at = entry.boundary, _clock_time(entry.evidence)
        zero_spread = comparable = 0
        for row in usable:
            try:
                if 0.05 <= abs(float(row["delta"])) <= 0.95:
                    comparable += 1
                    if float(row["ask"]) - float(row["bid"]) == 0.0:
                        zero_spread += 1
            except (KeyError, ValueError):
                continue
        degenerate = comparable > 0 and zero_spread == comparable

        scores: list[tuple[str, int, float, float]] = []
        for basis_name in ("NBBO_MID", "BID", "ASK"):
            errors: list[float] = []
            within = 0
            for row in usable:
                key = ContractKey.from_row(row)
                if not (0.05 <= abs(float(row["delta"])) <= 0.95):
                    continue
                exp_d = key.expiration
                if edge is not None and exp_d > edge:
                    days = float((exp_d - valuation.date()).days)
                else:
                    days = (
                        datetime.combine(exp_d, clock_at, tzinfo=EASTERN) - valuation
                    ).total_seconds() / 86400.0
                if days <= 0.0:
                    continue
                try:
                    bid, ask = float(row["bid"]), float(row["ask"])
                except (KeyError, ValueError):
                    continue
                target = {"NBBO_MID": (bid + ask) / 2.0, "BID": bid, "ASK": ask}[
                    basis_name
                ]
                solved = _solve_iv(
                    target,
                    spot=spot,
                    strike=float(row["strike"]),
                    years=days / entry.days_per_year,
                    rate=entry.rate,
                    right=key.option_right,
                )
                if solved is None:
                    continue
                err = abs(solved - float(row["implied_vol"]))
                errors.append(err)
                if err <= IMPLIED_VOL_TICK / 2:
                    within += 1
            if errors:
                scores.append(
                    (
                        basis_name,
                        len(errors),
                        statistics.median(errors),
                        within / len(errors),
                    )
                )
        return scores, degenerate

    # -- the ledger ---------------------------------------------------------
    archive = _archive_identity(
        archive_path=archive_path,
        archive_sha256=archive_sha256,
        capture=capture,
    )
    identity = CaptureIdentity(
        session_id=capture.session_id,
        manifest_hash=capture.manifest_hash,
        # Only a digest this process computed reaches the observation identity.
        # An unverified caller claim is reported beside the capture, never as
        # the thing an observation is traceable to.
        archive_sha256=archive.sha256 if archive.known else "",
    )
    best_rate = min(rate_scores, key=lambda s: s.delta_rmse) if rate_scores else None
    best_day = (
        min(day_count_scores, key=lambda s: s.delta_rmse) if day_count_scores else None
    )
    documented_rate_unit = _documented_rate_unit(capture)
    documented_iv_basis = _documented_iv_basis(capture)

    # -- did any of this actually settle anything? --------------------------
    #
    # Every table above has a smallest entry. Only some of them mean something,
    # and v2.1.23 could not tell the difference: with ``rate_value=0`` both rate
    # hypotheses are ``r = 0``, score identically to the last bit, and it
    # reported a documentation conflict on the strength of tuple ordering.
    rate_decision = _decide(
        sorted(s.delta_rmse for s in rate_scores),
        rows=best_rate.rows if best_rate else 0,
        # The two readings of a zero wire value are the same computation. No
        # quantity of rows separates them, because there is nothing between.
        identical_hypotheses=(wire_rate == wire_rate / 100.0),
    )
    day_count_decision = _decide(
        sorted(s.delta_rmse for s in day_count_scores),
        rows=best_day.rows if best_day else 0,
        identical_hypotheses=False,
    )

    # -- downstream, across every upstream path still admissible ------------
    #
    # v2.1.24 derived the clock and the IV basis from the global minimum and
    # only afterwards asked whether the rate or the day count had been settled
    # at all. When the answer was no, the downstream conclusions had already
    # been computed from one arbitrary member of an undecided set, and were
    # published as findings. An unresolved winner was hidden authority.
    #
    # So each surviving path is carried all the way through, and a downstream
    # dimension resolves only when every one of them lands on the same answer.
    admissible = _admissible(
        grid,
        best=(observed_rate_unit, day_count_label),
        rate_decision=rate_decision,
        day_count_decision=day_count_decision,
    )
    paths = {key: grid[key] for key in admissible.keys}
    exp_by_path = {key: expiration_scores_for(entry) for key, entry in paths.items()}
    iv_by_path = {key: iv_scores_for(entry) for key, entry in paths.items()}

    def _per_path_decision(
        key: tuple[str, str],
    ) -> tuple[InferenceDecision, str, InferenceDecision, str]:
        """The two downstream verdicts along one upstream path."""
        scores = exp_by_path[key]
        top = min(scores, key=lambda s: s.delta_rmse) if scores else None
        exp_dec = _decide(
            sorted(s.delta_rmse for s in scores),
            rows=top.rows if top else 0,
            identical_hypotheses=False,
        )
        iv_rows, degenerate = iv_by_path[key]
        iv_top = min(iv_rows, key=lambda s: s[2]) if iv_rows else None
        iv_dec = _decide(
            sorted(row[2] for row in iv_rows),
            rows=iv_top[1] if iv_top else 0,
            identical_hypotheses=degenerate,
            floor=IMPLIED_VOL_HALF_QUANTUM,
            # A solved volatility cannot land closer than half a reporting
            # quantum to the reported one, so adequacy is measured against that
            # and not against the delta noise floor, which is a different field.
            adequate=ADEQUATE_FIT_NOISE_FLOORS * IMPLIED_VOL_HALF_QUANTUM,
        )
        return (
            exp_dec,
            top.hypothesis if top and exp_dec.is_resolved else "",
            iv_dec,
            iv_top[0] if iv_top and iv_dec.is_resolved else "",
        )

    per_path = {key: _per_path_decision(key) for key in admissible.keys}

    def _agreed(index: int) -> tuple[InferenceDecision, str]:
        return _collapse(
            tuple(
                (per_path[key][index], per_path[key][index + 1])
                for key in admissible.keys
            )
        )

    expiration_decision, agreed_expiration = _agreed(0)
    iv_decision, agreed_iv_basis = _agreed(2)

    # The published tables are the numerical-best path's, so a reader sees real
    # numbers; the verdicts above are the cross-path ones. Where they differ,
    # the grid and ``inference_models`` show why.
    expiration_time_scores = exp_by_path[admissible.numerical_best]
    iv_basis_scores, _degenerate = iv_by_path[admissible.numerical_best]
    best_exp = (
        min(expiration_time_scores, key=lambda s: s.delta_rmse)
        if expiration_time_scores
        else None
    )
    best_iv = min(iv_basis_scores, key=lambda s: s[2]) if iv_basis_scores else None

    # The observed unit is a *finding* only when the comparison discriminated.
    # Everything economic hangs off it, so it is emptied here once rather than
    # guarded at each of the four places that used to read it regardless.
    observed_unit = observed_rate_unit if rate_decision.is_resolved else ""
    rate_status = _status_for(
        rate_decision, observed=observed_unit, documented=documented_rate_unit
    )
    rate_economics = _rate_economics(
        capture,
        wire_rate=wire_rate,
        # ``None``, not the minimum's rate. With the reading undecided there is
        # no established effective rate, and reporting the better-fitting of two
        # indistinguishable candidates as *the* rate the vendor priced with is
        # the report contradicting its own inference decision.
        effective_rate=effective_rate if rate_decision.is_resolved else None,
        observed_unit=observed_unit,
        documented_unit=documented_rate_unit,
    )
    observed_iv_basis = agreed_iv_basis
    iv_status = _status_for(
        iv_decision, observed=observed_iv_basis, documented=documented_iv_basis
    )
    # Labels come from the winning hypothesis, and only when the comparison
    # actually discriminated. An unresolved dimension gets the decision's own
    # name rather than the best of an indistinguishable set.
    day_count_token = (
        DAY_COUNT_TOKENS.get(best_day.hypothesis, best_day.hypothesis)
        if best_day and day_count_decision.is_resolved
        else day_count_decision.value
    )
    expiration_label = (
        agreed_expiration
        if expiration_decision.is_resolved
        else expiration_decision.value
    )
    # The whole search, published. The dimension tables above are slices, and a
    # slice cannot show that some *other* rate/day-count pair fits almost as
    # well as the selected one -- which is exactly what an auditor deciding
    # whether to trust the selection needs to see.
    joint_grid = tuple(
        {
            "rate_interpretation": unit,
            "effective_rate": grid[(unit, label)].rate,
            "day_count": label,
            "rows": grid[(unit, label)].rows,
            "median_abs_delta_error": grid[(unit, label)].median_abs_delta_error,
            "delta_rmse": grid[(unit, label)].delta_rmse,
            "selected": unit == observed_rate_unit and label == day_count_label,
        }
        for unit, _rate in rate_candidates
        for _basis, label in DAY_COUNT_HYPOTHESES
        if (unit, label) in grid
    )

    observations = [
        LiveBehaviorObservation(
            dimension=BehaviorDimension.RATE_UNITS,
            status=rate_status,
            basis=ObservationBasis.LIVE_NUMERICAL_RECONSTRUCTION,
            documented_value=documented_rate_unit,
            observed_value=observed_unit,
            decision=rate_decision,
            documentation_reference="components/parameters/rate_value/description",
            capture=identity,
            rows_used=best_rate.rows if best_rate else 0,
            scope="/v3/option/snapshot/greeks/first_order, greeks_version=latest",
            metrics=tuple(
                ReconstructionMetric(
                    hypothesis=s.hypothesis,
                    statistic="delta_rmse",
                    value=s.delta_rmse,
                    rows=s.rows,
                    selected=s is best_rate,
                )
                for s in rate_scores
            ),
            notes=(
                (
                    "The document says percent and the implementation reads a "
                    "decimal. Both records are kept; the observed reading "
                    "governs request construction because the request is "
                    "answered by the implementation."
                )
                if rate_status.is_conflict
                else (
                    "The implementation reads rate_value as documented."
                    if rate_decision.is_resolved
                    # Saying it "reads rate_value as documented" was the else
                    # branch of a two-way test, so an undiscriminated comparison
                    # produced a confident statement of agreement about the
                    # thing it had just failed to determine.
                    else (
                        f"This capture does not establish how the vendor reads "
                        f"rate_value: the comparison came out "
                        f"{rate_decision.value}. Nothing here says the "
                        "implementation agrees with the documentation, or that "
                        "it disagrees."
                    )
                )
            ),
        ),
        LiveBehaviorObservation(
            dimension=BehaviorDimension.DAY_COUNT,
            status=_status_for(
                day_count_decision, observed=day_count_token, documented=""
            ),
            basis=ObservationBasis.LIVE_NUMERICAL_RECONSTRUCTION,
            observed_value=day_count_token if day_count_decision.is_resolved else "",
            decision=day_count_decision,
            capture=identity,
            rows_used=best_day.rows if best_day else 0,
            scope="SPXW first-order greeks in this capture",
            metrics=tuple(
                ReconstructionMetric(
                    hypothesis=s.hypothesis,
                    statistic="delta_rmse",
                    value=s.delta_rmse,
                    rows=s.rows,
                    selected=s is best_day,
                )
                for s in day_count_scores
            ),
        ),
        LiveBehaviorObservation(
            dimension=BehaviorDimension.EXPIRATION_TIMESTAMP,
            status=_status_for(
                expiration_decision, observed=expiration_label, documented=""
            ),
            basis=ObservationBasis.LIVE_NUMERICAL_RECONSTRUCTION,
            observed_value=agreed_expiration,
            decision=expiration_decision,
            capture=identity,
            rows_used=best_exp.rows if best_exp else 0,
            scope=(
                "SPXW expirations up to and including "
                f"{clock_evidence.boundary_last_intraday or 'n/a'}. Beyond that "
                f"boundary {clock_evidence.contradicting_count} expirations in "
                "this capture price on whole calendar days instead, so this "
                "rule is NOT established for longer maturities and must not be "
                "applied to them. The exact transition is "
                f"{clock_evidence.boundary_status}: the nearest contradicting "
                f"expiration is {clock_evidence.boundary_first_whole_day or 'n/a'}, "
                f"{clock_evidence.boundary_gap_days} calendar days later, and "
                "nothing was captured in between."
            ),
            metrics=tuple(
                ReconstructionMetric(
                    hypothesis=s.hypothesis,
                    statistic="delta_rmse",
                    value=s.delta_rmse,
                    rows=s.rows,
                    selected=s is best_exp,
                )
                for s in expiration_time_scores
            ),
            notes=(
                "Read by inverting delta and implied volatility for the vendor's "
                "own time-to-expiry, not by scoring a global hypothesis. The "
                "structured scope is in expiration_clock_evidence; this string "
                "is a summary of it and never the only copy."
            ),
        ),
        LiveBehaviorObservation(
            dimension=BehaviorDimension.IV_PRICE_BASIS,
            # Derived for the same reason the rate's status is: a hard-coded
            # conflict cannot report a vendor that stopped conflicting, and it
            # made exit code 3 unreachable-by-construction rather than earned.
            status=iv_status,
            basis=ObservationBasis.LIVE_NUMERICAL_RECONSTRUCTION,
            documented_value=documented_iv_basis,
            observed_value=observed_iv_basis,
            decision=iv_decision,
            documentation_reference=(
                "components/schemas/first_order_greeks/properties/implied_vol"
            ),
            capture=identity,
            rows_used=best_iv[1] if best_iv else 0,
            scope="SPXW first-order greeks in this capture",
            metrics=tuple(
                ReconstructionMetric(
                    hypothesis=name,
                    statistic="median_abs_iv_error",
                    value=median,
                    rows=rows,
                    selected=best_iv is not None and name == best_iv[0],
                )
                for name, rows, median, _within in iv_basis_scores
            ),
            notes=(
                "The residual under NBBO mid is half the reporting tick of "
                "implied_vol, which is the floor -- there is nothing left to "
                "explain. Bid and ask are two orders of magnitude worse."
            ),
        ),
        LiveBehaviorObservation(
            dimension=BehaviorDimension.UNDERLYING_SOURCE,
            status=EvidenceStatus.LIVE_ONLY,
            basis=ObservationBasis.LIVE_FIELD_READ,
            observed_value="GREEKS_RESPONSE_EMBEDDED_VENDOR_UNDERLYING",
            capture=identity,
            rows_used=len(greeks),
            scope="reproduction of the vendor's own model",
            notes=(
                f"The Greeks response carries one underlying print ({spot} at "
                f"{embedded_times[0]}). The separately captured index snapshot "
                f"returned {index_price} at {index_time}, which is a different "
                "state. The index response is not what these Greeks were "
                "computed from and must not be described as synchronized."
            ),
        ),
        LiveBehaviorObservation(
            dimension=BehaviorDimension.UNDERLYING_TIMESTAMP,
            status=EvidenceStatus.LIVE_ONLY,
            basis=ObservationBasis.LIVE_FIELD_READ,
            observed_value="GREEKS_RESPONSE_EMBEDDED_UNDERLYING_TIMESTAMP",
            decision=InferenceDecision.RESOLVED,
            capture=identity,
            rows_used=len(greeks),
            scope="reproduction of the vendor's own model",
            notes=(
                f"The Greeks response stamps one underlying instant "
                f"({embedded_times[0]}) across every row. The separately "
                f"captured index snapshot is stamped {index_time}, so the two "
                "are different observations of the underlying and the "
                "embedded one is what these Greeks were computed against. "
                "Recorded as its own dimension because the analytical "
                "compatibility layer treats source and timestamp separately, "
                "and until v2.1.24 this ledger carried only the source -- so "
                "an empty unresolved list read as though every pricing "
                "dimension were settled."
            ),
        ),
        LiveBehaviorObservation(
            dimension=BehaviorDimension.DIVIDEND_CONVENTION,
            # **Unresolved, and it always was.** v2.1.24 recorded this as
            # DOCUMENTATION_ONLY with documentation_matched=true, citing an
            # article that is not pinned, not hashed and not carried by any
            # capture. That is the same defect the IV price basis had, and it
            # gets the same answer: the claim moves to
            # UNBOUND_DOCUMENTARY_CLAIMS and the dimension stays open.
            status=EvidenceStatus.UNRESOLVED,
            basis=ObservationBasis.REQUEST_BOUND,
            decision=InferenceDecision.NOT_IDENTIFIABLE,
            scope=(
                "annual_dividend=0.0 was requested, so every dividend "
                "convention produces the same zero and none is distinguishable "
                "from the others by this capture"
            ),
            notes=(
                "No pinned documentary extraction establishes what "
                "annual_dividend means to the vendor, and a zero-dividend "
                "request cannot establish it either. Resolving this needs an "
                "extraction in the pinned bundle or a capture with a non-zero "
                "dividend; see unbound_documentary_claims."
            ),
        ),
        LiveBehaviorObservation(
            dimension=BehaviorDimension.DIVIDEND_VALUE,
            # Separate from the convention, and genuinely settled: the value is
            # in the request binding, which was recomputed against the digest
            # stamped on the manifest records. What it *means* is the other
            # dimension's question, and that one is open.
            status=EvidenceStatus.LIVE_ONLY,
            basis=ObservationBasis.REQUEST_BOUND,
            observed_value=f"{capture.request.greeks_annual_dividend:g}",
            decision=InferenceDecision.RESOLVED,
            capture=identity,
            rows_used=len(greeks),
            scope="the verified greeks request for this capture",
            notes=(
                "The dividend amount the request carried, proved by the same "
                "binding that proves the rate value. Its interpretation is "
                "DIVIDEND_CONVENTION, which this capture does not settle."
            ),
        ),
        LiveBehaviorObservation(
            dimension=BehaviorDimension.RISK_FREE_RATE,
            # Needs *both*: an approval-bound declaration of what rate was
            # meant, and a live reading that says the vendor consumed it that
            # way. A declaration alone is a statement about our own request.
            status=(
                EvidenceStatus.LIVE_ONLY
                if capture.bound_rate_intent is not None
                and rate_economics.vendor_rate_identified
                else EvidenceStatus.UNRESOLVED
            ),
            basis=ObservationBasis.REQUEST_BOUND,
            observed_value=(
                f"{rate_economics.intended_economic_rate:g}"
                if capture.bound_rate_intent is not None
                and rate_economics.vendor_rate_identified
                else ""
            ),
            decision=(
                InferenceDecision.RESOLVED
                if capture.bound_rate_intent is not None
                and rate_economics.vendor_rate_identified
                else InferenceDecision.INSUFFICIENT_DATA
            ),
            capture=identity,
            rows_used=len(greeks) if capture.bound_rate_intent is not None else 0,
            scope="the economic rate this capture was approved to buy",
            notes=(
                "Established by an approval-bound rate intent read together "
                "with a resolved live rate reading. A capture with only one of "
                "the two leaves this open: an intent nobody checked against "
                "the vendor is a declaration, and a reading with no declared "
                "intent has nothing to be the rate *of*."
            ),
        ),
        *(
            [
                LiveBehaviorObservation(
                    dimension=BehaviorDimension.MINIMUM_TIME_FLOOR,
                    status=EvidenceStatus.DOCUMENTATION_ONLY,
                    basis=ObservationBasis.PINNED_DOCUMENTATION,
                    documented_value=capture.documentation.value_for(
                        "MINIMUM_TIME_FLOOR"
                    ),
                    decision=InferenceDecision.RESOLVED,
                    documentation_reference=(
                        "components/parameters/greeks_version/description"
                    ),
                    scope=(
                        "greeks_version=latest, which is what this capture requested"
                    ),
                    notes=(
                        "Re-derived from the pinned document's bytes on this "
                        "run, through a bundle whose fingerprint matched the "
                        "manifest-bound approval. Documentary, not live: no "
                        "row in this capture sits close enough to expiry to "
                        "exercise the floor."
                    ),
                )
            ]
            if capture.documentation is not None
            and capture.documentation.value_for("MINIMUM_TIME_FLOOR")
            else []
        ),
        LiveBehaviorObservation(
            dimension=BehaviorDimension.CONTRACT_LIST_UNIVERSE,
            status=EvidenceStatus.LIVE_ONLY,
            basis=ObservationBasis.LIVE_SET_COMPARISON,
            observed_value=universe.state,
            capture=identity,
            rows_used=universe.contract_list_count,
            scope=(
                "SPXW, session 2026-08-10, max_dte=60, quote and first-order "
                "greeks snapshots only"
            ),
            notes=(
                f"contract list {universe.contract_list_hash[:16]}, quote "
                f"{universe.quote_hash[:16]}, greeks {universe.greeks_hash[:16]}"
            ),
        ),
    ]

    return CaptureCertificationReport(
        schema_version=CAPTURE_CERTIFICATION_SCHEMA_VERSION,
        session_id=capture.session_id,
        manifest_hash=capture.manifest_hash,
        archive=archive,
        captured_at=capture.captured_at,
        parser_version=capture.parser_version,
        verified_records=capture.verified_records,
        record_hashes=capture.record_hashes,
        universe=universe,
        open_interest=coverage,
        synchrony=synchrony,
        underlying=underlying,
        rate_scores=rate_scores,
        day_count_scores=day_count_scores,
        expiration_time_scores=expiration_time_scores,
        iv_basis_scores=tuple(iv_basis_scores),
        clock_readings=tuple(readings),
        clock_evidence=clock_evidence,
        rate_economics=rate_economics,
        request=capture.request,
        ledger=VendorBehaviorLedger(observations=tuple(observations)),
        rows_reconstructed=len(usable),
        resolved_day_count=day_count_token,
        resolved_expiration_clock=expiration_label,
        decisions={
            "RATE_UNITS": rate_decision.value,
            "DAY_COUNT": day_count_decision.value,
            "EXPIRATION_TIMESTAMP": expiration_decision.value,
            "IV_PRICE_BASIS": iv_decision.value,
        },
        joint_grid=joint_grid,
        roots=winner.roots,
        rate_intent_binding={
            "bound": capture.bound_rate_intent is not None,
            "source": rate_economics.intended_rate_source,
            "rate_intent_fingerprint": (
                capture.bound_rate_intent.fingerprint
                if capture.bound_rate_intent is not None
                else ""
            ),
            "capture_records_binding_schema": capture.records_are_post_binding,
            "intent_schema_version": capture.intent_schema_version,
            "rate_intent_schema_version": (
                capture.bound_rate_intent.schema_version
                if capture.bound_rate_intent is not None
                else ""
            ),
        },
        models=admissible,
        documentation=(
            capture.documentation.as_dict()
            if capture.documentation is not None
            else {
                "documentary_authority": "NONE_CARRIED_BY_THIS_CAPTURE",
                "rules_rederived": [],
            }
        ),
    )
